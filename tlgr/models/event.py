"""The event envelope, and the shapes that describe the event vocabulary.

The envelope is the wire shape every consumer sees. `EventType` and
`EventTypeDetail` are the *catalogue* shapes: what `tlgr events list` and
`tlgr events get` print so that an agent can discover the subscribable surface
before it opens a stream, rather than learning it from prose.

The vocabulary itself lives in `tlgr/core/eventtypes.py`; only its shape is
here, because `models/` imports nothing from tlgr (§2.2).
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "DecodedEvent",
    "EventEnvelope",
    "EventType",
    "EventTypeDetail",
]


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
    #: The raw TL update, JSON-safe, when the consumer asked for it
    #: (`watch --with-raw`). Absent otherwise: it roughly doubles the frame.
    raw: dict[str, Any] | None = None


class EventType(Model):
    """One row of `tlgr events list`."""

    type: str
    group: str
    summary: str
    #: The `Update*` constructors that produce it.
    sources: list[str] = []
    telethon: str = ""
    #: Which sequence box orders it: pts, qts, seq, channel_pts, version, none.
    box: str = "none"
    bot_only: bool = False
    #: 0 when this build can parse every source; 229 when Telegram has the
    #: constructor and Telethon 1.44 does not.
    since_layer: int = 0
    available: bool = True
    derived: str = ""


class EventTypeDetail(EventType):
    """`tlgr events get <type>`: the row, plus the payload and an example."""

    payload: dict[str, str] = {}
    json_schema: dict[str, Any] | None = None
    filters: list[str] = []
    example: EventEnvelope | None = None


class DecodedEvent(Model):
    """`tlgr events decode`: one TL update or push payload, made a tlgr event."""

    event: str
    account: str = ""
    chat_id: int | None = None
    sender_id: int | None = None
    data: dict[str, Any] = {}
    raw: dict[str, Any] | None = None
    push: bool = False
