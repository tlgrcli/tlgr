"""The `call` group: 1:1 voice and video calls, as signalling.

The honest summary of what this group is, stated once here and repeated in
every answer it gives: **tlgr can ring, answer, hang up, rate and observe a
call, and it cannot talk.** There is no tgcalls binding behind it, so a call
tlgr accepts is a silent one, `media` is always `"none"`, and the parts of a
call that are genuinely media — turning the camera on, the audio itself — are
reachable only as opaque signalling packets somebody else's engine produced
(`call signal`).

What is left is more useful than it sounds. The key exchange, the ringing
state machine, the call log, the quality rating and the incoming-call stream
are all pure control plane, which means a headless machine can answer "is my
phone ringing, and who is it" — and can do it in a script.

Telethon is imported inside functions, never at module scope: importing the
registry is what builds `tlgr --help`, and that must not pull in Telethon.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from tlgr.core.errors import (
    IndeterminateError,
    NotFoundError,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.call import (
    MEDIA_NONE,
    Call,
    CallConfig,
    CallDebugUpload,
    CallDeclined,
    CallEnded,
    CallEvent,
    CallLogDeleted,
    CallLogEntry,
    CallRating,
    CallSignal,
    CallUpgrade,
)
from tlgr.models.page import Page
from tlgr.models.peer import Peer, PeerRef
from tlgr.ops import _calls, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The nine problem ids the official clients offer. Validated locally and
#: appended to the rating comment as hashtags, which is the whole protocol.
RATING_PROBLEMS = (
    "echo",
    "noise",
    "interruptions",
    "distorted_speech",
    "silent_local",
    "silent_remote",
    "dropped",
    "distorted_video",
    "pixelated_video",
)

_EXAMPLE_CALL: dict[str, Any] = {
    "call_id": 4815162342,
    "state": "waiting",
    "media": "none",
    "video": False,
    "out": True,
}

#: Background auto-discard timers, held so the event loop does not collect a
#: task nobody is awaiting.
_TIMERS: set[asyncio.Task[None]] = set()


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


def _now() -> str:
    return fmt_dt(datetime.now(timezone.utc)) or ""


def _peer_of(ctx: OpContext, user_id: int | None) -> Peer | None:
    """The other side of a call, as far as the entity cache knows it."""
    if not user_id:
        return None
    session = getattr(ctx, "session", None)
    entity = getattr(session, "me", None)
    if entity is not None and getattr(entity, "id", None) == user_id:
        return entity_to_peer(entity)
    return Peer(id=user_id, raw_id=user_id, kind="user")


def _live_model(ctx: OpContext, live: _calls.LiveCall, *, fingerprint: bool = False) -> Call:
    """What the daemon remembers about a call, as the wire shape."""
    model = Call(
        call_id=live.id,
        state=live.state,
        media=MEDIA_NONE,
        video=live.video,
        out=live.out,
        access_hash=live.access_hash,
        admin_id=live.admin_id,
        participant_id=live.participant_id,
        peer=_peer_of(ctx, live.peer_id),
        conference_supported=live.conference_supported,
        connections=live.connections,
        protocol=_calls.protocol_dict(),
        need_rating=live.need_rating,
        need_debug=live.need_debug,
        reason=live.reason,
        duration=live.duration,
    )
    if fingerprint and live.key and live.g_a:
        indices = _calls.emoji_indices(live.key, live.g_a)
        model.fingerprint = [f"#{index}" for index in indices]
    return model


def _absorb(ctx: OpContext, phone_call: Any, *, out: bool = False) -> _calls.LiveCall:
    """Record a `phoneCall*` constructor into the live-call store."""
    live = _calls.LiveCall(
        id=int(getattr(phone_call, "id", 0) or 0),
        access_hash=int(getattr(phone_call, "access_hash", 0) or 0),
        state=_calls.state_name(phone_call),
        video=bool(getattr(phone_call, "video", False)),
        out=out,
        admin_id=getattr(phone_call, "admin_id", None),
        participant_id=getattr(phone_call, "participant_id", None),
        conference_supported=getattr(phone_call, "conference_supported", None),
        connections=len(getattr(phone_call, "connections", None) or []),
        need_rating=getattr(phone_call, "need_rating", None),
        need_debug=getattr(phone_call, "need_debug", None),
        reason=_calls.reason_name(getattr(phone_call, "reason", None)),
        duration=getattr(phone_call, "duration", None),
    )
    me = getattr(getattr(ctx, "session", None), "me", None)
    my_id = int(getattr(me, "id", 0) or 0)
    admin = live.admin_id or 0
    participant = live.participant_id or 0
    if my_id:
        live.out = admin == my_id
        live.peer_id = participant if admin == my_id else admin
    elif participant:
        live.peer_id = participant
    return _calls.remember_call(ctx.account, live)


def _group_call_of(result: Any) -> Any:
    """The `groupCall` an `Updates` carries in its `updateGroupCall`."""
    direct = getattr(result, "call", None)
    if direct is not None and type(direct).__name__.startswith("GroupCall"):
        return direct
    for update in getattr(result, "updates", None) or []:
        call = getattr(update, "call", None)
        if call is not None and type(call).__name__.startswith("GroupCall"):
            return call
    return None


def _phone_call_of(result: Any) -> Any:
    """The `phoneCall*` inside a `phone.PhoneCall` or an `Updates`."""
    direct = getattr(result, "phone_call", None)
    if direct is not None:
        return direct
    for update in getattr(result, "updates", None) or []:
        found = getattr(update, "phone_call", None)
        if found is not None:
            return found
    return None


async def _dh_parameters(ctx: OpContext) -> tuple[int, int, bytes]:
    """`(p, g, server random)` — validated, never trusted as sent."""
    from telethon.tl.functions import messages as fn

    config = await _client(ctx)(fn.GetDhConfigRequest(version=0, random_length=256))
    p_bytes = bytes(getattr(config, "p", b"") or b"")
    g = int(getattr(config, "g", 0) or 0)
    if not p_bytes or not g:
        raise IndeterminateError(
            "the server did not send DH parameters, so no call key exchange could start"
        )
    checks = _calls.dh_verdict(p_bytes, g)
    if not checks["ok"]:
        raise IndeterminateError(
            "the DH parameters the server sent did not validate "
            f"({checks}); no call was placed, and this is not a refusal by the peer"
        )
    return int.from_bytes(p_bytes, "big"), g, bytes(getattr(config, "random", b"") or b"")


def _secret(p: int, g: int, server_random: bytes) -> tuple[int, bytes]:
    """A fresh exponent and its `g^x mod p`, mixed with the server's entropy."""
    local = secrets.token_bytes(256)
    mixed = bytes(a ^ b for a, b in zip(local, server_random.ljust(256, b"\0"), strict=False))
    x = int.from_bytes(mixed, "big") % (p - 2) + 1
    return x, pow(g, x, p).to_bytes(256, "big")


