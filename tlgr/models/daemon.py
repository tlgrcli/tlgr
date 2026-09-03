"""Daemon, job, flood and dead-letter shapes.

Everything an operator reads when something is wrong. Two of these deserve a
note:

* `AccountHealth.state` is a **state machine**, not a boolean. v1 reported
  which clients the daemon held, so an account whose connection had died
  still appeared in `accounts` and the daemon still called itself healthy
  (COR-13/COR-37). `state` plus `behind_seconds` is what makes "alive" and
  "working" separable.
* `FloodRecord` exists because a flood deadline outlives a process. Telethon
  remembers one in memory and forgets it on exit, so v1 re-hit every wait
  after a restart — and re-hitting a wait is how a short one becomes long.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "AccountHealth",
    "DaemonStatus",
    "DeadLetter",
    "DeadLetterResult",
    "EventBusStatus",
    "FloodRecord",
    "FloodResult",
    "HealthSummary",
    "Job",
    "JobState",
    "JobTestFrame",
    "LifecycleResult",
    "LogLine",
    "ReconnectResult",
    "ReconnectedAccount",
    "SaveStateResult",
    "SavedState",
    "ServiceResult",
    "WebhookProbe",
    "WebhookSettings",
]


class AccountHealth(Model):
    """One account's connection and sync health."""

    alias: str
    state: str = "unknown"
    user_id: int | None = None
    username: str | None = None
    dc_id: int | None = None
    proxy: str | None = None
    ping_ms: float | None = None
    pts: int | None = None
    qts: int | None = None
    seq: int | None = None
    date: str | None = None
    behind_seconds: int | None = None
    catching_up: bool = False
    channels_tracked: int = 0
    last_update_at: str | None = None
    connected_since: str | None = None
    reconnects: int = 0
    in_flight: int = 0
    resync_needed: list[int] = []
    flood_waits: int = 0
    circuit: str = "closed"
    frozen: bool = False
    error: str | None = None


class EventBusStatus(Model):
    buffered: int = 0
    oldest_seq: int | None = None
    last_seq: int = 0
    subscribers: int = 0
    dropped: int = 0


class DaemonStatus(Model):
    """`tlgr daemon status`. `running` and `healthy` are different questions."""

    running: bool = False
    ready: bool = False
    healthy: bool = False
    pid: int | None = None
    uptime_seconds: int = 0
    version: str = ""
    protocol: int = 0
    layer: int = 0
    socket: str = ""
    socket_owner: int | None = None
    managed_by: str | None = None
    accounts: list[AccountHealth] = []
    events: EventBusStatus | None = None
    webhook: dict[str, Any] = {}
    jobs: list[dict[str, Any]] = []
    # v1's `/daemon/status` carried these two, and AGENT.md documents them.
    connections: dict[str, bool] = {}
    disconnected: list[str] = []


class HealthSummary(Model):
    """`tlgr status`: one screen, the states where everything else fails."""

    account: str = ""
    user_id: int | None = None
    username: str | None = None
    authorized: bool = False
    connected: bool = False
    dc_id: int | None = None
    proxy: str | None = None
    ping_ms: float | None = None
    layer: int = 0
    behind_seconds: int | None = None
    daemon_running: bool = False
    daemon_healthy: bool = False
    jobs_running: int = 0
    webhook_enabled: bool = False
    flood_waits: int = 0
    frozen: dict[str, Any] = {}
    unconfirmed_sessions: int = 0
    terms_pending: bool = False
    problems: list[str] = []


class LifecycleResult(Model):
    """`daemon start` / `stop` / `restart`."""

    started: bool = False
    stopped: bool = False
    restarted: bool = False
    already: bool = False
    pid: int | None = None
    socket: str = ""
    ready: bool = False
    accounts: list[str] = []
    catch_up: bool = True


