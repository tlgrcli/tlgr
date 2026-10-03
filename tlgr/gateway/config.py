"""YAML-based job configuration for the Gateway pipeline.

Parses ``~/.tlgr/jobs.yaml`` into :class:`GatewayConfig` objects that the
:class:`~tlgr.gateway.engine.Gateway` consumes, plus the optional top-level
``pacing:`` block (per-account pacer rules and expiry).

Validation is strict about what it understands. An unknown key on a job or
an action, a bad duration, a percent outside 0-100 or an unknown presence
mode is reported with the job's name and the action's position, rather than
being ignored into a job that silently never does what it says. A broken
job is skipped (and reported); the other jobs in the file still load, so a
typo in a new DM job cannot stop a working forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core.config import CONFIG_DIR
from tlgr.core.errors import ConfigurationError
from tlgr.filters.compose import FilterNode, parse_filter_config
from tlgr.gateway.knobs import KNOB_KEYS, KnobError, parse_duration, parse_knobs
from tlgr.gateway.pacer import DEFAULT_PACING, PacingRule
from tlgr.processors import ProcessorChain, create_chain_from_list


@dataclass
class ActionConfig:
    """A single action in a job's action list."""

    name: str = ""
    config: Any = None
    filters: FilterNode | None = None
    processors: ProcessorChain | None = None
    #: The action's own config, validated by the action (None: not parsed yet).
    params: dict[str, Any] | None = None
    #: Knobs set on this action, parsed; job knobs fill in the rest.
    knobs: dict[str, Any] = field(default_factory=dict)


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

JOB_KEYS = frozenset(
    {"name", "account", "enabled", "events", "filters", "processors", "actions"} | KNOB_KEYS
)
_PIPELINE_KEYS = frozenset({"filters", "processors"})


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
    #: Job-level knobs: defaults for every action of the job.
    knobs: dict[str, Any] = field(default_factory=dict)


