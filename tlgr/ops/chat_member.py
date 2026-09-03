"""`chat member *` and `chat request *`: who is in a chat, and on what terms.

Three shapes are deliberate, and each one is a v1 bug turned into a rule.

* **A member is a participant, not a user.** v1's `chat members` returned
  `{id, first_name, username, is_bot}`; the `ChannelParticipant*` wrapper —
  status, rank, join date, inviter, promoter, both rights masks — went in the
  bin. `chat members` still works and still answers with those keys, but the
  row around them is now a whole `Participant`.
* **Restricting is a read-modify-write.** `channels.editBanned` replaces the
  entire mask, so sending only the flags a caller named would silently give
  back every restriction they did not mention. The current mask is fetched,
  patched, and sent complete.
* **Kick and ban are different operations.** A kick is `editBanned(view)`
  followed by an empty mask, so the person may come back; a ban leaves the
  mask in place. v1 had one command and it did the second thing while
  reading like the first.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.admin import (
    JoinRequest,
    MemberResult,
    MembersAdded,
    MissingInvitee,
    Participant,
    RequestResult,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef, Rights
from tlgr.ops import _admin, _rights, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_MEMBER: dict[str, Any] = {
    "id": 4242,
    "user_id": 4242,
    "name": "Alice",
    "username": "alice",
    "status": "admin",
    "rank": "moderator",
    "date": "2026-02-01T10:00:00Z",
}

#: `channels.getParticipants` filters, keyed the way the CLI spells them.
_FILTERS = {
    "recent": "ChannelParticipantsRecent",
    "admins": "ChannelParticipantsAdmins",
    "bots": "ChannelParticipantsBots",
    "contacts": "ChannelParticipantsContacts",
    "kicked": "ChannelParticipantsKicked",
    "banned": "ChannelParticipantsBanned",
    "restricted": "ChannelParticipantsBanned",
    "mentions": "ChannelParticipantsMentions",
    "search": "ChannelParticipantsSearch",
}

#: `messages.missingInvitee` flags → the reason string tlgr reports.
_MISSING_REASONS = (
    ("premium_would_allow_invite", "premium-would-allow-invite"),
    ("premium_required_for_pm", "premium-required-for-pm"),
)


def _participants_filter(name: str, search: str, topic: int | None) -> Any:
    """The `ChannelParticipantsFilter` for a CLI `--filter` name."""
    from telethon.tl import types

    if search and name == "recent":
        name = "search"
    cls = getattr(types, _FILTERS[name])
    if name in ("kicked", "banned", "restricted", "search", "mentions"):
        if name == "mentions":
            return cls(q=search or None, top_msg_id=topic)
        return cls(q=search or "")
    return cls()


def _missing(raw: Any) -> list[MissingInvitee]:
    """`messages.invitedUsers.missing_invitees`, verbatim rather than dropped."""
    out: list[MissingInvitee] = []
    for item in getattr(raw, "missing_invitees", None) or []:
        reason = "privacy-restricted"
        for flag, label in _MISSING_REASONS:
            if getattr(item, flag, False):
                reason = label
                break
        out.append(MissingInvitee(user_id=int(getattr(item, "user_id", 0) or 0), reason=reason))
    return out


async def _default_banned(ctx: OpContext, peer: Any) -> Rights | None:
    """The chat-wide default mask, for the `effective_permissions` column."""
    try:
        _full, entity, _entities = await _admin.full_chat(ctx, peer)
    except UsageError:
        return None
    return _rights.model_from_banned(getattr(entity, "default_banned_rights", None))


async def _member_of(ctx: OpContext, peer: Any, user: Any) -> Any:
    """The raw participant object, or NOT_FOUND.

    Fetched before every restrict/promote so the mask that goes back is the
    caller's change applied to what is actually there.
    """
    from telethon.tl.functions import channels as fn

    if _admin.is_channel(peer):
        try:
            reply = await _admin.client(ctx)(
                fn.GetParticipantRequest(channel=_admin.input_channel(peer), participant=user)
            )
        except Exception as exc:
            if type(exc).__name__ == "UserNotParticipantError":
                raise NotFoundError("that user is not a member of this chat") from exc
            raise
        return reply
    full, _entity, entities = await _admin.full_chat(ctx, peer)
    wanted = _send.peer_id_of(user)
    holder = getattr(full, "participants", None)
    for row in getattr(holder, "participants", None) or []:
        if int(getattr(row, "user_id", 0) or 0) == abs(wanted):
            return type("_Small", (), {"participant": row, "users": list(entities.values())})()
    raise NotFoundError("that user is not a member of this chat")


# ---------------------------------------------------------------------------
# chat member list
# ---------------------------------------------------------------------------


class MemberListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    filter: Annotated[
        str,
        choice(
            "recent",
            "admins",
            "bots",
            "contacts",
            "kicked",
            "banned",
            "restricted",
            "mentions",
            help="Which participant list. kicked = removed, restricted = still in the chat.",
        ),
    ] = "recent"
    search: Annotated[
        str, opt("--search", "-s", metavar="TEXT", help="Server-side name query.")
    ] = ""
    topic: Annotated[
        int | None,
        opt("--topic", metavar="ID", kind="msg_id", help="With --filter mentions: one topic."),
    ] = None
    via_link: Annotated[
        str | None,
        opt("--via-link", metavar="LINK", help="Only people who joined through this invite link."),
    ] = None
    subscription_expired: Annotated[
        bool,
        opt("--subscription-expired", help="With --via-link: lapsed paid subscribers."),
    ] = False
    bots: Annotated[bool, opt("--bots", help="Shorthand for --filter bots.")] = False
    admins: Annotated[bool, opt("--admins", help="Shorthand for --filter admins (v1).")] = False


async def _importers(
    ctx: OpContext, req: MemberListReq, peer: Any, limit: int, state: dict[str, Any], op: str
) -> Page[Participant]:
    """`--via-link`: who came in through one invite (messages.getChatInviteImporters)."""
    from datetime import datetime, timezone

    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    offset_date = state.get("date")
    offset_user: Any = types.InputUserEmpty()
    if state.get("user"):
        offset_user = types.InputUser(
            user_id=int(state["user"]), access_hash=int(state.get("hash", 0) or 0)
        )
    reply = await _admin.client(ctx)(
        fn.GetChatInviteImportersRequest(
            peer=peer,
            offset_date=datetime.fromtimestamp(offset_date, tz=timezone.utc)
            if offset_date
            else None,
            offset_user=offset_user,
            limit=limit,
            link=req.via_link,
            q=req.search or None,
            subscription_expired=req.subscription_expired or None,
        )
    )
    entities = _admin.entity_map(reply)
    chat_id = _send.peer_id_of(peer)
    items: list[Participant] = []
    last: Any = None
    for row in getattr(reply, "importers", None) or []:
        user_id = int(getattr(row, "user_id", 0) or 0)
        entity = entities.get(user_id)
        items.append(
            Participant(
                id=user_id,
                user_id=user_id,
                chat_id=chat_id,
                peer=_admin.peer_row(entity) if entity is not None else None,
                username=getattr(entity, "username", None),
                name=_admin.display_name(entity),
                is_bot=bool(getattr(entity, "bot", False)),
                status="member",
                date=fmt_dt(getattr(row, "date", None)),
                date_unix=to_unix(getattr(row, "date", None)),
                about=getattr(row, "about", None),
                approved_by=getattr(row, "approved_by", None),
                via_link=req.via_link,
            )
        )
        last = row
    next_state: dict[str, Any] = {}
    if last is not None:
        entity = entities.get(int(getattr(last, "user_id", 0) or 0))
        next_state = {
            "date": to_unix(getattr(last, "date", None)) or 0,
            "user": int(getattr(last, "user_id", 0) or 0),
            "hash": int(getattr(entity, "access_hash", 0) or 0),
        }
    return build_page(
        items,
        op=op,
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", 0) or 0),
    )


async def list_members(ctx: OpContext, req: MemberListReq) -> Page[Participant]:
    """One page of a chat's participants, wrapper and all."""
    from telethon.tl.functions import channels as fn

    limit, state = _admin.window(ctx, "chat.member.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.via_link:
        return await _importers(ctx, req, peer, limit, state, "chat.member.list")

    name = "bots" if req.bots else ("admins" if req.admins else req.filter)
    if not _admin.is_channel(peer):
        # A basic group has no participant paging at all: the whole list
        # arrives inside chatFull, and slicing it here is the only offset.
        full, entity, entities = await _admin.full_chat(ctx, peer)
        holder = getattr(full, "participants", None)
        rows = list(getattr(holder, "participants", None) or [])
        if name == "admins":
            rows = [r for r in rows if type(r).__name__ != "ChatParticipant"]
        if name == "bots":
            rows = [
                r
                for r in rows
                if getattr(entities.get(int(getattr(r, "user_id", 0) or 0)), "bot", False)
            ]
        default = _rights.model_from_banned(getattr(entity, "default_banned_rights", None))
        offset = int(state.get("offset", 0) or 0)
        window = rows[offset : offset + limit]
        items = [
            _admin.participant_model(
                row, chat_id=chat_id, entities=entities, default_banned=default
            )
            for row in window
        ]
        return build_page(
            items,
            op="chat.member.list",
            kind=PageKind.PARTICIPANTS,
            state={"offset": offset + len(window)},
            account=ctx.account,
            has_more=offset + len(window) < len(rows),
            total=len(rows),
        )

    offset = int(state.get("offset", 0) or 0)
    reply = await _admin.client(ctx)(
        fn.GetParticipantsRequest(
            channel=_admin.input_channel(peer),
            filter=_participants_filter(name, req.search, req.topic),
            offset=offset,
            limit=limit,
            hash=0,
        )
    )
    entities = _admin.entity_map(reply)
    default = await _default_banned(ctx, peer) if name in ("banned", "restricted") else None
    rows = list(getattr(reply, "participants", None) or [])
    if name == "restricted":
        rows = [r for r in rows if not getattr(r, "left", False)]
    elif name == "banned":
        rows = [r for r in rows if getattr(r, "left", False) or True]
    items = [
        _admin.participant_model(row, chat_id=chat_id, entities=entities, default_banned=default)
        for row in rows
    ]
    return build_page(
        items,
        op="chat.member.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(rows)},
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", 0) or 0),
    )


SPEC_MEMBER_LIST = OperationSpec(
    id="chat.member.list",
    request=MemberListReq,
    response=Page[Participant],
    impl=list_members,
    summary="List members with the participant filters the API offers",
    description=(
        "Every row keeps its `ChannelParticipant*` wrapper: status, rank, "
        "join date, inviter, promoter and both rights masks. `--filter "
        "kicked` is people who were removed; `--filter restricted` is people "
        "still in the chat with a mask on them. A chat with "
        "`participants_hidden` answers PERMISSION_DENIED rather than an "
        "empty page, because "
        "“nobody is in this group” would be a lie."
    ),
    aliases=("chat.members",),
    legacy_paths=("chat members",),
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "username", "name", "status", "rank"),
    headers=("ID", "Username", "Name", "Status", "Rank"),
    example={"items": [_EXAMPLE_MEMBER], "has_more": False, "total": 1},
    example_args="chat member list @mygroup --filter admins",
    covers=(
        "groups-channels-admin.banned-list",
        "groups-channels-admin.channel-subscriptions-admin",
        "groups-channels-admin.invite-link-importers",
        "groups-channels-admin.member-mention-autocomplete",
        "groups-channels-admin.members-list",
    ),
    covers_partial=("groups-channels-admin.admin-list",),
    coverage_note="`--filter admins` lists them; `chat admin list` is the primary owner.",
)


