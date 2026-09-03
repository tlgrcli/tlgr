"""The `reaction` group: reacting, reading reactions, and the policy around them.

`messages.sendReaction` takes the **whole desired state**, not a delta. Adding
a second reaction means resending the first one too, and removing one means
resending the others; a client that treats it as "add this emoji" silently
replaces everything the account had already put on the message. Every write
here therefore reads my current reactions first, and `--replace` is the
explicit way to ask for the destructive reading.

One spelling for a reaction across the group: a unicode emoji is itself, and a
custom (Premium) emoji is `custom:<document_id>`. `reaction list` hands back
exactly what `reaction remove` accepts.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.message import Message
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.reaction import (
    AvailableReaction,
    ChatReactions,
    MessageReactionState,
    PaidReactionResult,
    ReactionPrivacy,
    ReactionPurge,
    ReactionReport,
    ReactionResult,
    ReactionTag,
    ReactionUser,
    TopReactor,
)
from tlgr.ops import _send
from tlgr.ops._common import (
    affected_loop,
    already,
    client,
    ids,
    input_channel,
    is_not_modified,
    window,
)
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import message_to_model, peer_id_of, reactions_summary
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The prefix that names a custom (Premium) emoji reaction in tlgr's JSON.
CUSTOM = "custom:"

_EXAMPLE_REACT: dict[str, Any] = {
    "chat_id": 777123,
    "msg_id": 12345,
    "emoji": "👍",
    "reacted": True,
    "mine": ["👍"],
    "reactions": {"counts": {"👍": 4}, "mine": ["👍"], "total": 4},
}


# ---------------------------------------------------------------------------
# The one spelling of a reaction
# ---------------------------------------------------------------------------


def name_of(reaction: Any) -> str:
    """A TL `Reaction` → the string tlgr prints and accepts back."""
    emoticon = getattr(reaction, "emoticon", None)
    if emoticon:
        return str(emoticon)
    document = getattr(reaction, "document_id", None)
    if document is not None:
        return f"{CUSTOM}{document}"
    if type(reaction).__name__ == "ReactionPaid":
        return "stars"
    return "?"


def to_tl(name: str) -> Any:
    """`"👍"` / `"custom:123"` / `"stars"` → the TL `Reaction` for it."""
    from telethon.tl import types

    if name == "stars":
        return types.ReactionPaid()
    if name.startswith(CUSTOM):
        try:
            return types.ReactionCustomEmoji(document_id=int(name[len(CUSTOM) :]))
        except ValueError as exc:
            raise UsageError(f"{name!r} is not a custom emoji id", field="custom") from exc
    if not name:
        raise UsageError("a reaction cannot be empty", field="emoji")
    return types.ReactionEmoji(emoticon=name)


def _wanted(emoji: list[str], custom: list[int]) -> list[str]:
    """The reactions a caller named, in the order they named them."""
    return [*emoji, *(f"{CUSTOM}{document}" for document in custom)]


async def _current(ctx: OpContext, peer: Any, chat_id: int, msg_id: int) -> tuple[Any, list[str]]:
    """`(message, the reactions this account currently holds on it)`.

    Read before every write because `sendReaction` replaces the set: without
    this, "add 🎉" would silently remove the 👍 that was already there.
    """
    message = await client(ctx).get_messages(peer, ids=msg_id)
    if message is None:
        raise NotFoundError(f"message {msg_id} was not found in {chat_id}")
    summary = reactions_summary(message)
    return message, list(summary.mine) if summary is not None else []


def _result(
    chat_id: int, msg_id: int, message: Any, wanted: list[str], *, reacted: bool
) -> ReactionResult:
    summary = reactions_summary(message)
    return ReactionResult(
        chat_id=chat_id,
        msg_id=msg_id,
        emoji=wanted[0] if wanted else "",
        reacted=reacted,
        mine=list(summary.mine) if summary is not None else [],
        reactions=summary,
    )


async def _send_reaction(
    ctx: OpContext,
    peer: Any,
    chat_id: int,
    msg_id: int,
    names: list[str],
    *,
    big: bool = False,
    recent: bool = False,
    reacted: bool,
    primary: list[str],
) -> ReactionResult:
    """Send the full desired state, mapping NOT_MODIFIED to `already`."""
    from telethon.tl.functions import messages as fn

    try:
        updates = await client(ctx)(
            fn.SendReactionRequest(
                peer=peer,
                msg_id=msg_id,
                reaction=[to_tl(name) for name in names] or None,
                big=big or None,
                add_to_recent=recent or None,
            )
        )
    except Exception as exc:
        if not is_not_modified(exc):
            raise
        already(ctx)
        message, mine = await _current(ctx, peer, chat_id, msg_id)
        result = _result(chat_id, msg_id, message, primary, reacted=reacted)
        result.already = True
        result.mine = mine
        return result

    produced = _send.messages_from_updates(updates, chat_id=chat_id)
    message = produced[0] if produced else None
    summary = message.reactions if message is not None else None
    return ReactionResult(
        chat_id=chat_id,
        msg_id=msg_id,
        emoji=primary[0] if primary else "",
        reacted=reacted,
        mine=list(summary.mine) if summary is not None else [],
        reactions=summary,
    )


# ---------------------------------------------------------------------------
# reaction add / remove
# ---------------------------------------------------------------------------


class AddReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id or link.")]
    emoji: Annotated[
        list[str],
        arg(2, metavar="EMOJI", variadic=True, help="Reactions; omit to clear them all."),
    ] = []
    custom: Annotated[
        list[int], opt("--custom", metavar="ID", help="Custom (Premium) emoji id; repeatable.")
    ] = []
    big: Annotated[bool, opt("--big", help="Play the big animation.")] = False
    recent: Annotated[bool, opt("--recent", help="Remember it as recently used.")] = False
    replace: Annotated[
        bool, opt("--replace", help="Send exactly this set instead of adding to mine.")
    ] = False
    send_as: Annotated[
        PeerRef | None, opt("--send-as", metavar="PEER", kind="peer", help="Paid reactions only.")
    ] = None


async def add(ctx: OpContext, req: AddReq) -> ReactionResult:
    """React to a message, keeping the reactions I already had.

    v1's `message react` is this command; it kept one reaction at a time
    because it sent the emoji alone, which is what `sendReaction`'s
    whole-state contract turns into "replace everything". Passing no emoji
    clears my reactions, as it always did.
    """
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    if req.send_as is not None:
        raise UsageError(
            "sendReaction has no send-as field; only a Star reaction can be paid as a "
            "channel — use `tlgr reaction pay --send-as`",
            field="send-as",
        )

    wanted = _wanted(req.emoji, req.custom)
    _, mine = await _current(ctx, peer, chat_id, req.msg_id)
    if not wanted:
        names: list[str] = []
    elif req.replace:
        names = wanted
    else:
        # Ascending `chosen_order`: the ones already there first, the new ones
        # last, which is the order Telegram stores and every client renders.
        names = [*mine, *(name for name in wanted if name not in mine)]

    return await _send_reaction(
        ctx,
        peer,
        chat_id,
        req.msg_id,
        names,
        big=req.big,
        recent=req.recent,
        reacted=bool(names),
        primary=wanted,
    )


SPEC_ADD = OperationSpec(
    id="reaction.add",
    request=AddReq,
    response=ReactionResult,
    impl=add,
    summary="React to a message with one or more unicode or custom emoji",
    description=(
        "`sendReaction` carries the whole desired state, so tlgr reads the "
        "reactions this account already has and resends them alongside the "
        "new one; `--replace` sends exactly what was asked for instead. "
        "Reacting twice is `already: true`, not an error."
    ),
    aliases=("msg.react", "react.add"),
    legacy_paths=("message react", "msg react"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "emoji", "reacted"),
    example=_EXAMPLE_REACT,
    example_args="reaction add @alice 12345 👍",
    tags=frozenset({"visible-to-others"}),
    covers=(
        "emoji.interaction",
        "messages-core.reaction-add-remove",
        "messages-core.saved-tags-add",
        "reaction.add-to-recent",
        "reaction.big",
        "reaction.send-custom-emoji",
        "reaction.send-emoji",
        "reaction.send-multiple",
        "reaction.service-messages",
    ),
    coverage_note=(
        "`emoji.interaction` (the hearts-and-fireworks tap animation) is the "
        "same gesture from a CLI's point of view: tlgr sends the reaction and "
        "does not replay the per-tap animation payload."
    ),
)


class RemoveReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id or link.")]
    emoji: Annotated[
        str, arg(2, metavar="EMOJI", required=False, help="Which one; omit for all of mine.")
    ] = ""
    custom: Annotated[
        int | None, opt("--custom", metavar="ID", help="Remove this custom-emoji reaction.")
    ] = None
    every: Annotated[bool, opt("--every", help="Remove every reaction of mine.")] = False


async def remove(ctx: OpContext, req: RemoveReq) -> ReactionResult:
    """Remove one of my reactions, or all of them.

    Removal is the same `sendReaction` with the surviving set; an empty
    vector clears everything.
    """
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    target = f"{CUSTOM}{req.custom}" if req.custom is not None else req.emoji
    message, mine = await _current(ctx, peer, chat_id, req.msg_id)

    if not mine:
        already(ctx)
        result = _result(chat_id, req.msg_id, message, [target] if target else [], reacted=False)
        result.already = True
        return result

    if req.every or not target:
        names: list[str] = []
    else:
        if target not in mine:
            already(ctx)
            result = _result(chat_id, req.msg_id, message, [target], reacted=False)
            result.already = True
            return result
        names = [name for name in mine if name != target]

    return await _send_reaction(
        ctx,
        peer,
        chat_id,
        req.msg_id,
        names,
        reacted=False,
        primary=[target] if target else [],
    )


SPEC_REMOVE = OperationSpec(
    id="reaction.remove",
    request=RemoveReq,
    response=ReactionResult,
    impl=remove,
    summary="Remove one of my reactions, or all of them",
    description=(
        "Naming no reaction removes all of mine. A reaction that was not "
        "there is `already: true`, so a retry is safe."
    ),
    aliases=("message.unreact", "react.remove"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "emoji", "reacted"),
    example={**_EXAMPLE_REACT, "reacted": False, "mine": []},
    example_args="reaction remove @alice 12345 👍",
    covers=("reaction.remove",),
)


# ---------------------------------------------------------------------------
# reaction list / user list
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        list[str], arg(1, metavar="MSG_ID", variadic=True, help="One or more message ids.")
    ] = []
    top_senders: Annotated[
        bool, opt("--top-senders", help="Include the Star-reaction leaderboard.")
    ] = False


async def reaction_list(ctx: OpContext, req: ListReq) -> Page[MessageReactionState]:
    """Reaction counts on one or many messages, refreshed from the server.

    `messages.getMessagesReactions` is the only source of
    `messageReactions.top_reactors`: the leaderboard is not on the plain
    message, so `--top-senders` cannot be answered from a cached copy.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    wanted = ids(tuple(req.msg_id))
    if not wanted:
        raise UsageError("at least one message id is required", field="msg_id")

    updates = await client(ctx)(fn.GetMessagesReactionsRequest(peer=peer, id=wanted))
    by_id: dict[int, Any] = {}
    for update in getattr(updates, "updates", None) or []:
        if type(update).__name__ == "UpdateMessageReactions":
            by_id[int(getattr(update, "msg_id", 0))] = getattr(update, "reactions", None)
        message = getattr(update, "message", None)
        if message is not None and getattr(message, "reactions", None) is not None:
            by_id.setdefault(int(message.id), message.reactions)

    items: list[MessageReactionState] = []
    for msg_id in wanted:
        raw = by_id.get(msg_id)

        class _Holder:
            reactions = raw

        summary = reactions_summary(_Holder())
        items.append(
            MessageReactionState(
                chat_id=chat_id,
                msg_id=msg_id,
                reactions=summary,
                can_see_list=bool(getattr(raw, "can_see_list", False)),
                as_tags=bool(getattr(raw, "reactions_as_tags", False)),
                top_reactors=_reactors(raw) if req.top_senders else [],
            )
        )
    return Page(items=items, has_more=False, total=len(items))


