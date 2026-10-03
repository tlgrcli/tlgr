"""Filters about the relationship with the other side: contact, first contact.

Both may need Telegram. `sender_is_contact` usually reads the `contact` flag
off the sender entity the update already carried, and only fetches the user
when the update came without one. `chat_is_new` cannot be answered from the
message at all: message ids are numbered per account, not per chat, so id 1
in a dialog does not exist and "no earlier message" takes a history probe.
Both answers are cached per account, so a busy chat costs one probe, not one
per message.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from tlgr.filters import register_filter
from tlgr.gateway.event import Event

#: A contact flag is trusted this long before the sender is fetched again.
CONTACT_TTL_S = 600.0
_CACHE_SIZE = 5000

#: `(account, user id) -> (is a contact, when we learned it)`.
_contacts: OrderedDict[tuple[str, int], tuple[bool, float]] = OrderedDict()
#: `(account, chat id) -> the id of the first message, or 0 for "not new"`.
_first_message: OrderedDict[tuple[str, int], int] = OrderedDict()


def _remember(cache: OrderedDict[Any, Any], key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_SIZE:
        cache.popitem(last=False)


def clear_caches() -> None:
    _contacts.clear()
    _first_message.clear()


def _is_user(entity: Any) -> bool:
    return type(entity).__name__ == "User"


@register_filter("sender_is_contact")
async def filter_sender_is_contact(event: Event, value: Any) -> tuple[bool, str]:
    """The sender is (or, with `false`, is not) in the account's contacts."""
    if event.source != "telegram":
        return False, "sender_is_contact requires telegram source"
    message = event.raw.message
    sender_id = getattr(message, "sender_id", None)
    if sender_id is None or sender_id < 0:
        # A channel post or an anonymous admin: no user, so no contact.
        contact = False
    else:
        key = (event.account, int(sender_id))
        sender = getattr(message, "sender", None)
        cached = _contacts.get(key)
        if _is_user(sender) and not getattr(sender, "min", False):
            contact = bool(getattr(sender, "contact", False))
            _remember(_contacts, key, (contact, time.monotonic()))
        elif cached is not None and time.monotonic() - cached[1] < CONTACT_TTL_S:
            contact = cached[0]
        else:
            try:
                sender = await message.get_sender()
            except Exception as exc:
                return False, f"sender_is_contact: could not fetch the sender ({exc})"
            contact = bool(getattr(sender, "contact", False)) if _is_user(sender) else False
            _remember(_contacts, key, (contact, time.monotonic()))
    if contact == bool(value):
        return True, f"sender_is_contact={contact}"
    return False, f"sender_is_contact={contact}, expected {value}"


@register_filter("chat_is_new")
async def filter_chat_is_new(event: Event, value: Any) -> tuple[bool, str]:
    """In a private chat, this message is the first one the dialog has ever had."""
    if event.source != "telegram":
        return False, "chat_is_new requires telegram source"
    tg = event.raw
    message = tg.message
    if not getattr(tg, "is_private", False):
        is_new = False
    else:
        key = (event.account, int(tg.chat_id))
        first = _first_message.get(key)
        if first is None:
            client = getattr(tg, "client", None)
            if client is None:
                return False, "chat_is_new: no client to probe the history with"
            try:
                peer = getattr(tg, "input_chat", None) or tg.chat_id
                older = await client.get_messages(peer, limit=1, offset_id=int(message.id))
            except Exception as exc:
                return False, f"chat_is_new: the history probe failed ({exc})"
            first = 0 if older else int(message.id)
            _remember(_first_message, key, first)
        is_new = first != 0 and int(message.id) == first
    if is_new == bool(value):
        return True, f"chat_is_new={is_new}"
    return False, f"chat_is_new={is_new}, expected {value}"
