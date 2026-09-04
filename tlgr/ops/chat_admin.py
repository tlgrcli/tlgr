"""`chat admin *`, `chat permission *`, `chat admin-log *` and `chat transfer`.

The rights vocabulary is the spine of this module and it lives in
`ops/_rights.py`, so `chat admin promote --rights`, `chat member restrict
--deny` and `chat permission set --allow` cannot drift apart. Three rules
follow from the API rather than from taste.

* **Both masks are replaced, never patched, server-side.** Every writer here
  therefore reads the current mask first and sends a complete one.
* **A right this Telethon has no field for is refused, not dropped.**
  `manage-linked-peers` and `manage-welcome-messages` are layer-229 flags;
  asking for one exits 13 with the reason instead of quietly granting less
  than the caller asked for.
* **The admin log is normalised but never lossy.** Fifty-odd
  `channelAdminLogEventAction*` constructors become `{action, prev, new}` so
  a script can switch on one string, and `raw_type` keeps the TL name so
  nothing that was in the reply is unavailable.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.admin import (
    AdminLogEvent,
    AdminResult,
    AntiSpamReport,
    Participant,
    PermissionResult,
    PermissionView,
    RightInfo,
    TransferResult,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _admin, _rights, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_ADMIN: dict[str, Any] = {
    "id": 4242,
    "user_id": 4242,
    "name": "Alice",
    "status": "admin",
    "rank": "moderator",
    "can_edit": True,
}

#: `--filter` names for the admin log → `ChannelAdminLogEventsFilter` flags.
#: Telethon's own `iter_admin_log` shifts four of them (restrict→ban,
#: unrestrict→unban, ban→kick, unban→unkick) and has no flag at all for
#: invites/send/forums/sub_extend/edit_rank, so tlgr builds the filter itself
#: and keeps the CLI names identical to the API's.
_LOG_FILTERS = {
    "join": "join",
    "leave": "leave",
    "invite": "invite",
    "ban": "ban",
    "unban": "unban",
    "kick": "kick",
    "unkick": "unkick",
    "promote": "promote",
    "demote": "demote",
    "info": "info",
    "settings": "settings",
    "pinned": "pinned",
    "edit": "edit",
    "delete": "delete",
    "group-call": "group_call",
    "invites": "invites",
    "send": "send",
    "forums": "forums",
    "sub-extend": "sub_extend",
    "edit-rank": "edit_rank",
}

_ACTION_PREFIX = "ChannelAdminLogEventAction"


def _kebab(name: str) -> str:
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("-")
        out.append(char.lower())
    return "".join(out)


def _plain(value: Any) -> Any:
    """A Telethon object as plain JSON-able data, losing nothing on the way.

    `prev`/`new` in an admin-log row can be a whole message, a rights mask or
    an invite link, and guessing which fields matter is how a log viewer ends
    up hiding the one field somebody needed.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, datetime):
        return fmt_dt(value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if item is not None}
    as_dict = getattr(value, "to_dict", None)
    if callable(as_dict):
        return _plain(as_dict())
    return str(value)


# ---------------------------------------------------------------------------
# chat admin list
# ---------------------------------------------------------------------------


class AdminListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    rights: Annotated[
        bool, opt("--rights/--no-rights", help="Expand each admin's mask into right names.")
    ] = True


async def _antispam_bot(ctx: OpContext, peer: Any) -> Participant | None:
    """The anti-spam bot, which the server never lists but the GUI shows.

    `channelFull.antispam` means Telegram's own bot is moderating; it holds
    admin rights and deletes messages, so leaving it out of "who administers
    this chat" would make the admin log's deletions look like they came from
    nobody.
    """
    from telethon.tl.functions import help as help_fn

    full, _entity, _entities = await _admin.full_chat(ctx, peer)
    if not getattr(full, "antispam", False):
        return None
    config = await _admin.client(ctx)(help_fn.GetAppConfigRequest(hash=0))
    bot_id = 0
    for item in getattr(getattr(config, "config", None), "value", None) or []:
        if getattr(item, "key", "") == "telegram_antispam_user_id":
            raw = getattr(getattr(item, "value", None), "value", 0)
            bot_id = int(float(raw or 0))
    if not bot_id:
        return None
    return Participant(
        id=bot_id,
        user_id=bot_id,
        chat_id=_send.peer_id_of(peer),
        name="Telegram Anti-Spam",
        is_bot=True,
        status="admin",
        rank="anti-spam",
    )


