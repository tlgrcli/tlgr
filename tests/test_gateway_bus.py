"""A gateway job fed from the daemon's event bus, with real TL updates.

The bus hands a job the raw TL `Update*` beside the envelope, while every
filter and action reads a high-level Telethon event (`event.chat_id`,
`event.is_private`, `event.reply()`). These tests drive a job the way the
daemon does, so a shape mismatch fails here instead of in production, where
it showed up as `matched=0` for days.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_telethon import FakeTelegramClient, World
from telethon.tl import types

from tlgr.gateway.config import _parse_job
from tlgr.gateway.engine import Gateway

SOURCE = 1234
SOURCE_MARKED = -1000000001234
ALICE = 4242


class _Bus:
    def __init__(self) -> None:
        self.handlers: list = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    def remove_handler(self, handler) -> None:
        self.handlers.remove(handler)

    async def deliver(self, update, *, event_type: str = "message_new", account: str = "Neo"):
        envelope = SimpleNamespace(type=event_type, account=account)
        for handler in list(self.handlers):
            await handler(envelope, update)


class _JobClient:
    def __init__(self, world: World, chats: dict[str, int]) -> None:
        self.client = FakeTelegramClient(world)
        self.client.forward_messages = AsyncMock()
        self.client.send_message = AsyncMock()
        self.resolve_chat = AsyncMock(side_effect=lambda ref: chats[ref])


def _channel_post(text: str = "breaking", *, channel: int = SOURCE, out: bool = False):
    message = types.Message(
        id=5, peer_id=types.PeerChannel(channel_id=channel), date=None, message=text, out=out
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)


def _private(text: str = "hi", *, out: bool = False):
    message = types.Message(
        id=6,
        peer_id=types.PeerUser(user_id=ALICE),
        from_id=None if out else types.PeerUser(user_id=ALICE),
        date=None,
        message=text,
        out=out,
    )
    return types.UpdateNewMessage(message=message, pts=1, pts_count=1)


async def _running(job: Gateway, bus: _Bus) -> None:
    job.start()
    for _ in range(100):
        if bus.handlers:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the job never subscribed to the bus")


@pytest.fixture
def bus() -> _Bus:
    return _Bus()


@pytest.fixture
async def client() -> _JobClient:
    return _JobClient(World(), {"@source": SOURCE_MARKED, "@archive": -1000000009999})


class TestForwardFromTheBus:
    async def test_a_post_in_the_watched_channel_is_forwarded(self, bus, client):
        config = _parse_job(
            {
                "name": "archive",
                "account": "Neo",
                "filters": {"chat_id": "@source"},
                "actions": [{"forward": {"to": ["@archive"]}}],
            }
        )
        job = Gateway(config, client, bus=bus)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post())
        finally:
            await job.stop()

        assert job._stats == {"matched": 1, "skipped": 0, "errors": 0}
        client.client.forward_messages.assert_awaited_once()
        assert client.client.forward_messages.await_args.args[0] == -1000000009999

    async def test_a_post_elsewhere_is_skipped(self, bus, client):
        config = _parse_job(
            {
                "name": "archive",
                "account": "Neo",
                "filters": {"chat_id": "@source"},
                "actions": [{"forward": {"to": ["@archive"]}}],
            }
        )
        job = Gateway(config, client, bus=bus)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post(channel=777))
        finally:
            await job.stop()

        assert job._stats["matched"] == 0
        assert job._stats["skipped"] == 1
        client.client.forward_messages.assert_not_awaited()

    async def test_the_processed_copy_is_sent_with_the_rewritten_text(self, bus, client):
        config = _parse_job(
            {
                "name": "rename",
                "account": "Neo",
                "filters": {"chat_id": "@source"},
                "actions": [
                    {
                        "forward": {
                            "to": ["@archive"],
                            "processors": [
                                {"type": "regex", "pattern": "wbnet", "replacement": "VPEON"}
                            ],
                        }
                    }
                ],
            }
        )
        job = Gateway(config, client, bus=bus)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post("join wbnet today"))
        finally:
            await job.stop()

        client.client.send_message.assert_awaited_once_with(-1000000009999, "join VPEON today")

    async def test_another_accounts_post_is_ignored(self, bus, client):
        config = _parse_job(
            {
                "name": "archive",
                "account": "Neo",
                "filters": {"chat_id": "@source"},
                "actions": [{"forward": {"to": ["@archive"]}}],
            }
        )
        job = Gateway(config, client, bus=bus)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post(), account="other")
        finally:
            await job.stop()

        assert job._stats == {"matched": 0, "skipped": 0, "errors": 0}


class TestReplyFromTheBus:
    @pytest.fixture
    def config(self):
        return _parse_job(
            {
                "name": "away",
                "account": "Neo",
                "filters": {"chat_type": "private"},
                "actions": [{"reply": "away"}],
            }
        )

    async def test_an_incoming_private_message_is_answered(self, bus, client, config):
        client.client.send_message = AsyncMock()
        job = Gateway(config, client, bus=bus)
        await _running(job, bus)
        try:
            await bus.deliver(_private())
        finally:
            await job.stop()

        assert job._stats["matched"] == 1
        client.client.send_message.assert_awaited_once()

    async def test_the_accounts_own_message_is_not_answered(self, bus, client, config):
        """`new_message` means incoming, as it did when jobs used `NewMessage(incoming=True)`."""
        job = Gateway(config, client, bus=bus)
        await _running(job, bus)
        try:
            await bus.deliver(_private(out=True))
        finally:
            await job.stop()

        assert job._stats == {"matched": 0, "skipped": 0, "errors": 0}
        client.client.send_message.assert_not_awaited()


class TestChatRefResolution:
    async def test_numeric_refs_are_left_alone(self, bus, client):
        config = _parse_job(
            {
                "name": "ids",
                "account": "Neo",
                "filters": {"chat_id": [SOURCE_MARKED, "@source"]},
                "actions": [{"forward": {"to": ["@archive"]}}],
            }
        )
        job = Gateway(config, client, bus=bus)
        await job.setup()

        assert config.filters.filter_value == [SOURCE_MARKED, SOURCE_MARKED]
        client.resolve_chat.assert_awaited_once_with("@source")

    async def test_an_unresolvable_ref_is_logged_and_kept(self, bus, client, caplog):
        client.resolve_chat = AsyncMock(side_effect=ValueError("no such chat"))
        config = _parse_job(
            {
                "name": "typo",
                "account": "Neo",
                "filters": {"chat_id": "@nope"},
                "actions": [{"forward": {"to": ["@archive"]}}],
            }
        )
        job = Gateway(config, client, bus=bus)
        await job.setup()

        assert config.filters.filter_value == "@nope"
        assert "cannot resolve chat @nope" in caplog.text
