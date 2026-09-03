"""The `net` group: what the connection is actually doing.

Separate from `daemon status`, which is about the process, and from `config
server get`, which is about Telegram's settings. This is about *this socket*:
which data centre, which transport, through which proxy, how far the clock has
drifted, and how long a round trip takes.

The clock is the field worth explaining. MTProto stamps every request with a
`msg_id` derived from the local time, and the server rejects one outside a
window of a few minutes — silently, as far as the client is concerned. An
account whose host clock has drifted therefore stops working with no error
anybody can read, which is why `time_offset_seconds` is reported and warned
about rather than left for somebody to guess at.
"""

from __future__ import annotations

import contextlib
import time
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.net import (
    DcOption,
    NearestDc,
    NetStatus,
    NetUsage,
    PingResult,
    SyncCursors,
)
from tlgr.models.page import Page
from tlgr.ops._params import choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:
        raise UsageError("this operation needs a connected account")
    return client


def _sessions(ctx: OpContext) -> Any:
    daemon = getattr(ctx, "daemon", None)
    return getattr(daemon, "sessions", None)


def _spanned(ctx: OpContext) -> list[str]:
    alias = (ctx.account or "").strip()
    sessions = _sessions(ctx)
    known = list(getattr(sessions, "aliases", []) or [])
    if alias and alias != "all":
        return [alias]
    return known


def _session_client(ctx: OpContext, alias: str) -> Any:
    sessions = _sessions(ctx)
    session = sessions.get(alias) if sessions is not None else None
    return getattr(session, "client", None)


# ---------------------------------------------------------------------------
# net dc list / nearest
# ---------------------------------------------------------------------------


class DcListReq(Request):
    ipv6: Annotated[bool, opt("--ipv6", help="Only IPv6 endpoints.")] = False
    media_only: Annotated[bool, opt("--media-only", help="Only media endpoints.")] = False
    cdn: Annotated[bool, opt("--cdn", help="Only CDN endpoints.")] = False
    test: Annotated[
        bool, opt("--test", help="List the test data centres instead of production.")
    ] = False
    resolve: Annotated[
        bool,
        opt("--resolve", help="Fetch the config over DNS/HTTPS when every DC is unreachable."),
    ] = False


#: Telegram's published test data centres. Hard-coded because the point of
#: asking for them is that you cannot reach a production DC to be told.
_TEST_DCS: tuple[tuple[int, str, int], ...] = (
    (1, "149.154.175.10", 80),
    (2, "149.154.167.40", 80),
    (3, "149.154.175.117", 80),
)


async def dc_list(ctx: OpContext, req: DcListReq) -> Page[DcOption]:
    """Telegram's data centres and their endpoints.

    Refreshed by the `sync_dc_options` event: ignoring `updateDcOptions` is
    how a long-lived daemon ends up stranded on an address Telegram has
    retired.
    """
    if req.test:
        rows = [
            DcOption(id=dc_id, ip_address=address, port=port) for dc_id, address, port in _TEST_DCS
        ]
        return build_page(rows, op="net.dc.list", kind=PageKind.LOCAL, has_more=False)

    if req.resolve:
        raise _no_doh()

    from telethon.tl import functions

    from tlgr.ops.config import _dc_options

    client = _client(ctx)
    config = await client(functions.help.GetConfigRequest())
    current = int(getattr(getattr(client, "session", None), "dc_id", 0) or 0)
    rows = _dc_options(config)
    for row in rows:
        row.current = row.id == current
    if req.ipv6:
        rows = [row for row in rows if row.ipv6]
    if req.media_only:
        rows = [row for row in rows if row.media_only]
    if req.cdn:
        rows = [row for row in rows if row.cdn]
    if not rows:
        raise NotFoundError("no data centre matches that filter")
    return build_page(rows, op="net.dc.list", kind=PageKind.LOCAL, has_more=False, total=len(rows))


def _no_doh() -> Exception:
    """`--resolve` is honest about not existing yet.

    The DNS-over-HTTPS config fallback is real work — fetch the payload,
    verify its RSA signature, feed the dcOptions into the session — and
    Telethon has none of it. Shipping a `--resolve` that quietly did nothing
    would be worse than one that says so; a configured MTProxy is the working
    stand-in today.
    """
    from tlgr.core.errors import NotSupportedError

    return NotSupportedError(
        "--resolve needs the DNS/HTTPS config fallback, which Telethon does not "
        "implement (it only retries the hard-coded DC addresses). Configure an "
        "MTProxy instead: tlgr proxy add 'tg://proxy?...' --set"
    )


