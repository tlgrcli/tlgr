"""The `export` group: Telegram's data export (takeout).

A takeout is a *mode*, not a request. `account.initTakeoutSession` returns an
id, and every subsequent call — `upload.getFile` included — has to be wrapped
in `invokeWithTakeout` or it simply is not part of the export. `file_max_size`
is fixed at that moment and cannot be raised later.

Two consequences shape this group. The session id is held by the daemon, in
memory, for the account it was opened on, so `export start` and `export
message download` are separate commands rather than one long-running call an
interrupted terminal would abandon. And `TAKEOUT_INIT_DELAY_X` — another
logged-in session has to approve the export, or 24 hours must pass if there is
none — is reported as a structured error with `retry_after` rather than slept
through in silence.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, RateLimitError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.export import (
    ExportedFile,
    ExportResult,
    MessageRange,
    TakeoutSession,
    TakeoutStatus,
)
from tlgr.models.message import Message
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._params import opt, parse_dt
from tlgr.ops._serialize import message_to_model
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_SCOPES = ("contacts", "messages", "users", "chats", "megagroups", "channels", "bots", "files")


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:
        raise UsageError("this operation needs a connected account")
    return client


def _sessions(ctx: OpContext) -> Any:
    daemon = getattr(ctx, "daemon", None)
    return getattr(daemon, "sessions", None)


def _store(ctx: OpContext) -> dict[str, dict[str, Any]]:
    """Open takeout sessions, per account, on the daemon.

    In memory rather than on disk, deliberately: a takeout id is only valid
    for the connection that opened it, so persisting one across a restart
    would hand back an id every subsequent request would be rejected with.
    """
    daemon = getattr(ctx, "daemon", None)
    if daemon is None:
        raise UsageError("this operation runs inside the daemon")
    existing = getattr(daemon, "takeouts", None)
    if existing is None:
        existing = {}
        daemon.takeouts = existing
    return existing


def _active(ctx: OpContext) -> dict[str, Any]:
    entry = _store(ctx).get(ctx.account)
    if not entry:
        raise NotFoundError("no takeout session is open for this account. Run: tlgr export start")
    return entry


async def _invoke(ctx: OpContext, request: Any) -> Any:
    """Run *request* inside the account's takeout session when one is open.

    Outside a session the call still works — it simply runs against the normal
    flood budget instead of the takeout one — which is why `export account
    download` is useful before `export start` has been approved.
    """
    from telethon.tl import functions

    client = _client(ctx)
    entry = _store(ctx).get(ctx.account)
    if not entry:
        return await client(request)
    return await client(
        functions.InvokeWithTakeoutRequest(takeout_id=entry["takeout_id"], query=request)
    )


# ---------------------------------------------------------------------------
# export start / status / end
# ---------------------------------------------------------------------------


class ExportStartReq(Request):
    contacts: Annotated[bool, opt("--contacts", help="Include contacts.")] = False
    messages: Annotated[bool, opt("--messages", help="Include private-chat history.")] = False
    users: Annotated[bool, opt("--users", help="Include private chats.")] = False
    chats: Annotated[bool, opt("--chats", help="Include basic groups.")] = False
    megagroups: Annotated[bool, opt("--megagroups", help="Include supergroups.")] = False
    channels: Annotated[bool, opt("--channels", help="Include channels.")] = False
    bots: Annotated[bool, opt("--bots", help="Include bot chats.")] = False
    files: Annotated[bool, opt("--files", help="Include media files.")] = False
    max_file_size: Annotated[
        int,
        opt(
            "--max-file-size",
            metavar="BYTES",
            help="file_max_size, declared up front and unchangeable afterwards.",
        ),
    ] = 100 * 1024 * 1024
    wait: Annotated[
        bool, opt("--wait", help="Report TAKEOUT_INIT_DELAY as a wait instead of failing.")
    ] = False


async def export_start(ctx: OpContext, req: ExportStartReq) -> TakeoutSession:
    """Open a takeout session.

    `file_max_size` cannot be changed later, so it is declared here. A
    `TAKEOUT_INIT_DELAY_X` means another logged-in session has to approve the
    export first — 24 hours if there is none — and it comes back as
    RATE_LIMITED carrying `retry_after`, because "try again later" without
    "how much later" is not actionable.
    """
    from telethon.tl import functions

    store = _store(ctx)
    if ctx.account in store:
        entry = store[ctx.account]
        ctx.mark_already()
        return TakeoutSession(
            takeout_id=entry["takeout_id"],
            scope=entry["scope"],
            started_at=entry["started_at"],
            max_file_size=entry["max_file_size"],
            already=True,
        )

    scope = [name for name in _SCOPES if getattr(req, name, False)]
    if not scope:
        raise UsageError(
            "name at least one scope: --messages, --contacts, --channels, --files, …",
            field="messages",
        )

    request = functions.account.InitTakeoutSessionRequest(
        contacts=req.contacts,
        message_users=req.users or req.messages,
        message_chats=req.chats,
        message_megagroups=req.megagroups,
        message_channels=req.channels,
        files=req.files,
        file_max_size=req.max_file_size if req.files else None,
    )
    try:
        result = await _client(ctx)(request)
    except Exception as exc:
        raise _takeout_delay(exc, waiting=req.wait) from exc

    entry = {
        "takeout_id": int(getattr(result, "id", 0) or 0),
        "scope": scope,
        "started_at": _now(),
        "max_file_size": req.max_file_size,
    }
    _store(ctx)[ctx.account] = entry
    return TakeoutSession(
        takeout_id=entry["takeout_id"],
        scope=scope,
        started_at=entry["started_at"],
        max_file_size=entry["max_file_size"],
    )


def _takeout_delay(exc: Exception, *, waiting: bool) -> Exception:
    seconds = getattr(exc, "seconds", None)
    if type(exc).__name__ != "TakeoutInitDelayError" and seconds is None:
        return exc
    hint = (
        "another logged-in session has to approve this export (or 24 hours must "
        "pass if there is none). Approve it in Settings → Privacy → Data export."
    )
    if waiting:
        hint += f" Retry in {seconds}s."
    return RateLimitError(f"the export cannot start yet: {hint}", wait_seconds=int(seconds or 0))


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SPEC_EXPORT_START = OperationSpec(
    id="export.start",
    request=ExportStartReq,
    response=TakeoutSession,
    impl=export_start,
    summary="Open a Telegram data-export (takeout) session",
    description=(
        "The returned id wraps every subsequent request in `invokeWithTakeout`, "
        "`upload.getFile` included. `file_max_size` cannot be changed later."
    ),
    aliases=("daemon.takeout.start",),
    mutating=True,
    idempotent=True,
    surface=Surface.DAEMON,
    rate_class="read",
    timeout_s=120,
    columns=("takeout_id", "scope", "started_at", "max_file_size"),
    example={"takeout_id": 1234567890, "scope": ["messages", "files"], "max_file_size": 104857600},
    example_args="export start --messages --files",
    covers_partial=(
        "takeout.contacts",
        "takeout.files",
        "takeout.messages",
        "takeout.personal-info",
        "updates.takeout-session",
    ),
    coverage_note=(
        "opens the session the other export commands run inside; each of them "
        "owns the data it fetches."
    ),
    tags=frozenset({"agent-safe"}),
)


class ExportStatusReq(Request):
    ranges: Annotated[
        bool, opt("--ranges/--no-ranges", help="Include messages.getSplitRanges output.")
    ] = True


async def export_status(ctx: OpContext, req: ExportStatusReq) -> TakeoutStatus:
    """The active takeout session and the message ranges it must be walked in.

    Split ranges are not advice. For private chats and basic groups the export
    has to call `messages.getSplitRanges` and then wrap each range in
    `invokeWithMessagesRange`, restarting pagination per range — Telethon does
    none of that, so tlgr wraps it by hand.
    """
    entry = _store(ctx).get(ctx.account)
    if not entry:
        return TakeoutStatus(active=False)

    status = TakeoutStatus(
        active=True,
        takeout_id=entry["takeout_id"],
        started_at=entry["started_at"],
        scope=entry["scope"],
        max_file_size=entry["max_file_size"],
    )
    if req.ranges:
        from telethon.tl import functions

        with contextlib.suppress(Exception):
            reply = await _invoke(ctx, functions.messages.GetSplitRangesRequest())
            status.ranges = [
                MessageRange(
                    min_id=int(getattr(row, "min_id", 0) or 0),
                    max_id=int(getattr(row, "max_id", 0) or 0),
                )
                for row in (reply or [])
            ]
    return status


SPEC_EXPORT_STATUS = OperationSpec(
    id="export.status",
    request=ExportStatusReq,
    response=TakeoutStatus,
    impl=export_status,
    summary="Show the active takeout session and its message ranges",
    aliases=("daemon.takeout.status",),
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=60,
    columns=("active", "takeout_id", "scope", "max_file_size"),
    example={"active": True, "takeout_id": 1234567890, "scope": ["messages"]},
    example_args="export status",
    covers=("updates.takeout-session", "updates.takeout-split-ranges"),
    tags=frozenset({"agent-safe"}),
)


class ExportEndReq(Request):
    failed: Annotated[
        bool, opt("--failed", help="Report the export as unsuccessful (success=false).")
    ] = False


async def export_end(ctx: OpContext, req: ExportEndReq) -> ExportResult:
    """Close the takeout session.

    Must be called: an open session blocks the next export, and the next
    `export start` then fails with a delay that looks like Telegram refusing
    rather than like a session nobody closed.
    """
    from telethon.tl import functions

    store = _store(ctx)
    entry = store.get(ctx.account)
    if not entry:
        ctx.mark_already()
        return ExportResult(finished=False, success=not req.failed)

    with contextlib.suppress(Exception):
        await _invoke(ctx, functions.account.FinishTakeoutSessionRequest(success=not req.failed))
    store.pop(ctx.account, None)
    return ExportResult(finished=True, takeout_id=entry["takeout_id"], success=not req.failed)


SPEC_EXPORT_END = OperationSpec(
    id="export.end",
    request=ExportEndReq,
    response=ExportResult,
    impl=export_end,
    summary="Close the takeout session",
    description="An open session blocks the next export, so this is not optional.",
    aliases=("export.finish", "daemon.takeout.finish"),
    mutating=True,
    idempotent=True,
    surface=Surface.DAEMON,
    rate_class="read",
    timeout_s=60,
    example={"finished": True, "takeout_id": 1234567890, "success": True},
    example_args="export end",
    covers_partial=("takeout.messages", "updates.takeout-session"),
    coverage_note="closes the session; the data comes from the download commands.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# export account download
# ---------------------------------------------------------------------------


class ExportAccountReq(Request):
    out: Annotated[str, opt("--out", metavar="DIR", kind="path", help="Output directory.")] = (
        "./telegram-export"
    )
    photos: Annotated[bool, opt("--photos", help="Profile photos.")] = False
    sessions: Annotated[bool, opt("--sessions", help="Sessions and websites.")] = False
    stories: Annotated[bool, opt("--stories", help="Story archive.")] = False
    contacts: Annotated[bool, opt("--contacts", help="Contacts and top peers.")] = False
    left_channels: Annotated[bool, opt("--left-channels", help="Channels I left.")] = False
    everything: Annotated[bool, opt("--everything", help="All of the above.")] = True


async def export_account_download(ctx: OpContext, req: ExportAccountReq) -> ExportResult:
    """Export the personal information a takeout covers.

    Every call is wrapped in `invokeWithTakeout` when a session is open, and
    runs normally when one is not — which is deliberately useful: the
    personal-info half needs no approval delay, so it works while the message
    export is still waiting for one.
    """
    from telethon.tl import functions

    out = Path(req.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    want = _wanted(req)
    written: list[ExportedFile] = []
    skipped: list[str] = []

    jobs: list[tuple[str, str, Any]] = [
        ("profile", "profile.json", functions.users.GetFullUserRequest(id="me")),
        (
            "photos",
            "photos.json",
            functions.photos.GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=100),
        ),
        ("sessions", "sessions.json", functions.account.GetAuthorizationsRequest()),
        ("sessions", "websites.json", functions.account.GetWebAuthorizationsRequest()),
        ("contacts", "contacts.json", functions.contacts.GetSavedRequest()),
        (
            "left_channels",
            "left_channels.json",
            functions.channels.GetLeftChannelsRequest(offset=0),
        ),
    ]

    for name, filename, request in jobs:
        if name not in want:
            continue
        if name == "profile" and "photos" not in want and not req.everything:
            continue
        try:
            if name == "profile":
                request = functions.users.GetFullUserRequest(id=await _self(ctx))
            elif name == "photos":
                request = functions.photos.GetUserPhotosRequest(
                    user_id=await _self(ctx), offset=0, max_id=0, limit=100
                )
            reply = await _invoke(ctx, request)
        except Exception as exc:
            skipped.append(f"{filename}: {type(exc).__name__}: {exc}")
            continue
        written.append(_dump(out / filename, reply))

    if "stories" in want:
        with contextlib.suppress(Exception):
            reply = await _invoke(
                ctx,
                functions.stories.GetStoriesArchiveRequest(
                    peer=await _self(ctx), offset_id=0, limit=100
                ),
            )
            written.append(_dump(out / "stories.json", reply))

    for note in skipped:
        ctx.warn(note)
    return ExportResult(written=len(written), files=written, out=str(out), skipped=skipped)


def _wanted(req: ExportAccountReq) -> set[str]:
    chosen = {
        name
        for name in ("photos", "sessions", "stories", "contacts", "left_channels")
        if getattr(req, name, False)
    }
    if chosen and not any(
        getattr(req, name)
        for name in ("photos", "sessions", "stories", "contacts", "left_channels")
    ):
        chosen = set()
    if not chosen:
        chosen = {"photos", "sessions", "stories", "contacts", "left_channels"}
    chosen.add("profile")
    return chosen


async def _self(ctx: OpContext) -> Any:
    from telethon.tl import types

    return types.InputUserSelf()


def _dump(path: Path, value: Any) -> ExportedFile:
    from tlgr.core.tl import tl_to_builtins

    body = json.dumps(tl_to_builtins(value), ensure_ascii=False, indent=2)
    path.write_text(body, encoding="utf-8")
    return ExportedFile(path=str(path), kind=path.stem, bytes=len(body.encode("utf-8")))


SPEC_EXPORT_ACCOUNT = OperationSpec(
    id="export.account.download",
    request=ExportAccountReq,
    response=ExportResult,
    impl=export_account_download,
    summary="Export personal info: profile, photos, sessions, stories, contacts, left channels",
    description=(
        "Runs inside the takeout session when one is open, and normally when "
        "it is not — so the personal-info half works while a message export "
        "is still waiting for approval."
    ),
    surface=Surface.DAEMON,
    rate_class="bulk",
    timeout_s=900,
    columns=("written", "out"),
    example={"written": 5, "out": "./telegram-export"},
    example_args="export account download --out ./export",
    covers=("takeout.contacts", "takeout.personal-info", "updates.takeout-export-run"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# export message download
# ---------------------------------------------------------------------------


class ExportMessagesReq(Request):
    out: Annotated[str, opt("--out", metavar="DIR", kind="path", help="Output directory.")] = (
        "./telegram-export"
    )
    chat: Annotated[
        list[PeerRef],
        opt("--chat", metavar="CHAT", kind="peer", help="Only these chats (repeatable)."),
    ] = []
    since: Annotated[
        str | None, opt("--since", metavar="WHEN", kind="datetime", help="Only after this.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", kind="datetime", help="Only before this.")
    ] = None
    files: Annotated[bool, opt("--files", help="Also download media.")] = False
    per_chat: Annotated[
        int, opt("--per-chat", metavar="N", ge=1, le=100000, help="Messages per chat.")
    ] = 1000


async def export_message_download(
    ctx: OpContext, req: ExportMessagesReq
) -> AsyncIterator[Page[Message]]:
    """Export chat history inside the takeout session, as NDJSON on disk.

    Checkpointed per chat, because a takeout still meets FLOOD_WAIT — a
    generous budget is not an absent one — and an export that has to restart
    from message zero after four hours is an export nobody finishes.
    """
    _active(ctx)  # refuse to export outside a session: it would not be a takeout
    out = Path(req.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    client = _client(ctx)
    since = parse_dt(req.since) if req.since else None
    until = parse_dt(req.until) if req.until else None

    if not req.chat:
        raise UsageError(
            "name the chats to export with --chat; exporting every dialog is a "
            "different, much longer operation and should be asked for explicitly",
            field="chat",
        )

    for ref in req.chat:
        peer = await _send.resolve(ctx, ref)
        chat_id = _send.peer_id_of(peer)
        path = out / f"chat_{chat_id}.jsonl"
        rows: list[Message] = []
        with path.open("a", encoding="utf-8") as handle:
            async for message in client.iter_messages(peer, limit=req.per_chat, offset_date=until):
                stamp = getattr(message, "date", None)
                if since is not None and stamp is not None and stamp < since:
                    break
                model = message_to_model(message, chat_id=chat_id)
                rows.append(model)
                from tlgr.models.base import to_builtins

                handle.write(json.dumps(to_builtins(model), ensure_ascii=False) + "\n")
        if req.files:
            ctx.warn(
                "--files is recorded but media are not downloaded here; use "
                "`tlgr media download` per message, which shares the takeout session"
            )
        yield build_page(
            rows,
            op="export.message.download",
            kind=PageKind.HISTORY,
            state={"chat_id": chat_id},
            account=ctx.account,
            has_more=False,
            total=len(rows),
        )


SPEC_EXPORT_MESSAGES = OperationSpec(
    id="export.message.download",
    request=ExportMessagesReq,
    response=Page[Message],
    impl=export_message_download,
    summary="Export chat history inside the takeout session",
    description=(
        "One NDJSON file per chat, appended as it goes: a takeout still meets "
        "FLOOD_WAIT, and an export that restarts from zero after four hours "
        "is one nobody finishes."
    ),
    stream=True,
    paginated=PageKind.HISTORY,
    surface=Surface.DAEMON,
    rate_class="bulk",
    timeout_s=900,
    columns=("id", "date", "text"),
    empty_exit=EXIT_EMPTY,
    example={
        "items": [
            {
                "id": 12345,
                "chat_id": 777123,
                "date": "2026-09-03T09:14:07Z",
                "date_unix": 1788340447,
            }
        ],
        "has_more": False,
    },
    example_args="export message download --chat @alice --out ./export",
    covers=("takeout.files", "takeout.messages"),
    tags=frozenset({"agent-safe"}),
)
