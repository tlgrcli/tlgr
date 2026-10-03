"""End-to-end tests for the Gateway pipeline: filters, processors, actions.

The job is fed real TL updates and its actions land on a scheduler with a
virtual clock and a recording op runner, so each test reads what would have
reached the op layer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_telethon import FakeTelegramClient, World
from job_helpers import ALICE, deliver, group_update, make_scheduler, private_update

from tlgr.actions import register_action
from tlgr.filters.compose import parse_filter_config
from tlgr.gateway.config import ActionConfig, GatewayConfig
from tlgr.gateway.engine import Gateway
from tlgr.processors import ProcessorChain


def _client():
    return SimpleNamespace(client=FakeTelegramClient(World()), resolve_chat=AsyncMock())


def _reply(text, **kwargs):
    return ActionConfig(name="reply", config={"text": text, "typing": False}, **kwargs)


@pytest.fixture
async def sched():
    scheduler, runner, clock = make_scheduler()
    scheduler.start()
    try:
        yield scheduler, runner, clock
    finally:
        await scheduler.stop(timeout=0.1)


class TestGatewayPipeline:
    async def test_filter_match_triggers_action(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(
            name="test-reply",
            account="work",
            filters=parse_filter_config({"chat_type": "private"}),
            actions=[_reply("hello!")],
        )
        gw = Gateway(config, _client(), scheduler=scheduler)
        await gw.setup()

        await deliver(gw, private_update(10))
        await clock.advance(1)

        assert runner.requests("message.send") == [
            {"chat": str(ALICE), "text": "hello!", "reply_to": 10, "parse": "md"}
        ]
        assert gw._stats["matched"] == 1

    async def test_filter_mismatch_skips(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(
            name="test-skip",
            account="work",
            filters=parse_filter_config({"chat_type": "private"}),
            actions=[_reply("hello!")],
        )
        gw = Gateway(config, _client(), scheduler=scheduler)
        await gw.setup()

        await deliver(gw, group_update(10))
        await clock.advance(1)

        assert runner.calls == []
        assert gw._stats["skipped"] == 1

    async def test_no_filters_matches_all(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(name="test-all", account="work", actions=[_reply("yo")])
        gw = Gateway(config, _client(), scheduler=scheduler)

        await deliver(gw, group_update(10))
        await clock.advance(1)

        assert runner.requests("message.send")[0]["text"] == "yo"

    async def test_multiple_actions(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(
            name="test-multi",
            account="work",
            actions=[_reply("got it!"), _reply("second reply")],
        )
        gw = Gateway(config, _client(), scheduler=scheduler)

        await deliver(gw, private_update(10))
        await clock.run_for(5)

        assert [r["text"] for r in runner.requests("message.send")] == ["got it!", "second reply"]

    async def test_per_action_filter(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(
            name="test-per-action",
            account="work",
            actions=[
                _reply("private only", filters=parse_filter_config({"chat_type": "private"})),
                _reply("always"),
            ],
        )
        gw = Gateway(config, _client(), scheduler=scheduler)

        await deliver(gw, group_update(10))
        await clock.advance(1)

        assert [r["text"] for r in runner.requests("message.send")] == ["always"]

    async def test_job_level_processors(self, sched):
        scheduler, runner, clock = sched
        chain = ProcessorChain().add("add_prefix", {"prefix": "[BOT]"})
        config = GatewayConfig(
            name="test-proc", account="work", processors=chain, actions=[_reply("hello")]
        )
        gw = Gateway(config, _client(), scheduler=scheduler)

        await deliver(gw, private_update(10))
        await clock.advance(1)

        assert "[BOT]" in runner.requests("message.send")[0]["text"]

    async def test_unknown_action_logs_error(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(
            name="test-unknown",
            account="work",
            actions=[ActionConfig(name="nonexistent_action", config="x")],
        )
        gw = Gateway(config, _client(), scheduler=scheduler)

        await deliver(gw, private_update(10))

        assert gw._stats["errors"] == 1

    async def test_a_function_action_still_runs_at_once(self, sched):
        scheduler, runner, clock = sched
        seen = []

        @register_action("_test_legacy")
        async def legacy(event, config, client, chain=None):
            seen.append((event.raw.message.id, config))

        config = GatewayConfig(
            name="test-legacy",
            account="work",
            actions=[ActionConfig(name="_test_legacy", config="cfg")],
        )
        gw = Gateway(config, _client(), scheduler=scheduler)
        await deliver(gw, private_update(10))
        assert seen == [(10, "cfg")]

    async def test_status_reports_per_action_counters(self, sched):
        scheduler, runner, clock = sched
        config = GatewayConfig(name="test-status", account="work", actions=[_reply("hi")])
        gw = Gateway(config, _client(), scheduler=scheduler)
        await deliver(gw, private_update(10))
        status = gw.status()
        assert status["matched"] == 1
        assert status["actions"][0]["pending"] == 1
        await clock.advance(1)
        status = gw.status()
        assert status["actions"][0]["done"] == 1
        assert status["actions"][0]["pending"] == 0

    async def test_without_a_scheduler_an_action_is_an_error_not_a_crash(self):
        config = GatewayConfig(name="test-none", account="work", actions=[_reply("hi")])
        gw = Gateway(config, _client())
        await deliver(gw, private_update(10))
        assert gw._stats["errors"] == 1
