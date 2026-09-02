"""Starting the daemon exactly once, whoever asks first (§5.8, COR-14).

v1's version of this file is the reason two `tlgr` commands in a shell
pipeline could produce two daemons, and the reason a daemon owned by another
uid was reported as "not running" and then had its pid file deleted. Four
rules, each of which was broken:

1. **The probe is a lock, not a file test.** `daemon.pid` existing says
   nothing; the process may be gone, or may belong to someone else. Spawning
   is serialised by an exclusive `flock`, so twenty simultaneous CLIs produce
   one daemon.
2. **Readiness is an HTTP 200 from `/v1/status`,** not the socket file
   appearing. The socket exists from `bind()`, which is before any account is
   connected and before the middleware is installed.
3. **Nothing unlinks another process's socket or pid file.** A stale socket is
   removed only while holding the spawn lock and only once the pid it belongs
   to is shown to be gone.
4. **`PermissionError` is not "not running".** `os.kill(pid, 0)` raising
   `EPERM` means the process exists and belongs to somebody else — the one
   case where deleting its files is worst.

The spawn lock is a *separate* file from the daemon's own singleton lock. They
cannot be the same file: the parent holds the spawn lock across the spawn, and
the child takes the singleton lock as its first act, so sharing one file would
deadlock every start.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tlgr.core.errors import DaemonError, DaemonNotRunningError, DaemonVersionMismatchError
from tlgr.core.paths import TlgrPaths

__all__ = [
    "SPAWN_LOCK",
    "DaemonState",
    "daemon_state",
    "is_listening",
    "read_pid",
    "spawn_daemon",
    "wait_ready",
]

SPAWN_LOCK = "daemon.spawn.lock"

_POLL_MIN = 0.02
_POLL_MAX = 0.25


@dataclass(frozen=True)
class DaemonState:
    """What `daemon.state` says, when it says anything."""

    version: str = ""
    protocol: int = 0
    pid: int = 0
    socket: str = ""
    managed_by: str = ""
    started_at: str = ""

    @property
    def supervised(self) -> bool:
        """True when launchd/systemd owns the process, so we must not fork one."""
        return self.managed_by in ("launchd", "systemd")


def daemon_state(paths: TlgrPaths) -> DaemonState:
    try:
        raw = json.loads(paths.state.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return DaemonState()
    if not isinstance(raw, dict):
        return DaemonState()
    return DaemonState(
        version=str(raw.get("version", "")),
        protocol=int(raw.get("protocol", 0) or 0),
        pid=int(raw.get("pid", 0) or 0),
        socket=str(raw.get("socket", "")),
        managed_by=str(raw.get("managed_by", "")),
        started_at=str(raw.get("started_at", "")),
    )


def read_pid(paths: TlgrPaths) -> int | None:
    """The daemon's pid if a live process holds it, else None.

    A `PermissionError` from `kill(pid, 0)` means the pid is *taken* by a
    process we may not signal. Reporting that as "not running" (v1) led
    straight to deleting a running daemon's pid file.
    """
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


def is_listening(socket_path: Path, timeout: float = 0.5) -> bool:
    """Can we open the socket at all? Distinguishes 'gone' from 'refusing'."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except PermissionError:
        # Someone else's daemon, or a socket we may not open. Either way it is
        # running: refusing loudly beats replacing it.
        return True
    except OSError as exc:
        # ENOTSOCK is a leftover *file* at the socket path — a crash during
        # bind, or a copied home directory. Anything else unexpected is
        # treated as "running": refusing loudly beats replacing a live daemon.
        return exc.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK)
    finally:
        sock.close()


def _remove_stale_socket(paths: TlgrPaths) -> None:
    """Remove a socket whose owner is provably gone. Only under the spawn lock."""
    if not paths.socket.exists():
        return
    if is_listening(paths.socket):
        return
    if read_pid(paths) is not None:
        # A live process owns the pid file: its socket is not ours to remove,
        # even when it is not answering yet.
        return
    with contextlib.suppress(OSError):
        paths.socket.unlink()


def spawn_daemon(base: Path, *, python: str | None = None) -> subprocess.Popen[bytes]:
    """Fork the daemon into its own session, detached from this terminal."""
    return subprocess.Popen(
        [python or sys.executable, "-m", "tlgr.daemon.main", "--base", str(base)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_ready(
    probe: Any,
    *,
    timeout: float,
    deadline_message: str = "",
) -> dict[str, Any] | None:
    """Poll *probe* (a zero-argument callable returning the status dict or None).

    Backs off from 20 ms to 250 ms so a fast start costs one poll and a slow
    one does not spin a core.
    """
    end = time.monotonic() + timeout
    wait = _POLL_MIN
    while True:
        result = probe()
        if result is not None:
            return result
        if time.monotonic() >= end:
            return None
        time.sleep(min(wait, max(0.0, end - time.monotonic())))
        wait = min(wait * 1.5, _POLL_MAX)


def log_tail(paths: TlgrPaths, lines: int = 20) -> str:
    """The last few log lines, for a start-up failure message."""
    try:
        content = paths.log_file.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


class SpawnLock:
    """`flock` around the decision to spawn. Context manager, non-blocking."""

    def __init__(self, base: Path) -> None:
        self.path = base / SPAWN_LOCK
        self._fd: int | None = None
        self.acquired = False

    def __enter__(self) -> SpawnLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            self.acquired = False
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            if self.acquired:
                with contextlib.suppress(OSError):  # pragma: no cover
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def ensure_running(
    paths: TlgrPaths,
    status_probe: Any,
    *,
    auto_start: bool,
    start_timeout: float,
    python: str | None = None,
) -> dict[str, Any]:
    """Return the daemon's `/v1/status`, starting it if that is allowed.

    `status_probe()` returns the decoded status dict or `None` when the daemon
    is not answering. Everything here is about *not* starting a second one.
    """
    current = status_probe()
    if current is not None:
        return current

    state = daemon_state(paths)
    if state.supervised:
        raise DaemonNotRunningError(
            f"the daemon is managed by {state.managed_by} and is not answering; "
            f"start it with: tlgr daemon restart"
        )

    if not auto_start:
        raise DaemonNotRunningError(
            "Daemon is not running and [daemon] auto_start is false. "
            "Start it with: tlgr daemon start"
        )

    with SpawnLock(paths.base) as lock:
        # Whoever lost the race polls; whoever won double-checks before
        # spawning, because the winner may have been queued behind a start
        # that has just finished.
        current = status_probe()
        if current is not None:
            return current
        if lock.acquired:
            _remove_stale_socket(paths)
            spawn_daemon(paths.base, python=python)
        ready = wait_ready(status_probe, timeout=start_timeout)

    if ready is None:
        tail = log_tail(paths)
        detail = f"\nLast lines of {paths.log_file}:\n{tail}" if tail else ""
        raise DaemonError(f"daemon did not become ready within {start_timeout:g}s{detail}")
    return ready


def check_protocol(status: dict[str, Any], *, client_protocol: int) -> int:
    """Compare protocols and say what to do (§5.7).

    Returns 0 to proceed, -1 when the daemon is older and must be restarted,
    and raises when it is newer — killing a newer daemon to satisfy an older
    CLI would break whatever started it.
    """
    daemon = status.get("daemon") or {}
    protocol = int(daemon.get("protocol", 0) or 0)
    if protocol == client_protocol:
        return 0
    if protocol > client_protocol:
        raise DaemonVersionMismatchError(
            f"the running daemon speaks protocol {protocol}, this CLI speaks "
            f"{client_protocol}; upgrade the CLI, or run: tlgr daemon stop"
        )
    return -1