def _reactors(raw: Any) -> list[TopReactor]:
    out: list[TopReactor] = []
    for reactor in getattr(raw, "top_reactors", None) or []:
        out.append(
            TopReactor(
                user_id=peer_id_of(getattr(reactor, "peer_id", None)),
                stars=int(getattr(reactor, "count", 0) or 0),
                anonymous=bool(getattr(reactor, "anonymous", False)),
                mine=bool(getattr(reactor, "my", False)),
            )
        )
    return out


SPEC_LIST = OperationSpec(
    id="reaction.list",
    request=ListReq,
    response=Page[MessageReactionState],
    impl=reaction_list,
    summary="Reaction counts on one or many messages",
    description=(
        "`--top-senders` adds the Star-reaction leaderboard, which only "
        "`messages.getMessagesReactions` returns — it is not on the message."
    ),
    aliases=("react.list",),
    columns=("msg_id", "can_see_list"),
    example={
        "items": [
            {
                "chat_id": 777123,
                "msg_id": 12345,
                "reactions": {"counts": {"👍": 4}, "total": 4},
                "can_see_list": True,
            }
        ],
        "has_more": False,
    },
    example_args="reaction list @alice 12345",
    covers=(
        "messages-core.reactions-count",
        "reaction.bulk-refresh",
        "reaction.paid-leaderboard",
        "reaction.read-summary",
        "stories.available-reactions",
    ),
    coverage_note=(
        "`stories.available-reactions` is the same catalogue this group "
        "publishes as `reaction catalog`; the story surface itself is PR-8's."
    ),
)


class UserListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id.")]
    emoji: Annotated[str | None, opt("--emoji", metavar="EMOJI", help="Only this reaction.")] = None
    custom: Annotated[
        int | None, opt("--custom", metavar="ID", help="Only this custom-emoji reaction.")
    ] = None


async def user_list(ctx: OpContext, req: UserListReq) -> Page[ReactionUser]:
    """Who reacted, per emoji.

    Only available when `messageReactions.can_see_list` — groups and small
    channels. Pagination is an opaque *string* offset, so the cursor carries
    it verbatim rather than an integer that would restart the walk.
    """
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "reaction.user.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    filter_name = f"{CUSTOM}{req.custom}" if req.custom is not None else req.emoji

    result = await client(ctx)(
        fn.GetMessageReactionsListRequest(
            peer=peer,
            id=req.msg_id,
            limit=limit,
            reaction=to_tl(filter_name) if filter_name else None,
            offset=state.get("offset") or None,
        )
    )
    items = [
        ReactionUser(
            user_id=peer_id_of(getattr(row, "peer_id", None)) or 0,
            reaction=name_of(getattr(row, "reaction", None)),
            date=fmt_dt(getattr(row, "date", None)),
            date_unix=to_unix(getattr(row, "date", None)),
            big=bool(getattr(row, "big", False)),
            unread=bool(getattr(row, "unread", False)),
            mine=bool(getattr(row, "my", False)),
        )
        for row in (getattr(result, "reactions", None) or [])
    ]
    next_offset = getattr(result, "next_offset", None)
    return build_page(
        items,
        op="reaction.user.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


SPEC_USER_LIST = OperationSpec(
    id="reaction.user.list",
    request=UserListReq,
    response=Page[ReactionUser],
    impl=user_list,
    summary="Who reacted to a message, per emoji",
    description="Only where `can_see_list` is set — groups, and small channels.",
    aliases=("react.who", "reaction.users"),
    paginated=PageKind.PARTICIPANTS,
    columns=("user_id", "reaction", "date"),
    example={
        "items": [{"user_id": 4242, "reaction": "👍", "date": "2026-09-03T09:20:00Z"}],
        "has_more": False,
    },
    example_args="reaction user list @alice 12345",
    covers=("reaction.who-reacted",),
)


# ---------------------------------------------------------------------------
# reaction unread list
# ---------------------------------------------------------------------------


class UnreadListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Only this forum topic.")
    ] = None
    direct_to: Annotated[
        PeerRef | None,
        opt("--direct-to", metavar="USER", kind="user", help="Only this monoforum topic."),
    ] = None
    read_all: Annotated[bool, opt("--read-all", help="Mark every unread reaction as read.")] = False


async def unread_list(ctx: OpContext, req: UnreadListReq) -> Page[Message]:
    """Messages in this chat carrying reactions I have not seen."""
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "reaction.unread.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    saved = await _send.resolve(ctx, req.direct_to) if req.direct_to is not None else None

    result = await client(ctx)(
        fn.GetUnreadReactionsRequest(
            peer=peer,
            offset_id=int(state.get("offset_id") or 0),
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            top_msg_id=req.topic,
            saved_peer_id=saved,
        )
    )
    items = [
        message_to_model(raw, chat_id=chat_id) for raw in (getattr(result, "messages", None) or [])
    ]
    if req.read_all:
        await affected_loop(
            ctx,
            lambda offset: fn.ReadReactionsRequest(
                peer=peer, top_msg_id=req.topic, saved_peer_id=saved
            ),
        )
    return build_page(
        items,
        op="reaction.unread.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset_id": items[-1].id if items else 0},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_UNREAD_LIST = OperationSpec(
    id="reaction.unread.list",
    request=UnreadListReq,
    response=Page[Message],
    impl=unread_list,
    summary="Unread reactions in a chat, and optionally mark them read",
    description=(
        "`--read-all` drives `messages.readReactions` until its offset comes "
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
                "text": "on my way",
            }
        ],
        "has_more": False,
    },
    example_args="reaction unread list @alice",
    covers=("reaction.read-all",),
)


