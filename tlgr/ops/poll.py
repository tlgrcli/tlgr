"""The `poll` group: create, read, vote in and close polls and quizzes.

Two facts about Telegram's poll API shape everything here.

* **An answer is opaque bytes, not an index.** `poll.answers[i].option` is
  whatever the server assigned, and `shuffle_answers` means the order a client
  shows is not the order the server stores. Every command that names an answer
  therefore *refetches the poll first* and resolves the caller's index against
  the server's copy; the bytes come back in `option_b64` so a machine can
  round-trip them.
* **There is no `stopPoll` method.** Closing a poll is `messages.editMessage`
  with the same poll and `closed=True`, which means the whole constructor has
  to be resent — including the answers, with their original option bytes, or
  every vote already cast would be attached to answers that no longer exist.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, NotSupportedError, PermissionError_, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_dt, parse_duration, to_unix
from tlgr.models.base import Request
from tlgr.models.message import Message, MessageEntity
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.poll import Poll, PollOption, PollStats, PollVoter
from tlgr.ops import _send
from tlgr.ops._common import (
    affected_loop,
    already,
    client,
    input_channel,
    is_not_modified,
    only,
    random_id,
    window,
)
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import media_summary, message_to_model
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_POLL: dict[str, Any] = {
    "chat_id": 777123,
    "msg_id": 12345,
    "poll_id": 5069438842982500000,
    "question": "Lunch?",
    "type": "poll",
    "total_voters": 3,
    "can_vote": True,
    "options": [
        {"index": 0, "text": "Pizza", "option_b64": "AA", "voters": 2, "percent": 66.7},
        {"index": 1, "text": "Sushi", "option_b64": "AQ", "voters": 1, "percent": 33.3},
    ],
}


# ---------------------------------------------------------------------------
# Reading a poll off a message
# ---------------------------------------------------------------------------


def _entities(items: Any) -> list[MessageEntity]:
    """Telethon entities → models, reusing the message serialiser's rules."""
    from tlgr.ops._serialize import message_entities

    class _Holder:
        entities = list(items or [])

    return message_entities(_Holder())


def _text_with_entities(value: Any) -> tuple[str, list[MessageEntity]]:
    """`TextWithEntities` → `(text, entities)`, tolerating a bare string."""
    if value is None:
        return "", []
    if isinstance(value, str):
        return value, []
    return str(getattr(value, "text", "") or ""), _entities(getattr(value, "entities", None))


def _b64(raw: bytes | None) -> str:
    return base64.urlsafe_b64encode(raw or b"").decode().rstrip("=")


def poll_model(media: Any, *, chat_id: int = 0, msg_id: int = 0) -> Poll:
    """`MessageMediaPoll` → the one `Poll` shape every command in this group returns.

    `can_vote` is derived here rather than left to the caller: eligibility is
    a client-side reading of `closed`, `revoting_disabled`, `subscribers_only`
    and `countries_iso2`, and an agent that cannot see it learns the answer by
    sending a vote and reading the RPC error.
    """
    raw = getattr(media, "poll", None)
    results = getattr(media, "results", None)
    question, entities = _text_with_entities(getattr(raw, "question", None))
    close_date = getattr(raw, "close_date", None)

    voters_by_option: dict[bytes, Any] = {
        bytes(getattr(item, "option", b"")): item
        for item in (getattr(results, "results", None) or [])
    }
    total = int(getattr(results, "total_voters", 0) or 0)

    options: list[PollOption] = []
    my_votes: list[int] = []
    for index, answer in enumerate(getattr(raw, "answers", None) or []):
        option = bytes(getattr(answer, "option", b"") or b"")
        text, answer_entities = _text_with_entities(getattr(answer, "text", None))
        tally = voters_by_option.get(option)
        voters = int(getattr(tally, "voters", 0) or 0) if tally is not None else None
        chosen = bool(getattr(tally, "chosen", False)) if tally is not None else False
        if chosen:
            my_votes.append(index)
        added_by = getattr(answer, "added_by", None)
        options.append(
            PollOption(
                index=index,
                text=text,
                entities=answer_entities,
                option_b64=_b64(option),
                voters=voters,
                percent=round(100.0 * voters / total, 1) if voters is not None and total else None,
                chosen=chosen,
                correct=getattr(tally, "correct", None) if tally is not None else None,
                added_by=_peer_id(added_by),
                added_date=fmt_dt(getattr(answer, "date", None)),
                media=media_summary(getattr(answer, "media", None)),
            )
        )

    solution, solution_entities = (
        (
            getattr(results, "solution", None) or "",
            _entities(getattr(results, "solution_entities", None)),
        )
        if results is not None
        else ("", [])
    )
    poll = Poll(
        chat_id=chat_id,
        msg_id=msg_id,
        poll_id=int(getattr(raw, "id", 0) or 0) or None,
        question=question,
        entities=entities,
        type="quiz" if getattr(raw, "quiz", False) else "poll",
        can_vote=True,
        closed=bool(getattr(raw, "closed", False)),
        public_voters=bool(getattr(raw, "public_voters", False)),
        multiple=bool(getattr(raw, "multiple_choice", False)),
        open_answers=bool(getattr(raw, "open_answers", False)),
        revoting_disabled=bool(getattr(raw, "revoting_disabled", False)),
        shuffle=bool(getattr(raw, "shuffle_answers", False)),
        hide_results_until_close=bool(getattr(raw, "hide_results_until_close", False)),
        subscribers_only=bool(getattr(raw, "subscribers_only", False)),
        countries=list(getattr(raw, "countries_iso2", None) or []),
        close_period=getattr(raw, "close_period", None),
        close_date=fmt_dt(close_date),
        close_date_unix=to_unix(close_date),
        total_voters=total,
        my_votes=my_votes,
        options=options,
        recent_voters=[
            pid
            for pid in (_peer_id(peer) for peer in (getattr(results, "recent_voters", None) or []))
            if pid is not None
        ],
        solution=solution or None,
        solution_entities=solution_entities,
        min=bool(getattr(results, "min", False)),
        has_unread_votes=bool(getattr(results, "has_unread_votes", False)),
        can_view_stats=bool(getattr(results, "can_view_stats", False)),
    )
    _derive_can_vote(poll)
    return poll


