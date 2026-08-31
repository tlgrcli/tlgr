"""CLI-side IPC client that talks to the daemon via Unix socket."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tlgr.core.config import get_socket_path, get_pid_path, CONFIG_DIR, load_app_config
from tlgr.core.errors import (
    DaemonNotRunningError,
    DaemonError,
    IPCError,
    RateLimitError,
    SpamFlagError,
)


def _daemon_is_running(base: Path | None = None) -> int | None:
    """Return daemon PID if running, else None."""
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


def _auto_start_daemon(base: Path | None = None) -> None:
    """Fork and start the daemon in background with retry."""
    max_retries = 2
    for attempt in range(max_retries + 1):
        proc = subprocess.Popen(
            [sys.executable, "-m", "tlgr.daemon.server", "--base", str(base or CONFIG_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        sock_path = get_socket_path(base)
        wait = 0.1
        for _ in range(50):
            time.sleep(wait)
            if sock_path.exists():
                return
            wait = min(wait * 1.3, 0.5)
        if attempt < max_retries:
            pid_path = get_pid_path(base)
            pid_path.unlink(missing_ok=True)
            continue
    raise DaemonError(
        "Daemon did not start after retries. "
        "Check logs with: tlgr daemon logs"
    )


def _ensure_daemon(base: Path | None = None) -> None:
    """Ensure daemon is running; auto-start if configured."""
    if _daemon_is_running(base):
        return
    cfg = load_app_config(base)
    if cfg.daemon.auto_start:
        _auto_start_daemon(base)
    else:
        raise DaemonNotRunningError(
            "Daemon is not running. Start it with 'tlgr daemon start'."
        )


def ipc_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    base: Path | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    """Send an HTTP request to the daemon via Unix socket.

    Uses raw socket to avoid requiring aiohttp on the client side.
    """
    import socket as sock_mod

    _ensure_daemon(base)
    sock_path = get_socket_path(base)

    s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
    except (ConnectionRefusedError, FileNotFoundError) as e:
        s.close()
        raise DaemonNotRunningError(f"Cannot connect to daemon: {e}")

    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body, default=str).encode()

    request_line = f"{method} {path} HTTP/1.1\r\n"
    headers = f"Host: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n"
    s.sendall((request_line + headers).encode() + body_bytes)

    # Read response
    chunks: list[bytes] = []
    while True:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        except sock_mod.timeout:
            break
    s.close()

    raw = b"".join(chunks).decode("utf-8", errors="replace")

    # Parse HTTP response
    if "\r\n\r\n" not in raw:
        raise IPCError(f"Malformed daemon response")

    header_part, body_part = raw.split("\r\n\r\n", 1)
    status_line = header_part.split("\r\n")[0]
    # e.g. "HTTP/1.1 200 OK"
    parts = status_line.split(" ", 2)
    status_code = int(parts[1]) if len(parts) >= 2 else 500

    # Handle chunked transfer encoding
    if "transfer-encoding: chunked" in header_part.lower():
        body_part = _decode_chunked(body_part)

    try:
        result = json.loads(body_part)
    except json.JSONDecodeError:
        if status_code >= 400:
            raise IPCError(f"Daemon error ({status_code}): {body_part[:200]}")
        result = {"raw": body_part}

    if status_code >= 400:
        error_msg = result.get("error", f"Daemon returned {status_code}")
        if result.get("code") == "RATE_LIMITED":
            raise RateLimitError(error_msg, wait_seconds=result.get("wait_seconds", 0))
        if result.get("code") in ("PEER_FLOOD", "ACCOUNT_FROZEN"):
            raise SpamFlagError(error_msg, code=result["code"])
        raise IPCError(error_msg)

    return result


def _decode_chunked(data: str) -> str:
    """Decode HTTP chunked transfer encoding."""
    result: list[str] = []
    while data:
        # Find chunk size line
        nl = data.find("\r\n")
        if nl == -1:
            break
        size_str = data[:nl].strip()
        if not size_str:
            data = data[nl + 2:]
            continue
        try:
            size = int(size_str, 16)
        except ValueError:
            break
        if size == 0:
            break
        data = data[nl + 2:]
        result.append(data[:size])
        data = data[size + 2:]
    return "".join(result)
