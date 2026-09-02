"""A dialog's `last_message` must carry the same reaction summary a history does.

`_dialog_extras` already learned to label a service event and a media-only
message (L082/L093), for a stated reason: a consumer reading the dialog list
should not have to re-fetch the chat to find out what the last message *was*.
Reaction state was left behind by that same argument and it belongs to it —
`gh_pending` files a delivered chat whose contact wrote again, and the rule
for those (L090) turns on whether this account already reacted. With
`reactions` absent from the dialog list, that question was unanswerable from
the census: two consecutive wakes opened the chat by hand to answer it, and
opening a closed or user-driven chat is one read receipt away from deleting
the user's own unread badge (L056).

So: same field, same shape, same site as the history serializer.
"""

from __future__ import annotations

from types import SimpleNamespace

from tlgr.core.client import ClientWrapper


def _rc(emoticon=None, count=1, chosen=None, document_id=None):
    reaction = (
        SimpleNamespace(emoticon=emoticon)
        if emoticon is not None
        else SimpleNamespace(document_id=document_id)
    )
    return SimpleNamespace(reaction=reaction, count=count, chosen_order=chosen)


def _msg(mid=5, text="سلام", *, out=False, reactions=None, media=None, action=None):
    return SimpleNamespace(
        id=mid,
        date="2026-09-02",
        text=text,
        out=out,
        action=action,
        media=media,
        reactions=reactions,
    )


def _dialog(msg):
    return SimpleNamespace(unread_count=1, dialog=None, message=msg)


def test_last_message_reports_our_own_reaction():
    """The whole point: `mine` says we already hearted their closing line."""
    msg = _msg(reactions=SimpleNamespace(results=[_rc("❤", count=1, chosen=0)]))
    extras = ClientWrapper._dialog_extras(_dialog(msg))
    assert extras["last_message"]["reactions"]["mine"] == ["❤"]
    assert extras["last_message"]["reactions"]["counts"] == {"❤": 1}


def test_last_message_reaction_by_them_only():
    """A contact reacting to US: counted, but `mine` stays empty."""
    msg = _msg(out=True, reactions=SimpleNamespace(results=[_rc("👍", count=2, chosen=None)]))
    extras = ClientWrapper._dialog_extras(_dialog(msg))
    assert extras["last_message"]["reactions"]["counts"] == {"👍": 2}
    assert extras["last_message"]["reactions"]["mine"] == []


def test_field_absent_when_there_are_no_reactions():
    """Absent, not empty — the field only appears where it means something."""
    extras = ClientWrapper._dialog_extras(_dialog(_msg(reactions=None)))
    assert "reactions" not in extras["last_message"]


def test_reactions_survive_alongside_media_and_service_labels():
    """The new field must not displace the labels already emitted here."""
    media = SimpleNamespace(document=SimpleNamespace(attributes=[], mime_type=None))
    msg = _msg(
        text="", media=media, reactions=SimpleNamespace(results=[_rc("❤", count=1, chosen=0)])
    )
    extras = ClientWrapper._dialog_extras(_dialog(msg))
    lm = extras["last_message"]
    assert lm["media_type"] == "SimpleNamespace"
    assert lm["reactions"]["mine"] == ["❤"]


def test_no_message_means_no_crash():
    """An empty dialog has no last_message to decorate."""
    extras = ClientWrapper._dialog_extras(
        SimpleNamespace(unread_count=0, dialog=None, message=None)
    )
    assert "last_message" not in extras
