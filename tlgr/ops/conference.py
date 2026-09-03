"""The `conference` group: call links, and the E2E calls behind them.

A conference is a group call that belongs to no chat: you create it, you get a
`t.me/call/<slug>` link, and anyone with the link can join. Half of that is
the easiest surface in this PR and half of it is the hardest, and the split is
worth stating plainly because it decides what every command here can do.

* **No crypto needed:** creating a link, reading a conference by link or by
  invitation message, ringing people into it, declining an invitation,
  revoking the link, listing participants, ending it. All fully implemented.
* **Crypto needed:** *joining*, *removing* somebody, and sending anything
  inside. Conferences are end-to-end encrypted: the group state and the shared
  key live in an `e2e.chain` blockchain, and every one of those operations
  requires a signed block built on the current tip. Writing that block builder
  is a project on the scale of the rest of tlgr. So tlgr **carries** blocks
  (`--block`, `--encrypted-blob`, `--broadcast-block`) and **reads** the chain
  (`conference chain list`), and refuses, by name and up front, to pretend it
  can build one.

The refusal is a usage error rather than a server round trip on purpose: a
request that is going to fail should fail where the missing piece is, and the
message should name it.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import Annotated, Any, NoReturn

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.models.base import Request
from tlgr.models.call import (
    MEDIA_NONE,
    ChainBlock,
    ConferenceCreated,
    ConferenceDeclined,
    ConferenceInfo,
    ConferenceInvited,
    ConferenceRelayed,
    ConferenceRemoved,
    ConferenceRevoked,
)
from tlgr.models.page import Page
from tlgr.models.peer import Peer, PeerRef
from tlgr.ops import _calls, _send
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_REF: dict[str, Any] = {"id": 900100, "access_hash": 12345, "slug": "AbCdEf"}


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


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


def _needs_block(what: str, field: str = "block") -> NoReturn:
    """The one refusal every E2E-gated command makes, worded the same way."""
    raise UsageError(
        f"{what} needs a signed e2e.chain block built on the current tip of subchain 0, "
        "plus your int256 public key. Conferences are end-to-end encrypted and tlgr has "
        "no block builder, so pass --block and --public-key from an external E2E "
        "implementation; `conference chain list --tip` gives you the tip to build on",
        field=field,
    )


def _call_of(result: Any) -> Any:
    """The `groupCall` in a `phone.GroupCall` or in an `Updates`."""
    direct = getattr(result, "call", None)
    if direct is not None and type(direct).__name__ in ("GroupCall", "GroupCallDiscarded"):
        return direct
    for update in getattr(result, "updates", None) or []:
        call = getattr(update, "call", None)
        if call is not None:
            return call
    return None


def _slug_of(call: Any) -> str | None:
    link = getattr(call, "invite_link", None)
    return link.rsplit("/", 1)[-1] if link else None


# ---------------------------------------------------------------------------
# conference create
# ---------------------------------------------------------------------------


class CreateReq(Request):
    join: Annotated[bool, opt("--join", help="Also join it — needs the E2E material.")] = False
    public_key: Annotated[
        str | None, opt("--public-key", metavar="HEX", help="Your int256 E2E public key.")
    ] = None
    block: Annotated[
        str | None,
        opt("--block", metavar="PATH", kind="path", help="Initial e2e.chain block for subchain 0."),
    ] = None
    params_json: Annotated[
        str | None,
        opt("--params-json", metavar="PATH", kind="path", help="tgcalls join payload."),
    ] = None
    muted: Annotated[bool, opt("--muted/--unmuted", help="Join muted.")] = True


async def create(ctx: OpContext, req: CreateReq) -> ConferenceCreated:
    """Create a call link: a conference call tied to no chat.

    Without `--join` this is the easiest conference operation there is — one
    RPC, no crypto — and it hands back `groupCall.invite_link`, the shareable
    link. With `--join` the API additionally wants a fresh public key, a valid
    initial chain block and a tgcalls payload, none of which tlgr can build.
    """
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    if req.join and (not req.block or not req.public_key):
        _needs_block("creating and joining a conference in one step")

    result = await _client(ctx)(
        fn.CreateConferenceCallRequest(
            random_id=secrets.randbits(31),
            join=req.join or None,
            muted=req.muted or None,
            public_key=_public_key(req.public_key) if req.public_key else None,
            block=_read_bytes(req.block, field="block") if req.block else None,
            params=types.DataJSON(data=_read_text(req.params_json, field="params-json"))
            if req.params_json
            else None,
        )
    )
    call = _call_of(result)
    if call is None:
        raise NotFoundError("the server created a conference without naming it")
    slug = _slug_of(call)
    ref = _calls.call_ref_of(call, slug=slug)
    ctx.emit("conference_created", {"call_id": ref.id, "slug": slug})
    limits = await _calls.app_config(ctx)
    cap = limits.get("conference_call_size_limit")
    if cap:
        ctx.warn(f"a conference holds at most {cap} participants")
    return ConferenceCreated(
        call=ref,
        slug=slug,
        invite_link=getattr(call, "invite_link", None),
        creator=True,
        joined=req.join,
        media=MEDIA_NONE,
    )


SPEC_CREATE = OperationSpec(
    id="conference.create",
    request=CreateReq,
    response=ConferenceCreated,
    impl=create,
    aliases=("conf.create",),
    summary="Create a call link (a conference call not tied to any chat)",
    mutating=True,
    rate_class="send",
    columns=("call.id", "slug", "invite_link"),
    example={
        "call": _EXAMPLE_REF,
        "media": "none",
        "joined": False,
        "slug": "AbCdEf",
        "invite_link": "https://t.me/call/AbCdEf",
    },
    example_args="conference create",
    covers=("conference.create-link",),
    covers_partial=("conference.create-and-join",),
    coverage_note=(
        "creating the link is complete; joining at creation needs a signed "
        "e2e.chain block, which tlgr accepts (--block) but cannot build"
    ),
)


# ---------------------------------------------------------------------------
# conference get
# ---------------------------------------------------------------------------


class GetReq(Request):
    call: Annotated[
        str,
        arg(0, metavar="CALL", help="A call link, a slug, id:access_hash, or msg:<id>."),
    ]
    qr: Annotated[bool, opt("--qr", help="Also return the link as QR payload text.")] = False
    limits: Annotated[
        bool, opt("--limits", help="Include the conference size and message caps.")
    ] = False


async def get(ctx: OpContext, req: GetReq) -> ConferenceInfo:
    """Inspect a conference by link, slug, id or invitation message.

    The honest half of conference support: reading a call link needs no E2E
    state at all. There is no `exportConferenceLink` method — the link *is*
    `groupCall.invite_link`.
    """
    from telethon.tl.functions import phone as fn

    handle = await _calls.resolve_call(ctx, req.call)
    result = await _client(ctx)(fn.GetGroupCallRequest(call=handle.input, limit=0))
    call = getattr(result, "call", None)
    if call is None:
        raise NotFoundError("that call link does not resolve to a call")
    slug = _slug_of(call) or handle.ref.slug
    info = ConferenceInfo(
        call=_calls.call_ref_of(call, slug=slug, msg_id=handle.ref.msg_id),
        slug=slug,
        invite_link=getattr(call, "invite_link", None),
        participants_count=int(getattr(call, "participants_count", 0) or 0),
        creator=bool(getattr(call, "creator", False)),
        messages_enabled=bool(getattr(call, "messages_enabled", False)),
        conference=bool(getattr(call, "conference", False)),
        title=getattr(call, "title", None),
    )
    if req.limits:
        config = await _calls.app_config(ctx)
        info.limits = {k: int(v) for k, v in config.items() if isinstance(v, (int, float))}
    if req.qr:
        info.qr = info.invite_link
        ctx.warn(
            "tlgr does not bundle a QR encoder: `qr` is the exact text to encode, "
            "pipe it into qrencode or any QR renderer"
        )
    return info


SPEC_GET = OperationSpec(
    id="conference.get",
    request=GetReq,
    response=ConferenceInfo,
    impl=get,
    aliases=("conf.get", "conference.info"),
    summary="Inspect a conference by link, slug, id or invitation message",
    columns=("call.id", "slug", "participants_count", "messages_enabled"),
    example={
        "call": _EXAMPLE_REF,
        "conference": True,
        "slug": "AbCdEf",
        "participants_count": 3,
    },
    example_args="conference get https://t.me/call/AbCdEf",
    covers=("conference.get-link", "conference.join-by-slug", "conference.size-limit"),
    covers_partial=("conference.link-qr",),
    coverage_note=(
        "`--qr` returns the exact text to encode; drawing the code needs a QR "
        "encoder tlgr does not bundle"
    ),
)


# ---------------------------------------------------------------------------
# conference invite / decline
# ---------------------------------------------------------------------------


class InviteReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A call link, slug or id:access_hash.")]
    user: Annotated[
        list[PeerRef],
        arg(1, metavar="USER", variadic=True, kind="user", help="Who to ring."),
    ] = []
    video: Annotated[bool, opt("--video", help="Ring them as a video call.")] = False
    fallback_link: Annotated[
        bool, opt("--fallback-link", help="Report the link for users who cannot be invited.")
    ] = True


async def invite(ctx: OpContext, req: InviteReq) -> ConferenceInvited:
    """Ring people into a conference, falling back to the link.

    The invitation is a `messageActionConferenceCall` service message and the
    receiving client rings on it. Per-user outcomes are classified the way the
    GUI does rather than collapsed into one failure, because "already in the
    call" and "their privacy settings refuse you" call for different actions.
    """
    from telethon import utils
    from telethon.tl.functions import phone as fn

    if not req.user:
        raise UsageError("give at least one user to invite", field="user")
    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    client = _client(ctx)

    invited: list[Peer] = []
    failed: list[dict[str, Any]] = []
    for reference in req.user:
        target = await _send.resolve(ctx, reference)
        marked = _send.peer_id_of(target)
        model = Peer(id=marked, raw_id=abs(marked), kind="user")
        try:
            await client(
                fn.InviteConferenceCallParticipantRequest(
                    call=handle.input,
                    user_id=utils.get_input_user(target),
                    video=req.video or None,
                )
            )
            invited.append(model)
        except Exception as exc:
            text = f"{type(exc).__name__} {exc}".upper().replace("_", "")
            if "ALREADYPARTICIPANT" in text:
                reason = "already-in-call"
            elif "PRIVACY" in text:
                reason = "privacy-restricted"
            elif "KICKED" in text or "BANNED" in text:
                reason = "kicked"
            else:
                raise
            failed.append({"peer": model.id, "reason": reason})

    result = ConferenceInvited(invited=invited, failed=failed)
    if failed and req.fallback_link:
        with contextlib.suppress(Exception):
            info = await client(fn.GetGroupCallRequest(call=handle.input, limit=0))
            result.link = getattr(getattr(info, "call", None), "invite_link", None)
        if result.link:
            ctx.warn(
                "some users could not be rung; send them the link yourself with "
                "`message send`, which asks before it writes on your behalf"
            )
    ctx.emit("conference_invited", {"call_id": handle.ref.id, "count": len(invited)})
    return result


SPEC_INVITE = OperationSpec(
    id="conference.invite",
    request=InviteReq,
    response=ConferenceInvited,
    impl=invite,
    aliases=("conf.invite",),
    summary="Ring somebody into a conference, falling back to sending them the link",
    mutating=True,
    rate_class="send",
    columns=("invited", "failed", "link"),
    example={"invited": [{"id": 4242, "raw_id": 4242, "kind": "user"}], "failed": []},
    example_args="conference invite AbCdEf @alice",
    covers=("conference.invite-link-fallback", "conference.invite-user"),
    tags=frozenset({"visible-to-others"}),
)


class DeclineReq(Request):
    msg_id: Annotated[int, arg(0, metavar="MSG_ID", help="The invitation service message id.")]


async def decline(ctx: OpContext, req: DeclineReq) -> ConferenceDeclined:
    """Decline a conference invitation, or stop ringing somebody you invited.

    One RPC for both directions. No chat is needed because
    `messageActionConferenceCall` only occurs in private chats, which share
    one id sequence.
    """
    from telethon.tl.functions import phone as fn

    await _client(ctx)(fn.DeclineConferenceCallInviteRequest(msg_id=req.msg_id))
    ctx.emit("conference_declined", {"msg_id": req.msg_id})
    return ConferenceDeclined(msg_id=req.msg_id, declined=True)


SPEC_DECLINE = OperationSpec(
    id="conference.decline",
    request=DeclineReq,
    response=ConferenceDeclined,
    impl=decline,
    aliases=("conf.decline",),
    summary="Decline a conference invitation, or stop ringing somebody you invited",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("msg_id", "declined"),
    example={"msg_id": 900, "declined": True},
    example_args="conference decline 900",
    covers=("conference.decline-invite", "groupcall.stop-ringing"),
)


# ---------------------------------------------------------------------------
# conference join
# ---------------------------------------------------------------------------


class JoinReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A call link, slug, id or msg:<id>.")]
    public_key: Annotated[
        str | None, opt("--public-key", metavar="HEX", help="Your int256 E2E public key.")
    ] = None
    block: Annotated[
        str | None,
        opt("--block", metavar="PATH", kind="path", help="Join block built on the current tip."),
    ] = None
    params_json: Annotated[
        str | None,
        opt("--params-json", metavar="PATH", kind="path", help="tgcalls join payload."),
    ] = None
    muted: Annotated[bool, opt("--muted/--unmuted", help="Join muted.")] = True
    video_stopped: Annotated[bool, opt("--video-stopped", help="Join with video off.")] = True


async def join(ctx: OpContext, req: JoinReq) -> ConferenceCreated:
    """Join a conference — which needs E2E material tlgr cannot generate.

    Unlike `vc join`, this is not usable with a synthetic payload: joining a
    conference means fetching the tip of subchain 0, building a signed join
    block on top of it, and retrying whenever the server answers
    `CONF_WRITE_CHAIN_INVALID`. The command exists so a bridge, or a user with
    an external block builder, has a place to plug in; without `--block` and
    `--public-key` it stops with an explanation rather than an RPC failure.
    `join_as` is always yourself for a conference.
    """
    from telethon.tl import types
    from telethon.tl.functions import phone as fn

    if not req.block or not req.public_key:
        _needs_block("joining a conference")
    if not req.params_json:
        raise UsageError(
            "joining also needs a tgcalls join payload (--params-json) from a real "
            "media engine; tlgr has none",
            field="params_json",
        )

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    result = await _client(ctx)(
        fn.JoinGroupCallRequest(
            call=handle.input,
            join_as=types.InputPeerSelf(),
            params=types.DataJSON(data=_read_text(req.params_json, field="params-json")),
            muted=req.muted or None,
            video_stopped=req.video_stopped or None,
            public_key=_public_key(req.public_key),
            block=_read_bytes(req.block, field="block"),
        )
    )
    call = _call_of(result)
    ref = _calls.call_ref_of(call) if call is not None else handle.ref
    ctx.emit("conference_joined", {"call_id": ref.id})
    ctx.warn("tlgr carries no media: this is server-side presence in an E2E call")
    return ConferenceCreated(
        call=ref,
        slug=handle.ref.slug,
        invite_link=getattr(call, "invite_link", None),
        creator=False,
        joined=True,
        media=MEDIA_NONE,
    )


SPEC_JOIN = OperationSpec(
    id="conference.join",
    request=JoinReq,
    response=ConferenceCreated,
    impl=join,
    aliases=("conf.join",),
    summary="Join a conference call (requires E2E material tlgr cannot generate)",
    mutating=True,
    rate_class="send",
    columns=("call.id", "joined", "media"),
    example={"call": _EXAMPLE_REF, "joined": True, "media": "none"},
    example_args=(
        "conference join AbCdEf --block /tmp/b.bin --public-key ff --params-json /tmp/p.json"
    ),
    covers_partial=("conference.join-by-invite-message",),
    coverage_note=(
        "the request is built and sent; the signed join block it needs is an "
        "e2e.chain builder tlgr does not have and accepts from outside instead"
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# conference remove / revoke
# ---------------------------------------------------------------------------


class RemoveReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A call link, slug or id:access_hash.")]
    user: Annotated[
        list[PeerRef],
        arg(1, metavar="USER", required=False, variadic=True, kind="user", help="Who to remove."),
    ] = []
    left_only: Annotated[
        bool, opt("--left-only", help="Prune participants that already dropped off.")
    ] = False
    block: Annotated[
        str | None,
        opt("--block", metavar="PATH", kind="path", help="Removal block that rotates the key."),
    ] = None


async def remove(ctx: OpContext, req: RemoveReq) -> ConferenceRemoved:
    """Remove conference participants, or prune the ones that already left.

    `phone.deleteConferenceCallParticipants` always needs a valid block that
    takes the users out of the group state *and rotates the shared key* —
    otherwise the people you removed could still decrypt. tlgr will not send
    that request without one. `--left-only` is the housekeeping pass real
    clients run by themselves. You cannot remove yourself this way: use
    `vc leave`.
    """
    from telethon import utils
    from telethon.tl.functions import phone as fn

    if not req.block:
        _needs_block("removing a conference participant")
    if not req.user and not req.left_only:
        raise UsageError("give users to remove, or --left-only to prune", field="user")

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    removed: list[Peer] = []
    ids: list[int] = []
    for reference in req.user:
        target = await _send.resolve(ctx, reference)
        ids.append(int(utils.get_input_user(target).user_id))
        marked = _send.peer_id_of(target)
        removed.append(Peer(id=marked, raw_id=abs(marked), kind="user"))

    await _client(ctx)(
        fn.DeleteConferenceCallParticipantsRequest(
            call=handle.input,
            ids=ids,
            block=_read_bytes(req.block, field="block"),
            only_left=req.left_only or None,
            kick=bool(req.user) or None,
        )
    )
    ctx.emit("conference_removed", {"call_id": handle.ref.id, "count": len(ids)})
    return ConferenceRemoved(
        call=handle.ref, removed=removed, only_left=req.left_only, kicked=bool(req.user)
    )


SPEC_REMOVE = OperationSpec(
    id="conference.remove",
    request=RemoveReq,
    response=ConferenceRemoved,
    impl=remove,
    aliases=("conf.remove", "conference.kick"),
    summary="Remove conference participants, or prune the ones that already left",
    description="Needs the `remove_users` chain permission and a key-rotating block.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("call.id", "removed", "only_left"),
    example={"call": _EXAMPLE_REF, "only_left": True},
    example_args="conference remove AbCdEf --left-only --block /tmp/b.bin",
    covers_partial=("conference.kick-participant", "conference.prune-left"),
    coverage_note=(
        "the request is built and sent; the removal block that rotates the shared "
        "key is an e2e.chain builder tlgr does not have and accepts from outside"
    ),
    tags=frozenset({"visible-to-others"}),
)


class RevokeReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A call link, slug or id:access_hash.")]


async def revoke(ctx: OpContext, req: RevokeReq) -> ConferenceRevoked:
    """Revoke a conference's call link. People already in the call stay."""
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    await _client(ctx)(fn.ToggleGroupCallSettingsRequest(call=handle.input, reset_invite_hash=True))
    ctx.emit("conference_revoked", {"call_id": handle.ref.id})
    return ConferenceRevoked(call=handle.ref, revoked=True)


