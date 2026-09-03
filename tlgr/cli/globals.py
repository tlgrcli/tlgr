"""The global flags, attached to every generated command *and* to the root.

`tlgr chat list --json` and `tlgr --json chat list` must both work. In v1 only
the second did, and the first exited 2 with "No such option" (UX-01), which is
the single most common thing an agent gets wrong on its first call. The flags
are therefore declared once here and attached in both places; a command-level
value wins over a root-level one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import click

__all__ = [
    "GLOBAL_FLAG_NAMES",
    "CliState",
    "add_global_options",
    "env_bool",
    "resolve_account",
    "state_from",
]


def env_bool(key: str) -> bool:
    return os.environ.get(key, "").lower() in ("1", "true", "yes", "y", "on")


def _env_or(key: str, fallback: str = "") -> str:
    return os.environ.get(key, "") or fallback


#: Parameter names the generator must never collide with when deriving flags
#: from request fields.
GLOBAL_FLAG_NAMES = frozenset(
    {
        "use_json",
        "use_plain",
        "account",
        "results_only",
        "select_fields",
        "dry_run",
        "force",
        "no_input",
        "flood_wait_max",
        "timeout",
        "verbose",
        "no_daemon_restart",
        "enable_commands",
        "columns",
        "wide",
        "no_header",
        # Pagination is transport-level, not part of any request struct
        # (registry lint L5 forbids those field names), so the generated
        # flags land here and travel to the daemon beside the request.
        "limit",
        "cursor",
        "fetch_all",
    }
)


@dataclass
class CliState:
    """The merged view of the global flags for one invocation."""

    fmt: str = "human"
    account: str = ""
    results_only: bool = False
    select: str | None = None
    dry_run: bool = False
    force: bool = False
    no_input: bool = False
    flood_wait_max: int | None = None
    timeout: float | None = None
    verbose: bool = False
    no_daemon_restart: bool = False
    enable_commands: str = ""
    columns: str | None = None
    wide: bool = False
    no_header: bool = False
    limit: int | None = None
    cursor: str | None = None
    fetch_all: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def json(self) -> bool:
        return self.fmt == "json"


def _flag_options() -> list[Any]:
    """The option decorators, built fresh so Click never shares a Parameter."""
    return [
        click.option(
            "--json", "use_json", is_flag=True, default=None, help="Output JSON to stdout."
        ),
        click.option(
            "--plain", "use_plain", is_flag=True, default=None, help="Output stable TSV for piping."
        ),
        click.option(
            "--account",
            "-a",
            default=None,
            metavar="ALIAS",
            help="Account alias to use.  [env: TLGR_ACCOUNT]",
        ),
        click.option(
            "--results-only",
            is_flag=True,
            default=None,
            help="In JSON mode, print only the result (no envelope).",
        ),
        click.option(
            "--select",
            "select_fields",
            default=None,
            metavar="FIELDS",
            help="Project comma-separated fields out of the result (dot paths allowed).",
        ),
        click.option(
            "--columns", default=None, metavar="COLS", help="Override the human/plain columns."
        ),
        click.option("--wide", is_flag=True, default=None, help="Do not truncate any column."),
        click.option("--no-header", is_flag=True, default=None, help="Omit the header row."),
        click.option(
            "--yes", "-y", "force", is_flag=True, default=None, help="Skip confirmations."
        ),
        click.option(
            "--no-input", is_flag=True, default=None, help="Never prompt; fail instead (CI/agent)."
        ),
        click.option(
            "--flood-wait-max",
            type=int,
            default=None,
            help="Max seconds to auto-sleep on a rate limit.",
        ),
        click.option("--timeout", type=float, default=None, help="Client-side timeout in seconds."),
        click.option(
            "--no-daemon-restart",
            is_flag=True,
            default=None,
            help="Never restart the daemon automatically.",
        ),
        click.option(
            "--enable-commands",
            default=None,
            metavar="IDS",
            help="Comma-separated allowlist of operation ids.",
        ),
        click.option("--verbose", "-v", is_flag=True, default=None, help="Verbose logging."),
    ]


def add_global_options(command: Any) -> Any:
    """Attach every global flag to *command*.

    Defaults are `None` rather than `False` so that "not given here" stays
    distinguishable from "given as false", which is what lets a command-level
    flag override a root-level one without clobbering it.
    """
    for decorator in reversed(_flag_options()):
        command = decorator(command)
    return command


def _merge(target: dict[str, Any], params: dict[str, Any]) -> None:
    """Fold command-level flag values over the root-level ones."""
    for key, value in params.items():
        if value is not None and key in GLOBAL_FLAG_NAMES:
            target[key] = value


def state_from(ctx: click.Context, params: dict[str, Any] | None = None) -> CliState:
    """Build the merged `CliState` for this invocation."""
    merged: dict[str, Any] = dict(ctx.obj or {})
    if params:
        _merge(merged, params)

    use_json = merged.get("use_json")
    use_plain = merged.get("use_plain")
    if use_json is None:
        use_json = merged.get("json") or env_bool("TLGR_JSON")
    if use_plain is None:
        use_plain = env_bool("TLGR_PLAIN")
    if use_json and use_plain:
        raise click.UsageError("cannot combine --json and --plain")

    fmt = merged.get("fmt", "human")
    if use_json:
        fmt = "json"
    elif use_plain:
        fmt = "plain"

    return CliState(
        fmt=fmt,
        account=merged.get("account") or "",
        results_only=bool(merged.get("results_only")),
        select=merged.get("select_fields") or merged.get("select"),
        dry_run=bool(merged.get("dry_run")),
        force=bool(merged.get("force")),
        no_input=bool(merged.get("no_input")),
        flood_wait_max=merged.get("flood_wait_max"),
        timeout=merged.get("timeout"),
        verbose=bool(merged.get("verbose")),
        no_daemon_restart=bool(merged.get("no_daemon_restart")),
        enable_commands=merged.get("enable_commands") or "",
        columns=merged.get("columns"),
        wide=bool(merged.get("wide")),
        no_header=bool(merged.get("no_header")),
        limit=merged.get("limit"),
        cursor=merged.get("cursor"),
        fetch_all=bool(merged.get("fetch_all")),
    )


def _require_account_enabled() -> bool:
    """`TLGR_REQUIRE_ACCOUNT`, else `[defaults] require_account`."""
    env = os.environ.get("TLGR_REQUIRE_ACCOUNT", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        from tlgr.core.config import load_app_config

        return bool(load_app_config().defaults.require_account)
    except Exception:
        return False


def resolve_account(
    state: CliState,
    *,
    positional: str | None = None,
    require: bool | None = None,
) -> str:
    """Resolve the account, in one place, in one order.

    positional → `-a/--account` → `TLGR_ACCOUNT` → `[accounts] default` →
    the active alias. The daemon never picks for you (COR-02); when nothing
    resolves and the operation needs one, that is a USAGE error naming the
    ways to supply it.
    """
    alias = (positional or state.account or _env_or("TLGR_ACCOUNT")).strip()

    if not alias:
        try:
            from tlgr.core.config import CONFIG_DIR, load_app_config

            alias = (load_app_config().default_account or "").strip()
            if not alias:
                from tlgr.core.accounts import AccountManager

                alias = (AccountManager(CONFIG_DIR).get_active() or "").strip()
        except Exception:
            alias = ""

    if not alias and (require or (require is None and _require_account_enabled())):
        raise click.UsageError(
            "No account specified. Pass -a <alias>, set TLGR_ACCOUNT, or set "
            "[accounts] default in config.toml (see: tlgr account list)."
        )
    return alias
