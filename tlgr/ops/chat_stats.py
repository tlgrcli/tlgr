"""`chat stats *`, `chat revenue *` and `boost *`: the numbers, never redrawn.

Three rules, and the first one is the reason this module exists at all.

* **Every `stats.*` call goes to `channelFull.stats_dc`, not the home DC.**
  The server answers a stats request on the wrong data centre with
  `STATS_MIGRATE_X`, and a client that does not follow it reports "no
  statistics" for a channel that has plenty. `_stats()` follows the
  migration through Telethon's exported sender, once, for every stats call
  in the module.
* **A graph is emitted verbatim.** Telegram's chart specification
  (columns/types/colors/names/subchart/y_scaled/percentage/stacked) is
  reported as the API's own JSON. tlgr never tries to draw it: a redrawn
  chart is a chart that can be subtly wrong, and the caller is better placed
  to render than we are. Async graphs appear as `{token, zoom_token}` until
  `--load-graphs` or `--graph` resolves them.
* **Revenue is read-only on purpose.** When `withdrawal_enabled` is true the
  command says so and points at an official client: tlgr does not implement
  `payments.getStarsRevenueWithdrawalUrl`, because it moves money and wants
  the 2FA password.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import IndeterminateError, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.admin import (
    Boost,
    BoostApplied,
    BoostStatus,
    ChatStats,
    Graph,
    PublicForward,
    RevenueSummary,
    RevenueTransaction,
    StatValue,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _admin, _send
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_STATS: dict[str, Any] = {
    "chat_id": -1001600,
    "type": "broadcast",
    "followers": {"current": 1200.0, "previous": 1150.0, "growth": 4.3},
    "graphs": [{"name": "growth_graph", "token": "abc123"}],
}

#: appConfig keys that gate a tlgr flag, and the flag they gate. `boost get
#: --features` maps them so "why is --autotranslate refused" has an answer
#: that does not require reading the API docs.
_BOOST_FEATURES = {
    "channel_autotranslation_level_min": "chat setting set --autotranslate",
    "channel_wallpaper_level_min": "chat wallpaper set",
    "group_wallpaper_level_min": "chat wallpaper set",
    "channel_emoji_status_level_min": "chat edit --emoji-status",
    "group_emoji_status_level_min": "chat edit --emoji-status",
    "group_emoji_stickers_level_min": "chat setting set --emoji-set",
    "channel_restrict_sponsored_level_min": "chat setting set --ads off",
    "channel_custom_wallpaper_level_min": "chat wallpaper set --file",
    "channel_profile_bg_icon_level_min": "chat edit --profile-color-emoji",
    "channel_bg_icon_level_min": "chat edit --color-emoji",
    "boosts_channel_level_max": "(the level ceiling)",
}


async def _stats(ctx: OpContext, request: Any) -> Any:
    """Send a `stats.*` request, following STATS_MIGRATE to the stats DC.

    Telethon's own `get_stats` does this with a borrowed exported sender;
    doing it here means every stats call in the module — not just the two
    Telethon wraps — reaches the right data centre.
    """
    client = _admin.client(ctx)
    try:
        return await client(request)
    except Exception as exc:
        if type(exc).__name__ != "StatsMigrateError":
            raise
        dc = int(getattr(exc, "dc", 0) or 0)
        borrow = getattr(client, "_borrow_exported_sender", None)
        if borrow is None or not dc:  # pragma: no cover - Telethon always has it
            raise IndeterminateError(
                "these statistics live on another data centre and this Telethon "
                "build has no exported sender to reach it"
            ) from exc
        sender = await borrow(dc)
        try:
            return await sender.send(request)
        finally:
            release = getattr(client, "_return_exported_sender", None)
            if release is not None:
                await release(sender)


def _moment(value: Any) -> str | None:
    """`next_withdrawal_at` arrives as a unix int, not a datetime."""
    if isinstance(value, int) and value:
        from datetime import datetime, timezone

        return fmt_dt(datetime.fromtimestamp(value, tz=timezone.utc))
    return fmt_dt(value)


def _stat_value(raw: Any) -> StatValue | None:
    """`statsAbsValueAndPrev` / `statsPercentValue` → one shape with growth."""
    if raw is None:
        return None
    if hasattr(raw, "part"):
        part = float(getattr(raw, "part", 0.0) or 0.0)
        total = float(getattr(raw, "total", 0.0) or 0.0)
        return StatValue(
            current=part, previous=total, growth=round(100.0 * part / total, 2) if total else 0.0
        )
    current = float(getattr(raw, "current", 0.0) or 0.0)
    previous = float(getattr(raw, "previous", 0.0) or 0.0)
    growth = round(100.0 * (current - previous) / previous, 2) if previous else 0.0
    return StatValue(current=current, previous=previous, growth=growth)


def _graph(name: str, raw: Any) -> Graph:
    """One `statsGraph*`, whichever of the three shapes it is."""
    kind = type(raw).__name__
    if kind == "StatsGraphAsync":
        return Graph(name=name, token=str(getattr(raw, "token", "") or ""))
    if kind == "StatsGraphError":
        return Graph(name=name, error=str(getattr(raw, "error", "") or ""))
    payload = getattr(getattr(raw, "json", None), "data", None)
    parsed: Any = None
    if payload:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):  # pragma: no cover - the server sends JSON
            parsed = payload
    return Graph(
        name=name, json=parsed, zoom_token=str(getattr(raw, "zoom_token", "") or "") or None
    )


def _collect_graphs(result: Any) -> list[Graph]:
    """Every `*_graph` field on a stats reply, in declaration order.

    Reflected rather than listed: broadcast, megagroup, message and story
    statistics carry different graph sets, and a hand-written list would go
    stale the next time the server grows one.
    """
    out: list[Graph] = []
    for name in getattr(result, "__slots__", ()) or dir(result):
        if not name.endswith("_graph"):
            continue
        value = getattr(result, name, None)
        if value is not None:
            out.append(_graph(name, value))
    return out


async def _resolve_graphs(ctx: OpContext, graphs: list[Graph], *, zoom: int | None) -> None:
    from telethon.tl.functions import stats as fn

    for graph in graphs:
        if not graph.token:
            continue
        try:
            resolved = await _stats(ctx, fn.LoadAsyncGraphRequest(token=graph.token, x=zoom))
        except Exception as exc:  # a graph that will not load must not kill the report
            graph.error = f"{type(exc).__name__}: {exc}"
            continue
        loaded = _graph(graph.name, resolved)
        graph.json = loaded.json
        graph.zoom_token = loaded.zoom_token or graph.zoom_token
        graph.error = loaded.error


def _write_graphs(graphs: list[Graph], directory: str) -> None:
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    for graph in graphs:
        if graph.json is None:
            continue
        path = root / f"{graph.name}.json"
        path.write_text(json.dumps(graph.json, ensure_ascii=False, indent=2), encoding="utf-8")
        graph.path = str(path)
        graph.json = None


# ---------------------------------------------------------------------------
# chat stats get
# ---------------------------------------------------------------------------


class StatsGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel or supergroup.")]
    message: Annotated[
        int | None, opt("--message", metavar="ID", kind="msg_id", help="Per-post statistics.")
    ] = None
    story: Annotated[int | None, opt("--story", metavar="ID", help="Story statistics.")] = None
    poll: Annotated[
        int | None, opt("--poll", metavar="ID", kind="msg_id", help="Poll vote statistics.")
    ] = None
    dark: Annotated[bool, opt("--dark", help="Ask for the dark colour set in the specs.")] = False
    graph: Annotated[
        str | None, opt("--graph", metavar="TOKEN", help="Resolve one async graph token.")
    ] = None
    zoom: Annotated[int | None, opt("--zoom", metavar="X", help="With --graph: zoom into x.")] = (
        None
    )
    load_graphs: Annotated[
        bool, opt("--load-graphs", help="Resolve every async graph before printing.")
    ] = False
    out: Annotated[
        str | None, opt("--out", metavar="DIR", kind="path", help="Write graph specs to files.")
    ] = None


async def get_stats(ctx: OpContext, req: StatsGetReq) -> ChatStats:
    """Channel, supergroup, post, story or poll statistics."""
    from telethon.tl.functions import stats as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = peer_id_of(peer) or 0

    if req.graph:
        resolved = await _stats(ctx, fn.LoadAsyncGraphRequest(token=req.graph, x=req.zoom))
        graph = _graph("graph", resolved)
        stats = ChatStats(chat_id=chat_id, type="graph", graphs=[graph])
        if req.out:
            _write_graphs(stats.graphs, req.out)
        return stats

    channel = _admin.input_channel(peer) if _admin.is_channel(peer) else None
    if req.message is not None:
        if channel is None:
            raise UsageError("post statistics need a channel", field="message")
        result = await _stats(
            ctx, fn.GetMessageStatsRequest(channel=channel, msg_id=req.message, dark=req.dark)
        )
        kind = "message"
    elif req.story is not None:
        result = await _stats(ctx, fn.GetStoryStatsRequest(peer=peer, id=req.story, dark=req.dark))
        kind = "story"
    elif req.poll is not None:
        result = await _stats(
            ctx, fn.GetPollStatsRequest(peer=peer, msg_id=req.poll, dark=req.dark)
        )
        kind = "poll"
    else:
        if channel is None:
            raise UsageError(
                "statistics need a channel or supergroup; a basic group has none", field="chat"
            )
        _full, entity, _entities = await _admin.full_chat(ctx, peer)
        megagroup = bool(getattr(entity, "megagroup", False))
        request = (
            fn.GetMegagroupStatsRequest(channel=channel, dark=req.dark)
            if megagroup
            else fn.GetBroadcastStatsRequest(channel=channel, dark=req.dark)
        )
        result = await _stats(ctx, request)
        kind = "megagroup" if megagroup else "broadcast"

    period_raw = getattr(result, "period", None)
    stats = ChatStats(
        chat_id=chat_id,
        type=kind,
        period=(
            {
                "min_date": fmt_dt(getattr(period_raw, "min_date", None)) or "",
                "max_date": fmt_dt(getattr(period_raw, "max_date", None)) or "",
            }
            if period_raw is not None
            else {}
        ),
        followers=_stat_value(getattr(result, "followers", None)),
        views_per_post=_stat_value(getattr(result, "views_per_post", None)),
        shares_per_post=_stat_value(getattr(result, "shares_per_post", None)),
        reactions_per_post=_stat_value(getattr(result, "reactions_per_post", None)),
        enabled_notifications=_stat_value(getattr(result, "enabled_notifications", None)),
        members=_stat_value(getattr(result, "members", None)),
        messages=_stat_value(getattr(result, "messages", None)),
        viewers=_stat_value(getattr(result, "viewers", None)),
        posters=_stat_value(getattr(result, "posters", None)),
        views=getattr(result, "views", None),
        forwards=getattr(result, "forwards", None),
        reactions=getattr(result, "reactions", None),
        graphs=_collect_graphs(result),
    )
    for post in getattr(result, "recent_posts_interactions", None) or []:
        stats.recent_posts.append(
            {
                "msg_id": int(getattr(post, "msg_id", 0) or 0),
                "story_id": int(getattr(post, "story_id", 0) or 0),
                "views": int(getattr(post, "views", 0) or 0),
                "forwards": int(getattr(post, "forwards", 0) or 0),
                "reactions": int(getattr(post, "reactions", 0) or 0),
            }
        )
    if req.load_graphs:
        await _resolve_graphs(ctx, stats.graphs, zoom=req.zoom)
    if req.out:
        _write_graphs(stats.graphs, req.out)
    return stats


SPEC_STATS_GET = OperationSpec(
    id="chat.stats.get",
    request=StatsGetReq,
    response=ChatStats,
    impl=get_stats,
    summary="Channel, supergroup, post, story or poll statistics",
    description=(
        "Needs `channelFull.can_view_stats` (channels need about 500 "
        "members). Every call is routed to `channelFull.stats_dc`. Graph "
        "payloads are the API's own chart specification, emitted verbatim; "
        "async graphs stay as `{token, zoom_token}` until `--load-graphs` or "
        "`--graph` resolves them, and `--out DIR` writes each one to a file "
        "instead of inlining it."
    ),
    aliases=("stats.get",),
    timeout_s=300,
    columns=("chat_id", "type"),
    example=_EXAMPLE_STATS,
    example_args="chat stats get @mychannel --load-graphs",
    covers=(
        "groups-channels-admin.channel-stats",
        "groups-channels-admin.poll-stats",
        "groups-channels-admin.stats-async-graph",
        "groups-channels-admin.supergroup-stats",
        "messages-core.message-statistics",
    ),
    covers_partial=(
        "groups-channels-admin.message-stats",
        "groups-channels-admin.story-stats",
    ),
    coverage_note="Per-post numbers are here; the repost list is `chat stats list`.",
)


class StatsListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    message: Annotated[
        int | None, opt("--message", metavar="ID", kind="msg_id", help="Public forwards of a post.")
    ] = None
    story: Annotated[
        int | None, opt("--story", metavar="ID", help="Public forwards of a story.")
    ] = None


async def list_public_forwards(ctx: OpContext, req: StatsListReq) -> Page[PublicForward]:
    """Who reposted a post or a story, publicly."""
    from telethon.tl.functions import stats as fn

    limit, state = _admin.window(ctx, "chat.stats.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    offset = str(state.get("offset", "") or "")
    if req.message is not None:
        reply = await _stats(
            ctx,
            fn.GetMessagePublicForwardsRequest(
                channel=_admin.input_channel(peer), msg_id=req.message, offset=offset, limit=limit
            ),
        )
    elif req.story is not None:
        reply = await _stats(
            ctx,
            fn.GetStoryPublicForwardsRequest(peer=peer, id=req.story, offset=offset, limit=limit),
        )
    else:
        raise UsageError("name --message or --story", field="message")

    entities = _admin.entity_map(reply)
    rows: list[PublicForward] = []
    for item in getattr(reply, "forwards", None) or []:
        message = getattr(item, "message", None)
        story = getattr(item, "story", None)
        source = message if message is not None else story
        chat_id = peer_id_of(getattr(source, "peer_id", None) or getattr(source, "peer", None)) or 0
        entity = entities.get(chat_id)
        date = getattr(source, "date", None)
        rows.append(
            PublicForward(
                chat_id=chat_id,
                chat_title=_admin.display_name(entity),
                msg_id=int(getattr(message, "id", 0) or 0) or None,
                story_id=int(getattr(story, "id", 0) or 0) or None,
                views=getattr(source, "views", None),
                date=fmt_dt(date),
                date_unix=to_unix(date),
            )
        )
    return build_page(
        rows,
        op="chat.stats.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": str(getattr(reply, "next_offset", "") or "")},
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", len(rows)) or len(rows)),
    )


SPEC_STATS_LIST = OperationSpec(
    id="chat.stats.list",
    request=StatsListReq,
    response=Page[PublicForward],
    impl=list_public_forwards,
    summary="Public forwards (reposts) of a post or a story",
    description="Also routed to the stats DC. The cursor is the opaque `next_offset` string.",
    aliases=("stats.list",),
    paginated=PageKind.PARTICIPANTS,
    columns=("chat_id", "chat_title", "msg_id", "views"),
    example={"items": [{"chat_id": -1001700, "chat_title": "Repost", "msg_id": 12, "views": 90}]},
    example_args="chat stats list @mychannel --message 918",
    covers=("groups-channels-admin.message-stats", "groups-channels-admin.story-stats"),
)


# ---------------------------------------------------------------------------
# chat revenue
# ---------------------------------------------------------------------------


def _stars(raw: Any) -> int:
    """A `starsAmount` as whole Stars.

    The nanos field is a fractional Star and is dropped here deliberately:
    every balance tlgr prints is the whole-Star figure the GUI shows, and
    silently rounding it up or down per call would make two reports of the
    same balance disagree.
    """
    return int(getattr(raw, "amount", 0) or 0)


class RevenueGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    ton: Annotated[bool, opt("--ton", help="TON (ad) revenue instead of Stars.")] = False
    since: Annotated[
        PeerRef | None,
        opt("--since", metavar="USER", kind="user", help="Stars earned from this user's messages."),
    ] = None
    dark: Annotated[bool, opt("--dark", help="Dark colour set in the graph specs.")] = False


async def get_revenue(ctx: OpContext, req: RevenueGetReq) -> RevenueSummary:
    """Stars or TON revenue, read-only by design."""
    from telethon.tl.functions import account as acct_fn
    from telethon.tl.functions import payments as fn

    peer = await _send.resolve(ctx, req.chat)
    handle = _admin.client(ctx)
    reply = await handle(
        fn.GetStarsRevenueStatsRequest(peer=peer, dark=req.dark or None, ton=req.ton or None)
    )
    status = getattr(reply, "status", None)
    next_at = getattr(status, "next_withdrawal_at", None)
    summary = RevenueSummary(
        chat_id=peer_id_of(peer) or 0,
        currency="ton" if req.ton else "stars",
        current_balance=_stars(getattr(status, "current_balance", None)),
        available_balance=_stars(getattr(status, "available_balance", None)),
        overall_revenue=_stars(getattr(status, "overall_revenue", None)),
        withdrawal_enabled=bool(getattr(status, "withdrawal_enabled", False)),
        next_withdrawal_at=_moment(next_at),
        usd_rate=float(getattr(reply, "usd_rate", 0.0) or 0.0),
        graphs=_collect_graphs(reply),
    )
    if summary.withdrawal_enabled:
        ctx.warn(
            "a withdrawal is available. tlgr does not implement "
            "payments.getStarsRevenueWithdrawalUrl: it moves money and needs your "
            "2FA password, so use an official client for that step"
        )
    if req.since is not None:
        user = await _send.resolve(ctx, req.since)
        paid = await handle(
            acct_fn.GetPaidMessagesRevenueRequest(user_id=_admin.input_user(user), parent_peer=peer)
        )
        summary.from_user_revenue = _stars(getattr(paid, "stars_amount", None))
    return summary


SPEC_REVENUE_GET = OperationSpec(
    id="chat.revenue.get",
    request=RevenueGetReq,
    response=RevenueSummary,
    impl=get_revenue,
    summary="Stars / TON revenue of a channel (and per-user paid-message revenue)",
    description=(
        "Read-only by design: when `withdrawal_enabled` is true the command "
        "says so and points at an official client, because "
        "`payments.getStarsRevenueWithdrawalUrl` moves money and wants the "
        "2FA password. Needs `can_view_revenue` / `can_view_stars_revenue`."
    ),
    columns=("chat_id", "currency", "available_balance", "overall_revenue"),
    example={
        "chat_id": -1001600,
        "currency": "stars",
        "current_balance": 120,
        "overall_revenue": 900,
    },
    example_args="chat revenue get @mychannel",
    covers=(
        "groups-channels-admin.paid-message-revenue",
        "groups-channels-admin.stars-revenue-stats",
    ),
)


class RevenueListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    ton: Annotated[bool, opt("--ton", help="TON transactions instead of Stars.")] = False
    inbound: Annotated[bool, opt("--in", help="Incoming only.")] = False
    outbound: Annotated[bool, opt("--out", help="Outgoing only.")] = False
    ascending: Annotated[bool, opt("--ascending", help="Oldest first.")] = False
    subscription: Annotated[
        str | None, opt("--subscription", metavar="ID", help="Only this subscription's rows.")
    ] = None


async def list_revenue(ctx: OpContext, req: RevenueListReq) -> Page[RevenueTransaction]:
    """The Stars or TON transaction history of a channel."""
    from telethon.tl.functions import payments as fn

    limit, state = _admin.window(ctx, "chat.revenue.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    reply = await _admin.client(ctx)(
        fn.GetStarsTransactionsRequest(
            peer=peer,
            offset=str(state.get("offset", "") or ""),
            limit=limit,
            inbound=req.inbound or None,
            outbound=req.outbound or None,
            ascending=req.ascending or None,
            ton=req.ton or None,
            subscription_id=req.subscription,
        )
    )
    rows: list[RevenueTransaction] = []
    for row in getattr(reply, "history", None) or []:
        date = getattr(row, "date", None)
        rows.append(
            RevenueTransaction(
                id=str(getattr(row, "id", "") or ""),
                date=fmt_dt(date),
                date_unix=to_unix(date),
                amount=_stars(getattr(row, "amount", None)),
                currency="ton" if req.ton else "stars",
                peer=type(getattr(row, "peer", None)).__name__,
                title=str(getattr(row, "title", "") or ""),
                refund=bool(getattr(row, "refund", False)),
                pending=bool(getattr(row, "pending", False)),
                failed=bool(getattr(row, "failed", False)),
                subscription_period=getattr(row, "subscription_period", None),
            )
        )
    return build_page(
        rows,
        op="chat.revenue.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": str(getattr(reply, "next_offset", "") or "")},
        account=ctx.account,
        limit=limit,
    )


SPEC_REVENUE_LIST = OperationSpec(
    id="chat.revenue.list",
    request=RevenueListReq,
    response=Page[RevenueTransaction],
    impl=list_revenue,
    summary="Stars / TON transaction history of a channel",
    description="The cursor is the opaque `next_offset` string the server hands back.",
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "date", "amount", "title"),
    example={"items": [{"id": "tx1", "amount": 50, "title": "Subscription"}], "has_more": False},
    example_args="chat revenue list @mychannel --in",
    covers=("groups-channels-admin.stars-transactions",),
)


# ---------------------------------------------------------------------------
# boost
# ---------------------------------------------------------------------------


class BoostGetReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", kind="peer", required=False, help="Chat to inspect."),
    ] = None
    features: Annotated[bool, opt("--features", help="Print what each boost level unlocks.")] = (
        False
    )
    level: Annotated[
        int | None, opt("--level", metavar="N", help="With --features: one level.")
    ] = None
    kind: Annotated[
        str | None, opt("--kind", metavar="CHANNEL|GROUP", help="With --features and no chat.")
    ] = None


async def get_boosts(ctx: OpContext, req: BoostGetReq) -> BoostStatus:
    """A chat's boost level and progress, and what the next level unlocks."""
    from telethon.tl.functions import help as help_fn
    from telethon.tl.functions import premium as fn

    handle = _admin.client(ctx)
    status = BoostStatus()
    if req.chat is not None:
        peer = await _send.resolve(ctx, req.chat)
        reply = await handle(fn.GetBoostsStatusRequest(peer=peer))
        audience = getattr(reply, "premium_audience", None)
        status = BoostStatus(
            chat_id=peer_id_of(peer) or 0,
            level=int(getattr(reply, "level", 0) or 0),
            boosts=int(getattr(reply, "boosts", 0) or 0),
            current_level_boosts=int(getattr(reply, "current_level_boosts", 0) or 0),
            next_level_boosts=getattr(reply, "next_level_boosts", None),
            premium_audience=(
                {
                    "part": float(getattr(audience, "part", 0.0) or 0.0),
                    "total": float(getattr(audience, "total", 0.0) or 0.0),
                }
                if audience is not None
                else None
            ),
            boost_url=str(getattr(reply, "boost_url", "") or ""),
            my_boost=bool(getattr(reply, "my_boost", False)),
            boosts_applied=len(getattr(reply, "my_boost_slots", None) or []) or None,
            prepaid_giveaways=[
                {
                    "id": int(getattr(item, "id", 0) or 0),
                    "quantity": int(getattr(item, "quantity", 0) or 0),
                    "months": int(getattr(item, "months", 0) or 0),
                }
                for item in (getattr(reply, "prepaid_giveaways", None) or [])
            ],
        )
    elif not req.features:
        raise UsageError("name a chat, or pass --features for the level table", field="chat")

    if req.features:
        config = await handle(help_fn.GetAppConfigRequest(hash=0))
        for item in getattr(getattr(config, "config", None), "value", None) or []:
            key = str(getattr(item, "key", "") or "")
            if key not in _BOOST_FEATURES:
                continue
            raw = getattr(getattr(item, "value", None), "value", None)
            try:
                needed = int(float(raw or 0))
            except (TypeError, ValueError):  # pragma: no cover - appConfig is numeric here
                continue
            if req.level is not None and needed != req.level:
                continue
            status.features.append({"key": key, "level": needed, "unlocks": _BOOST_FEATURES[key]})
        status.features.sort(key=lambda row: (int(row["level"]), str(row["key"])))
    return status


