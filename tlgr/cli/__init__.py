"""CLI entry point using Click with nested command groups."""

from __future__ import annotations

import os
import sys

import click

from tlgr import __version__
from tlgr.core.errors import TlgrError, emit_error, exit_code_for


def _env_bool(key: str) -> bool:
    return os.environ.get(key, "").lower() in ("1", "true", "yes", "y", "on")


def _env_or(key: str, fallback: str) -> str:
    return os.environ.get(key, "") or fallback


def _registry_op(cmd_name: str, rest: list[str]) -> str | None:
    """The canonical op id an invocation resolves to, if the registry owns it."""
    from tlgr.registry import ALIASES

    candidates = [cmd_name]
    if rest and not rest[0].startswith("-"):
        candidates.append(f"{cmd_name}.{rest[0]}")
    for candidate in reversed(candidates):
        found = ALIASES.get(candidate)
        if found is not None:
            return found
    return None


class TlgrGroup(click.Group):
    """Custom group that handles errors, sandboxing, and output formatting."""

    def invoke(self, ctx: click.Context) -> None:
        try:
            super().invoke(ctx)
        except TlgrError as e:
            use_json = ctx.params.get("json") or (ctx.obj and ctx.obj.get("json"))
            emit_error(e, use_json=bool(use_json))
            sys.exit(exit_code_for(e))
        except (click.ClickException, click.exceptions.Exit, SystemExit):
            raise
        except KeyboardInterrupt:
            sys.exit(130)
        except Exception as e:
            use_json = ctx.params.get("json") or (ctx.obj and ctx.obj.get("json"))
            emit_error(e, use_json=bool(use_json))
            sys.exit(1)

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        """Override to enforce --enable-commands before dispatching."""
        cmd_name, cmd, rest = super().resolve_command(ctx, args)

        enabled = ctx.params.get("enable_commands") or ""
        enabled = enabled.strip()

        # A registry-generated command enforces the allowlist itself, by
        # canonical op id, so `--enable-commands agent.exit-codes` also allows
        # the `exit-codes` alias (SEC-04). The path matching below is v1's and
        # stays only for the groups that are still hand-written.
        if enabled and cmd_name and _registry_op(cmd_name, rest) is not None:
            return cmd_name, cmd, rest

        if enabled and cmd_name:
            allow = {p.strip().lower() for p in enabled.split(",") if p.strip()}
            if allow and "*" not in allow and "all" not in allow:
                name_l = cmd_name.lower()
                group_allowed = name_l in allow
                has_sub_rules = any(a.startswith(f"{name_l}.") for a in allow)
                if not group_allowed and not has_sub_rules:
                    click.echo(
                        f"Error: command {cmd_name!r} is not enabled "
                        f"(set --enable-commands to allow it)",
                        err=True,
                    )
                    sys.exit(2)
                if has_sub_rules and isinstance(cmd, click.Group) and rest:
                    sub_name = rest[0] if rest else None
                    if sub_name and not group_allowed:
                        full_path = f"{name_l}.{sub_name.lower()}"
                        if full_path not in allow:
                            click.echo(
                                f"Error: command {full_path!r} is not enabled "
                                f"(set --enable-commands to allow it)",
                                err=True,
                            )
                            sys.exit(2)

        return cmd_name, cmd, rest


