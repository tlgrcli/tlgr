"""The `vc` group: video chats, livestreams, live stories and RTMP.

One MTProto surface (`phone.*groupCall*`) is four products in the GUI, and
which one you get depends on the peer and two flags: a video chat in a group,
a livestream in a channel, an RTMP stream when `rtmp_stream` is set, a live
story when it hangs off a story. tlgr keeps that as one noun with one set of
verbs and reports `kind`, rather than shipping four near-identical groups.

What is real here and what is not:

* **Real.** Creating, scheduling, starting, ending, titling, recording,
  muting, moderating, inviting, links, RTMP credentials, the participant
  list, the live update stream, and downloading a livestream to disk. None of
  that needs a media engine.
* **Not real.** Anything that would put your microphone or camera on the
  wire. `vc join` obtains *server-side presence*, `vc mute` flips a
  server-side flag, `vc video set` announces a state — and every one of them
  says `media: none`, because there is no audio behind it.

`vc download` is the one place where a headless CLI is strictly better than
the GUI: it cannot play a livestream, but it can record one.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from tlgr.core.errors import (
    NotFoundError,
    NotSupportedError,
    PermissionError_,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.call import (
    MEDIA_NONE,
    ActiveCall,
    CallIdentity,
    CallRef,
    GroupCall,
    GroupCallCreated,
    GroupCallEnded,
    GroupCallEvent,
    GroupCallInvited,
    GroupCallJoined,
    GroupCallLeft,
    GroupCallLink,
    GroupCallParticipant,
    GroupCallSettings,
    GroupCallStarted,
    InCallMessage,
    InCallMessagesDeleted,
    MuteState,
    ParticipantRemoved,
    RaisedHand,
    RtmpInfo,
    StreamChannel,
    StreamDownload,
    VideoState,
    VolumeState,
)
from tlgr.models.page import Page
from tlgr.models.peer import Peer, PeerRef
from tlgr.ops import _calls, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The default overlay lifetime and length cap, used when the app-config does
#: not carry them. Both are validated locally so a message that cannot land
#: fails here rather than at the server.
DEFAULT_MESSAGE_LENGTH = 128
DEFAULT_MESSAGE_TTL = 10

_EXAMPLE_REF: dict[str, Any] = {"id": 900100, "access_hash": 12345}
_EXAMPLE_CALL: dict[str, Any] = {
    "call": _EXAMPLE_REF,
    "kind": "video-chat",
    "media": "none",
    "title": "standup",
    "participants_count": 4,
}


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


def _now() -> str:
    return fmt_dt(datetime.now(timezone.utc)) or ""


def _entities(result: Any) -> dict[int, Any]:
    """`{raw id: entity}` for everything a `phone.*` answer carried along."""
    found: dict[int, Any] = {}
    for entity in (getattr(result, "users", None) or []) + (getattr(result, "chats", None) or []):
        found[int(getattr(entity, "id", 0) or 0)] = entity
    return found


def _peer_model(peer: Any, entities: dict[int, Any]) -> Peer | None:
    """A `Peer` constructor plus the entity table → the output shape."""
    if peer is None:
        return None
    raw = int(
        getattr(peer, "user_id", None)
        or getattr(peer, "channel_id", None)
        or getattr(peer, "chat_id", None)
        or 0
    )
    entity = entities.get(raw)
    if entity is not None:
        return entity_to_peer(entity)
    if getattr(peer, "channel_id", None):
        return Peer(id=-1000000000000 - raw, raw_id=raw, kind="channel")
    if getattr(peer, "chat_id", None):
        return Peer(id=-raw, raw_id=raw, kind="group")
    return Peer(id=raw, raw_id=raw, kind="user")


def _kind_of(call: Any, chat: Any = None) -> str:
    """Which of the four products this call is."""
    if getattr(call, "conference", False):
        return "conference"
    if getattr(call, "rtmp_stream", False):
        return "rtmp"
    if chat is not None and getattr(chat, "broadcast", False):
        return "livestream"
    return "video-chat"


def _group_model(
    call: Any, *, chat: Any = None, entities: dict[int, Any] | None = None
) -> GroupCall:
    """A `groupCall` or `groupCallDiscarded` as the wire shape."""
    if type(call).__name__ == "GroupCallDiscarded":
        return GroupCall(
            call=_calls.call_ref_of(call),
            discarded=True,
            duration=int(getattr(call, "duration", 0) or 0),
        )
    link = getattr(call, "invite_link", None)
    return GroupCall(
        call=_calls.call_ref_of(call, slug=link.rsplit("/", 1)[-1] if link else None),
        kind=_kind_of(call, chat),
        media=MEDIA_NONE,
        title=getattr(call, "title", None),
        participants_count=int(getattr(call, "participants_count", 0) or 0),
        join_muted=bool(getattr(call, "join_muted", False)),
        can_change_join_muted=bool(getattr(call, "can_change_join_muted", False)),
        messages_enabled=bool(getattr(call, "messages_enabled", False)),
        can_change_messages_enabled=bool(getattr(call, "can_change_messages_enabled", False)),
        record_start_date=fmt_dt(getattr(call, "record_start_date", None)),
        record_video_active=bool(getattr(call, "record_video_active", False)),
        rtmp_stream=bool(getattr(call, "rtmp_stream", False)),
        listeners_hidden=bool(getattr(call, "listeners_hidden", False)),
        conference=bool(getattr(call, "conference", False)),
        creator=bool(getattr(call, "creator", False)),
        schedule_date=fmt_dt(getattr(call, "schedule_date", None)),
        schedule_start_subscribed=bool(getattr(call, "schedule_start_subscribed", False)),
        stream_dc_id=getattr(call, "stream_dc_id", None),
        invite_link=link,
        send_paid_messages_stars=getattr(call, "send_paid_messages_stars", None),
        default_send_as=_peer_model(getattr(call, "default_send_as", None), entities or {}),
        unmuted_video_count=int(getattr(call, "unmuted_video_count", 0) or 0),
        unmuted_video_limit=int(getattr(call, "unmuted_video_limit", 0) or 0),
        version=int(getattr(call, "version", 0) or 0),
        chat=entity_to_peer(chat) if chat is not None else None,
    )


def _participant_model(raw: Any, entities: dict[int, Any]) -> GroupCallParticipant:
    volume = getattr(raw, "volume", None)
    return GroupCallParticipant(
        peer=_peer_model(getattr(raw, "peer", None), entities),
        source=int(getattr(raw, "source", 0) or 0),
        muted=bool(getattr(raw, "muted", False)),
        can_self_unmute=bool(getattr(raw, "can_self_unmute", False)),
        muted_by_you=bool(getattr(raw, "muted_by_you", False)),
        volume=int(volume) // 100 if volume else None,
        volume_by_admin=bool(getattr(raw, "volume_by_admin", False)),
        raise_hand=getattr(raw, "raise_hand_rating", None) is not None,
        raise_hand_rating=getattr(raw, "raise_hand_rating", None),
        video=getattr(raw, "video", None) is not None,
        presentation=getattr(raw, "presentation", None) is not None,
        video_joined=bool(getattr(raw, "video_joined", False)),
        is_self=bool(getattr(raw, "is_self", False)),
        left=bool(getattr(raw, "left", False)),
        about=getattr(raw, "about", None),
        joined_at=fmt_dt(getattr(raw, "date", None)),
        last_active=fmt_dt(getattr(raw, "active_date", None)),
        paid_stars_total=getattr(raw, "paid_stars_total", None),
    )


async def _chat_peer(ctx: OpContext, ref: PeerRef | None) -> Any:
    if ref is None:
        raise UsageError("a chat is required", field="chat")
    return await _send.resolve(ctx, ref)


def _call_from_updates(updates: Any) -> Any:
    """The `groupCall` an `Updates` carries in its `updateGroupCall`."""
    for update in getattr(updates, "updates", None) or []:
        call = getattr(update, "call", None)
        if call is not None:
            return call
    return None


async def _fetch_call(ctx: OpContext, handle: _calls.CallHandle) -> tuple[Any, dict[int, Any]]:
    from telethon.tl.functions import phone as fn

    result = await _client(ctx)(fn.GetGroupCallRequest(call=handle.input, limit=0))
    call = getattr(result, "call", None)
    if call is None:
        raise NotFoundError("that call does not exist any more")
    return call, _entities(result)


def _forbid_e2e(op: str) -> None:
    raise UsageError(
        f"{op} needs a signed e2e.chain block and an int256 public key. Conferences are "
        "end-to-end encrypted and tlgr has no block builder, so pass --block and "
        "--public-key from an external E2E implementation, or use a video chat instead",
        field="block",
    )


# ---------------------------------------------------------------------------
# vc create
# ---------------------------------------------------------------------------


class CreateReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to start it.")]
    title: Annotated[str | None, opt("--title", help="Call title; defaults to the chat name.")] = (
        None
    )
    schedule: Annotated[
        str | None,
        opt("--schedule", metavar="WHEN", kind="datetime", help="Schedule it instead of starting."),
    ] = None
    rtmp: Annotated[
        bool, opt("--rtmp", help="RTMP mode: one external encoder publishes all media.")
    ] = False


async def create(ctx: OpContext, req: CreateReq) -> GroupCallCreated:
    """Start or schedule a video chat, livestream or RTMP stream.

    One RPC covers all three products and the name follows the peer. A chat
    holds one call at a time, so creating a new one *terminates* the old one —
    which is why this is a confirmed operation rather than a convenience.
    """
    from telethon.tl.functions import phone as fn

    peer = await _chat_peer(ctx, req.chat)
    result = await _client(ctx)(
        fn.CreateGroupCallRequest(
            peer=peer,
            random_id=secrets.randbits(31),
            title=req.title,
            schedule_date=parse_dt(req.schedule) if req.schedule else None,
            rtmp_stream=req.rtmp or None,
        )
    )
    call = _call_from_updates(result)
    if call is None:
        raise NotSupportedError(
            "the server created the call without telling us which one; nothing to address"
        )
    model = _group_model(call)
    ctx.emit("vc_created", {"chat_id": _send.peer_id_of(peer), "call_id": model.call.id})
    return GroupCallCreated(
        call=model.call,
        chat_id=_send.peer_id_of(peer),
        kind="rtmp" if req.rtmp else model.kind,
        title=model.title or req.title,
        schedule_date=model.schedule_date,
        rtmp_stream=req.rtmp,
    )


SPEC_CREATE = OperationSpec(
    id="vc.create",
    request=CreateReq,
    response=GroupCallCreated,
    impl=create,
    summary="Start or schedule a video chat, livestream or RTMP stream in a chat",
    description=(
        "Needs `manage_call`. A chat has one call at a time: creating a new "
        "one ends the old one, which is why it asks."
    ),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("call.id", "chat_id", "title", "kind"),
    example={"call": _EXAMPLE_REF, "chat_id": -1000000005150, "kind": "video-chat"},
    example_args="vc create @newsroom",
    covers=(
        "groupcall.create-livestream",
        "groupcall.create-rtmp-call",
        "groupcall.create-video-chat",
        "groupcall.schedule",
        "groupcall.set-title-on-create",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# vc start
# ---------------------------------------------------------------------------


class StartReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat with the call.")]


async def start(ctx: OpContext, req: StartReq) -> GroupCallStarted:
    """Start a scheduled video chat now; subscribers with a reminder are told."""
    from telethon.tl.functions import phone as fn

    handle = await _calls.resolve_call(ctx, req.chat.raw)
    handle = await _calls.concrete_call(ctx, handle)
    await _client(ctx)(fn.StartScheduledGroupCallRequest(call=handle.input))
    chat_id = _send.peer_id_of(handle.chat) if handle.chat is not None else 0
    ctx.emit("vc_started", {"call_id": handle.ref.id, "chat_id": chat_id})
    return GroupCallStarted(call=handle.ref, chat_id=chat_id)


SPEC_START = OperationSpec(
    id="vc.start",
    request=StartReq,
    response=GroupCallStarted,
    impl=start,
    summary="Start a scheduled video chat now",
    mutating=True,
    rate_class="send",
    columns=("call.id", "chat_id", "started"),
    example={"call": _EXAMPLE_REF, "chat_id": -1000000005150, "started": True},
    example_args="vc start @newsroom",
    covers=("groupcall.start-scheduled",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# vc get
# ---------------------------------------------------------------------------


class GetReq(Request):
    call: Annotated[
        str,
        arg(0, metavar="CALL", help="A chat, id:access_hash, a call link, or msg:<id>."),
    ]
    limits: Annotated[bool, opt("--limits", help="Add the app-config caps.")] = False
    stream_channels: Annotated[
        bool, opt("--stream-channels", help="Add the live stream channels and the live edge.")
    ] = False
    check_sources: Annotated[
        list[int],
        opt("--check-sources", metavar="SSRC", help="Ask which of these sources are still joined."),
    ] = []
    donors: Annotated[bool, opt("--donors", help="Add Stars donated and top donors.")] = False


async def get(ctx: OpContext, req: GetReq) -> GroupCall:
    """Everything about one group call.

    `limit` on `getGroupCall` is unusual — at least three participants always
    come back — so tlgr asks for 0 and leaves paging to `vc participant list`.
    When `listeners_hidden` is set the participant list carries publishers
    only: `participants_count` is the audience, and reporting the short list
    as "the call" would be a lie about an empty room.
    """
    from telethon.tl.functions import phone as fn

    handle = await _calls.resolve_call(ctx, req.call)
    call, entities = await _fetch_call(ctx, handle)
    chat = entities.get(int(getattr(getattr(handle, "chat", None), "channel_id", 0) or 0))
    model = _group_model(call, chat=chat, entities=entities)
    if handle.ref.slug:
        model.call.slug = handle.ref.slug

    if req.limits:
        config = await _calls.app_config(ctx)
        model.limits = {k: int(v) for k, v in config.items() if isinstance(v, (int, float))}

    if req.stream_channels:
        channels = await _client(ctx)(fn.GetGroupCallStreamChannelsRequest(call=handle.input))
        rows = [
            StreamChannel(
                channel=int(getattr(item, "channel", 0) or 0),
                scale=int(getattr(item, "scale", 0) or 0),
                last_timestamp_ms=int(getattr(item, "last_timestamp_ms", 0) or 0),
            )
            for item in (getattr(channels, "channels", None) or [])
        ]
        model.stream_channels = rows
        model.live_edge_ms = max((row.last_timestamp_ms for row in rows), default=0)
        if not rows:
            ctx.warn("no stream channels yet: the publisher is idle, try again in a second")

    if req.check_sources:
        checked = await _client(ctx)(
            fn.CheckGroupCallRequest(call=handle.input, sources=list(req.check_sources))
        )
        joined = [int(s) for s in (checked or [])]
        model.sources_joined = joined
        model.sources_missing = [s for s in req.check_sources if s not in joined]

    if req.donors:
        stars = await _client(ctx)(fn.GetGroupCallStarsRequest(call=handle.input))
        model.donors = {
            "total": int(getattr(stars, "stars", 0) or 0),
            "top": [
                (peer.title or peer.username or str(peer.id))
                for peer in (
                    _peer_model(item, _entities(stars))
                    for item in (getattr(stars, "top_peers", None) or [])
                )
                if peer is not None
            ],
        }
    return model


SPEC_GET = OperationSpec(
    id="vc.get",
    request=GetReq,
    response=GroupCall,
    impl=get,
    summary="Everything about a group call: state, recording, limits, stream channels, donors",
    description=(
        "`CALL` accepts a chat, `id:access_hash`, a t.me video-chat or "
        "`t.me/call/<slug>` link, or `msg:<id>` for an invitation."
    ),
    columns=("call.id", "kind", "title", "participants_count", "rtmp_stream"),
    example=_EXAMPLE_CALL,
    example_args="vc get @newsroom",
    covers=(
        "groupcall.check-connection",
        "groupcall.get",
        "groupcall.limits-config",
        "groupcall.listeners-hidden",
        "groupcall.paid-comment-tiers",
        "groupcall.participant-limits",
        "groupcall.recording-status",
        "groupcall.resolve-call-link",
        "groupcall.stars-top-donors",
        "groupcall.stream-channels",
    ),
)


# ---------------------------------------------------------------------------
# vc list
# ---------------------------------------------------------------------------


class ListReq(Request):
    scheduled: Annotated[
        bool, opt("--scheduled", help="Include chats whose call is only scheduled.")
    ] = True
    empty: Annotated[bool, opt("--empty", help="Include active calls nobody is in.")] = True


async def list_calls(ctx: OpContext, req: ListReq) -> Page[ActiveCall]:
    """Chats with a call running right now.

    Not an RPC of its own: `call_active` and `call_not_empty` ride on the
    `Chat`/`Channel` constructors the dialog list already returns, so this is
    one pass over the cached dialogs plus one `getGroupCall` per hit — and the
    per-hit call is what makes the page size worth respecting.
    """
    limit = min(int(getattr(ctx, "limit", None) or 30), 200)
    token = getattr(ctx, "cursor", None)
    state = (
        decode_cursor(token, op="vc.list", kind=PageKind.LOCAL, account=ctx.account)
        if token
        else {}
    )
    offset = int(state.get("offset", 0) or 0)

    hits: list[Any] = []
    async for dialog in _client(ctx).iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue
        active = bool(getattr(entity, "call_active", False))
        if not active:
            continue
        if not req.empty and not getattr(entity, "call_not_empty", False):
            continue
        hits.append(entity)

    window = hits[offset : offset + limit]
    items: list[ActiveCall] = []
    for entity in window:
        row = ActiveCall(
            chat=entity_to_peer(entity),
            chat_id=_send.peer_id_of(entity),
            active=True,
            not_empty=bool(getattr(entity, "call_not_empty", False)),
        )
        with contextlib.suppress(Exception):
            handle = await _calls.resolve_call(ctx, str(row.chat_id))
            call, entities = await _fetch_call(ctx, handle)
            model = _group_model(call, chat=entity, entities=entities)
            row.call = model.call
            row.title = model.title
            row.participants_count = model.participants_count
            row.schedule_date = model.schedule_date
            row.rtmp_stream = model.rtmp_stream
        if row.schedule_date and not req.scheduled:
            continue
        items.append(row)

    return build_page(
        items,
        op="vc.list",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window)},
        account=ctx.account,
        has_more=offset + len(window) < len(hits),
        total=len(hits),
    )


SPEC_LIST = OperationSpec(
    id="vc.list",
    request=ListReq,
    response=Page[ActiveCall],
    impl=list_calls,
    summary="Chats with a video chat, livestream or scheduled call running right now",
    paginated=PageKind.LOCAL,
    columns=("chat_id", "title", "participants_count", "not_empty"),
    headers=("Chat", "Title", "In call", "Live"),
    example={"items": [{"chat_id": -1000000005150, "title": "standup"}], "has_more": False},
    example_args="vc list",
    covers=("groupcall.active-calls-list", "groupcall.detect-active-call"),
)


# ---------------------------------------------------------------------------
# vc set
# ---------------------------------------------------------------------------


class SetReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    title: Annotated[
        str | None, opt("--title", help="Rename the call; empty resets it to the chat name.")
    ] = None
    join_muted: Annotated[
        str | None, choice("on", "off", help="New participants arrive muted.")
    ] = None
    messages: Annotated[
        str | None, choice("on", "off", help="In-call message/comment overlay.")
    ] = None
    comment_price: Annotated[
        int | None,
        opt("--comment-price", metavar="STARS", help="Minimum Stars to comment; 0 is free."),
    ] = None
    reminder: Annotated[
        str | None, choice("on", "off", help="Be notified when a scheduled call starts.")
    ] = None
    record: Annotated[str | None, choice("start", "stop", help="Server-side recording.")] = None
    record_title: Annotated[str | None, opt("--record-title", help="Name for the recording.")] = (
        None
    )
    record_video: Annotated[bool, opt("--record-video", help="Also record video.")] = False
    record_portrait: Annotated[bool, opt("--record-portrait", help="Portrait video recording.")] = (
        False
    )


async def set_call(ctx: OpContext, req: SetReq) -> GroupCallSettings:
    """The in-call settings sheet as one command.

    Four RPCs sit behind it and each is sent only for the flags you passed,
    so `vc set --title x` does not silently re-assert the recording state.
    The result is read back from the server rather than echoed, because
    `--join-muted` on an RTMP call is refused server-side and reporting what
    we asked for would hide that.
    """
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    client = _client(ctx)
    changed: list[str] = []

    if req.title is not None:
        await client(fn.EditGroupCallTitleRequest(call=handle.input, title=req.title))
        changed.append("title")

    settings: dict[str, Any] = {}
    if req.join_muted is not None:
        settings["join_muted"] = req.join_muted == "on"
    if req.messages is not None:
        settings["messages_enabled"] = req.messages == "on"
    if req.comment_price is not None:
        settings["send_paid_messages_stars"] = req.comment_price
    if settings:
        await client(fn.ToggleGroupCallSettingsRequest(call=handle.input, **settings))
        changed.extend(sorted(settings))

    if req.reminder is not None:
        await client(
            fn.ToggleGroupCallStartSubscriptionRequest(
                call=handle.input, subscribed=req.reminder == "on"
            )
        )
        changed.append("reminder")

    if req.record is not None:
        await client(
            fn.ToggleGroupCallRecordRequest(
                call=handle.input,
                start=req.record == "start" or None,
                video=req.record_video or None,
                title=req.record_title,
                video_portrait=req.record_portrait or None,
            )
        )
        changed.append("record")
        ctx.warn("the recording is delivered to the starting admin's Saved Messages when it stops")

    if not changed:
        raise UsageError("nothing to set; pass at least one flag", field="title")

    call, entities = await _fetch_call(ctx, handle)
    model = _group_model(call, entities=entities)
    ctx.emit("vc_settings", {"call_id": model.call.id, "changed": changed})
    return GroupCallSettings(
        call=model.call,
        title=model.title,
        join_muted=model.join_muted,
        messages_enabled=model.messages_enabled,
        send_paid_messages_stars=model.send_paid_messages_stars,
        schedule_start_subscribed=model.schedule_start_subscribed,
        record_start_date=model.record_start_date,
        record_video_active=model.record_video_active,
        changed=changed,
    )


SPEC_SET = OperationSpec(
    id="vc.set",
    request=SetReq,
    response=GroupCallSettings,
    impl=set_call,
    aliases=("story.live.settings", "vc.record"),
    summary="Call settings: title, mute-on-join, messages, comment price, reminder, recording",
    description=(
        "Everything except `--reminder` needs `manage_call`. Recording is "
        "server-side and every participant sees the badge."
    ),
    mutating=True,
    rate_class="send",
    columns=("call.id", "title", "join_muted", "messages_enabled", "changed"),
    example={"call": _EXAMPLE_REF, "title": "standup", "changed": ["title"]},
    example_args="vc set @newsroom --title standup",
    covers=(
        "groupcall.edit-title",
        "groupcall.record-start-audio",
        "groupcall.record-start-video",
        "groupcall.record-stop",
        "groupcall.schedule-reminder",
        "groupcall.set-comment-price",
        "groupcall.toggle-join-muted",
        "groupcall.toggle-messages-enabled",
        "stories.live-settings",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# vc end
# ---------------------------------------------------------------------------


class EndReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]


async def end(ctx: OpContext, req: EndReq) -> GroupCallEnded:
    """End a video chat, livestream, live story or conference for everyone."""
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    result = await _client(ctx)(fn.DiscardGroupCallRequest(call=handle.input))
    call = _call_from_updates(result)
    duration = int(getattr(call, "duration", 0) or 0) if call is not None else None
    ctx.emit("vc_ended", {"call_id": handle.ref.id})
    return GroupCallEnded(call=handle.ref, ended=True, duration=duration)


SPEC_END = OperationSpec(
    id="vc.end",
    request=EndReq,
    response=GroupCallEnded,
    impl=end,
    aliases=("story.live.end", "conference.end"),
    summary="End a video chat, livestream, live story or conference for everyone",
    description="Irreversible: the call becomes `groupCallDiscarded`.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("call.id", "ended", "duration"),
    example={"call": _EXAMPLE_REF, "ended": True, "duration": 900},
    example_args="vc end @newsroom",
    covers=("conference.end", "groupcall.discard", "livestory.end", "stories.live-end"),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# vc join / leave
# ---------------------------------------------------------------------------


class JoinReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Appear as yourself or a channel."),
    ] = None
    remember: Annotated[bool, opt("--remember", help="Store --send-as as the chat's default.")] = (
        False
    )
    muted: Annotated[bool, opt("--muted/--unmuted", help="Join muted.")] = True
    video_stopped: Annotated[bool, opt("--video-stopped", help="Join with video off.")] = True
    invite_hash: Annotated[
        str | None, opt("--invite-hash", help="Speaker link hash: grants can_self_unmute.")
    ] = None
    params_json: Annotated[
        str | None,
        opt("--params-json", metavar="PATH", kind="path", help="tgcalls join payload."),
    ] = None
    listen_only: Annotated[
        bool,
        opt("--listen-only", help="Synthesize a listener payload. Experimental — see the docs."),
    ] = False
    public_key: Annotated[
        str | None, opt("--public-key", metavar="HEX", help="int256 E2E key (conferences).")
    ] = None
    block: Annotated[
        str | None, opt("--block", metavar="PATH", kind="path", help="E2E join block.")
    ] = None


def _listener_params() -> str:
    """A syntactically valid listener payload, with no media behind it.

    An empty or `{}` `params` is not sanctioned anywhere in the documentation
    and must be expected to fail, so `--listen-only` produces the shape a real
    engine produces — a fresh SSRC, ICE credentials, a DTLS fingerprint —
    purely to obtain server-side presence, which is a hard prerequisite for
    `vc download`. It carries no audio and never will.
    """
    import json

    fingerprint = ":".join(f"{byte:02X}" for byte in secrets.token_bytes(32))
    return json.dumps(
        {
            "ufrag": secrets.token_hex(4),
            "pwd": secrets.token_hex(12),
            "fingerprints": [{"hash": "sha-256", "setup": "active", "fingerprint": fingerprint}],
            "ssrc": secrets.randbits(31),
            "ssrc-groups": [],
        }
    )


async def join(ctx: OpContext, req: JoinReq) -> GroupCallJoined:
    """Join a group call as a participant — control plane only.

    `params` is documented as a payload the local tgcalls engine produces.
    tlgr has no engine, so there are exactly two honest options and both are
    offered: bring your own payload with `--params-json`, or take
    `--listen-only`, which synthesizes a valid-looking one to obtain presence
    and says so in the answer. Neither carries audio. Remember `source`:
    `vc leave` needs it.
    """
    import json

    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    call, entities = await _fetch_call(ctx, handle)
    conference = bool(getattr(call, "conference", False))

    if conference and (not req.block or not req.public_key):
        _forbid_e2e("joining a conference")

    if not req.params_json and not req.listen_only:
        raise UsageError(
            "joining needs a tgcalls join payload: pass --params-json from a real media "
            "engine, or --listen-only to synthesize a listener payload for presence only",
            field="params_json",
        )
    payload = (
        _read_text(req.params_json, field="params-json") if req.params_json else _listener_params()
    )

    join_as: Any = types.InputPeerSelf()
    if req.send_as is not None:
        if conference:
            raise UsageError(
                "a conference is always joined as yourself; --send-as is for video chats",
                field="send_as",
            )
        join_as = await _send.resolve(ctx, req.send_as)

    result = await _client(ctx)(
        fn.JoinGroupCallRequest(
            call=handle.input,
            join_as=join_as,
            params=types.DataJSON(data=payload),
            muted=req.muted or None,
            video_stopped=req.video_stopped or None,
            invite_hash=req.invite_hash,
            public_key=int(req.public_key, 16) if req.public_key else None,
            block=_read_bytes(req.block, field="block") if req.block else None,
        )
    )

    if req.remember and req.send_as is not None and handle.chat is not None:
        await _client(ctx)(fn.SaveDefaultGroupCallJoinAsRequest(peer=handle.chat, join_as=join_as))

    mode = "webrtc"
    connection: dict[str, Any] = {}
    for update in getattr(result, "updates", None) or []:
        params = getattr(update, "params", None)
        if params is None:
            continue
        with contextlib.suppress(ValueError):
            connection = json.loads(getattr(params, "data", "") or "{}")
    if connection.get("rtmp"):
        mode = "rtmp"
    elif connection.get("stream"):
        mode = "stream"

    try:
        source = int(json.loads(payload).get("ssrc", 0) or 0)
    except ValueError:  # pragma: no cover - a bring-your-own payload may differ
        source = 0

    ctx.emit("vc_joined", {"call_id": handle.ref.id, "source": source})
    ctx.warn("tlgr has no media engine: this is server-side presence, not audio")
    return GroupCallJoined(
        call=handle.ref,
        media=MEDIA_NONE,
        source=source,
        mode=mode,
        join_as=_peer_model(getattr(call, "default_send_as", None), entities)
        if req.send_as is None
        else None,
        can_self_unmute=bool(req.invite_hash),
        params=connection or None,
        experimental=req.listen_only,
    )


SPEC_JOIN = OperationSpec(
    id="vc.join",
    request=JoinReq,
    response=GroupCallJoined,
    impl=join,
    summary="Join a group call — control plane only, no audio is sent or received",
    description=(
        "`--listen-only` is experimental: the server's acceptance of a "
        "synthesized payload is not documented. It exists because joining is "
        "a hard prerequisite for `vc download`."
    ),
    mutating=True,
    rate_class="send",
    columns=("call.id", "source", "mode", "media"),
    example={"call": _EXAMPLE_REF, "source": 1234567, "mode": "stream", "media": "none"},
    example_args="vc join @newsroom --listen-only",
    covers=(
        "groupcall.detect-stream-mode",
        "groupcall.join",
        "groupcall.join-via-invite-hash",
        "groupcall.save-default-join-as",
    ),
    tags=frozenset({"visible-to-others"}),
)


class LeaveReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    source: Annotated[
        int, opt("--source", metavar="SSRC", help="SSRC you joined with; 0 for a listener.")
    ] = 0


async def leave(ctx: OpContext, req: LeaveReq) -> GroupCallLeft:
    """Leave a call. It keeps running for everyone else."""
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    await _client(ctx)(fn.LeaveGroupCallRequest(call=handle.input, source=req.source))
    ctx.emit("vc_left", {"call_id": handle.ref.id, "source": req.source})
    return GroupCallLeft(call=handle.ref, source=req.source, left=True)


SPEC_LEAVE = OperationSpec(
    id="vc.leave",
    request=LeaveReq,
    response=GroupCallLeft,
    impl=leave,
    aliases=("conference.leave",),
    summary="Leave a group call (it keeps running for everyone else)",
    description=(
        "After leaving a conference the remaining participants still have to "
        "prune you from the E2E chain — `conference remove --left-only`."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("call.id", "source", "left"),
    example={"call": _EXAMPLE_REF, "source": 1234567, "left": True},
    example_args="vc leave @newsroom",
    covers=("conference.leave", "groupcall.leave"),
)


# ---------------------------------------------------------------------------
# vc invite / link / remove
# ---------------------------------------------------------------------------


class InviteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat with the call.")]
    user: Annotated[
        list[PeerRef],
        arg(1, metavar="USER", variadic=True, kind="user", help="Who to invite."),
    ] = []
    add_to_chat: Annotated[
        bool, opt("--add-to-chat", help="Add non-members to the chat first.")
    ] = False
    link_fallback: Annotated[
        bool, opt("--link-fallback", help="Report the link for users who cannot be invited.")
    ] = True


async def invite(ctx: OpContext, req: InviteReq) -> GroupCallInvited:
    """Invite people into a video chat, adding them to the chat if asked.

    `phone.inviteToGroupCall` only works for chat members, so the per-user
    outcome is classified the way the GUI toasts it rather than collapsed into
    one error: invited, added and invited, already in the call, or refused by
    the user's privacy settings.
    """
    from telethon import utils
    from telethon.tl.functions import channels as channels_fn
    from telethon.tl.functions import messages as messages_fn
    from telethon.tl.functions import phone as fn

    if not req.user:
        raise UsageError("give at least one user to invite", field="user")
    peer = await _chat_peer(ctx, req.chat)
    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.chat.raw))
    client = _client(ctx)

    invited: list[Peer] = []
    added: list[Peer] = []
    failed: list[dict[str, Any]] = []
    for reference in req.user:
        target = await _send.resolve(ctx, reference)
        user = utils.get_input_user(target)
        model = Peer(id=_send.peer_id_of(target), raw_id=_send.peer_id_of(target), kind="user")
        try:
            await client(fn.InviteToGroupCallRequest(call=handle.input, users=[user]))
            invited.append(model)
            continue
        except Exception as exc:
            text = f"{type(exc).__name__} {exc}".upper().replace("_", "")
            if "ALREADYPARTICIPANT" in text:
                failed.append({"peer": model.id, "reason": "already-in-call"})
                continue
            if not req.add_to_chat or "NOTPARTICIPANT" not in text:
                failed.append({"peer": model.id, "reason": "privacy-restricted"})
                continue
        try:
            if type(peer).__name__ == "InputPeerChannel":
                await client(
                    channels_fn.InviteToChannelRequest(
                        channel=utils.get_input_channel(peer), users=[user]
                    )
                )
            else:
                await client(
                    messages_fn.AddChatUserRequest(chat_id=peer.chat_id, user_id=user, fwd_limit=0)
                )
            await client(fn.InviteToGroupCallRequest(call=handle.input, users=[user]))
            added.append(model)
            invited.append(model)
        except Exception:
            failed.append({"peer": model.id, "reason": "cannot-add"})

    result = GroupCallInvited(invited=invited, added=added, failed=failed)
    if failed and req.link_fallback:
        with contextlib.suppress(Exception):
            exported = await client(fn.ExportGroupCallInviteRequest(call=handle.input))
            result.link = getattr(exported, "link", None)
    ctx.emit("vc_invited", {"call_id": handle.ref.id, "count": len(invited)})
    return result


SPEC_INVITE = OperationSpec(
    id="vc.invite",
    request=InviteReq,
    response=GroupCallInvited,
    impl=invite,
    summary="Invite people into a video chat, adding them to the chat first if needed",
    mutating=True,
    rate_class="send",
    columns=("invited", "added", "failed"),
    example={"invited": [{"id": 4242, "raw_id": 4242, "kind": "user"}], "added": []},
    example_args="vc invite @newsroom @alice",
    covers=("groupcall.invite-members", "groupcall.invite-nonmember"),
    tags=frozenset({"visible-to-others"}),
)


class LinkReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat with the call.")]
    speaker: Annotated[
        bool, opt("--speaker", help="Link that grants can_self_unmute at join time.")
    ] = False
    revoke: Annotated[
        bool, opt("--revoke", help="Invalidate every existing speaker and listener link.")
    ] = False


async def link(ctx: OpContext, req: LinkReq) -> GroupCallLink:
    """Export or revoke the video chat's listener and speaker links.

    `phone.exportGroupCallInvite` does not work for a call in a private group;
    the documented fallback is the chat's own invite link, which is fetched
    automatically and marked `fallback: true` so nobody mistakes one for the
    other.
    """
    from telethon.tl.functions import messages as messages_fn
    from telethon.tl.functions import phone as fn

    peer = await _chat_peer(ctx, req.chat)
    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.chat.raw))
    client = _client(ctx)

    if req.revoke:
        await client(fn.ToggleGroupCallSettingsRequest(call=handle.input, reset_invite_hash=True))
        ctx.emit("vc_link_revoked", {"call_id": handle.ref.id})
        return GroupCallLink(kind="speaker" if req.speaker else "listener", revoked=True)

    try:
        exported = await client(
            fn.ExportGroupCallInviteRequest(call=handle.input, can_self_unmute=req.speaker or None)
        )
        url = getattr(exported, "link", None)
        invite_hash = url.rsplit("=", 1)[-1] if url and "=" in url else None
        return GroupCallLink(
            kind="speaker" if req.speaker else "listener", link=url, invite_hash=invite_hash
        )
    except Exception as exc:
        text = f"{type(exc).__name__} {exc}".upper()
        if "FORBIDDEN" not in text and "PRIVATE" not in text and "INVALID" not in text:
            raise
        fallback = await client(messages_fn.ExportChatInviteRequest(peer=peer))
        ctx.warn("this call is in a private chat; falling back to the chat's own invite link")
        return GroupCallLink(kind="chat", link=getattr(fallback, "link", None), fallback=True)


SPEC_LINK = OperationSpec(
    id="vc.link",
    request=LinkReq,
    response=GroupCallLink,
    impl=link,
    summary="Export or revoke the video chat's listener and speaker links",
    description="`--speaker` and `--revoke` need `manage_call`.",
    mutating=True,
    rate_class="send",
    columns=("kind", "link", "fallback"),
    example={"kind": "listener", "link": "https://t.me/c/5150?voicechat=abc"},
    example_args="vc link @newsroom",
    covers=(
        "groupcall.export-listener-link",
        "groupcall.export-speaker-link",
        "groupcall.private-chat-link-fallback",
        "groupcall.reset-invite-hash",
    ),
)


class RemoveReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat with the call.")]
    peer: Annotated[PeerRef, arg(1, metavar="PEER", kind="peer", help="Who to remove.")]
    ban: Annotated[bool, opt("--ban", help="Ban them from the chat, not just the call.")] = False


async def remove(ctx: OpContext, req: RemoveReq) -> ParticipantRemoved:
    """Remove a participant from a video chat.

    There is no "kick from call" RPC: the clients restrict or ban the user in
    the chat, which drops them from the call. Conferences work differently —
    `conference remove`.
    """
    from telethon import utils
    from telethon.tl import types
    from telethon.tl.functions import channels as channels_fn
    from telethon.tl.functions import messages as messages_fn

    chat = await _chat_peer(ctx, req.chat)
    target = await _send.resolve(ctx, req.peer)
    client = _client(ctx)

    if type(chat).__name__ == "InputPeerChannel":
        rights = types.ChatBannedRights(
            until_date=None,
            view_messages=req.ban or None,
            send_messages=True,
            send_media=True,
            send_plain=True,
        )
        await client(
            channels_fn.EditBannedRequest(
                channel=utils.get_input_channel(chat), participant=target, banned_rights=rights
            )
        )
    else:
        await client(
            messages_fn.DeleteChatUserRequest(
                chat_id=chat.chat_id, user_id=utils.get_input_user(target)
            )
        )
    ctx.emit("vc_participant_removed", {"chat_id": _send.peer_id_of(chat)})
    return ParticipantRemoved(
        chat_id=_send.peer_id_of(chat),
        peer=Peer(id=_send.peer_id_of(target), raw_id=_send.peer_id_of(target), kind="user"),
        banned=req.ban,
    )


SPEC_REMOVE = OperationSpec(
    id="vc.remove",
    request=RemoveReq,
    response=ParticipantRemoved,
    impl=remove,
    summary="Remove a participant from a video chat",
    description="Needs `ban_users`; restricting them in the chat is what drops the call.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("chat_id", "banned", "removed"),
    example={"chat_id": -1000000005150, "banned": False, "removed": True},
    example_args="vc remove @newsroom @alice",
    covers=("groupcall.remove-participant",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# vc mute / unmute / raise-hand / volume / video
# ---------------------------------------------------------------------------


async def _edit_participant(
    ctx: OpContext, call_ref: str, peer_ref: PeerRef | None, **fields: Any
) -> tuple[CallRef, Peer | None, Any]:
    """`phone.editGroupCallParticipant` with the peer defaulting to yourself."""
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, call_ref))
    participant: Any = types.InputPeerSelf()
    model: Peer | None = None
    if peer_ref is not None:
        participant = await _send.resolve(ctx, peer_ref)
        marked = _send.peer_id_of(participant)
        model = Peer(id=marked, raw_id=abs(marked), kind="user")
    result = await _client(ctx)(
        fn.EditGroupCallParticipantRequest(call=handle.input, participant=participant, **fields)
    )
    return handle.ref, model, result


class MuteReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    peer: Annotated[
        PeerRef | None,
        arg(1, metavar="PEER", required=False, kind="peer", help="Whom; omit for yourself."),
    ] = None
    for_me: Annotated[
        bool, opt("--for-me", help="Mute them only in your own playback (muted_by_you).")
    ] = False


async def mute(ctx: OpContext, req: MuteReq) -> MuteState:
    """Mute yourself, force-mute a participant, or silence someone for yourself.

    The same RPC means two different things depending on your rights: with
    `manage_call` it is a force-mute that takes `can_self_unmute` away, and
    without it the server records `muted_by_you` instead. That is a real
    difference in what other people experience, so `--for-me` is explicit
    rather than inferred.
    """
    if req.peer is not None and not req.for_me:
        ctx.warn(
            "without manage_call this becomes a mute-for-me; pass --for-me if that is "
            "what you meant"
        )
    ref, peer, _ = await _edit_participant(ctx, req.call, req.peer, muted=True)
    ctx.emit("vc_muted", {"call_id": ref.id})
    return MuteState(
        call=ref,
        peer=peer,
        muted=True,
        can_self_unmute=False if req.peer is not None and not req.for_me else None,
        muted_by_you=bool(req.peer is not None and req.for_me),
    )


SPEC_MUTE = OperationSpec(
    id="vc.mute",
    request=MuteReq,
    response=MuteState,
    impl=mute,
    summary="Mute yourself, force-mute a participant, or silence someone just for you",
    description=(
        "Self-mute flips a server-side flag; there is no microphone behind it, and `media` says so."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("call.id", "muted", "muted_by_you", "media"),
    example={"call": _EXAMPLE_REF, "muted": True, "media": "none"},
    example_args="vc mute @newsroom",
    covers=("groupcall.mute-for-me", "groupcall.mute-participant"),
    covers_partial=("groupcall.mute-self",),
    coverage_note="the unmute half lives in `vc unmute`",
)


class UnmuteReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    peer: Annotated[
        PeerRef | None,
        arg(1, metavar="PEER", required=False, kind="peer", help="Whom; omit for yourself."),
    ] = None
    for_me: Annotated[bool, opt("--for-me", help="Undo a mute-for-me on this participant.")] = False


async def unmute(ctx: OpContext, req: UnmuteReq) -> MuteState:
    """Unmute yourself, or allow a force-muted participant to speak.

    Unmuting somebody else does not open their microphone: it restores
    `can_self_unmute`, which the GUI calls "Allow to speak". The alias says
    so, because the RPC's name does not.
    """
    ref, peer, _ = await _edit_participant(ctx, req.call, req.peer, muted=False)
    ctx.emit("vc_unmuted", {"call_id": ref.id})
    if req.peer is not None:
        ctx.warn("this restores can_self_unmute; it does not open their microphone")
    return MuteState(
        call=ref,
        peer=peer,
        muted=False,
        can_self_unmute=True,
        muted_by_you=False,
    )


SPEC_UNMUTE = OperationSpec(
    id="vc.unmute",
    request=UnmuteReq,
    response=MuteState,
    impl=unmute,
    aliases=("vc.allow-speak",),
    summary="Unmute yourself, or allow a force-muted participant to speak",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("call.id", "muted", "can_self_unmute", "media"),
    example={"call": _EXAMPLE_REF, "muted": False, "can_self_unmute": True, "media": "none"},
    example_args="vc unmute @newsroom",
    covers=("groupcall.allow-to-speak", "groupcall.mute-self"),
)


class HandReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    peer: Annotated[
        PeerRef | None,
        arg(1, metavar="PEER", required=False, kind="peer", help="Whom; omit for yourself."),
    ] = None
    lower: Annotated[bool, opt("--lower", help="Lower the hand instead of raising it.")] = False


async def raise_hand(ctx: OpContext, req: HandReq) -> RaisedHand:
    """Ask to speak, or clear a raised hand.

    Video chats and livestreams only — conferences have no raised hands, and
    `raise_hand_rating` is what orders the admin's request queue.
    """
    ref, peer, _ = await _edit_participant(ctx, req.call, req.peer, raise_hand=not req.lower)
    return RaisedHand(call=ref, peer=peer, raise_hand=not req.lower)


SPEC_RAISE_HAND = OperationSpec(
    id="vc.raise-hand",
    request=HandReq,
    response=RaisedHand,
    impl=raise_hand,
    aliases=("vc.lower-hand",),
    summary="Ask to speak, or clear a raised hand",
    description="Lowering somebody else's hand needs `manage_call`.",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("call.id", "raise_hand", "raise_hand_rating"),
    example={"call": _EXAMPLE_REF, "raise_hand": True},
    example_args="vc raise-hand @newsroom",
    covers=("groupcall.lower-participant-hand", "groupcall.raise-hand"),
)


class VolumeReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    peer: Annotated[PeerRef, arg(1, metavar="PEER", kind="peer", help="Whose volume.")]
    percent: Annotated[
        int, arg(2, metavar="PERCENT", help="0 to 200; 100 is normal.", ge=0, le=200)
    ]


async def set_volume(ctx: OpContext, req: VolumeReq) -> VolumeState:
    """Set a participant's volume.

    `PERCENT` is 0–200 and maps to the API's 1..20000. With moderation rights
    it becomes everyone's default (`volume_by_admin`) and lands in the admin
    log; without them it is your own playback only — which, with no media
    engine, means nothing audible happens here.
    """
    ref, peer, _ = await _edit_participant(
        ctx, req.call, req.peer, volume=max(1, req.percent * 100)
    )
    if req.percent == 0:
        ctx.warn("volume 0 is the same action as a mute-for-me")
    ctx.warn("tlgr plays no audio: this changes the server-side setting only")
    return VolumeState(call=ref, peer=peer, volume=req.percent)


SPEC_VOLUME_SET = OperationSpec(
    id="vc.volume.set",
    request=VolumeReq,
    response=VolumeState,
    impl=set_volume,
    summary="Set a participant's volume",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("call.id", "volume", "volume_by_admin"),
    example={"call": _EXAMPLE_REF, "volume": 150},
    example_args="vc volume set @newsroom @alice 150",
    covers=("groupcall.set-volume",),
)


class VideoReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    on: Annotated[bool, opt("--on", help="Announce your camera as running.")] = False
    off: Annotated[bool, opt("--off", help="Announce it as stopped.")] = False
    pause: Annotated[bool, opt("--pause", help="Mark the stream paused.")] = False
    resume: Annotated[bool, opt("--resume", help="Unpause it.")] = False
    screen: Annotated[
        bool, opt("--screen", help="Act on the screen-share connection, not the camera.")
    ] = False
    params_json: Annotated[
        str | None,
        opt("--params-json", metavar="PATH", kind="path", help="Payload for --screen --on."),
    ] = None


async def set_video(ctx: OpContext, req: VideoReq) -> VideoState:
    """Camera and screen-share state in a group call.

    Control-only, and here the gap is total: these calls tell the server what
    your video is doing and the frames go through tgcalls, which tlgr does not
    have. `--screen --on` registers a *second* connection and needs its own
    real payload, so it is plumbing for a bridge; `--screen --off` is a safe
    control call that drops the presentation and keeps the main connection.
    """
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    if req.on and req.off:
        raise UsageError("--on and --off contradict each other", field="on")
    if req.pause and req.resume:
        raise UsageError("--pause and --resume contradict each other", field="pause")

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    client = _client(ctx)

    if req.screen:
        if req.on:
            if not req.params_json:
                raise UsageError(
                    "sharing a screen registers a second tgcalls connection and needs its "
                    "own join payload; pass --params-json from a real media engine",
                    field="params_json",
                )
            await client(
                fn.JoinGroupCallPresentationRequest(
                    call=handle.input,
                    params=types.DataJSON(data=_read_text(req.params_json, field="params-json")),
                )
            )
            ctx.warn("tlgr presents nothing: the connection exists, the frames do not")
            return VideoState(call=handle.ref, presentation=True)
        if req.off:
            await client(fn.LeaveGroupCallPresentationRequest(call=handle.input))
            return VideoState(call=handle.ref, presentation=False)
        if req.pause or req.resume:
            ref, _, _ = await _edit_participant(ctx, req.call, None, presentation_paused=req.pause)
            return VideoState(call=ref, presentation_paused=req.pause)
        raise UsageError("--screen needs --on, --off, --pause or --resume", field="screen")

    fields: dict[str, Any] = {}
    if req.on or req.off:
        fields["video_stopped"] = req.off
    if req.pause or req.resume:
        fields["video_paused"] = req.pause
    if not fields:
        raise UsageError("give --on, --off, --pause or --resume", field="on")
    ref, _, _ = await _edit_participant(ctx, req.call, None, **fields)
    ctx.warn("tlgr has no camera: this announces a state, it does not send video")
    return VideoState(
        call=ref,
        video_stopped=fields.get("video_stopped"),
        video_paused=fields.get("video_paused"),
    )


SPEC_VIDEO_SET = OperationSpec(
    id="vc.video.set",
    request=VideoReq,
    response=VideoState,
    impl=set_video,
    summary="Camera and screen-share state in a group call",
    mutating=True,
    rate_class="send",
    columns=("call.id", "video_stopped", "presentation", "media"),
    example={"call": _EXAMPLE_REF, "video_stopped": False, "media": "none"},
    example_args="vc video set @newsroom --on",
    covers=(
        "groupcall.pause-my-video",
        "groupcall.pause-presentation",
        "groupcall.screen-share-start",
        "groupcall.screen-share-stop",
        "groupcall.toggle-my-video",
    ),
)


# ---------------------------------------------------------------------------
# vc participant list / identity list
# ---------------------------------------------------------------------------


class ParticipantListReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    user: Annotated[
        list[PeerRef],
        opt("--user", metavar="PEER", kind="peer", help="Only these participants."),
    ] = []
    source: Annotated[
        list[int], opt("--source", metavar="SSRC", help="Only the owners of these SSRCs.")
    ] = []
    raised_hands: Annotated[
        bool, opt("--raised-hands", help="Only participants asking to speak.")
    ] = False
    video: Annotated[bool, opt("--video", help="Only participants publishing video.")] = False


async def participant_list(ctx: OpContext, req: ParticipantListReq) -> Page[GroupCallParticipant]:
    """List who is in a call.

    String-cursor pagination, seeded from `getGroupCall`'s
    `participants_next_offset` and stopped when `next_offset` comes back
    empty — re-sending an empty offset loops forever, which is the bug this
    implementation exists to not have.
    """
    from telethon.tl.functions import phone as fn

    limit = min(int(getattr(ctx, "limit", None) or 30), 200)
    token = getattr(ctx, "cursor", None)
    state = (
        decode_cursor(
            token, op="vc.participant.list", kind=PageKind.PARTICIPANTS, account=ctx.account
        )
        if token
        else {}
    )

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    ids = [await _send.resolve(ctx, reference) for reference in req.user]
    result = await _client(ctx)(
        fn.GetGroupParticipantsRequest(
            call=handle.input,
            ids=ids,
            sources=list(req.source),
            offset=str(state.get("offset", "") or ""),
            limit=limit,
        )
    )
    entities = _entities(result)
    items = [
        _participant_model(raw, entities) for raw in (getattr(result, "participants", None) or [])
    ]
    if req.raised_hands:
        items = sorted(
            (p for p in items if p.raise_hand),
            key=lambda p: p.raise_hand_rating or 0,
            reverse=True,
        )
    if req.video:
        items = [p for p in items if p.video or p.presentation]

    call = getattr(result, "call", None)
    if call is not None and getattr(call, "listeners_hidden", False):
        ctx.warn(
            "listeners are hidden in this call: the page carries publishers only, and "
            "participants_count is the real audience"
        )
    if getattr(call, "conference", False):
        for participant in items:
            participant.state = "joined" if not participant.left else "invited"

    next_offset = str(getattr(result, "next_offset", "") or "")
    return build_page(
        items,
        op="vc.participant.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


SPEC_PARTICIPANT_LIST = OperationSpec(
    id="vc.participant.list",
    request=ParticipantListReq,
    response=Page[GroupCallParticipant],
    impl=participant_list,
    aliases=("conference.participants", "vc.participants"),
    summary="List the participants of a video chat, livestream, live story or conference",
    description=(
        "The fourth conference state — present in the E2E chain with no media "
        "— needs a block parser tlgr does not have and is reported as null."
    ),
    paginated=PageKind.PARTICIPANTS,
    columns=("peer.id", "muted", "video", "raise_hand", "source"),
    headers=("Peer", "Muted", "Video", "Hand", "SSRC"),
    example={"items": [{"source": 1234567, "muted": True}], "has_more": False},
    example_args="vc participant list @newsroom",
    covers=(
        "conference.participant-states",
        "conference.participants",
        "groupcall.list-participants",
        "groupcall.participants-by-id",
    ),
)


class IdentityListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat with the call.")]
    comment: Annotated[
        bool, opt("--comment", help="The live-story comment-author list instead of join-as.")
    ] = False


async def identity_list(ctx: OpContext, req: IdentityListReq) -> Page[CallIdentity]:
    """Peers you may appear as: "Display as" and "Comment as".

    Two GUI pickers, two RPCs, one command. Live stories and conferences must
    be *joined* as yourself; only the comment author may differ, which is why
    the two lists are not interchangeable.
    """
    from telethon.tl.functions import channels as channels_fn
    from telethon.tl.functions import phone as fn

    peer = await _chat_peer(ctx, req.chat)
    client = _client(ctx)
    if req.comment:
        result = await client(channels_fn.GetSendAsRequest(peer=peer, for_live_stories=True))
        kind = "send-as"
        rows = getattr(result, "peers", None) or []
        raw_peers = [getattr(row, "peer", row) for row in rows]
        default = None
    else:
        result = await client(fn.GetGroupCallJoinAsRequest(peer=peer))
        kind = "join-as"
        raw_peers = list(getattr(result, "peers", None) or [])
        default = getattr(result, "default_peer", None)

    entities = _entities(result)
    default_id = getattr(default, "user_id", None) or getattr(default, "channel_id", None)
    items: list[CallIdentity] = []
    for raw in raw_peers:
        model = _peer_model(raw, entities)
        if model is None:  # pragma: no cover - the server sends real peers
            continue
        items.append(
            CallIdentity(
                peer=model,
                kind=kind,
                default=bool(default_id and model.raw_id == default_id),
                is_self=model.is_self,
            )
        )
    return Page(items=items, has_more=False, total=len(items))


SPEC_IDENTITY_LIST = OperationSpec(
    id="vc.identity.list",
    request=IdentityListReq,
    response=Page[CallIdentity],
    impl=identity_list,
    aliases=("vc.join-as.list", "vc.send-as.list"),
    summary="Peers you may appear as in a call ('Display as' / 'Comment as')",
    columns=("peer.id", "kind", "default"),
    headers=("Peer", "Kind", "Default"),
    example={"items": [{"kind": "join-as", "default": True}], "has_more": False},
    example_args="vc identity list @newsroom",
    covers=("groupcall.join-as-list", "groupcall.send-as-list"),
)


# ---------------------------------------------------------------------------
# vc rtmp get
# ---------------------------------------------------------------------------


class RtmpReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat or channel.")]
    live_story: Annotated[
        bool, opt("--live-story", help="Credentials for the peer's live story.")
    ] = False
    revoke: Annotated[
        bool, opt("--revoke", help="Rotate the key — this breaks any running encoder.")
    ] = False
    show_key: Annotated[bool, opt("--show-key", help="Print the key instead of masking it.")] = (
        False
    )
    key_file: Annotated[
        str | None,
        opt("--key-file", metavar="PATH", kind="path", help="Write the key to a 0600 file."),
    ] = None


async def rtmp_get(ctx: OpContext, req: RtmpReq) -> RtmpInfo:
    """Get or rotate the RTMP ingest URL and stream key.

    The key is a publishing credential and the obvious thing to do with CLI
    output is paste it into a bug report, so it is masked unless you asked for
    it in so many words. Fetch this *before* `vc create --rtmp`.
    """
    import os

    from telethon.tl.functions import phone as fn

    peer = await _chat_peer(ctx, req.chat)
    result = await _client(ctx)(
        fn.GetGroupCallStreamRtmpUrlRequest(
            peer=peer, revoke=bool(req.revoke), live_story=req.live_story or None
        )
    )
    key = str(getattr(result, "key", "") or "")
    info = RtmpInfo(
        url=str(getattr(result, "url", "") or ""),
        peer=Peer(id=_send.peer_id_of(peer), raw_id=abs(_send.peer_id_of(peer)), kind="channel"),
        live_story=req.live_story,
        revoked=req.revoke,
    )
    if req.key_file:
        handle = os.open(req.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(handle, key.encode())
        finally:
            os.close(handle)
        info.key_file = req.key_file
        info.key = "<written to file>"
    elif req.show_key:
        info.key, info.key_shown = key, True
    else:
        info.key = f"{key[:4]}…{key[-2:]}" if len(key) > 8 else "<hidden>"
        ctx.warn("the stream key is masked; pass --show-key or --key-file to get it")
    if req.revoke:
        ctx.warn("the old key is dead: any encoder still publishing with it has stopped")
    return info


SPEC_RTMP_GET = OperationSpec(
    id="vc.rtmp.get",
    request=RtmpReq,
    response=RtmpInfo,
    impl=rtmp_get,
    aliases=("story.live.rtmp",),
    summary="Get or rotate the RTMP ingest URL and stream key",
    description="Needs `manage_call`; revoking needs owner privileges.",
    mutating=True,
    rate_class="send",
    columns=("url", "key", "key_shown"),
    example={"url": "rtmps://dc4-1.rtmp.t.me/s/", "key": "abcd…yz", "key_shown": False},
    example_args="vc rtmp get @newsroom",
    covers=(
        "groupcall.rtmp-get-url",
        "groupcall.rtmp-revoke-key",
        "livestory.rtmp-revoke",
        "stories.live-rtmp-url",
    ),
)


# ---------------------------------------------------------------------------
# vc send / message delete
# ---------------------------------------------------------------------------


class SendReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    text: Annotated[str, arg(1, metavar="TEXT", help="What to say, or the reaction emoji.")]
    custom_emoji: Annotated[
        int | None,
        opt("--custom-emoji", metavar="ID", help="Send a custom emoji reaction (Premium)."),
    ] = None
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Comment as a channel (live stories)."),
    ] = None
    remember: Annotated[bool, opt("--remember", help="Store --send-as as the default.")] = False
    parse: Annotated[str | None, choice("md", "html", "none", help="Text formatting.")] = "md"
    stars: Annotated[
        int | None, opt("--stars", metavar="N", help="Donate Stars to highlight it (at least 1).")
    ] = None
    confirm_stars: Annotated[
        bool, opt("--confirm-stars", help="Required with --stars: it spends real Stars.")
    ] = False


async def send(ctx: OpContext, req: SendReq) -> InCallMessage:
    """Send an in-call message or emoji reaction.

    Reactions are not a separate API: a standard one is a message whose text
    is the emoji, a custom one is fallback text plus a single custom-emoji
    entity — hence `vc react` as an alias rather than a second command. The
    overlay has no history and no fetch method at all, so `vc watch` is the
    only way to read anyone else's.

    `--stars` spends the account's Star balance, so it is never implicit:
    an explicit amount *and* `--confirm-stars` are both required.
    """
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    if req.stars and not req.confirm_stars:
        raise UsageError(
            f"--stars {req.stars} spends real Stars; add --confirm-stars to mean it",
            field="stars",
        )

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    call, entities = await _fetch_call(ctx, handle)
    if not getattr(call, "messages_enabled", False):
        raise PermissionError_(
            "in-call messages are off for this call (`vc set CALL --messages on` turns "
            "them on, with manage_call)"
        )

    config = await _calls.app_config(ctx)
    cap = int(config.get("group_call_message_length_limit", DEFAULT_MESSAGE_LENGTH) or 0)
    text, entity_models = _send.body(req.text, parse=req.parse)
    if cap and len(text.encode()) > cap:
        raise UsageError(f"an in-call message is at most {cap} characters", field="text")

    tl_entities = _send.tl_entities(entity_models) or []
    if req.custom_emoji is not None:
        from tlgr.core.text import utf16_len

        tl_entities = [
            types.MessageEntityCustomEmoji(
                offset=0, length=utf16_len(text), document_id=req.custom_emoji
            )
        ]

    send_as: Any = None
    if req.send_as is not None:
        send_as = await _send.resolve(ctx, req.send_as)

    result = await _client(ctx)(
        fn.SendGroupCallMessageRequest(
            call=handle.input,
            message=types.TextWithEntities(text=text, entities=tl_entities),
            random_id=secrets.randbits(63),
            allow_paid_stars=req.stars,
            send_as=send_as,
        )
    )
    if req.remember and send_as is not None:
        await _client(ctx)(fn.SaveDefaultSendAsRequest(call=handle.input, send_as=send_as))

    msg_id = 0
    date: Any = None
    for update in getattr(result, "updates", None) or []:
        message = getattr(update, "message", None)
        if message is not None:
            msg_id = int(getattr(message, "id", 0) or 0) or msg_id
            date = getattr(message, "date", None) or date
    ttl = int(config.get("group_call_message_ttl", DEFAULT_MESSAGE_TTL) or DEFAULT_MESSAGE_TTL)
    ctx.emit("vc_message", {"call_id": handle.ref.id, "text": text})
    return InCallMessage(
        call=handle.ref,
        msg_id=msg_id,
        from_id=_peer_model(getattr(call, "default_send_as", None), entities),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        text=text,
        paid_message_stars=req.stars,
        ttl=ttl,
    )


SPEC_SEND = OperationSpec(
    id="vc.send",
    request=SendReq,
    response=InCallMessage,
    impl=send,
    aliases=("story.live.comment", "vc.react"),
    summary="Send an in-call message or emoji reaction (live-story comments included)",
    description=(
        "The overlay lives about ten seconds and has no history method; run "
        "`vc watch --messages` to read the other side."
    ),
    mutating=True,
    rate_class="send",
    columns=("call.id", "msg_id", "text", "ttl"),
    example={"call": _EXAMPLE_REF, "msg_id": 7, "text": "🔥", "ttl": 10},
    example_args='vc send @newsroom "hello"',
    covers=(
        "groupcall.save-default-send-as",
        "groupcall.send-message",
        "groupcall.send-reaction",
        "stories.live-comments",
        "stories.live-highlight-comment",
        "stories.live-message-sender",
    ),
    tags=frozenset({"visible-to-others"}),
)


class MessageDeleteReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    id: Annotated[
        list[int],
        arg(1, metavar="ID", required=False, variadic=True, help="In-call message ids."),
    ] = []
    from_peer: Annotated[
        PeerRef | None,
        opt("--from", metavar="PEER", kind="peer", help="Delete everything this peer said."),
    ] = None
    report_spam: Annotated[
        bool, opt("--report-spam", help="Report the deleted messages as spam.")
    ] = False


async def message_delete(ctx: OpContext, req: MessageDeleteReq) -> InCallMessagesDeleted:
    """Delete in-call messages, or every message from one participant.

    Two RPCs, picked by whether `--from` is given; deletions reach everyone as
    `updateDeleteGroupCallMessages`. `--report-spam` only means anything when
    moderating somebody else's messages.
    """
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    client = _client(ctx)

    if req.from_peer is not None:
        participant = await _send.resolve(ctx, req.from_peer)
        await client(
            fn.DeleteGroupCallParticipantMessagesRequest(
                call=handle.input, participant=participant, report_spam=req.report_spam or None
            )
        )
        marked = _send.peer_id_of(participant)
        return InCallMessagesDeleted(
            call=handle.ref,
            participant=Peer(id=marked, raw_id=abs(marked), kind="user"),
            reported=req.report_spam,
        )

    if not req.id:
        raise UsageError("give message ids, or --from to clear one participant", field="id")
    await client(
        fn.DeleteGroupCallMessagesRequest(
            call=handle.input, messages=list(req.id), report_spam=req.report_spam or None
        )
    )
    return InCallMessagesDeleted(call=handle.ref, deleted=list(req.id), reported=req.report_spam)


SPEC_MESSAGE_DELETE = OperationSpec(
    id="vc.message.delete",
    request=MessageDeleteReq,
    response=InCallMessagesDeleted,
    impl=message_delete,
    aliases=("story.live.moderate",),
    summary="Delete in-call messages, or every message from one participant",
    description="Your own messages always; anyone else's needs call moderation rights.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("call.id", "deleted", "reported"),
    example={"call": _EXAMPLE_REF, "deleted": [7], "reported": False},
    example_args="vc message delete @newsroom 7",
    covers=(
        "groupcall.delete-own-message",
        "groupcall.delete-participant-messages",
        "groupcall.report-message-spam",
    ),
)


# ---------------------------------------------------------------------------
# vc download
# ---------------------------------------------------------------------------


class DownloadReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    out: Annotated[str, opt("--out", metavar="PATH", kind="path", help="Output file.")] = ""
    quality: Annotated[
        int, opt("--quality", metavar="0|1|2", help="Video quality: 0 lowest.", ge=0, le=2)
    ] = 1
    channel: Annotated[
        int | None, opt("--channel", metavar="N", help="Video channel; omit for audio only.")
    ] = None
    audio_only: Annotated[bool, opt("--audio-only", help="Fetch only the audio segments.")] = False
    since: Annotated[
        str, opt("--since", metavar="live|MS", help="Start at the live edge or a timestamp.")
    ] = "live"
    duration: Annotated[
        int,
        opt("--duration", metavar="DURATION", kind="duration", help="Stop after this much media."),
    ] = 30
    scale: Annotated[int, opt("--scale", metavar="N", help="Segment scale; 0 is 1000ms.", ge=0)] = 0


async def _stream_call(client: Any, dc_id: int | None) -> tuple[Any, Any]:
    """`(send, release)` for the media DC a livestream is served from.

    Telethon's exported-sender API is private, so it is used through `getattr`
    and falls back to the ordinary client: a fallback that fetches from the
    wrong DC fails loudly, which is better than a chunk loop that silently
    never starts.
    """
    borrow = getattr(client, "_borrow_exported_sender", None)
    call = getattr(client, "_call", None)
    if not dc_id or borrow is None or call is None:

        async def direct(request: Any) -> Any:
            return await client(request)

        async def noop() -> None:
            return None

        return direct, noop

    sender = await borrow(dc_id)

    async def send(request: Any) -> Any:
        return await call(sender, request)

    async def release() -> None:
        release_fn = getattr(client, "_return_exported_sender", None)
        if release_fn is not None:
            await release_fn(sender)

    return send, release


async def download(ctx: OpContext, req: DownloadReq) -> StreamDownload:
    """Archive a livestream to disk, one 1 MB chunk at a time.

    tlgr cannot play a livestream and can record one. Chunks are
    `upload.getFile` against `inputGroupCallStream`, sent to the call's
    `stream_dc_id`; a `TIME_TOO_BIG` or a flood wait means the chunk is not
    ready yet, so the same one is retried rather than skipped — skipping is
    how a recording ends up with holes in it.
    """
    from pathlib import Path

    from telethon.tl import types
    from telethon.tl.functions import phone as fn
    from telethon.tl.functions import upload as upload_fn

    if req.out in ("", "-"):
        raise UsageError(
            "give --out PATH: the recording is written by the daemon, which has no "
            "access to your terminal's stdout",
            field="out",
        )

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    call, _ = await _fetch_call(ctx, handle)
    dc_id = getattr(call, "stream_dc_id", None)

    channels = await _client(ctx)(fn.GetGroupCallStreamChannelsRequest(call=handle.input))
    rows = list(getattr(channels, "channels", None) or [])
    if not rows:
        raise NotFoundError("this call publishes no stream channels yet; the publisher is idle")
    wanted = rows[0]
    for row in rows:
        if req.channel is not None and int(getattr(row, "channel", 0) or 0) == req.channel:
            wanted = row
    scale = int(getattr(wanted, "scale", req.scale) or req.scale)
    segment_ms = 1000 >> scale

    if req.since == "live":
        time_ms = int(getattr(wanted, "last_timestamp_ms", 0) or 0)
    else:
        try:
            time_ms = int(req.since)
        except ValueError as exc:
            raise UsageError(
                "--since wants `live` or a chunk timestamp in ms", field="since"
            ) from exc

    send, release = await _stream_call(_client(ctx), dc_id)
    written = 0
    chunks = 0
    first_ms = time_ms
    deadline = max(1, req.duration)
    target = Path(req.out)
    try:
        with target.open("wb") as handle_out:
            while chunks * segment_ms < deadline * 1000:
                location = types.InputGroupCallStream(
                    call=handle.input,
                    time_ms=time_ms,
                    scale=scale,
                    video_channel=None if req.audio_only else req.channel,
                    video_quality=None if req.audio_only else req.quality,
                )
                try:
                    result = await send(
                        upload_fn.GetFileRequest(location=location, offset=0, limit=1048576)
                    )
                except Exception as exc:
                    text = f"{type(exc).__name__} {exc}".upper().replace("_", "")
                    if "TIMETOOBIG" in text or "FLOODWAIT" in text:
                        await asyncio.sleep(0.1)
                        continue
                    raise
                payload = bytes(getattr(result, "bytes", b"") or b"")
                if not payload:
                    await asyncio.sleep(0.1)
                    time_ms += segment_ms
                    continue
                handle_out.write(payload)
                written += len(payload)
                chunks += 1
                time_ms += segment_ms
    finally:
        await release()

    ctx.warn("recorded, not played: tlgr writes the segments and decodes nothing")
    return StreamDownload(
        call=handle.ref,
        out=str(target),
        bytes=written,
        chunks=chunks,
        mode="rtmp" if getattr(call, "rtmp_stream", False) else "stream",
        stream_dc_id=dc_id,
        first_time_ms=first_ms,
        last_time_ms=time_ms,
    )


SPEC_DOWNLOAD = OperationSpec(
    id="vc.download",
    request=DownloadReq,
    response=StreamDownload,
    impl=download,
    summary="Archive a livestream or live story to disk (stream-mode chunks)",
    description=(
        "Join the call first (`vc join --listen-only`): chunk fetches fail "
        "with GROUPCALL_JOIN_MISSING otherwise."
    ),
    mutating=True,
    rate_class="file",
    timeout_s=900,
    columns=("call.id", "out", "bytes", "chunks", "mode"),
    example={"call": _EXAMPLE_REF, "out": "/tmp/stream.ogg", "bytes": 1048576, "chunks": 1},
    example_args="vc download @newsroom --out /tmp/stream.ogg",
    covers=("groupcall.download-stream", "livestory.join-as-viewer"),
    covers_partial=("stories.live-join",),
    coverage_note="watching a live story as a viewer is owned by `story live get`",
)


# ---------------------------------------------------------------------------
# vc watch
# ---------------------------------------------------------------------------


class WatchReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A chat, id:access_hash or call link.")]
    messages: Annotated[bool, opt("--messages", help="Include in-call messages.")] = True
    participants: Annotated[
        bool, opt("--participants", help="Include participant joins, leaves and mutes.")
    ] = True
    idle_timeout: Annotated[
        int,
        opt("--idle-timeout", metavar="DURATION", kind="duration", help="Give up after silence."),
    ] = 3600


async def watch(ctx: OpContext, req: WatchReq) -> Any:
    """Stream live call state: participants, mutes, in-call messages, connection.

    Implements the documented version-gap rule: `updateGroupCall` and
    `updateGroupCallParticipants` carry a monotonically increasing version and
    must be applied in order, so a gap emits a `resync` record instead of a
    silently reordered state. This is also the *only* way to read in-call
    messages — they are not part of the chat history and no method fetches
    them.
    """
    from telethon import events
    from telethon.tl import types

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    client = _client(ctx)
    queue: asyncio.Queue[GroupCallEvent] = asyncio.Queue()
    seen = {"version": 0}

    def _same_call(update: Any) -> bool:
        call = getattr(update, "call", None)
        return int(getattr(call, "id", 0) or 0) in (0, handle.ref.id)

    async def handler(update: Any) -> None:
        name = type(update).__name__
        if not _same_call(update):
            return
        version = int(getattr(update, "version", 0) or 0)
        resync = bool(version and seen["version"] and version > seen["version"] + 1)
        if version:
            seen["version"] = max(seen["version"], version)

        if name == "UpdateGroupCall":
            call = getattr(update, "call", None)
            await queue.put(
                GroupCallEvent(
                    kind="call.state",
                    at=_now(),
                    call=_calls.call_ref_of(call),
                    version=version or None,
                    resync=resync,
                )
            )
        elif name == "UpdateGroupCallParticipants" and req.participants:
            entities = _entities(update)
            await queue.put(
                GroupCallEvent(
                    kind="participants",
                    at=_now(),
                    call=handle.ref,
                    version=version or None,
                    participants=[
                        _participant_model(raw, entities)
                        for raw in (getattr(update, "participants", None) or [])
                    ],
                    resync=resync,
                )
            )
        elif name == "UpdateGroupCallConnection":
            import json

            params = getattr(update, "params", None)
            payload: dict[str, Any] = {}
            with contextlib.suppress(ValueError):
                payload = json.loads(getattr(params, "data", "") or "{}")
            await queue.put(
                GroupCallEvent(
                    kind="connection", at=_now(), call=handle.ref, connection=payload or None
                )
            )
        elif name == "UpdateGroupCallMessage" and req.messages:
            message = getattr(update, "message", None)
            await queue.put(
                GroupCallEvent(
                    kind="message",
                    at=_now(),
                    call=handle.ref,
                    message=InCallMessage(
                        call=handle.ref,
                        msg_id=int(getattr(message, "id", 0) or 0),
                        text=str(getattr(getattr(message, "message", None), "text", "") or ""),
                        date=fmt_dt(getattr(message, "date", None)),
                    ),
                )
            )
        elif name == "UpdateDeleteGroupCallMessages" and req.messages:
            await queue.put(
                GroupCallEvent(
                    kind="message-deleted",
                    at=_now(),
                    call=handle.ref,
                    blocks=[str(i) for i in (getattr(update, "messages", None) or [])],
                )
            )
        elif name == "UpdateGroupCallEncryptedMessage" and req.messages:
            await queue.put(
                GroupCallEvent(
                    kind="message-encrypted",
                    at=_now(),
                    call=handle.ref,
                    encrypted=_calls.b64(bytes(getattr(update, "encrypted_message", b"") or b"")),
                )
            )
        elif name == "UpdateGroupCallChainBlocks":
            await queue.put(
                GroupCallEvent(
                    kind="chain",
                    at=_now(),
                    call=handle.ref,
                    blocks=[_calls.b64(b) for b in (getattr(update, "blocks", None) or [])],
                )
            )

    wanted = [
        types.UpdateGroupCall,
        types.UpdateGroupCallParticipants,
        types.UpdateGroupCallConnection,
        types.UpdateGroupCallChainBlocks,
    ]
    for name in (
        "UpdateGroupCallMessage",
        "UpdateDeleteGroupCallMessages",
        "UpdateGroupCallEncryptedMessage",
    ):
        found = getattr(types, name, None)
        if found is not None:
            wanted.append(found)

    builder = events.Raw(types=wanted)
    client.add_event_handler(handler, builder)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=max(1, req.idle_timeout))
            except (TimeoutError, asyncio.TimeoutError):
                yield Page(items=[], has_more=False)
                return
            yield Page(items=[event], has_more=True)
    finally:
        client.remove_event_handler(handler, builder)


SPEC_WATCH = OperationSpec(
    id="vc.watch",
    request=WatchReq,
    response=Page[GroupCallEvent],
    impl=watch,
    summary="Stream live call state: participants, mutes, in-call messages, connection changes",
    description=(
        "Conference messages arrive encrypted and are emitted as opaque "
        "blobs: decrypting them needs the E2E key tlgr cannot derive."
    ),
    stream=True,
    columns=("kind", "call.id", "version"),
    example={"items": [{"kind": "participants", "at": "2026-09-03T09:14:07Z"}]},
    example_args="vc watch @newsroom",
    covers=("groupcall.watch-in-call-chat", "groupcall.watch-updates"),
)


def _read_bytes(path: str, *, field: str) -> bytes:
    from pathlib import Path

    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise UsageError(f"--{field}: {exc.strerror or exc}", field=field) from exc


def _read_text(path: str, *, field: str) -> str:
    from pathlib import Path

    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"--{field}: {exc.strerror or exc}", field=field) from exc
