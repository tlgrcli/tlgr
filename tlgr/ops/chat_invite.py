"""`chat invite *` and `chat join`: links in, and the queue behind them.

An invite link is two different objects depending on which side of it you
stand on, and conflating them is how a CLI ends up with one command that
sometimes needs admin rights and sometimes does not. Here the split is
explicit: `chat invite get <chat> <link>` inspects a link *you* own
(`messages.getExportedChatInvite`, needs `invite-users`), while `chat invite
get <link>` previews a link somebody handed you
(`messages.checkChatInvite`, needs nothing).

Two behaviours are worth knowing before scripting against this module.

* **Editing the permanent link replaces it.** `messages.editExportedChatInvite`
  may answer with `exportedChatInviteReplaced`, and both links are reported
  (`link` plus `replaced_link`) rather than only the new one.
* **Joining has three successful outcomes.** Joined, already a member
  (`already: true`), and request-sent (`pending_approval: true`). All three
  exit 0; only an expired or invalid hash is an error.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, PermissionError_, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_dt, parse_duration, to_unix
from tlgr.models.admin import (
    Invite,
    InviteDeleted,
    InviteInfo,
    InvitePeek,
    InviteRevoked,
    JoinResult,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _admin, _send
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import entity_to_peer, message_to_model
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_INVITE: dict[str, Any] = {
    "link": "https://t.me/+AbCdEf",
    "title": "Launch week",
    "permanent": False,
    "usage_limit": 25,
    "usage": 3,
}

#: A Stars subscription link is billed per 30-day period, always.
_SUBSCRIPTION_PERIOD = 30 * 86400


def _expiry(value: str | None) -> Any:
    """`--expires 7d` or `--expires 2026-03-01T00:00Z` → a datetime, or None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "off", "never", "none", "0"):
        return None
    seconds = parse_duration(text)
    if seconds is not None:
        from datetime import datetime, timedelta, timezone

        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    moment = parse_dt(text)
    if moment is None:
        raise UsageError(f"{value!r} is neither a duration nor a timestamp", field="expires")
    return moment


def _invite_model(raw: Any) -> Invite:
    """`chatInviteExported` → `Invite`, with the Stars pricing spelled out."""
    pricing = getattr(raw, "subscription_pricing", None)
    return Invite(
        link=str(getattr(raw, "link", "") or ""),
        title=getattr(raw, "title", None),
        permanent=bool(getattr(raw, "permanent", False)),
        revoked=bool(getattr(raw, "revoked", False)),
        request_needed=bool(getattr(raw, "request_needed", False)),
        admin_id=getattr(raw, "admin_id", None),
        date=fmt_dt(getattr(raw, "date", None)),
        date_unix=to_unix(getattr(raw, "date", None)),
        start_date=fmt_dt(getattr(raw, "start_date", None)),
        expire_date=fmt_dt(getattr(raw, "expire_date", None)),
        usage_limit=getattr(raw, "usage_limit", None),
        usage=getattr(raw, "usage", None),
        requested=getattr(raw, "requested", None),
        subscription_expired=getattr(raw, "subscription_expired", None),
        subscription_pricing=(
            {
                "period": int(getattr(pricing, "period", 0) or 0),
                "amount": int(getattr(pricing, "amount", 0) or 0),
            }
            if pricing is not None
            else None
        ),
    )


def _exported(reply: Any) -> tuple[Any, str | None]:
    """`(the invite, the link it replaced)` — the replacement is never hidden."""
    if type(reply).__name__ == "ExportedChatInviteReplaced":
        return getattr(reply, "new_invite", None), str(
            getattr(getattr(reply, "invite", None), "link", "") or ""
        )
    invite = getattr(reply, "invite", None)
    return (invite if invite is not None else reply), None


def _render_qr(ctx: OpContext, link: str, png: str | None) -> tuple[str | None, str | None]:
    """An ASCII QR, and optionally a PNG, when a QR library is installed.

    The API contributes nothing to a QR code beyond the link, so this is
    purely local rendering — and a pure-Python QR encoder is not something
    tlgr should carry when `pip install tlgr[qr]` says it in one line.
    """
    try:
        import segno
    except ImportError:
        ctx.warn(
            "no QR encoder is installed; `pip install 'tlgr[qr]'` (segno) to render "
            "the link as a QR code. The link itself is in `link`"
        )
        return None, None
    code = segno.make(link, error="m")
    if png:
        code.save(png, scale=6)
    import io

    buffer = io.StringIO()
    code.terminal(out=buffer, compact=True)
    return buffer.getvalue(), png