# ---------------------------------------------------------------------------
# reaction catalog
# ---------------------------------------------------------------------------


class CatalogReq(Request):
    top: Annotated[bool, opt("--top", help="Featured / top reactions.")] = False
    recent: Annotated[bool, opt("--recent", help="My recently used reactions.")] = False
    forget: Annotated[bool, opt("--forget", help="Clear the recently-used list.")] = False
    refresh: Annotated[bool, opt("--refresh", help="Ignore the cached hash.")] = False


async def catalog(ctx: OpContext, req: CatalogReq) -> Page[AvailableReaction]:
    """The reaction catalogue: standard, featured, or my recently used.

    Paged locally: all three endpoints answer with the whole list (hash
    cached), so a server-side offset would be a fiction.
    """
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "reaction.catalog", PageKind.LOCAL, default=50)
    handle = client(ctx)

    if req.forget:
        await handle(fn.ClearRecentReactionsRequest())

    if req.recent:
        source = "recent"
        result = await handle(fn.GetRecentReactionsRequest(limit=100, hash=0))
        rows = [
            AvailableReaction(emoticon=name_of(item), source="recent")
            for item in (getattr(result, "reactions", None) or [])
        ]
    elif req.top:
        source = "top"
        result = await handle(fn.GetTopReactionsRequest(limit=100, hash=0))
        rows = [
            AvailableReaction(emoticon=name_of(item), source="top")
            for item in (getattr(result, "reactions", None) or [])
        ]
    else:
        source = "available"
        result = await handle(fn.GetAvailableReactionsRequest(hash=0))
        rows = [
            AvailableReaction(
                emoticon=str(getattr(item, "reaction", "")),
                title=str(getattr(item, "title", "") or ""),
                premium=bool(getattr(item, "premium", False)),
                inactive=bool(getattr(item, "inactive", False)),
                source="available",
                static_icon_id=getattr(getattr(item, "static_icon", None), "id", None),
                select_animation_id=getattr(getattr(item, "select_animation", None), "id", None),
            )
            for item in (getattr(result, "reactions", None) or [])
        ]

    offset = int(state.get("offset") or 0)
    window_rows = rows[offset : offset + limit]
    return build_page(
        window_rows,
        op="reaction.catalog",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window_rows), "source": source},
        account=ctx.account,
        has_more=offset + len(window_rows) < len(rows),
        total=len(rows),
    )


SPEC_CATALOG = OperationSpec(
    id="reaction.catalog",
    request=CatalogReq,
    response=Page[AvailableReaction],
    impl=catalog,
    summary="The reaction catalogue: standard, featured, or my recently used",
    description=(
        "Three endpoints, one row shape; `source` says which list a row came "
        "from. `--forget` clears the recently-used list first."
    ),
    aliases=("react.catalog",),
    paginated=PageKind.LOCAL,
    mutating=False,
    tags=frozenset({"mutating-checked"}),
    columns=("emoticon", "title", "premium", "source"),
    example={
        "items": [{"emoticon": "👍", "title": "Thumbs Up", "source": "available"}],
        "has_more": False,
    },
    example_args="reaction catalog",
    covers=(
        "reaction.available-list",
        "reaction.clear-recent",
        "reaction.recent-list",
        "reaction.top-featured",
    ),
)


# ---------------------------------------------------------------------------
# reaction chat get / set
# ---------------------------------------------------------------------------


def _chat_reactions(full: Any, chat_id: int) -> ChatReactions:
    available = getattr(full, "available_reactions", None)
    name = type(available).__name__
    if name == "ChatReactionsAll":
        mode = "all"
    elif name == "ChatReactionsSome":
        mode = "some"
    else:
        mode = "none"
    return ChatReactions(
        chat_id=chat_id,
        mode=mode,  # type: ignore[arg-type]
        reactions=[name_of(item) for item in (getattr(available, "reactions", None) or [])],
        allow_custom=bool(getattr(available, "allow_custom", False)),
        reactions_limit=getattr(full, "reactions_limit", None),
        paid_enabled=getattr(full, "paid_reactions_available", None),
    )


async def _full_chat(ctx: OpContext, peer: Any) -> Any:
    """`chatFull`/`channelFull` for a peer — the source of truth for the policy."""
    from telethon.tl import types
    from telethon.tl.functions import channels as ch
    from telethon.tl.functions import messages as fn

    if isinstance(peer, types.InputPeerChannel):
        result = await client(ctx)(ch.GetFullChannelRequest(channel=input_channel(peer)))
    elif isinstance(peer, types.InputPeerChat):
        result = await client(ctx)(fn.GetFullChatRequest(chat_id=peer.chat_id))
    else:
        raise UsageError(
            "reactions are configured per group or channel, not per private chat", field="chat"
        )
    return getattr(result, "full_chat", None)


class ChatGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        int | None,
        opt("--msg-id", metavar="ID", kind="msg_id", help="Narrow to one message."),
    ] = None


async def chat_get(ctx: OpContext, req: ChatGetReq) -> ChatReactions:
    """Which reactions this chat — or this one message — allows."""
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    policy = _chat_reactions(await _full_chat(ctx, peer), chat_id)
    if req.msg_id is None:
        return policy

    policy.msg_id = req.msg_id
    message = await client(ctx).get_messages(peer, ids=req.msg_id)
    if message is None:
        raise NotFoundError(f"message {req.msg_id} was not found in {chat_id}")
    if policy.mode == "all":
        # The per-message answer is the chat policy narrowed by the global
        # catalogue: "everything" means everything Telegram currently ships.
        from telethon.tl.functions import messages as fn

        available = await client(ctx)(fn.GetAvailableReactionsRequest(hash=0))
        policy.reactions = [
            str(item.reaction)
            for item in (getattr(available, "reactions", None) or [])
            if not getattr(item, "inactive", False)
        ]
    return policy


