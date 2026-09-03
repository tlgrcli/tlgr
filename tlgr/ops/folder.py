"""The `folder` group: chat folders, and the links that share them.

A chat folder is a *filter*, not a container: Telegram stores one
`dialogFilter` per folder and evaluates it client-side against the dialog
list. Three consequences shape this module.

* **Every edit is a read-modify-write.** `messages.updateDialogFilter`
  replaces the whole filter, so changing an emoji by sending only the emoji
  would silently empty the folder. Everything here fetches the current
  filter, applies the change and writes the result back — in one call, never
  one per chat, because per-chat calls are the classic FLOOD_WAIT generator.
* **Peers go in batches, and the three lists must stay disjoint.** A peer in
  both `pinned_peers` and `exclude_peers` is rejected by the server; a peer
  removed from `include_peers` but still matching a type flag comes straight
  back, which is why `folder remove --exclude` exists.
* **A shared folder is a different constructor.** `dialogFilterChatlist`
  carries no type flags at all, so setting one on a shared folder is refused
  here with a sentence instead of by the server with `FILTER_NOT_SUPPORTED`.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind
from tlgr.models.base import Request
from tlgr.models.dialog import (
    ChatlistInvite,
    ChatlistJoin,
    ChatlistUpdates,
    Folder,
    FolderDeleted,
    FolderList,
    FolderOrder,
    ShareDeleted,
    SuggestedFolder,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: Folder ids 0 and 1 are Telegram's peer-folders (main and archive); a chat
#: folder always gets an id of at least 2, and every official client picks
#: the smallest unused one.
FIRST_FILTER_ID = 2

_EXAMPLE_FOLDER: dict[str, Any] = {
    "id": 2,
    "title": "Work",
    "emoticon": "💼",
    "include_peers": [777123],
    "groups": True,
}

_TYPE_FLAGS = (
    "contacts",
    "non_contacts",
    "groups",
    "broadcasts",
    "bots",
    "exclude_muted",
    "exclude_read",
    "exclude_archived",
)


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


def _already(ctx: OpContext) -> None:
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


def _title_text(value: Any) -> str:
    """A folder title, which is `TextWithEntities` since layer 187."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(getattr(value, "text", "") or "")


def _title_of(raw: Any) -> str:
    return _title_text(getattr(raw, "title", None))


def _ids(peers: Any) -> list[int]:
    """`[InputPeer]` → marked ids, so a folder reads like every other output."""
    from telethon import utils

    out: list[int] = []
    for peer in peers or []:
        try:
            out.append(int(utils.get_peer_id(peer)))
        except (TypeError, ValueError):
            continue
    return out


def folder_model(raw: Any) -> Folder:
    """A `dialogFilter` / `dialogFilterChatlist` / `dialogFilterDefault`."""
    name = type(raw).__name__
    if name == "DialogFilterDefault":
        return Folder(id=0, title="All chats", is_default=True)
    return Folder(
        id=int(getattr(raw, "id", 0) or 0),
        title=_title_of(raw),
        emoticon=getattr(raw, "emoticon", None),
        color=getattr(raw, "color", None),
        title_noanimate=bool(getattr(raw, "title_noanimate", False)),
        include_peers=_ids(getattr(raw, "include_peers", None)),
        exclude_peers=_ids(getattr(raw, "exclude_peers", None)),
        pinned_peers=_ids(getattr(raw, "pinned_peers", None)),
        contacts=bool(getattr(raw, "contacts", False)),
        non_contacts=bool(getattr(raw, "non_contacts", False)),
        groups=bool(getattr(raw, "groups", False)),
        broadcasts=bool(getattr(raw, "broadcasts", False)),
        bots=bool(getattr(raw, "bots", False)),
        exclude_muted=bool(getattr(raw, "exclude_muted", False)),
        exclude_read=bool(getattr(raw, "exclude_read", False)),
        exclude_archived=bool(getattr(raw, "exclude_archived", False)),
        is_chatlist=name == "DialogFilterChatlist",
        has_my_invites=bool(getattr(raw, "has_my_invites", False)),
    )


async def raw_filters(ctx: OpContext) -> tuple[list[Any], bool]:
    """`(filters, tags_enabled)` straight from the server.

    Exported because `chat list --folder`, `chat mute --folder` and
    `chat pin --folder` all evaluate a folder client-side — there is no
    `getDialogs(filter_id)` — and they must read the same filter this module
    writes.
    """
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetDialogFiltersRequest())
    filters = list(getattr(result, "filters", None) or [])
    return filters, bool(getattr(result, "tags_enabled", False))


async def find_filter(ctx: OpContext, ref: str) -> Any:
    """The raw filter named by an id or a title, or NOT_FOUND.

    Accepting the title is not sugar: folder ids are invisible in every
    official client, so a human who wants "Work" has no way to learn that it
    is filter 4 except by listing them first.
    """
    wanted = (ref or "").strip()
    if not wanted:
        raise UsageError("a folder id or name is required", field="folder")
    filters, _ = await raw_filters(ctx)
    if wanted.lstrip("-").isdigit():
        target = int(wanted)
        for raw in filters:
            if int(getattr(raw, "id", -1)) == target:
                return raw
        raise NotFoundError(f"no chat folder with id {target}")
    lowered = wanted.casefold()
    for raw in filters:
        if _title_of(raw).casefold() == lowered:
            return raw
    known = ", ".join(_title_of(f) for f in filters if _title_of(f)) or "none"
    raise NotFoundError(f"no chat folder called {wanted!r} (folders: {known})")


def _next_id(filters: list[Any]) -> int:
    used = {int(getattr(raw, "id", 0) or 0) for raw in filters}
    candidate = FIRST_FILTER_ID
    while candidate in used:
        candidate += 1
    return candidate


