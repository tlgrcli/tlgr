"""The `chat` group: the dialog list, and everything you can do to one dialog.

This is the surface an agent starts from, so three semantics are frozen and
must not drift (AGENT.md says so, and `tests/test_agentmd_compat.py` holds
the line):

* **`chat catchup` and `chat list` emit no read receipts.** They are how you
  find out what happened; finding out must not tell anyone you looked.
* **`chat open` does emit one, deliberately.** It humanises the account and
  clears the owner's own unread badge — which is also why `--no-read` exists
  for chats a person is handling by hand.
* **`chat unread` sets Telegram's manual unread *flag*.** It restores the
  owner's badge; it does not un-send the receipt the other side already got,
  and no command can.

Two implementation notes that shape most of the module. Telegram has no
`getDialogs(filter_id)`, so `--folder <name>` is evaluated client-side
against `messages.getDialogFilters` — a folder is a filter, not a container.
And `peerNotifySettings` is sparse: an omitted field means "inherit the scope
default", so every notification change here is a read-modify-write.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from tlgr.core.errors import (
    EXIT_EMPTY,
    NotSupportedError,
    PermissionError_,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.timefmt import fmt_dt, parse_dt, parse_duration, to_unix
from tlgr.models.base import Request
from tlgr.models.dialog import (
    ActionBar,
    ArchiveResult,
    ArchiveSettings,
    Badge,
    Catchup,
    CatchupChat,
    ChatInfo,
    ChatSwitches,
    ChatTheme,
    ClearResult,
    DeleteChatResult,
    Dialog,
    Folder,
    FolderBadge,
    ImportState,
    LeaveResult,
    MuteResult,
    NotifySettings,
    NotifyView,
    OpenResult,
    PeerResult,
    PinnedDialogs,
    Poster,
    PosterReport,
    Promo,
    ReadChats,
    SavedDialog,
    SecretChat,
    ThemeResult,
    TranslateResult,
    TtlResult,
    TypingResult,
    UnreadResult,
    Wallpaper,
    WallpaperResult,
)
from tlgr.models.message import Message, ReportResult
from tlgr.models.page import Page
from tlgr.models.peer import Peer, PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import (
    action_bar,
    chat_theme,
    entity_to_peer,
    message_to_model,
    notify_settings,
    peer_id_of,
    wallpaper,
)
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `mute_until` for "forever". Telegram's own sentinel is 2^31-1.
MUTE_FOREVER = 2**31 - 1

#: Telegram's peer-folders. There are exactly two and there will not be a
#: third: `folder_id` is 0 (main) or 1 (archive), and everything a user calls
#: a "folder" is a dialog *filter* instead.
FOLDER_MAIN = 0
FOLDER_ARCHIVE = 1

_EXAMPLE_DIALOG: dict[str, Any] = {
    "chat": {"id": 777123, "raw_id": 777123, "kind": "user", "title": "Alice"},
    "unread_count": 3,
    "top_message_id": 4210,
}

_ACTIONS = {
    "typing": "SendMessageTypingAction",
    "cancel": "SendMessageCancelAction",
    "record-audio": "SendMessageRecordAudioAction",
    "record-video": "SendMessageRecordVideoAction",
    "record-round": "SendMessageRecordRoundAction",
    "upload-photo": "SendMessageUploadPhotoAction",
    "upload-video": "SendMessageUploadVideoAction",
    "upload-document": "SendMessageUploadDocumentAction",
    "upload-audio": "SendMessageUploadAudioAction",
    "location": "SendMessageGeoLocationAction",
    "contact": "SendMessageChooseContactAction",
    "sticker": "SendMessageChooseStickerAction",
    "game": "SendMessageGamePlayAction",
    "history-import": "SendMessageHistoryImportAction",
    "speaking": "SpeakingInGroupCallAction",
}

_REPORT_REASONS = {
    "spam": "InputReportReasonSpam",
    "violence": "InputReportReasonViolence",
    "porn": "InputReportReasonPornography",
    "child-abuse": "InputReportReasonChildAbuse",
    "geo-irrelevant": "InputReportReasonGeoIrrelevant",
    "fake": "InputReportReasonFake",
    "copyright": "InputReportReasonCopyright",
    "drugs": "InputReportReasonIllegalDrugs",
    "personal-details": "InputReportReasonPersonalDetails",
    "other": "InputReportReasonOther",
}

#: What every secret-chat command that needs key material answers with.
SECRET_UNSUPPORTED = (
    "Telethon speaks no MTProto-2.0 end-to-end layer, so tlgr cannot hold a "
    "secret chat's keys: DH validation, AES-IGE, in/out sequence numbers, "
    "PFS re-keying and local key storage are a module tlgr does not have yet. "
    "`chat secret discard` works, because discarding needs only the chat id"
)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


def _already(ctx: OpContext) -> None:
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


def _window(ctx: OpContext, op: str, kind: PageKind, default: int = 50) -> tuple[int, Any]:
    """`(limit, cursor state)` — `--limit`/`--cursor` are transport-level (L5)."""
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 1000), state


def _input_channel(peer: Any) -> Any:
    """The `InputChannel` a `channels.*` request wants, or a usage error."""
    from telethon import utils

    try:
        return utils.get_input_channel(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError(
            "this operation only works in a channel or supergroup", field="chat"
        ) from exc


def _is_channel(peer: Any) -> bool:
    return type(peer).__name__ in ("InputPeerChannel", "InputPeerChannelFromMessage")


async def _affected_loop(ctx: OpContext, make_request: Any) -> int:
    """Drive an `AffectedHistory` call until `offset == 0`.

    The server answers a big history with a partial result and an offset to
    resume from. Calling once and reporting success is how "clear the whole
    history" clears the first hundred messages.
    """
    client = _client(ctx)
    total = 0
    offset = 0
    for _ in range(100):
        result = await client(make_request(offset))
        total += int(getattr(result, "pts_count", 0) or 0)
        offset = int(getattr(result, "offset", 0) or 0)
        if offset == 0:
            break
        limiter = getattr(ctx, "limiter", None)
        if limiter is not None:
            await limiter.acquire("bulk")
    return total


def _peer_folder(name: str | None) -> int | None:
    """`main`/`archive`/`all` → the peer-folder id, or None for "not one"."""
    value = (name or "").strip().lower()
    if value in ("", "main", "inbox", "0"):
        return FOLDER_MAIN
    if value in ("archive", "archived", "1"):
        return FOLDER_ARCHIVE
    if value == "all":
        return None
    return None


def _is_peer_folder(name: str | None) -> bool:
    return (name or "").strip().lower() in ("", "main", "inbox", "0", "archive", "archived", "1")


async def _read_filter(ctx: OpContext, name: str | None) -> Any:
    """The raw `dialogFilter` a `--folder <name|id>` names, or None."""
    if name is None or _is_peer_folder(name) or name.strip().lower() == "all":
        return None
    from tlgr.ops.folder import find_filter

    return await find_filter(ctx, name)


def _entity_map(result: Any) -> dict[int, Any]:
    """`{marked id: entity}` for the chats and users a reply carried."""
    from telethon import utils

    out: dict[int, Any] = {}
    for entity in list(getattr(result, "chats", None) or []) + list(
        getattr(result, "users", None) or []
    ):
        try:
            out[int(utils.get_peer_id(entity))] = entity
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return out


def _dialog_model(raw: Any, entities: dict[int, Any], messages: dict[int, Any]) -> Dialog:
    """One `dialog` row plus the entity and top message that came with it."""
    from tlgr.ops.draft import draft_model

    chat_id = peer_id_of(getattr(raw, "peer", None)) or 0
    entity = entities.get(chat_id)
    peer = (
        entity_to_peer(entity)
        if entity is not None
        else Peer(id=chat_id, raw_id=abs(chat_id), kind="unknown")
    )
    top_id = int(getattr(raw, "top_message", 0) or 0)
    top = messages.get(top_id) if top_id else None
    draft_raw = getattr(raw, "draft", None)
    draft = None
    if draft_raw is not None and type(draft_raw).__name__ != "DraftMessageEmpty":
        draft = draft_model(draft_raw, chat_id=chat_id)
    folder_id = int(getattr(raw, "folder_id", 0) or 0)
    return Dialog(
        chat=peer,
        unread_count=int(getattr(raw, "unread_count", 0) or 0),
        unread_mentions_count=int(getattr(raw, "unread_mentions_count", 0) or 0),
        unread_reactions_count=int(getattr(raw, "unread_reactions_count", 0) or 0),
        unread_poll_votes_count=int(getattr(raw, "unread_poll_votes_count", 0) or 0),
        unread_mark=bool(getattr(raw, "unread_mark", False)),
        read_inbox_max_id=int(getattr(raw, "read_inbox_max_id", 0) or 0),
        read_outbox_max_id=int(getattr(raw, "read_outbox_max_id", 0) or 0),
        top_message_id=top_id or None,
        pinned=bool(getattr(raw, "pinned", False)),
        folder_id=folder_id,
        archived=folder_id == FOLDER_ARCHIVE,
        notify=notify_settings(getattr(raw, "notify_settings", None)),
        draft=draft,
        ttl_period=getattr(raw, "ttl_period", None),
        view_forum_as_messages=getattr(raw, "view_forum_as_messages", None),
        requests_pending=getattr(entity, "requests_pending", None),
        restricted=bool(getattr(entity, "restricted", False)),
        restriction_reason=[
            str(getattr(reason, "text", "") or "")
            for reason in (getattr(entity, "restriction_reason", None) or [])
        ],
        participants_count=getattr(entity, "participants_count", None),
        last_message=message_to_model(top, chat_id=chat_id) if top is not None else None,
    )


async def fetch_dialogs(
    ctx: OpContext,
    *,
    folder_id: int | None,
    limit: int,
    offset_date: Any = None,
    offset_id: int = 0,
    offset_peer: Any = None,
) -> tuple[list[Dialog], list[Any]]:
    """One page of `messages.getDialogs`, as models and as raw rows.

    Raw rows come back too because the cursor is built from the *last row's*
    peer, and rebuilding an `InputPeer` from a marked id would be a second
    resolution of something the server just sent.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(
        fn.GetDialogsRequest(
            offset_date=offset_date,
            offset_id=offset_id,
            offset_peer=offset_peer or types.InputPeerEmpty(),
            limit=limit,
            hash=0,
            folder_id=folder_id if folder_id else None,
        )
    )
    entities = _entity_map(result)
    messages = {
        int(getattr(message, "id", 0) or 0): message
        for message in (getattr(result, "messages", None) or [])
    }
    rows = [row for row in (getattr(result, "dialogs", None) or []) if hasattr(row, "peer")]
    return [_dialog_model(row, entities, messages) for row in rows], rows


async def _all_dialogs(ctx: OpContext, *, folder_id: int | None, cap: int = 2000) -> list[Dialog]:
    """Every dialog, walked inside the daemon.

    A folder is evaluated client-side, so anything scoped by one needs the
    whole list; walking it here is what keeps "the dialog list was fully
    enumerated" true for the callers that depend on it.
    """
    out: list[Dialog] = []
    offset_date: Any = None
    offset_id = 0
    offset_peer: Any = None
    seen: set[int] = set()
    while len(out) < cap:
        page, rows = await fetch_dialogs(
            ctx,
            folder_id=folder_id,
            limit=100,
            offset_date=offset_date,
            offset_id=offset_id,
            offset_peer=offset_peer,
        )
        fresh = [d for d in page if d.chat.id not in seen]
        out.extend(fresh)
        seen.update(d.chat.id for d in page)
        if len(page) < 100 or not fresh:
            break
        last = page[-1]
        offset_id = last.top_message_id or 0
        offset_date = parse_dt(last.last_message.date) if last.last_message else None
        offset_peer = _offset_peer(rows[-1]) if rows else None
    return out


def _offset_peer(row: Any) -> Any:
    """The `InputPeer` for a dialog row's peer, without a round trip."""
    from telethon.tl import types

    peer = getattr(row, "peer", None)
    user_id = getattr(peer, "user_id", None)
    if user_id is not None:
        return types.InputPeerUser(user_id=int(user_id), access_hash=0)
    chat_id = getattr(peer, "chat_id", None)
    if chat_id is not None:
        return types.InputPeerChat(chat_id=int(chat_id))
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is not None:
        return types.InputPeerChannel(channel_id=int(channel_id), access_hash=0)
    return types.InputPeerEmpty()


def _matches_filter(dialog: Dialog, raw: Any) -> bool:
    """Does this dialog fall into that chat folder?

    The same rules every official client applies: an explicit include or pin
    always wins, an explicit exclude always loses, and the type flags decide
    the rest.
    """
    chat_id = dialog.chat.id
    from tlgr.ops.folder import folder_model

    model = folder_model(raw)
    if chat_id in model.exclude_peers:
        return False
    if chat_id in model.include_peers or chat_id in model.pinned_peers:
        return True
    if model.is_chatlist:
        return False
    kind = dialog.chat.kind
    if model.exclude_muted and dialog.notify is not None and dialog.notify.muted:
        return False
    if model.exclude_read and not (dialog.unread_count or dialog.unread_mark):
        return False
    if model.exclude_archived and dialog.archived:
        return False
    if kind == "bot":
        return model.bots
    if kind in ("user", "saved"):
        return model.contacts or model.non_contacts
    if kind in ("group", "supergroup"):
        return model.groups
    if kind == "channel":
        return model.broadcasts
    return False


async def folder_counts(ctx: OpContext, filters: list[Any], folders: list[Folder]) -> None:
    """Fill in `chats`/`unread_*` for each folder from one dialog walk.

    Exported for `folder list --with-counts`: there is no server-side count,
    and asking per folder would be one full dialog walk per folder.
    """
    dialogs = await _all_dialogs(ctx, folder_id=None)
    dialogs += await _all_dialogs(ctx, folder_id=FOLDER_ARCHIVE)
    for raw, model in zip(filters, folders, strict=True):
        if model.is_default:
            members = [d for d in dialogs if not d.archived]
        else:
            members = [d for d in dialogs if _matches_filter(d, raw)]
        model.chats = len(members)
        model.unread_chats = sum(1 for d in members if d.unread_count or d.unread_mark)
        model.unread_messages = sum(d.unread_count for d in members)


async def _resolve_many(
    ctx: OpContext, refs: Any, *, from_file: str | None = None
) -> list[tuple[Any, int]]:
    """`[(InputPeer, marked id)]` for a variadic peer list, plus `--from-file`."""
    items = list(refs or [])
    if from_file:
        items.extend(_lines(from_file))
    out: list[tuple[Any, int]] = []
    for ref in items:
        peer = await _send.resolve(ctx, ref)
        out.append((peer, _send.peer_id_of(peer)))
    return out


def _lines(path: str) -> list[str]:
    """One peer per line from a file, or from stdin when the path is `-`."""
    import sys
    from pathlib import Path

    if path == "-":
        if sys.stdin is None or sys.stdin.isatty():
            raise UsageError("--from-file - was given but stdin is a terminal", field="from-file")
        text = sys.stdin.read()
    else:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"--from-file: {exc.strerror or exc}", field="from-file") from exc
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


async def _folder_members(ctx: OpContext, folder: str, kind: str | None = None) -> list[Dialog]:
    """Every dialog of a folder — peer-folder or chat folder — optionally typed."""
    raw = await _read_filter(ctx, folder)
    if raw is None:
        dialogs = await _all_dialogs(ctx, folder_id=_peer_folder(folder))
    else:
        dialogs = [
            d
            for d in await _all_dialogs(ctx, folder_id=None)
            + await _all_dialogs(ctx, folder_id=FOLDER_ARCHIVE)
            if _matches_filter(d, raw)
        ]
    return [d for d in dialogs if _kind_matches(d.chat.kind, kind)]


def _kind_matches(kind: str, wanted: str | None) -> bool:
    if not wanted:
        return True
    if wanted == "group":
        return kind in ("group", "supergroup")
    if wanted == "user":
        return kind in ("user", "saved")
    return kind == wanted


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


# ---------------------------------------------------------------------------
# chat list
# ---------------------------------------------------------------------------


