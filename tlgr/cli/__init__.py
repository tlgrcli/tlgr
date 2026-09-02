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
from tlgr.cli.legacy import agent as legacy_agent  # noqa: E402
from tlgr.cli.legacy.account import account_group  # noqa: E402
from tlgr.cli.legacy.chat import chat_group  # noqa: E402
from tlgr.cli.legacy.completion import completion_group  # noqa: E402
from tlgr.cli.legacy.config_cmd import config_group  # noqa: E402
from tlgr.cli.legacy.contact import contact_group  # noqa: E402
from tlgr.cli.legacy.daemon_cmd import daemon_group  # noqa: E402
from tlgr.cli.legacy.draft import draft_group  # noqa: E402
from tlgr.cli.legacy.job import job_group  # noqa: E402
from tlgr.cli.legacy.media import media_group  # noqa: E402
from tlgr.cli.legacy.message import message_group  # noqa: E402
from tlgr.cli.legacy.profile import profile_group  # noqa: E402
from tlgr.cli.legacy.user import user_group  # noqa: E402
from tlgr.cli.legacy.watch import watch_command  # noqa: E402

cli.add_command(account_group, "account")
cli.add_command(message_group, "message")
cli.add_command(message_group, "msg")
cli.add_command(chat_group, "chat")
cli.add_command(contact_group, "contact")
cli.add_command(draft_group, "draft")
cli.add_command(profile_group, "profile")
cli.add_command(media_group, "media")
cli.add_command(daemon_group, "daemon")
cli.add_command(job_group, "job")
cli.add_command(config_group, "config")
cli.add_command(completion_group, "completion")
cli.add_command(user_group, "user")
cli.add_command(watch_command, "watch")


# ---------------------------------------------------------------------------
# Top-level action shortcuts (desire paths)
# ---------------------------------------------------------------------------


@cli.command("send")
@click.argument("chat")
@click.argument("text", required=False, default="")
@click.option("--file", "file_path", default=None, help="File to attach.")
@click.option("--caption", default=None, help="Caption for file.")
@click.option("--reply-to", type=int, default=None, help="Reply to message ID.")
@click.option("--silent", is_flag=True, help="Send without notification.")
@click.pass_context
def shortcut_send(
    ctx: click.Context,
    chat: str,
    text: str,
    file_path: str | None,
    caption: str | None,
    reply_to: int | None,
    silent: bool,
) -> None:
    """Send a message (shortcut for 'message send')."""
    ctx.invoke(
        message_group.commands["send"],
        chat=chat,
        text=text,
        file_path=file_path,
        caption=caption,
        reply_to=reply_to,
        silent=silent,
        account=ctx.obj.get("account"),
    )


@cli.command("login")
@click.argument("phone")
@click.option("--alias", default=None, help="Alias for this account.")
@click.pass_context
def shortcut_login(ctx: click.Context, phone: str, alias: str | None) -> None:
    """Add and authenticate a Telegram account (shortcut for 'account add')."""
    ctx.invoke(account_group.commands["add"], phone=phone, alias=alias)


@cli.command("logout")
@click.argument("alias")
@click.pass_context
def shortcut_logout(ctx: click.Context, alias: str) -> None:
    """Remove an account (shortcut for 'account remove')."""
    ctx.invoke(account_group.commands["remove"], alias=alias)


@cli.command("status")
@click.pass_context
def shortcut_status(ctx: click.Context) -> None:
    """Show daemon status (shortcut for 'daemon status')."""
    ctx.invoke(daemon_group.commands["status"])


@cli.command("chats")
@click.option("--type", "chat_type", default=None, help="Filter: user, group, channel, bot.")
@click.option("--search", "-s", default=None, help="Filter by name.")
@click.option("--limit", "-n", type=int, default=None)
@click.pass_context
def shortcut_chats(
    ctx: click.Context, chat_type: str | None, search: str | None, limit: int | None
) -> None:
    """List all chats (shortcut for 'chat list')."""
    ctx.invoke(
        chat_group.commands["list"],
        chat_type=chat_type,
        search=search,
        limit=limit,
        account=ctx.obj.get("account"),
    )


