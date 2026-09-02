"""`OperationSpec` → `click.Command`. One factory, zero per-command modules.

This is where the registry pays for itself: the argument list, the flags, the
help text, the example epilogue, the pagination flags, the dry-run
short-circuit, the policy check and the rendering all come from the spec, so
none of them can be forgotten for one command and remembered for another —
which is exactly how v1 ended up honouring `--dry-run` in 9 commands and
ignoring it in 12 (COR-17).
"""

from __future__ import annotations

import asyncio
import types
import typing
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import click
import msgspec

from tlgr.cli import errors as cli_errors
from tlgr.cli import params as ptypes
from tlgr.cli import render as renderer
from tlgr.cli.confirm import confirm
from tlgr.cli.globals import (
    CliState,
    add_global_options,
    env_bool,
    resolve_account,
    state_from,
)
from tlgr.core.errors import EXIT_EMPTY, DaemonError, PermissionError_, UsageError
from tlgr.core.pagination import DATE_OFFSET_KINDS
from tlgr.models.base import UNSET
from tlgr.models.peer import PeerRef
from tlgr.ops._params import cli_meta
from tlgr.ops._spec import OperationSpec, Surface
from tlgr.registry import REGISTRY, policy_allows

__all__ = [
    "LocalContext",
    "build_click_tree",
    "build_command",
    "run_op",
    "set_dispatcher",
]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class LocalContext:
    """The `OpContext` a `Surface.LOCAL` operation runs against."""

    account: str = ""
    dry_run: bool = False
    request_id: str = ""
    warnings: list[str] = field(default_factory=list)
    command_tree: Callable[[Sequence[str], bool], dict[str, Any] | None] | None = None

    def warn(self, message: str) -> None:
        self.warnings.append(message)


Dispatcher = Callable[[OperationSpec, msgspec.Struct, CliState], dict[str, Any]]
_dispatch: Dispatcher | None = None


def set_dispatcher(dispatcher: Dispatcher | None) -> None:
    """Install the daemon transport.

    Stage A registers only local operations; the daemon surface arrives with
    the transport, and until then asking for it is a daemon error rather than
    a traceback.
    """
    global _dispatch
    _dispatch = dispatcher


def _command_tree(path: Sequence[str], include_hidden: bool) -> dict[str, Any] | None:
    """Describe the Click tree for `tlgr schema`, imported lazily.

    `cli/__init__` imports this module, so reaching back into it has to happen
    at call time rather than at import time.
    """
    from tlgr.cli.introspect import describe

    return describe(tuple(path), include_hidden=include_hidden)


def run_op(spec: OperationSpec, request: msgspec.Struct, state: CliState) -> dict[str, Any]:
    """Execute one operation and return its response envelope."""
    if state.enable_commands and not policy_allows(state.enable_commands, spec.id):
        raise PermissionError_(
            f"operation {spec.id!r} is not enabled (add it to --enable-commands to allow it)",
        )

    # A local operation must not go looking for an account at all: reading the
    # active alias to attach it to `tlgr schema` would be a lie about what ran.
    account = resolve_account(state) if spec.needs_account else state.account
    request_id = uuid.uuid4().hex

    # The dry-run short-circuit lives here, before any implementation runs, so
    # an operation cannot forget to honour it.
    if state.dry_run and spec.mutating:
        return {
            "ok": True,
            "op": spec.id,
            "account": account or None,
            "result": {"dry_run": True, "would": spec.id, "request": msgspec.to_builtins(request)},
            "meta": {"request_id": request_id, "dry_run": True},
        }

    if spec.surface is Surface.DAEMON:
        if _dispatch is None:
            raise DaemonError(
                f"{spec.id} runs in the daemon, and this build has no transport wired up yet"
            )
        return _dispatch(spec, request, state)

    context = LocalContext(
        account=account, dry_run=state.dry_run, request_id=request_id, command_tree=_command_tree
    )
    result = asyncio.run(spec.impl(context, request))
    envelope: dict[str, Any] = {
        "ok": True,
        "op": spec.id,
        "result": msgspec.to_builtins(result),
        "meta": {"request_id": request_id, "warnings": context.warnings},
    }
    if account:
        envelope["account"] = account
    return envelope


# ---------------------------------------------------------------------------
# Field → Click parameter
# ---------------------------------------------------------------------------


