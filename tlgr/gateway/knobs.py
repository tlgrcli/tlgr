"""The knobs every job action shares: delay, percent, presence, takeover, dry run.

They are parsed once, when `jobs.yaml` is read, into plain values the
scheduler can use without re-reading YAML: a delay is a `(low, high)` pair of
seconds, a quiet window is two minutes-of-day. A malformed value raises
`KnobError` with a message that names the key and the value, because the only
person who will ever read it is the one who typed the YAML.

Durations accept `500ms`, `5s`, `1.5s`, `2m`, `1h`, `24h` and a bare number of
seconds; a range is two durations with a dash, the unit written once or on
both ends (`10-90s`, `1m-3m`, `500ms-2s`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "PRESENCE_MODES",
    "TAKEOVER_MODES",
    "ActionKnobs",
    "KnobError",
    "Presence",
    "QuietHours",
    "merge_knobs",
    "parse_delay",
    "parse_duration",
    "parse_knobs",
    "parse_percent",
    "parse_presence",
]

PRESENCE_MODES = ("leave", "blip", "session")
TAKEOVER_MODES = ("cancel", "cancel_read", "ignore")

#: The knob names valid on a job and on every action.
KNOB_KEYS = frozenset({"delay", "percent", "presence", "on_takeover", "dry_run"})

_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


class KnobError(ValueError):
    """A knob value that cannot be parsed. The message is for a human."""


def parse_duration(value: Any, *, what: str = "duration", default_unit: str = "s") -> float:
    """`"90s"`, `"1.5m"`, `"500ms"`, `30` → seconds."""
    if isinstance(value, bool):
        raise KnobError(f"{what}: {value!r} is not a duration")
    if isinstance(value, (int, float)):
        if value < 0:
            raise KnobError(f"{what}: {value!r} is negative")
        return float(value) * _UNITS[default_unit]
    if not isinstance(value, str):
        raise KnobError(f"{what}: {value!r} is not a duration (try 5s, 2m, 500ms)")
    match = _DURATION_RE.match(value)
    if match is None:
        raise KnobError(f"{what}: {value!r} is not a duration (try 5s, 2m, 500ms)")
    number, unit = match.groups()
    return float(number) * _UNITS[(unit or default_unit).lower()]


def parse_delay(value: Any) -> tuple[float, float]:
    """`"10-90s"` → `(10.0, 90.0)`; a single duration is a fixed delay."""
    if value is None:
        return (0.0, 0.0)
    if isinstance(value, str) and "-" in value.strip().lstrip("-"):
        low_raw, _, high_raw = value.strip().partition("-")
        high_match = _DURATION_RE.match(high_raw)
        low_match = _DURATION_RE.match(low_raw)
        if high_match is None or low_match is None:
            raise KnobError(f"delay: {value!r} is not a range (try 10-90s or 1m-3m)")
        # `10-90s`: the unit written once, on the high end, applies to both.
        unit = high_match.group(2) or "s"
        low = parse_duration(low_raw, what="delay", default_unit=unit.lower())
        high = parse_duration(high_raw, what="delay")
        if low > high:
            raise KnobError(f"delay: {value!r} has its low end above its high end")
        return (low, high)
    seconds = parse_duration(value, what="delay")
    return (seconds, seconds)


def parse_percent(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise KnobError(f"percent: {value!r} is not a number from 0 to 100")
    try:
        number = float(str(value).strip().rstrip("%"))
    except ValueError as exc:
        raise KnobError(f"percent: {value!r} is not a number from 0 to 100") from exc
    if number != int(number) or not 0 <= number <= 100:
        raise KnobError(f"percent: {value!r} must be a whole number from 0 to 100")
    return int(number)


def _minutes(text: str, raw: Any) -> int:
    match = _CLOCK_RE.match(text)
    if match is None:
        raise KnobError(f"quiet_hours: {raw!r} is not HH:MM-HH:MM")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise KnobError(f"quiet_hours: {raw!r} has an impossible time")
    return hours * 60 + minutes


@dataclass(frozen=True)
class QuietHours:
    """A daily window, `start` inclusive and `end` exclusive, in minutes of day.

    `start > end` wraps midnight (`23:00-07:00`); `start == end` is empty.
    """

    start: int
    end: int
    text: str = ""

    @classmethod
    def parse(cls, value: Any) -> QuietHours:
        if not isinstance(value, str) or "-" not in value:
            raise KnobError(f"quiet_hours: {value!r} is not HH:MM-HH:MM")
        start, _, end = value.partition("-")
        return cls(_minutes(start, value), _minutes(end, value), value.strip())

    def contains(self, moment: datetime) -> bool:
        minute = moment.hour * 60 + moment.minute
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def window_end(self, moment: datetime) -> datetime:
        """When the window that contains *moment* closes (*moment* if it is outside)."""
        if not self.contains(moment):
            return moment
        end = moment.replace(hour=self.end // 60, minute=self.end % 60, second=0, microsecond=0)
        if end <= moment:
            end += timedelta(days=1)
        return end


@dataclass(frozen=True)
class Presence:
    mode: str = "leave"
    quiet_hours: QuietHours | None = None


def parse_presence(value: Any) -> Presence:
    """`session`, or `{mode: session, quiet_hours: "01:00-08:00"}`."""
    if value is None:
        return Presence()
    if isinstance(value, str):
        mode, quiet = value.strip().lower(), None
    elif isinstance(value, dict):
        unknown = set(value) - {"mode", "quiet_hours"}
        if unknown:
            raise KnobError(f"presence: unknown key(s) {sorted(unknown)}; use mode, quiet_hours")
        mode = str(value.get("mode", "leave")).strip().lower()
        quiet = value.get("quiet_hours")
    else:
        raise KnobError(f"presence: {value!r} is not a mode or a mapping")
    if mode not in PRESENCE_MODES:
        raise KnobError(f"presence: unknown mode {mode!r}; use one of {', '.join(PRESENCE_MODES)}")
    return Presence(mode=mode, quiet_hours=QuietHours.parse(quiet) if quiet else None)


@dataclass(frozen=True)
class ActionKnobs:
    """The resolved knobs for one action, job defaults already applied.

    The defaults reproduce what a job did before the knobs existed: act at
    once, every time, without touching presence or the dry-run switch.
    """

    delay: tuple[float, float] = (0.0, 0.0)
    percent: int = 100
    presence: Presence = field(default_factory=Presence)
    on_takeover: str = "cancel"
    dry_run: bool = False


def parse_knobs(raw: dict[str, Any], *, where: str = "") -> dict[str, Any]:
    """The knob keys present in *raw*, parsed. Absent keys stay absent.

    Partial on purpose: a job's knobs are defaults for its actions, so "the
    action did not say" must stay distinguishable from "the action said the
    default".
    """
    prefix = f"{where}: " if where else ""
    out: dict[str, Any] = {}
    try:
        if "delay" in raw:
            out["delay"] = parse_delay(raw["delay"])
        if "percent" in raw:
            out["percent"] = parse_percent(raw["percent"])
        if "presence" in raw:
            out["presence"] = parse_presence(raw["presence"])
        if "on_takeover" in raw:
            mode = str(raw["on_takeover"]).strip().lower()
            if mode not in TAKEOVER_MODES:
                raise KnobError(
                    f"on_takeover: unknown mode {raw['on_takeover']!r}; "
                    f"use one of {', '.join(TAKEOVER_MODES)}"
                )
            out["on_takeover"] = mode
        if "dry_run" in raw:
            if not isinstance(raw["dry_run"], bool):
                raise KnobError(f"dry_run: {raw['dry_run']!r} is not true or false")
            out["dry_run"] = raw["dry_run"]
    except KnobError as exc:
        raise KnobError(f"{prefix}{exc}") from None
    return out


def merge_knobs(job: dict[str, Any], action: dict[str, Any]) -> ActionKnobs:
    """Job knobs as defaults, the action's own knobs on top."""
    merged = {**job, **action}
    return ActionKnobs(**merged)
