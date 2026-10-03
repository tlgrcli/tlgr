"""Pacing for job actions: one queue per (account, action kind).

The daemon's token buckets (`daemon/ratelimit.py`) protect the account from a
burst of requests. They do not make a stream of automated reactions look like
a person: a bucket with a burst of twenty lets twenty reactions out in the same
second after a quiet hour. A pacer is the other half. It spaces one kind of
action on one account at a floor of `every` seconds plus jitter, and caps the
count in any rolling hour.

Jitter only ever lengthens the gap. `every: 4s` is a promise that reactions
are never closer than four seconds, so the spacing is drawn from
`[every, every * 1.5]` (a mean of 1.25x, about +-20% around it) rather than
from a window centred on `every` that would break the floor.

A FLOOD_WAIT on a kind pushes that kind's next slot past the wait and doubles
its spacing, up to eight times. The slow-down lifts once ten minutes pass
without another one. Other kinds are unaffected: a flood on reactions says
nothing about forwards.

Time comes from an injected clock (`now()`, wall seconds), so tests drive the
spacing without sleeping.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Protocol

__all__ = ["DEFAULT_PACING", "Clock", "Pacer", "PacingRule", "SystemClock"]


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall-clock time and real sleeps."""

    def now(self) -> float:
        import time

        return time.time()

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(max(0.0, seconds))


@dataclass(frozen=True)
class PacingRule:
    every: float
    per_hour: int | None = None


#: Cautious defaults, per account. A reaction every four seconds at most and
#: three hundred an hour is well under anything a person does by hand.
DEFAULT_PACING: dict[str, PacingRule] = {
    "react": PacingRule(every=4.0, per_hour=300),
    "read": PacingRule(every=2.0),
    "view": PacingRule(every=2.0),
    "forward": PacingRule(every=1.5),
    "reply": PacingRule(every=1.5),
}

#: The spacing multiplier is drawn from this range.
JITTER = (1.0, 1.5)
MAX_SLOWDOWN = 8.0
SLOWDOWN_RECOVERY_S = 600.0
HOUR = 3600.0


class Pacer:
    """Spacing and an hourly cap for one action kind on one account."""

    def __init__(self, rule: PacingRule, *, rng: random.Random | None = None) -> None:
        self.rule = rule
        self.rng = rng or random.Random()
        self.next_at = 0.0
        self.slowdown = 1.0
        self.last_flood_at = 0.0
        self._recent: deque[float] = deque()

    def configure(self, rule: PacingRule) -> None:
        self.rule = rule

    def delay(self, now: float) -> float:
        """Seconds until the next slot opens; 0 when one is open now."""
        if self.slowdown > 1.0 and now - self.last_flood_at >= SLOWDOWN_RECOVERY_S:
            self.slowdown = 1.0
        wait = max(0.0, self.next_at - now)
        cap = self.rule.per_hour
        if cap:
            while self._recent and now - self._recent[0] >= HOUR:
                self._recent.popleft()
            if len(self._recent) >= cap:
                # A millisecond past the hour, so float rounding cannot land the
                # slot inside the window it is waiting out.
                wait = max(wait, self._recent[0] + HOUR - now + 0.001)
        return wait

    def consume(self, now: float) -> None:
        """Take the open slot and schedule the next one."""
        spacing = self.rule.every * self.rng.uniform(*JITTER) * self.slowdown
        self.next_at = now + spacing
        if self.rule.per_hour:
            self._recent.append(now)

    def penalize(self, now: float, wait: float) -> None:
        """A FLOOD_WAIT of *wait* seconds: hold the queue and widen its spacing."""
        self.slowdown = min(MAX_SLOWDOWN, self.slowdown * 2.0)
        self.last_flood_at = now
        self.next_at = max(self.next_at, now + max(0.0, wait))

    def snapshot(self) -> dict[str, float | int | None]:
        return {
            "every_s": self.rule.every,
            "per_hour": self.rule.per_hour,
            "slowdown": self.slowdown,
            "last_hour": len(self._recent),
        }
