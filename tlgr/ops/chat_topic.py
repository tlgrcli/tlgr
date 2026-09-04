"""`chat topic *`: forum topics, which are message threads with a UI.

A topic is not a chat. Its id is the id of the `messageActionTopicCreate`
service message that started it, which is also what every `--topic` flag on
`message send/list` takes — so the id this module returns is directly usable
in the messages group and nothing has to be translated.

Three server rules shape the module and are worth stating once.

* **General is id 1.** It always exists, cannot be deleted, and is the one
  topic whose `top_msg_id` must be *omitted* rather than sent as 1. It is
  also the only topic that may be hidden.
* **Deleting a topic is a history drain.** `messages.deleteTopicHistory`
  answers with `affectedHistory` and an offset to resume from, and the server
  emits no dedicated update — other clients learn about it from the deleted
  root message.
* **Muting a topic is a notification exception, not a topic property.** It
  goes through `account.updateNotifySettings` with an
  `inputNotifyForumTopic`, and lives in your account rather than in the chat.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import random
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_duration, to_unix
from tlgr.models.admin import Topic, TopicPinResult, TopicReadResult, TopicResult
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _admin, _send
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import message_to_model
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_TOPIC: dict[str, Any] = {
    "id": 314,
    "title": "Releases",
    "closed": False,
    "pinned": True,
    "unread_count": 2,
    "top_message": 918,
}

#: Telegram's own sentinel for "muted forever".
MUTE_FOREVER = 2**31 - 1


def _topic_model(raw: Any, *, chat_id: int) -> Topic:
    """`forumTopic` (or `forumTopicDeleted`) → `Topic`."""
    if type(raw).__name__ == "ForumTopicDeleted":
        return Topic(id=int(getattr(raw, "id", 0) or 0), chat_id=chat_id, deleted=True)
    notify = getattr(raw, "notify_settings", None)
    mute_until = getattr(notify, "mute_until", None)
    from_id = getattr(raw, "from_id", None)
    return Topic(
        id=int(getattr(raw, "id", 0) or 0),
        chat_id=chat_id,
        title=str(getattr(raw, "title", "") or ""),
        icon_emoji_id=getattr(raw, "icon_emoji_id", None),
        icon_color=getattr(raw, "icon_color", None),
        closed=bool(getattr(raw, "closed", False)),
        pinned=bool(getattr(raw, "pinned", False)),
        hidden=bool(getattr(raw, "hidden", False)),
        my=bool(getattr(raw, "my", False)),
        top_message=int(getattr(raw, "top_message", 0) or 0) or None,
        unread_count=int(getattr(raw, "unread_count", 0) or 0),
        unread_mentions_count=int(getattr(raw, "unread_mentions_count", 0) or 0),
        unread_reactions_count=int(getattr(raw, "unread_reactions_count", 0) or 0),
        from_id=abs(_send.peer_id_of(from_id)) if from_id is not None else None,
        muted=bool(mute_until) if mute_until is not None else None,
        date=fmt_dt(getattr(raw, "date", None)),
        date_unix=to_unix(getattr(raw, "date", None)),
    )


async def _forum_peer(ctx: OpContext, ref: PeerRef) -> Any:
    """The peer, refusing a chat that is not a forum with a readable reason."""
    peer = await _send.resolve(ctx, ref)
    if not _admin.is_channel(peer):
        raise UsageError(
            "topics only exist in forum supergroups; turn them on with "
            "`tlgr chat setting set <chat> --forum on`",
            field="chat",
        )
    return peer


def _emoji_id(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "off", "none", "0"):
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise UsageError(
            "--icon-emoji takes a custom-emoji document id", field="icon_emoji"
        ) from exc


async def _topic_link(ctx: OpContext, peer: Any, topic_id: int) -> str | None:
    """`t.me/<username>/<topic_id>` for a public forum, else None."""
    from telethon.tl.functions import channels as fn

    try:
        reply = await _admin.client(ctx)(
            fn.ExportMessageLinkRequest(
                channel=_admin.input_channel(peer), id=topic_id, thread=True
            )
        )
    except Exception:
        return None
    return str(getattr(reply, "link", "") or "") or None


# ---------------------------------------------------------------------------
# chat topic list / get
# ---------------------------------------------------------------------------


class TopicListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    search: Annotated[str, opt("--search", "-s", metavar="TEXT", help="Title query.")] = ""
    closed: Annotated[bool, opt("--closed", help="Only closed topics.")] = False
    hidden: Annotated[bool, opt("--hidden", help="Only hidden topics.")] = False
    pinned: Annotated[bool, opt("--pinned", help="Only pinned topics.")] = False


async def list_topics(ctx: OpContext, req: TopicListReq) -> Page[Topic]:
    """A forum's topics, newest activity first. General (id 1) is always there."""
    from datetime import datetime, timezone

    from telethon.tl.functions import messages as fn

    limit, state = _admin.window(ctx, "chat.topic.list", PageKind.PARTICIPANTS)
    peer = await _forum_peer(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    offset_date = state.get("date")
    reply = await _admin.client(ctx)(
        fn.GetForumTopicsRequest(
            peer=peer,
            q=req.search or None,
            offset_date=datetime.fromtimestamp(offset_date, tz=timezone.utc)
            if offset_date
            else None,
            offset_id=int(state.get("id", 0) or 0),
            offset_topic=int(state.get("topic", 0) or 0),
            limit=limit,
        )
    )
    rows = [_topic_model(row, chat_id=chat_id) for row in (getattr(reply, "topics", None) or [])]
    # The three filters are client-side over the page: the API has no flag
    # for any of them. The cursor is built from the last row the *server*
    # sent, not the last row that survived the filter — otherwise a page
    # whose tail was filtered out would resume in the wrong place.
    items = list(rows)
    if req.closed:
        items = [t for t in items if t.closed]
    if req.hidden:
        items = [t for t in items if t.hidden]
    if req.pinned:
        items = [t for t in items if t.pinned]
    next_state: dict[str, Any] = {}
    if rows:
        last = rows[-1]
        next_state = {
            "date": last.date_unix or 0,
            "id": last.top_message or 0,
            "topic": last.id,
        }
    return build_page(
        items,
        op="chat.topic.list",
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        has_more=len(rows) >= limit,
        total=int(getattr(reply, "count", 0) or 0),
    )


SPEC_TOPIC_LIST = OperationSpec(
    id="chat.topic.list",
    request=TopicListReq,
    response=Page[Topic],
    impl=list_topics,
    summary="List or search a forum's topics",
    description=(
        "The cursor packs the `(offset_date, offset_id, offset_topic)` "
        "triple of the last row. `--closed`, `--hidden` and `--pinned` are "
        "client-side filters over the page, because the API offers no flag "
        "for any of them."
    ),
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "title", "unread_count", "closed", "pinned"),
    headers=("ID", "Title", "Unread", "Closed", "Pinned"),
    example={"items": [_EXAMPLE_TOPIC], "has_more": False, "total": 1},
    example_args="chat topic list @myforum",
    covers=("groups-channels-admin.topic-list",),
    covers_partial=("groups-channels-admin.topic-unread-counters",),
    coverage_note="The counters are on every row; `chat topic read` clears them and owns the id.",
)


class TopicGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[
        list[int], arg(1, metavar="TOPIC", kind="msg_id", variadic=True, help="Topic ids.")
    ] = []


async def get_topics(ctx: OpContext, req: TopicGetReq) -> list[Topic]:
    """One or more topics by id, with their public link when there is one.

    A `forumTopicDeleted` row comes back as `{id, deleted: true}` — that is
    the only signal the API gives that a topic was removed, so dropping it
    would turn "deleted" into "never existed".
    """
    from telethon.tl.functions import messages as fn

    if not req.topic:
        raise UsageError("name at least one topic id", field="topic")
    peer = await _forum_peer(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    reply = await _admin.client(ctx)(
        fn.GetForumTopicsByIDRequest(peer=peer, topics=[int(t) for t in req.topic])
    )
    items = [_topic_model(row, chat_id=chat_id) for row in (getattr(reply, "topics", None) or [])]
    if not items:
        raise NotFoundError("no such topic in this forum")
    for item in items:
        if not item.deleted:
            item.link = await _topic_link(ctx, peer, item.id)
    return items


SPEC_TOPIC_GET = OperationSpec(
    id="chat.topic.get",
    request=TopicGetReq,
    response=list[Topic],
    impl=get_topics,
    summary="Get one or more topics by id",
    description=(
        "`forumTopicDeleted` rows are reported as `{id, deleted: true}`, "
        "which is the only signal the API gives that a topic was removed."
    ),
    columns=("id", "title", "closed", "link"),
    example=[_EXAMPLE_TOPIC],
    example_args="chat topic get @myforum 314",
    empty_exit=EXIT_EMPTY,
    covers=("groups-channels-admin.topic-get", "groups-channels-admin.topic-link"),
)


# ---------------------------------------------------------------------------
# chat topic create / edit / close / reopen / hide / unhide / delete
# ---------------------------------------------------------------------------


class TopicCreateReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    title: Annotated[str, arg(1, metavar="TITLE", help="Topic title.")]
    icon_emoji: Annotated[
        str | None, opt("--icon-emoji", metavar="ID", help="Custom-emoji icon (document id).")
    ] = None
    icon_color: Annotated[
        int | None, opt("--icon-color", metavar="RGB", help="Icon colour; immutable afterwards.")
    ] = None
    send_as: Annotated[
        PeerRef | None,
        opt(
            "--send-as", metavar="PEER", kind="peer", help="Post the creation notice as this peer."
        ),
    ] = None


async def create_topic(ctx: OpContext, req: TopicCreateReq) -> TopicResult:
    """Create a topic. The id you get back is the one `--topic` takes."""
    from telethon.tl.functions import messages as fn

    peer = await _forum_peer(ctx, req.chat)
    send_as = await _send.resolve(ctx, req.send_as) if req.send_as is not None else None
    updates = await _admin.client(ctx)(
        fn.CreateForumTopicRequest(
            peer=peer,
            title=req.title,
            icon_color=req.icon_color,
            icon_emoji_id=_emoji_id(req.icon_emoji) or None,
            random_id=random.getrandbits(63),
            send_as=send_as,
        )
    )
    topic_id = 0
    for update in getattr(updates, "updates", None) or []:
        message = getattr(update, "message", None)
        if message is not None and getattr(message, "id", None):
            topic_id = int(message.id)
            break
    chat_id = _send.peer_id_of(peer)
    ctx.emit("chat_topic_created", {"chat_id": chat_id, "topic_id": topic_id})
    return TopicResult(chat_id=chat_id, topic_id=topic_id, title=req.title)


SPEC_TOPIC_CREATE = OperationSpec(
    id="chat.topic.create",
    request=TopicCreateReq,
    response=TopicResult,
    impl=create_topic,
    summary="Create a topic",
    description=(
        "The returned id is the id of the `messageActionTopicCreate` service "
        "message, which is exactly what every `--topic` flag takes. "
        "Non-Premium accounts may only use icons from "
        "`inputStickerSetEmojiDefaultTopicIcons`."
    ),
    mutating=True,
    columns=("chat_id", "topic_id", "title"),
    example={"chat_id": -1001500, "topic_id": 314, "title": "Releases"},
    example_args="chat topic create @myforum Releases",
    covers=("groups-channels-admin.topic-create",),
    tags=frozenset({"visible-to-others"}),
)


class TopicEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[int, arg(1, metavar="TOPIC", kind="msg_id", help="Topic id.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New title.")] = None
    icon_emoji: Annotated[
        str | None, opt("--icon-emoji", metavar="ID", help="New custom-emoji icon.")
    ] = None
    no_icon: Annotated[bool, opt("--no-icon", help="Drop the custom emoji icon.")] = False
    closed: Annotated[bool | None, opt("--closed", help="Close or reopen in the same call.")] = None
    hidden: Annotated[bool | None, opt("--hidden", help="Hide or show (General only).")] = None


async def _edit_topic(
    ctx: OpContext,
    peer: Any,
    topic_id: int,
    *,
    title: str | None = None,
    icon_emoji_id: int | None = None,
    closed: bool | None = None,
    hidden: bool | None = None,
) -> TopicResult:
    from telethon.tl.functions import messages as fn

    await _admin.client(ctx)(
        fn.EditForumTopicRequest(
            peer=peer,
            topic_id=topic_id,
            title=title,
            icon_emoji_id=icon_emoji_id,
            closed=closed,
            hidden=hidden,
        )
    )
    changed = [
        name
        for name, value in (
            ("title", title),
            ("icon_emoji", icon_emoji_id),
            ("closed", closed),
            ("hidden", hidden),
        )
        if value is not None
    ]
    return TopicResult(
        chat_id=_send.peer_id_of(peer),
        topic_id=topic_id,
        title=title,
        icon_emoji_id=icon_emoji_id,
        closed=closed,
        hidden=hidden,
        changed=changed,
    )


async def edit_topic(ctx: OpContext, req: TopicEditReq) -> TopicResult:
    """Rename a topic, re-icon it, or close/hide it in the same call."""
    peer = await _forum_peer(ctx, req.chat)
    if (
        req.title is None
        and req.icon_emoji is None
        and not req.no_icon
        and req.closed is None
        and req.hidden is None
    ):
        raise UsageError("nothing to change", field="title")
    if req.topic == _admin.GENERAL_TOPIC and (req.icon_emoji or req.no_icon):
        raise UsageError("the General topic has no icon of its own", field="icon_emoji")
    icon = 0 if req.no_icon else _emoji_id(req.icon_emoji)
    return await _edit_topic(
        ctx,
        peer,
        req.topic,
        title=req.title,
        icon_emoji_id=icon,
        closed=req.closed,
        hidden=req.hidden,
    )


SPEC_TOPIC_EDIT = OperationSpec(
    id="chat.topic.edit",
    request=TopicEditReq,
    response=TopicResult,
    impl=edit_topic,
    summary="Rename a topic or change its icon",
    description=(
        "`icon_color` cannot be changed after creation — the API has no "
        "field for it — and the General topic accepts only `--title` and "
        "`--hidden`."
    ),
    mutating=True,
    columns=("chat_id", "topic_id", "title"),
    example={"chat_id": -1001500, "topic_id": 314, "title": "Releases"},
    example_args="chat topic edit @myforum 314 --title 'Release notes'",
    covers=("groups-channels-admin.topic-edit",),
    covers_partial=(
        "groups-channels-admin.topic-close-reopen",
        "groups-channels-admin.topic-hide-general",
    ),
    coverage_note=(
        "`--closed`/`--hidden` do it in one call; `chat topic reopen` and "
        "`chat topic unhide` own the ids."
    ),
)


class TopicOneReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[int, arg(1, metavar="TOPIC", kind="msg_id", help="Topic id.")]


async def close_topic(ctx: OpContext, req: TopicOneReq) -> TopicResult:
    """Close a topic: only admins may post in it afterwards."""
    peer = await _forum_peer(ctx, req.chat)
    return await _edit_topic(ctx, peer, req.topic, closed=True)


SPEC_TOPIC_CLOSE = OperationSpec(
    id="chat.topic.close",
    request=TopicOneReq,
    response=TopicResult,
    impl=close_topic,
    summary="Close a topic",
    mutating=True,
    columns=("chat_id", "topic_id", "closed"),
    example={"chat_id": -1001500, "topic_id": 314, "closed": True},
    example_args="chat topic close @myforum 314",
    covers_partial=("groups-channels-admin.topic-close-reopen",),
    coverage_note="The closing half; `chat topic reopen` owns the id.",
)


async def reopen_topic(ctx: OpContext, req: TopicOneReq) -> TopicResult:
    """Reopen a closed topic."""
    peer = await _forum_peer(ctx, req.chat)
    return await _edit_topic(ctx, peer, req.topic, closed=False)


SPEC_TOPIC_REOPEN = OperationSpec(
    id="chat.topic.reopen",
    request=TopicOneReq,
    response=TopicResult,
    impl=reopen_topic,
    summary="Reopen a closed topic",
    mutating=True,
    columns=("chat_id", "topic_id", "closed"),
    example={"chat_id": -1001500, "topic_id": 314, "closed": False},
    example_args="chat topic reopen @myforum 314",
    covers=("groups-channels-admin.topic-close-reopen",),
)


class TopicGeneralReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]


