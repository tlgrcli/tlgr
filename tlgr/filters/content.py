"""Content-based filters — keyword matching, regex, links."""

from __future__ import annotations

import re
from typing import Any

from tlgr.filters import register_filter
from tlgr.gateway.event import Event


def _text(event: Event) -> str:
    if event.source == "telegram":
        msg = event.raw.message
        return (msg.text or getattr(msg, "message", "") or "").lower()
    raw = event.raw
    if isinstance(raw, dict):
        return str(raw.get("text", "")).lower()
    return ""


@register_filter("contains")
def filter_contains(event: Event, value: Any) -> tuple[bool, str]:
    """All keywords must appear (AND).  Value: list[str]."""
    text = _text(event)
    keywords = value if isinstance(value, list) else [value]
    for kw in keywords:
        if str(kw).lower() not in text:
            return False, f"missing keyword: {kw}"
    return True, "all keywords found"


@register_filter("contains_any")
def filter_contains_any(event: Event, value: Any) -> tuple[bool, str]:
    """At least one keyword must appear (OR).  Value: list[str]."""
    text = _text(event)
    keywords = value if isinstance(value, list) else [value]
    if any(str(kw).lower() in text for kw in keywords):
        return True, "keyword matched"
    return False, "none of keywords found"


@register_filter("excludes")
def filter_excludes(event: Event, value: Any) -> tuple[bool, str]:
    """No listed keyword may appear.  Value: list[str]."""
    text = _text(event)
    keywords = value if isinstance(value, list) else [value]
    for kw in keywords:
        if str(kw).lower() in text:
            return False, f"excluded keyword: {kw}"
    return True, "no excluded keywords"


@register_filter("regex")
def filter_regex(event: Event, value: Any) -> tuple[bool, str]:
    """Text must match the regex pattern.  Value: str."""
    if event.source == "telegram":
        msg = event.raw.message
        text = msg.text or getattr(msg, "message", "") or ""
    elif isinstance(event.raw, dict):
        text = str(event.raw.get("text", ""))
    else:
        text = ""
    if re.search(str(value), text, re.IGNORECASE):
        return True, "regex matched"
    return False, "regex not matched"


@register_filter("has_links")
def filter_has_links(event: Event, value: Any) -> tuple[bool, str]:
    """Message must (or must not) contain URL entities.  Value: bool."""
    if event.source != "telegram":
        return False, "has_links requires telegram source"
    msg = event.raw.message
    has = bool(
        msg.entities
        and any(
            hasattr(e, "url")
            or e.__class__.__name__ in ("MessageEntityUrl", "MessageEntityTextUrl")
            for e in msg.entities
        )
    )
    if has == bool(value):
        return True, "links filter matched"
    return False, "links filter"
