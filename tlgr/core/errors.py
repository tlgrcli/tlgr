"""Error types, stable exit codes, and JSON error formatting for tlgr."""

from __future__ import annotations

import json
import sys
from typing import Any

# Stable exit codes for automation/agent consumption.
EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_EMPTY = 3
EXIT_AUTH = 4
EXIT_NOT_FOUND = 5
EXIT_PERMISSION = 6
EXIT_RATE_LIMITED = 7
EXIT_RETRYABLE = 8
EXIT_SPAM_FLAGGED = 9
EXIT_CONFIG = 10
EXIT_DAEMON = 11
EXIT_IPC = 12
EXIT_INDETERMINATE = 13
EXIT_CANCELLED = 130

EXIT_CODE_MAP: dict[str, dict[str, Any]] = {
    "SUCCESS": {"code": EXIT_SUCCESS, "description": "Success"},
    "GENERIC": {"code": EXIT_GENERIC, "description": "Generic failure"},
    "USAGE": {"code": EXIT_USAGE, "description": "Usage or parse error"},
    "EMPTY": {"code": EXIT_EMPTY, "description": "Empty results"},
    "AUTH_ERROR": {"code": EXIT_AUTH, "description": "Authentication required"},
    "SESSION_ERROR": {"code": EXIT_AUTH, "description": "Session error (re-auth needed)"},
    "CHAT_NOT_FOUND": {"code": EXIT_NOT_FOUND, "description": "Chat or entity not found"},
    "PERMISSION_DENIED": {"code": EXIT_PERMISSION, "description": "Permission denied"},
    "RATE_LIMITED": {"code": EXIT_RATE_LIMITED, "description": "Rate limited (retry later)"},
    "RETRYABLE": {"code": EXIT_RETRYABLE, "description": "Transient/retryable error"},
    "PEER_FLOOD": {
        "code": EXIT_SPAM_FLAGGED,
        "description": "Account spam-flagged for messaging strangers (PeerFlood) — stop sending",
    },
    "ACCOUNT_FROZEN": {
        "code": EXIT_SPAM_FLAGGED,
        "description": "Account frozen/restricted by Telegram — stop sending",
    },
    "CONFIG_ERROR": {"code": EXIT_CONFIG, "description": "Configuration error"},
    "DAEMON_ERROR": {"code": EXIT_DAEMON, "description": "Daemon error"},
    "DAEMON_NOT_RUNNING": {"code": EXIT_DAEMON, "description": "Daemon is not running"},
    "IPC_ERROR": {"code": EXIT_IPC, "description": "IPC communication error"},
    "INDETERMINATE": {
        "code": EXIT_INDETERMINATE,
        "description": (
            "Question could not be answered authoritatively — treat as unknown, "
            "never as a negative"
        ),
    },
    "CANCELLED": {"code": EXIT_CANCELLED, "description": "Interrupted (SIGINT)"},
}


class TlgrError(Exception):
    """Base error for all tlgr errors."""

    code: str = "TLGR_ERROR"
    exit_code: int = EXIT_GENERIC
    hint: str = ""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class AuthenticationError(TlgrError):
    code = "AUTH_ERROR"
    exit_code = EXIT_AUTH
    hint = "Run: tlgr account add <phone>"


class SessionError(TlgrError):
    code = "SESSION_ERROR"
    exit_code = EXIT_AUTH
    hint = "Session expired. Run: tlgr account add <phone>"


class ConfigurationError(TlgrError):
    code = "CONFIG_ERROR"
    exit_code = EXIT_CONFIG
    hint = "Run: tlgr config init"


class ChatNotFoundError(TlgrError):
    code = "CHAT_NOT_FOUND"
    exit_code = EXIT_NOT_FOUND
    hint = "Run: tlgr chat list  to find available chats"


class PermissionError_(TlgrError):
    code = "PERMISSION_DENIED"
    exit_code = EXIT_PERMISSION


class RateLimitError(TlgrError):
    code = "RATE_LIMITED"
    exit_code = EXIT_RATE_LIMITED

    def __init__(self, message: str, wait_seconds: int = 0):
        super().__init__(message, code="RATE_LIMITED")
        self.wait_seconds = wait_seconds
        if wait_seconds:
            self.hint = f"Rate limited. Retry after {wait_seconds}s"


class SpamFlagError(TlgrError):
    """Telegram has restricted this account from messaging strangers.

    Distinct from RateLimitError: a FloodWait clears after `wait_seconds`,
    whereas PeerFlood/FROZEN is an account-level spam flag with no advertised
    expiry. Callers running outreach must stop ALL outgoing traffic for the
    account rather than back off and retry.
    """

    code = "PEER_FLOOD"
    exit_code = EXIT_SPAM_FLAGGED
    hint = "Account is spam-flagged. Stop sending from it and let it rest."


class DaemonError(TlgrError):
    code = "DAEMON_ERROR"
    exit_code = EXIT_DAEMON
    hint = "Run: tlgr daemon start"


class DaemonNotRunningError(DaemonError):
    code = "DAEMON_NOT_RUNNING"
    exit_code = EXIT_DAEMON
    hint = "Daemon is not running. Start it with: tlgr daemon start"


class IPCError(TlgrError):
    code = "IPC_ERROR"
    exit_code = EXIT_IPC
    hint = "Check daemon status with: tlgr daemon status"


def exit_code_for(error: Exception) -> int:
    """Return the stable exit code for an error."""
    if isinstance(error, TlgrError):
        return error.exit_code
    return EXIT_GENERIC


def format_error_json(error: Exception) -> dict[str, Any]:
    """Format an error as a JSON-serializable dict."""
    code = getattr(error, "code", "UNKNOWN_ERROR")
    result: dict[str, Any] = {
        "error": str(error),
        "code": code,
        "exit_code": exit_code_for(error),
    }
    if isinstance(error, RateLimitError) and error.wait_seconds:
        result["wait_seconds"] = error.wait_seconds
    return result


def emit_error(error: Exception, use_json: bool = False) -> None:
    """Emit an error to stderr (human) and optionally stdout (JSON)."""
    if use_json:
        json.dump(format_error_json(error), sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()

    hint = getattr(error, "hint", "")
    if hint:
        print(f"Error: {error}\n  {hint}", file=sys.stderr)
    else:
        print(f"Error: {error}", file=sys.stderr)
