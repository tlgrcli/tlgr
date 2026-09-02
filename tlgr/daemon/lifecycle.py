"""Daemon lifecycle management: fork, PID file, signal handling."""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
from pathlib import Path

from tlgr.core.config import get_logs_dir, get_pid_path, get_socket_path

log = logging.getLogger("tlgr.daemon")


def write_pid(base: Path | None = None) -> None:
    pid_path = get_pid_path(base)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))
    atexit.register(_cleanup, base)


def _cleanup(base: Path | None = None) -> None:
    get_pid_path(base).unlink(missing_ok=True)
    get_socket_path(base).unlink(missing_ok=True)


def read_pid(base: Path | None = None) -> int | None:
    pid_path = get_pid_path(base)
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pid_path.unlink(missing_ok=True)
        return None


def daemonize(base: Path | None = None) -> None:
    """Double-fork to background and redirect stdio."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    os.umask(0)
    if os.fork() > 0:
        sys.exit(0)

    sys.stdin.close()
    logs_dir = get_logs_dir(base)
    log_file = logs_dir / "daemon.log"

    fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, sys.stdout.fileno())
    os.dup2(fd, sys.stderr.fileno())
    os.close(fd)


def setup_logging(base: Path | None = None, level: str = "info") -> None:
    logs_dir = get_logs_dir(base)
    log_file = logs_dir / "daemon.log"

    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_file)),
            logging.StreamHandler(sys.stderr),
        ],
    )


def stop_daemon(base: Path | None = None) -> bool:
    """Send SIGTERM to the daemon. Returns True if signal was sent."""
    pid = read_pid(base)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        get_pid_path(base).unlink(missing_ok=True)
        return False