# ---------------------------------------------------------------------------
# chat member get
# ---------------------------------------------------------------------------


class MemberGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="The member.")]


async def get_member(ctx: OpContext, req: MemberGetReq) -> Participant:
    """One member's status, rights, rank, join date and inviter."""
    peer = await _send.resolve(ctx, req.chat)
    user = await _send.resolve(ctx, req.user)
    reply = await _member_of(ctx, peer, user)
    entities = _admin.entity_map(reply)
    return _admin.participant_model(
        reply.participant,
        chat_id=_send.peer_id_of(peer),
        entities=entities,
        default_banned=await _default_banned(ctx, peer),
    )


SPEC_MEMBER_GET = OperationSpec(
    id="chat.member.get",
    request=MemberGetReq,
    response=Participant,
    impl=get_member,
    summary="One member's status, rights, rank, join date and inviter",
    description=(
        "`effective_permissions` is the chat's defaults patched with this "
        "member's own mask, in the same allow-polarity vocabulary "
        "`chat permission get` prints — which is the answer to “what may "
        "this person actually do” rather than “what did an admin type”."
    ),
    columns=("id", "name", "status", "rank"),
    example=_EXAMPLE_MEMBER,
    example_args="chat member get @mygroup @alice",
    empty_exit=EXIT_EMPTY,
    covers=(
        "groups-channels-admin.member-get",
        "groups-channels-admin.member-invited-by",
        "groups-channels-admin.member-permissions-view",
    ),
)


