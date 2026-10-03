"""Raw TL updates → the high-level Telethon events filters read.

The daemon's bus carries each raw TL `Update*` beside the normalised
envelope, because one `events.Raw()` handler sees every constructor while
Telethon's high-level builders drop service messages, topic ids and the
updates they do not model. Every filter in `tlgr/filters/` was written
against those high-level events, though: `event.chat_id`, `is_private`,
`event.message.sender_id`, `reply()`. Handing them the raw update made jobs
and webhook filters fail or never match.

`build_event` closes that gap the way Telethon's own `_dispatch_update`
does (its `EventBuilderDict`): build with the builder, attach the update's
entities and the client, then apply the builder's filter. Gateway jobs and
the webhook pusher both go through it, so a filter behaves the same in a
job, in a webhook, and in the client-handler fallback.
"""

from __future__ import annotations

import inspect
from typing import Any

from telethon import events

__all__ = ["build_event", "builder_for_bus_type", "builder_for_job_event"]

#: The v1 job event names (`jobs.yaml` `events:`) and their builders.
#: `new_message` means incoming only, as it always has for jobs: an
#: auto-reply must never answer the account's own messages.
_JOB_BUILDERS: dict[str, tuple[type, dict[str, Any]]] = {
    "new_message": (events.NewMessage, {"incoming": True}),
    "message_edited": (events.MessageEdited, {}),
    "message_deleted": (events.MessageDeleted, {}),
    "chat_action": (events.ChatAction, {}),
    "user_joined": (events.UserUpdate, {}),
    "message_read": (events.MessageRead, {}),
}

#: Bus event types (the v2 taxonomy) that have a high-level builder. A
#: webhook reports both directions, so `message_new` is not incoming-only
#: here; `is_incoming` / `sender_is_self` filter on direction.
_BUS_BUILDERS: dict[str, tuple[type, dict[str, Any]]] = {
    "message_new": (events.NewMessage, {}),
    "message_edited": (events.MessageEdited, {}),
    "message_deleted": (events.MessageDeleted, {}),
    "message_service": (events.ChatAction, {}),
    "member_chat": (events.ChatAction, {}),
    "member_channel": (events.ChatAction, {}),
    "read_inbox": (events.MessageRead, {"inbox": True}),
    "read_outbox": (events.MessageRead, {"inbox": False}),
    "user_status": (events.UserUpdate, {}),
}


def builder_for_job_event(name: str) -> Any | None:
    """A fresh builder for a `jobs.yaml` event name, or None if unknown."""
    entry = _JOB_BUILDERS.get(name)
    if entry is None:
        return None
    cls, kwargs = entry
    return cls(**kwargs)


def builder_for_bus_type(event_type: str) -> Any | None:
    """A fresh builder for a bus event type, or None if Telethon models none."""
    entry = _BUS_BUILDERS.get(event_type)
    if entry is None:
        return None
    cls, kwargs = entry
    return cls(**kwargs)


async def build_event(
    builders: list[tuple[str, Any]], update: Any, client: Any
) -> tuple[Any, str] | None:
    """The first `(event, label)` a builder makes of *update* and accepts.

    *builders* pairs a label with a builder; the label comes back so the
    caller knows which one matched. None means no builder modelled the
    update, or its own filter (`incoming=True`, `inbox=`) declined it.
    """
    self_id = getattr(client, "_self_id", None)
    for label, builder in builders:
        event = builder.build(update, None, self_id)
        if not event:
            continue
        if isinstance(event, events.common.EventCommon):
            event.original_update = update
            event._entities = getattr(update, "_entities", None) or {}
            event._set_client(client)
        if not builder.resolved:
            await builder.resolve(client)
        passed = builder.filter(event)
        if inspect.isawaitable(passed):
            passed = await passed
        if passed:
            return event, label
    return None
