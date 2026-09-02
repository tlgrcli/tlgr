"""Whether the daemon is doing anything, and whether it may stop (COR-08/11).

v1 counted "seconds since the last IPC request" and nothing else, so:

* a `chat posters` scan ten minutes into a dialog walk was idle, and the
  monitor killed it mid-flight (COR-11);
* a daemon whose only job was pushing webhooks was idle by definition, so it
  stopped and the webhook silently stopped with it (COR-08);
* an open `tlgr watch` stream was idle for as long as the chat was quiet.

Activity here is the union of everything that would be lost by exiting: in
flight requests, open event streams, running file transfers, running jobs, an
enabled webhook, and a pending login. `idle_timeout` counts only while all of
them are zero, and the monitor refuses to stop while the in-flight counter is
non-zero even if the clock says otherwise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["ActivityTracker"]


@dataclass
class ActivityTracker:
    """Counters plus a last-activity clock. Cheap enough to touch per request."""

    in_flight: int = 0
    event_streams: int = 0
    transfers: int = 0
    pending_logins: int = 0
    webhook_enabled: bool = False
    jobs_running: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    last_request_at: float = 0.0

    def touch(self) -> None:
        self.last_activity = time.monotonic()
        self.last_request_at = time.time()

    # -- scopes ------------------------------------------------------------

    def begin_request(self) -> None:
        self.in_flight += 1
        self.touch()

    def end_request(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.touch()

    def begin_stream(self) -> None:
        self.event_streams += 1
        self.touch()

    def end_stream(self) -> None:
        self.event_streams = max(0, self.event_streams - 1)
        self.touch()

    def begin_transfer(self) -> None:
        self.transfers += 1
        self.touch()

    def end_transfer(self) -> None:
        self.transfers = max(0, self.transfers - 1)
        self.touch()

    # -- decision ----------------------------------------------------------

    @property
    def busy(self) -> bool:
        return bool(
            self.in_flight
            or self.event_streams
            or self.transfers
            or self.pending_logins
            or self.jobs_running
            or self.webhook_enabled
        )

    def idle_seconds(self) -> float:
        if self.busy:
            return 0.0
        return max(0.0, time.monotonic() - self.last_activity)

    def may_stop(self, idle_timeout: int) -> bool:
        """True only when nothing is happening and nothing happened recently."""
        if idle_timeout <= 0:
            return False
        if self.busy:
            return False
        return self.idle_seconds() >= idle_timeout

    def snapshot(self) -> dict[str, object]:
        return {
            "in_flight": self.in_flight,
            "event_streams": self.event_streams,
            "transfers": self.transfers,
            "pending_logins": self.pending_logins,
            "jobs_running": self.jobs_running,
            "webhook": self.webhook_enabled,
            "idle_seconds": round(self.idle_seconds(), 1),
        }


def effective_idle_timeout(configured: int, *, webhook_enabled: bool, managed_by: str = "") -> int:
    """§6.10: forced to 0 when stopping would break something.

    A webhook subscriber that exits has silently unsubscribed. Under
    launchd/systemd an idle exit is either respawned immediately (churn) or,
    with `KeepAlive.SuccessfulExit=false`, never respawned at all — which is
    COR-39, and is why the plist also forces this to 0.
    """
    if webhook_enabled:
        return 0
    if managed_by in ("launchd", "systemd"):
        return 0
    return max(0, configured)
