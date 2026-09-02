"""Every timestamp and duration tlgr reads or writes goes through here.

v1 called `str(datetime)` in a dozen places and produced
`"2025-03-06 12:00:00+00:00"` — a format nothing parses without a custom rule
(COR-35). v2 emits RFC-3339 UTC with a `Z`, ships a `*_unix` sibling for
anything an agent is likely to compare, and renders local time only in human
tables. There is one function per direction, and no other module formats a
date.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

# `datetime.UTC` is 3.11+; we support 3.10.
UTC = timezone.utc

__all__ = [
    "fmt_dt",
    "fmt_local",
    "legacy_dates_enabled",
    "parse_dt",
    "parse_duration",
    "to_unix",
]

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$", re.IGNORECASE)
_RELATIVE_RE = re.compile(r"^([+-])(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

FOREVER = frozenset({"forever", "never", "none", "unlimited"})


class TimeFormatError(ValueError):
    """A duration or timestamp that cannot be understood.

    A plain ValueError subclass so `core.timefmt` keeps its dependencies to
    the standard library; `core.errors.classify` maps it to USAGE.
    """


# ---------------------------------------------------------------------------
# Out
# ---------------------------------------------------------------------------


def fmt_dt(value: datetime | None, *, legacy: bool = False) -> str | None:
    """RFC-3339 UTC with a `Z` suffix, or None.

    *legacy* restores v1's `str(datetime)` spelling for the one minor release
    that `[defaults] legacy_dates` covers (§12.4).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        # Telethon hands back aware datetimes; anything naive reaching here is
        # a local wall-clock value, and guessing UTC would silently shift it.
        value = value.astimezone()
    utc = value.astimezone(UTC)
    if legacy:
        return str(utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_unix(value: datetime | None) -> int | None:
    """Seconds since the epoch, for the `*_unix` sibling of every timestamp."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return int(value.timestamp())


def legacy_dates_enabled() -> bool:
    """`[defaults] legacy_dates` — v1 date spelling, for one minor release."""
    try:
        from tlgr.core.config import load_app_config

        return bool(getattr(load_app_config().defaults, "legacy_dates", False))
    except Exception:
        return False


def fmt_local(value: str | datetime | None, *, now: datetime | None = None) -> str:
    """Human rendering: local time, precision by recency.

    `today 14:02` for today, `Mon 09:14` inside the last week, and the full
    `2026-09-02 09:14` beyond that — a table of timestamps is read for "when
    relative to me", not for the year.
    """
    if value is None:
        return "-"
    dt = parse_dt(value) if isinstance(value, str) else value
    if dt is None:
        return "-"
    local = dt.astimezone()
    reference = (now or datetime.now(UTC)).astimezone()
    delta = reference.date() - local.date()
    if delta.days == 0:
        return f"today {local:%H:%M}"
    if delta.days == 1:
        return f"yesterday {local:%H:%M}"
    if 0 < delta.days < 7:
        return f"{local:%a %H:%M}"
    return f"{local:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# In
# ---------------------------------------------------------------------------


def parse_dt(text: str | datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Parse RFC-3339, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM` or a relative offset.

    Naive values are read in the **local** zone and converted to UTC: a user
    typing `2026-09-02 09:00` means nine in their own morning, and reading it
    as UTC silently moves the appointment (COR-23).
    """
    if text is None:
        return None
    if isinstance(text, datetime):
        return text if text.tzinfo else text.astimezone()

    raw = text.strip()
    if not raw:
        return None
    reference = now or datetime.now(UTC)

    low = raw.lower()
    if low == "now":
        return reference
    if low in ("today", "midnight"):
        local = reference.astimezone()
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    relative = _RELATIVE_RE.match(raw)
    if relative:
        sign, amount, unit = relative.groups()
        seconds = float(amount) * _UNIT_SECONDS[unit.lower()]
        return reference + timedelta(seconds=seconds if sign == "+" else -seconds)

    if raw.isdigit() and len(raw) >= 9:
        # A bare unix timestamp; 9 digits keeps `20260902` out of this branch.
        return datetime.fromtimestamp(int(raw), tz=UTC)

    candidate = raw.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError as exc:
            raise TimeFormatError(
                f"{text!r} is not a timestamp: expected RFC-3339, YYYY-MM-DD, "
                "or a relative offset such as -90m or +3d"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def parse_duration(text: str | int | None) -> int | None:
    """`30s 5m 2h 7d 1w forever` → seconds, or None for "forever".

    `0` is legal and means "now/immediately"; None means "no end", which is
    why the return type cannot simply be an int.
    """
    if text is None:
        return None
    if isinstance(text, int):
        return text
    raw = text.strip().lower()
    if not raw:
        return None
    if raw in FOREVER:
        return None
    match = _DURATION_RE.match(raw)
    if not match:
        raise TimeFormatError(
            f"{text!r} is not a duration: expected 30s, 5m, 2h, 7d, 1w or 'forever'"
        )
    amount, unit = match.groups()
    return int(float(amount) * _UNIT_SECONDS.get(unit or "s", 1))
