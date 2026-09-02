"""End-to-end tests for the Gateway pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tlgr.filters.compose import parse_filter_config
from tlgr.gateway.config import ActionConfig, GatewayConfig
from tlgr.gateway.engine import Gateway
from tlgr.processors import ProcessorChain


def _make_tg_event(text="hello", is_private=True, sender_id=100):
    msg = MagicMock()
    msg.text = text
    msg.message = text
    msg.sender_id = sender_id
    msg.out = False
    msg.reply_to = None
    msg.forward = None
    msg.media = None
    msg.entities = None
    msg.date = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    msg.action = None
    msg.sender = MagicMock(bot=False)

    ev = MagicMock()
    ev.message = msg
    ev.chat_id = 42
    ev.is_private = is_private
    ev.is_group = not is_private
    ev.is_channel = False
    ev.reply = AsyncMock()

    chat = MagicMock()
    chat.title = ""
    chat.megagroup = False
    ev.chat = chat

    return ev


def _make_client():
    client = MagicMock()
    client.resolve_chat = AsyncMock(return_value=999)
    client.client = MagicMock()
    client.client.forward_messages = AsyncMock()
    client.client.send_message = AsyncMock()
    client.client.send_file = AsyncMock()
    client.client.on = MagicMock(side_effect=lambda *a, **kw: lambda f: f)
    return client


class TestGatewayPipeline:
    @pytest.mark.asyncio
    async def test_filter_match_triggers_action(self):
        config = GatewayConfig(
            name="test-reply",
            account="test",
            filters=parse_filter_config({"chat_type": "private"}),
            actions=[ActionConfig(name="reply", config="hello!")],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event(is_private=True)
        await gw._handle(tg_event)

        tg_event.reply.assert_awaited_once_with("hello!")
        assert gw._stats["matched"] == 1

    @pytest.mark.asyncio
    async def test_filter_mismatch_skips(self):
        config = GatewayConfig(
            name="test-skip",
            account="test",
            filters=parse_filter_config({"chat_type": "private"}),
            actions=[ActionConfig(name="reply", config="hello!")],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event(is_private=False)
        await gw._handle(tg_event)

        tg_event.reply.assert_not_awaited()
        assert gw._stats["skipped"] == 1

    @pytest.mark.asyncio
    async def test_no_filters_matches_all(self):
        config = GatewayConfig(
            name="test-all",
            account="test",
            filters=None,
            actions=[ActionConfig(name="reply", config="yo")],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event()
        await gw._handle(tg_event)

        tg_event.reply.assert_awaited_once_with("yo")

    @pytest.mark.asyncio
    async def test_multiple_actions(self):
        config = GatewayConfig(
            name="test-multi",
            account="test",
            actions=[
                ActionConfig(name="reply", config="got it!"),
                ActionConfig(name="reply", config="second reply"),
            ],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event()
        await gw._handle(tg_event)

        assert tg_event.reply.await_count == 2

    @pytest.mark.asyncio
    async def test_per_action_filter(self):
        config = GatewayConfig(
            name="test-per-action",
            account="test",
            actions=[
                ActionConfig(
                    name="reply",
                    config="private only",
                    filters=parse_filter_config({"chat_type": "private"}),
                ),
                ActionConfig(name="reply", config="always"),
            ],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event(is_private=False)
        await gw._handle(tg_event)

        # First action should be skipped (filter mismatch), second should run
        assert tg_event.reply.await_count == 1
        assert tg_event.reply.call_args[0][0] == "always"

    @pytest.mark.asyncio
    async def test_job_level_processors(self):
        chain = ProcessorChain().add("add_prefix", {"prefix": "[BOT]"})
        config = GatewayConfig(
            name="test-proc",
            account="test",
            processors=chain,
            actions=[ActionConfig(name="reply", config="hello")],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event()
        await gw._handle(tg_event)

        call_text = tg_event.reply.call_args[0][0]
        assert "[BOT]" in call_text

    @pytest.mark.asyncio
    async def test_unknown_action_logs_error(self):
        config = GatewayConfig(
            name="test-unknown",
            account="test",
            actions=[ActionConfig(name="nonexistent_action", config="x")],
        )
        client = _make_client()
        gw = Gateway(config, client)
        await gw.setup()

        tg_event = _make_tg_event()
        await gw._handle(tg_event)

        assert gw._stats["errors"] == 1