SPEC_CHAT_GET = OperationSpec(
    id="reaction.chat.get",
    request=ChatGetReq,
    response=ChatReactions,
    impl=chat_get,
    summary="Which reactions a chat (or one message) allows",
    description=(
        "`chatFull`/`channelFull.available_reactions` is the source of truth. "
        "With `--msg-id` an `all` policy is expanded against the live "
        "catalogue, which is the set that would actually be accepted."
    ),
    aliases=("reaction.available",),
    columns=("chat_id", "mode", "reactions_limit"),
    example={"chat_id": -1001234567890, "mode": "some", "reactions": ["👍", "❤"]},
    example_args="reaction chat get @news",
    covers=("reaction.chat-available-get", "reaction.message-available"),
)


class ChatSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    every: Annotated[bool, opt("--every", help="Allow every reaction.")] = False
    none: Annotated[bool, opt("--none", help="Disable reactions.")] = False
    some: Annotated[
        str | None, opt("--some", metavar="EMOJI,...", help="Allow exactly this set.")
    ] = None
    allow_custom: Annotated[bool, opt("--allow-custom", help="Allow custom-emoji reactions.")] = (
        False
    )
    max_unique: Annotated[
        int | None,
        opt("--max-unique", metavar="N", help="Cap unique reactions per message."),
    ] = None
    paid: Annotated[str | None, choice("on", "off", help="Star (paid) reactions on a channel.")] = (
        None
    )


