"""The `config` group: this installation's settings, and Telegram's own.

Four different things wear the word "config", and keeping them apart is most
of this module's job:

* `config get/set/list/keys/path/init/validate` — **local** settings, in
  `config.toml`. Identity, transport, proxy, flood budget, presence policy,
  event buffer.
* `config server get` — Telegram's MTProto configuration (`help.getConfig`):
  `message_length_max`, `edit_time_limit`, the DC list.
* `config app get` — the client configuration (`help.getAppConfig`): the
  limits and kill switches almost every feature is gated on, plus the
  account-freeze fields that turn a bare `FROZEN_METHOD_INVALID` into
  something actionable.
* `config info get`, `config country list`, `config promo get`,
  `config suggestion list` — the flat read-only `help.*` endpoints.

v1 had nine documented keys and parsed the file with `raw.get(x, default)` at
every call site, so a typo was silently the default. `config keys` is now
machine-readable and `config set` validates against it, which is the
difference between "tlgr ignored your setting" and an error naming the key.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.config import (
    ConfigEntry,
    ConfigKey,
    ConfigPaths,
    ConfigValue,
    InitResult,
    ValidationIssue,
    ValidationReport,
)
from tlgr.models.net import (
    AppConfigDoc,
    Country,
    CountryCode,
    DcOption,
    InfoTopic,
    PromoData,
    ServerConfig,
)
from tlgr.models.page import Page
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]


# ---------------------------------------------------------------------------
# The key catalogue (§10.2)
# ---------------------------------------------------------------------------


def _key(
    key: str,
    section: str,
    field: str,
    type_: str,
    default: Any,
    help_: str,
    *,
    scope: str = "global",
    restart: bool = False,
    secret: bool = False,
    choices: tuple[str, ...] = (),
) -> tuple[ConfigKey, tuple[str, str]]:
    return (
        ConfigKey(
            key=key,
            type=type_,
            default=default,
            scope=scope,
            section=section,
            requires_restart=restart,
            secret=secret,
            help=help_,
            choices=list(choices),
        ),
        (section, field),
    )


#: Every documented knob, machine-readable so an agent can discover them
#: without reading prose. `requires_restart` is not decoration: an identity or
#: transport key only takes effect on the next `initConnection`, and a `set`
#: that pretended otherwise would be a lie about what happened.
_CATALOGUE: tuple[tuple[ConfigKey, tuple[str, str]], ...] = (
    # -- identity: what Settings → Devices shows the user ------------------
    _key(
        "client.device_model",
        "identity",
        "device_model",
        "string",
        "",
        "Device name shown in Settings → Devices. Must be honest: it is how a "
        "user recognises — and safely terminates — the tlgr session.",
        restart=True,
    ),
    _key(
        "client.system_version",
        "identity",
        "system_version",
        "string",
        "",
        "System version sent in initConnection.",
        restart=True,
    ),
    _key(
        "client.lang_code",
        "identity",
        "lang_code",
        "string",
        "",
        "Language tlgr asks the server to localise its own error and service messages into.",
        restart=True,
    ),
    _key(
        "client.system_lang_code",
        "identity",
        "system_lang_code",
        "string",
        "",
        "System language sent in initConnection.",
        restart=True,
    ),
    _key(
        "client.tz_offset",
        "identity",
        "tz_offset",
        "bool",
        True,
        "Send this host's UTC offset in initConnection params. Business hours "
        "and scheduled-message display depend on it.",
        restart=True,
    ),
    # -- network -----------------------------------------------------------
    _key(
        "net.proxy",
        "network",
        "proxy",
        "string",
        "",
        "Active proxy URL. Prefer `tlgr proxy set`, which reconnects for you.",
        restart=True,
        secret=True,
    ),
    _key(
        "net.ipv6",
        "network",
        "ipv6",
        "bool",
        False,
        "Connect over IPv6. One argument; matters on IPv6-only hosts.",
        restart=True,
    ),
    _key(
        "net.connect_timeout",
        "network",
        "connect_timeout",
        "int",
        10,
        "Seconds to wait for a connection before giving up.",
        restart=True,
    ),
    _key(
        "net.connection",
        "network",
        "connection",
        "string",
        "tcp_full",
        "MTProto transport: tcp_full, tcp_abridged, tcp_intermediate, "
        "tcp_obfuscated or http. Obfuscated helps on hostile networks.",
        restart=True,
        choices=("tcp_full", "tcp_abridged", "tcp_intermediate", "tcp_obfuscated", "http"),
    ),
    # -- daemon ------------------------------------------------------------
    _key("daemon.auto_start", "daemon", "auto_start", "bool", True, "Start the daemon on CLI use."),
    _key(
        "daemon.log_level",
        "daemon",
        "log_level",
        "string",
        "info",
        "Daemon log level.",
        choices=("debug", "info", "warning", "error"),
    ),
    _key(
        "daemon.idle_timeout",
        "daemon",
        "idle_timeout",
        "int",
        1800,
        "Seconds of inactivity before the daemon stops; 0 disables. An idle "
        "stop with catch-up off is a permanent sync hole.",
    ),
    _key(
        "daemon.event_buffer",
        "daemon",
        "event_buffer",
        "int",
        4096,
        "Events kept per account for `--since` replay. Older ones produce a "
        "`gap` frame rather than silence.",
        restart=True,
    ),
    _key(
        "daemon.event_workers",
        "daemon",
        "event_workers",
        "int",
        8,
        "Worker lanes the bus dispatches handlers on, keyed by chat.",
        restart=True,
    ),
    _key(
        "daemon.state_save_interval",
        "daemon",
        "state_save_interval",
        "int",
        60,
        "How often pts/qts and the entity cache are flushed. Telethon only "
        "writes them on a clean disconnect.",
    ),
    _key(
        "daemon.drain_seconds",
        "daemon",
        "drain_seconds",
        "int",
        30,
        "How long a shutdown waits for in-flight requests.",
    ),
    _key(
        "daemon.resync_depth",
        "daemon",
        "resync_depth",
        "int",
        50,
        "Messages re-read per channel after a differenceTooLong.",
    ),
    # -- flood -------------------------------------------------------------
    _key(
        "flood.sleep_threshold",
        "flood",
        "sleep_threshold",
        "int",
        120,
        "Seconds of FLOOD_WAIT tlgr will sleep off inside a request. Longer "
        "waits come back as RATE_LIMITED with the deadline.",
    ),
    _key(
        "flood.max_wait",
        "flood",
        "max_wait",
        "int",
        600,
        "Ceiling on the sleep threshold, whatever a caller asks for.",
    ),
    _key(
        "flood.persist",
        "flood",
        "persist",
        "bool",
        True,
        "Remember flood deadlines across restarts. Off means a fresh process "
        "re-trips every wait it had already earned.",
    ),
    # -- presence ----------------------------------------------------------
    _key(
        "presence.mode",
        "presence",
        "mode",
        "string",
        "off",
        "off | online | mirror. Default off: tlgr announces nothing rather "
        "than claiming to be offline while reading, which api terms 1.4 "
        "forbids.",
        choices=("off", "online", "mirror"),
    ),
    # -- limits ------------------------------------------------------------
    _key(
        "limits.entity_cache",
        "limits",
        "entity_cache",
        "int",
        20000,
        "Peers Telethon keeps access hashes for in memory.",
        restart=True,
    ),
    _key(
        "limits.request_retries",
        "limits",
        "request_retries",
        "int",
        5,
        "Attempts per request before giving up.",
        restart=True,
    ),
    _key(
        "limits.dialog_scan_max",
        "limits",
        "dialog_scan_max",
        "int",
        5000,
        "How many dialogs a peer resolution will scan before answering "
        "INDETERMINATE rather than 'not found'.",
    ),
    _key(
        "limits.max_album",
        "limits",
        "max_album",
        "int",
        10,
        "Maximum files in one album.",
    ),
    # -- defaults ----------------------------------------------------------
    _key(
        "defaults.output",
        "defaults",
        "output",
        "string",
        "human",
        "Default output mode.",
        choices=("human", "json", "plain"),
    ),
    _key(
        "defaults.parse_mode",
        "defaults",
        "parse_mode",
        "string",
        "none",
        "Default message parse mode. `none` because v1's markdown default "
        "silently ate underscores and asterisks in ordinary text.",
        choices=("none", "md", "html"),
    ),
    _key(
        "defaults.require_account",
        "defaults",
        "require_account",
        "bool",
        False,
        "Require -a on every command instead of falling back to a default.",
    ),
    _key(
        "defaults.confirm_destructive",
        "defaults",
        "confirm_destructive",
        "bool",
        True,
        "Prompt before a destructive command off a TTY.",
    ),
    _key(
        "defaults.timezone",
        "defaults",
        "timezone",
        "string",
        "",
        "Timezone for human date rendering. Empty means the host's.",
    ),
    _key(
        "defaults.legacy_dates",
        "defaults",
        "legacy_dates",
        "bool",
        False,
        "Print v1's `str(datetime)` spelling instead of RFC-3339.",
    ),
    _key(
        "accounts.default",
        "accounts",
        "default",
        "string",
        "",
        "Account used when -a is not given.",
    ),
    # -- security / policy -------------------------------------------------
    _key(
        "security.require_token",
        "security",
        "require_token",
        "bool",
        False,
        "Require X-Tlgr-Token on the IPC socket as well as the peer-uid check.",
        restart=True,
    ),
    _key(
        "security.peer_uid_check",
        "security",
        "peer_uid_check",
        "bool",
        True,
        "Refuse socket connections from another uid.",
        restart=True,
    ),
    _key(
        "logging.redact",
        "logging",
        "redact",
        "bool",
        True,
        "Redact access hashes, tokens and secrets from the log.",
    ),
)

KEYS: dict[str, ConfigKey] = {entry[0].key: entry[0] for entry in _CATALOGUE}
_FIELDS: dict[str, tuple[str, str]] = {entry[0].key: entry[1] for entry in _CATALOGUE}

#: v1 spelled nine of these without a section prefix. §12.4: a documented name
#: does not stop working because the catalogue grew a namespace.
_LEGACY_KEYS: dict[str, str] = {
    "output": "defaults.output",
    "drop_author": "defaults.drop_author",
    "delete_after": "defaults.delete_after",
    "default_account": "accounts.default",
    "require_account": "defaults.require_account",
    "auto_start": "daemon.auto_start",
    "log_level": "daemon.log_level",
    "idle_timeout": "daemon.idle_timeout",
    "flood_wait_max": "flood.sleep_threshold",
}


def _resolve_key(name: str) -> ConfigKey:
    key = _LEGACY_KEYS.get(name, name)
    found = KEYS.get(key)
    if found is None:
        raise NotFoundError(f"unknown config key {name!r}. Run: tlgr config keys")
    return found


def _paths() -> Any:
    from tlgr.core.paths import TlgrPaths

    return TlgrPaths()


def _raw() -> dict[str, Any]:
    from tlgr.core.config import _load_toml

    return _load_toml(_paths().config)


def _write(document: dict[str, Any]) -> None:
    from tlgr.core.config import _save_toml

    _save_toml(_paths().config, document)


def _stored(document: dict[str, Any], key: ConfigKey) -> tuple[Any, bool]:
    section, field = _FIELDS[key.key]
    block = document.get(section)
    if isinstance(block, dict) and field in block:
        return block[field], True
    return key.default, False


def _coerce(key: ConfigKey, raw: str) -> Any:
    """A CLI string into the key's declared type, or a usage error naming it."""
    if key.type == "bool":
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        raise UsageError(f"{key.key} is a boolean; got {raw!r}", field="value")
    if key.type == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise UsageError(f"{key.key} is an integer; got {raw!r}", field="value") from exc
    if key.choices and raw not in key.choices:
        raise UsageError(
            f"{key.key} must be one of {', '.join(key.choices)}; got {raw!r}", field="value"
        )
    return raw