# ---------------------------------------------------------------------------
# chat member add
# ---------------------------------------------------------------------------


class MemberAddReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[
        list[PeerRef], arg(1, metavar="USER", kind="user", variadic=True, help="Who to add.")
    ] = []
    forward_history: Annotated[
        int,
        opt("--forward-history", metavar="N", help="Basic groups: past messages the member sees."),
    ] = 0
    invite_link_fallback: Annotated[
        bool,
        opt("--invite-link-fallback", help="DM the invite link to anyone who could not be added."),
    ] = False


async def add_members(ctx: OpContext, req: MemberAddReq) -> MembersAdded:
    """Add members, and say by name who could not be added and why.

    `messages.invitedUsers.missing_invitees` is reported verbatim: "added 3
    of 5" with no names is not something a script can act on, and privacy
    settings make partial failure the normal case rather than the exception.
    """
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    if not req.user:
        raise UsageError("name at least one user to add", field="user")
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    users = [_admin.input_user(await _send.resolve(ctx, ref)) for ref in req.user]
    handle = _admin.client(ctx)

    missing: list[MissingInvitee] = []
    if _admin.is_channel(peer):
        reply = await handle(
            chan_fn.InviteToChannelRequest(channel=_admin.input_channel(peer), users=users)
        )
        missing = _missing(reply)
    else:
        for user in users:
            reply = await handle(
                msg_fn.AddChatUserRequest(
                    chat_id=_admin.small_chat_id(peer),
                    user_id=user,
                    fwd_limit=req.forward_history,
                )
            )
            missing.extend(_missing(reply))

    refused = {item.user_id for item in missing}
    added = [int(getattr(u, "user_id", 0) or 0) for u in users]
    added = [uid for uid in added if uid not in refused]
    invited: list[int] = []
    if missing and req.invite_link_fallback:
        invited = await _dm_the_link(ctx, peer, missing)
    ctx.emit("chat_members_added", {"chat_id": chat_id, "added": added})
    return MembersAdded(chat_id=chat_id, added=added, missing=missing, invited_by_link=invited)


async def _dm_the_link(ctx: OpContext, peer: Any, missing: list[MissingInvitee]) -> list[int]:
    """`--invite-link-fallback`: send the link to whoever could not be added.

    Opt-in because it *sends a message* on the owner's behalf to people who
    have already expressed a privacy preference against being added.
    """
    from telethon.tl.functions import messages as fn

    handle = _admin.client(ctx)
    invite = await handle(fn.ExportChatInviteRequest(peer=peer))
    link = str(getattr(invite, "link", "") or "")
    reached: list[int] = []
    for item in missing:
        try:
            target = await _send.resolve(ctx, str(item.user_id))
            await handle.send_message(target, link)
            reached.append(item.user_id)
        except Exception as exc:
            ctx.warn(f"could not send the invite link to {item.user_id}: {exc}")
    return reached


