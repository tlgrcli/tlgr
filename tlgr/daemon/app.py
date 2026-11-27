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
import uuid
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
from tlgr.core.paths import TlgrPaths, protect_home, write_private
from tlgr.daemon import dispatch as dispatch_module
from tlgr.daemon.events import EventBus
from tlgr.daemon.files import TransferSlots
from tlgr.daemon.idle import ActivityTracker, effective_idle_timeout
from tlgr.daemon.jobs import JobRunner
from tlgr.daemon.peercred import current_uid, peer_of, token_matches
from tlgr.daemon.policy import Policy
from tlgr.daemon.preauth import PreAuthService
from tlgr.daemon.sessions import SessionManager
from tlgr.daemon.stream import NdjsonResponse, walk_pages
from tlgr.daemon.transfers import TransferStore
from tlgr.daemon.webhook import WebhookPusher
from tlgr.version import HEADER_PROTOCOL, HEADER_TOKEN, MIN_DAEMON_PROTOCOL, PROTOCOL

log = logging.getLogger("tlgr.daemon")

__all__ = ["Daemon", "build_app"]

_V1_PREFIX = "/v1/"

#: How long the IPC server waits for a request handler that is still running
#: once everything else has stopped, before cancelling it. aiohttp's default is
#: 60 seconds, applied twice (wait, then cancel and wait again), and a handler
#: blocked on something that will never answer kept a stopped daemon alive for
#: all of it while the restart client had long given up.
SERVER_SHUTDOWN_GRACE_S = 2.0

#: How long a job action already talking to Telegram may run on at shutdown.
#: It is cut short after this, not lost: the scheduler puts it back in the
#: queue and `pending.json` resumes it at the next start.
JOB_ACTION_GRACE_S = 2.0