# ---------------------------------------------------------------------------
# call start
# ---------------------------------------------------------------------------


class StartReq(Request):
    user: Annotated[
        PeerRef | None,
        arg(0, metavar="USER", required=False, kind="user", help="Who to ring."),
    ] = None
    video: Annotated[bool, opt("--video", help="Ring as a video call.")] = False
    from_message: Annotated[
        str | None,
        opt(
            "--from-message",
            metavar="CHAT:MSG_ID",
            help="Redial the peer of a call log row, or a shared contact card.",
        ),
    ] = None
    check: Annotated[
        bool,
        opt("--check", help="Only report whether the peer can be called. Nothing rings."),
    ] = False
    wait: Annotated[bool, opt("--wait", help="Block until the call is answered or discarded.")] = (
        False
    )
    wait_timeout: Annotated[
        int, opt("--wait-timeout", metavar="DURATION", kind="duration", help="Cap on --wait.")
    ] = 60
    auto_discard: Annotated[
        int,
        opt(
            "--auto-discard",
            metavar="DURATION",
            kind="duration",
            help="Hang up automatically after this long; 0 leaves the call ringing.",
        ),
    ] = 60


async def _redial_target(ctx: OpContext, reference: str) -> PeerRef:
    """The user behind a call log row or a shared contact card."""
    from tlgr.models.peer import parse_message_link, parse_peer_ref

    link = parse_message_link(reference)
    if link is not None:
        chat_ref, msg_id = link
    else:
        chat, _, tail = reference.rpartition(":")
        if not chat or not tail.isdigit():
            raise UsageError(
                "--from-message wants CHAT:MSG_ID or a t.me message link", field="from_message"
            )
        chat_ref, msg_id = parse_peer_ref(chat), int(tail)

    peer = await _send.resolve(ctx, chat_ref)
    message = await _client(ctx).get_messages(peer, ids=msg_id)
    if message is None:
        raise NotFoundError(f"message {msg_id} is not there to redial")

    media = getattr(message, "media", None)
    phone = getattr(getattr(media, "user_id", None) and media, "phone_number", None)
    if phone:
        from telethon.tl.functions import contacts as contacts_fn

        resolved = await _client(ctx)(contacts_fn.ResolvePhoneRequest(phone=str(phone)))
        users = getattr(resolved, "users", None) or []
        if users:
            return parse_peer_ref(str(users[0].id))
    sender = getattr(message, "from_id", None) or getattr(message, "peer_id", None)
    user_id = getattr(sender, "user_id", None)
    if not user_id:
        raise UsageError("that message does not name a user to call", field="from_message")
    return parse_peer_ref(str(user_id))


async def _availability(ctx: OpContext, user: Any) -> Call:
    """`--check`: the three flags the GUI uses to grey out the call buttons."""
    from telethon.tl.functions import users as users_fn

    full = await _client(ctx)(users_fn.GetFullUserRequest(user))
    inner = getattr(full, "full_user", None)
    entity = (getattr(full, "users", None) or [None])[0]
    return Call(
        call_id=0,
        state="checked",
        media=MEDIA_NONE,
        peer=entity_to_peer(entity) if entity is not None else None,
        can_call=bool(getattr(inner, "phone_calls_available", False)),
        can_video_call=bool(getattr(inner, "video_calls_available", False)),
        private=bool(getattr(inner, "phone_calls_private", False)),
    )


def _arm_auto_discard(ctx: OpContext, call_id: int, seconds: int) -> None:
    """Hang up a call that nobody answered, so automation leaves nothing open."""

    async def timer() -> None:
        from telethon.tl import types
        from telethon.tl.functions import phone as fn

        await asyncio.sleep(seconds)
        live = _calls.live_calls(ctx.account).get(call_id)
        if live is None or live.state in ("active", "discarded") or live.discarded:
            return
        live.discarded = True
        with contextlib.suppress(Exception):
            await _client(ctx)(
                fn.DiscardCallRequest(
                    peer=types.InputPhoneCall(id=live.id, access_hash=live.access_hash),
                    duration=0,
                    reason=types.PhoneCallDiscardReasonMissed(),
                    connection_id=0,
                )
            )
        live.state = "discarded"
        live.reason = "missed"

    task = asyncio.get_event_loop().create_task(timer())
    _TIMERS.add(task)
    task.add_done_callback(_TIMERS.discard)


async def _wait_for(ctx: OpContext, call_id: int, seconds: int) -> Any:
    """Wait for the next `updatePhoneCall` about *call_id*."""
    from telethon import events
    from telethon.tl import types

    client = _client(ctx)
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def handler(update: Any) -> None:
        if type(update).__name__ != "UpdatePhoneCall":
            return
        call = getattr(update, "phone_call", None)
        if getattr(call, "id", None) == call_id:
            await queue.put(call)

    builder = events.Raw(types=[types.UpdatePhoneCall])
    client.add_event_handler(handler, builder)
    try:
        return await asyncio.wait_for(queue.get(), timeout=max(1, seconds))
    except (TimeoutError, asyncio.TimeoutError):
        return None
    finally:
        client.remove_event_handler(handler, builder)


