"""Hiding a peer's stories — the per-account "Hide Stories" toggle.

Two properties matter to the callers that do this in bulk: the flag is read
back from the *fresh* user object (so a no-op costs no RPC), and `user get`
reports the current value (so the state is auditable without a write).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telethon.tl.functions.stories import TogglePeerStoriesHiddenRequest
from telethon.tl.types import User

from tlgr.core.client import ClientWrapper
from tlgr.core.errors import TlgrError


class _FakeTelethon:
    def __init__(self, entity):
        self.entity = entity
        self.requests: list = []

    async def get_entity(self, ref):
        return self.entity

    async def __call__(self, request):
        self.requests.append(request)
        return True


def _make(entity):
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FakeTelethon(entity)
    return w


def _user(uid=7, hidden=None):
    return User(id=uid, first_name="u", username="someone", stories_hidden=hidden)


def test_hides_stories_and_reports_the_peer():
    w = _make(_user())
    result = asyncio.run(w.set_stories_hidden(7))
    assert result == {
        "user_id": 7,
        "username": "someone",
        "hidden": True,
        "already": False,
    }
    (req,) = w._client.requests
    assert isinstance(req, TogglePeerStoriesHiddenRequest)
    assert req.hidden is True


def test_already_hidden_sends_no_request():
    w = _make(_user(hidden=True))
    result = asyncio.run(w.set_stories_hidden(7))
    assert result["already"] is True
    assert result["hidden"] is True
    assert w._client.requests == []


def test_unhide_is_the_same_toggle_the_other_way():
    w = _make(_user(hidden=True))
    result = asyncio.run(w.set_stories_hidden(7, hidden=False))
    assert result["already"] is False
    (req,) = w._client.requests
    assert req.hidden is False


def test_unhide_of_a_visible_peer_is_also_a_no_op():
    w = _make(_user(hidden=None))
    assert asyncio.run(w.set_stories_hidden(7, hidden=False))["already"] is True
    assert w._client.requests == []


def test_refuses_a_non_user_peer():
    from telethon.tl.types import Channel

    w = _make(Channel(id=9, title="c", photo=None, date=None))
    with pytest.raises(TlgrError):
        asyncio.run(w.set_stories_hidden(9))
    assert w._client.requests == []


# -- the flag has to be readable, not only writable -------------------------


class _FullFake(_FakeTelethon):
    async def __call__(self, request):
        raise RuntimeError("no full-user fetch in this test")


def test_user_get_reports_stories_hidden():
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FullFake(_user(hidden=True))
    info = asyncio.run(w.get_user_info(7))
    assert info["stories_hidden"] is True


def test_user_get_reports_false_when_unset():
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FullFake(_user(hidden=None))
    assert asyncio.run(w.get_user_info(7))["stories_hidden"] is False