async def chat_set(ctx: OpContext, req: ChatSetReq) -> ChatReactions:
    """Set a chat's reaction policy, its unique cap and its Star reactions.

    `available_reactions` is mandatory on the wire while the other fields are
    optional, so changing only the cap still means resending the whole
    policy: this is a read-modify-write, never a blind overwrite.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    current = _chat_reactions(await _full_chat(ctx, peer), chat_id)

    chosen = [flag for flag in (req.every, req.none, bool(req.some)) if flag]
    if len(chosen) > 1:
        raise UsageError("--every, --none and --some are three answers to one question")

    if req.every:
        available: Any = types.ChatReactionsAll(allow_custom=req.allow_custom or None)
    elif req.none:
        available = types.ChatReactionsNone()
    elif req.some is not None:
        names = [part.strip() for part in req.some.split(",") if part.strip()]
        if not names:
            raise UsageError("--some needs at least one reaction", field="some")
        available = types.ChatReactionsSome(reactions=[to_tl(name) for name in names])
    elif current.mode == "all":
        available = types.ChatReactionsAll(
            allow_custom=(req.allow_custom or current.allow_custom) or None
        )
    elif current.mode == "some":
        available = types.ChatReactionsSome(reactions=[to_tl(name) for name in current.reactions])
    else:
        available = types.ChatReactionsNone()

    await client(ctx)(
        fn.SetChatAvailableReactionsRequest(
            peer=peer,
            available_reactions=available,
            reactions_limit=req.max_unique,
            paid_enabled=None if req.paid is None else req.paid == "on",
        )
    )
    updated = _chat_reactions(await _full_chat(ctx, peer), chat_id)
    ctx.emit("chat_reactions_set", {"chat_id": chat_id, "mode": updated.mode})
    return updated


SPEC_CHAT_SET = OperationSpec(
    id="reaction.chat.set",
    request=ChatSetReq,
    response=ChatReactions,
    impl=chat_set,
    summary="Restrict which reactions a chat allows, cap them, enable Star reactions",
    description=(
        "Needs the `change_info` admin right. Because the wire field is "
        "mandatory, tlgr reads the current policy first and resends it "
        "unchanged when only the cap or the Star switch was asked for."
    ),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "mode", "reactions_limit", "paid_enabled"),
    example={"chat_id": -1001234567890, "mode": "some", "reactions": ["👍"], "reactions_limit": 3},
    example_args="reaction chat set @news --some 👍,❤",
    covers=("reaction.chat-available-set", "reaction.chat-unique-limit", "reaction.paid-enable"),
)


# ---------------------------------------------------------------------------
# reaction default get / set
# ---------------------------------------------------------------------------


class DefaultGetReq(Request):
    pass


async def default_get(ctx: OpContext, req: DefaultGetReq) -> ReactionTag:
    """The quick (double-tap) reaction.

    There is no getter: the value ships in `help.getConfig().reactions_default`,
    which is why it is read here rather than remembered from the last write.
    """
    from telethon.tl.functions import help as fn

    config = await client(ctx)(fn.GetConfigRequest())
    reaction = getattr(config, "reactions_default", None)
    if reaction is None:
        return ReactionTag(reaction="")
    return ReactionTag(reaction=name_of(reaction))


SPEC_DEFAULT_GET = OperationSpec(
    id="reaction.default.get",
    request=DefaultGetReq,
    response=ReactionTag,
    impl=default_get,
    summary="Show the quick (double-tap) reaction",
    description="Read from `help.getConfig().reactions_default`; there is no dedicated getter.",
    columns=("reaction",),
    example={"reaction": "❤"},
    example_args="reaction default get",
    covers=(),
    covers_partial=(),
    tags=frozenset({"infrastructure"}),
)


class DefaultSetReq(Request):
    emoji: Annotated[
        str, arg(0, metavar="EMOJI", required=False, help="The reaction to make default.")
    ] = ""
    custom: Annotated[
        int | None, opt("--custom", metavar="ID", help="Use a custom emoji (Premium).")
    ] = None


async def default_set(ctx: OpContext, req: DefaultSetReq) -> ReactionTag:
    """Set the quick (double-tap) reaction."""
    from telethon.tl.functions import messages as fn

    name = f"{CUSTOM}{req.custom}" if req.custom is not None else req.emoji
    if not name:
        raise UsageError("a reaction is required", field="emoji")
    await client(ctx)(fn.SetDefaultReactionRequest(reaction=to_tl(name)))
    return ReactionTag(reaction=name)


SPEC_DEFAULT_SET = OperationSpec(
    id="reaction.default.set",
    request=DefaultSetReq,
    response=ReactionTag,
    impl=default_set,
    summary="Set the quick (double-tap) reaction",
    mutating=True,
    idempotent=True,
    columns=("reaction",),
    example={"reaction": "❤"},
    example_args="reaction default set ❤",
    covers=("reaction.quick-default",),
)


# ---------------------------------------------------------------------------
# reaction tag list / set
# ---------------------------------------------------------------------------


class TagListReq(Request):
    peer: Annotated[
        PeerRef | None,
        opt("--peer", metavar="CHAT", kind="peer", help="Tags used inside one saved dialog."),
    ] = None
    suggested: Annotated[bool, opt("--suggested", help="Default/suggested tag reactions.")] = False
    refresh: Annotated[bool, opt("--refresh", help="Ignore the cached hash.")] = False
    rename: Annotated[
        str | None, opt("--rename", metavar="REACTION=TITLE", help="Name or rename a tag.")
    ] = None
    clear_title: Annotated[
        str | None, opt("--clear-title", metavar="REACTION", help="Drop a tag's name.")
    ] = None


async def tag_list(ctx: OpContext, req: TagListReq) -> Page[ReactionTag]:
    """Saved Messages reaction tags — mine, or the suggested ones.

    Tagging a saved message *is* reacting to it (`reaction add me <id> 📌`);
    this command only names the tags and reads them back.
    """
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    if req.rename is not None or req.clear_title is not None:
        await _rename_tag(ctx, req)

    if req.suggested:
        result = await handle(fn.GetDefaultTagReactionsRequest(hash=0))
        items = [
            ReactionTag(reaction=name_of(item), suggested=True)
            for item in (getattr(result, "reactions", None) or [])
        ]
        return Page(items=items, has_more=False, total=len(items))

    peer = await _send.resolve(ctx, req.peer) if req.peer is not None else None
    result = await handle(fn.GetSavedReactionTagsRequest(hash=0, peer=peer))
    items = [
        ReactionTag(
            reaction=name_of(getattr(tag, "reaction", None)),
            title=getattr(tag, "title", None),
            count=int(getattr(tag, "count", 0) or 0),
        )
        for tag in (getattr(result, "tags", None) or [])
    ]
    return Page(items=items, has_more=False, total=len(items))


async def _rename_tag(ctx: OpContext, req: TagListReq) -> None:
    from telethon.tl.functions import messages as fn

    if req.clear_title is not None:
        await client(ctx)(
            fn.UpdateSavedReactionTagRequest(reaction=to_tl(req.clear_title), title=None)
        )
        return
    reaction, sep, title = (req.rename or "").partition("=")
    if not sep:
        raise UsageError("--rename wants REACTION=TITLE", field="rename")
    await client(ctx)(
        fn.UpdateSavedReactionTagRequest(reaction=to_tl(reaction), title=title or None)
    )


SPEC_TAG_LIST = OperationSpec(
    id="reaction.tag.list",
    request=TagListReq,
    response=Page[ReactionTag],
    impl=tag_list,
    summary="Saved Messages reaction tags (and the suggested ones)",
    description=(
        "Premium. Tagging a saved message is reacting to it — "
        "`tlgr reaction add me <id> 📌` — and this is where the tags are named."
    ),
    mutating=True,
    columns=("reaction", "title", "count"),
    example={"items": [{"reaction": "📌", "title": "invoices", "count": 12}], "has_more": False},
    example_args="reaction tag list",
    covers=(
        "dialogs.saved-tags",
        "messages-core.saved-tags-manage",
        "reaction.default-tag-reactions",
        "reaction.saved-tags-list",
    ),
)


class TagSetReq(Request):
    emoji: Annotated[str, arg(0, metavar="EMOJI", help="The tag reaction.")]
    title: Annotated[str, arg(1, metavar="TITLE", required=False, help="The name to give it.")] = ""
    custom: Annotated[int | None, opt("--custom", metavar="ID", help="A custom-emoji tag.")] = None
    clear: Annotated[bool, opt("--clear", help="Remove the name.")] = False


async def tag_set(ctx: OpContext, req: TagSetReq) -> ReactionTag:
    """Name or rename a Saved Messages tag. Names are local to my account."""
    from telethon.tl.functions import messages as fn

    name = f"{CUSTOM}{req.custom}" if req.custom is not None else req.emoji
    title = "" if req.clear else req.title
    await client(ctx)(fn.UpdateSavedReactionTagRequest(reaction=to_tl(name), title=title or None))
    return ReactionTag(reaction=name, title=title or None)


SPEC_TAG_SET = OperationSpec(
    id="reaction.tag.set",
    request=TagSetReq,
    response=ReactionTag,
    impl=tag_set,
    summary="Name or rename a Saved Messages tag",
    description="Premium. Omitting the title, or passing `--clear`, removes the name.",
    mutating=True,
    idempotent=True,
    columns=("reaction", "title"),
    example={"reaction": "📌", "title": "invoices"},
    example_args="reaction tag set 📌 invoices",
    covers=("reaction.saved-tag-rename",),
)


# ---------------------------------------------------------------------------
# reaction pay / privacy
# ---------------------------------------------------------------------------


def _privacy(mode: str, peer: Any) -> Any:
    from telethon.tl import types

    if mode == "anonymous":
        return types.PaidReactionPrivacyAnonymous()
    if mode == "peer":
        if peer is None:
            raise UsageError("privacy 'peer' needs --send-as to say which channel", field="send-as")
        return types.PaidReactionPrivacyPeer(peer=peer)
    return types.PaidReactionPrivacyDefault()


def _privacy_name(value: Any) -> str:
    name = type(value).__name__
    if name.endswith("Anonymous"):
        return "anonymous"
    if name.endswith("Peer"):
        return "peer"
    return "default"


class PayReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Post id.")]
    stars: Annotated[int | None, opt("--stars", metavar="N", help="How many Stars to send.")] = None
    anonymous: Annotated[bool, opt("--anonymous", help="Hide my name on the leaderboard.")] = False
    send_as: Annotated[
        PeerRef | None, opt("--send-as", metavar="PEER", kind="peer", help="Pay as my channel.")
    ] = None
    senders: Annotated[
        bool, opt("--senders", help="List the peers I may pay as, instead of paying.")
    ] = False


async def pay(ctx: OpContext, req: PayReq) -> PaidReactionResult:
    """Send a Star (paid) reaction, or list the identities I could pay as.

    This spends the account's Star balance, so there is no default amount and
    no automatic retry: `--stars` is mandatory and the confirmation is the
    ordinary destructive-command one. `--senders` is free and never pays.

    `random_id` is not the usual 64 random bits here — the API wants
    `(unixtime << 32) | random_uint32`, and a plain random value is rejected.
    """
    import os
    import time

    from telethon.tl.functions import channels as ch
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    # Resolved before anything is spent: a paid reaction only exists on a
    # channel post, and finding that out after the payment is not an option.
    input_channel(peer)

    if req.senders:
        found = await client(ctx)(ch.GetSendAsRequest(peer=peer, for_paid_reactions=True))
        return PaidReactionResult(
            chat_id=chat_id,
            msg_id=req.msg_id,
            senders=[
                pid
                for pid in (
                    peer_id_of(getattr(item, "peer", item))
                    for item in (getattr(found, "peers", None) or [])
                )
                if pid is not None
            ],
        )

    if not req.stars or req.stars < 1:
        raise UsageError(
            "--stars N is required: a paid reaction spends real Stars and tlgr never "
            "picks the amount",
            field="stars",
        )
    send_as = await _send.resolve(ctx, req.send_as) if req.send_as is not None else None
    mode = "peer" if send_as is not None else ("anonymous" if req.anonymous else "default")

    updates = await client(ctx)(
        fn.SendPaidReactionRequest(
            peer=peer,
            msg_id=req.msg_id,
            count=int(req.stars),
            random_id=(int(time.time()) << 32) | int.from_bytes(os.urandom(4), "big"),
            private=_privacy(mode, send_as),
        )
    )
    top: list[TopReactor] = []
    for update in getattr(updates, "updates", None) or []:
        if type(update).__name__ == "UpdateMessageReactions":
            top = _reactors(getattr(update, "reactions", None))
    ctx.emit("reaction_paid", {"chat_id": chat_id, "msg_id": req.msg_id, "stars": req.stars})
    return PaidReactionResult(
        chat_id=chat_id,
        msg_id=req.msg_id,
        stars_sent=int(req.stars),
        privacy=mode,  # type: ignore[arg-type]
        top_reactors=top,
    )


SPEC_PAY = OperationSpec(
    id="reaction.pay",
    request=PayReq,
    response=PaidReactionResult,
    impl=pay,
    summary="Send a Star (paid) reaction to a channel post",
    description=(
        "Spends real Stars. `--stars` is mandatory, there is no default "
        "amount, and a failed payment is never retried automatically. "
        "`--senders` lists the identities you could pay as without paying."
    ),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "stars_sent"),
    example={"chat_id": -1001234567890, "msg_id": 12345, "stars_sent": 50},
    example_args="reaction pay @news 12345 --stars 50",
    tags=frozenset({"visible-to-others"}),
    covers=(
        "messages-core.reaction-paid-star",
        "reaction.live-story-paid",
        "reaction.paid-send",
        "reaction.paid-send-as",
    ),
    coverage_note=(
        "`reaction.live-story-paid` is the same Star spend inside a live "
        "story; the live-story stream itself is control-only for a CLI and "
        "belongs to the calls group."
    ),
)


class PrivacyGetReq(Request):
    pass


async def privacy_get(ctx: OpContext, req: PrivacyGetReq) -> ReactionPrivacy:
    """How my paid reactions are attributed by default.

    Worth calling on startup: `updatePaidReactionPrivacy` only reaches online
    sessions and is not replayed by `getDifference`.
    """
    from telethon.tl.functions import messages as fn

    value = await client(ctx)(fn.GetPaidReactionPrivacyRequest())
    inner = getattr(value, "private", None)
    for update in getattr(value, "updates", None) or []:
        # The answer is an `updatePaidReactionPrivacy` inside an Updates batch;
        # reading the envelope as the value itself always says "default".
        if getattr(update, "private", None) is not None:
            inner = update.private
    return ReactionPrivacy(
        privacy=_privacy_name(inner),  # type: ignore[arg-type]
        peer_id=peer_id_of(getattr(inner, "peer", None)),
    )


SPEC_PRIVACY_GET = OperationSpec(
    id="reaction.privacy.get",
    request=PrivacyGetReq,
    response=ReactionPrivacy,
    impl=privacy_get,
    summary="Default privacy of my paid reactions",
    columns=("privacy",),
    example={"privacy": "default"},
    example_args="reaction privacy get",
    covers=("reaction.paid-privacy-get",),
)


class PrivacySetReq(Request):
    mode: Annotated[str, arg(0, metavar="MODE", help="default | anonymous | peer.")]
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="The post's chat.")
    ] = None
    msg_id: Annotated[
        int | None, opt("--msg-id", metavar="ID", kind="msg_id", help="The post itself.")
    ] = None
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Attribute them to this channel."),
    ] = None


async def privacy_set(ctx: OpContext, req: PrivacySetReq) -> ReactionPrivacy:
    """Change how paid reactions are attributed, including ones already sent.

    This rewrites the attribution of Stars already spent on that post, and
    changes the account-wide default as a side effect — which is why it takes
    the post rather than being a settings-only toggle.
    """
    from telethon.tl.functions import messages as fn

    if req.mode not in ("default", "anonymous", "peer"):
        raise UsageError("mode is one of default, anonymous, peer", field="mode")
    if req.chat is None or req.msg_id is None:
        raise UsageError(
            "--chat and --msg-id name the post whose attribution changes", field="msg-id"
        )
    peer = await _send.resolve(ctx, req.chat)
    send_as = await _send.resolve(ctx, req.send_as) if req.send_as is not None else None
    await client(ctx)(
        fn.TogglePaidReactionPrivacyRequest(
            peer=peer, msg_id=req.msg_id, private=_privacy(req.mode, send_as)
        )
    )
    return ReactionPrivacy(
        privacy=req.mode,  # type: ignore[arg-type]
        peer_id=peer_id_of(send_as),
        msg_id=req.msg_id,
    )


SPEC_PRIVACY_SET = OperationSpec(
    id="reaction.privacy.set",
    request=PrivacySetReq,
    response=ReactionPrivacy,
    impl=privacy_set,
    summary="Change the privacy of paid reactions, including ones already sent",
    description="Also changes the account-wide default, which is what the server does.",
    mutating=True,
    idempotent=True,
    columns=("privacy", "msg_id"),
    example={"privacy": "anonymous", "msg_id": 12345},
    example_args="reaction privacy set anonymous --chat @news --msg-id 12345",
    covers=("reaction.paid-privacy-set",),
)


# ---------------------------------------------------------------------------
# reaction purge / report
# ---------------------------------------------------------------------------


class PurgeReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Whose reactions.")]
    msg: Annotated[
        int | None, opt("--msg", metavar="ID", kind="msg_id", help="Only this message.")
    ] = None
    every: Annotated[bool, opt("--every", help="Every reaction this member left in the chat.")] = (
        False
    )


async def purge(ctx: OpContext, req: PurgeReq) -> ReactionPurge:
    """Delete a member's reactions — on one message, or across the chat.

    Moderation: needs the delete-messages / ban rights. Both the singular and
    the plural method exist on the wire; which one runs is the difference
    between `--msg` and `--every`, and asking for neither is a usage error
    rather than a guess.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    participant = await _send.resolve(ctx, req.user)
    user_id = _send.peer_id_of(participant)

    if req.every:
        await client(ctx)(fn.DeleteParticipantReactionsRequest(peer=peer, participant=participant))
        scope = "chat"
    elif req.msg is not None:
        await client(ctx)(
            fn.DeleteParticipantReactionRequest(peer=peer, msg_id=req.msg, participant=participant)
        )
        scope = "message"
    else:
        raise UsageError("name a message with --msg, or pass --every", field="msg")

    ctx.emit("reaction_purged", {"chat_id": chat_id, "user_id": user_id, "scope": scope})
    return ReactionPurge(
        chat_id=chat_id,
        user_id=user_id,
        msg_id=req.msg,
        deleted=True,
        scope=scope,  # type: ignore[arg-type]
    )