async def list_admins(ctx: OpContext, req: AdminListReq) -> Page[Participant]:
    """Administrators with their rights, ranks and who promoted them."""
    from telethon.tl import types
    from telethon.tl.functions import channels as fn

    limit, state = _admin.window(ctx, "chat.admin.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    offset = int(state.get("offset", 0) or 0)

    if not _admin.is_channel(peer):
        full, _entity, entities = await _admin.full_chat(ctx, peer)
        holder = getattr(full, "participants", None)
        rows = [
            row
            for row in (getattr(holder, "participants", None) or [])
            if type(row).__name__ != "ChatParticipant"
        ]
        window = rows[offset : offset + limit]
        items = [
            _admin.participant_model(row, chat_id=chat_id, entities=entities) for row in window
        ]
        return build_page(
            items,
            op="chat.admin.list",
            kind=PageKind.PARTICIPANTS,
            state={"offset": offset + len(window)},
            account=ctx.account,
            has_more=offset + len(window) < len(rows),
            total=len(rows),
        )

    reply = await _admin.client(ctx)(
        fn.GetParticipantsRequest(
            channel=_admin.input_channel(peer),
            filter=types.ChannelParticipantsAdmins(),
            offset=offset,
            limit=limit,
            hash=0,
        )
    )
    entities = _admin.entity_map(reply)
    items = [
        _admin.participant_model(row, chat_id=chat_id, entities=entities)
        for row in (getattr(reply, "participants", None) or [])
    ]
    if not req.rights:
        for item in items:
            item.admin_rights = None
    total = int(getattr(reply, "count", 0) or 0)
    if not offset:
        bot = await _antispam_bot(ctx, peer)
        if bot is not None:
            items.append(bot)
            total += 1
    return build_page(
        items,
        op="chat.admin.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(items)},
        account=ctx.account,
        limit=limit,
        total=total,
    )


SPEC_ADMIN_LIST = OperationSpec(
    id="chat.admin.list",
    request=AdminListReq,
    response=Page[Participant],
    impl=list_admins,
    summary="List administrators with their rights and ranks",
    description=(
        "The creator is reported as `status: creator`. When the chat has "
        "Telegram's aggressive anti-spam turned on, its bot is appended "
        "locally exactly as the GUI does — the server never lists it, and "
        "its deletions do show up in the admin log."
    ),
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "name", "status", "rank", "can_edit"),
    headers=("ID", "Name", "Status", "Rank", "Editable"),
    example={"items": [_EXAMPLE_ADMIN], "has_more": False, "total": 1},
    example_args="chat admin list @mygroup",
    covers=("groups-channels-admin.admin-list", "groups-channels-admin.basic-group-admin"),
)


# ---------------------------------------------------------------------------
# chat admin promote / demote
# ---------------------------------------------------------------------------


class AdminPromoteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Who to promote.")]
    rights: Annotated[
        str | None, opt("--rights", metavar="RIGHTS", help="The whole mask, comma-separated.")
    ] = None
    grant: Annotated[
        str | None, opt("--grant", metavar="RIGHTS", help="Add these to the current mask.")
    ] = None
    revoke: Annotated[
        str | None, opt("--revoke", metavar="RIGHTS", help="Remove these from the current mask.")
    ] = None
    everything: Annotated[bool, opt("--all", help="Grant every right you hold yourself.")] = False
    none: Annotated[bool, opt("--none", help="Empty mask (the same as `chat admin demote`).")] = (
        False
    )
    except_rights: Annotated[
        str | None, opt("--except", metavar="RIGHTS", help="With --all: withhold these.")
    ] = None
    rank: Annotated[
        str | None,
        opt("--rank", metavar="TITLE", help="Custom title, max 16 chars."),
    ] = None
    anonymous: Annotated[
        bool | None, opt("--anonymous", help="Shorthand for the `anonymous` right.")
    ] = None