async def start(ctx: OpContext, req: StartReq) -> Call:
    """Ring a user. Signalling only: the callee hears silence.

    tlgr drives the whole documented handshake — a validated DH prime,
    `g_a_hash` in `requestCall`, `confirmCall` with the real `g_a` once the
    peer answers — because a client that skips it is not placing a call, it is
    asking the server to place one for it. What it does not have is an audio
    engine, which is why the answer says `media: none` rather than implying
    otherwise.
    """
    from telethon import utils
    from telethon.tl.functions import phone as fn

    reference = req.user
    if req.from_message:
        reference = await _redial_target(ctx, req.from_message)
    if reference is None:
        raise UsageError("give a user to call, or --from-message to redial", field="user")

    peer = await _send.resolve(ctx, reference)
    try:
        user = utils.get_input_user(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError("only a user can be called", field="user") from exc

    if req.check:
        return await _availability(ctx, user)

    p, g, server_random = await _dh_parameters(ctx)
    a, g_a = _secret(p, g, server_random)

    result = await _client(ctx)(
        fn.RequestCallRequest(
            user_id=user,
            g_a_hash=hashlib.sha256(g_a).digest(),
            protocol=_calls.protocol(),
            video=req.video or None,
            random_id=secrets.randbits(31),
        )
    )
    phone_call = _phone_call_of(result)
    if phone_call is None:
        raise IndeterminateError("the server accepted the call request without describing the call")

    live = _absorb(ctx, phone_call, out=True)
    live.a, live.p, live.g, live.g_a = a, p, g, g_a
    ctx.emit("call_started", {"call_id": live.id, "video": live.video})

    if req.wait:
        answered = await _wait_for(ctx, live.id, req.wait_timeout)
        if answered is not None:
            live = _absorb(ctx, answered, out=True)
            g_b = bytes(getattr(answered, "g_b", b"") or b"")
            if g_b and live.state == "accepted":
                key = pow(int.from_bytes(g_b, "big"), a, p).to_bytes(256, "big")
                confirmed = await _client(ctx)(
                    fn.ConfirmCallRequest(
                        peer=_calls_input(live),
                        g_a=g_a,
                        key_fingerprint=_calls.key_fingerprint(key),
                        protocol=_calls.protocol(),
                    )
                )
                final = _phone_call_of(confirmed)
                if final is not None:
                    live = _absorb(ctx, final, out=True)
                live.key, live.a, live.p, live.g, live.g_a = key, a, p, g, g_a
    elif req.auto_discard:
        _arm_auto_discard(ctx, live.id, req.auto_discard)

    model = _live_model(ctx, live)
    if not req.wait and req.auto_discard:
        ctx.warn(f"the call is discarded automatically in {req.auto_discard}s")
    return model


def _calls_input(live: _calls.LiveCall) -> Any:
    from telethon.tl import types

    return types.InputPhoneCall(id=live.id, access_hash=live.access_hash)


SPEC_START = OperationSpec(
    id="call.start",
    request=StartReq,
    response=Call,
    impl=start,
    summary="Ring a user, voice or video — signalling only, no audio",
    description=(
        "tlgr runs the real key exchange and reports the relay list a media "
        "engine would use, but it has no media engine: `media` is always "
        "`none` and the callee hears silence. `--auto-discard` exists so "
        "automation cannot leave a call ringing forever."
    ),
    mutating=True,
    rate_class="send",
    timeout_s=180,
    columns=("call_id", "state", "video", "media"),
    example=_EXAMPLE_CALL,
    example_args="call start @alice",
    covers=(
        "calls.availability-flags",
        "calls.call-shared-contact",
        "calls.confirm-handshake",
        "calls.place-video-call",
        "calls.place-voice-call",
        "calls.redial",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# call accept
# ---------------------------------------------------------------------------


class AcceptReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    ack_only: Annotated[
        bool,
        opt("--ack-only", help="Only mark the call received (busy-lock), do not answer."),
    ] = False
    no_ack: Annotated[
        bool,
        opt("--no-ack", help="Skip the implicit receivedCall so several calls may ring."),
    ] = False


async def accept(ctx: OpContext, req: AcceptReq) -> Call:
    """Answer an incoming call, or only mark it received.

    The implicit `receivedCall` is the busy-lock every client sends first:
    without it a second caller gets a ring instead of "busy". `--no-ack` is
    for the case a bot deliberately wants several calls ringing at once,
    which the API documents as legitimate.
    """
    from telethon.tl.functions import phone as fn

    peer, known = _calls.input_phone_call(ctx, req.call)
    if not req.no_ack:
        await _client(ctx)(fn.ReceivedCallRequest(peer=peer))
    if req.ack_only:
        if known is None:  # pragma: no cover - input_phone_call refuses first
            raise NotFoundError(f"call {peer.id} is not one this daemon has seen")
        known.state = "requested"
        ctx.emit("call_received", {"call_id": peer.id})
        return _live_model(ctx, known)

    p, g, server_random = await _dh_parameters(ctx)
    b, g_b = _secret(p, g, server_random)
    result = await _client(ctx)(
        fn.AcceptCallRequest(peer=peer, g_b=g_b, protocol=_calls.protocol())
    )
    phone_call = _phone_call_of(result)
    live = _absorb(ctx, phone_call) if phone_call is not None else known
    if live is None:  # pragma: no cover - the server always answers with a call
        raise IndeterminateError("the server accepted the answer without describing the call")
    live.a, live.p, live.g, live.g_a = b, p, g, g_b
    ctx.emit("call_accepted", {"call_id": live.id})
    ctx.warn("tlgr answered the signalling; there is no audio engine behind it")
    return _live_model(ctx, live)


SPEC_ACCEPT = OperationSpec(
    id="call.accept",
    request=AcceptReq,
    response=Call,
    impl=accept,
    summary="Answer an incoming call (signalling only), or just mark it received",
    description=(
        "Accepting without an audio engine leaves a silent call — useful for "
        "automation and testing, useless for talking."
    ),
    mutating=True,
    rate_class="send",
    columns=("call_id", "state", "video", "media"),
    example={**_EXAMPLE_CALL, "state": "accepted", "out": False},
    example_args="call accept 4815162342",
    covers=("calls.accept-call", "calls.mark-received-busy"),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# call decline
# ---------------------------------------------------------------------------


class DeclineReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    reason: Annotated[str, choice("missed", "busy", help="Discard reason sent to the caller.")] = (
        "missed"
    )
    reply: Annotated[
        str | None,
        opt("--reply", metavar="TEXT", help="Also send this message to the caller."),
    ] = None


async def decline(ctx: OpContext, req: DeclineReq) -> CallDeclined:
    """Refuse a call, optionally with the text reply the GUI offers.

    Two operations, exactly as in the official clients: the discard, and then
    an ordinary message. There is no "decline with text" RPC.
    """
    from telethon.tl.functions import phone as fn

    peer, known = _calls.input_phone_call(ctx, req.call)
    await _client(ctx)(
        fn.DiscardCallRequest(
            peer=peer,
            duration=0,
            reason=_calls.discard_reason(req.reason),
            connection_id=0,
        )
    )
    if known is not None:
        known.state = "discarded"
        known.reason = req.reason

    reply_id: int | None = None
    if req.reply:
        caller = known.peer_id if known is not None else None
        if not caller:
            raise UsageError(
                "--reply needs to know who called; run `tlgr call watch` so the "
                "daemon holds the call",
                field="reply",
            )
        sent = await _client(ctx).send_message(caller, req.reply)
        reply_id = int(getattr(sent, "id", 0) or 0)
    ctx.emit("call_declined", {"call_id": peer.id, "reason": req.reason})
    return CallDeclined(call_id=peer.id, reason=req.reason, reply_message_id=reply_id)


SPEC_DECLINE = OperationSpec(
    id="call.decline",
    request=DeclineReq,
    response=CallDeclined,
    impl=decline,
    summary="Refuse an incoming call, optionally with a text reply",
    mutating=True,
    rate_class="send",
    columns=("call_id", "reason"),
    example={"call_id": 4815162342, "reason": "missed"},
    example_args="call decline 4815162342",
    covers=("calls.decline-call", "calls.respond-with-text"),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# call end
# ---------------------------------------------------------------------------


class EndReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    reason: Annotated[
        str,
        choice("hangup", "disconnect", "busy", "missed", help="Discard reason."),
    ] = "hangup"
    duration: Annotated[
        int, opt("--duration", metavar="N", help="Measured duration in seconds.", ge=0)
    ] = 0
    connection_id: Annotated[
        int, opt("--connection-id", metavar="N", help="tgcalls connection id, if bridged.", ge=0)
    ] = 0
    video: Annotated[bool, opt("--video", help="Report that video was on at the end.")] = False


async def end(ctx: OpContext, req: EndReq) -> CallEnded:
    """Hang up. The answer says whether the server wants a rating or a debug blob.

    `need_rating` and `need_debug` are the only reason `call rate` and `call
    debug upload` are ever worth running, so they are reported rather than
    dropped with the rest of the `Updates`.
    """
    from telethon.tl.functions import phone as fn

    peer, known = _calls.input_phone_call(ctx, req.call)
    result = await _client(ctx)(
        fn.DiscardCallRequest(
            peer=peer,
            duration=req.duration,
            reason=_calls.discard_reason(req.reason),
            connection_id=req.connection_id,
            video=req.video or None,
        )
    )
    discarded = _phone_call_of(result)
    if known is not None:
        known.state = "discarded"
        known.reason = req.reason
        known.duration = req.duration
        known.need_rating = bool(getattr(discarded, "need_rating", False))
        known.need_debug = bool(getattr(discarded, "need_debug", False))
    ctx.emit("call_ended", {"call_id": peer.id, "reason": req.reason})
    return CallEnded(
        call_id=peer.id,
        reason=req.reason,
        duration=req.duration,
        need_rating=bool(getattr(discarded, "need_rating", False)),
        need_debug=bool(getattr(discarded, "need_debug", False)),
    )


SPEC_END = OperationSpec(
    id="call.end",
    request=EndReq,
    response=CallEnded,
    impl=end,
    summary="Hang up an active call",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("call_id", "reason", "duration", "need_rating"),
    example={"call_id": 4815162342, "reason": "hangup", "duration": 42, "need_rating": False},
    example_args="call end 4815162342",
    covers=("calls.hangup",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# call get
# ---------------------------------------------------------------------------


class GetReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    fingerprint: Annotated[
        bool, opt("--fingerprint", help="Derive the key verification indices.")
    ] = True


async def get(ctx: OpContext, req: GetReq) -> Call:
    """The live state of a call this daemon is holding.

    There is no `phone.getCall`: a call exists in the update stream and in the
    client that ran the handshake, nowhere else. So this reads what the daemon
    knows, and says `null` for the parts only the handshake could supply —
    notably the key verification, which is derivable exactly when *this*
    session did the exchange.
    """
    call_id, _ = _calls.parse_call_id(req.call)
    live = _calls.live_calls(ctx.account).get(call_id)
    if live is None:
        raise NotFoundError(
            f"call {call_id} is not one this daemon has seen; "
            "`tlgr call watch` picks up incoming calls, `tlgr call start` outgoing ones"
        )
    model = _live_model(ctx, live, fingerprint=req.fingerprint)
    if req.fingerprint and model.fingerprint is None:
        ctx.warn("no key verification: this session did not run the DH exchange for that call")
    elif model.fingerprint is not None:
        ctx.warn(
            "the four values are indices into Telegram's 333-emoji table, which tlgr "
            "does not bundle; compare the indices, not emoji"
        )
    return model


SPEC_GET = OperationSpec(
    id="call.get",
    request=GetReq,
    response=Call,
    impl=get,
    summary="State of a call, including the key verification indices",
    description=(
        "`conference_supported` says whether `call invite` may be offered at "
        "all; `fingerprint` is null unless this session ran the key exchange."
    ),
    columns=("call_id", "state", "video", "media", "conference_supported"),
    example={**_EXAMPLE_CALL, "state": "active", "conference_supported": True},
    example_args="call get 4815162342",
    covers=("calls.conference-supported-flag",),
    covers_partial=("calls.emoji-fingerprint",),
    coverage_note=(
        "the four verification values are reported as indices into Telegram's "
        "333-emoji table; tlgr does not bundle the table, and guessing it for a "
        "security check would be worse than not printing it"
    ),
)


# ---------------------------------------------------------------------------
# call rate
# ---------------------------------------------------------------------------


class RateReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    rating: Annotated[int, arg(1, metavar="RATING", help="1 to 5 stars.", ge=1, le=5)]
    comment: Annotated[str, opt("--comment", help="Free-text comment.")] = ""
    problem: Annotated[
        list[str],
        opt("--problem", metavar="NAME", help="Problem to report; repeat for several."),
    ] = []
    user_initiative: Annotated[
        bool, opt("--user-initiative", help="The user rated unprompted.")
    ] = False


async def rate(ctx: OpContext, req: RateReq) -> CallRating:
    """Rate a finished call, and name the problems.

    The nine problem ids are a fixed list validated here rather than at the
    server, and they travel as hashtags appended to the comment — that is not
    a tlgr convention, it is the wire format the clients use.
    """
    from telethon.tl.functions import phone as fn

    peer, _ = _calls.input_phone_call(ctx, req.call)
    unknown = [p for p in req.problem if p not in RATING_PROBLEMS]
    if unknown:
        raise UsageError(
            f"unknown problem(s) {unknown}; expected any of {', '.join(RATING_PROBLEMS)}",
            field="problem",
        )
    comment = " ".join([req.comment.strip(), *(f"#{p}" for p in req.problem)]).strip()
    await _client(ctx)(
        fn.SetCallRatingRequest(
            peer=peer,
            rating=req.rating,
            comment=comment,
            user_initiative=req.user_initiative or None,
        )
    )
    return CallRating(call_id=peer.id, rating=req.rating, comment=comment)


SPEC_RATE = OperationSpec(
    id="call.rate",
    request=RateReq,
    response=CallRating,
    impl=rate,
    summary="Rate call quality and report specific problems",
    mutating=True,
    rate_class="send",
    columns=("call_id", "rating", "comment"),
    example={"call_id": 4815162342, "rating": 4, "comment": "#echo"},
    example_args="call rate 4815162342 4",
    covers=("calls.rate-call", "calls.rate-problems"),
)


# ---------------------------------------------------------------------------
# call signal
# ---------------------------------------------------------------------------


class SignalReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    data: Annotated[
        str | None, opt("--data", metavar="B64", help="Opaque packet to send, base64.")
    ] = None
    file: Annotated[
        str | None,
        opt("--file", metavar="PATH", kind="path", help="Read the packet from a file."),
    ] = None
    follow: Annotated[bool, opt("--follow", help="Keep streaming inbound signalling packets.")] = (
        False
    )
    idle_timeout: Annotated[
        int,
        opt("--idle-timeout", metavar="DURATION", kind="duration", help="Give up after silence."),
    ] = 3600


async def signal(ctx: OpContext, req: SignalReq) -> Any:
    """Carry one tgcalls signalling packet, and optionally watch for replies.

    Bridge plumbing, and the only route to some things MTProto has no verb
    for: "switch this voice call to video" is a media-state packet, not an
    RPC. tlgr can carry the bytes and can never produce them, which is why
    they go in and come out as opaque base64.
    """
    from telethon import events
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    peer, _ = _calls.input_phone_call(ctx, req.call)
    payload: bytes | None = None
    if req.file:
        import sys
        from pathlib import Path

        payload = sys.stdin.buffer.read() if req.file == "-" else Path(req.file).read_bytes()
    elif req.data:
        payload = _calls.unb64(req.data, field="data")

    if payload is None and not req.follow:
        raise UsageError("give --data or --file to send, or --follow to listen", field="data")

    if payload is not None:
        await _client(ctx)(fn.SendSignalingDataRequest(peer=peer, data=payload))
        yield Page(
            items=[
                CallSignal(
                    direction="out",
                    call_id=peer.id,
                    data=_calls.b64(payload),
                    at=_now(),
                )
            ],
            has_more=req.follow,
        )

    if not req.follow:
        return

    client = _client(ctx)
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def handler(update: Any) -> None:
        if type(update).__name__ != "UpdatePhoneCallSignalingData":
            return
        if int(getattr(update, "phone_call_id", 0) or 0) != peer.id:
            return
        await queue.put(bytes(getattr(update, "data", b"") or b""))

    builder = events.Raw(types=[types.UpdatePhoneCallSignalingData])
    client.add_event_handler(handler, builder)
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=max(1, req.idle_timeout))
            except (TimeoutError, asyncio.TimeoutError):
                yield Page(items=[], has_more=False)
                return
            yield Page(
                items=[
                    CallSignal(direction="in", call_id=peer.id, data=_calls.b64(data), at=_now())
                ],
                has_more=True,
            )
    finally:
        client.remove_event_handler(handler, builder)


SPEC_SIGNAL = OperationSpec(
    id="call.signal",
    request=SignalReq,
    response=Page[CallSignal],
    impl=signal,
    summary="Relay a tgcalls signalling packet to the peer (bridge plumbing)",
    description=(
        "The packets are opaque to tlgr. This is also the only route for "
        "turning a camera on mid-call: MTProto has no verb for it, only a "
        "media-state packet a real engine produces."
    ),
    mutating=True,
    stream=True,
    rate_class="send",
    columns=("direction", "call_id", "at"),
    example={"items": [{"direction": "out", "call_id": 4815162342, "data": "AA==", "at": "x"}]},
    example_args="call signal 4815162342 --data AA==",
    covers=("calls.signaling-data", "calls.switch-to-video"),
)


# ---------------------------------------------------------------------------
# call watch
# ---------------------------------------------------------------------------


class WatchReq(Request):
    auto_ack: Annotated[
        bool, opt("--auto-ack", help="Send receivedCall for each incoming call (busy-lock).")
    ] = False
    conference: Annotated[bool, opt("--conference", help="Also emit conference invitations.")] = (
        True
    )
    idle_timeout: Annotated[
        int,
        opt("--idle-timeout", metavar="DURATION", kind="duration", help="Give up after silence."),
    ] = 3600


def _conference_event(ctx: OpContext, message: Any, *, should_ring: bool) -> CallEvent:
    action = getattr(message, "action", None)
    others = [
        Peer(
            id=int(getattr(peer, "user_id", 0) or 0),
            raw_id=int(getattr(peer, "user_id", 0) or 0),
            kind="user",
        )
        for peer in (getattr(action, "other_participants", None) or [])
    ]
    missed = bool(getattr(action, "missed", False))
    return CallEvent(
        kind="conference.invite-cancelled" if missed else "conference.invite",
        at=_now(),
        call_id=int(getattr(action, "call_id", 0) or 0) or None,
        video=bool(getattr(action, "video", False)),
        msg_id=int(getattr(message, "id", 0) or 0),
        other_participants=others,
        should_ring=should_ring,
        peer=_peer_of(ctx, getattr(getattr(message, "peer_id", None), "user_id", None)),
    )


async def watch(ctx: OpContext, req: WatchReq) -> Any:
    """Stream incoming rings, state changes and conference invitations.

    This is the honest answer to "can tlgr take calls": it can tell you the
    phone is ringing and who is calling, and hand that to a notifier or a
    script. Conference invitations do not arrive as `updatePhoneCall` at all —
    they are service messages — so they are folded in here rather than left
    for the caller to discover.
    """
    from telethon import events
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    client = _client(ctx)
    queue: asyncio.Queue[CallEvent] = asyncio.Queue()
    config = await _calls.app_config(ctx) if req.conference else {}
    requests_disabled = bool(config.get("call_requests_disabled", False))

    async def handler(update: Any) -> None:
        name = type(update).__name__
        if name == "UpdatePhoneCall":
            phone_call = getattr(update, "phone_call", None)
            live = _absorb(ctx, phone_call)
            if req.auto_ack and live.state == "requested":
                with contextlib.suppress(Exception):
                    await client(
                        fn.ReceivedCallRequest(
                            peer=types.InputPhoneCall(id=live.id, access_hash=live.access_hash)
                        )
                    )
            await queue.put(
                CallEvent(
                    kind=f"call.{live.state}",
                    at=_now(),
                    call_id=live.id,
                    peer=_peer_of(ctx, live.peer_id),
                    video=live.video,
                    state=live.state,
                    should_ring=live.state == "requested" and not requests_disabled,
                )
            )
        elif name == "UpdatePhoneCallSignalingData":
            await queue.put(
                CallEvent(
                    kind="call.signalling",
                    at=_now(),
                    call_id=int(getattr(update, "phone_call_id", 0) or 0),
                    data=_calls.b64(bytes(getattr(update, "data", b"") or b"")),
                )
            )
        elif req.conference and name in ("UpdateNewMessage", "UpdateShortChatMessage"):
            message = getattr(update, "message", None)
            action = getattr(message, "action", None)
            if type(action).__name__ != "MessageActionConferenceCall":
                return
            should_ring = not requests_disabled and not (
                getattr(action, "missed", False) or getattr(action, "active", False)
            )
            await queue.put(_conference_event(ctx, message, should_ring=should_ring))

    builder = events.Raw(
        types=[
            types.UpdatePhoneCall,
            types.UpdatePhoneCallSignalingData,
            types.UpdateNewMessage,
        ]
    )
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
    id="call.watch",
    request=WatchReq,
    response=Page[CallEvent],
    impl=watch,
    summary="Stream call signalling: incoming rings, state changes, conference invitations",
    description=(
        "`should_ring` is resolved here from the app-config and the action "
        "flags, so every notifier does not have to get it right separately."
    ),
    stream=True,
    columns=("kind", "call_id", "state", "should_ring"),
    example={"items": [{"kind": "call.requested", "at": "2026-09-03T09:14:07Z"}]},
    example_args="call watch",
    covers=("calls.incoming-signalling", "conference.incoming-ring"),
)


# ---------------------------------------------------------------------------
# call invite (upgrade to a conference)
# ---------------------------------------------------------------------------


class InviteReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    user: Annotated[
        list[PeerRef],
        arg(1, metavar="USER", required=False, variadic=True, kind="user", help="Who to add."),
    ] = []
    params_json: Annotated[
        str | None,
        opt("--params-json", metavar="PATH", kind="path", help="tgcalls join payload."),
    ] = None
    public_key: Annotated[
        str | None, opt("--public-key", metavar="HEX", help="Your int256 E2E public key.")
    ] = None
    block: Annotated[
        str | None,
        opt("--block", metavar="PATH", kind="path", help="Pre-built e2e.chain block."),
    ] = None


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


def _public_key(value: str) -> int:
    try:
        return int(value, 16)
    except ValueError as exc:
        raise UsageError("--public-key must be hex", field="public_key") from exc


async def invite(ctx: OpContext, req: InviteReq) -> CallUpgrade:
    """Upgrade a 1:1 call into a conference and pull the other side in.

    Half of this is free and half of it is cryptography tlgr does not do.
    Creating the conference with `join=true` needs a signed initial
    `e2e.chain` block; tlgr cannot build one, so without `--block` and
    `--public-key` the command stops here with a usage error naming the
    missing piece rather than sending a request that is going to fail at the
    server. Everything after the slug exists — discarding the 1:1 call with
    `migrateConferenceCall` so the peer auto-joins, and inviting further
    users — is fully supported.
    """
    from telethon import utils
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    peer, known = _calls.input_phone_call(ctx, req.call)
    if known is not None and known.conference_supported is False:
        raise UsageError("the other side's client does not support conference calls", field="call")
    if not req.block or not req.public_key:
        raise UsageError(
            "upgrading a call to a conference needs a signed initial e2e.chain block "
            "(ChangeSetGroupState + ChangeSetSharedKey) and your int256 public key; "
            "tlgr cannot build the block, so pass --block and --public-key from an "
            "external E2E implementation",
            field="block",
        )

    created = await _client(ctx)(
        fn.CreateConferenceCallRequest(
            join=True,
            random_id=secrets.randbits(31),
            public_key=_public_key(req.public_key),
            block=_read_bytes(req.block, field="block"),
            params=types.DataJSON(data=_read_text(req.params_json, field="params-json"))
            if req.params_json
            else None,
        )
    )
    call = _group_call_of(created)
    if call is None:
        raise IndeterminateError(
            "the conference was created but the server did not name it; the 1:1 call "
            "was left alone rather than discarded into nothing"
        )
    ref = _calls.call_ref_of(call)
    link = getattr(call, "invite_link", None)
    slug = link.rsplit("/", 1)[-1] if link else None
    ref.slug = slug

    await _client(ctx)(
        fn.DiscardCallRequest(
            peer=peer,
            duration=0,
            reason=types.PhoneCallDiscardReasonMigrateConferenceCall(slug=slug or ""),
            connection_id=0,
        )
    )

    invited: list[Peer] = []
    group = types.InputGroupCall(id=ref.id, access_hash=ref.access_hash or 0)
    for reference in req.user:
        target = await _send.resolve(ctx, reference)
        await _client(ctx)(
            fn.InviteConferenceCallParticipantRequest(
                call=group, user_id=utils.get_input_user(target)
            )
        )
        invited.append(_peer_of(ctx, _send.peer_id_of(target)) or Peer(id=0, raw_id=0, kind="user"))

    ctx.emit("call_migrated", {"call_id": peer.id, "slug": slug})
    return CallUpgrade(
        call_id=peer.id,
        conference=ref,
        slug=slug,
        invite_link=link,
        invited=invited,
        migrated=True,
    )


SPEC_INVITE = OperationSpec(
    id="call.invite",
    request=InviteReq,
    response=CallUpgrade,
    impl=invite,
    aliases=("call.add-people",),
    summary="Upgrade a 1:1 call into a conference and pull the other side in",
    mutating=True,
    rate_class="send",
    columns=("call_id", "slug", "invite_link"),
    example={"call_id": 4815162342, "slug": "AbCdEf", "migrated": True},
    example_args="call invite 4815162342 @bobby --block /tmp/block.bin --public-key ff",
    covers_partial=("calls.migrate-to-conference",),
    coverage_note=(
        "the migration and the invitations are complete; creating the conference "
        "needs a signed e2e.chain block, which tlgr accepts (--block) but cannot "
        "build"
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# call config get
# ---------------------------------------------------------------------------


class ConfigReq(Request):
    timeouts: Annotated[bool, opt("--timeouts", help="Include the ringing timeouts.")] = True
    dh: Annotated[bool, opt("--dh", help="Include the DH parameters and their verdict.")] = False
    raw: Annotated[bool, opt("--raw", help="Include the tgcalls blob verbatim.")] = False


async def config_get(ctx: OpContext, req: ConfigReq) -> CallConfig:
    """Everything a client needs before it can ring, in one place.

    The DH block is reported with its validation verdict beside it, never on
    its own: a prime the server chose and the client did not check is not a
    key exchange.
    """
    import json

    from telethon.tl.functions import help as help_fn
    from telethon.tl.functions import messages as messages_fn
    from telethon.tl.functions import phone as fn

    client = _client(ctx)
    blob = await client(fn.GetCallConfigRequest())
    text = str(getattr(blob, "data", "") or "")
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {}
    tgcalls: dict[str, Any] = dict(parsed) if isinstance(parsed, dict) else {"value": parsed}
    if req.raw:
        tgcalls["raw"] = text

    timeouts: dict[str, int] = {}
    if req.timeouts:
        server = await client(help_fn.GetConfigRequest())
        for name in (
            "call_receive_timeout_ms",
            "call_ring_timeout_ms",
            "call_connect_timeout_ms",
            "call_packet_timeout_ms",
        ):
            value = getattr(server, name, None)
            if value is not None:
                timeouts[name] = int(value)

    dh: dict[str, Any] | None = None
    if req.dh:
        config = await client(messages_fn.GetDhConfigRequest(version=0, random_length=0))
        p_bytes = bytes(getattr(config, "p", b"") or b"")
        g = int(getattr(config, "g", 0) or 0)
        dh = {"version": int(getattr(config, "version", 0) or 0), **_calls.dh_verdict(p_bytes, g)}

    limits = await _calls.app_config(ctx)
    numeric = {k: int(v) for k, v in limits.items() if isinstance(v, (int, float))}
    return CallConfig(
        tgcalls_config=tgcalls,
        timeouts=timeouts,
        dh=dh,
        call_requests_disabled=bool(limits.get("call_requests_disabled", False)),
        conference_call_size_limit=numeric.get("conference_call_size_limit"),
        limits=numeric,
    )


SPEC_CONFIG_GET = OperationSpec(
    id="call.config.get",
    request=ConfigReq,
    response=CallConfig,
    impl=config_get,
    summary="VoIP configuration: tgcalls blob, ringing timeouts, DH parameters, call limits",
    columns=("call_requests_disabled", "conference_call_size_limit"),
    example={"timeouts": {"call_ring_timeout_ms": 90000}, "call_requests_disabled": False},
    example_args="call config get",
    covers=(
        "calls.client-call-requests-disabled",
        "calls.dh-config",
        "calls.get-call-config",
        "calls.timeout-config",
    ),
)


# ---------------------------------------------------------------------------
# call debug upload
# ---------------------------------------------------------------------------


class DebugReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="Call id, or id:access_hash.")]
    json_file: Annotated[
        str | None,
        opt("--json-file", metavar="PATH", kind="path", help="tgcalls debug JSON to upload."),
    ] = None
    log_file: Annotated[
        str | None,
        opt("--log-file", metavar="PATH", kind="path", help="Whole log file to upload."),
    ] = None


async def debug_upload(ctx: OpContext, req: DebugReq) -> CallDebugUpload:
    """Upload a debug blob or a log file the server asked for.

    Only meaningful when a real media engine produced it — the server asks via
    `phoneCallDiscarded.need_debug`. tlgr never fabricates one, which is why
    both inputs are files and neither has a default.
    """
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    peer, _ = _calls.input_phone_call(ctx, req.call)
    if not req.json_file and not req.log_file:
        raise UsageError(
            "give --json-file or --log-file; tlgr has no media engine and will not "
            "invent a debug blob",
            field="json_file",
        )
    uploaded: list[str] = []
    if req.json_file:
        data = _read_text(req.json_file, field="json-file")
        await _client(ctx)(fn.SaveCallDebugRequest(peer=peer, debug=types.DataJSON(data=data)))
        uploaded.append("debug")
    if req.log_file:
        handle = await ctx.upload_file(req.log_file)  # type: ignore[attr-defined]
        await _client(ctx)(fn.SaveCallLogRequest(peer=peer, file=handle))
        uploaded.append("log")
    return CallDebugUpload(call_id=peer.id, uploaded=uploaded)


SPEC_DEBUG_UPLOAD = OperationSpec(
    id="call.debug.upload",
    request=DebugReq,
    response=CallDebugUpload,
    impl=debug_upload,
    summary="Upload call debug information or a call log file",
    mutating=True,
    rate_class="file",
    columns=("call_id", "uploaded"),
    example={"call_id": 4815162342, "uploaded": ["debug"]},
    example_args="call debug upload 4815162342 --json-file /tmp/debug.json",
    covers=("calls.save-debug", "calls.save-log"),
)


# ---------------------------------------------------------------------------
# call log list
# ---------------------------------------------------------------------------


class LogListReq(Request):
    missed: Annotated[bool, opt("--missed", help="Missed calls only (the server filter).")] = False
    with_peer: Annotated[
        PeerRef | None,
        opt("--with", metavar="USER", kind="user", help="Only calls with this peer."),
    ] = None
    video: Annotated[bool, opt("--video", help="Video calls only (filtered locally).")] = False
    since: Annotated[
        str | None, opt("--since", metavar="WHEN", kind="datetime", help="Lower date bound.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", kind="datetime", help="Upper date bound.")
    ] = None


def _log_entry(ctx: OpContext, message: Any, chat_id: int) -> CallLogEntry | None:
    """One call-log service message as a flat row, or None if it is not one."""
    action = getattr(message, "action", None)
    name = type(action).__name__
    if name not in ("MessageActionPhoneCall", "MessageActionConferenceCall"):
        return None
    out = bool(getattr(message, "out", False))
    reason = _calls.reason_name(getattr(action, "reason", None))
    conference = name == "MessageActionConferenceCall"
    missed = bool(getattr(action, "missed", False)) if conference else reason in ("missed", "busy")
    date = getattr(message, "date", None)
    others = [
        Peer(
            id=int(getattr(peer, "user_id", 0) or 0),
            raw_id=int(getattr(peer, "user_id", 0) or 0),
            kind="user",
        )
        for peer in (getattr(action, "other_participants", None) or [])
    ]
    return CallLogEntry(
        msg_id=int(getattr(message, "id", 0) or 0),
        chat_id=chat_id,
        date=fmt_dt(date) or "",
        date_unix=to_unix(date) or 0,
        kind="conference" if conference else "call",
        direction="out" if out else "in",
        peer=_peer_of(ctx, abs(chat_id)) if chat_id > 0 else None,
        call_id=int(getattr(action, "call_id", 0) or 0) or None,
        video=bool(getattr(action, "video", False)),
        reason=reason,
        duration=int(getattr(action, "duration", 0) or 0),
        missed=missed,
        active=bool(getattr(action, "active", False)) if conference else None,
        other_participants=others,
    )


async def log_list(ctx: OpContext, req: LogListReq) -> Page[CallLogEntry]:
    """The Calls tab: voice, video, group and conference calls in one list.

    Not a method of its own — the call log is a global `messages.search` with
    `inputMessagesFilterPhoneCalls`, and the rows are service messages. This
    decodes both actions into one flat shape (Android shows the conference
    one as "Incoming/Outgoing/Missed Group Call") and keeps `chat_id` and
    `msg_id`, so `message get` jumps to the bubble and `call start
    --from-message` redials.
    """
    from telethon import utils
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    limit = min(int(getattr(ctx, "limit", None) or 30), 100)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state = (
        decode_cursor(token, op="call.log.list", kind=PageKind.SEARCH, account=ctx.account)
        if token
        else {}
    )

    peer: Any = types.InputPeerEmpty()
    if req.with_peer is not None:
        peer = await _send.resolve(ctx, req.with_peer)

    result = await _client(ctx)(
        fn.SearchRequest(
            peer=peer,
            q="",
            filter=types.InputMessagesFilterPhoneCalls(missed=req.missed or None),
            min_date=parse_dt(req.since) if req.since else None,
            max_date=parse_dt(req.until) if req.until else None,
            offset_id=int(state.get("offset_id", 0) or 0),
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
        )
    )

    items: list[CallLogEntry] = []
    last_id = 0
    for message in getattr(result, "messages", None) or []:
        last_id = int(getattr(message, "id", 0) or 0) or last_id
        try:
            chat_id = int(utils.get_peer_id(getattr(message, "peer_id", None)))
        except (TypeError, ValueError):  # pragma: no cover - malformed row
            chat_id = 0
        entry = _log_entry(ctx, message, chat_id)
        if entry is None:
            continue
        if req.video and not entry.video:
            continue
        items.append(entry)

    total = getattr(result, "count", None)
    return build_page(
        items,
        op="call.log.list",
        kind=PageKind.SEARCH,
        state={"offset_id": last_id},
        account=ctx.account,
        limit=limit,
        has_more=bool(last_id) and len(getattr(result, "messages", None) or []) >= limit,
        total=int(total) if isinstance(total, int) else None,
    )


SPEC_LOG_LIST = OperationSpec(
    id="call.log.list",
    request=LogListReq,
    response=Page[CallLogEntry],
    impl=log_list,
    summary="The Calls tab: every voice, video, group and conference call",
    description=(
        "Rows are service messages, decoded here: `messageActionPhoneCall` and "
        "`messageActionConferenceCall` become one flat shape."
    ),
    paginated=PageKind.SEARCH,
    columns=("date", "direction", "kind", "video", "duration", "reason", "chat_id", "msg_id"),
    headers=("Date", "Dir", "Kind", "Video", "Secs", "Reason", "Chat", "Msg"),
    example={
        "items": [
            {
                "msg_id": 900,
                "chat_id": 4242,
                "date": "2026-09-03T09:14:07Z",
                "date_unix": 1788340447,
                "kind": "call",
                "direction": "in",
                "duration": 42,
            }
        ],
        "has_more": False,
    },
    example_args="call log list",
    covers=(
        "calls.history-list",
        "calls.history-missed-only",
        "calls.history-per-peer",
        "calls.history-service-message-parse",
    ),
)


# ---------------------------------------------------------------------------
# call log delete
# ---------------------------------------------------------------------------


class LogDeleteReq(Request):
    id: Annotated[
        list[str],
        arg(
            0,
            metavar="CHAT:MSG_ID",
            required=False,
            variadic=True,
            help="Rows as `call log list` prints them.",
        ),
    ] = []
    revoke: Annotated[bool, opt("--revoke", help="Delete for both sides.")] = False
    history: Annotated[
        bool, opt("--history", help="Delete the whole call log instead of named rows.")
    ] = False


async def log_delete(ctx: OpContext, req: LogDeleteReq) -> CallLogDeleted:
    """Delete call log rows, or the entire call log.

    A row is the underlying service message, so named rows go down tlgr's
    ordinary delete path. `--history` is a different RPC that answers with an
    offset to resume from; calling it once and reporting success is how "clear
    my call history" ends up clearing the first page.
    """
    from telethon.tl.functions import messages as fn

    from tlgr.models.peer import parse_peer_ref

    client = _client(ctx)
    if req.history:
        deleted = 0
        for _ in range(100):
            affected = await client(fn.DeletePhoneCallHistoryRequest(revoke=req.revoke or None))
            deleted += len(getattr(affected, "messages", None) or [])
            if not int(getattr(affected, "offset", 0) or 0):
                break
            limiter = getattr(ctx, "limiter", None)
            if limiter is not None:
                await limiter.acquire("bulk")
        ctx.emit("call_log_cleared", {"revoked": req.revoke})
        return CallLogDeleted(deleted=deleted, revoked=req.revoke)

    if not req.id:
        raise UsageError("give CHAT:MSG_ID rows, or --history to clear the log", field="id")

    grouped: dict[str, list[int]] = {}
    for token in req.id:
        chat, _, tail = str(token).rpartition(":")
        if not chat or not tail.lstrip("-").isdigit():
            raise UsageError(f"{token!r} is not CHAT:MSG_ID", field="id")
        grouped.setdefault(chat, []).append(int(tail))

    deleted = 0
    for chat, ids in grouped.items():
        peer = await _send.resolve(ctx, parse_peer_ref(chat))
        await client.delete_messages(peer, ids, revoke=req.revoke)
        deleted += len(ids)
    ctx.emit("call_log_deleted", {"count": deleted})
    return CallLogDeleted(deleted=deleted, revoked=req.revoke)


SPEC_LOG_DELETE = OperationSpec(
    id="call.log.delete",
    request=LogDeleteReq,
    response=CallLogDeleted,
    impl=log_delete,
    summary="Delete call log entries, or the whole call log",
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("deleted", "revoked"),
    example={"deleted": 3, "revoked": True},
    example_args="call log delete 4242:900",
    covers=("calls.history-delete-selected", "messages-core.delete-call-history"),
)
