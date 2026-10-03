"""A gateway job fed from the daemon's event bus, with real TL updates.

The bus hands a job the raw TL `Update*` beside the envelope, while every
filter reads a high-level Telethon event (`event.chat_id`, `event.is_private`).
These tests drive a job the way the daemon does, and its actions run as real
operations through the daemon's dispatcher against the fake client, so a
shape mismatch anywhere between the update and the request fails here instead
of in production, where it showed up as `matched=0` for days.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_telethon import make_channel, make_user
from job_helpers import ALICE, daemon_job_env, photo_media, until
from telethon.tl import types

from tlgr.gateway.config import _parse_job
from tlgr.gateway.engine import Gateway

SOURCE = 1234
SOURCE_MARKED = -1000000001234
ARCHIVE = 9999
ARCHIVE_MARKED = -1000000009999


class _Bus:
    def __init__(self) -> None:
        self.handlers: list = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    def remove_handler(self, handler) -> None:
        self.handlers.remove(handler)

    async def deliver(self, update, *, event_type: str = "message_new", account: str = "work"):
        envelope = SimpleNamespace(type=event_type, account=account)
        for handler in list(self.handlers):
            await handler(envelope, update)


def _channel_post(text: str = "breaking", *, channel: int = SOURCE, msg_id: int = 5, media=None):
    message = types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(channel_id=channel),
        date=None,
        message=text,
        post=True,
        media=media,
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)


def _private(text: str = "hi", *, out: bool = False, msg_id: int = 6):
    message = types.Message(
        id=msg_id,
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
async def env(daemon, world):
    world.add_user(make_user(ALICE, username="alice"))
    world.add_channel(make_channel(SOURCE, title="Source"))
    world.add_channel(make_channel(ARCHIVE, title="Archive"))
    world.add_message(SOURCE_MARKED, "breaking", message_id=5)
    world.add_message(ALICE, "hi", message_id=6, sender_id=ALICE)
    env = await daemon_job_env(daemon)
    env.client = SimpleNamespace(
        client=env.client.client,
        resolve_chat=AsyncMock(
            side_effect=lambda ref: {"@source": SOURCE_MARKED, "@archive": ARCHIVE_MARKED}[ref]
        ),
    )
    return env


def _forward_job(**action):
    return _parse_job(
        {
            "name": "archive",
            "account": "work",
            "filters": {"chat_id": "@source"},
            "actions": [{"forward": {"to": [str(ARCHIVE_MARKED)], **action}}],
        }
    )


class TestForwardFromTheBus:
    async def test_a_post_in_the_watched_channel_is_forwarded(self, bus, env, world):
        job = Gateway(_forward_job(), env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post())
            await until(lambda: world.called("ForwardMessagesRequest"), clock=env.clock)
        finally:
            await job.stop()

        assert job._stats == {"matched": 1, "skipped": 0, "errors": 0}
        sent = world.called("ForwardMessagesRequest")[0]
        assert list(sent.id) == [5]
        assert sent.drop_author is None
        assert env.scheduler.job_stats("archive")[0]["done"] == 1

    async def test_drop_author_reaches_the_request(self, bus, env, world):
        job = Gateway(_forward_job(drop_author=True), env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post())
            await until(lambda: world.called("ForwardMessagesRequest"), clock=env.clock)
        finally:
            await job.stop()
        assert world.called("ForwardMessagesRequest")[0].drop_author is True

    async def test_a_post_elsewhere_is_skipped(self, bus, env, world):
        job = Gateway(_forward_job(), env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post(channel=777))
            await env.clock.advance(5)
        finally:
            await job.stop()

        assert job._stats["matched"] == 0
        assert job._stats["skipped"] == 1
        assert not world.called("ForwardMessagesRequest")

    async def test_the_processed_copy_is_sent_with_the_rewritten_text(self, bus, env, world):
        config = _forward_job(
            processors=[
                {"type": "regex", "pattern": "wbnet", "replacement": "VPEON", "flags": "i"}
            ],
            drop_author=True,
        )
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post("join WBNET today"))
            await until(lambda: world.called("SendMessageRequest"), clock=env.clock)
        finally:
            await job.stop()

        sent = world.called("SendMessageRequest")[0]
        assert sent.message == "join VPEON today"
        assert not world.called("ForwardMessagesRequest")
        assert world.history(ARCHIVE_MARKED)[-1].message == "join VPEON today"

    async def test_processed_media_is_re_sent_with_the_rewritten_caption(self, bus, env, world):
        world.add_media_message(SOURCE_MARKED, message_id=8, text="wbnet photo")
        config = _forward_job(
            processors=[{"type": "regex", "pattern": "wbnet", "replacement": "VPEON"}]
        )
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post("wbnet photo", msg_id=8, media=photo_media()))
            await until(lambda: world.called("SendMediaRequest"), clock=env.clock)
        finally:
            await job.stop()

        sent = world.called("SendMediaRequest")[0]
        assert sent.message == "VPEON photo"
        assert type(sent.media).__name__ == "InputMediaPhoto"

    async def test_another_accounts_post_is_ignored(self, bus, env, world):
        job = Gateway(_forward_job(), env.client, bus=bus, scheduler=env.scheduler)
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
                "account": "work",
                "filters": {"chat_type": "private"},
                "actions": [{"reply": "away"}],
            }
        )

    async def test_an_incoming_private_message_is_answered(self, bus, env, world, config):
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_private())
            await until(lambda: world.called("SendMessageRequest"), clock=env.clock)
        finally:
            await job.stop()

        assert job._stats["matched"] == 1
        sent = world.called("SendMessageRequest")[0]
        assert sent.message == "away"
        assert sent.reply_to.reply_to_msg_id == 6
        # "typing..." first, by default.
        assert world.called("SetTypingRequest")

    async def test_the_accounts_own_message_is_not_answered(self, bus, env, world, config):
        """`new_message` means incoming, as it did when jobs used `NewMessage(incoming=True)`."""
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_private(out=True))
            await env.clock.advance(20)
        finally:
            await job.stop()

        assert job._stats == {"matched": 0, "skipped": 0, "errors": 0}
        assert not world.called("SendMessageRequest")


class TestReactReadViewEndToEnd:
    async def test_react_reads_first_then_replaces_the_reaction(self, bus, env, world):
        config = _parse_job({"name": "ack", "account": "work", "actions": [{"react": "👍"}]})
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_private())
            await until(lambda: world.called("SendReactionRequest"), clock=env.clock)
        finally:
            await job.stop()
        names = [name for name, _ in world.calls]
        assert names.index("ReadHistoryRequest") < names.index("SendReactionRequest")
        assert world.read_inbox[ALICE] == 6
        reaction = world.called("SendReactionRequest")[0]
        assert [r.emoticon for r in reaction.reaction] == ["👍"]

    async def test_the_read_rpc_follows_the_peer(self, bus, env, world):
        config = _parse_job({"name": "reader", "account": "work", "actions": [{"read": {}}]})
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_private())
            await bus.deliver(_channel_post())
            await until(lambda: len(world.called("ReadHistoryRequest")) == 2, clock=env.clock)
        finally:
            await job.stop()
        private, channel = world.called("ReadHistoryRequest")
        assert type(private).__module__.endswith("messages")
        assert private.max_id == 6
        assert type(channel).__module__.endswith("channels")
        assert channel.max_id == 5

    async def test_a_forum_topic_is_read_with_read_discussion(self, bus, env, world):
        world.add_channel(make_channel(4444, title="Forum", megagroup=True))
        config = _parse_job({"name": "reader", "account": "work", "actions": [{"read": {}}]})
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        message = types.Message(
            id=70,
            peer_id=types.PeerChannel(channel_id=4444),
            from_id=types.PeerUser(user_id=ALICE),
            date=None,
            message="topic chatter",
            reply_to=types.MessageReplyHeader(reply_to_msg_id=60, forum_topic=True),
        )
        await _running(job, bus)
        try:
            await bus.deliver(types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1))
            await until(lambda: world.called("ReadDiscussionRequest"), clock=env.clock)
        finally:
            await job.stop()
        sent = world.called("ReadDiscussionRequest")[0]
        assert (sent.msg_id, sent.read_max_id) == (60, 70)

    async def test_a_channel_post_view_is_incremented(self, bus, env, world):
        config = _parse_job({"name": "viewer", "account": "work", "actions": [{"view": {}}]})
        job = Gateway(config, env.client, bus=bus, scheduler=env.scheduler)
        await _running(job, bus)
        try:
            await bus.deliver(_channel_post())
            await until(lambda: world.called("GetMessagesViewsRequest"), clock=env.clock)
        finally:
            await job.stop()
        sent = world.called("GetMessagesViewsRequest")[0]
        assert sent.increment is True
        assert list(sent.id) == [5]
