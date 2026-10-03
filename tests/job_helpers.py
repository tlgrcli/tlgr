"""Shared pieces for the job-action tests: real TL updates, a recording runner.

The updates are real `telethon.tl.types` objects, built the way the server
sends them, and turned into the high-level event a job sees with the same
`build_event` the daemon uses. The `RecordingRunner` stands in for the op
layer where a test is about scheduling rather than about what an operation
sends; the integration tests run the real operations against the fake client.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

from fake_clock import FakeClock
from telethon.tl import types

from tlgr.gateway.engine import Gateway, _builders
from tlgr.gateway.event import Event
from tlgr.gateway.scheduler import ActionScheduler
from tlgr.gateway.tlevents import build_event

ALICE = 4242
BOB = 4343
CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL
GROUP = 777
GROUP_ID = -GROUP


class RecordingRunner:
    """An op runner that records `(op, request, now)` and can be told to fail."""

    def __init__(self, clock: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.clock = clock
        self.failures: dict[str, list[BaseException]] = {}
        self.results: dict[str, Any] = {}

    def fail(self, op: str, *errors: BaseException) -> None:
        self.failures.setdefault(op, []).extend(errors)

    async def __call__(self, op: str, request: dict[str, Any]) -> Any:
        now = self.clock.now() if self.clock is not None else 0.0
        self.calls.append((op, dict(request), now))
        pending = self.failures.get(op)
        if pending:
            raise pending.pop(0)
        result = self.results.get(op)
        if callable(result):
            return result(request)
        return result

    def ops(self, *names: str) -> list[str]:
        return [op for op, _, _ in self.calls if not names or op in names]

    def requests(self, op: str) -> list[dict[str, Any]]:
        return [request for name, request, _ in self.calls if name == op]

    def times(self, op: str) -> list[float]:
        return [when for name, _, when in self.calls if name == op]


def make_scheduler(
    *, clock: FakeClock | None = None, seed: int = 1, store: Any = None, **kwargs: Any
) -> tuple[ActionScheduler, RecordingRunner, FakeClock]:
    clock = clock or FakeClock()
    runner = RecordingRunner(clock)
    scheduler = ActionScheduler(
        "work", runner, clock=clock, rng=random.Random(seed), store=store, **kwargs
    )
    return scheduler, runner, clock


def private_update(
    message_id: int,
    text: str = "hello",
    *,
    user: int = ALICE,
    out: bool = False,
    media: Any = None,
    grouped_id: int | None = None,
) -> types.UpdateNewMessage:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerUser(user_id=user),
        from_id=None if out else types.PeerUser(user_id=user),
        date=None,
        message=text,
        out=out,
        media=media,
        grouped_id=grouped_id,
    )
    return types.UpdateNewMessage(message=message, pts=1, pts_count=1)


def channel_update(
    message_id: int, text: str = "post", *, channel: int = CHANNEL, media: Any = None
) -> types.UpdateNewChannelMessage:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(channel_id=channel),
        date=None,
        message=text,
        post=True,
        media=media,
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)


def group_update(message_id: int, text: str = "hi all", *, user: int = ALICE) -> Any:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChat(chat_id=GROUP),
        from_id=types.PeerUser(user_id=user),
        date=None,
        message=text,
    )
    return types.UpdateNewMessage(message=message, pts=1, pts_count=1)


def topic_update(message_id: int, topic: int, *, channel: int = CHANNEL) -> Any:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(channel_id=channel),
        from_id=types.PeerUser(user_id=ALICE),
        date=None,
        message="in a topic",
        reply_to=types.MessageReplyHeader(
            reply_to_msg_id=topic, forum_topic=True, reply_to_top_id=None
        ),
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)


def voice_media(*, ttl: int | None = None, round_video: bool = False) -> Any:
    attributes: list[Any]
    if round_video:
        attributes = [types.DocumentAttributeVideo(duration=3, w=240, h=240, round_message=True)]
        mime = "video/mp4"
    else:
        attributes = [types.DocumentAttributeAudio(duration=3, voice=True)]
        mime = "audio/ogg"
    document = types.Document(
        id=99,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type=mime,
        size=10,
        dc_id=2,
        attributes=attributes,
    )
    return types.MessageMediaDocument(document=document, ttl_seconds=ttl)


def photo_media() -> Any:
    photo = types.Photo(id=77, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=2)
    return types.MessageMediaPhoto(photo=photo)


async def tg_event(update: Any, client: Any = None) -> Any:
    """The high-level Telethon event a job's filters and actions read."""
    if client is None:
        from fake_telethon import FakeTelegramClient, World

        client = FakeTelegramClient(World())
    built = await build_event(_builders(["new_message"]), update, client)
    assert built is not None, "the builder declined the update"
    return built[0]


async def deliver(job: Gateway, update: Any, client: Any = None) -> None:
    """Hand *update* to a job the way its bus handler does."""
    await job._handle(await tg_event(update, client), "new_message")


def envelope(event: Any, account: str = "work") -> Event:
    return Event(source="telegram", raw=event, account=account)


def bus_envelope(account: str = "work", event_type: str = "message_new") -> Any:
    return SimpleNamespace(type=event_type, account=account)


async def daemon_job_env(daemon: Any, alias: str = "work", *, seed: int = 1) -> Any:
    """Connect *alias* and give it a scheduler on a virtual clock.

    The scheduler runs the real operations through the daemon's dispatcher
    against the fake client, so a test asserts on the requests that reached
    "Telegram" (`world.calls`), not on a mock.
    """
    from tlgr.gateway.executor import DaemonOpRunner

    await daemon.sessions.connect_all([alias])
    clock = FakeClock()
    scheduler = ActionScheduler(
        alias, DaemonOpRunner(daemon, alias), clock=clock, rng=random.Random(seed)
    )
    daemon._schedulers[alias] = scheduler
    daemon.bus.add_handler(scheduler.on_bus)
    scheduler.start()
    return SimpleNamespace(
        scheduler=scheduler, clock=clock, client=daemon.get_client(alias), alias=alias
    )


async def until(predicate: Any, *, clock: FakeClock | None = None, timeout: float = 5.0) -> None:
    """Wait (really, briefly) for work running through the dispatcher to land."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        if clock is not None:
            await clock.advance(0.5)
        await asyncio.sleep(0.01)