class ListReq(Request):
    folder: Annotated[
        str,
        opt("--folder", metavar="FOLDER", help="main | archive | all | folder id | folder name."),
    ] = "main"
    type: Annotated[
        str | None,
        choice(
            "user",
            "bot",
            "group",
            "supergroup",
            "channel",
            "forum",
            "saved",
            "self",
            help="Filter by peer kind.",
        ),
    ] = None
    unread: Annotated[bool, opt("--unread", help="Only chats with unread messages or a mark.")] = (
        False
    )
    unread_mark: Annotated[
        bool, opt("--unread-mark", help="Only chats carrying the manual unread mark.")
    ] = False
    with_mentions: Annotated[
        bool, opt("--with-mentions", help="Only chats with unread mentions.")
    ] = False
    with_reactions: Annotated[
        bool, opt("--with-reactions", help="Only chats with unread reactions.")
    ] = False
    with_join_requests: Annotated[
        bool, opt("--with-join-requests", help="Only chats with pending join requests.")
    ] = False
    with_drafts: Annotated[bool, opt("--with-drafts", help="Only chats with a saved draft.")] = (
        False
    )
    pinned: Annotated[bool, opt("--pinned", help="Only pinned dialogs, in pinned order.")] = False
    muted: Annotated[bool, opt("--muted", help="Only muted chats.")] = False
    unmuted: Annotated[bool, opt("--unmuted", help="Only unmuted chats.")] = False
    search: Annotated[
        str | None, opt("--search", "-s", metavar="TEXT", help="Match title or username.")
    ] = None
    inactive: Annotated[
        bool, opt("--inactive", help="The groups/channels you have not opened for longest.")
    ] = False
    scope: Annotated[
        str,
        choice(
            "dialogs",
            "admined-public",
            "inactive",
            "left",
            help="What to list instead of the dialog list.",
        ),
    ] = "dialogs"
    common_with: Annotated[
        PeerRef | None,
        opt("--common-with", metavar="USER", kind="user", help="Chats shared with this user."),
    ] = None
    by_location: Annotated[
        bool, opt("--by-location", help="With --scope admined-public: geogroups.")
    ] = False
    check_limit: Annotated[
        bool, opt("--check-limit", help="With --scope admined-public: report the limit instead.")
    ] = False
    for_personal: Annotated[
        bool,
        opt("--for-personal", help="With --scope admined-public: personal-channel candidates."),
    ] = False
    sort: Annotated[
        str, choice("default", "date", "unread", "name", "pinned", help="Ordering.")
    ] = "default"


async def list_chats(ctx: OpContext, req: ListReq) -> Page[Dialog]:
    """The chat list, filtered the way the GUI's quick filters filter it.

    A `--folder <name>` is applied client-side because Telegram has no
    `getDialogs(filter_id)`; that also means such a listing walks every page
    inside the daemon, which is what keeps "the dialog list was fully
    enumerated" an honest statement for `user dialog-status` to rely on.
    """
    limit, state = _window(ctx, "chat.list", PageKind.DIALOGS)
    scope = "inactive" if req.inactive else req.scope

    if req.common_with is not None:
        return await _common_chats(ctx, req, limit)
    if scope != "dialogs":
        return await _chat_scope(ctx, req, scope, limit)
    if req.pinned:
        return await _pinned_dialogs(ctx, req)

    chat_filter = await _read_filter(ctx, req.folder)
    folder_id = None if chat_filter is not None else _peer_folder(req.folder)
    fetch_all = bool(getattr(ctx, "fetch_all", False))
    walk_all = fetch_all or chat_filter is not None

    rows: list[Any] = []
    if walk_all:
        items = await _all_dialogs(ctx, folder_id=folder_id)
        if chat_filter is not None:
            seen = {d.chat.id for d in items}
            archived = await _all_dialogs(ctx, folder_id=FOLDER_ARCHIVE)
            items += [d for d in archived if d.chat.id not in seen]
            items = [d for d in items if _matches_filter(d, chat_filter)]
            member_of = int(getattr(chat_filter, "id", 0) or 0)
            for dialog in items:
                dialog.folders = [member_of]
    else:
        items, rows = await fetch_dialogs(
            ctx,
            folder_id=folder_id,
            limit=limit,
            offset_date=parse_dt(state.get("offset_date")) if state.get("offset_date") else None,
            offset_id=int(state.get("offset_id", 0) or 0),
            offset_peer=_state_peer(state),
        )

    items = _sorted(_apply_filters(items, req), req.sort)
    if walk_all:
        return Page(
            items=items if fetch_all else items[:limit],
            has_more=False,
            total=len(items),
        )

    next_state: dict[str, Any] = {}
    if rows and items:
        next_state = {
            "offset_id": int(getattr(rows[-1], "top_message", 0) or 0),
            "peer": _peer_state(rows[-1]),
        }
    return build_page(
        items,
        op="chat.list",
        kind=PageKind.DIALOGS,
        state=next_state,
        account=ctx.account,
        limit=limit,
        has_more=None if next_state else False,
    )


def _peer_state(row: Any) -> dict[str, Any]:
    peer = getattr(row, "peer", None)
    for attribute in ("user_id", "chat_id", "channel_id"):
        value = getattr(peer, attribute, None)
        if value is not None:
            return {"kind": attribute, "id": int(value)}
    return {}


def _state_peer(state: dict[str, Any]) -> Any:
    from telethon.tl import types

    saved = state.get("peer") or {}
    kind = saved.get("kind")
    value = int(saved.get("id") or 0)
    if kind == "user_id":
        return types.InputPeerUser(user_id=value, access_hash=0)
    if kind == "chat_id":
        return types.InputPeerChat(chat_id=value)
    if kind == "channel_id":
        return types.InputPeerChannel(channel_id=value, access_hash=0)
    return None


def _apply_filters(items: list[Dialog], req: ListReq) -> list[Dialog]:
    out = items
    if req.type:
        if req.type == "forum":
            out = [d for d in out if d.view_forum_as_messages is not None]
        elif req.type == "self":
            out = [d for d in out if d.chat.is_self or d.chat.kind == "saved"]
        else:
            out = [d for d in out if _kind_matches(d.chat.kind, req.type)]
    if req.unread:
        out = [d for d in out if d.unread_count or d.unread_mark]
    if req.unread_mark:
        out = [d for d in out if d.unread_mark]
    if req.with_mentions:
        out = [d for d in out if d.unread_mentions_count]
    if req.with_reactions:
        out = [d for d in out if d.unread_reactions_count]
    if req.with_join_requests:
        out = [d for d in out if (d.requests_pending or 0) > 0]
    if req.with_drafts:
        out = [d for d in out if d.draft is not None and not d.draft.empty]
    if req.muted:
        out = [d for d in out if d.notify is not None and d.notify.muted]
    if req.unmuted:
        out = [d for d in out if d.notify is None or not d.notify.muted]
    if req.search:
        needle = req.search.casefold()
        out = [
            d
            for d in out
            if needle in d.chat.title.casefold() or needle in (d.chat.username or "").casefold()
        ]
    return out


def _sorted(items: list[Dialog], how: str) -> list[Dialog]:
    if how == "name":
        return sorted(items, key=lambda d: d.chat.title.casefold())
    if how == "unread":
        return sorted(items, key=lambda d: d.unread_count, reverse=True)
    if how == "pinned":
        return sorted(items, key=lambda d: (not d.pinned, -(d.top_message_id or 0)))
    if how == "date":
        return sorted(items, key=lambda d: -(d.top_message_id or 0))
    return items


async def _pinned_dialogs(ctx: OpContext, req: ListReq) -> Page[Dialog]:
    """`messages.getPinnedDialogs` — the pinned rows, in their pinned order."""
    from telethon.tl.functions import messages as fn

    folder_id = _peer_folder(req.folder) or FOLDER_MAIN
    result = await _client(ctx)(fn.GetPinnedDialogsRequest(folder_id=folder_id))
    entities = _entity_map(result)
    messages = {int(getattr(m, "id", 0) or 0): m for m in (getattr(result, "messages", None) or [])}
    items = [
        _dialog_model(row, entities, messages)
        for row in (getattr(result, "dialogs", None) or [])
        if hasattr(row, "peer")
    ]
    for order, dialog in enumerate(items):
        dialog.pinned = True
        dialog.pinned_order = order
    return Page(items=_apply_filters(items, req), has_more=False, total=len(items))


async def _common_chats(ctx: OpContext, req: ListReq, limit: int) -> Page[Dialog]:
    """`messages.getCommonChats` — what you and this user are both in."""
    from telethon.tl.functions import messages as fn

    user = await _send.resolve(ctx, req.common_with)
    result = await _client(ctx)(fn.GetCommonChatsRequest(user_id=user, max_id=0, limit=limit))
    items = [Dialog(chat=entity_to_peer(chat)) for chat in (getattr(result, "chats", None) or [])]
    return Page(items=_apply_filters(items, req), has_more=False, total=len(items))


async def _chat_scope(ctx: OpContext, req: ListReq, scope: str, limit: int) -> Page[Dialog]:
    """The three chat lists that are not the dialog list.

    `inactive` is the CHANNELS_TOO_MUCH escape hatch (it names what to leave),
    `admined-public` is the public-username budget, and `left` is what you
    once joined. None of them is a dialog, so they arrive as chats with the
    dialog fields absent rather than zeroed.
    """
    from telethon.tl.functions import channels as fn

    items: list[Dialog] = []
    if scope == "inactive":
        result = await _client(ctx)(fn.GetInactiveChannelsRequest())
        dates = list(getattr(result, "dates", None) or [])
        for index, chat in enumerate(getattr(result, "chats", None) or []):
            dialog = Dialog(chat=entity_to_peer(chat))
            if index < len(dates):
                dialog.inactive_since = fmt_dt(dates[index])
            dialog.participants_count = getattr(chat, "participants_count", None)
            items.append(dialog)
    elif scope == "left":
        result = await _client(ctx)(fn.GetLeftChannelsRequest(offset=0))
        items = [Dialog(chat=entity_to_peer(c)) for c in (getattr(result, "chats", None) or [])]
    else:
        result = await _client(ctx)(
            fn.GetAdminedPublicChannelsRequest(
                by_location=req.by_location or None,
                check_limit=req.check_limit or None,
                for_personal=req.for_personal or None,
            )
        )
        items = [Dialog(chat=entity_to_peer(c)) for c in (getattr(result, "chats", None) or [])]
    items = _apply_filters(items, req)
    return Page(items=items[:limit], has_more=len(items) > limit, total=len(items))


SPEC_LIST = OperationSpec(
    id="chat.list",
    request=ListReq,
    response=Page[Dialog],
    impl=list_chats,
    summary="List dialogs with folder, type, unread, pinned and search filters",
    description=(
        "Never emits a read receipt. `--folder` takes a peer-folder "
        "(main/archive/all) or a chat folder by id or name, evaluated "
        "client-side because Telegram has no getDialogs(filter_id). "
        "`--scope` swaps the dialog list for the admined-public, inactive or "
        "left-channel lists, which are chats rather than dialogs."
    ),
    aliases=("chats", "inbox"),
    legacy_paths=("chat list", "chats", "inbox"),
    paginated=PageKind.DIALOGS,
    columns=("chat.id", "chat.title", "chat.kind", "unread_count"),
    headers=("ID", "Name", "Type", "Unread"),
    example={"items": [_EXAMPLE_DIALOG], "has_more": True},
    example_args="chat list --unread",
    covers=(
        "dialogs.folder-chat-count",
        "dialogs.inactive-chats",
        "dialogs.join-requests-badge",
        "dialogs.list-archive",
        "dialogs.list-folder",
        "dialogs.list-main",
        "dialogs.pinned-list",
        "dialogs.restricted-peer",
        "dialogs.saved-messages",
        "dialogs.unread-marks-list",
        "dialogs.unread-quick-filter",
        "groups-channels-admin.admined-public-chats",
        "groups-channels-admin.common-chats",
        "groups-channels-admin.inactive-chats",
        "groups-channels-admin.left-channels",
    ),
    covers_partial=("contacts-users.contacts-sort", "dialogs.search-peers"),
    coverage_note=(
        "Peer search here is a substring match over the dialog list; the "
        "global one is `contact search`. `--sort` orders chats, not contacts."
    ),
)


# ---------------------------------------------------------------------------
# chat open / catchup
# ---------------------------------------------------------------------------


class OpenReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to open.")]
    no_read: Annotated[
        bool, opt("--no-read", help="Peek: fetch the history and emit no read receipt.")
    ] = False
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Only this forum topic.")
    ] = None
    increment_views: Annotated[
        bool, opt("--increment-views", help="Also count a view on channel posts.")
    ] = False


async def open_chat(ctx: OpContext, req: OpenReq) -> OpenResult:
    """Open a chat the way a human does: recent history *and* a read receipt.

    The receipt is the point, and it has two effects: the other side sees you
    read it, and the owner's own unread badge is cleared. The second one is
    why `--no-read` exists — on a chat a person is handling by hand that badge
    is their only reminder that they owe a reply.
    """
    limit = int(getattr(ctx, "limit", None) or 30)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    client = _client(ctx)

    kwargs: dict[str, Any] = {}
    if req.topic is not None:
        kwargs["reply_to"] = req.topic
    raw = [m async for m in client.iter_messages(peer, limit=limit, **kwargs) if m is not None]
    messages = [message_to_model(m, chat_id=chat_id) for m in raw]

    if req.increment_views and raw:
        from telethon.tl.functions import messages as fn

        await client(
            fn.GetMessagesViewsRequest(peer=peer, id=[m.id for m in messages], increment=True)
        )

    marked_read = False
    if not req.no_read:
        top = max((m.id for m in messages), default=0)
        await client.send_read_acknowledge(peer, max_id=top)
        marked_read = True
        ctx.emit("chat_read", {"chat_id": chat_id, "max_id": top})
    return OpenResult(chat_id=chat_id, marked_read=marked_read, messages=messages)


SPEC_OPEN = OperationSpec(
    id="chat.open",
    request=OpenReq,
    response=OpenResult,
    impl=open_chat,
    summary="Open a chat like a human: recent history AND a read receipt",
    description=(
        "SEMANTICS ARE FROZEN. The read receipt is visible to the other side "
        "and irreversible; `--no-read` is the silent peek, and so is "
        "`message list`."
    ),
    legacy_paths=("chat open",),
    mutating=True,
    rate_class="read",
    columns=("chat_id", "marked_read"),
    example={"chat_id": 777123, "marked_read": True, "messages": []},
    example_args="chat open @alice",
    covers=("dialogs.open-chat", "dialogs.peek-chat"),
    tags=frozenset({"visible-to-others"}),
)


class CatchupReq(Request):
    type: Annotated[
        str | None, choice("user", "bot", "group", "channel", help="Filter by peer kind.")
    ] = None
    folder: Annotated[
        str, opt("--folder", metavar="FOLDER", help="Restrict to a folder / archive / main.")
    ] = "main"
    limit_chats: Annotated[
        int, opt("--limit-chats", metavar="N", help="Max unread chats to include.", ge=1)
    ] = 20
    per_chat: Annotated[
        int, opt("--per-chat", metavar="N", help="Max messages per chat.", ge=1)
    ] = 10


async def catchup(ctx: OpContext, req: CatchupReq) -> Catchup:
    """What did I miss: every unread chat with its recent messages, read-only.

    Emits no read receipts, which is what makes it safe to run at the start
    of every session. A chat carrying only the manual unread mark is included
    even though its `unread_count` is 0 — that mark is somebody saying "come
    back to this".
    """
    dialogs = await _folder_members(ctx, req.folder, req.type)
    unread = [d for d in dialogs if d.unread_count or d.unread_mark][: req.limit_chats]

    client = _client(ctx)
    chats: list[CatchupChat] = []
    for dialog in unread:
        peer = await _send.resolve(ctx, str(dialog.chat.id))
        raw = [m async for m in client.iter_messages(peer, limit=req.per_chat) if m is not None]
        chats.append(
            CatchupChat(
                id=dialog.chat.id,
                chat=dialog.chat,
                name=dialog.chat.title,
                unread_count=dialog.unread_count,
                unread_mark=dialog.unread_mark,
                messages=[message_to_model(m, chat_id=dialog.chat.id) for m in raw],
            )
        )
    return Catchup(chats=chats)


SPEC_CATCHUP = OperationSpec(
    id="chat.catchup",
    request=CatchupReq,
    response=Catchup,
    impl=catchup,
    summary="What did I miss: every unread chat with its recent messages",
    description=(
        "SEMANTICS ARE FROZEN: read-only, emits no read receipts, and "
        "includes chats that carry only the manual unread mark."
    ),
    aliases=("catchup",),
    legacy_paths=("chat catchup", "catchup"),
    timeout_s=300,
    columns=("chats.id", "chats.name", "chats.unread_count"),
    example={"chats": [{"id": 777123, "name": "Alice", "unread_count": 3, "messages": []}]},
    example_args="chat catchup",
    covers=("dialogs.sync-updates",),
    coverage_note="The CLI-visible half; the getDifference loop is the daemon's (PR-4).",
)


# ---------------------------------------------------------------------------
# chat read / unread
# ---------------------------------------------------------------------------