# ---------------------------------------------------------------------------
# chat invite create / edit / revoke / delete
# ---------------------------------------------------------------------------


class InviteCreateReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="Label shown to admins.")] = (
        None
    )
    expires: Annotated[
        str | None, opt("--expires", metavar="WHEN", help="Expiry, as a duration or a timestamp.")
    ] = None
    usage_limit: Annotated[
        int | None,
        opt("--limit", metavar="N", help="Maximum joins; excludes --request-approval."),
    ] = None
    request_approval: Annotated[
        bool, opt("--request-approval", help="Joins land in the approval queue instead.")
    ] = False
    subscription_stars: Annotated[
        int | None,
        opt("--subscription-stars", metavar="N", help="Paid link: Stars per 30-day period."),
    ] = None
    replace_primary: Annotated[
        bool, opt("--replace-primary", help="Revoke and replace the permanent link.")
    ] = False


async def create_invite(ctx: OpContext, req: InviteCreateReq) -> Invite:
    """Mint an invite link: expiring, join-limited, approval-gated or paid."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.usage_limit is not None and req.request_approval:
        raise UsageError(
            "--limit and --request-approval are mutually exclusive: a link that "
            "queues joins has no join count to cap",
            field="limit",
        )
    peer = await _send.resolve(ctx, req.chat)
    pricing = (
        types.StarsSubscriptionPricing(
            period=_SUBSCRIPTION_PERIOD, amount=int(req.subscription_stars)
        )
        if req.subscription_stars
        else None
    )
    reply = await _admin.client(ctx)(
        fn.ExportChatInviteRequest(
            peer=peer,
            title=req.title,
            expire_date=_expiry(req.expires),
            usage_limit=req.usage_limit,
            request_needed=req.request_approval or None,
            subscription_pricing=pricing,
            legacy_revoke_permanent=req.replace_primary or None,
        )
    )
    invite, replaced = _exported(reply)
    model = _invite_model(invite)
    model.replaced_link = replaced
    ctx.emit("chat_invite_created", {"chat_id": _send.peer_id_of(peer), "link": model.link})
    return model


SPEC_INVITE_CREATE = OperationSpec(
    id="chat.invite.create",
    request=InviteCreateReq,
    response=Invite,
    impl=create_invite,
    summary="Create an invite link (expiring, limited, approval-gated or paid)",
    description=(
        "`--limit` and `--request-approval` are mutually exclusive, which is "
        "the server's rule and not ours. The subscription period is fixed at "
        "30 days; creating a paid link costs nothing, only joining does. "
        "`--replace-primary` invalidates the old permanent link for everyone "
        "who holds it."
    ),
    mutating=True,
    columns=("link", "title", "expire_date", "usage_limit"),
    headers=("Link", "Title", "Expires", "Limit"),
    example=_EXAMPLE_INVITE,
    example_args="chat invite create @mygroup --title 'Launch week' --limit 25 --expires 7d",
    covers=(
        "groups-channels-admin.invite-link-create",
        "groups-channels-admin.invite-link-primary",
        "groups-channels-admin.invite-link-subscription",
    ),
)


class InviteEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    link: Annotated[str, arg(1, metavar="LINK", help="The invite link to edit.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New label.")] = None
    expires: Annotated[
        str | None, opt("--expires", metavar="WHEN", help="New expiry; `off` clears it.")
    ] = None
    usage_limit: Annotated[
        int | None, opt("--limit", metavar="N", help="New usage limit; 0 clears it.")
    ] = None
    request_approval: Annotated[
        bool | None, opt("--request-approval", help="Turn the approval queue on or off.")
    ] = None


async def edit_invite(ctx: OpContext, req: InviteEditReq) -> Invite:
    """Change a link's label, expiry, cap or approval gate."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    if (
        req.title is None
        and req.expires is None
        and req.usage_limit is None
        and req.request_approval is None
    ):
        raise UsageError("nothing to change", field="title")
    reply = await _admin.client(ctx)(
        fn.EditExportedChatInviteRequest(
            peer=peer,
            link=req.link,
            title=req.title,
            expire_date=_expiry(req.expires),
            usage_limit=req.usage_limit,
            request_needed=req.request_approval,
        )
    )
    invite, replaced = _exported(reply)
    model = _invite_model(invite)
    model.replaced_link = replaced
    return model


