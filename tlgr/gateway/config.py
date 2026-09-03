"""YAML-based job configuration for the Gateway pipeline.

Parses ``~/.tlgr/jobs.yaml`` into :class:`GatewayConfig` objects that the
:class:`~tlgr.gateway.engine.Gateway` consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core.config import CONFIG_DIR
from tlgr.core.errors import ConfigurationError
from tlgr.filters.compose import FilterNode, parse_filter_config
from tlgr.processors import ProcessorChain, create_chain_from_list


@dataclass
class ActionConfig:
    """A single action in a job's action list."""

    name: str = ""
    config: Any = None
    filters: FilterNode | None = None
    processors: ProcessorChain | None = None


ALL_EVENT_TYPES = frozenset(
    {
        "new_message",
        "message_edited",
        "message_deleted",
        "chat_action",
        "user_joined",
        "message_read",
    }
)


@dataclass
class GatewayConfig:
    """Parsed configuration for one Gateway job."""

    name: str = ""
    account: str = ""
    enabled: bool = True
    events: list[str] = field(default_factory=lambda: ["new_message"])
    filters: FilterNode | None = None
    processors: ProcessorChain | None = None
    actions: list[ActionConfig] = field(default_factory=list)


def _parse_action(raw: dict[str, Any]) -> ActionConfig:
    """Parse a concise action entry.

    Concise syntax: the action name is the dict key, the value is its config.

    Examples::

        {"reply": "hello"}              -> ActionConfig(name="reply", config="hello")
        {"forward": {"to": "@chan"}}     -> ActionConfig(name="forward", config={"to": "@chan"})
    """
    for key, value in raw.items():
        ac = ActionConfig(name=key)

        if isinstance(value, str):
            ac.config = value
        elif isinstance(value, dict):
            ac.config = {k: v for k, v in value.items() if k not in ("filters", "processors")}
            if len(ac.config) == 1 and "text" in ac.config:
                ac.config = ac.config["text"]
            ac.filters = parse_filter_config(value.get("filters"))
            procs = value.get("processors")
            if procs:
                ac.processors = create_chain_from_list(procs) if isinstance(procs, list) else None
        else:
            ac.config = value

        return ac

    return ActionConfig()


def _parse_job(raw: dict[str, Any]) -> GatewayConfig:
    cfg = GatewayConfig(
        name=raw.get("name", ""),
        account=raw.get("account", ""),
        enabled=raw.get("enabled", True),
    )

    raw_events = raw.get("events")
    if raw_events and isinstance(raw_events, list):
        cfg.events = [str(e) for e in raw_events if str(e) in ALL_EVENT_TYPES]
    elif raw_events and isinstance(raw_events, str):
        cfg.events = [raw_events] if raw_events in ALL_EVENT_TYPES else ["new_message"]

    cfg.filters = parse_filter_config(raw.get("filters"))

    procs = raw.get("processors")
    if procs and isinstance(procs, list):
        cfg.processors = create_chain_from_list(procs)

    actions_raw = raw.get("actions", [])
    for action_raw in actions_raw:
        if isinstance(action_raw, dict):
            cfg.actions.append(_parse_action(action_raw))

    return cfg


def load_gateway_configs(base: Path | None = None) -> list[GatewayConfig]:
    """Load all gateway jobs from ``jobs.yaml``."""
    base = base or CONFIG_DIR
    jobs_path = base / "jobs.yaml"
    if not jobs_path.exists():
        return []

    try:
        import yaml
    except ModuleNotFoundError as e:
        raise ConfigurationError(
            "PyYAML is required for jobs.yaml support. Install with: pip install pyyaml"
        ) from e

    with open(jobs_path) as f:
        data = yaml.safe_load(f)

    if not data or "jobs" not in data:
        return []

    return [_parse_job(j) for j in data["jobs"] if isinstance(j, dict)]


def save_gateway_configs(configs: list[GatewayConfig], base: Path | None = None) -> None:
    """Serialize gateway configs back to ``jobs.yaml`` (minimal round-trip)."""
    base = base or CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    jobs_path = base / "jobs.yaml"

    try:
        import yaml
    except ModuleNotFoundError as e:
        raise ConfigurationError(
            "PyYAML is required for jobs.yaml support. Install with: pip install pyyaml"
        ) from e

    jobs_list: list[dict[str, Any]] = []
    for cfg in configs:
        d: dict[str, Any] = {"name": cfg.name}
        if cfg.account:
            d["account"] = cfg.account
        if not cfg.enabled:
            d["enabled"] = False
        # filters and processors are not round-tripped here;
        # users edit the YAML directly.
        jobs_list.append(d)

    with open(jobs_path, "w") as f:
        yaml.dump({"jobs": jobs_list}, f, default_flow_style=False, sort_keys=False)
    jobs_path.chmod(0o600)
