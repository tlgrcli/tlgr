"""The call operations — `call`, `vc` and `conference` — end to end.

Same arrangement as the message suite: a real daemon on a real socket, the
real dispatcher, a fake Telegram. Two things are asserted throughout that are
particular to this group.

* **The request that was built.** A call surface is almost entirely raw
  `phone.*` requests, so "did it work" means "was the right TL object sent
  with the right flags" — `world.called("DiscardCallRequest")[0].reason` is
  the assertion, not the return value.
* **`media: none`.** Every operation that a reader could mistake for "you are
  now in a call" says on the wire that tlgr carries no audio. If that ever
  stops being true of the output, these tests fail.

The three streaming operations are driven through their implementations
directly: a stream has no envelope to assert on, and feeding an update into
the fake client is the whole point of the test.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
)

ALICE = 4242
CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL
CALL_ID = 900100


@pytest.fixture(autouse=True)
def _forget_calls():
    """The live-call store is per daemon, so a test must not inherit one."""
    from tlgr.ops import _calls

    _calls.reset_calls()
    yield
    _calls.reset_calls()


@pytest.fixture
def peers(world):
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_channel(make_channel(CHANNEL, title="News", megagroup=True))
    return world


@pytest.fixture
def group_call(peers, world):
    """A video chat running in the channel, with two participants."""
    world.add_group_call(CALL_ID, chat_id=CHANNEL_ID, messages_enabled=True)
    world.add_participant(CALL_ID, world.me.id, source=111, is_self=True, muted=True)
    world.add_participant(CALL_ID, ALICE, source=222, raise_hand_rating=5)
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


class _Failure:
    """One shape for an error however the daemon chose to deliver it."""

    def __init__(self, code: str, exit_code: int, message: str) -> None:
        self.code = code
        self.exit_code = exit_code
        self.message = message

    def __str__(self) -> str:
        return self.message


async def rows(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> list:
    """The items of a paginated op.

    A paginated envelope carries its items in `result` and its cursor in
    `page` (§5.2), which is exactly the shape `--json` hands a caller — so the
    tests read it the same way a script would.
    """
    return await result(client, in_thread, op, request, **kwargs)


async def failure(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> _Failure:
    """The error a call produced, in either of the two shapes it can arrive in.

    An INDETERMINATE answer is HTTP 200 with `ok: false` — "we could not
    establish this" is not a server failure — while the rest of the table is a
    4xx/5xx the transport raises. A helper that knew only one of them would
    quietly pass on the other.
    """
    from tlgr.core.errors import TlgrError

    try:
        envelope = await call(client, in_thread, op, request, **kwargs)
    except TlgrError as exc:
        return _Failure(exc.code, exc.exit_code, str(exc))
    assert envelope.get("ok") is False, f"expected {op} to fail, got {envelope}"
    body = envelope["error"]
    return _Failure(
        str(body.get("code", "")), int(body.get("exit_code", 1)), str(body.get("message", ""))
    )


# ---------------------------------------------------------------------------
# 1:1 calls
# ---------------------------------------------------------------------------


class TestCallStart:
    async def test_a_call_is_placed_with_a_hashed_g_a(self, live_daemon, client, in_thread, peers):
        """The handshake is real: `requestCall` carries SHA-256(g_a), not g_a."""
        import hashlib

        placed = await result(client, in_thread, "call.start", {"user": "@alice"})
        assert placed["state"] == "waiting"
        assert placed["media"] == "none", "tlgr has no audio engine and must say so"
        request = peers.called("RequestCallRequest")[0]
        assert len(request.g_a_hash) == hashlib.sha256(b"").digest_size
        assert request.protocol.min_layer == 65
        assert request.protocol.max_layer == 92

    async def test_the_dh_parameters_are_validated_not_trusted(
        self, live_daemon, client, in_thread, peers
    ):
        """A prime the client cannot verify is not a key exchange (exit 13)."""
        from telethon.tl import types

        peers.raw["GetDhConfigRequest"] = lambda request: types.messages.DhConfig(
            g=2, p=b"\x07" * 256, version=1, random=b""
        )
        error = await failure(client, in_thread, "call.start", {"user": "@alice"})
        assert error.exit_code == EXIT_INDETERMINATE
        assert "did not validate" in str(error)
        assert peers.called("RequestCallRequest") == [], "nothing may ring on bad parameters"

    async def test_video_reaches_the_request(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "call.start", {"user": "@alice", "video": True})
        assert peers.called("RequestCallRequest")[0].video is True

    async def test_check_reports_availability_without_ringing(
        self, live_daemon, client, in_thread, peers
    ):
        """`--dry-run` is the registry's own short-circuit, so the probe is `--check`."""
        probe = await result(client, in_thread, "call.start", {"user": "@alice", "check": True})
        assert probe["state"] == "checked"
        assert probe["can_call"] is True
        assert peers.called("RequestCallRequest") == []

    async def test_check_reports_a_private_peer(self, live_daemon, client, in_thread, peers):
        peers.calls_available = False
        probe = await result(client, in_thread, "call.start", {"user": "@alice", "check": True})
        assert probe["can_call"] is False
        assert probe["private"] is True

    async def test_a_dry_run_never_reaches_the_implementation(
        self, live_daemon, client, in_thread, peers
    ):
        envelope = await call(client, in_thread, "call.start", {"user": "@alice"}, dry_run=True)
        assert envelope["result"]["would"] == "call.start"
        assert peers.called("RequestCallRequest") == []

    async def test_calling_a_channel_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "call.start", {"user": str(CHANNEL_ID)})
        assert error.exit_code == EXIT_USAGE

    async def test_redial_reads_the_peer_off_a_call_log_row(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_message(ALICE, "", message_id=501)
        await result(client, in_thread, "call.start", {"from_message": f"{ALICE}:501"})
        assert peers.called("RequestCallRequest")


class TestCallLifecycle:
    @pytest.fixture
    async def ringing(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "call.start", {"user": "@alice"})
        return peers.called("RequestCallRequest") and next(iter(peers.phone_calls))

    async def test_an_id_alone_is_enough_to_hang_up(
        self, live_daemon, client, in_thread, peers, ringing
    ):
        """The daemon remembers the access hash, so a human types the id."""
        ended = await result(client, in_thread, "call.end", {"call": str(ringing)})
        assert ended["reason"] == "hangup"
        assert ended["need_rating"] is True
        request = peers.called("DiscardCallRequest")[0]
        assert type(request.reason).__name__ == "PhoneCallDiscardReasonHangup"

    async def test_an_unknown_call_is_not_found(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "call.end", {"call": "12345"})
        assert error.exit_code == EXIT_NOT_FOUND
        assert "id:access_hash" in str(error)

    async def test_an_explicit_access_hash_needs_no_daemon_state(
        self, live_daemon, client, in_thread, peers
    ):
        ended = await result(client, in_thread, "call.end", {"call": "12345:678"})
        assert ended["call_id"] == 12345
        assert peers.called("DiscardCallRequest")[0].peer.access_hash == 678

    async def test_decline_sends_the_reason_and_no_message(
        self, live_daemon, client, in_thread, peers, ringing
    ):
        declined = await result(
            client, in_thread, "call.decline", {"call": str(ringing), "reason": "busy"}
        )
        assert declined["reason"] == "busy"
        assert "reply_message_id" not in declined
        assert type(peers.called("DiscardCallRequest")[0].reason).__name__ == (
            "PhoneCallDiscardReasonBusy"
        )

    async def test_decline_with_a_reply_is_discard_plus_a_message(
        self, live_daemon, client, in_thread, peers, ringing
    ):
        declined = await result(
            client,
            in_thread,
            "call.decline",
            {"call": str(ringing), "reply": "can't talk"},
        )
        assert declined["reply_message_id"]
        assert [m.message for m in peers.history(ALICE)] == ["can't talk"]

    async def test_accept_acknowledges_before_answering(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_phone_call(777001, state="Requested", admin_id=ALICE, participant_id=777)
        accepted = await result(client, in_thread, "call.accept", {"call": "777001:3885005"})
        assert accepted["media"] == "none"
        assert peers.called("ReceivedCallRequest"), "the busy-lock comes first"
        assert peers.called("AcceptCallRequest")[0].g_b

    async def test_ack_only_stops_before_the_answer(self, live_daemon, client, in_thread, peers):
        peers.add_phone_call(777002, state="Requested")
        await result(client, in_thread, "call.accept", {"call": "777002:3885010", "ack_only": True})
        assert peers.called("ReceivedCallRequest")
        assert peers.called("AcceptCallRequest") == []

    async def test_no_ack_lets_several_calls_ring(self, live_daemon, client, in_thread, peers):
        peers.add_phone_call(777003, state="Requested")
        await result(client, in_thread, "call.accept", {"call": "777003:3885015", "no_ack": True})
        assert peers.called("ReceivedCallRequest") == []


class TestCallGetAndRate:
    async def test_get_reads_the_state_the_daemon_holds(
        self, live_daemon, client, in_thread, peers
    ):
        placed = await result(client, in_thread, "call.start", {"user": "@alice"})
        state = await result(client, in_thread, "call.get", {"call": str(placed["call_id"])})
        assert state["state"] == "waiting"
        assert state["media"] == "none"
        # `conference_supported` only exists on the *active* constructor, so a
        # ringing call reports nothing rather than guessing False.
        assert "conference_supported" not in state

    async def test_a_call_nobody_ran_the_exchange_for_has_no_fingerprint(
        self, live_daemon, client, in_thread, peers
    ):
        from tlgr.ops import _calls

        _calls.remember_call("work", _calls.LiveCall(id=99, access_hash=1, state="active"))
        envelope = await call(client, in_thread, "call.get", {"call": "99"})
        assert "fingerprint" not in envelope["result"]
        assert any("did not run the DH exchange" in w for w in envelope["meta"]["warnings"])

    async def test_a_key_yields_four_verification_indices(
        self, live_daemon, client, in_thread, peers
    ):
        from tlgr.ops import _calls

        _calls.remember_call(
            "work",
            _calls.LiveCall(
                id=98, access_hash=1, state="active", key=b"\x01" * 256, g_a=b"\x02" * 256
            ),
        )
        state = await result(client, in_thread, "call.get", {"call": "98"})
        assert len(state["fingerprint"]) == 4
        assert all(value.startswith("#") for value in state["fingerprint"])

    async def test_get_on_an_unknown_call_is_not_found(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "call.get", {"call": "4242"})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_problems_travel_as_hashtags_on_the_comment(
        self, live_daemon, client, in_thread, peers
    ):
        rated = await result(
            client,
            in_thread,
            "call.rate",
            {"call": "12345:678", "rating": 4, "comment": "ok", "problem": ["echo", "noise"]},
        )
        assert rated["comment"] == "ok #echo #noise"
        assert peers.called("SetCallRatingRequest")[0].rating == 4

    async def test_an_invented_problem_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        error = await failure(
            client,
            in_thread,
            "call.rate",
            {"call": "12345:678", "rating": 5, "problem": ["static"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_rating_outside_one_to_five_is_refused(
        self, live_daemon, client, in_thread, peers
    ):
        error = await failure(client, in_thread, "call.rate", {"call": "12345:678", "rating": 9})
        assert error.exit_code == EXIT_USAGE


class TestCallConfigAndDebug:
    async def test_the_config_carries_the_timeouts_and_the_limits(
        self, live_daemon, client, in_thread, peers
    ):
        config = await result(client, in_thread, "call.config.get", {})
        assert config["timeouts"]["call_ring_timeout_ms"] == 90000
        assert config["conference_call_size_limit"] == 200
        assert config["call_requests_disabled"] is False

    async def test_the_dh_block_carries_its_verdict(self, live_daemon, client, in_thread, peers):
        config = await result(client, in_thread, "call.config.get", {"dh": True})
        assert config["dh"]["ok"] is True
        assert config["dh"]["safe_prime"] is True

    async def test_raw_keeps_the_blob_verbatim(self, live_daemon, client, in_thread, peers):
        config = await result(client, in_thread, "call.config.get", {"raw": True})
        assert config["tgcalls_config"]["raw"].startswith("{")

    async def test_a_debug_upload_needs_a_file(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "call.debug.upload", {"call": "12345:678"})
        assert error.exit_code == EXIT_USAGE
        assert "will not invent" in str(error)

    async def test_a_debug_json_is_uploaded_verbatim(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        blob = tmp_path / "debug.json"
        blob.write_text('{"rtt": 42}', encoding="utf-8")
        uploaded = await result(
            client, in_thread, "call.debug.upload", {"call": "12345:678", "json_file": str(blob)}
        )
        assert uploaded["uploaded"] == ["debug"]
        assert peers.called("SaveCallDebugRequest")[0].debug.data == '{"rtt": 42}'


class TestCallLog:
    @pytest.fixture
    def log(self, peers, world):
        for index in range(1, 6):
            world.add_call_log_entry(500 + index, chat_id=ALICE, out=index % 2 == 0)
        world.add_call_log_entry(600, chat_id=ALICE, reason="missed")
        world.add_call_log_entry(700, chat_id=ALICE, conference=True, video=True)
        return world

    async def test_the_calls_tab_decodes_its_service_messages(
        self, live_daemon, client, in_thread, log
    ):
        items = await rows(client, in_thread, "call.log.list", {})
        kinds = {row["kind"] for row in items}
        assert kinds == {"call", "conference"}
        conference = next(row for row in items if row["kind"] == "conference")
        assert conference["video"] is True
        assert conference["other_participants"]

    async def test_the_missed_filter_is_the_servers_own(self, live_daemon, client, in_thread, log):
        items = await rows(client, in_thread, "call.log.list", {"missed": True})
        assert [row["msg_id"] for row in items] == [600]
        assert log.called("SearchRequest")[-1].filter.missed is True

    async def test_video_is_filtered_locally(self, live_daemon, client, in_thread, log):
        items = await rows(client, in_thread, "call.log.list", {"video": True})
        assert items and all(row["video"] for row in items)

    async def test_a_cursor_continues_where_the_page_stopped(
        self, live_daemon, client, in_thread, log
    ):
        first = await call(client, in_thread, "call.log.list", {}, limit=3)
        assert first["page"]["has_more"] is True
        second = await call(
            client, in_thread, "call.log.list", {}, limit=3, cursor=first["page"]["next_cursor"]
        )
        assert {row["msg_id"] for row in first["result"]} & {
            row["msg_id"] for row in second["result"]
        } == set()

    async def test_a_hand_edited_cursor_is_a_usage_error(self, live_daemon, client, in_thread, log):
        error = await failure(client, in_thread, "call.log.list", {}, cursor="nonsense")
        assert error.exit_code == EXIT_USAGE

    async def test_deleting_rows_takes_chat_and_id(self, live_daemon, client, in_thread, log):
        deleted = await result(
            client, in_thread, "call.log.delete", {"id": [f"{ALICE}:501"], "revoke": True}
        )
        assert deleted == {"deleted": 1, "revoked": True}
        assert log.called("delete_messages")[0]["ids"] == [501]

    async def test_a_row_without_a_chat_is_a_usage_error(self, live_daemon, client, in_thread, log):
        error = await failure(client, in_thread, "call.log.delete", {"id": ["501"]})
        assert error.exit_code == EXIT_USAGE

    async def test_clearing_the_history_loops_until_the_offset_is_zero(
        self, live_daemon, client, in_thread, log
    ):
        deleted = await result(client, in_thread, "call.log.delete", {"history": True})
        assert deleted["deleted"] == 7
        assert log.called("DeletePhoneCallHistoryRequest")


class TestCallInvite:
    async def test_upgrading_without_a_block_names_the_missing_piece(
        self, live_daemon, client, in_thread, peers
    ):
        error = await failure(
            client, in_thread, "call.invite", {"call": "12345:678", "user": ["@alice"]}
        )
        assert error.exit_code == EXIT_USAGE
        assert "e2e.chain block" in str(error)
        assert peers.called("CreateConferenceCallRequest") == []

    async def test_with_a_block_the_call_migrates_and_invites(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        block = tmp_path / "block.bin"
        block.write_bytes(b"signed")
        upgraded = await result(
            client,
            in_thread,
            "call.invite",
            {
                "call": "12345:678",
                "user": ["@alice"],
                "block": str(block),
                "public_key": "ff",
            },
        )
        assert upgraded["slug"] == "AbCdEf"
        assert upgraded["migrated"] is True
        reason = peers.called("DiscardCallRequest")[0].reason
        assert type(reason).__name__ == "PhoneCallDiscardReasonMigrateConferenceCall"
        assert reason.slug == "AbCdEf"
        assert peers.called("InviteConferenceCallParticipantRequest")


# ---------------------------------------------------------------------------
# Video chats
# ---------------------------------------------------------------------------


class TestVideoChatLifecycle:
    async def test_creating_a_video_chat_returns_the_call_to_address(
        self, live_daemon, client, in_thread, peers
    ):
        created = await result(client, in_thread, "vc.create", {"chat": str(CHANNEL_ID)})
        assert created["call"]["id"] == 900200
        assert created["kind"] in ("video-chat", "livestream")
        assert peers.called("CreateGroupCallRequest")[0].random_id

    async def test_rtmp_and_a_schedule_reach_the_request(
        self, live_daemon, client, in_thread, peers
    ):
        created = await result(
            client,
            in_thread,
            "vc.create",
            {"chat": str(CHANNEL_ID), "rtmp": True, "schedule": "2026-09-04T10:00:00Z"},
        )
        assert created["rtmp_stream"] is True
        request = peers.called("CreateGroupCallRequest")[0]
        assert request.rtmp_stream is True
        assert request.schedule_date is not None

    async def test_a_scheduled_call_is_started_by_the_chat(
        self, live_daemon, client, in_thread, group_call
    ):
        started = await result(client, in_thread, "vc.start", {"chat": str(CHANNEL_ID)})
        assert started["started"] is True
        assert group_call.called("StartScheduledGroupCallRequest")[0].call.id == CALL_ID

    async def test_ending_a_call_reports_its_duration(
        self, live_daemon, client, in_thread, group_call
    ):
        ended = await result(client, in_thread, "vc.end", {"call": str(CHANNEL_ID)})
        assert ended["ended"] is True
        assert ended["duration"] == 900

    async def test_a_chat_without_a_call_is_not_found(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "vc.get", {"call": str(CHANNEL_ID)})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_a_private_chat_has_no_video_chat(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "vc.get", {"call": "@alice"})
        assert error.exit_code == EXIT_USAGE


class TestVideoChatGet:
    async def test_a_call_is_addressable_by_chat_or_by_id(
        self, live_daemon, client, in_thread, group_call
    ):
        by_chat = await result(client, in_thread, "vc.get", {"call": str(CHANNEL_ID)})
        by_id = await result(client, in_thread, "vc.get", {"call": f"{CALL_ID}:{CALL_ID * 3}"})
        assert by_chat["call"] == by_id["call"]
        assert by_chat["media"] == "none"

    async def test_limits_come_from_the_app_config(
        self, live_daemon, client, in_thread, group_call
    ):
        info = await result(client, in_thread, "vc.get", {"call": str(CHANNEL_ID), "limits": True})
        assert info["limits"]["groupcall_video_participants_max"] == 30

    async def test_stream_channels_carry_the_live_edge(
        self, live_daemon, client, in_thread, group_call
    ):
        info = await result(
            client, in_thread, "vc.get", {"call": str(CHANNEL_ID), "stream_channels": True}
        )
        assert info["live_edge_ms"] == 5000
        assert info["stream_channels"][0]["channel"] == 1

    async def test_check_sources_separates_joined_from_missing(
        self, live_daemon, client, in_thread, group_call
    ):
        info = await result(
            client,
            in_thread,
            "vc.get",
            {"call": str(CHANNEL_ID), "check_sources": [111, 222]},
        )
        assert info["sources_joined"] == [111]
        assert info["sources_missing"] == [222]

    async def test_donors_are_reported_when_asked(self, live_daemon, client, in_thread, group_call):
        info = await result(client, in_thread, "vc.get", {"call": str(CHANNEL_ID), "donors": True})
        assert info["donors"]["total"] == 250

    async def test_active_calls_are_listed_from_the_dialogs(
        self, live_daemon, client, in_thread, group_call, world
    ):
        entity = world.chats[CHANNEL]
        entity.call_active = True
        entity.call_not_empty = True
        items = await rows(client, in_thread, "vc.list", {})
        assert [row["chat_id"] for row in items] == [CHANNEL_ID]
        assert items[0]["call"]["id"] == CALL_ID


class TestVideoChatSettings:
    async def test_only_the_flags_you_pass_are_sent(
        self, live_daemon, client, in_thread, group_call
    ):
        changed = await result(
            client, in_thread, "vc.set", {"call": str(CHANNEL_ID), "title": "standup"}
        )
        assert changed["changed"] == ["title"]
        assert group_call.called("EditGroupCallTitleRequest")[0].title == "standup"
        assert group_call.called("ToggleGroupCallRecordRequest") == []

    async def test_settings_are_read_back_from_the_server(
        self, live_daemon, client, in_thread, group_call
    ):
        changed = await result(
            client,
            in_thread,
            "vc.set",
            {"call": str(CHANNEL_ID), "join_muted": "on", "messages": "off"},
        )
        assert changed["join_muted"] is True
        assert changed["messages_enabled"] is False

    async def test_recording_warns_where_the_file_lands(
        self, live_daemon, client, in_thread, group_call
    ):
        envelope = await call(
            client,
            in_thread,
            "vc.set",
            {"call": str(CHANNEL_ID), "record": "start", "record_video": True},
        )
        assert envelope["result"]["record_video_active"] is True
        assert any("Saved Messages" in w for w in envelope["meta"]["warnings"])

    async def test_setting_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(client, in_thread, "vc.set", {"call": str(CHANNEL_ID)})
        assert error.exit_code == EXIT_USAGE

    async def test_the_reminder_is_its_own_rpc(self, live_daemon, client, in_thread, group_call):
        await result(client, in_thread, "vc.set", {"call": str(CHANNEL_ID), "reminder": "on"})
        assert group_call.called("ToggleGroupCallStartSubscriptionRequest")[0].subscribed is True


class TestVideoChatJoinLeave:
    async def test_joining_without_a_payload_is_refused(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(client, in_thread, "vc.join", {"call": str(CHANNEL_ID)})
        assert error.exit_code == EXIT_USAGE
        assert "--listen-only" in str(error)

    async def test_listen_only_builds_a_payload_and_says_it_is_silent(
        self, live_daemon, client, in_thread, group_call
    ):
        import json

        envelope = await call(
            client, in_thread, "vc.join", {"call": str(CHANNEL_ID), "listen_only": True}
        )
        joined = envelope["result"]
        assert joined["media"] == "none"
        assert joined["experimental"] is True
        assert joined["source"], "a join must remember its SSRC so `vc leave` can use it"
        assert any("no media engine" in w for w in envelope["meta"]["warnings"])
        payload = json.loads(group_call.called("JoinGroupCallRequest")[0].params.data)
        assert payload["fingerprints"] and payload["ufrag"]

    async def test_the_playback_mode_comes_from_the_connection_params(
        self, live_daemon, client, in_thread, group_call
    ):
        joined = await result(
            client, in_thread, "vc.join", {"call": str(CHANNEL_ID), "listen_only": True}
        )
        assert joined["mode"] == "stream"

    async def test_an_invite_hash_is_forwarded(self, live_daemon, client, in_thread, group_call):
        joined = await result(
            client,
            in_thread,
            "vc.join",
            {"call": str(CHANNEL_ID), "listen_only": True, "invite_hash": "abc"},
        )
        assert joined["can_self_unmute"] is True
        assert group_call.called("JoinGroupCallRequest")[0].invite_hash == "abc"

    async def test_leaving_carries_the_source(self, live_daemon, client, in_thread, group_call):
        left = await result(client, in_thread, "vc.leave", {"call": str(CHANNEL_ID), "source": 111})
        assert left["left"] is True
        assert group_call.called("LeaveGroupCallRequest")[0].source == 111


class TestVideoChatParticipants:
    async def test_participants_come_back_with_their_flags(
        self, live_daemon, client, in_thread, group_call
    ):
        items = await rows(client, in_thread, "vc.participant.list", {"call": str(CHANNEL_ID)})
        assert len(items) == 2
        assert items[0]["muted"] is True

    async def test_raised_hands_are_ordered_by_their_rating(
        self, live_daemon, client, in_thread, group_call
    ):
        items = await rows(
            client,
            in_thread,
            "vc.participant.list",
            {"call": str(CHANNEL_ID), "raised_hands": True},
        )
        assert [row["raise_hand_rating"] for row in items] == [5]

    async def test_the_cursor_walks_the_string_offset(
        self, live_daemon, client, in_thread, group_call
    ):
        first = await call(
            client, in_thread, "vc.participant.list", {"call": str(CHANNEL_ID)}, limit=1
        )
        assert first["page"]["has_more"] is True
        second = await call(
            client,
            in_thread,
            "vc.participant.list",
            {"call": str(CHANNEL_ID)},
            limit=1,
            cursor=first["page"]["next_cursor"],
        )
        assert second["result"][0]["source"] != first["result"][0]["source"]
        assert group_call.called("GetGroupParticipantsRequest")[-1].offset == "1"

    async def test_a_hidden_audience_is_never_reported_as_an_empty_call(
        self, live_daemon, client, in_thread, group_call
    ):
        group_call.group_calls[CALL_ID].listeners_hidden = True
        envelope = await call(client, in_thread, "vc.participant.list", {"call": str(CHANNEL_ID)})
        assert any("listeners are hidden" in w for w in envelope["meta"]["warnings"])

    async def test_the_display_as_list_marks_the_default(
        self, live_daemon, client, in_thread, group_call
    ):
        page = await result(client, in_thread, "vc.identity.list", {"chat": str(CHANNEL_ID)})
        assert page["items"][0]["kind"] == "join-as"

    async def test_the_comment_as_list_is_a_different_rpc(
        self, live_daemon, client, in_thread, group_call
    ):
        page = await result(
            client, in_thread, "vc.identity.list", {"chat": str(CHANNEL_ID), "comment": True}
        )
        assert page["items"][0]["kind"] == "send-as"
        assert group_call.called("GetSendAsRequest")[0].for_live_stories is True


class TestVideoChatModeration:
    async def test_self_mute_targets_yourself_and_carries_no_audio(
        self, live_daemon, client, in_thread, group_call
    ):
        muted = await result(client, in_thread, "vc.mute", {"call": str(CHANNEL_ID)})
        assert muted["muted"] is True
        assert muted["media"] == "none"
        request = group_call.called("EditGroupCallParticipantRequest")[0]
        assert type(request.participant).__name__ == "InputPeerSelf"

    async def test_muting_somebody_else_warns_about_the_for_me_case(
        self, live_daemon, client, in_thread, group_call
    ):
        envelope = await call(
            client, in_thread, "vc.mute", {"call": str(CHANNEL_ID), "peer": "@alice"}
        )
        assert any("mute-for-me" in w for w in envelope["meta"]["warnings"])
        assert envelope["result"]["can_self_unmute"] is False

    async def test_unmuting_somebody_restores_can_self_unmute(
        self, live_daemon, client, in_thread, group_call
    ):
        envelope = await call(
            client, in_thread, "vc.unmute", {"call": str(CHANNEL_ID), "peer": "@alice"}
        )
        assert envelope["result"]["can_self_unmute"] is True
        assert any("does not open their microphone" in w for w in envelope["meta"]["warnings"])
        assert group_call.called("EditGroupCallParticipantRequest")[0].muted is False

    async def test_a_hand_is_raised_and_lowered_through_one_flag(
        self, live_daemon, client, in_thread, group_call
    ):
        raised = await result(client, in_thread, "vc.raise-hand", {"call": str(CHANNEL_ID)})
        assert raised["raise_hand"] is True
        lowered = await result(
            client, in_thread, "vc.raise-hand", {"call": str(CHANNEL_ID), "lower": True}
        )
        assert lowered["raise_hand"] is False
        assert group_call.called("EditGroupCallParticipantRequest")[-1].raise_hand is False

    async def test_percent_is_mapped_onto_the_apis_scale(
        self, live_daemon, client, in_thread, group_call
    ):
        volume = await result(
            client,
            in_thread,
            "vc.volume.set",
            {"call": str(CHANNEL_ID), "peer": "@alice", "percent": 150},
        )
        assert volume["volume"] == 150
        assert group_call.called("EditGroupCallParticipantRequest")[0].volume == 15000

    async def test_a_volume_over_two_hundred_is_refused(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(
            client,
            in_thread,
            "vc.volume.set",
            {"call": str(CHANNEL_ID), "peer": "@alice", "percent": 900},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_removing_a_participant_restricts_them_in_the_chat(
        self, live_daemon, client, in_thread, group_call
    ):
        removed = await result(
            client, in_thread, "vc.remove", {"chat": str(CHANNEL_ID), "peer": "@alice"}
        )
        assert removed["removed"] is True
        assert group_call.called("EditBannedRequest")[0].banned_rights.send_messages is True

    async def test_ban_takes_view_messages_away_too(
        self, live_daemon, client, in_thread, group_call
    ):
        await result(
            client,
            in_thread,
            "vc.remove",
            {"chat": str(CHANNEL_ID), "peer": "@alice", "ban": True},
        )
        assert group_call.called("EditBannedRequest")[0].banned_rights.view_messages is True


class TestVideoChatVideoAndLinks:
    async def test_announcing_the_camera_says_no_frames_are_sent(
        self, live_daemon, client, in_thread, group_call
    ):
        envelope = await call(
            client, in_thread, "vc.video.set", {"call": str(CHANNEL_ID), "on": True}
        )
        assert envelope["result"]["video_stopped"] is False
        assert envelope["result"]["media"] == "none"
        assert any("no camera" in w for w in envelope["meta"]["warnings"])

    async def test_on_and_off_together_are_a_usage_error(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(
            client, in_thread, "vc.video.set", {"call": str(CHANNEL_ID), "on": True, "off": True}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_sharing_a_screen_needs_its_own_payload(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(
            client,
            in_thread,
            "vc.video.set",
            {"call": str(CHANNEL_ID), "screen": True, "on": True},
        )
        assert error.exit_code == EXIT_USAGE
        assert "second tgcalls connection" in str(error)

    async def test_stopping_a_presentation_is_a_safe_control_call(
        self, live_daemon, client, in_thread, group_call
    ):
        state = await result(
            client,
            in_thread,
            "vc.video.set",
            {"call": str(CHANNEL_ID), "screen": True, "off": True},
        )
        assert state["presentation"] is False
        assert group_call.called("LeaveGroupCallPresentationRequest")

    async def test_the_listener_link_is_exported(self, live_daemon, client, in_thread, group_call):
        link = await result(client, in_thread, "vc.link", {"chat": str(CHANNEL_ID)})
        assert link["kind"] == "listener"
        assert link["link"].endswith("listener")

    async def test_the_speaker_link_sets_can_self_unmute(
        self, live_daemon, client, in_thread, group_call
    ):
        link = await result(
            client, in_thread, "vc.link", {"chat": str(CHANNEL_ID), "speaker": True}
        )
        assert link["kind"] == "speaker"
        assert group_call.called("ExportGroupCallInviteRequest")[0].can_self_unmute is True

    async def test_a_private_chat_falls_back_to_the_chat_invite(
        self, live_daemon, client, in_thread, group_call
    ):
        from telethon.errors.rpcerrorlist import GroupcallForbiddenError

        def refuse(request: Any) -> Any:
            raise GroupcallForbiddenError(request)

        group_call.raw["ExportGroupCallInviteRequest"] = refuse
        envelope = await call(client, in_thread, "vc.link", {"chat": str(CHANNEL_ID)})
        assert envelope["result"]["fallback"] is True
        assert envelope["result"]["link"] == "https://t.me/+fallback"

    async def test_revoking_resets_the_invite_hash(
        self, live_daemon, client, in_thread, group_call
    ):
        link = await result(client, in_thread, "vc.link", {"chat": str(CHANNEL_ID), "revoke": True})
        assert link["revoked"] is True
        assert group_call.called("ToggleGroupCallSettingsRequest")[0].reset_invite_hash is True


class TestVideoChatInviteAndRtmp:
    async def test_inviting_a_member_reports_them_as_invited(
        self, live_daemon, client, in_thread, group_call
    ):
        invited = await result(
            client, in_thread, "vc.invite", {"chat": str(CHANNEL_ID), "user": ["@alice"]}
        )
        assert invited["invited"][0]["id"] == ALICE
        assert group_call.called("InviteToGroupCallRequest")

    async def test_a_refused_invite_is_classified_not_raised(
        self, live_daemon, client, in_thread, group_call
    ):
        from telethon.errors.rpcerrorlist import UserPrivacyRestrictedError

        def refuse(request: Any) -> Any:
            raise UserPrivacyRestrictedError(request)

        group_call.raw["InviteToGroupCallRequest"] = refuse
        invited = await result(
            client, in_thread, "vc.invite", {"chat": str(CHANNEL_ID), "user": ["@alice"]}
        )
        assert invited["failed"][0]["reason"] == "privacy-restricted"
        assert invited["link"], "the fallback link is offered rather than the invite failing"

    async def test_inviting_nobody_is_a_usage_error(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(client, in_thread, "vc.invite", {"chat": str(CHANNEL_ID)})
        assert error.exit_code == EXIT_USAGE

    async def test_the_stream_key_is_masked_by_default(
        self, live_daemon, client, in_thread, group_call
    ):
        envelope = await call(client, in_thread, "vc.rtmp.get", {"chat": str(CHANNEL_ID)})
        info = envelope["result"]
        assert info["key"] != group_call.rtmp_key
        assert "…" in info["key"]
        assert any("masked" in w for w in envelope["meta"]["warnings"])

    async def test_show_key_prints_it_in_full(self, live_daemon, client, in_thread, group_call):
        info = await result(
            client, in_thread, "vc.rtmp.get", {"chat": str(CHANNEL_ID), "show_key": True}
        )
        assert info["key"] == group_call.rtmp_key
        assert info["key_shown"] is True

    async def test_a_key_file_is_written_privately(
        self, live_daemon, client, in_thread, group_call, tmp_path
    ):
        target = tmp_path / "key.txt"
        info = await result(
            client,
            in_thread,
            "vc.rtmp.get",
            {"chat": str(CHANNEL_ID), "key_file": str(target)},
        )
        assert info["key_file"] == str(target)
        assert target.read_text() == group_call.rtmp_key
        assert oct(target.stat().st_mode)[-3:] == "600"

    async def test_revoking_warns_that_the_encoder_stops(
        self, live_daemon, client, in_thread, group_call
    ):
        envelope = await call(
            client, in_thread, "vc.rtmp.get", {"chat": str(CHANNEL_ID), "revoke": True}
        )
        assert envelope["result"]["revoked"] is True
        assert group_call.called("GetGroupCallStreamRtmpUrlRequest")[0].revoke is True


class TestInCallMessages:
    async def test_a_message_is_length_checked_locally(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(
            client, in_thread, "vc.send", {"call": str(CHANNEL_ID), "text": "x" * 200}
        )
        assert error.exit_code == EXIT_USAGE
        assert group_call.called("SendGroupCallMessageRequest") == []

    async def test_a_message_carries_the_overlay_lifetime(
        self, live_daemon, client, in_thread, group_call
    ):
        sent = await result(
            client, in_thread, "vc.send", {"call": str(CHANNEL_ID), "text": "hello"}
        )
        assert sent["ttl"] == 10
        assert group_call.called("SendGroupCallMessageRequest")[0].message.text == "hello"

    async def test_messages_off_is_a_permission_error(
        self, live_daemon, client, in_thread, group_call
    ):
        group_call.group_calls[CALL_ID].messages_enabled = False
        error = await failure(client, in_thread, "vc.send", {"call": str(CHANNEL_ID), "text": "hi"})
        assert error.exit_code == EXIT_PERMISSION

    async def test_a_custom_emoji_is_one_entity_over_the_fallback_text(
        self, live_daemon, client, in_thread, group_call
    ):
        await result(
            client,
            in_thread,
            "vc.send",
            {"call": str(CHANNEL_ID), "text": "🔥", "custom_emoji": 12345},
        )
        entities = group_call.called("SendGroupCallMessageRequest")[0].message.entities
        assert type(entities[0]).__name__ == "MessageEntityCustomEmoji"
        assert entities[0].document_id == 12345

    async def test_stars_are_never_spent_implicitly(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(
            client, in_thread, "vc.send", {"call": str(CHANNEL_ID), "text": "hi", "stars": 5}
        )
        assert error.exit_code == EXIT_USAGE
        assert "--confirm-stars" in str(error)
        assert group_call.called("SendGroupCallMessageRequest") == []

    async def test_confirmed_stars_reach_the_request(
        self, live_daemon, client, in_thread, group_call
    ):
        sent = await result(
            client,
            in_thread,
            "vc.send",
            {"call": str(CHANNEL_ID), "text": "hi", "stars": 5, "confirm_stars": True},
        )
        assert sent["paid_message_stars"] == 5
        assert group_call.called("SendGroupCallMessageRequest")[0].allow_paid_stars == 5

    async def test_deleting_by_id_uses_the_message_rpc(
        self, live_daemon, client, in_thread, group_call
    ):
        deleted = await result(
            client, in_thread, "vc.message.delete", {"call": str(CHANNEL_ID), "id": [7, 8]}
        )
        assert deleted["deleted"] == [7, 8]
        assert group_call.called("DeleteGroupCallMessagesRequest")[0].messages == [7, 8]

    async def test_deleting_by_participant_uses_the_other_rpc(
        self, live_daemon, client, in_thread, group_call
    ):
        deleted = await result(
            client,
            in_thread,
            "vc.message.delete",
            {"call": str(CHANNEL_ID), "from_peer": "@alice", "report_spam": True},
        )
        assert deleted["reported"] is True
        assert group_call.called("DeleteGroupCallParticipantMessagesRequest")[0].report_spam

    async def test_deleting_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(client, in_thread, "vc.message.delete", {"call": str(CHANNEL_ID)})
        assert error.exit_code == EXIT_USAGE


class TestStreamDownload:
    async def test_a_livestream_is_written_to_disk(
        self, live_daemon, client, in_thread, group_call, tmp_path
    ):
        target = tmp_path / "stream.ogg"
        recorded = await result(
            client,
            in_thread,
            "vc.download",
            {"call": str(CHANNEL_ID), "out": str(target), "duration": 2},
        )
        assert recorded["chunks"] == 2
        assert recorded["bytes"] == len(group_call.stream_chunk) * 2
        assert target.read_bytes().startswith(b"OggS")
        assert recorded["media"] == "none"

    async def test_the_daemon_will_not_write_to_your_stdout(
        self, live_daemon, client, in_thread, group_call
    ):
        error = await failure(
            client, in_thread, "vc.download", {"call": str(CHANNEL_ID), "out": "-"}
        )
        assert error.exit_code == EXIT_USAGE
        assert "the daemon" in str(error)

    async def test_an_idle_publisher_is_not_found_rather_than_an_empty_file(
        self, live_daemon, client, in_thread, group_call, tmp_path
    ):
        from telethon.tl import types

        group_call.raw["GetGroupCallStreamChannelsRequest"] = lambda request: (
            types.phone.GroupCallStreamChannels(channels=[])
        )
        error = await failure(
            client,
            in_thread,
            "vc.download",
            {"call": str(CHANNEL_ID), "out": str(tmp_path / "x.ogg")},
        )
        assert error.exit_code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# Conferences
# ---------------------------------------------------------------------------


class TestConference:
    async def test_a_call_link_needs_no_crypto_at_all(self, live_daemon, client, in_thread, peers):
        created = await result(client, in_thread, "conference.create", {})
        assert created["invite_link"] == "https://t.me/call/AbCdEf"
        assert created["slug"] == "AbCdEf"
        assert created["joined"] is False
        assert peers.called("CreateConferenceCallRequest")[0].join is None

    async def test_creating_and_joining_needs_a_block(self, live_daemon, client, in_thread, peers):
        error = await failure(client, in_thread, "conference.create", {"join": True})
        assert error.exit_code == EXIT_USAGE
        assert "e2e.chain block" in str(error)
        assert peers.called("CreateConferenceCallRequest") == []

    async def test_a_conference_is_read_by_its_link(self, live_daemon, client, in_thread, peers):
        peers.add_group_call(
            900300, conference=True, invite_link="https://t.me/call/AbCdEf", title=None
        )
        info = await result(
            client, in_thread, "conference.get", {"call": "https://t.me/call/AbCdEf"}
        )
        assert info["slug"] == "AbCdEf"
        assert info["conference"] is True

    async def test_an_invitation_message_addresses_the_call(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(900300, conference=True, title=None)
        info = await result(client, in_thread, "conference.get", {"call": "msg:900"})
        assert info["call"]["id"] == 900300
        request = peers.called("GetGroupCallRequest")[0]
        assert type(request.call).__name__ == "InputGroupCallInviteMessage"

    async def test_the_qr_flag_hands_back_the_text_to_encode(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(
            900300, conference=True, invite_link="https://t.me/call/AbCdEf", title=None
        )
        envelope = await call(client, in_thread, "conference.get", {"call": "AbCdEf", "qr": True})
        assert envelope["result"]["qr"] == "https://t.me/call/AbCdEf"
        assert any("does not bundle a QR encoder" in w for w in envelope["meta"]["warnings"])

    async def test_inviting_rings_each_user(self, live_daemon, client, in_thread, peers):
        peers.add_group_call(900300, conference=True, title=None)
        invited = await result(
            client, in_thread, "conference.invite", {"call": "AbCdEf", "user": ["@alice"]}
        )
        assert invited["invited"][0]["id"] == ALICE
        assert peers.called("InviteConferenceCallParticipantRequest")

    async def test_declining_needs_only_the_service_message_id(
        self, live_daemon, client, in_thread, peers
    ):
        declined = await result(client, in_thread, "conference.decline", {"msg_id": 900})
        assert declined == {"msg_id": 900, "declined": True}
        assert peers.called("DeclineConferenceCallInviteRequest")[0].msg_id == 900

    async def test_joining_refuses_before_it_asks_the_server(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(900300, conference=True, title=None)
        error = await failure(client, in_thread, "conference.join", {"call": "AbCdEf"})
        assert error.exit_code == EXIT_USAGE
        assert peers.called("JoinGroupCallRequest") == []

    async def test_joining_with_the_material_sends_the_block(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        peers.add_group_call(900300, conference=True, title=None)
        block = tmp_path / "b.bin"
        block.write_bytes(b"join-block")
        params = tmp_path / "p.json"
        params.write_text("{}", encoding="utf-8")
        joined = await result(
            client,
            in_thread,
            "conference.join",
            {
                "call": "AbCdEf",
                "block": str(block),
                "public_key": "ff",
                "params_json": str(params),
            },
        )
        assert joined["media"] == "none"
        request = peers.called("JoinGroupCallRequest")[0]
        assert request.block == b"join-block"
        assert type(request.join_as).__name__ == "InputPeerSelf"

    async def test_removing_refuses_without_a_key_rotating_block(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(900300, conference=True, title=None)
        error = await failure(
            client, in_thread, "conference.remove", {"call": "AbCdEf", "user": ["@alice"]}
        )
        assert error.exit_code == EXIT_USAGE
        assert peers.called("DeleteConferenceCallParticipantsRequest") == []

    async def test_pruning_the_left_participants_is_a_flag(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        peers.add_group_call(900300, conference=True, title=None)
        block = tmp_path / "b.bin"
        block.write_bytes(b"prune")
        removed = await result(
            client,
            in_thread,
            "conference.remove",
            {"call": "AbCdEf", "left_only": True, "block": str(block)},
        )
        assert removed["only_left"] is True
        assert peers.called("DeleteConferenceCallParticipantsRequest")[0].only_left is True

    async def test_revoking_a_link_resets_the_hash(self, live_daemon, client, in_thread, peers):
        peers.add_group_call(900300, conference=True, title=None)
        revoked = await result(client, in_thread, "conference.revoke", {"call": "AbCdEf"})
        assert revoked["revoked"] is True
        assert peers.called("ToggleGroupCallSettingsRequest")[0].reset_invite_hash is True

    async def test_sending_without_a_payload_is_refused(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(900300, conference=True, title=None)
        error = await failure(client, in_thread, "conference.send", {"call": "AbCdEf"})
        assert error.exit_code == EXIT_USAGE
        assert "no shared key" in str(error)

    async def test_an_encrypted_blob_is_carried_verbatim(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        peers.add_group_call(900300, conference=True, title=None)
        blob = tmp_path / "m.bin"
        blob.write_bytes(b"\x00\x01cipher")
        sent = await result(
            client,
            in_thread,
            "conference.send",
            {"call": "AbCdEf", "encrypted_blob": str(blob)},
        )
        assert sent["kind"] == "encrypted-message"
        assert peers.called("SendGroupCallEncryptedMessageRequest")[0].encrypted_message == (
            b"\x00\x01cipher"
        )

    async def test_a_broadcast_block_goes_to_the_other_rpc(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        peers.add_group_call(900300, conference=True, title=None)
        block = tmp_path / "n.bin"
        block.write_bytes(b"nonce")
        sent = await result(
            client,
            in_thread,
            "conference.send",
            {"call": "AbCdEf", "broadcast_block": str(block)},
        )
        assert sent["kind"] == "broadcast"
        assert peers.called("SendConferenceCallBroadcastRequest")[0].block == b"nonce"

    async def test_the_chain_comes_back_as_base64_with_heights(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(900300, conference=True, title=None)
        items = await rows(client, in_thread, "conference.chain.list", {"call": "AbCdEf"})
        assert [row["height"] for row in items] == [0, 1]
        assert base64.b64decode(items[0]["block"]) == b"block-0"

    async def test_the_tip_is_one_block_at_offset_minus_one(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_group_call(900300, conference=True, title=None)
        items = await rows(
            client, in_thread, "conference.chain.list", {"call": "AbCdEf", "tip": True}
        )
        assert len(items) == 1
        assert peers.called("GetGroupCallChainBlocksRequest")[0].offset == -1


# ---------------------------------------------------------------------------
# Streaming operations, driven through their implementations
# ---------------------------------------------------------------------------


class _StreamCtx:
    """The slice of `OpContext` a streaming call operation actually touches."""

    dry_run = False
    request_id = "t"
    limit = None
    cursor = None

    def __init__(self, client: Any, account: str = "work") -> None:
        self.client = client
        self.account = account
        self.warnings: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.session = None

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def emit(self, event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        self.events.append((event_type, payload))


async def _first_page(generator: Any) -> Any:
    async for page in generator:
        return page
    return None


class TestStreams:
    async def test_call_watch_reports_a_ring_and_who_is_calling(self, world):
        import asyncio

        from fake_telethon import FakeTelegramClient

        from tlgr.ops.call import WatchReq, watch

        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        stream = watch(ctx, WatchReq())
        page_task = asyncio.create_task(_first_page(stream))
        await asyncio.sleep(0)
        incoming = world.add_phone_call(777010, state="Requested", admin_id=ALICE)
        from telethon.tl import types

        await client.feed(types.UpdatePhoneCall(phone_call=incoming))
        page = await asyncio.wait_for(page_task, timeout=2)
        assert page.items[0].kind == "call.requested"
        assert page.items[0].should_ring is True
        await stream.aclose()

    async def test_call_watch_can_answer_the_busy_lock_itself(self, world):
        import asyncio

        from fake_telethon import FakeTelegramClient
        from telethon.tl import types

        from tlgr.ops.call import WatchReq, watch

        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        stream = watch(ctx, WatchReq(auto_ack=True))
        page_task = asyncio.create_task(_first_page(stream))
        await asyncio.sleep(0)
        await client.feed(
            types.UpdatePhoneCall(phone_call=world.add_phone_call(777011, state="Requested"))
        )
        await asyncio.wait_for(page_task, timeout=2)
        assert world.called("ReceivedCallRequest")
        await stream.aclose()

    async def test_a_conference_invitation_is_folded_into_the_same_stream(self, world):
        import asyncio

        from fake_telethon import FakeTelegramClient
        from telethon.tl import types

        from tlgr.ops.call import WatchReq, watch

        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        stream = watch(ctx, WatchReq())
        page_task = asyncio.create_task(_first_page(stream))
        await asyncio.sleep(0)
        message = types.MessageService(
            id=901,
            peer_id=types.PeerUser(user_id=ALICE),
            action=types.MessageActionConferenceCall(call_id=5, video=True),
        )
        await client.feed(types.UpdateNewMessage(message=message, pts=1, pts_count=1))
        page = await asyncio.wait_for(page_task, timeout=2)
        assert page.items[0].kind == "conference.invite"
        assert page.items[0].msg_id == 901
        await stream.aclose()

    async def test_a_signalling_packet_goes_out_as_bytes_and_comes_back_as_base64(self, world):
        from fake_telethon import FakeTelegramClient

        from tlgr.ops import _calls
        from tlgr.ops.call import SignalReq, signal

        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        _calls.remember_call("work", _calls.LiveCall(id=55, access_hash=9))
        page = await _first_page(signal(ctx, SignalReq(call="55", data="AAEC")))
        assert page.items[0].direction == "out"
        assert world.called("SendSignalingDataRequest")[0].data == base64.b64decode("AAEC")

    async def test_signalling_with_nothing_to_send_or_follow_is_a_usage_error(self, world):
        from fake_telethon import FakeTelegramClient

        from tlgr.core.errors import UsageError
        from tlgr.ops import _calls
        from tlgr.ops.call import SignalReq, signal

        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        _calls.remember_call("work", _calls.LiveCall(id=56, access_hash=9))
        with pytest.raises(UsageError):
            await _first_page(signal(ctx, SignalReq(call="56")))

    async def test_vc_watch_applies_participants_and_notices_a_version_gap(self, world):
        import asyncio

        from fake_telethon import FakeTelegramClient
        from telethon.tl import types

        from tlgr.ops.vc import WatchReq, watch

        world.add_group_call(CALL_ID)
        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        stream = watch(ctx, WatchReq(call=f"{CALL_ID}:{CALL_ID * 3}"))

        first = asyncio.create_task(_first_page(stream))
        await asyncio.sleep(0)
        call_ref = types.InputGroupCall(id=CALL_ID, access_hash=CALL_ID * 3)
        await client.feed(
            types.UpdateGroupCallParticipants(
                call=call_ref,
                participants=[
                    types.GroupCallParticipant(
                        peer=types.PeerUser(user_id=ALICE), date=None, source=222
                    )
                ],
                version=2,
            )
        )
        page = await asyncio.wait_for(first, timeout=2)
        assert page.items[0].kind == "participants"
        assert page.items[0].resync is False

        second = asyncio.create_task(_first_page(stream))
        await asyncio.sleep(0)
        await client.feed(
            types.UpdateGroupCallParticipants(call=call_ref, participants=[], version=9)
        )
        gap = await asyncio.wait_for(second, timeout=2)
        assert gap.items[0].resync is True, "a version gap must be reported, not smoothed over"
        await stream.aclose()

    async def test_vc_watch_is_the_only_way_to_read_the_in_call_chat(self, world):
        import asyncio

        from fake_telethon import FakeTelegramClient
        from telethon.tl import types

        from tlgr.ops.vc import WatchReq, watch

        world.add_group_call(CALL_ID)
        client = FakeTelegramClient(world)
        ctx = _StreamCtx(client)
        stream = watch(ctx, WatchReq(call=f"{CALL_ID}:{CALL_ID * 3}"))
        page_task = asyncio.create_task(_first_page(stream))
        await asyncio.sleep(0)
        update_type = getattr(types, "UpdateGroupCallMessage", None)
        if update_type is None:  # pragma: no cover - present in layer 227
            pytest.skip("this Telethon has no updateGroupCallMessage")
        await client.feed(
            update_type(
                call=types.InputGroupCall(id=CALL_ID, access_hash=CALL_ID * 3),
                message=types.GroupCallMessage(
                    id=7,
                    from_id=types.PeerUser(user_id=ALICE),
                    date=None,
                    message=types.TextWithEntities(text="🔥", entities=[]),
                )
                if hasattr(types, "GroupCallMessage")
                else None,
            )
        )
        page = await asyncio.wait_for(page_task, timeout=2)
        assert page.items[0].kind == "message"
        await stream.aclose()


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


class TestSurface:
    def test_every_alias_reaches_its_operation(self):
        from tlgr.registry import canonical

        assert canonical("call add-people") == "call.invite"
        assert canonical("conf create") == "conference.create"
        assert canonical("vc participants") == "vc.participant.list"
        assert canonical("vc allow-speak") == "vc.unmute"
        assert canonical("conference leave") == "vc.leave"
        assert canonical("conference end") == "vc.end"
        assert canonical("story live rtmp") == "vc.rtmp.get"

    def test_the_group_never_claims_to_carry_media(self):
        """Every shape a reader could mistake for "you are in a call" says no."""
        import msgspec

        from tlgr.registry import REGISTRY

        for op_id in (
            "call.start",
            "call.accept",
            "vc.join",
            "vc.mute",
            "vc.video.set",
            "conference.join",
        ):
            example = msgspec.to_builtins(REGISTRY[op_id].example)
            assert example.get("media") == "none", op_id

    def test_the_e2e_gated_operations_say_what_is_missing(self):
        """A refusal that does not name the missing piece is a dead end."""
        from tlgr.registry import REGISTRY

        for op_id in ("conference.join", "conference.remove", "call.invite"):
            assert "block" in " ".join(REGISTRY[op_id].coverage_note.split())