SPEC_INVITE_EDIT = OperationSpec(
    id="chat.invite.edit",
    request=InviteEditReq,
    response=Invite,
    impl=edit_invite,
    summary="Edit an invite link",
    description=(
        "May answer with `messages.exportedChatInviteReplaced`; both links "
        "are reported, the new one in `link` and the old one in "
        "`replaced_link`. A paid subscription link accepts only `--title`."
    ),
    mutating=True,
    columns=("link", "title", "expire_date", "usage_limit"),
    example=_EXAMPLE_INVITE,
    example_args="chat invite edit @mygroup https://t.me/+AbCdEf --limit 50",
    covers=("groups-channels-admin.invite-link-edit",),
)


class InviteRevokeReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    link: Annotated[str, arg(1, metavar="LINK", help="The invite link to revoke.")]


async def revoke_invite(ctx: OpContext, req: InviteRevokeReq) -> InviteRevoked:
    """Revoke a link. Revoking the permanent one mints a replacement."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    reply = await _admin.client(ctx)(
        fn.EditExportedChatInviteRequest(peer=peer, link=req.link, revoked=True)
    )
    invite, replaced = _exported(reply)
    link = str(getattr(invite, "link", "") or req.link)
    ctx.emit("chat_invite_revoked", {"chat_id": _send.peer_id_of(peer), "link": req.link})
    return InviteRevoked(link=link, revoked=True, replaced_link=replaced)


SPEC_INVITE_REVOKE = OperationSpec(
    id="chat.invite.revoke",
    request=InviteRevokeReq,
    response=InviteRevoked,
    impl=revoke_invite,
    summary="Revoke an invite link",
    description=(
        "Revoked links stay listable with `chat invite list --revoked` until "
        "`chat invite delete` removes them."
    ),
    mutating=True,
    destructive=True,
    columns=("link", "revoked"),
    example={"link": "https://t.me/+AbCdEf", "revoked": True},
    example_args="chat invite revoke @mygroup https://t.me/+AbCdEf --yes",
    covers=("groups-channels-admin.invite-link-revoke",),
)


class InviteDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    link: Annotated[
        str | None, arg(1, metavar="LINK", required=False, help="The revoked link to delete.")
    ] = None
    revoked: Annotated[bool, opt("--revoked", help="Delete every revoked link instead.")] = False
    admin: Annotated[
        PeerRef | None,
        opt("--admin", metavar="USER", kind="user", help="With --revoked: whose links to purge."),
    ] = None


async def delete_invite(ctx: OpContext, req: InviteDeleteReq) -> InviteDeleted:
    """Delete one revoked link, or every revoked link of one admin."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    handle = _admin.client(ctx)
    if req.revoked:
        admin: Any = types.InputUserSelf()
        if req.admin is not None:
            admin = _admin.input_user(await _send.resolve(ctx, req.admin))
        await handle(fn.DeleteRevokedExportedChatInvitesRequest(peer=peer, admin_id=admin))
        return InviteDeleted(chat_id=_send.peer_id_of(peer), deleted=-1)
    if not req.link:
        raise UsageError("name a link, or pass --revoked to purge them all", field="link")
    await handle(fn.DeleteExportedChatInviteRequest(peer=peer, link=req.link))
    return InviteDeleted(chat_id=_send.peer_id_of(peer), deleted=1)


SPEC_INVITE_DELETE = OperationSpec(
    id="chat.invite.delete",
    request=InviteDeleteReq,
    response=InviteDeleted,
    impl=delete_invite,
    summary="Delete a revoked invite link, or every revoked link of an admin",
    description=(
        "Only revoked links can be deleted; revoke an active one first. "
        "`--revoked` reports `deleted: -1`, because "
        "`messages.deleteRevokedExportedChatInvites` answers with a bare "
        "`true` and inventing a count would be inventing data."
    ),
    mutating=True,
    destructive=True,
    columns=("chat_id", "deleted"),
    example={"chat_id": -1001500, "deleted": 1},
    example_args="chat invite delete @mygroup --revoked --yes",
    covers=(
        "groups-channels-admin.invite-link-delete",
        "groups-channels-admin.invite-link-delete-all-revoked",
    ),
)