#: How long a live stream has to write its `end` frame once the shutdown has
#: told it to stop.
STREAM_CLOSE_GRACE_S = 1.0


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
        # Uploads and `--caption-file`-style reads refuse anything under this
        # home, even when it is not `$TLGR_HOME` (`guard_outbound_file`).
        protect_home(self.paths.base)
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
            on_client=self._attach_handlers,
            **factory_kwargs,
        )
        self.webhook_config = load_webhook_config(self.paths.base)
        self.webhook = WebhookPusher(self.webhook_config, self.paths.base, accounts=self.get_client)
        self._job_runner = JobRunner()
        # One action scheduler per account, shared by every job on it: the
        # pacer queues are per (account, action kind), not per job.
        self._schedulers: dict[str, Any] = {}
        self._pacing: dict[str, Any] = {}
        # Long file transfers: the ones `--background` hands over, and the
        # per-DC budgets that stop one 2 GB download from starving five
        # thumbnail fetches (§6.7).
        self.transfers = TransferStore()
        self.transfer_slots = TransferSlots()
        # Login runs in the daemon because the daemon owns the session files
        # (§6.8); a pending login holds one, so it counts as activity and the
        # idle monitor cannot stop the daemon out from under a half-finished
        # sign-in.
        self.preauth = PreAuthService(self.sessions)
        self.managed_by = managed_by
        self.ready = False
        self.shutting_down = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        #: The drain the stopping client asked for (`daemon stop --grace`), or
        #: None for the config's `drain_seconds`.
        self._requested_drain: float | None = None
        #: The task of every IPC request whose handler is still running, and
        #: the ones the shutdown cancelled because the drain ran out.
        self._request_tasks: set[asyncio.Task[Any]] = set()
        self._abandoned: set[asyncio.Task[Any]] = set()
        self._start_time = time.time()
        self._runner: web.AppRunner | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._jobs_task: asyncio.Task[None] | None = None
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
        """Feed an account's updates into the bus, once its client is built.

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

            # One `Raw` handler, not six high-level ones. The high-level
            # builders drop service messages, topic ids and every action kind
            # Telethon does not model, so a stream built on them can only ever
            # show a subset of what the GUI shows; `normalise` names all 163
            # update constructors instead (docs/design/EVENTS.md). The six
            # story updates, which have no builder at all, arrive here too.
            register(on_update, tl_events.Raw())
        except Exception as exc:  # pragma: no cover - a fake client has no builders
            log.debug("could not register the raw Telethon handler for %s: %s", alias, exc)
            register(on_update)

    # -- the job engine's account handles ---------------------------------

    def get_client(self, account: str = "") -> Any:
        if not account:
            return None
        session = self.sessions.get(account)
        return session.job_client if session and session.client is not None else None

    async def ensure_client(self, account: str = "") -> Any:
        """Connect an account on demand and hand back its job client.

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
        return session.job_client if session.client is not None else None

    def touch_ipc(self) -> None:
        self.activity.touch()

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._job_runner.list_jobs()

    @property
    def schedulers(self) -> dict[str, Any]:
        return dict(self._schedulers)

    def action_scheduler(self, alias: str) -> Any:
        """The account's job-action scheduler, created and started on first use."""
        found = self._schedulers.get(alias)
        if found is not None:
            return found
        from tlgr.gateway.executor import DaemonOpRunner
        from tlgr.gateway.pending import PendingStore
        from tlgr.gateway.scheduler import ActionScheduler

        found = ActionScheduler(
            alias,
            DaemonOpRunner(self, alias),
            store=PendingStore(self.paths.account_dir(alias) / "pending.json"),
            tz=self._jobs_timezone(),
            # The account-level `[presence]` setting already owns the status.
            presence_enabled=self.config.presence.mode == "off",
        )
        found.configure(self._pacing.get(alias))
        found.preload()
        self.bus.add_handler(found.on_bus)
        found.start()
        self._schedulers[alias] = found
        return found

    def _jobs_timezone(self) -> Any:
        """`[defaults] timezone` for quiet hours; local time when it is unset."""
        name = (self.config.defaults.timezone or "").strip()
        if not name:
            return None
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            log.warning("unknown timezone %r in [defaults]; quiet hours use local time", name)
            return None

    async def remove_job(self, name: str) -> bool:
        removed = await self._job_runner.remove_job(name)
        for scheduler in self._schedulers.values():
            scheduler.forget_job(name)
        return removed

    async def enable_job(self, name: str) -> bool:
        return await self._job_runner.enable_job(name)

    async def disable_job(self, name: str) -> bool:
        # Disabling says "stop": what the job had queued is dropped as well.
        for scheduler in self._schedulers.values():
            scheduler.cancel(job=name)
        return await self._job_runner.disable_job(name)

    async def reload_jobs(self) -> dict[str, Any]:
        from tlgr.gateway.config import load_jobs_file

        jobs_file = await asyncio.to_thread(load_jobs_file, self.base)
        for problem in jobs_file.problems:
            log.error("jobs.yaml: %s", problem)
        new_configs = jobs_file.jobs
        self._pacing = jobs_file.pacing
        for alias, scheduler in self._schedulers.items():
            scheduler.configure(self._pacing.get(alias))
        default_account = self.config.default_account or self.accounts.get_active() or ""
        old_names = set(self._job_runner._jobs)
        new_names = {jc.name for jc in new_configs}
        # A job that failed validation keeps running as it was: a typo in an
        # edit must not take a working job down with it.
        removed = old_names - new_names - set(jobs_file.rejected)
        added = new_names - old_names
        updated = old_names & new_names

        for name in removed:
            await self.remove_job(name)
        active: dict[str, set[str]] = {}
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
                scheduler = self.action_scheduler(alias)
                job = self._job_runner.create_job(
                    job_config, client, self.webhook, self.bus, scheduler
                )
                if job.enabled:
                    job.start()
                active.setdefault(alias, set()).add(job_config.name)
            except Exception:
                log.exception("could not create job %r", job_config.name)
        # Pending actions saved by the last run come back once their jobs
        # exist again, with the due times they had.
        for alias, names in active.items():
            self._schedulers[alias].resume(names)
        self.activity.jobs_running = len(
            [j for j in self._job_runner.list_jobs() if j.get("running")]
        )
        return {
            "reloaded": True,
            "added": sorted(added),
            "removed": sorted(removed),
            "updated": sorted(updated),
            "problems": list(jobs_file.problems),
        }

    def request_shutdown(self, drain: float | None = None) -> None:
        """Ask `run()` to stop, draining for *drain* seconds if given.

        A client that says how long it will wait has to be given at most that
        long: `daemon stop --grace 10` asked for ten seconds and the daemon used
        to drain for the config's thirty, so the client gave up and reported a
        failure while the daemon was still on its way down.
        """
        if drain is not None:
            drain = max(0.0, float(drain))
            if self._requested_drain is None or drain < self._requested_drain:
                self._requested_drain = drain
        self._shutdown_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._shutdown_event.is_set()

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
            "actions": {alias: s.snapshot() for alias, s in self._schedulers.items()},
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
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=SERVER_SHUTDOWN_GRACE_S)
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
        self._jobs_task = asyncio.create_task(self._start_jobs(), name="tlgr-jobs")
        await self._shutdown_event.wait()
        await self.shutdown(drain=self._requested_drain)

    async def _start_jobs(self) -> None:
        """Load `jobs.yaml` and start every enabled job, once accounts are up.

        Without this a restarted daemon ran no jobs until someone ran
        `tlgr job reload`, which is how the gateway sat idle from a restart
        on 2026-09-30 until it was noticed. It runs in the background because
        each job may wait for its account to come online, and readiness must
        not wait for that.
        """
        try:
            result = await self.reload_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not start jobs from jobs.yaml")
            return
        if result.get("added"):
            log.info("started %d job(s) from jobs.yaml", len(result["added"]))

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
        """The ordered teardown of §6.11, bounded so a restart can rely on it.

        Setting `shutting_down` first is what ends the live streams
        (`events.watch`, `job test`, `daemon logs --follow`): each one is
        racing this event and closes with its `end` frame. They are not
        drained, because they never finish on their own; neither are job
        actions, which are cut short after `JOB_ACTION_GRACE_S` and resume from
        `pending.json`. The drain waits only for client requests that will
        finish, and never past *drain*.
        """
        if self.shutting_down.is_set():
            return
        self.shutting_down.set()
        self.ready = False
        deadline = time.monotonic() + (
            drain if drain is not None else self.config.daemon.drain_seconds
        )

        for task in (self._idle_task, self._jobs_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        # Job actions start nothing new from here; one already talking to
        # Telegram is an in-flight request and the drain below waits for it.
        for scheduler in list(self._schedulers.values()):
            scheduler.close()

        # Wait for in-flight requests rather than cancelling them: a ten
        # minute scan that is killed at second 599 has cost the account the
        # requests it already made and produced nothing (COR-11).
        # Only the requests that end on their own count here: an open event
        # stream or a job action stuck on Telegram used to hold the drain to
        # its deadline on every stop.
        while self.activity.draining > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        # The live streams were told to stop when `shutting_down` was set;
        # this is only the moment they need to write their `end` frame.
        streams_by = time.monotonic() + STREAM_CLOSE_GRACE_S
        while self.activity.event_streams > 0 and time.monotonic() < streams_by:
            await asyncio.sleep(0.02)
        await self._cancel_requests()

        with contextlib.suppress(Exception):
            await self._job_runner.stop_all()
        # After the jobs (nothing new is queued) and before the sessions (a
        # running action may finish): what is still pending is written to
        # `accounts/<alias>/pending.json` and resumes at the next start.
        for scheduler in list(self._schedulers.values()):
            self.bus.remove_handler(scheduler.on_bus)
            with contextlib.suppress(Exception):
                await scheduler.stop(timeout=JOB_ACTION_GRACE_S)
        # A half-written download keeps its `.part` file, so the next
        # `media download --resume` continues where the shutdown stopped it.
        with contextlib.suppress(Exception):
            await self.transfers.stop_all()
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

    async def _cancel_requests(self) -> None:
        """Cancel the requests the drain gave up on, so each caller hears why.

        A request waiting on something that will never answer (a flood-wait
        sleep, an account that is not coming back) otherwise outlives the
        drain, and the IPC server's cleanup then waits for it while every
        account already reports stopped. Cancelled here, the caller gets a
        RETRYABLE answer (see `activity_middleware`) instead of a reset.
        """
        left = [task for task in self._request_tasks if not task.done()]
        if not left:
            return
        log.info(
            "cancelling %d request(s) still running after the drain",
            len(left),
            extra={"count": len(left)},
        )
        for task in left:
            self._abandoned.add(task)
            task.cancel()
        await asyncio.wait(left, timeout=SERVER_SHUTDOWN_GRACE_S)

    def absorb_shutdown_cancel(self) -> bool:
        """True when the current request was cancelled by `_cancel_requests`.

        The caller then answers instead of re-raising; the cancellation is
        cleared so the task can finish writing that answer.
        """
        task = asyncio.current_task()
        if task is None or task not in self._abandoned:
            return False
        self._abandoned.discard(task)
        uncancel = getattr(task, "uncancel", None)
        if uncancel is not None:
            uncancel()
        return True


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


def _stopped_mid_request() -> RetryableError:
    """What a request cancelled by the shutdown is answered with."""
    return RetryableError("the daemon stopped before this request finished; retry")


@web.middleware
async def activity_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Count the request, and refuse new work while shutting down (§6.11)."""
    daemon: Daemon = request.app[DAEMON_KEY]
    if daemon.shutting_down.is_set() and request.path != "/v1/status":
        return _error_response(
            request, RetryableError("the daemon is shutting down; retry in a moment")
        )
    task = asyncio.current_task()
    daemon.activity.begin_request()
    if task is not None:
        daemon._request_tasks.add(task)
    try:
        return await handler(request)
    except asyncio.CancelledError:
        if not daemon.absorb_shutdown_cancel():
            raise
        # The shutdown cancelled it after the drain ran out. Answer, rather
        # than drop the connection: "retry once the daemon is back" is the
        # truth, and a reset reads as a crash. (A stream answers in its own
        # framing, in `_handle_op_stream`, before it gets here.)
        return _error_response(request, _stopped_mid_request())
    finally:
        if task is not None:
            daemon._request_tasks.discard(task)
            daemon._abandoned.discard(task)
        daemon.activity.end_request()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def handle_op(request: web.Request) -> web.StreamResponse:
    daemon: Daemon = request.app[DAEMON_KEY]
    raw = await request.read()
    op_request = dispatch_module.decode_request(raw)
    if op_request.stream or op_request.all or _is_stream_op(op_request.op):
        # A streaming operation is streamed whether or not the caller
        # remembered to say so: its result is an async iterator, and answering
        # a plain POST with "cannot encode an async_generator" would report a
        # tlgr bug as the caller's mistake.
        return await _handle_op_stream(request, daemon, op_request)
    envelope = await dispatch_module.dispatch(daemon, op_request)
    return web.Response(
        body=json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8"),
        content_type="application/json",
    )


def _is_stream_op(op_id: str) -> bool:
    from tlgr.registry import ALIASES, REGISTRY

    spec = REGISTRY.get(ALIASES.get(op_id.replace(" ", "."), op_id))
    return bool(spec is not None and spec.stream)


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
    live = "live-stream" in spec.tags
    # A live stream is open-ended, so it is counted as a stream rather than as
    # a request the shutdown drain should wait for.
    if live:
        daemon.activity.begin_stream()
    stopped = False
    try:
        # `spec` is the same object `resolve_spec` returned above; the
        # execute() call is what runs the policy/account/timeout prologue.
        _, context, result = await dispatch_module.execute(daemon, op_request)
        if "frames" in spec.tags and hasattr(result, "__aiter__"):
            # A frame-producing operation writes its own NDJSON vocabulary —
            # events, `gap`, `lag`, `heartbeat`. Wrapping those in `item`
            # frames would make a heartbeat indistinguishable from an event
            # for anybody reading the stream one line at a time.
            count = 0
            frames = _until_shutdown(daemon, result) if live else result
            async for frame in frames:
                if not isinstance(frame, dict):  # pragma: no cover - impl contract
                    continue
                await stream.write(frame)
                count += 1
            stopped = live and daemon.shutting_down.is_set()
        elif hasattr(result, "__aiter__"):
            # A `--all` walk paces itself against the account's own limiter,
            # inside the daemon: v1 looped in the client and hammered the
            # socket with no backpressure between pages (ROB-01).
            count = await walk_pages(
                result, stream, limiter=context.limiter, rate_class=spec.rate_class
            )
        else:
            count = await _stream_result(stream, result)
    except asyncio.CancelledError:
        if not daemon.absorb_shutdown_cancel():
            raise
        # The response is already started, so the answer is the stream's own
        # failing `end` frame rather than an error body.
        return await stream.fail(_stopped_mid_request(), account=op_request.account)
    except Exception as exc:
        return await stream.fail(exc, account=op_request.account)
    finally:
        if live:
            daemon.activity.end_stream()
    end: dict[str, Any] = {"ok": True, "count": count}
    if stopped:
        # Told apart from the stream ending on its own terms, so a watcher
        # knows to reconnect (and resume with `--since`) rather than stop.
        end["reason"] = "shutdown"
    return await stream.end(**end, elapsed_ms=int((time.monotonic() - started) * 1000))


async def _until_shutdown(daemon: Daemon, frames: Any) -> Any:
    """Yield from *frames* until the daemon starts shutting down.

    `events.watch` waits on its bus subscription with no deadline but its
    `--follow-for`, and the bus closing that subscription at shutdown does not
    wake it. Without this race an open `tlgr watch` kept the shutdown drain
    busy to its deadline and then held the IPC server's cleanup until aiohttp
    gave up on it, so the daemon reported every account stopped and never
    exited. Closing the generator runs its own `finally` (the unsubscribe).
    """
    iterator = frames.__aiter__()
    stopping = asyncio.ensure_future(daemon.shutting_down.wait())
    try:
        while True:
            step = asyncio.ensure_future(iterator.__anext__())
            await asyncio.wait({step, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if not step.done():
                step.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await step
                return
            try:
                frame = step.result()
            except StopAsyncIteration:
                return
            yield frame
    finally:
        stopping.cancel()
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


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


#: v1's `/v1/events` query names → the `events.watch` request fields they are
#: now spelled as. The endpoint predates the operation; §12.4 says a
#: documented shape does not disappear because the code behind it moved.
_EVENTS_QUERY_ALIASES = {"types": "events", "chats": "chat", "timeout": "follow_for"}


def _events_request(query: Any) -> dict[str, Any]:
    """`GET /v1/events?…` → the `events.watch` request body.

    The endpoint is a GET-shaped alias of `POST /v1/op {op: events.watch}`:
    one implementation, one filter vocabulary, one set of frames. Two code
    paths reading the same bus with two ideas of what `--events` means is
    exactly the drift the registry exists to remove.
    """
    body: dict[str, Any] = {}
    for key, value in query.items():
        name = _EVENTS_QUERY_ALIASES.get(key, key)
        if name in ("account", "flood_wait_max"):
            continue
        if name in ("chat", "sender"):
            body[name] = [part for part in str(value).split(",") if part]
        else:
            body[name] = value
    # The endpoint's historical default is everything; `tlgr watch`'s is v1's
    # `new_message`, and that difference is deliberate.
    body.setdefault("events", "all")
    return body


async def handle_events(request: web.Request) -> web.StreamResponse:
    daemon: Daemon = request.app[DAEMON_KEY]
    account = request.query.get("account", "").strip()
    if not account:
        return _error_response(
            request, UsageError("GET /v1/events needs ?account=<alias>", field="account")
        )
    from tlgr.models.envelope import OpRequest
    from tlgr.version import VERSION

    op_request = OpRequest(
        op="events.watch",
        account=account,
        request=_events_request(request.query),
        request_id=request.headers.get("X-Tlgr-Request-Id", "") or uuid.uuid4().hex,
        client_version=VERSION,
        protocol=PROTOCOL,
        stream=True,
    )
    # Counted as a stream by `_handle_op_stream`, like any live stream.
    return await _handle_op_stream(request, daemon, op_request)


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
        # tell a graceful shutdown from a crash. The drain is the one the caller
        # asked for, because the caller is waiting exactly that long.
        asyncio.get_running_loop().call_later(0.01, daemon.request_shutdown, drain)
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
        # Validated before anything is swapped: a policy entry that matches no
        # operation fails the whole reload and leaves the running config.
        policy = Policy.from_config(fresh.policy.allow, fresh.policy.deny)
        if fresh.network != daemon.config.network:
            requires_restart.append("network")
        if fresh.identity != daemon.config.identity:
            requires_restart.append("identity")
        daemon.config = fresh
        daemon.policy = policy
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
    """The application: four `/v1/*` routes and one middleware chain.

    PR-12 removed the last v1 route. Everything the daemon serves now goes
    through `POST /v1/op`, which means the peer-uid check, the policy
    allowlist, the version handshake, the flood budget and the error
    classification apply to every command without exception — the thing the
    v1 routes could only approximate by being registered here.
    """
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
    return app
