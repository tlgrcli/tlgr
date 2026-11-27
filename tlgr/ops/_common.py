"""The plumbing every operation module needs, in one place.

`message` grew these helpers first — the client accessor, the pagination
window, the affected-history loop, the id-range expander — and PR-9's five
new modules need all of them. Copying them five times is how "`--limit` means
at most 1000" becomes true in one module and false in the next, so they live
here and `ops/message.py` imports them like everybody else.

Telethon is imported inside functions, never at module scope: importing the
registry is what builds `tlgr --help`, and that must not pull in Telethon.
"""

from __future__ import annotations

from typing import Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, decode_cursor
from tlgr.ops._spec import OpContext

__all__ = [
    "affected_loop",
    "already",
    "client",
    "ids",
    "ids_outside_chat",
    "input_channel",
    "is_not_modified",
    "is_range",
    "not_in_chat",
    "only",
    "random_id",
    "window",
]


def client(ctx: OpContext) -> Any:
    """The connected Telethon client, or a usage error."""
    handle = getattr(ctx, "client", None)
    if handle is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return handle


def window(ctx: OpContext, op: str, kind: PageKind, default: int = 20) -> tuple[int, Any]:
    """`(limit, cursor state)` for a paginated op.

    `--limit`/`--cursor` are transport-level and never request fields
    (registry lint L5), so every paginated implementation reads them the same
    way instead of redeclaring them.
    """
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 1000), state


def ids(values: tuple[int, ...] | tuple[str, ...] | list[str] | None) -> list[int]:
    """Expand `100-120` ranges alongside plain ids.

    A range is what a human types when deleting a burst of messages, and
    making them spell out twenty ids is how a wrong one gets in.
    """
    out: list[int] = []
    for value in values or ():
        text = str(value)
        if is_range(text):
            head, _, tail = text.partition("-")
            try:
                start, end = int(head), int(tail)
            except ValueError as exc:
                raise UsageError(f"{text!r} is not an id or an id range", field="msg_id") from exc
            if end < start or end - start > 10_000:
                raise UsageError(f"{text!r} is not a usable id range", field="msg_id")
            out.extend(range(start, end + 1))
        else:
            try:
                out.append(int(text))
            except ValueError as exc:
                raise UsageError(f"{text!r} is not a message id", field="msg_id") from exc
    return out


def is_range(value: object) -> bool:
    """`100-120` rather than `100` or `-5`: the shape `ids` expands."""
    return "-" in str(value)[1:]


async def ids_outside_chat(ctx: OpContext, peer: Any, wanted: list[int]) -> list[int]:
    """The ids in *wanted* that are not messages of *peer*, in order.

    Private chats and basic groups share one message-id counter per account,
    and `messages.deleteMessages` takes bare ids with no peer at all: Telethon
    drops the peer it was given. Deleting `[105]` "in @alice" therefore deletes
    message 105 wherever it lives, in some other chat, for both sides. So the
    ids are fetched back through the peer first (`get_messages` with a peer
    returns None for an id from another chat), and only the ones that come
    back may be deleted. Channels and supergroups number their own messages
    and `channels.deleteMessages` honours the channel, so they are not checked.

    A deleted message also comes back None: from here "already gone" and
    "belongs to someone else" look the same, and both mean "do not send it".
    """
    from telethon.tl import types

    if not wanted or isinstance(
        peer, (types.InputPeerChannel, types.InputPeerChannelFromMessage, types.InputChannel)
    ):
        return []
    handle = client(ctx)
    present: set[int] = set()
    for start in range(0, len(wanted), 100):
        if start:
            limiter = getattr(ctx, "limiter", None)
            if limiter is not None:
                await limiter.acquire("bulk")
        found = await handle.get_messages(peer, ids=wanted[start : start + 100])
        if found is not None and not isinstance(found, list):
            found = [found]
        present.update(int(m.id) for m in found or [] if m is not None)
    return [i for i in wanted if i not in present]


def not_in_chat(chat: str, refused: list[int]) -> NotFoundError:
    """The refusal for ids `ids_outside_chat` found, raised before any delete."""
    listed = ", ".join(str(i) for i in refused[:20])
    if len(refused) > 20:
        listed += f" and {len(refused) - 20} more"
    error = NotFoundError(
        f"message id(s) {listed} are not in {chat}: deleted already, or they belong to "
        "another chat. In private chats and basic groups Telegram deletes by bare id "
        "across the whole account, so nothing was deleted."
    )
    error.hint = f"Check the ids with: tlgr message list {chat}"
    return error


def is_not_modified(exc: BaseException) -> bool:
    """MESSAGE_NOT_MODIFIED is success: the world already looks like that.

    Matched on the class name *and* the message with underscores stripped,
    because Telethon spells it `MessageNotModifiedError` and the server
    spells it `MESSAGE_NOT_MODIFIED`.
    """
    text = f"{type(exc).__name__} {exc}".upper().replace("_", "")
    return "NOTMODIFIED" in text


def input_channel(peer: Any) -> Any:
    """The `InputChannel` a `channels.*` request wants, or a usage error.

    `utils.get_input_channel` is arithmetic on the peer we already hold; going
    back to `get_input_entity` would be a round trip for something that is
    already known, and would hide the real problem when the peer is a user.
    """
    from telethon import utils

    try:
        return utils.get_input_channel(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError(
            "this operation only works in a channel or supergroup", field="chat"
        ) from exc


async def affected_loop(ctx: OpContext, make_request: Any) -> int:
    """Drive a `messages.AffectedHistory` call until `offset == 0`.

    `readMentions`, `readReactions`, `readPollVotes`, `unpinAllMessages` and
    `deleteParticipantHistory` all return a partial result with an offset to
    resume from. v1 called them once and reported success, so "unpin
    everything" unpinned the first hundred.
    """
    handle = client(ctx)
    total = 0
    offset = 0
    for _ in range(100):
        result = await handle(make_request(offset))
        total += int(getattr(result, "pts_count", 0) or 0)
        offset = int(getattr(result, "offset", 0) or 0)
        if offset == 0:
            break
        limiter = getattr(ctx, "limiter", None)
        if limiter is not None:
            await limiter.acquire("bulk")
    return total


def already(ctx: OpContext) -> None:
    """Record that the world already looked the way the caller asked for."""
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


def only(values: dict[str, Any], request: Any) -> dict[str, Any]:
    """Keep the keys *request* actually accepts.

    The send requests share most of their flags and differ in a few; a
    filtered dict is how one options mapping serves all of them without a
    per-request copy that drifts.
    """
    import inspect

    allowed = set(inspect.signature(request.__init__).parameters)
    return {k: v for k, v in values.items() if k in allowed and v is not None}


def random_id() -> int:
    """A 64-bit `random_id`, as every send request wants one."""
    import os

    return int.from_bytes(os.urandom(8), "big", signed=True)
