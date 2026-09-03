"""One confirmation path for every destructive operation.

v1 asked in some commands, ignored `--no-input` in others, and would block
forever on a prompt in a pipeline (COR-16). The rules here are absolute:

* on a TTY, a destructive op prompts unless `--yes`;
* off a TTY it *requires* `--yes` and otherwise fails with USAGE (exit 2);
* `--no-input` never prompts and never blocks, whatever the TTY says.
"""

from __future__ import annotations

import sys
from typing import Any

import click

from tlgr.core.errors import UsageError

__all__ = ["confirm"]


def _is_tty(stream: Any = None) -> bool:
    stream = stream or sys.stdin
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def confirm(
    prompt: str,
    *,
    force: bool = False,
    no_input: bool = False,
    tty: bool | None = None,
    hint: str = "",
) -> bool:
    """Ask, or decide without asking. Raises UsageError when it cannot ask."""
    if force:
        return True

    interactive = _is_tty() if tty is None else tty
    if no_input or not interactive:
        raise UsageError(
            f"{prompt} — refusing to continue without --yes"
            + (f" ({hint})" if hint else "")
            + (
                ". stdin is not a terminal, so there is nobody to ask."
                if not interactive
                else ". --no-input was given."
            ),
            field="yes",
        )
    return bool(click.confirm(prompt, default=False))
