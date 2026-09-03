"""The message operations, end to end through a real daemon.

Every test here goes over a real Unix socket, through the real middleware
chain and the real dispatcher, into the real implementation, against a fake
Telegram. That is the only arrangement in which "did the send actually work"
means anything: the assertion is usually that the fake's *world changed*, not
that a request object looked plausible.
"""

from __future__ import annotations

from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_AUTH,
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_RATE_LIMITED,
    EXIT_RETRYABLE,
    EXIT_SPAM_FLAGGED,
    EXIT_USAGE,
    classify,
)

ALICE = 4242
CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL


@pytest.fixture
def peers(world):
    """One user and one channel this account can address."""
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_channel(make_channel(CHANNEL, title="News"))
    return world


@pytest.fixture
def history(peers, world):
    for index in range(1, 6):
        world.add_message(ALICE, f"message {index}", message_id=100 + index)
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


class TestSend:
    async def test_a_text_send_lands_in_the_history(self, live_daemon, client, in_thread, peers):
        sent = await result(client, in_thread, "message.send", {"chat": "@alice", "text": "hi"})
        assert sent["text"] == "hi"
        assert sent["chat_id"] == ALICE
        assert sent["out"] is True
        assert [m.message for m in peers.history(ALICE)] == ["hi"]

    async def test_markdown_is_parsed_into_entities_with_utf16_offsets(
        self, live_daemon, client, in_thread, peers
    ):
        """An emoji is one character and two UTF-16 units — the classic bug."""
        await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "🎉 **loud**", "parse": "md"},
        )
        request = peers.called("SendMessageRequest")[0]
        assert request.message == "🎉 loud"
        bold = [e for e in request.entities if type(e).__name__ == "MessageEntityBold"]
        assert bold and bold[0].offset == 3, "the emoji must count as two units"

    async def test_the_default_parse_mode_is_none(self, live_daemon, client, in_thread, peers):
        """COR-21: v1 silently ate underscores and asterisks."""
        sent = await result(
            client, in_thread, "message.send", {"chat": "@alice", "text": "a_b_c *x*"}
        )
        assert sent["text"] == "a_b_c *x*"

    async def test_a_reply_carries_its_target(self, live_daemon, client, in_thread, history):
        await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "yes", "reply_to": 103},
        )
        request = history.called("SendMessageRequest")[0]
        assert request.reply_to.reply_to_msg_id == 103

    async def test_a_quote_is_sent_with_the_reply(self, live_daemon, client, in_thread, history):
        await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "this bit", "reply_to": 103, "quote": "message"},
        )
        request = history.called("SendMessageRequest")[0]
        assert request.reply_to.quote_text == "message"

    async def test_schedule_online_is_the_sentinel_not_a_time(
        self, live_daemon, client, in_thread, peers
    ):
        from tlgr.ops._send import SCHEDULE_ONLINE

        await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "later", "schedule": "online"},
        )
        request = peers.called("SendMessageRequest")[0]
        assert int(request.schedule_date.timestamp()) == SCHEDULE_ONLINE

    async def test_silent_and_protect_reach_the_request(
        self, live_daemon, client, in_thread, peers
    ):
        await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "quiet", "silent": True, "protect": True},
        )
        request = peers.called("SendMessageRequest")[0]
        assert request.silent is True
        assert request.noforwards is True

    async def test_over_long_text_is_refused_unless_split(
        self, live_daemon, client, in_thread, peers
    ):
        long_text = "x" * 5000
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "message.send", {"chat": "@alice", "text": long_text})
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert "--split" in str(caught.value)

    async def test_split_sends_every_part_and_reports_the_siblings(
        self, live_daemon, client, in_thread, peers
    ):
        sent = await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "y " * 3000, "split": True},
        )
        assert len(peers.called("SendMessageRequest")) >= 2
        assert sent["batch"], "the other parts' ids must be reported"

    async def test_a_dice_is_media_not_text(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "message.send", {"chat": "@alice", "dice": "🎯"})
        request = peers.called("SendMediaRequest")[0]
        assert request.media.emoticon == "🎯"

    async def test_a_location_needs_lat_lon(self, live_daemon, client, in_thread, peers):
        await result(
            client, in_thread, "message.send", {"chat": "@alice", "location": "51.5,-0.12"}
        )
        request = peers.called("SendMediaRequest")[0]
        assert request.media.geo_point.lat == pytest.approx(51.5)

    async def test_a_rich_body_is_refused_with_not_supported(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        """Exit 13 with NOT_SUPPORTED: the layer lacks it, the send did not fail."""
        body = tmp_path / "body.md"
        body.write_text("# hello")
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "message.send",
                {"chat": "@alice", "rich_markdown": str(body)},
            )
        error = classify(caught.value)
        assert error.exit_code == EXIT_INDETERMINATE
        assert error.code == "NOT_SUPPORTED"

    async def test_typing_is_shown_before_the_send(self, live_daemon, client, in_thread, peers):
        await result(
            client,
            in_thread,
            "message.send",
            {"chat": "@alice", "text": "one two three", "typing_auto": True},
        )
        assert peers.called("action"), "--typing-auto must actually show a typing action"

    async def test_a_send_is_echoed_onto_the_event_bus(self, live_daemon, client, in_thread, peers):
        """§6.5: Telethon dispatches no NewMessage for our own sends."""
        seen: list[Any] = []
        live_daemon.bus.add_handler(lambda envelope, raw: seen.append(envelope))
        await result(client, in_thread, "message.send", {"chat": "@alice", "text": "hi"})
        assert any(event.type == "message_out" for event in seen)