class ReadReq(Request):
    chat: Annotated[
        list[PeerRef],
        arg(0, metavar="CHAT", required=False, variadic=True, kind="peer", help="Chats to read."),
    ] = []
    up_to: Annotated[
        int | None, opt("--up-to", metavar="ID", kind="msg_id", help="Read up to this message id.")
    ] = None
    mentions: Annotated[bool, opt("--mentions", help="Also clear unread mentions.")] = False
    reactions: Annotated[bool, opt("--reactions", help="Also clear unread reactions.")] = False
    polls: Annotated[bool, opt("--polls", help="Also clear unread poll votes.")] = False
    topic: Annotated[
        int | None,
        opt("--topic", metavar="ID", kind="msg_id", help="Advance a comment thread instead."),
    ] = None
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="A Saved-Messages sublist."),
    ] = None
    folder: Annotated[
        str | None,
        opt("--folder", metavar="FOLDER", help="Read every chat of a folder instead."),
    ] = None
    type: Annotated[
        str | None, choice("user", "bot", "group", "channel", help="With --folder: peer kind.")
    ] = None
    from_file: Annotated[
        str | None,
        opt("--from-file", metavar="PATH", kind="path", help="Peers from a file, '-' for stdin."),
    ] = None
    continue_on_error: Annotated[
        bool, opt("--continue-on-error", help="Keep going and report per-peer results.")
    ] = True


async def read(ctx: OpContext, req: ReadReq) -> ReadChats:
    """Send a read receipt — for one chat, several, or a whole folder.

    A read receipt is irreversible for the other side: `chat unread` restores
    only the owner's own badge. `--folder` is the CLI form of the GUI's
    multi-select, so it runs one chat at a time behind the session's flood
    limiter and reports per-peer results rather than stopping at the first
    failure.
    """
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    targets = await _resolve_many(ctx, req.chat, from_file=req.from_file)
    if req.folder is not None:
        for dialog in await _folder_members(ctx, req.folder, req.type):
            peer = await _send.resolve(ctx, str(dialog.chat.id))
            targets.append((peer, dialog.chat.id))
    if not targets:
        raise UsageError("give a chat, --folder or --from-file", field="chat")

    if req.topic is not None and len(targets) == 1:
        peer, chat_id = targets[0]
        await client(
            fn.ReadDiscussionRequest(peer=peer, msg_id=req.topic, read_max_id=req.up_to or 0)
        )
        ctx.emit("chat_read", {"chat_id": chat_id, "topic": req.topic})
        return ReadChats(read=True, chat_id=chat_id, results=[PeerResult(chat_id=chat_id, ok=True)])

    if req.saved_peer is not None and len(targets) == 1:
        peer, chat_id = targets[0]
        origin = await _send.resolve(ctx, req.saved_peer)
        await client(
            fn.ReadSavedHistoryRequest(parent_peer=peer, peer=origin, max_id=req.up_to or 0)
        )
        ctx.emit("chat_read", {"chat_id": chat_id, "saved_peer": _send.peer_id_of(origin)})
        return ReadChats(read=True, chat_id=chat_id, results=[PeerResult(chat_id=chat_id, ok=True)])

    results: list[PeerResult] = []
    mentions_read = reactions_read = 0
    limiter = getattr(ctx, "limiter", None)
    for index, (peer, chat_id) in enumerate(targets):
        if index and limiter is not None:
            await limiter.acquire("bulk")
        try:
            await client.send_read_acknowledge(peer, max_id=req.up_to or 0)
            if req.mentions:
                mentions_read += await _affected_loop(
                    ctx, lambda offset, p=peer: fn.ReadMentionsRequest(peer=p)
                )
            if req.reactions:
                reactions_read += await _affected_loop(
                    ctx, lambda offset, p=peer: fn.ReadReactionsRequest(peer=p)
                )
            if req.polls:
                await client(fn.ReadPollVotesRequest(peer=peer))
            results.append(PeerResult(chat_id=chat_id, ok=True))
            ctx.emit("chat_read", {"chat_id": chat_id, "max_id": req.up_to or 0})
        except Exception as exc:
            if not req.continue_on_error:
                raise
            results.append(PeerResult(chat_id=chat_id, ok=False, error=_error_text(exc)))

    return ReadChats(
        read=any(r.ok for r in results),
        chat_id=results[0].chat_id if len(results) == 1 else None,
        results=results,
        mentions_read=mentions_read if req.mentions else None,
        reactions_read=reactions_read if req.reactions else None,
        polls_read=True if req.polls else None,
    )


SPEC_READ = OperationSpec(
    id="chat.read",
    request=ReadReq,
    response=ReadChats,
    impl=read,
    summary="Send a read receipt for chats, a thread, or a whole folder",
    description=(
        "Irreversible for the other side. `readHistory` is namespace-split "
        "(channels.* for supergroups and channels) and Telethon picks the "
        "right one; the mention and reaction sweeps are looped until the "
        "server stops returning an offset."
    ),
    aliases=("chat.read-all",),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("read", "results"),
    example={"read": True, "results": [{"chat_id": 777123, "ok": True}]},
    example_args="chat read @alice",
    covers=("dialogs.mark-read", "dialogs.mark-read-all", "dialogs.read-discussion"),
    covers_partial=(
        "dialogs.bulk-chat-actions",
        "dialogs.monoforum-topics",
        "dialogs.saved-sublists",
    ),
    coverage_note=(
        "The read half of the bulk and saved-sublist surfaces; archiving in "
        "bulk is `chat archive` and listing sublists is `chat saved list`."
    ),
    tags=frozenset({"visible-to-others"}),
)


class UnreadReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to mark unread.")]
    clear: Annotated[bool, opt("--clear", help="Clear the mark instead of setting it.")] = False
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="A monoforum topic instead."),
    ] = None


async def unread(ctx: OpContext, req: UnreadReq) -> UnreadResult:
    """Mark a chat unread again — the undo for an accidental read receipt.

    SEMANTICS ARE FROZEN: this sets Telegram's manual unread *flag*, not a
    numeric count, and it does not un-send the receipt the other side already
    got. Chats flagged this way come back with `unread_mark: true` and do
    appear in `chat list --unread`, `inbox` and `catchup`.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    parent = await _send.resolve(ctx, req.saved_peer) if req.saved_peer is not None else None
    await _client(ctx)(
        fn.MarkDialogUnreadRequest(
            peer=types.InputDialogPeer(peer),
            unread=not req.clear or None,
            parent_peer=parent,
        )
    )
    ctx.emit("chat_unread", {"chat_id": chat_id, "unread": not req.clear})
    return UnreadResult(chat_id=chat_id, unread=not req.clear)


SPEC_UNREAD = OperationSpec(
    id="chat.unread",
    request=UnreadReq,
    response=UnreadResult,
    impl=unread,
    summary="Mark a chat unread again — the undo for an accidental read receipt",
    description=(
        "Restores the badge the account owner sees. It cannot un-send the "
        "read receipt the other side already got; nothing can."
    ),
    legacy_paths=("chat unread",),
    mutating=True,
    idempotent=True,
    columns=("chat_id", "unread"),
    example={"unread": True, "chat_id": 777123},
    example_args="chat unread @alice",
    covers=(
        "dialogs.mark-unread",
        "dialogs.mark-unread-clear",
        "messages-core.chat-mark-unread",
    ),
)


# ---------------------------------------------------------------------------
# chat get
# ---------------------------------------------------------------------------


class GetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to describe.")]
    full: Annotated[
        bool, opt("--full", help="Also fetch getFullUser / getFullChat / getFullChannel.")
    ] = False
    dialog: Annotated[bool, opt("--dialog", help="Include the dialog record.")] = True
    refresh: Annotated[bool, opt("--refresh", help="Bypass the 60 s server-side *Full cache.")] = (
        False
    )
    field: Annotated[
        str | None, opt("--field", metavar="NAME", help="Emit one field only (scripting).")
    ] = None


async def get(ctx: OpContext, req: GetReq) -> ChatInfo:
    """Everything one chat is: the peer, its dialog row, and optionally *Full.

    `--full` is opt-in because `getFullChannel` is a second round trip that a
    caller listing thirty chats does not want, and because the server caches
    it for about a minute anyway.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    entity = await client.get_entity(peer)
    shape = entity_to_peer(entity)

    info = ChatInfo(
        id=shape.id,
        raw_id=shape.raw_id,
        type=shape.kind,
        title=shape.title,
        name=shape.title,
        username=shape.username,
        usernames=shape.usernames,
        restricted=bool(getattr(entity, "restricted", False)) or None,
        restriction_reason=[
            str(getattr(reason, "text", "") or "")
            for reason in (getattr(entity, "restriction_reason", None) or [])
        ],
        left=bool(getattr(entity, "left", False)) or None,
        creator=bool(getattr(entity, "creator", False)) or None,
        forum=bool(getattr(entity, "forum", False)) or None,
        gigagroup=bool(getattr(entity, "gigagroup", False)) or None,
        join_to_send=getattr(entity, "join_to_send", None),
        join_request=getattr(entity, "join_request", None),
        noforwards=getattr(entity, "noforwards", None),
        participants_count=getattr(entity, "participants_count", None),
        level=getattr(entity, "level", None),
        linked_monoforum_id=getattr(entity, "linked_monoforum_id", None),
    )

    if req.dialog:
        result = await client(fn.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)]))
        for row in getattr(result, "dialogs", None) or []:
            _fill_dialog(info, row)

    if req.full:
        await _fill_full(ctx, info, peer, entity)
    if req.field:
        import msgspec

        data = msgspec.to_builtins(info)
        if req.field not in data:
            raise UsageError(f"chat get has no field {req.field!r}", field="field")
    if chat_id and not info.id:  # pragma: no cover - defensive
        info.id = chat_id
    return info


def _fill_dialog(info: ChatInfo, row: Any) -> None:
    from tlgr.ops.draft import draft_model

    info.folder_id = int(getattr(row, "folder_id", 0) or 0)
    info.pinned = bool(getattr(row, "pinned", False))
    info.unread = int(getattr(row, "unread_count", 0) or 0)
    info.unread_mentions = int(getattr(row, "unread_mentions_count", 0) or 0)
    info.unread_reactions = int(getattr(row, "unread_reactions_count", 0) or 0)
    info.unread_mark = bool(getattr(row, "unread_mark", False))
    info.read_inbox_max_id = int(getattr(row, "read_inbox_max_id", 0) or 0)
    info.read_outbox_max_id = int(getattr(row, "read_outbox_max_id", 0) or 0)
    info.top_message_id = int(getattr(row, "top_message", 0) or 0) or None
    info.notify_settings = notify_settings(getattr(row, "notify_settings", None))
    info.ttl_period = getattr(row, "ttl_period", None)
    info.view_forum_as_messages = getattr(row, "view_forum_as_messages", None)
    draft = getattr(row, "draft", None)
    if draft is not None and type(draft).__name__ != "DraftMessageEmpty":
        info.draft = draft_model(draft, chat_id=info.id)


async def _fill_full(ctx: OpContext, info: ChatInfo, peer: Any, entity: Any) -> None:
    """The `*Full` half: three different calls, one flat answer."""
    from telethon.tl.functions import channels as cfn
    from telethon.tl.functions import messages as mfn
    from telethon.tl.functions import users as ufn

    client = _client(ctx)
    kind = type(entity).__name__
    if kind == "User":
        result = await client(ufn.GetFullUserRequest(peer))
        full = getattr(result, "full_user", None)
        info.about = getattr(full, "about", None)
        info.blocked = getattr(full, "blocked", None)
        info.blocked_my_stories_from = getattr(full, "blocked_my_stories_from", None)
        info.common_chats_count = getattr(full, "common_chats_count", None)
        info.personal_channel_id = getattr(full, "personal_channel_id", None)
        info.ttl_period = getattr(full, "ttl_period", info.ttl_period)
        info.settings = action_bar(getattr(full, "settings", None), chat_id=info.id)
        info.theme = chat_theme(getattr(full, "theme_emoticon", None) and _emoji_theme(full))
        info.wallpaper = wallpaper(getattr(full, "wallpaper", None))
        info.translations_disabled = getattr(full, "translations_disabled", None)
        return

    if kind == "Chat":
        result = await client(mfn.GetFullChatRequest(chat_id=getattr(entity, "id", 0)))
    else:
        result = await client(cfn.GetFullChannelRequest(channel=_input_channel(peer)))
    full = getattr(result, "full_chat", None)
    info.about = getattr(full, "about", None)
    info.participants_count = getattr(full, "participants_count", info.participants_count)
    info.admins_count = getattr(full, "admins_count", None)
    info.kicked_count = getattr(full, "kicked_count", None)
    info.banned_count = getattr(full, "banned_count", None)
    info.online_count = getattr(full, "online_count", None)
    info.requests_pending = getattr(full, "requests_pending", None)
    info.recent_requesters = list(getattr(full, "recent_requesters", None) or [])
    info.slowmode_seconds = getattr(full, "slowmode_seconds", None)
    info.hidden_prehistory = getattr(full, "hidden_prehistory", None)
    info.participants_hidden = getattr(full, "participants_hidden", None)
    info.antispam = getattr(full, "antispam", None)
    info.linked_chat_id = peer_id_of_channel(getattr(full, "linked_chat_id", None))
    info.stats_dc = getattr(full, "stats_dc", None)
    info.can_view_stats = getattr(full, "can_view_stats", None)
    info.can_view_participants = getattr(full, "can_view_participants", None)
    info.can_set_stickers = getattr(full, "can_set_stickers", None)
    info.can_set_location = getattr(full, "can_set_location", None)
    info.can_delete_channel = getattr(full, "can_delete_channel", None)
    info.can_view_revenue = getattr(full, "can_view_revenue", None)
    info.can_view_stars_revenue = getattr(full, "can_view_stars_revenue", None)
    info.paid_reactions_available = getattr(full, "paid_reactions_available", None)
    info.has_welcome_messages = getattr(full, "stargifts_available", None)
    info.boosts_applied = getattr(full, "boosts_applied", None)
    info.ttl_period = getattr(full, "ttl_period", info.ttl_period)
    info.translations_disabled = getattr(full, "translations_disabled", None)
    info.theme = chat_theme(_emoji_theme(full))
    info.wallpaper = wallpaper(getattr(full, "wallpaper", None))
    info.settings = action_bar(getattr(full, "settings", None), chat_id=info.id)
    info.default_send_as = peer_id_of(getattr(full, "default_send_as", None))
    sticker_set = getattr(full, "stickerset", None)
    info.sticker_set = getattr(sticker_set, "short_name", None)
    emoji_set = getattr(full, "emojiset", None)
    info.emoji_set = getattr(emoji_set, "short_name", None)
    reactions = getattr(full, "available_reactions", None)
    info.available_reactions = [
        str(getattr(r, "emoticon", "") or getattr(r, "document_id", ""))
        for r in (getattr(reactions, "reactions", None) or [])
    ] or None
    invite = getattr(full, "exported_invite", None)
    info.exported_invite = getattr(invite, "link", None)
    info.pending_suggestions = list(getattr(full, "pending_suggestions", None) or [])
    location = getattr(full, "location", None)
    info.location = getattr(location, "address", None)


def _emoji_theme(full: Any) -> Any:
    """`theme_emoticon` as something `chat_theme()` can read."""
    emoticon = getattr(full, "theme_emoticon", None)
    if not emoticon:
        return None
    return type("_Theme", (), {"emoticon": emoticon, "title": emoticon})()


def peer_id_of_channel(raw_id: Any) -> int | None:
    """A bare `linked_chat_id` as the marked id every other field uses."""
    from tlgr.ops._serialize import marked_id

    if raw_id is None:
        return None
    return marked_id(int(raw_id), "supergroup")


SPEC_GET = OperationSpec(
    id="chat.get",
    request=GetReq,
    response=ChatInfo,
    impl=get,
    summary="Full info for one chat: dialog record, settings, notify, ttl, theme",
    description=(
        "`--full` adds users.getFullUser / messages.getFullChat / "
        "channels.getFullChannel, which the server caches for about a minute. "
        "`read_outbox_max_id` is here; the per-member reader list is "
        "`message seen`."
    ),
    legacy_paths=("chat get",),
    columns=("id", "type", "title", "username"),
    example={"id": 777123, "type": "user", "title": "Alice", "name": "Alice", "unread": 3},
    example_args="chat get @alice --full",
    covers=(
        "dialogs.chat-full-settings",
        "dialogs.get-peer-dialog",
        "dialogs.online-count",
        "dialogs.read-receipts-outbox",
        "groups-channels-admin.bulk-resolve-chats",
        "groups-channels-admin.get-full-info",
        "media.content-protection",
        "updates.presence-group-online-count",
    ),
    covers_partial=("groups-channels-admin.pending-suggestions",),
    coverage_note="Pending suggestions are reported here; dismissing one is PR-7's.",
)


# ---------------------------------------------------------------------------
# chat archive / autoarchive
# ---------------------------------------------------------------------------


