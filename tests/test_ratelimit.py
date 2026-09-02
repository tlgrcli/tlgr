"""Pacing, flood memory and the breaker (§6.4).

The property that matters most is the boring one: a flood deadline earned
before a restart is still owed after it. Telethon's memory is in-process, so
v1 re-hit every wait after a daemon bounce — and re-hitting a flood wait is
how a two-minute wait becomes an hour.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tlgr.core.errors import (
    EXIT_RATE_LIMITED,
    EXIT_SPAM_FLAGGED,
    RateLimitError,
    SpamFlagError,
    classify,
)
from tlgr.daemon.ratelimit import CircuitBreaker, FloodMemory, RateLimiter, TokenBucket


class TestTokenBucket:
    def test_a_full_bucket_does_not_wait(self):
        bucket = TokenBucket(rate=10, burst=5)
        assert bucket.delay() == 0.0

    def test_an_empty_bucket_waits_proportionally(self):
        bucket = TokenBucket(rate=2, burst=1)
        bucket.consume()
        assert 0.4 < bucket.delay() <= 0.5

    def test_it_refills_over_time(self):
        bucket = TokenBucket(rate=1000, burst=2)
        bucket.consume(2)
        time.sleep(0.01)
        assert bucket.tokens > 0

    def test_it_never_exceeds_the_burst(self):
        bucket = TokenBucket(rate=1000, burst=3)
        time.sleep(0.02)
        assert bucket.tokens <= 3

    async def test_take_paces_a_sequence(self):
        bucket = TokenBucket(rate=100, burst=1)
        started = time.monotonic()
        for _ in range(4):
            await bucket.take()
        # Three refills at 100/s ≈ 30 ms; the point is that it waited at all.
        assert time.monotonic() - started >= 0.02


class TestFloodMemory:
    def test_a_deadline_survives_a_restart(self, tlgr_home: Path):
        """The whole reason this file exists rather than using Telethon's."""
        path = tlgr_home / "flood.json"
        first = FloodMemory(path)
        first.remember("SendMessageRequest", 120, peer=-100)
        assert first.remaining("SendMessageRequest", peer=-100) > 0

        second = FloodMemory(path)
        assert 110 < second.remaining("SendMessageRequest", peer=-100) <= 120

    def test_it_is_keyed_by_method_and_peer(self, tlgr_home: Path):
        memory = FloodMemory(tlgr_home / "flood.json")
        memory.remember("SendMessageRequest", 60, peer=-100)
        assert memory.remaining("SendMessageRequest", peer=-200) == 0
        assert memory.remaining("GetHistoryRequest", peer=-100) == 0

    def test_an_expired_deadline_is_forgotten(self, tlgr_home: Path):
        memory = FloodMemory(tlgr_home / "flood.json")
        memory.remember("X", 0)
        assert memory.remaining("X") == 0

    def test_the_file_is_private(self, tlgr_home: Path):
        path = tlgr_home / "flood.json"
        FloodMemory(path).remember("X", 60)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_persistence_can_be_turned_off(self, tlgr_home: Path):
        path = tlgr_home / "flood.json"
        memory = FloodMemory(path, persist=False)
        memory.remember("X", 60)
        assert not path.exists()
        assert memory.remaining("X") > 0


class TestCircuitBreaker:
    def test_it_opens_and_reports_its_state(self):
        breaker = CircuitBreaker()
        assert breaker.state == "closed"
        breaker.trip("PEER_FLOOD")
        assert breaker.state == "open"
        breaker.reset()
        assert breaker.state == "closed"

    def test_three_privacy_refusals_in_a_minute_trip_it(self):
        """The early warning that a PEER_FLOOD is coming."""
        breaker = CircuitBreaker()
        assert breaker.strike() is False
        assert breaker.strike() is False
        assert breaker.strike() is True
        assert breaker.open

    def test_it_closes_when_the_freeze_expires(self):
        breaker = CircuitBreaker()
        breaker.trip("frozen", until=time.time() - 1)
        assert breaker.expired() is True