class JobConfigError(ConfigurationError):
    """One job (or the pacing block) that cannot be loaded; `problems` says why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _split_action(name: str, value: Any) -> tuple[Any, dict[str, Any], Any, Any]:
    """`(the action's own config, knob keys, filters, processors)`.

    A react written as a weighted mapping (`{"👍": 3}`) is all config; any
    other mapping carries its knobs and pipeline keys beside its own keys.
    """
    if not isinstance(value, dict) or (name == "react" and "emoji" not in value):
        return value, {}, None, None
    own = {k: v for k, v in value.items() if k not in KNOB_KEYS and k not in _PIPELINE_KEYS}
    knobs = {k: value[k] for k in KNOB_KEYS if k in value}
    return own, knobs, value.get("filters"), value.get("processors")


def _parse_action(raw: dict[str, Any], *, where: str = "action") -> ActionConfig:
    """Parse a concise action entry.

    Concise syntax: the action name is the dict key, the value is its config.

    Examples::

        {"reply": "hello"}              -> ActionConfig(name="reply", config="hello")
        {"forward": {"to": "@chan"}}     -> ActionConfig(name="forward", config={"to": "@chan"})
        {"react": ["👍", "🔥"]}          -> ActionConfig(name="react", config=["👍", "🔥"])
    """
    from tlgr.actions import get_action, get_builtin

    if not isinstance(raw, dict) or len(raw) != 1:
        raise JobConfigError([f"{where}: an action is a mapping with exactly one key"])
    name, value = next(iter(raw.items()))
    name = str(name)
    own, knob_raw, filters_raw, procs = _split_action(name, value)
    problems: list[str] = []

    ac = ActionConfig(name=name)
    # The old shape, kept for actions written against the function interface:
    # a mapping with only `text` collapses to the text.
    ac.config = own["text"] if isinstance(own, dict) and set(own) == {"text"} else own

    found = get_action(name)
    if found is None:
        problems.append(f"{where}: unknown action {name!r}")
    try:
        ac.knobs = parse_knobs(knob_raw, where=where)
    except KnobError as exc:
        problems.append(str(exc))
    if filters_raw is not None and not isinstance(filters_raw, dict):
        problems.append(f"{where}: filters must be a mapping")
    else:
        ac.filters = parse_filter_config(filters_raw)
    if procs is not None:
        if not isinstance(procs, list):
            problems.append(f"{where}: processors must be a list")
        else:
            builtin = get_builtin(name)
            if builtin is not None and not builtin.accepts_processors:
                problems.append(f"{where}: {name} does not take processors")
            try:
                ac.processors = create_chain_from_list(procs) if procs else None
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
    builtin = get_builtin(name)
    if builtin is not None:
        try:
            ac.params = builtin.parse(own)
        except KnobError as exc:
            problems.append(f"{where}: {exc}")
    if problems:
        raise JobConfigError(problems)
    return ac


def _parse_job(raw: dict[str, Any]) -> GatewayConfig:
    """One `jobs:` entry; raises `JobConfigError` listing every problem in it."""
    name = raw.get("name", "")
    label = f"job {name!r}" if name else "a job without a name"
    problems: list[str] = []
    if not name:
        problems.append(f"{label}: `name` is required")
    unknown = sorted(set(raw) - JOB_KEYS)
    if unknown:
        problems.append(f"{label}: unknown key(s) {unknown}; valid: {', '.join(sorted(JOB_KEYS))}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append(f"{label}: enabled must be true or false")
    cfg = GatewayConfig(name=str(name), account=str(raw.get("account", "") or ""))
    cfg.enabled = bool(enabled)

    raw_events = raw.get("events")
    if raw_events and isinstance(raw_events, list):
        cfg.events = [str(e) for e in raw_events if str(e) in ALL_EVENT_TYPES]
    elif raw_events and isinstance(raw_events, str):
        cfg.events = [raw_events] if raw_events in ALL_EVENT_TYPES else ["new_message"]

    filters = raw.get("filters")
    if filters is not None and not isinstance(filters, dict):
        problems.append(f"{label}: filters must be a mapping")
    else:
        cfg.filters = parse_filter_config(filters)

    procs = raw.get("processors")
    if procs and isinstance(procs, list):
        try:
            cfg.processors = create_chain_from_list(procs)
        except ValueError as exc:
            problems.append(f"{label}: {exc}")

    try:
        cfg.knobs = parse_knobs(raw, where=label)
    except KnobError as exc:
        problems.append(str(exc))

    actions_raw = raw.get("actions", [])
    if not isinstance(actions_raw, list):
        problems.append(f"{label}: actions must be a list")
        actions_raw = []
    for position, action_raw in enumerate(actions_raw, start=1):
        try:
            cfg.actions.append(_parse_action(action_raw, where=f"{label}, action {position}"))
        except JobConfigError as exc:
            problems.extend(exc.problems)

    if problems:
        raise JobConfigError(problems)
    return cfg


def parse_pacing(raw: Any) -> dict[str, Any]:
    """The top-level `pacing:` block → `{alias: AccountPacing}`."""
    from tlgr.gateway.scheduler import AccountPacing

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise JobConfigError(["pacing: must be a mapping of account alias to settings"])
    problems: list[str] = []
    out: dict[str, Any] = {}
    kinds = set(DEFAULT_PACING)
    for alias, block in raw.items():
        where = f"pacing.{alias}"
        if not isinstance(block, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        pacing = AccountPacing()
        for key, value in block.items():
            if key == "expire":
                if not isinstance(value, dict):
                    problems.append(f"{where}.expire: must be a mapping of action to duration")
                    continue
                for kind, duration in value.items():
                    if kind not in kinds:
                        problems.append(f"{where}.expire: unknown action {kind!r}")
                        continue
                    if duration in (None, "never", 0):
                        pacing.expire[kind] = None
                        continue
                    try:
                        pacing.expire[kind] = parse_duration(
                            duration, what=f"{where}.expire.{kind}"
                        )
                    except KnobError as exc:
                        problems.append(str(exc))
                continue
            if key not in kinds:
                problems.append(
                    f"{where}: unknown key {key!r}; use {', '.join(sorted(kinds))} or expire"
                )
                continue
            if not isinstance(value, dict) or set(value) - {"every", "per_hour"}:
                problems.append(f"{where}.{key}: takes {{every: <duration>, per_hour: <n>}}")
                continue
            default = DEFAULT_PACING[key]
            try:
                every = (
                    parse_duration(value["every"], what=f"{where}.{key}.every")
                    if "every" in value
                    else default.every
                )
            except KnobError as exc:
                problems.append(str(exc))
                continue
            # An absent `per_hour` keeps the default cap; `per_hour: null` lifts it.
            per_hour = value.get("per_hour", default.per_hour)
            if per_hour is not None and (
                isinstance(per_hour, bool) or not isinstance(per_hour, int) or per_hour <= 0
            ):
                problems.append(f"{where}.{key}.per_hour: must be a positive whole number")
                continue
            pacing.rules[key] = PacingRule(every=every, per_hour=per_hour)
        out[str(alias)] = pacing
    if problems:
        raise JobConfigError(problems)
    return out


@dataclass
class JobsFile:
    """Everything `jobs.yaml` says, and every problem found reading it."""

    jobs: list[GatewayConfig] = field(default_factory=list)
    pacing: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    #: Names of jobs that are in the file but failed validation.
    rejected: list[str] = field(default_factory=list)


def parse_jobs_document(data: Any) -> JobsFile:
    out = JobsFile()
    if not data:
        return out
    if not isinstance(data, dict):
        out.problems.append("jobs.yaml must be a mapping with a `jobs:` list")
        return out
    unknown = sorted(set(data) - {"jobs", "pacing"})
    if unknown:
        out.problems.append(f"jobs.yaml: unknown top-level key(s) {unknown}; use jobs, pacing")
    try:
        out.pacing = parse_pacing(data.get("pacing"))
    except JobConfigError as exc:
        out.problems.extend(exc.problems)
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        out.problems.append("jobs.yaml: `jobs` must be a list")
        return out
    seen: set[str] = set()
    for entry in jobs:
        if not isinstance(entry, dict):
            out.problems.append("jobs.yaml: every entry under `jobs` must be a mapping")
            continue
        try:
            config = _parse_job(entry)
        except JobConfigError as exc:
            out.problems.extend(exc.problems)
            if entry.get("name"):
                out.rejected.append(str(entry["name"]))
            continue
        if config.name in seen:
            out.problems.append(f"job {config.name!r}: the name is used twice")
            continue
        seen.add(config.name)
        out.jobs.append(config)
    return out


def load_jobs_file(base: Path | None = None) -> JobsFile:
    """Read and validate ``jobs.yaml``; a missing file is an empty one."""
    base = base or CONFIG_DIR
    jobs_path = base / "jobs.yaml"
    if not jobs_path.exists():
        return JobsFile()

    try:
        import yaml
    except ModuleNotFoundError as e:
        raise ConfigurationError(
            "PyYAML is required for jobs.yaml support. Install with: pip install pyyaml"
        ) from e

    with open(jobs_path) as f:
        data = yaml.safe_load(f)
    return parse_jobs_document(data)


def load_gateway_configs(base: Path | None = None) -> list[GatewayConfig]:
    """Load all valid gateway jobs from ``jobs.yaml``."""
    return load_jobs_file(base).jobs


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
