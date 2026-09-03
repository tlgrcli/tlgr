"""Tests for the action registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tlgr.actions import get_action, list_actions, register_action
from tlgr.gateway.event import Event
from tlgr.processors import ProcessorChain


class TestRegistry:
    def test_builtin_actions_registered(self):
        names = list_actions()
        assert "reply" in names
        assert "forward" in names

    def test_get_unknown_returns_none(self):
        assert get_action("nonexistent") is None

    def test_custom_action_registration(self):
        @register_action("_test_noop")
        async def noop(event, config, client, chain=None):
            pass

        assert get_action("_test_noop") is not None


def _make_event(text="hello"):
    msg = MagicMock()
    msg.text = text
    msg.message = text
    msg.sender_id = 100
    msg.media = None
    msg.action = None

    tg_event = MagicMock()
    tg_event.message = msg
    tg_event.reply = AsyncMock()
    tg_event.chat_id = 42

    return Event(source="telegram", raw=tg_event, account="test")


def _make_client():
    client = MagicMock()
    client.resolve_chat = AsyncMock(return_value=999)
    client.client = MagicMock()
    client.client.forward_messages = AsyncMock()
    client.client.send_message = AsyncMock()
    client.client.send_file = AsyncMock()
    return client


class TestReplyAction:
    @pytest.mark.asyncio
    async def test_reply_string(self):
        action = get_action("reply")
        ev = _make_event()
        client = _make_client()
        await action(ev, "hello!", client, None)
        ev.raw.reply.assert_awaited_once_with("hello!")

    @pytest.mark.asyncio
    async def test_reply_with_processors(self):
        action = get_action("reply")
        ev = _make_event()
        client = _make_client()
        chain = ProcessorChain().add("add_prefix", {"prefix": "[BOT]"})
        await action(ev, "hello!", client, chain)
        ev.raw.reply.assert_awaited_once()
        call_text = ev.raw.reply.call_args[0][0]
        assert "[BOT]" in call_text

    @pytest.mark.asyncio
    async def test_reply_non_telegram(self):
        action = get_action("reply")
        ev = Event(source="webhook", raw={}, account="test")
        client = _make_client()
        await action(ev, "hello!", client, None)
        # Should not crash, just log a warning


class TestForwardAction:
    @pytest.mark.asyncio
    async def test_forward_simple(self):
        action = get_action("forward")
        ev = _make_event()
        client = _make_client()
        await action(ev, {"to": "@dest"}, client, None)
        client.client.forward_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_forward_with_processors(self):
        action = get_action("forward")
        ev = _make_event(text="hello world")
        client = _make_client()
        chain = ProcessorChain().add("add_prefix", {"prefix": "[FWD]"})
        await action(ev, {"to": ["@dest"]}, client, chain)
        client.client.send_message.assert_awaited_once()
        call_text = client.client.send_message.call_args[0][1]
        assert "[FWD]" in call_text

    @pytest.mark.asyncio
    async def test_forward_string_config(self):
        action = get_action("forward")
        ev = _make_event()
        client = _make_client()
        await action(ev, "@dest", client, None)
        client.client.forward_messages.assert_awaited_once()
