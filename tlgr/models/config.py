"""Local configuration shapes: keys, effective values, paths, validation.

`config get`/`set` are about **this installation** — identity, transport,
proxy, flood budget, presence policy, event buffer. The server's own
configuration is a different noun (`config server`, `config app`,
`config info`) and a different model file (`models/net.py`), because reading
`message_length_max` off Telegram and setting `daemon.idle_timeout` on this
machine have nothing in common but the word "config".
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "ConfigEntry",
    "ConfigKey",
    "ConfigPaths",
    "ConfigValue",
    "InitResult",
    "ValidationIssue",
    "ValidationReport",
]


class ConfigKey(Model):
    """One documented knob, machine-readable so an agent need not read docs."""

    key: str
    type: str = "string"
    default: Any = None
    scope: str = "global"
    section: str = ""
    requires_restart: bool = False
    secret: bool = False
    help: str = ""
    choices: list[str] = []


class ConfigEntry(Model):
    """One key's effective value, and where it came from."""

    key: str
    value: Any = None
    default: Any = None
    source: str = "default"
    scope: str = "global"


class ConfigValue(Model):
    """The result of a `config get` / `set` / `unset`."""

    key: str
    value: Any = None
    previous: Any = None
    default: Any = None
    source: str = ""
    help: str = ""
    updated: bool = False
    removed: bool = False
    already: bool = False
    requires_restart: bool = False
    applied: bool = False


class ConfigPaths(Model):
    """Where everything lives. Session and secrets files are credentials."""

    config_dir: str = ""
    config: str = ""
    jobs: str = ""
    webhook: str = ""
    secrets: str = ""
    sessions: str = ""
    logs: str = ""
    socket: str = ""
    pid: str = ""
    dead_letter: str = ""
    path: str | None = None


class InitResult(Model):
    created: list[str] = []
    skipped: list[str] = []
    path: str = ""


class ValidationIssue(Model):
    file: str
    message: str
    key: str | None = None


class ValidationReport(Model):
    ok: bool = True
    valid: bool = True
    files: list[str] = []
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