SPEC_MEMBER_ADD = OperationSpec(
    id="chat.member.add",
    request=MemberAddReq,
    response=MembersAdded,
    impl=add_members,
    summary="Add members to a group or channel",
    description=(
        "`missing` carries `messages.invitedUsers.missing_invitees` verbatim "
        "— one `{user_id, reason}` per refusal, with reason in "
        "privacy-restricted, premium-would-allow-invite or "
        "premium-required-for-pm. `--invite-link-fallback` DMs the link to "
        "them, and is opt-in because it sends a message on your behalf."
    ),
    mutating=True,
    rate_class="bulk",
    columns=("chat_id", "added"),
    example={"chat_id": -1001500, "added": [4242], "missing": []},
    example_args="chat member add @mygroup @alice @carol",
    covers=(
        "groups-channels-admin.add-members",
        "groups-channels-admin.add-members-failure-report",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# Moderation: remove / ban / unban
# ---------------------------------------------------------------------------


async def _purge(ctx: OpContext, peer: Any, user: Any, ids: list[int]) -> int:
    """`--purge` / `--messages`: everything that member wrote, or named ids."""
    from telethon.tl.functions import channels as fn

    channel = _admin.input_channel(peer)
    removed = 0
    if ids:
        reply = await _admin.client(ctx)(fn.DeleteMessagesRequest(channel=channel, id=list(ids)))
        removed += int(getattr(reply, "pts_count", 0) or 0)
    return removed


async def _purge_all(ctx: OpContext, peer: Any, user: Any) -> int:
    from telethon.tl.functions import channels as fn

    channel = _admin.input_channel(peer)
    return await _admin.affected_loop(
        ctx,
        lambda _offset: fn.DeleteParticipantHistoryRequest(channel=channel, participant=user),
    )


async def _report_spam(ctx: OpContext, peer: Any, user: Any, ids: list[int]) -> bool:
    from telethon.tl.functions import channels as fn

    await _admin.client(ctx)(
        fn.ReportSpamRequest(
            channel=_admin.input_channel(peer), participant=user, id=list(ids) or []
        )
    )
    return True


class MemberRemoveReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[
        list[PeerRef], arg(1, metavar="USER", kind="user", variadic=True, help="Who to remove.")
    ] = []
    purge: Annotated[bool, opt("--purge", help="Delete everything they ever sent here.")] = False
    messages: Annotated[
        list[int], opt("--messages", metavar="ID", help="Also delete these message ids.")
    ] = []
    report: Annotated[bool, opt("--report", help="Report the messages as spam too.")] = False


async def remove_member(ctx: OpContext, req: MemberRemoveReq) -> list[MemberResult]:
    """Kick: ban, then immediately lift the ban, so they may rejoin.

    Telegram has no kick method. `editBanned(view_messages=True)` followed by
    an empty mask is what every client calls a kick, and the second call is
    the whole difference from `chat member ban`.
    """
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    if not req.user:
        raise UsageError("name at least one user to remove", field="user")
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    handle = _admin.client(ctx)
    out: list[MemberResult] = []
    for ref in req.user:
        user = await _send.resolve(ctx, ref)
        if _admin.is_channel(peer):
            channel = _admin.input_channel(peer)
            await handle(
                chan_fn.EditBannedRequest(
                    channel=channel,
                    participant=user,
                    banned_rights=_rights.build_banned_rights([]),
                )
            )
            await asyncio.sleep(0)
            await handle(
                chan_fn.EditBannedRequest(
                    channel=channel,
                    participant=user,
                    banned_rights=_rights.build_banned_rights(_rights.all_allowed()),
                )
            )
        else:
            await handle(
                msg_fn.DeleteChatUserRequest(
                    chat_id=_admin.small_chat_id(peer), user_id=_admin.input_user(user)
                )
            )
        purged = await _purge_all(ctx, peer, user) if req.purge else None
        if req.messages:
            purged = (purged or 0) + await _purge(ctx, peer, user, req.messages)
        reported = (
            await _report_spam(ctx, peer, user, req.messages)
            if (req.report and _admin.is_channel(peer))
            else None
        )
        out.append(
            MemberResult(
                chat_id=chat_id,
                user_id=abs(_send.peer_id_of(user)),
                removed=True,
                purged_messages=purged,
                reported=reported,
            )
        )
    ctx.emit("chat_members_removed", {"chat_id": chat_id, "users": [r.user_id for r in out]})
    return out


SPEC_MEMBER_REMOVE = OperationSpec(
    id="chat.member.remove",
    request=MemberRemoveReq,
    response=list[MemberResult],
    impl=remove_member,
    summary="Remove (kick) a member; they can rejoin",
    description=(
        "A kick is `editBanned(view_messages)` followed by an empty mask, so "
        "the person may come back. Use `chat member ban` to keep them out. "
        "`--purge` drains `messages.affectedHistory` until the server stops "
        "handing back an offset."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("chat_id", "user_id", "removed"),
    example=[{"chat_id": -1001500, "user_id": 4242, "removed": True}],
    example_args="chat member remove @mygroup @spammer --purge --yes",
    covers=("groups-channels-admin.remove-member",),
    covers_partial=(
        "groups-channels-admin.delete-member-history",
        "groups-channels-admin.report-member",
    ),
    coverage_note=(
        "`--purge`/`--report` are the moderate box; the standalone commands "
        "are `chat member delete-history` and `chat member report`."
    ),
    tags=frozenset({"visible-to-others"}),
)


class MemberBanReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[
        list[PeerRef], arg(1, metavar="USER", kind="user", variadic=True, help="Who to ban.")
    ] = []
    until: Annotated[
        str | None,
        opt("--until", metavar="WHEN", help="Ban expiry; 0, under 30s or over 366d = forever."),
    ] = None
    purge: Annotated[bool, opt("--purge", help="Delete everything they ever sent here.")] = False
    messages: Annotated[
        list[int], opt("--messages", metavar="ID", help="Also delete these message ids.")
    ] = []
    report: Annotated[bool, opt("--report", help="Report the messages as spam too.")] = False


async def ban_member(ctx: OpContext, req: MemberBanReq) -> list[MemberResult]:
    """The GUI's moderate box: ban, purge and report in one confirmation."""
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    if not req.user:
        raise UsageError("name at least one user to ban", field="user")
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    until = _rights.parse_until(req.until)
    label, label_unix = _rights.until_label(until)
    handle = _admin.client(ctx)
    out: list[MemberResult] = []
    for ref in req.user:
        user = await _send.resolve(ctx, ref)
        if _admin.is_channel(peer):
            await handle(
                chan_fn.EditBannedRequest(
                    channel=_admin.input_channel(peer),
                    participant=user,
                    banned_rights=_rights.build_banned_rights([], until=until),
                )
            )
        else:
            # A basic group has no ban at all: removal is the strongest thing
            # the peer shape supports, and saying so beats pretending.
            ctx.warn("a basic group cannot ban; the member was removed instead")
            await handle(
                msg_fn.DeleteChatUserRequest(
                    chat_id=_admin.small_chat_id(peer), user_id=_admin.input_user(user)
                )
            )
        purged = await _purge_all(ctx, peer, user) if req.purge else None
        if req.messages:
            purged = (purged or 0) + await _purge(ctx, peer, user, req.messages)
        reported = (
            await _report_spam(ctx, peer, user, req.messages)
            if (req.report and _admin.is_channel(peer))
            else None
        )
        out.append(
            MemberResult(
                chat_id=chat_id,
                user_id=abs(_send.peer_id_of(user)),
                banned=True,
                until=label,
                until_unix=label_unix,
                purged_messages=purged,
                reported=reported,
            )
        )
    ctx.emit("chat_members_banned", {"chat_id": chat_id, "users": [r.user_id for r in out]})
    return out


SPEC_MEMBER_BAN = OperationSpec(
    id="chat.member.ban",
    request=MemberBanReq,
    response=list[MemberResult],
    impl=ban_member,
    summary="Ban a member, optionally purging and reporting them in one step",
    description=(
        "`participant` is an `InputPeer`, so a channel posting in the group "
        "— or an anonymous admin's channel — can be banned as well as a "
        "user. `--until` follows Telegram's own rounding: 0, under 30 "
        "seconds and over 366 days all mean forever."
    ),
    aliases=("chat.ban",),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("chat_id", "user_id", "banned", "until"),
    example=[{"chat_id": -1001500, "user_id": 4242, "banned": True}],
    example_args="chat member ban @mygroup @spammer --purge --report --yes",
    covers=("groups-channels-admin.ban-member", "groups-channels-admin.moderate-member"),
    covers_partial=(
        "groups-channels-admin.delete-member-history",
        "groups-channels-admin.report-member",
    ),
    coverage_note=(
        "The moderate box bundles them; `chat member delete-history` and "
        "`chat member report` own the standalone ids."
    ),
    tags=frozenset({"visible-to-others"}),
)


class MemberUnbanReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[
        list[PeerRef], arg(1, metavar="USER", kind="user", variadic=True, help="Who to unban.")
    ] = []


async def unban_member(ctx: OpContext, req: MemberUnbanReq) -> list[MemberResult]:
    """Send an all-clear mask. The user may return; they are not re-added."""
    from telethon.tl.functions import channels as fn

    if not req.user:
        raise UsageError("name at least one user to unban", field="user")
    peer = await _send.resolve(ctx, req.chat)
    channel = _admin.input_channel(peer)
    chat_id = _send.peer_id_of(peer)
    out: list[MemberResult] = []
    for ref in req.user:
        user = await _send.resolve(ctx, ref)
        await _admin.client(ctx)(
            fn.EditBannedRequest(
                channel=channel,
                participant=user,
                banned_rights=_rights.build_banned_rights(_rights.all_allowed()),
            )
        )
        out.append(MemberResult(chat_id=chat_id, user_id=abs(_send.peer_id_of(user)), banned=False))
    return out


SPEC_MEMBER_UNBAN = OperationSpec(
    id="chat.member.unban",
    request=MemberUnbanReq,
    response=list[MemberResult],
    impl=unban_member,
    summary="Lift a ban or a restriction",
    description=(
        "Sends an all-clear mask, which takes the user off the Removed and "
        "Restricted lists. It does not put them back in the chat."
    ),
    mutating=True,
    columns=("chat_id", "user_id", "banned"),
    example=[{"chat_id": -1001500, "user_id": 4242, "banned": False}],
    example_args="chat member unban @mygroup @alice",
    covers=("groups-channels-admin.unban-member",),
)


# ---------------------------------------------------------------------------
# chat member restrict
# ---------------------------------------------------------------------------


class MemberRestrictReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Supergroup.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="The member.")]
    deny: Annotated[
        str | None, opt("--deny", metavar="RIGHTS", help="Rights to take away, comma-separated.")
    ] = None
    allow: Annotated[
        str | None, opt("--allow", metavar="RIGHTS", help="Rights to give back, comma-separated.")
    ] = None
    none: Annotated[bool, opt("--none", help="Read-only: deny everything but view-messages.")] = (
        False
    )
    everything: Annotated[bool, opt("--all", help="Allow everything.")] = False
    clear: Annotated[bool, opt("--clear", help="Drop the mask; fall back to the chat default.")] = (
        False
    )
    replace: Annotated[
        bool, opt("--replace", help="Treat --deny/--allow as the whole mask, not a patch.")
    ] = False
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", help="When the restriction lapses.")
    ] = None
    purge: Annotated[bool, opt("--purge", help="Also delete everything they sent.")] = False