SPEC_BOOST_GET = OperationSpec(
    id="boost.get",
    request=BoostGetReq,
    response=BoostStatus,
    impl=get_boosts,
    summary="Boost status of a chat, or the boost-level feature table",
    description=(
        "`--features` maps the appConfig level keys to the tlgr flags they "
        "gate, so “why was `--autotranslate` refused” has an answer without "
        "reading the API docs. `boost_url` is the shareable boost link."
    ),
    aliases=("chat.boost.get",),
    columns=("chat_id", "level", "boosts", "next_level_boosts"),
    example={"chat_id": -1001600, "level": 3, "boosts": 12, "next_level_boosts": 15},
    example_args="boost get @mychannel",
    covers=(
        "giveaway.boost-status",
        "groups-channels-admin.boost-level-features",
        "groups-channels-admin.boost-link",
        "groups-channels-admin.boost-status",
    ),
)


class BoostListReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", kind="peer", required=False, help="Chat whose boosters to list."),
    ] = None
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Only the boosts this user applied."),
    ] = None
    gifts: Annotated[bool, opt("--gifts", help="Only gift and giveaway boosts.")] = False
    mine: Annotated[bool, opt("--mine", help="My own boost slots across every chat.")] = False


async def list_boosts(ctx: OpContext, req: BoostListReq) -> Page[Boost]:
    """Boosters of a chat, one user's boosts, or my own slots."""
    from telethon.tl.functions import premium as fn

    limit, state = _admin.window(ctx, "boost.list", PageKind.PARTICIPANTS)
    handle = _admin.client(ctx)

    if req.mine:
        reply = await handle(fn.GetMyBoostsRequest())
        rows = [
            Boost(
                slot=int(getattr(row, "slot", 0) or 0),
                chat_id=peer_id_of(getattr(row, "peer", None)) or None,
                date=fmt_dt(getattr(row, "date", None)),
                date_unix=to_unix(getattr(row, "date", None)),
                expires=fmt_dt(getattr(row, "expires", None)),
                cooldown_until_date=fmt_dt(getattr(row, "cooldown_until_date", None)),
            )
            for row in (getattr(reply, "my_boosts", None) or [])
        ]
        return build_page(
            rows,
            op="boost.list",
            kind=PageKind.PARTICIPANTS,
            account=ctx.account,
            has_more=False,
            total=len(rows),
        )

    if req.chat is None:
        raise UsageError("name a chat, or pass --mine for your own slots", field="chat")
    peer = await _send.resolve(ctx, req.chat)
    if req.user is not None:
        user = await _send.resolve(ctx, req.user)
        reply = await handle(fn.GetUserBoostsRequest(peer=peer, user_id=_admin.input_user(user)))
        raw_rows = getattr(reply, "boosts", None) or []
        next_state: dict[str, Any] = {}
        more = False
    else:
        reply = await handle(
            fn.GetBoostsListRequest(
                peer=peer,
                offset=str(state.get("offset", "") or ""),
                limit=limit,
                gifts=req.gifts or None,
            )
        )
        raw_rows = getattr(reply, "boosts", None) or []
        next_state = {"offset": str(getattr(reply, "next_offset", "") or "")}
        more = bool(next_state["offset"])

    rows = [
        Boost(
            id=str(getattr(row, "id", "") or ""),
            user_id=getattr(row, "user_id", None),
            chat_id=peer_id_of(peer) or None,
            gift=bool(getattr(row, "gift", False)),
            giveaway=bool(getattr(row, "giveaway", False)),
            unclaimed=bool(getattr(row, "unclaimed", False)),
            multiplier=getattr(row, "multiplier", None),
            stars=getattr(row, "stars", None),
            date=fmt_dt(getattr(row, "date", None)),
            date_unix=to_unix(getattr(row, "date", None)),
            expires=fmt_dt(getattr(row, "expires", None)),
        )
        for row in raw_rows
    ]
    return build_page(
        rows,
        op="boost.list",
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        has_more=more,
        total=int(getattr(reply, "count", len(rows)) or len(rows)),
    )


