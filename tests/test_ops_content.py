"""The content operations — polls, reactions, checklists, locations, search.

Same arrangement as `test_ops_message.py`: a real Unix socket, the real
middleware chain, the real dispatcher, a fake Telegram. The assertions are
about *the world changing* — a vote lands on the stored poll, a tick lands on
the stored checklist — and, where the exact TL request is the whole point
(option bytes, the `sendReaction` full-state rule, the search offset triple),
about the request the fake recorded.
"""

from __future__ import annotations

from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
)

ALICE = 4242
BOB = 9001
CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL


@pytest.fixture
def peers(world):
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_user(make_user(BOB, username="bob"))
    world.add_channel(make_channel(CHANNEL, title="News"))
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


async def fails(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    """Run an op that must fail, and hand back the exception."""
    from tlgr.core.errors import TlgrError

    try:
        await call(client, in_thread, op, request, **kwargs)
    except TlgrError as exc:
        return exc
    raise AssertionError(f"{op} was expected to fail")


async def make_poll(client, in_thread, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "chat": "@alice",
        "question": "Lunch?",
        "options": ["Pizza", "Sushi"],
    }
    request.update(overrides)
    return await result(client, in_thread, "poll.create", request)


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


class TestPollCreate:
    async def test_a_poll_lands_in_the_history_with_server_assigned_options(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread)
        assert poll["question"] == "Lunch?"
        assert [option["text"] for option in poll["options"]] == ["Pizza", "Sushi"]
        # The option identifier is the server's, not the index the caller typed.
        assert [option["option_b64"] for option in poll["options"]] == ["AA", "AQ"]
        stored = peers.history(ALICE)[-1]
        assert stored.media.poll.question.text == "Lunch?"

    async def test_a_quiz_sends_the_correct_answer_as_an_index(
        self, live_daemon, client, in_thread, peers
    ):
        """Layer 227 spells `correct_answers` as indices, not option bytes."""
        await make_poll(client, in_thread, quiz=True, correct=1, explanation="Sushi wins")
        request = peers.called("SendMediaRequest")[0]
        assert request.media.correct_answers == [1]
        assert request.media.solution == "Sushi wins"
        assert request.media.poll.quiz is True

    async def test_a_quiz_without_a_correct_answer_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        error = await fails(
            client,
            in_thread,
            "poll.create",
            {"chat": "@alice", "question": "?", "options": ["a", "b"], "quiz": True},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_quiz_cannot_be_multiple_choice(self, live_daemon, client, in_thread, peers):
        error = await fails(
            client,
            in_thread,
            "poll.create",
            {
                "chat": "@alice",
                "question": "?",
                "options": ["a", "b"],
                "quiz": True,
                "correct": 0,
                "multiple": True,
            },
        )
        assert error.exit_code == EXIT_USAGE

    async def test_one_answer_is_not_a_poll(self, live_daemon, client, in_thread, peers):
        error = await fails(
            client,
            in_thread,
            "poll.create",
            {"chat": "@alice", "question": "?", "options": ["only"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_hide_results_without_a_deadline_is_refused(
        self, live_daemon, client, in_thread, peers
    ):
        """The GUI gates the toggle behind the duration switch; so does tlgr."""
        error = await fails(
            client,
            in_thread,
            "poll.create",
            {"chat": "@alice", "question": "?", "options": ["a", "b"], "hide_results": True},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_layer_229_description_is_not_supported_rather_than_dropped(
        self, live_daemon, client, in_thread, peers
    ):
        error = await fails(
            client,
            in_thread,
            "poll.create",
            {
                "chat": "@alice",
                "question": "?",
                "options": ["a", "b"],
                "description": "about lunch",
            },
        )
        assert error.exit_code == EXIT_INDETERMINATE
        assert error.code == "NOT_SUPPORTED"

    async def test_the_closing_deadline_reaches_the_request(
        self, live_daemon, client, in_thread, peers
    ):
        await make_poll(client, in_thread, duration="2h", hide_results=True)
        request = peers.called("SendMediaRequest")[0]
        assert request.media.poll.close_period == 7200
        assert request.media.poll.hide_results_until_close is True

    async def test_countries_are_normalised_and_validated(
        self, live_daemon, client, in_thread, peers
    ):
        await make_poll(client, in_thread, countries="it,de")
        request = peers.called("SendMediaRequest")[0]
        assert request.media.poll.countries_iso2 == ["IT", "DE"]

        error = await fails(
            client,
            in_thread,
            "poll.create",
            {"chat": "@alice", "question": "?", "options": ["a", "b"], "countries": "italy"},
        )
        assert error.exit_code == EXIT_USAGE


class TestPollVote:
    async def test_a_vote_moves_the_tally(self, live_daemon, client, in_thread, peers):
        poll = await make_poll(client, in_thread)
        voted = await result(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [1]},
        )
        assert voted["my_votes"] == [1]
        assert voted["total_voters"] == 1
        assert voted["options"][1]["voters"] == 1
        assert voted["options"][1]["percent"] == 100.0

    async def test_the_index_is_resolved_to_the_option_bytes(
        self, live_daemon, client, in_thread, peers
    ):
        """`--shuffle` makes the display order per-viewer, so the index alone lies."""
        poll = await make_poll(client, in_thread, shuffle=True)
        await result(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [1]},
        )
        request = peers.called("SendVoteRequest")[0]
        assert request.options == [b"\x01"]

    async def test_retracting_sends_an_empty_vector(self, live_daemon, client, in_thread, peers):
        poll = await make_poll(client, in_thread)
        await result(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [0]},
        )
        retracted = await result(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "retract": True},
        )
        assert peers.called("SendVoteRequest")[-1].options == []
        # `omit_defaults` drops an empty list: "no votes" is the absence.
        assert retracted.get("my_votes", []) == []
        assert retracted.get("total_voters", 0) == 0

    async def test_a_second_answer_needs_a_multiple_choice_poll(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [0, 1]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_an_option_that_does_not_exist_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [7]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_voting_in_a_closed_poll_is_permission_denied(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread)
        await result(client, in_thread, "poll.close", {"chat": "@alice", "msg_id": poll["msg_id"]})
        error = await fails(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [0]},
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_voting_on_a_message_that_is_not_a_poll_is_not_found(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_message(ALICE, "just text", message_id=500)
        error = await fails(
            client, in_thread, "poll.vote", {"chat": "@alice", "msg_id": 500, "options": [0]}
        )
        assert error.exit_code == EXIT_NOT_FOUND


class TestPollGetAndClose:
    async def test_get_reports_the_state_and_why_voting_is_blocked(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread, subscribers_only=True)
        read = await result(
            client, in_thread, "poll.get", {"chat": "@alice", "msg_id": poll["msg_id"]}
        )
        assert read["restriction"] == "subscribers-only"

    async def test_a_quiz_that_was_answered_cannot_be_voted_in_again(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread, quiz=True, correct=0)
        await result(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [0]},
        )
        read = await result(
            client, in_thread, "poll.get", {"chat": "@alice", "msg_id": poll["msg_id"]}
        )
        assert read["can_vote"] is False
        assert read["restriction"] == "already-voted"

    async def test_close_is_an_edit_carrying_the_whole_poll(
        self, live_daemon, client, in_thread, peers
    ):
        """There is no stopPoll: the answers must be resent or the votes orphan."""
        poll = await make_poll(client, in_thread)
        closed = await result(
            client, in_thread, "poll.close", {"chat": "@alice", "msg_id": poll["msg_id"]}
        )
        assert closed["closed"] is True
        request = peers.called("EditMessageRequest")[0]
        assert request.media.poll.closed is True
        assert [answer.option for answer in request.media.poll.answers] == [b"\x00", b"\x01"]

    async def test_closing_twice_reports_already(self, live_daemon, client, in_thread, peers):
        poll = await make_poll(client, in_thread)
        await result(client, in_thread, "poll.close", {"chat": "@alice", "msg_id": poll["msg_id"]})
        envelope = await call(
            client, in_thread, "poll.close", {"chat": "@alice", "msg_id": poll["msg_id"]}
        )
        assert envelope["result"]["already"] is True
        assert len(peers.called("EditMessageRequest")) == 1


class TestPollOptions:
    async def test_an_answer_can_be_added_to_an_open_poll(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread, allow_adding_options=True)
        grown = await result(
            client,
            in_thread,
            "poll.option.add",
            {"chat": "@alice", "msg_id": poll["msg_id"], "text": "Ramen"},
        )
        assert [option["text"] for option in grown["options"]] == ["Pizza", "Sushi", "Ramen"]
        assert grown["options"][2]["option_b64"] == "Ag"

    async def test_a_closed_answer_list_refuses_new_answers(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "poll.option.add",
            {"chat": "@alice", "msg_id": poll["msg_id"], "text": "Ramen"},
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_removing_an_answer_addresses_it_by_its_bytes(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread, allow_adding_options=True)
        shrunk = await result(
            client,
            in_thread,
            "poll.option.remove",
            {"chat": "@alice", "msg_id": poll["msg_id"], "option": 0},
        )
        assert peers.called("DeletePollAnswerRequest")[0].option == b"\x00"
        assert [option["text"] for option in shrunk["options"]] == ["Sushi"]


class TestPollVoters:
    async def test_a_public_poll_lists_who_voted(self, live_daemon, client, in_thread, peers):
        poll = await make_poll(client, in_thread, public_voters=True)
        await result(
            client,
            in_thread,
            "poll.vote",
            {"chat": "@alice", "msg_id": poll["msg_id"], "options": [0]},
        )
        rows = await result(
            client, in_thread, "poll.voter.list", {"chat": "@alice", "msg_id": poll["msg_id"]}
        )
        assert [row["user_id"] for row in rows] == [peers.me.id]
        assert rows[0].get("option", 0) == 0

    async def test_an_anonymous_poll_has_no_voter_list(self, live_daemon, client, in_thread, peers):
        poll = await make_poll(client, in_thread)
        error = await fails(
            client, in_thread, "poll.voter.list", {"chat": "@alice", "msg_id": poll["msg_id"]}
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_unread_votes_can_be_listed_and_cleared(
        self, live_daemon, client, in_thread, peers
    ):
        poll = await make_poll(client, in_thread)
        rows = await result(
            client, in_thread, "poll.unread.list", {"chat": "@alice", "read_all": True}
        )
        assert [row["id"] for row in rows] == [poll["msg_id"]]
        assert peers.called("ReadPollVotesRequest")


class TestPollStats:
    async def test_the_graph_is_fetched_from_the_stats_dc_on_migrate(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.errors import StatsMigrateError

        peers.add_message(CHANNEL_ID, "poll", message_id=900)
        peers.fail_next("GetPollStatsRequest", StatsMigrateError(request=None, capture=4))
        stats = await result(
            client, in_thread, "poll.stats.get", {"chat": str(CHANNEL_ID), "msg_id": 900}
        )
        assert stats["dc_id"] == 4
        assert peers.called("borrow_exported_sender")
        assert '"columns"' in stats["graph"]


# ---------------------------------------------------------------------------
# reaction
# ---------------------------------------------------------------------------


@pytest.fixture
def history(peers, world):
    for index in range(1, 6):
        world.add_message(ALICE, f"message {index}", message_id=100 + index)
    world.add_message(CHANNEL_ID, "post", message_id=900)
    return world


class TestReactionAdd:
    async def test_a_reaction_reports_what_is_now_on_the_message(
        self, live_daemon, client, in_thread, history
    ):
        """v1's `message react`, unchanged in what it answers."""
        out = await result(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "emoji": ["👍"]}
        )
        assert out["reacted"] is True
        assert out["mine"] == ["👍"]
        assert out["reactions"]["mine"] == ["👍"]

    async def test_the_v1_path_still_works(self, live_daemon, client, in_thread, history):
        out = await result(
            client, in_thread, "message.react", {"chat": "@alice", "msg_id": 103, "emoji": ["👍"]}
        )
        assert out["emoji"] == "👍"
        assert out["msg_id"] == 103
        assert out["reacted"] is True

    async def test_a_second_reaction_keeps_the_first(self, live_daemon, client, in_thread, history):
        """sendReaction carries the whole state; a delta would drop the 👍."""
        await result(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "emoji": ["👍"]}
        )
        await result(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "emoji": ["🎉"]}
        )
        request = history.called("SendReactionRequest")[-1]
        assert [item.emoticon for item in request.reaction] == ["👍", "🎉"]

    async def test_replace_sends_exactly_what_was_asked_for(
        self, live_daemon, client, in_thread, history
    ):
        await result(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "emoji": ["👍"]}
        )
        await result(
            client,
            in_thread,
            "reaction.add",
            {"chat": "@alice", "msg_id": 103, "emoji": ["🎉"], "replace": True},
        )
        request = history.called("SendReactionRequest")[-1]
        assert [item.emoticon for item in request.reaction] == ["🎉"]

    async def test_a_custom_emoji_is_spelled_the_same_way_in_and_out(
        self, live_daemon, client, in_thread, history
    ):
        out = await result(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "custom": [55]}
        )
        request = history.called("SendReactionRequest")[-1]
        assert request.reaction[0].document_id == 55
        assert out["mine"] == ["custom:55"]

    async def test_a_duplicate_reaction_is_already(self, live_daemon, client, in_thread, history):
        from telethon.errors import MessageNotModifiedError

        history.fail_next("SendReactionRequest", MessageNotModifiedError(None))
        envelope = await call(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "emoji": ["👍"]}
        )
        assert envelope["result"]["already"] is True

    async def test_send_as_points_at_the_command_that_can_pay(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(
            client,
            in_thread,
            "reaction.add",
            {"chat": "@alice", "msg_id": 103, "emoji": ["👍"], "send_as": "@alice"},
        )
        assert error.exit_code == EXIT_USAGE
        assert "reaction pay" in str(error)

    async def test_the_big_and_recent_flags_reach_the_request(
        self, live_daemon, client, in_thread, history
    ):
        await result(
            client,
            in_thread,
            "reaction.add",
            {"chat": "@alice", "msg_id": 103, "emoji": ["👍"], "big": True, "recent": True},
        )
        request = history.called("SendReactionRequest")[-1]
        assert request.big is True and request.add_to_recent is True


class TestReactionRemove:
    async def test_removing_one_resends_the_others(self, live_daemon, client, in_thread, history):
        await result(
            client,
            in_thread,
            "reaction.add",
            {"chat": "@alice", "msg_id": 103, "emoji": ["👍", "🎉"]},
        )
        await result(
            client, in_thread, "reaction.remove", {"chat": "@alice", "msg_id": 103, "emoji": "👍"}
        )
        request = history.called("SendReactionRequest")[-1]
        assert [item.emoticon for item in request.reaction] == ["🎉"]

    async def test_removing_everything_sends_an_empty_vector(
        self, live_daemon, client, in_thread, history
    ):
        await result(
            client, in_thread, "reaction.add", {"chat": "@alice", "msg_id": 103, "emoji": ["👍"]}
        )
        await result(client, in_thread, "reaction.remove", {"chat": "@alice", "msg_id": 103})
        assert history.called("SendReactionRequest")[-1].reaction is None

    async def test_removing_one_that_is_not_there_is_already(
        self, live_daemon, client, in_thread, history
    ):
        envelope = await call(
            client, in_thread, "reaction.remove", {"chat": "@alice", "msg_id": 103, "emoji": "👍"}
        )
        assert envelope["result"]["already"] is True
        assert history.called("SendReactionRequest") == []

    async def test_a_message_that_does_not_exist_is_not_found(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(client, in_thread, "reaction.remove", {"chat": "@alice", "msg_id": 999})
        assert error.exit_code == EXIT_NOT_FOUND


class TestReactionRead:
    async def test_counts_are_refreshed_for_several_messages_at_once(
        self, live_daemon, client, in_thread, history
    ):
        page = await result(
            client, in_thread, "reaction.list", {"chat": "@alice", "msg_id": ["101-103"]}
        )
        assert [row["msg_id"] for row in page["items"]] == [101, 102, 103]
        assert history.called("GetMessagesReactionsRequest")[0].id == [101, 102, 103]

    async def test_no_message_id_is_a_usage_error(self, live_daemon, client, in_thread, history):
        error = await fails(client, in_thread, "reaction.list", {"chat": "@alice"})
        assert error.exit_code == EXIT_USAGE

    async def test_who_reacted_is_listed_with_a_string_cursor(
        self, live_daemon, client, in_thread, history
    ):
        from telethon.tl import types

        history.raw["GetMessageReactionsListRequest"] = lambda request: (
            types.messages.MessageReactionsList(
                count=2,
                reactions=[
                    types.MessagePeerReaction(
                        peer_id=types.PeerUser(user_id=ALICE),
                        date=None,
                        reaction=types.ReactionEmoji(emoticon="👍"),
                    )
                ],
                chats=[],
                users=[],
                next_offset="page2",
            )
        )
        envelope = await call(
            client, in_thread, "reaction.user.list", {"chat": "@alice", "msg_id": 103}
        )
        assert envelope["result"][0]["user_id"] == ALICE
        assert envelope["result"][0]["reaction"] == "👍"
        assert envelope["page"]["has_more"] is True
        assert envelope["page"]["next_cursor"]

    async def test_the_cursor_carries_the_opaque_offset_back(
        self, live_daemon, client, in_thread, history
    ):
        from tlgr.core.pagination import PageKind, decode_cursor, encode_cursor

        token = encode_cursor(
            op="reaction.user.list",
            kind=PageKind.PARTICIPANTS,
            state={"offset": "page2"},
            account="work",
        )
        await call(
            client,
            in_thread,
            "reaction.user.list",
            {"chat": "@alice", "msg_id": 103},
            cursor=token,
        )
        assert history.called("GetMessageReactionsListRequest")[0].offset == "page2"
        assert decode_cursor(token, op="reaction.user.list", account="work") == {"offset": "page2"}

    async def test_a_cursor_from_another_op_is_rejected(
        self, live_daemon, client, in_thread, history
    ):
        from tlgr.core.pagination import PageKind, encode_cursor

        token = encode_cursor(
            op="poll.voter.list", kind=PageKind.PARTICIPANTS, state={}, account="work"
        )
        error = await fails(
            client,
            in_thread,
            "reaction.user.list",
            {"chat": "@alice", "msg_id": 103},
            cursor=token,
        )
        assert error.exit_code == EXIT_USAGE

    async def test_unread_reactions_can_be_listed_and_cleared(
        self, live_daemon, client, in_thread, history
    ):
        from telethon.tl import types

        history.raw["GetUnreadReactionsRequest"] = lambda request: types.messages.Messages(
            messages=[history.find(ALICE, 103)], topics=[], chats=[], users=[]
        )
        rows = await result(
            client, in_thread, "reaction.unread.list", {"chat": "@alice", "read_all": True}
        )
        assert [row["id"] for row in rows] == [103]
        assert history.called("ReadReactionsRequest")


class TestReactionCatalog:
    def _catalogue(self, world):
        from telethon.tl import types

        def handler(request):
            document = types.DocumentEmpty(id=1)
            return types.messages.AvailableReactions(
                hash=0,
                reactions=[
                    types.AvailableReaction(
                        reaction=emoji,
                        title=title,
                        static_icon=document,
                        appear_animation=document,
                        select_animation=document,
                        activate_animation=document,
                        effect_animation=document,
                        premium=premium or None,
                    )
                    for emoji, title, premium in (
                        ("👍", "Thumbs Up", False),
                        ("❤", "Heart", False),
                        ("🎉", "Party", True),
                    )
                ],
            )

        world.raw["GetAvailableReactionsRequest"] = handler

    async def test_the_standard_catalogue_is_paged_locally(
        self, live_daemon, client, in_thread, peers
    ):
        self._catalogue(peers)
        envelope = await call(client, in_thread, "reaction.catalog", {}, limit=2)
        assert [row["emoticon"] for row in envelope["result"]] == ["👍", "❤"]
        assert envelope["page"]["has_more"] is True
        assert envelope["page"]["total"] == 3

        second = await call(
            client, in_thread, "reaction.catalog", {}, cursor=envelope["page"]["next_cursor"]
        )
        assert [row["emoticon"] for row in second["result"]] == ["🎉"]
        assert second["result"][0]["premium"] is True

    async def test_forget_clears_the_recent_list_first(self, live_daemon, client, in_thread, peers):
        self._catalogue(peers)
        await result(client, in_thread, "reaction.catalog", {"forget": True})
        assert peers.called("ClearRecentReactionsRequest")

    async def test_the_recent_list_is_its_own_source(self, live_daemon, client, in_thread, peers):
        from telethon.tl import types

        peers.raw["GetRecentReactionsRequest"] = lambda request: types.messages.Reactions(
            hash=0, reactions=[types.ReactionEmoji(emoticon="🔥")]
        )
        rows = await result(client, in_thread, "reaction.catalog", {"recent": True})
        assert rows[0] == {"emoticon": "🔥", "source": "recent"}


class TestReactionPolicy:
    async def test_reading_a_chats_policy(self, live_daemon, client, in_thread, history):
        from telethon.tl import types

        history.chat_reactions[CHANNEL_ID] = types.ChatReactionsSome(
            reactions=[types.ReactionEmoji(emoticon="👍")]
        )
        history.reactions_limit[CHANNEL_ID] = 3
        policy = await result(client, in_thread, "reaction.chat.get", {"chat": str(CHANNEL_ID)})
        assert policy["mode"] == "some"
        assert policy["reactions"] == ["👍"]
        assert policy["reactions_limit"] == 3

    async def test_a_private_chat_has_no_reaction_policy(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(client, in_thread, "reaction.chat.get", {"chat": "@alice"})
        assert error.exit_code == EXIT_USAGE

    async def test_setting_only_the_cap_resends_the_existing_policy(
        self, live_daemon, client, in_thread, history
    ):
        """`available_reactions` is mandatory on the wire: a blind write would wipe it."""
        from telethon.tl import types

        history.chat_reactions[CHANNEL_ID] = types.ChatReactionsSome(
            reactions=[types.ReactionEmoji(emoticon="👍")]
        )
        out = await result(
            client,
            in_thread,
            "reaction.chat.set",
            {"chat": str(CHANNEL_ID), "max_unique": 2},
        )
        request = history.called("SetChatAvailableReactionsRequest")[0]
        assert [item.emoticon for item in request.available_reactions.reactions] == ["👍"]
        assert out["reactions_limit"] == 2

    async def test_the_three_modes_are_one_question(self, live_daemon, client, in_thread, history):
        error = await fails(
            client,
            in_thread,
            "reaction.chat.set",
            {"chat": str(CHANNEL_ID), "every": True, "none": True},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_star_reactions_can_be_switched_on(self, live_daemon, client, in_thread, history):
        out = await result(
            client,
            in_thread,
            "reaction.chat.set",
            {"chat": str(CHANNEL_ID), "every": True, "paid": "on"},
        )
        assert out["mode"] == "all"
        assert out["paid_enabled"] is True

    async def test_a_message_narrows_an_all_policy_to_the_live_catalogue(
        self, live_daemon, client, in_thread, history
    ):
        from telethon.tl import types

        history.chat_reactions[CHANNEL_ID] = types.ChatReactionsAll(allow_custom=True)
        document = types.DocumentEmpty(id=1)
        history.raw["GetAvailableReactionsRequest"] = lambda request: (
            types.messages.AvailableReactions(
                hash=0,
                reactions=[
                    types.AvailableReaction(
                        reaction="👍",
                        title="Thumbs Up",
                        static_icon=document,
                        appear_animation=document,
                        select_animation=document,
                        activate_animation=document,
                        effect_animation=document,
                    )
                ],
            )
        )
        policy = await result(
            client,
            in_thread,
            "reaction.chat.get",
            {"chat": str(CHANNEL_ID), "msg_id": 900},
        )
        assert policy["reactions"] == ["👍"]
        assert policy["msg_id"] == 900


class TestReactionDefaultsAndTags:
    async def test_the_quick_reaction_round_trips(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "reaction.default.set", {"emoji": "❤"})
        assert peers.default_reaction.emoticon == "❤"
        assert await result(client, in_thread, "reaction.default.get", {}) == {"reaction": "❤"}

    async def test_setting_no_reaction_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        error = await fails(client, in_thread, "reaction.default.set", {})
        assert error.exit_code == EXIT_USAGE

    async def test_a_tag_can_be_named_and_read_back(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "reaction.tag.set", {"emoji": "📌", "title": "invoices"})
        page = await result(client, in_thread, "reaction.tag.list", {})
        assert page["items"][0] == {"reaction": "📌", "title": "invoices", "count": 1}

    async def test_clearing_a_tag_name_sends_no_title(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "reaction.tag.set", {"emoji": "📌", "clear": True})
        assert peers.called("UpdateSavedReactionTagRequest")[-1].title is None

    async def test_renaming_through_the_list_command(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "reaction.tag.list", {"rename": "📌=receipts"})
        assert peers.saved_tags["📌"] == "receipts"

    async def test_a_rename_without_a_title_separator_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        error = await fails(client, in_thread, "reaction.tag.list", {"rename": "📌"})
        assert error.exit_code == EXIT_USAGE

    async def test_the_suggested_tags_are_their_own_list(
        self, live_daemon, client, in_thread, peers
    ):
        page = await result(client, in_thread, "reaction.tag.list", {"suggested": True})
        assert page["items"][0]["suggested"] is True


class TestReactionPaid:
    async def test_paying_spends_the_amount_that_was_asked_for(
        self, live_daemon, client, in_thread, history
    ):
        out = await result(
            client,
            in_thread,
            "reaction.pay",
            {"chat": str(CHANNEL_ID), "msg_id": 900, "stars": 50},
        )
        assert out["stars_sent"] == 50
        assert history.star_balance == 950
        assert out["top_reactors"][0]["stars"] == 50

    async def test_the_random_id_is_the_documented_timestamp_form(
        self, live_daemon, client, in_thread, history
    ):
        """`(unixtime << 32) | random_uint32` — unlike every other send."""
        import time

        await result(
            client,
            in_thread,
            "reaction.pay",
            {"chat": str(CHANNEL_ID), "msg_id": 900, "stars": 1},
        )
        request = history.called("SendPaidReactionRequest")[0]
        assert abs((request.random_id >> 32) - int(time.time())) < 60

    async def test_there_is_no_default_amount(self, live_daemon, client, in_thread, history):
        error = await fails(
            client, in_thread, "reaction.pay", {"chat": str(CHANNEL_ID), "msg_id": 900}
        )
        assert error.exit_code == EXIT_USAGE
        assert history.called("SendPaidReactionRequest") == []

    async def test_listing_the_senders_never_pays(self, live_daemon, client, in_thread, history):
        out = await result(
            client,
            in_thread,
            "reaction.pay",
            {"chat": str(CHANNEL_ID), "msg_id": 900, "senders": True},
        )
        assert out["senders"] == [history.me.id]
        assert history.called("SendPaidReactionRequest") == []

    async def test_a_private_chat_fails_before_anything_is_spent(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(
            client,
            in_thread,
            "reaction.pay",
            {"chat": "@alice", "msg_id": 103, "stars": 10},
        )
        assert error.exit_code == EXIT_USAGE
        assert history.called("SendPaidReactionRequest") == []

    async def test_privacy_round_trips(self, live_daemon, client, in_thread, history):
        await result(
            client,
            in_thread,
            "reaction.privacy.set",
            {"mode": "anonymous", "chat": str(CHANNEL_ID), "msg_id": 900},
        )
        assert await result(client, in_thread, "reaction.privacy.get", {}) == {
            "privacy": "anonymous"
        }

    async def test_privacy_set_needs_the_post_it_rewrites(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(client, in_thread, "reaction.privacy.set", {"mode": "anonymous"})
        assert error.exit_code == EXIT_USAGE

    async def test_an_unknown_privacy_mode_is_a_usage_error(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(
            client,
            in_thread,
            "reaction.privacy.set",
            {"mode": "secret", "chat": str(CHANNEL_ID), "msg_id": 900},
        )
        assert error.exit_code == EXIT_USAGE


class TestReactionModeration:
    async def test_purging_one_message_uses_the_singular_method(
        self, live_daemon, client, in_thread, history
    ):
        out = await result(
            client,
            in_thread,
            "reaction.purge",
            {"chat": str(CHANNEL_ID), "user": "@alice", "msg": 900},
        )
        assert out["scope"] == "message" and out["deleted"] is True
        assert history.called("DeleteParticipantReactionRequest")

    async def test_purging_the_whole_chat_uses_the_plural_method(
        self, live_daemon, client, in_thread, history
    ):
        out = await result(
            client,
            in_thread,
            "reaction.purge",
            {"chat": str(CHANNEL_ID), "user": "@alice", "every": True},
        )
        assert out["scope"] == "chat"
        assert history.called("DeleteParticipantReactionsRequest")

    async def test_naming_neither_scope_is_a_usage_error(
        self, live_daemon, client, in_thread, history
    ):
        error = await fails(
            client, in_thread, "reaction.purge", {"chat": str(CHANNEL_ID), "user": "@alice"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_reporting_can_also_block_the_sender(
        self, live_daemon, client, in_thread, history
    ):
        out = await result(
            client,
            in_thread,
            "reaction.report",
            {"chat": str(CHANNEL_ID), "msg_id": 900, "user": "@alice", "ban": True},
        )
        assert out["ok"] is True and out["banned"] is True
        assert history.called("ReportReactionRequest")
        assert history.called("BlockFromRepliesRequest")

    async def test_reporting_alone_does_not_ban(self, live_daemon, client, in_thread, history):
        out = await result(
            client,
            in_thread,
            "reaction.report",
            {"chat": str(CHANNEL_ID), "msg_id": 900, "user": "@alice"},
        )
        assert out.get("banned", False) is False
        assert history.called("BlockFromRepliesRequest") == []


# ---------------------------------------------------------------------------
# todo
# ---------------------------------------------------------------------------


async def make_todo(client, in_thread, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "chat": "@alice",
        "title": "Release checklist",
        "tasks": ["tag the commit", "publish the wheel", "announce it"],
    }
    request.update(overrides)
    return await result(client, in_thread, "todo.create", request)


class TestTodoCreate:
    async def test_tasks_are_numbered_from_one(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        assert [task["id"] for task in checklist["tasks"]] == [1, 2, 3]
        assert checklist["title"] == "Release checklist"
        request = peers.called("SendMediaRequest")[0]
        assert [item.id for item in request.media.todo.list] == [1, 2, 3]

    async def test_an_empty_checklist_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        error = await fails(
            client, in_thread, "todo.create", {"chat": "@alice", "title": "x", "tasks": []}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_the_permission_flags_reach_the_request(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(
            client, in_thread, others_can_add=True, others_can_complete=True
        )
        assert checklist["others_can_add"] is True
        assert checklist["others_can_complete"] is True

    async def test_the_v1_style_alias_reaches_the_same_op(self, live_daemon, client, in_thread):
        from tlgr.registry import canonical

        assert canonical("message checklist") == "todo.create"


class TestTodoToggle:
    async def test_ticking_and_unticking_happen_in_one_request(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread)
        await result(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["1", "2"]},
        )
        out = await result(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["3"], "undone": ["1"]},
        )
        request = peers.called("ToggleTodoCompletedRequest")[-1]
        assert request.completed == [3] and request.incompleted == [1]
        assert {task["id"] for task in out["tasks"] if task.get("done")} == {2, 3}
        assert out["done_count"] == 2

    async def test_a_completion_records_who_and_when(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        out = await result(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["1"]},
        )
        first = out["tasks"][0]
        assert first["completed_by"] == peers.me.id
        assert first["completed_date"].endswith("Z")

    async def test_ticking_what_is_already_ticked_is_already(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread)
        await result(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["1"]},
        )
        envelope = await call(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["1"]},
        )
        assert envelope["result"]["already"] is True
        assert len(peers.called("ToggleTodoCompletedRequest")) == 1

    async def test_an_unknown_task_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["99"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_naming_no_task_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        error = await fails(
            client, in_thread, "todo.toggle", {"chat": "@alice", "msg_id": checklist["msg_id"]}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_send_as_sets_the_chats_default_identity_first(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread)
        await result(
            client,
            in_thread,
            "todo.toggle",
            {
                "chat": "@alice",
                "msg_id": checklist["msg_id"],
                "done": ["1"],
                "send_as": str(CHANNEL_ID),
            },
        )
        assert peers.called("SaveDefaultSendAsRequest")

    async def test_a_message_that_is_not_a_checklist_is_not_found(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_message(ALICE, "just text", message_id=500)
        error = await fails(
            client, in_thread, "todo.toggle", {"chat": "@alice", "msg_id": 500, "done": ["1"]}
        )
        assert error.exit_code == EXIT_NOT_FOUND


class TestTodoAddAndEdit:
    async def test_appending_continues_the_numbering(self, live_daemon, client, in_thread, peers):
        """Reusing an id is TODO_ITEM_DUPLICATE; reusing a freed one moves a tick."""
        checklist = await make_todo(client, in_thread, others_can_add=True)
        out = await result(
            client,
            in_thread,
            "todo.add",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "tasks": ["sign the release"]},
        )
        assert [item.id for item in peers.called("AppendTodoListRequest")[0].list] == [4]
        assert [task["id"] for task in out["tasks"]] == [1, 2, 3, 4]

    async def test_appending_to_a_closed_list_warns(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        envelope = await call(
            client,
            in_thread,
            "todo.add",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "tasks": ["x"]},
        )
        assert envelope["meta"]["warnings"]

    async def test_removing_a_task_keeps_the_survivors_ids(
        self, live_daemon, client, in_thread, peers
    ):
        """Renumbering would move every completion onto a different task."""
        checklist = await make_todo(client, in_thread)
        await result(
            client,
            in_thread,
            "todo.toggle",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "done": ["3"]},
        )
        out = await result(
            client,
            in_thread,
            "todo.edit",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "remove_task": ["1"]},
        )
        assert [task["id"] for task in out["tasks"]] == [2, 3]
        assert [task["id"] for task in out["tasks"] if task.get("done")] == [3]

    async def test_renaming_a_task_and_the_list(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        out = await result(
            client,
            in_thread,
            "todo.edit",
            {
                "chat": "@alice",
                "msg_id": checklist["msg_id"],
                "title": "Release 2.0",
                "rename_task": ["2=publish the sdist"],
            },
        )
        assert out["title"] == "Release 2.0"
        assert out["tasks"][1]["title"] == "publish the sdist"

    async def test_a_rename_without_a_separator_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "todo.edit",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "rename_task": ["2"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_checklist_cannot_be_emptied_by_editing(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "todo.edit",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "remove_task": ["1", "2", "3"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_the_permission_switches_are_tri_state(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread, others_can_add=True)
        out = await result(
            client,
            in_thread,
            "todo.edit",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "others_can_complete": "on"},
        )
        # Not asked about, so left alone; asked about, so changed.
        assert out["others_can_add"] is True
        assert out["others_can_complete"] is True

    async def test_an_unknown_switch_value_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        checklist = await make_todo(client, in_thread)
        error = await fails(
            client,
            in_thread,
            "todo.edit",
            {"chat": "@alice", "msg_id": checklist["msg_id"], "others_can_add": "maybe"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_reading_a_checklist_back(self, live_daemon, client, in_thread, peers):
        checklist = await make_todo(client, in_thread)
        read = await result(
            client, in_thread, "todo.get", {"chat": "@alice", "msg_id": checklist["msg_id"]}
        )
        assert read["title"] == "Release checklist"
        assert len(read["tasks"]) == 3