async def _my_admin_rights(ctx: OpContext, peer: Any) -> set[str]:
    """What the caller may hand out: you cannot grant a right you do not hold.

    The owner holds everything, which is why the creator short-circuits; a
    chat we cannot query answers with the full set so `--all` degrades to
    "ask for everything and let the server refuse" rather than to nothing.
    """
    from telethon.tl import types
    from telethon.tl.functions import channels as fn

    try:
        reply = await _admin.client(ctx)(
            fn.GetParticipantRequest(
                channel=_admin.input_channel(peer), participant=types.InputPeerSelf()
            )
        )
    except Exception:
        return set(_rights.all_allowed(mask="admin"))
    participant = getattr(reply, "participant", None)
    if type(participant).__name__ == "ChannelParticipantCreator":
        return set(_rights.all_allowed(mask="admin"))
    mask = _rights.model_from_admin(getattr(participant, "admin_rights", None))
    return set(_rights.granted_names(mask, mask="admin"))


async def promote_admin(ctx: OpContext, req: AdminPromoteReq) -> AdminResult:
    """Promote a member, or re-cut an existing admin's mask and rank."""
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    peer = await _send.resolve(ctx, req.chat)
    user = await _send.resolve(ctx, req.user)
    chat_id = _send.peer_id_of(peer)
    user_id = abs(_send.peer_id_of(user))

    absolute = _rights.parse_names(req.rights, mask="admin", field="rights")
    add = _rights.parse_names(req.grant, mask="admin", field="grant")
    remove = _rights.parse_names(req.revoke, mask="admin", field="revoke")
    excluded = _rights.parse_names(req.except_rights, mask="admin", field="except")
    _rights.require_supported(absolute + add + remove, mask="admin")

    if not _admin.is_channel(peer):
        # A basic group has one bit, not a mask. Saying which rights were
        # dropped beats pretending the whole mask was applied.
        await _admin.client(ctx)(
            msg_fn.EditChatAdminRequest(
                chat_id=_admin.small_chat_id(peer),
                user_id=_admin.input_user(user),
                is_admin=not req.none,
            )
        )
        dropped = sorted(set(absolute) | set(add))
        if dropped:
            ctx.warn(
                "a basic group has no granular admin rights; "
                "`chat convert <chat> supergroup` first, or accept the all-or-nothing bit"
            )
        return AdminResult(chat_id=chat_id, user_id=user_id, dropped=dropped, rank=req.rank)

    current: set[str] = set()
    try:
        reply = await _admin.client(ctx)(
            chan_fn.GetParticipantRequest(channel=_admin.input_channel(peer), participant=user)
        )
        current = set(
            _rights.granted_names(
                _rights.model_from_admin(getattr(reply.participant, "admin_rights", None)),
                mask="admin",
            )
        )
    except Exception as exc:
        if type(exc).__name__ != "UserNotParticipantError":
            raise

    editor = _rights.MaskEdit(mask="admin", current=current)
    wanted = editor.resolve(
        absolute=absolute or None,
        add=add,
        remove=remove,
        everything=req.everything,
        nothing=req.none,
        exclude=excluded,
        ceiling=await _my_admin_rights(ctx, peer) if req.everything else None,
    )
    if req.anonymous is True:
        wanted.add("anonymous")
    elif req.anonymous is False:
        wanted.discard("anonymous")

    await _admin.client(ctx)(
        chan_fn.EditAdminRequest(
            channel=_admin.input_channel(peer),
            user_id=_admin.input_user(user),
            admin_rights=_rights.build_admin_rights(wanted),
            rank=req.rank or "",
        )
    )
    ctx.emit("chat_admin_changed", {"chat_id": chat_id, "user_id": user_id})
    return AdminResult(
        chat_id=chat_id,
        user_id=user_id,
        admin_rights=_rights.model_from_admin(_rights.build_admin_rights(wanted)),
        rank=req.rank,
        already=wanted == current and req.rank is None,
    )


