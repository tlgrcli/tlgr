"""An in-memory stand-in for `telethon.TelegramClient` (§11.1).

Backed by a small world model rather than by recorded fixtures, so the same
fake serves every group as the surface grows: a test says what exists and what
should go wrong, not which bytes came back in 2026.

Two rules make it worth trusting:

* it returns **real Telethon type objects** (`types.User`, `types.Channel`,
  `types.Message`), so a serialiser tested against it is tested against the
  shapes it will actually meet;
* it **records every request** in `world.calls`, so a test can assert what was
  *sent* — "`mute_until` is within five seconds of now + 3600" is the COR-01
  regression test, and it is not expressible by inspecting the return value.

This is the stage-B core: connection lifecycle, the `disconnected` future,
event registration and dispatch, a raw request table, and a handful of
entities and dialogs. Message operations arrive with the ops that need them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telethon.tl import types

__all__ = ["FakeTelegramClient", "World", "make_channel", "make_message", "make_user"]


# ---------------------------------------------------------------------------
# Builders — real Telethon objects, minimally populated
# ---------------------------------------------------------------------------


def make_user(user_id: int, *, username: str | None = None, first: str = "Test") -> types.User:
    return types.User(
        id=user_id,
        is_self=False,
        contact=False,
        mutual_contact=False,
        deleted=False,
        bot=False,
        verified=False,
        restricted=False,
        min=False,
        first_name=first,
        username=username,
        access_hash=user_id * 7,
    )


def make_channel(
    channel_id: int, *, title: str = "Channel", megagroup: bool = False
) -> types.Channel:
    return types.Channel(
        id=channel_id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(timezone.utc),
        creator=False,
        left=False,
        broadcast=not megagroup,
        megagroup=megagroup,
        access_hash=channel_id * 11,
        username=None,
    )


def make_message(
    message_id: int,
    *,
    chat_id: int = -100123,
    text: str = "",
    sender_id: int | None = None,
    out: bool = False,
    date: datetime | None = None,
) -> types.Message:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(channel_id=abs(chat_id) - 1000000000000)
        if chat_id < -1000000000000
        else types.PeerUser(user_id=abs(chat_id)),
        date=date or datetime.now(timezone.utc),
        message=text,
        out=out,
    )
    if sender_id is not None:
        message.from_id = types.PeerUser(user_id=sender_id)
    return message


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class World:
    """What exists, and what should go wrong."""

    me: types.User = field(
        default_factory=lambda: types.User(
            id=777,
            is_self=True,
            contact=False,
            mutual_contact=False,
            deleted=False,
            bot=False,
            verified=False,
            restricted=False,
            min=False,
            first_name="Me",
            username="me",
            access_hash=1,
        )
    )
    authorized: bool = True
    users: dict[int, types.User] = field(default_factory=dict)
    chats: dict[int, Any] = field(default_factory=dict)
    dialogs: list[Any] = field(default_factory=list)
    messages: dict[int, list[types.Message]] = field(default_factory=dict)
    #: request type name → callable(request) -> result, or a plain value.
    raw: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    # -- behaviour knobs ---------------------------------------------------

    _fail_next: dict[str, BaseException] = field(default_factory=dict)
    latency_ms: float = 0.0
    #: Drop the connection after this many requests. 0 disables.
    disconnect_after: int = 0
    connect_error: BaseException | None = None
    catch_ups: int = 0
    connects: int = 0
    saves: int = 0

    def fail_next(self, request_name: str, exc: BaseException) -> None:
        """Make the next request of that type raise, once."""
        self._fail_next[request_name] = exc

    def flood(self, request_name: str, seconds: int) -> None:
        from telethon.errors import FloodWaitError

        self._fail_next[request_name] = FloodWaitError(types.InputPeerSelf, capture=seconds)

    def add_user(self, user: types.User) -> types.User:
        self.users[user.id] = user
        return user

    def add_channel(self, channel: Any) -> Any:
        self.chats[channel.id] = channel
        return channel

    def called(self, request_name: str) -> list[Any]:
        return [payload for name, payload in self.calls if name == request_name]


class _Dialog:
    """The shape `iter_dialogs` yields, reduced to what tlgr reads."""

    def __init__(self, entity: Any, *, unread: int = 0) -> None:
        self.entity = entity
        self.id = getattr(entity, "id", 0)
        self.name = getattr(entity, "title", None) or getattr(entity, "first_name", "")
        self.unread_count = unread
        self.message = None
        self.archived = False
        self.pinned = False


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class FakeTelegramClient:
    """Everything the daemon touches on a Telethon client, and nothing else."""

    def __init__(self, world: World | None = None, session: Any = None, **_: Any) -> None:
        self.world = world or World()
        self.session = _FakeSession()
        self.session_path = session
        self.flood_sleep_threshold = 120
        self._connected = False
        self._handlers: list[tuple[Any, Any]] = []
        self.disconnected: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._requests = 0

    # -- connection --------------------------------------------------------

    async def connect(self) -> bool:
        if self.world.connect_error is not None:
            error, self.world.connect_error = self.world.connect_error, None
            raise error
        self.world.connects += 1
        self._connected = True
        if self.disconnected.done():
            self.disconnected = asyncio.get_event_loop().create_future()
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._resolve_disconnected()

    def _resolve_disconnected(self) -> None:
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    def drop(self) -> None:
        """Simulate the transport going away without a clean disconnect."""
        self._connected = False
        self._resolve_disconnected()

    def is_connected(self) -> bool:
        return self._connected

    async def is_user_authorized(self) -> bool:
        return self.world.authorized

    async def get_me(self) -> types.User:
        return self.world.me

    async def catch_up(self) -> None:
        self.world.catch_ups += 1

    async def log_out(self) -> bool:
        self.world.authorized = False
        await self.disconnect()
        return True

    async def _save_states_and_entities(self) -> None:
        self.world.saves += 1

    # -- events ------------------------------------------------------------

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self._handlers.append((callback, event))

    def remove_event_handler(self, callback: Any, event: Any = None) -> None:
        self._handlers = [(cb, ev) for cb, ev in self._handlers if cb is not callback]

    def on(self, event: Any) -> Any:  # pragma: no cover - decorator form
        def decorator(callback: Any) -> Any:
            self.add_event_handler(callback, event)
            return callback

        return decorator

    async def feed(self, event: Any) -> None:
        """Deliver *event* to every registered handler, in registration order."""
        for callback, _builder in list(self._handlers):
            result = callback(event)
            if hasattr(result, "__await__"):
                await result

    # -- requests ----------------------------------------------------------

    async def __call__(self, request: Any, ordered: bool = False) -> Any:
        name = type(request).__name__
        self.world.calls.append((name, request))
        self._requests += 1

        failure = self.world._fail_next.pop(name, None)
        if failure is not None:
            raise failure
        if self.world.latency_ms:
            await asyncio.sleep(self.world.latency_ms / 1000.0)
        if self.world.disconnect_after and self._requests >= self.world.disconnect_after:
            self.drop()
            raise ConnectionError("Cannot send requests while disconnected")

        handler = self.world.raw.get(name)
        if callable(handler):
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        if handler is not None:
            return handler
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)

    # -- entities ----------------------------------------------------------

    async def get_entity(self, ref: Any) -> Any:
        entity = self._lookup(ref)
        if entity is None:
            raise ValueError(f"Could not find the input entity for {ref!r}")
        return entity

    async def get_input_entity(self, ref: Any) -> Any:
        entity = await self.get_entity(ref)
        if isinstance(entity, types.User):
            return types.InputPeerUser(entity.id, entity.access_hash or 0)
        if isinstance(entity, types.Channel):
            return types.InputPeerChannel(entity.id, entity.access_hash or 0)
        return types.InputPeerChat(entity.id)

    def _lookup(self, ref: Any) -> Any:
        if ref in ("me", "self"):
            return self.world.me
        if isinstance(ref, str):
            handle = ref.lstrip("@").lower()
            for user in self.world.users.values():
                if (user.username or "").lower() == handle:
                    return user
            for chat in self.world.chats.values():
                if (getattr(chat, "username", None) or "").lower() == handle:
                    return chat
            if ref.lstrip("-").isdigit():
                return self._lookup(int(ref))
            return None
        if isinstance(ref, int):
            raw = abs(ref)
            if ref < -1000000000000:
                raw = -1000000000000 - ref
            return self.world.users.get(raw) or self.world.chats.get(raw)
        return ref

    def iter_dialogs(self, limit: int | None = None, **_: Any) -> Any:
        entities = list(self.world.dialogs) or [
            _Dialog(entity) for entity in self.world.chats.values()
        ]
        if limit is not None:
            entities = entities[:limit]
        return _AsyncList(entities)


class _AsyncList:
    """`async for` over a list, which is all `iter_*` needs to be here."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> _AsyncList:
        self._iter = iter(self._items)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeSession:
    def __init__(self) -> None:
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


def fake_client_factory(world: World | None = None) -> Any:
    """A `client_factory` for `SessionManager` that ignores the session path."""
    shared = world or World()

    def factory(session_path: Any, options: Any) -> FakeTelegramClient:
        return FakeTelegramClient(shared, session=session_path)

    factory.world = shared  # type: ignore[attr-defined]
    return factory
