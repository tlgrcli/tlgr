"""The `daemon` group: lifecycle, health, floods and dead letters.

Two halves that look alike and are not. `start`, `stop`, `restart`, `install`,
`uninstall`, `logs` and `status` run **outside** the daemon — they are how you
find out that it is not running, so they cannot need it to answer. Everything
else (`reconnect`, `save-state`, `flood *`, `dead-letter *`) runs inside it,
because it is asking about state only the running process has.

`status` is the one worth reading the code of. v1 reported which clients the
daemon *held*: a client whose connection had died was still in the dict, still
listed under `accounts`, and the daemon still called itself healthy (COR-13,
COR-37). Here every account carries a state, a `pts` and a `behind_seconds`,
and `healthy` is false when any account needs a login, is frozen, or has
fallen behind — so "the process is alive" and "the daemon works" are separate
answers to separate questions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import (
    EXIT_EMPTY,
    DaemonError,
    DaemonNotRunningError,
    NotFoundError,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.daemon import (
    AccountHealth,
    DaemonStatus,
    DeadLetter,
    DeadLetterResult,
    EventBusStatus,
    FloodRecord,
    FloodResult,
    LifecycleResult,
    LogLine,
    ReconnectedAccount,
    ReconnectResult,
    SavedState,
    SaveStateResult,
    ServiceResult,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops._params import choice, opt, parse_dt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _base() -> Path:
    from tlgr.core.paths import default_base

    return default_base()


def _writable_base(what: str) -> Path:
    """The tlgr home, refused when it is somebody's live installation.

    A home carrying a `.production` marker belongs to a running daemon with
    real accounts in it. Starting a second one there shares the session files,
    and Telegram revokes an auth key it sees two clients on — so the marker is
    a hard stop rather than a warning, with `TLGR_ALLOW_PRODUCTION_HOME=1` as
    the escape hatch a person types on purpose.
    """
    from tlgr.core.paths import refuse_production_home

    base = _base()
    refuse_production_home(base)
    return base


def _probe(timeout: float = 2.0) -> dict[str, Any] | None:
    """`GET /v1/status`, without ever starting a daemon to answer it.

    `tlgr daemon status` exists to tell you the daemon is down; auto-starting
    one to find out would make the question unanswerable.
    """
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(_base(), timeout=timeout, auto_start=False, no_restart=True)
    with contextlib.suppress(Exception):
        return client.probe_status()
    return None


def _admin(action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(_base(), timeout=30.0, auto_start=False, no_restart=True)
    return client.admin(action, body or {})


def _stamp(value: float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spanned(ctx: OpContext) -> list[str]:
    """Which accounts an `--account all` daemon operation covers.

    Empty or `all` means every account the daemon holds. Naming one narrows
    it. There is no "pick one for me": v1 did that and a two-account user
    silently operated on the wrong identity (COR-02).
    """
    alias = (ctx.account or "").strip()
    daemon = getattr(ctx, "daemon", None)
    sessions = getattr(daemon, "sessions", None)
    known = list(getattr(sessions, "aliases", []) or [])
    if alias and alias != "all":
        if known and alias not in known:
            raise NotFoundError(f"account {alias!r} is not connected. Run: tlgr daemon status")
        return [alias]
    return known


def _daemon(ctx: OpContext) -> Any:
    daemon = getattr(ctx, "daemon", None)
    if daemon is None:
        raise DaemonError("this operation runs inside the daemon")
    return daemon


def _telethon_layer() -> int:
    with contextlib.suppress(Exception):
        from telethon.tl.alltlobjects import LAYER

        return int(LAYER)
    return 0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _spawn(base: Path, *, foreground: bool = False) -> Any:
    """Start the daemon process.

    Spawned rather than imported, and not only because `ops/` may not import
    `daemon/` (§2.2): a daemon that shares this process's file descriptors,
    signal handlers and event loop is not the process a supervisor will start
    later, so testing one would not test the other.
    """
    command = [sys.executable, "-m", "tlgr.daemon.main", "--base", str(base)]
    if foreground:
        command.append("--foreground")
        return subprocess.Popen(command)
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_ready(timeout: float) -> dict[str, Any] | None:
    """Poll `/v1/status` until the daemon answers.

    Readiness is a *reply*, not a file. v1 waited for the socket to appear,
    which happens at `bind()` — before any account has connected and before
    the daemon can serve anything (ROB-07).
    """
    from tlgr.transport.autostart import wait_ready
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(_base(), auto_start=False, no_restart=True)
    return wait_ready(client.probe_status, timeout=timeout)


class DaemonStartReq(Request):
    foreground: Annotated[
        bool, opt("--foreground", help="Run in the foreground instead of forking.")
    ] = False
    connect: Annotated[
        list[str],
        opt("--connect", metavar="ALIAS", help="Only connect these accounts (repeatable)."),
    ] = []
    catch_up: Annotated[
        bool,
        opt(
            "--catch-up/--no-catch-up",
            help="Load the persisted pts/qts/seq and fetch the difference before dispatching.",
        ),
    ] = True
    idle_timeout: Annotated[
        int | None,
        opt(
            "--idle-timeout", metavar="DURATION", kind="duration", help="0 disables the idle stop."
        ),
    ] = None
    wait: Annotated[
        int, opt("--wait", metavar="DURATION", kind="duration", help="How long to wait for ready.")
    ] = 30


async def daemon_start(ctx: OpContext, req: DaemonStartReq) -> LifecycleResult:
    """Start the update-receiving daemon.

    `catch_up` defaults to true and `idle_timeout` to 0 for good reason: v1
    combined an idle stop at 1,800 s with an effectively disabled catch-up, so
    the daemon shut down, restarted on the next command, and never fetched
    what it had missed. That combination is a guaranteed, permanent sync hole.
    """
    from tlgr.core.process import read_pid

    base = _writable_base("tlgr daemon start")
    existing = read_pid(base)
    if existing:
        status = _probe() or {}
        return LifecycleResult(
            started=False,
            already=True,
            pid=existing,
            socket=str(status.get("daemon", {}).get("socket", "")),
            ready=bool(status.get("daemon", {}).get("ready")),
            catch_up=req.catch_up,
        )

    environment_note = _start_environment(req)
    if req.foreground:
        raise SystemExit(_spawn(base, foreground=True).wait())

    process = _spawn(base)
    status = _wait_ready(float(req.wait))
    if status is None:
        raise DaemonError(
            f"the daemon did not become ready within {req.wait}s; check the log: tlgr daemon logs"
        )
    info = status.get("daemon", {})
    if environment_note:
        ctx.warn(environment_note)
    return LifecycleResult(
        started=True,
        pid=int(info.get("pid") or read_pid(base) or process.pid),
        socket=str(info.get("socket", "")),
        ready=bool(info.get("ready")),
        accounts=[row.get("alias", "") for row in status.get("accounts", [])],
        catch_up=req.catch_up,
    )


def _start_environment(req: DaemonStartReq) -> str:
    """Apply the per-start overrides through the environment the child reads."""
    notes: list[str] = []
    if req.idle_timeout is not None:
        os.environ["TLGR_IDLE_TIMEOUT"] = str(int(req.idle_timeout))
        notes.append(f"idle_timeout was set to {int(req.idle_timeout)}s for this run only")
    if not req.catch_up:
        os.environ["TLGR_CATCH_UP"] = "0"
        notes.append(
            "catch-up is disabled for this run: updates that arrive while the "
            "daemon is down will not be recovered"
        )
    if req.connect:
        os.environ["TLGR_PRECONNECT"] = ",".join(req.connect)
    return "; ".join(notes)


SPEC_DAEMON_START = OperationSpec(
    id="daemon.start",
    request=DaemonStartReq,
    response=LifecycleResult,
    impl=daemon_start,
    summary="Start the update-receiving daemon",
    description=(
        "Waits for an HTTP 200 from `/v1/status`, not for the socket file: "
        "the socket exists from `bind()`, before any account has connected "
        "(ROB-07). Catch-up is on by default and the idle stop is off, "
        "because the two together are what made v1 lose updates silently."
    ),
    legacy_paths=("daemon start",),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=120,
    example={"started": True, "pid": 41231, "ready": True, "catch_up": True},
    example_args="daemon start",
    covers=(
        "updates.stream-daemon-multi-account",
        "updates.sync-catch-up-on-start",
        "updates.sync-new-session-triggers-diff",
    ),
    covers_partial=("updates.ops-daemon-lifecycle",),
    coverage_note="starting the process; stopping it cleanly is `daemon stop`.",
    tags=frozenset({"agent-safe"}),
)


class DaemonStopReq(Request):
    timeout: Annotated[
        int,
        opt("--grace", metavar="DURATION", kind="duration", help="Drain period before SIGKILL."),
    ] = 10


async def daemon_stop(ctx: OpContext, req: DaemonStopReq) -> LifecycleResult:
    """Stop the daemon, letting it flush pts and the entity cache first.

    Every shutdown path has to `await disconnect()`: a SIGKILL loses the
    update state and the cached access hashes, and losing an access hash is
    what makes the next catch-up silently skip a channel.
    """
    from tlgr.core.process import read_pid

    base = _base()
    pid = read_pid(base)
    if pid is None:
        return LifecycleResult(stopped=False, already=True)

    with contextlib.suppress(Exception):
        _admin("stop", {"drain_s": float(req.timeout)})

    deadline = time.monotonic() + max(1.0, float(req.timeout))
    while time.monotonic() < deadline:
        if read_pid(base) is None:
            return LifecycleResult(stopped=True, pid=pid)
        time.sleep(0.1)

    from tlgr.core.process import stop_daemon

    stop_daemon(base)
    for _ in range(20):
        time.sleep(0.25)
        if read_pid(base) is None:
            return LifecycleResult(stopped=True, pid=pid)
    raise DaemonError(f"the daemon (pid {pid}) did not stop within {req.timeout}s")


SPEC_DAEMON_STOP = OperationSpec(
    id="daemon.stop",
    request=DaemonStopReq,
    response=LifecycleResult,
    impl=daemon_stop,
    summary="Stop the daemon",
    description=(
        "Asks it to drain in-flight requests and disconnect cleanly, then "
        "falls back to SIGTERM. A killed daemon loses its `pts` and the "
        "cached access hashes catch-up needs."
    ),
    legacy_paths=("daemon stop",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=60,
    example={"stopped": True, "pid": 41231},
    example_args="daemon stop",
    covers=("updates.ops-daemon-lifecycle",),
    tags=frozenset({"agent-safe"}),
)


class DaemonRestartReq(Request):
    timeout: Annotated[
        int,
        opt("--grace", metavar="DURATION", kind="duration", help="Drain period before SIGKILL."),
    ] = 10
    wait: Annotated[
        int, opt("--wait", metavar="DURATION", kind="duration", help="How long to wait for ready.")
    ] = 30


async def daemon_restart(ctx: OpContext, req: DaemonRestartReq) -> LifecycleResult:
    """Stop and start, waiting for readiness at both ends."""
    from tlgr.core.process import read_pid

    base = _writable_base("tlgr daemon restart")
    if read_pid(base) is not None:
        await daemon_stop(ctx, DaemonStopReq(timeout=req.timeout))
    started = await daemon_start(ctx, DaemonStartReq(wait=req.wait))
    return LifecycleResult(
        restarted=True,
        pid=started.pid,
        socket=started.socket,
        ready=started.ready,
        accounts=started.accounts,
    )


SPEC_DAEMON_RESTART = OperationSpec(
    id="daemon.restart",
    request=DaemonRestartReq,
    response=LifecycleResult,
    impl=daemon_restart,
    summary="Restart the daemon",
    legacy_paths=("daemon restart",),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=180,
    example={"restarted": True, "pid": 41999},
    example_args="daemon restart",
    covers_partial=("updates.ops-daemon-lifecycle",),
    coverage_note="a stop and a start; the lifecycle itself is `daemon stop`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Service installation
# ---------------------------------------------------------------------------


def _supervisor(choice_: str) -> str:
    if choice_ != "auto":
        return choice_
    return "launchd" if platform.system() == "Darwin" else "systemd"


class DaemonInstallReq(Request):
    supervisor: Annotated[
        str, choice("auto", "launchd", "systemd", help="Which service manager to install into.")
    ] = "auto"
    keep_alive: Annotated[
        bool, opt("--keep-alive/--no-keep-alive", help="Restart the daemon on crash.")
    ] = True


async def daemon_install(ctx: OpContext, req: DaemonInstallReq) -> ServiceResult:
    """Install as a *user* service: it holds session files under $HOME.

    Both backends force `idle_timeout` to 0. Under a supervisor a clean idle
    exit is either a respawn loop or a daemon that never comes back (COR-39).
    """
    base = _writable_base("tlgr daemon install")
    kind = _supervisor(req.supervisor)
    if kind == "launchd":
        from tlgr.core import launchd
        from tlgr.core.config import get_logs_dir

        if launchd.is_installed():
            ctx.mark_already()
            return ServiceResult(
                installed=True, already=True, supervisor=kind, path=str(launchd.PLIST_PATH)
            )
        path = launchd.install(base, get_logs_dir(base))
    else:
        from tlgr.core import systemd

        if systemd.is_installed():
            ctx.mark_already()
            return ServiceResult(
                installed=True, already=True, supervisor=kind, path=str(systemd.unit_path())
            )
        path = systemd.install(base)
    if not req.keep_alive:
        ctx.warn(
            "--no-keep-alive is recorded but not honoured by the generated unit; "
            "edit it directly to disable the restart"
        )
    return ServiceResult(installed=True, supervisor=kind, unit=path.name, path=str(path))


SPEC_DAEMON_INSTALL = OperationSpec(
    id="daemon.install",
    request=DaemonInstallReq,
    response=ServiceResult,
    impl=daemon_install,
    summary="Install the daemon as a user service (auto-start, restart on crash)",
    description=(
        "macOS gets a LaunchAgent, Linux a systemd **user** unit — user, "
        "because the daemon holds session files under $HOME and must run as "
        "their owner."
    ),
    legacy_paths=("daemon install",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=60,
    example={"installed": True, "supervisor": "launchd", "path": "~/Library/LaunchAgents/…"},
    example_args="daemon install",
    covers_partial=("updates.ops-daemon-lifecycle",),
    coverage_note="the supervisor half; running the daemon is `daemon start`/`stop`.",
    tags=frozenset({"agent-safe"}),
)


class DaemonUninstallReq(Request):
    stop: Annotated[bool, opt("--stop/--no-stop", help="Also stop a running daemon.")] = True


async def daemon_uninstall(ctx: OpContext, req: DaemonUninstallReq) -> ServiceResult:
    """Remove the user service."""
    from tlgr.core import launchd, systemd

    removed = launchd.uninstall() if platform.system() == "Darwin" else False
    removed = systemd.uninstall() or removed
    stopped = False
    if req.stop:
        result = await daemon_stop(ctx, DaemonStopReq())
        stopped = result.stopped
    if not removed:
        ctx.mark_already()
    return ServiceResult(uninstalled=removed, already=not removed, stopped=stopped)


SPEC_DAEMON_UNINSTALL = OperationSpec(
    id="daemon.uninstall",
    request=DaemonUninstallReq,
    response=ServiceResult,
    impl=daemon_uninstall,
    summary="Remove the daemon service",
    legacy_paths=("daemon uninstall",),
    mutating=True,
    destructive=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=60,
    example={"uninstalled": True, "stopped": True},
    example_args="daemon uninstall",
    covers_partial=("updates.ops-daemon-lifecycle",),
    coverage_note="the supervisor half; running the daemon is `daemon start`/`stop`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

_LOG_LEVELS = ("debug", "info", "warning", "error")
_LEVEL_RANK = {name: index for index, name in enumerate(_LOG_LEVELS)}
_PLAIN_LOG = re.compile(r"^(?P<ts>\S+)\s+(?P<level>[A-Z]+)\s+(?P<logger>\S+)\s+(?P<message>.*)$")


def _parse_log(line: str) -> LogLine:
    """One log line, structured where it can be and verbatim where it cannot."""
    stripped = line.rstrip("\n")
    if stripped.startswith("{"):
        with contextlib.suppress(json.JSONDecodeError):
            record = json.loads(stripped)
            if isinstance(record, dict):
                return LogLine(
                    ts=str(record.get("ts") or record.get("time") or ""),
                    level=str(record.get("level", "")).lower(),
                    account=record.get("account"),
                    logger=str(record.get("logger", "")),
                    message=str(record.get("message", "")),
                    raw=stripped,
                )
    match = _PLAIN_LOG.match(stripped)
    if match:
        return LogLine(
            ts=match.group("ts"),
            level=match.group("level").lower(),
            logger=match.group("logger"),
            message=match.group("message"),
            raw=stripped,
        )
    return LogLine(message=stripped, raw=stripped)


def _wanted_log(entry: LogLine, level: str | None, account: str | None, grep: str | None) -> bool:
    if level and _LEVEL_RANK.get(entry.level, 0) < _LEVEL_RANK.get(level, 0):
        return False
    if account and entry.account != account:
        return False
    return not (grep and grep.lower() not in entry.raw.lower())


class DaemonLogsReq(Request):
    follow: Annotated[bool, opt("--follow", "-f", help="Follow the log as it is written.")] = False
    lines: Annotated[int, opt("--lines", metavar="N", ge=1, le=100000)] = 50
    level: Annotated[str | None, choice(*_LOG_LEVELS, help="Minimum level to show.")] = None
    log_account: Annotated[
        str | None,
        opt("--for-account", metavar="ALIAS", help="Only lines tagged with this account."),
    ] = None
    grep: Annotated[str | None, opt("--grep", metavar="TEXT", help="Substring filter.")] = None


async def daemon_logs(ctx: OpContext, req: DaemonLogsReq) -> AsyncIterator[dict[str, Any]]:
    """The daemon log, tailed and filtered.

    Read here rather than exec'ing `tail`, so that `--level`, `--for-account`
    and `--grep` mean the same thing whether or not you are following, and so
    the output is structured rather than whatever the log formatter happened
    to print.
    """
    path = _base() / "logs" / "daemon.log"
    if not path.exists():
        raise NotFoundError(f"no log file at {path}. Has the daemon ever started?")

    with path.open(encoding="utf-8", errors="replace") as handle:
        tail = handle.readlines()[-req.lines :]
        for line in tail:
            entry = _parse_log(line)
            if _wanted_log(entry, req.level, req.log_account, req.grep):
                yield {"type": "log", **_log_frame(entry)}
        if not req.follow:
            return
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if not line:
                await asyncio.sleep(0.25)
                continue
            entry = _parse_log(line)
            if _wanted_log(entry, req.level, req.log_account, req.grep):
                yield {"type": "log", **_log_frame(entry)}


def _log_frame(entry: LogLine) -> dict[str, Any]:
    from tlgr.models.base import to_builtins

    frame = to_builtins(entry)
    return frame if isinstance(frame, dict) else {"raw": entry.raw}


SPEC_DAEMON_LOGS = OperationSpec(
    id="daemon.logs",
    request=DaemonLogsReq,
    response=None,
    impl=daemon_logs,
    summary="View or follow the daemon log",
    description=(
        "Structured lines with secrets redacted: an auth key, an access hash, "
        "a proxy secret and a webhook token are never written to the log in "
        "the first place."
    ),
    legacy_paths=("daemon logs",),
    stream=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=900,
    example={"type": "log", "level": "info", "message": "daemon ready with 1 account(s)"},
    example_args="daemon logs --lines 100 --level warning",
    covers_partial=("updates.ops-daemon-lifecycle",),
    coverage_note="the operator's view of the process; the lifecycle is `daemon stop`.",
    tags=frozenset({"agent-safe", "frames", "live-stream"}),
)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _account_health(row: dict[str, Any]) -> AccountHealth:
    return AccountHealth(
        alias=str(row.get("alias", "")),
        state=str(row.get("state", "unknown")),
        user_id=row.get("user_id"),
        username=row.get("username"),
        dc_id=row.get("dc_id"),
        proxy=row.get("proxy"),
        pts=row.get("pts"),
        qts=row.get("qts"),
        seq=row.get("seq"),
        date=row.get("date"),
        behind_seconds=row.get("behind_seconds"),
        catching_up=bool(row.get("catch_up_pending")),
        channels_tracked=int(row.get("channels_tracked") or 0),
        last_update_at=row.get("last_update"),
        connected_since=row.get("connected_since"),
        reconnects=int(row.get("reconnects") or 0),
        in_flight=int(row.get("in_flight") or 0),
        resync_needed=list(row.get("resync_needed") or []),
        flood_waits=int(row.get("flood_entries") or 0),
        circuit=str(row.get("circuit", "closed")),
        frozen=str(row.get("state", "")) == "frozen",
        error=row.get("reason"),
    )


#: An account in one of these states is not doing its job, whatever the
#: process is doing. `healthy` has to be false for all of them, or the flag
#: means "a process exists" — which is the question nobody was asking.
_UNHEALTHY = frozenset({"needs_login", "frozen", "degraded", "stopped"})


class DaemonStatusReq(Request):
    check: Annotated[
        bool, opt("--check", help="Exit 11 when the daemon or any account is unhealthy.")
    ] = False


async def daemon_status(ctx: OpContext, req: DaemonStatusReq) -> DaemonStatus:
    """Daemon and per-account health, as two separate answers.

    `running` has always meant "a process is alive". `ready` and `healthy`
    are the questions people were actually asking, and v1 could not tell them
    apart: an account whose connection had died was still counted (COR-37).
    """
    from tlgr.core.process import read_pid

    base = _base()
    pid = read_pid(base)
    status = _probe()
    if status is None:
        result = DaemonStatus(running=pid is not None, ready=False, healthy=False, pid=pid)
        if req.check:
            raise DaemonNotRunningError("the daemon is not answering on its socket")
        return result

    info = status.get("daemon", {})
    rows = [_account_health(row) for row in status.get("accounts", [])]
    if ctx.account and ctx.account != "all":
        rows = [row for row in rows if row.alias == ctx.account]
    unhealthy = [row for row in rows if row.state in _UNHEALTHY]
    result = DaemonStatus(
        running=True,
        ready=bool(info.get("ready")),
        healthy=bool(info.get("ready")) and not unhealthy,
        pid=info.get("pid") or pid,
        uptime_seconds=int(info.get("uptime_s") or 0),
        version=str(info.get("version", "")),
        protocol=int(info.get("protocol") or 0),
        layer=_telethon_layer(),
        socket=str(info.get("socket", "")),
        socket_owner=os.getuid(),
        managed_by=info.get("managed_by"),
        accounts=rows,
        events=EventBusStatus(**status["events"])
        if isinstance(status.get("events"), dict)
        else None,
        webhook=status.get("webhook") or {},
        jobs=status.get("jobs") or [],
        connections={row.alias: row.state == "online" for row in rows},
        disconnected=sorted(row.alias for row in rows if row.state != "online"),
    )
    if req.check and not result.healthy:
        raise DaemonError(
            "the daemon is not healthy: "
            + (", ".join(f"{row.alias} is {row.state}" for row in unhealthy) or "not ready")
        )
    return result


SPEC_DAEMON_STATUS = OperationSpec(
    id="daemon.status",
    request=DaemonStatusReq,
    response=DaemonStatus,
    impl=daemon_status,
    summary="Show daemon and per-account connection health",
    description=(
        "`running` is about the process, `ready` about the socket, `healthy` "
        "about the accounts. v1 had only the first and reported every client "
        "it held as connected, so a fully deaf daemon looked fine (COR-37)."
    ),
    legacy_paths=("daemon status",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("running", "ready", "healthy", "pid", "uptime_seconds", "disconnected"),
    example={
        "running": True,
        "ready": True,
        "healthy": True,
        "pid": 41231,
        "uptime_seconds": 8123,
        "accounts": [{"alias": "work", "state": "online", "pts": 91824}],
    },
    example_args="daemon status --check",
    covers=(
        "bots.bot-updates-status",
        "updates.ops-single-updates-consumer",
        "updates.session-persistence",
    ),
    covers_partial=(
        "updates.config-account-frozen",
        "updates.net-connection-status",
        "updates.ops-reconnect-health",
        "updates.stream-daemon-multi-account",
        "updates.sync-updating-indicator",
    ),
    coverage_note=(
        "reports the state; the network detail is `net status`, the freeze "
        "fields are `config app get`, and recovery is `daemon reconnect`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Reconnect and save-state
# ---------------------------------------------------------------------------


class DaemonReconnectReq(Request):
    reset_proxy: Annotated[
        bool, opt("--reset-proxy", help="Rebuild the client with the currently selected proxy.")
    ] = False
    catch_up: Annotated[
        bool, opt("--catch-up/--no-catch-up", help="Fetch the difference after reconnecting.")
    ] = True


async def daemon_reconnect(ctx: OpContext, req: DaemonReconnectReq) -> ReconnectResult:
    """Force a reconnect, and by default a catch-up with it.

    Also the documented recovery for a `TypeNotFoundError` from a constructor
    of a newer layer: the guidance is to treat it like a 500 — reopen the
    socket, re-`initConnection`, then `getDifference` — because a socket that
    has met an unparseable constructor cannot be trusted to be in sync.
    """
    daemon = _daemon(ctx)
    out: list[ReconnectedAccount] = []
    for alias in _spanned(ctx):
        session = daemon.sessions.get(alias)
        if session is None:
            out.append(ReconnectedAccount(alias=alias, error="not connected"))
            continue
        row = ReconnectedAccount(alias=alias)
        try:
            if req.reset_proxy:
                await daemon.sessions.release(alias)
                session = await daemon.sessions.ensure(alias)
            else:
                client = session.client
                if client is not None:
                    await client.disconnect()
                    await client.connect()
            row.reconnected = True
            row.dc_id = getattr(getattr(session.client, "session", None), "dc_id", None)
            if req.catch_up:
                await session.catch_up()
                session.resync_needed.clear()
                row.caught_up = True
        except Exception as exc:
            row.error = f"{type(exc).__name__}: {exc}"
        out.append(row)
    if not out:
        ctx.warn("no accounts are connected; nothing to reconnect")
    return ReconnectResult(accounts=out)


SPEC_DAEMON_RECONNECT = OperationSpec(
    id="daemon.reconnect",
    request=DaemonReconnectReq,
    response=ReconnectResult,
    impl=daemon_reconnect,
    summary="Force a reconnect (and catch-up) for one or every account",
    mutating=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=180,
    columns=("accounts.alias", "accounts.reconnected", "accounts.caught_up", "accounts.error"),
    example={"accounts": [{"alias": "work", "reconnected": True, "caught_up": True}]},
    example_args="daemon reconnect",
    covers=("updates.ops-reconnect-health", "updates.sync-old-layer-socket-reset"),
    covers_partial=("updates.sync-new-session-triggers-diff",),
    coverage_note="the manual recovery; the automatic one runs in the supervisor.",
    tags=frozenset({"agent-safe"}),
)


class DaemonSaveStateReq(Request):
    pass


async def daemon_save_state(ctx: OpContext, req: DaemonSaveStateReq) -> SaveStateResult:
    """Flush pts/qts/seq and the entity cache to the session file now.

    Telethon persists only on `disconnect()`, so a SIGKILL'd daemon loses both
    the update state and the access hashes that make channel catch-up
    possible. The daemon does this on a timer; this is the manual trigger.
    """
    from tlgr.core import telethon_compat as compat

    daemon = _daemon(ctx)
    rows: list[SavedState] = []
    for alias in _spanned(ctx):
        session = daemon.sessions.get(alias)
        if session is None or session.client is None:
            rows.append(SavedState(alias=alias, error="not connected"))
            continue
        row = SavedState(alias=alias)
        try:
            await compat.save_state(session.client)
            state, channels = compat.session_state(session.client)
            row.pts = state.get("pts")
            row.qts = state.get("qts")
            row.seq = state.get("seq")
            row.date = state.get("date")
            row.channels = len(channels)
            row.entities = compat.entity_count(session.client)
        except Exception as exc:
            row.error = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    daemon.bus.flush_state()
    return SaveStateResult(accounts=rows)


SPEC_DAEMON_SAVE_STATE = OperationSpec(
    id="daemon.save-state",
    request=DaemonSaveStateReq,
    response=SaveStateResult,
    impl=daemon_save_state,
    summary="Flush update state and the entity cache to the session file now",
    description=(
        "Telethon writes the session only on a clean `disconnect()`. A "
        "SIGKILL therefore costs the `pts` progress *and* the cached access "
        "hashes — and a channel whose access hash is gone is silently skipped "
        "by the next catch-up."
    ),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=60,
    columns=("accounts.alias", "accounts.pts", "accounts.channels", "accounts.entities"),
    example={"accounts": [{"alias": "work", "pts": 91824, "channels": 12, "entities": 480}]},
    example_args="daemon save-state",
    covers=("updates.sync-peer-cache-from-updates",),
    covers_partial=("updates.session-persistence", "updates.sync-state-persistence"),
    coverage_note="flushes it on demand; the periodic flush is the session supervisor's.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Floods
# ---------------------------------------------------------------------------


def _flood_kind(method: str) -> str:
    lowered = method.lower()
    for needle, kind in (
        ("slowmode", "slowmode"),
        ("premium", "premium_wait"),
        ("peer_flood", "peer_flood"),
        ("takeout", "takeout_delay"),
    ):
        if needle in lowered:
            return kind
    return "flood_wait"


class FloodListReq(Request):
    include_expired: Annotated[
        bool, opt("--include-expired", help="Also show deadlines that have already passed.")
    ] = False


async def flood_list(ctx: OpContext, req: FloodListReq) -> Page[FloodRecord]:
    """The rate-limit deadlines this installation still owes.

    tlgr keeps its own persistent store keyed `(account, method, peer)`.
    Telethon remembers a `FloodWaitError` in memory and forgets it on exit, so
    v1 re-hit every wait after a restart — and re-hitting a wait is how a
    short one becomes a long one.
    """
    daemon = _daemon(ctx)
    rows: list[FloodRecord] = []
    aliases = _spanned(ctx) or [row.alias for row in daemon.accounts.list_accounts()]
    for alias in aliases:
        limiter = daemon.sessions.limiter(alias)
        for deadline in limiter.flood.entries(include_expired=req.include_expired):
            rows.append(
                FloodRecord(
                    account=alias,
                    kind=_flood_kind(deadline.method),
                    method=deadline.method,
                    chat=deadline.peer or None,
                    wait_seconds=deadline.remaining,
                    until=_stamp(deadline.until),
                    circuit_open=limiter.breaker.open,
                    expired=deadline.remaining == 0,
                )
            )
    rows.sort(key=lambda row: (-row.wait_seconds, row.account, row.method))
    limit = int(getattr(ctx, "limit", None) or 100)
    return build_page(rows[:limit], op="daemon.flood.list", kind=PageKind.LOCAL, has_more=False)


SPEC_FLOOD_LIST = OperationSpec(
    id="daemon.flood.list",
    request=FloodListReq,
    response=Page[FloodRecord],
    impl=flood_list,
    summary="List active rate-limit deadlines",
    aliases=("daemon.floods",),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("account", "kind", "method", "wait_seconds", "until", "circuit_open"),
    example={
        "items": [
            {
                "account": "work",
                "kind": "flood_wait",
                "method": "SendMessageRequest",
                "wait_seconds": 41,
                "until": "2026-09-03T09:20:00Z",
            }
        ],
        "has_more": False,
    },
    example_args="daemon flood list",
    covers=("updates.net-flood-wait",),
    tags=frozenset({"agent-safe"}),
)


class FloodClearReq(Request):
    method: Annotated[
        str | None, opt("--method", metavar="NAME", help="Only this request type.")
    ] = None
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="Only this peer.")
    ] = None
    everything: Annotated[
        bool, opt("--every", help="Clear every remembered deadline for the account.")
    ] = False


async def flood_clear(ctx: OpContext, req: FloodClearReq) -> FloodResult:
    """Forget remembered deadlines and close the circuit breaker.

    Clearing a *live* server-side FLOOD_WAIT does not lift it — the next call
    re-trips it, more expensively. This is for after the cause is fixed, or to
    reopen an account an operator has actually looked at.
    """
    daemon = _daemon(ctx)
    if not (req.method or req.chat or req.everything):
        raise UsageError("say what to clear: --method, --chat, or --every", field="method")
    cleared = 0
    touched: list[str] = []
    for alias in _spanned(ctx) or [row.alias for row in daemon.accounts.list_accounts()]:
        limiter = daemon.sessions.limiter(alias)
        peer = req.chat.raw if req.chat is not None else None
        cleared += (
            limiter.flood.forget(method=req.method or "", peer=peer)
            if not req.everything
            else _clear_all(limiter)
        )
        limiter.reset_breaker()
        touched.append(alias)
    if not cleared:
        ctx.mark_already()
    return FloodResult(cleared=cleared, circuit_open=False, accounts=touched)


def _clear_all(limiter: Any) -> int:
    count = len(limiter.flood.entries(include_expired=True))
    limiter.flood.clear()
    return count


SPEC_FLOOD_CLEAR = OperationSpec(
    id="daemon.flood.clear",
    request=FloodClearReq,
    response=FloodResult,
    impl=flood_clear,
    summary="Clear remembered rate-limit deadlines and reset the circuit breaker",
    description=(
        "Local memory only. Telegram's own wait is unaffected, so clearing a "
        "deadline that has not actually passed simply spends the next request "
        "learning that again."
    ),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=30,
    example={"cleared": 3, "circuit_open": False, "accounts": ["work"]},
    example_args="daemon flood clear --every",
    covers_partial=("updates.net-flood-wait",),
    coverage_note="the reset half; the accounting is `daemon flood list`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Dead letters
# ---------------------------------------------------------------------------


def _dead_letters(ctx: OpContext) -> tuple[Any, list[dict[str, Any]]]:
    daemon = _daemon(ctx)
    webhook = daemon.webhook
    return webhook, webhook.read_dead_letters()


def _dead_letter_model(index: int, entry: dict[str, Any]) -> DeadLetter:
    identifier = str(entry.get("delivery_id") or f"dl-{index}")
    return DeadLetter(
        id=identifier,
        seq=int(entry.get("seq") or 0),
        source=str(entry.get("source", "webhook")),
        event=str(entry.get("event", "")),
        account=str(entry.get("account", "")),
        attempts=int(entry.get("attempts") or 1),
        last_error=str(entry.get("reason", "")),
        first_failed_at=str(entry.get("first_failed_at") or entry.get("ts", "")),
        last_failed_at=str(entry.get("ts", "")),
    )


def _matches(entry: dict[str, Any], source: str, since: str | None, events: str | None) -> bool:
    if source != "all" and str(entry.get("source", "webhook")) != source:
        return False
    if since and str(entry.get("ts", "")) < since:
        return False
    if events:
        wanted = {part.strip() for part in events.split(",") if part.strip()}
        if str(entry.get("event", "")) not in wanted:
            return False
    return True


class DeadLetterListReq(Request):
    source: Annotated[str, choice("webhook", "job", "all", help="Which consumer failed.")] = "all"
    since: Annotated[
        str | None,
        opt("--since", metavar="WHEN", kind="datetime", help="Only entries after this time."),
    ] = None
    events: Annotated[
        str | None, opt("--events", metavar="TYPES", help="Filter by event type.")
    ] = None


async def dead_letter_list(ctx: OpContext, req: DeadLetterListReq) -> Page[DeadLetter]:
    """Events no consumer could be given.

    One store, shared by the webhook pusher and the gateway actions, at mode
    0600 and size-rotated. v1 appended full message text to a world-readable
    file that grew without limit (SEC-06).
    """
    _webhook, entries = _dead_letters(ctx)
    since = _iso(req.since)
    rows = [
        _dead_letter_model(index, entry)
        for index, entry in enumerate(entries)
        if _matches(entry, req.source, since, req.events)
    ]
    if ctx.account and ctx.account != "all":
        rows = [row for row in rows if row.account == ctx.account]
    limit = int(getattr(ctx, "limit", None) or 100)
    return build_page(
        rows[:limit],
        op="daemon.dead-letter.list",
        kind=PageKind.LOCAL,
        has_more=len(rows) > limit,
        total=len(rows),
    )


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    parsed = parse_dt(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else value


SPEC_DEAD_LETTER_LIST = OperationSpec(
    id="daemon.dead-letter.list",
    request=DeadLetterListReq,
    response=Page[DeadLetter],
    impl=dead_letter_list,
    summary="List events that could not be delivered",
    aliases=("webhook.dead-letter.list", "job.dead-letter.list"),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("id", "source", "event", "account", "attempts", "last_error", "last_failed_at"),
    example={
        "items": [
            {
                "id": "0f3c…",
                "source": "webhook",
                "event": "message_new",
                "account": "work",
                "attempts": 3,
                "last_error": "HTTP 502",
            }
        ],
        "has_more": False,
    },
    example_args="daemon dead-letter list",
    covers_partial=("updates.stream-webhook-delivery",),
    coverage_note="the failure store; delivery itself is the webhook pusher.",
    empty_exit=EXIT_EMPTY,
    tags=frozenset({"agent-safe"}),
)


class DeadLetterSendReq(Request):
    source: Annotated[str, choice("webhook", "job", "all", help="Which consumer to re-drive.")] = (
        "all"
    )
    id: Annotated[
        list[str], opt("--id", metavar="ID", help="Only these entries (repeatable).")
    ] = []
    since: Annotated[
        str | None, opt("--since", metavar="WHEN", kind="datetime", help="Only entries after this.")
    ] = None
    keep_on_success: Annotated[
        bool, opt("--keep-on-success", help="Do not remove entries that deliver.")
    ] = False
    url: Annotated[str | None, opt("--url", metavar="URL", help="Deliver to this URL instead.")] = (
        None
    )


async def dead_letter_send(ctx: OpContext, req: DeadLetterSendReq) -> DeadLetterResult:
    """Re-deliver what was dead-lettered, keeping the original delivery id.

    A receiver keyed on `Idempotency-Key` therefore sees a duplicate rather
    than a new event, which is what makes a drain safe to run twice.
    """
    webhook, entries = _dead_letters(ctx)
    since = _iso(req.since)
    wanted = set(req.id)
    remaining: list[dict[str, Any]] = []
    attempted = delivered = failed = 0

    for index, entry in enumerate(entries):
        identifier = str(entry.get("delivery_id") or f"dl-{index}")
        if wanted and identifier not in wanted:
            remaining.append(entry)
            continue
        if not _matches(entry, req.source, since, None):
            remaining.append(entry)
            continue
        attempted += 1
        ok, error = await webhook.deliver_once(entry, url=req.url or "")
        if ok:
            delivered += 1
            if req.keep_on_success:
                remaining.append(entry)
        else:
            failed += 1
            entry["reason"] = error
            entry["attempts"] = int(entry.get("attempts") or 1) + 1
            entry["ts"] = _now()
            remaining.append(entry)

    webhook.write_dead_letters(remaining)
    if attempted == 0:
        ctx.mark_already()
    return DeadLetterResult(
        attempted=attempted, delivered=delivered, failed=failed, remaining=len(remaining)
    )


SPEC_DEAD_LETTER_SEND = OperationSpec(
    id="daemon.dead-letter.send",
    request=DeadLetterSendReq,
    response=DeadLetterResult,
    impl=dead_letter_send,
    summary="Re-deliver dead-lettered events",
    aliases=(
        "webhook.dead-letter.drain",
        "job.dead-letter.drain",
        "daemon.dead-letter.drain",
    ),
    mutating=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=300,
    example={"attempted": 4, "delivered": 3, "failed": 1, "remaining": 1},
    example_args="daemon dead-letter send",
    covers_partial=("updates.stream-webhook-delivery",),
    coverage_note="the replay half; the live delivery path is the webhook pusher.",
    tags=frozenset({"agent-safe"}),
)


class DeadLetterDeleteReq(Request):
    source: Annotated[str, choice("webhook", "job", "all", help="Restrict by consumer.")] = "all"
    id: Annotated[
        list[str], opt("--id", metavar="ID", help="Only these entries (repeatable).")
    ] = []
    until: Annotated[
        str | None,
        opt("--until", metavar="WHEN", kind="datetime", help="Only entries older than this."),
    ] = None
    everything: Annotated[bool, opt("--every", help="Discard everything.")] = False


async def dead_letter_delete(ctx: OpContext, req: DeadLetterDeleteReq) -> DeadLetterResult:
    """Discard dead-lettered events permanently."""
    webhook, entries = _dead_letters(ctx)
    if not (req.id or req.until or req.everything):
        raise UsageError("say what to delete: --id, --until, or --every", field="id")
    until = _iso(req.until)
    wanted = set(req.id)
    remaining: list[dict[str, Any]] = []
    deleted = 0
    for index, entry in enumerate(entries):
        identifier = str(entry.get("delivery_id") or f"dl-{index}")
        drop = req.everything
        if wanted:
            drop = identifier in wanted
        elif until:
            drop = str(entry.get("ts", "")) < until
        if drop and _matches(entry, req.source, None, None):
            deleted += 1
            continue
        remaining.append(entry)
    webhook.write_dead_letters(remaining)
    if deleted == 0:
        ctx.mark_already()
    return DeadLetterResult(deleted=deleted, remaining=len(remaining))


SPEC_DEAD_LETTER_DELETE = OperationSpec(
    id="daemon.dead-letter.delete",
    request=DeadLetterDeleteReq,
    response=DeadLetterResult,
    impl=dead_letter_delete,
    summary="Permanently discard dead-lettered events",
    aliases=("webhook.dead-letter.clear", "job.dead-letter.clear"),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=60,
    example={"deleted": 12, "remaining": 0},
    example_args="daemon dead-letter delete --every",
    covers_partial=("updates.stream-webhook-delivery",),
    coverage_note="the disposal half; delivery is the webhook pusher's.",
    tags=frozenset({"agent-safe"}),
)