async def hide_topic(ctx: OpContext, req: TopicGeneralReq) -> TopicResult:
    """Hide the General topic. The server refuses any other id."""
    peer = await _forum_peer(ctx, req.chat)
    return await _edit_topic(ctx, peer, _admin.GENERAL_TOPIC, hidden=True)


SPEC_TOPIC_HIDE = OperationSpec(
    id="chat.topic.hide",
    request=TopicGeneralReq,
    response=TopicResult,
    impl=hide_topic,
    summary="Hide the General topic",
    description="Only General (id 1) may be hidden; the server refuses any other id.",
    mutating=True,
    columns=("chat_id", "topic_id", "hidden"),
    example={"chat_id": -1001500, "topic_id": 1, "hidden": True},
    example_args="chat topic hide @myforum",
    covers_partial=("groups-channels-admin.topic-hide-general",),
    coverage_note="The hiding half; `chat topic unhide` owns the id.",
)


async def unhide_topic(ctx: OpContext, req: TopicGeneralReq) -> TopicResult:
    """Show the General topic again."""
    peer = await _forum_peer(ctx, req.chat)
    return await _edit_topic(ctx, peer, _admin.GENERAL_TOPIC, hidden=False)


SPEC_TOPIC_UNHIDE = OperationSpec(
    id="chat.topic.unhide",
    request=TopicGeneralReq,
    response=TopicResult,
    impl=unhide_topic,
    summary="Show the General topic again",
    mutating=True,
    columns=("chat_id", "topic_id", "hidden"),
    example={"chat_id": -1001500, "topic_id": 1, "hidden": False},
    example_args="chat topic unhide @myforum",
    covers=("groups-channels-admin.topic-hide-general",),
)