# ---------------------------------------------------------------------------
# chat invite get / list / open
# ---------------------------------------------------------------------------


class InviteGetReq(Request):
    target: Annotated[
        PeerRef, arg(0, metavar="CHAT|LINK", kind="peer", help="A chat, or an invite link.")
    ]
    link: Annotated[
        str | None, arg(1, metavar="LINK", required=False, help="With a chat: which link.")
    ] = None
    qr: Annotated[bool, opt("--qr", help="Also render the link as an ASCII QR code.")] = False
    png: Annotated[
        str | None, opt("--png", metavar="PATH", kind="path", help="Write the QR code to a PNG.")
    ] = None


async def get_invite(ctx: OpContext, req: InviteGetReq) -> InviteInfo:
    """One of your links, your primary link, or a preview of somebody else's."""
    from telethon.tl.functions import messages as fn

    handle = _admin.client(ctx)

    if req.target.kind == "invite" and req.link is None:
        reply = await handle(fn.CheckChatInviteRequest(hash=str(req.target.value)))
        kind = type(reply).__name__
        chat = getattr(reply, "chat", None)
        info = InviteInfo(link=req.target.raw)
        if kind == "ChatInviteAlready":
            info.already_member = True
            info.chat = entity_to_peer(chat) if chat is not None else None
            info.chat_title = _admin.display_name(chat)
        elif kind == "ChatInvitePeek":
            info.already_member = False
            info.chat = entity_to_peer(chat) if chat is not None else None
            info.chat_title = _admin.display_name(chat)
            info.peek_expires = fmt_dt(getattr(reply, "expires", None))
        else:
            info.already_member = False
            info.chat_title = str(getattr(reply, "title", "") or "")
            info.members_count = getattr(reply, "participants_count", None)
            info.about = getattr(reply, "about", None)
            info.public = bool(getattr(reply, "public", False))
            info.request_needed = bool(getattr(reply, "request_needed", False))
        if req.qr or req.png:
            info.qr, info.png = _render_qr(ctx, info.link, req.png)
        return info

    peer = await _send.resolve(ctx, req.target)
    if req.link:
        reply = await handle(fn.GetExportedChatInviteRequest(peer=peer, link=req.link))
        invite, _replaced = _exported(reply)
    else:
        full, _entity, _entities = await _admin.full_chat(ctx, peer)
        invite = getattr(full, "exported_invite", None)
        if invite is None:
            raise NotFoundError(
                "this chat has no primary invite link visible to you; "
                "`chat invite create` mints one"
            )
    base = _invite_model(invite)
    info = InviteInfo(**{key: getattr(base, key) for key in base.__struct_fields__})
    if req.qr or req.png:
        info.qr, info.png = _render_qr(ctx, info.link, req.png)
    return info


SPEC_INVITE_GET = OperationSpec(
    id="chat.invite.get",
    request=InviteGetReq,
    response=InviteInfo,
    impl=get_invite,
    summary="Inspect one invite link, the primary link, or preview a link you were given",
    description=(
        "One argument that is a `t.me/+…` or `joinchat` link previews it "
        "with `messages.checkChatInvite` and needs no rights; a chat plus a "
        "link inspects your own with `messages.getExportedChatInvite` and "
        "needs `invite-users`. A peek answers with `peek_expires`, which is "
        "the window `chat invite open` reads inside."
    ),
    columns=("link", "chat_title", "members_count", "already_member"),
    example={**_EXAMPLE_INVITE, "chat_title": "News", "members_count": 120},
    example_args="chat invite get https://t.me/+AbCdEf",
    empty_exit=EXIT_EMPTY,
    covers=(
        "groups-channels-admin.check-invite",
        "groups-channels-admin.invite-link-get",
        "groups-channels-admin.invite-link-qr",
    ),
    covers_partial=("groups-channels-admin.invite-link-primary",),
    coverage_note=(
        "The QR is rendered locally and needs the optional `tlgr[qr]` extra; "
        "without it the link is still reported and a warning says so. "
        "Minting the primary link is `chat invite create`."
    ),
)


class InviteListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    admin: Annotated[
        PeerRef | None,
        opt("--admin", metavar="USER", kind="user", help="Whose links to list; defaults to me."),
    ] = None
    revoked: Annotated[bool, opt("--revoked", help="Revoked links instead of active ones.")] = False
    by_admin: Annotated[
        bool, opt("--by-admin", help="One row per admin with their link counts instead.")
    ] = False


async def list_invites(ctx: OpContext, req: InviteListReq) -> Page[Invite]:
    """A chat's invite links, or (with `--by-admin`) who made how many."""
    from datetime import datetime, timezone

    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    limit, state = _admin.window(ctx, "chat.invite.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    handle = _admin.client(ctx)

    if req.by_admin:
        reply = await handle(fn.GetAdminsWithInvitesRequest(peer=peer))
        rows = [
            Invite(
                link="",
                admin_id=int(getattr(row, "admin_id", 0) or 0),
                invites_count=int(getattr(row, "invites_count", 0) or 0),
                revoked_invites_count=int(getattr(row, "revoked_invites_count", 0) or 0),
            )
            for row in (getattr(reply, "admins", None) or [])
        ]
        return build_page(
            rows,
            op="chat.invite.list",
            kind=PageKind.PARTICIPANTS,
            account=ctx.account,
            has_more=False,
            total=len(rows),
        )

    admin: Any = types.InputUserSelf()
    if req.admin is not None:
        admin = _admin.input_user(await _send.resolve(ctx, req.admin))
    offset_date = state.get("date")
    reply = await handle(
        fn.GetExportedChatInvitesRequest(
            peer=peer,
            admin_id=admin,
            limit=limit,
            revoked=req.revoked or None,
            offset_date=datetime.fromtimestamp(offset_date, tz=timezone.utc)
            if offset_date
            else None,
            offset_link=state.get("link") or None,
        )
    )
    items = [_invite_model(row) for row in (getattr(reply, "invites", None) or [])]
    next_state: dict[str, Any] = {}
    if items:
        last = items[-1]
        next_state = {"date": last.date_unix or 0, "link": last.link}
    return build_page(
        items,
        op="chat.invite.list",
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", 0) or 0),
    )


SPEC_INVITE_LIST = OperationSpec(
    id="chat.invite.list",
    request=InviteListReq,
    response=Page[Invite],
    impl=list_invites,
    summary="List a chat's invite links (active, revoked, or grouped by admin)",
    description=(
        "`admin_id` is mandatory in the request, so it defaults to you; only "
        "the owner may name somebody else. The cursor packs the "
        "`(date, link)` pair of the last row. `--by-admin` swaps the rows "
        "for one per admin, with their active and revoked counts in `usage` "
        "and `requested`."
    ),
    paginated=PageKind.PARTICIPANTS,
    columns=("link", "title", "usage", "expire_date"),
    headers=("Link", "Title", "Used", "Expires"),
    example={"items": [_EXAMPLE_INVITE], "has_more": False},
    example_args="chat invite list @mygroup --revoked",
    covers=(
        "groups-channels-admin.invite-link-admins",
        "groups-channels-admin.invite-link-list",
    ),
)


class InviteOpenReq(Request):
    link: Annotated[PeerRef, arg(0, metavar="LINK", kind="peer", help="The invite link.")]


async def open_invite(ctx: OpContext, req: InviteOpenReq) -> InvitePeek:
    """Read a private channel through an invite peek, without joining.

    Only works while the server answers `chatInvitePeek`. `peek_expires` is
    reported so a script knows the window; after it the peer is dropped and
    reading answers CHANNEL_PRIVATE.
    """
    from telethon.tl.functions import messages as fn

    if req.link.kind != "invite":
        raise UsageError("this takes an invite link (t.me/+… or joinchat/…)", field="link")
    handle = _admin.client(ctx)
    reply = await handle(fn.CheckChatInviteRequest(hash=str(req.link.value)))
    if type(reply).__name__ != "ChatInvitePeek":
        raise PermissionError_(
            "the server did not offer a peek for this link; "
            "`chat invite get` shows what it did say, and `chat join` joins"
        )
    chat = getattr(reply, "chat", None)
    limit = int(getattr(ctx, "limit", None) or 20)
    raw = [m async for m in handle.iter_messages(chat, limit=limit) if m is not None]
    chat_id = _admin.entity_id(chat)
    return InvitePeek(
        chat=entity_to_peer(chat) if chat is not None else None,
        chat_title=_admin.display_name(chat),
        peek_expires=fmt_dt(getattr(reply, "expires", None)),
        messages=[message_to_model(m, chat_id=chat_id) for m in raw],
    )


SPEC_INVITE_OPEN = OperationSpec(
    id="chat.invite.open",
    request=InviteOpenReq,
    response=InvitePeek,
    impl=open_invite,
    summary="Read a private channel through an invite peek, without joining",
    description=(
        "A peek is the server's offer, not ours: when it answers anything "
        "other than `chatInvitePeek` this exits 6 rather than joining on "
        "your behalf."
    ),
    columns=("chat_title", "peek_expires"),
    example={"chat_title": "News", "peek_expires": "2026-02-01T11:00:00Z", "messages": []},
    example_args="chat invite open https://t.me/+AbCdEf",
    covers=("groups-channels-admin.invite-peek",),
)


# ---------------------------------------------------------------------------
# chat join
# ---------------------------------------------------------------------------


class JoinReq(Request):
    target: Annotated[
        PeerRef, arg(0, metavar="CHAT|LINK", kind="peer", help="A public chat, or an invite link.")
    ]


async def join_chat(ctx: OpContext, req: JoinReq) -> JoinResult:
    """Join a public chat by username, or a private one by invite link.

    Three outcomes are all success: joined, already a member, and
    request-sent. Only an expired or invalid hash is an error, because a
    script that treats "you are already in this group" as a failure will
    retry forever.
    """
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    handle = _admin.client(ctx)
    if req.target.kind == "invite":
        try:
            updates = await handle(msg_fn.ImportChatInviteRequest(hash=str(req.target.value)))
        except Exception as exc:
            name = type(exc).__name__
            if name == "UserAlreadyParticipantError":
                _admin.already(ctx)
                return JoinResult(chat_id=0, joined=True, already=True)
            if name == "InviteRequestSentError":
                return JoinResult(chat_id=0, joined=False, pending_approval=True)
            raise
        chats = list(getattr(updates, "chats", None) or [])
        chat = chats[0] if chats else None
        peer = entity_to_peer(chat) if chat is not None else None
        result = JoinResult(
            chat_id=peer.id if peer is not None else 0,
            title=peer.title if peer is not None else "",
            joined=True,
        )
        if type(updates).__name__ == "ChatInviteJoinResultNeedsWebView":  # pragma: no cover
            result.joined = False
            result.needs_web_view = "the server wants a web-view confirmation tlgr cannot open"
        ctx.emit("chat_joined", {"chat_id": result.chat_id})
        return result

    peer = await _send.resolve(ctx, req.target)
    updates = await handle(chan_fn.JoinChannelRequest(channel=_admin.input_channel(peer)))
    chats = list(getattr(updates, "chats", None) or [])
    entity = chats[0] if chats else None
    chat_id = _send.peer_id_of(peer)
    ctx.emit("chat_joined", {"chat_id": chat_id})
    return JoinResult(chat_id=chat_id, title=_admin.display_name(entity), joined=True)


SPEC_JOIN = OperationSpec(
    id="chat.join",
    request=JoinReq,
    response=JoinResult,
    impl=join_chat,
    summary="Join a public group/channel, or a private one by invite link",
    description=(
        "`INVITE_REQUEST_SENT` is success-with-pending (exit 0, "
        "`pending_approval: true`) and `USER_ALREADY_PARTICIPANT` is "
        "`already: true` (exit 0); an expired hash exits 5. A layer-229 join "
        "that needs a web view is reported in `needs_web_view` rather than "
        "claimed as a join."
    ),
    mutating=True,
    columns=("chat_id", "title", "joined", "pending_approval"),
    example={"chat_id": -1001500, "title": "News", "joined": True},
    example_args="chat join @somechannel",
    covers=("groups-channels-admin.join-by-invite", "groups-channels-admin.join-by-username"),
    tags=frozenset({"visible-to-others"}),
)
