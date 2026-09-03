"""Local operations — the ones that need no account and no daemon.

They are the proof that the registry can carry an operation end to end: these
two were hand-written Click commands in v1 and are now generated, with the
same JSON a v1 agent already parses.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from tlgr.core.errors import EXIT_CODE_MAP, DaemonError, DaemonNotRunningError, UsageError
from tlgr.models.base import Model, Request
from tlgr.models.daemon import HealthSummary
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [
    "SPEC_CAPABILITIES",
    "SPEC_COMPLETION",
    "SPEC_EXIT_CODES",
    "SPEC_PARITY",
    "SPEC_SCHEMA",
    "SPEC_STATUS",
    "SPEC_WHOAMI",
    "Capabilities",
    "CompletionScript",
    "ErrorEntry",
    "ExitCodeEntry",
    "ExitCodes",
    "SchemaDoc",
    "WhoAmI",
]


class ExitCodeEntry(Model):
    code: int
    description: str


class ErrorEntry(Model):
    """One row of the RPC-error taxonomy (§7.2)."""

    name: str
    code: str
    exit: int
    http: int
    retryable: bool = False
    hint: str = ""
    #: The field a regex error captures: `FLOOD_WAIT_X` carries a wait,
    #: `*_MIGRATE_X` a data centre, `FILE_PART_X_MISSING` a part number. An
    #: agent that cannot see it can only retry blindly.
    extra: str = ""


class ExitCodes(Model):
    """Deliberately a mapping, not a list: this is the exact JSON v1 printed."""

    exit_codes: dict[str, ExitCodeEntry]
    errors: list[ErrorEntry] = []


class ExitCodesReq(Request):
    errors: Annotated[
        bool,
        opt("--errors", help="Also print the RPC error to exit-code mapping."),
    ] = False
    search: Annotated[
        str | None, opt("--search", metavar="TEXT", help="Filter the error table.")
    ] = None


#: Which regex-matched errors carry a parameter, and what it means. The number
#: in `FLOOD_WAIT_42` is not part of the name; dropping it would leave a
#: caller knowing it must wait and not for how long.
_ERROR_EXTRA: dict[str, str] = {
    "FloodWaitError": "wait_seconds",
    "SlowModeWaitError": "wait_seconds",
    "FloodPremiumWaitError": "wait_seconds",
    "FloodTestPhoneWaitError": "wait_seconds",
    "TakeoutInitDelayError": "wait_seconds",
    "PhoneMigrateError": "new_dc",
    "NetworkMigrateError": "new_dc",
    "UserMigrateError": "new_dc",
    "FileMigrateError": "new_dc",
    "FilePartMissingError": "which",
}


def _error_table() -> list[ErrorEntry]:
    """The §7.2 mapping, rendered from the one table that implements it."""
    from tlgr.core.errors import ERROR_MAP

    rows = [
        ErrorEntry(
            name=name,
            code=rule.code,
            exit=rule.exit_code,
            http=rule.http,
            retryable=rule.retryable,
            hint=rule.hint,
            extra=_ERROR_EXTRA.get(name, ""),
        )
        for name, rule in ERROR_MAP.items()
    ]
    rows.sort(key=lambda row: (row.exit, row.name))
    return rows


async def exit_codes(ctx: OpContext, req: ExitCodesReq) -> ExitCodes:
    """Return the stable exit-code table, and optionally the error taxonomy.

    The exit codes are a compatibility contract: a code never changes meaning.
    `--errors` adds the row *above* them — which Telethon exception becomes
    which code — so an agent can decide whether to retry without catching the
    exception itself.
    """
    table = ExitCodes(
        exit_codes={
            name: ExitCodeEntry(code=int(info["code"]), description=str(info["description"]))
            for name, info in EXIT_CODE_MAP.items()
        }
    )
    if req.errors:
        rows = _error_table()
        if req.search:
            needle = req.search.lower()
            rows = [row for row in rows if needle in row.name.lower() or needle in row.code.lower()]
        table.errors = rows
    return table


SPEC_EXIT_CODES = OperationSpec(
    id="agent.exit-codes",
    request=ExitCodesReq,
    response=ExitCodes,
    impl=exit_codes,
    summary="Print the stable exit codes, and the RPC error to exit-code mapping",
    description=(
        "Every tlgr command exits with one of these codes. They are a "
        "compatibility contract: a code never changes meaning. `--errors` "
        "adds the row above them — which Telethon exception becomes which "
        "code, whether it is retryable, and the parameter a regex error "
        "carries (`FLOOD_WAIT_42` is a wait of 42 seconds, not a distinct "
        "error)."
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
    covers=("updates.net-error-taxonomy", "updates.net-migrate-errors"),
    tags=frozenset({"agent-safe"}),
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
# whoami
# ---------------------------------------------------------------------------


class WhoAmI(Model):
    """What an agent needs before its first real command.

    `output_schema_version` is the field to branch on: v2 changed a handful of
    documented shapes (RFC-3339 dates, marked ids, `Page` envelopes, `none` as
    the default parse mode), and a consumer must be able to tell which set it
    is looking at without probing for one of them.
    """

    #: No default, deliberately: `Model` omits a field equal to its default,
    #: and the one field a consumer branches on must never be absent.
    output_schema_version: int
    account: str = ""
    user_id: int | None = None
    username: str | None = None
    phone: str | None = None
    is_bot: bool = False
    premium: bool = False
    frozen: bool = False
    daemon_running: bool = False
    daemon_healthy: bool | None = None
    daemon_version: str | None = None
    daemon_uptime: int | None = None
    accounts_connected: list[str] = []
    accounts_disconnected: list[str] = []
    active_jobs: list[str] = []
    config_dir: str = ""
    layer: int = 0
    telethon_version: str = ""
    tlgr_version: str = ""
    device_model: str = ""
    app_version: str = ""
    enabled_commands: list[str] = []


class WhoAmIReq(Request):
    pass


async def whoami(ctx: OpContext, req: WhoAmIReq) -> WhoAmI:
    """Report the active account, the daemon's health and this client's identity.

    Local, and it must stay local: this is what an agent calls to find out
    that the daemon is *not* running, so needing the daemon to answer would
    make the question unanswerable.

    `layer` is reported because it is the honest bound on everything else: a
    build on Telethon's layer 227 meets constructors from layer 229 in the
    wild and cannot parse them, and an agent that knows the number can predict
    which features will be missing instead of discovering them one failure at
    a time.
    """
    from tlgr import __version__
    from tlgr.core.accounts import AccountManager
    from tlgr.core.identity import load_identity
    from tlgr.core.paths import default_base

    base = default_base()
    manager = AccountManager(base)
    alias = ctx.account or manager.get_active() or ""
    account = manager.get_account(alias) if alias else None

    info = WhoAmI(
        output_schema_version=2,
        account=alias,
        user_id=account.user_id if account else None,
        username=account.username if account else None,
        phone=account.phone if account else None,
        frozen=bool(account and account.health.state == "frozen"),
        config_dir=str(base),
        layer=_layer(),
        telethon_version=_telethon_version(),
        tlgr_version=__version__,
    )
    with contextlib.suppress(Exception):
        identity = load_identity(base)
        info.device_model = identity.device_model
        info.app_version = identity.app_version

    enabled = getattr(ctx, "enable_commands", "") or ""
    if enabled:
        info.enabled_commands = [part.strip() for part in enabled.split(",") if part.strip()]

    status = _daemon_status()
    if status is None:
        return info

    daemon = status.get("daemon", {})
    info.daemon_running = True
    info.daemon_version = daemon.get("version")
    info.daemon_uptime = daemon.get("uptime_s")
    rows = status.get("accounts", []) or []
    info.accounts_connected = sorted(
        str(row.get("alias", "")) for row in rows if row.get("state") == "online"
    )
    info.accounts_disconnected = sorted(
        str(row.get("alias", "")) for row in rows if row.get("state") != "online"
    )
    # `healthy` is about the accounts, not about the process: v1 reported every
    # client the daemon held as connected, so a fully deaf daemon looked fine.
    info.daemon_healthy = bool(daemon.get("ready")) and not info.accounts_disconnected
    info.active_jobs = [
        str(job.get("name", "")) for job in (status.get("jobs") or []) if job.get("running")
    ]
    for row in rows:
        if row.get("alias") == alias:
            info.user_id = row.get("user_id") or info.user_id
            info.username = row.get("username") or info.username
            info.frozen = row.get("state") == "frozen"
    return info


def _telethon_layer() -> int:
    return _layer()


def _layer() -> int:
    with contextlib.suppress(Exception):
        from telethon.tl.alltlobjects import LAYER

        return int(LAYER)
    return 0


def _telethon_version() -> str:
    with contextlib.suppress(Exception):
        from tlgr.core.telethon_compat import telethon_version

        return telethon_version()
    return ""


def _probe() -> dict[str, Any] | None:
    """`/v1/status`, or None. Never starts a daemon to answer for it."""
    return _daemon_status()


def _daemon_status() -> dict[str, Any] | None:
    """`/v1/status`, never starting a daemon to answer a question about it."""
    from tlgr.core.paths import default_base
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(default_base(), timeout=2.0, auto_start=False, no_restart=True)
    with contextlib.suppress(Exception):
        return client.probe_status()
    return None


SPEC_WHOAMI = OperationSpec(
    id="agent.whoami",
    request=WhoAmIReq,
    response=WhoAmI,
    impl=whoami,
    summary="Report the active account, daemon health and client identity",
    description=(
        "The first call an agent should make. `output_schema_version` says "
        "which output contract it is talking to, and `layer` says how far "
        "behind Telegram's current schema this build is."
    ),
    legacy_paths=("agent whoami",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    example={
        "output_schema_version": 2,
        "account": "work",
        "user_id": 777,
        "daemon_running": True,
        "daemon_healthy": True,
        "layer": 227,
        "tlgr_version": "2.0.0",
    },
    example_args="agent whoami",
    covers_partial=("updates.invoke-init-connection", "updates.invoke-with-layer"),
    coverage_note=(
        "reports the identity and layer this build declares; setting them is "
        "`config set`, and `status` reports the connection."
    ),
    tags=frozenset({"agent-safe"}),
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


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


class Capabilities(Model):
    """What this build can do, cannot do, and will not do.

    The third list is the one that matters. "Cannot" is a gap somebody may
    close; "will not" is a decision, and an agent that cannot tell them apart
    will keep asking for the second kind for ever.
    """

    layer: int = 0
    telethon_version: str = ""
    tlgr_version: str = ""
    event_types: int = 0
    unsupported_constructors: list[str] = []
    secret_chats: str = ""
    pfs: str = ""
    calls_media: str = ""
    push: str = ""
    presence_policy: str = ""
    read_receipt_policy: str = ""
    prohibited: list[dict[str, str]] = []
    premium_gated: list[str] = []
    bot_only: list[str] = []
    admin_only: list[str] = []
    limits: dict[str, Any] = {}
    operations: int = 0


class CapabilitiesReq(Request):
    section: Annotated[
        str | None,
        choice("protocol", "policy", "gates", "events", "limits", help="Restrict the report."),
    ] = None


#: Things tlgr will not do, and why. Not a list of missing features: each of
#: these is a decision, and an agent that reads it stops asking.
_PROHIBITED: tuple[tuple[str, str], ...] = (
    (
        "fake a read receipt",
        "Not calling messages.readHistory is fine — you simply did not read it. "
        "Reading and then suppressing the receipt violates api terms 1.4.",
    ),
    (
        "suppress typing status",
        "Same clause. tlgr sends setTyping when it is composing and never lies about it.",
    ),
    (
        "misrepresent online status",
        "'Ghost mode' is explicitly forbidden by api terms 1.4. presence.mode defaults to "
        "'off', which announces nothing rather than claiming to be offline while reading.",
    ),
    (
        "pass an integrity attestation",
        "invokeWithGooglePlayIntegrity, invokeWithApnsSecret and invokeWithReCaptcha are "
        "device-attestation flows. tlgr reports the demand and stops rather than "
        "impersonating a phone.",
    ),
    (
        "execute a payment",
        "Buying stars, paying an invoice, bidding in a gift auction and withdrawing "
        "revenue are financial actions a person performs, not an agent.",
    ),
)

_PREMIUM_GATED = (
    "voice transcription",
    "saved-message reaction tags",
    "story stealth mode",
    "uploading notification sounds",
    "larger upload limits and folder counts",
)

_BOT_ONLY = (
    "inline queries and callback answers",
    "shipping and pre-checkout answers",
    "business-connection messages",
    "chat-boost updates",
)

_ADMIN_ONLY = (
    "channel participant updates for other users",
    "pending join requests",
    "the admin log",
)


async def capabilities(ctx: OpContext, req: CapabilitiesReq) -> Capabilities:
    """The honest-limits report, meant to be read before planning.

    Everything here is derived or fixed, never guessed: the layer and the
    unparseable constructors come from the event taxonomy, the operation count
    from the registry, and the policy entries are the decisions recorded in
    ARCHITECTURE and the API terms.
    """
    from tlgr import __version__
    from tlgr.core import eventtypes
    from tlgr.registry import REGISTRY

    report = Capabilities(
        layer=_layer(),
        telethon_version=_telethon_version(),
        tlgr_version=__version__,
        event_types=len(eventtypes.TYPES),
        unsupported_constructors=sorted(eventtypes.NEWER_THAN_LAYER_227),
        operations=len(REGISTRY),
        secret_chats=(
            "envelope only: Telethon implements no MTProto 2.0 end-to-end layer, so tlgr "
            "can report that encrypted traffic exists and acknowledge the qts, and cannot "
            "read or send it"
        ),
        pfs=(
            "not implemented: auth.bindTempAuthKey needs changes inside Telethon's "
            "MTProtoSender. The practical mitigation is protecting the session file, which "
            "is written 0600 and audited at start"
        ),
        calls_media=(
            "signalling only: ring, accept, reject and hang up work; carrying the audio or "
            "video stream needs tgcalls and is out of scope for a CLI"
        ),
        push=(
            "not registered: the daemon holds a socket, so it has no need of push. "
            "`tlgr events decode --push` reads a payload a phone relayed"
        ),
        presence_policy=(
            "presence.mode defaults to 'off': tlgr announces nothing rather than claiming "
            "to be offline while reading"
        ),
        read_receipt_policy=(
            "never faked: reading without calling messages.readHistory is allowed, "
            "suppressing a receipt after reading is not"
        ),
        prohibited=[{"action": action, "reason": reason} for action, reason in _PROHIBITED],
        premium_gated=list(_PREMIUM_GATED),
        bot_only=list(_BOT_ONLY),
        admin_only=list(_ADMIN_ONLY),
    )

    if req.section:
        return _section(report, req.section)
    return report


def _section(report: Capabilities, section: str) -> Capabilities:
    """Blank everything outside the requested section, keeping the shape."""
    keep = {
        "protocol": {"layer", "telethon_version", "tlgr_version", "unsupported_constructors"},
        "policy": {"prohibited", "presence_policy", "read_receipt_policy"},
        "gates": {"premium_gated", "bot_only", "admin_only"},
        "events": {"event_types", "unsupported_constructors"},
        "limits": {"limits", "operations"},
    }[section]
    trimmed = Capabilities()
    for field in keep:
        setattr(trimmed, field, getattr(report, field))
    return trimmed


SPEC_CAPABILITIES = OperationSpec(
    id="agent.capabilities",
    request=CapabilitiesReq,
    response=Capabilities,
    impl=capabilities,
    summary="Report what this build can do, cannot do, and will not do",
    description=(
        "Three different things, deliberately separated. `unsupported_*` is "
        "what this Telethon layer cannot parse; `premium_gated`/`bot_only`/"
        "`admin_only` is what this *account* may not reach; `prohibited` is "
        "what tlgr refuses on purpose, with the reason. Only the first is a "
        "gap somebody might close."
    ),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    example={
        "layer": 227,
        "tlgr_version": "2.0.0",
        "event_types": 114,
        "prohibited": [{"action": "fake a read receipt", "reason": "api terms 1.4"}],
    },
    example_args="agent capabilities --section policy",
    covers=("updates.session-pfs", "updates.sync-disable-updates"),
    covers_partial=(
        "updates.invoke-with-layer",
        "updates.ops-single-updates-consumer",
        "updates.presence-read-receipts-policy",
        "updates.sync-old-layer-socket-reset",
    ),
    coverage_note=(
        "states the policy and the layer bound; the switches themselves are "
        "`config set`, and recovery is `daemon reconnect`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# status — the one-screen summary
# ---------------------------------------------------------------------------


class HealthReq(Request):
    check: Annotated[bool, opt("--check", help="Exit non-zero when anything is unhealthy.")] = False


async def account_status(ctx: OpContext, req: HealthReq) -> HealthSummary:
    """One screen: account, connection, sync lag, daemon, floods.

    Deliberately the union of several groups rather than a link to them. The
    states it surfaces — frozen, terms not accepted, an unconfirmed new login,
    a flood the account still owes — are the ones in which *every other
    command* starts failing, and a user whose sends are being refused should
    not have to know which of five nouns to ask first.
    """
    from tlgr.core.accounts import AccountManager
    from tlgr.core.paths import default_base

    base = default_base()
    manager = AccountManager(base)
    alias = ctx.account or manager.get_active() or ""
    account = manager.get_account(alias) if alias else None

    summary = HealthSummary(
        account=alias,
        user_id=account.user_id if account else None,
        username=account.username if account else None,
        layer=_telethon_layer(),
    )

    status = _probe()
    if status is None:
        summary.problems.append("the daemon is not running (tlgr daemon start)")
        if req.check:
            raise DaemonNotRunningError("the daemon is not answering on its socket")
        return summary

    daemon = status.get("daemon", {})
    summary.daemon_running = True
    summary.jobs_running = len([j for j in (status.get("jobs") or []) if j.get("running")])
    summary.webhook_enabled = bool((status.get("webhook") or {}).get("enabled"))

    rows = [row for row in (status.get("accounts") or []) if not alias or row.get("alias") == alias]
    row = rows[0] if rows else {}
    state = str(row.get("state", "unknown"))
    summary.authorized = state not in ("needs_login", "unknown", "not_connected")
    summary.connected = state == "online"
    summary.dc_id = row.get("dc_id")
    summary.proxy = row.get("proxy")
    summary.behind_seconds = row.get("behind_seconds")
    summary.flood_waits = int(row.get("flood_entries") or 0)
    summary.daemon_healthy = bool(daemon.get("ready")) and state == "online"

    if state == "needs_login":
        summary.problems.append(f"{alias} needs to log in again (tlgr account add)")
    if state == "frozen":
        summary.frozen = {"state": "frozen", "reason": row.get("reason") or ""}
        summary.problems.append(
            f"{alias} is frozen by Telegram; see `tlgr config app get --frozen` for the appeal link"
        )
    if str(row.get("circuit", "closed")) != "closed":
        summary.problems.append(
            f"the send circuit breaker is open for {alias}: {row.get('circuit_reason') or 'spam flagged'}"
        )
    if summary.flood_waits:
        summary.problems.append(
            f"{summary.flood_waits} rate-limit deadline(s) outstanding (tlgr daemon flood list)"
        )
    if not daemon.get("ready"):
        summary.problems.append("the daemon is running but not ready")

    if req.check and summary.problems:
        raise DaemonError("; ".join(summary.problems))
    return summary


SPEC_STATUS = OperationSpec(
    id="agent.status",
    request=HealthReq,
    response=HealthSummary,
    impl=account_status,
    summary="One-screen health summary: account, connection, sync lag, daemon, floods",
    description=(
        "The union of several groups on purpose. A frozen account, an open "
        "circuit breaker, an outstanding flood deadline and a daemon that is "
        "up but not ready are the states in which every *other* command "
        "starts failing, and `--check` turns them into an exit code a monitor "
        "can read."
    ),
    legacy_paths=("status",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("account", "connected", "daemon_healthy", "behind_seconds", "problems"),
    example={
        "account": "work",
        "authorized": True,
        "connected": True,
        "daemon_running": True,
        "daemon_healthy": True,
        "layer": 227,
    },
    example_args="status --check",
    covers=("updates.invoke-with-layer",),
    covers_partial=("updates.config-account-frozen", "updates.net-flood-wait"),
    coverage_note=(
        "surfaces the states; the detail is `config app get --frozen` and `daemon flood list`."
    ),
    tags=frozenset({"agent-safe"}),
)
