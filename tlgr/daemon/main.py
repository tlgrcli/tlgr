"""The daemon entry point, in the order §6.1 requires.

The order is the design. `umask` before anything creates a file; the singleton
lock before any work, so two simultaneous starts cannot both proceed; the
permission audit before a session file is opened; the socket bound and
answering `ready: false` before the first account connect, so a CLI waiting to
start us learns we exist in milliseconds rather than in however long a
connect takes.

A second daemon exits **0**, not 1. Under launchd with
`KeepAlive.SuccessfulExit=false`, exiting non-zero means "restart me", so v1's
`sys.exit(1)` on "already running" produced an infinite respawn loop (COR-39).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from tlgr.core.config import load_app_config
from tlgr.core.errors import ConfigurationError
from tlgr.core.paths import TlgrPaths, require_safe_permissions
from tlgr.daemon.app import Daemon
from tlgr.daemon.lifecycle import daemonize, setup_logging, write_pid
from tlgr.daemon.singleton import FileLock, LockBusy

log = logging.getLogger("tlgr.daemon")

__all__ = ["main", "run"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tlgr-daemon", description="the tlgr daemon")
    parser.add_argument("--base", default="", help="tlgr home directory (default ~/.tlgr)")
    parser.add_argument("--foreground", action="store_true", help="do not fork; log to stderr too")
    parser.add_argument("--log-level", default="", help="debug | info | warning | error")
    return parser.parse_args(argv)


async def run(daemon: Daemon) -> None:
    """Install signal handlers and run until told to stop."""
    loop = asyncio.get_running_loop()
    # A task with no reference can be garbage-collected mid-flight, which is
    # COR-41: a SIGHUP reload that vanished halfway through.
    pending: set[asyncio.Task[None]] = set()

    def request_stop() -> None:
        log.info("shutdown requested")
        daemon.request_shutdown()

    def request_reload() -> None:
        log.info("reload requested (SIGHUP)")
        task = loop.create_task(_reload(daemon))
        pending.add(task)
        task.add_done_callback(pending.discard)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_stop)
    with contextlib.suppress(NotImplementedError, AttributeError):
        loop.add_signal_handler(signal.SIGHUP, request_reload)

    await daemon.run()


async def _reload(daemon: Daemon) -> None:
    from tlgr.daemon.app import _reload as reload_config

    try:
        await reload_config(daemon, ["config", "jobs", "policy"])
    except Exception as exc:  # pragma: no cover - a bad config must not kill us
        log.error("reload failed, keeping the running configuration: %s", exc)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # SEC-01: before anything creates a file. A daemon started from a shell
    # with a permissive umask would otherwise write a world-readable session,
    # log and socket, and fixing the modes afterwards leaves a window.
    os.umask(0o077)

    base = Path(args.base).expanduser() if args.base else None
    paths = TlgrPaths(base)
    paths.ensure_base()

    try:
        config = load_app_config(paths.base)
    except ConfigurationError as exc:
        print(f"tlgr daemon: {exc}", file=sys.stderr)
        return 10

    lock = FileLock(paths.lock)
    try:
        lock.acquire()
    except LockBusy as exc:
        # Exit 0: this is the expected outcome of racing a start, and a
        # non-zero exit tells launchd to respawn us forever (COR-39).
        print(f"tlgr daemon: already running{f' (pid {exc.pid})' if exc.pid else ''}")
        return 0

    level = args.log_level or config.daemon.log_level
    if not args.foreground:
        daemonize(paths.base)
        # The pid written before the fork belongs to a process that no longer
        # exists; the lock survives the fork, its recorded pid must too.
        lock.write_pid()
    setup_logging(paths.base, level, foreground=args.foreground)
    write_pid(paths.base)

    try:
        require_safe_permissions(paths.base)
    except ConfigurationError as exc:
        log.error("%s", exc)
        print(f"tlgr daemon: {exc}", file=sys.stderr)
        lock.release()
        return 10

    managed_by = _managed_by()
    daemon = Daemon(paths.base, config=config, managed_by=managed_by)
    log.info(
        "daemon starting",
        extra={"pid": os.getpid(), "version": __import__("tlgr").__version__},
    )
    try:
        asyncio.run(run(daemon))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        lock.release()
    return 0


def _managed_by() -> str:
    """Whether a supervisor started us, which changes the idle policy (§6.12)."""
    if os.environ.get("INVOCATION_ID"):
        return "systemd"
    if os.environ.get("XPC_SERVICE_NAME", "").startswith("dev.tlgr"):
        return "launchd"
    return ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