SPEC_PURGE = OperationSpec(
    id="reaction.purge",
    request=PurgeReq,
    response=ReactionPurge,
    impl=purge,
    summary="Delete a member's reactions on one message or across a chat",
    description="Moderation: needs `delete_messages` or ban rights in the chat.",
    aliases=("react.purge",),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("chat_id", "user_id", "scope", "deleted"),
    example={"chat_id": -1001234567890, "user_id": 4242, "deleted": True, "scope": "message"},
    example_args="reaction purge @news @alice --msg 12345",
    covers=(
        "messages-core.reaction-delete-from-sender",
        "reaction.purge-participant-message",
    ),
)


class ReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="The post.")]
    user: Annotated[PeerRef, arg(2, metavar="USER", kind="user", help="Who reacted.")]
    ban: Annotated[bool, opt("--ban", help="Also block them from replying.")] = False


async def report(ctx: OpContext, req: ReportReq) -> ReactionReport:
    """Report a reaction on my own post, optionally blocking the member.

    The ban half is `contacts.blockFromReplies`, which is exactly what the GUI
    does from this menu; the full moderation surface belongs to the group
    admin commands.
    """
    from telethon.tl.functions import contacts as ct
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    reaction_peer = await _send.resolve(ctx, req.user)
    await client(ctx)(
        fn.ReportReactionRequest(peer=peer, id=req.msg_id, reaction_peer=reaction_peer)
    )
    banned = False
    if req.ban:
        await client(ctx)(ct.BlockFromRepliesRequest(msg_id=req.msg_id, delete_message=True))
        banned = True
    return ReactionReport(
        ok=True,
        banned=banned,
        chat_id=chat_id,
        msg_id=req.msg_id,
        user_id=_send.peer_id_of(reaction_peer),
    )


SPEC_REPORT = OperationSpec(
    id="reaction.report",
    request=ReportReq,
    response=ReactionReport,
    impl=report,
    summary="Report a reaction, optionally blocking the member who left it",
    description="Only valid on your own posts. Reporting cannot be undone from a client.",
    mutating=True,
    rate_class="send",
    columns=("ok", "banned"),
    example={"ok": True, "banned": False, "chat_id": -1001234567890, "msg_id": 12345},
    example_args="reaction report @news 12345 @alice",
    covers=("groups-channels-admin.report-reaction", "reaction.ban-sender", "reaction.report"),
)
