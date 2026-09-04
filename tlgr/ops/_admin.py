"""Plumbing the `chat member/admin/invite/topic/setting/stats` modules share.

Six things every group-administration operation needs and none of them should
own: the client off the context, the page window, an `InputChannel` with a
readable error when the peer is not one, the `channelFull`/`chatFull` fetch,
the `affectedHistory` drain loop, and the participant serialiser.

The serialiser is the load-bearing one. v1 flattened a `ChannelParticipant*`
into a plain user and lost `status`, `rank`, `date`, `inviter_id`,
`promoted_by`, `kicked_by` and both rights masks in the process — which is
why v1 could list members but could not answer "is this person banned or
merely restricted".

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, decode_cursor
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.admin import Participant
from tlgr.models.peer import Peer, Rights
from tlgr.ops import _rights
from tlgr.ops._serialize import entity_to_peer, peer_id_of
from tlgr.ops._spec import OpContext

__all__ = [
    "GENERAL_TOPIC",
    "affected_loop",
    "already",
    "client",
    "display_name",
    "entity_id",
    "entity_map",
    "full_chat",
    "input_channel",
    "input_user",
    "is_channel",
    "is_small_chat",
    "participant_model",
    "small_chat_id",
    "window",
]

#: The General topic. It always exists, cannot be deleted, and is the one id
#: `top_msg_id` must be *omitted* for.
GENERAL_TOPIC = 1


def client(ctx: OpContext) -> Any:
    handle = getattr(ctx, "client", None)
    if handle is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return handle


def already(ctx: OpContext) -> None:
    """Flag `meta.already`: the world already looked the way we were asked for."""
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


def window(ctx: OpContext, op: str, kind: PageKind, default: int = 50) -> tuple[int, Any]:
    """`(limit, cursor state)` — `--limit`/`--cursor` are transport-level (L5)."""
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 200), state


def is_channel(peer: Any) -> bool:
    return type(peer).__name__ in ("InputPeerChannel", "InputPeerChannelFromMessage")


def is_small_chat(peer: Any) -> bool:
    """A legacy basic group — the shape with no granular rights at all."""
    return type(peer).__name__ == "InputPeerChat"


def small_chat_id(peer: Any) -> int:
    return int(getattr(peer, "chat_id", 0) or 0)


def input_channel(peer: Any) -> Any:
    """The `InputChannel` a `channels.*` request wants, or a usage error."""
    from telethon import utils

    try:
        return utils.get_input_channel(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError(
            "this operation only works in a channel or supergroup; "
            "convert a basic group with `tlgr chat convert <chat> supergroup`",
            field="chat",
        ) from exc


def input_user(peer: Any) -> Any:
    """The `InputUser` a `*.editAdmin`-style request wants."""
    from telethon import utils

    try:
        return utils.get_input_user(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError("that reference does not name a user", field="user") from exc


async def affected_loop(ctx: OpContext, make_request: Any) -> int:
    """Drive an `affectedHistory` call until `offset == 0`.

    The server answers a large history with a partial result and an offset to
    resume from. Calling once and reporting success is how "delete everything
    this member sent" deletes the first hundred messages.
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


def entity_map(result: Any) -> dict[int, Any]:
    """`{marked id: entity}` for the chats and users a reply carried."""
    from telethon import utils

    out: dict[int, Any] = {}
    for entity in list(getattr(result, "chats", None) or []) + list(
        getattr(result, "users", None) or []
    ):
        try:
            out[int(utils.get_peer_id(entity))] = entity
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return out


async def full_chat(ctx: OpContext, peer: Any) -> tuple[Any, Any, dict[int, Any]]:
    """`(full, entity, entities)` for a group or channel of any shape.

    Basic groups and channels answer with different requests and different
    field names; every caller here wants the same three things, so the split
    happens once.
    """
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    handle = client(ctx)
    if is_channel(peer):
        result = await handle(chan_fn.GetFullChannelRequest(channel=input_channel(peer)))
    elif is_small_chat(peer):
        result = await handle(msg_fn.GetFullChatRequest(chat_id=small_chat_id(peer)))
    else:
        raise UsageError("this operation only works in a group or channel", field="chat")
    full = getattr(result, "full_chat", None)
    chat_id = peer_id_of(peer) or 0
    entities = entity_map(result)
    entity = entities.get(chat_id)
    if entity is None:
        chats = list(getattr(result, "chats", None) or [])
        entity = chats[0] if chats else None
    return full, entity, entities


