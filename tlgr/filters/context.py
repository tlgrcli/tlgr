"""Event-context filters — chat type, direction, sender metadata."""

from __future__ import annotations

import re
from typing import Any

from tlgr.filters import register_filter
from tlgr.gateway.event import Event


def _tg_event(event: Event):
    """Return the raw Telethon event, or *None* if the source is not Telegram."""
    if event.source != "telegram":
        return None
    return event.raw


@register_filter("chat_type")
def filter_chat_type(event: Event, value: Any) -> tuple[bool, str]:
    """Match chat type: private, group, supergroup, channel, bot."""
    tg = _tg_event(event)
    if tg is None:
        return False, "chat_type requires telegram source"

    expected = value if isinstance(value, list) else [value]

    if tg.is_private:
        actual = "private"
    elif tg.is_group:
        actual = "group"
    elif tg.is_channel:
        actual = "channel"
    else:
        actual = "unknown"

    # Telethon doesn't expose supergroup directly on the event; check the chat entity.
    if actual == "group" and hasattr(tg, "chat") and hasattr(tg.chat, "megagroup"):
        if tg.chat.megagroup:
            actual = "supergroup"

    if actual in expected:
        return True, f"chat_type={actual}"
    return False, f"chat_type {actual} not in {expected}"


@register_filter("chat_id")
def filter_chat_id(event: Event, value: Any) -> tuple[bool, str]:
    """Match by chat ID or @username.  Value may be a single ref or a list."""
    tg = _tg_event(event)
    if tg is None:
        return False, "chat_id requires telegram source"

    refs = value if isinstance(value, list) else [value]
    chat_id = tg.chat_id

    for ref in refs:
        try:
            if int(ref) == chat_id:
                return True, f"chat_id matched {ref}"
        except (ValueError, TypeError):
            pass
        # @username matching requires entity resolution at setup time;
        # the gateway pre-resolves these into the filter value.
        if isinstance(ref, str) and ref.startswith("@"):
            continue

    return False, f"chat_id {chat_id} not in {refs}"


@register_filter("chat_title")
def filter_chat_title(event: Event, value: Any) -> tuple[bool, str]:
    """Regex match against the chat title."""
    tg = _tg_event(event)
    if tg is None:
        return False, "chat_title requires telegram source"
    title = getattr(tg.chat, "title", "") or ""
    if re.search(str(value), title, re.IGNORECASE):
        return True, "chat_title matched"
    return False, f"chat_title '{title}' does not match '{value}'"


@register_filter("is_incoming")
def filter_is_incoming(event: Event, value: Any) -> tuple[bool, str]:
    tg = _tg_event(event)
    if tg is None:
        return False, "is_incoming requires telegram source"
    incoming = not tg.message.out
    if incoming == bool(value):
        return True, "direction matched"
    return False, f"is_incoming={incoming}, expected {value}"


@register_filter("sender_is_bot")
def filter_sender_is_bot(event: Event, value: Any) -> tuple[bool, str]:
    tg = _tg_event(event)
    if tg is None:
        return False, "sender_is_bot requires telegram source"
    sender = tg.message.sender
    is_bot = getattr(sender, "bot", False) if sender else False
    if is_bot == bool(value):
        return True, "bot check matched"
    return False, f"sender_is_bot={is_bot}, expected {value}"


@register_filter("sender_is_self")
def filter_sender_is_self(event: Event, value: Any) -> tuple[bool, str]:
    tg = _tg_event(event)
    if tg is None:
        return False, "sender_is_self requires telegram source"
    is_self = tg.message.out
    if is_self == bool(value):
        return True, "self check matched"
    return False, f"sender_is_self={is_self}, expected {value}"