async def delete_topic(ctx: OpContext, req: TopicOneReq) -> TopicResult:
    """Delete a topic and every message in it, draining the offset loop."""
    from telethon.tl.functions import messages as fn

    peer = await _forum_peer(ctx, req.chat)
    if req.topic == _admin.GENERAL_TOPIC:
        raise UsageError(
            "the General topic cannot be deleted; `chat topic hide` removes it from view",
            field="topic",
        )
    deleted = await _admin.affected_loop(
        ctx, lambda _offset: fn.DeleteTopicHistoryRequest(peer=peer, top_msg_id=req.topic)
    )
    chat_id = _send.peer_id_of(peer)
    ctx.emit("chat_topic_deleted", {"chat_id": chat_id, "topic_id": req.topic})
    return TopicResult(
        chat_id=chat_id, topic_id=req.topic, deleted=True, changed=[f"messages:{deleted}"]
    )


SPEC_TOPIC_DELETE = OperationSpec(
    id="chat.topic.delete",
    request=TopicOneReq,
    response=TopicResult,
    impl=delete_topic,
    summary="Delete a topic and all its messages",
    description=(
        "Drains `messages.affectedHistory` until the offset is 0. No "
        "dedicated update is emitted; other clients learn about it from the "
        "deleted root message."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("chat_id", "topic_id", "deleted"),
    example={"chat_id": -1001500, "topic_id": 314, "deleted": True},
    example_args="chat topic delete @myforum 314 --yes",
    covers=("groups-channels-admin.topic-delete",),
)


# ---------------------------------------------------------------------------
# chat topic pin / unpin
# ---------------------------------------------------------------------------


class TopicPinReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[
        list[int], arg(1, metavar="TOPIC", kind="msg_id", variadic=True, help="Topic ids.")
    ] = []
    reorder: Annotated[
        bool, opt("--reorder", help="Treat the ids as the complete pinned order.")
    ] = False
    force: Annotated[
        bool, opt("--force", help="With --reorder: unpin topics missing from the list.")
    ] = False


async def pin_topics(ctx: OpContext, req: TopicPinReq) -> TopicPinResult:
    """Pin topics, or (with `--reorder`) declare the whole pinned order."""
    from telethon.tl.functions import messages as fn

    peer = await _forum_peer(ctx, req.chat)
    if not req.topic:
        raise UsageError("name at least one topic id", field="topic")
    handle = _admin.client(ctx)
    ids = [int(t) for t in req.topic]
    if req.reorder:
        await handle(
            fn.ReorderPinnedForumTopicsRequest(peer=peer, order=ids, force=req.force or None)
        )
    else:
        for topic_id in ids:
            await handle(
                fn.UpdatePinnedForumTopicRequest(peer=peer, topic_id=topic_id, pinned=True)
            )
    return TopicPinResult(chat_id=_send.peer_id_of(peer), pinned=ids)


SPEC_TOPIC_PIN = OperationSpec(
    id="chat.topic.pin",
    request=TopicPinReq,
    response=TopicPinResult,
    impl=pin_topics,
    summary="Pin topics (pass several ids to set the pinned order)",
    description=(
        "`--reorder` sends the ids as the complete order; `--force` also "
        "unpins anything missing from the list. At most `topics_pinned_limit` "
        "topics can be pinned."
    ),
    mutating=True,
    columns=("chat_id", "pinned"),
    example={"chat_id": -1001500, "pinned": [314]},
    example_args="chat topic pin @myforum 314",
    covers=("groups-channels-admin.topic-reorder-pinned",),
    covers_partial=("groups-channels-admin.topic-pin",),
    coverage_note="Pinning is here; `chat topic unpin` owns the id.",
)


class TopicUnpinReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[
        list[int],
        arg(1, metavar="TOPIC", kind="msg_id", variadic=True, required=False, help="Topic ids."),
    ] = []
    everything: Annotated[bool, opt("--all", help="Unpin every pinned topic.")] = False


async def unpin_topics(ctx: OpContext, req: TopicUnpinReq) -> TopicPinResult:
    """Unpin topics, or clear the pinned order entirely."""
    from telethon.tl.functions import messages as fn

    peer = await _forum_peer(ctx, req.chat)
    handle = _admin.client(ctx)
    if req.everything:
        await handle(fn.ReorderPinnedForumTopicsRequest(peer=peer, order=[], force=True))
        return TopicPinResult(chat_id=_send.peer_id_of(peer), unpinned=[], pinned=[])
    if not req.topic:
        raise UsageError("name a topic id, or pass --all", field="topic")
    ids = [int(t) for t in req.topic]
    for topic_id in ids:
        await handle(fn.UpdatePinnedForumTopicRequest(peer=peer, topic_id=topic_id, pinned=False))
    return TopicPinResult(chat_id=_send.peer_id_of(peer), unpinned=ids)


SPEC_TOPIC_UNPIN = OperationSpec(
    id="chat.topic.unpin",
    request=TopicUnpinReq,
    response=TopicPinResult,
    impl=unpin_topics,
    summary="Unpin a topic",
    mutating=True,
    columns=("chat_id", "unpinned"),
    example={"chat_id": -1001500, "unpinned": [314]},
    example_args="chat topic unpin @myforum 314",
    covers=("groups-channels-admin.topic-pin",),
)


# ---------------------------------------------------------------------------
# chat topic mute / unmute
# ---------------------------------------------------------------------------


class TopicMuteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[int, arg(1, metavar="TOPIC", kind="msg_id", help="Topic id.")]
    duration: Annotated[
        str | None,
        arg(2, metavar="DURATION", required=False, help="How long; omit to mute forever."),
    ] = None
    silent: Annotated[
        bool | None, opt("--silent", help="Deliver without a sound instead of muting.")
    ] = None
    previews: Annotated[
        bool | None, opt("--previews", help="Show message text in notifications.")
    ] = None


async def _notify_topic(
    ctx: OpContext,
    peer: Any,
    topic_id: int,
    *,
    mute_until: int | None,
    silent: bool | None = None,
    previews: bool | None = None,
) -> TopicResult:
    """One `inputNotifyForumTopic` exception, written absolutely.

    `mute_until` is an absolute wall-clock timestamp, computed here. v1
    computed a timed mute from the event loop's clock and every one of them
    resolved to 1970 (COR-01); a topic mute must not repeat that.
    """
    from datetime import datetime, timezone

    from telethon.tl import types
    from telethon.tl.functions import account as fn

    stamp = (
        datetime.fromtimestamp(mute_until, tz=timezone.utc) if mute_until not in (None, 0) else None
    )
    await _admin.client(ctx)(
        fn.UpdateNotifySettingsRequest(
            peer=types.InputNotifyForumTopic(peer=peer, top_msg_id=topic_id),
            settings=types.InputPeerNotifySettings(
                mute_until=stamp,
                silent=silent,
                show_previews=previews,
            ),
        )
    )
    return TopicResult(
        chat_id=_send.peer_id_of(peer),
        topic_id=topic_id,
        mute_until=fmt_dt(stamp),
        silent=silent,
        previews=previews,
    )


async def mute_topic(ctx: OpContext, req: TopicMuteReq) -> TopicResult:
    """Mute one topic. Omitting the duration means forever."""
    import time

    peer = await _forum_peer(ctx, req.chat)
    until: int | None = MUTE_FOREVER
    if req.duration:
        seconds = parse_duration(req.duration)
        if seconds is None:
            raise UsageError(f"{req.duration!r} is not a duration", field="duration")
        until = int(time.time()) + int(seconds)
    if req.silent is not None and req.duration is None:
        until = None
    return await _notify_topic(
        ctx, peer, req.topic, mute_until=until, silent=req.silent, previews=req.previews
    )


SPEC_TOPIC_MUTE = OperationSpec(
    id="chat.topic.mute",
    request=TopicMuteReq,
    response=TopicResult,
    impl=mute_topic,
    summary="Mute a topic",
    description=(
        "`mute_until` is an absolute timestamp computed from the wall clock. "
        "`--silent on` without a duration switches to silent delivery "
        "instead of muting."
    ),
    mutating=True,
    columns=("chat_id", "topic_id", "mute_until"),
    example={"chat_id": -1001500, "topic_id": 314, "mute_until": "2038-01-19T03:14:07Z"},
    example_args="chat topic mute @myforum 314 8h",
    covers_partial=("groups-channels-admin.topic-notify-settings",),
    coverage_note="Muting half; `chat topic unmute` owns the id.",
)


async def unmute_topic(ctx: OpContext, req: TopicOneReq) -> TopicResult:
    """Unmute a topic (mute_until = 0)."""
    peer = await _forum_peer(ctx, req.chat)
    return await _notify_topic(ctx, peer, req.topic, mute_until=0)


SPEC_TOPIC_UNMUTE = OperationSpec(
    id="chat.topic.unmute",
    request=TopicOneReq,
    response=TopicResult,
    impl=unmute_topic,
    summary="Unmute a topic",
    mutating=True,
    columns=("chat_id", "topic_id", "mute_until"),
    example={"chat_id": -1001500, "topic_id": 314},
    example_args="chat topic unmute @myforum 314",
    covers=("groups-channels-admin.topic-notify-settings",),
)


# ---------------------------------------------------------------------------
# chat topic read
# ---------------------------------------------------------------------------


class TopicReadReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Forum supergroup.")]
    topic: Annotated[int, arg(1, metavar="TOPIC", kind="msg_id", help="Topic id.")]
    max_id: Annotated[
        int, opt("--max-id", metavar="ID", help="Read up to this message id; 0 means everything.")
    ] = 0
    mentions: Annotated[bool, opt("--mentions", help="Also clear the unread-mentions badge.")] = (
        False
    )
    reactions: Annotated[
        bool, opt("--reactions", help="Also clear the unread-reactions badge.")
    ] = False
    list_only: Annotated[
        bool, opt("--list", help="Do not read: list the unread mentions/reactions instead.")
    ] = False


async def read_topic(ctx: OpContext, req: TopicReadReq) -> TopicReadResult:
    """Mark a topic read, or list what is unread in it.

    Reading a topic does not read the rest of the forum, and `top_msg_id`
    must be omitted for General — sending 1 there is how a client ends up
    reading nothing.
    """
    from telethon.tl.functions import messages as fn

    peer = await _forum_peer(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    handle = _admin.client(ctx)
    top = None if req.topic == _admin.GENERAL_TOPIC else req.topic

    if req.list_only:
        items = []
        if req.mentions or not req.reactions:
            reply = await handle(
                fn.GetUnreadMentionsRequest(
                    peer=peer,
                    top_msg_id=top,
                    offset_id=0,
                    add_offset=0,
                    limit=int(getattr(ctx, "limit", None) or 50),
                    max_id=0,
                    min_id=0,
                )
            )
            items += [
                message_to_model(m, chat_id=chat_id)
                for m in (getattr(reply, "messages", None) or [])
            ]
        if req.reactions:
            reply = await handle(
                fn.GetUnreadReactionsRequest(
                    peer=peer,
                    top_msg_id=top,
                    offset_id=0,
                    add_offset=0,
                    limit=int(getattr(ctx, "limit", None) or 50),
                    max_id=0,
                    min_id=0,
                )
            )
            items += [
                message_to_model(m, chat_id=chat_id)
                for m in (getattr(reply, "messages", None) or [])
            ]
        return TopicReadResult(chat_id=chat_id, topic_id=req.topic, items=items)

    await handle(
        fn.ReadDiscussionRequest(peer=peer, msg_id=req.topic, read_max_id=req.max_id or 0x7FFFFFFF)
    )
    if req.mentions:
        await handle(fn.ReadMentionsRequest(peer=peer, top_msg_id=top))
    if req.reactions:
        await handle(fn.ReadReactionsRequest(peer=peer, top_msg_id=top))
    ctx.emit("chat_topic_read", {"chat_id": chat_id, "topic_id": req.topic})
    return TopicReadResult(
        chat_id=chat_id,
        topic_id=req.topic,
        max_id=req.max_id or None,
        unread_count=0,
    )


SPEC_TOPIC_READ = OperationSpec(
    id="chat.topic.read",
    request=TopicReadReq,
    response=TopicReadResult,
    impl=read_topic,
    summary="Mark a topic read, including its mentions and reactions",
    description=(
        "SEMANTICS: this emits a read receipt inside the topic, exactly like "
        "`chat open` does for a chat. `--list` is the silent half. "
        "`top_msg_id` is omitted for General (id 1), which is what the API "
        "requires. Sending and listing messages inside a topic is "
        "`message send/list --topic`."
    ),
    mutating=True,
    columns=("chat_id", "topic_id", "unread_count"),
    example={"chat_id": -1001500, "topic_id": 314, "unread_count": 0},
    example_args="chat topic read @myforum 314 --mentions",
    covers=(
        "groups-channels-admin.topic-messages",
        "groups-channels-admin.topic-unread-counters",
    ),
    tags=frozenset({"visible-to-others"}),
)
