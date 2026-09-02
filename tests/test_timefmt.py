"""Timestamps and durations: one format out, several accepted in."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tlgr.core.timefmt import (
    TimeFormatError,
    fmt_dt,
    fmt_local,
    parse_dt,
    parse_duration,
    to_unix,
)

UTC = timezone.utc
NOON = datetime(2026, 9, 2, 9, 14, 7, tzinfo=UTC)


class TestOut:
    def test_rfc3339_with_z(self):
        assert fmt_dt(NOON) == "2026-09-02T09:14:07Z"

    def test_other_zones_are_converted(self):
        tehran = NOON.astimezone(timezone(timedelta(hours=3, minutes=30)))
        assert fmt_dt(tehran) == "2026-09-02T09:14:07Z"

    def test_none_stays_none(self):
        assert fmt_dt(None) is None
        assert to_unix(None) is None

    def test_unix_sibling(self):
        assert to_unix(NOON) == int(NOON.timestamp())

    def test_legacy_spelling_is_available(self):
        """`[defaults] legacy_dates` restores v1's str(datetime) for one release."""
        assert fmt_dt(NOON, legacy=True) == "2026-09-02 09:14:07+00:00"

    def test_roundtrips_through_the_parser(self):
        assert parse_dt(fmt_dt(NOON)) == NOON


class TestHuman:
    def test_today(self):
        now = datetime.now().astimezone()
        assert fmt_local(now.replace(hour=14, minute=2), now=now).startswith("today ")

    def test_yesterday(self):
        now = datetime.now().astimezone()
        assert fmt_local(now - timedelta(days=1), now=now).startswith("yesterday ")

    def test_this_week_is_a_weekday(self):
        now = datetime.now().astimezone()
        rendered = fmt_local(now - timedelta(days=3), now=now)
        assert len(rendered.split()) == 2
        assert not rendered.startswith(("today", "yesterday"))

    def test_older_is_a_full_date(self):
        now = datetime.now().astimezone()
        assert fmt_local(now - timedelta(days=40), now=now).count("-") == 2

    def test_missing_renders_as_a_dash(self):
        assert fmt_local(None) == "-"


class TestParseDt:
    @pytest.mark.parametrize(
        "text",
        [
            "2026-09-02T09:14:07Z",
            "2026-09-02T09:14:07+00:00",
            "2026-09-02",
            "2026-09-02T09:14",
        ],
    )
    def test_accepted_forms(self, text):
        assert parse_dt(text) is not None

    def test_naive_values_are_local_not_utc(self):
        """Typing 09:00 means nine in your own morning (COR-23)."""
        parsed = parse_dt("2026-09-02T09:00")
        assert parsed is not None
        assert parsed.utcoffset() == datetime(2026, 9, 2, 9).astimezone().utcoffset()

    def test_relative_offsets(self):
        assert parse_dt("-90m", now=NOON) == NOON - timedelta(minutes=90)
        assert parse_dt("+3d", now=NOON) == NOON + timedelta(days=3)

    def test_now(self):
        assert parse_dt("now", now=NOON) == NOON

    def test_unix_timestamp(self):
        assert parse_dt("1788340447") == datetime.fromtimestamp(1788340447, tz=UTC)

    def test_empty_is_none(self):
        assert parse_dt("") is None
        assert parse_dt(None) is None

    @pytest.mark.parametrize("text", ["tomorrow-ish", "2026-13-45", "5 o'clock"])
    def test_rejects_nonsense(self, text):
        with pytest.raises(TimeFormatError):
            parse_dt(text)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("30s", 30),
            ("5m", 300),
            ("2h", 7200),
            ("7d", 604800),
            ("1w", 604800),
            ("45", 45),
            ("0", 0),
        ],
    )
    def test_units(self, text, seconds):
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", ["forever", "never", "none", ""])
    def test_forever_is_none(self, text):
        assert parse_duration(text) is None

    def test_zero_is_not_forever(self):
        """0 means 'now'; only 'forever' means 'no end'."""
        assert parse_duration("0") == 0

    @pytest.mark.parametrize("text", ["soon", "5y", "-30s", "m"])
    def test_rejects_nonsense(self, text):
        with pytest.raises(TimeFormatError):
            parse_duration(text)
