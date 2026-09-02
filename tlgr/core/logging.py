"""Structured, rotating, redacted logs — configured exactly once.

Three v1 problems die here:

* **SEC-05/06.** The daemon logged message text, phone numbers and webhook
  tokens at INFO into a 0644 file that grew without bound. Logs are now JSON
  lines in a 0600 rotating file, and every record passes a redaction filter.
* **COR-40.** `logging.basicConfig` was called with two handlers and then
  called again by another entry point, so lines were duplicated and the file
  handler was attached twice. `setup_logging()` is idempotent: it owns the
  root logger's handlers and replaces them.
* Redaction is an **allow-list**. A blocklist of patterns over free-form
  message text is not a control — one Persian message with a phone number
  spelled in Eastern Arabic digits defeats it. Only the fields named in
  `SAFE_FIELDS` survive into the record's `extra`; everything else is dropped,
  and the message itself is scrubbed of the few high-value literals that do
  have a reliable shape (tokens, auth keys, `access_hash`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "SAFE_FIELDS",
    "RedactionFilter",
    "setup_logging",
]

#: The only keys allowed out of a log record's structured payload.
SAFE_FIELDS = frozenset(
    {
        "account",
        "op",
        "request_id",
        "elapsed_ms",
        "status",
        "code",
        "exit_code",
        "state",
        "reason",
        "seq",
        "chat_id",
        "peer_id",
        "count",
        "attempt",
        "pid",
        "uid",
        "path",
        "method",
        "rate_class",
        "wait_seconds",
        "event_type",
        "delivery_id",
        "alias",
        "version",
        "protocol",
    }
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\b(auth_?key|api_?hash|access_?hash|file_?reference)\b\s*[=:]\s*\S+"),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)\b(token|secret|password|passwd)\b\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    (re.compile(r"\+\d[\d\s\-()]{6,}\d"), "<phone>"),
)

_PLACEHOLDER = "<redacted>"


class RedactionFilter(logging.Filter):
    """Scrub a record in place. Never disabled by `--verbose`."""

    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.enabled:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken %-format is not our problem
            return True
        for pattern, replacement in _REDACTIONS:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with only allow-listed extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in SAFE_FIELDS and value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    log_file: Path | None,
    *,
    level: str = "info",
    stderr: bool = False,
    redact: bool = True,
    max_bytes: int = 8 * 1024 * 1024,
    backups: int = 5,
) -> logging.Logger:
    """Install the daemon's logging configuration, replacing anything present.

    Returns the root logger. Passing `log_file=None` gives stderr only, which
    is what `--foreground` in a test wants.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with contextlib.suppress(Exception):  # pragma: no cover
            handler.close()

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    redaction = RedactionFilter(redact)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create the file privately *before* the handler opens it: the handler
        # would otherwise create it with the process umask, and a daemon whose
        # umask was widened by its parent would leave a readable log.
        if not log_file.exists():
            log_file.touch(mode=0o600)
        else:
            with contextlib.suppress(OSError):  # pragma: no cover
                os.chmod(log_file, 0o600)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    if stderr or log_file is None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        stream.addFilter(redaction)
        root.addHandler(stream)

    # aiohttp's access log is off (SEC-05); its client session warnings are not
    # interesting at INFO either.
    logging.getLogger("aiohttp.access").disabled = True
    return root