SPEC_ADMIN_PROMOTE = OperationSpec(
    id="chat.admin.promote",
    request=AdminPromoteReq,
    response=AdminResult,
    impl=promote_admin,
    summary="Promote a member to admin, or change an existing admin's rights and rank",
    description=(
        "`--rights` sets the mask absolutely; `--grant`/`--revoke` patch the "
        "one the member already has, which is read first. `--all` grants "
        "every right *you* hold, because the server refuses to let you give "
        "away more. A basic group has no granular rights: the request "
        "collapses to `messages.editChatAdmin` and the dropped names are "
        "reported in `dropped` rather than silently lost."
    ),
    aliases=("chat.admin.edit",),
    mutating=True,
    columns=("chat_id", "user_id", "rank"),
    example={"chat_id": -1001500, "user_id": 4242, "rank": "moderator"},
    example_args="chat admin promote @mygroup @alice --rights ban-users,delete-messages",
    covers=(
        "groupcall.admin-right-manage-call",
        "groups-channels-admin.member-tag-rank",
        "groups-channels-admin.promote-admin",
        "stories.admin-rights",
    ),
    covers_partial=("groups-channels-admin.basic-group-admin",),
    coverage_note="Basic groups get the one all-or-nothing bit; `chat admin list` owns the id.",
    tags=frozenset({"visible-to-others"}),
)


class AdminDemoteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Who to dismiss.")]


async def demote_admin(ctx: OpContext, req: AdminDemoteReq) -> AdminResult:
    """Send an empty mask. Membership survives; only the rights go."""
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    peer = await _send.resolve(ctx, req.chat)
    user = await _send.resolve(ctx, req.user)
    if _admin.is_channel(peer):
        await _admin.client(ctx)(
            chan_fn.EditAdminRequest(
                channel=_admin.input_channel(peer),
                user_id=_admin.input_user(user),
                admin_rights=_rights.build_admin_rights([]),
                rank="",
            )
        )
    else:
        await _admin.client(ctx)(
            msg_fn.EditChatAdminRequest(
                chat_id=_admin.small_chat_id(peer),
                user_id=_admin.input_user(user),
                is_admin=False,
            )
        )
    chat_id = _send.peer_id_of(peer)
    user_id = abs(_send.peer_id_of(user))
    ctx.emit("chat_admin_changed", {"chat_id": chat_id, "user_id": user_id, "demoted": True})
    return AdminResult(
        chat_id=chat_id,
        user_id=user_id,
        admin_rights=_rights.model_from_admin(_rights.build_admin_rights([])),
    )


SPEC_ADMIN_DEMOTE = OperationSpec(
    id="chat.admin.demote",
    request=AdminDemoteReq,
    response=AdminResult,
    impl=demote_admin,
    summary="Dismiss an administrator",
    description=(
        "Sends an empty `ChatAdminRights`; the person keeps their "
        "membership. You need `add-admins` and — for somebody another admin "
        "promoted — `channelParticipantAdmin.can_edit`."
    ),
    mutating=True,
    destructive=True,
    columns=("chat_id", "user_id"),
    example={"chat_id": -1001500, "user_id": 4242},
    example_args="chat admin demote @mygroup @alice --yes",
    covers=("groups-channels-admin.demote-admin",),
    covers_partial=("groups-channels-admin.basic-group-admin",),
    coverage_note="`is_admin=false` in a basic group; `chat admin list` owns the id.",
)


# ---------------------------------------------------------------------------
# chat permission get / list / set
# ---------------------------------------------------------------------------


class PermissionGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]


