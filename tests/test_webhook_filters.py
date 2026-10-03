"""`[webhook.filters]` against the updates the daemon's bus really carries.

Both halves were dead. `filters.chats` was compared against a set nothing
filled, so configuring it dropped every event with a chat. Every other key
was evaluated against the raw TL update, while the filters read a
high-level Telethon event, so they raised (the bus logged "event handler
failed") or never matched. These tests use real TL updates and Telethon
builders, the way the bus delivers them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_telethon import FakeTelegramClient, World
from telethon.tl import types

from tlgr.core.config import WebhookConfig, WebhookFilterConfig
from tlgr.daemon.webhook import WebhookPusher
from tlgr.models.event import EventEnvelope

SOURCE = 1234
SOURCE_MARKED = -1000000001234
ALICE = 4242


def _channel_post(text: str = "breaking news", *, channel: int = SOURCE):
    message = types.Message(
        id=5, peer_id=types.PeerChannel(channel_id=channel), date=None, message=text
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


def _envelope(update, event_type: str = "message_new") -> EventEnvelope:
    peer = update.message.peer_id if hasattr(update, "message") else None
    if isinstance(peer, types.PeerChannel):
        chat_id = int(f"-100{peer.channel_id:010d}")
    elif isinstance(peer, types.PeerUser):
        chat_id = peer.user_id
    else:
        chat_id = None
    return EventEnvelope(
        seq=1, ts="2026-10-03T12:00:00Z", account="Neo", type=event_type, chat_id=chat_id
    )


@pytest.fixture
async def account():
    return SimpleNamespace(
        client=FakeTelegramClient(World()),
        resolve_chat=AsyncMock(return_value=SOURCE_MARKED),
    )


def _pusher(account, *, chats=(), events=("message_new",), **filters) -> tuple[WebhookPusher, list]:
    config = WebhookConfig(
        enabled=True,
        url="http://127.0.0.1:1/hook",
        events=list(events),
        filters=WebhookFilterConfig(chats=list(chats), raw=filters),
    )
    pusher = WebhookPusher(config, accounts=lambda alias: account if alias == "Neo" else None)
    sent: list = []
    pusher.enqueue = sent.append  # type: ignore[method-assign]
    return pusher, sent


async def _push(pusher: WebhookPusher, update, event_type: str = "message_new") -> None:
    await pusher.on_event(_envelope(update, event_type), update)


class TestFilterKeys:
    async def test_chat_type_lets_a_private_message_through(self, account):
        pusher, sent = _pusher(account, chat_type="private")
        await _push(pusher, _private())
        assert len(sent) == 1

    async def test_chat_type_stops_a_channel_post(self, account):
        pusher, sent = _pusher(account, chat_type="private")
        await _push(pusher, _channel_post())
        assert sent == []

    async def test_contains_reads_the_message_text(self, account):
        pusher, sent = _pusher(account, contains=["breaking"])
        await _push(pusher, _channel_post("breaking news"))
        await _push(pusher, _channel_post("weather"))
        assert len(sent) == 1

    async def test_the_accounts_own_messages_are_still_reported(self, account):
        """Unlike a job, a webhook reports both directions; `is_incoming` narrows it."""
        pusher, sent = _pusher(account, chat_type="private")
        await _push(pusher, _private(out=True))
        assert len(sent) == 1

        narrowed, narrowed_sent = _pusher(account, chat_type="private", is_incoming=True)
        await _push(narrowed, _private(out=True))
        assert narrowed_sent == []

    async def test_a_type_telethon_does_not_model_is_held_back_and_logged_once(
        self, account, caplog
    ):
        pusher, sent = _pusher(account, events=("typing",), chat_type="private")
        update = types.UpdateUserTyping(user_id=ALICE, action=types.SendMessageTypingAction())
        for _ in range(2):
            await pusher.on_event(
                EventEnvelope(seq=1, ts="t", account="Neo", type="typing", chat_id=ALICE), update
            )
        assert sent == []
        assert caplog.text.count("cannot be applied to typing events") == 1

    async def test_the_daemons_own_sends_skip_the_filter_keys(self, account):
        pusher, sent = _pusher(account, chat_type="private")
        await pusher.on_event(_envelope(_channel_post()), None)
        assert len(sent) == 1

    async def test_an_account_that_is_not_connected_delivers_nothing_filtered(self, account):
        pusher, sent = _pusher(account, chat_type="private")
        envelope = _envelope(_private())
        envelope.account = "offline"
        await pusher.on_event(envelope, _private())
        assert sent == []


class TestChats:
    async def test_a_named_chat_is_resolved_and_matched(self, account):
        pusher, sent = _pusher(account, chats=["@source"])
        await _push(pusher, _channel_post())
        await _push(pusher, _channel_post(channel=777))
        await _push(pusher, _channel_post())
        assert len(sent) == 2
        account.resolve_chat.assert_awaited_once_with("@source")

    async def test_a_numeric_chat_needs_no_account(self, account):
        pusher, sent = _pusher(account, chats=[str(SOURCE_MARKED)])
        envelope = _envelope(_channel_post())
        envelope.account = "offline"
        await pusher.on_event(envelope, _channel_post())
        assert len(sent) == 1

    async def test_an_unresolvable_chat_is_logged_and_retried_later(self, account, caplog):
        account.resolve_chat = AsyncMock(side_effect=[ValueError("offline"), SOURCE_MARKED])
        pusher, sent = _pusher(account, chats=["@source"])

        await _push(pusher, _channel_post())
        assert sent == []
        assert "webhook cannot resolve chat @source" in caplog.text

        await _push(pusher, _channel_post())  # still inside the retry interval
        assert account.resolve_chat.await_count == 1

        pusher._next_resolve = 0.0
        await _push(pusher, _channel_post())
        assert len(sent) == 1