async def _resolve_all(ctx: OpContext, refs: Any) -> list[Any]:
    return [await _send.resolve(ctx, ref) for ref in refs or []]


def _same_peer(left: Any, right: Any) -> bool:
    from telethon import utils

    try:
        return int(utils.get_peer_id(left)) == int(utils.get_peer_id(right))
    except (TypeError, ValueError):
        return False


def _without(peers: list[Any], drop: list[Any]) -> list[Any]:
    return [p for p in peers if not any(_same_peer(p, d) for d in drop)]


def _append(peers: list[Any], add: list[Any]) -> list[Any]:
    out = list(peers)
    for peer in add:
        if not any(_same_peer(peer, existing) for existing in out):
            out.append(peer)
    return out


def _rebuild(raw: Any, **changes: Any) -> Any:
    """A copy of *raw* with *changes* applied.

    `updateDialogFilter` replaces the filter wholesale, so the safe edit is
    to reconstruct the same constructor with every field carried over.
    """
    from telethon.tl import types

    common = {
        "id": int(getattr(raw, "id", 0) or 0),
        "title": getattr(raw, "title", None) or types.TextWithEntities(text="", entities=[]),
        "pinned_peers": list(getattr(raw, "pinned_peers", None) or []),
        "include_peers": list(getattr(raw, "include_peers", None) or []),
        "emoticon": getattr(raw, "emoticon", None),
        "color": getattr(raw, "color", None),
        "title_noanimate": getattr(raw, "title_noanimate", None),
    }
    if type(raw).__name__ == "DialogFilterChatlist":
        common.update({"has_my_invites": getattr(raw, "has_my_invites", None)})
        common.update(changes)
        common.pop("exclude_peers", None)
        for flag in _TYPE_FLAGS:
            common.pop(flag, None)
        return types.DialogFilterChatlist(**common)
    common["exclude_peers"] = list(getattr(raw, "exclude_peers", None) or [])
    for flag in _TYPE_FLAGS:
        common[flag] = getattr(raw, flag, None)
    common.update(changes)
    return types.DialogFilter(**common)


async def _write(ctx: OpContext, filter_id: int, raw: Any) -> None:
    from telethon.tl.functions import messages as fn

    await _client(ctx)(fn.UpdateDialogFilterRequest(id=filter_id, filter=raw))


async def strip_peers(ctx: OpContext, raw: Any, chat_ids: set[int]) -> None:
    """Drop these chats from every list of one folder.

    Exported for `chat leave --remove-from-folders`: a folder that still
    names a chat you left shows an empty row in every client.
    """
    from telethon import utils

    def keep(peers: Any) -> list[Any]:
        out = []
        for peer in peers or []:
            try:
                if int(utils.get_peer_id(peer)) in chat_ids:
                    continue
            except (TypeError, ValueError):  # pragma: no cover - defensive
                pass
            out.append(peer)
        return out

    changes: dict[str, Any] = {
        "include_peers": keep(getattr(raw, "include_peers", None)),
        "pinned_peers": keep(getattr(raw, "pinned_peers", None)),
    }
    if type(raw).__name__ != "DialogFilterChatlist":
        changes["exclude_peers"] = keep(getattr(raw, "exclude_peers", None))
    await _write(ctx, int(getattr(raw, "id", 0) or 0), _rebuild(raw, **changes))


async def folder_pinned_write(
    ctx: OpContext,
    raw: Any,
    peers: list[Any],
    *,
    unpin: bool = False,
    order: bool = False,
) -> None:
    """Pin, unpin or reorder chats *inside* a folder.

    Exported for `chat pin --folder`: pinning in a folder is a filter edit,
    not `toggleDialogPin`, and the pinned list must stay disjoint from the
    excluded one or the whole filter is rejected.
    """
    filter_id = int(getattr(raw, "id", 0) or 0)
    pinned = list(getattr(raw, "pinned_peers", None) or [])
    include = list(getattr(raw, "include_peers", None) or [])

    if order:
        pinned = list(peers)
    elif unpin:
        pinned = _without(pinned, peers)
        include = _append(include, peers)
    else:
        pinned = _append(pinned, peers)
        include = _without(include, peers)

    changes: dict[str, Any] = {"pinned_peers": pinned, "include_peers": include}
    if type(raw).__name__ != "DialogFilterChatlist":
        changes["exclude_peers"] = _without(list(getattr(raw, "exclude_peers", None) or []), pinned)
    await _write(ctx, filter_id, _rebuild(raw, **changes))


def _refuse_flags_on_chatlist(raw: Any, requested: bool) -> None:
    if requested and type(raw).__name__ == "DialogFilterChatlist":
        raise UsageError(
            "a shared folder has no type filters: Telegram stores only its "
            "peer list, so --contacts/--groups/--exclude-muted and friends "
            "cannot be set on one",
            field="folder",
        )


def _chatlist(filter_id: int) -> Any:
    from telethon.tl import types

    return types.InputChatlistDialogFilter(filter_id=filter_id)


def _slug(text: str) -> str:
    """`t.me/addlist/AbC` → `AbC`; a bare slug passes through."""
    value = (text or "").strip()
    for marker in ("addlist/", "list/"):
        if marker in value:
            value = value.split(marker, 1)[1]
    return value.strip("/").split("?", 1)[0]


def _invite_model(raw: Any, *, slug: str = "") -> ChatlistInvite:
    from tlgr.ops._serialize import peer_id_of

    url = str(getattr(raw, "url", "") or "")
    return ChatlistInvite(
        slug=slug or _slug(url),
        url=url,
        title=_title_text(getattr(raw, "title", None)),
        peers=[pid for pid in (peer_id_of(p) for p in getattr(raw, "peers", None) or []) if pid],
    )