class ArchiveReq(Request):
    chat: Annotated[
        list[PeerRef],
        arg(0, metavar="CHAT", variadic=True, kind="peer", help="Chats to archive."),
    ] = []
    undo: Annotated[bool, opt("--undo", help="Unarchive (folder 0) instead.")] = False
    dismiss_bar: Annotated[
        bool, opt("--dismiss-bar", help="Also hide the 'auto-archived' action bar.")
    ] = False
    from_file: Annotated[
        str | None,
        opt("--from-file", metavar="PATH", kind="path", help="Peers from a file, '-' for stdin."),
    ] = None


async def archive(ctx: OpContext, req: ArchiveReq) -> ArchiveResult:
    """Move chats into the archive, or back out of it.

    `folders.editPeerFolders` takes a *vector*, which makes this the one
    genuinely batched chat action Telegram offers: twenty peers cost one RPC.
    `folder_id` is only ever 0 or 1; there is no third peer-folder.
    """
    from telethon.tl import types
    from telethon.tl.functions import folders as fn
    from telethon.tl.functions import messages as mfn

    targets = await _resolve_many(ctx, req.chat, from_file=req.from_file)
    if not targets:
        raise UsageError("give at least one chat", field="chat")
    folder_id = FOLDER_MAIN if req.undo else FOLDER_ARCHIVE

    await _client(ctx)(
        fn.EditPeerFoldersRequest(
            folder_peers=[
                types.InputFolderPeer(peer=peer, folder_id=folder_id) for peer, _ in targets
            ]
        )
    )
    bar_hidden = False
    if req.dismiss_bar:
        for peer, _ in targets:
            await _client(ctx)(mfn.HidePeerSettingsBarRequest(peer=peer))
        bar_hidden = True

    ids = [chat_id for _, chat_id in targets]
    ctx.emit("chat_archive", {"chat_ids": ids, "archived": not req.undo})
    return ArchiveResult(
        archived=not req.undo,
        chat_id=ids[0] if len(ids) == 1 else None,
        chat_ids=ids,
        bar_hidden=bar_hidden,
    )


SPEC_ARCHIVE = OperationSpec(
    id="chat.archive",
    request=ArchiveReq,
    response=ArchiveResult,
    impl=archive,
    summary="Move chats to the archive, or back out of it",
    description=(
        "One RPC for any number of peers, because `folders.editPeerFolders` "
        "is the only batched chat action Telegram has. `--undo` is the half "
        "v1 never had."
    ),
    aliases=("chat.unarchive",),
    legacy_paths=("chat archive",),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("archived", "chat_ids"),
    example={"archived": True, "chat_id": 777123, "chat_ids": [777123]},
    example_args="chat archive @alice",
    covers=(
        "dialogs.archive",
        "dialogs.bulk-chat-actions",
        "dialogs.unarchive",
        "dialogs.unarchive-autoarchived",
    ),
)


class AutoArchiveReq(Request):
    auto: Annotated[
        str | None, choice("on", "off", help="Auto-archive and mute new non-contacts.")
    ] = None
    keep_unmuted: Annotated[
        str | None, choice("on", "off", help="Keep unmuted chats in the archive.")
    ] = None
    keep_folders: Annotated[
        str | None, choice("on", "off", help="Keep archived chats that belong to a folder.")
    ] = None


async def autoarchive_set(ctx: OpContext, req: AutoArchiveReq) -> ArchiveSettings:
    """The chat-list archive rules, read-modify-written.

    `setGlobalPrivacySettings` replaces the whole constructor, and the other
    flags in it belong to `privacy global`: sending only the archive fields
    would quietly reset somebody's read-marks and paid-message settings.
    """
    from telethon.tl.functions import account as fn

    client = _client(ctx)
    current = await client(fn.GetGlobalPrivacySettingsRequest())
    wanted = {
        "archive_and_mute_new_noncontact_peers": getattr(
            current, "archive_and_mute_new_noncontact_peers", None
        ),
        "keep_archived_unmuted": getattr(current, "keep_archived_unmuted", None),
        "keep_archived_folders": getattr(current, "keep_archived_folders", None),
    }
    asked = {
        "archive_and_mute_new_noncontact_peers": req.auto,
        "keep_archived_unmuted": req.keep_unmuted,
        "keep_archived_folders": req.keep_folders,
    }
    changed = False
    for name, value in asked.items():
        if value is None:
            continue
        new = value == "on"
        if bool(wanted[name]) != new:
            changed = True
        wanted[name] = new or None

    if not any(v is not None for v in asked.values()):
        return _archive_settings(current)
    if not changed:
        _already(ctx)
        return _archive_settings(current)

    for name, value in wanted.items():
        setattr(current, name, value)
    await client(fn.SetGlobalPrivacySettingsRequest(settings=current))
    ctx.emit("chat_autoarchive", {k: bool(v) for k, v in wanted.items()})
    return _archive_settings(current)


def _archive_settings(raw: Any) -> ArchiveSettings:
    return ArchiveSettings(
        archive_and_mute_new_noncontact_peers=bool(
            getattr(raw, "archive_and_mute_new_noncontact_peers", False)
        ),
        keep_archived_unmuted=bool(getattr(raw, "keep_archived_unmuted", False)),
        keep_archived_folders=bool(getattr(raw, "keep_archived_folders", False)),
    )


SPEC_AUTOARCHIVE_SET = OperationSpec(
    id="chat.autoarchive.set",
    request=AutoArchiveReq,
    response=ArchiveSettings,
    impl=autoarchive_set,
    summary="Chat-list archive rules for new and archived chats",
    description=(
        "Auto-archiving new non-contacts needs Premium unless the app config "
        "says otherwise; the server refuses it otherwise."
    ),
    mutating=True,
    idempotent=True,
    columns=("archive_and_mute_new_noncontact_peers", "keep_archived_unmuted"),
    example={"archive_and_mute_new_noncontact_peers": True, "keep_archived_unmuted": False},
    example_args="chat autoarchive set --auto on",
    covers=("dialogs.archive-settings",),
)


# ---------------------------------------------------------------------------
# chat mute
# ---------------------------------------------------------------------------


class MuteReq(Request):
    chat: Annotated[
        list[PeerRef],
        arg(0, metavar="CHAT", required=False, variadic=True, kind="peer", help="Chats to mute."),
    ] = []
    for_: Annotated[
        int | None,
        opt("--for", metavar="DURATION", kind="duration", help="Mute for 1h | 8h | 2d."),
    ] = None
    until: Annotated[
        str | None,
        opt("--until", metavar="TS", kind="datetime", help="Mute until an absolute time."),
    ] = None
    off: Annotated[bool, opt("--off", help="Unmute, keeping the other notify fields.")] = False
    stories: Annotated[bool, opt("--stories", help="Mute the peer's stories instead.")] = False
    folder: Annotated[
        str | None, opt("--folder", metavar="FOLDER", help="Apply to every chat of a folder.")
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="A forum topic.")
    ] = None


async def mute(ctx: OpContext, req: MuteReq) -> MuteResult:
    """Mute or unmute chats, for a duration, until a time, or forever.

    `mute_until` is an **absolute unix timestamp**. v1 computed it from the
    event loop's monotonic clock, so every timed mute resolved to 1970 and
    did nothing at all (COR-01); it is computed from the wall clock here, and
    the test asserts the value that reaches the request.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    targets = await _resolve_many(ctx, req.chat)
    if req.folder is not None:
        for dialog in await _folder_members(ctx, req.folder):
            peer = await _send.resolve(ctx, str(dialog.chat.id))
            targets.append((peer, dialog.chat.id))
    if not targets:
        raise UsageError("give a chat, or --folder", field="chat")

    until = _mute_until(req)
    settings_kwargs: dict[str, Any] = {}
    if req.stories:
        settings_kwargs["stories_muted"] = not req.off
    else:
        settings_kwargs["mute_until"] = until

    client = _client(ctx)
    limiter = getattr(ctx, "limiter", None)
    results: list[PeerResult] = []
    for index, (peer, chat_id) in enumerate(targets):
        if index and limiter is not None:
            # One RPC per chat: Telegram has no batched updateNotifySettings.
            await limiter.acquire("bulk")
        notify_peer = (
            types.InputNotifyForumTopic(peer=peer, top_msg_id=req.topic)
            if req.topic is not None
            else types.InputNotifyPeer(peer=peer)
        )
        try:
            await client(
                fn.UpdateNotifySettingsRequest(
                    peer=notify_peer,
                    settings=types.InputPeerNotifySettings(**settings_kwargs),
                )
            )
            results.append(PeerResult(chat_id=chat_id, ok=True))
        except Exception as exc:
            if len(targets) == 1:
                raise
            results.append(PeerResult(chat_id=chat_id, ok=False, error=_error_text(exc)))

    ids = [r.chat_id for r in results if r.ok]
    ctx.emit("chat_mute", {"chat_ids": ids, "muted": not req.off})
    return MuteResult(
        muted=not req.off,
        chat_id=ids[0] if len(ids) == 1 else None,
        chat_ids=ids,
        mute_until=fmt_dt(until) if until and not req.off else None,
        mute_until_unix=to_unix(until) if until and not req.off else None,
        stories=req.stories,
        results=results if len(results) > 1 else [],
    )


def _mute_until(req: MuteReq) -> datetime | None:
    """The absolute moment the mute ends, from `--off`, `--for` or `--until`."""
    if req.off:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if req.until:
        parsed = parse_dt(req.until)
        if parsed is None:
            raise UsageError(f"--until: cannot read {req.until!r} as a time", field="until")
        return parsed
    if req.for_:
        # The CLI's `duration` type has already turned `8h` into seconds; a
        # caller talking to the daemon directly sends the seconds itself.
        return datetime.now(timezone.utc) + timedelta(seconds=int(req.for_))
    return datetime.fromtimestamp(MUTE_FOREVER, tz=timezone.utc)


SPEC_MUTE = OperationSpec(
    id="chat.mute",
    request=MuteReq,
    response=MuteResult,
    impl=mute,
    summary="Mute or unmute chats, for a duration or forever",
    description=(
        "`mute_until` is absolute wall-clock time (COR-01). "
        "`inputPeerNotifySettings` is sparse — only the field being changed "
        "is sent, so the rest keeps inheriting the scope default. `--folder` "
        "costs one RPC per chat because Telegram has no batched form."
    ),
    aliases=("chat.unmute",),
    legacy_paths=("chat mute",),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("muted", "chat_ids", "mute_until"),
    example={"muted": True, "chat_id": 777123, "chat_ids": [777123]},
    example_args="chat mute @alice --for 8h",
    covers=(
        "dialogs.mute-folder",
        "dialogs.mute-for-duration",
        "dialogs.mute-forever",
        "dialogs.unmute",
        "groups-channels-admin.chat-notify-settings",
    ),
)


# ---------------------------------------------------------------------------
# chat pin
# ---------------------------------------------------------------------------


class PinReq(Request):
    chat: Annotated[
        list[PeerRef], arg(0, metavar="CHAT", variadic=True, kind="peer", help="Chats to pin.")
    ] = []
    unpin: Annotated[bool, opt("--unpin", help="Remove the pin instead.")] = False
    folder: Annotated[
        str, opt("--folder", metavar="FOLDER", help="Pin inside a chat folder, or main|archive.")
    ] = "main"
    order: Annotated[
        bool, opt("--order", help="Treat the arguments as the complete pinned order.")
    ] = False
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="Pin a Saved-Messages sublist."),
    ] = None


async def pin(ctx: OpContext, req: PinReq) -> PinnedDialogs:
    """Pin or unpin dialogs, or rewrite the pinned order outright.

    Pinning *inside a chat folder* is a different operation from pinning in
    the chat list: it edits the folder's `pinned_peers` rather than calling
    `toggleDialogPin`, and the folder's pinned and excluded lists must stay
    disjoint or the server rejects the filter.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    targets = await _resolve_many(ctx, req.chat)
    if not targets:
        raise UsageError("give at least one chat", field="chat")

    if req.saved_peer is not None:
        origin = await _send.resolve(ctx, req.saved_peer)
        await client(
            fn.ToggleSavedDialogPinRequest(
                peer=types.InputDialogPeer(origin), pinned=not req.unpin or None
            )
        )
        chat_id = _send.peer_id_of(origin)
        return PinnedDialogs(pinned=not req.unpin, chat_id=chat_id, chat_ids=[chat_id])

    chat_filter = await _read_filter(ctx, req.folder)
    if chat_filter is not None:
        return await _pin_in_folder(ctx, req, chat_filter, targets)

    folder_id = _peer_folder(req.folder) or FOLDER_MAIN
    if req.order:
        await client(
            fn.ReorderPinnedDialogsRequest(
                folder_id=folder_id,
                order=[types.InputDialogPeer(peer) for peer, _ in targets],
                force=True,
            )
        )
        ids = [chat_id for _, chat_id in targets]
        ctx.emit("chat_pin", {"chat_ids": ids, "order": True})
        return PinnedDialogs(pinned=True, chat_ids=ids, folder=req.folder, order=ids)

    for peer, _ in targets:
        await client(
            fn.ToggleDialogPinRequest(
                peer=types.InputDialogPeer(peer), pinned=not req.unpin or None
            )
        )
    ids = [chat_id for _, chat_id in targets]
    ctx.emit("chat_pin", {"chat_ids": ids, "pinned": not req.unpin})
    return PinnedDialogs(
        pinned=not req.unpin,
        chat_id=ids[0] if len(ids) == 1 else None,
        chat_ids=ids,
        folder=req.folder,
    )


async def _pin_in_folder(
    ctx: OpContext, req: PinReq, chat_filter: Any, targets: list[tuple[Any, int]]
) -> PinnedDialogs:
    from tlgr.ops.folder import folder_pinned_write

    ids = [chat_id for _, chat_id in targets]
    await folder_pinned_write(
        ctx, chat_filter, [peer for peer, _ in targets], unpin=req.unpin, order=req.order
    )
    ctx.emit("chat_pin", {"chat_ids": ids, "folder": req.folder})
    return PinnedDialogs(
        pinned=not req.unpin,
        chat_id=ids[0] if len(ids) == 1 else None,
        chat_ids=ids,
        folder=req.folder,
        order=ids if req.order else [],
    )


SPEC_PIN = OperationSpec(
    id="chat.pin",
    request=PinReq,
    response=PinnedDialogs,
    impl=pin,
    summary="Pin or unpin dialogs, or rewrite the whole pinned order",
    description=(
        "`--folder <name>` pins inside a chat folder, which is a filter edit "
        "rather than `toggleDialogPin`. PINNED_DIALOGS_TOO_MUCH is the "
        "server's answer when the pinned limit is reached."
    ),
    aliases=("chat.unpin", "chat.pin-order"),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("pinned", "chat_ids", "folder"),
    example={"pinned": True, "chat_id": 777123, "chat_ids": [777123], "folder": "main"},
    example_args="chat pin @alice",
    covers=("dialogs.pin", "dialogs.pin-in-folder", "dialogs.pin-reorder", "dialogs.unpin"),
)


# ---------------------------------------------------------------------------
# chat leave / delete / clear
# ---------------------------------------------------------------------------


class LeaveReq(Request):
    chat: Annotated[
        list[PeerRef],
        arg(0, metavar="CHAT", required=False, variadic=True, kind="peer", help="Chats to leave."),
    ] = []
    delete_history: Annotated[
        bool, opt("--delete-history", help="Also delete my copy of the history.")
    ] = False
    remove_from_folders: Annotated[
        bool, opt("--remove-from-folders", help="Strip the peer from every chat folder.")
    ] = False
    common_with: Annotated[
        PeerRef | None,
        opt("--common-with", metavar="USER", kind="user", help="Leave every group shared with."),
    ] = None


async def leave(ctx: OpContext, req: LeaveReq) -> LeaveResult:
    """Leave groups and channels, optionally cleaning up after yourself.

    A basic group's creator is asked about first: `getFutureChatCreatorAfterLeave`
    names who inherits it, and reporting that is the difference between
    leaving and abandoning.
    """
    from telethon.tl.functions import channels as cfn
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    targets = await _resolve_many(ctx, req.chat)
    if req.common_with is not None:
        user = await _send.resolve(ctx, req.common_with)
        common = await client(fn.GetCommonChatsRequest(user_id=user, max_id=0, limit=100))
        for chat in getattr(common, "chats", None) or []:
            peer = await _send.resolve(ctx, str(_marked(chat)))
            targets.append((peer, _marked(chat)))
    if not targets:
        raise UsageError("give a chat, or --common-with", field="chat")

    left: list[int] = []
    errors: list[PeerResult] = []
    successor: int | None = None
    for peer, chat_id in targets:
        try:
            if _is_channel(peer):
                await client(cfn.LeaveChannelRequest(channel=_input_channel(peer)))
            else:
                successor = await _basic_group_successor(ctx, peer) or successor
                await client(fn.DeleteChatUserRequest(chat_id=_raw_chat_id(peer), user_id="me"))
            if req.delete_history:
                await _affected_loop(
                    ctx,
                    lambda offset, p=peer: fn.DeleteHistoryRequest(peer=p, max_id=0, revoke=False),
                )
            left.append(chat_id)
            ctx.emit("chat_leave", {"chat_id": chat_id})
        except Exception as exc:
            errors.append(PeerResult(chat_id=chat_id, ok=False, error=_error_text(exc)))

    if req.remove_from_folders and left:
        await _strip_from_folders(ctx, left)

    return LeaveResult(
        left=bool(left),
        chat_id=left[0] if len(left) == 1 else None,
        chat_ids=left,
        errors=errors,
        successor=successor,
    )


