"""A media-only message must be distinguishable from a message with no content.

A sticker, photo, voice note or video sent with no caption serializes as
{"text": "", "out": false} — byte-identical to a message nobody typed anything
into. Every message-bearing response (message list, chat open, catchup, inbox)
therefore rendered a contact's reply as blank, and a wake reading the chat saw
silence where there was an answer. `service` (test_service_messages.py) solved
exactly this shape for Telegram's own events; `media_type` is its counterpart
for real media, and is emitted unconditionally for the same reason: the cheap
type marker has to be there for the reader who did NOT know to ask for it.

The verbose `include_media` payload is unchanged — that one is opt-in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tlgr.core.client import ClientWrapper


class MessageMediaPhoto(SimpleNamespace):
    pass


class MessageMediaDocument(SimpleNamespace):
    pass


def _msg(mid, text, *, out=False, media=None):
    return SimpleNamespace(
        id=mid, date="2026-09-02", text=text, out=out, action=None,
        reply_to_msg_id=None, sender=None, sender_id=None, media=media,
        entities=None, reactions=None, reply_to=None, forward=None,
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


def test_media_only_message_is_labelled_without_asking():
    """The caption-less sticker case, with include_media left off."""
    w = _wrap([_msg(2, "", media=MessageMediaDocument()),
               _msg(1, "سلام")])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["media_type"] == "MessageMediaDocument"
    assert out[0]["text"] == ""
    assert "service" not in out[0]


def test_truly_empty_message_has_no_media_type():
    """The distinction the label exists to make."""
    w = _wrap([_msg(1, "")])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert "media_type" not in out[0]


def test_text_message_with_media_keeps_both():
    """A captioned photo is text AND media — neither field hides the other."""
    w = _wrap([_msg(1, "اینم پروفایلم", media=MessageMediaPhoto())])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["text"] == "اینم پروفایلم"
    assert out[0]["media_type"] == "MessageMediaPhoto"


def test_include_media_payload_still_works_alongside():
    w = _wrap([_msg(1, "", media=MessageMediaPhoto())])
    out = asyncio.run(w.get_messages(7, limit=10, include_media=True))
    assert out[0]["media_type"] == "MessageMediaPhoto"
    assert out[0]["media"]["type"] == "MessageMediaPhoto"


def test_get_message_single_also_labels_media():
    w = _wrap([_msg(2, "", media=MessageMediaDocument())])
    out = asyncio.run(w.get_message(7, 2))
    assert out["media_type"] == "MessageMediaDocument"
