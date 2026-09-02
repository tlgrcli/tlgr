"""The event envelope.

Only the envelope is fixed here. The `type` vocabulary and per-type payloads
belong to `docs/design/EVENTS.md` and land with the updates group (PR-4); what
this file guarantees is that every event, whatever its type, is addressable by
`seq`, attributable to an account, and cheaply filterable by chat or sender.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = ["EventEnvelope"]


class EventEnvelope(Model):
    seq: int
    ts: str
    account: str
    type: str
    payload: dict[str, Any] = {}
    chat_id: int | None = None
    sender_id: int | None = None
    # True when this event echoes an action tlgr itself performed, so a
    # gateway rule cannot loop by reacting to its own output.
    self_origin: bool = False