def _redact(key: ConfigKey, value: Any) -> Any:
    return "<redacted>" if key.secret and value else value


# ---------------------------------------------------------------------------
# Local settings
# ---------------------------------------------------------------------------


class ConfigGetReq(Request):
    key: Annotated[str, arg(0, metavar="KEY")]
    source: Annotated[
        bool, opt("--source", help="Also report which file and section the value came from.")
    ] = False


async def config_get(ctx: OpContext, req: ConfigGetReq) -> ConfigValue:
    """Read one local key. Server-side configuration is a different noun."""
    key = _resolve_key(req.key)
    value, present = _stored(_raw(), key)
    return ConfigValue(
        key=key.key,
        value=_redact(key, value),
        default=key.default,
        source=(f"{_paths().config} [{key.section}]" if present else "default")
        if req.source
        else ("file" if present else "default"),
        help=key.help,
        requires_restart=key.requires_restart,
    )


SPEC_CONFIG_GET = OperationSpec(
    id="config.get",
    request=ConfigGetReq,
    response=ConfigValue,
    impl=config_get,
    summary="Read one local configuration key",
    legacy_paths=("config get",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("key", "value", "source"),
    example={"key": "daemon.idle_timeout", "value": 0, "default": 1800, "source": "file"},
    example_args="config get daemon.idle_timeout",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigSetReq(Request):
    key: Annotated[str, arg(0, metavar="KEY")]
    value: Annotated[str, arg(1, metavar="VALUE")]
    apply: Annotated[
        bool, opt("--apply/--no-apply", help="Ask a running daemon to adopt the change now.")
    ] = True


async def config_set(ctx: OpContext, req: ConfigSetReq) -> ConfigValue:
    """Write one local key, validated against the catalogue.

    Validation is the point. v1 read the file with `raw.get(key, default)` at
    every call site, so a typo or a wrong type was silently the default and
    the user's setting simply never happened.
    """
    key = _resolve_key(req.key)
    section, field = _FIELDS[key.key]
    value = _coerce(key, req.value)
    document = _raw()
    previous, present = _stored(document, key)
    if present and previous == value:
        ctx.mark_already()
        return ConfigValue(
            key=key.key,
            value=_redact(key, value),
            previous=_redact(key, previous),
            already=True,
            requires_restart=key.requires_restart,
        )

    document.setdefault(section, {})[field] = value
    _write(document)

    applied = False
    if req.apply:
        applied = _reload_daemon()
    if key.requires_restart and not applied:
        ctx.warn(f"{key.key} takes effect on the next reconnect: tlgr daemon reconnect")
    return ConfigValue(
        key=key.key,
        value=_redact(key, value),
        previous=_redact(key, previous) if present else None,
        default=key.default,
        updated=True,
        requires_restart=key.requires_restart,
        applied=applied,
    )


def _reload_daemon() -> bool:
    """Ask a running daemon to re-read the file. Absent daemon is not an error."""
    from tlgr.core.paths import default_base
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(default_base(), timeout=10.0, auto_start=False, no_restart=True)
    with contextlib.suppress(Exception):
        client.admin("reload", {"what": ["config", "policy"]})
        return True
    return False


SPEC_CONFIG_SET = OperationSpec(
    id="config.set",
    request=ConfigSetReq,
    response=ConfigValue,
    impl=config_set,
    summary="Write one local configuration key",
    description=(
        "Validated against `config keys`: a wrong type or an unknown key is "
        "an error naming it, not a silent fallback to the default. Identity "
        "and transport keys only take effect on the next `initConnection`, "
        "and the response says so."
    ),
    legacy_paths=("config set",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=30,
    example={"key": "daemon.idle_timeout", "value": 0, "previous": 1800, "updated": True},
    example_args="config set daemon.idle_timeout 0",
    covers=(
        "updates.event-report-message-delivery",
        "updates.invoke-init-connection",
        "updates.net-local-addr",
        "updates.net-network-type",
        "updates.net-parallel-connections",
        "updates.net-proxy-for-calls",
        "updates.net-transport-mode",
        "updates.sync-dispatch-ordering",
    ),
    covers_partial=(
        "updates.config-dns-fallback",
        "updates.invoke-client-proxy-declare",
        "updates.invoke-init-params-json",
        "updates.net-flood-wait",
        "updates.net-ipv6",
        "updates.net-proxy-autoswitch",
        "updates.net-proxy-system",
        "updates.net-test-dc",
        "updates.net-timeouts-retries",
        "updates.ops-reconnect-health",
        "updates.ops-single-updates-consumer",
        "updates.presence-keepalive-period",
        "updates.presence-set-online",
        "updates.sync-catch-up-on-start",
        "updates.sync-channel-short-poll",
        "updates.sync-disable-updates",
        "updates.sync-new-session-triggers-diff",
    ),
    coverage_note=(
        "sets the switch; the behaviour it selects belongs to the group that "
        "implements it (`proxy`, `sync`, `net`, `daemon`)."
    ),
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigUnsetReq(Request):
    key: Annotated[str, arg(0, metavar="KEY")]
    apply: Annotated[
        bool, opt("--apply/--no-apply", help="Ask a running daemon to adopt the change now.")
    ] = True


async def config_unset(ctx: OpContext, req: ConfigUnsetReq) -> ConfigValue:
    """Remove a key, reverting it to its documented default."""
    key = _resolve_key(req.key)
    section, field = _FIELDS[key.key]
    document = _raw()
    previous, present = _stored(document, key)
    if not present:
        ctx.mark_already()
        return ConfigValue(key=key.key, default=key.default, already=True)
    del document[section][field]
    if not document[section]:
        del document[section]
    _write(document)
    if req.apply:
        _reload_daemon()
    return ConfigValue(
        key=key.key, previous=_redact(key, previous), default=key.default, removed=True
    )


SPEC_CONFIG_UNSET = OperationSpec(
    id="config.unset",
    request=ConfigUnsetReq,
    response=ConfigValue,
    impl=config_unset,
    summary="Remove a local configuration key (revert to its default)",
    legacy_paths=("config unset",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=30,
    example={"key": "daemon.idle_timeout", "previous": 0, "default": 1800, "removed": True},
    example_args="config unset daemon.idle_timeout",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigListReq(Request):
    section: Annotated[str | None, opt("--section", metavar="NAME", help="Only this section.")] = (
        None
    )
    defaults: Annotated[
        bool, opt("--defaults", help="Include keys still at their default value.")
    ] = False


async def config_list(ctx: OpContext, req: ConfigListReq) -> Page[ConfigEntry]:
    """The effective configuration, with where each value came from.

    Secrets — proxy passwords, MTProxy secrets, the webhook token, `api_hash`
    — are redacted. `config list` is the command people paste into bug
    reports.
    """
    document = _raw()
    rows: list[ConfigEntry] = []
    for key in KEYS.values():
        if req.section and key.section != req.section and not key.key.startswith(f"{req.section}."):
            continue
        value, present = _stored(document, key)
        if not present and not req.defaults:
            continue
        rows.append(
            ConfigEntry(
                key=key.key,
                value=_redact(key, value),
                default=key.default,
                source="file" if present else "default",
                scope=key.scope,
            )
        )
    return build_page(rows, op="config.list", kind=PageKind.LOCAL, has_more=False, total=len(rows))


SPEC_CONFIG_LIST = OperationSpec(
    id="config.list",
    request=ConfigListReq,
    response=Page[ConfigEntry],
    impl=config_list,
    summary="Show the effective local configuration",
    description="Secrets are redacted: this is the command people paste into bug reports.",
    legacy_paths=("config list",),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("key", "value", "source"),
    example={
        "items": [{"key": "daemon.idle_timeout", "value": 0, "source": "file"}],
        "has_more": False,
    },
    example_args="config list --defaults",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigKeysReq(Request):
    section: Annotated[str | None, opt("--section", metavar="NAME", help="Only this section.")] = (
        None
    )
    search: Annotated[
        str | None, opt("--search", metavar="TEXT", help="Substring match on key or help.")
    ] = None


async def config_keys(ctx: OpContext, req: ConfigKeysReq) -> Page[ConfigKey]:
    """Every documented knob, with its type, default and restart requirement.

    Machine-readable on purpose: an agent that has to discover the knobs from
    prose will get one wrong, and a wrong key was silently the default in v1.
    """
    rows = list(KEYS.values())
    if req.section:
        rows = [
            row
            for row in rows
            if row.section == req.section or row.key.startswith(f"{req.section}.")
        ]
    if req.search:
        needle = req.search.lower()
        rows = [row for row in rows if needle in row.key.lower() or needle in row.help.lower()]
    rows.sort(key=lambda row: row.key)
    return build_page(rows, op="config.keys", kind=PageKind.LOCAL, has_more=False, total=len(rows))


SPEC_CONFIG_KEYS = OperationSpec(
    id="config.keys",
    request=ConfigKeysReq,
    response=Page[ConfigKey],
    impl=config_keys,
    summary="List every documented configuration key with its type and default",
    legacy_paths=("config keys",),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("key", "type", "default", "requires_restart", "help"),
    example={
        "items": [
            {
                "key": "presence.mode",
                "type": "string",
                "default": "off",
                "help": "off | online | mirror.",
            }
        ],
        "has_more": False,
    },
    example_args="config keys --section presence",
    covers=(
        "updates.invoke-init-params-json",
        "updates.net-timeouts-retries",
        "updates.presence-read-receipts-policy",
        "updates.presence-set-online",
    ),
    covers_partial=("updates.invoke-init-connection",),
    coverage_note="documents the knobs; writing one is `config set`.",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigPathReq(Request):
    file: Annotated[
        str | None,
        choice(
            "config",
            "jobs",
            "webhook",
            "sessions",
            "logs",
            "dead-letter",
            "socket",
            "pid",
            "secrets",
            help="Print just one path.",
        ),
    ] = None


async def config_path(ctx: OpContext, req: ConfigPathReq) -> ConfigPaths:
    """Where everything lives.

    The session files and the secrets file are credential material at mode
    0600: exclude them from backups, and never paste their contents.
    """
    paths = _paths()
    report = ConfigPaths(
        config_dir=str(paths.base),
        config=str(paths.config),
        jobs=str(paths.jobs),
        webhook=str(paths.webhook),
        secrets=str(paths.token),
        sessions=str(paths.accounts),
        logs=str(paths.logs),
        socket=str(paths.socket),
        pid=str(paths.pid),
        dead_letter=str(paths.dead_letter),
    )
    if req.file:
        chosen = {
            "config": report.config,
            "jobs": report.jobs,
            "webhook": report.webhook,
            "sessions": report.sessions,
            "logs": report.logs,
            "dead-letter": report.dead_letter,
            "socket": report.socket,
            "pid": report.pid,
            "secrets": report.secrets,
        }[req.file]
        return ConfigPaths(config_dir=report.config_dir, path=chosen)
    return report


SPEC_CONFIG_PATH = OperationSpec(
    id="config.path",
    request=ConfigPathReq,
    response=ConfigPaths,
    impl=config_path,
    summary="Print the paths of the configuration, jobs, webhook, session and log files",
    legacy_paths=("config path",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("config_dir", "config", "jobs", "webhook"),
    example={"config_dir": "~/.tlgr", "config": "~/.tlgr/config.toml"},
    example_args="config path --file socket",
    covers_partial=("updates.session-persistence",),
    coverage_note="says where the session lives; persisting it is the session supervisor's.",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigInitReq(Request):
    overwrite: Annotated[bool, opt("--overwrite", help="Replace files that already exist.")] = False


async def config_init(ctx: OpContext, req: ConfigInitReq) -> InitResult:
    """Create the default configuration files, all at mode 0600.

    v1 wrote all three with `write_text`, world-readable — and `webhook.toml`
    holds a token (SEC-07).
    """
    from tlgr.core.paths import write_private

    paths = _paths()
    paths.ensure_base()
    created: list[str] = []
    skipped: list[str] = []
    for name, path, body in (
        ("config.toml", paths.config, _DEFAULT_CONFIG),
        ("jobs.yaml", paths.jobs, _DEFAULT_JOBS),
        ("webhook.toml", paths.webhook, _DEFAULT_WEBHOOK),
    ):
        if path.exists() and not req.overwrite:
            skipped.append(name)
            continue
        write_private(path, body)
        created.append(name)
    if not created:
        ctx.mark_already()
    return InitResult(created=created, skipped=skipped, path=str(paths.base))


_DEFAULT_CONFIG = """\
[defaults]
output = "human"
# `none` because v1's markdown default silently ate `_`, `*` and backticks.
parse_mode = "none"

[accounts]
default = ""

[daemon]
auto_start = true
log_level = "info"
# 0 disables the idle stop. An idle stop with catch-up off is a sync hole.
idle_timeout = 0

[presence]
# tlgr announces nothing rather than claiming to be offline while reading.
mode = "off"
"""

_DEFAULT_JOBS = """\
# Gateway jobs. Add one non-interactively with:
#   tlgr job add --name NAME --action 'reply:hello'
jobs: []
"""

_DEFAULT_WEBHOOK = """\
[webhook]
enabled = false
url = ""
# Prefer the HMAC signature over a bearer token: it authenticates the body.
secret = ""
events = ["message_new"]

[webhook.retry]
enabled = true
max_attempts = 5
backoff_base = 2

[webhook.filters]
chats = []
"""


SPEC_CONFIG_INIT = OperationSpec(
    id="config.init",
    request=ConfigInitReq,
    response=InitResult,
    impl=config_init,
    summary="Create the default configuration files",
    description="Written 0600 through the one writer that chmods before it renames (SEC-07).",
    legacy_paths=("config init",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=30,
    example={"created": ["config.toml", "jobs.yaml", "webhook.toml"], "path": "~/.tlgr"},
    example_args="config init",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class ConfigValidateReq(Request):
    strict: Annotated[bool, opt("--strict", help="Treat warnings (unknown keys) as errors.")] = (
        False
    )
    file: Annotated[
        str | None, choice("config", "jobs", "webhook", help="Only validate one file.")
    ] = None


async def config_validate(ctx: OpContext, req: ConfigValidateReq) -> ValidationReport:
    """Check the three configuration files before the daemon has to.

    Also checks the *names*: a filter, processor, action or event name nobody
    registered parses fine and then silently never matches, which is the
    failure mode this command exists to convert into a message.
    """
    from tlgr.core.config import load_app_config, load_webhook_config

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    checked: list[str] = []
    paths = _paths()

    if req.file in (None, "config"):
        checked.append("config.toml")
        try:
            load_app_config(paths.base)
        except Exception as exc:
            errors.append(ValidationIssue(file="config.toml", message=str(exc)))
        for section, block in _raw().items():
            if not isinstance(block, dict):
                continue
            for field in block:
                if not any(pair == (section, field) for pair in _FIELDS.values()):
                    warnings.append(
                        ValidationIssue(
                            file="config.toml",
                            key=f"{section}.{field}",
                            message="not a documented key; run `tlgr config keys`",
                        )
                    )

    if req.file in (None, "jobs"):
        checked.append("jobs.yaml")
        errors.extend(_validate_jobs(paths.base))

    if req.file in (None, "webhook"):
        checked.append("webhook.toml")
        try:
            webhook = load_webhook_config(paths.base)
            if webhook.enabled and not webhook.url:
                errors.append(
                    ValidationIssue(file="webhook.toml", message="enabled but no url is set")
                )
            for event in webhook.events:
                _check_event(event, "webhook.toml", errors)
        except Exception as exc:
            errors.append(ValidationIssue(file="webhook.toml", message=str(exc)))

    if req.strict:
        errors.extend(warnings)
        warnings = []
    return ValidationReport(
        ok=not errors, valid=not errors, files=checked, errors=errors, warnings=warnings
    )


def _validate_jobs(base: Any) -> list[ValidationIssue]:
    from tlgr.actions import get_action
    from tlgr.gateway.config import load_gateway_configs

    issues: list[ValidationIssue] = []
    try:
        configs = load_gateway_configs(base)
    except Exception as exc:
        return [ValidationIssue(file="jobs.yaml", message=str(exc))]
    for config in configs:
        if not config.name:
            issues.append(ValidationIssue(file="jobs.yaml", message="a job has no `name`"))
        if not config.actions:
            issues.append(
                ValidationIssue(
                    file="jobs.yaml", key=config.name, message="has no actions and would do nothing"
                )
            )
        for action in config.actions:
            if get_action(action.name) is None:
                issues.append(
                    ValidationIssue(
                        file="jobs.yaml",
                        key=config.name,
                        message=f"unknown action {action.name!r}",
                    )
                )
        for event in config.events:
            _check_event(event, "jobs.yaml", issues, key=config.name)
    return issues


def _check_event(name: str, file: str, into: list[ValidationIssue], key: str | None = None) -> None:
    from tlgr.core import eventtypes

    try:
        eventtypes.resolve_selectors(name)
    except Exception:
        into.append(
            ValidationIssue(
                file=file,
                key=key,
                message=f"unknown event type {name!r}; run `tlgr events list`",
            )
        )


SPEC_CONFIG_VALIDATE = OperationSpec(
    id="config.validate",
    request=ConfigValidateReq,
    response=ValidationReport,
    impl=config_validate,
    summary="Validate the configuration, jobs and webhook files",
    description=(
        "Names as well as syntax: a filter, action or event name nobody "
        "registered parses fine and then silently never matches."
    ),
    legacy_paths=("config validate",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("ok", "files"),
    example={"ok": True, "valid": True, "files": ["config.toml", "jobs.yaml", "webhook.toml"]},
    example_args="config validate --strict",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


# ---------------------------------------------------------------------------
# Server-side configuration
# ---------------------------------------------------------------------------


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:
        raise UsageError("this operation needs a connected account")
    return client


def _tl(value: Any) -> Any:
    from tlgr.core.tl import tl_to_builtins

    return tl_to_builtins(value)


def _dc_options(config: Any) -> list[DcOption]:
    out: list[DcOption] = []
    for option in getattr(config, "dc_options", None) or []:
        out.append(
            DcOption(
                id=int(getattr(option, "id", 0) or 0),
                ip_address=str(getattr(option, "ip_address", "") or ""),
                port=int(getattr(option, "port", 0) or 0),
                ipv6=bool(getattr(option, "ipv6", False)),
                media_only=bool(getattr(option, "media_only", False)),
                tcpo_only=bool(getattr(option, "tcpo_only", False)),
                cdn=bool(getattr(option, "cdn", False)),
                static=bool(getattr(option, "static", False)),
                this_port_only=bool(getattr(option, "this_port_only", False)),
            )
        )
    return out


class ConfigServerReq(Request):
    key: Annotated[
        list[str], opt("--key", metavar="NAME", help="Print one field (repeatable).")
    ] = []
    dc_options: Annotated[bool, opt("--dc-options", help="Include the dc_options array.")] = False


async def config_server_get(ctx: OpContext, req: ConfigServerReq) -> ServerConfig:
    """`help.getConfig` — the server's own limits and endpoints.

    Feeding `message_length_max` into `message send` is what avoids a
    MESSAGE_TOO_LONG round trip; `online_update_period_ms` is what
    `presence.mode = online` refreshes on instead of a hard-coded minute.
    """
    from telethon.tl import functions

    from tlgr.core.timefmt import fmt_dt, to_unix

    config = await _client(ctx)(functions.help.GetConfigRequest())
    date = getattr(config, "date", None)
    report = ServerConfig(
        expires=fmt_dt(getattr(config, "expires", None)),
        test_mode=bool(getattr(config, "test_mode", False)),
        this_dc=int(getattr(config, "this_dc", 0) or 0),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        chat_size_max=int(getattr(config, "chat_size_max", 0) or 0),
        megagroup_size_max=int(getattr(config, "megagroup_size_max", 0) or 0),
        message_length_max=int(getattr(config, "message_length_max", 0) or 0),
        caption_length_max=int(getattr(config, "caption_length_max", 0) or 0),
        online_update_period_ms=int(getattr(config, "online_update_period_ms", 0) or 0),
        offline_blur_timeout_ms=int(getattr(config, "offline_blur_timeout_ms", 0) or 0),
        offline_idle_timeout_ms=int(getattr(config, "offline_idle_timeout_ms", 0) or 0),
        edit_time_limit=int(getattr(config, "edit_time_limit", 0) or 0),
        revoke_time_limit=int(getattr(config, "revoke_time_limit", 0) or 0),
        rating_e_decay=int(getattr(config, "rating_e_decay", 0) or 0),
        forwarded_count_max=int(getattr(config, "forwarded_count_max", 0) or 0),
        push_chat_period_ms=int(getattr(config, "push_chat_period_ms", 0) or 0),
        dc_options=_dc_options(config) if req.dc_options else [],
    )
    if req.key:
        whole = _tl(config)
        report.values = {
            name: whole.get(name) for name in req.key if isinstance(whole, dict) and name in whole
        }
        missing = [name for name in req.key if name not in report.values]
        if missing:
            ctx.warn(f"help.getConfig has no field(s): {', '.join(missing)}")
    return report


SPEC_CONFIG_SERVER = OperationSpec(
    id="config.server.get",
    request=ConfigServerReq,
    response=ServerConfig,
    impl=config_server_get,
    summary="Read the MTProto server configuration (help.getConfig)",
    aliases=("net.config",),
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=30,
    columns=("this_dc", "message_length_max", "edit_time_limit", "revoke_time_limit"),
    example={"this_dc": 4, "message_length_max": 4096, "edit_time_limit": 172800},
    example_args="config server get --dc-options",
    covers=("updates.config-mtproto", "updates.presence-keepalive-period"),
    covers_partial=("updates.config-dc-options",),
    coverage_note="reads the config; enumerating the endpoints is `net dc list`.",
    tags=frozenset({"agent-safe"}),
)


class ConfigAppReq(Request):
    prefix: Annotated[
        str | None,
        arg(0, metavar="KEY", required=False, help="Dotted key or prefix to filter."),
    ] = None
    frozen: Annotated[bool, opt("--frozen", help="Print only the account-freeze fields.")] = False
    include_config: Annotated[bool, opt("--config", help="Also include help.getConfig.")] = False


async def config_app_get(ctx: OpContext, req: ConfigAppReq) -> AppConfigDoc:
    """`help.getAppConfig` — the limits and kill switches everything is gated on.

    The freeze fields are why this is not merely diagnostic: without
    `freeze_since_date`, `freeze_until_date` and `freeze_appeal_url`, a frozen
    account produces a bare `FROZEN_METHOD_INVALID` on every send and nothing
    that tells the user what to do about it.
    """
    from telethon.tl import functions

    from tlgr.core.timefmt import fmt_unix

    result = await _client(ctx)(functions.help.GetAppConfigRequest(hash=0))
    if type(result).__name__ == "HelpAppConfigNotModified":
        return AppConfigDoc(not_modified=True)

    values = _tl(getattr(result, "config", None))
    flat = _flatten_json_object(values)
    report = AppConfigDoc(hash=int(getattr(result, "hash", 0) or 0), values=flat)

    for field, target in (
        ("freeze_since_date", "freeze_since_date"),
        ("freeze_until_date", "freeze_until_date"),
    ):
        raw = flat.get(field)
        if isinstance(raw, (int, float)):
            setattr(report, target, fmt_unix(int(raw)))
    appeal = flat.get("freeze_appeal_url")
    if isinstance(appeal, str):
        report.freeze_appeal_url = appeal

    if req.frozen:
        report.values = {k: v for k, v in flat.items() if k.startswith("freeze_")}
    elif req.prefix:
        report.values = {k: v for k, v in flat.items() if k.startswith(req.prefix)}
        if not report.values:
            raise NotFoundError(f"no app-config key starts with {req.prefix!r}")

    if req.include_config:
        report.config = await config_server_get(ctx, ConfigServerReq(dc_options=True))
    return report


def _flatten_json_object(value: Any) -> dict[str, Any]:
    """A TL `JsonObject` tree → a flat `{key: value}` dict.

    `help.getAppConfig` returns a JSON document encoded as TL objects; leaving
    it in that shape would make every consumer walk `{"_": "JsonObjectValue",
    "key": …, "value": {"_": "JsonString", "value": …}}` by hand.
    """
    out: dict[str, Any] = {}
    for entry in (value or {}).get("value", []) if isinstance(value, dict) else []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        out[key] = _json_value(entry.get("value"))
    return out


def _json_value(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    kind = node.get("_")
    if kind == "JsonNull":
        return None
    if kind == "JsonArray":
        return [_json_value(item) for item in node.get("value", []) or []]
    if kind == "JsonObject":
        return _flatten_json_object(node)
    return node.get("value")


SPEC_CONFIG_APP = OperationSpec(
    id="config.app.get",
    request=ConfigAppReq,
    response=AppConfigDoc,
    impl=config_app_get,
    summary="Read the client (app) configuration (help.getAppConfig)",
    description=(
        "Almost every feature in Telegram has a limit or a kill switch here. "
        "`--frozen` prints the account-freeze fields, which are what turn a "
        "bare FROZEN_METHOD_INVALID into an actionable message."
    ),
    aliases=("settings.app-config",),
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=30,
    example={"hash": 1834712, "values": {"reactions_user_max_default": 1}},
    example_args="config app get --frozen",
    covers=("account.app-config", "updates.config-account-frozen", "updates.config-app"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# The flat help.* endpoints
# ---------------------------------------------------------------------------

_INFO_TOPICS = (
    "support",
    "invite-text",
    "premium-promo",
    "peer-colors",
    "timezones",
    "languages",
    "cdn",
    "recent-links",
    "emoji-keywords",
    "deep-link",
)


class ConfigInfoReq(Request):
    topic: Annotated[str, choice(*_INFO_TOPICS, help="Which endpoint to read.")]
    value: Annotated[
        str | None,
        arg(
            0,
            metavar="VALUE",
            required=False,
            help="The tg:// link for deep-link, the query for emoji-keywords.",
        ),
    ] = None
    lang: Annotated[
        str | None, opt("--lang", metavar="CODE", help="Language where the endpoint takes one.")
    ] = None
    search: Annotated[
        str | None, opt("--search", metavar="TEXT", help="Filter the returned list.")
    ] = None


async def config_info_get(ctx: OpContext, req: ConfigInfoReq) -> InfoTopic:
    """One of the flat read-only `help.*` endpoints.

    One command rather than ten thin siblings, because none of them carries an
    option of its own. What they are *for* differs, though: `timezones` feeds
    business hours, `peer-colors` ids are required by `account.updateColor`,
    `languages` exists to choose `lang_code` (tlgr does not localise its own
    output), and `premium-promo` prints prices — subscribing is a payment a
    person performs.
    """
    from telethon.tl import functions

    client = _client(ctx)
    lang = req.lang or "en"
    request, items_key = _info_request(req, lang, functions)
    result = await client(request)
    body = _tl(result)
    items = body.get(items_key) if isinstance(body, dict) and items_key else None
    rows = [row for row in (items or []) if isinstance(row, dict)]
    if req.search:
        needle = req.search.lower()
        rows = [row for row in rows if needle in str(row).lower()]
    topic = InfoTopic(topic=req.topic, items=rows, raw=body if isinstance(body, dict) else {})
    if not rows and not topic.raw:
        raise NotFoundError(f"{req.topic} returned nothing")
    return topic


def _info_request(req: ConfigInfoReq, lang: str, functions: Any) -> tuple[Any, str]:
    """The request for a topic, and the field its list lives in."""
    if req.topic == "support":
        return functions.help.GetSupportRequest(), ""
    if req.topic == "invite-text":
        return functions.help.GetInviteTextRequest(), ""
    if req.topic == "premium-promo":
        return functions.help.GetPremiumPromoRequest(), "period_options"
    if req.topic == "peer-colors":
        return functions.help.GetPeerColorsRequest(hash=0), "colors"
    if req.topic == "timezones":
        return functions.help.GetTimezonesListRequest(hash=0), "timezones"
    if req.topic == "cdn":
        return functions.help.GetCdnConfigRequest(), "public_keys"
    if req.topic == "recent-links":
        return functions.help.GetRecentMeUrlsRequest(referer=""), "urls"
    if req.topic == "languages":
        return functions.langpack.GetLanguagesRequest(lang_pack=""), ""
    if req.topic == "emoji-keywords":
        return functions.messages.GetEmojiKeywordsRequest(lang_code=lang), "keywords"
    if req.topic == "deep-link":
        if not req.value:
            raise UsageError("deep-link needs the tg:// link as its argument", field="value")
        return functions.help.GetDeepLinkInfoRequest(path=_deep_link_path(req.value)), ""
    raise UsageError(f"unknown topic {req.topic!r}", field="topic")


def _deep_link_path(link: str) -> str:
    """`tg://resolve?domain=x` → `resolve?domain=x`, which is what the API wants."""
    return link.removeprefix("tg://").removeprefix("https://t.me/").lstrip("/")


SPEC_CONFIG_INFO = OperationSpec(
    id="config.info.get",
    request=ConfigInfoReq,
    response=InfoTopic,
    impl=config_info_get,
    summary="Read one of the server's flat informational endpoints",
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=60,
    empty_exit=EXIT_EMPTY,
    example={"topic": "timezones", "items": [{"id": "Europe/London", "utc_offset": 0}]},
    example_args="config info get timezones",
    covers=(
        "updates.config-cdn",
        "updates.config-deep-link-info",
        "updates.config-emoji-keywords",
        "updates.config-invite-text",
        "updates.config-peer-colors",
        "updates.config-premium-promo",
        "updates.config-recent-me-urls",
        "updates.config-support",
        "updates.config-timezones",
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Countries
# ---------------------------------------------------------------------------

#: ISO-3166 alpha-2 → the regional-indicator pair that renders as its flag.
#: A pure client-side derivation: TDLib's getCountryFlagEmoji has no MTProto
#: counterpart, and neither does the preferred-language hint.
_FLAG_BASE = 0x1F1E6


def _flag(iso2: str) -> str:
    if len(iso2) != 2 or not iso2.isalpha():
        return ""
    return "".join(chr(_FLAG_BASE + ord(char.upper()) - ord("A")) for char in iso2)


class CountryListReq(Request):
    code: Annotated[str | None, opt("--code", metavar="ISO2", help="One country by ISO code.")] = (
        None
    )
    search: Annotated[
        str | None, opt("--search", metavar="TEXT", help="Match on country name.")
    ] = None
    phone: Annotated[
        str | None,
        opt("--phone", metavar="NUMBER", help="Classify a number: country, prefix, validity."),
    ] = None
    lang: Annotated[
        str | None, opt("--lang", metavar="CODE", help="Language for the localised names.")
    ] = None


async def country_list(ctx: OpContext, req: CountryListReq) -> Page[Country]:
    """Countries, phone prefixes and number patterns.

    `--phone` is the reason to have it: validating a number *before* `tlgr
    login` turns a wasted `auth.sendCode` — and the flood budget it costs —
    into a local error.
    """
    from telethon.tl import functions

    result = await _client(ctx)(
        functions.help.GetCountriesListRequest(lang_code=req.lang or "", hash=0)
    )
    rows: list[Country] = []
    digits = "".join(char for char in (req.phone or "") if char.isdigit())

    for entry in getattr(result, "countries", None) or []:
        iso2 = str(getattr(entry, "iso2", "") or "")
        codes = [
            CountryCode(
                country_code=str(getattr(code, "country_code", "") or ""),
                prefixes=[str(p) for p in (getattr(code, "prefixes", None) or [])],
                patterns=[str(p) for p in (getattr(code, "patterns", None) or [])],
            )
            for code in getattr(entry, "country_codes", None) or []
        ]
        country = Country(
            iso2=iso2,
            name=str(getattr(entry, "name", "") or getattr(entry, "default_name", "") or ""),
            default_name=str(getattr(entry, "default_name", "") or ""),
            hidden=bool(getattr(entry, "hidden", False)),
            flag_emoji=_flag(iso2),
            preferred_language=iso2.lower(),
            codes=codes,
        )
        if digits:
            matched = _match_phone(digits, codes)
            if matched is None:
                continue
            country.matched_prefix, country.valid = matched
        elif (req.code and iso2.upper() != req.code.upper()) or (
            req.search and req.search.lower() not in country.name.lower()
        ):
            continue
        rows.append(country)

    if digits and not rows:
        raise NotFoundError(f"no country claims the prefix of {req.phone!r}")
    limit = int(getattr(ctx, "limit", None) or 300)
    return build_page(
        rows[:limit],
        op="config.country.list",
        kind=PageKind.LOCAL,
        has_more=len(rows) > limit,
        total=len(rows),
    )


def _match_phone(digits: str, codes: list[CountryCode]) -> tuple[str, bool] | None:
    """The longest matching dial prefix, and whether the rest fits a pattern."""
    best: tuple[str, bool] | None = None
    for code in codes:
        if not digits.startswith(code.country_code):
            continue
        rest = digits[len(code.country_code) :]
        prefixes = code.prefixes or [""]
        for prefix in prefixes:
            if not rest.startswith(prefix):
                continue
            valid = not code.patterns or any(
                len(rest) == len(pattern.replace(" ", "")) for pattern in code.patterns
            )
            candidate = (code.country_code + prefix, valid)
            if best is None or len(candidate[0]) > len(best[0]):
                best = candidate
    return best


SPEC_COUNTRY_LIST = OperationSpec(
    id="config.country.list",
    request=CountryListReq,
    response=Page[Country],
    impl=country_list,
    summary="List or look up countries, phone prefixes and number patterns",
    description=(
        "`--phone` classifies a number locally, which turns a wasted "
        "`auth.sendCode` into an error before it costs the flood budget. The "
        "flag emoji and the preferred language are derived client-side: "
        "neither has an MTProto counterpart."
    ),
    aliases=("config.countries", "auth.countries"),
    paginated=PageKind.LOCAL,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="read",
    timeout_s=30,
    columns=("iso2", "name", "flag_emoji", "codes"),
    empty_exit=EXIT_EMPTY,
    example={
        "items": [{"iso2": "GB", "name": "United Kingdom", "flag_emoji": "🇬🇧"}],
        "has_more": False,
    },
    example_args="config country list --phone +447700900000",
    covers=(
        "auth.countries-list",
        "auth.prelogin-language",
        "updates.config-countries",
        "updates.config-country-lookup",
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# Promo and suggestions
# ---------------------------------------------------------------------------


class PromoReq(Request):
    hide: Annotated[bool, opt("--hide", help="Hide the current promo dialog.")] = False


async def config_promo_get(ctx: OpContext, req: PromoReq) -> PromoData:
    """The promoted / PSA / sponsored chat the server pins to the dialog list.

    Official clients render it specially at the top of the list, so `chat
    list` needs it for parity. A `proxy` flag means the promo arrived because
    of an MTProxy sponsor — which is the reason tlgr does not declare its
    proxy to the server by default.
    """
    from telethon.tl import functions

    from tlgr.core.timefmt import fmt_unix

    client = _client(ctx)
    result = await client(functions.help.GetPromoDataRequest())
    kind = type(result).__name__
    if kind == "HelpPromoDataEmpty":
        return PromoData(kind="none", expires=fmt_unix(getattr(result, "expires", None)))

    peer = getattr(result, "peer", None)
    from tlgr.core.tl import peer_marked_id

    report = PromoData(
        kind="psa" if getattr(result, "psa_type", None) else "promo",
        chat_id=peer_marked_id(peer),
        psa_type=getattr(result, "psa_type", None),
        psa_message=getattr(result, "psa_message", None),
        proxy=bool(getattr(result, "proxy", False)),
        expires=fmt_unix(getattr(result, "expires", None)),
        pending_suggestions=[str(s) for s in (getattr(result, "pending_suggestions", None) or [])],
    )
    if req.hide:
        if peer is None:
            ctx.mark_already()
            return report
        await client(functions.help.HidePromoDataRequest(peer=peer))
        report.hidden = True
    return report


SPEC_PROMO = OperationSpec(
    id="config.promo.get",
    request=PromoReq,
    response=PromoData,
    impl=config_promo_get,
    summary="Show (or hide) the promoted / PSA chat the server pins to the dialog list",
    mutating=True,
    idempotent=True,
    surface=Surface.DAEMON,
    rate_class="read",
    timeout_s=30,
    example={"kind": "psa", "psa_type": "covid", "expires": "2026-09-04T00:00:00Z"},
    example_args="config promo get",
    covers=("updates.config-promo-psa", "updates.invoke-client-proxy-declare"),
    coverage_note="",
    tags=frozenset({"agent-safe", "mutating-checked"}),
)