class TestRateLimiter:
    def _limiter(self, tmp: Path) -> RateLimiter:
        return RateLimiter(
            buckets={"read": (100.0, 10), "send": (1.0, 1)},
            flood_path=tmp / "flood.json",
            sleep_threshold=120,
            max_wait=600,
        )

    def test_an_owed_flood_wait_is_refused_without_a_round_trip(self, tlgr_home: Path):
        limiter = self._limiter(tlgr_home)
        limiter.note_flood("SendMessageRequest", 300, peer=-100)
        with pytest.raises(RateLimitError) as caught:
            limiter.check(rate_class="send", method="SendMessageRequest", peer=-100)
        body = classify(caught.value)
        assert body.exit_code == EXIT_RATE_LIMITED
        assert 290 < body.wait_seconds <= 300

    def test_an_open_breaker_refuses_sends(self, tlgr_home: Path):
        limiter = self._limiter(tlgr_home)
        limiter.trip("PeerFloodError")
        with pytest.raises(SpamFlagError) as caught:
            limiter.check(rate_class="send")
        assert classify(caught.value).exit_code == EXIT_SPAM_FLAGGED

    def test_an_open_breaker_still_allows_reads(self, tlgr_home: Path):
        """Being told to stop sending is not being told to stop looking."""
        limiter = self._limiter(tlgr_home)
        limiter.trip("PeerFloodError")
        limiter.check(rate_class="read")

    def test_a_frozen_account_is_reported_as_frozen(self, tlgr_home: Path):
        from tlgr.core.errors import AccountFrozenError

        limiter = self._limiter(tlgr_home)
        limiter.trip("account is frozen by Telegram", appeal_url="https://t.me/spambot")
        with pytest.raises(AccountFrozenError) as caught:
            limiter.check(rate_class="send")
        assert "spambot" in str(caught.value)

    def test_slow_mode_is_refused_locally(self, tlgr_home: Path):
        """The server would refuse it too, and charge us for asking."""
        limiter = self._limiter(tlgr_home)
        limiter.note_slow_mode(-100, time.time() + 30)
        with pytest.raises(RateLimitError) as caught:
            limiter.check(rate_class="send", peer=-100)
        assert "slow mode" in str(caught.value)

    def test_the_sleep_budget_honours_the_request(self, tlgr_home: Path):
        """COR-15: --flood-wait-max means what it says."""
        limiter = self._limiter(tlgr_home)
        assert limiter.sleep_budget(5, remaining_timeout=120) == 5
        assert limiter.sleep_budget(None, remaining_timeout=120) == 120
        assert limiter.sleep_budget(999999, remaining_timeout=120) == 120
        # Never longer than what is left of the caller's own timeout.
        assert limiter.sleep_budget(120, remaining_timeout=10) == 10

    def test_the_breaker_reopens_the_gate_after_a_reset(self, tlgr_home: Path):
        limiter = self._limiter(tlgr_home)
        limiter.trip("PeerFloodError")
        limiter.reset_breaker()
        limiter.check(rate_class="send")

    def test_the_snapshot_says_what_status_needs(self, tlgr_home: Path):
        limiter = self._limiter(tlgr_home)
        limiter.note_flood("X", 60)
        snapshot = limiter.snapshot()
        assert snapshot["circuit"] == "closed"
        assert snapshot["flood_until"] is not None

    async def test_acquire_paces_by_rate_class(self, tlgr_home: Path):
        limiter = self._limiter(tlgr_home)
        started = time.monotonic()
        await limiter.acquire("send")
        await limiter.acquire("send")
        assert time.monotonic() - started >= 0.5, "the send bucket did not pace"

    async def test_reads_are_not_paced_like_sends(self, tlgr_home: Path):
        limiter = self._limiter(tlgr_home)
        started = time.monotonic()
        await asyncio.gather(*(limiter.acquire("read") for _ in range(5)))
        assert time.monotonic() - started < 0.2


async def test_the_daemon_gives_each_account_its_own_limiter(daemon, stub_account):
    first = daemon.sessions.limiter(stub_account)
    assert daemon.sessions.limiter(stub_account) is first
    assert daemon.sessions.limiter("other") is not first


async def test_unfreeze_closes_the_breaker_over_the_socket(live_daemon, client, in_thread):
    live_daemon.sessions.limiter("work").trip("PeerFloodError")
    result = await in_thread(client.admin, "unfreeze", {"account": "work"})
    assert result["circuit"] == "closed"
    assert live_daemon.sessions.limiter("work").breaker.open is False
