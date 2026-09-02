"""The event bus: numbering, replay, ordering and backpressure (§6.5).

The two properties that matter to a consumer are that `seq` is monotonic and
persisted — so `--since` survives a daemon restart — and that falling behind
produces a *signal* rather than silence. A consumer that quietly skips events
cannot tell a quiet chat from a lost hour.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tlgr.daemon.events import EVENT_TYPES, EventBus, normalise
from tlgr.models.event import EventEnvelope

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bus(tlgr_home: Path):
    from tlgr.core.paths import TlgrPaths

    paths = TlgrPaths(tlgr_home)
    paths.ensure_account_dir("work")
    # 16 is the bus's floor for a ring buffer; asking for less gets 16.
    return EventBus(state_dir_for=paths.events_state, buffer_size=16, workers=4)


class TestSequence:
    async def test_seq_is_monotonic_per_account(self, bus):
        first = bus.emit("work", "message_new", {})
        second = bus.emit("work", "message_new", {})
        other = bus.emit("other", "message_new", {})
        assert (first.seq, second.seq) == (1, 2)
        assert other.seq == 1, "accounts share a counter"

    async def test_seq_is_persisted_and_reloaded(self, bus, tlgr_home):
        from tlgr.core.paths import TlgrPaths

        for _ in range(5):
            bus.emit("work", "message_new", {})
        bus.flush_state()

        state = json.loads(TlgrPaths(tlgr_home).events_state("work").read_text())
        assert state["seq"] == 5

        fresh = EventBus(state_dir_for=TlgrPaths(tlgr_home).events_state)
        assert fresh.emit("work", "message_new", {}).seq == 6

    async def test_the_state_file_is_private(self, bus, tlgr_home):
        from tlgr.core.paths import TlgrPaths

        bus.emit("work", "message_new", {})
        bus.flush_state()
        path = TlgrPaths(tlgr_home).events_state("work")
        assert path.stat().st_mode & 0o777 == 0o600


class TestReplay:
    async def test_since_returns_only_newer_events(self, bus):
        for _ in range(4):
            bus.emit("work", "message_new", {})
        events, gap = bus.replay("work", since=2)
        assert [e.seq for e in events] == [3, 4]
        assert gap is None

    async def test_a_since_older_than_the_buffer_reports_a_gap(self, bus):
        """Delivering the newest 4,096 with no signal would be a silent lie."""
        for _ in range(24):  # the ring holds 16
            bus.emit("work", "message_new", {})
        events, gap = bus.replay("work", since=1)
        assert gap is not None
        assert gap["type"] == "gap"
        assert gap["from"] == 9
        assert gap["requested"] == 1
        assert gap["lost"] == 7
        assert [e.seq for e in events] == list(range(9, 25))

    async def test_no_since_starts_from_now(self, bus):
        bus.emit("work", "message_new", {})
        events, gap = bus.replay("work", since=None)
        assert events == []
        assert gap is None


class TestSubscribers:
    async def test_a_subscriber_only_gets_what_it_asked_for(self, bus):
        wants = bus.subscribe("work", types=["message_new"], chats=[-100])
        bus.emit("work", "message_new", {}, chat_id=-100)
        bus.emit("work", "message_new", {}, chat_id=-200)
        bus.emit("work", "message_edited", {}, chat_id=-100)
        bus.emit("other", "message_new", {}, chat_id=-100)
        assert wants.queue.qsize() == 1

    async def test_a_slow_subscriber_lags_without_blocking_the_bus(self, bus):
        """ROB-02: the bus must never wait for its slowest consumer."""
        slow = bus.subscribe("work", maxsize=4)
        fast = bus.subscribe("work", maxsize=100)
        for _ in range(20):
            bus.emit("work", "message_new", {})
        assert slow.queue.qsize() == 4
        assert slow.take_lag() == 16, "the subscriber was not told what it lost"
        assert fast.queue.qsize() == 20, "one slow consumer starved another"

    async def test_unsubscribing_stops_delivery(self, bus):
        subscriber = bus.subscribe("work")
        bus.unsubscribe(subscriber)
        bus.emit("work", "message_new", {})
        assert subscriber.queue.qsize() == 0


class TestWorkerLanes:
    async def test_handlers_run_off_the_publishing_path(self, bus):
        seen: list[int] = []

        async def handler(envelope: EventEnvelope, raw) -> None:
            seen.append(envelope.seq)

        bus.add_handler(handler)
        await bus.start()
        try:
            bus.emit("work", "message_new", {}, chat_id=1)
            assert seen == [], "the handler ran inside emit()"
            await asyncio.sleep(0.05)
            assert seen == [1]
        finally:
            await bus.stop()

    async def test_per_chat_order_is_preserved_under_load(self, bus):
        """A message and its edit must not be processed out of order."""
        seen: dict[int, list[int]] = {}

        async def handler(envelope: EventEnvelope, raw) -> None:
            # Interleave deliberately: a lane that did not preserve order
            # would let a later event overtake here.
            await asyncio.sleep(0.001 * (envelope.seq % 3))
            seen.setdefault(envelope.chat_id, []).append(envelope.seq)

        bus.add_handler(handler)
        await bus.start()
        try:
            for index in range(30):
                bus.emit("work", "message_new", {}, chat_id=-(index % 5))
            await asyncio.sleep(0.4)
            for chat, seqs in seen.items():
                assert seqs == sorted(seqs), f"chat {chat} was processed out of order"
        finally:
            await bus.stop()

    async def test_a_failing_handler_does_not_stop_the_lane(self, bus):
        survived: list[int] = []

        async def broken(envelope: EventEnvelope, raw) -> None:
            raise RuntimeError("boom")

        async def working(envelope: EventEnvelope, raw) -> None:
            survived.append(envelope.seq)

        bus.add_handler(broken)
        bus.add_handler(working)
        await bus.start()
        try:
            bus.emit("work", "message_new", {}, chat_id=1)
            bus.emit("work", "message_new", {}, chat_id=1)
            await asyncio.sleep(0.05)
            assert survived == [1, 2]
        finally:
            await bus.stop()


class TestNormalisation:
    def test_every_starter_type_is_snake_case(self):
        for name in EVENT_TYPES:
            assert name == name.lower()
            assert " " not in name and "-" not in name

    def test_an_unknown_update_is_not_given_a_made_up_name(self):
        class Whatever:
            pass

        assert normalise("work", Whatever()) is None

    def test_a_message_delete_carries_its_ids(self):
        class Deleted:
            __module__ = "telethon.events.messagedeleted"
            __qualname__ = "MessageDeleted.Event"
            deleted_ids = [4, 5]
            chat_id = -100

        kind, payload, chat_id, sender = normalise("work", Deleted())
        assert kind == "message_deleted"
        assert payload["message_ids"] == [4, 5]
        assert chat_id == -100

    def test_a_new_message_carries_the_model_not_a_raw_dict(self):
        """COR-07: `to_dict()` puts datetimes and bytes into the payload."""

        from fake_telethon import make_message

        class NewMessage:
            __module__ = "telethon.events.newmessage"
            __qualname__ = "NewMessage.Event"
            chat_id = -1000000000123

            def __init__(self):
                self.message = make_message(7, chat_id=-1000000000123, text="hi", sender_id=5)

        kind, payload, chat_id, sender = normalise("work", NewMessage())
        assert kind == "message_new"
        assert payload["id"] == 7
        assert payload["text"] == "hi"
        assert isinstance(payload["date"], str), "a datetime reached the payload"
        assert sender == 5
        json.dumps(payload)  # the whole point: it is JSON already


class TestSelfOrigin:
    async def test_an_action_tlgr_performed_is_echoed(self, bus):
        """Telethon does not dispatch our own sends; `tlgr watch` must see them."""
        envelope = bus.emit("work", "message_new", {"id": 1}, self_origin=True)
        assert envelope.self_origin is True
        events, _ = bus.replay("work", since=0)
        assert events[-1].self_origin is True


@pytest.mark.asyncio
async def test_the_events_endpoint_delivers_replays_and_heartbeats(live_daemon, tlgr_home):
    """§12.3 item 11, end to end over the socket."""
    from tlgr.transport.client import DaemonClient

    live_daemon.bus.emit("work", "message_new", {"id": 1}, chat_id=-100)
    live_daemon.bus.emit("work", "message_new", {"id": 2}, chat_id=-100)

    client = DaemonClient(tlgr_home, auto_start=False)
    client._ready = True
    loop = asyncio.get_running_loop()

    def read() -> list[dict]:
        frames = []
        for frame in client.events(account="work", since=0, timeout=1):
            frames.append(frame)
            if frame.get("type") == "end":
                break
        return frames

    frames = await loop.run_in_executor(None, read)
    kinds = [f.get("type") for f in frames]
    assert kinds[0] == "meta"
    assert kinds[-1] == "end"
    # `meta`, `heartbeat` and `end` are control frames and share the frame
    # namespace with events; an event's `type` is one of the taxonomy's.
    replayed = [f for f in frames if f.get("type") in EVENT_TYPES]
    assert [f["payload"]["id"] for f in replayed] == [1, 2]


@pytest.mark.asyncio
async def test_an_events_stream_counts_as_activity(live_daemon):
    """COR-08: an open watch is not idle, however quiet the chat is."""
    before = live_daemon.activity.event_streams
    live_daemon.activity.begin_stream()
    assert live_daemon.activity.busy is True
    live_daemon.activity.end_stream()
    assert live_daemon.activity.event_streams == before