class TestListAndSearch:
    async def test_history_comes_back_newest_first(self, live_daemon, client, in_thread, history):
        page = await call(client, in_thread, "message.list", {"chat": "@alice"})
        assert [item["id"] for item in page["result"]] == [105, 104, 103, 102, 101]

    async def test_reverse_walks_forwards(self, live_daemon, client, in_thread, history):
        page = await call(client, in_thread, "message.list", {"chat": "@alice", "reverse": True})
        assert [item["id"] for item in page["result"]] == [101, 102, 103, 104, 105]

    async def test_a_cursor_continues_where_the_page_stopped(
        self, live_daemon, client, in_thread, history
    ):
        first = await call(client, in_thread, "message.list", {"chat": "@alice"}, limit=2)
        assert first["page"]["has_more"] is True
        token = first["page"]["next_cursor"]
        assert token
        second = await call(
            client, in_thread, "message.list", {"chat": "@alice"}, limit=2, cursor=token
        )
        assert [item["id"] for item in second["result"]] == [103, 102]

    async def test_a_hand_edited_cursor_is_a_usage_error_not_a_restart(
        self, live_daemon, client, in_thread, history
    ):
        with pytest.raises(Exception) as caught:
            await call(
                client, in_thread, "message.list", {"chat": "@alice"}, cursor="nonsense.token"
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_explicit_ids_bypass_the_offsets(self, live_daemon, client, in_thread, history):
        page = await call(client, in_thread, "message.list", {"chat": "@alice", "ids": ["102"]})
        assert [item["id"] for item in page["result"]] == [102]

    async def test_an_id_range_expands(self, live_daemon, client, in_thread, history):
        page = await call(client, in_thread, "message.list", {"chat": "@alice", "ids": ["102-104"]})
        assert [item["id"] for item in page["result"]] == [102, 103, 104]

    async def test_search_filters_by_text(self, live_daemon, client, in_thread, history):
        page = await call(
            client, in_thread, "message.search", {"chat": "@alice", "query": "message 3"}
        )
        assert [item["id"] for item in page["result"]] == [103]

    async def test_a_non_ascii_query_survives_the_wire(
        self, live_daemon, client, in_thread, peers, world
    ):
        """COR-04: a Persian query never arrived in v1."""
        world.add_message(ALICE, "سلام دنیا", message_id=200)
        page = await call(client, in_thread, "message.search", {"chat": "@alice", "query": "سلام"})
        assert [item["id"] for item in page["result"]] == [200]

    async def test_regex_search_is_local_and_says_so(self, live_daemon, client, in_thread, history):
        page = await call(
            client, in_thread, "message.search", {"chat": "@alice", "regex": r"message [45]"}
        )
        assert sorted(item["id"] for item in page["result"]) == [104, 105]
        assert any("local scan" in w for w in page["meta"]["warnings"])

    async def test_an_unknown_media_filter_is_a_usage_error(
        self, live_daemon, client, in_thread, history
    ):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "message.list", {"chat": "@alice", "type": "banana"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_all_walks_every_page_inside_the_daemon(
        self, live_daemon, client, in_thread, history
    ):
        client._ready = True
        frames = await in_thread(
            lambda: list(
                client.op_stream("message.list", {"chat": "@alice"}, account="work", limit=2)
            )
        )
        kinds = [frame["type"] for frame in frames]
        assert kinds[0] == "meta" and kinds[-1] == "end"
        assert frames[-1]["ok"] is True


class TestGet:
    async def test_a_missing_message_is_not_found(self, live_daemon, client, in_thread, history):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "message.get", {"chat": "@alice", "msg_id": 999})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_with_reply_resolves_the_parent(self, live_daemon, client, in_thread, history):
        from telethon.tl import types

        target = history.find(ALICE, 105)
        target.reply_to = types.MessageReplyHeader(reply_to_msg_id=101)
        found = await result(
            client, in_thread, "message.get", {"chat": "@alice", "msg_id": 105, "with_reply": True}
        )
        assert found["reply"]["id"] == 101

    async def test_context_returns_the_neighbours(self, live_daemon, client, in_thread, history):
        found = await result(
            client, in_thread, "message.get", {"chat": "@alice", "msg_id": 103, "context": 2}
        )
        assert found["context"], "--context must return the surrounding messages"
        assert 103 not in [item["id"] for item in found["context"]]


class TestEditDeleteForward:
    async def test_an_edit_changes_the_text(self, live_daemon, client, in_thread, history):
        edited = await result(
            client,
            in_thread,
            "message.edit",
            {"chat": "@alice", "msg_id": 103, "text": "fixed"},
        )
        assert edited["text"] == "fixed"
        assert history.find(ALICE, 103).message == "fixed"

    async def test_message_not_modified_is_already_not_an_error(
        self, live_daemon, client, in_thread, history
    ):
        from telethon.errors import MessageNotModifiedError

        history.fail_next("EditMessageRequest", MessageNotModifiedError(None))
        envelope = await call(
            client,
            in_thread,
            "message.edit",
            {"chat": "@alice", "msg_id": 103, "text": "message 3"},
        )
        assert envelope["ok"] is True
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True

    async def test_check_reports_editability_without_editing(
        self, live_daemon, client, in_thread, history
    ):
        checked = await result(
            client, in_thread, "message.edit", {"chat": "@alice", "msg_id": 103, "check": True}
        )
        assert checked["can_edit"] is True
        assert history.find(ALICE, 103).message == "message 3"

    async def test_delete_removes_the_messages(self, live_daemon, client, in_thread, history):
        deleted = await result(
            client, in_thread, "message.delete", {"chat": "@alice", "msg_id": ["102", "103"]}
        )
        assert deleted["ids"] == [102, 103]
        assert history.find(ALICE, 102) is None

    async def test_delete_needs_ids_or_a_sender(self, live_daemon, client, in_thread, history):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "message.delete", {"chat": "@alice"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_forward_reaches_every_destination(self, live_daemon, client, in_thread, history):
        page = await call(
            client,
            in_thread,
            "message.forward",
            {"chat": "@alice", "msg_id": ["101"], "to": ["@alice", str(CHANNEL_ID)]},
        )
        assert len(history.called("ForwardMessagesRequest")) == 2
        assert {item["chat_id"] for item in page["result"]["items"]} == {ALICE, CHANNEL_ID}

    async def test_as_copy_drops_the_author(self, live_daemon, client, in_thread, history):
        await call(
            client,
            in_thread,
            "message.forward",
            {"chat": "@alice", "msg_id": ["101"], "to": ["@alice"], "as_copy": True},
        )
        assert history.called("ForwardMessagesRequest")[0].drop_author is True

    async def test_forward_without_a_destination_is_a_usage_error(
        self, live_daemon, client, in_thread, history
    ):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "message.forward", {"chat": "@alice", "msg_id": ["101"]})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestPinAndRead:
    async def test_pin_is_silent_by_default(self, live_daemon, client, in_thread, history):
        pinned = await result(client, in_thread, "message.pin", {"chat": "@alice", "msg_id": 103})
        assert pinned["pinned"] is True
        assert history.called("UpdatePinnedMessageRequest")[0].silent is True

    async def test_unpin_all_loops_the_affected_history(
        self, live_daemon, client, in_thread, history
    ):
        await result(client, in_thread, "message.pin", {"chat": "@alice", "msg_id": 103})
        out = await result(
            client, in_thread, "message.unpin", {"chat": "@alice", "unpin_all": True}
        )
        assert out["unpinned"] == 1
        assert history.pinned[ALICE] == set()

    async def test_pinned_messages_are_listed_by_the_filter(
        self, live_daemon, client, in_thread, history
    ):
        await result(client, in_thread, "message.pin", {"chat": "@alice", "msg_id": 103})
        page = await call(client, in_thread, "message.list", {"chat": "@alice", "type": "pinned"})
        assert [item["id"] for item in page["result"]] == [103]

    async def test_read_marks_the_history(self, live_daemon, client, in_thread, history):
        out = await result(client, in_thread, "message.read", {"chat": "@alice", "up_to": 104})
        assert out["read_up_to"] == 104
        assert history.read_inbox[ALICE] == 104

    async def test_unread_only_lists_after_the_divider(
        self, live_daemon, client, in_thread, history
    ):
        history.read_inbox[ALICE] = 103
        page = await call(client, in_thread, "message.list", {"chat": "@alice", "unread": True})
        assert [item["id"] for item in page["result"]] == [105, 104]


class TestSmallSurfaces:
    async def test_a_private_chat_has_no_message_link(
        self, live_daemon, client, in_thread, history
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "message.link", {"chat": "@alice", "msg_id": 103})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_channel_link_falls_back_to_the_private_form(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(
            client, in_thread, "message.link", {"chat": str(CHANNEL_ID), "msg_id": 42}
        )
        assert out["link"].endswith(f"/c/{CHANNEL}/42")

    async def test_entity_list_runs_without_an_account(self, live_daemon, client, in_thread):
        out = await result(
            client,
            in_thread,
            "message.entity.list",
            {"text": "**bold** and `code`", "parse": "md"},
            account="",
        )
        assert out["text"] == "bold and code"
        assert {e["type"] for e in out["entities"]} == {"bold", "code"}
        assert out["length_utf16"] == len("bold and code")

    async def test_entity_list_separates_the_entities_the_server_derives(
        self, live_daemon, client, in_thread
    ):
        out = await result(
            client,
            in_thread,
            "message.entity.list",
            {"text": "see https://t.me #tag", "parse": "md"},
            account="",
        )
        assert {e["type"] for e in out["auto_entities"]} == {"url", "hashtag"}
        # `entities` is what tlgr sends; an automatic entity must never be in
        # it, because the server rejects a message that re-declares one.
        assert "entities" not in out

    async def test_views_do_not_increment_unless_asked(self, live_daemon, client, in_thread, peers):
        await call(
            client,
            in_thread,
            "message.view.get",
            {"chat": str(CHANNEL_ID), "msg_id": ["42"]},
        )
        assert peers.called("GetMessagesViewsRequest")[0].increment is False

    async def test_read_receipts_report_a_privacy_refusal_as_a_reason(
        self, live_daemon, client, in_thread, history
    ):
        def refuse(request: Any) -> Any:
            raise ValueError("USER_PRIVACY_RESTRICTED")

        history.raw["GetMessageReadParticipantsRequest"] = refuse
        history.raw["GetOutboxReadDateRequest"] = refuse
        out = await result(
            client, in_thread, "message.read-receipt.list", {"chat": "@alice", "msg_id": 103}
        )
        assert "hides read dates" in out["unavailable_reason"]

    async def test_translate_needs_a_target_language(self, live_daemon, client, in_thread, history):
        with pytest.raises(Exception) as caught:
            await call(
                client, in_thread, "message.translate", {"chat": "@alice", "msg_id": ["103"]}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_rich_translation_is_not_supported(
        self, live_daemon, client, in_thread, history
    ):
        with pytest.raises(Exception) as caught:
            await call(
                client,
                in_thread,
                "message.translate",
                {"chat": "@alice", "msg_id": ["103"], "lang": "en", "rich": True},
            )
        assert classify(caught.value).code == "NOT_SUPPORTED"

    async def test_a_game_url_is_refused_rather_than_faked(
        self, live_daemon, client, in_thread, peers
    ):
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "message.game.get",
                {"chat": "@alice", "msg_id": 1, "url": True},
            )
        assert classify(caught.value).code == "NOT_SUPPORTED"


class TestTheErrorTableEndToEnd:
    """§12.3 criterion 14: raise in the fake client, read the CLI's exit code.

    `test_errors_map.py` proves the §7.2 table maps an exception name to a
    code and an exit status. That is the mapping in isolation; this is the
    path — a real Telethon exception raised inside a real request, through the
    dispatcher, the error middleware, the socket, and the client's `classify`,
    which is what the exit code is actually built from. One row per exit
    status in the table, because the failure mode being guarded against is a
    layer that swallows or relabels an error, not a wrong table entry.
    """

    ROWS = [
        ("FloodWaitError", EXIT_RATE_LIMITED, "RATE_LIMITED"),
        ("PeerFloodError", EXIT_SPAM_FLAGGED, "PEER_FLOOD"),
        ("AuthKeyUnregisteredError", EXIT_AUTH, "SESSION_ERROR"),
        ("ChatAdminRequiredError", EXIT_PERMISSION, "PERMISSION_DENIED"),
        ("UsernameNotOccupiedError", EXIT_NOT_FOUND, "NOT_FOUND"),
        ("MessageTooLongError", EXIT_USAGE, "USAGE"),
        ("ServerError", EXIT_RETRYABLE, "RETRYABLE"),
    ]

    @pytest.mark.parametrize(("name", "exit_code", "code"), ROWS, ids=[r[0] for r in ROWS])
    async def test_a_raise_in_the_client_arrives_as_its_exit_code(
        self, live_daemon, client, in_thread, peers, name, exit_code, code
    ):
        import telethon.errors as tl_errors

        klass = getattr(tl_errors, name)
        try:
            failure: BaseException = klass(request=None)
        except TypeError:
            # `ServerError` and friends are built from a server reply.
            failure = klass(request=None, message=f"{name} raised", code=500)
        peers.fail_next("SendMessageRequest", failure)

        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "message.send", {"chat": "@alice", "text": "hi"})
        body = classify(caught.value)
        assert (body.code, body.exit_code) == (code, exit_code)

    async def test_not_modified_is_success_and_says_already(
        self, live_daemon, client, in_thread, history
    ):
        """The one row that is not a failure: the world already looks like that."""
        import telethon.errors as tl_errors

        history.fail_next("EditMessageRequest", tl_errors.MessageNotModifiedError(request=None))
        envelope = await call(
            client,
            in_thread,
            "message.edit",
            {"chat": "@alice", "msg_id": 103, "text": "message 3"},
        )
        assert envelope["ok"] is True
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True
