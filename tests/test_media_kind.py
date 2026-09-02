"""`media_type` says WHAT CLASS; `media_kind` says WHAT IT IS.

L082 made a caption-less media message visible instead of blank, but the label
it added is `MessageMediaDocument` — one name shared by a thumbs-up sticker, a
voice note, a video note, a GIF and a PDF. When the message has no caption the
media IS the message, so for a reader deciding "did they react, or are they
talking to us?" those are opposite facts wearing one shape. The document's own
attributes already carry the answer and cost nothing to read: no download, no
extra request.

`media_kind` is emitted unconditionally for the same reason `media_type` is —
the cheap marker has to be there for the reader who did not know to ask — along
with whatever the attributes make free (a sticker's `alt` emoji, which is its
content; an audio/video `duration`; a `file_name`; the `mime_type`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tlgr.core.client import ClientWrapper, media_details


class MessageMediaPhoto(SimpleNamespace):
    pass


class MessageMediaDocument(SimpleNamespace):
    pass


class DocumentAttributeSticker(SimpleNamespace):
    pass


class DocumentAttributeAudio(SimpleNamespace):
    pass


class DocumentAttributeVideo(SimpleNamespace):
    pass


class DocumentAttributeAnimated(SimpleNamespace):
    pass


class DocumentAttributeFilename(SimpleNamespace):
    pass


def _doc(*attrs, mime=None):
    return MessageMediaDocument(document=SimpleNamespace(mime_type=mime, attributes=list(attrs)))


def _msg(mid, text, *, out=False, media=None):
    return SimpleNamespace(
        id=mid,
        date="2026-09-02",
        text=text,
        out=out,
        action=None,
        reply_to_msg_id=None,
        sender=None,
        sender_id=None,
        media=media,
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


# --- the classifier itself ------------------------------------------------


def test_sticker_carries_its_alt_emoji():
    """A 👍 sticker and a 😢 sticker are opposite replies. The alt IS the text."""
    d = media_details(_doc(DocumentAttributeSticker(alt="👍"), mime="image/webp"))
    assert d["kind"] == "sticker"
    assert d["alt"] == "👍"


def test_voice_note_is_not_a_sticker():
    """Someone talking to us, which no wake should mistake for a reaction."""
    d = media_details(_doc(DocumentAttributeAudio(voice=True, duration=7), mime="audio/ogg"))
    assert d["kind"] == "voice"
    assert d["duration"] == 7


def test_music_file_is_audio_not_voice():
    d = media_details(_doc(DocumentAttributeAudio(voice=False, duration=210), mime="audio/mpeg"))
    assert d["kind"] == "audio"


def test_round_video_message_is_a_video_note():
    d = media_details(_doc(DocumentAttributeVideo(round_message=True, duration=4)))
    assert d["kind"] == "video_note"


def test_gif_carries_video_and_animated_and_reads_as_gif():
    """The ordering trap: an mp4 GIF has BOTH attributes, video listed first."""
    d = media_details(
        _doc(
            DocumentAttributeVideo(round_message=False, duration=3),
            DocumentAttributeAnimated(),
            DocumentAttributeFilename(file_name="giphy.mp4"),
        )
    )
    assert d["kind"] == "gif"


def test_video_sticker_stays_a_sticker():
    """The other ordering trap: a webm sticker also carries a video attribute."""
    d = media_details(
        _doc(
            DocumentAttributeVideo(round_message=False, duration=2),
            DocumentAttributeSticker(alt="🔥"),
        )
    )
    assert d["kind"] == "sticker"
    assert d["alt"] == "🔥"


def test_plain_file_reports_its_name_and_mime():
    d = media_details(
        _doc(DocumentAttributeFilename(file_name="resume.pdf"), mime="application/pdf")
    )
    assert d["kind"] == "file"
    assert d["file_name"] == "resume.pdf"
    assert d["mime_type"] == "application/pdf"


def test_photo_is_a_photo():
    assert media_details(MessageMediaPhoto(photo=object()))["kind"] == "photo"


def test_non_document_media_falls_back_to_its_own_name():
    """Polls, geo, contacts: the class name minus the prefix is the honest answer."""

    class MessageMediaPoll(SimpleNamespace):
        pass

    assert media_details(MessageMediaPoll())["kind"] == "poll"


def test_none_media_is_empty():
    assert media_details(None) == {}


# --- wired into every serialization site ----------------------------------


def test_get_messages_labels_kind_without_asking():
    w = _wrap([_msg(2, "", media=_doc(DocumentAttributeSticker(alt="👍")))])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["media_type"] == "MessageMediaDocument"
    assert out[0]["media_kind"] == "sticker"
    assert out[0]["media_alt"] == "👍"


def test_get_message_single_labels_kind():
    w = _wrap([_msg(2, "", media=_doc(DocumentAttributeAudio(voice=True, duration=11)))])
    out = asyncio.run(w.get_message(7, 2))
    assert out["media_kind"] == "voice"
    assert out["media_duration"] == 11


def test_include_media_payload_gains_the_same_detail():
    w = _wrap([_msg(1, "", media=_doc(DocumentAttributeSticker(alt="🙏")))])
    out = asyncio.run(w.get_messages(7, limit=10, include_media=True))
    assert out[0]["media"]["has_file"] is True
    assert out[0]["media"]["kind"] == "sticker"
    assert out[0]["media"]["alt"] == "🙏"


def test_dialog_last_message_labels_kind_too():
    """The surface the inbox and gh_pending read FIRST (L082's larger half)."""
    dialog = SimpleNamespace(
        unread_count=1,
        dialog=None,
        message=_msg(9, "", media=_doc(DocumentAttributeSticker(alt="❤"))),
    )
    extras = ClientWrapper._dialog_extras(dialog)
    assert extras["last_message"]["media_type"] == "MessageMediaDocument"
    assert extras["last_message"]["media_kind"] == "sticker"
    assert extras["last_message"]["media_alt"] == "❤"


def test_text_message_has_no_kind():
    w = _wrap([_msg(1, "سلام")])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert "media_kind" not in out[0]
