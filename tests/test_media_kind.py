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

from types import SimpleNamespace

from tlgr.ops._serialize import media_summary


def media_details(media):
    """v1's dict shape, from the typed summary that replaced it.

    PR-12 deleted `ClientWrapper` and with it `media_details`; the logic it
    carried lives in `media_summary` and is what the ops serialise with. This
    shim keeps the *claims* below written the way they were made, because
    they are about the classifier and not about which module holds it.
    """
    summary = media_summary(media)
    if summary is None:
        return {}
    out = {"kind": summary.kind}
    for name in ("alt", "duration", "file_name", "mime_type"):
        value = getattr(summary, name, None)
        if value is not None:
            out[name] = value
    return out


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
