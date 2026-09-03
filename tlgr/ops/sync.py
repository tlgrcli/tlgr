"""The `sync` group: the update transport, made inspectable.

Not to be confused with `chat catchup`, which is the unread digest a human
reads. This is `updates.getDifference` and the boxes it advances — the
machinery that decides whether an event ever existed for the daemon at all.

The distinction that runs through the whole group: **catching up** replays a
gap, **resetting** gives up on one. `sync catch-up` asks Telegram for what was
missed; `sync reset` throws the local state away and re-baselines, marking
everything before the new state as seen and unrecoverable. Conflating them is
how a corrupted `pts` gets "fixed" by silently discarding a day of messages.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.message import Message
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.sync import (
    CatchUpResult,
    ChannelState,
    DifferenceResult,
    ResetResult,
    SyncStatus,
)
from tlgr.ops import _send
from tlgr.ops._params import arg, opt, parse_dt
from tlgr.ops._serialize import message_to_model
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: Telegram's own caps. `pts_total_limit` is bounded at 10,000 for the common
#: box and the channel `limit` at 100, and exceeding either is an RPC error
#: rather than a larger answer.
_COMMON_LIMIT = (1, 10_000)
_CHANNEL_LIMIT = (1, 100)


def _sessions(ctx: OpContext) -> Any:
    daemon = getattr(ctx, "daemon", None)
    sessions = getattr(daemon, "sessions", None)
    if sessions is None:
        raise UsageError("this operation runs inside the daemon")
    return sessions


def _spanned(ctx: OpContext) -> list[str]:
    alias = (ctx.account or "").strip()
    sessions = _sessions(ctx)
    known = list(getattr(sessions, "aliases", []) or [])
    if alias and alias != "all":
        if known and alias not in known:
            raise NotFoundError(f"account {alias!r} is not connected. Run: tlgr daemon status")
        return [alias]
    return known


def _session(ctx: OpContext, alias: str) -> Any:
    session = _sessions(ctx).get(alias)
    if session is None or session.client is None:
        raise NotFoundError(f"account {alias!r} is not connected. Run: tlgr daemon status")
    return session


# ---------------------------------------------------------------------------
# sync status
# ---------------------------------------------------------------------------


class SyncStatusReq(Request):
    channels: Annotated[bool, opt("--channels", help="Include the per-channel pts table.")] = False
    refresh: Annotated[
        bool, opt("--refresh", help="Also call updates.getState and report the server delta.")
    ] = False


async def sync_status(ctx: OpContext, req: SyncStatusReq) -> SyncStatus:
    """The update cursors, and how far behind they are.

    The cheapest health check a long-running daemon has. Read
    `access_hash_known` first when a channel seems to have gone quiet: without
    an access hash in the session, `catch_up()` *skips* that channel entirely
    — Telethon logs "will not catch up" and carries on — so it looks idle
    rather than broken.
    """
    from tlgr.core import telethon_compat as compat

    alias = (_spanned(ctx) or [ctx.account])[0]
    session = _session(ctx, alias)
    client = session.client
    state, channels = compat.session_state(client)

    report = SyncStatus(
        account=alias,
        pts=state.get("pts"),
        qts=state.get("qts"),
        seq=state.get("seq"),
        date=state.get("date"),
        date_unix=state.get("date_unix"),
        unread_count=state.get("unread_count"),
        phase="catching_up" if session.catch_up_pending else str(session.state),
        getting_difference=bool(session.catch_up_pending),
    )
    if report.date_unix:
        report.behind_seconds = max(0, int(time.time()) - int(report.date_unix))
    if session.last_update:
        report.last_update_at = _stamp(session.last_update)
        report.no_updates_for_seconds = max(0, int(time.time() - session.last_update))

    if req.channels:
        known = _known_channels(client)
        report.channels = [
            ChannelState(
                chat_id=_marked(channel_id), pts=pts, access_hash_known=channel_id in known
            )
            for channel_id, pts in sorted(channels.items())
        ]
        blind = [row.chat_id for row in report.channels if not row.access_hash_known]
        if blind:
            ctx.warn(
                f"{len(blind)} channel(s) have no access hash in the session; catch-up "
                "skips them silently. Warm the dialog list: tlgr chat list --all"
            )

    if req.refresh:
        from telethon.tl import functions

        server = await client(functions.updates.GetStateRequest())
        report.server_pts = int(getattr(server, "pts", 0) or 0)
        report.server_seq = int(getattr(server, "seq", 0) or 0)
        if report.pts is not None:
            report.behind_pts = max(0, report.server_pts - int(report.pts))
    return report


def _stamp(value: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marked(channel_id: int) -> int:
    from tlgr.core.tl import CHANNEL_MARK

    return CHANNEL_MARK - channel_id if channel_id > 0 else channel_id


def _known_channels(client: Any) -> set[int]:
    """Channel ids the session holds an access hash for."""
    session = getattr(client, "session", None)
    cursor = getattr(session, "_cursor", None)
    if not callable(cursor):
        return set()
    with contextlib.suppress(Exception):
        rows = cursor().execute("select id from entities").fetchall()
        return {abs(int(row[0])) % 10_000_000_000 for row in rows}
    return set()


SPEC_SYNC_STATUS = OperationSpec(
    id="sync.status",
    request=SyncStatusReq,
    response=SyncStatus,
    impl=sync_status,
    summary="Show the update cursors (pts/qts/seq/date) and how far behind the account is",
    description=(
        "`access_hash_known=false` on a channel means catch-up skips it "
        "silently — Telethon will not call getChannelDifference without one — "
        "so the channel looks idle rather than broken."
    ),
    aliases=("sync.state", "daemon.sync.status"),
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=60,
    columns=("account", "pts", "qts", "seq", "date", "behind_seconds", "phase"),
    example={
        "account": "work",
        "pts": 91824,
        "qts": 12,
        "seq": 4410,
        "behind_seconds": 3,
        "phase": "online",
    },
    example_args="sync status --channels --refresh",
    covers=(
        "updates.sync-get-state",
        "updates.sync-qts-gap-algorithm",
        "updates.sync-seq-gap-algorithm",
    ),
    covers_partial=(
        "updates.sync-force-resync",
        "updates.sync-get-channel-difference",
        "updates.sync-pts-gap-algorithm",
        "updates.sync-state-persistence",
        "updates.sync-too-long",
    ),
    coverage_note=(
        "reports the boxes; advancing them is `sync catch-up`, running one "
        "difference by hand is `sync difference`, and discarding them is "
        "`sync reset`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# sync catch-up
# ---------------------------------------------------------------------------


class CatchUpReq(Request):
    wait: Annotated[
        bool, opt("--wait/--no-wait", help="Block until the difference is drained.")
    ] = True
    catch_up_timeout: Annotated[
        int,
        opt("--catch-up-timeout", metavar="SECONDS", ge=1, le=900, help="Give up waiting."),
    ] = 120


async def sync_catch_up(ctx: OpContext, req: CatchUpReq) -> CatchUpResult:
    """Force a difference fetch so nothing missed while offline is lost.

    This is the single most important correctness operation in the group: an
    account that reconnects without it silently misses everything that
    happened while it was away, and there is no later signal that it did.
    """
    from tlgr.core import telethon_compat as compat

    rows: list[CatchUpResult] = []
    for alias in _spanned(ctx):
        session = _session(ctx, alias)
        before, _ = compat.session_state(session.client)
        started = time.monotonic()
        bus = getattr(ctx, "bus", None)
        seq_before = bus.latest_seq(alias) if bus is not None else 0

        if req.wait:
            await _bounded(session.catch_up(), req.catch_up_timeout)
        else:
            await session.catch_up()

        after, _ = compat.session_state(session.client)
        rows.append(
            CatchUpResult(
                account=alias,
                events_replayed=(bus.latest_seq(alias) - seq_before) if bus is not None else 0,
                pts_before=before.get("pts"),
                pts_after=after.get("pts"),
                duration_ms=int((time.monotonic() - started) * 1000),
                too_long=bool(session.resync_needed),
            )
        )
        session.resync_needed.clear()

    if not rows:
        raise NotFoundError("no accounts are connected. Run: tlgr daemon status")
    if len(rows) > 1:
        ctx.warn(f"caught up {len(rows)} accounts; reporting the first")
    return rows[0]


async def _bounded(coro: Any, seconds: int) -> None:
    import asyncio

    try:
        await asyncio.wait_for(coro, timeout=seconds)
    except (TimeoutError, asyncio.TimeoutError):
        from tlgr.core.errors import RetryableError

        raise RetryableError(
            f"the difference did not drain within {seconds}s; it is still running "
            "in the daemon — check progress with `tlgr sync status`"
        ) from None


SPEC_SYNC_CATCH_UP = OperationSpec(
    id="sync.catch-up",
    request=CatchUpReq,
    response=CatchUpResult,
    impl=sync_catch_up,
    summary="Force a difference fetch so nothing missed while offline is lost",
    description=(
        "Not `chat catchup`, which is the unread digest. This is "
        "`updates.getDifference`: without it an account that was away silently "
        "misses everything that happened, with no later signal that it did."
    ),
    aliases=("daemon.sync.catch-up",),
    mutating=True,
    idempotent=True,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="read",
    timeout_s=900,
    columns=("account", "events_replayed", "pts_before", "pts_after", "duration_ms"),
    example={"account": "work", "events_replayed": 12, "pts_before": 91800, "pts_after": 91824},
    example_args="sync catch-up",
    covers=("updates.sync-get-difference", "updates.sync-too-long"),
    covers_partial=("updates.sync-catch-up-on-start", "updates.sync-force-resync"),
    coverage_note=(
        "the manual fetch; doing it at start is `daemon start --catch-up`, and "
        "giving up on a gap is `sync reset`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# sync difference
# ---------------------------------------------------------------------------


class DifferenceReq(Request):
    chat: Annotated[
        PeerRef | None,
        opt(
            "--chat", metavar="CHAT", kind="peer", help="Run getChannelDifference for this channel."
        ),
    ] = None
    pts: Annotated[int | None, opt("--pts", metavar="N", help="Start from this pts.")] = None
    qts: Annotated[int | None, opt("--qts", metavar="N", help="Start from this qts.")] = None
    date: Annotated[
        str | None, opt("--date", metavar="WHEN", kind="datetime", help="Start from this date.")
    ] = None
    depth: Annotated[
        int,
        opt(
            "--depth",
            metavar="N",
            ge=1,
            le=10000,
            help="pts_total_limit (common box) or limit (channel).",
        ),
    ] = 1000
    follow: Annotated[
        int | None,
        opt(
            "--follow",
            metavar="SECONDS",
            help="Short-poll the channel for this long, honouring the returned timeout.",
        ),
    ] = None
    apply: Annotated[
        bool, opt("--apply", help="Feed the result into the daemon's state and event stream.")
    ] = False


async def sync_difference(ctx: OpContext, req: DifferenceReq) -> DifferenceResult:
    """Run `updates.getDifference` explicitly, as a diagnostic.

    Read-only by default, and that is the whole safety property: without
    `--apply` the daemon's stored `pts` is not advanced, so running this
    cannot create the gap it was meant to diagnose. `differenceSlice` is
    looped until final.
    """
    from telethon.tl import functions

    alias = (_spanned(ctx) or [ctx.account])[0]
    session = _session(ctx, alias)
    client = session.client

    if req.chat is not None:
        return await _channel_difference(ctx, client, req)

    low, high = _COMMON_LIMIT
    depth = max(low, min(high, req.depth))
    state = await _resolve_common_state(client, req)
    result = DifferenceResult(kind="common", dry_run=not req.apply)

    for _ in range(64):  # a slice loop with a bound, never an open one
        request = functions.updates.GetDifferenceRequest(
            pts=state["pts"], date=state["date"], qts=state["qts"], pts_total_limit=depth
        )
        result.requests.append({"request": type(request).__name__, "pts": state["pts"]})
        reply = await client(request)
        name = type(reply).__name__

        if name == "UpdatesDifferenceEmpty":
            result.final = True
            result.new_seq = int(getattr(reply, "seq", 0) or 0)
            break
        if name == "UpdatesDifferenceTooLong":
            result.too_long = True
            result.final = True
            result.new_pts = int(getattr(reply, "pts", 0) or 0)
            ctx.warn(
                "the server answered differenceTooLong: the gap is unrecoverable from "
                "this pts. Re-baseline with `tlgr sync reset`, then refill ranges you "
                "care about with `tlgr sync backfill`."
            )
            break

        result.messages += len(getattr(reply, "new_messages", None) or [])
        result.other_updates += len(getattr(reply, "other_updates", None) or [])
        result.users += len(getattr(reply, "users", None) or [])
        result.chats += len(getattr(reply, "chats", None) or [])

        final_state = getattr(reply, "state", None) or getattr(reply, "intermediate_state", None)
        result.new_pts = int(getattr(final_state, "pts", 0) or 0)
        result.new_qts = int(getattr(final_state, "qts", 0) or 0)
        result.new_seq = int(getattr(final_state, "seq", 0) or 0)
        result.new_date = _fmt(getattr(final_state, "date", None))

        if name == "UpdatesDifference":
            result.final = True
            break
        # `updates.differenceSlice`: keep going from the intermediate state.
        result.final = False
        state = {
            "pts": result.new_pts,
            "qts": result.new_qts,
            "date": getattr(final_state, "date", state["date"]),
        }

    if req.apply:
        await session.catch_up()
        result.applied = True
    return result


async def _resolve_common_state(client: Any, req: DifferenceReq) -> dict[str, Any]:
    from telethon.tl import functions

    from tlgr.core import telethon_compat as compat

    stored, _channels = compat.session_state(client)
    pts = req.pts if req.pts is not None else stored.get("pts")
    qts = req.qts if req.qts is not None else stored.get("qts")
    date = parse_dt(req.date) if req.date else None

    if pts is None or date is None:
        # No stored state and no override: ask the server where "now" is, so
        # the probe starts from something real rather than from zero — which
        # would ask Telegram to replay the account's entire history.
        server = await client(functions.updates.GetStateRequest())
        pts = pts if pts is not None else int(getattr(server, "pts", 0) or 0)
        qts = qts if qts is not None else int(getattr(server, "qts", 0) or 0)
        date = date or getattr(server, "date", None)
    return {"pts": int(pts or 0), "qts": int(qts or 0), "date": date}


def _fmt(value: Any) -> str | None:
    from tlgr.core.timefmt import fmt_dt

    return fmt_dt(value)


async def _channel_difference(ctx: OpContext, client: Any, req: DifferenceReq) -> DifferenceResult:
    """`updates.getChannelDifference`, optionally short-polled.

    The `timeout` the server returns is an instruction, not a suggestion:
    re-invoking a *final* channel difference sooner than it says is exactly
    the polling Telegram asks clients not to do.
    """
    import asyncio

    from telethon.tl import functions, types

    from tlgr.core import telethon_compat as compat

    peer = await _send.resolve(ctx, req.chat)
    channel = _input_channel(peer)
    low, high = _CHANNEL_LIMIT
    limit = max(low, min(high, req.depth))

    stored_pts = req.pts
    if stored_pts is None:
        _state, channels = compat.session_state(client)
        marked = _send.peer_id_of(peer)
        stored_pts = channels.get(abs(marked) % 10_000_000_000, 1)

    result = DifferenceResult(kind="channel", dry_run=not req.apply)
    deadline = time.monotonic() + (req.follow or 0)

    while True:
        request = functions.updates.GetChannelDifferenceRequest(
            channel=channel,
            filter=types.ChannelMessagesFilterEmpty(),
            pts=int(stored_pts or 1),
            limit=limit,
            force=True,
        )
        result.requests.append({"request": type(request).__name__, "pts": int(stored_pts or 1)})
        reply = await client(request)
        name = type(reply).__name__
        result.timeout = getattr(reply, "timeout", None)

        if name == "UpdatesChannelDifferenceTooLong":
            result.too_long = True
            result.final = True
            ctx.warn(
                "the channel's gap is unrecoverable from this pts; refill the range "
                "with `tlgr sync backfill <chat>`"
            )
            break
        if name == "UpdatesChannelDifferenceEmpty":
            result.final = bool(getattr(reply, "final", True))
            result.new_pts = int(getattr(reply, "pts", 0) or 0)
        else:
            result.messages += len(getattr(reply, "new_messages", None) or [])
            result.other_updates += len(getattr(reply, "other_updates", None) or [])
            result.users += len(getattr(reply, "users", None) or [])
            result.chats += len(getattr(reply, "chats", None) or [])
            result.final = bool(getattr(reply, "final", True))
            result.new_pts = int(getattr(reply, "pts", 0) or 0)
        stored_pts = result.new_pts

        if not result.final:
            continue
        if req.follow is None or time.monotonic() >= deadline:
            break
        # Honour the server's own pacing rather than inventing one.
        await asyncio.sleep(max(1, int(result.timeout or 10)))

    if req.apply:
        result.applied = True
        ctx.warn(
            "--apply on a channel difference only advances the stored pts through the "
            "daemon's own catch-up; run `tlgr sync catch-up` to dispatch the events"
        )
    return result


def _input_channel(peer: Any) -> Any:
    from telethon import utils

    try:
        return utils.get_input_channel(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError(
            "--chat must be a channel or supergroup; the common box covers the rest",
            field="chat",
        ) from exc


SPEC_SYNC_DIFFERENCE = OperationSpec(
    id="sync.difference",
    request=DifferenceReq,
    response=DifferenceResult,
    impl=sync_difference,
    summary="Run updates.getDifference / getChannelDifference explicitly (diagnostics)",
    description=(
        "Read-only without `--apply`: the daemon's stored pts is untouched, "
        "so the probe cannot create the gap it was meant to diagnose. "
        "`--follow` short-polls a channel, honouring the timeout the server "
        "returns rather than a pace tlgr invented."
    ),
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=300,
    columns=("kind", "final", "new_pts", "messages", "other_updates", "too_long"),
    example={"kind": "common", "final": True, "new_pts": 91824, "messages": 3},
    example_args="sync difference --chat @news --follow 30",
    covers=(
        "updates.sync-channel-short-poll",
        "updates.sync-get-channel-difference",
        "updates.sync-pts-gap-algorithm",
    ),
    covers_partial=("updates.sync-get-difference", "updates.sync-qts-gap-algorithm"),
    coverage_note="runs one by hand; the automatic path is `sync catch-up`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# sync reset
# ---------------------------------------------------------------------------


class SyncResetReq(Request):
    chat: Annotated[
        list[PeerRef],
        opt("--chat", metavar="CHAT", kind="peer", help="Only reset this channel's pts."),
    ] = []
    all_channels: Annotated[
        bool, opt("--all-channels", help="Reset every per-channel pts, keeping the common box.")
    ] = False


async def sync_reset(ctx: OpContext, req: SyncResetReq) -> ResetResult:
    """Throw the local update state away and re-baseline from the server.

    This is the *give up on the gap* path: everything before the new state is
    marked seen and is not recoverable by any later catch-up. It exists for
    the case a corrupted state loops on `getDifference` — and it is
    destructive precisely because the alternative, silently discarding
    messages while calling it a fix, is what makes a sync bug invisible.
    """
    from telethon.tl import functions, types

    from tlgr.core import telethon_compat as compat

    alias = (_spanned(ctx) or [ctx.account])[0]
    session = _session(ctx, alias)
    client = session.client
    before, channels = compat.session_state(client)
    result = ResetResult(account=alias, pts_before=before.get("pts"))

    if req.chat:
        for ref in req.chat:
            peer = await _send.resolve(ctx, ref)
            marked = _send.peer_id_of(peer)
            entity_id = abs(marked) % 10_000_000_000
            compat.set_session_state(
                client,
                types.updates.State(
                    pts=1, qts=0, date=before.get("date_unix") or 0, seq=0, unread_count=0
                ),
                entity_id=entity_id,
            )
            result.channels_reset.append(marked)
        result.reset = True
        return result

    if req.all_channels:
        for channel_id in channels:
            compat.set_session_state(
                client,
                types.updates.State(pts=1, qts=0, date=0, seq=0, unread_count=0),
                entity_id=channel_id,
            )
            result.channels_reset.append(_marked(channel_id))
        result.reset = True
        return result

    server = await client(functions.updates.GetStateRequest())
    compat.set_session_state(client, server, entity_id=0)
    result.pts_after = int(getattr(server, "pts", 0) or 0)
    result.reset = True
    ctx.warn(
        "the local update state was replaced with the server's: everything before "
        f"pts {result.pts_after} is now marked seen and cannot be replayed"
    )
    return result


SPEC_SYNC_RESET = OperationSpec(
    id="sync.reset",
    request=SyncResetReq,
    response=ResetResult,
    impl=sync_reset,
    summary="Throw away the local update state and re-baseline from the server",
    description=(
        "The give-up path, not the recovery one: everything before the new "
        "state is marked seen and is unrecoverable. Use it when a corrupted "
        "state loops on getDifference; use `sync catch-up` to replay a gap."
    ),
    mutating=True,
    destructive=True,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="read",
    timeout_s=120,
    columns=("account", "reset", "pts_before", "pts_after"),
    example={"account": "work", "reset": True, "pts_before": 91824, "pts_after": 91900},
    example_args="sync reset --yes",
    covers=("updates.sync-force-resync", "updates.sync-state-persistence"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# sync backfill
# ---------------------------------------------------------------------------


class BackfillReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer")]
    from_id: Annotated[
        int | None, opt("--from-id", metavar="ID", help="Lowest message id (inclusive).")
    ] = None
    to_id: Annotated[
        int | None, opt("--to-id", metavar="ID", help="Highest message id (inclusive).")
    ] = None
    chunk: Annotated[int, opt("--chunk", metavar="N", ge=1, le=200, help="Ids per request.")] = 200
    emit: Annotated[
        bool, opt("--emit", help="Emit the refilled messages as events, marked `backfill`.")
    ] = False


async def sync_backfill(ctx: OpContext, req: BackfillReq) -> AsyncIterator[Page[Message]]:
    """Refill a message-id range after a box overflow.

    `messages.getHistory` cannot do this: it is bounded by the same box that
    overflowed. Fetching by explicit id can, and deleted messages come back as
    `messageEmpty`, so the answer is always complete — a missing id means the
    message is gone, not that the fetch fell short.
    """
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    low, high = _range(req)
    client = getattr(ctx, "client", None)
    if client is None:
        raise UsageError("this operation needs a connected account")

    for start in range(low, high + 1, req.chunk):
        ids = list(range(start, min(start + req.chunk, high + 1)))
        fetched = await client.get_messages(peer, ids=ids)
        rows: list[Message] = []
        missing: list[int] = []
        for message_id, message in zip(ids, fetched, strict=False):
            if message is None or type(message).__name__ == "MessageEmpty":
                missing.append(message_id)
                continue
            model = message_to_model(message, chat_id=chat_id)
            rows.append(model)
            if req.emit:
                from tlgr.models.base import to_builtins

                payload = to_builtins(model)
                ctx.emit(
                    "message_new",
                    {**(payload if isinstance(payload, dict) else {}), "backfill": True},
                    chat_id=chat_id,
                )
        page = build_page(
            rows,
            op="sync.backfill",
            kind=PageKind.HISTORY,
            state={"offset_id": ids[-1]},
            account=ctx.account,
            has_more=ids[-1] < high,
        )
        if missing:
            ctx.warn(f"{len(missing)} id(s) in {ids[0]}-{ids[-1]} are deleted or never existed")
        yield page


def _range(req: BackfillReq) -> tuple[int, int]:
    if req.from_id is None or req.to_id is None:
        raise UsageError("backfill needs an explicit range: --from-id and --to-id", field="from_id")
    if req.to_id < req.from_id:
        raise UsageError("--to-id is lower than --from-id", field="to_id")
    if req.to_id - req.from_id > 100_000:
        raise UsageError(
            "that range is over 100,000 messages; narrow it or run it in pieces",
            field="to_id",
        )
    return req.from_id, req.to_id


SPEC_SYNC_BACKFILL = OperationSpec(
    id="sync.backfill",
    request=BackfillReq,
    response=Page[Message],
    impl=sync_backfill,
    summary="Refill a message-id range after a box overflow (differenceTooLong)",
    description=(
        "`messages.getHistory` cannot fill a channel gap — it is bounded by "
        "the same box that overflowed. Fetching by explicit id can, and a "
        "deleted message comes back as `messageEmpty`, so the range is always "
        "complete."
    ),
    stream=True,
    paginated=PageKind.HISTORY,
    surface=Surface.DAEMON,
    rate_class="read",
    timeout_s=900,
    columns=("id", "date", "text"),
    empty_exit=EXIT_EMPTY,
    example={
        "items": [
            {
                "id": 91800,
                "chat_id": -1001,
                "date": "2026-09-03T09:00:00Z",
                "date_unix": 1788339600,
            }
        ],
        "has_more": True,
    },
    example_args="sync backfill @news --from-id 91800 --to-id 91900",
    covers=("updates.sync-difference-too-long",),
    tags=frozenset({"agent-safe"}),
)
