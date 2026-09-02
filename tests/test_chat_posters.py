"""`chat posters` — the harvest primitive that replaces a hand-rolled loop.

Every wake was re-implementing the same ~12-call `message list --sender
--offset-id` walk over a group's history and deduping senders in Python.
The loop is boilerplate; the bugs in it are not (off-by-one offsets, missing
bot/deleted flags, no flood backoff). This encodes the contract once.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import User

from tlgr.core.client import ClientWrapper


def _msg(mid, sender):
    return SimpleNamespace(
        id=mid,
        sender_id=getattr(sender, "id", None),
        sender=sender,
        date=datetime(2026, 8, 31, tzinfo=timezone.utc),
        text=f"m{mid}",
    )


def _user(uid, username=None, bot=False, deleted=False, first="U", last=None):
    return User(
        id=uid, first_name=first, last_name=last, username=username, bot=bot, deleted=deleted
    )


class _FakeTelethon:
    """iter_messages that paginates internally, exactly as Telethon's does."""

    def __init__(self, messages, flood_after=None):
        self._messages = messages
        self._flood_after = flood_after
        self.requested_limits: list[int] = []

    def iter_messages(self, chat_id, limit=20, offset_id=0, **kw):
        self.requested_limits.append(limit)
        msgs = self._messages
        flood_after = self._flood_after

        async def _gen():
            for i, m in enumerate(msgs[:limit]):
                if flood_after is not None and i == flood_after:
                    raise FloodWaitError(GetHistoryRequest, capture=25)
                yield m

        return _gen()


def _wrap(fake):
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = fake
    return w


def test_counts_are_per_sender_and_sorted_descending():
    a, b, c = _user(1, "alice"), _user(2, "bob"), _user(3)
    msgs = [_msg(i, a) for i in range(10)] + [_msg(i, b) for i in range(10, 13)] + [_msg(99, c)]
    r = asyncio.run(_wrap(_FakeTelethon(msgs)).chat_posters(-1004451258462))
    assert [p["id"] for p in r["posters"]] == [1, 2, 3]
    assert [p["count"] for p in r["posters"]] == [10, 3, 1]
    assert r["scanned_messages"] == 14
    assert r["distinct_posters"] == 3


def test_negative_chat_ids_pass_straight_through():
    fake = _FakeTelethon([_msg(1, _user(7))])
    seen = {}
    orig = fake.iter_messages

    def spy(chat_id, **kw):
        seen["chat"] = chat_id
        return orig(chat_id, **kw)

    fake.iter_messages = spy
    asyncio.run(_wrap(fake).chat_posters(-1004451258462))
    assert seen["chat"] == -1004451258462


def test_bot_and_deleted_flags_are_exposed_for_filtering():
    """Callers filter on these to avoid messaging bots and dead accounts —
    if they are missing the filter silently passes everyone."""
    msgs = [
        _msg(1, _user(1, "helperbot", bot=True)),
        _msg(2, _user(2, deleted=True)),
        _msg(3, _user(3, "real")),
    ]
    r = asyncio.run(_wrap(_FakeTelethon(msgs)).chat_posters(-100))
    by_id = {p["id"]: p for p in r["posters"]}
    assert by_id[1]["is_bot"] is True and by_id[1]["is_deleted"] is False
    assert by_id[2]["is_deleted"] is True and by_id[2]["is_bot"] is False
    assert by_id[3]["is_bot"] is False and by_id[3]["is_deleted"] is False
    assert by_id[3]["username"] == "real"


def test_name_joins_first_and_last():
    r = asyncio.run(
        _wrap(_FakeTelethon([_msg(1, _user(1, first="Ali", last="R"))])).chat_posters(-100)
    )
    assert r["posters"][0]["name"] == "Ali R"


def test_last_seen_is_the_newest_message_since_history_walks_backwards():
    u = _user(1)
    msgs = [_msg(500, u), _msg(400, u), _msg(300, u)]
    r = asyncio.run(_wrap(_FakeTelethon(msgs)).chat_posters(-100))
    assert r["posters"][0]["last_message_id"] == 500


def test_max_messages_bounds_the_scan_and_is_hard_capped():
    fake = _FakeTelethon([_msg(i, _user(1)) for i in range(100)])
    asyncio.run(_wrap(fake).chat_posters(-100, max_messages=25))
    assert fake.requested_limits == [25]

    fake2 = _FakeTelethon([_msg(1, _user(1))])
    asyncio.run(_wrap(fake2).chat_posters(-100, max_messages=10**9))
    assert fake2.requested_limits == [20000]  # runaway scans are refused


def test_default_scan_is_bounded():
    fake = _FakeTelethon([_msg(1, _user(1))])
    asyncio.run(_wrap(fake).chat_posters(-100))
    assert fake.requested_limits == [2000]


def test_limit_trims_to_the_top_n_posters():
    msgs = [_msg(i, _user(1)) for i in range(5)] + [_msg(9, _user(2))]
    r = asyncio.run(_wrap(_FakeTelethon(msgs)).chat_posters(-100, limit=1))
    assert [p["id"] for p in r["posters"]] == [1]
    assert r["distinct_posters"] == 2  # the total is still honest


def test_messages_without_a_sender_are_skipped_not_counted_as_a_poster():
    msgs = [
        _msg(1, _user(1)),
        SimpleNamespace(id=2, sender_id=None, sender=None, date=None, text=""),
    ]
    r = asyncio.run(_wrap(_FakeTelethon(msgs)).chat_posters(-100))
    assert r["distinct_posters"] == 1
    assert r["scanned_messages"] == 2


def test_flood_wait_backs_off_and_returns_the_partial_harvest():
    """Back off, don't hammer: keep what was collected and say it is partial."""
    msgs = [_msg(i, _user(1)) for i in range(3)] + [_msg(9, _user(2))]
    r = asyncio.run(_wrap(_FakeTelethon(msgs, flood_after=3)).chat_posters(-100))
    assert r["partial"] is True
    assert r["flood_wait"] == 25
    assert r["posters"][0]["count"] == 3


def test_flood_wait_before_any_result_propagates_as_a_rate_limit():
    """Nothing to hand back means the caller must see the real rate limit
    (exit 7 with wait_seconds), not an empty answer that reads as 'no posters'."""
    fake = _FakeTelethon([_msg(1, _user(1))], flood_after=0)
    try:
        asyncio.run(_wrap(fake).chat_posters(-100))
    except FloodWaitError as e:
        assert e.seconds == 25
    else:
        raise AssertionError("FloodWaitError should propagate when nothing was collected")


def test_empty_chat_yields_no_posters():
    r = asyncio.run(_wrap(_FakeTelethon([])).chat_posters(-100))
    assert r["posters"] == []
    assert r["distinct_posters"] == 0