SPEC_REVOKE = OperationSpec(
    id="conference.revoke",
    request=RevokeReq,
    response=ConferenceRevoked,
    impl=revoke,
    aliases=("conf.revoke", "conference.link.revoke"),
    summary="Revoke a conference's call link",
    description="Creator only. Nobody new can join with the old link.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("call.id", "revoked"),
    example={"call": _EXAMPLE_REF, "revoked": True},
    example_args="conference revoke AbCdEf",
    covers=("conference.revoke-link",),
)


# ---------------------------------------------------------------------------
# conference send
# ---------------------------------------------------------------------------


class SendReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A call link, slug or id:access_hash.")]
    encrypted_blob: Annotated[
        str | None,
        opt("--encrypted-blob", metavar="PATH", kind="path", help="Pre-encrypted payload."),
    ] = None
    broadcast_block: Annotated[
        str | None,
        opt(
            "--broadcast-block",
            metavar="PATH",
            kind="path",
            help="Serialized e2e.chain broadcast block for subchain 1.",
        ),
    ] = None


async def send(ctx: OpContext, req: SendReq) -> ConferenceRelayed:
    """Relay an E2E-encrypted in-call message, or a verification broadcast.

    A relay, not a composer. Conference chat is end-to-end encrypted, so tlgr
    can carry a payload and cannot produce one: it has no shared key (that
    comes from the chain) and no block builder. The same is true of the emoji
    verification, which is a commit-reveal broadcast on subchain 1. Incoming
    messages surface as opaque blobs in `vc watch`.
    """
    from telethon.tl.functions import phone as fn

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    if not req.encrypted_blob and not req.broadcast_block:
        raise UsageError(
            "give --encrypted-blob or --broadcast-block: conference messages are "
            "end-to-end encrypted and tlgr has no shared key to encrypt with",
            field="encrypted_blob",
        )
    kind = "encrypted-message"
    if req.encrypted_blob:
        await _client(ctx)(
            fn.SendGroupCallEncryptedMessageRequest(
                call=handle.input,
                encrypted_message=_read_bytes(req.encrypted_blob, field="encrypted-blob"),
            )
        )
    if req.broadcast_block:
        await _client(ctx)(
            fn.SendConferenceCallBroadcastRequest(
                call=handle.input, block=_read_bytes(req.broadcast_block, field="broadcast-block")
            )
        )
        kind = "broadcast" if not req.encrypted_blob else "both"
    return ConferenceRelayed(call=handle.ref, kind=kind, sent=True)


