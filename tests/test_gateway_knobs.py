"""Parsing the knobs every job action shares."""

from __future__ import annotations

from datetime import datetime

import pytest

from tlgr.gateway.knobs import (
    ActionKnobs,
    KnobError,
    QuietHours,
    merge_knobs,
    parse_delay,
    parse_duration,
    parse_knobs,
    parse_percent,
    parse_presence,
)


class TestDurations:
    @pytest.mark.parametrize(
        ("raw", "seconds"),
        [("5s", 5.0), ("500ms", 0.5), ("2m", 120.0), ("1.5s", 1.5), ("24h", 86400.0), (30, 30.0)],
    )
    def test_units(self, raw, seconds):
        assert parse_duration(raw) == seconds

    @pytest.mark.parametrize("raw", ["soon", "5 parsecs", "-3s", True, [], "1x"])
    def test_garbage_is_refused(self, raw):
        with pytest.raises(KnobError):
            parse_duration(raw)

    @pytest.mark.parametrize(
        ("raw", "pair"),
        [
            ("10-90s", (10.0, 90.0)),
            ("1-3m", (60.0, 180.0)),
            ("1m-3m", (60.0, 180.0)),
            ("500ms-2s", (0.5, 2.0)),
            ("5s", (5.0, 5.0)),
            (None, (0.0, 0.0)),
        ],
    )
    def test_ranges(self, raw, pair):
        assert parse_delay(raw) == pair

    def test_an_inverted_range_is_refused(self):
        with pytest.raises(KnobError, match="low end above"):
            parse_delay("90-10s")


class TestPercent:
    @pytest.mark.parametrize("raw", [0, 60, 100, "60", "60%"])
    def test_valid(self, raw):
        assert 0 <= parse_percent(raw) <= 100

    @pytest.mark.parametrize("raw", [-1, 101, 50.5, "half", True])
    def test_invalid(self, raw):
        with pytest.raises(KnobError):
            parse_percent(raw)


class TestPresence:
    def test_short_form(self):
        assert parse_presence("session").mode == "session"

    def test_long_form_with_quiet_hours(self):
        presence = parse_presence({"mode": "blip", "quiet_hours": "01:00-08:00"})
        assert presence.mode == "blip"
        assert presence.quiet_hours == QuietHours(60, 480, "01:00-08:00")

    @pytest.mark.parametrize(
        "raw", ["always", {"mode": "loud"}, {"mode": "blip", "quiet": "1-2"}, 7]
    )
    def test_invalid(self, raw):
        with pytest.raises(KnobError):
            parse_presence(raw)


class TestQuietHours:
    def test_a_window_inside_one_day(self):
        quiet = QuietHours.parse("01:00-08:00")
        assert quiet.contains(datetime(2026, 1, 1, 3, 0))
        assert not quiet.contains(datetime(2026, 1, 1, 8, 0))
        assert quiet.window_end(datetime(2026, 1, 1, 3, 0)) == datetime(2026, 1, 1, 8, 0)

    def test_a_window_across_midnight(self):
        quiet = QuietHours.parse("23:00-07:00")
        assert quiet.contains(datetime(2026, 1, 1, 23, 30))
        assert quiet.contains(datetime(2026, 1, 2, 6, 59))
        assert quiet.window_end(datetime(2026, 1, 1, 23, 30)) == datetime(2026, 1, 2, 7, 0)

    def test_outside_the_window_nothing_is_held(self):
        quiet = QuietHours.parse("01:00-08:00")
        moment = datetime(2026, 1, 1, 12, 0)
        assert quiet.window_end(moment) == moment

    @pytest.mark.parametrize("raw", ["25:00-08:00", "1-8", "01:00", None])
    def test_invalid(self, raw):
        with pytest.raises(KnobError):
            QuietHours.parse(raw)


class TestMerging:
    def test_defaults_reproduce_the_old_behaviour(self):
        knobs = merge_knobs({}, {})
        assert knobs == ActionKnobs()
        assert knobs.delay == (0.0, 0.0)
        assert knobs.percent == 100
        assert knobs.presence.mode == "leave"
        assert knobs.dry_run is False

    def test_the_action_wins_over_the_job(self):
        job = parse_knobs({"delay": "10-20s", "percent": 50, "on_takeover": "ignore"})
        action = parse_knobs({"percent": 80})
        knobs = merge_knobs(job, action)
        assert knobs.delay == (10.0, 20.0)
        assert knobs.percent == 80
        assert knobs.on_takeover == "ignore"

    def test_errors_name_where_they_came_from(self):
        with pytest.raises(KnobError, match="job 'x', action 1: on_takeover"):
            parse_knobs({"on_takeover": "panic"}, where="job 'x', action 1")

    def test_dry_run_must_be_a_boolean(self):
        with pytest.raises(KnobError, match="dry_run"):
            parse_knobs({"dry_run": "yes"})