async def restrict_member(ctx: OpContext, req: MemberRestrictReq) -> MemberResult:
    """Read the member's current mask, patch it, and send it back complete.

    `channels.editBanned` replaces the whole mask. Sending only the flags the
    caller named would hand back every restriction they did not mention —
    which is a moderation action nobody asked for.
    """
    from telethon.tl.functions import channels as fn

    peer = await _send.resolve(ctx, req.chat)
    if not _admin.is_channel(peer):
        raise UsageError(
            "a basic group has no per-member mask; use `chat permission set`, "
            "or convert the group with `chat convert <chat> supergroup`",
            field="chat",
        )
    user = await _send.resolve(ctx, req.user)
    reply = await _member_of(ctx, peer, user)
    current = _rights.model_from_banned(getattr(reply.participant, "banned_rights", None))
    base = set(_rights.granted_names(current, mask="member")) if current else set()
    if current is None:
        base = set(_rights.all_allowed())

    deny = _rights.parse_names(req.deny, mask="member", field="deny")
    allow = _rights.parse_names(req.allow, mask="member", field="allow")
    _rights.require_supported(deny + allow, mask="member")

    if req.clear or req.everything:
        allowed = set(_rights.all_allowed())
    elif req.none:
        allowed = set(_rights.read_only())
    elif req.replace:
        allowed = set(allow) if allow else set(_rights.all_allowed())
        allowed -= set(deny)
    else:
        allowed = (base | set(allow)) - set(deny)

    until = _rights.parse_until(req.until)
    label, label_unix = _rights.until_label(until)
    await _admin.client(ctx)(
        fn.EditBannedRequest(
            channel=_admin.input_channel(peer),
            participant=user,
            banned_rights=_rights.build_banned_rights(allowed, until=until),
        )
    )
    purged = await _purge_all(ctx, peer, user) if req.purge else None
    order = list(_rights.MEMBER_MASK)
    return MemberResult(
        chat_id=_send.peer_id_of(peer),
        user_id=abs(_send.peer_id_of(user)),
        allow=[n for n in order if n in allowed],
        deny=[n for n in order if n not in allowed and n in _rights.all_allowed()],
        until=label,
        until_unix=label_unix,
        purged_messages=purged,
    )