SPEC_BOOST_LIST = OperationSpec(
    id="boost.list",
    request=BoostListReq,
    response=Page[Boost],
    impl=list_boosts,
    summary="List boosters of a chat, one user's boosts, or my own boost slots",
    description=(
        "The cursor is the opaque `next_offset` string. `--mine` reports "
        "each slot's `cooldown_until_date`, which is what `boost add` needs "
        "before moving a slot to another chat."
    ),
    aliases=("chat.boost.list",),
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "user_id", "gift", "expires"),
    example={"items": [{"id": "b1", "user_id": 4242, "expires": "2026-06-01T00:00:00Z"}]},
    example_args="boost list @mychannel",
    covers=(
        "giveaway.boosts-list",
        "giveaway.boosts-unrestrict",
        "giveaway.user-boosts",
        "groups-channels-admin.boost-list",
        "groups-channels-admin.boost-user",
    ),
    coverage_note=(
        "The boost surface a booster and an admin both read. Letting boosters "
        "bypass restrictions is `chat setting set` territory and shares this "
        "surface."
    ),
)


class BoostAddReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to boost.")]
    slots: Annotated[
        list[int], opt("--slots", metavar="ID", help="Slot ids to spend; default: the free ones.")
    ] = []


async def add_boost(ctx: OpContext, req: BoostAddReq) -> BoostApplied:
    """Spend Premium boost slots on a chat.

    With no `--slots` the free slots reported by `premium.getMyBoosts` are
    spent, which is what the GUI does. Moving a slot that is already on
    another chat is rate-limited by its `cooldown_until_date`, so the
    cooldown is reported rather than discovered as a flood wait.
    """
    from telethon.tl.functions import premium as fn

    handle = _admin.client(ctx)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = peer_id_of(peer) or 0

    slots = [int(s) for s in req.slots]
    cooldown: str | None = None
    if not slots:
        mine = await handle(fn.GetMyBoostsRequest())
        for row in getattr(mine, "my_boosts", None) or []:
            if getattr(row, "peer", None) is None:
                slots.append(int(getattr(row, "slot", 0) or 0))
            elif peer_id_of(row.peer) == chat_id:
                _admin.already(ctx)
        if not slots:
            free_cooldowns = [
                fmt_dt(getattr(row, "cooldown_until_date", None))
                for row in (getattr(mine, "my_boosts", None) or [])
                if getattr(row, "cooldown_until_date", None) is not None
            ]
            cooldown = next((value for value in free_cooldowns if value), None)
            raise NotFoundError(
                "no free boost slot; `boost list --mine` shows each slot and when "
                "it comes off cooldown"
            )

    try:
        reply = await handle(fn.ApplyBoostRequest(peer=peer, slots=slots or None))
    except Exception as exc:
        if "BOOST_NOT_MODIFIED" not in str(exc):
            raise
        _admin.already(ctx)
        return BoostApplied(chat_id=chat_id, already=True, slots=slots)
    ctx.emit("chat_boosted", {"chat_id": chat_id, "slots": slots})
    return BoostApplied(
        chat_id=chat_id,
        level=int(getattr(reply, "level", 0) or 0),
        boosts=int(getattr(reply, "boosts", 0) or 0),
        my_boost=bool(getattr(reply, "my_boost", True)),
        peer=str(req.chat.raw),
        slots=slots,
        cooldown_until_date=cooldown,
    )


SPEC_BOOST_ADD = OperationSpec(
    id="boost.add",
    request=BoostAddReq,
    response=BoostApplied,
    impl=add_boost,
    summary="Boost a channel or group with my Premium slots",
    description=(
        "Needs Telegram Premium (PREMIUM_ACCOUNT_REQUIRED exits 6). "
        "BOOST_NOT_MODIFIED reports `already: true` and exits 0; moving a "
        "slot during its cooldown raises a flood wait and exits 7 with the "
        "wait."
    ),
    aliases=("boost.apply", "chat.boost.apply", "premium.boost.apply"),
    mutating=True,
    columns=("chat_id", "level", "boosts"),
    example={"chat_id": -1001600, "level": 4, "boosts": 15, "my_boost": True},
    example_args="boost add @mychannel",
    covers=(
        "giveaway.boost-status",
        "groups-channels-admin.boost-apply",
        "premium.apply-boost",
    ),
    tags=frozenset({"visible-to-others"}),
)
