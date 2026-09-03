"""Polls and quizzes.

Telegram addresses a poll answer by an **opaque byte string**, not by its
index: `poll.answers[2].option` is whatever the server assigned, and with
`shuffle_answers` the order a client renders is not the order the server
stores. Every option therefore carries both — `index` for a human to type and
`option_b64` for a machine to round-trip — so `poll vote 2` can be resolved
against the server's copy rather than against whatever the caller last saw.
"""

from __future__ import annotations

from typing import Literal

from tlgr.models.base import Model
from tlgr.models.message import MediaSummary, MessageEntity

__all__ = ["Poll", "PollOption", "PollStats", "PollVoter"]


class PollOption(Model):
    """One answer, with the two names it answers to.

    `voters`/`percent` are absent rather than zero until results are visible:
    a poll that hides its results until it closes reports nothing, and zero
    would be a wrong answer rather than a missing one.
    """

    index: int
    text: str = ""
    entities: list[MessageEntity] = []
    #: The server's opaque option identifier, base64. Everything after
    #: creation — voting, deleting an answer, listing voters — addresses the
    #: option by these bytes.
    option_b64: str = ""
    voters: int | None = None
    percent: float | None = None
    chosen: bool = False
    correct: bool | None = None
    added_by: int | None = None
    added_date: str | None = None
    media: MediaSummary | None = None


class Poll(Model):
    """A poll or quiz and everything currently known about its results.

    `can_vote` and `restriction` are computed here rather than left to the
    caller: eligibility is a client-side derivation over `closed`,
    `subscribers_only` and `countries`, and an agent that cannot see it ends
    up learning the answer by sending a vote and reading the RPC error.
    """

    #: First and required: `omit_defaults` would drop `"poll"` and `true`,
    #: and an agent reading a missing `can_vote` as False would be wrong.
    type: Literal["poll", "quiz"]
    can_vote: bool
    chat_id: int = 0
    msg_id: int = 0
    poll_id: int | None = None
    question: str = ""
    entities: list[MessageEntity] = []
    description: str | None = None
    closed: bool = False
    public_voters: bool = False
    multiple: bool = False
    open_answers: bool = False
    revoting_disabled: bool = False
    shuffle: bool = False
    hide_results_until_close: bool = False
    subscribers_only: bool = False
    countries: list[str] = []
    close_period: int | None = None
    close_date: str | None = None
    close_date_unix: int | None = None
    total_voters: int = 0
    restriction: str | None = None
    my_votes: list[int] = []
    options: list[PollOption] = []
    recent_voters: list[int] = []
    solution: str | None = None
    solution_entities: list[MessageEntity] = []
    #: `pollResults.min` — the payload is partial and a `poll get` refreshes it.
    min: bool = False
    has_unread_votes: bool = False
    can_view_stats: bool = False
    already: bool = False


class PollVoter(Model):
    """One row of `messages.getPollVotes`.

    `options` rather than `option` because a multiple-choice poll answers with
    `messagePeerVoteMultiple`; the singular is kept as the first entry so a
    table stays readable.
    """

    user_id: int
    option: int | None = None
    options: list[int] = []
    date: str | None = None
    date_unix: int | None = None


class PollStats(Model):
    """The vote-statistics graph, as the server hands it over.

    Telegram answers with either an inline zipped JSON payload or an
    asynchronous token to be loaded later; both are reported rather than one
    being silently resolved, because loading a token costs another request on
    the stats DC.
    """

    chat_id: int = 0
    msg_id: int = 0
    graph: str | None = None
    token: str | None = None
    dark: bool = False
    dc_id: int | None = None