def _peer_id(peer: Any) -> int | None:
    if peer is None:
        return None
    from tlgr.ops._serialize import peer_id_of

    return peer_id_of(peer)


def _derive_can_vote(poll: Poll) -> None:
    """Fill `can_vote`/`restriction` — the reason a `sendVote` would be refused."""
    if poll.closed:
        poll.can_vote, poll.restriction = False, "closed"
    elif poll.my_votes and (poll.revoting_disabled or poll.type == "quiz"):
        poll.can_vote, poll.restriction = False, "already-voted"
    elif poll.subscribers_only:
        # The full rule also needs the channel's join date, which is not on
        # the message; reporting the restriction without claiming a verdict is
        # better than guessing one either way.
        poll.restriction = "subscribers-only"
    elif poll.countries:
        poll.restriction = "country-restricted"


async def _fetch(ctx: OpContext, peer: Any, chat_id: int, msg_id: int) -> tuple[Any, Poll]:
    """The message carrying the poll, and the poll on it."""
    message = await client(ctx).get_messages(peer, ids=msg_id)
    media = getattr(message, "media", None) if message is not None else None
    if getattr(media, "poll", None) is None:
        raise NotFoundError(f"message {msg_id} in {chat_id} is not a poll")
    return message, poll_model(media, chat_id=chat_id, msg_id=msg_id)


def _options_for(poll: Poll, indices: list[int]) -> list[bytes]:
    """Caller-typed indices → the server's opaque option bytes."""
    out: list[bytes] = []
    for index in indices:
        if not 0 <= index < len(poll.options):
            raise UsageError(
                f"option {index} does not exist; this poll has {len(poll.options)}",
                field="options",
            )
        out.append(base64.urlsafe_b64decode(poll.options[index].option_b64 + "=="))
    return out


async def _reread(ctx: OpContext, peer: Any, chat_id: int, msg_id: int, updates: Any) -> Poll:
    """The poll as it stands after a mutation.

    The update batch carries the new `MessageMediaPoll` for every method in
    this group except `sendVote`'s partial results, so reading it out of the
    reply avoids a second round trip; falling back to a refetch is what makes
    the callers uniform.
    """
    for update in getattr(updates, "updates", None) or []:
        media = getattr(getattr(update, "message", None), "media", None)
        if getattr(media, "poll", None) is not None:
            return poll_model(media, chat_id=chat_id, msg_id=msg_id)
        if getattr(update, "poll", None) is not None and not getattr(
            getattr(update, "results", None), "min", False
        ):
            return poll_model(update, chat_id=chat_id, msg_id=msg_id)
    _, poll = await _fetch(ctx, peer, chat_id, msg_id)
    return poll


