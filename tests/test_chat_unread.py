"""Tests for chat unread marking — the undo for an accidental read receipt."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.functions.messages import MarkDialogUnreadRequest

from tlgr.core.client import ClientWrapper


class _FakeTelethon:
    def __init__(self):
        self.requests: list = []

    async def get_input_entity(self, chat_id):
        return f"entity:{chat_id}"

    async def __call__(self, request):
        self.requests.append(request)
        return True


def _make():
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _FakeTelethon()
    return w


def test_mark_chat_unread_sets_the_flag():
    w = _make()
    result = asyncio.run(w.mark_chat_unread(8328329704))
    assert result == {"unread": True, "chat_id": 8328329704}
    (req,) = w._client.requests
    assert isinstance(req, MarkDialogUnreadRequest)
    assert req.peer == "entity:8328329704"
    assert req.unread is True


def test_mark_chat_unread_can_clear():
    w = _make()
    result = asyncio.run(w.mark_chat_unread(42, unread=False))
    assert result == {"unread": False, "chat_id": 42}
    (req,) = w._client.requests
    assert req.unread is False


# --- the manual mark has to survive into list_chats/--unread, or the badge is
# --- visible in the phone and invisible to every tool that looks for work.


class _Dlg:
    def __init__(self, cid, unread_count, unread_mark):
        from telethon.tl.types import User

        self.id = cid
        self.entity = User(id=cid, first_name=f"u{cid}", bot=False)
        self.unread_count = unread_count
        self.message = None
        self.dialog = SimpleNamespace(read_outbox_max_id=0, unread_mark=unread_mark)


class _ListFake:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def iter_dialogs(self):
        for d in self._dialogs:
            yield d


def _list(dialogs):
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = _ListFake(dialogs)

    async def _run():
        return [c async for c in w.list_chats(unread_only=True)]

    return asyncio.run(_run())


def test_hand_marked_chat_shows_in_unread_listing():
    # unread_count 0 — nothing new arrived; someone marked it unread by hand
    rows = _list([_Dlg(1, 0, True)])
    assert [r["id"] for r in rows] == [1]
    assert rows[0]["unread_mark"] is True


def test_plain_read_chat_still_filtered_out():
    assert _list([_Dlg(2, 0, False)]) == []


def test_unread_mark_absent_when_not_set():
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    extras = w._dialog_extras(_Dlg(3, 4, False))
    assert "unread_mark" not in extras
    assert extras["unread_count"] == 4
