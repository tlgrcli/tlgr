"""The daemon object and its aiohttp application (§5, §6.1).

Everything that used to be spread across `server.py` and `ipc.py` — deciding
who may connect, which account answers, what an exception means, whether the
daemon may stop — happens in one middleware chain here. That matters most for
the routes this file does **not** define: the v1 routes in `ipc.py` are
registered into the same application, so peer-uid authentication, the policy
allowlist, the §7.2 error mapping and idle accounting apply to every
unmigrated command from day one rather than at its own group's PR (§12.4).

Readiness is published in two steps, deliberately. The socket is bound and
`ready: false` is served *before* any account connects (ROB-07), so a CLI can
tell "the process is alive" from "the daemon works" (COR-37) instead of
waiting on a connect that may take thirty seconds or never finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from tlgr import __version__
from tlgr.core.accounts import AccountManager
from tlgr.core.config import AppConfig, load_app_config, load_webhook_config
from tlgr.core.errors import (
    DaemonVersionMismatchError,
    PermissionError_,
    RetryableError,
    UsageError,
    classify,
    error_body_dict,
    http_status_for,
)
from tlgr.core.paths import TlgrPaths, write_private
from tlgr.daemon import dispatch as dispatch_module
from tlgr.daemon.events import EventBus
from tlgr.daemon.idle import ActivityTracker, effective_idle_timeout
from tlgr.daemon.jobs import JobRunner
from tlgr.daemon.peercred import current_uid, peer_of, token_matches
from tlgr.daemon.policy import Policy
from tlgr.daemon.preauth import PreAuthService
from tlgr.daemon.sessions import SessionManager
from tlgr.daemon.stream import NdjsonResponse, pump_events, walk_pages
from tlgr.daemon.webhook import WebhookPusher
from tlgr.version import HEADER_PROTOCOL, HEADER_TOKEN, MIN_DAEMON_PROTOCOL, PROTOCOL

log = logging.getLogger("tlgr.daemon")

__all__ = ["Daemon", "build_app"]

_V1_PREFIX = "/v1/"


def _stamp(value: float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Daemon:
    """The process's state: sessions, bus, webhook, jobs, activity, policy."""

    def __init__(
        self,
        base: Path | None = None,
        *,
        config: AppConfig | None = None,
        client_factory: Any = None,
        managed_by: str = "",
    ) -> None:
        self.paths = TlgrPaths(base)
        self.base = self.paths.base
        self.config = config or load_app_config(self.paths.base)
        self.accounts = AccountManager(self.paths.base)
        self.policy = Policy.from_config(self.config.policy.allow, self.config.policy.deny)
        self.activity = ActivityTracker()
        self.bus = EventBus(
            state_dir_for=self.paths.events_state,
            buffer_size=self.config.daemon.event_buffer,
            workers=self.config.daemon.event_workers,
        )
        factory_kwargs = {"client_factory": client_factory} if client_factory else {}
        self.sessions = SessionManager(
            self.paths,
            self.config,
            accounts=self.accounts,
            on_session_ready=self._attach_handlers,
            **factory_kwargs,
        )
        self.webhook_config = load_webhook_config(self.paths.base)
        self.webhook = WebhookPusher(self.webhook_config, self.paths.base)
        self._job_runner = JobRunner()
        # Login runs in the daemon because the daemon owns the session files
        # (§6.8); a pending login holds one, so it counts as activity and the
        # idle monitor cannot stop the daemon out from under a half-finished
        # sign-in.
        self.preauth = PreAuthService(self.sessions)
        self.managed_by = managed_by
        self.ready = False
        self.shutting_down = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._start_time = time.time()
        self._runner: web.AppRunner | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._token: str | None = None
        self.idle_timeout = effective_idle_timeout(
            self.config.daemon.idle_timeout,
            webhook_enabled=self.webhook_config.enabled,
            managed_by=managed_by,
        )
        self.activity.webhook_enabled = self.webhook_config.enabled

    # -- authentication ----------------------------------------------------

    @property
    def token(self) -> str | None:
        if self._token is None:
            try:
                self._token = self.paths.token.read_text().strip() or ""
            except OSError:
                self._token = ""
        return self._token or None

    # -- session plumbing --------------------------------------------------

    async def _attach_handlers(self, session: Any) -> None:
        """Feed a newly connected account's updates into the bus.

        The handler body is deliberately tiny: normalise, number, fan out. Any
        real work happens on a bus worker lane, because with
        `sequential_updates=True` a slow handler stalls every account (ROB-02).
        """
        client = session.client
        if client is None:
            return
        register = getattr(client, "add_event_handler", None)
        if register is None:
            return
        from tlgr.daemon.events import normalise

        alias = session.alias

        async def on_update(event: Any) -> None:
            session.note_update()
            normalised = normalise(alias, event)
            if normalised is None:
                return
            event_type, payload, chat_id, sender_id = normalised
            self.bus.emit(
                alias,
                event_type,
                payload,
                chat_id=chat_id,
                sender_id=sender_id,
                raw=event,
            )

        try:
            from telethon import events as tl_events

            for builder in (
                tl_events.NewMessage(),
                tl_events.MessageEdited(),
                tl_events.MessageDeleted(),
                tl_events.MessageRead(),
                tl_events.ChatAction(),
                tl_events.UserUpdate(),
            ):
                register(on_update, builder)
        except Exception as exc:  # pragma: no cover - a fake client has no builders
            log.debug("could not register Telethon handlers for %s: %s", alias, exc)
            register(on_update)

    # -- v1 compatibility surface -----------------------------------------

    @property
    def _clients(self) -> dict[str, Any]:
        """alias → `ClientWrapper`, for the v1 handlers still in `ipc.py`."""
        return {
            alias: session.wrapper
            for alias, session in ((a, self.sessions.get(a)) for a in self.sessions.aliases)
            if session is not None and session.client is not None
        }

    def get_client(self, account: str = "") -> Any:
        if not account:
            return None
        session = self.sessions.get(account)
        return session.wrapper if session and session.client is not None else None

    async def ensure_client(self, account: str = "") -> Any:
        """v1's on-demand connect, now going through the SessionManager.

        Returning `None` for an empty account is the point: v1 answered with
        "whichever client came first", so an under-specified request silently
        used the wrong identity (COR-02).
        """
        if not account:
            return None
        try:
            session = await self.sessions.ensure(account)
        except Exception as exc:
            log.warning(
                "on-demand connect failed for %s: %s", account, exc, extra={"account": account}
            )
            return None
        with contextlib.suppress(Exception):
            await session.acquire(timeout=15.0)
        return session.wrapper if session.client is not None else None

    def touch_ipc(self) -> None:
        self.activity.touch()

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._job_runner.list_jobs()

    async def remove_job(self, name: str) -> bool:
        return await self._job_runner.remove_job(name)

    async def enable_job(self, name: str) -> bool:
        return await self._job_runner.enable_job(name)

    async def disable_job(self, name: str) -> bool:
        return await self._job_runner.disable_job(name)

    async def reload_jobs(self) -> dict[str, Any]:
        from tlgr.gateway.config import load_gateway_configs

        new_configs = await asyncio.to_thread(load_gateway_configs, self.base)
        default_account = self.config.default_account or self.accounts.get_active() or ""
        old_names = set(self._job_runner._jobs)
        new_names = {jc.name for jc in new_configs}
        removed = old_names - new_names
        added = new_names - old_names
        updated = old_names & new_names

        for name in removed:
            await self._job_runner.remove_job(name)
        for job_config in new_configs:
            if job_config.name not in added and job_config.name not in updated:
                continue
            if job_config.name in updated:
                await self._job_runner.remove_job(job_config.name)
            if not job_config.enabled:
                continue
            alias = job_config.account or default_account
            client = await self.ensure_client(alias)
            if client is None:
                log.warning("job %r references unusable account %r", job_config.name, alias)
                continue
            try:
                job = self._job_runner.create_job(job_config, client, self.webhook, self.bus)
                if job.enabled:
                    job.start()
            except Exception:
                log.exception("could not create job %r", job_config.name)
        self.activity.jobs_running = len(
            [j for j in self._job_runner.list_jobs() if j.get("running")]
        )
        return {
            "reloaded": True,
            "added": sorted(added),
            "removed": sorted(removed),
            "updated": sorted(updated),
        }

    def status(self) -> dict[str, Any]:
        """v1's `/daemon/status` body, unchanged (it is a documented shape).

        `connections` and `healthy` exist because the wrapper existing and the
        wrapper being usable are different facts, and v1 reported only the
        first — a fully dead daemon looked healthy.
        """
        uptime = int(time.time() - self._start_time)
        connections = {alias: client.is_connected for alias, client in self._clients.items()}
        disconnected = sorted(alias for alias, ok in connections.items() if not ok)
        return {
            "running": True,
            "pid": os.getpid(),
            "uptime_seconds": uptime,
            "accounts": list(self._clients),
            "connections": connections,
            "disconnected": disconnected,
            "healthy": not disconnected,
            "jobs": self._job_runner.list_jobs(),
        }

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    # -- the v2 status -----------------------------------------------------

    def v1_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "daemon": {
                "version": __version__,
                "protocol": PROTOCOL,
                "pid": os.getpid(),
                "uptime_s": int(time.time() - self._start_time),
                "ready": self.ready,
                "started_at": _stamp(self._start_time),
                "managed_by": self.managed_by or None,
                "idle_timeout_s": self.idle_timeout,
                "socket": str(self.paths.socket),
                "shutting_down": self.shutting_down.is_set(),
            },
            "accounts": self.sessions.snapshot(),
            "jobs": self._job_runner.list_jobs(),
            "webhook": self.webhook.snapshot(),
            "activity": {
                **{**self.activity.snapshot(), "pending_logins": self.preauth.pending_count},
                "last_request": _stamp(self.activity.last_request_at),
            },
        }

    # -- lifecycle ---------------------------------------------------------

    def write_state(self) -> None:
        write_private(
            self.paths.state,
            json.dumps(
                {
                    "version": __version__,
                    "protocol": PROTOCOL,
                    "pid": os.getpid(),
                    "socket": str(self.paths.socket),
                    "managed_by": self.managed_by,
                    "started_at": _stamp(self._start_time),
                },
                indent=2,
            ),
        )

    async def bind(self) -> web.AppRunner:
        """Bind the socket and start serving with `ready: false` (ROB-07)."""
        app = build_app(self)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.UnixSite(runner, str(self.paths.socket))
        await site.start()
        os.chmod(self.paths.socket, 0o600)
        self._runner = runner
        self.write_state()
        log.info(
            "daemon listening on %s", self.paths.socket, extra={"path": str(self.paths.socket)}
        )
        return runner

    async def start_services(self) -> None:
        await self.bus.start()
        await self.webhook.start()
        if self.webhook_config.enabled:
            self.bus.add_handler(self.webhook.on_event)

    async def connect_accounts(self) -> dict[str, str]:
        aliases = self.accounts.connect_order(
            self.config.default_account, list(self.config.daemon.preconnect)
        )
        if not aliases:
            return {}
        return await self.sessions.connect_all(aliases)

    async def run(self) -> None:
        """Bind, connect, publish ready, and wait for a shutdown signal."""
        await self.start_services()
        await self.bind()
        results = await self.connect_accounts()
        self.ready = True
        log.info(
            "daemon ready with %d account(s)",
            len(results),
            extra={"count": len(results)},
        )
        self._idle_task = asyncio.create_task(self._idle_monitor(), name="tlgr-idle")
        await self._shutdown_event.wait()
        await self.shutdown()

    async def _idle_monitor(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(5)
            if self.idle_timeout <= 0:
                continue
            self.activity.pending_logins = self.preauth.pending_count
            if self.activity.may_stop(self.idle_timeout):
                log.info(
                    "idle for %ds with nothing in flight — stopping",
                    int(self.activity.idle_seconds()),
                )
                self.request_shutdown()
                return

    async def shutdown(self, *, drain: float | None = None) -> None:
        """The ordered teardown of §6.11."""
        if self.shutting_down.is_set():
            return
        self.shutting_down.set()
        self.ready = False
        deadline = time.monotonic() + (
            drain if drain is not None else self.config.daemon.drain_seconds
        )

        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task

        # Wait for in-flight requests rather than cancelling them: a ten
        # minute scan that is killed at second 599 has cost the account the
        # requests it already made and produced nothing (COR-11).
        while self.activity.in_flight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        with contextlib.suppress(Exception):
            await self._job_runner.stop_all()
        with contextlib.suppress(Exception):
            await self.webhook.stop()
        await self.bus.stop()
        await self.sessions.stop_all()
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        for path in (self.paths.socket, self.paths.state):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        log.info("daemon stopped")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def _is_v1(request: web.Request) -> bool:
    return request.path.startswith(_V1_PREFIX)


def _error_response(request: web.Request, exc: BaseException, *, op: str = "") -> web.Response:
    """One error shape per surface, from one classification (§7.1, COR-06).

    v1 collapsed every failure to `IPC_ERROR`/exit 12 on the way out of the
    daemon, so a flood wait, a missing chat and a permission error were
    indistinguishable to the caller. The body differs between the two surfaces
    only in its wrapper: the v1 routes keep the flat shape their callers
    parse, and both carry the same classified code and exit status.
    """
    body = error_body_dict(classify(exc))
    status = http_status_for(exc)
    if _is_v1(request):
        payload: dict[str, Any] = {"ok": False, "error": body}
        if op:
            payload["op"] = op
    else:
        payload = dict(body)
    return web.Response(
        body=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        content_type="application/json",
        status=status,
    )


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    daemon: Daemon = request.app[DAEMON_KEY]
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except asyncio.CancelledError:
        # The client hung up. Not an error, and not something to answer.
        raise
    except Exception as exc:
        log.info(
            "request failed: %s",
            exc,
            extra={"path": request.path, "code": classify(exc).code},
        )
        return _error_response(request, exc)
    finally:
        daemon.touch_ipc()


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Peer uid, with a shared token as the fallback (SEC-01, §8.2)."""
    daemon: Daemon = request.app[DAEMON_KEY]
    security = daemon.config.security
    supplied = request.headers.get(HEADER_TOKEN)

    peer = None
    if security.peer_uid_check:
        transport = request.transport
        sock = transport.get_extra_info("socket") if transport is not None else None
        peer = peer_of(sock)

    if peer is not None and peer.uid != current_uid():
        log.warning(
            "refused a connection from uid %s (pid %s)",
            peer.uid,
            peer.pid,
            extra={"uid": peer.uid, "pid": peer.pid, "path": request.path},
        )
        return _error_response(
            request, PermissionError_("this socket only accepts connections from its own user")
        )

    needs_token = security.require_token or (peer is None and security.peer_uid_check)
    if needs_token and not token_matches(supplied, daemon.token):
        log.warning(
            "refused a connection with a missing or wrong token", extra={"path": request.path}
        )
        return _error_response(
            request,
            PermissionError_(
                "this daemon requires X-Tlgr-Token; the token is in ~/.tlgr/ipc.token"
            ),
        )
    return await handler(request)


@web.middleware
async def version_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Refuse a client we cannot understand; never guess (§5.7)."""
    raw = request.headers.get(HEADER_PROTOCOL)
    if raw and _is_v1(request) and request.path != "/v1/status":
        try:
            client_protocol = int(raw)
        except ValueError:
            return _error_response(request, UsageError(f"invalid {HEADER_PROTOCOL}: {raw!r}"))
        if client_protocol < MIN_DAEMON_PROTOCOL:
            return _error_response(
                request,
                DaemonVersionMismatchError(
                    f"this daemon speaks protocol {PROTOCOL}; the client speaks "
                    f"{client_protocol}. Restart the daemon: tlgr daemon restart"
                ),
            )
    return await handler(request)


async def _requested_flood_budget(request: web.Request) -> tuple[str, int | None]:
    """`(account, flood_wait_max)` from a legacy request's body or query.

    Reading the body here is safe: aiohttp caches it, so the handler's own
    `request.json()` sees the same bytes.
    """
    account = request.query.get("account", "")
    raw = request.query.get("flood_wait_max")
    if request.method in ("POST", "PUT", "PATCH"):
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                account = str(body.get("account", account) or account)
                raw = body.get("flood_wait_max", raw)
    if raw in (None, ""):
        return account, None
    with contextlib.suppress(TypeError, ValueError):
        return account, int(raw)
    return account, None


@web.middleware
async def flood_budget_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Honour `--flood-wait-max` on the v1 routes as well (COR-15).

    The v2 dispatcher applies the budget itself, per operation. The forty
    hand-written v1 handlers do not thread the value through, so it is applied
    here instead — which is why the flag now means something for every command
    rather than for none.
    """
    if _is_v1(request):
        return await handler(request)
    daemon: Daemon = request.app[DAEMON_KEY]
    account, budget = await _requested_flood_budget(request)
    session = daemon.sessions.get(account) if account and budget is not None else None
    if session is None:
        return await handler(request)
    with session.flood_budget(budget):
        return await handler(request)


@web.middleware
async def activity_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Count the request, and refuse new work while shutting down (§6.11)."""
    daemon: Daemon = request.app[DAEMON_KEY]
    if daemon.shutting_down.is_set() and request.path != "/v1/status":
        return _error_response(
            request, RetryableError("the daemon is shutting down; retry in a moment")
        )
    daemon.activity.begin_request()
    try:
        return await handler(request)
    finally:
        daemon.activity.end_request()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def handle_op(request: web.Request) -> web.StreamResponse:
    daemon: Daemon = request.app[DAEMON_KEY]
    raw = await request.read()
    op_request = dispatch_module.decode_request(raw)
    if op_request.stream or op_request.all:
        return await _handle_op_stream(request, daemon, op_request)
    envelope = await dispatch_module.dispatch(daemon, op_request)
    return web.Response(
        body=json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8"),
        content_type="application/json",
    )


async def _handle_op_stream(
    request: web.Request, daemon: Daemon, op_request: Any
) -> web.StreamResponse:
    from tlgr.daemon.dispatch import resolve_spec

    stream = NdjsonResponse(request)
    try:
        spec = resolve_spec(op_request.op)
    except Exception as exc:
        return _error_response(request, exc, op=op_request.op)
    await stream.prepare(op=spec.id, account=op_request.account, request_id=op_request.request_id)
    started = time.monotonic()
    try:
        # `spec` is the same object `resolve_spec` returned above; the
        # execute() call is what runs the policy/account/timeout prologue.
        _, context, result = await dispatch_module.execute(daemon, op_request)
        if hasattr(result, "__aiter__"):
            # A `--all` walk paces itself against the account's own limiter,
            # inside the daemon: v1 looped in the client and hammered the
            # socket with no backpressure between pages (ROB-01).
            count = await walk_pages(
                result, stream, limiter=context.limiter, rate_class=spec.rate_class
            )
        else:
            count = await _stream_result(stream, result)
    except Exception as exc:
        return await stream.fail(exc, account=op_request.account)
    return await stream.end(
        ok=True, count=count, elapsed_ms=int((time.monotonic() - started) * 1000)
    )


async def _stream_result(stream: NdjsonResponse, result: Any) -> int:
    """Emit a non-streaming result as items, so the framing is the same."""
    from tlgr.models.base import to_builtins

    body = to_builtins(result) if result is not None else None
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        items = body["items"]
        page = {
            "has_more": bool(body.get("has_more")),
            "next_cursor": body.get("next_cursor"),
        }
    else:
        items = body if isinstance(body, list) else [body]
        page = None
    for index, item in enumerate(items, start=1):
        await stream.write({"type": "item", "seq": index, "data": item})
    if page is not None:
        await stream.write({"type": "page", **page, "fetched": len(items)})
    return len(items)


async def handle_events(request: web.Request) -> web.StreamResponse:
    daemon: Daemon = request.app[DAEMON_KEY]
    account = request.query.get("account", "").strip()
    if not account:
        return _error_response(
            request, UsageError("GET /v1/events needs ?account=<alias>", field="account")
        )
    types = [t for t in request.query.get("types", "").split(",") if t]
    chats = [
        int(c) for c in request.query.get("chats", "").split(",") if c.strip().lstrip("-").isdigit()
    ]
    since_raw = request.query.get("since")
    since = int(since_raw) if since_raw and since_raw.lstrip("-").isdigit() else None
    timeout = min(int(request.query.get("timeout", 3600) or 3600), 86400)

    subscriber = daemon.bus.subscribe(account, types=types, chats=chats)
    stream = NdjsonResponse(request)
    daemon.activity.begin_stream()
    try:
        await stream.prepare(account=account, seq=daemon.bus.latest_seq(account))
        replayed, gap = daemon.bus.replay(account, since)
        if gap is not None:
            await stream.write(gap)
        from tlgr.models.base import to_builtins

        for event in replayed:
            await stream.write(to_builtins(event))
        reason = await pump_events(
            stream,
            subscriber,
            timeout=timeout,
            shutdown=daemon.shutting_down,
        )
        return await stream.end(ok=True, reason=reason)
    except (ConnectionResetError, asyncio.CancelledError):
        return await stream.end(ok=True, reason="client-disconnected")
    except Exception as exc:
        return await stream.fail(exc, account=account)
    finally:
        daemon.bus.unsubscribe(subscriber)
        daemon.activity.end_stream()


async def handle_status(request: web.Request) -> web.Response:
    daemon: Daemon = request.app[DAEMON_KEY]
    return web.Response(
        body=json.dumps(daemon.v1_status(), ensure_ascii=False, default=str).encode("utf-8"),
        content_type="application/json",
    )


_ADMIN_OPS = {
    "stop": "daemon.stop",
    "reload": "daemon.reload",
    "resync": "daemon.resync",
    "logout": "account.logout",
    "unfreeze": "account.unfreeze",
}


async def handle_admin(request: web.Request) -> web.Response:
    daemon: Daemon = request.app[DAEMON_KEY]
    action = request.match_info["action"]
    if action not in _ADMIN_OPS:
        return _error_response(request, UsageError(f"unknown admin action {action!r}"))
    daemon.policy.enforce(_ADMIN_OPS[action], None)

    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    result: dict[str, Any]
    if action == "stop":
        drain = float(body.get("drain_s", daemon.config.daemon.drain_seconds))
        # Answer first, then stop: a caller that never hears "stopping" cannot
        # tell a graceful shutdown from a crash.
        asyncio.get_running_loop().call_later(0.01, daemon.request_shutdown)
        result = {"stopping": True, "drain_s": drain}
    elif action == "reload":
        result = await _reload(daemon, body.get("what") or ["config", "jobs", "policy"])
    elif action == "resync":
        result = await _resync(daemon, body)
    elif action == "unfreeze":
        alias = str(body.get("account", "")).strip()
        daemon.sessions.limiter(alias).reset_breaker()
        result = {"account": alias, "circuit": "closed"}
    else:  # logout
        result = await _logout(daemon, body)
    return web.Response(
        body=json.dumps({"ok": True, **result}, ensure_ascii=False, default=str).encode("utf-8"),
        content_type="application/json",
    )


async def _reload(daemon: Daemon, what: list[str]) -> dict[str, Any]:
    """Re-read from disk and swap, atomically (§6.9).

    Parsing into new structs before swapping is the whole trick: a config with
    a typo leaves the running one in force and reports `CONFIG_ERROR`, rather
    than half-applying and leaving the daemon in a state no file describes.
    """
    changed: list[str] = []
    requires_restart: list[str] = []
    if "config" in what:
        fresh = await asyncio.to_thread(load_app_config, daemon.base)
        if fresh.network != daemon.config.network:
            requires_restart.append("network")
        if fresh.identity != daemon.config.identity:
            requires_restart.append("identity")
        daemon.config = fresh
        daemon.policy = Policy.from_config(fresh.policy.allow, fresh.policy.deny)
        daemon.idle_timeout = effective_idle_timeout(
            fresh.daemon.idle_timeout,
            webhook_enabled=daemon.webhook_config.enabled,
            managed_by=daemon.managed_by,
        )
        changed.append("config")
    if "policy" in what and "config" not in what:
        fresh = await asyncio.to_thread(load_app_config, daemon.base)
        daemon.policy = Policy.from_config(fresh.policy.allow, fresh.policy.deny)
        changed.append("policy")
    if "jobs" in what:
        await daemon.reload_jobs()
        changed.append("jobs")
    return {"reloaded": changed, "requires_restart": requires_restart}


async def _resync(daemon: Daemon, body: dict[str, Any]) -> dict[str, Any]:
    alias = str(body.get("account", "")).strip()
    session = daemon.sessions.get(alias)
    if session is None:
        raise UsageError(f"account {alias!r} is not connected", field="account")
    await session.catch_up()
    session.resync_needed.clear()
    return {"account": alias, "resynced": True}


async def _logout(daemon: Daemon, body: dict[str, Any]) -> dict[str, Any]:
    alias = str(body.get("account", "")).strip()
    session = daemon.sessions.get(alias)
    if session is None or session.client is None:
        raise UsageError(f"account {alias!r} is not connected", field="account")
    await session.client.log_out()
    await daemon.sessions.release(alias)
    daemon.accounts.set_health(alias, "needs_login", reason="logged out")
    return {"account": alias, "logged_out": True}


#: aiohttp deprecates bare string keys on `Application`; a typed key is also
#: the difference between a missing dependency being a KeyError at request
#: time and a type error at import time.
DAEMON_KEY: web.AppKey[Daemon] = web.AppKey("daemon", Daemon)


def build_app(daemon: Daemon) -> web.Application:
    """The application: the v2 routes, the v1 routes, one middleware chain."""
    app = web.Application(
        middlewares=[
            error_middleware,
            auth_middleware,
            version_middleware,
            activity_middleware,
            flood_budget_middleware,
        ]
    )
    app[DAEMON_KEY] = daemon
    app.router.add_post("/v1/op", handle_op)
    app.router.add_get("/v1/events", handle_events)
    app.router.add_get("/v1/status", handle_status)
    app.router.add_post("/v1/admin/{action}", handle_admin)

    # The v1 routes ride the same middleware, so every fix above is global
    # from day one instead of arriving with each group's migration (§12.4).
    from tlgr.daemon.ipc import register_legacy_routes

    register_legacy_routes(app, daemon)
    return app
