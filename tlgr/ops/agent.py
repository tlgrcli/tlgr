"""Local operations — the ones that need no account and no daemon.

They are the proof that the registry can carry an operation end to end: these
two were hand-written Click commands in v1 and are now generated, with the
same JSON a v1 agent already parses.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from tlgr.core.errors import EXIT_CODE_MAP, UsageError
from tlgr.models.base import Model, Request
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [
    "SPEC_COMPLETION",
    "SPEC_EXIT_CODES",
    "SPEC_PARITY",
    "SPEC_SCHEMA",
    "SPEC_WHOAMI",
    "CompletionScript",
    "ExitCodeEntry",
    "ExitCodes",
    "SchemaDoc",
    "Whoami",
]


class ExitCodeEntry(Model):
    code: int
    description: str


class ExitCodes(Model):
    """Deliberately a mapping, not a list: this is the exact JSON v1 printed."""

    exit_codes: dict[str, ExitCodeEntry]


class ExitCodesReq(Request):
    pass


async def exit_codes(ctx: OpContext, req: ExitCodesReq) -> ExitCodes:
    """Return the stable exit-code table."""
    return ExitCodes(
        exit_codes={
            name: ExitCodeEntry(code=int(info["code"]), description=str(info["description"]))
            for name, info in EXIT_CODE_MAP.items()
        }
    )


SPEC_EXIT_CODES = OperationSpec(
    id="agent.exit-codes",
    request=ExitCodesReq,
    response=ExitCodes,
    impl=exit_codes,
    summary="Print the stable exit codes for automation",
    description=(
        "Every tlgr command exits with one of these codes. They are a "
        "compatibility contract: a code never changes meaning."
    ),
    aliases=("exit-codes",),
    legacy_paths=("agent exit-codes",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=5,
    example={
        "exit_codes": {
            "SUCCESS": {"code": 0, "description": "Success"},
            "USAGE": {"code": 2, "description": "Usage or parse error"},
        }
    },
    example_args="agent exit-codes",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class SchemaDoc(Model):
    """`tlgr schema` output. Free-form on purpose: it *is* a schema document."""

    schema_version: int
    build: str
    ops: dict[str, Any] = {}


class SchemaReq(Request):
    path: Annotated[
        tuple[str, ...],
        arg(
            0,
            metavar="PATH",
            required=False,
            variadic=True,
            help="Limit the document to one command path, e.g. `schema message send`.",
        ),
    ] = ()
    include_hidden: Annotated[
        bool,
        opt("--include-hidden", help="Include hidden commands and flags."),
    ] = False


async def schema(ctx: OpContext, req: SchemaReq) -> dict[str, Any]:
    """Build the machine-readable schema document.

    The Click command tree is handed in through the context rather than
    imported: `ops/` must not import `cli/` (§2.2), and the tree is a CLI
    concern that only the CLI can describe.
    """
    from tlgr.schema import build_schema

    provider = getattr(ctx, "command_tree", None)
    command = provider(req.path, req.include_hidden) if callable(provider) else None
    return build_schema(path=req.path, command=command, include_hidden=req.include_hidden)


SPEC_SCHEMA = OperationSpec(
    id="agent.schema",
    request=SchemaReq,
    response=dict,
    impl=schema,
    summary="Print the machine-readable schema of the CLI",
    description=(
        "One JSON document: the command tree, and for every registered "
        "operation its request and response JSON Schema plus a validated "
        "example. Draft 2020-12."
    ),
    legacy_paths=("schema",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    example={"schema_version": 2, "build": "2.0.0", "ops": {}},
    example_args="schema message",
    tags=frozenset({"infrastructure", "agent-safe", "json-only"}),
)


class ParityReq(Request):
    domain: Annotated[
        str | None, opt("--domain", metavar="NAME", help="Only this catalog domain.")
    ] = None
    priority: Annotated[str | None, choice("P0", "P1", "P2", "P3", help="Only this priority.")] = (
        None
    )
    uncovered: Annotated[
        bool, opt("--uncovered", help="List the uncovered ids and why they are uncovered.")
    ] = False


async def parity(ctx: OpContext, req: ParityReq) -> dict[str, Any]:
    """Report catalog coverage, computed from the registry.

    The number is derived, never asserted: `tlgr.parity` subtracts what every
    `OperationSpec` declares it covers from what the catalog index says
    exists. A waived id stays in the denominator and is reported with the PR
    that closes it, so coverage cannot be improved by moving the goalposts.
    """
    from tlgr.parity import compute

    report = compute().to_dict()
    if req.domain:
        report["by_domain"] = {
            name: stats for name, stats in report["by_domain"].items() if name == req.domain
        }
        report["uncovered"] = [u for u in report["uncovered"] if u["domain"] == req.domain]
    if req.priority:
        report["by_priority"] = {
            name: stats for name, stats in report["by_priority"].items() if name == req.priority
        }
        report["uncovered"] = [u for u in report["uncovered"] if u["priority"] == req.priority]
    if not req.uncovered:
        report["uncovered"] = report["uncovered"][:20]
    return report


SPEC_PARITY = OperationSpec(
    id="agent.parity",
    request=ParityReq,
    response=dict,
    impl=parity,
    summary="Report feature-parity coverage against the Telegram catalog",
    description=(
        "Coverage is computed, not claimed: every operation declares the "
        "catalog ids it covers and this subtracts them from the index that "
        "ships in the package. `--uncovered` prints the full gap list with "
        "the PR that closes each one."
    ),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    example={"catalog_version": "2026-09-02", "required": 1797, "covered": 133, "percent": 7.4},
    example_args="agent parity",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


# ---------------------------------------------------------------------------
# agent whoami
# ---------------------------------------------------------------------------


#: Bumped when a documented output shape changes in a way a consumer must
#: notice. v2 is: RFC-3339 dates, marked chat ids, `Page` envelopes, `none`
#: as the default parse mode.
SCHEMA_VERSION = 2


class Whoami(Model):
    """The one call an agent makes before it does anything else.

    `output_schema_version` is the field to branch on: v2 changed a handful
    of documented shapes (RFC-3339 dates, marked chat ids, `Page` envelopes,
    `none` as the default parse mode), and a consumer that reads this can
    tell which set it is looking at without probing for one of them.
    """

    #: No default on purpose: `Model` omits fields that equal their default,
    #: and the one field an agent branches on must never be absent.
    output_schema_version: int
    account: str = ""
    user_id: int | None = None
    username: str | None = None
    phone: str | None = None
    daemon_running: bool = False
    config_dir: str = ""
    enabled_commands: list[str] = []
    daemon_uptime: float | None = None
    daemon_healthy: bool | None = None
    accounts_connected: list[str] = []
    accounts_disconnected: list[str] = []
    active_jobs: list[str] = []


class WhoamiReq(Request):
    pass


async def whoami(ctx: OpContext, req: WhoamiReq) -> Whoami:
    """Who am I, is the daemon up, and what is this build allowed to do.

    Local on purpose: the answer has to be available *when the daemon is
    not*, because "is it running?" is the question. The daemon is asked for
    the connection map only when its pid file says there is one to ask.
    """
    from tlgr.core.accounts import AccountManager
    from tlgr.core.paths import TlgrPaths

    paths = TlgrPaths(getattr(getattr(ctx, "paths", None), "base", None))
    manager = AccountManager(paths.base)
    alias = (getattr(ctx, "account", "") or "").strip() or (manager.get_active() or "")
    record = manager.get_account(alias) if alias else None
    info = Whoami(
        output_schema_version=SCHEMA_VERSION,
        account=alias,
        user_id=record.user_id if record else None,
        username=record.username if record else None,
        phone=record.phone if record else None,
        daemon_running=_daemon_alive(paths),
        config_dir=str(paths.base),
    )
    if not info.daemon_running:
        return info
    with contextlib.suppress(Exception):
        from tlgr.transport.client import DaemonClient

        client = DaemonClient(paths.base, timeout=5.0, auto_start=False)
        status = client.request("GET", "/v1/status") or {}
        info.daemon_uptime = status.get("uptime_seconds")
        info.daemon_healthy = status.get("healthy")
        connections = status.get("connections") or {}
        info.accounts_connected = sorted(a for a, ok in connections.items() if ok)
        info.accounts_disconnected = sorted(a for a, ok in connections.items() if not ok)
        info.active_jobs = [job["name"] for job in (status.get("jobs") or []) if job.get("running")]
    return info


def _daemon_alive(paths: Any) -> bool:
    """Is there a live process behind the pid file?

    A stale pid file is the normal aftermath of a crash, and reporting
    `daemon_running: true` for one sends an agent into a retry loop against
    a socket nobody is listening on.
    """
    import os

    try:
        pid = int(paths.pid.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


SPEC_WHOAMI = OperationSpec(
    id="agent.whoami",
    request=WhoamiReq,
    response=Whoami,
    impl=whoami,
    summary="Report the active account, daemon status and environment",
    description=(
        "The orientation call. `output_schema_version` is 2 for this build; "
        "branch on it rather than probing for a renamed field."
    ),
    legacy_paths=("agent whoami",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("account", "user_id", "username", "daemon_running"),
    example={
        "output_schema_version": 2,
        "account": "work",
        "user_id": 4242,
        "username": "me",
        "daemon_running": True,
        "config_dir": "/home/me/.tlgr",
    },
    example_args="agent whoami",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------


class CompletionScript(Model):
    shell: str = ""
    text: str = ""


class CompletionReq(Request):
    shell: Annotated[
        str,
        arg(0, metavar="SHELL", help="bash, zsh or fish."),
    ]


_COMPLETION_RC = {
    "bash": "~/.bashrc",
    "zsh": "~/.zshrc",
    "fish": "~/.config/fish/completions/tlgr.fish",
}
_COMPLETION_SOURCE = {
    "bash": 'eval "$(_TLGR_COMPLETE=bash_source tlgr)"',
    "zsh": 'eval "$(_TLGR_COMPLETE=zsh_source tlgr)"',
    "fish": "_TLGR_COMPLETE=fish_source tlgr | source",
}


async def completion(ctx: OpContext, req: CompletionReq) -> CompletionScript:
    """Print the shell completion script, exactly as v1 printed it.

    Click generates the completion itself; what this emits is the line that
    asks it to, plus where to put it. Tagged `text` so the human and plain
    forms are the script and nothing else — a table of one long cell would
    be unpasteable.
    """
    shell = req.shell.strip().lower()
    if shell not in _COMPLETION_SOURCE:
        raise UsageError(f"unknown shell {req.shell!r}: one of bash, zsh, fish", field="shell")
    line = _COMPLETION_SOURCE[shell]
    return CompletionScript(
        shell=shell, text=f"# Add to {_COMPLETION_RC[shell]}:\n# {line}\n\n{line}"
    )


SPEC_COMPLETION = OperationSpec(
    id="agent.completion",
    request=CompletionReq,
    response=CompletionScript,
    impl=completion,
    summary="Print the shell completion script for bash, zsh or fish",
    aliases=("completion",),
    legacy_paths=("completion",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=5,
    columns=("shell",),
    example={"shell": "bash", "text": '# Add to ~/.bashrc:\n# eval "$(…)"'},
    example_args="completion bash",
    tags=frozenset({"infrastructure", "agent-safe", "text"}),
)
