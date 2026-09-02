"""Turning an exception into output and an exit status, once.

Two rules from §9:

* in JSON mode the error object goes to **stdout**, so an agent parsing
  stdout always gets JSON, and a one-line summary goes to stderr;
* a Click usage error is formatted the same way as any other error in JSON
  mode — v1 let Click print its own English to stderr and exited 2 with no
  JSON at all, which is UX-02.
"""

from __future__ import annotations

import sys
from typing import Any

import click

from tlgr.core.errors import (
    EXIT_CANCELLED,
    EXIT_USAGE,
    classify,
    error_body_dict,
    exit_code_for,
)

__all__ = ["emit", "exit_status", "handle"]


def _usage_body(exc: click.UsageError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": "USAGE",
        "message": exc.format_message(),
        "error": exc.format_message(),
        "exit_code": EXIT_USAGE,
    }
    if exc.ctx is not None:
        body["usage"] = exc.ctx.get_usage()
        body["command"] = exc.ctx.command_path
    parameter = getattr(exc, "param", None)
    if parameter is not None and getattr(parameter, "name", None):
        body["field"] = parameter.name
    return body


def body_for(exc: BaseException) -> dict[str, Any]:
    """The error object printed in JSON mode."""
    if isinstance(exc, click.UsageError):
        return _usage_body(exc)
    return error_body_dict(classify(exc))


def exit_status(exc: BaseException) -> int:
    if isinstance(exc, KeyboardInterrupt):
        return EXIT_CANCELLED
    if isinstance(exc, click.UsageError):
        return exc.exit_code or EXIT_USAGE
    return exit_code_for(exc) if hasattr(exc, "exit_code") else classify(exc).exit_code


def emit(exc: BaseException, *, use_json: bool = False, op: str = "") -> None:
    """Write the error out: JSON to stdout when asked, a line to stderr always."""
    body = body_for(exc)
    if use_json:
        import json

        envelope: dict[str, Any] = {"ok": False, "error": body}
        if op:
            envelope["op"] = op
        json.dump(envelope, sys.stdout, default=str, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.stdout.flush()

    message = body.get("message") or str(exc)
    hint = body.get("hint") or getattr(exc, "hint", "")
    print(f"Error: {message}", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)


def handle(exc: BaseException, *, use_json: bool = False, op: str = "") -> int:
    """Emit *exc* and return the status the process should exit with."""
    emit(exc, use_json=use_json, op=op)
    return exit_status(exc)