# ---------------------------------------------------------------------------
# poll create
# ---------------------------------------------------------------------------


class CreateReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to post it.")]
    question: Annotated[str, arg(1, metavar="QUESTION", help="The question.")]
    options: Annotated[
        list[str], arg(2, metavar="OPTION", variadic=True, help="Answers, in order.")
    ] = []
    description: Annotated[
        str | None, opt("--description", metavar="TEXT", help="Poll description (layer 229).")
    ] = None
    multiple: Annotated[bool, opt("--multiple", help="Allow multiple answers.")] = False
    public_voters: Annotated[bool, opt("--public-voters", help="Everyone can see who voted.")] = (
        False
    )
    quiz: Annotated[bool, opt("--quiz", help="Quiz mode.")] = False
    correct: Annotated[
        int | None, opt("--correct", metavar="N", help="Correct option index (quiz).")
    ] = None
    explanation: Annotated[
        str | None, opt("--explanation", metavar="TEXT", help="Quiz solution text.")
    ] = None
    explanation_file: Annotated[
        str | None,
        opt("--explanation-file", metavar="PATH", kind="path", help="Media on the solution."),
    ] = None
    no_revote: Annotated[bool, opt("--no-revote", help="Disallow changing a vote.")] = False
    allow_adding_options: Annotated[
        bool, opt("--allow-adding-options", help="Voters may add answers (open answers).")
    ] = False
    shuffle: Annotated[bool, opt("--shuffle", help="Shuffle answer order per viewer.")] = False
    subscribers_only: Annotated[
        bool, opt("--subscribers-only", help="Only channel subscribers may vote.")
    ] = False
    countries: Annotated[
        str | None, opt("--countries", metavar="ISO2,...", help="Restrict voting to these.")
    ] = None
    duration: Annotated[
        str | None, opt("--duration", metavar="DURATION", help="Auto-close after this long.")
    ] = None
    close_at: Annotated[
        str | None, opt("--close-at", metavar="TS", kind="datetime", help="Auto-close then.")
    ] = None
    hide_results: Annotated[
        bool, opt("--hide-results", help="Hide results until the poll closes.")
    ] = False
    media: Annotated[
        str | None, opt("--media", metavar="PATH|URL", help="Media attached to the poll.")
    ] = None
    option_media: Annotated[
        list[str], opt("--option-media", metavar="N=PATH", help="Media on one answer.")
    ] = []
    parse: Annotated[
        str | None, opt("--parse", metavar="MODE", help="md|html|none for every text.")
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None


async def build_media(ctx: OpContext, req: CreateReq) -> Any:
    """`CreateReq` → the `InputMediaPoll` a send needs.

    Public because `message send --poll` builds the same thing from JSON: two
    spellings of "create a poll" that disagreed about defaults would be worse
    than one shared builder.
    """
    from telethon.tl import types

    if req.description is not None:
        raise NotSupportedError(
            "--description is a layer-229 poll field and the pinned Telethon speaks 227; "
            "post the description as the message text instead"
        )
    if req.quiz and req.multiple:
        raise UsageError("a quiz cannot be multiple-choice", field="quiz")
    if req.quiz and req.correct is None:
        raise UsageError("--quiz needs --correct to say which answer is right", field="correct")
    if req.duration and req.close_at:
        raise UsageError("--duration and --close-at are the same field", field="duration")
    if req.hide_results and not (req.duration or req.close_at):
        raise UsageError(
            "--hide-results only means something with --duration or --close-at",
            field="hide-results",
        )
    if len(req.options) < 2:
        raise UsageError("a poll needs at least two answers", field="options")
    if req.correct is not None and not 0 <= req.correct < len(req.options):
        raise UsageError(f"--correct {req.correct} is not one of the answers", field="correct")

    per_option = _option_media(req.option_media, len(req.options))
    answers: list[Any] = []
    for index, text in enumerate(req.options):
        plain, entities = _send.body(text, parse=req.parse)
        source = per_option.get(index)
        answers.append(
            types.InputPollAnswer(
                text=types.TextWithEntities(text=plain, entities=_send.tl_entities(entities) or []),
                media=await _send.input_media(ctx, source) if source else None,
            )
        )

    question, question_entities = _send.body(req.question, parse=req.parse)
    close_date = parse_dt(req.close_at) if req.close_at else None
    poll = types.Poll(
        id=0,
        question=types.TextWithEntities(
            text=question, entities=_send.tl_entities(question_entities) or []
        ),
        answers=answers,
        hash=0,
        closed=None,
        public_voters=req.public_voters or None,
        multiple_choice=req.multiple or None,
        quiz=req.quiz or None,
        open_answers=req.allow_adding_options or None,
        revoting_disabled=req.no_revote or None,
        shuffle_answers=req.shuffle or None,
        hide_results_until_close=req.hide_results or None,
        subscribers_only=req.subscribers_only or None,
        close_period=parse_duration(req.duration) if req.duration else None,
        close_date=close_date,
        countries_iso2=_countries(req.countries),
    )
    solution, solution_entities = (
        _send.body(req.explanation, parse=req.parse) if req.explanation else ("", [])
    )
    return types.InputMediaPoll(
        poll=poll,
        # Layer 227 spells `correct_answers` as a vector of *indices*: an
        # `inputPollAnswer` carries no option bytes at creation time, because
        # the server is the one that assigns them.
        correct_answers=[req.correct] if req.correct is not None else None,
        attached_media=await _send.input_media(ctx, req.media) if req.media else None,
        solution=solution or None,
        solution_entities=(_send.tl_entities(solution_entities) or []) if solution else None,
        solution_media=(
            await _send.input_media(ctx, req.explanation_file) if req.explanation_file else None
        ),
    )


def _option_media(pairs: list[str], count: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for pair in pairs:
        index, sep, path = pair.partition("=")
        if not sep:
            raise UsageError("--option-media wants N=PATH", field="option-media")
        try:
            position = int(index)
        except ValueError as exc:
            raise UsageError(
                f"--option-media: {index!r} is not an index", field="option-media"
            ) from exc
        if not 0 <= position < count:
            raise UsageError(
                f"--option-media {position} is not one of the answers", field="option-media"
            )
        out[position] = path
    return out


def _countries(value: str | None) -> list[str] | None:
    if not value:
        return None
    codes = [part.strip().upper() for part in value.replace(" ", ",").split(",") if part.strip()]
    for code in codes:
        if len(code) != 2 or not code.isalpha():
            raise UsageError(
                f"--countries: {code!r} is not an ISO-3166 alpha-2 code", field="countries"
            )
    return codes or None


async def create(ctx: OpContext, req: CreateReq) -> Poll:
    """Post a poll or quiz, and report the answers with the ids the server gave them.

    The reply is read back rather than echoed: the option bytes every later
    command needs exist only once the server has assigned them.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    media = await build_media(ctx, req)
    reply_to = await _send.reply_target(ctx, reply_to=req.reply_to, topic=req.topic)
    values = {
        "peer": peer,
        "media": media,
        "message": "",
        "random_id": random_id(),
        "silent": req.silent or None,
        "noforwards": req.protect or None,
        "reply_to": reply_to,
        "schedule_date": _send.schedule_at(req.schedule),
        "schedule_repeat_period": _send.repeat_period(req.repeat),
        "send_as": await _send.resolve(ctx, req.send_as) if req.send_as is not None else None,
        "effect": _send.effect_id(req.effect),
        "allow_paid_stars": req.paid_stars,
    }
    result = await client(ctx)(fn.SendMediaRequest(**only(values, fn.SendMediaRequest)))
    sent = _send.message_from_updates(result, chat_id=chat_id)
    poll = await _reread(ctx, peer, chat_id, sent.id, result)
    poll.msg_id = sent.id
    ctx.emit("poll_created", {"chat_id": chat_id, "msg_id": sent.id, "question": poll.question})
    return poll


SPEC_CREATE = OperationSpec(
    id="poll.create",
    request=CreateReq,
    response=Poll,
    impl=create,
    summary="Create a poll or quiz with every option the GUI exposes",
    description=(
        "Answers are addressed by index everywhere in this group, and the "
        "opaque identifier the server assigned each one comes back in "
        "`options[].option_b64` for callers that would rather hold the bytes."
    ),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "poll_id", "question"),
    example=_EXAMPLE_POLL,
    example_args="poll create @team 'Lunch?' Pizza Sushi",
    covers=(
        "poll.allow-adding-options",
        "poll.allow-revoting",
        "poll.attached-media",
        "poll.close-period",
        "poll.country-restriction",
        "poll.create-regular",
        "poll.hide-results-until-close",
        "poll.multiple-choice",
        "poll.option-media",
        "poll.public-voters",
        "poll.quiz",
        "poll.quiz-explanation",
        "poll.quiz-explanation-media",
        "poll.send-as",
        "poll.shuffle-options",
        "poll.subscribers-only",
    ),
    coverage_note=(
        "`--description` (layer 229) is refused with NOT_SUPPORTED: the "
        "pinned Telethon's `poll` constructor has no such field."
    ),
)


# ---------------------------------------------------------------------------
# poll get
# ---------------------------------------------------------------------------


class GetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]
    hash: Annotated[
        int, opt("--hash", metavar="N", help="Last seen poll hash for a cheap not-modified reply.")
    ] = 0
    with_recent_voters: Annotated[
        bool, opt("--with-recent-voters", help="Resolve the recent-voter peers.")
    ] = False
    follow: Annotated[bool, opt("--follow", help="Wait for the poll to close, then report.")] = (
        False
    )
    interval: Annotated[
        str, opt("--interval", metavar="DURATION", help="Refresh interval for --follow.")
    ] = "15s"
    follow_for: Annotated[
        str, opt("--follow-for", metavar="DURATION", help="Give up following after this long.")
    ] = "5m"


async def get(ctx: OpContext, req: GetReq) -> Poll:
    """Poll state and results, with the reason voting is blocked when it is.

    `--follow` blocks until the poll closes rather than streaming: a streaming
    operation streams unconditionally in this architecture, and `poll get`
    has to keep answering with one object.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, poll = await _fetch(ctx, peer, chat_id, req.msg_id)

    if poll.min or req.hash:
        refreshed = await client(ctx)(
            fn.GetPollResultsRequest(peer=peer, msg_id=req.msg_id, poll_hash=req.hash)
        )
        poll = await _reread(ctx, peer, chat_id, req.msg_id, refreshed)

    if req.follow and not poll.closed:
        poll = await _follow(ctx, peer, chat_id, req)
    if req.with_recent_voters and poll.recent_voters:
        # `pollResults.recent_voters` are `min` peers: they mean nothing until
        # they are resolved against a real entity.
        for peer_id in list(poll.recent_voters):
            try:
                await client(ctx).get_entity(peer_id)
            except (ValueError, TypeError):
                ctx.warn(f"recent voter {peer_id} could not be resolved")
    return poll


async def _follow(ctx: OpContext, peer: Any, chat_id: int, req: GetReq) -> Poll:
    """Refresh until the poll closes or the caller's patience runs out."""
    from telethon.tl.functions import messages as fn

    interval = max(5, int(parse_duration(req.interval) or 15))
    deadline = max(interval, int(parse_duration(req.follow_for) or 300))
    waited = 0
    poll = (await _fetch(ctx, peer, chat_id, req.msg_id))[1]
    while not poll.closed and waited < deadline:
        await asyncio.sleep(interval)
        waited += interval
        limiter = getattr(ctx, "limiter", None)
        if limiter is not None:
            await limiter.acquire("read")
        refreshed = await client(ctx)(
            fn.GetPollResultsRequest(peer=peer, msg_id=req.msg_id, poll_hash=0)
        )
        poll = await _reread(ctx, peer, chat_id, req.msg_id, refreshed)
    if not poll.closed:
        ctx.warn(f"--follow gave up after {waited}s; the poll is still open")
    return poll


SPEC_GET = OperationSpec(
    id="poll.get",
    request=GetReq,
    response=Poll,
    impl=get,
    summary="Poll state and results, with the reason voting is blocked",
    description=(
        "`can_vote` and `restriction` are computed on the client: closed, "
        "already voted on a quiz, subscribers-only or country-restricted. "
        "`--follow` waits for the poll to close instead of streaming, so the "
        "answer is still one object."
    ),
    aliases=("poll.results",),
    columns=("msg_id", "question", "closed", "total_voters", "can_vote"),
    example=_EXAMPLE_POLL,
    example_args="poll get @team 12345",
    covers=(
        "poll.get-results",
        "poll.live-updates",
        "poll.option-properties",
        "poll.recent-voters-preview",
        "poll.vote-restriction-reasons",
    ),
    coverage_note=(
        "`poll.option-properties` and `poll.vote-restriction-reasons` have no "
        "MTProto method at all; both are the client-side derivation this op "
        "publishes as `can_vote`/`restriction` and the per-option flags."
    ),
    timeout_s=360,
)


# ---------------------------------------------------------------------------
# poll vote / close
# ---------------------------------------------------------------------------


class VoteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]
    options: Annotated[
        list[int], arg(2, metavar="OPTION", variadic=True, help="Option indices.")
    ] = []
    retract: Annotated[bool, opt("--retract", help="Retract the vote (empty vector).")] = False


async def vote(ctx: OpContext, req: VoteReq) -> Poll:
    """Vote in a poll, or retract the vote by passing no options.

    Indices are resolved against a freshly fetched copy of the poll because
    `shuffle_answers` makes the display order per-viewer; voting on "the
    second one I saw" would otherwise be a different answer for each client.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, poll = await _fetch(ctx, peer, chat_id, req.msg_id)
    if req.options and req.retract:
        raise UsageError("--retract takes no options", field="retract")
    if not poll.can_vote and not req.retract:
        raise PermissionError_(
            f"this poll cannot be voted in: {poll.restriction or 'closed'}",
        )
    if len(req.options) > 1 and not poll.multiple:
        raise UsageError("this poll accepts one answer", field="options")

    chosen = _options_for(poll, list(req.options)) if not req.retract else []
    result = await client(ctx)(fn.SendVoteRequest(peer=peer, msg_id=req.msg_id, options=chosen))
    updated = await _reread(ctx, peer, chat_id, req.msg_id, result)
    ctx.emit("poll_vote", {"chat_id": chat_id, "msg_id": req.msg_id, "options": list(req.options)})
    return updated


SPEC_VOTE = OperationSpec(
    id="poll.vote",
    request=VoteReq,
    response=Poll,
    impl=vote,
    summary="Vote in a poll, or retract your vote",
    description=(
        "Indices are resolved against the server's own copy of the poll, so "
        "`--shuffle` cannot make the same index mean two things. An empty "
        "vector retracts, which quizzes and no-revote polls refuse."
    ),
    mutating=True,
    rate_class="send",
    columns=("msg_id", "total_voters", "my_votes"),
    example=_EXAMPLE_POLL,
    example_args="poll vote @team 12345 0",
    covers=("poll.retract-vote", "poll.vote"),
)


class CloseReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]


async def close(ctx: OpContext, req: CloseReq) -> Poll:
    """Stop a poll: final results, no more voting.

    There is no `stopPoll` on the wire. This is `editMessage` carrying the
    same poll with `closed=True`, answers and option bytes included, because
    a rewritten answer list would orphan every vote already cast.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    message, poll = await _fetch(ctx, peer, chat_id, req.msg_id)
    if poll.closed:
        already(ctx)
        poll.already = True
        return poll

    raw = message.media.poll
    closed = types.Poll(
        id=raw.id,
        question=raw.question,
        answers=raw.answers,
        hash=getattr(raw, "hash", 0) or 0,
        closed=True,
        public_voters=getattr(raw, "public_voters", None),
        multiple_choice=getattr(raw, "multiple_choice", None),
        quiz=getattr(raw, "quiz", None),
    )
    try:
        result = await client(ctx)(
            fn.EditMessageRequest(peer=peer, id=req.msg_id, media=types.InputMediaPoll(poll=closed))
        )
    except Exception as exc:
        if not is_not_modified(exc):
            raise
        already(ctx)
        poll.already = True
        return poll
    updated = await _reread(ctx, peer, chat_id, req.msg_id, result)
    updated.closed = True
    ctx.emit("poll_closed", {"chat_id": chat_id, "msg_id": req.msg_id})
    return updated


SPEC_CLOSE = OperationSpec(
    id="poll.close",
    request=CloseReq,
    response=Poll,
    impl=close,
    summary="Stop a poll so the results are final",
    description=(
        "Closing cannot be undone. An already-closed poll is `already: true`, not an error."
    ),
    aliases=("poll.stop",),
    mutating=True,
    destructive=True,
    idempotent=True,
    rate_class="send",
    columns=("msg_id", "closed", "total_voters"),
    example={**_EXAMPLE_POLL, "closed": True},
    example_args="poll close @team 12345",
    covers=("poll.stop",),
)


# ---------------------------------------------------------------------------
# poll option add / remove
# ---------------------------------------------------------------------------


class OptionAddReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]
    text: Annotated[str, arg(2, metavar="TEXT", help="The answer to add.")]
    media: Annotated[
        str | None, opt("--media", metavar="PATH", kind="path", help="Media on the new option.")
    ] = None
    parse: Annotated[str | None, opt("--parse", metavar="MODE", help="md|html|none.")] = None


