"""Daemonising, pid files, signals — the parts that must happen in order.

Three v1 bugs are fixed by ordering alone:

* **`os.umask(0)` inside `daemonize()`** made every file the daemon created
  afterwards world-writable, including the log and the socket. The umask is
  now `0o077`, set in `main()` *before* anything creates a file (SEC-01).
* **the log was opened 0644** and `basicConfig` was called with two handlers
  from two entry points, so lines were duplicated (COR-40). Logging is now
  configured once, by `core/logging.py`, into a 0600 rotating file.
* **`atexit` unlinked the socket and pid file unconditionally**, so a second
  daemon that exited because one was already running deleted the *running*
  one's files on the way out. Cleanup now only removes files this process
  wrote (COR-14).
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from tlgr.core.logging import setup_logging as _setup_logging
from tlgr.core.paths import TlgrPaths, write_private

log = logging.getLogger("tlgr.daemon")

__all__ = [
    "daemonize",
    "read_pid",
    "remove_pid",
    "setup_logging",
    "stop_daemon",
    "write_pid",
]


def write_pid(base: Path | None = None) -> None:
    """Write our pid, and arrange to remove *only our own* file at exit."""
    paths = TlgrPaths(base)
    pid = os.getpid()
    write_private(paths.pid, f"{pid}\n")

    def cleanup() -> None:
        # Re-read before deleting: if another daemon took over the file, the
        # pid in it is not ours and the file is not ours to remove.
        with contextlib.suppress(OSError, ValueError):
            if int(paths.pid.read_text().strip()) == pid:
                paths.pid.unlink(missing_ok=True)

    atexit.register(cleanup)


def remove_pid(base: Path | None = None) -> None:
    paths = TlgrPaths(base)
    with contextlib.suppress(OSError, ValueError):
        if int(paths.pid.read_text().strip()) == os.getpid():
            paths.pid.unlink(missing_ok=True)


def read_pid(base: Path | None = None) -> int | None:
    """The pid of a live daemon, or None. Never deletes anything.

    v1 deleted the pid file whenever `kill(pid, 0)` failed — including on
    `PermissionError`, which means the process exists and belongs to someone
    else. Reading is now a read (COR-14c).
    """
    paths = TlgrPaths(base)
    try:
        pid = int(paths.pid.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def daemonize(base: Path | None = None) -> None:
    """Double-fork into the background, with stdio pointed at the log.

    The umask is *not* touched here: `main()` sets `0o077` before anything is
    created, and resetting it in the middle of daemonising is how v1 ended up
    with a world-writable socket.
    """
    paths = TlgrPaths(base)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    paths.ensure_logs()
    log_file = paths.log_file
    if not log_file.exists():
        log_file.touch(mode=0o600)

    with contextlib.suppress(OSError):
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, sys.stdin.fileno())
        os.close(devnull)

    fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, sys.stdout.fileno())
    os.dup2(fd, sys.stderr.fileno())
    os.close(fd)


def setup_logging(
    base: Path | None = None, level: str = "info", *, foreground: bool = False
) -> None:
    paths = TlgrPaths(base)
    paths.ensure_logs()
    _setup_logging(paths.log_file, level=level, stderr=foreground)


def stop_daemon(base: Path | None = None) -> bool:
    """SIGTERM the daemon. True when the signal was delivered."""
    pid = read_pid(base)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        log.warning("the daemon (pid %s) belongs to another user; not signalling it", pid)
        return False