async def get_permissions(ctx: OpContext, req: PermissionGetReq) -> PermissionView:
    """The chat-wide defaults, printed in the polarity `--allow` accepts."""
    peer = await _send.resolve(ctx, req.chat)
    _full, entity, _entities = await _admin.full_chat(ctx, peer)
    rights = _rights.model_from_banned(getattr(entity, "default_banned_rights", None))
    if rights is None:
        rights = _rights.model_from_banned(_rights.build_banned_rights(_rights.all_allowed()))
    return PermissionView(
        chat_id=_send.peer_id_of(peer),
        allow=_rights.granted_names(rights, mask="member"),
        deny=_rights.denied_names(rights, mask="member"),
        rights=rights,
    )


SPEC_PERMISSION_GET = OperationSpec(
    id="chat.permission.get",
    request=PermissionGetReq,
    response=PermissionView,
    impl=get_permissions,
    summary="Show the chat-wide default permissions (what every member may do)",
    description=(
        "Allow-polarity, using the same names `chat permission set "
        "--allow/--deny` accepts, so the output round-trips back into the "
        "input."
    ),
    columns=("chat_id", "allow", "deny"),
    example={"chat_id": -1001500, "allow": ["send-messages"], "deny": ["send-media"]},
    example_args="chat permission get @mygroup",
    covers_partial=("groups-channels-admin.default-permissions",),
    coverage_note="Reading half; `chat permission set` writes them and owns the id.",
)


class PermissionListReq(Request):
    mask: Annotated[str, choice("admin", "member", "all", help="Which vocabulary to print.")] = (
        "all"
    )
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Also mark what you may grant here."),
    ] = None


async def list_rights(ctx: OpContext, req: PermissionListReq) -> list[RightInfo]:
    """The canonical right vocabulary — the single source of truth for names.

    Without `--chat` this is a static table and touches no network; with one
    it also marks which rights the caller may currently hand out, which needs
    the caller's own participant row.
    """
    grantable: dict[str, bool] = {}
    if req.chat is not None:
        peer = await _send.resolve(ctx, req.chat)
        mine = await _my_admin_rights(ctx, peer)
        for name in _rights.ADMIN_MASK:
            grantable[f"admin:{name}"] = name in mine
        may_ban = "ban-users" in mine
        for name in _rights.MEMBER_MASK:
            grantable[f"member:{name}"] = may_ban
    return _rights.catalog(mask=req.mask, grantable=grantable)


SPEC_PERMISSION_LIST = OperationSpec(
    id="chat.permission.list",
    request=PermissionListReq,
    response=list[RightInfo],
    impl=list_rights,
    summary="Print the canonical right vocabulary (admin mask, member mask, layer support)",
    description=(
        "The one place the names come from: `chat admin promote --rights`, "
        "`chat member restrict --deny`, `chat permission set --allow` and "
        "`bot default-rights set` all read this table. "
        "`manage-linked-peers` and `manage-welcome-messages` are layer-229 "
        "flags Telethon 1.44 cannot express and are marked "
        "`supported: false` rather than omitted."
    ),
    rate_class="local",
    columns=("name", "mask", "tl_flag", "supported"),
    headers=("Name", "Mask", "TL flag", "Supported"),
    example=[
        {
            "name": "ban-users",
            "mask": "admin",
            "tl_flag": "ban_users",
            "polarity": "allow",
            "supported": True,
        }
    ],
    example_args="chat permission list --mask member",
    covers_partial=(
        "groups-channels-admin.default-permissions",
        "groups-channels-admin.promote-admin",
        "groups-channels-admin.restrict-member",
    ),
    coverage_note=(
        "The vocabulary the three writing commands share; each of them owns its own catalog id."
    ),
)


class PermissionSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    allow: Annotated[
        str | None, opt("--allow", metavar="RIGHTS", help="Rights every member may use.")
    ] = None
    deny: Annotated[
        str | None, opt("--deny", metavar="RIGHTS", help="Rights no member may use.")
    ] = None
    everything: Annotated[bool, opt("--all", help="Allow everything (empty banned mask).")] = False
    none: Annotated[bool, opt("--none", help="Deny everything except view-messages.")] = False
    replace: Annotated[
        bool, opt("--replace", help="Treat --allow/--deny as the whole mask, not a patch.")
    ] = False


async def set_permissions(ctx: OpContext, req: PermissionSetReq) -> PermissionResult:
    """Read the chat's current default mask, patch it, and send it complete."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    _full, entity, _entities = await _admin.full_chat(ctx, peer)
    current = _rights.model_from_banned(getattr(entity, "default_banned_rights", None))
    base = (
        set(_rights.granted_names(current, mask="member"))
        if current is not None
        else set(_rights.all_allowed())
    )

    allow = _rights.parse_names(req.allow, mask="member", field="allow")
    deny = _rights.parse_names(req.deny, mask="member", field="deny")
    _rights.require_supported(allow + deny, mask="member")
    if not (allow or deny or req.everything or req.none):
        raise UsageError("nothing to change: pass --allow, --deny, --all or --none", field="allow")

    editor = _rights.MaskEdit(mask="member", current=base)
    wanted = editor.resolve(
        absolute=(set(allow) - set(deny)) if (req.replace and allow) else None,
        add=allow,
        remove=deny,
        everything=req.everything,
        nothing=req.none,
    )
    # `view-messages` is not a chat-wide default: a chat nobody may read is a
    # chat nobody is in, and the server refuses it.
    wanted.add("view-messages")

    if wanted == base:
        _admin.already(ctx)
        order = list(_rights.MEMBER_MASK)
        return PermissionResult(
            chat_id=_send.peer_id_of(peer),
            allow=[n for n in order if n in wanted],
            deny=[n for n in order if n not in wanted and n in _rights.all_allowed()],
            changed=False,
            already=True,
        )

    await _admin.client(ctx)(
        fn.EditChatDefaultBannedRightsRequest(
            peer=peer, banned_rights=_rights.build_banned_rights(wanted)
        )
    )
    order = list(_rights.MEMBER_MASK)
    ctx.emit("chat_permissions_changed", {"chat_id": _send.peer_id_of(peer)})
    return PermissionResult(
        chat_id=_send.peer_id_of(peer),
        allow=[n for n in order if n in wanted],
        deny=[n for n in order if n not in wanted and n in _rights.all_allowed()],
        changed=True,
    )


SPEC_PERMISSION_SET = OperationSpec(
    id="chat.permission.set",
    request=PermissionSetReq,
    response=PermissionResult,
    impl=set_permissions,
    summary="Set the chat-wide default permissions",
    description=(
        "Read-modify-write: the current `default_banned_rights` are fetched "
        "and patched, because a fresh mask resets every flag you did not "
        "mention. `view-messages` is not settable here — a chat nobody may "
        "read is `chat member ban`, not a permission — and `until_date` is "
        "ignored. Works for basic groups too."
    ),
    mutating=True,
    columns=("chat_id", "allow", "deny", "changed"),
    example={"chat_id": -1001500, "allow": ["send-messages"], "deny": [], "changed": True},
    example_args="chat permission set @mygroup --deny send-media,send-stickers",
    covers=("groups-channels-admin.default-permissions",),
)


# ---------------------------------------------------------------------------
# chat admin-log
# ---------------------------------------------------------------------------


class AdminLogReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    filter: Annotated[
        str | None,
        opt("--filter", metavar="CLASSES", help="Comma-separated event classes; default all."),
    ] = None
    admin: Annotated[
        list[PeerRef], opt("--admin", metavar="USER", kind="user", help="Only these admins.")
    ] = []
    search: Annotated[str, opt("--search", "-s", metavar="TEXT", help="Free-text query.")] = ""
    min_id: Annotated[int, opt("--min-id", metavar="ID", help="Stop at this event id.")] = 0


def _log_filter(value: str | None) -> Any:
    if not value:
        return None
    from telethon.tl import types

    flags: dict[str, bool] = {}
    for raw in str(value).replace(",", " ").split():
        name = raw.strip().lower()
        if not name:
            continue
        if name == "all":
            return None
        if name not in _LOG_FILTERS:
            raise UsageError(
                f"{name!r} is not an admin-log event class; "
                f"pick from {', '.join(sorted(_LOG_FILTERS))}",
                field="filter",
            )
        flags[_LOG_FILTERS[name]] = True
    return types.ChannelAdminLogEventsFilter(**flags)


def _log_event(raw: Any, *, chat_id: int) -> AdminLogEvent:
    action = getattr(raw, "action", None)
    name = type(action).__name__
    slug = _kebab(name[len(_ACTION_PREFIX) :]) if name.startswith(_ACTION_PREFIX) else _kebab(name)
    body = _plain(action) if action is not None else {}
    previous: Any = None
    current: Any = None
    if isinstance(body, dict):
        for key, value in body.items():
            if key.startswith("prev"):
                previous = value
            elif key.startswith("new"):
                current = value
        if previous is None and current is None:
            current = {k: v for k, v in body.items() if k != "_"} or None
    return AdminLogEvent(
        id=int(getattr(raw, "id", 0) or 0),
        date=fmt_dt(getattr(raw, "date", None)),
        date_unix=to_unix(getattr(raw, "date", None)),
        user_id=int(getattr(raw, "user_id", 0) or 0),
        action=slug,
        raw_type=name,
        prev=previous,
        new=current,
        chat_id=chat_id,
    )


async def list_admin_log(ctx: OpContext, req: AdminLogReq) -> Page[AdminLogEvent]:
    """Recent actions, newest first, with `max_id` as the cursor."""
    from telethon.tl.functions import channels as fn

    limit, state = _admin.window(ctx, "chat.admin-log.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    admins = [_admin.input_user(await _send.resolve(ctx, ref)) for ref in req.admin]
    reply = await _admin.client(ctx)(
        fn.GetAdminLogRequest(
            channel=_admin.input_channel(peer),
            q=req.search or "",
            max_id=int(state.get("max_id", 0) or 0),
            min_id=req.min_id,
            limit=limit,
            events_filter=_log_filter(req.filter),
            admins=admins or None,
        )
    )
    events = list(getattr(reply, "events", None) or [])
    items = [_log_event(row, chat_id=chat_id) for row in events]
    lowest = min((item.id for item in items), default=0)
    return build_page(
        items,
        op="chat.admin-log.list",
        kind=PageKind.PARTICIPANTS,
        state={"max_id": lowest},
        account=ctx.account,
        limit=limit,
    )


SPEC_ADMIN_LOG_LIST = OperationSpec(
    id="chat.admin-log.list",
    request=AdminLogReq,
    response=Page[AdminLogEvent],
    impl=list_admin_log,
    summary="Recent actions (the admin log)",
    description=(
        "The filter is built here rather than through Telethon's "
        "`iter_admin_log`, which shifts four names (restrict→ban, "
        "unrestrict→unban, ban→kick, unban→unkick) and has no flag at all "
        "for invites, send, forums, sub-extend or edit-rank. tlgr's "
        "`--filter` names are the API's. Retention is about 48 hours for "
        "most classes, and the endpoint is aggressively flood-limited."
    ),
    paginated=PageKind.PARTICIPANTS,
    rate_class="bulk",
    columns=("id", "date", "user_id", "action"),
    headers=("Event", "When", "By", "Action"),
    example={
        "items": [
            {
                "id": 91,
                "date": "2026-02-01T10:00:00Z",
                "user_id": 777,
                "action": "participant-toggle-ban",
                "raw_type": "ChannelAdminLogEventActionParticipantToggleBan",
            }
        ],
        "has_more": False,
    },
    example_args="chat admin-log list @mygroup --filter ban,kick",
    covers=("groupcall.admin-log", "groups-channels-admin.admin-log"),
)


class AntiSpamReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Supergroup.")]
    msg_id: Annotated[
        int, arg(1, metavar="MSG_ID", kind="msg_id", help="The wrongly deleted message.")
    ]


async def report_false_positive(ctx: OpContext, req: AntiSpamReportReq) -> AntiSpamReport:
    """Tell Telegram its anti-spam bot deleted something it should not have."""
    from telethon.tl.functions import channels as fn

    peer = await _send.resolve(ctx, req.chat)
    await _admin.client(ctx)(
        fn.ReportAntiSpamFalsePositiveRequest(channel=_admin.input_channel(peer), msg_id=req.msg_id)
    )
    return AntiSpamReport(chat_id=_send.peer_id_of(peer), msg_id=req.msg_id, reported=True)


SPEC_ADMIN_LOG_REPORT = OperationSpec(
    id="chat.admin-log.report",
    request=AntiSpamReportReq,
    response=AntiSpamReport,
    impl=report_false_positive,
    summary="Report an anti-spam deletion as a false positive",
    description=(
        "The candidate ids come from `chat admin-log list --filter delete`, "
        "where the anti-spam bot's deletions appear."
    ),
    mutating=True,
    columns=("chat_id", "msg_id", "reported"),
    example={"chat_id": -1001500, "msg_id": 918, "reported": True},
    example_args="chat admin-log report @mygroup 918",
    covers=("groups-channels-admin.antispam-false-positive",),
)


# ---------------------------------------------------------------------------
# chat transfer
# ---------------------------------------------------------------------------


class TransferReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="The new owner.")]
    password: Annotated[
        str | None,
        opt(
            secret=True,
            envvar="TLGR_2FA_PASSWORD",
            help="Two-factor password. Never taken on the command line.",
        ),
    ] = None


async def transfer_chat(ctx: OpContext, req: TransferReq) -> TransferResult:
    """Hand ownership over. Needs the 2FA password, and is irreversible.

    The password is never a positional or a value-taking flag: argv is
    world-readable through `ps` and lands in shell history (STYLE §3).
    """
    from telethon.password import compute_check
    from telethon.tl.functions import account as acct_fn
    from telethon.tl.functions import messages as fn

    if not req.password:
        raise UsageError(
            "the 2FA password is required; pass --password-env, --password-stdin "
            "or --password-file (never as an argument)",
            field="password",
        )
    peer = await _send.resolve(ctx, req.chat)
    user = await _send.resolve(ctx, req.user)
    handle = _admin.client(ctx)
    algo = await handle(acct_fn.GetPasswordRequest())
    await handle(
        fn.EditChatCreatorRequest(
            peer=peer,
            user_id=_admin.input_user(user),
            password=compute_check(algo, req.password),
        )
    )
    chat_id = _send.peer_id_of(peer)
    new_owner = abs(_send.peer_id_of(user))
    ctx.emit("chat_owner_changed", {"chat_id": chat_id, "new_owner_id": new_owner})
    return TransferResult(chat_id=chat_id, new_owner_id=new_owner)


SPEC_TRANSFER = OperationSpec(
    id="chat.transfer",
    request=TransferReq,
    response=TransferResult,
    impl=transfer_chat,
    summary="Transfer ownership of a group or channel (2FA)",
    description=(
        "The target must already be an admin. PASSWORD_HASH_INVALID exits 4; "
        "PASSWORD_TOO_FRESH / SESSION_TOO_FRESH and CHANNELS_TOO_MUCH exit "
        "6 with the wait reported."
    ),
    aliases=("chat.admin.transfer",),
    mutating=True,
    destructive=True,
    columns=("chat_id", "new_owner_id"),
    example={"chat_id": -1001500, "new_owner_id": 4242},
    example_args="chat transfer @mygroup @alice --password-stdin --yes",
    covers=("groups-channels-admin.transfer-ownership",),
    tags=frozenset({"visible-to-others"}),
)
