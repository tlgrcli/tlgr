"""A serialized message must say whether THIS account already reacted.

tlgr had an `include_reactions` flag that was off by default, unexposed on
`chat open`/`catchup`, and emitted `str(msg.reactions)` — a Telethon repr. So
nothing reading a history could tell an unacknowledged message from one this
account had already hearted. The only way to find out was to send a duplicate
reaction and read the failure: Telegram answers one with MESSAGE_NOT_MODIFIED,
which surfaced as a generic error and looked like a broken send.

`reactions.mine` closes that: it comes from ReactionCount.chosen_order, which
Telegram sets only on reactions this account made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tlgr.core.client import ClientWrapper


def _rc(emoticon=None, count=1, chosen=None, document_id=None):
    reaction = SimpleNamespace(emoticon=emoticon) if emoticon is not None \
        else SimpleNamespace(document_id=document_id)
    return SimpleNamespace(reaction=reaction, count=count, chosen_order=chosen)


def _msg(mid, text, *, out=False, reactions=None):
    return SimpleNamespace(
        id=mid, date="2026-09-02", text=text, out=out, action=None,
        reply_to_msg_id=None, sender=None, sender_id=None, media=None,
        entities=None, reactions=reactions, reply_to=None, forward=None,
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


def test_no_reactions_means_no_field():
    """The field only appears where it means something."""
    w = _wrap([_msg(1, "سلام")])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert "reactions" not in out[0]


def test_reaction_by_someone_else_is_not_mine():
    r = SimpleNamespace(results=[_rc("❤", count=1, chosen=None)])
    w = _wrap([_msg(1, "زدم واست", reactions=r)])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["reactions"]["counts"] == {"❤": 1}
    assert out[0]["reactions"]["mine"] == []


def test_our_own_reaction_is_reported_as_mine():
    """The whole point: don't re-react to something already hearted."""
    r = SimpleNamespace(results=[_rc("❤", count=2, chosen=0)])
    w = _wrap([_msg(1, "زدم واست", reactions=r)])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["reactions"]["mine"] == ["❤"]
    assert out[0]["reactions"]["counts"] == {"❤": 2}


def test_mixed_reactions_separate_ours_from_theirs():
    r = SimpleNamespace(results=[
        _rc("❤", count=2, chosen=0),
        _rc("👍", count=3, chosen=None),
    ])
    w = _wrap([_msg(1, "x", reactions=r)])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["reactions"]["counts"] == {"❤": 2, "👍": 3}
    assert out[0]["reactions"]["mine"] == ["❤"]


def test_custom_premium_reaction_is_named_not_dropped():
    """A custom reaction has no emoticon; it must still be visible."""
    r = SimpleNamespace(results=[_rc(None, count=1, chosen=0, document_id=555)])
    w = _wrap([_msg(1, "x", reactions=r)])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert out[0]["reactions"]["counts"] == {"custom:555": 1}
    assert out[0]["reactions"]["mine"] == ["custom:555"]


def test_empty_results_is_treated_as_no_reactions():
    """A reactions object with nothing in it is not a reaction."""
    w = _wrap([_msg(1, "x", reactions=SimpleNamespace(results=[]))])
    out = asyncio.run(w.get_messages(7, limit=10))
    assert "reactions" not in out[0]


def test_get_message_single_also_reports_reactions():
    r = SimpleNamespace(results=[_rc("❤", count=1, chosen=0)])
    w = _wrap([_msg(2, "x", reactions=r)])
    out = asyncio.run(w.get_message(7, 2))
    assert out["reactions"]["mine"] == ["❤"]


def test_raw_repr_still_available_behind_the_flag():
    """include_reactions kept working, moved to reactions_raw."""
    r = SimpleNamespace(results=[_rc("❤", count=1, chosen=0)])
    w = _wrap([_msg(1, "x", reactions=r)])
    out = asyncio.run(w.get_messages(7, limit=10, include_reactions=True))
    assert "reactions_raw" in out[0]
    assert out[0]["reactions"]["mine"] == ["❤"]


class _AlreadyReacted(Exception):
    def __str__(self):
        return "Content of the message was not modified (caused by SendReactionRequest)"


def test_duplicate_reaction_reports_already_not_error():
    """MESSAGE_NOT_MODIFIED is the desired end state, not a failed send."""
    class _T:
        async def __call__(self, req):
            raise _AlreadyReacted()

    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _T()
    out = asyncio.run(w.react_to_message(7, 1, "❤"))
    assert out == {"reacted": True, "msg_id": 1, "emoji": "❤", "already": True}


def test_fresh_reaction_reports_already_false():
    class _T:
        async def __call__(self, req):
            return None

    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _T()
    out = asyncio.run(w.react_to_message(7, 1, "❤"))
    assert out["already"] is False


def test_other_react_errors_still_raise():
    """Only 'not modified' is swallowed — a real failure must stay a failure."""
    class _T:
        async def __call__(self, req):
            raise RuntimeError("PEER_FLOOD")

    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _T()
    try:
        asyncio.run(w.react_to_message(7, 1, "❤"))
    except RuntimeError as e:
        assert "PEER_FLOOD" in str(e)
    else:
        raise AssertionError("a real react failure must propagate")
