"""What a built-in job action is: plan at event time, execute when due.

An action is split in two because the two halves happen at different times,
possibly in different processes:

* `plan` runs on the bus lane when the event arrives. It reads the Telethon
  event (through `MessageFacts`), applies processors, picks the emoji, and
  returns plain payloads. It must not await anything slow; it does not await
  at all.
* `execute` runs when the item is due and its pacer slot is open, maybe after
  a restart, from nothing but the persisted `PendingItem`. It calls
  operations through the runtime (`rt.op("reaction.add", ...)`), never
  Telethon.

The class attributes tell the scheduler how to treat the action: which pacer
queue it uses, when it expires, whether quiet hours hold it, which takeover
modes cancel it, and how its items coalesce.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Protocol

from tlgr.gateway.knobs import KnobError
from tlgr.processors import ProcessorChain

__all__ = ["Action", "ActionError", "MessageFacts", "Outcome", "Runtime", "facts_from_event"]

HOUR = 3600.0


class ActionError(KnobError):
    """An action config that cannot work. The message names the key."""


@dataclass
class Outcome:
    """What happened to a batch: `done`, `skipped`, or `later` (re-queued at `due_at`)."""

    status: str = "done"
    due_at: float | None = None


@dataclass
class MessageFacts:
    """What the actions need from a message, read once from the Telethon event."""

    chat_id: int
    msg_id: int
    peer: str = "user"
    topic_id: int | None = None
    grouped_id: int | None = None
    #: `message.text`: markdown when the client has a parse mode, so a
    #: processed re-send keeps its formatting the way the old action did.
    text: str = ""
    #: `photo`, `document`, `webpage`, `other`, or None.
    media: str | None = None
    voice_or_round: bool = False
    view_once: bool = False
    service: bool = False


class Runtime(Protocol):
    """The scheduler services an executing action may use."""

    rng: random.Random

    def now(self) -> float: ...

    async def op(self, op: str, request: dict[str, Any]) -> Any: ...

    async def ensure_read(self, item: Any) -> None: ...

    def read_mark(self, chat_id: int, topic_id: int | None) -> int: ...

    def begin_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None: ...

    def note_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None: ...

    def begin_send(self, chat_id: int) -> None: ...

    def note_sent(self, chat_id: int, ids: list[int]) -> None: ...

    def spawn(self, coro: Any, *, what: str) -> None: ...


class Action:
    """One built-in action kind. Subclasses set the attributes and the hooks."""

    name: str = ""
    #: Seconds after the event arrived when a pending item stops being worth
    #: doing; None means never. Overridable per account in `pacing.expire`.
    default_expiry: float | None = None
    #: Quiet hours hold this action until the window ends.
    quiet_hold: bool = True
    #: The `on_takeover` modes that cancel a pending item of this kind.
    cancelled_by: frozenset[str] = frozenset()
    #: One action per album (the official apps attach album reactions to one
    #: message), instead of one per message of the group.
    album_once: bool = False
    accepts_processors: bool = False

    def parse(self, config: Any) -> dict[str, Any]:
        """Validate the action's own config (knobs already removed)."""
        return {}

    def plan(
        self,
        facts: MessageFacts,
        params: dict[str, Any],
        chain: ProcessorChain | None,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        """Payloads to schedule; an empty list means there is nothing to do."""
        return [{}]

    def extra_delay(self, facts: MessageFacts, params: dict[str, Any]) -> float:
        """Seconds added on top of the `delay` knob."""
        return 0.0

    def key_extra(self, payload: dict[str, Any]) -> str:
        """What distinguishes two payloads planned from one message."""
        return ""

    def batch_key(self, item: Any) -> tuple[Any, ...] | None:
        """Items with the same key that are due together run as one call."""
        return None

    async def execute(self, items: list[Any], rt: Runtime) -> Outcome:
        raise NotImplementedError

    def describe(self, items: list[Any]) -> str:
        first = items[0]
        return f"{self.name} chat {first.chat_id} msg {first.msg_id}"


def _peer_kind(tg_event: Any, message: Any) -> str:
    peer = getattr(message, "peer_id", None)
    kind = type(peer).__name__
    if kind == "PeerUser":
        return "user"
    if kind == "PeerChat":
        return "chat"
    if getattr(message, "post", False):
        return "channel"
    chat = getattr(tg_event, "chat", None)
    if getattr(chat, "broadcast", False):
        return "channel"
    return "megagroup"


def _topic_of(message: Any) -> int | None:
    reply = getattr(message, "reply_to", None)
    if reply is None or not getattr(reply, "forum_topic", False):
        return None
    top = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
    return int(top) if top else None


def _media_kind(media: Any) -> str | None:
    if media is None:
        return None
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return "photo"
    if name == "MessageMediaDocument":
        return "document"
    if name == "MessageMediaWebPage":
        return "webpage"
    if name in ("MessageMediaEmpty", "MessageMediaUnsupported"):
        return None
    return "other"


def _voice_or_round(media: Any) -> bool:
    if type(media).__name__ != "MessageMediaDocument":
        return False
    if getattr(media, "voice", False) or getattr(media, "round", False):
        return True
    document = getattr(media, "document", None)
    for attribute in getattr(document, "attributes", None) or []:
        name = type(attribute).__name__
        if name == "DocumentAttributeAudio" and getattr(attribute, "voice", False):
            return True
        if name == "DocumentAttributeVideo" and getattr(attribute, "round_message", False):
            return True
    return False


def facts_from_event(tg_event: Any) -> MessageFacts:
    message = tg_event.message
    media = getattr(message, "media", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        text = getattr(message, "message", "") or ""
    return MessageFacts(
        chat_id=int(tg_event.chat_id),
        msg_id=int(message.id),
        peer=_peer_kind(tg_event, message),
        topic_id=_topic_of(message),
        grouped_id=getattr(message, "grouped_id", None),
        text=text or "",
        media=_media_kind(media),
        voice_or_round=_voice_or_round(media),
        view_once=bool(getattr(media, "ttl_seconds", None)),
        service=getattr(message, "action", None) is not None,
    )


def check_keys(name: str, config: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(config) - allowed)
    if unknown:
        own = ", ".join(sorted(allowed)) or "none"
        raise ActionError(
            f"{name}: unknown key(s) {unknown}. Its own keys: {own}; every action also "
            "takes filters, delay, percent, presence, on_takeover and dry_run"
        )


def require_bool(name: str, key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ActionError(f"{name}: {key} must be true or false, got {value!r}")
    return value