SPEC_MEMBER_RESTRICT = OperationSpec(
    id="chat.member.restrict",
    request=MemberRestrictReq,
    response=MemberResult,
    impl=restrict_member,
    summary="Restrict what one member may do, with an expiry",
    description=(
        "Read-modify-write of the member's current mask: `--deny` and "
        "`--allow` patch it, `--replace` supplies it whole, `--none` is "
        "read-only and `--clear` drops it back to the chat default. Names "
        "come from `chat permission list --mask member`."
    ),
    mutating=True,
    columns=("chat_id", "user_id", "until"),
    example={"chat_id": -1001500, "user_id": 4242, "deny": ["send-media"], "allow": []},
    example_args="chat member restrict @mygroup @alice --deny send-media --until 7d",
    covers=("groups-channels-admin.restrict-member",),
)


# ---------------------------------------------------------------------------
# chat member edit / delete-history / report
# ---------------------------------------------------------------------------


class MemberEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="The member.")]
    rank: Annotated[
        str | None,
        opt("--rank", metavar="TITLE", help="Custom title, max 16 chars."),
    ] = None
    free_messages: Annotated[
        bool | None,
        opt("--free-messages", help="Let this user message the channel without paying Stars."),
    ] = None
    refund: Annotated[
        bool, opt("--refund", help="With --free-messages: refund the Stars already paid.")
    ] = False


async def edit_member(ctx: OpContext, req: MemberEditReq) -> MemberResult:
    """A member's custom rank, and their paid-message exception."""
    from telethon.tl.functions import account as acct_fn
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    user = await _send.resolve(ctx, req.user)
    handle = _admin.client(ctx)
    if req.rank is None and req.free_messages is None:
        raise UsageError("nothing to change: pass --rank or --free-messages", field="rank")

    if req.rank is not None:
        await handle(fn.EditChatParticipantRankRequest(peer=peer, participant=user, rank=req.rank))
    if req.free_messages is not None:
        await handle(
            acct_fn.ToggleNoPaidMessagesExceptionRequest(
                user_id=_admin.input_user(user),
                parent_peer=peer,
                refund_charged=req.refund or None,
                require_payment=None if req.free_messages else True,
            )
        )
    return MemberResult(
        chat_id=_send.peer_id_of(peer),
        user_id=abs(_send.peer_id_of(user)),
        rank=req.rank,
        free_messages=req.free_messages,
    )


SPEC_MEMBER_EDIT = OperationSpec(
    id="chat.member.edit",
    request=MemberEditReq,
    response=MemberResult,
    impl=edit_member,
    summary="Edit a member's custom rank and paid-message exception",
    description=(
        "The rank shows up as `message.from_rank` in supergroups and "
        "`chatParticipant.rank` in basic groups; `chat admin promote --rank` "
        "writes the same field for admins."
    ),
    mutating=True,
    columns=("chat_id", "user_id", "rank"),
    example={"chat_id": -1001500, "user_id": 4242, "rank": "moderator"},
    example_args="chat member edit @mygroup @alice --rank moderator",
    covers=("groups-channels-admin.paid-messages-price",),
    covers_partial=("groups-channels-admin.member-tag-rank",),
    coverage_note="Ranks for plain members; `chat admin promote --rank` owns the admin half.",
)


class MemberPurgeReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Supergroup or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Whose messages to delete.")]


async def delete_member_history(ctx: OpContext, req: MemberPurgeReq) -> MemberResult:
    """Delete every message one member ever sent, draining the offset loop."""
    peer = await _send.resolve(ctx, req.chat)
    if not _admin.is_channel(peer):
        raise UsageError(
            "only supergroups and channels support deleting one member's history", field="chat"
        )
    user = await _send.resolve(ctx, req.user)
    deleted = await _purge_all(ctx, peer, user)
    return MemberResult(
        chat_id=_send.peer_id_of(peer),
        user_id=abs(_send.peer_id_of(user)),
        deleted=deleted,
        purged_messages=deleted,
    )


