"""The `events` group and `watch`, end to end through a real daemon.

`watch` is the one operation whose correctness is about *time*: it has to
deliver what already happened, then what happens next, and say so when it
cannot. Each test here therefore drives a real socket, a real bus and the real
NDJSON framing rather than calling the implementation directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from tlgr.core.errors import EXIT_INDETERMINATE, EXIT_NOT_FOUND, EXIT_USAGE, classify

ALICE = 4242


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


def local(op_id: str, request: dict[str, Any] | None = None, **state: Any) -> Any:
    """Run a `Surface.LOCAL` operation the way the CLI does."""
    import msgspec

    from tlgr.cli.gen import LocalContext
    from tlgr.registry import get

    spec = get(op_id)
    context = LocalContext(account=state.pop("account", ""))
    for key, value in state.items():
        setattr(context, key, value)
    payload = msgspec.convert(request or {}, type=spec.request, strict=False)
    return asyncio.run(spec.impl(context, payload))


class TestEventList:
    def test_it_lists_the_whole_taxonomy(self):
        page = local("events.list", {}, limit=1000)
        assert page.total > 100
        assert {row.type for row in page.items} >= {"message_new", "read_inbox", "typing"}

    def test_a_group_filter_narrows_it(self):
        page = local("events.list", {"group": "read"}, limit=100)
        assert {row.group for row in page.items} == {"read"}

    def test_available_hides_what_this_build_cannot_receive(self):
        everything = local("events.list", {}, limit=1000)
        available = local("events.list", {"available": True}, limit=1000)
        assert available.total < everything.total
        assert all(row.available and not row.bot_only for row in available.items)

    def test_raw_lists_constructors(self):
        page = local("events.list", {"raw": True, "search": "UpdateBotStopped"}, limit=50)
        assert [row.sources for row in page.items] == [["UpdateBotStopped"]]

    def test_the_page_carries_a_cursor_when_there_is_more(self):
        page = local("events.list", {}, limit=5)
        assert page.has_more is True
        assert page.next_cursor


class TestEventGet:
    def test_it_reports_the_sequence_box(self):
        detail = local("events.get", {"type": "message_new"})
        assert detail.box == "pts"
        assert "UpdateNewMessage" in detail.sources
        assert detail.example is not None

    def test_a_raw_constructor_resolves_to_its_type(self):
        detail = local("events.get", {"type": "raw:UpdateReadHistoryOutbox"})
        assert detail.type == "read_outbox"

    def test_a_json_schema_is_emitted_on_request(self):
        detail = local("events.get", {"type": "read_inbox", "json_schema": True})
        assert detail.json_schema is not None
        assert detail.json_schema["properties"]["max_id"]["type"] == "integer"

    def test_an_unknown_type_is_not_found(self):
        from tlgr.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            local("events.get", {"type": "message_exploded"})


class TestEventDecode:
    def test_a_raw_update_becomes_an_envelope(self, tmp_path):
        path = tmp_path / "update.json"
        path.write_text(json.dumps({"_": "UpdateReadHistoryInbox", "max_id": 9}))
        decoded = local("events.decode", {"input": str(path)})
        assert decoded.event == "read_inbox"
        assert decoded.data["max_id"] == 9

    def test_a_container_says_why_it_carries_no_event(self, tmp_path):
        from tlgr.core.errors import NotSupportedError

        path = tmp_path / "update.json"
        path.write_text(json.dumps({"_": "UpdatesTooLong"}))
        with pytest.raises(NotSupportedError) as excinfo:
            local("events.decode", {"input": str(path)})
        assert classify(excinfo.value).exit_code == EXIT_INDETERMINATE

    def test_an_unknown_constructor_is_not_found(self, tmp_path):
        from tlgr.core.errors import NotFoundError

        path = tmp_path / "update.json"
        path.write_text(json.dumps({"_": "UpdateSomethingElse"}))
        with pytest.raises(NotFoundError) as excinfo:
            local("events.decode", {"input": str(path)})
        assert classify(excinfo.value).exit_code == EXIT_NOT_FOUND

    def test_a_plain_json_push_payload_is_classified(self, tmp_path):
        path = tmp_path / "push.b64"
        payload = {"data": {"loc_key": "SESSION_REVOKE", "custom": {}}}
        path.write_text(base64.b64encode(json.dumps(payload).encode()).decode())
        decoded = local("events.decode", {"input": str(path), "push": True})
        assert decoded.event == "account_session_revoked"
        assert decoded.push is True

    def test_a_message_push_maps_onto_message_new(self, tmp_path):
        path = tmp_path / "push.b64"
        payload = {"loc_key": "CHANNEL_MESSAGE_TEXT", "chat_id": "-100123"}
        path.write_text(base64.b64encode(json.dumps(payload).encode()).decode())
        decoded = local("events.decode", {"input": str(path), "push": True})
        assert decoded.event == "message_new"
        assert decoded.chat_id == -100123

    def test_an_encrypted_payload_round_trips_with_the_right_key(self, tmp_path):
        """Encrypt with the same derivation the decoder uses, then decode it."""
        import hashlib
        import os

        from telethon.crypto import AES

        auth_key = os.urandom(256)
        body = json.dumps({"loc_key": "DC_UPDATE", "custom": {"dc": 4}}).encode()
        plain = len(body).to_bytes(4, "little") + body
        plain += b"\x00" * (-len(plain) % 16)
        msg_key = hashlib.sha256(auth_key[88:120] + plain).digest()[8:24]
        sha256_a = hashlib.sha256(msg_key + auth_key[0:36]).digest()
        sha256_b = hashlib.sha256(auth_key[40:76] + msg_key).digest()
        key = sha256_a[:8] + sha256_b[8:24] + sha256_a[24:32]
        iv = sha256_b[:8] + sha256_a[8:24] + sha256_b[24:32]
        blob = msg_key + AES.encrypt_ige(plain, key, iv)

        path = tmp_path / "push.b64"
        path.write_text(base64.b64encode(blob).decode())
        decoded = local(
            "events.decode",
            {
                "input": str(path),
                "push": True,
                "key": base64.b64encode(auth_key).decode(),
            },
        )
        assert decoded.event == "sync_dc_options"

    def test_a_wrong_key_is_indeterminate_not_a_plausible_answer(self, tmp_path):
        import os

        from tlgr.core.errors import IndeterminateError

        path = tmp_path / "push.b64"
        path.write_text(base64.b64encode(os.urandom(80)).decode())
        with pytest.raises(IndeterminateError):
            local(
                "events.decode",
                {
                    "input": str(path),
                    "push": True,
                    "key": base64.b64encode(os.urandom(256)).decode(),
                },
            )

    def test_an_encrypted_payload_without_a_key_is_a_usage_error(self, tmp_path):
        import os

        from tlgr.core.errors import UsageError

        path = tmp_path / "push.b64"
        path.write_text(base64.b64encode(os.urandom(80)).decode())
        with pytest.raises(UsageError):
            local("events.decode", {"input": str(path), "push": True})


class TestWatch:
    async def test_it_replays_what_already_happened(self, live_daemon, client, in_thread):
        live_daemon.bus.emit("work", "message_new", {"id": 1}, chat_id=-100)
        live_daemon.bus.emit("work", "read_inbox", {"max_id": 4}, chat_id=-100)

        frames = await _watch(client, in_thread, {"since": 0, "events": "all", "follow": False})
        kinds = [frame.get("type") for frame in frames]
        assert kinds[0] == "meta"
        assert kinds[-1] == "end"
        assert "message_new" in kinds and "read_inbox" in kinds

    async def test_the_default_selection_is_v1s_new_message(self, live_daemon, client, in_thread):
        """§12.4: `tlgr watch` with no flags means what it meant in v1."""
        live_daemon.bus.emit("work", "message_new", {"id": 1})
        live_daemon.bus.emit("work", "typing", {"user_id": 4242})
        frames = await _watch(client, in_thread, {"since": 0, "follow": False})
        assert [f.get("type") for f in frames if f.get("seq")] == ["message_new"]

    async def test_a_group_selector_expands(self, live_daemon, client, in_thread):
        live_daemon.bus.emit("work", "read_inbox", {"max_id": 1})
        live_daemon.bus.emit("work", "read_outbox", {"max_id": 2})
        live_daemon.bus.emit("work", "message_new", {"id": 3})
        frames = await _watch(client, in_thread, {"since": 0, "events": "read", "follow": False})
        assert sorted(f["type"] for f in frames if f.get("seq")) == ["read_inbox", "read_outbox"]

    async def test_exclude_subtracts(self, live_daemon, client, in_thread):
        live_daemon.bus.emit("work", "read_inbox", {"max_id": 1})
        live_daemon.bus.emit("work", "read_outbox", {"max_id": 2})
        frames = await _watch(
            client,
            in_thread,
            {"since": 0, "events": "read", "exclude": "read_outbox", "follow": False},
        )
        assert [f["type"] for f in frames if f.get("seq")] == ["read_inbox"]

    async def test_a_chat_filter_uses_marked_ids(self, live_daemon, client, in_thread):
        live_daemon.bus.emit("work", "message_new", {"id": 1}, chat_id=-100)
        live_daemon.bus.emit("work", "message_new", {"id": 2}, chat_id=-200)
        frames = await _watch(client, in_thread, {"since": 0, "chat": ["-100"], "follow": False})
        assert [f["payload"]["id"] for f in frames if f.get("seq")] == [1]

    async def test_a_since_older_than_the_buffer_reports_a_gap(
        self, live_daemon, client, in_thread
    ):
        live_daemon.bus.buffer_size = 4
        for index in range(12):
            live_daemon.bus.emit("work", "message_new", {"id": index})
        frames = await _watch(client, in_thread, {"since": 1, "events": "all", "follow": False})
        gaps = [frame for frame in frames if frame.get("type") == "gap"]
        assert gaps and gaps[0]["lost"] > 0

    async def test_an_unknown_event_selector_is_a_usage_error(self, live_daemon, client, in_thread):
        frames = await _watch(client, in_thread, {"events": "messages", "follow": False})
        end = frames[-1]
        assert end["type"] == "end"
        assert end["ok"] is False
        assert end["error"]["exit_code"] == EXIT_USAGE

    async def test_it_follows_and_heartbeats(self, live_daemon, client, in_thread):
        """A quiet chat must be distinguishable from a dead connection."""
        frames = await _watch(
            client,
            in_thread,
            {"events": "all", "follow": True, "heartbeat": 1, "follow_for": 2},
        )
        assert any(frame.get("type") == "heartbeat" for frame in frames)
        assert frames[-1]["type"] == "end"

    async def test_max_events_stops_the_stream(self, live_daemon, client, in_thread):
        for index in range(5):
            live_daemon.bus.emit("work", "message_new", {"id": index})
        frames = await _watch(
            client,
            in_thread,
            {"since": 0, "events": "all", "follow": False, "max_events": 2},
        )
        assert len([f for f in frames if f.get("seq")]) == 2

    async def test_the_cursor_frame_says_where_to_resume(self, live_daemon, client, in_thread):
        live_daemon.bus.emit("work", "message_new", {"id": 1})
        frames = await _watch(
            client,
            in_thread,
            {"since": 0, "events": "all", "follow": False, "print_cursor": True},
        )
        cursor = [frame for frame in frames if frame.get("type") == "cursor"]
        assert cursor and cursor[0]["latest_seq"]["work"] == 1


class TestEventsEndpoint:
    async def test_the_get_endpoint_and_the_op_agree(self, live_daemon, client, in_thread):
        """`GET /v1/events` is a GET-shaped alias of the same operation."""
        live_daemon.bus.emit("work", "message_new", {"id": 7}, chat_id=-100)

        def read() -> list[dict[str, Any]]:
            frames = []
            for frame in client.events(account="work", since=0, timeout=1, follow="false"):
                frames.append(frame)
                if frame.get("type") == "end":
                    break
            return frames

        client._ready = True
        frames = await in_thread(read)
        payloads = [f["payload"]["id"] for f in frames if f.get("type") == "message_new"]
        assert payloads == [7]


class TestReplay:
    async def test_it_returns_the_buffered_range(self, live_daemon, client, in_thread):
        for index in range(4):
            live_daemon.bus.emit("work", "message_new", {"id": index})
        items = await _replay(client, in_thread, {"since": 1, "events": "all"})
        assert [item["payload"]["id"] for item in items] == [1, 2, 3]

    async def test_a_range_before_the_buffer_is_indeterminate(self, live_daemon, client, in_thread):
        """Returning the newest page would be a silent lie about catching up."""
        live_daemon.bus.buffer_size = 4
        for index in range(12):
            live_daemon.bus.emit("work", "message_new", {"id": index})
        frames = await _frames(client, in_thread, "events.replay", {"since": 1, "events": "all"})
        end = frames[-1]
        assert end["ok"] is False
        assert end["error"]["exit_code"] == EXIT_INDETERMINATE

    async def test_difference_turns_the_gap_into_a_warning(self, live_daemon, client, in_thread):
        live_daemon.bus.buffer_size = 4
        for index in range(12):
            live_daemon.bus.emit("work", "message_new", {"id": index})
        frames = await _frames(
            client,
            in_thread,
            "events.replay",
            {"since": 1, "events": "all", "difference": True},
        )
        assert frames[-1]["ok"] is True

    async def test_a_filter_applies_to_the_replay(self, live_daemon, client, in_thread):
        live_daemon.bus.emit("work", "message_new", {"id": 1})
        live_daemon.bus.emit("work", "read_inbox", {"max_id": 2})
        items = await _replay(client, in_thread, {"since": 0, "events": "read"})
        assert [item["type"] for item in items] == ["read_inbox"]

    async def test_an_empty_range_is_not_an_error(self, live_daemon, client, in_thread):
        items = await _replay(client, in_thread, {"since": 0, "events": "all"})
        assert items == []


async def _frames(client, in_thread, op_id: str, request: dict[str, Any]) -> list[dict[str, Any]]:
    def read() -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for frame in client.op_stream(op_id, request, account="work"):
            frames.append(frame)
            if frame.get("type") == "end":
                break
        return frames

    client._ready = True
    return await in_thread(read)


async def _replay(client, in_thread, request: dict[str, Any]) -> list[dict[str, Any]]:
    frames = await _frames(client, in_thread, "events.replay", request)
    return [frame["data"] for frame in frames if frame.get("type") == "item"]


async def _watch(client, in_thread, request: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive `events.watch` over the socket and collect its frames."""

    def read() -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for frame in client.op_stream("events.watch", request, account="work"):
            frames.append(frame)
            if frame.get("type") == "end":
                break
        return frames

    client._ready = True
    return await in_thread(read)
