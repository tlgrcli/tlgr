"""Service messages must be distinguishable from a real empty-text message.

Telegram renders "you added X to your contacts", "X joined Telegram", pinned
messages and friends as MessageService objects with no text. They used to
serialize as {"text": "", "out": true} — identical in shape to an outgoing
line somebody actually typed, which is exactly what tlgr-agent's playbook
reads as "the user took this chat over manually".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tlgr.core.client import ClientWrapper


class _Action(SimpleNamespace):
    pass


class MessageActionContactSignUp(_Action):
    pass


def _msg(mid, text, *, out=False, action=None):
    return SimpleNamespace(
        id=mid,
        date="2026-08-31",
        text=text,
        out=out,
        action=action,
        reply_to_msg_id=None,
        sender=None,
        sender_id=None,
        media=None,
        entities=None,
        reactions=None,
        reply_to=None,
        forward=None,
    )


class _FakeTelethon:
    def __init__(self, msgs):
        self._msgs = msgs

    def iter_messages(self, chat_id, limit=20, offset_id=0, **kw):
        msgs = self._msgs[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    async def get_messages(self, chat_id, ids=None):
        return [m for m in self._msgs if m.id in (ids or [])]


def _wrap(msgs):
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FakeTelethon(msgs)
    return w


def test_service_message_is_labelled():
    w = _wrap(
        [_msg(2, "", out=True, action=MessageActionContactSignUp()), _msg(1, "سلام", out=True)]
    )
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["service"] == "MessageActionContactSignUp"
    assert out[0]["text"] == ""


def test_ordinary_message_has_no_service_key():
    w = _wrap([_msg(1, "سلام", out=True)])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert "service" not in out[0]


def test_empty_text_without_action_is_not_service():
    """A media-only message has empty text but is NOT a service message."""
    w = _wrap([_msg(1, "", out=True)])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert "service" not in out[0]


def test_get_message_single_also_labels_service():
    w = _wrap([_msg(2, "", out=True, action=MessageActionContactSignUp())])
    out = asyncio.run(w.get_message(7, 2))
    assert out["service"] == "MessageActionContactSignUp"