SPEC_SEND = OperationSpec(
    id="conference.send",
    request=SendReq,
    response=ConferenceRelayed,
    impl=send,
    aliases=("conf.send",),
    summary="Send an E2E-encrypted in-call message, or a key-verification broadcast",
    mutating=True,
    rate_class="send",
    columns=("call.id", "kind", "sent"),
    example={"call": _EXAMPLE_REF, "kind": "encrypted-message", "sent": True},
    example_args="conference send AbCdEf --encrypted-blob /tmp/msg.bin",
    covers_partial=("conference.broadcast-nonce", "conference.encrypted-message"),
    coverage_note=(
        "tlgr is the transport: it carries a payload an external E2E "
        "implementation produced, and cannot encrypt or sign one itself"
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# conference chain list
# ---------------------------------------------------------------------------


class ChainListReq(Request):
    call: Annotated[str, arg(0, metavar="CALL", help="A call link, slug or id:access_hash.")]
    subchain: Annotated[
        int, opt("--subchain", metavar="0|1", help="0 = group state, 1 = broadcasts.", ge=0, le=1)
    ] = 0
    tip: Annotated[bool, opt("--tip", help="Only the latest block.")] = False
    offset: Annotated[
        int, opt("--offset", metavar="N", help="Start at this height; -1 is the tip.")
    ] = 0


async def chain_list(ctx: OpContext, req: ChainListReq) -> Page[ChainBlock]:
    """Read the conference's E2E blockchain.

    Fetching and dumping blocks is easy; validating or creating them is not,
    so the blocks come out as base64 with their heights and nothing else. That
    is exactly what an external E2E implementation needs from a transport, and
    it is what `conference join --block` is built on top of.
    """
    from telethon.tl.functions import phone as fn

    limit = min(int(getattr(ctx, "limit", None) or 30), 100)
    token = getattr(ctx, "cursor", None)
    state = (
        decode_cursor(token, op="conference.chain.list", kind=PageKind.LOCAL, account=ctx.account)
        if token
        else {}
    )
    offset = int(state.get("offset", req.offset) or 0)
    if req.tip:
        offset, limit = -1, 1

    handle = await _calls.concrete_call(ctx, await _calls.resolve_call(ctx, req.call))
    result = await _client(ctx)(
        fn.GetGroupCallChainBlocksRequest(
            call=handle.input, sub_chain_id=req.subchain, offset=offset, limit=limit
        )
    )
    raw_blocks: list[Any] = []
    for update in getattr(result, "updates", None) or []:
        raw_blocks.extend(getattr(update, "blocks", None) or [])
        next_offset = int(getattr(update, "next_offset", 0) or 0)
        break
    else:
        next_offset = offset + len(raw_blocks)

    start = max(0, next_offset - len(raw_blocks))
    items = [
        ChainBlock(
            sub_chain_id=req.subchain,
            height=start + index,
            block=_calls.b64(bytes(block)),
            next_offset=next_offset,
        )
        for index, block in enumerate(raw_blocks)
    ]
    return build_page(
        items,
        op="conference.chain.list",
        kind=PageKind.LOCAL,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(items) and not req.tip and len(items) >= limit,
    )


SPEC_CHAIN_LIST = OperationSpec(
    id="conference.chain.list",
    request=ChainListReq,
    response=Page[ChainBlock],
    impl=chain_list,
    aliases=("conf.chain.list",),
    summary="Read the conference's E2E blockchain",
    description=(
        "Blocks are base64 and unvalidated: tlgr is a transport for an "
        "external E2E implementation, not a participant in the protocol."
    ),
    paginated=PageKind.LOCAL,
    columns=("sub_chain_id", "height", "next_offset"),
    headers=("Chain", "Height", "Next"),
    example={"items": [{"sub_chain_id": 0, "height": 12, "block": "AA=="}], "has_more": False},
    example_args="conference chain list AbCdEf --tip",
    covers=("conference.chain-blocks",),
)