def _unwrap(annotation: Any) -> tuple[Any, bool, bool]:
    """Return (base type, optional?, unset?) for a request annotation."""
    optional = False
    unset = False
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _unwrap(typing.get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        args = list(typing.get_args(annotation))
        if type(None) in args:
            optional = True
            args = [a for a in args if a is not type(None)]
        if msgspec.UnsetType in args:
            unset = True
            args = [a for a in args if a is not msgspec.UnsetType]
        base, inner_optional, inner_unset = _unwrap(args[0]) if args else (str, False, False)
        return base, optional or inner_optional, unset or inner_unset
    return annotation, optional, unset


def _click_type(base: Any, kind: str, choices: Sequence[str]) -> Any:
    if choices:
        return click.Choice(list(choices))
    typed = ptypes.for_kind(kind)
    if typed is not None:
        return typed
    if base is PeerRef:
        return ptypes.PEER
    if typing.get_origin(base) is Literal:
        return click.Choice([str(v) for v in typing.get_args(base)])
    if base is int:
        return click.INT
    if base is float:
        return click.FLOAT
    if base is bool:
        return click.BOOL
    return click.STRING


def _flag_name(name: str) -> str:
    return "--" + name.replace("_", "-")


def _describe(annotation: Any) -> str:
    if typing.get_origin(annotation) is typing.Annotated:
        for extra in typing.get_args(annotation)[1:]:
            if isinstance(extra, msgspec.Meta) and extra.description:
                return str(extra.description)
    return ""


def _meta_of(annotation: Any) -> dict[str, Any]:
    if typing.get_origin(annotation) is typing.Annotated:
        for extra in typing.get_args(annotation)[1:]:
            if isinstance(extra, msgspec.Meta):
                return cli_meta(extra)
    return {}


@dataclass(frozen=True)
class _Field:
    name: str
    annotation: Any
    default: Any
    required: bool
    cli: dict[str, Any]
    base: Any
    optional: bool
    unset: bool
    container: Any


def _fields(request: type[msgspec.Struct]) -> list[_Field]:
    hints = typing.get_type_hints(request, include_extras=True)
    info = msgspec.inspect.type_info(request)
    out: list[_Field] = []
    for spec_field in getattr(info, "fields", ()):
        annotation = hints[spec_field.name]
        base, optional, unset = _unwrap(annotation)
        container = typing.get_origin(base)
        if container in (list, tuple, set):
            args = typing.get_args(base)
            base = args[0] if args else str
        default = spec_field.default
        if default is msgspec.NODEFAULT:
            factory = spec_field.default_factory
            default = factory() if factory is not msgspec.NODEFAULT else None
        out.append(
            _Field(
                name=spec_field.name,
                annotation=annotation,
                default=None if default is UNSET else default,
                required=spec_field.required,
                cli=_meta_of(annotation),
                base=base,
                optional=optional,
                unset=unset,
                container=container,
            )
        )
    return out


def _parameter(f: _Field) -> click.Parameter:
    """One request field as one Click parameter (the §4.2 table)."""
    help_text = _describe(f.annotation)
    kind = str(f.cli.get("kind", ""))
    choices = f.cli.get("choices") or ()
    click_type = _click_type(f.base, kind, choices)

    if f.cli.get("role") == "arg":
        variadic = bool(f.cli.get("variadic"))
        return click.Argument(
            [f.name],
            required=bool(f.cli.get("required", True)) and not variadic,
            nargs=-1 if variadic else 1,
            type=click_type,
            # Click appends the `...` that says "repeatable" itself, but only
            # when it derives the metavar; an explicit one loses it.
            metavar=(f.cli.get("metavar") or None) if not variadic else None,
        )

    flags = list(f.cli.get("flags") or [])
    if f.base is bool and not f.cli.get("count"):
        # False default → a plain flag. True or tri-state → a paired flag,
        # because "leave it alone" and "turn it off" are different requests.
        # A default of True, or a tri-state, needs the negative half: "leave
        # it alone" and "turn it off" are different requests, and only a
        # paired flag can say both.
        paired = f.default is True or (f.optional and f.default is None)
        negative = f"--no-{f.name.replace('_', '-')}"
        if not flags:
            flags = [_flag_name(f.name)]
        if paired and not any("/" in flag for flag in flags):
            flags = [f"{flags[0]}/{negative}", *flags[1:]]
        return click.Option(
            [*flags, f.name],
            is_flag=True,
            default=f.default if not f.optional else None,
            help=help_text or None,
            hidden=bool(f.cli.get("hidden")),
        )

    if not flags:
        flags = [_flag_name(f.name)]
    # Click wants the long option first so it can derive the parameter name;
    # `opt("-n", "--limit")` puts the short one first for readability.
    ordered = sorted(flags, key=lambda flag: not flag.startswith("--"))
    return click.Option(
        [*ordered, f.name],
        type=click_type,
        default=f.default if not (f.unset or f.optional) else None,
        required=f.required and not f.optional,
        multiple=f.container in (list, tuple, set),
        metavar=f.cli.get("metavar") or None,
        envvar=f.cli.get("envvar") or None,
        show_default=f.default not in (None, "", [], False),
        help=help_text or None,
        hidden=bool(f.cli.get("hidden")),
        count=bool(f.cli.get("count")),
    )


def _secret_options(f: _Field) -> list[click.Parameter]:
    """A secret never gets a value-taking flag (STYLE §3)."""
    base = f.name.replace("_", "-")
    label = f.name.replace("_", " ")
    return [
        click.Option(
            [f"--{base}-env", f"{f.name}_env"],
            metavar="VAR",
            default=None,
            help=f"Read the {label} from this environment variable.",
        ),
        click.Option(
            [f"--{base}-stdin", f"{f.name}_stdin"],
            is_flag=True,
            default=False,
            help=f"Read the {label} from stdin.",
        ),
        click.Option(
            [f"--{base}-file", f"{f.name}_file"],
            metavar="PATH",
            default=None,
            help=f"Read the {label} from this file.",
        ),
    ]


def _pagination_options(spec: OperationSpec) -> list[click.Parameter]:
    options: list[click.Parameter] = [
        click.Option(
            ["-n", "--limit", "limit"], type=int, default=None, help="Maximum items to return."
        ),
        click.Option(
            ["--cursor", "cursor"], default=None, metavar="TOKEN", help="Continue a page."
        ),
        click.Option(["--all", "fetch_all"], is_flag=True, default=False, help="Walk every page."),
    ]
    if spec.paginated in DATE_OFFSET_KINDS:
        options += [
            click.Option(
                ["--since", "since"], type=ptypes.DATETIME, default=None, help="Only after this."
            ),
            click.Option(
                ["--until", "until"], type=ptypes.DATETIME, default=None, help="Only before this."
            ),
        ]
    return options


# ---------------------------------------------------------------------------
# Command assembly
# ---------------------------------------------------------------------------


def _build_request(spec: OperationSpec, fields: list[_Field], values: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    for f in fields:
        if f.cli.get("secret"):
            from tlgr.ops._params import read_secret

            secret = read_secret(
                f.name,
                env=values.get(f"{f.name}_env"),
                stdin=bool(values.get(f"{f.name}_stdin")),
                file=values.get(f"{f.name}_file"),
                default_env=str(f.cli.get("envvar") or ""),
            )
            if secret is not None:
                kwargs[f.name] = secret
            continue

        if f.name not in values:
            continue
        value = values[f.name]
        if value is None:
            # An absent option on an Unset field sends nothing at all, which
            # is what makes "leave alone" expressible.
            continue
        if f.container in (list, tuple, set):
            if not value:
                continue
            value = list(value) if f.container is not tuple else tuple(value)
        kwargs[f.name] = value
    try:
        request = spec.request(**kwargs)
        # Constructing a Struct does not run msgspec's constraints (ge, le,
        # pattern, min_length) — only decoding does. Round-tripping is what
        # makes `--limit-hint 500` fail in the CLI instead of in the daemon.
        return msgspec.convert(msgspec.to_builtins(request), type=spec.request)
    except (msgspec.ValidationError, TypeError, ValueError) as exc:
        field = None
        message = str(exc)
        if " - at `$." in message:
            field = message.split(" - at `$.", 1)[1].rstrip("`")
        raise UsageError(message, field=field) from exc


def build_command(
    spec: OperationSpec, *, name: str | None = None, hidden: bool = False
) -> click.Command:
    """Turn one spec into one Click command."""
    fields = _fields(spec.request)
    parameters: list[click.Parameter] = []
    positional = sorted(
        (f for f in fields if f.cli.get("role") == "arg"), key=lambda f: int(f.cli.get("pos", 0))
    )
    for f in positional:
        parameters.append(_parameter(f))
    for f in fields:
        if f.cli.get("role") == "arg":
            continue
        parameters.extend(_secret_options(f) if f.cli.get("secret") else [_parameter(f)])

    if spec.paginated is not None:
        parameters.extend(_pagination_options(spec))
        parameters.append(
            # `-n` belongs to --limit here, so --dry-run gets no short form.
            click.Option(["--dry-run"], is_flag=True, default=None, help="Do not actually do it.")
        )
    else:
        parameters.append(
            click.Option(
                ["--dry-run", "-n"], is_flag=True, default=None, help="Do not actually do it."
            )
        )

    def callback(**values: Any) -> None:
        ctx = click.get_current_context()
        state = state_from(ctx, values)
        if spec.destructive and not state.dry_run:
            confirm(
                f"{spec.summary} — this cannot be undone.",
                force=state.force,
                no_input=state.no_input,
                hint=f"pass --yes to confirm {spec.id}",
            )
        request = _build_request(spec, fields, values)
        envelope = run_op(spec, request, state)
        # A schema document is JSON whether or not anybody asked: there is no
        # table shape for it, and v1 printed JSON here unconditionally. When
        # nobody asked for --json it is also printed bare, which is the exact
        # document v1 wrote; the envelope appears only once JSON is requested.
        json_only = "json-only" in spec.tags
        fmt = "json" if json_only else state.fmt
        renderer.render(
            envelope,
            fmt=fmt,
            results_only=state.results_only or (json_only and state.fmt != "json"),
            select=state.select,
            spec_columns=spec.columns,
            headers=spec.headers,
            columns=state.columns,
            wide=state.wide,
            no_header=state.no_header,
        )
        result = envelope.get("result")
        if spec.empty_exit == EXIT_EMPTY and not result:
            ctx.exit(EXIT_EMPTY)

    command = OpCommand(
        spec,
        name=name or spec.verb,
        params=parameters,
        callback=callback,
        help=_help_text(spec),
        short_help=spec.summary,
        epilog=_epilog(spec),
        hidden=hidden or bool(spec.deprecated),
    )
    return add_global_options(command)


class OpCommand(click.Command):
    """A generated command that reports its own failures.

    Errors are caught here rather than in the root group so that the new
    rules — the `{"ok": false, "error": {...}}` envelope, JSON usage errors
    (UX-02), the exit code from the §7.2 table — apply to registry-generated
    commands only. Every unmigrated v1 command keeps v1's error output until
    its own group PR moves it.
    """

    def __init__(self, spec: OperationSpec, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.spec = spec

    def _wants_json(self, ctx: click.Context | None, args: Sequence[str] = ()) -> bool:
        if "--json" in args:
            return True
        if "--plain" in args:
            return False
        obj = (ctx.obj if ctx is not None else None) or {}
        return bool(obj.get("json") or obj.get("use_json")) or env_bool("TLGR_JSON")

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, list(args))
        except click.UsageError as exc:
            if not self._wants_json(ctx, args):
                raise
            # v1 let Click print English to stderr and exited 2 with no JSON
            # at all, which is UX-02: an agent got nothing to parse.
            ctx.exit(cli_errors.handle(exc, use_json=True, op=self.spec.id))
            raise

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.Abort):
            raise
        except KeyboardInterrupt as exc:
            ctx.exit(cli_errors.handle(exc, use_json=False, op=self.spec.id))
        except BaseException as exc:
            use_json = self._wants_json(ctx) or bool(ctx.params.get("use_json"))
            ctx.exit(cli_errors.handle(exc, use_json=use_json, op=self.spec.id))


def _help_text(spec: OperationSpec) -> str:
    body = spec.summary.rstrip(".") + "."
    if spec.description:
        body += "\n\n" + spec.description
    if spec.deprecated:
        body += f"\n\nDEPRECATED: {spec.deprecated}"
    return body


def _epilog(spec: OperationSpec) -> str:
    if not spec.example_args:
        return ""
    return f"Example:\n\n  tlgr {spec.example_args}"


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def _place(root: dict[str, Any], path: Sequence[str], command: click.Command) -> None:
    """Attach *command* at *path*, creating the groups it needs on the way."""
    *groups, leaf = path
    container: Any = root
    walked: list[str] = []
    for name in groups:
        walked.append(name)
        existing = (
            container.get(name) if isinstance(container, dict) else container.commands.get(name)
        )
        if existing is None or not isinstance(existing, click.Group):
            existing = click.Group(name=name, help=f"{' '.join(walked)} operations.")
            if isinstance(container, dict):
                container[name] = existing
            else:
                container.add_command(existing, name)
        container = existing
    command.name = leaf
    if isinstance(container, dict):
        container[leaf] = command
    else:
        container.add_command(command, leaf)


def build_click_tree(
    registry: dict[str, OperationSpec] | None = None,
) -> dict[str, click.Command]:
    """Every registered operation as a nested Click tree, keyed by top-level name.

    Canonical paths are visible. Aliases are hidden duplicates — they exist so
    that a habit keeps working, not so that `--help` lists the same command
    twice. `legacy_paths` stay visible: they are the v1 paths the docs name,
    and §12.4 promises none of them disappears.
    """
    specs = (registry or REGISTRY).values()
    root: dict[str, Any] = {}

    for spec in sorted(specs, key=lambda s: s.id):
        _place(root, spec.path, build_command(spec))
    for spec in sorted(specs, key=lambda s: s.id):
        for alias in spec.aliases:
            _place(root, tuple(alias.split(".")), build_command(spec, hidden=True))
        for legacy in spec.legacy_paths:
            path = tuple(legacy.replace(".", " ").split())
            if path == spec.path:
                continue
            _place(root, path, build_command(spec))
    return root