def _marked(entity: Any) -> int:
    from telethon import utils

    return int(utils.get_peer_id(entity))


def _raw_chat_id(peer: Any) -> int:
    value = getattr(peer, "chat_id", None)
    if value is None:
        raise UsageError("this is not a basic group", field="chat")
    return int(value)


async def _basic_group_successor(ctx: OpContext, peer: Any) -> int | None:
    """Who inherits a basic group when its creator leaves, if anyone."""
    from telethon.tl.functions import messages as fn

    try:
        result = await _client(ctx)(fn.GetFutureChatCreatorAfterLeaveRequest(peer=peer))
    except Exception:
        return None
    return peer_id_of(getattr(result, "user_id", None)) or getattr(result, "user_id", None)


async def _strip_from_folders(ctx: OpContext, chat_ids: list[int]) -> None:
    """Drop the peers from every folder, so no folder points at a chat we left."""
    from tlgr.ops.folder import folder_model, raw_filters, strip_peers

    filters, _ = await raw_filters(ctx)
    wanted = set(chat_ids)
    for raw in filters:
        model = folder_model(raw)
        touched = wanted & (
            set(model.include_peers) | set(model.pinned_peers) | set(model.exclude_peers)
        )
        if touched:
            await strip_peers(ctx, raw, touched)


SPEC_LEAVE = OperationSpec(
    id="chat.leave",
    request=LeaveReq,
    response=LeaveResult,
    impl=leave,
    summary="Leave groups and channels",
    description=(
        "Bulk leaving is the CHANNELS_TOO_MUCH escape hatch — pair it with "
        "`chat list --scope inactive`, which names the chats you have not "
        "opened in the longest time."
    ),
    legacy_paths=("chat leave",),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("left", "chat_ids"),
    example={"left": True, "chat_id": -1000000005150, "chat_ids": [-1000000005150]},
    example_args="chat leave @somegroup",
    covers=(
        "dialogs.delete-and-leave",
        "dialogs.leave-group",
        "groups-channels-admin.leave",
        "groups-channels-admin.owner-leave-successor",
    ),
    covers_partial=("contacts-users.user-leave-common-groups",),
    coverage_note="`--common-with` leaves the shared groups; listing them is `user chat list`.",
)


class DeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to delete.")]
    for_both: Annotated[
        bool, opt("--for-both", help="Private chat / basic group: delete for the other side too.")
    ] = False
    for_everyone: Annotated[
        bool, opt("--for-everyone", help="Owner only: destroy the group or channel itself.")
    ] = False
    for_me: Annotated[
        bool, opt("--for-me", help="Leave it and wipe only my copy of the history.")
    ] = False


async def delete(ctx: OpContext, req: DeleteReq) -> DeleteChatResult:
    """Delete a chat: from my list, for both sides, or for everyone.

    `--for-everyone` destroys the group or channel and is owner-only and
    irreversible; the server gates it on `channelFull.can_delete_channel`,
    which is member-count limited, so a large channel refuses.
    """
    from telethon.tl.functions import channels as cfn
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.for_everyone:
        try:
            if _is_channel(peer):
                await client(cfn.DeleteChannelRequest(channel=_input_channel(peer)))
            else:
                await client(fn.DeleteChatRequest(chat_id=_raw_chat_id(peer)))
        except Exception as exc:
            if "ADMIN" in str(exc).upper() or "RIGHTS" in str(exc).upper():
                raise PermissionError_(
                    "deleting a group or channel for everyone is owner-only"
                ) from exc
            raise
        ctx.emit("chat_delete", {"chat_id": chat_id, "scope": "everyone"})
        return DeleteChatResult(chat_id=chat_id, deleted=True, scope="everyone")

    left = False
    if _is_channel(peer):
        await client(cfn.LeaveChannelRequest(channel=_input_channel(peer)))
        left = True
        if req.for_me or req.for_both:
            await _affected_loop(
                ctx,
                lambda offset, p=peer: cfn.DeleteHistoryRequest(
                    channel=_input_channel(p), max_id=0, for_everyone=False
                ),
            )
    else:
        await _affected_loop(
            ctx,
            lambda offset, p=peer: fn.DeleteHistoryRequest(
                peer=p, max_id=0, revoke=req.for_both or None
            ),
        )
    scope = "both" if req.for_both else "me"
    ctx.emit("chat_delete", {"chat_id": chat_id, "scope": scope})
    return DeleteChatResult(chat_id=chat_id, deleted=True, scope=scope, left=left)


SPEC_DELETE = OperationSpec(
    id="chat.delete",
    request=DeleteReq,
    response=DeleteChatResult,
    impl=delete,
    summary="Delete a chat: for me, for both sides, or for everyone",
    description=(
        "`--for-everyone` is owner-only and destroys the chat itself. "
        "Leaving a channel does not delete the history for anyone else."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("chat_id", "deleted", "scope"),
    example={"chat_id": 777123, "deleted": True, "scope": "me"},
    example_args="chat delete @alice --yes",
    covers=(
        "dialogs.delete-chat-private",
        "dialogs.delete-group-for-all",
        "groups-channels-admin.delete-basic-group",
        "groups-channels-admin.delete-channel",
        "groups-channels-admin.delete-for-all-members",
        "messages-core.history-delete-conversation",
        "messages-core.saved-dialog-delete",
    ),
)


class ClearReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to clear.")]
    for_both: Annotated[
        bool, opt("--for-both", help="Also delete for the other side (revoke).")
    ] = False
    max_id: Annotated[
        int | None, opt("--max-id", metavar="ID", kind="msg_id", help="Only up to this id.")
    ] = None
    since: Annotated[
        str | None,
        opt("--since", metavar="TS", kind="datetime", help="Range start (private chats only)."),
    ] = None
    until: Annotated[
        str | None,
        opt("--until", metavar="TS", kind="datetime", help="Range end (private chats only)."),
    ] = None
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="Clear a Saved-Messages sublist."),
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Clear one forum topic.")
    ] = None


async def clear(ctx: OpContext, req: ClearReq) -> ClearResult:
    """Clear a chat's history while keeping the chat itself.

    `just_clear` is what separates this from `chat delete`. The server
    answers a long history with a partial result and an offset to resume
    from, so the call is looped until it reports nothing left — clearing the
    first hundred messages and calling it done was v1's bug.
    """
    from telethon.tl.functions import channels as cfn
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    max_id = req.max_id or 0

    if req.saved_peer is not None:
        origin = await _send.resolve(ctx, req.saved_peer)
        affected = await _affected_loop(
            ctx,
            lambda offset, p=peer, o=origin: fn.DeleteSavedHistoryRequest(
                peer=o, max_id=max_id, parent_peer=p
            ),
        )
    elif req.topic is not None:
        affected = await _affected_loop(
            ctx,
            lambda offset, p=peer: fn.DeleteTopicHistoryRequest(peer=p, top_msg_id=req.topic),
        )
    elif _is_channel(peer):
        if req.since or req.until:
            raise UsageError(
                "a date range only works in private chats and basic groups "
                "(the server answers CHAT_REVOKE_DATE_UNSUPPORTED elsewhere)",
                field="since",
            )
        affected = await _affected_loop(
            ctx,
            lambda offset, p=peer: cfn.DeleteHistoryRequest(
                channel=_input_channel(p), max_id=max_id, for_everyone=req.for_both or None
            ),
        )
    else:
        affected = await _affected_loop(
            ctx,
            lambda offset, p=peer: fn.DeleteHistoryRequest(
                peer=p,
                max_id=max_id,
                just_clear=True,
                revoke=req.for_both or None,
                min_date=parse_dt(req.since) if req.since else None,
                max_date=parse_dt(req.until) if req.until else None,
            ),
        )

    ctx.emit("chat_clear", {"chat_id": chat_id, "for_both": req.for_both})
    return ClearResult(chat_id=chat_id, cleared=True, messages_affected=affected)


SPEC_CLEAR = OperationSpec(
    id="chat.clear",
    request=ClearReq,
    response=ClearResult,
    impl=clear,
    summary="Clear a chat's history — for me, for both sides, or a date range",
    description=(
        "Irreversible. Date ranges are private-chat and basic-group only; "
        "supergroups clear through `channels.deleteHistory`, where clearing "
        "for everyone needs admin rights."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    timeout_s=600,
    columns=("chat_id", "cleared", "messages_affected"),
    example={"chat_id": 777123, "cleared": True, "messages_affected": 412},
    example_args="chat clear @alice --yes",
    covers=(
        "dialogs.clear-history-both",
        "dialogs.clear-history-by-date",
        "dialogs.clear-history-self",
        "groups-channels-admin.clear-history",
        "messages-core.history-clear",
        "messages-core.history-clear-date-range",
        "messages-core.history-clear-topic",
    ),
)


# ---------------------------------------------------------------------------
# chat typing
# ---------------------------------------------------------------------------


class TypingReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to type in.")]
    action: Annotated[
        str,
        choice(*sorted(_ACTIONS), help="Which action to broadcast."),
    ] = "typing"
    cancel: Annotated[bool, opt("--cancel", help="Alias for --action cancel.")] = False
    duration: Annotated[
        float, opt("--duration", metavar="SECONDS", help="Keep it alive this long.", ge=0)
    ] = 5.0
    progress: Annotated[
        int | None,
        opt("--progress", metavar="PERCENT", help="Percent, for the upload actions."),
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="A forum topic.")
    ] = None


async def typing(ctx: OpContext, req: TypingReq) -> TypingResult:
    """Broadcast a chat action, and hold it for a while.

    Telegram expires an action after about six seconds, so a `--duration`
    longer than that is re-sent every five: the alternative is an indicator
    that flickers off halfway through.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    action_name = "cancel" if req.cancel else req.action
    class_name = _ACTIONS.get(action_name)
    if class_name is None:
        raise UsageError(f"--action {action_name!r} is not a chat action", field="action")

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    kwargs: dict[str, Any] = {}
    if req.progress is not None and "Upload" in class_name:
        kwargs["progress"] = req.progress
    action = getattr(types, class_name)(**kwargs)

    client = _client(ctx)
    seconds = 0.0 if action_name == "cancel" else min(float(req.duration), 300.0)
    deadline = seconds
    while True:
        await client(fn.SetTypingRequest(peer=peer, action=action, top_msg_id=req.topic))
        if deadline <= 5.0:
            break
        await asyncio.sleep(5.0)
        deadline -= 5.0
    if 0 < deadline <= 5.0:
        await asyncio.sleep(deadline)

    ctx.emit("chat_typing", {"chat_id": chat_id, "action": action_name})
    return TypingResult(
        chat_id=chat_id, action=action_name, duration=seconds, typing=action_name != "cancel"
    )


SPEC_TYPING = OperationSpec(
    id="chat.typing",
    request=TypingReq,
    response=TypingResult,
    impl=typing,
    summary="Send or cancel a chat action (typing, recording, uploading)",
    description=(
        "Seeing who else is typing is `watch --events typing`; this is the outgoing half."
    ),
    legacy_paths=("chat typing",),
    mutating=True,
    rate_class="send",
    timeout_s=330,
    columns=("chat_id", "action", "duration"),
    example={"chat_id": 777123, "action": "typing", "duration": 5.0, "typing": True},
    example_args="chat typing @alice",
    covers=(
        "dialogs.typing-cancel",
        "dialogs.typing-send",
        "groupcall.speaking-indicator",
        "messages-core.typing-action",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# chat mention list
# ---------------------------------------------------------------------------


class MentionListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to look in.")]
    kind: Annotated[
        str, choice("mention", "reaction", "poll-vote", help="Which unread queue to list.")
    ] = "mention"
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Restrict to a topic.")
    ] = None
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="Restrict to a saved sublist."),
    ] = None
    read: Annotated[bool, opt("--read", help="Mark the listed items read afterwards.")] = False


async def mention_list(ctx: OpContext, req: MentionListReq) -> Page[Message]:
    """The three unread queues that sit next to `unread_count`.

    Mentions, reactions and poll votes each have their own counter on the
    dialog and their own list call. `--read` clears the one you listed, which
    is the only way to make the badge agree with what you have seen.
    """
    from telethon.tl.functions import messages as fn

    limit, state = _window(ctx, "chat.mention.list", PageKind.HISTORY, default=20)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    offset_id = int(state.get("offset_id", 0) or 0)
    client = _client(ctx)

    if req.kind == "reaction":
        saved = await _send.resolve(ctx, req.saved_peer) if req.saved_peer is not None else None
        result = await client(
            fn.GetUnreadReactionsRequest(
                peer=peer,
                offset_id=offset_id,
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=0,
                top_msg_id=req.topic,
                saved_peer_id=saved,
            )
        )
    elif req.kind == "poll-vote":
        result = await client(
            fn.GetUnreadPollVotesRequest(
                peer=peer,
                offset_id=offset_id,
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=0,
                top_msg_id=req.topic,
            )
        )
    else:
        result = await client(
            fn.GetUnreadMentionsRequest(
                peer=peer,
                offset_id=offset_id,
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=0,
                top_msg_id=req.topic,
            )
        )

    items = [
        message_to_model(message, chat_id=chat_id)
        for message in (getattr(result, "messages", None) or [])
    ]
    if req.read and items:
        if req.kind == "reaction":
            await _affected_loop(ctx, lambda offset, p=peer: fn.ReadReactionsRequest(peer=p))
        elif req.kind == "poll-vote":
            await client(fn.ReadPollVotesRequest(peer=peer, top_msg_id=req.topic))
        else:
            await _affected_loop(ctx, lambda offset, p=peer: fn.ReadMentionsRequest(peer=p))

    next_state = {"offset_id": items[-1].id} if items else {}
    return build_page(
        items,
        op="chat.mention.list",
        kind=PageKind.HISTORY,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_MENTION_LIST = OperationSpec(
    id="chat.mention.list",
    request=MentionListReq,
    response=Page[Message],
    impl=mention_list,
    summary="Unread mentions, reactions or poll votes of a chat",
    aliases=("chat.mentions", "chat.reactions", "chat.poll-votes"),
    paginated=PageKind.HISTORY,
    mutating=True,
    idempotent=True,
    columns=("id", "date", "text"),
    headers=("ID", "Date", "Text"),
    example={
        "items": [
            {
                "id": 12345,
                "chat_id": 777123,
                "date": "2026-09-03T09:14:07Z",
                "date_unix": 1788340447,
                "text": "@me look",
            }
        ],
        "has_more": False,
    },
    example_args="chat mention list @somegroup",
    covers=(
        "dialogs.unread-mentions",
        "dialogs.unread-poll-votes",
        "dialogs.unread-reactions",
    ),
)


# ---------------------------------------------------------------------------
# chat poster list
# ---------------------------------------------------------------------------


class PosterListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to harvest.")]
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Scan window start.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Scan window end.")
    ] = None
    min_messages: Annotated[
        int, opt("--min-messages", metavar="N", help="Drop senders below this count.", ge=1)
    ] = 1
    max_messages: Annotated[
        int,
        opt("--max-messages", metavar="N", help="How much history to walk.", ge=1, le=20000),
    ] = 2000


async def poster_list(ctx: OpContext, req: PosterListReq) -> PosterReport:
    """Distinct senders in a chat's recent history, by message count.

    The walk is internal: every agent that hand-rolled this loop got the
    offsets or the flood backoff wrong. A flood wait mid-scan returns the
    partial harvest with `partial: true` rather than an error, because half
    the senders is a useful answer and an exception is not.
    """
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    client = _client(ctx)
    limit = int(getattr(ctx, "limit", None) or 0)

    since = parse_dt(req.since) if req.since else None
    until = parse_dt(req.until) if req.until else None

    counts: dict[int, Poster] = {}
    scanned = 0
    partial = False
    flood_wait: int | None = None
    try:
        async for message in client.iter_messages(peer, limit=req.max_messages, offset_date=until):
            if message is None:
                continue
            scanned += 1
            date = getattr(message, "date", None)
            if since is not None and date is not None and date < since:
                break
            sender_id = peer_id_of(getattr(message, "from_id", None))
            if sender_id is None:
                sender_id = peer_id_of(getattr(message, "peer_id", None))
            if sender_id is None:
                continue
            poster = counts.get(sender_id)
            if poster is None:
                poster = Poster(user_id=sender_id, id=sender_id)
                counts[sender_id] = poster
            poster.count += 1
            if poster.last_msg_id is None:
                poster.last_msg_id = int(getattr(message, "id", 0) or 0)
                poster.date = fmt_dt(date)
                poster.date_unix = to_unix(date)
    except Exception as exc:
        seconds = getattr(exc, "seconds", None)
        if seconds is None:
            raise
        partial = True
        flood_wait = int(seconds)
        ctx.warn(f"flood wait of {seconds}s cut the scan short; the harvest is partial")

    await _name_posters(ctx, counts)
    posters = sorted(
        (p for p in counts.values() if p.count >= req.min_messages),
        key=lambda p: (-p.count, p.user_id),
    )
    if limit:
        posters = posters[:limit]
    ctx.emit("chat_posters", {"chat_id": chat_id, "scanned": scanned})
    return PosterReport(
        posters=posters,
        scanned_messages=scanned,
        distinct_posters=len(counts),
        partial=partial,
        flood_wait=flood_wait,
    )


async def _name_posters(ctx: OpContext, counts: dict[int, Poster]) -> None:
    """Resolve each distinct sender once; a failure leaves the id, not a hole."""
    client = _client(ctx)
    for sender_id, poster in counts.items():
        try:
            entity = await client.get_entity(sender_id)
        except Exception:
            continue
        shape = entity_to_peer(entity)
        poster.username = shape.username
        poster.name = shape.title
        poster.is_bot = shape.kind == "bot"
        poster.is_deleted = bool(getattr(entity, "deleted", False))


SPEC_POSTER_LIST = OperationSpec(
    id="chat.poster.list",
    request=PosterListReq,
    response=PosterReport,
    impl=poster_list,
    summary="Harvest the senders that posted in a chat over a message window",
    description=(
        "Pagination is internal — do not hand-roll the walk. Senders are not "
        "always users: an anonymous admin and a linked channel post under a "
        "negative channel id, so filter to positive ids when harvesting people."
    ),
    aliases=("chat.posters",),
    legacy_paths=("chat posters",),
    timeout_s=600,
    rate_class="bulk",
    empty_exit=EXIT_EMPTY,
    columns=("posters.user_id", "posters.name", "posters.count"),
    example={
        "posters": [{"user_id": 4242, "id": 4242, "name": "Alice", "count": 44}],
        "scanned_messages": 2400,
        "distinct_posters": 137,
    },
    example_args="chat poster list @somegroup",
    covers=(),
    covers_partial=(),
    tags=frozenset({"infrastructure"}),
)


# ---------------------------------------------------------------------------
# chat notify
# ---------------------------------------------------------------------------


class NotifyGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to describe.")]
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="A forum topic.")
    ] = None
    effective: Annotated[
        bool, opt("--effective", help="Merge the scope default under the exception.")
    ] = True


async def notify_get(ctx: OpContext, req: NotifyGetReq) -> NotifyView:
    """A chat's notification exception, and what it resolves to.

    `peerNotifySettings` fields are ternary: unset means "inherit the scope
    default". Both halves are reported because they answer different
    questions — the exception is what you must send back, the effective value
    is what the user is actually asking about.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    notify_peer = (
        types.InputNotifyForumTopic(peer=peer, top_msg_id=req.topic)
        if req.topic is not None
        else types.InputNotifyPeer(peer=peer)
    )
    raw = await client(fn.GetNotifySettingsRequest(peer=notify_peer))
    settings = notify_settings(raw) or NotifySettings()

    scope_name, scope_peer = _scope_for(peer)
    view = NotifyView(chat_id=chat_id, settings=settings, scope=scope_name)
    if req.effective:
        default_raw = await client(fn.GetNotifySettingsRequest(peer=scope_peer))
        default = notify_settings(default_raw) or NotifySettings()
        view.scope_default = default
        merged, inherited = _merge_notify(settings, default)
        view.effective = merged
        view.inherited = inherited
    return view