SPEC_DC_LIST = OperationSpec(
    id="net.dc.list",
    request=DcListReq,
    response=Page[DcOption],
    impl=dc_list,
    summary="List Telegram data centres and their endpoints",
    paginated=PageKind.LOCAL,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=30,
    columns=("id", "ip_address", "port", "ipv6", "media_only", "cdn", "current"),
    empty_exit=EXIT_EMPTY,
    example={
        "items": [{"id": 4, "ip_address": "149.154.167.91", "port": 443, "current": True}],
        "has_more": False,
    },
    example_args="net dc list --ipv6",
    covers=(
        "updates.config-dc-options",
        "updates.config-dns-fallback",
        "updates.net-ipv6",
        "updates.net-test-dc",
    ),
    tags=frozenset({"agent-safe"}),
)


class DcNearestReq(Request):
    pass


async def dc_nearest(ctx: OpContext, req: DcNearestReq) -> NearestDc:
    """Ask the server which data centre is nearest.

    Callable before authorization, and the cheapest probe there is — which is
    why `proxy test` and `net ping` both time this call rather than reaching
    into Telethon's keepalive, which exposes no round-trip time.
    """
    from telethon.tl import functions

    client = _client(ctx)
    result = await client(functions.help.GetNearestDcRequest())
    return NearestDc(
        country=str(getattr(result, "country", "") or ""),
        this_dc=int(getattr(result, "this_dc", 0) or 0),
        nearest_dc=int(getattr(result, "nearest_dc", 0) or 0),
        current_dc=int(getattr(getattr(client, "session", None), "dc_id", 0) or 0) or None,
    )