@cli.command("inbox")
@click.option("--type", "chat_type", default=None, help="Filter: user, group, channel, bot.")
@click.option("--limit", "-n", type=int, default=None)
@click.pass_context
def shortcut_inbox(ctx: click.Context, chat_type: str | None, limit: int | None) -> None:
    """List chats with unread messages (shortcut for 'chat list --unread')."""
    ctx.invoke(
        chat_group.commands["list"],
        chat_type=chat_type,
        limit=limit,
        unread=True,
        account=ctx.obj.get("account"),
    )


@cli.command("catchup")
@click.option("--type", "chat_type", default=None, help="Filter: user, group, channel, bot.")
@click.option("--limit-chats", type=int, default=20)
@click.option("--per-chat", type=int, default=10)
@click.pass_context
def shortcut_catchup(
    ctx: click.Context, chat_type: str | None, limit_chats: int, per_chat: int
) -> None:
    """Unread chats with their recent messages (shortcut for 'chat catchup')."""
    ctx.invoke(
        chat_group.commands["catchup"],
        chat_type=chat_type,
        limit_chats=limit_chats,
        per_chat=per_chat,
        account=ctx.obj.get("account"),
    )


@cli.command("contacts")
@click.pass_context
def shortcut_contacts(ctx: click.Context) -> None:
    """List all contacts (shortcut for 'contact list')."""
    ctx.invoke(contact_group.commands["list"], account=ctx.obj.get("account"))


@cli.command("dl")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.option("--out-dir", default=None, help="Output directory.")
@click.pass_context
def shortcut_download(ctx: click.Context, chat: str, msg_id: int, out_dir: str | None) -> None:
    """Download media (shortcut for 'media download')."""
    ctx.invoke(
        media_group.commands["download"],
        chat=chat,
        msg_id=msg_id,
        out_dir=out_dir,
        account=ctx.obj.get("account"),
    )


@cli.command("up")
@click.argument("chat")
@click.argument("path", type=click.Path(exists=True))
@click.option("--caption", default="", help="Caption for the file.")
@click.pass_context
def shortcut_upload(ctx: click.Context, chat: str, path: str, caption: str) -> None:
    """Upload a file (shortcut for 'media upload')."""
    ctx.invoke(
        media_group.commands["upload"],
        chat=chat,
        path=path,
        caption=caption,
        account=ctx.obj.get("account"),
    )


# ---------------------------------------------------------------------------
# The generated tree
# ---------------------------------------------------------------------------


#: Commands that still live in `cli/legacy` *inside* a group the registry now
#: generates. Each entry is a promise to delete: `agent whoami` needs the
#: account manager, so it migrates with the account group (PR-2).
LEGACY_EXTRAS: dict[str, list[click.Command]] = {
    "agent": [legacy_agent.agent_whoami],
}


def build_cli() -> click.Group:
    """Compose the generated command tree with the v1 groups still hand-written.

    A group must be defined in exactly one of the two places. Being defined in
    both would mean a migration half-landed — one path generated, one path
    still hand-written, silently disagreeing — so it fails the import rather
    than the user's next command (§12.4). The one sanctioned overlap is
    LEGACY_EXTRAS, which is an explicit, enumerated list rather than an
    accident.
    """
    import tlgr.ops  # noqa: F401  — importing it is what populates the registry
    from tlgr.cli.gen import set_dispatcher
    from tlgr.transport import make_dispatcher

    # Installing the transport here, rather than importing it in `gen.py`, is
    # what keeps `cli/gen.py` testable with a fake dispatcher and keeps the
    # daemon out of the CLI's import graph.
    set_dispatcher(make_dispatcher())

    generated = build_click_tree()
    clash = sorted(set(generated) & set(cli.commands))
    if clash:
        raise RuntimeError(
            f"these command groups are defined both by the registry and by "
            f"tlgr/cli/legacy: {clash}. Delete the legacy module."
        )
    for name, command in generated.items():
        for extra in LEGACY_EXTRAS.get(name, []):
            if isinstance(command, click.Group):
                command.add_command(extra, extra.name)
        cli.add_command(command, name)
    return cli


build_cli()