def _scope_for(peer: Any) -> tuple[str, Any]:
    """Which notification scope a peer inherits from."""
    from telethon.tl import types

    name = type(peer).__name__
    if name in ("InputPeerChannel", "InputPeerChannelFromMessage"):
        return "broadcasts", types.InputNotifyBroadcasts()
    if name == "InputPeerChat":
        return "chats", types.InputNotifyChats()
    return "users", types.InputNotifyUsers()


def _merge_notify(
    exception: NotifySettings, default: NotifySettings
) -> tuple[NotifySettings, list[str]]:
    merged = NotifySettings(
        muted=exception.muted,
        mute_until=exception.mute_until,
        mute_until_unix=exception.mute_until_unix,
    )
    inherited: list[str] = []
    for field in ("silent", "show_previews", "sound", "stories_muted", "stories_hide_sender"):
        value = getattr(exception, field)
        if value is None:
            value = getattr(default, field)
            inherited.append(field)
        setattr(merged, field, value)
    if exception.mute_until_unix is None:
        merged.muted = default.muted
        merged.mute_until = default.mute_until
        merged.mute_until_unix = default.mute_until_unix
        inherited.append("mute_until")
    return merged, inherited


SPEC_NOTIFY_GET = OperationSpec(
    id="chat.notify.get",
    request=NotifyGetReq,
    response=NotifyView,
    impl=notify_get,
    summary="Show a chat's notification settings, exception and effective value",
    columns=("chat_id", "settings.muted", "scope"),
    example={"chat_id": 777123, "settings": {"muted": True}, "scope": "users"},
    example_args="chat notify get @alice",
    covers=("dialogs.notify-get",),
)


class NotifySetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to change.")]
    preview: Annotated[
        str | None, choice("on", "off", "default", help="Message preview in notifications.")
    ] = None
    silent: Annotated[
        str | None, choice("on", "off", "default", help="Deliver without a sound.")
    ] = None
    sound: Annotated[
        str | None,
        opt("--sound", metavar="SOUND", help="none | default | <ringtone id> | local:<name>."),
    ] = None
    stories_mute: Annotated[
        str | None, choice("on", "off", "default", help="Mute this peer's stories.")
    ] = None
    stories_hide_sender: Annotated[
        str | None, choice("on", "off", "default", help="Hide the sender on story alerts.")
    ] = None
    gifts: Annotated[
        str | None, choice("on", "off", help="Star-gift notifications for a channel you admin.")
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="A forum topic.")
    ] = None
    reset: Annotated[
        bool, opt("--reset", help="Drop the exception and inherit the scope default.")
    ] = False


async def notify_set(ctx: OpContext, req: NotifySetReq) -> NotifyView:
    """Set one chat's notification exception, one field at a time.

    `inputPeerNotifySettings` only carries the fields you set, and `default`
    is expressed by *omitting* one — which is why every value here is a
    three-way choice rather than a boolean flag.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn
    from telethon.tl.functions import payments as pfn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    notify_peer = (
        types.InputNotifyForumTopic(peer=peer, top_msg_id=req.topic)
        if req.topic is not None
        else types.InputNotifyPeer(peer=peer)
    )

    kwargs: dict[str, Any] = {}
    if not req.reset:
        _tri(kwargs, "show_previews", req.preview)
        _tri(kwargs, "silent", req.silent)
        _tri(kwargs, "stories_muted", req.stories_mute)
        _tri(kwargs, "stories_hide_sender", req.stories_hide_sender)
        if req.sound is not None:
            kwargs["sound"] = _sound_value(req.sound)

    if req.gifts is not None:
        await client(
            pfn.ToggleChatStarGiftNotificationsRequest(peer=peer, enabled=req.gifts == "on")
        )
    if kwargs or req.reset:
        await client(
            fn.UpdateNotifySettingsRequest(
                peer=notify_peer, settings=types.InputPeerNotifySettings(**kwargs)
            )
        )
    ctx.emit("chat_notify", {"chat_id": chat_id})
    return await notify_get(ctx, NotifyGetReq(chat=req.chat, topic=req.topic, effective=True))


def _tri(kwargs: dict[str, Any], name: str, value: str | None) -> None:
    """`on`/`off` set the field; `default` and absence leave it unset."""
    if value in (None, "default"):
        return
    kwargs[name] = value == "on"


def _sound_value(text: str) -> Any:
    from telethon.tl import types

    value = text.strip()
    if value in ("none", "off", "silent"):
        return types.NotificationSoundNone()
    if value in ("default", ""):
        return types.NotificationSoundDefault()
    if value.startswith("local:"):
        title = value.split(":", 1)[1]
        return types.NotificationSoundLocal(title=title, data=title)
    try:
        return types.NotificationSoundRingtone(id=int(value))
    except ValueError as exc:
        raise UsageError(
            "--sound takes none, default, a ringtone id, or local:<name>", field="sound"
        ) from exc


SPEC_NOTIFY_SET = OperationSpec(
    id="chat.notify.set",
    request=NotifySetReq,
    response=NotifyView,
    impl=notify_set,
    summary="Set one chat's notification exception",
    description=(
        "Every switch takes on|off|default, because `default` is a real third "
        "state: it removes the exception so the chat inherits the scope again."
    ),
    mutating=True,
    idempotent=True,
    columns=("chat_id", "settings.muted"),
    example={"chat_id": 777123, "settings": {"muted": False, "silent": True}},
    example_args="chat notify set @alice --silent on",
    covers=(
        "dialogs.gift-notifications",
        "dialogs.notify-preview",
        "dialogs.notify-silent",
    ),
)


# ---------------------------------------------------------------------------
# chat ttl / theme / wallpaper / translate / set
# ---------------------------------------------------------------------------


class TtlSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    period: Annotated[
        str | None,
        arg(
            1, metavar="PERIOD", required=False, help="1d | 1w | 1m | seconds | off; omit to show."
        ),
    ] = None


async def ttl_set(ctx: OpContext, req: TtlSetReq) -> TtlResult:
    """Set — or, with no period, show — a chat's auto-delete timer.

    Either side may set it in a private chat; a group or channel needs the
    change-info right. Only server-accepted values work, and the server says
    `TTL_PERIOD_INVALID` about the rest rather than rounding.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.period is None:
        result = await client(fn.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)]))
        for row in getattr(result, "dialogs", None) or []:
            return TtlResult(chat_id=chat_id, ttl_period=getattr(row, "ttl_period", None))
        return TtlResult(chat_id=chat_id)

    text = req.period.strip().lower()
    period = 0 if text in ("off", "0", "none") else (parse_duration(text) or 0)
    if text not in ("off", "0", "none") and not period:
        raise UsageError(f"cannot read {req.period!r} as a duration", field="period")
    await client(fn.SetHistoryTTLRequest(peer=peer, period=period))
    ctx.emit("chat_ttl", {"chat_id": chat_id, "ttl_period": period})
    return TtlResult(chat_id=chat_id, ttl_period=period or None, set=True)


SPEC_TTL_SET = OperationSpec(
    id="chat.ttl.set",
    request=TtlSetReq,
    response=TtlResult,
    impl=ttl_set,
    summary="Set or show a chat's auto-delete timer",
    mutating=True,
    idempotent=True,
    columns=("chat_id", "ttl_period"),
    example={"chat_id": 777123, "ttl_period": 86400, "set": True},
    example_args="chat ttl set @alice 1d",
    covers=("dialogs.ttl-get", "dialogs.ttl-set"),
)


class ThemeListReq(Request):
    gifts: Annotated[
        bool, opt("--gifts", help="Collectible gift themes instead of emoji ones.")
    ] = False


async def theme_list(ctx: OpContext, req: ThemeListReq) -> Page[ChatTheme]:
    """The chat themes this account may set."""
    from telethon.tl.functions import account as fn

    limit, state = _window(ctx, "chat.theme.list", PageKind.RATE, default=50)
    client = _client(ctx)
    if req.gifts:
        result = await client(
            fn.GetUniqueGiftChatThemesRequest(
                offset=str(state.get("offset", "")), limit=limit, hash=0
            )
        )
        raw = list(getattr(result, "themes", None) or [])
        next_offset = getattr(result, "next_offset", None)
        items = [t for t in (chat_theme(theme) for theme in raw) if t is not None]
        return build_page(
            items,
            op="chat.theme.list",
            kind=PageKind.RATE,
            state={"offset": next_offset} if next_offset else {},
            account=ctx.account,
            has_more=bool(next_offset),
        )

    result = await client(fn.GetChatThemesRequest(hash=0))
    raw = list(getattr(result, "themes", None) or [])
    items = [t for t in (chat_theme(theme) for theme in raw) if t is not None]
    return Page(items=items, has_more=False, total=len(items))


SPEC_THEME_LIST = OperationSpec(
    id="chat.theme.list",
    request=ThemeListReq,
    response=Page[ChatTheme],
    impl=theme_list,
    summary="List the chat themes available",
    paginated=PageKind.RATE,
    columns=("emoticon", "title"),
    headers=("Emoji", "Theme"),
    example={"items": [{"emoticon": "🌷", "title": "🌷"}], "has_more": False},
    example_args="chat theme list",
    covers=("dialogs.chat-theme-list",),
)


class ThemeSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    emoji: Annotated[
        str | None, opt("--emoji", metavar="EMOJI", help="Emoji theme from `chat theme list`.")
    ] = None
    gift: Annotated[
        str | None, opt("--gift", metavar="SLUG", help="Collectible gift theme slug.")
    ] = None
    unset: Annotated[bool, opt("--unset", help="Remove the per-chat theme.")] = False


async def theme_set(ctx: OpContext, req: ThemeSetReq) -> ThemeResult:
    """Set or clear one chat's theme. Both sides of the chat see it."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.unset or not (req.emoji or req.gift):
        if not req.unset:
            raise UsageError("give --emoji, --gift, or --unset", field="emoji")
        theme: Any = types.InputChatThemeEmpty()
        model = None
    elif req.gift:
        theme = types.InputChatThemeUniqueGift(slug=req.gift)
        model = ChatTheme(gift_slug=req.gift, title=req.gift)
    else:
        theme = types.InputChatTheme(emoticon=req.emoji or "")
        model = ChatTheme(emoticon=req.emoji, title=req.emoji or "")

    await _client(ctx)(fn.SetChatThemeRequest(peer=peer, theme=theme))
    ctx.emit("chat_theme", {"chat_id": chat_id, "theme": req.emoji or req.gift})
    return ThemeResult(chat_id=chat_id, theme=model)


SPEC_THEME_SET = OperationSpec(
    id="chat.theme.set",
    request=ThemeSetReq,
    response=ThemeResult,
    impl=theme_set,
    summary="Set or remove the theme of one chat",
    aliases=("chat.theme.unset",),
    mutating=True,
    idempotent=True,
    columns=("chat_id", "theme.emoticon"),
    example={"chat_id": 777123, "theme": {"emoticon": "🌷", "title": "🌷"}},
    example_args="chat theme set @alice --emoji 🌷",
    covers=(
        "dialogs.chat-theme-reset",
        "gift.as-chat-theme",
        "gifts.set-as-chat-theme",
        "theme.set-chat-theme",
    ),
    tags=frozenset({"visible-to-others"}),
)


class WallpaperSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    slug: Annotated[
        str | None, opt("--slug", metavar="SLUG", help="Wallpaper slug from the gallery.")
    ] = None
    file: Annotated[
        str | None, opt("--file", metavar="PATH", kind="path", help="Upload a local image.")
    ] = None
    color: Annotated[
        str | None, opt("--color", metavar="HEX", help="Solid or gradient fill, '#rrggbb'.")
    ] = None
    blur: Annotated[bool, opt("--blur", help="Blur the background.")] = False
    intensity: Annotated[
        int | None, opt("--intensity", metavar="N", help="Pattern intensity, -100..100.")
    ] = None
    for_both: Annotated[bool, opt("--for-both", help="Apply on the other side too (Premium).")] = (
        False
    )
    from_message: Annotated[
        int | None,
        opt("--from-message", metavar="ID", kind="msg_id", help="Accept a suggested wallpaper."),
    ] = None
    revert: Annotated[bool, opt("--revert", help="Restore my previous wallpaper.")] = False
    unset: Annotated[bool, opt("--unset", help="Remove the chat wallpaper.")] = False


async def wallpaper_set(ctx: OpContext, req: WallpaperSetReq) -> WallpaperResult:
    """Set, accept, revert or remove one chat's wallpaper.

    `--from-message` is the "same" acknowledgement: it passes the service
    message id with no wallpaper of its own, which is how a client accepts
    the background the other side suggested.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    paper: Any = None
    settings: Any = None
    if req.file:
        upload = getattr(ctx, "upload_file", None)
        if upload is None:  # pragma: no cover - the daemon always supplies one
            raise UsageError("this context cannot upload files")
        handle = await upload(req.file)
        settings = _wallpaper_settings(req)
        uploaded = await client(
            afn.UploadWallPaperRequest(file=handle, mime_type="image/jpeg", settings=settings)
        )
        paper = types.InputWallPaper(
            id=getattr(uploaded, "id", 0), access_hash=getattr(uploaded, "access_hash", 0)
        )
    elif req.slug:
        paper = types.InputWallPaperSlug(slug=req.slug)
        settings = _wallpaper_settings(req)
    elif req.color:
        paper = types.InputWallPaperNoFile(id=0)
        settings = _wallpaper_settings(req)

    result = await client(
        fn.SetChatWallPaperRequest(
            peer=peer,
            for_both=req.for_both or None,
            revert=req.revert or None,
            wallpaper=paper,
            settings=settings,
            id=req.from_message,
        )
    )
    model = None
    for update in getattr(result, "updates", None) or []:
        found = getattr(update, "wallpaper", None)
        if found is not None:
            model = wallpaper(found)
            break
    if model is None and paper is not None:
        model = Wallpaper(slug=req.slug, blur=req.blur, intensity=req.intensity)
    ctx.emit("chat_wallpaper", {"chat_id": chat_id})
    return WallpaperResult(
        chat_id=chat_id,
        wallpaper=None if req.unset else model,
        for_both=req.for_both,
        overridden=req.revert,
    )


