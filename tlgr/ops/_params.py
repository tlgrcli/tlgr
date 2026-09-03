"""The annotation vocabulary a request field uses to describe its CLI shape.

Fields are declared as `Annotated[T, arg(...)]` or `Annotated[T, opt(...)]`.
Both produce a single `msgspec.Meta` whose `extra["cli"]` the generator reads
through `msgspec.inspect.type_info()`, so one declaration drives the Click
parameter, the JSON Schema and the reference docs.

**Exactly one Meta per field.** `type_info` surfaces only the first `Meta`'s
`extra` and `description`, so stacking `Annotated[int, opt(...), Meta(ge=1)]`
silently loses the second one's help text. `arg()`/`opt()` therefore take the
constraint keywords themselves and emit one merged `Meta`; registry lint L14
rejects a field that carries two.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec

from tlgr.core.errors import UsageError
from tlgr.core.timefmt import parse_dt, parse_duration
from tlgr.models.peer import parse_message_link, parse_peer_ref, parse_user_ref

__all__ = [
    "CLI_KEY",
    "ParamKind",
    "arg",
    "choice",
    "cli_meta",
    "opt",
    "parse_dt",
    "parse_duration",
    "parse_message_link",
    "parse_peer_ref",
    "parse_user_ref",
    "read_secret",
    "secret_flags",
]

CLI_KEY = "cli"

ParamKind = Literal["", "peer", "user", "msg_id", "duration", "datetime", "json", "path"]

_CONSTRAINTS = (
    "ge",
    "gt",
    "le",
    "lt",
    "multiple_of",
    "pattern",
    "min_length",
    "max_length",
    "tz",
)


#: Keys whose falsy value carries meaning. `required=False` is the whole
#: point of an optional positional, and dropping it would default it back to
#: True; `pos=0` survives the filter only because 0 != "" and 0 is not False.
_ALWAYS = frozenset({"pos", "required"})


def _is_empty(value: Any) -> bool:
    """Drop defaults from the stored metadata, without confusing 0 with False."""
    if value is None or value == "":
        return True
    return value is False or value == []


def _meta(cli: dict[str, Any], help: str, constraints: dict[str, Any]) -> msgspec.Meta:
    unknown = set(constraints) - set(_CONSTRAINTS)
    if unknown:
        raise TypeError(f"unknown constraint(s) for a request field: {sorted(unknown)}")
    return msgspec.Meta(
        description=help or None,
        extra={CLI_KEY: {k: v for k, v in cli.items() if k in _ALWAYS or not _is_empty(v)}},
        **constraints,
    )


def arg(
    pos: int,
    *,
    metavar: str = "",
    required: bool = True,
    variadic: bool = False,
    help: str = "",
    kind: ParamKind = "",
    **constraints: Any,
) -> msgspec.Meta:
    """Declare the field as a positional argument at index *pos*."""
    return _meta(
        {
            "role": "arg",
            "pos": pos,
            "metavar": metavar,
            "required": required,
            "variadic": variadic,
            "kind": kind,
        },
        help,
        constraints,
    )


def opt(
    *flags: str,
    help: str = "",
    metavar: str = "",
    envvar: str = "",
    hidden: bool = False,
    secret: bool = False,
    count: bool = False,
    kind: ParamKind = "",
    **constraints: Any,
) -> msgspec.Meta:
    """Declare the field as an option.

    Without *flags* the name is derived from the field
    (`reply_to` → `--reply-to`). Give flags to override, to add a short form,
    or to spell the negative half of a paired boolean (`--keep/--no-keep`).
    """
    return _meta(
        {
            "role": "opt",
            "flags": list(flags),
            "metavar": metavar,
            "envvar": envvar,
            "hidden": hidden,
            "secret": secret,
            "count": count,
            "kind": kind,
        },
        help,
        constraints,
    )


def choice(*values: str, help: str = "", metavar: str = "", **constraints: Any) -> msgspec.Meta:
    """An option restricted to *values*, shown in --help and in the schema."""
    return _meta(
        {"role": "opt", "flags": [], "metavar": metavar, "choices": list(values)},
        help,
        constraints,
    )


def cli_meta(node: Any) -> dict[str, Any]:
    """The `cli` dict off a field, or `{}` when the field is unannotated.

    Accepts either a `msgspec.Meta` or the `msgspec.inspect.Metadata` node the
    generator actually walks; both carry `.extra`, and asking for one specific
    type here would force every caller to convert.
    """
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict):
        return {}
    value = extra.get(CLI_KEY)
    return dict(value) if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def secret_flags(base: str) -> tuple[str, str, str]:
    """The three flags a secret field generates: `--x-env/--x-stdin/--x-file`.

    A secret never gets a value-taking flag, because argv is world-readable
    through `ps` and ends up in shell history (STYLE §3).
    """
    return (f"--{base}-env", f"--{base}-stdin", f"--{base}-file")


def read_secret(
    name: str,
    *,
    env: str | None = None,
    stdin: bool = False,
    file: str | None = None,
    default_env: str = "",
) -> str | None:
    """Read a secret from a file, stdin or the environment, in that order.

    File first because it is the most deliberate, stdin next because it is
    explicit, environment last because it is the easiest to leak.
    """
    import os
    import sys

    if file:
        try:
            with open(file, encoding="utf-8") as handle:
                return handle.read().strip("\n")
        except OSError as exc:
            raise UsageError(f"--{name}-file: {exc.strerror or exc}", field=name) from exc

    if stdin:
        if sys.stdin is None or sys.stdin.isatty():
            raise UsageError(f"--{name}-stdin was given but stdin is a terminal", field=name)
        return sys.stdin.read().strip("\n")

    variable = env or default_env
    if variable:
        value = os.environ.get(variable)
        if value is not None:
            return value
        if env:
            raise UsageError(f"--{name}-env names {variable}, which is not set", field=name)
    return None