class ServiceResult(Model):
    """`daemon install` / `uninstall`."""

    installed: bool = False
    uninstalled: bool = False
    already: bool = False
    supervisor: str = ""
    unit: str = ""
    path: str = ""
    stopped: bool = False


class LogLine(Model):
    ts: str = ""
    level: str = ""
    account: str | None = None
    logger: str = ""
    message: str = ""
    raw: str = ""


class ReconnectedAccount(Model):
    alias: str
    reconnected: bool = False
    dc_id: int | None = None
    caught_up: bool = False
    error: str | None = None


class ReconnectResult(Model):
    accounts: list[ReconnectedAccount] = []


class SavedState(Model):
    alias: str
    pts: int | None = None
    qts: int | None = None
    seq: int | None = None
    date: str | None = None
    channels: int = 0
    entities: int = 0
    error: str | None = None


class SaveStateResult(Model):
    accounts: list[SavedState] = []


class FloodRecord(Model):
    """One remembered rate-limit deadline."""

    account: str
    #: No default: `Model` omits a field equal to its default, and a record
    #: whose kind is absent reads as one that has no kind.
    kind: str
    method: str = ""
    chat: str | None = None
    wait_seconds: int = 0
    until: str | None = None
    hits: int = 1
    circuit_open: bool = False
    expired: bool = False


class FloodResult(Model):
    cleared: int = 0
    circuit_open: bool = False
    accounts: list[str] = []


class DeadLetter(Model):
    """An event a consumer could not be given."""

    id: str
    seq: int = 0
    source: str = "webhook"
    event: str = ""
    account: str = ""
    chat_id: int | None = None
    attempts: int = 1
    last_error: str = ""
    first_failed_at: str = ""
    last_failed_at: str = ""


class DeadLetterResult(Model):
    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    deleted: int = 0
    remaining: int = 0
    dry_run: bool = False


class JobState(Model):
    """One gateway job."""

    name: str
    account: str = ""
    enabled: bool = True
    running: bool = False
    events: list[str] = []
    filters: dict[str, Any] = {}
    processors: list[str] = []
    actions: list[dict[str, Any]] = []
    matched: int = 0
    skipped: int = 0
    actions_run: int = 0
    errors: int = 0
    last_match_at: str | None = None
    last_error: str | None = None


class Job(Model):
    """The result of a job mutation."""

    name: str
    #: No default at all: this is the answer `job enable`/`job disable` was
    #: asked for, and `Model` drops a field equal to its default — so either
    #: value would sometimes be missing from the reply.
    enabled: bool
    account: str = ""
    events: list[str] = []
    removed: bool = False
    reloaded: bool = False
    already: bool = False
    loaded: int = 0
    added: list[str] = []
    removed_names: list[str] = []
    changed: list[str] = []
    errors: list[str] = []


class JobTestFrame(Model):
    """One event fed through a job, and what the pipeline decided."""

    seq: int = 0
    event: str = ""
    matched: bool = False
    filter_trace: list[str] = []
    processed_text: str | None = None
    actions: list[dict[str, Any]] = []


class WebhookSettings(Model):
    """`tlgr webhook get` / `set`. Secrets are redacted unless asked for."""

    enabled: bool = False
    url: str = ""
    events: list[str] = []
    filters: dict[str, Any] = {}
    sign: str = "hmac-sha256"
    secret: str | None = None
    token: str | None = None
    max_attempts: int = 5
    backoff: int = 2
    timeout: int = 30
    queue: int = 10000
    on_lag: str = "drop"
    batch: int = 1
    last_delivery_at: str | None = None
    last_status: int | None = None
    delivered: int = 0
    failed: int = 0
    dead_letters: int = 0
    queue_depth: int = 0
    last_seq: int = 0


class WebhookProbe(Model):
    """`tlgr webhook test`: the exact request a receiver would have to verify."""

    url: str = ""
    status: int | None = None
    latency_ms: int = 0
    request_headers: dict[str, str] = {}
    body: str = ""
    error: str | None = None