def entity_id(entity: Any) -> int:
    """The marked id of an *entity* (`Chat`/`Channel`/`User`), or 0.

    Not the same call as `_serialize.peer_id_of`, which reads a `Peer*`/
    `InputPeer*` and answers None for an entity — an entity carries `id`,
    not `channel_id`, so asking the wrong one of the two silently yields 0
    for every row.
    """
    from telethon import utils

    if entity is None:
        return 0
    try:
        return int(utils.get_peer_id(entity))
    except (TypeError, ValueError):
        return 0


def display_name(entity: Any) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    return " ".join(part for part in parts if part).strip()


_STATUS = {
    "ChannelParticipantCreator": "creator",
    "ChatParticipantCreator": "creator",
    "ChannelParticipantAdmin": "admin",
    "ChatParticipantAdmin": "admin",
    "ChannelParticipantSelf": "self",
    "ChannelParticipantLeft": "left",
    "ChannelParticipant": "member",
    "ChatParticipant": "member",
}


def _participant_id(raw: Any) -> int:
    user_id = getattr(raw, "user_id", None)
    if isinstance(user_id, int):
        return user_id
    peer = getattr(raw, "peer", None)
    if peer is not None:
        return abs(peer_id_of(peer) or 0)
    return 0


def effective_rights(default_banned: Rights | None, member: Rights | None) -> Rights | None:
    """The chat defaults, patched with one member's own mask.

    A member's `banned_rights` only names what was taken away *from them*;
    everything else falls back to the chat default. Reporting the per-user
    mask alone answers "what did the admin type", not "what may this person
    actually do", which is the question anybody moderating is asking.
    """
    if default_banned is None and member is None:
        return None
    out = Rights()
    for name in _rights.MEMBER_MASK:
        flag = name.replace("-", "_")
        flag = {"send_rounds": "send_roundvideos"}.get(flag, flag)
        if not hasattr(out, flag):
            # A layer-229 right the model has no field for. `chat permission
            # list` names it as unsupported; inventing a field here would
            # report a permission tlgr cannot actually read.
            continue
        value: bool | None = None
        if default_banned is not None:
            value = getattr(default_banned, flag, None)
        if member is not None:
            own = getattr(member, flag, None)
            if own is False:
                value = False
            elif own is True and value is None:
                value = True
        setattr(out, flag, value)
    return out


def participant_model(
    raw: Any,
    *,
    chat_id: int = 0,
    entities: dict[int, Any] | None = None,
    default_banned: Rights | None = None,
) -> Participant:
    """One `ChannelParticipant*`/`ChatParticipant*` as a `Participant`."""
    kind = type(raw).__name__
    user_id = _participant_id(raw)
    entity = (entities or {}).get(user_id)
    status = _STATUS.get(kind, "member")
    if kind == "ChannelParticipantBanned":
        status = "banned" if getattr(raw, "left", False) else "restricted"

    admin = _rights.model_from_admin(getattr(raw, "admin_rights", None))
    banned = _rights.model_from_banned(getattr(raw, "banned_rights", None))
    date = getattr(raw, "date", None)
    subscription = getattr(raw, "subscription_until_date", None)

    return Participant(
        id=user_id,
        user_id=user_id,
        chat_id=chat_id,
        peer=entity_to_peer(entity) if entity is not None else None,
        username=getattr(entity, "username", None),
        name=display_name(entity),
        is_bot=bool(getattr(entity, "bot", False)),
        status=status,
        rank=getattr(raw, "rank", None),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        inviter_id=getattr(raw, "inviter_id", None),
        promoted_by=getattr(raw, "promoted_by", None),
        kicked_by=getattr(raw, "kicked_by", None),
        via_request=getattr(raw, "via_request", None),
        subscription_until_date=fmt_dt(subscription),
        admin_rights=admin,
        banned_rights=banned,
        effective_permissions=effective_rights(default_banned, banned),
        can_edit=getattr(raw, "can_edit", None),
    )


def peer_row(entity: Any) -> Peer:
    return entity_to_peer(entity)


def require_found(value: Any, message: str) -> Any:
    if value is None:
        raise NotFoundError(message)
    return value