def _wallpaper_settings(req: WallpaperSetReq) -> Any:
    from telethon.tl import types

    colors = [_hex(part) for part in (req.color or "").split("-") if part.strip()]
    return types.WallPaperSettings(
        blur=req.blur or None,
        intensity=req.intensity,
        background_color=colors[0] if colors else None,
        second_background_color=colors[1] if len(colors) > 1 else None,
    )


def _hex(value: str) -> int:
    try:
        return int(value.strip().lstrip("#"), 16)
    except ValueError as exc:
        raise UsageError(f"--color: {value!r} is not a hex colour", field="color") from exc


SPEC_WALLPAPER_SET = OperationSpec(
    id="chat.wallpaper.set",
    request=WallpaperSetReq,
    response=WallpaperResult,
    impl=wallpaper_set,
    summary="Set, apply, revert or remove the wallpaper of one chat",
    aliases=("chat.wallpaper.unset",),
    mutating=True,
    idempotent=True,
    rate_class="file",
    timeout_s=300,
    columns=("chat_id", "wallpaper.slug"),
    example={"chat_id": 777123, "wallpaper": {"slug": "pattern"}, "for_both": False},
    example_args="chat wallpaper set @alice --slug pattern",
    covers=(
        "contacts-users.user-wallpaper",
        "dialogs.chat-wallpaper-apply-suggested",
        "dialogs.chat-wallpaper-revert",
        "stories.story-set-wallpaper",
        "wallpaper.set-for-channel-group",
        "wallpaper.set-for-chat",
    ),
    tags=frozenset({"visible-to-others"}),
)


class TranslateReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    state: Annotated[str, arg(1, metavar="STATE", help="on | off.")]


async def translate(ctx: OpContext, req: TranslateReq) -> TranslateResult:
    """Turn Telegram's translation bar on or off for one chat.

    `off` stores `translations_disabled=true` — the GUI's "Don't translate".
    Translating actual messages is `message translate`.
    """
    from telethon.tl.functions import messages as fn

    state = req.state.strip().lower()
    if state not in ("on", "off"):
        raise UsageError("state must be 'on' or 'off'", field="state")
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    disabled = state == "off"
    await _client(ctx)(fn.TogglePeerTranslationsRequest(peer=peer, disabled=disabled or None))
    ctx.emit("chat_translate", {"chat_id": chat_id, "disabled": disabled})
    return TranslateResult(chat_id=chat_id, translations_disabled=disabled)


SPEC_TRANSLATE = OperationSpec(
    id="chat.translate",
    request=TranslateReq,
    response=TranslateResult,
    impl=translate,
    summary="Turn Telegram's translation bar on or off for a chat",
    mutating=True,
    idempotent=True,
    columns=("chat_id", "translations_disabled"),
    example={"chat_id": 777123, "translations_disabled": True},
    example_args="chat translate @alice off",
    covers=(
        "dialogs.translate-toggle",
        "lang.chat-autotranslate",
        "messages-core.translate-chat-toggle",
    ),
)


class SetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    sharing: Annotated[
        str | None, choice("on", "off", help="Allow forwarding and saving from this chat.")
    ] = None
    request_msg: Annotated[
        int | None,
        opt("--request-msg", metavar="ID", kind="msg_id", help="The message that asked."),
    ] = None
    view_as: Annotated[
        str | None, choice("topics", "messages", help="Show a forum as topics or one list.")
    ] = None
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Default identity to post as."),
    ] = None
    send_as_list: Annotated[
        bool, opt("--send-as-list", help="List the identities you may post as and stop.")
    ] = False


async def set_chat(ctx: OpContext, req: SetReq) -> ChatSwitches:
    """The per-dialog switches: content sharing, forum view mode, send-as.

    Send-as is a per-*dialog* setting rather than a per-message flag — it is
    mirrored in chatFull/channelFull — which is why it lives here and not in
    `message send`.
    """
    from telethon.tl.functions import channels as cfn
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    out = ChatSwitches(chat_id=chat_id)

    if req.send_as_list:
        result = await client(cfn.GetSendAsRequest(peer=peer))
        out.send_as_options = [
            entity_to_peer(entity) for entity in (getattr(result, "chats", None) or [])
        ] + [entity_to_peer(entity) for entity in (getattr(result, "users", None) or [])]
        return out

    if req.sharing is not None:
        await client(
            fn.ToggleNoForwardsRequest(
                peer=peer, enabled=req.sharing == "off", request_msg_id=req.request_msg
            )
        )
        out.noforwards = req.sharing == "off"
    if req.view_as is not None:
        await client(
            cfn.ToggleViewForumAsMessagesRequest(
                channel=_input_channel(peer), enabled=req.view_as == "messages"
            )
        )
        out.view_forum_as_messages = req.view_as == "messages"
    if req.send_as is not None:
        identity = await _send.resolve(ctx, req.send_as)
        await client(fn.SaveDefaultSendAsRequest(peer=peer, send_as=identity))
        out.default_send_as = _send.peer_id_of(identity)
    if req.sharing is None and req.view_as is None and req.send_as is None:
        raise UsageError("give --sharing, --view-as, --send-as or --send-as-list", field="sharing")
    ctx.emit("chat_set", {"chat_id": chat_id})
    return out


SPEC_SET = OperationSpec(
    id="chat.set",
    request=SetReq,
    response=ChatSwitches,
    impl=set_chat,
    summary="Per-dialog switches: content sharing, forum view mode, send-as",
    description=(
        "`--sharing off` is `noforwards`, which needs Premium in a private "
        "chat and owner rights elsewhere. `--send-as-list` reports the "
        "identities available and changes nothing."
    ),
    mutating=True,
    idempotent=True,
    tags=frozenset({"mutating-checked"}),
    columns=("chat_id", "noforwards", "default_send_as"),
    example={"chat_id": 777123, "noforwards": True},
    example_args="chat set @alice --sharing off",
    covers=(
        "dialogs.no-forwards-private",
        "dialogs.send-as-default",
        "dialogs.view-as-topics",
    ),
)


# ---------------------------------------------------------------------------
# chat action-bar / badge / promo
# ---------------------------------------------------------------------------


class ActionBarGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    hide: Annotated[bool, opt("--hide", help="Dismiss the bar.")] = False


async def action_bar_get(ctx: OpContext, req: ActionBarGetReq) -> ActionBar:
    """The anti-spam info box Telegram draws above a chat with a stranger.

    `registration_month`, `phone_country`, `name_change_date` and
    `photo_change_date` are the strongest cold-outreach triage signals a
    client is ever given, and no other call reports them; `geo_distance`
    appears when the peer found you through People Nearby.
    """
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    result = await client(fn.GetPeerSettingsRequest(peer=peer))
    settings = getattr(result, "settings", result)
    model = action_bar(settings, chat_id=chat_id)

    if req.hide:
        await client(fn.HidePeerSettingsBarRequest(peer=peer))
        model.hidden = True
        ctx.emit("chat_action_bar", {"chat_id": chat_id, "hidden": True})
    return model


SPEC_ACTION_BAR_GET = OperationSpec(
    id="chat.action-bar.get",
    request=ActionBarGetReq,
    response=ActionBar,
    impl=action_bar_get,
    summary="Read or dismiss the action bar of a chat (the anti-spam info box)",
    description=(
        "The bar's own buttons live elsewhere: `contact add`, "
        "`contact share-phone`, `user block`, `chat report --spam` and "
        "`chat archive --undo`."
    ),
    mutating=True,
    idempotent=True,
    tags=frozenset({"mutating-checked"}),
    columns=("chat_id", "report_spam", "add_contact", "phone_country"),
    example={"chat_id": 777123, "report_spam": True, "phone_country": "DE"},
    example_args="chat action-bar get @alice",
    covers=(
        "contacts-users.nearby-geo-distance",
        "contacts-users.user-action-bar",
        "contacts-users.user-action-bar-hide",
        "dialogs.actionbar-invite-members",
        "dialogs.peer-settings-get",
    ),
)


class BadgeGetReq(Request):
    folder: Annotated[
        str, opt("--folder", metavar="FOLDER", help="Scope the badge to a folder.")
    ] = "all"
    include_muted: Annotated[
        bool, opt("--include-muted", help="Count muted chats toward the badge.")
    ] = False
    count: Annotated[str, choice("chats", "messages", help="Count chats or messages.")] = "chats"
    limits: Annotated[bool, opt("--limits", help="Also report the chat-list limits.")] = False


async def badge_get(ctx: OpContext, req: BadgeGetReq) -> Badge:
    """The unread badge, computed the way a client computes it.

    There is no server-side total: every official client walks its own dialog
    list and adds up. This does the same, which is why the muted split is
    reported rather than folded in — "12 unread, 9 of them muted" is a
    different fact from "12 unread".
    """
    dialogs = await _all_dialogs(ctx, folder_id=None)
    archived = await _all_dialogs(ctx, folder_id=FOLDER_ARCHIVE)
    seen = {d.chat.id for d in dialogs}
    dialogs += [d for d in archived if d.chat.id not in seen]

    chat_filter = await _read_filter(ctx, req.folder)
    if chat_filter is not None:
        dialogs = [d for d in dialogs if _matches_filter(d, chat_filter)]
    elif _is_peer_folder(req.folder):
        wanted = _peer_folder(req.folder)
        dialogs = [d for d in dialogs if d.folder_id == wanted]

    badge = Badge()
    for dialog in dialogs:
        muted = dialog.notify is not None and dialog.notify.muted
        unread = dialog.unread_count or (1 if dialog.unread_mark else 0)
        if not unread:
            continue
        if muted:
            badge.muted_chats += 1
            badge.muted_messages += dialog.unread_count
            if not req.include_muted:
                continue
        badge.chats += 1
        badge.messages += dialog.unread_count
        badge.mentions += dialog.unread_mentions_count
        badge.reactions += dialog.unread_reactions_count

    filters, _ = await _folder_filters(ctx)
    for raw in filters:
        from tlgr.ops.folder import folder_model

        model = folder_model(raw)
        if model.is_default:
            continue
        members = [d for d in dialogs if _matches_filter(d, raw)]
        badge.folders.append(
            FolderBadge(
                id=model.id,
                title=model.title,
                chats=sum(1 for d in members if d.unread_count or d.unread_mark),
                messages=sum(d.unread_count for d in members),
            )
        )

    if req.limits:
        badge.limits = await _chat_limits(ctx)
    return badge


async def _folder_filters(ctx: OpContext) -> tuple[list[Any], bool]:
    from tlgr.ops.folder import raw_filters

    return await raw_filters(ctx)


_LIMIT_KEYS = (
    "dialog_filters_limit_default",
    "dialog_filters_limit_premium",
    "dialog_filters_chats_limit_default",
    "dialog_filters_chats_limit_premium",
    "dialogs_pinned_limit_default",
    "dialogs_pinned_limit_premium",
    "dialogs_folder_pinned_limit_default",
    "dialogs_folder_pinned_limit_premium",
    "chatlist_invites_limit_default",
    "chatlist_invites_limit_premium",
    "chatlist_joined_limit_default",
    "chatlist_joined_limit_premium",
    "channels_limit_default",
    "channels_limit_premium",
    "saved_dialogs_pinned_limit_default",
    "saved_dialogs_pinned_limit_premium",
)


async def _chat_limits(ctx: OpContext) -> dict[str, Any]:
    """The chat-list limits out of `help.getAppConfig`, named rather than dumped."""
    from telethon.tl.functions import help as fn

    result = await _client(ctx)(fn.GetAppConfigRequest(hash=0))
    config = getattr(result, "config", result)
    values = _json_object(config)
    return {key: values[key] for key in _LIMIT_KEYS if key in values}


def _json_object(node: Any) -> dict[str, Any]:
    """A TL `JSONObject` as a plain dict, one level deep."""
    out: dict[str, Any] = {}
    for entry in getattr(node, "value", None) or []:
        key = getattr(entry, "key", None)
        value = getattr(entry, "value", None)
        if key is None:
            continue
        out[str(key)] = getattr(value, "value", None)
    return out


SPEC_BADGE_GET = OperationSpec(
    id="chat.badge.get",
    request=BadgeGetReq,
    response=Badge,
    impl=badge_get,
    summary="Aggregate unread badge and the chat-list limits behind it",
    description=(
        "Computed locally from the dialog list, because no server-side total "
        "exists. Muted chats are counted separately rather than silently."
    ),
    timeout_s=300,
    columns=("chats", "messages", "mentions", "reactions"),
    example={"chats": 4, "messages": 17, "mentions": 1, "reactions": 0},
    example_args="chat badge get",
    covers=("dialogs.badge-preferences", "dialogs.limits", "dialogs.unread-counters"),
)


class PromoListReq(Request):
    dismiss: Annotated[
        str | None, opt("--dismiss", metavar="KEY", help="Dismiss one suggestion key.")
    ] = None
    hide_promo: Annotated[bool, opt("--hide-promo", help="Hide the promoted / PSA dialog.")] = False


async def promo_list(ctx: OpContext, req: PromoListReq) -> Promo:
    """The rows a client puts *above* the chat list.

    `BIRTHDAY_CONTACTS_TODAY` is an inverted suggestion: its presence in the
    dismissed list means "do not show the bar", so it is reported in both
    lists rather than translated into a boolean that would read backwards.
    """
    from telethon.tl.functions import contacts as cfn
    from telethon.tl.functions import help as fn

    client = _client(ctx)
    config = _json_object(getattr(await client(fn.GetAppConfigRequest(hash=0)), "config", None))
    promo = Promo(
        pending_suggestions=[str(v) for v in (config.get("pending_suggestions") or [])],
        dismissed_suggestions=[str(v) for v in (config.get("dismissed_suggestions") or [])],
    )

    data = await client(fn.GetPromoDataRequest())
    peer = getattr(data, "peer", None)
    if peer is not None:
        promo.promo_peer = peer_id_of(peer)
        promo.psa_type = getattr(data, "psa_type", None)
        promo.psa_message = getattr(data, "psa_message", None)

    try:
        birthdays = await client(cfn.GetBirthdaysRequest())
        promo.birthdays_today = [
            int(getattr(entry, "contact_id", 0) or 0)
            for entry in (getattr(birthdays, "contacts", None) or [])
        ]
    except Exception as exc:  # pragma: no cover - optional surface
        ctx.warn(f"birthdays unavailable: {_error_text(exc)}")

    if req.dismiss:
        from telethon.tl import types

        await client(
            fn.DismissSuggestionRequest(peer=types.InputPeerEmpty(), suggestion=req.dismiss)
        )
        if req.dismiss not in promo.dismissed_suggestions:
            promo.dismissed_suggestions.append(req.dismiss)
    if req.hide_promo and promo.promo_peer is not None:
        await client(fn.HidePromoDataRequest(peer=await _send.resolve(ctx, str(promo.promo_peer))))
        promo.hidden = True
    return promo


SPEC_PROMO_LIST = OperationSpec(
    id="chat.promo.list",
    request=PromoListReq,
    response=Promo,
    impl=promo_list,
    summary="Chat-list top rows: pending suggestions, birthday bar, PSA promo",
    aliases=("chat.suggestions",),
    mutating=True,
    idempotent=True,
    columns=("pending_suggestions", "promo_peer"),
    example={"pending_suggestions": ["VALIDATE_PHONE_NUMBER"], "dismissed_suggestions": []},
    example_args="chat promo list",
    covers=(
        "contacts-users.contacts-birthday-dismiss",
        "dialogs.birthday-bar",
        "dialogs.promo-psa",
        "dialogs.suggestions-dismiss",
    ),
)


