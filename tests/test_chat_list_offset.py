"""Regression test: list_chats must honor offset so cursor pagination advances."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import User

from tlgr.core.client import ClientWrapper


def _fake_user(uid: int, name: str) -> User:
    return User(id=uid, first_name=name, username=name.lower(), bot=False)


class _FakeTelethon:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def iter_dialogs(self):
        for d in self._dialogs:
            yield d


def _make_wrapper(n: int) -> ClientWrapper:
    dialogs = [
        SimpleNamespace(id=i, entity=_fake_user(i, f"user{i}"), unread_count=0, message=None)
        for i in range(n)
    ]
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FakeTelethon(dialogs)
    return w


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def test_offset_advances_pages():
    w = _make_wrapper(10)
    page1 = _collect(w.list_chats(limit=4, chat_type="user"))
    page2 = _collect(w.list_chats(limit=4, chat_type="user", offset=4))
    page3 = _collect(w.list_chats(limit=4, chat_type="user", offset=8))
    ids = [c["id"] for c in page1 + page2 + page3]
    assert ids == list(range(10))
    assert len(page3) == 2


def test_offset_counts_post_filter_matches():
    w = _make_wrapper(6)
    # mark odd ids as bots so the "user" filter drops them
    for d in w._client._dialogs:
        if d.id % 2:
            d.entity.bot = True
    page = _collect(w.list_chats(limit=2, chat_type="user", offset=1))
    assert [c["id"] for c in page] == [2, 4]


def test_offset_beyond_end_is_empty():
    w = _make_wrapper(3)
    page = _collect(w.list_chats(limit=5, chat_type="user", offset=10))
    assert page == []