SPEC_DC_NEAREST = OperationSpec(
    id="net.dc.nearest",
    request=DcNearestReq,
    response=NearestDc,
    impl=dc_nearest,
    summary="Ask the server which data centre is nearest",
    aliases=("net.nearest-dc",),
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=30,
    columns=("country", "this_dc", "nearest_dc", "current_dc"),
    example={"country": "GB", "this_dc": 4, "nearest_dc": 4, "current_dc": 4},
    example_args="net dc nearest",
    covers=("updates.config-nearest-dc",),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# net ping
# ---------------------------------------------------------------------------


async def _time_call(client: Any, via: str) -> float | None:
    """One probe, in milliseconds, or None when it failed."""
    from telethon.tl import functions

    request = (
        functions.updates.GetStateRequest()
        if via == "get-state"
        else functions.help.GetNearestDcRequest()
    )
    started = time.monotonic()
    try:
        await client(request)
    except Exception:
        return None
    return round((time.monotonic() - started) * 1000, 2)


class PingReq(Request):
    probes: Annotated[int, opt("--probes", metavar="N", ge=1, le=20)] = 3
    via: Annotated[str, choice("nearest-dc", "get-state", help="Which RPC to time.")] = "nearest-dc"


async def net_ping(ctx: OpContext, req: PingReq) -> PingResult:
    """Round-trip latency to the current data centre.

    A cheap RPC is timed rather than the transport's own keepalive, because
    Telethon exposes no round-trip accessor: `MTProtoSender._keepalive_ping`
    is private and records nothing a caller can read.
    """
    client = _client(ctx)
    samples: list[float] = []
    failures = 0
    for _ in range(req.probes):
        sample = await _time_call(client, req.via)
        if sample is None:
            failures += 1
        else:
            samples.append(sample)

    result = PingResult(
        account=ctx.account,
        dc_id=int(getattr(getattr(client, "session", None), "dc_id", 0) or 0) or None,
        probes=req.probes,
        loss=round(failures / req.probes, 3) if req.probes else 0.0,
    )
    if samples:
        result.min_ms = min(samples)
        result.max_ms = max(samples)
        result.avg_ms = round(sum(samples) / len(samples), 2)
    else:
        ctx.warn("every probe failed; the account may be disconnected")
    return result


SPEC_NET_PING = OperationSpec(
    id="net.ping",
    request=PingReq,
    response=PingResult,
    impl=net_ping,
    summary="Measure round-trip latency to the current data centre",
    aliases=("daemon.net.ping",),
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=60,
    columns=("account", "dc_id", "probes", "min_ms", "avg_ms", "max_ms", "loss"),
    example={"account": "work", "dc_id": 4, "probes": 3, "avg_ms": 41.2, "loss": 0.0},
    example_args="net ping --probes 5",
    covers=("updates.net-ping-latency",),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# net status
# ---------------------------------------------------------------------------

#: Beyond this, `msg_id`s start falling outside the server's acceptance window
#: and requests are dropped with nothing the client can report.
_CLOCK_WARN_SECONDS = 30


class NetStatusReq(Request):
    ping: Annotated[bool, opt("--ping/--no-ping", help="Measure latency as part of it.")] = True


async def net_status(ctx: OpContext, req: NetStatusReq) -> NetStatus:
    """The connection, in one object.

    `phase` is derived rather than read: Telethon has no phase enum, only
    `is_connected()` and a `disconnected` future, so "catching up" has to be
    inferred from the session state the supervisor keeps.
    """
    from tlgr.core import telethon_compat as compat

    alias = ctx.account or (_spanned(ctx) or [""])[0]
    client = _session_client(ctx, alias) or getattr(ctx, "client", None)
    if client is None:
        raise NotFoundError(f"account {alias!r} is not connected. Run: tlgr daemon status")

    sessions = _sessions(ctx)
    session = sessions.get(alias) if sessions is not None else None
    tl_session = getattr(client, "session", None)
    connected = bool(client.is_connected()) if hasattr(client, "is_connected") else False

    state, channels = compat.session_state(client)
    cursors = SyncCursors(
        pts=state.get("pts"),
        qts=state.get("qts"),
        seq=state.get("seq"),
        date=state.get("date"),
        date_unix=state.get("date_unix"),
    )

    report = NetStatus(
        account=alias,
        authorized=True,
        connected=connected,
        phase=_phase(session, connected),
        dc_id=int(getattr(tl_session, "dc_id", 0) or 0) or None,
        dc_address=str(getattr(tl_session, "server_address", "") or "") or None,
        ipv6=bool(getattr(client, "_use_ipv6", False)),
        transport=type(getattr(client, "_connection", None)).__name__,
        proxy=_proxy_label(client),
        layer=_layer(),
        time_offset_seconds=_time_offset(client),
        exported_senders=len(getattr(client, "_borrowed_senders", None) or {}),
        reconnects=int(getattr(session, "reconnects", 0) or 0),
        last_error=(getattr(session, "reason", "") or None) if session is not None else None,
        state=cursors,
        frozen=getattr(session, "state", "") == "frozen",
    )
    if channels:
        ctx.warn(f"{len(channels)} channels have their own pts; see `tlgr sync status --channels`")

    if cursors.date_unix:
        report.behind_seconds = max(0, int(time.time()) - int(cursors.date_unix))

    if abs(report.time_offset_seconds) >= _CLOCK_WARN_SECONDS:
        ctx.warn(
            f"this host's clock is {report.time_offset_seconds}s from the server's; "
            "MTProto message ids fall outside the acceptance window and requests "
            "are dropped with no error. Fix the clock."
        )
    if req.ping and connected:
        sample = await _time_call(client, "nearest-dc")
        report.ping_ms = sample
    return report


def _phase(session: Any, connected: bool) -> str:
    if session is None:
        return "connected" if connected else "disconnected"
    if getattr(session, "catch_up_pending", False):
        return "catching_up"
    state = str(getattr(session, "state", "") or "")
    return state or ("connected" if connected else "disconnected")


def _layer() -> int:
    with contextlib.suppress(Exception):
        from telethon.tl.alltlobjects import LAYER

        return int(LAYER)
    return 0


def _time_offset(client: Any) -> int:
    sender = getattr(client, "_sender", None)
    state = getattr(sender, "state", None)
    return int(getattr(state, "time_offset", 0) or 0)


def _proxy_label(client: Any) -> str | None:
    """The proxy in force, never its credentials."""
    proxy = getattr(client, "_proxy", None)
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return f"{proxy.get('proxy_type', 'proxy')}://{proxy.get('addr')}:{proxy.get('port')}"
    with contextlib.suppress(Exception):
        return f"{proxy[0]}:{proxy[1]}"
    return "configured"


SPEC_NET_STATUS = OperationSpec(
    id="net.status",
    request=NetStatusReq,
    response=NetStatus,
    impl=net_status,
    summary="Show the connection: DC, transport, proxy, latency, layer, clock offset",
    description=(
        "A clock more than 30 seconds from the server's is reported as a "
        "warning, because MTProto derives `msg_id` from local time and the "
        "server drops anything outside its window — with no error the client "
        "can see."
    ),
    aliases=("daemon.net.status",),
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=60,
    columns=("account", "connected", "phase", "dc_id", "ping_ms", "layer", "behind_seconds"),
    example={
        "account": "work",
        "connected": True,
        "phase": "online",
        "dc_id": 4,
        "layer": 227,
    },
    example_args="net status",
    covers=(
        "updates.net-connection-status",
        "updates.session-export-auth-dc",
        "updates.session-time-sync",
        "updates.sync-updating-indicator",
    ),
    covers_partial=("updates.net-migrate-errors", "updates.net-ping-latency"),
    coverage_note=(
        "reports the connection; a migration that escapes Telethon is named "
        "by `agent exit-codes`, and repeated probes are `net ping`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# net usage
# ---------------------------------------------------------------------------


class NetUsageReq(Request):
    reset: Annotated[bool, opt("--reset", help="Zero the counters after reporting.")] = False


async def net_usage_get(ctx: OpContext, req: NetUsageReq) -> NetUsage:
    """Bytes and requests since the daemon started.

    Partial parity, said plainly: official clients break usage down per
    method, and Telethon does no byte accounting at all, so these are coarse
    per-class counters tlgr keeps itself. They live in memory and reset with
    the daemon.
    """
    daemon = getattr(ctx, "daemon", None)
    sessions = _sessions(ctx)
    alias = ctx.account or "all"
    counters = getattr(daemon, "usage", None) or {}
    row = counters.get(alias, {}) if isinstance(counters, dict) else {}

    report = NetUsage(
        account=alias,
        since=_started_at(daemon),
        rpc_bytes_sent=int(row.get("rpc_bytes_sent", 0)),
        rpc_bytes_received=int(row.get("rpc_bytes_received", 0)),
        download_bytes=int(row.get("download_bytes", 0)),
        upload_bytes=int(row.get("upload_bytes", 0)),
        requests=int(row.get("requests", 0)),
        updates_received=_updates_seen(daemon, ctx),
        reconnects=sum(
            int(getattr(sessions.get(name), "reconnects", 0) or 0)
            for name in (_spanned(ctx) or [])
            if sessions is not None and sessions.get(name) is not None
        ),
    )
    if not row:
        ctx.warn(
            "byte counters are not instrumented in this build; requests, updates "
            "and reconnects are exact and the byte totals are zero"
        )
    if req.reset and isinstance(counters, dict):
        counters.pop(alias, None)
    return report


def _started_at(daemon: Any) -> str:
    from tlgr.core.timefmt import fmt_unix

    started = getattr(daemon, "_start_time", None)
    return (fmt_unix(int(started)) or "") if started else ""


def _updates_seen(daemon: Any, ctx: OpContext) -> int:
    bus = getattr(daemon, "bus", None)
    if bus is None:
        return 0
    return sum(int(bus.latest_seq(alias)) for alias in (_spanned(ctx) or []))


SPEC_NET_USAGE = OperationSpec(
    id="net.usage.get",
    request=NetUsageReq,
    response=NetUsage,
    impl=net_usage_get,
    summary="Report bytes sent/received and requests per account",
    description=(
        "Coarse per-class counters, not the official clients' per-method "
        "breakdown: Telethon does no byte accounting, so anything finer would "
        "be invented. In memory, and reset with the daemon."
    ),
    aliases=("daemon.net.usage",),
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("account", "requests", "updates_received", "reconnects"),
    example={"account": "all", "requests": 128, "updates_received": 91824, "reconnects": 1},
    example_args="net usage get",
    covers=("updates.ops-network-usage-stats",),
    tags=frozenset({"agent-safe"}),
)