async def option_add(ctx: OpContext, req: OptionAddReq) -> Poll:
    """Add an answer to an open-answer poll.

    The server assigns the new option's bytes, so the poll is read back: the
    index a caller will vote with does not exist until the round trip is done.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, poll = await _fetch(ctx, peer, chat_id, req.msg_id)
    if not poll.open_answers:
        raise PermissionError_(
            "this poll was not created with --allow-adding-options, so no answer may be added"
        )
    plain, entities = _send.body(req.text, parse=req.parse)
    answer = types.InputPollAnswer(
        text=types.TextWithEntities(text=plain, entities=_send.tl_entities(entities) or []),
        media=await _send.input_media(ctx, req.media) if req.media else None,
    )
    result = await client(ctx)(fn.AddPollAnswerRequest(peer=peer, msg_id=req.msg_id, answer=answer))
    return await _reread(ctx, peer, chat_id, req.msg_id, result)


SPEC_OPTION_ADD = OperationSpec(
    id="poll.option.add",
    request=OptionAddReq,
    response=Poll,
    impl=option_add,
    summary="Add an answer to an open-answer poll",
    mutating=True,
    rate_class="send",
    columns=("msg_id", "total_voters"),
    example=_EXAMPLE_POLL,
    example_args="poll option add @team 12345 'Ramen'",
    covers=("poll.add-option",),
)


class OptionRemoveReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]
    option: Annotated[int, arg(2, metavar="OPTION", help="Option index to delete.", ge=0)]


async def option_remove(ctx: OpContext, req: OptionRemoveReq) -> Poll:
    """Delete an answer from an open-answer poll, losing its votes."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, poll = await _fetch(ctx, peer, chat_id, req.msg_id)
    option = _options_for(poll, [req.option])[0]
    result = await client(ctx)(
        fn.DeletePollAnswerRequest(peer=peer, msg_id=req.msg_id, option=option)
    )
    return await _reread(ctx, peer, chat_id, req.msg_id, result)