@click.group(cls=TlgrGroup)
@click.version_option(__version__, prog_name="tlgr")
@click.option(
    "--json",
    "use_json",
    is_flag=True,
    default=_env_bool("TLGR_JSON"),
    help="Output JSON to stdout.",
)
@click.option(
    "--plain",
    "use_plain",
    is_flag=True,
    default=_env_bool("TLGR_PLAIN"),
    help="Output stable TSV for piping.",
)
@click.option(
    "--account",
    "-a",
    default=_env_or("TLGR_ACCOUNT", ""),
    help="Account alias to use.",
)
@click.option(
    "--enable-commands",
    default=_env_or("TLGR_ENABLE_COMMANDS", ""),
    help="Comma-separated allowlist of commands (e.g. 'message.send,chat.list').",
)
@click.option(
    "--results-only",
    is_flag=True,
    default=False,
    help="In JSON mode, emit only the primary result (strip envelope).",
)
@click.option(
    "--select",
    "select_fields",
    default=None,
    help="In JSON mode, select comma-separated fields (supports dot paths).",
)
@click.option(
    "--dry-run",
    "-n",
    is_flag=True,
    default=False,
    help="Preview destructive operations without executing.",
)
@click.option(
    "--force",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmations for destructive commands.",
)
@click.option(
    "--flood-wait-max",
    type=int,
    default=None,
    help="Max seconds to auto-sleep on rate limit (default from config).",
)
@click.option(
    "--no-input",
    is_flag=True,
    default=False,
    help="Never prompt; fail instead (CI/agent mode).",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose logging to stderr.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    use_json: bool,
    use_plain: bool,
    account: str | None,
    enable_commands: str,
    results_only: bool,
    select_fields: str | None,
    dry_run: bool,
    flood_wait_max: int | None,
    force: bool,
    no_input: bool,
    verbose: bool,
) -> None:
    """tlgr — Full Telegram account control CLI."""
    ctx.ensure_object(dict)

    # TLGR_AUTO_JSON: default to JSON when stdout is piped and env var is set
    if _env_bool("TLGR_AUTO_JSON") and not use_json and not use_plain:
        if not sys.stdout.isatty():
            use_json = True

    if use_json and use_plain:
        click.echo("Error: cannot combine --json and --plain", err=True)
        sys.exit(2)

    if use_json:
        ctx.obj["fmt"] = "json"
    elif use_plain:
        ctx.obj["fmt"] = "plain"
    else:
        ctx.obj["fmt"] = "human"

    ctx.obj["json"] = use_json
    ctx.obj["account"] = account or ""
    ctx.obj["enable_commands"] = enable_commands
    ctx.obj["results_only"] = results_only
    ctx.obj["select"] = select_fields
    ctx.obj["dry_run"] = dry_run
    ctx.obj["flood_wait_max"] = flood_wait_max
    # The forty hand-written v1 commands do not thread this through their own
    # request bodies, so the transport attaches it and the daemon applies it
    # per request. Without this the flag parsed and did nothing (COR-15).
    from tlgr.transport import set_default_flood_wait_max

    set_default_flood_wait_max(flood_wait_max)
    ctx.obj["force"] = force
    ctx.obj["no_input"] = no_input
    ctx.obj["verbose"] = verbose

    if verbose:
        import logging

        logging.basicConfig(
            level=logging.DEBUG, stream=sys.stderr, format="%(levelname)s: %(message)s"
        )


# ---------------------------------------------------------------------------
# Import and register sub-groups
# ---------------------------------------------------------------------------

from tlgr.cli.gen import build_click_tree  # noqa: E402
from tlgr.cli.legacy.chat import chat_create, chat_members  # noqa: E402
from tlgr.cli.legacy.profile import profile_group  # noqa: E402

cli.add_command(profile_group, "profile")


# ---------------------------------------------------------------------------
# The generated tree
# ---------------------------------------------------------------------------


#: Commands that still live in `cli/legacy` *inside* a group the registry now
#: generates. Each entry is a promise to delete, and an enumerated list is
#: the only kind of overlap that is a decision rather than an accident. PR-2
#: took `agent whoami` out of it, PR-4 took `daemon` and `job`; what is left
#: is the sanctioned overlap for the group PRs still to come.
LEGACY_EXTRAS: dict[str, list[click.Command]] = {
    # `chat create` and `chat members` are member/admin operations and
    # migrate with the groups-and-channels group (PR-7).
    "chat": [chat_members, chat_create],
}


def build_cli() -> click.Group:
    """Compose the generated command tree with the v1 groups still hand-written.

    A *command* must be defined in exactly one of the two places. Being
    defined in both would mean a migration half-landed — one path generated,
    one still hand-written, silently disagreeing — so it fails the import
    rather than the user's next command (§12.4).

    A *group* may legitimately be shared while a migration is in flight, in
    both directions: LEGACY_EXTRAS puts a v1 command inside a generated group
    (`agent whoami` until PR-2 moves it), and merging puts a generated command
    inside a v1 group (`account status`, whose group migrates in PR-2). Both
    are enumerated by the code that does the merging, and a name that appears
    twice is still a hard failure.
    """
    import tlgr.ops  # noqa: F401  — importing it is what populates the registry
    from tlgr.cli.gen import set_dispatcher
    from tlgr.transport import make_dispatcher, make_stream_dispatcher

    # Installing the transport here, rather than importing it in `gen.py`, is
    # what keeps `cli/gen.py` testable with a fake dispatcher and keeps the
    # daemon out of the CLI's import graph.
    set_dispatcher(make_dispatcher(), make_stream_dispatcher())

    generated = build_click_tree()
    for name, command in generated.items():
        for extra in LEGACY_EXTRAS.get(name, []):
            if isinstance(command, click.Group):
                command.add_command(extra, extra.name)
        existing = cli.commands.get(name)
        if existing is None:
            cli.add_command(command, name)
            continue
        if not (isinstance(existing, click.Group) and isinstance(command, click.Group)):
            raise RuntimeError(
                f"the command {name!r} is defined both by the registry and by "
                f"tlgr/cli/legacy. Delete the legacy module."
            )
        for sub_name, sub in command.commands.items():
            if sub_name in existing.commands:
                raise RuntimeError(
                    f"{name} {sub_name} is defined both by the registry and by "
                    f"tlgr/cli/legacy. Delete the legacy command."
                )
            existing.add_command(sub, sub_name)
    return cli


build_cli()
