"""`config.toml` → typed structs, and the path helpers everything else uses.

v1 parsed the file by hand into dataclasses with `raw.get("x", default)` at
every key, which meant a typo in `config.toml` was silently the default and a
new section had to be threaded through three functions. Here the schema of
§10.2 *is* the type: msgspec decodes the TOML into it, an unknown key inside a
known section is reported with its path, and a wrong type is a `CONFIG_ERROR`
naming the key instead of a `TypeError` three modules later.

The v1 `jobs.toml` engine that used to live in this module is gone: jobs are
`jobs.yaml`, parsed by `gateway/config.py`, and the TOML loader had no callers
left (MNT-04).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, TypeVar

import msgspec

from tlgr.core.errors import ConfigurationError
from tlgr.core.paths import TlgrPaths, default_base, write_private

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

try:
    import tomli_w
except ImportError:  # pragma: no cover - optional at runtime, required to write
    tomli_w = None  # type: ignore[assignment]

#: Kept as a module constant because v1 modules import it directly. New code
#: should call `default_base()` so that `TLGR_HOME` is honoured per call.
CONFIG_DIR = default_base()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class _Section(msgspec.Struct, forbid_unknown_fields=False):
    """A config section.

    Unknown keys are tolerated rather than fatal: a config written by a newer
    tlgr must not stop an older one from starting, and `tlgr config validate`
    is where unknown keys are reported.
    """


# ---------------------------------------------------------------------------
# Sections (§10.2)
# ---------------------------------------------------------------------------


class Defaults(_Section):
    drop_author: bool = False
    delete_after: bool = False
    output: str = "human"
    require_account: bool = False
    #: v2 emits RFC-3339; `legacy_dates` restores v1's `str(datetime)` spelling
    #: for the one minor release the migration note in §12.4 covers.
    legacy_dates: bool = False
    #: v1 defaulted to markdown, which silently ate `_`, `*` and backticks in
    #: ordinary text (COR-21). `none` is the safe default.
    parse_mode: str = "none"
    timezone: str = ""
    confirm_destructive: bool = True


class DaemonConfig(_Section):
    auto_start: bool = True
    log_level: str = "info"
    idle_timeout: int = 1800
    #: Seconds tlgr will sleep off inside a request before giving up. Kept at
    #: the daemon level as well as under `[flood]` because v1 spelled it here.
    flood_wait_max: int = 120
    start_timeout: int = 30
    drain_seconds: int = 30
    preconnect: list[str] = []
    event_buffer: int = 4096
    event_workers: int = 8
    resync_depth: int = 50
    state_save_interval: int = 60


class IdentityConfig(_Section):
    device_model: str = ""
    system_version: str = ""
    lang_code: str = ""
    system_lang_code: str = ""
    tz_offset: bool = True


class PresenceConfig(_Section):
    #: off | online | mirror. Off by default because appearing online is a
    #: visible, account-affecting behaviour the operator must opt into.
    mode: str = "off"


class NetworkConfig(_Section):
    proxy: str = ""
    ipv6: bool = False
    connect_timeout: int = 10
    connection: str = "tcp_full"


class FloodConfig(_Section):
    sleep_threshold: int = 120
    max_wait: int = 600
    persist: bool = True


class RateClassConfig(_Section):
    rate: float = 10.0
    burst: int = 20
    new_peers_per_day: int = 0


class LimitsConfig(_Section):
    entity_cache: int = 20000
    request_retries: int = 5
    dialog_scan_max: int = 5000
    download_concurrency_small: int = 5
    download_concurrency_large: int = 2
    upload_parts_in_flight: int = 4
    max_album: int = 10


class SecurityConfig(_Section):
    require_token: bool = False
    peer_uid_check: bool = True
    warn_insecure_webhook: bool = True


class PolicyConfig(_Section):
    allow: list[str] = msgspec.field(default_factory=lambda: ["*"])
    deny: list[str] = []


class LoggingConfig(_Section):
    redact: bool = True
    max_bytes: int = 8388608
    backups: int = 5


class MediaConfig(_Section):
    download_dir: str = ""
    ffprobe: str = "auto"


class AccountsSection(_Section):
    default: str = ""


#: The shipped defaults for an unwarmed account (§6.4). `resolve` is slow
#: because `contacts.resolveUsername` floods at roughly 50 calls in a short
#: period; `send` is slow because a young account that sends fast gets frozen.
_DEFAULT_RATES: dict[str, RateClassConfig] = {
    "read": RateClassConfig(rate=10.0, burst=20),
    "resolve": RateClassConfig(rate=0.5, burst=5),
    "send": RateClassConfig(rate=1.0, burst=3, new_peers_per_day=30),
    "bulk": RateClassConfig(rate=2.0, burst=4),
    "file": RateClassConfig(rate=5.0, burst=10),
    "local": RateClassConfig(rate=1000.0, burst=1000),
}


class AppConfig(msgspec.Struct, forbid_unknown_fields=False):
    """The whole of `config.toml`, decoded."""

    accounts: AccountsSection = msgspec.field(default_factory=AccountsSection)
    defaults: Defaults = msgspec.field(default_factory=Defaults)
    daemon: DaemonConfig = msgspec.field(default_factory=DaemonConfig)
    identity: IdentityConfig = msgspec.field(default_factory=IdentityConfig)
    presence: PresenceConfig = msgspec.field(default_factory=PresenceConfig)
    network: NetworkConfig = msgspec.field(default_factory=NetworkConfig)
    flood: FloodConfig = msgspec.field(default_factory=FloodConfig)
    rate: dict[str, RateClassConfig] = msgspec.field(default_factory=dict)
    limits: LimitsConfig = msgspec.field(default_factory=LimitsConfig)
    security: SecurityConfig = msgspec.field(default_factory=SecurityConfig)
    policy: PolicyConfig = msgspec.field(default_factory=PolicyConfig)
    logging: LoggingConfig = msgspec.field(default_factory=LoggingConfig)
    media: MediaConfig = msgspec.field(default_factory=MediaConfig)

    @property
    def default_account(self) -> str:
        """v1 spelled `[accounts] default` as a flat attribute; keep it."""
        return self.accounts.default

    def rate_for(self, rate_class: str) -> RateClassConfig:
        """The bucket for a `rate_class`, falling back to the shipped default."""
        configured = self.rate.get(rate_class)
        if configured is not None:
            return configured
        return _DEFAULT_RATES.get(rate_class, _DEFAULT_RATES["read"])


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class WebhookRetryConfig(_Section):
    enabled: bool = True
    max_attempts: int = 3
    backoff_base: int = 2


class WebhookFilterConfig(_Section):
    chats: list[str] = []
    raw: dict[str, Any] = {}


class WebhookConfig(_Section):
    enabled: bool = False
    url: str = ""
    token: str = ""
    #: HMAC-SHA256 key for `X-Tlgr-Signature`. Falls back to `token` so an
    #: existing config keeps signing without being edited.
    secret: str = ""
    events: list[str] = msgspec.field(default_factory=lambda: ["message_new"])
    queue_size: int = 2048
    workers: int = 4
    timeout: int = 30
    retry: WebhookRetryConfig = msgspec.field(default_factory=WebhookRetryConfig)
    filters: WebhookFilterConfig = msgspec.field(default_factory=WebhookFilterConfig)

    @property
    def signing_key(self) -> str:
        return self.secret or self.token


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as handle:
            loaded: dict[str, Any] = tomllib.load(handle)
        return loaded
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid TOML: {exc}") from exc


def _save_toml(path: Path, data: dict[str, Any]) -> None:
    if tomli_w is None:
        raise ConfigurationError("tomli_w is required to write TOML files")
    write_private(path, tomli_w.dumps(data).encode("utf-8"))


_T = TypeVar("_T")


def _decode(raw: dict[str, Any], type_: type[_T], what: str) -> _T:
    try:
        decoded: _T = msgspec.convert(raw, type=type_, strict=False)
        return decoded
    except msgspec.ValidationError as exc:
        raise ConfigurationError(f"{what}: {exc}") from exc


def load_app_config(base: Path | None = None) -> AppConfig:
    """Decode `config.toml` under *base*.

    A malformed value is a `CONFIG_ERROR` naming the key, not a silent
    fallback to the default: "the daemon ignored your setting" is the failure
    mode this whole module exists to remove.
    """
    paths = TlgrPaths(base)
    raw = _load_toml(paths.config)
    config = _decode(raw, AppConfig, f"{paths.config}")
    if not config.rate:
        config.rate = dict(_DEFAULT_RATES)
    else:
        merged = dict(_DEFAULT_RATES)
        merged.update(config.rate)
        config.rate = merged
    _apply_env(config)
    return config


_ENV_KEYS: tuple[tuple[str, str, str], ...] = (
    ("TLGR_ACCOUNT", "accounts", "default"),
    ("TLGR_LOG_LEVEL", "daemon", "log_level"),
)


def _apply_env(config: AppConfig) -> None:
    """CLI flag → environment → `config.toml` → default (§10.2)."""
    for env_key, section, key in _ENV_KEYS:
        value = os.environ.get(env_key, "").strip()
        if value:
            setattr(getattr(config, section), key, value)


def load_webhook_config(base: Path | None = None) -> WebhookConfig:
    paths = TlgrPaths(base)
    raw = _load_toml(paths.webhook).get("webhook", {})
    if not raw:
        return WebhookConfig()
    filters_raw = dict(raw.get("filters", {}) or {})
    chats = filters_raw.pop("chats", [])
    extra = {k: v for k, v in filters_raw.items() if k != "raw"}
    raw = dict(raw)
    raw["filters"] = {"chats": chats, "raw": extra}
    return _decode(raw, WebhookConfig, f"{paths.webhook}")


def save_webhook_config(config: WebhookConfig, base: Path | None = None) -> None:
    paths = TlgrPaths(base)
    body: dict[str, Any] = msgspec.to_builtins(config)
    filters: dict[str, Any] = body.pop("filters", {}) or {}
    merged: dict[str, Any] = {k: v for k, v in filters.items() if k != "raw"}
    merged.update(filters.get("raw", {}) or {})
    body["filters"] = merged
    _save_toml(paths.webhook, {"webhook": body})


# ---------------------------------------------------------------------------
# Path helpers (kept at their v1 names; every caller in the tree uses them)
# ---------------------------------------------------------------------------


def get_config_dir() -> Path:
    return _ensure_dir(default_base())


def get_accounts_dir(base: Path | None = None) -> Path:
    return _ensure_dir(TlgrPaths(base).accounts)


def get_logs_dir(base: Path | None = None) -> Path:
    return TlgrPaths(base).ensure_logs()


def get_downloads_dir(base: Path | None = None) -> Path:
    return TlgrPaths(base).ensure_downloads()


def get_socket_path(base: Path | None = None) -> Path:
    return TlgrPaths(base).socket


def get_pid_path(base: Path | None = None) -> Path:
    return TlgrPaths(base).pid