SPEC_OPTION_REMOVE = OperationSpec(
    id="poll.option.remove",
    request=OptionRemoveReq,
    response=Poll,
    impl=option_remove,
    summary="Delete an answer from an open-answer poll",
    description="The votes cast on that answer go with it. Surviving answers keep their ids.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("msg_id", "total_voters"),
    example=_EXAMPLE_POLL,
    example_args="poll option remove @team 12345 2",
    covers=("poll.delete-option",),
)


# ---------------------------------------------------------------------------
# poll voter list
# ---------------------------------------------------------------------------


class VoterListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]
    option: Annotated[int | None, opt("--option", metavar="N", help="Only this option index.")] = (
        None
    )


async def voter_list(ctx: OpContext, req: VoterListReq) -> Page[PollVoter]:
    """Who voted, per option — public polls only.

    Pagination is `votesList.next_offset`, an opaque *string*: treating it as
    an integer offset (which is what every other listing uses) silently
    restarts the walk at the top.
    """
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "poll.voter.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, poll = await _fetch(ctx, peer, chat_id, req.msg_id)
    if not poll.public_voters:
        raise PermissionError_("this poll is anonymous; there is no voter list to read")

    option = _options_for(poll, [req.option])[0] if req.option is not None else None
    by_option = {
        base64.urlsafe_b64decode(item.option_b64 + "=="): item.index for item in poll.options
    }
    result = await client(ctx)(
        fn.GetPollVotesRequest(
            peer=peer,
            id=req.msg_id,
            limit=limit,
            option=option,
            offset=state.get("offset") or None,
        )
    )
    items: list[PollVoter] = []
    for vote_row in getattr(result, "votes", None) or []:
        options = [
            by_option[bytes(raw)]
            for raw in (getattr(vote_row, "options", None) or ([getattr(vote_row, "option", b"")]))
            if bytes(raw) in by_option
        ]
        date = getattr(vote_row, "date", None)
        items.append(
            PollVoter(
                user_id=_peer_id(getattr(vote_row, "peer", None)) or 0,
                option=options[0] if options else req.option,
                options=options,
                date=fmt_dt(date),
                date_unix=to_unix(date),
            )
        )
    next_offset = getattr(result, "next_offset", None)
    return build_page(
        items,
        op="poll.voter.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


SPEC_VOTER_LIST = OperationSpec(
    id="poll.voter.list",
    request=VoterListReq,
    response=Page[PollVoter],
    impl=voter_list,
    summary="Who voted, per option (public polls)",
    description="Anonymous polls answer with PERMISSION_DENIED — the list does not exist.",
    aliases=("poll.voters",),
    paginated=PageKind.PARTICIPANTS,
    columns=("user_id", "option", "date"),
    example={
        "items": [{"user_id": 4242, "option": 0, "date": "2026-09-03T09:20:00Z"}],
        "has_more": False,
    },
    example_args="poll voter list @team 12345",
    covers=("poll.get-voters",),
)


# ---------------------------------------------------------------------------
# poll unread list
# ---------------------------------------------------------------------------


class UnreadListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Only this forum topic.")
    ] = None
    read_all: Annotated[bool, opt("--read-all", help="Mark every poll vote as read.")] = False


