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