def _peer_ids(peers: Any) -> list[int]:
    from tlgr.ops._serialize import peer_id_of

    return [pid for pid in (peer_id_of(p) for p in peers or []) if pid]


# ---------------------------------------------------------------------------
# folder list
# ---------------------------------------------------------------------------


class ListReq(Request):
    with_counts: Annotated[
        bool, opt("--with-counts", help="Evaluate each folder over the dialog list.")
    ] = False
    tags: Annotated[
        str | None, choice("on", "off", help="Turn folder tags on or off account-wide.")
    ] = None


async def list_folders(ctx: OpContext, req: ListReq) -> FolderList:
    """Every chat folder, in display order, with the peers each one names.

    `--with-counts` walks the dialog list **once** and applies each filter to
    it, because there is no server-side count: asking Telegram per folder
    would be one full dialog walk per folder.
    """
    from telethon.tl.functions import messages as fn

    # `folder list` is a read, so it stays dry-runnable: the one write it can
    # perform is guarded here rather than by declaring the whole listing
    # mutating, which would make `--dry-run folder list` print a stub.
    if req.tags is not None:
        if ctx.dry_run:
            ctx.warn(f"--dry-run: folder tags would be turned {req.tags}")
        else:
            await _client(ctx)(fn.ToggleDialogFilterTagsRequest(enabled=req.tags == "on"))

    filters, tags_enabled = await raw_filters(ctx)
    if req.tags is not None and not ctx.dry_run:
        tags_enabled = req.tags == "on"
    folders = [folder_model(raw) for raw in filters]

    if req.with_counts:
        from tlgr.ops.chat import folder_counts

        await folder_counts(ctx, filters, folders)
    return FolderList(tags_enabled=tags_enabled, folders=folders)


SPEC_LIST = OperationSpec(
    id="folder.list",
    request=ListReq,
    response=FolderList,
    impl=list_folders,
    summary="List chat folders in display order",
    description=(
        "Shared folders (`is_chatlist`) carry no type flags, and the 'All "
        "chats' placeholder appears as id 0 so `folder reorder` can position "
        "it. `--tags on|off` is the one write here and it honours --dry-run "
        "itself, so listing stays available under it."
    ),
    tags=frozenset({"mutating-checked"}),
    example={"tags_enabled": False, "folders": [_EXAMPLE_FOLDER]},
    example_args="folder list",
    covers=("dialogs.folder-emoticon-title", "dialogs.folder-list", "dialogs.folder-tags"),
)


# ---------------------------------------------------------------------------
# folder create / edit
# ---------------------------------------------------------------------------


class CreateReq(Request):
    title: Annotated[str, arg(0, metavar="TITLE", help="Folder title.")]
    emoji: Annotated[str | None, opt("--emoji", metavar="EMOJI", help="Folder icon.")] = None
    no_title_animation: Annotated[
        bool, opt("--no-title-animation", help="Do not animate custom emoji in the title.")
    ] = False
    color: Annotated[
        str | None, opt("--color", metavar="N", help="Tag colour 0-6, or 'none' (Premium).")
    ] = None
    contacts: Annotated[bool, opt("--contacts", help="Include every contact.")] = False
    non_contacts: Annotated[bool, opt("--non-contacts", help="Include non-contacts.")] = False
    groups: Annotated[bool, opt("--groups", help="Include groups.")] = False
    channels: Annotated[bool, opt("--channels", help="Include channels.")] = False
    bots: Annotated[bool, opt("--bots", help="Include bots.")] = False
    exclude_muted: Annotated[bool, opt("--exclude-muted", help="Drop muted chats.")] = False
    exclude_read: Annotated[bool, opt("--exclude-read", help="Drop fully read chats.")] = False
    exclude_archived: Annotated[bool, opt("--exclude-archived", help="Drop archived chats.")] = (
        False
    )
    include: Annotated[
        list[PeerRef], opt("--include", metavar="CHAT", kind="peer", help="Always include.")
    ] = []
    exclude: Annotated[
        list[PeerRef], opt("--exclude", metavar="CHAT", kind="peer", help="Always exclude.")
    ] = []
    pin: Annotated[
        list[PeerRef], opt("--pin", metavar="CHAT", kind="peer", help="Pin inside the folder.")
    ] = []


def _color(value: str | None) -> int | None:
    if value is None:
        return None
    if value.strip().lower() in ("none", "off", ""):
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise UsageError("--color takes 0-6 or 'none'", field="color") from exc
    if not 0 <= number <= 6:
        raise UsageError("--color takes 0-6 or 'none'", field="color")
    return number


