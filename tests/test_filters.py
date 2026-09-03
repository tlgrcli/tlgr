"""Tests for the filter registry and composition engine."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from tlgr.filters import get_filter, list_filters, register_filter
from tlgr.filters.compose import (
    FilterNode,
    Op,
    evaluate,
    parse_filter_config,
)
from tlgr.gateway.event import Event

# ---------------------------------------------------------------------------
# Helpers — mock Telethon events
# ---------------------------------------------------------------------------


def _make_tg_event(
    *,
    text: str = "hello world",
    is_private: bool = False,
    is_group: bool = False,
    is_channel: bool = False,
    sender_id: int = 100,
    out: bool = False,
    reply_to=None,
    forward=None,
    media=None,
    entities=None,
    date=None,
    sender_bot: bool = False,
    chat_title: str = "",
):
    msg = MagicMock()
    msg.text = text
    msg.message = text
    msg.sender_id = sender_id
    msg.out = out
    msg.reply_to = reply_to
    msg.forward = forward
    msg.media = media
    msg.entities = entities
    msg.date = date or datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    msg.action = None

    sender = MagicMock()
    sender.bot = sender_bot
    msg.sender = sender

    ev = MagicMock()
    ev.message = msg
    ev.chat_id = 42
    ev.is_private = is_private
    ev.is_group = is_group
    ev.is_channel = is_channel

    chat = MagicMock()
    chat.title = chat_title
    chat.megagroup = False
    ev.chat = chat

    return ev


def _wrap(tg_event) -> Event:
    return Event(source="telegram", raw=tg_event, account="test")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtin_filters_registered(self):
        names = list_filters()
        assert "chat_type" in names
        assert "contains" in names
        assert "types" in names
        assert "after" in names
        assert "from_users" in names

    def test_get_unknown_returns_none(self):
        assert get_filter("nonexistent") is None

    def test_custom_filter_registration(self):
        @register_filter("_test_custom")
        def custom(event, value):
            return True, "ok"

        assert get_filter("_test_custom") is not None
        ev = _wrap(_make_tg_event())
        ok, reason = get_filter("_test_custom")(ev, None)
        assert ok


# ---------------------------------------------------------------------------
# Individual filters
# ---------------------------------------------------------------------------


class TestContextFilters:
    def test_chat_type_private(self):
        ev = _wrap(_make_tg_event(is_private=True))
        f = get_filter("chat_type")
        ok, _ = f(ev, "private")
        assert ok

    def test_chat_type_mismatch(self):
        ev = _wrap(_make_tg_event(is_private=True))
        f = get_filter("chat_type")
        ok, _ = f(ev, "group")
        assert not ok

    def test_chat_type_list(self):
        ev = _wrap(_make_tg_event(is_channel=True))
        f = get_filter("chat_type")
        ok, _ = f(ev, ["group", "channel"])
        assert ok

    def test_chat_id_match(self):
        ev = _wrap(_make_tg_event())
        f = get_filter("chat_id")
        ok, _ = f(ev, 42)
        assert ok

    def test_chat_id_no_match(self):
        ev = _wrap(_make_tg_event())
        f = get_filter("chat_id")
        ok, _ = f(ev, 999)
        assert not ok

    def test_chat_title_regex(self):
        ev = _wrap(_make_tg_event(chat_title="Breaking News Channel"))
        f = get_filter("chat_title")
        ok, _ = f(ev, "breaking")
        assert ok

    def test_is_incoming_true(self):
        ev = _wrap(_make_tg_event(out=False))
        f = get_filter("is_incoming")
        ok, _ = f(ev, True)
        assert ok

    def test_sender_is_bot(self):
        ev = _wrap(_make_tg_event(sender_bot=True))
        f = get_filter("sender_is_bot")
        ok, _ = f(ev, True)
        assert ok

    def test_sender_is_self(self):
        ev = _wrap(_make_tg_event(out=True))
        f = get_filter("sender_is_self")
        ok, _ = f(ev, True)
        assert ok


class TestContentFilters:
    def test_contains_all(self):
        ev = _wrap(_make_tg_event(text="hello world"))
        f = get_filter("contains")
        ok, _ = f(ev, ["hello", "world"])
        assert ok

    def test_contains_missing(self):
        ev = _wrap(_make_tg_event(text="hello world"))
        f = get_filter("contains")
        ok, _ = f(ev, ["hello", "missing"])
        assert not ok

    def test_contains_any(self):
        ev = _wrap(_make_tg_event(text="hello world"))
        f = get_filter("contains_any")
        ok, _ = f(ev, ["missing", "world"])
        assert ok

    def test_excludes(self):
        ev = _wrap(_make_tg_event(text="hello world"))
        f = get_filter("excludes")
        ok, _ = f(ev, ["spam"])
        assert ok
        ok, _ = f(ev, ["hello"])
        assert not ok

    def test_regex(self):
        ev = _wrap(_make_tg_event(text="order #12345"))
        f = get_filter("regex")
        ok, _ = f(ev, r"#\d+")
        assert ok

    def test_regex_no_match(self):
        ev = _wrap(_make_tg_event(text="no numbers"))
        f = get_filter("regex")
        ok, _ = f(ev, r"#\d+")
        assert not ok


class TestMessageFilters:
    def test_is_reply(self):
        ev = _wrap(_make_tg_event(reply_to=MagicMock()))
        f = get_filter("is_reply")
        ok, _ = f(ev, True)
        assert ok

    def test_is_not_reply(self):
        ev = _wrap(_make_tg_event(reply_to=None))
        f = get_filter("is_reply")
        ok, _ = f(ev, False)
        assert ok

    def test_is_forward(self):
        ev = _wrap(_make_tg_event(forward=MagicMock()))
        f = get_filter("is_forward")
        ok, _ = f(ev, True)
        assert ok

    def test_has_media(self):
        ev = _wrap(_make_tg_event(media=MagicMock()))
        f = get_filter("has_media")
        ok, _ = f(ev, True)
        assert ok

    def test_no_media(self):
        ev = _wrap(_make_tg_event(media=None))
        f = get_filter("has_media")
        ok, _ = f(ev, False)
        assert ok


class TestTemporalFilters:
    def test_after(self):
        ev = _wrap(_make_tg_event(date=datetime(2025, 7, 1, tzinfo=timezone.utc)))
        f = get_filter("after")
        ok, _ = f(ev, "2025-06-01")
        assert ok

    def test_after_fail(self):
        ev = _wrap(_make_tg_event(date=datetime(2025, 5, 1, tzinfo=timezone.utc)))
        f = get_filter("after")
        ok, _ = f(ev, "2025-06-01")
        assert not ok

    def test_before(self):
        ev = _wrap(_make_tg_event(date=datetime(2025, 5, 1, tzinfo=timezone.utc)))
        f = get_filter("before")
        ok, _ = f(ev, "2025-06-01")
        assert ok

    def test_time_of_day(self):
        ev = _wrap(_make_tg_event(date=datetime(2025, 6, 15, 14, 30, tzinfo=timezone.utc)))
        f = get_filter("time_of_day")
        ok, _ = f(ev, "09:00-18:00")
        assert ok

    def test_time_of_day_outside(self):
        ev = _wrap(_make_tg_event(date=datetime(2025, 6, 15, 23, 30, tzinfo=timezone.utc)))
        f = get_filter("time_of_day")
        ok, _ = f(ev, "09:00-18:00")
        assert not ok


class TestUserFilters:
    def test_from_users(self):
        ev = _wrap(_make_tg_event(sender_id=42))
        f = get_filter("from_users")
        ok, _ = f(ev, [42, 99])
        assert ok

    def test_from_users_no_match(self):
        ev = _wrap(_make_tg_event(sender_id=1))
        f = get_filter("from_users")
        ok, _ = f(ev, [42, 99])
        assert not ok

    def test_exclude_users(self):
        ev = _wrap(_make_tg_event(sender_id=42))
        f = get_filter("exclude_users")
        ok, _ = f(ev, [42])
        assert not ok


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_parse_empty(self):
        assert parse_filter_config(None) is None
        assert parse_filter_config({}) is None

    def test_parse_single_leaf(self):
        node = parse_filter_config({"chat_type": "private"})
        assert node is not None
        assert node.op is Op.LEAF
        assert node.filter_name == "chat_type"

    def test_parse_multiple_and(self):
        node = parse_filter_config({"chat_type": "private", "contains": ["hello"]})
        assert node.op is Op.AND
        assert len(node.children) == 2

    def test_parse_any_of(self):
        node = parse_filter_config(
            {
                "any_of": [
                    {"contains": ["hello"]},
                    {"from_users": [42]},
                ]
            }
        )
        assert node.op is Op.OR

    def test_parse_none_of(self):
        node = parse_filter_config(
            {
                "none_of": [
                    {"contains": ["spam"]},
                ]
            }
        )
        assert node.op is Op.NOT

    def test_evaluate_and(self):
        ev = _wrap(_make_tg_event(is_private=True, text="hello world"))
        node = parse_filter_config({"chat_type": "private", "contains": ["hello"]})
        ok, _ = evaluate(node, ev)
        assert ok

    def test_evaluate_and_fail(self):
        ev = _wrap(_make_tg_event(is_private=True, text="goodbye"))
        node = parse_filter_config({"chat_type": "private", "contains": ["hello"]})
        ok, _ = evaluate(node, ev)
        assert not ok

    def test_evaluate_or(self):
        ev = _wrap(_make_tg_event(text="goodbye", sender_id=42))
        node = parse_filter_config(
            {
                "any_of": [
                    {"contains": ["hello"]},
                    {"from_users": [42]},
                ]
            }
        )
        ok, _ = evaluate(node, ev)
        assert ok

    def test_evaluate_or_fail(self):
        ev = _wrap(_make_tg_event(text="goodbye", sender_id=1))
        node = parse_filter_config(
            {
                "any_of": [
                    {"contains": ["hello"]},
                    {"from_users": [42]},
                ]
            }
        )
        ok, _ = evaluate(node, ev)
        assert not ok

    def test_evaluate_not(self):
        ev = _wrap(_make_tg_event(text="good content"))
        node = parse_filter_config(
            {
                "none_of": [
                    {"contains": ["spam"]},
                ]
            }
        )
        ok, _ = evaluate(node, ev)
        assert ok

    def test_evaluate_not_fail(self):
        ev = _wrap(_make_tg_event(text="this is spam"))
        node = parse_filter_config(
            {
                "none_of": [
                    {"contains": ["spam"]},
                ]
            }
        )
        ok, _ = evaluate(node, ev)
        assert not ok

    def test_nested_composition(self):
        ev = _wrap(_make_tg_event(is_private=True, text="hello", sender_id=42))
        node = parse_filter_config(
            {
                "chat_type": "private",
                "any_of": [
                    {"contains": ["hello"]},
                    {"from_users": [99]},
                ],
                "none_of": [
                    {"contains": ["spam"]},
                ],
            }
        )
        ok, _ = evaluate(node, ev)
        assert ok

    def test_no_filters_passes(self):
        ev = _wrap(_make_tg_event())
        ok, _ = evaluate(None, ev)
        assert ok

    def test_unknown_filter_fails(self):
        node = FilterNode(op=Op.LEAF, filter_name="nonexistent_xyz", filter_value=True)
        ev = _wrap(_make_tg_event())
        ok, reason = evaluate(node, ev)
        assert not ok
        assert "unknown filter" in reason