async def unread_list(ctx: OpContext, req: UnreadListReq) -> Page[Message]:
    """Polls in this chat with votes I have not seen, newest first."""
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "poll.unread.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    result = await client(ctx)(
        fn.GetUnreadPollVotesRequest(
            peer=peer,
            offset_id=int(state.get("offset_id") or 0),
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            top_msg_id=req.topic,
        )
    )
    items = [
        message_to_model(raw, chat_id=chat_id) for raw in (getattr(result, "messages", None) or [])
    ]
    if req.read_all:
        await affected_loop(
            ctx, lambda offset: fn.ReadPollVotesRequest(peer=peer, top_msg_id=req.topic)
        )

    return build_page(
        items,
        op="poll.unread.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset_id": items[-1].id if items else 0},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_UNREAD_LIST = OperationSpec(
    id="poll.unread.list",
    request=UnreadListReq,
    response=Page[Message],
    impl=unread_list,
    summary="Polls in a chat with votes I have not read yet",
    description=(
        "`--read-all` drives `messages.readPollVotes` until its offset comes "
        "back zero; calling it once clears only the first page of the badge."
    ),
    paginated=PageKind.PARTICIPANTS,
    tags=frozenset({"mutating-checked"}),
    columns=("id", "chat_id", "date", "text"),
    example={
        "items": [
            {
                "id": 12345,
                "chat_id": 777123,
                "date": "2026-09-03T09:14:07Z",
                "date_unix": 1788340447,
                "text": "Lunch?",
            }
        ],
        "has_more": False,
    },
    example_args="poll unread list @team",
    covers=("poll.read-votes", "poll.unread-votes"),
)