async def create(ctx: OpContext, req: CreateReq) -> Folder:
    """Create a chat folder, picking the id the way every official client does.

    The id is chosen client-side (smallest unused >= 2) because Telegram has
    no "create" call: a folder exists as soon as `updateDialogFilter` is sent
    with an id nothing else is using.
    """
    from telethon.tl import types

    if not req.title.strip():
        raise UsageError("a folder needs a title", field="title")

    filters, _ = await raw_filters(ctx)
    filter_id = _next_id(filters)
    include = await _resolve_all(ctx, req.include)
    pinned = await _resolve_all(ctx, req.pin)
    exclude = await _resolve_all(ctx, req.exclude)
    # A peer cannot be pinned and included twice, and cannot be both included
    # and excluded; the server rejects the whole filter if it is.
    include = _without(include, pinned)
    exclude = _without(exclude, pinned + include)

    raw = types.DialogFilter(
        id=filter_id,
        title=types.TextWithEntities(text=req.title, entities=[]),
        pinned_peers=pinned,
        include_peers=include,
        exclude_peers=exclude,
        contacts=req.contacts or None,
        non_contacts=req.non_contacts or None,
        groups=req.groups or None,
        broadcasts=req.channels or None,
        bots=req.bots or None,
        exclude_muted=req.exclude_muted or None,
        exclude_read=req.exclude_read or None,
        exclude_archived=req.exclude_archived or None,
        emoticon=req.emoji,
        color=_color(req.color),
        title_noanimate=req.no_title_animation or None,
    )
    if not (include or pinned or any(getattr(raw, flag, None) for flag in _TYPE_FLAGS[:5])):
        raise UsageError(
            "a folder needs something in it: pass --include/--pin or a type flag such as --groups",
            field="include",
        )
    await _write(ctx, filter_id, raw)
    ctx.emit("folder_create", {"id": filter_id, "title": req.title})
    return folder_model(raw)


SPEC_CREATE = OperationSpec(
    id="folder.create",
    request=CreateReq,
    response=Folder,
    impl=create,
    summary="Create a chat folder",
    description=(
        "Peers are batched into one `updateDialogFilter`; adding them one at "
        "a time is what earns a FLOOD_WAIT on a folder with thirty chats."
    ),
    mutating=True,
    rate_class="bulk",
    columns=("id", "title", "emoticon"),
    example=_EXAMPLE_FOLDER,
    example_args='folder create "Work" --groups --emoji 💼',
    covers=("dialogs.folder-create",),
)


class EditReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New title.")] = None
    emoji: Annotated[str | None, opt("--emoji", metavar="EMOJI", help="Folder icon.")] = None
    no_title_animation: Annotated[
        bool | None, opt("--no-title-animation", help="Animate custom emoji in the title.")
    ] = None
    color: Annotated[
        str | None, opt("--color", metavar="N", help="Tag colour 0-6, or 'none' (Premium).")
    ] = None
    contacts: Annotated[bool | None, opt("--contacts", help="Include every contact.")] = None
    non_contacts: Annotated[bool | None, opt("--non-contacts", help="Include non-contacts.")] = None
    groups: Annotated[bool | None, opt("--groups", help="Include groups.")] = None
    channels: Annotated[bool | None, opt("--channels", help="Include channels.")] = None
    bots: Annotated[bool | None, opt("--bots", help="Include bots.")] = None
    exclude_muted: Annotated[bool | None, opt("--exclude-muted", help="Drop muted chats.")] = None
    exclude_read: Annotated[bool | None, opt("--exclude-read", help="Drop read chats.")] = None
    exclude_archived: Annotated[
        bool | None, opt("--exclude-archived", help="Drop archived chats.")
    ] = None
    add: Annotated[
        list[PeerRef], opt("--add", "--include", metavar="CHAT", kind="peer", help="Add a chat.")
    ] = []
    remove: Annotated[
        list[PeerRef], opt("--remove", metavar="CHAT", kind="peer", help="Remove a chat.")
    ] = []
    exclude: Annotated[
        list[PeerRef], opt("--exclude", metavar="CHAT", kind="peer", help="Always exclude.")
    ] = []
    pin: Annotated[
        list[PeerRef], opt("--pin", metavar="CHAT", kind="peer", help="Pin inside the folder.")
    ] = []
    clear_include: Annotated[bool, opt("--clear-include", help="Empty the include list.")] = False
    clear_exclude: Annotated[bool, opt("--clear-exclude", help="Empty the exclude list.")] = False


async def edit(ctx: OpContext, req: EditReq) -> Folder:
    """Change one thing about a folder without losing the rest.

    `updateDialogFilter` replaces the filter, so this reads the current one,
    applies only what was asked for and writes the whole thing back.
    """
    from telethon.tl import types

    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    flags = {
        "contacts": req.contacts,
        "non_contacts": req.non_contacts,
        "groups": req.groups,
        "broadcasts": req.channels,
        "bots": req.bots,
        "exclude_muted": req.exclude_muted,
        "exclude_read": req.exclude_read,
        "exclude_archived": req.exclude_archived,
    }
    _refuse_flags_on_chatlist(raw, any(value is not None for value in flags.values()))

    changes: dict[str, Any] = {name: value for name, value in flags.items() if value is not None}
    if req.title is not None:
        changes["title"] = types.TextWithEntities(text=req.title, entities=[])
    if req.emoji is not None:
        changes["emoticon"] = req.emoji
    if req.color is not None:
        changes["color"] = _color(req.color)
    if req.no_title_animation is not None:
        changes["title_noanimate"] = req.no_title_animation or None

    include = list(getattr(raw, "include_peers", None) or [])
    exclude = list(getattr(raw, "exclude_peers", None) or [])
    pinned = list(getattr(raw, "pinned_peers", None) or [])
    if req.clear_include:
        include = []
    if req.clear_exclude:
        exclude = []
    added = await _resolve_all(ctx, req.add)
    removed = await _resolve_all(ctx, req.remove)
    pins = await _resolve_all(ctx, req.pin)
    excluded = await _resolve_all(ctx, req.exclude)

    include = _append(_without(include, removed + pins), added)
    pinned = _append(_without(pinned, removed), pins)
    exclude = _append(_without(exclude, added + pins), excluded)
    include = _without(include, exclude)

    changes["include_peers"] = include
    changes["pinned_peers"] = pinned
    if type(raw).__name__ != "DialogFilterChatlist":
        changes["exclude_peers"] = exclude

    updated = _rebuild(raw, **changes)
    await _write(ctx, filter_id, updated)
    ctx.emit("folder_edit", {"id": filter_id})
    return folder_model(updated)