SPEC_MEMBER_DELETE_HISTORY = OperationSpec(
    id="chat.member.delete-history",
    request=MemberPurgeReq,
    response=MemberResult,
    impl=delete_member_history,
    summary="Delete every message one member ever sent",
    description=(
        "`channels.deleteParticipantHistory` answers with "
        "`messages.affectedHistory` and an offset to resume from; the loop "
        "runs in the daemon until the offset is 0, because deleting the "
        "first hundred messages and reporting success is not deleting a "
        "history."
    ),
    aliases=("chat.member.purge",),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("chat_id", "user_id", "deleted"),
    example={"chat_id": -1001500, "user_id": 4242, "deleted": 42},
    example_args="chat member delete-history @mygroup @spammer --yes",
    covers=("groups-channels-admin.delete-member-history",),
)


class MemberReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Supergroup or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Who to report.")]
    messages: Annotated[
        list[int], opt("--messages", metavar="ID", help="Message ids to attach to the report.")
    ] = []


async def report_member(ctx: OpContext, req: MemberReportReq) -> MemberResult:
    """Report a member as spam. Reporting the *chat* is `chat report`."""
    peer = await _send.resolve(ctx, req.chat)
    user = await _send.resolve(ctx, req.user)
    await _report_spam(ctx, peer, user, req.messages)
    return MemberResult(
        chat_id=_send.peer_id_of(peer), user_id=abs(_send.peer_id_of(user)), reported=True
    )