# ---------------------------------------------------------------------------
# poll stats get
# ---------------------------------------------------------------------------


class StatsGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Poll message id.")]
    dark: Annotated[bool, opt("--dark", help="Dark-theme graph.")] = False


async def stats_get(ctx: OpContext, req: StatsGetReq) -> PollStats:
    """The poll's vote-statistics graph, from the channel's stats DC.

    Statistics do not live on the home data centre: the first call answers
    `STATS_MIGRATE_X` and the request has to be re-issued on that DC through
    a borrowed sender, exactly as Telethon's own `get_stats` does.
    """
    from telethon import errors
    from telethon.tl.functions import stats as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    input_channel(peer)  # a poll graph only exists for channels/supergroups
    request = fn.GetPollStatsRequest(peer=peer, msg_id=req.msg_id, dark=req.dark or None)
    handle = client(ctx)
    dc_id: int | None = None
    try:
        result = await handle(request)
    except errors.StatsMigrateError as exc:
        dc_id = int(exc.dc)
        sender = await handle._borrow_exported_sender(dc_id)
        try:
            result = await sender.send(request)
        finally:
            await handle._return_exported_sender(sender)

    graph = getattr(result, "votes_graph", None) or result
    error = getattr(graph, "error", None)
    if error:
        raise NotFoundError(f"the poll statistics graph is unavailable: {error}")
    payload = getattr(getattr(graph, "json", None), "data", None)
    return PollStats(
        chat_id=chat_id,
        msg_id=req.msg_id,
        graph=payload,
        token=getattr(graph, "token", None) or getattr(graph, "zoom_token", None),
        dark=req.dark,
        dc_id=dc_id,
    )


SPEC_STATS_GET = OperationSpec(
    id="poll.stats.get",
    request=StatsGetReq,
    response=PollStats,
    impl=stats_get,
    summary="Poll vote statistics graph (channel admins)",
    description=(
        "Needs `channelFull.can_view_stats`. The graph comes back either "
        "inline as `graph` or as an asynchronous `token` to load later; both "
        "are reported rather than one being silently resolved."
    ),
    columns=("chat_id", "msg_id", "token"),
    example={"chat_id": -1001234567890, "msg_id": 12345, "token": "graph-token"},
    example_args="poll stats get @news 12345",
    covers=("poll.statistics",),
    timeout_s=180,
)
