"""Pacing, flood memory and the circuit breaker — one instance per account.

Four separate mechanisms live here because they fail differently:

* **token buckets per `rate_class`.** Reading and sending are not the same
  activity: `contacts.resolveUsername` floods at roughly fifty calls in a
  short period, while `messages.getHistory` tolerates ten a second. The op's
  `rate_class` picks the bucket, so an implementation never has to know.
* **persisted flood memory.** Telethon remembers a `FloodWaitError` in
  `_flood_waited_requests`, in memory, and forgets it when the process exits.
  v1 therefore re-hit every wait after a daemon bounce — and re-hitting a
  flood wait is how a short wait becomes a long one. Deadlines are written to
  `accounts/<alias>/flood.json` keyed by `(method, peer)` and loaded at start.
* **the sleep policy.** A wait we can afford is slept inside the daemon and
  reported as `meta.flood_wait_slept`; anything longer comes back immediately
  as `RATE_LIMITED` with `wait_seconds`, because a CLI blocked for 40 minutes
  with no output is indistinguishable from a hang.
* **the circuit breaker.** `PEER_FLOOD` and `FROZEN_*` are not waits. They are
  the account being told to stop, and the correct response is to stop sending
  entirely rather than back off and retry — so the breaker refuses every
  `rate_class="send"` operation while leaving reads working, and only an
  operator (or an expired `freeze_until`) closes it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core.errors import AccountFrozenError, RateLimitError, SpamFlagError
from tlgr.core.paths import write_private

log = logging.getLogger("tlgr.daemon.ratelimit")

__all__ = ["CircuitBreaker", "FloodMemory", "RateLimiter", "TokenBucket"]


class TokenBucket:
    """Classic token bucket on the monotonic clock.

    `take()` is `async` because the only useful thing to do with an empty
    bucket is wait for it, and waiting must not block the event loop.
    """

    __slots__ = ("_tokens", "_updated", "burst", "rate")

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = max(rate, 0.0001)
        self.burst = max(burst, 1)
        self._tokens = float(self.burst)
        self._updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
            self._updated = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def delay(self, cost: float = 1.0) -> float:
        """Seconds until *cost* tokens are available. 0 when they already are."""
        self._refill()
        if self._tokens >= cost:
            return 0.0
        return (cost - self._tokens) / self.rate

    def consume(self, cost: float = 1.0) -> None:
        self._refill()
        self._tokens -= cost

    async def take(self, cost: float = 1.0) -> float:
        """Wait for and consume *cost* tokens; returns how long it waited."""
        waited = 0.0
        while True:
            wait = self.delay(cost)
            if wait <= 0:
                self.consume(cost)
                return waited
            await asyncio.sleep(wait)
            waited += wait


@dataclass
class FloodDeadline:
    until: float
    seconds: int
    method: str
    peer: str = ""

    @property
    def remaining(self) -> int:
        return max(0, round(self.until - time.time()))


class FloodMemory:
    """Flood deadlines that survive a restart, keyed by `(method, peer)`."""

    def __init__(self, path: Path | None = None, *, persist: bool = True) -> None:
        self.path = path
        self.persist = persist and path is not None
        self._deadlines: dict[str, FloodDeadline] = {}
        self.load()

    @staticmethod
    def key(method: str, peer: Any = None) -> str:
        return f"{method}|{peer if peer is not None else ''}"

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            until = float(value.get("until", 0) or 0)
            if until <= now:
                continue
            self._deadlines[str(key)] = FloodDeadline(
                until=until,
                seconds=int(value.get("seconds", 0) or 0),
                method=str(value.get("method", "")),
                peer=str(value.get("peer", "")),
            )

    def save(self) -> None:
        if not self.persist or self.path is None:
            return
        now = time.time()
        payload = {
            key: {
                "until": deadline.until,
                "seconds": deadline.seconds,
                "method": deadline.method,
                "peer": deadline.peer,
            }
            for key, deadline in self._deadlines.items()
            if deadline.until > now
        }
        with contextlib.suppress(OSError):
            write_private(self.path, json.dumps(payload, indent=0))

    def remember(self, method: str, seconds: int, peer: Any = None) -> None:
        key = self.key(method, peer)
        self._deadlines[key] = FloodDeadline(
            until=time.time() + max(0, seconds),
            seconds=int(seconds),
            method=method,
            peer=str(peer or ""),
        )
        self.save()

    def remaining(self, method: str, peer: Any = None) -> int:
        """Seconds still owed for `(method, peer)`, else 0."""
        now = time.time()
        for key in (self.key(method, peer), self.key(method, None)):
            deadline = self._deadlines.get(key)
            if deadline is None:
                continue
            if deadline.until <= now:
                self._deadlines.pop(key, None)
                continue
            return deadline.remaining
        return 0

    def clear(self) -> None:
        self._deadlines.clear()
        self.save()

    def snapshot(self) -> dict[str, int]:
        return {key: deadline.remaining for key, deadline in self._deadlines.items()}

    def entries(self, *, include_expired: bool = False) -> list[FloodDeadline]:
        """Every remembered deadline, for `tlgr daemon flood list`.

        Expired ones are hidden by default and available on request: "this
        account hit a wait an hour ago" is diagnosis, not a live constraint,
        and mixing the two makes a healthy account look throttled.
        """
        now = time.time()
        return [
            deadline
            for deadline in self._deadlines.values()
            if include_expired or deadline.until > now
        ]

    def forget(self, *, method: str = "", peer: Any = None) -> int:
        """Drop the deadlines matching *method*/*peer*; returns how many.

        Clearing a live server-side FLOOD_WAIT does not lift it — the next
        call re-trips it. This exists for after the cause is fixed, and to
        reset a breaker an operator has investigated.
        """
        removed = 0
        for key, deadline in list(self._deadlines.items()):
            if method and deadline.method != method:
                continue
            if peer is not None and deadline.peer != str(peer):
                continue
            del self._deadlines[key]
            removed += 1
        if removed:
            self.save()
        return removed

    @property
    def next_deadline(self) -> float | None:
        alive = [d.until for d in self._deadlines.values() if d.until > time.time()]
        return max(alive) if alive else None


@dataclass
class CircuitBreaker:
    """Open means "this account has been told to stop sending"."""

    open: bool = False
    reason: str = ""
    since: float = 0.0
    until: float | None = None
    appeal_url: str = ""
    #: Consecutive privacy-class refusals on new peers, which are the early
    #: warning that a PEER_FLOOD is coming.
    strikes: list[float] = field(default_factory=list)

    def trip(self, reason: str, *, until: float | None = None, appeal_url: str = "") -> None:
        self.open = True
        self.reason = reason
        self.since = time.time()
        self.until = until
        self.appeal_url = appeal_url

    def reset(self) -> None:
        self.open = False
        self.reason = ""
        self.until = None
        self.appeal_url = ""
        self.strikes.clear()

    def strike(self, *, window: float = 60.0, limit: int = 3) -> bool:
        """Record a privacy refusal; True when that trips the breaker."""
        now = time.time()
        self.strikes = [t for t in self.strikes if now - t <= window]
        self.strikes.append(now)
        if len(self.strikes) >= limit:
            self.trip("repeated privacy refusals on new peers")
            return True
        return False

    @property
    def state(self) -> str:
        if not self.open:
            return "closed"
        if self.until is not None and self.until <= time.time():
            return "half-open"
        return "open"

    def expired(self) -> bool:
        return self.open and self.until is not None and self.until <= time.time()


class RateLimiter:
    """Everything above, wired together for one account."""

    def __init__(
        self,
        *,
        buckets: dict[str, tuple[float, int]] | None = None,
        flood_path: Path | None = None,
        persist: bool = True,
        sleep_threshold: int = 120,
        max_wait: int = 600,
    ) -> None:
        specs = buckets or {}
        self._buckets = {name: TokenBucket(rate, burst) for name, (rate, burst) in specs.items()}
        self.flood = FloodMemory(flood_path, persist=persist)
        self.breaker = CircuitBreaker()
        self.sleep_threshold = sleep_threshold
        self.max_wait = max_wait
        self.slow_mode: dict[int, float] = {}

    # -- buckets -----------------------------------------------------------

    def bucket(self, rate_class: str) -> TokenBucket:
        if rate_class not in self._buckets:
            self._buckets[rate_class] = TokenBucket(10.0, 20)
        return self._buckets[rate_class]

    async def acquire(self, rate_class: str, *, cost: float = 1.0) -> float:
        return await self.bucket(rate_class).take(cost)

    # -- pre-flight --------------------------------------------------------

    def check(self, *, rate_class: str, method: str = "", peer: Any = None) -> None:
        """Refuse, without a round trip, what the server would refuse anyway.

        Three things are known locally: the breaker is open, a flood deadline
        for this `(method, peer)` has not passed, or the chat's slow mode says
        the next send is in the future. Spending a request to be told so costs
        the account's reputation as well as the round trip.
        """
        if self.breaker.expired():
            self.breaker.reset()
        if self.breaker.open and rate_class == "send":
            error = AccountFrozenError if "froz" in self.breaker.reason.lower() else SpamFlagError
            raise error(
                f"this account is not allowed to send right now: {self.breaker.reason}"
                + (f" (appeal: {self.breaker.appeal_url})" if self.breaker.appeal_url else "")
            )

        if method:
            remaining = self.flood.remaining(method, peer)
            if remaining > 0:
                raise RateLimitError(
                    f"{method} is still rate limited for this account "
                    f"({remaining}s remaining from an earlier FLOOD_WAIT)",
                    wait_seconds=remaining,
                )

        if peer is not None and rate_class == "send":
            next_send = self.slow_mode.get(int(peer)) if _is_int(peer) else None
            if next_send and next_send > time.time():
                wait = round(next_send - time.time())
                raise RateLimitError(
                    f"slow mode is active in this chat for another {wait}s", wait_seconds=wait
                )

    # -- reactions ---------------------------------------------------------

    def sleep_budget(self, requested_max: int | None, remaining_timeout: float) -> int:
        """How long this request may sleep off a flood wait (§6.4, COR-15).

        The per-request `--flood-wait-max` is honoured exactly: a caller that
        said "never sleep more than 5 seconds" must not be held for 120 because
        that is the daemon's default.
        """
        limit = self.sleep_threshold if requested_max is None else max(0, int(requested_max))
        limit = min(limit, self.max_wait)
        return int(max(0, min(limit, remaining_timeout)))

    def note_flood(self, method: str, seconds: int, peer: Any = None) -> None:
        self.flood.remember(method, seconds, peer)

    def note_slow_mode(self, chat_id: int, next_send_date: float | None) -> None:
        if next_send_date:
            self.slow_mode[int(chat_id)] = float(next_send_date)
        else:
            self.slow_mode.pop(int(chat_id), None)

    def trip(self, reason: str, *, until: float | None = None, appeal_url: str = "") -> None:
        log.warning("circuit breaker opened: %s", reason, extra={"reason": reason})
        self.breaker.trip(reason, until=until, appeal_url=appeal_url)

    def reset_breaker(self) -> None:
        self.breaker.reset()

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        deadline = self.flood.next_deadline
        return {
            "circuit": self.breaker.state,
            "circuit_reason": self.breaker.reason or None,
            "flood_until": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(deadline)) if deadline else None
            ),
            "flood_entries": len(self.flood.snapshot()),
        }


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True
