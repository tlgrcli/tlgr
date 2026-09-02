"""Time-based filters — date ranges and time-of-day."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from tlgr.filters import register_filter
from tlgr.gateway.event import Event


def _parse_date(date_str: str) -> datetime:
    date_str = date_str.strip()
    rel = re.match(r"^(\d+)([dwmh])$", date_str.lower())
    if rel:
        val, unit = int(rel.group(1)), rel.group(2)
        now = datetime.now(timezone.utc)
        deltas = {
            "h": timedelta(hours=val),
            "d": timedelta(days=val),
            "w": timedelta(weeks=val),
            "m": timedelta(days=val * 30),
        }
        return now - deltas[unit]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {date_str}")


def _msg_date(event: Event) -> datetime | None:
    if event.source == "telegram":
        return event.raw.message.date
    return event.timestamp


@register_filter("after")
def filter_after(event: Event, value: Any) -> tuple[bool, str]:
    """Message date must be at or after *value*.  Value: date string."""
    msg_date = _msg_date(event)
    if msg_date is None:
        return True, "no date"
    cutoff = _parse_date(str(value))
    a = cutoff.replace(tzinfo=timezone.utc) if cutoff.tzinfo is None else cutoff
    d = msg_date.replace(tzinfo=timezone.utc) if msg_date.tzinfo is None else msg_date
    if d >= a:
        return True, "after cutoff"
    return False, "before cutoff"


@register_filter("before")
def filter_before(event: Event, value: Any) -> tuple[bool, str]:
    """Message date must be at or before *value*.  Value: date string."""
    msg_date = _msg_date(event)
    if msg_date is None:
        return True, "no date"
    cutoff = _parse_date(str(value))
    b = cutoff.replace(tzinfo=timezone.utc) if cutoff.tzinfo is None else cutoff
    d = msg_date.replace(tzinfo=timezone.utc) if msg_date.tzinfo is None else msg_date
    if d <= b:
        return True, "before cutoff"
    return False, "after cutoff"


@register_filter("time_of_day")
def filter_time_of_day(event: Event, value: Any) -> tuple[bool, str]:
    """Message time must fall within a range.  Value: ``"HH:MM-HH:MM"``."""
    msg_date = _msg_date(event)
    if msg_date is None:
        return True, "no date"
    d = msg_date.replace(tzinfo=timezone.utc) if msg_date.tzinfo is None else msg_date
    current = d.strftime("%H:%M")

    if isinstance(value, str) and "-" in value:
        start, end = value.split("-", 1)
        start, end = start.strip(), end.strip()
        if start <= end:
            ok = start <= current <= end
        else:
            ok = current >= start or current <= end
        if ok:
            return True, "time_of_day matched"
        return False, f"time {current} not in {value}"
    return False, "time_of_day expects 'HH:MM-HH:MM'"