SPEC_EDIT = OperationSpec(
    id="folder.edit",
    request=EditReq,
    response=Folder,
    impl=edit,
    summary="Edit a chat folder",
    description=(
        "Every flag is a paired switch (`--groups/--no-groups`) because "
        "'leave it alone' and 'turn it off' are different requests, and the "
        "whole filter is rewritten in one call."
    ),
    mutating=True,
    rate_class="bulk",
    columns=("id", "title", "emoticon"),
    example=_EXAMPLE_FOLDER,
    example_args='folder edit Work --title "Work & clients"',
    covers=("dialogs.folder-edit",),
)


# ---------------------------------------------------------------------------
# folder add / remove
# ---------------------------------------------------------------------------


class AddReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]
    chat: Annotated[
        list[PeerRef], arg(1, metavar="CHAT", variadic=True, kind="peer", help="Chats to add.")
    ] = []
    pin: Annotated[bool, opt("--pin", help="Add to the pinned list instead.")] = False


async def add(ctx: OpContext, req: AddReq) -> Folder:
    """Add chats to a folder, and stop excluding them.

    Dropping the peer from `exclude_peers` is part of adding it: a chat that
    is included *and* excluded is the state Telegram refuses, and it is the
    state a naive append produces on a chat somebody removed yesterday.
    """
    if not req.chat:
        raise UsageError("give at least one chat to add", field="chat")
    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    peers = await _resolve_all(ctx, req.chat)

    include = list(getattr(raw, "include_peers", None) or [])
    pinned = list(getattr(raw, "pinned_peers", None) or [])
    exclude = _without(list(getattr(raw, "exclude_peers", None) or []), peers)
    if req.pin:
        pinned = _append(_without(pinned, peers), peers)
        include = _without(include, peers)
    else:
        include = _append(include, _without(peers, pinned))

    changes: dict[str, Any] = {"include_peers": include, "pinned_peers": pinned}
    if type(raw).__name__ != "DialogFilterChatlist":
        changes["exclude_peers"] = exclude
    updated = _rebuild(raw, **changes)
    await _write(ctx, filter_id, updated)
    ctx.emit("folder_add", {"id": filter_id, "chats": _ids(peers)})
    return folder_model(updated)


SPEC_ADD = OperationSpec(
    id="folder.add",
    request=AddReq,
    response=Folder,
    impl=add,
    summary="Add chats to a folder",
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("id", "title", "include_peers"),
    example=_EXAMPLE_FOLDER,
    example_args="folder add Work @alice",
    covers=("dialogs.folder-add-chat",),
)


class RemoveReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]
    chat: Annotated[
        list[PeerRef], arg(1, metavar="CHAT", variadic=True, kind="peer", help="Chats to remove.")
    ] = []
    exclude: Annotated[
        bool, opt("--exclude", help="Also exclude, so a type flag cannot pull it back in.")
    ] = False


async def remove(ctx: OpContext, req: RemoveReq) -> Folder:
    """Remove chats from a folder.

    Removing from `include_peers` is not enough when the chat still matches a
    type flag — it reappears on the next sync. `--exclude` is what the GUI
    does in that case, and the only way to make the removal stick.
    """
    if not req.chat:
        raise UsageError("give at least one chat to remove", field="chat")
    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    peers = await _resolve_all(ctx, req.chat)

    changes: dict[str, Any] = {
        "include_peers": _without(list(getattr(raw, "include_peers", None) or []), peers),
        "pinned_peers": _without(list(getattr(raw, "pinned_peers", None) or []), peers),
    }
    if type(raw).__name__ != "DialogFilterChatlist":
        exclude = list(getattr(raw, "exclude_peers", None) or [])
        changes["exclude_peers"] = _append(exclude, peers) if req.exclude else exclude
    elif req.exclude:
        raise UsageError(
            "a shared folder has no exclude list; remove the chat instead",
            field="exclude",
        )

    updated = _rebuild(raw, **changes)
    await _write(ctx, filter_id, updated)
    ctx.emit("folder_remove", {"id": filter_id, "chats": _ids(peers)})
    return folder_model(updated)


SPEC_REMOVE = OperationSpec(
    id="folder.remove",
    request=RemoveReq,
    response=Folder,
    impl=remove,
    summary="Remove chats from a folder",
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("id", "title", "include_peers"),
    example=_EXAMPLE_FOLDER,
    example_args="folder remove Work @alice --exclude",
    covers=("dialogs.folder-remove-chat",),
)


# ---------------------------------------------------------------------------
# folder delete / reorder
# ---------------------------------------------------------------------------


class DeleteReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]
    leave_chats: Annotated[
        str,
        opt(
            "--leave-chats",
            metavar="WHAT",
            help="none (default) | suggested | all | a comma-separated chat list.",
        ),
    ] = "none"


