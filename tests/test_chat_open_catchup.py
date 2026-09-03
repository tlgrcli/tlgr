"""Tests for the humanlike exploration primitives: open_chat and catchup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import User

from tlgr.core.client import ClientWrapper


class _FakeMsg(SimpleNamespace):
    pass


class _FakeTelethon:
    def __init__(self, dialogs, messages_by_chat):
        self._dialogs = dialogs
        self._messages = messages_by_chat
        self.read_acks: list = []

    async def iter_dialogs(self):
        for d in self._dialogs:
            yield d

    def iter_messages(self, chat_id, limit=20, offset_id=0, **kw):
        msgs = self._messages.get(chat_id, [])[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    async def send_read_acknowledge(self, chat_id, **kw):
        self.read_acks.append(chat_id)


def _msg(mid, text, out=False):
    return _FakeMsg(
        id=mid,
        date="2026-08-31",
        text=text,
        out=out,
        reply_to_msg_id=None,
        sender=None,
        sender_id=None,
        media=None,
        entities=None,
    )


def _make(unreads):
    """unreads: {chat_id: unread_count}; every chat gets 6 messages."""
    dialogs, messages = [], {}
    for cid, unread in unreads.items():
        user = User(id=cid, first_name=f"u{cid}", bot=False)
        dialogs.append(SimpleNamespace(id=cid, entity=user, unread_count=unread, message=None))
        messages[cid] = [_msg(i, f"m{i}") for i in range(6, 0, -1)]
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FakeTelethon(dialogs, messages)
    return w


def test_open_chat_marks_read_by_default():
    w = _make({7: 0})
    r = asyncio.run(w.open_chat(7, limit=3))
    assert r["marked_read"] is True
    assert w._client.read_acks == [7]
    assert len(r["messages"]) == 3


def test_open_chat_silent_peek():
    w = _make({7: 0})
    r = asyncio.run(w.open_chat(7, limit=3, mark_read=False))
    assert r["marked_read"] is False
    assert w._client.read_acks == []


def test_catchup_returns_unread_chats_with_messages():
    w = _make({1: 3, 2: 0, 3: 1})
    chats = asyncio.run(w.catchup(per_chat=10))
    ids = {c["id"] for c in chats}
    assert ids == {1, 3}  # only unread chats
    by_id = {c["id"]: c for c in chats}
    assert len(by_id[1]["messages"]) == 5  # unread+2, floor 5
    assert len(by_id[3]["messages"]) == 5
    assert w._client.read_acks == []  # catchup never emits receipts


def test_catchup_caps_depth_at_per_chat():
    w = _make({1: 50})
    chats = asyncio.run(w.catchup(per_chat=4))
    assert len(chats[0]["messages"]) == 4