# ---------------------------------------------------------------------------
# chat saved list
# ---------------------------------------------------------------------------


class SavedListReq(Request):
    in_: Annotated[
        PeerRef | None,
        opt("--in", metavar="CHAT", kind="peer", help="me/saved, or a monoforum channel."),
    ] = None
    pinned: Annotated[bool, opt("--pinned", help="Only pinned sublists, in order.")] = False
    unread: Annotated[bool, opt("--unread", help="Only sublists with unread messages.")] = False


async def saved_list(ctx: OpContext, req: SavedListReq) -> Page[SavedDialog]:
    """Saved-Messages sublists, or a channel's direct-message topics.

    One call family answers both: a monoforum topic *is* a saved dialog whose
    `parent_peer` is the channel. Reading, clearing and pinning one are the
    `--saved-peer` flags on `chat read`, `chat clear` and `chat pin`.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    limit, state = _window(ctx, "chat.saved.list", PageKind.DIALOGS, default=50)
    client = _client(ctx)
    parent = await _send.resolve(ctx, req.in_) if req.in_ is not None else None
    parent_id = _send.peer_id_of(parent) if parent is not None else None

    if req.pinned:
        result = await client(fn.GetPinnedSavedDialogsRequest())
    else:
        result = await client(
            fn.GetSavedDialogsRequest(
                offset_date=None,
                offset_id=int(state.get("offset_id", 0) or 0),
                offset_peer=types.InputPeerEmpty(),
                limit=limit,
                hash=0,
                parent_peer=parent,
            )
        )

    entities = _entity_map(result)
    messages = {int(getattr(m, "id", 0) or 0): m for m in (getattr(result, "messages", None) or [])}
    items: list[SavedDialog] = []
    for row in getattr(result, "dialogs", None) or []:
        origin_id = peer_id_of(getattr(row, "peer", None)) or 0
        entity = entities.get(origin_id)
        top_id = int(getattr(row, "top_message", 0) or 0)
        top = messages.get(top_id)
        items.append(
            SavedDialog(
                origin_peer=entity_to_peer(entity) if entity is not None else None,
                origin_id=origin_id,
                parent_peer=parent_id,
                top_message_id=top_id or None,
                pinned=bool(getattr(row, "pinned", False)) or req.pinned,
                unread_count=int(getattr(row, "unread_count", 0) or 0),
                unread_mark=bool(getattr(row, "unread_mark", False)),
                date=fmt_dt(getattr(top, "date", None)) if top is not None else None,
                date_unix=to_unix(getattr(top, "date", None)) if top is not None else None,
            )
        )
    if req.unread:
        items = [i for i in items if i.unread_count or i.unread_mark]

    next_state = {"offset_id": items[-1].top_message_id or 0} if items and not req.pinned else {}
    return build_page(
        items,
        op="chat.saved.list",
        kind=PageKind.DIALOGS,
        state=next_state,
        account=ctx.account,
        limit=None if req.pinned else limit,
        has_more=False if req.pinned else None,
        total=getattr(result, "count", None),
    )


SPEC_SAVED_LIST = OperationSpec(
    id="chat.saved.list",
    request=SavedListReq,
    response=Page[SavedDialog],
    impl=saved_list,
    summary="Saved-Messages sublists and channel direct-message topics",
    paginated=PageKind.DIALOGS,
    columns=("origin_id", "unread_count", "pinned"),
    headers=("Origin", "Unread", "Pinned"),
    example={"items": [{"origin_id": 4242, "unread_count": 0}], "has_more": False},
    example_args="chat saved list",
    covers=("dialogs.monoforum-topics", "dialogs.saved-sublists"),
)


# ---------------------------------------------------------------------------
# chat report
# ---------------------------------------------------------------------------


class ReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to report.")]
    messages: Annotated[
        list[int], opt("--messages", metavar="ID", kind="msg_id", help="Message ids to report.")
    ] = []
    option: Annotated[
        str | None, opt("--option", metavar="HEX", help="Option bytes from the previous step.")
    ] = None
    comment: Annotated[
        str | None, opt("--comment", metavar="TEXT", help="Free text when the tree asks.")
    ] = None
    spam: Annotated[bool, opt("--spam", help="One-shot action-bar report instead of the tree.")] = (
        False
    )
    reason: Annotated[
        str | None, choice(*sorted(_REPORT_REASONS), help="Legacy account.reportPeer reason.")
    ] = None
    block: Annotated[bool, opt("--block", help="Block the peer afterwards.")] = False
    delete: Annotated[bool, opt("--delete", help="Delete the chat afterwards.")] = False


async def report(ctx: OpContext, req: ReportReq) -> ReportResult:
    """Report a chat, a user or specific messages.

    The modern flow is a server-driven state machine: the first call answers
    with a menu, each `--option` walks one level deeper, and the last step
    may ask for a comment. **Option bytes are opaque and session-specific** —
    they are echoed as hex for the next invocation and must never be stored.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.spam:
        await client(fn.ReportSpamRequest(peer=peer))
        await client(fn.HidePeerSettingsBarRequest(peer=peer))
        result = ReportResult(ok=True)
    elif req.reason is not None:
        reason = getattr(types, _REPORT_REASONS[req.reason])()
        await client(afn.ReportPeerRequest(peer=peer, reason=reason, message=req.comment or ""))
        result = ReportResult(ok=True)
    else:
        result = await _report_tree(ctx, peer, req)

    if result.ok and req.block:
        from telethon.tl.functions import contacts as cfn

        await client(cfn.BlockRequest(id=peer))
    if result.ok and req.delete:
        await _affected_loop(
            ctx, lambda offset, p=peer: fn.DeleteHistoryRequest(peer=p, max_id=0, revoke=False)
        )
    if result.ok:
        ctx.emit("chat_report", {"chat_id": chat_id})
    return result


async def _report_tree(ctx: OpContext, peer: Any, req: ReportReq) -> ReportResult:
    from telethon.tl.functions import messages as fn

    option = b""
    if req.option:
        try:
            option = bytes.fromhex(req.option)
        except ValueError as exc:
            raise UsageError(
                "--option takes the hex bytes printed by the previous step", field="option"
            ) from exc

    answer = await _client(ctx)(
        fn.ReportRequest(
            peer=peer,
            id=[int(i) for i in req.messages],
            option=option,
            message=req.comment or "",
        )
    )
    name = type(answer).__name__
    if name == "ReportResultChooseOption":
        return ReportResult(
            ok=False,
            title=str(getattr(answer, "title", "") or ""),
            options=[
                {
                    "text": str(getattr(item, "text", "") or ""),
                    "option": bytes(getattr(item, "option", b"") or b"").hex(),
                }
                for item in (getattr(answer, "options", None) or [])
            ],
        )
    if name == "ReportResultAddComment":
        return ReportResult(
            ok=False,
            comment_required=not bool(getattr(answer, "optional", False)),
            options=[{"option": bytes(getattr(answer, "option", b"") or b"").hex()}],
        )
    return ReportResult(ok=True)


SPEC_REPORT = OperationSpec(
    id="chat.report",
    request=ReportReq,
    response=ReportResult,
    impl=report,
    summary="Report a chat, a user or specific messages",
    description=(
        "Without --spam or --reason this walks Telegram's own option tree: "
        "call it, read `options`, call again with `--option <hex>`. The bytes "
        "are opaque and path-specific — never persist them."
    ),
    aliases=("chat.report-spam",),
    mutating=True,
    destructive=True,
    columns=("ok", "title"),
    example={"ok": False, "title": "What is wrong?", "options": []},
    example_args="chat report @spammer --spam --yes",
    covers=(
        "contacts-users.user-report-messages",
        "contacts-users.user-report-spam",
        "dialogs.report-chat",
        "dialogs.report-spam-bar",
        "dialogs.report-spam-supergroup",
        "groups-channels-admin.report-chat",
        "groups-channels-admin.report-chat-photo",
        "privacy.report-profile-photo",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# chat secret
# ---------------------------------------------------------------------------


class SecretListReq(Request):
    requests: Annotated[
        bool, opt("--requests", help="Only incoming, not-yet-accepted requests.")
    ] = False
    fingerprint: Annotated[bool, opt("--fingerprint", help="Include the key fingerprint.")] = False


async def secret_list(ctx: OpContext, req: SecretListReq) -> Page[SecretChat]:
    """Secret chats this session holds — which tlgr cannot enumerate yet."""
    raise NotSupportedError(f"chat secret list is not supported: {SECRET_UNSUPPORTED}")


SPEC_SECRET_LIST = OperationSpec(
    id="chat.secret.list",
    request=SecretListReq,
    response=Page[SecretChat],
    impl=secret_list,
    summary="Secret chats this session holds, with state and key fingerprint",
    description=(
        "NOT SUPPORTED. Secret chats never appear in `messages.getDialogs` "
        "and are bound to the one session that created them, so listing them "
        "needs the local key store the E2E module would own."
    ),
    columns=("id", "state"),
    example={"items": [], "has_more": False},
    example_args="chat secret list",
    covers=(
        "contacts-users.user-secret-chat",
        "dialogs.secret-fingerprint",
        "dialogs.secret-list",
    ),
    coverage_note="Registered and refused with NOT_SUPPORTED (exit 13) until the E2E module exists.",
)


class SecretStartReq(Request):
    user: Annotated[
        PeerRef | None,
        arg(0, metavar="USER", required=False, kind="user", help="Who to start it with."),
    ] = None
    accept: Annotated[
        int | None, opt("--accept", metavar="ID", help="Accept this incoming secret chat.")
    ] = None


async def secret_start(ctx: OpContext, req: SecretStartReq) -> SecretChat:
    """Start or accept a secret chat — which needs the E2E module."""
    raise NotSupportedError(f"chat secret start is not supported: {SECRET_UNSUPPORTED}")


SPEC_SECRET_START = OperationSpec(
    id="chat.secret.start",
    request=SecretStartReq,
    response=SecretChat,
    impl=secret_start,
    summary="Start a secret chat with a user, or accept an incoming request",
    description=(
        "NOT SUPPORTED. `requestEncryption`/`acceptEncryption` need a "
        "validated Diffie-Hellman exchange tlgr cannot perform yet."
    ),
    mutating=True,
    aliases=("chat.secret.accept",),
    columns=("id", "state"),
    example={"id": 0, "state": "unsupported"},
    example_args="chat secret start @alice",
    covers=("dialogs.secret-accept", "dialogs.secret-create"),
    coverage_note="Registered and refused with NOT_SUPPORTED (exit 13) until the E2E module exists.",
)


class SecretSendReq(Request):
    id: Annotated[int, arg(0, metavar="ID", help="Secret chat id.")]
    text: Annotated[str, arg(1, metavar="TEXT", required=False, help="What to send.")] = ""
    ttl: Annotated[
        int | None, opt("--ttl", metavar="DURATION", kind="duration", help="Self-destruct timer.")
    ] = None
    read: Annotated[bool, opt("--read", help="Acknowledge up to now.")] = False
    typing: Annotated[bool, opt("--typing", help="Show the typing indicator.")] = False


async def secret_send(ctx: OpContext, req: SecretSendReq) -> SecretChat:
    """Send into a secret chat — which needs the E2E module."""
    raise NotSupportedError(f"chat secret send is not supported: {SECRET_UNSUPPORTED}")


SPEC_SECRET_SEND = OperationSpec(
    id="chat.secret.send",
    request=SecretSendReq,
    response=SecretChat,
    impl=secret_send,
    summary="Send into a secret chat, set its timer, ack or show typing",
    description=(
        "NOT SUPPORTED. The payload of `messages.sendEncrypted` is a "
        "separately-serialised, AES-IGE-encrypted message with its own "
        "sequence numbers; Telethon builds none of it."
    ),
    mutating=True,
    columns=("id", "state"),
    example={"id": 0, "state": "unsupported"},
    example_args="chat secret send 12 hello",
    covers=("dialogs.secret-read-typing", "dialogs.secret-send", "dialogs.secret-ttl"),
    coverage_note="Registered and refused with NOT_SUPPORTED (exit 13) until the E2E module exists.",
)


class SecretDiscardReq(Request):
    id: Annotated[int, arg(0, metavar="ID", help="Secret chat id.")]
    delete_history: Annotated[bool, opt("--delete-history", help="Also delete the history.")] = (
        False
    )
    report_spam: Annotated[bool, opt("--report-spam", help="Report it as spam.")] = False


async def secret_discard(ctx: OpContext, req: SecretDiscardReq) -> SecretChat:
    """Discard a secret chat — the one secret-chat operation that works today.

    Discarding needs no key material: the chat id is enough, which is why
    this is implemented while its siblings are not.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    if req.report_spam:
        await client(
            fn.ReportEncryptedSpamRequest(
                peer=types.InputEncryptedChat(chat_id=req.id, access_hash=0)
            )
        )
    await client(
        fn.DiscardEncryptionRequest(chat_id=req.id, delete_history=req.delete_history or None)
    )
    ctx.emit("chat_secret_discard", {"id": req.id})
    return SecretChat(id=req.id, state="discarded", discarded=True)


SPEC_SECRET_DISCARD = OperationSpec(
    id="chat.secret.discard",
    request=SecretDiscardReq,
    response=SecretChat,
    impl=secret_discard,
    summary="Discard a secret chat, optionally deleting it or reporting spam",
    mutating=True,
    destructive=True,
    columns=("id", "discarded"),
    example={"id": 12, "state": "discarded", "discarded": True},
    example_args="chat secret discard 12 --yes",
    covers=("dialogs.report-encrypted-spam", "dialogs.secret-discard"),
)


# ---------------------------------------------------------------------------
# chat import
# ---------------------------------------------------------------------------


class ImportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to import into.")]
    export: Annotated[
        str, arg(1, metavar="EXPORT", kind="path", help="Exported .txt from the other app.")
    ]
    media_dir: Annotated[
        str | None,
        opt("--media-dir", metavar="PATH", kind="path", help="Attachments the export names."),
    ] = None
    check: Annotated[
        bool, opt("--check", help="Only report whether the import would be accepted.")
    ] = False


async def import_history(ctx: OpContext, req: ImportReq) -> ImportState:
    """Import a chat history exported from another messenger.

    Two checks come first and are worth running on their own (`--check`):
    the peer must accept imports at all, and the file's first lines must
    parse. `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_XMIN` is a retry, not a failure.
    """
    from pathlib import Path

    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    path = Path(req.export).expanduser()
    if not path.exists():
        raise UsageError(f"{req.export} does not exist", field="export")
    head = path.read_text(encoding="utf-8", errors="replace")[:1024]

    await client(fn.CheckHistoryImportPeerRequest(peer=peer))
    await client(fn.CheckHistoryImportRequest(import_head=head))
    media = sorted(p for p in Path(req.media_dir).expanduser().iterdir()) if req.media_dir else []
    if req.check or ctx.dry_run:
        return ImportState(chat_id=chat_id, media_count=len(media), state="checked")

    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    handle = await upload(path)
    started = await client(
        fn.InitHistoryImportRequest(peer=peer, file=handle, media_count=len(media))
    )
    import_id = int(getattr(started, "id", 0) or 0)

    uploaded = 0
    for item in media:
        if not item.is_file():
            continue
        from telethon.tl import types

        file_handle = await upload(item)
        await client(
            fn.UploadImportedMediaRequest(
                peer=peer,
                import_id=import_id,
                file_name=item.name,
                media=types.InputMediaUploadedDocument(
                    file=file_handle, mime_type="application/octet-stream", attributes=[]
                ),
            )
        )
        uploaded += 1

    await client(fn.StartHistoryImportRequest(peer=peer, import_id=import_id))
    ctx.emit("chat_import", {"chat_id": chat_id, "import_id": import_id})
    return ImportState(
        chat_id=chat_id,
        import_id=import_id,
        media_count=len(media),
        media=uploaded,
        started=True,
        state="started",
    )


SPEC_IMPORT = OperationSpec(
    id="chat.import",
    request=ImportReq,
    response=ImportState,
    impl=import_history,
    summary="Import a chat history exported from another messenger",
    description=(
        "Only a private chat, or a group you created (or hold import rights "
        "in), accepts an import. `--check` and `--dry-run` stop after the "
        "two feasibility calls."
    ),
    mutating=True,
    rate_class="file",
    timeout_s=900,
    columns=("chat_id", "import_id", "state"),
    example={"chat_id": 777123, "import_id": 42, "media_count": 3, "state": "started"},
    example_args="chat import @alice ./whatsapp.txt --check",
    covers=("dialogs.history-import", "groups-channels-admin.import-chat-history"),
)