async def delete(ctx: OpContext, req: DeleteReq) -> FolderDeleted:
    """Delete a folder; for a shared one, optionally leave what came with it.

    An *imported* folder is not deleted with `updateDialogFilter`: the chats
    joined through it would stay, silently. `chatlists.leaveChatlist` is the
    call that removes both, and the peers it should take come from
    `getLeaveChatlistSuggestions` — printed before anything is left.
    """
    from telethon.tl.functions import chatlists as cfn
    from telethon.tl.functions import messages as fn

    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    shared = type(raw).__name__ == "DialogFilterChatlist"
    choice_ = (req.leave_chats or "none").strip().lower()

    suggested: list[Any] = []
    if shared:
        suggested = list(
            await _client(ctx)(cfn.GetLeaveChatlistSuggestionsRequest(_chatlist(filter_id)))
        )

    leaving: list[Any] = []
    if choice_ == "all":
        leaving = list(getattr(raw, "include_peers", None) or []) + list(
            getattr(raw, "pinned_peers", None) or []
        )
    elif choice_ == "suggested":
        leaving = suggested
    elif choice_ not in ("", "none"):
        refs = [part.strip() for part in choice_.split(",") if part.strip()]
        leaving = [await _send.resolve(ctx, ref) for ref in refs]

    if shared:
        await _client(ctx)(cfn.LeaveChatlistRequest(chatlist=_chatlist(filter_id), peers=leaving))
    else:
        if leaving:
            raise UsageError(
                "--leave-chats only applies to a shared folder you joined; a "
                "folder you made is just a filter, so deleting it leaves no chats",
                field="leave-chats",
            )
        await _client(ctx)(fn.UpdateDialogFilterRequest(id=filter_id, filter=None))

    ctx.emit("folder_delete", {"id": filter_id})
    return FolderDeleted(
        id=filter_id,
        deleted=True,
        left_chats=_ids(leaving),
        suggested=_peer_ids(suggested),
    )


SPEC_DELETE = OperationSpec(
    id="folder.delete",
    request=DeleteReq,
    response=FolderDeleted,
    impl=delete,
    summary="Delete a chat folder",
    description=(
        "A folder you made is a filter: deleting it keeps every chat. A "
        "shared folder you joined can take its chats with it, which is why "
        "--leave-chats exists and defaults to keeping them."
    ),
    aliases=("folder.leave",),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("id", "deleted"),
    example={"id": 2, "deleted": True, "left_chats": []},
    example_args="folder delete Work",
    covers=("dialogs.chatlist-leave", "dialogs.folder-delete"),
)


class ReorderReq(Request):
    folder: Annotated[
        list[str],
        arg(0, metavar="FOLDER", variadic=True, help="Folders in the order you want them."),
    ] = []


async def reorder(ctx: OpContext, req: ReorderReq) -> FolderOrder:
    """Set the tab order, including where "All chats" sits.

    Id 0 is the main list; naming it anywhere but first is a Premium feature,
    and the server says so rather than this doing the check.
    """
    from telethon.tl.functions import messages as fn

    if not req.folder:
        raise UsageError("give the folders in the order you want them", field="folder")
    order: list[int] = []
    for ref in req.folder:
        text = ref.strip()
        if text in ("0", "main", "all"):
            order.append(0)
            continue
        order.append(int(getattr(await find_filter(ctx, text), "id", 0) or 0))
    await _client(ctx)(fn.UpdateDialogFiltersOrderRequest(order=order))
    ctx.emit("folder_reorder", {"order": order})
    return FolderOrder(order=order)


SPEC_REORDER = OperationSpec(
    id="folder.reorder",
    request=ReorderReq,
    response=FolderOrder,
    impl=reorder,
    summary="Set the display order of chat folders",
    mutating=True,
    idempotent=True,
    columns=("order",),
    example={"order": [0, 2, 3]},
    example_args="folder reorder main Work Family",
    covers=("dialogs.folder-reorder",),
)


# ---------------------------------------------------------------------------
# folder suggested / updates
# ---------------------------------------------------------------------------


class SuggestedReq(Request):
    add: Annotated[
        str | None,
        opt("--add", metavar="TITLE", help="Add the suggested folder with this title."),
    ] = None


async def suggested_list(ctx: OpContext, req: SuggestedReq) -> Page[SuggestedFolder]:
    """Telegram's own folder suggestions, and adding one.

    Adding is not a separate call: a suggestion is a ready-made filter, so it
    is written with `updateDialogFilter` under a fresh id like any other.
    """
    from telethon.tl.functions import messages as fn

    raw_suggestions = list(await _client(ctx)(fn.GetSuggestedDialogFiltersRequest()))
    items: list[SuggestedFolder] = []
    for suggestion in raw_suggestions:
        inner = getattr(suggestion, "filter", None)
        model = folder_model(inner)
        items.append(
            SuggestedFolder(
                title=model.title,
                description=str(getattr(suggestion, "description", "") or ""),
                emoticon=model.emoticon,
                contacts=model.contacts,
                non_contacts=model.non_contacts,
                groups=model.groups,
                broadcasts=model.broadcasts,
                bots=model.bots,
                exclude_muted=model.exclude_muted,
                exclude_read=model.exclude_read,
                exclude_archived=model.exclude_archived,
            )
        )

    if req.add:
        wanted = req.add.strip().casefold()
        chosen = next(
            (
                s
                for s, model in zip(raw_suggestions, items, strict=True)
                if model.title.casefold() == wanted
            ),
            None,
        )
        if chosen is None:
            raise NotFoundError(f"Telegram is not suggesting a folder called {req.add!r}")
        filters, _ = await raw_filters(ctx)
        filter_id = _next_id(filters)
        updated = _rebuild(getattr(chosen, "filter", None), id=filter_id)
        await _write(ctx, filter_id, updated)
        for suggestion_model in items:
            if suggestion_model.title.casefold() == wanted:
                suggestion_model.added_id = filter_id
        ctx.emit("folder_create", {"id": filter_id, "title": req.add})

    return Page(items=items, has_more=False, total=len(items))


SPEC_SUGGESTED_LIST = OperationSpec(
    id="folder.suggested.list",
    request=SuggestedReq,
    response=Page[SuggestedFolder],
    impl=suggested_list,
    summary="List the chat folders Telegram suggests, and add one",
    mutating=True,
    idempotent=True,
    columns=("title", "description"),
    headers=("Folder", "What it collects"),
    example={"items": [{"title": "Unread", "description": "Unread chats"}], "has_more": False},
    example_args="folder suggested list",
    covers=("dialogs.folder-suggested",),
)


class UpdateListReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Shared folder id or name.")]
    join: Annotated[
        str | None, opt("--join", metavar="WHAT", help="all | a comma-separated chat list.")
    ] = None
    dismiss: Annotated[bool, opt("--dismiss", help="Hide the update badge.")] = False


async def update_list(ctx: OpContext, req: UpdateListReq) -> ChatlistUpdates:
    """New chats the folder's owner added since you joined.

    Poll this no more often than the app-config `chatlist_update_period`; the
    badge is a courtesy, not an event stream.
    """
    from telethon.tl.functions import chatlists as cfn

    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    if type(raw).__name__ != "DialogFilterChatlist":
        raise UsageError(
            "only a shared folder receives updates; this one is your own filter",
            field="folder",
        )
    chatlist = _chatlist(filter_id)
    result = await _client(ctx)(cfn.GetChatlistUpdatesRequest(chatlist=chatlist))
    missing = list(getattr(result, "missing_peers", None) or [])

    joined: list[int] = []
    if req.join:
        wanted = req.join.strip().lower()
        if wanted == "all":
            peers = await _peers_for(ctx, missing, result)
        else:
            peers = [
                await _send.resolve(ctx, part.strip())
                for part in req.join.split(",")
                if part.strip()
            ]
        if peers:
            await _client(ctx)(cfn.JoinChatlistUpdatesRequest(chatlist=chatlist, peers=peers))
            joined = _ids(peers)

    dismissed = False
    if req.dismiss:
        await _client(ctx)(cfn.HideChatlistUpdatesRequest(chatlist=chatlist))
        dismissed = True

    return ChatlistUpdates(
        id=filter_id,
        missing_peers=_peer_ids(missing),
        joined=joined,
        dismissed=dismissed,
    )


async def _peers_for(ctx: OpContext, peers: list[Any], result: Any) -> list[Any]:
    """`Peer` → `InputPeer`, using the chats/users the same reply carried.

    Resolving them one by one would be one `resolveUsername` per chat for
    peers the server just handed over in full.
    """
    from telethon import utils

    known: dict[int, Any] = {}
    for entity in list(getattr(result, "chats", None) or []) + list(
        getattr(result, "users", None) or []
    ):
        try:
            known[int(utils.get_peer_id(entity))] = utils.get_input_peer(entity)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    out: list[Any] = []
    for peer in peers:
        try:
            marked = int(utils.get_peer_id(peer))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        found = known.get(marked)
        if found is None:
            found = await _send.resolve(ctx, str(marked))
        out.append(found)
    return out


SPEC_UPDATE_LIST = OperationSpec(
    id="folder.update.list",
    request=UpdateListReq,
    response=ChatlistUpdates,
    impl=update_list,
    summary="New chats the owner added to a shared folder",
    aliases=("folder.updates",),
    mutating=True,
    idempotent=True,
    columns=("id", "missing_peers"),
    example={"id": 4, "missing_peers": [777123], "joined": []},
    example_args="folder update list Work",
    covers=("dialogs.chatlist-updates",),
)


# ---------------------------------------------------------------------------
# folder join
# ---------------------------------------------------------------------------


class JoinReq(Request):
    link: Annotated[str, arg(0, metavar="LINK", help="t.me/addlist/<slug>, or a bare slug.")]
    chats: Annotated[
        list[PeerRef],
        opt("--chats", metavar="CHAT", kind="peer", help="Which offered chats to join."),
    ] = []
    join_all: Annotated[bool, opt("--all-chats", help="Join every offered chat.")] = False


async def join(ctx: OpContext, req: JoinReq) -> ChatlistJoin:
    """Preview a shared-folder link, and join the chats you name.

    Previewing is the default and joining is opt-in per chat: an `addlist`
    link can carry fifty channels, and "joined fifty channels" is not a thing
    a command should do because a link was pasted.
    """
    from telethon.tl.functions import chatlists as cfn

    slug = _slug(req.link)
    if not slug:
        raise UsageError("that is not a folder link", field="link")
    preview = await _client(ctx)(cfn.CheckChatlistInviteRequest(slug=slug))
    already = type(preview).__name__ == "ChatlistInviteAlready"

    offered = list(getattr(preview, "peers", None) or [])
    missing = list(getattr(preview, "missing_peers", None) or [])
    result = ChatlistJoin(
        slug=slug,
        title=_title_text(getattr(preview, "title", None)),
        emoticon=getattr(preview, "emoticon", None),
        already_member=already,
        filter_id=getattr(preview, "filter_id", None),
        peers=_peer_ids(offered),
        missing_peers=_peer_ids(missing),
        already_peers=_peer_ids(getattr(preview, "already_peers", None)),
    )

    wanted: list[Any] = []
    if req.join_all:
        wanted = await _peers_for(ctx, missing or offered, preview)
    elif req.chats:
        wanted = await _resolve_all(ctx, req.chats)
    if not wanted:
        _already(ctx)
        return result

    if already:
        await _client(ctx)(
            cfn.JoinChatlistUpdatesRequest(
                chatlist=_chatlist(int(result.filter_id or 0)), peers=wanted
            )
        )
    else:
        await _client(ctx)(cfn.JoinChatlistInviteRequest(slug=slug, peers=wanted))
    result.joined = _ids(wanted)
    ctx.emit("folder_join", {"slug": slug, "joined": result.joined})
    return result


