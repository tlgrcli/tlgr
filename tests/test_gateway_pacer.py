"""The pacer: a floor between actions, jitter that only lengthens it, an hourly cap."""

from __future__ import annotations

import itertools
import random

from tlgr.gateway.pacer import DEFAULT_PACING, MAX_SLOWDOWN, Pacer, PacingRule


def _drain(pacer: Pacer, count: int, start: float = 0.0) -> list[float]:
    """Take *count* slots as fast as the pacer allows; return when each opened."""
    now = start
    taken: list[float] = []
    for _ in range(count):
        now += pacer.delay(now)
        pacer.consume(now)
        taken.append(now)
    return taken


class TestSpacing:
    def test_the_floor_is_never_broken_and_jitter_stays_in_bounds(self):
        pacer = Pacer(PacingRule(every=4.0), rng=random.Random(7))
        taken = _drain(pacer, 200)
        gaps = [b - a for a, b in itertools.pairwise(taken)]
        assert min(gaps) >= 4.0
        assert max(gaps) <= 6.0
        # Jitter, not a metronome.
        assert len({round(gap, 3) for gap in gaps}) > 50

    def test_a_free_slot_opens_immediately(self):
        pacer = Pacer(PacingRule(every=2.0))
        assert pacer.delay(100.0) == 0.0

    def test_a_backlog_drains_at_the_configured_rate(self):
        """Three hundred replayed messages are all acted on, none skipped, at the pace."""
        pacer = Pacer(PacingRule(every=2.0), rng=random.Random(1))
        taken = _drain(pacer, 300)
        assert len(taken) == 300
        span = taken[-1] - taken[0]
        assert 299 * 2.0 <= span <= 299 * 3.0


class TestHourlyCap:
    def test_the_cap_holds_the_next_slot_for_the_rest_of_the_hour(self):
        pacer = Pacer(PacingRule(every=1.0, per_hour=10), rng=random.Random(3))
        taken = _drain(pacer, 11)
        assert taken[10] >= taken[0] + 3600.0

    def test_no_rolling_hour_ever_exceeds_the_cap(self):
        pacer = Pacer(DEFAULT_PACING["react"], rng=random.Random(5))
        taken = _drain(pacer, 900)
        for index, moment in enumerate(taken):
            in_window = [t for t in taken[index:] if t < moment + 3600.0]
            assert len(in_window) <= 300


class TestFloodWait:
    def test_a_flood_holds_the_queue_and_slows_it(self):
        pacer = Pacer(PacingRule(every=2.0), rng=random.Random(2))
        pacer.consume(0.0)
        pacer.penalize(1.0, 30.0)
        assert pacer.delay(1.0) >= 30.0
        assert pacer.slowdown == 2.0
        now = 31.0
        pacer.consume(now)
        assert pacer.next_at - now >= 4.0

    def test_the_slowdown_is_capped_and_recovers(self):
        pacer = Pacer(PacingRule(every=1.0))
        for _ in range(10):
            pacer.penalize(0.0, 1.0)
        assert pacer.slowdown == MAX_SLOWDOWN
        pacer.delay(601.0)
        assert pacer.slowdown == 1.0