SPEC_MEMBER_REPORT = OperationSpec(
    id="chat.member.report",
    request=MemberReportReq,
    response=MemberResult,
    impl=report_member,
    summary="Report a member (and optionally their messages) as spam",
    mutating=True,
    columns=("chat_id", "user_id", "reported"),
    example={"chat_id": -1001500, "user_id": 4242, "reported": True},
    example_args="chat member report @mygroup @spammer --messages 918 --yes",
    covers=("groups-channels-admin.report-member",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# chat request list / approve / deny
# ---------------------------------------------------------------------------


class RequestListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    link: Annotated[
        str | None, opt("--link", metavar="LINK", help="Only requests from this invite link.")
    ] = None
    search: Annotated[str, opt("--search", "-s", metavar="TEXT", help="Filter by name.")] = ""
    approve: Annotated[
        list[PeerRef], opt("--approve", metavar="USER", kind="user", help="Approve one requester.")
    ] = []
    decline: Annotated[
        list[PeerRef], opt("--decline", metavar="USER", kind="user", help="Decline one requester.")
    ] = []
    approve_all: Annotated[bool, opt("--approve-all", help="Approve every pending request.")] = (
        False
    )
    decline_all: Annotated[bool, opt("--decline-all", help="Decline every pending request.")] = (
        False
    )
    community: Annotated[
        bool, opt("--community", help="Target a layer-229 community's peer-link requests.")
    ] = False


async def _hide_requests(
    ctx: OpContext,
    peer: Any,
    *,
    approve: list[PeerRef],
    decline: list[PeerRef],
    approve_all: bool,
    decline_all: bool,
    link: str | None,
) -> RequestResult:
    """Answer join requests, collecting per-user failures instead of aborting."""
    from telethon.tl.functions import messages as fn

    handle = _admin.client(ctx)
    result = RequestResult(chat_id=_send.peer_id_of(peer))
    for refs, approved in ((approve, True), (decline, False)):
        for ref in refs:
            user = await _send.resolve(ctx, ref)
            user_id = abs(_send.peer_id_of(user))
            try:
                await handle(
                    fn.HideChatJoinRequestRequest(
                        peer=peer, user_id=_admin.input_user(user), approved=approved
                    )
                )
            except Exception as exc:
                result.failed.append(MissingInvitee(user_id=user_id, reason=str(exc)))
                continue
            (result.approved if approved else result.denied).append(user_id)
    if approve_all or decline_all:
        await handle(fn.HideAllChatJoinRequestsRequest(peer=peer, approved=approve_all, link=link))
        result.all = True
    return result


async def list_requests(ctx: OpContext, req: RequestListReq) -> Page[JoinRequest]:
    """Pending join requests, and — with the answer flags — the answer to them.

    Listing is a read, so the op is not marked mutating and `--dry-run
    chat request list` keeps listing. The answer flags check `ctx.dry_run`
    themselves and warn instead of firing (the same rule `folder list --tags`
    follows).
    """
    from datetime import datetime, timezone

    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.community:
        from tlgr.ops._layer import community_gap

        community_gap("chat request list --community", "communities.getPeerLinkRequests")

    limit, state = _admin.window(ctx, "chat.request.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)

    answering = bool(req.approve or req.decline or req.approve_all or req.decline_all)
    if answering:
        if ctx.dry_run:
            ctx.warn("--dry-run: the join requests were listed, not answered")
        else:
            answered = await _hide_requests(
                ctx,
                peer,
                approve=req.approve,
                decline=req.decline,
                approve_all=req.approve_all,
                decline_all=req.decline_all,
                link=req.link,
            )
            ctx.emit(
                "chat_join_requests_answered",
                {
                    "chat_id": answered.chat_id,
                    "approved": answered.approved,
                    "denied": answered.denied,
                },
            )

    offset_date = state.get("date")
    offset_user: Any = types.InputUserEmpty()
    if state.get("user"):
        offset_user = types.InputUser(
            user_id=int(state["user"]), access_hash=int(state.get("hash", 0) or 0)
        )
    reply = await _admin.client(ctx)(
        fn.GetChatInviteImportersRequest(
            peer=peer,
            offset_date=datetime.fromtimestamp(offset_date, tz=timezone.utc)
            if offset_date
            else None,
            offset_user=offset_user,
            limit=limit,
            requested=True,
            link=req.link,
            q=req.search or None,
        )
    )
    entities = _admin.entity_map(reply)
    items: list[JoinRequest] = []
    last: Any = None
    for row in getattr(reply, "importers", None) or []:
        user_id = int(getattr(row, "user_id", 0) or 0)
        entity = entities.get(user_id)
        items.append(
            JoinRequest(
                user_id=user_id,
                username=getattr(entity, "username", None),
                name=_admin.display_name(entity),
                date=fmt_dt(getattr(row, "date", None)),
                date_unix=to_unix(getattr(row, "date", None)),
                about=getattr(row, "about", None),
                via_link=req.link,
                approved_by=getattr(row, "approved_by", None),
            )
        )
        last = row
    next_state: dict[str, Any] = {}
    if last is not None:
        entity = entities.get(int(getattr(last, "user_id", 0) or 0))
        next_state = {
            "date": to_unix(getattr(last, "date", None)) or 0,
            "user": int(getattr(last, "user_id", 0) or 0),
            "hash": int(getattr(entity, "access_hash", 0) or 0),
        }
    return build_page(
        items,
        op="chat.request.list",
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", 0) or 0),
    )


SPEC_REQUEST_LIST = OperationSpec(
    id="chat.request.list",
    request=RequestListReq,
    response=Page[JoinRequest],
    impl=list_requests,
    summary="List pending join requests",
    description=(
        "The answer flags (`--approve`, `--decline`, `--approve-all`, "
        "`--decline-all`) run before the listing and honour `--dry-run` "
        "themselves, so listing keeps working under a dry run instead of "
        "printing a stub."
    ),
    aliases=("chat.join-requests",),
    paginated=PageKind.PARTICIPANTS,
    columns=("user_id", "username", "name", "date"),
    headers=("User", "Username", "Name", "Requested"),
    example={
        "items": [{"user_id": 4242, "username": "alice", "name": "Alice"}],
        "has_more": False,
    },
    example_args="chat request list @mygroup",
    covers=("dialogs.community-join-requests", "groups-channels-admin.join-request-list"),
    covers_partial=("dialogs.join-requests-badge",),
    coverage_note="The badge itself is `chat list --with-join-requests`; this is the queue.",
    tags=frozenset({"mutating-checked"}),
)


class RequestAnswerReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[
        list[PeerRef], arg(1, metavar="USER", kind="user", variadic=True, help="Which requesters.")
    ] = []
    everyone: Annotated[bool, opt("--all", help="Answer every pending request.")] = False
    link: Annotated[
        str | None, opt("--link", metavar="LINK", help="With --all: only this invite link.")
    ] = None


async def approve_requests(ctx: OpContext, req: RequestAnswerReq) -> RequestResult:
    """Approve join requests, one by one or the whole queue."""
    peer = await _send.resolve(ctx, req.chat)
    if not req.user and not req.everyone:
        raise UsageError("name a user, or pass --all", field="user")
    result = await _hide_requests(
        ctx,
        peer,
        approve=list(req.user),
        decline=[],
        approve_all=req.everyone,
        decline_all=False,
        link=req.link,
    )
    ctx.emit(
        "chat_join_requests_answered",
        {"chat_id": result.chat_id, "approved": result.approved, "all": result.all},
    )
    return result


SPEC_REQUEST_APPROVE = OperationSpec(
    id="chat.request.approve",
    request=RequestAnswerReq,
    response=RequestResult,
    impl=approve_requests,
    summary="Approve join requests",
    description="Per-user failures are collected in `failed` rather than aborting the batch.",
    mutating=True,
    rate_class="bulk",
    columns=("chat_id", "approved"),
    example={"chat_id": -1001500, "approved": [4242], "failed": []},
    example_args="chat request approve @mygroup @alice",
    covers=("groups-channels-admin.join-request-approve-all",),
    covers_partial=("groups-channels-admin.join-request-approve-one",),
    coverage_note="Approving one is here; declining one is `chat request deny`.",
    tags=frozenset({"visible-to-others"}),
)


async def deny_requests(ctx: OpContext, req: RequestAnswerReq) -> RequestResult:
    """Decline join requests. A declined user may ask again after a cooldown."""
    peer = await _send.resolve(ctx, req.chat)
    if not req.user and not req.everyone:
        raise UsageError("name a user, or pass --all", field="user")
    result = await _hide_requests(
        ctx,
        peer,
        approve=[],
        decline=list(req.user),
        approve_all=False,
        decline_all=req.everyone,
        link=req.link,
    )
    ctx.emit(
        "chat_join_requests_answered",
        {"chat_id": result.chat_id, "denied": result.denied, "all": result.all},
    )
    return result


SPEC_REQUEST_DENY = OperationSpec(
    id="chat.request.deny",
    request=RequestAnswerReq,
    response=RequestResult,
    impl=deny_requests,
    summary="Decline join requests",
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("chat_id", "denied"),
    example={"chat_id": -1001500, "denied": [4242], "failed": []},
    example_args="chat request deny @mygroup --all --yes",
    covers=("groups-channels-admin.join-request-approve-one",),
    covers_partial=("groups-channels-admin.join-request-approve-all",),
    coverage_note="Declining the whole queue is here too; `chat request approve` owns the other id.",
)
