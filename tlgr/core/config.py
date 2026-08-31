"""TOML configuration loading for tlgr.

Handles three config files:
  - config.toml   (general settings)
  - routes.toml   (background job definitions)
  - webhook.toml  (outbound webhook push)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

try:
    import tomli_w
except ImportError:
    tomli_w = None  # type: ignore[assignment]

from tlgr.core.errors import ConfigurationError

CONFIG_DIR = Path.home() / ".tlgr"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Defaults:
    drop_author: bool = False
    delete_after: bool = False
    output: str = "human"
    require_account: bool = False


@dataclass
class DaemonConfig:
    auto_start: bool = True
    log_level: str = "info"
    idle_timeout: int = 1800
    flood_wait_max: int = 120


@dataclass
class JobFilterConfig:
    types: list[str] | None = None
    exclude_types: list[str] | None = None
    after: str | None = None
    before: str | None = None
    contains: list[str] | None = None
    contains_any: list[str] | None = None
    excludes: list[str] | None = None
    regex: str | None = None
    has_media: bool | None = None
    min_size: str | None = None
    max_size: str | None = None
    from_users: list[int] | None = None
    exclude_users: list[int] | None = None
    is_reply: bool | None = None
    is_forward: bool | None = None
    has_links: bool | None = None


@dataclass
class TransformInline:
    """An inline TOML-defined transform (always regex type)."""
    type: str = "regex"
    pattern: str = ""
    replacement: str = ""
    flags: str = ""


@dataclass
class DestinationConfig:
    chat: str = ""
    transforms: list[str | TransformInline] | None = None
    filters: JobFilterConfig | None = None


@dataclass
class JobConfig:
    name: str = ""
    type: str = "autoforward"
    account: str = ""
    enabled: bool = True
    # autoforward
    source: str = ""
    destinations: list[str | DestinationConfig] = field(default_factory=list)
    drop_author: bool = False
    delete_after: bool = False
    transforms: list[str | TransformInline] = field(default_factory=list)
    filters: JobFilterConfig | None = None
    # autoreply
    chats: list[str] = field(default_factory=list)
    reply: str = ""


@dataclass
class WebhookRetryConfig:
    enabled: bool = True
    max_attempts: int = 3
    backoff_base: int = 2


@dataclass
class WebhookFilterConfig:
    chats: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    token: str = ""
    events: list[str] = field(default_factory=lambda: [
        "new_message",
    ])
    retry: WebhookRetryConfig = field(default_factory=WebhookRetryConfig)
    filters: WebhookFilterConfig = field(default_factory=WebhookFilterConfig)


@dataclass
class AppConfig:
    defaults: Defaults = field(default_factory=Defaults)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    default_account: str = ""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _save_toml(path: Path, data: dict[str, Any]) -> None:
    if tomli_w is None:
        raise ConfigurationError("tomli_w is required to write TOML files")
    _ensure_dir(path.parent)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    path.chmod(0o600)


def _parse_filter(raw: dict[str, Any] | None) -> JobFilterConfig | None:
    if not raw:
        return None
    return JobFilterConfig(**{k: v for k, v in raw.items() if k in JobFilterConfig.__dataclass_fields__})


def _parse_transforms(raw: list[Any]) -> list[str | TransformInline]:
    result: list[str | TransformInline] = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            result.append(TransformInline(**{k: v for k, v in item.items() if k in TransformInline.__dataclass_fields__}))
    return result


def _parse_destinations(raw: list[Any]) -> list[str | DestinationConfig]:
    result: list[str | DestinationConfig] = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            dc = DestinationConfig(
                chat=item.get("chat", ""),
                transforms=_parse_transforms(item["transforms"]) if "transforms" in item else None,
                filters=_parse_filter(item.get("filters")),
            )
            result.append(dc)
    return result


def load_app_config(base: Path | None = None) -> AppConfig:
    base = base or CONFIG_DIR
    raw = _load_toml(base / "config.toml")
    cfg = AppConfig()

    defaults_raw = raw.get("defaults", {})
    cfg.defaults = Defaults(
        drop_author=defaults_raw.get("drop_author", False),
        delete_after=defaults_raw.get("delete_after", False),
        output=defaults_raw.get("output", "human"),
        require_account=defaults_raw.get("require_account", False),
    )

    daemon_raw = raw.get("daemon", {})
    cfg.daemon = DaemonConfig(
        auto_start=daemon_raw.get("auto_start", True),
        log_level=daemon_raw.get("log_level", "info"),
        idle_timeout=daemon_raw.get("idle_timeout", 1800),
        flood_wait_max=daemon_raw.get("flood_wait_max", 120),
    )

    cfg.default_account = raw.get("accounts", {}).get("default", "")
    return cfg


def load_jobs(base: Path | None = None) -> list[JobConfig]:
    base = base or CONFIG_DIR
    raw = _load_toml(base / "jobs.toml")
    jobs_raw = raw.get("jobs", [])
    jobs: list[JobConfig] = []
    for j in jobs_raw:
        jc = JobConfig(
            name=j.get("name", ""),
            type=j.get("type", "autoforward"),
            account=j.get("account", ""),
            enabled=j.get("enabled", True),
            source=j.get("source", ""),
            destinations=_parse_destinations(j.get("destinations", [])),
            drop_author=j.get("drop_author", False),
            delete_after=j.get("delete_after", False),
            transforms=_parse_transforms(j.get("transforms", [])),
            filters=_parse_filter(j.get("filters")),
            chats=j.get("chats", []),
            reply=j.get("reply", ""),
        )
        jobs.append(jc)
    return jobs


def save_jobs(jobs: list[JobConfig], base: Path | None = None) -> None:
    from dataclasses import asdict

    base = base or CONFIG_DIR
    jobs_dicts: list[dict[str, Any]] = []
    for j in jobs:
        d = asdict(j)
        # Strip None filter
        if d.get("filters") is None:
            del d["filters"]
        else:
            d["filters"] = {k: v for k, v in d["filters"].items() if v is not None}
        # Strip empty optionals
        for key in ("chats", "reply", "source"):
            if not d.get(key):
                d.pop(key, None)
        if not d.get("transforms"):
            d.pop("transforms", None)
        jobs_dicts.append(d)
    _save_toml(base / "jobs.toml", {"jobs": jobs_dicts})


def load_webhook_config(base: Path | None = None) -> WebhookConfig:
    base = base or CONFIG_DIR
    raw = _load_toml(base / "webhook.toml")
    wh_raw = raw.get("webhook", {})
    if not wh_raw:
        return WebhookConfig()

    retry_raw = wh_raw.get("retry", {})
    filters_raw = wh_raw.get("filters", {})

    extra_filters = {k: v for k, v in filters_raw.items() if k != "chats"}

    return WebhookConfig(
        enabled=wh_raw.get("enabled", False),
        url=wh_raw.get("url", ""),
        token=wh_raw.get("token", ""),
        events=wh_raw.get("events", ["new_message"]),
        retry=WebhookRetryConfig(
            enabled=retry_raw.get("enabled", True),
            max_attempts=retry_raw.get("max_attempts", 3),
            backoff_base=retry_raw.get("backoff_base", 2),
        ),
        filters=WebhookFilterConfig(
            chats=filters_raw.get("chats", []),
            raw=extra_filters,
        ),
    )


def get_config_dir() -> Path:
    return _ensure_dir(CONFIG_DIR)


def get_accounts_dir(base: Path | None = None) -> Path:
    return _ensure_dir((base or CONFIG_DIR) / "accounts")


def get_logs_dir(base: Path | None = None) -> Path:
    return _ensure_dir((base or CONFIG_DIR) / "logs")


def get_downloads_dir(base: Path | None = None) -> Path:
    return _ensure_dir((base or CONFIG_DIR) / "downloads")


def get_socket_path(base: Path | None = None) -> Path:
    return (base or CONFIG_DIR) / "daemon.sock"


def get_pid_path(base: Path | None = None) -> Path:
    return (base or CONFIG_DIR) / "daemon.pid"
