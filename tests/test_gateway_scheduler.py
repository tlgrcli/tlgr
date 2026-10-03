"""The job-action scheduler: delays, pacing, persistence, coalescing, takeover.

Driven with a virtual clock and a recording op runner, so an hour of pacing
takes milliseconds and every assertion is about what would have reached the
op layer, and when.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_clock import FakeClock
from fake_telethon import FakeTelegramClient, World
from job_helpers import (
    ALICE,
    CHANNEL_ID,
    bus_envelope,
    channel_update,
    deliver,
    make_scheduler,
    photo_media,
    private_update,
    voice_media,
)
from telethon import errors
from telethon.tl import types

from tlgr.actions.read import reading_seconds
from tlgr.actions.reply import typing_seconds
from tlgr.gateway.config import _parse_job
from tlgr.gateway.engine import Gateway
from tlgr.gateway.pacer import PacingRule
from tlgr.gateway.pending import PendingStore
from tlgr.gateway.scheduler import AccountPacing


def _job(scheduler, actions, **job_keys):
    config = _parse_job({"name": "dm", "account": "work", "actions": actions, **job_keys})
    client = SimpleNamespace(client=FakeTelegramClient(World()), resolve_chat=AsyncMock())
    return Gateway(config, client, scheduler=scheduler)


@pytest.fixture
async def sched():
    scheduler, runner, clock = make_scheduler()
    scheduler.start()
    try:
        yield scheduler, runner, clock
    finally:
        await scheduler.stop(timeout=0.1)


def _stats(scheduler, index=0, job="dm"):
    return next(row for row in scheduler.job_stats(job) if row["index"] == index)


class TestDelay:
    async def test_the_bus_handler_never_waits_for_the_delay(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {"delay": "90s"}}])
        started = time.monotonic()
        await deliver(job, private_update(10))
        assert time.monotonic() - started < 1.0
        assert runner.calls == []
        assert len(scheduler.items) == 1

        await clock.advance(89)
        assert runner.calls == []
        await clock.advance(2)
        assert runner.ops() == ["message.read"]

    async def test_the_delay_counts_from_arrival_not_the_message_date(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"reply": {"text": "ok", "typing": False, "delay": "30s"}}])
        update = private_update(10)
        update.message.date = datetime(2020, 1, 1)
        await deliver(job, update)
        item = next(iter(scheduler.items.values()))
        assert item.due_at == pytest.approx(clock.now() + 30)

    async def test_each_action_has_its_own_delay(self, sched):
        scheduler, runner, clock = sched
        job = _job(
            scheduler,
            [{"forward": {"to": "@archive"}}, {"react": {"emoji": "👍", "delay": "120s"}}],
        )
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["message.forward"]
        await clock.advance(120)
        assert runner.ops() == ["message.forward", "message.read", "reaction.add"]


class TestPercent:
    async def test_rolls_are_independent_and_reproducible(self):
        outcomes = []
        for _ in range(2):
            scheduler, runner, clock = make_scheduler(seed=42)
            job = _job(scheduler, [{"react": {"emoji": "👍", "percent": 50}}])
            for message_id in range(1, 41):
                await deliver(job, private_update(message_id))
            outcomes.append(sorted(item.msg_id for item in scheduler.items.values()))
            assert _stats(scheduler)["skipped"] == 40 - len(outcomes[-1])
        assert outcomes[0] == outcomes[1]
        assert 8 < len(outcomes[0]) < 32

    async def test_zero_percent_never_acts(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {"percent": 0}}])
        await deliver(job, private_update(10))
        assert scheduler.items == {}
        assert _stats(scheduler)["skipped"] == 1


class TestPacing:
    async def test_reactions_keep_the_floor_and_drain_a_backlog(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"react": "👍"}])
        for message_id in range(1, 31):
            await deliver(job, private_update(message_id, user=ALICE + message_id))
        await clock.run_for(200, step=1.0)
        times = runner.times("reaction.add")
        assert len(times) == 30
        gaps = [b - a for a, b in itertools.pairwise(times)]
        assert min(gaps) >= 4.0
        assert max(gaps) <= 6.0 + 1.0
        assert _stats(scheduler)["done"] == 30

    async def test_a_reaction_backlog_does_not_delay_forwards(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"react": "👍"}, {"forward": {"to": "@archive"}}])
        for message_id in range(1, 11):
            await deliver(job, private_update(message_id, user=ALICE + message_id))
        await clock.run_for(20, step=0.5)
        assert len(runner.requests("message.forward")) == 10
        assert len(runner.requests("reaction.add")) < 10

    async def test_configured_pacing_applies(self, sched):
        scheduler, runner, clock = sched
        scheduler.configure(AccountPacing(rules={"forward": PacingRule(every=10.0)}))
        job = _job(scheduler, [{"forward": {"to": "@archive"}}])
        for message_id in range(1, 4):
            await deliver(job, private_update(message_id))
        await clock.run_for(25, step=0.5)
        times = runner.times("message.forward")
        assert len(times) == 3
        assert times[1] - times[0] >= 10.0


class TestPersistence:
    async def test_pending_items_survive_a_restart_with_their_due_time(self, tmp_path):
        store = PendingStore(tmp_path / "pending.json")
        clock = FakeClock()
        first, runner, _ = make_scheduler(clock=clock, store=store)
        first.start()
        job = _job(first, [{"reply": {"text": "later", "typing": False, "delay": "100s"}}])
        await deliver(job, private_update(10))
        due = next(iter(first.items.values())).due_at
        await clock.advance(30)
        await first.stop(timeout=0.1)
        assert (tmp_path / "pending.json").stat().st_mode & 0o777 == 0o600

        second, runner2, _ = make_scheduler(clock=clock, store=store)
        assert second.resume({"dm"}) == 1
        second.start()
        try:
            assert next(iter(second.items.values())).due_at == due
            await clock.advance(69)
            assert runner2.calls == []
            await clock.advance(2)
            assert runner2.ops() == ["message.send"]
            assert runner2.requests("message.send")[0]["text"] == "later"
        finally:
            await second.stop(timeout=0.1)

    async def test_items_of_a_removed_job_are_dropped_on_resume(self, tmp_path):
        store = PendingStore(tmp_path / "pending.json")
        first, _, clock = make_scheduler(store=store)
        job = _job(first, [{"read": {"delay": "100s"}}])
        await deliver(job, private_update(10))
        first.flush()
        second, _, _ = make_scheduler(clock=clock, store=store)
        assert second.resume({"another-job"}) == 0
        assert second.items == {}

    async def test_a_replayed_update_is_not_acted_on_twice(self, tmp_path):
        store = PendingStore(tmp_path / "pending.json")
        first, runner, clock = make_scheduler(store=store)
        first.start()
        job = _job(first, [{"forward": {"to": "@archive"}}])
        await deliver(job, private_update(10))
        await clock.advance(1)
        await first.stop(timeout=0.1)

        second, runner2, _ = make_scheduler(clock=clock, store=store)
        second.resume({"dm"})
        second.start()
        try:
            await deliver(_job(second, [{"forward": {"to": "@archive"}}]), private_update(10))
            await clock.advance(5)
            assert runner2.calls == []
        finally:
            await second.stop(timeout=0.1)


class TestExpiry:
    async def test_a_reaction_expires_after_a_day_but_a_read_does_not(self, sched):
        scheduler, runner, clock = sched
        job = _job(
            scheduler,
            [{"react": {"emoji": "👍", "delay": "25h"}}, {"read": {"delay": "25h"}}],
        )
        await deliver(job, private_update(10))
        await clock.advance(25 * 3600 + 120)
        assert _stats(scheduler, 0)["expired"] == 1
        assert _stats(scheduler, 1)["done"] == 1
        assert runner.ops() == ["message.read"]

    async def test_expiry_is_configurable(self, sched):
        scheduler, runner, clock = sched
        scheduler.configure(AccountPacing(expire={"forward": 60.0}))
        job = _job(scheduler, [{"forward": {"to": "@archive", "delay": "2m"}}])
        await deliver(job, private_update(10))
        await clock.advance(200)
        assert _stats(scheduler)["expired"] == 1


class TestReact:
    async def test_the_reaction_replaces_and_follows_a_read_up_to_the_message(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"react": {"emoji": "🔥", "big": True}}])
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["message.read", "reaction.add"]
        assert runner.requests("message.read")[0] == {"chat": str(ALICE), "up_to": 10}
        assert runner.requests("reaction.add")[0] == {
            "chat": str(ALICE),
            "msg_id": 10,
            "emoji": ["🔥"],
            "replace": True,
            "big": True,
        }

    async def test_a_pending_read_is_coalesced_into_the_implied_one(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {"delay": "10m"}}, {"react": "👍"}])
        await deliver(job, private_update(10))
        await clock.run_for(15 * 60, step=5)
        assert runner.ops() == ["message.read", "reaction.add"]
        assert _stats(scheduler, 0)["done"] == 1

    async def test_no_read_when_the_chat_is_already_read_that_far(self, sched):
        scheduler, runner, clock = sched
        scheduler.external_read(ALICE, None, 50)
        job = _job(scheduler, [{"react": "👍"}], on_takeover="ignore")
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["reaction.add"]

    async def test_random_and_weighted_picks(self):
        scheduler, runner, clock = make_scheduler(seed=3)
        job = _job(scheduler, [{"react": ["👍", "❤", "🔥"]}])
        for message_id in range(1, 61):
            await deliver(job, private_update(message_id, user=ALICE + message_id))
        picked = {item.payload["emoji"] for item in scheduler.items.values()}
        assert picked == {"👍", "❤", "🔥"}

        scheduler, runner, clock = make_scheduler(seed=3)
        job = _job(scheduler, [{"react": {"👍": 9, "🔥": 1}}])
        for message_id in range(1, 201):
            await deliver(job, private_update(message_id, user=ALICE + message_id))
        counts = [item.payload["emoji"] for item in scheduler.items.values()]
        assert counts.count("👍") > 5 * counts.count("🔥")

    async def test_an_album_gets_one_reaction_on_its_caption(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"react": {"emoji": "👍", "delay": "5s"}}])
        await deliver(job, private_update(20, "", media=photo_media(), grouped_id=900))
        await deliver(job, private_update(21, "look", media=photo_media(), grouped_id=900))
        await deliver(job, private_update(22, "", media=photo_media(), grouped_id=900))
        await clock.advance(10)
        reactions = runner.requests("reaction.add")
        assert len(reactions) == 1
        assert reactions[0]["msg_id"] == 21

    async def test_reaction_invalid_is_an_error_and_is_not_retried(self, sched):
        scheduler, runner, clock = sched
        runner.fail("reaction.add", errors.ReactionInvalidError(request=None))
        job = _job(scheduler, [{"react": "🦄"}])
        await deliver(job, private_update(10))
        await clock.advance(200)
        assert len(runner.requests("reaction.add")) == 1
        stats = _stats(scheduler)
        assert stats["errors"] == 1
        assert "REACTION_INVALID" in (stats["last_error"] or "").upper()


class TestRead:
    async def test_due_reads_coalesce_to_the_highest_id(self, sched):
        scheduler, runner, clock = sched
        quick = _job(scheduler, [{"read": {}}])
        await deliver(quick, private_update(10, ""))
        await deliver(quick, private_update(12, ""))
        slow = _job(scheduler, [{"read": {"delay": "100s"}}])
        await deliver(slow, private_update(15, ""))
        await clock.advance(1)
        assert runner.requests("message.read") == [{"chat": str(ALICE), "up_to": 12}]
        await clock.advance(100)
        assert runner.requests("message.read")[-1] == {"chat": str(ALICE), "up_to": 15}

    def test_reading_time_grows_with_the_text_and_is_capped(self):
        from tlgr.actions.base import MessageFacts

        short = MessageFacts(chat_id=1, msg_id=1, text="hi")
        long = MessageFacts(chat_id=1, msg_id=1, text="word " * 250)
        huge = MessageFacts(chat_id=1, msg_id=1, text="word " * 5000)
        media = MessageFacts(chat_id=1, msg_id=1, media="photo")
        assert reading_seconds(short) < 1
        assert reading_seconds(long) == pytest.approx(60.0)
        assert reading_seconds(huge) == 60.0
        assert reading_seconds(media) == 3.0

    async def test_mentions_and_reactions_flags(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {"mentions": True, "reactions": True}}])
        await deliver(job, private_update(10, ""))
        await clock.advance(1)
        assert runner.requests("message.read")[0] == {
            "chat": str(ALICE),
            "up_to": 10,
            "mentions": True,
            "reactions": True,
        }


class TestView:
    async def test_a_channel_post_is_counted(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"view": {}}])
        await deliver(job, channel_update(40))
        await deliver(job, channel_update(41))
        await clock.advance(1)
        assert runner.requests("message.view.get") == [
            {"chat": str(CHANNEL_ID), "msg_id": ["40", "41"], "increment": True}
        ]

    async def test_a_voice_note_is_listened_to(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"view": True}])
        await deliver(job, private_update(10, "", media=voice_media()))
        await deliver(job, private_update(11, "", media=voice_media(round_video=True)))
        await clock.advance(1)
        assert runner.requests("message.read") == [{"chat": str(ALICE), "contents": ["10", "11"]}]

    async def test_view_once_media_is_left_alone_unless_enabled(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"view": {}}])
        await deliver(job, private_update(10, "", media=voice_media(ttl=2147483647)))
        await deliver(job, private_update(11, "just text"))
        await clock.advance(1)
        assert runner.calls == []
        assert _stats(scheduler)["skipped"] == 2

        brave = _job(scheduler, [{"view": {"include_view_once": True}}])
        await deliver(brave, private_update(12, "", media=voice_media(ttl=2147483647)))
        await clock.advance(1)
        assert runner.requests("message.read") == [{"chat": str(ALICE), "contents": ["12"]}]


class TestReply:
    async def test_typing_comes_first_and_lasts_as_long_as_the_text(self, sched):
        scheduler, runner, clock = sched
        text = "x" * 200
        job = _job(scheduler, [{"reply": text}])
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["chat.typing"]
        assert runner.requests("chat.typing")[0]["duration"] == typing_seconds(text) == 5.0
        await clock.advance(4.5)
        assert runner.ops() == ["chat.typing", "message.send"]
        assert runner.requests("message.send")[0] == {
            "chat": str(ALICE),
            "text": text,
            "reply_to": 10,
            "parse": "md",
        }

    def test_typing_is_clamped(self):
        assert typing_seconds("hi") == 2.0
        assert typing_seconds("x" * 10_000) == 15.0

    async def test_typing_can_be_turned_off(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"reply": {"text": "hey", "typing": False}}])
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["message.send"]

    async def test_no_typing_in_a_broadcast_channel(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"reply": "noted"}])
        await deliver(job, channel_update(40))
        await clock.advance(1)
        assert runner.ops() == ["message.send"]


class TestPresence:
    async def test_blip_goes_online_then_offline_after_the_action(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {}}], presence="blip")
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["profile.presence.set", "message.read"]
        assert runner.requests("profile.presence.set")[0] == {"state": "online"}
        await clock.advance(6)
        assert runner.requests("profile.presence.set")[-1] == {"state": "offline"}

    async def test_overlapping_blips_merge(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {}}], presence="blip")
        await deliver(job, private_update(10, user=ALICE))
        await deliver(job, private_update(11, user=ALICE + 1))
        await clock.run_for(15, step=0.5)
        states = [r["state"] for r in runner.requests("profile.presence.set")]
        assert states == ["online", "offline"]

    async def test_session_stays_online_while_more_is_due(self, sched):
        scheduler, runner, clock = sched
        job = _job(
            scheduler,
            [{"read": {}}, {"react": {"emoji": "👍", "delay": "8s"}}],
            presence="session",
        )
        await deliver(job, private_update(10))
        await clock.run_for(30, step=0.5)
        states = [r["state"] for r in runner.requests("profile.presence.set")]
        assert states == ["online", "offline"]
        offline_at = runner.times("profile.presence.set")[-1]
        assert offline_at > runner.times("reaction.add")[0]

    async def test_leave_never_touches_presence(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {}}])
        await deliver(job, private_update(10))
        await clock.advance(10)
        assert "profile.presence.set" not in runner.ops()

    async def test_quiet_hours_hold_until_the_window_ends(self):
        clock = FakeClock(start=datetime(2026, 1, 1, 2, 0).timestamp())
        scheduler, runner, clock = make_scheduler(clock=clock)
        scheduler.start()
        try:
            job = _job(
                scheduler,
                [{"react": "👍"}, {"forward": {"to": "@archive"}}],
                presence={"mode": "leave", "quiet_hours": "01:00-08:00"},
                on_takeover="ignore",
            )
            await deliver(job, private_update(10))
            await clock.advance(5)
            assert runner.ops() == ["message.forward"]
            await clock.run_for(6 * 3600 - 60, step=600)
            assert "reaction.add" not in runner.ops()
            await clock.run_for(3600, step=30)
            assert "reaction.add" in runner.ops()
            released = runner.times("reaction.add")[0]
            assert datetime.fromtimestamp(released).hour == 8
        finally:
            await scheduler.stop(timeout=0.1)


class TestTakeover:
    async def _queued(self, scheduler, on_takeover):
        job = _job(
            scheduler,
            [
                {"read": {"delay": "60s"}},
                {"view": {"delay": "60s"}},
                {"react": {"emoji": "👍", "delay": "60s"}},
                {"reply": {"text": "hi", "delay": "60s"}},
                {"forward": {"to": "@archive", "delay": "60s"}},
            ],
            on_takeover=on_takeover,
        )
        await deliver(job, private_update(10, "", media=voice_media()))
        return {item.action for item in scheduler.items.values()}

    async def test_cancel_drops_everything_but_the_forward(self, sched):
        scheduler, runner, clock = sched
        await self._queued(scheduler, "cancel")
        update = types.UpdateReadHistoryInbox(
            peer=types.PeerUser(user_id=ALICE),
            max_id=10,
            still_unread_count=0,
            pts=2,
            pts_count=1,
        )
        await scheduler.on_bus(bus_envelope(), update)
        assert {item.action for item in scheduler.items.values()} == {"forward"}
        assert _stats(scheduler, 2)["superseded"] == 1

    async def test_cancel_read_drops_only_read_and_view(self, sched):
        scheduler, runner, clock = sched
        await self._queued(scheduler, "cancel_read")
        await scheduler.on_bus(bus_envelope(), private_update(30, "on it", out=True))
        assert {item.action for item in scheduler.items.values()} == {
            "react",
            "reply",
            "forward",
        }

    async def test_ignore_keeps_everything(self, sched):
        scheduler, runner, clock = sched
        before = await self._queued(scheduler, "ignore")
        await scheduler.on_bus(bus_envelope(), private_update(30, "on it", out=True))
        assert {item.action for item in scheduler.items.values()} == before

    async def test_tlgrs_own_read_is_not_a_takeover(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {}}, {"reply": {"text": "hi", "delay": "60s"}}])
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert runner.ops() == ["message.read"]
        echo = types.UpdateReadHistoryInbox(
            peer=types.PeerUser(user_id=ALICE),
            max_id=10,
            still_unread_count=0,
            pts=2,
            pts_count=1,
        )
        await scheduler.on_bus(bus_envelope(), echo)
        assert [item.action for item in scheduler.items.values()] == ["reply"]

    async def test_a_message_after_the_read_point_survives(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"reply": {"text": "hi", "delay": "60s"}}])
        await deliver(job, private_update(10))
        await deliver(job, private_update(20))
        update = types.UpdateReadHistoryInbox(
            peer=types.PeerUser(user_id=ALICE),
            max_id=15,
            still_unread_count=1,
            pts=2,
            pts_count=1,
        )
        await scheduler.on_bus(bus_envelope(), update)
        assert [item.msg_id for item in scheduler.items.values()] == [20]


class TestFailures:
    async def test_flood_wait_reschedules_and_slows_the_queue(self, sched):
        scheduler, runner, clock = sched
        runner.fail("message.forward", errors.FloodWaitError(request=None, capture=30))
        job = _job(scheduler, [{"forward": {"to": "@archive"}}])
        await deliver(job, private_update(10))
        await clock.advance(1)
        assert len(runner.requests("message.forward")) == 1
        await clock.advance(20)
        assert len(runner.requests("message.forward")) == 1
        await clock.advance(20)
        assert len(runner.requests("message.forward")) == 2
        assert _stats(scheduler)["done"] == 1
        assert scheduler.pacers["forward"].slowdown == 2.0

    async def test_a_transient_failure_is_retried_with_backoff(self, sched):
        scheduler, runner, clock = sched
        runner.fail("message.forward", ConnectionError("reset"), ConnectionError("reset"))
        job = _job(scheduler, [{"forward": {"to": "@archive"}}])
        await deliver(job, private_update(10))
        await clock.run_for(120, step=1)
        assert len(runner.requests("message.forward")) == 3
        assert _stats(scheduler)["done"] == 1

    async def test_a_permanent_failure_is_counted(self, sched):
        scheduler, runner, clock = sched
        runner.fail("message.forward", errors.ChatWriteForbiddenError(request=None))
        job = _job(scheduler, [{"forward": {"to": "@archive"}}])
        await deliver(job, private_update(10))
        await clock.run_for(60, step=1)
        assert len(runner.requests("message.forward")) == 1
        assert _stats(scheduler)["errors"] == 1


class TestDryRun:
    async def test_dry_run_schedules_counts_and_never_calls_telegram(self, sched, caplog):
        caplog.set_level(logging.INFO, logger="tlgr.gateway")
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"react": "👍"}, {"reply": "hi"}], dry_run=True)
        await deliver(job, private_update(10))
        await clock.advance(5)
        assert runner.calls == []
        assert _stats(scheduler, 0)["done"] == 1
        assert _stats(scheduler, 1)["done"] == 1
        assert "dry run: would read" in caplog.text


class TestCountersAndQueue:
    async def test_counters_and_cancel(self, sched):
        scheduler, runner, clock = sched
        job = _job(scheduler, [{"read": {"delay": "60s"}}])
        await deliver(job, private_update(10, user=ALICE))
        await deliver(job, private_update(11, user=ALICE + 1))
        assert _stats(scheduler)["pending"] == 2
        cancelled = scheduler.cancel(chat_id=ALICE)
        assert [item.msg_id for item in cancelled] == [10]
        assert _stats(scheduler)["superseded"] == 1
        assert _stats(scheduler)["pending"] == 1
        assert scheduler.cancel() == []
        assert len(scheduler.cancel(everything=True)) == 1

    async def test_stop_waits_for_nothing_when_idle(self, sched):
        scheduler, runner, clock = sched
        await asyncio.wait_for(scheduler.stop(timeout=0.1), timeout=2)
