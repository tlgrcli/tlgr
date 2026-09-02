"""The event bus: one normalised stream per account, many consumers.

The shape of this module is dictated by one fact from ROB-02: with
`sequential_updates=True` a slow consumer stalls **every** account. v1's
webhook pusher did its HTTP POST inside the Telethon handler, with retries and
`asyncio.sleep` backoff, so one unreachable endpoint held the update loop for
up to 97 seconds and every account went deaf for that long.

So the Telethon handler here does exactly three things and then returns:

1. normalise the update into an `EventEnvelope` (models, never `to_dict()` —
   COR-07: a `datetime` or a `bytes` in a payload is a serialisation crash at
   delivery time, far away from the cause);
2. assign the account's next `seq`, which is monotonic and persisted so that
   `--since` survives a daemon restart;
3. `put_nowait` into the ring buffer, into each stream subscriber's bounded
   queue, and into one of the worker lanes.

Everything expensive — webhooks, gateway jobs — happens on a worker lane.
Lanes are chosen by `chat_id`, so per-chat order is preserved (a message and
its edit cannot be processed out of order) while different chats run
concurrently. A subscriber that cannot keep up is told so with a `lag` frame
and loses its own oldest events; the bus never blocks and no other consumer
is affected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tlgr.core.paths import write_private
from tlgr.models.event import EventEnvelope

log = logging.getLogger("tlgr.daemon.events")

__all__ = [
    "EVENT_TYPES",
    "EventBus",
    "Subscriber",
    "normalise",
]

#: The starter taxonomy (§3.7). The full vocabulary lands in PR-4; every name
#: is a lowercase snake_case noun-verb and new ones are additive.
EVENT_TYPES: tuple[str, ...] = (
    "message_new",
    "message_edited",
    "message_deleted",
    "message_read",
    "chat_action",
    "user_status",
    "reaction_changed",
    "draft_changed",
    "daemon_health",
)

_HEARTBEAT_SECONDS = 15.0
_STATE_FLUSH_SECONDS = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------


@dataclass
class Subscriber:
    """One consumer's bounded view of the bus.

    `dropped` is not a statistic, it is a promise: a consumer that fell behind
    is told exactly how many events it lost rather than silently skipping
    them, which is the difference between a gap you can recover from and one
    you never learn about.
    """

    account: str
    types: frozenset[str] = frozenset()
    chats: frozenset[int] = frozenset()
    maxsize: int = 1024
    queue: asyncio.Queue[EventEnvelope] = field(init=False)
    dropped: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.maxsize)

    def wants(self, event: EventEnvelope) -> bool:
        if self.account and event.account != self.account:
            return False
        if self.types and event.type not in self.types:
            return False
        return not (self.chats and (event.chat_id is None or event.chat_id not in self.chats))

    def offer(self, event: EventEnvelope) -> None:
        """Never blocks. Drops the oldest event when full and counts it."""
        if self.closed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)

    def take_lag(self) -> int:
        lag, self.dropped = self.dropped, 0
        return lag

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _message_payload(message: Any, chat_id: int | None) -> dict[str, Any]:
    from tlgr.models.base import to_builtins
    from tlgr.ops._serialize import message_to_model

    model = message_to_model(message, chat_id=chat_id)
    payload = to_builtins(model)
    return payload if isinstance(payload, dict) else {}


def normalise(
    account: str, event: Any
) -> tuple[str, dict[str, Any], int | None, int | None] | None:
    """Map one Telethon event onto `(type, payload, chat_id, sender_id)`.

    Returns None for an update tlgr has no name for yet; the full taxonomy is
    PR-4's job and the bus must not invent type names in the meantime.
    """
    name = type(event).__name__
    chat_id = getattr(event, "chat_id", None)
    if chat_id is not None:
        with contextlib.suppress(TypeError, ValueError):
            chat_id = int(chat_id)

    if name in ("NewMessage.Event", "Event") and hasattr(event, "message"):
        name = "NewMessage.Event"

    kind = _event_kind(event)
    if kind is None:
        return None

    if kind in ("message_new", "message_edited"):
        message = getattr(event, "message", None)
        payload = _message_payload(message, chat_id) if message is not None else {}
        return kind, payload, chat_id, payload.get("sender_id")

    if kind == "message_deleted":
        ids = list(getattr(event, "deleted_ids", None) or [])
        return kind, {"message_ids": ids}, chat_id, None

    if kind == "message_read":
        return (
            kind,
            {
                "max_id": getattr(event, "max_id", None),
                "outbox": bool(getattr(event, "outbox", False)),
            },
            chat_id,
            None,
        )

    if kind == "chat_action":
        action_message = getattr(event, "action_message", None)
        action = type(getattr(action_message, "action", None)).__name__ if action_message else ""
        return (
            kind,
            {
                "action": action,
                "user_id": getattr(event, "user_id", None),
                "user_ids": list(getattr(event, "user_ids", None) or []),
            },
            chat_id,
            getattr(event, "user_id", None),
        )

    if kind == "user_status":
        status = getattr(event, "status", None)
        return (
            kind,
            {
                "user_id": getattr(event, "user_id", None),
                "status": type(status).__name__ if status is not None else None,
                "online": bool(getattr(event, "online", False)),
            },
            chat_id,
            getattr(event, "user_id", None),
        )

    return None


def _event_kind(event: Any) -> str | None:
    """The tlgr type name for a Telethon event object.

    Matched on the qualified class name rather than by `isinstance`, so this
    module — and therefore the bus — does not import Telethon at all and can
    be unit-tested with a fake event.
    """
    qualname = f"{type(event).__module__}.{type(event).__qualname__}"
    for needle, kind in (
        ("newmessage", "message_new"),
        ("messageedited", "message_edited"),
        ("messagedeleted", "message_deleted"),
        ("messageread", "message_read"),
        ("chataction", "chat_action"),
        ("userupdate", "user_status"),
    ):
        if needle in qualname.lower():
            return kind
    explicit = getattr(event, "tlgr_type", None)
    return str(explicit) if explicit else None


# ---------------------------------------------------------------------------
# The bus
# ---------------------------------------------------------------------------


Handler = Callable[[EventEnvelope], Awaitable[None]]


class EventBus:
    def __init__(
        self,
        *,
        state_dir_for: Callable[[str], Path] | None = None,
        buffer_size: int = 4096,
        workers: int = 8,
        lane_queue_size: int = 512,
    ) -> None:
        self._state_path = state_dir_for
        self.buffer_size = max(16, buffer_size)
        self.workers = max(1, workers)
        self.lane_queue_size = max(16, lane_queue_size)
        self._seq: dict[str, int] = {}
        self._buffers: dict[str, deque[EventEnvelope]] = {}
        self._subscribers: list[Subscriber] = []
        self._handlers: list[Handler] = []
        self._lanes: list[asyncio.Queue[EventEnvelope]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._dirty: set[str] = set()
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._lanes = [asyncio.Queue(maxsize=self.lane_queue_size) for _ in range(self.workers)]
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"tlgr-event-lane-{index}")
            for index in range(self.workers)
        ]
        self._flush_task = asyncio.create_task(self._flush_loop(), name="tlgr-event-seq-flush")

    async def stop(self) -> None:
        self._running = False
        for task in [*self._tasks, self._flush_task]:
            if task is None:
                continue
            task.cancel()
        for task in [*self._tasks, self._flush_task]:
            if task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._flush_task = None
        for subscriber in list(self._subscribers):
            subscriber.close()
        self.flush_state()

    # -- sequence numbers --------------------------------------------------

    def _state_file(self, account: str) -> Path | None:
        return self._state_path(account) if self._state_path else None

    def load_seq(self, account: str) -> int:
        if account in self._seq:
            return self._seq[account]
        path = self._state_file(account)
        value = 0
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text())
                value = int(raw.get("seq", 0)) if isinstance(raw, dict) else int(raw)
            except (OSError, ValueError, json.JSONDecodeError):
                value = 0
        self._seq[account] = value
        return value

    def flush_state(self) -> None:
        """Persist every dirty account's `seq`. Cheap, and idempotent."""
        for account in list(self._dirty):
            path = self._state_file(account)
            self._dirty.discard(account)
            if path is None:
                continue
            with contextlib.suppress(OSError):
                write_private(path, json.dumps({"seq": self._seq.get(account, 0)}))

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_STATE_FLUSH_SECONDS)
            self.flush_state()

    # -- publishing --------------------------------------------------------

    def emit(
        self,
        account: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        chat_id: int | None = None,
        sender_id: int | None = None,
        self_origin: bool = False,
    ) -> EventEnvelope:
        """Build, number, buffer and fan out one event. Never blocks."""
        seq = self.load_seq(account) + 1
        self._seq[account] = seq
        self._dirty.add(account)
        envelope = EventEnvelope(
            seq=seq,
            ts=_now(),
            account=account,
            type=event_type,
            payload=payload or {},
            chat_id=chat_id,
            sender_id=sender_id,
            self_origin=self_origin,
        )
        self.publish(envelope)
        return envelope

    def publish(self, envelope: EventEnvelope) -> None:
        buffer = self._buffers.setdefault(envelope.account, deque(maxlen=self.buffer_size))
        buffer.append(envelope)

        for subscriber in self._subscribers:
            if subscriber.closed:
                continue
            if subscriber.wants(envelope):
                subscriber.offer(envelope)

        if self._lanes and self._handlers:
            lane = self._lane_for(envelope)
            try:
                lane.put_nowait(envelope)
            except asyncio.QueueFull:
                # The lane is the *handlers'* backlog. Dropping the oldest
                # keeps the update loop moving; the stream subscribers and the
                # ring buffer above still have the event.
                with contextlib.suppress(asyncio.QueueEmpty):
                    lane.get_nowait()
                log.warning(
                    "event worker lane is full; dropped the oldest event",
                    extra={"account": envelope.account, "seq": envelope.seq},
                )
                with contextlib.suppress(asyncio.QueueFull):
                    lane.put_nowait(envelope)

    def _lane_for(self, envelope: EventEnvelope) -> asyncio.Queue[EventEnvelope]:
        key = envelope.chat_id if envelope.chat_id is not None else hash(envelope.account)
        return self._lanes[abs(int(key)) % len(self._lanes)]

    async def _worker(self, index: int) -> None:
        lane = self._lanes[index]
        while True:
            envelope = await lane.get()
            for handler in list(self._handlers):
                try:
                    await handler(envelope)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "event handler failed",
                        extra={"account": envelope.account, "seq": envelope.seq},
                    )

    # -- consumers ---------------------------------------------------------

    def add_handler(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def remove_handler(self, handler: Handler) -> None:
        with contextlib.suppress(ValueError):
            self._handlers.remove(handler)

    def subscribe(
        self,
        account: str,
        *,
        types: Iterable[str] = (),
        chats: Iterable[int] = (),
        maxsize: int = 1024,
    ) -> Subscriber:
        subscriber = Subscriber(
            account=account,
            types=frozenset(types),
            chats=frozenset(int(c) for c in chats),
            maxsize=maxsize,
        )
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        subscriber.close()
        with contextlib.suppress(ValueError):
            self._subscribers.remove(subscriber)

    # -- replay ------------------------------------------------------------

    def replay(
        self, account: str, since: int | None
    ) -> tuple[list[EventEnvelope], dict[str, Any] | None]:
        """Events after *since*, plus a `gap` frame when some are already lost.

        A consumer that asks for events after 91,820 when the buffer starts at
        95,000 has missed 3,180 of them. Returning the newest 4,096 with no
        signal would be a silent lie; the gap frame is the honest answer and
        the consumer decides what to do about it.
        """
        buffer = self._buffers.get(account)
        if since is None or buffer is None or not buffer:
            return (list(buffer or ()), None) if since is not None else ([], None)

        oldest = buffer[0].seq
        gap: dict[str, Any] | None = None
        if since + 1 < oldest:
            gap = {
                "type": "gap",
                "from": oldest,
                "requested": since,
                "lost": oldest - since - 1,
            }
        return [event for event in buffer if event.seq > since], gap

    def latest_seq(self, account: str) -> int:
        return self.load_seq(account)

    def buffered(self, account: str) -> int:
        return len(self._buffers.get(account, ()))


async def heartbeat_ticker(interval: float = _HEARTBEAT_SECONDS) -> Any:
    """The `/v1/events` heartbeat clock, factored out so a test can shorten it."""
    await asyncio.sleep(interval)
    return {"type": "heartbeat", "ts": _now()}


def heartbeat_frame() -> dict[str, Any]:
    return {"type": "heartbeat", "ts": _now()}


def now_iso() -> str:
    return _now()


def monotonic() -> float:
    return time.monotonic()
