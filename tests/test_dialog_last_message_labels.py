"""The dialog list's `last_message` must carry the same labels as a history.

`chat list` / `inbox` summarise each dialog with its last message. That summary
carried only id/date/out/text, so an empty `text` there meant three unrelated
things at once: a Telegram service event, a caption-less sticker or photo, and
a message someone genuinely sent blank.

Both other readings are already labelled in get_messages() — `service` (L018)
and `media_type` — but a consumer reading the dialog list got neither, and the
dialog list is the surface most agents look at FIRST. An unlabelled service
event there reads as "an outgoing message we did not send", which is precisely
tlgr-agent's manual-takeover signal.
"""

from __future__ import annotations

from types import SimpleNamespace

from tlgr.core.client import ClientWrapper


class MessageActionContactSignUp(SimpleNamespace):
    pass


class MessageMediaDocument(SimpleNamespace):
    pass


def _dialog(msg):
    return SimpleNamespace(unread_count=0, dialog=None, message=msg)


def _msg(text, *, out=False, action=None, media=None):
    return SimpleNamespace(id=9, date="2026-09-02", text=text, out=out,
                           action=action, media=media)


def test_service_event_is_labelled_in_last_message():
    e = ClientWrapper._dialog_extras(
        _dialog(_msg("", out=True, action=MessageActionContactSignUp())))
    assert e["last_message"]["service"] == "MessageActionContactSignUp"
    assert e["last_message"]["text"] == ""


def test_media_only_is_labelled_in_last_message():
    e = ClientWrapper._dialog_extras(
        _dialog(_msg("", media=MessageMediaDocument())))
    assert e["last_message"]["media_type"] == "MessageMediaDocument"
    assert "service" not in e["last_message"]


def test_plain_text_gets_neither_label():
    e = ClientWrapper._dialog_extras(_dialog(_msg("سلام")))
    assert e["last_message"]["text"] == "سلام"
    assert "service" not in e["last_message"]
    assert "media_type" not in e["last_message"]


def test_truly_empty_message_stays_unlabelled():
    """The residual case the labels exist to isolate."""
    e = ClientWrapper._dialog_extras(_dialog(_msg("", out=True)))
    assert "service" not in e["last_message"]
    assert "media_type" not in e["last_message"]


def test_dialog_with_no_message_has_no_last_message():
    e = ClientWrapper._dialog_extras(_dialog(None))
    assert "last_message" not in e