SPEC_JOIN = OperationSpec(
    id="folder.join",
    request=JoinReq,
    response=ChatlistJoin,
    impl=join,
    summary="Preview and join a shared folder link",
    description=(
        "Without --chats or --all this only previews: the reply lists what "
        "the link offers and joins nothing."
    ),
    mutating=True,
    rate_class="bulk",
    columns=("slug", "title", "peers"),
    example={"slug": "AbCdEf", "title": "Work", "peers": [777123], "joined": []},
    example_args="folder join t.me/addlist/AbCdEf",
    covers=(
        "contacts-users.resolve-folder-link",
        "dialogs.chatlist-check",
        "dialogs.chatlist-join",
    ),
)


# ---------------------------------------------------------------------------
# folder share
# ---------------------------------------------------------------------------


class ShareListReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]


async def share_list(ctx: OpContext, req: ShareListReq) -> Page[ChatlistInvite]:
    """Every share link this folder has, with the chats each one carries."""
    from telethon.tl.functions import chatlists as cfn

    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    result = await _client(ctx)(cfn.GetExportedInvitesRequest(chatlist=_chatlist(filter_id)))
    items = [_invite_model(invite) for invite in getattr(result, "invites", None) or []]
    return Page(items=items, has_more=False, total=len(items))


SPEC_SHARE_LIST = OperationSpec(
    id="folder.share.list",
    request=ShareListReq,
    response=Page[ChatlistInvite],
    impl=share_list,
    summary="List a folder's shareable invite links",
    paginated=PageKind.LOCAL,
    columns=("slug", "title", "url"),
    headers=("Slug", "Title", "Link"),
    example={
        "items": [{"slug": "AbCdEf", "url": "https://t.me/addlist/AbCdEf", "title": "Work"}],
        "has_more": False,
    },
    example_args="folder share list Work",
    covers=("dialogs.chatlist-invite-list",),
)


class ShareSetReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]
    slug: Annotated[
        str | None, opt("--slug", metavar="SLUG", help="Edit this link instead of creating one.")
    ] = None
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="Link title.")] = None
    chats: Annotated[
        list[PeerRef], opt("--chats", metavar="CHAT", kind="peer", help="Chats the link carries.")
    ] = []
    all_eligible: Annotated[
        bool, opt("--all-eligible", help="Every chat of the folder that may be shared.")
    ] = False


async def share_set(ctx: OpContext, req: ShareSetReq) -> ChatlistInvite:
    """Create a share link, or edit one that exists.

    Only chats you can make an invite link for may be shared, so a folder of
    private groups produces a link with fewer chats than the folder has —
    the reply lists what actually went in rather than what was asked for.
    """
    from telethon.tl.functions import chatlists as cfn

    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    chatlist = _chatlist(filter_id)

    peers: list[Any] = []
    if req.all_eligible:
        peers = list(getattr(raw, "pinned_peers", None) or []) + list(
            getattr(raw, "include_peers", None) or []
        )
    elif req.chats:
        peers = await _resolve_all(ctx, req.chats)

    if req.slug:
        result = await _client(ctx)(
            cfn.EditExportedInviteRequest(
                chatlist=chatlist,
                slug=req.slug,
                title=req.title,
                peers=peers or None,
            )
        )
        invite = getattr(result, "invite", result)
        ctx.emit("folder_share", {"id": filter_id, "slug": req.slug})
        return _invite_model(invite, slug=req.slug)

    if not peers:
        raise UsageError(
            "a share link needs chats: pass --chats or --all-eligible",
            field="chats",
        )
    result = await _client(ctx)(
        cfn.ExportChatlistInviteRequest(
            chatlist=chatlist, title=req.title or _title_of(raw), peers=peers
        )
    )
    invite = getattr(result, "invite", result)
    model = _invite_model(invite)
    ctx.emit("folder_share", {"id": filter_id, "slug": model.slug})
    return model


SPEC_SHARE_SET = OperationSpec(
    id="folder.share.set",
    request=ShareSetReq,
    response=ChatlistInvite,
    impl=share_set,
    summary="Create or edit a folder's shareable invite link",
    aliases=("folder.share.create", "folder.share.edit"),
    mutating=True,
    rate_class="bulk",
    columns=("slug", "url", "title"),
    example={"slug": "AbCdEf", "url": "https://t.me/addlist/AbCdEf", "title": "Work"},
    example_args="folder share set Work --all-eligible",
    covers=("dialogs.chatlist-invite-create", "dialogs.chatlist-invite-edit"),
    tags=frozenset({"visible-to-others"}),
)


class ShareDeleteReq(Request):
    folder: Annotated[str, arg(0, metavar="FOLDER", help="Folder id or name.")]
    slug: Annotated[str, arg(1, metavar="SLUG", help="The link to revoke.")]


async def share_delete(ctx: OpContext, req: ShareDeleteReq) -> ShareDeleted:
    """Revoke one share link. Anyone holding it stops being able to use it."""
    from telethon.tl.functions import chatlists as cfn

    raw = await find_filter(ctx, req.folder)
    filter_id = int(getattr(raw, "id", 0) or 0)
    slug = _slug(req.slug)
    await _client(ctx)(cfn.DeleteExportedInviteRequest(chatlist=_chatlist(filter_id), slug=slug))
    ctx.emit("folder_share_delete", {"id": filter_id, "slug": slug})
    return ShareDeleted(slug=slug, deleted=True)


SPEC_SHARE_DELETE = OperationSpec(
    id="folder.share.delete",
    request=ShareDeleteReq,
    response=ShareDeleted,
    impl=share_delete,
    summary="Revoke a folder share link",
    mutating=True,
    destructive=True,
    columns=("slug", "deleted"),
    example={"slug": "AbCdEf", "deleted": True},
    example_args="folder share delete Work AbCdEf",
    covers=("dialogs.chatlist-invite-delete",),
)
