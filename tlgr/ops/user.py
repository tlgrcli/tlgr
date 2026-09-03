"""The `user` group: one person's profile, and what this account may do to them.

Two contracts here are frozen by `AGENT.md` and must not drift; the tests in
`tests/test_ops_contacts.py` hold the line.

* **`user dialog-status` is three-valued.** `resolved=true, has_dialog=true`
  is a dialog with an exact server-side message count;
  `resolved=true, has_dialog=false` is a *definitive* negative, licensed only
  by enumerating the account's complete dialog list; anything else is
  `resolved=false, has_dialog=null` and exit 13. "Could not find the input
  entity" is never evidence of absence — `get_input_entity` on a bare numeric
  id only consults the local cache, and its network fallback returns
  `UserEmpty` for any non-contact. Reading that as "no history" is the
  cold-contact bug this command exists to remove.
* **`user hide-stories` is idempotent and local.** It reads the fresh
  `stories_hidden` flag first and returns `already: true` with no RPC when
  there is nothing to do, so a bulk pass over hundreds of peers is nearly
  free. The other side is never notified and nothing about the chat, the
  contact entry or their access to us changes.

Access hashes are never printed. `access_hash_cached` says whether one is
held; the value is per-login-session state that is useless — and unsafe —
anywhere else.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.contact import (
    BlockResult,
    ContactRequirement,
    DialogStatus,
    MusicTrack,
    PersonalChannel,
    PhotoResult,
    ProfilePhoto,
    StoriesHidden,
    StoriesHiddenPeer,
    SuggestedBirthday,
    UserLink,
    UserProfile,
)
from tlgr.models.page import Page
from tlgr.models.peer import Chat, PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import action_bar, entity_to_peer, message_to_model, photo_summary
from tlgr.ops._spec import OpContext, OperationSpec
from tlgr.ops.contact import (
    birthday_text,
    client_of,
    display_name,
    fetch_user,
    input_user,
    mark_already,
    status_model,
    status_word,
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The Replies pseudo-chat. `contacts.blockFromReplies` takes a message id
#: *inside* it, which is why `--from-replies` is a bare integer.
REPLIES_PEER = 1271266957

_EXAMPLE_USER: dict[str, Any] = {
    "id": 777123,
    "raw_id": 777123,
    "first_name": "Alice",
    "name": "Alice",
    "username": "alice",
    "bio": "somewhere warm",
    "is_bot": False,
    "status": "offline",
}


def _window(ctx: OpContext, op: str, kind: PageKind, default: int = 50) -> tuple[int, Any]:
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 1000), state


def _has_hash(target: Any) -> bool:
    """Does this `InputUser` carry a real access hash?

    `InputUserFromMessage` deliberately does not, which is the honest answer
    for a `min` user: we can address them in that one context and nowhere
    else.
    """
    return bool(int(getattr(target, "access_hash", 0) or 0))


# ---------------------------------------------------------------------------
# user get
# ---------------------------------------------------------------------------


class GetReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="@username, id or +phone.")]
    full: Annotated[
        bool, opt("--full", help="Add users.getFullUser (bio, note, birthday, business, blocked).")
    ] = True
    refresh: Annotated[bool, opt("--refresh", help="Ignore the 60 s userFull cache.")] = False
    field: Annotated[
        str | None,
        choice(
            "id",
            "username",
            "phone",
            "bio",
            "birthday",
            "link",
            "status",
            "name",
            help="Emit a single field for scripting.",
        ),
    ] = None
    translate_bio: Annotated[
        str | None, opt("--translate-bio", metavar="LANG", help="Translate the bio.")
    ] = None
    from_chat: Annotated[
        PeerRef | None,
        opt("--from-chat", metavar="CHAT", kind="peer", help="Context for a `min` user."),
    ] = None
    from_message: Annotated[
        int | None,
        opt("--from-message", metavar="ID", kind="msg_id", help="Message id in --from-chat."),
    ] = None


def _colors(user: Any) -> dict[str, Any] | None:
    color = getattr(user, "color", None)
    profile = getattr(user, "profile_color", None)
    if color is None and profile is None:
        return None
    out: dict[str, Any] = {}
    if color is not None:
        out["name_color"] = getattr(color, "color", None)
        out["name_emoji_id"] = getattr(color, "background_emoji_id", None)
    if profile is not None:
        out["profile_color"] = getattr(profile, "color", None)
        out["profile_emoji_id"] = getattr(profile, "background_emoji_id", None)
    return out


def _business_hours(full: Any) -> dict[str, Any] | None:
    hours = getattr(full, "business_work_hours", None)
    if hours is None:
        return None
    return {
        "timezone": getattr(hours, "timezone_id", None),
        "open_now": getattr(hours, "open_now", None),
        "periods": [
            {"start": int(getattr(p, "start_minute", 0)), "end": int(getattr(p, "end_minute", 0))}
            for p in getattr(hours, "weekly_open", None) or []
        ],
    }


def profile_model(user: Any, *, full: Any = None, has_hash: bool = False) -> UserProfile:
    """A `User` (plus an optional `userFull`) as the profile shape.

    v1's keys survive verbatim — `id`, `first_name`, `username`, `bio`,
    `is_bot`, `status`, `stories_hidden`, `deleted`, `has_photo` — because
    AGENT.md documents them and agents read them today.
    """
    raw_id = int(getattr(user, "id", 0) or 0)
    status = getattr(user, "status", None)
    photo = getattr(user, "photo", None)
    model = UserProfile(
        id=raw_id,
        raw_id=raw_id,
        kind="bot" if getattr(user, "bot", False) else "user",
        first_name=getattr(user, "first_name", "") or "",
        last_name=getattr(user, "last_name", "") or "",
        name=display_name(user),
        username=getattr(user, "username", None),
        usernames=[
            u.username
            for u in (getattr(user, "usernames", None) or [])
            if getattr(u, "username", None)
        ],
        phone=getattr(user, "phone", None),
        status=status_word(status),
        status_detail=status_model(raw_id, status) if status is not None else None,
        is_self=bool(getattr(user, "is_self", False)),
        is_bot=bool(getattr(user, "bot", False)),
        is_contact=bool(getattr(user, "contact", False)),
        is_mutual_contact=bool(getattr(user, "mutual_contact", False)),
        is_close_friend=bool(getattr(user, "close_friend", False)),
        is_premium=bool(getattr(user, "premium", False)),
        is_support=bool(getattr(user, "support", False)),
        is_verified=bool(getattr(user, "verified", False)),
        is_scam=bool(getattr(user, "scam", False)),
        is_fake=bool(getattr(user, "fake", False)),
        deleted=bool(getattr(user, "deleted", False)),
        restricted=bool(getattr(user, "restricted", False)),
        restriction_reason=[
            str(getattr(r, "text", "") or "")
            for r in getattr(user, "restriction_reason", None) or []
        ],
        # No photo together with an empty status is the classic signature of
        # an account that blocked us — or of an abandoned one. Both signals
        # are reported; the conclusion is not drawn here, because it cannot
        # be drawn correctly.
        has_photo=photo is not None and type(photo).__name__ != "UserProfilePhotoEmpty",
        stories_hidden=bool(getattr(user, "stories_hidden", False)),
        lang_code=getattr(user, "lang_code", None),
        photo=photo_summary(photo),
        emoji_status_id=getattr(getattr(user, "emoji_status", None), "document_id", None),
        colors=_colors(user),
        access_hash_cached=has_hash or bool(getattr(user, "access_hash", None)),
        min=bool(getattr(user, "min", False)),
    )
    if full is None:
        return model

    model.full = True
    model.bio = getattr(full, "about", None) or ""
    note = getattr(full, "note", None)
    model.note = getattr(note, "text", None) if note is not None else None
    model.birthday = birthday_text(getattr(full, "birthday", None))
    model.blocked = getattr(full, "blocked", None)
    model.blocked_my_stories_from = getattr(full, "blocked_my_stories_from", None)
    model.common_chats_count = getattr(full, "common_chats_count", None)
    model.personal_channel_id = getattr(full, "personal_channel_id", None)
    model.personal_channel_message_id = getattr(full, "personal_channel_message", None)
    model.contact_require_premium = getattr(full, "contact_require_premium", None)
    model.send_paid_messages_stars = getattr(full, "send_paid_messages_stars", None)
    model.stargifts_count = getattr(full, "stargifts_count", None)
    rating = getattr(full, "stars_rating", None)
    model.stars_rating = getattr(rating, "level", None) if rating is not None else None
    tab = getattr(full, "main_tab", None)
    model.main_tab = type(tab).__name__.removeprefix("ProfileTab").lower() if tab else None
    model.unofficial_security_risk = getattr(full, "unofficial_security_risk", None)
    model.business_hours = _business_hours(full)
    location = getattr(full, "business_location", None)
    model.business_location = getattr(location, "address", None) if location else None
    intro = getattr(full, "business_intro", None)
    if intro is not None:
        model.business_intro = {
            "title": getattr(intro, "title", None),
            "description": getattr(intro, "description", None),
        }
    model.personal_photo = photo_summary(getattr(full, "personal_photo", None))
    model.fallback_photo = photo_summary(getattr(full, "fallback_photo", None))
    paper = getattr(full, "wallpaper", None)
    model.wallpaper = getattr(paper, "slug", None) if paper is not None else None
    settings = getattr(full, "settings", None)
    if settings is not None:
        from tlgr.models.base import to_builtins

        model.action_bar = to_builtins(action_bar(settings, chat_id=raw_id))
    return model


async def get(ctx: OpContext, req: GetReq) -> UserProfile:
    """Full profile of one user.

    A bare numeric id resolves only from this account's own peer cache —
    there is no MTProto call that turns an id into an access hash — so an
    uncached one fails rather than guessing. For a `min` user (someone seen
    only inside a channel message) pass `--from-chat/--from-message`;
    Telethon builds `inputUserFromMessage` for nobody.
    """
    from telethon.tl.functions import messages as mfn
    from telethon.tl.functions import users as ufn

    target = await input_user(ctx, req.user, from_chat=req.from_chat, from_message=req.from_message)
    user = await fetch_user(ctx, target)

    full = None
    if req.full:
        try:
            answer = await client_of(ctx)(ufn.GetFullUserRequest(id=target))
        except Exception as exc:
            # A profile we can see the shell of but not the inside of is a
            # real state (privacy, a deleted account); half an answer beats
            # an error that hides the half we do have.
            ctx.warn(f"users.getFullUser failed, reporting the short profile only: {exc}")
            answer = None
        if answer is not None:
            full = getattr(answer, "full_user", None)
            for candidate in getattr(answer, "users", None) or []:
                if int(getattr(candidate, "id", 0) or 0) == int(user.id):
                    user = candidate

    model = profile_model(user, full=full, has_hash=_has_hash(target))

    if req.translate_bio and model.bio:
        from telethon.tl import types

        try:
            translated = await client_of(ctx)(
                mfn.TranslateTextRequest(
                    to_lang=req.translate_bio,
                    text=[types.TextWithEntities(text=model.bio, entities=[])],
                )
            )
            first = list(getattr(translated, "result", None) or [])
            model.bio_translated = getattr(first[0], "text", None) if first else None
        except Exception as exc:  # pragma: no cover - server-side feature gate
            ctx.warn(f"bio translation is unavailable: {exc}")

    if req.field:
        # `--field` is a projection, not a different response: the other keys
        # are cleared rather than the shape changing, so a script that reads
        # `.username` keeps working either way.
        keep = {
            "id": "id",
            "username": "username",
            "phone": "phone",
            "bio": "bio",
            "birthday": "birthday",
            "status": "status",
            "name": "name",
        }.get(req.field)
        if req.field == "link":
            keep = "username"
        if keep is not None:
            blank = UserProfile(id=model.id)
            setattr(blank, keep, getattr(model, keep))
            return blank
    return model


SPEC_GET = OperationSpec(
    id="user.get",
    request=GetReq,
    response=UserProfile,
    impl=get,
    summary="Full profile of a user",
    description=(
        "Never prints an access hash: `access_hash_cached` says whether one "
        "is held. A bare numeric id resolves only from this account's peer "
        "cache; for a `min` user pass --from-chat/--from-message so "
        "`inputUserFromMessage` can be built. `userFull` is invalidated "
        "server-side after 60 s and whenever our own last-seen privacy "
        "changes. No photo plus an empty status is a signal, not a verdict: "
        "this never claims 'they blocked you'."
    ),
    legacy_paths=("user get",),
    columns=("id", "first_name", "username", "bio", "is_bot", "status", "stories_hidden"),
    example=_EXAMPLE_USER,
    example_args="user get @alice",
    covers=(
        "contacts-users.block-status",
        "contacts-users.resolve-min-users",
        "contacts-users.resolve-user-id",
        "contacts-users.user-badges",
        "contacts-users.user-bio",
        "contacts-users.user-bio-translate",
        "contacts-users.user-birthday-read",
        "contacts-users.user-business-hours",
        "contacts-users.user-business-intro",
        "contacts-users.user-business-location",
        "contacts-users.user-copy-fields",
        "contacts-users.user-emoji-status",
        "contacts-users.user-gifts-count",
        "contacts-users.user-main-profile-tab",
        "contacts-users.user-peer-colors",
        "contacts-users.user-phone",
        "contacts-users.user-profile-basic",
        "contacts-users.user-profile-full",
        "contacts-users.user-stars-rating",
        "contacts-users.user-status",
        "contacts-users.user-unofficial-warning",
        "contacts-users.user-usernames",
        "profile.security-risk-flag",
    ),
)


# ---------------------------------------------------------------------------
# user block / unblock
# ---------------------------------------------------------------------------


class BlockReq(Request):
    user: Annotated[
        PeerRef | None,
        arg(0, metavar="USER", required=False, kind="peer", help="User, bot or channel."),
    ] = None
    stories: Annotated[
        bool, opt("--stories", help="Story blocklist only: they keep messaging you.")
    ] = False
    report_spam: Annotated[bool, opt("--report-spam", help="Report spam first.")] = False
    delete_history: Annotated[
        bool, opt("--delete-history", help="Also delete the chat for BOTH sides.")
    ] = False
    from_replies: Annotated[
        int | None,
        opt("--from-replies", metavar="ID", help="Block the author of this Replies message."),
    ] = None
    delete_message: Annotated[
        bool, opt("--delete-message", help="With --from-replies: also delete that message.")
    ] = False


async def block(ctx: OpContext, req: BlockReq) -> BlockResult:
    """Block a user, bot or channel — optionally stories-only, with cleanup.

    The main blocklist and the story blocklist are independent: `--stories`
    stops them seeing our stories and nothing else. Stopping a bot *is*
    `contacts.block(bot)`; restarting it is `user unblock` plus `bot start`.
    """
    from telethon.tl.functions import contacts as fn
    from telethon.tl.functions import messages as mfn

    if req.from_replies is not None:
        await client_of(ctx)(
            fn.BlockFromRepliesRequest(
                msg_id=int(req.from_replies),
                delete_message=req.delete_message or None,
                delete_history=req.delete_history or None,
                report_spam=req.report_spam or None,
            )
        )
        ctx.emit("user_block", {"msg_id": int(req.from_replies), "source": "replies"})
        return BlockResult(
            peer_id=REPLIES_PEER,
            blocked=True,
            deleted=req.delete_history,
            reported=req.report_spam,
        )

    if req.user is None:
        raise UsageError("give a user to block, or --from-replies <msg-id>", field="user")
    peer = await _send.resolve(ctx, req.user)
    marked = _send.peer_id_of(peer)

    reported = False
    if req.report_spam:
        # Reporting before blocking, because a blocked peer's chat is no
        # longer somewhere a report can point at.
        await client_of(ctx)(mfn.ReportSpamRequest(peer=peer))
        reported = True

    await client_of(ctx)(fn.BlockRequest(id=peer, my_stories_from=req.stories or None))

    deleted = False
    if req.delete_history:
        await client_of(ctx)(mfn.DeleteHistoryRequest(peer=peer, max_id=0, revoke=True))
        deleted = True

    ctx.emit("user_block", {"peer_id": marked, "stories_only": req.stories})
    return BlockResult(
        peer_id=marked,
        blocked=True,
        stories_only=req.stories,
        deleted=deleted,
        reported=reported,
    )


SPEC_BLOCK = OperationSpec(
    id="user.block",
    request=BlockReq,
    response=BlockResult,
    impl=block,
    summary="Block a user, bot or channel — optionally stories-only, with report and cleanup",
    description=(
        "The main blocklist stops messages, calls, status, photo and "
        "stories. `--stories` is the independent story blocklist and stops "
        "only stories. `--delete-history` revokes for both sides, which is "
        "why the whole command is destructive."
    ),
    aliases=("chat.block", "contact.blocked.add"),
    mutating=True,
    destructive=True,
    columns=("peer_id", "blocked", "stories_only"),
    example={"peer_id": 777123, "blocked": True, "stories_only": False},
    example_args="user block @spammer",
    covers=(
        "contacts-users.block-delete-and-block",
        "contacts-users.block-from-replies",
        "dialogs.block-stories",
        "dialogs.block-user",
        "dialogs.bot-stop-restart",
    ),
    tags=frozenset({"visible-to-others"}),
)


class UnblockReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="peer", help="User, bot or channel.")]
    stories: Annotated[bool, opt("--stories", help="Remove from the story blocklist instead.")] = (
        False
    )


async def unblock(ctx: OpContext, req: UnblockReq) -> BlockResult:
    """Unblock a user, bot or channel. Idempotent."""
    from telethon.tl.functions import contacts as fn

    peer = await _send.resolve(ctx, req.user)
    marked = _send.peer_id_of(peer)
    changed = await client_of(ctx)(fn.UnblockRequest(id=peer, my_stories_from=req.stories or None))
    already = changed is False
    if already:
        mark_already(ctx)
    else:
        ctx.emit("user_unblock", {"peer_id": marked, "stories_only": req.stories})
    return BlockResult(peer_id=marked, blocked=False, stories_only=req.stories, already=already)


SPEC_UNBLOCK = OperationSpec(
    id="user.unblock",
    request=UnblockReq,
    response=BlockResult,
    impl=unblock,
    summary="Unblock a user, bot or channel",
    description="`already: true` means the peer was not on the list and no RPC changed anything.",
    aliases=("chat.unblock", "contact.blocked.remove"),
    mutating=True,
    idempotent=True,
    columns=("peer_id", "blocked", "already"),
    example={"peer_id": 777123, "blocked": False, "already": False},
    example_args="user unblock @alice",
    covers=("contacts-users.block-unblock", "dialogs.unblock-user"),
)


# ---------------------------------------------------------------------------
# user dialog-status
# ---------------------------------------------------------------------------


class DialogStatusReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Who to ask about.")]
    max_dialogs: Annotated[
        int,
        opt(
            "--max-dialogs",
            metavar="N",
            help="Cap the fallback dialog scan. Hitting it is indeterminate, never 'no'.",
            ge=1,
        ),
    ] = 5000


async def dialog_status(ctx: OpContext, req: DialogStatusReq) -> DialogStatus:
    """Does this account have prior history with this user? Three-valued.

    SEMANTICS FROZEN (AGENT.md). The naive probe — list a few messages, read
    the error — is unsound for a bare numeric id, because
    `get_input_entity` only consults the local cache and its network fallback
    returns `UserEmpty` for any non-contact. So:

    1. try to address the peer cheaply and, if that works, ask the server
       directly with `messages.getPeerDialogs` plus an exact message total;
    2. if it cannot be addressed, enumerate the account's *complete* dialog
       list. Finding the id is a positive; **exhausting** the list is the
       only thing that licenses a negative;
    3. if neither completes — cap, flood, RPC failure — report
       `resolved: false` and exit 13 so the caller fails closed.

    It reports on the dialog list: a conversation this account itself deleted
    is gone server-side too and correctly reads as no dialog.
    """
    from telethon import utils
    from telethon.tl import types
    from telethon.tl.functions import messages as mfn

    def unknown(reason: str) -> DialogStatus:
        """The third answer: report it, and make the process fail closed.

        The body is still returned — a caller needs `reason` and
        `scanned_dialogs` to decide what to do — and `mark_indeterminate`
        is what turns the exit status into 13, so "could not establish"
        can never be read as "no history".
        """
        out.reason = reason
        out.resolved = False
        out.has_dialog = None
        mark = getattr(ctx, "mark_indeterminate", None)
        if callable(mark):
            mark(reason)
        return out

    out = DialogStatus(ref=getattr(req.user, "raw", str(req.user)))
    target_id: int | None = None
    target_username: str | None = None
    if req.user.kind == "id":
        target_id = int(req.user.value)
    elif req.user.kind == "username":
        target_username = str(req.user.value).lstrip("@").lower()

    client = client_of(ctx)
    peer: Any = None
    try:
        peer = await _send.resolve(ctx, req.user)
    except Exception as exc:
        # NOT evidence of absence — a cold cache or an unknown handle.
        out.reason = f"entity not resolvable directly: {exc}"

    scanned = 0
    if peer is None:
        if target_id is None and target_username is None:
            return unknown(f"unusable reference: {out.ref!r}")
        try:
            async for dialog in client.iter_dialogs(limit=req.max_dialogs):
                scanned += 1
                entity = getattr(dialog, "entity", None)
                entity_id = getattr(entity, "id", None)
                dialog_id = getattr(dialog, "id", None)
                handle = (getattr(entity, "username", None) or "").lower()
                if (target_id is not None and target_id in (entity_id, dialog_id)) or (
                    target_username is not None and handle == target_username
                ):
                    peer = entity
                    out.source = "dialog_scan"
                    break
            else:
                if scanned >= req.max_dialogs:
                    out.scanned_dialogs = scanned
                    return unknown(
                        f"dialog scan hit the {req.max_dialogs}-dialog cap without a "
                        "match — indeterminate, NOT a negative"
                    )
        except Exception as exc:
            out.scanned_dialogs = scanned
            return unknown(f"dialog scan did not complete: {exc}")

        out.scanned_dialogs = scanned
        if peer is None:
            # The server handed over every dialog this account has and the
            # peer was not among them. This is the definitive negative, and
            # the only one.
            out.resolved = True
            out.has_dialog = False
            out.message_count = 0
            out.source = "dialog_scan"
            out.reason = "absent from the account's complete dialog list"
            out.id = target_id
            out.username = target_username
            return out

    try:
        input_peer = await client.get_input_entity(peer)
        answer = await client(
            mfn.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer=input_peer)])
        )
        dialogs = list(getattr(answer, "dialogs", None) or [])
        top = max((int(getattr(d, "top_message", 0) or 0) for d in dialogs), default=0)
        messages = await client.get_messages(input_peer, limit=1)
        total = getattr(messages, "total", None)
        total = int(total if total is not None else len(messages or []))
    except Exception as exc:
        return unknown(f"server dialog query failed: {exc}")

    with contextlib.suppress(TypeError, ValueError):
        out.id = int(utils.get_peer_id(peer))
    if out.id is None:
        out.id = target_id
    out.username = getattr(peer, "username", None) or target_username
    out.resolved = True
    # A scan hit stays a positive even if both sides have since wiped the
    # history: presence in the dialog list *is* the dialog.
    out.has_dialog = out.source == "dialog_scan" or bool(top) or total > 0
    out.message_count = total
    if out.source != "dialog_scan":
        out.source = "peer_dialogs"
    return out


SPEC_DIALOG_STATUS = OperationSpec(
    id="user.dialog-status",
    request=DialogStatusReq,
    response=DialogStatus,
    impl=dialog_status,
    summary="Does this account have prior history with this user? (three-valued, never guessed)",
    description=(
        "resolved=true/has_dialog=true — a dialog exists, message_count is "
        "the server's exact total. resolved=true/has_dialog=false — "
        "definitively none, because the COMPLETE dialog list was enumerated. "
        "resolved=false/has_dialog=null — exit 13, and `reason` says why. "
        "Exit 13 means UNKNOWN: a caller gating a cold first message must "
        "treat it as a refusal, never as a green light."
    ),
    legacy_paths=("user dialog-status",),
    rate_class="bulk",
    timeout_s=600,
    columns=("id", "username", "resolved", "has_dialog", "message_count", "source"),
    example={
        "ref": "@alice",
        "id": 777123,
        "username": "alice",
        "resolved": True,
        "has_dialog": True,
        "message_count": 12,
        "source": "peer_dialogs",
    },
    example_args="user dialog-status @alice",
    covers=("contacts-users.user-dialog-exists", "dialogs.dialog-exists"),
    covers_partial=("dialogs.resolve-peer",),
    coverage_note=(
        "The reference-resolution half of `dialogs.resolve-peer` is `resolve peer`; "
        "this op only answers the has-a-dialog question about a user."
    ),
)


# ---------------------------------------------------------------------------
# user hide-stories
# ---------------------------------------------------------------------------


class HideStoriesReq(Request):
    user: Annotated[
        list[PeerRef],
        arg(0, metavar="USER", variadic=True, kind="user", help="Peers to hide."),
    ] = []
    unhide: Annotated[bool, opt("--unhide", help="Put them back in the main stories bar.")] = False
    all_stories: Annotated[
        str | None,
        opt("--all", metavar="ON|OFF", help="Collapse or expand the whole story strip."),
    ] = None


async def hide_stories(ctx: OpContext, req: HideStoriesReq) -> StoriesHidden:
    """Hide or unhide a peer's stories — per account, silently.

    SEMANTICS FROZEN (AGENT.md). Exactly Telegram's own "Hide Stories" menu
    item: the peer leaves the main stories bar for the collapsed Hidden list.
    The other side is never notified and nothing about the chat, the contact
    entry or their access to us changes.

    The fresh `stories_hidden` flag is read first, so a peer already in the
    requested state costs no RPC and reports `already: true` — which is what
    makes a bulk pass over hundreds of peers nearly free to repeat.
    """
    from telethon.tl.functions import stories as sfn

    hidden = not req.unhide
    result = StoriesHidden(hidden=hidden)

    if req.all_stories is not None:
        wanted = req.all_stories.strip().lower()
        if wanted not in ("on", "off"):
            raise UsageError("--all takes on or off", field="all")
        await client_of(ctx)(sfn.ToggleAllStoriesHiddenRequest(hidden=wanted == "on"))
        result.all_hidden = wanted == "on"
        if not req.user:
            return result

    if not req.user:
        raise UsageError("give at least one user, or --all on|off", field="user")

    rows: list[StoriesHiddenPeer] = []
    for ref in req.user:
        target = await input_user(ctx, ref)
        user = await fetch_user(ctx, target)
        was = bool(getattr(user, "stories_hidden", False))
        already = was == hidden
        if not already:
            peer = await _send.resolve(ctx, ref)
            await client_of(ctx)(sfn.TogglePeerStoriesHiddenRequest(peer=peer, hidden=hidden))
            ctx.emit("stories_hidden", {"user_id": int(user.id), "hidden": hidden})
        rows.append(
            StoriesHiddenPeer(
                user_id=int(getattr(user, "id", 0) or 0),
                username=getattr(user, "username", None),
                hidden=hidden,
                already=already,
            )
        )

    first = rows[0]
    result.user_id = first.user_id
    result.username = first.username
    result.hidden = first.hidden
    result.already = first.already
    if len(rows) > 1:
        result.peers = rows
    elif all(row.already for row in rows):
        mark_already(ctx)
    return result


SPEC_HIDE_STORIES = OperationSpec(
    id="user.hide-stories",
    request=HideStoriesReq,
    response=StoriesHidden,
    impl=hide_stories,
    summary="Hide or unhide a peer's stories (per-account; the other side is never notified)",
    description=(
        "Idempotent: the fresh flag is read first and `already: true` means "
        "no RPC was sent, so repeating a bulk pass is nearly free. Purely "
        "local to this account — the chat, the contact entry and their "
        "access to you are untouched. `user get` reports the current value "
        "as `stories_hidden`. More than one peer fills `peers`; a single "
        "peer answers exactly as v1 did."
    ),
    legacy_paths=("user hide-stories",),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("user_id", "username", "hidden", "already"),
    example={"user_id": 777123, "username": "alice", "hidden": True, "already": False},
    example_args="user hide-stories @alice",
    covers=("contacts-users.user-hide-stories", "dialogs.hide-stories-peer"),
)


# ---------------------------------------------------------------------------
# user can-message
# ---------------------------------------------------------------------------


class CanMessageReq(Request):
    user: Annotated[
        list[PeerRef],
        arg(0, metavar="USER", variadic=True, kind="user", help="Who to check."),
    ] = []


async def can_message(ctx: OpContext, req: CanMessageReq) -> Page[ContactRequirement]:
    """Can I message this user, and at what price?

    Pairs with `user dialog-status` for cold-outreach gating: this answers
    "am I allowed to", that one answers "have I already". Reading the Stars
    price is fine; paying it is a payment a human initiates.
    """
    from telethon.tl.functions import users as ufn

    if not req.user:
        raise UsageError("give at least one user", field="user")
    targets = [await input_user(ctx, ref) for ref in req.user]
    answers = list(await client_of(ctx)(ufn.GetRequirementsToContactRequest(id=targets)) or [])

    rows: list[ContactRequirement] = []
    for target, answer in zip(targets, answers, strict=False):
        name = type(answer).__name__
        kind = {
            "RequirementToContactEmpty": "free",
            "RequirementToContactPremium": "premium",
            "RequirementToContactPaidMessages": "paid",
        }.get(name, "unknown")
        rows.append(
            ContactRequirement(
                user_id=int(getattr(target, "user_id", 0) or 0),
                result=kind,  # type: ignore[arg-type]
                stars_amount=getattr(answer, "stars_amount", None),
                contact_require_premium=kind == "premium" or None,
            )
        )
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_CAN_MESSAGE = OperationSpec(
    id="user.can-message",
    request=CanMessageReq,
    response=Page[ContactRequirement],
    impl=can_message,
    summary="Can I message this user, and at what price?",
    description=(
        "`free` | `premium` | `paid` (with `stars_amount`). The send-time "
        "failure this predicts is PRIVACY_PREMIUM_REQUIRED (403)."
    ),
    columns=("user_id", "result", "stars_amount"),
    headers=("User", "Requirement", "Stars"),
    example={"items": [{"user_id": 777123, "result": "free"}], "has_more": False},
    example_args="user can-message @alice",
    covers=(
        "contacts-users.user-paid-messages",
        "contacts-users.user-requirements-to-contact",
        "privacy.requirements-to-contact",
    ),
)


# ---------------------------------------------------------------------------
# user chat list
# ---------------------------------------------------------------------------


class ChatListReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Whose common chats.")]
    leave_all: Annotated[bool, opt("--leave-all", help="Leave every listed chat.")] = False


async def chat_list(ctx: OpContext, req: ChatListReq) -> Page[Chat]:
    """Groups and channels shared with a user, optionally leaving all of them.

    `userFull.common_chats_count` is the count; this is the list. Leaving is
    opt-in and destructive, and the chats are listed before anything is left.
    """
    from telethon.tl.functions import channels as cfn
    from telethon.tl.functions import messages as mfn

    limit, state = _window(ctx, "user.chat.list", PageKind.PARTICIPANTS, default=100)
    max_id = int(state.get("max_id", 0) or 0)
    target = await input_user(ctx, req.user)
    result = await client_of(ctx)(
        mfn.GetCommonChatsRequest(user_id=target, max_id=max_id, limit=limit)
    )
    chats = list(getattr(result, "chats", None) or [])

    rows: list[Chat] = []
    for entity in chats:
        peer = entity_to_peer(entity)
        rows.append(
            Chat(
                id=peer.id,
                raw_id=peer.raw_id,
                kind=peer.kind,
                title=peer.title,
                username=peer.username,
                usernames=peer.usernames,
                left=bool(getattr(entity, "left", False)),
            )
        )

    if req.leave_all and rows:
        # Listing is a read and stays dry-runnable, so this branch honours
        # --dry-run itself rather than turning the whole command into a stub.
        if getattr(ctx, "dry_run", False):
            ctx.warn(f"--dry-run: would leave {len(rows)} shared chats")
        else:
            from telethon import utils
            from telethon.tl import types

            for entity in chats:
                try:
                    if type(entity).__name__ == "Channel":
                        await client_of(ctx)(
                            cfn.LeaveChannelRequest(utils.get_input_channel(entity))
                        )
                    else:
                        await client_of(ctx)(
                            mfn.DeleteChatUserRequest(
                                chat_id=int(entity.id), user_id=types.InputUserSelf()
                            )
                        )
                except Exception as exc:
                    ctx.warn(f"could not leave {getattr(entity, 'title', entity)}: {exc}")
                    continue
                row = next((r for r in rows if r.raw_id == int(entity.id)), None)
                if row is not None:
                    row.left = True
            ctx.emit("user_common_chats_leave", {"count": len(rows)})

    return build_page(
        rows,
        op="user.chat.list",
        kind=PageKind.PARTICIPANTS,
        state={"max_id": min((abs(row.raw_id) for row in rows), default=0)},
        account=ctx.account,
        limit=limit,
    )


SPEC_CHAT_LIST = OperationSpec(
    id="user.chat.list",
    request=ChatListReq,
    response=Page[Chat],
    impl=chat_list,
    summary="Groups and channels you share with a user",
    description=(
        "`--leave-all` leaves every listed chat immediately — run it under "
        "--dry-run first, which prints what would go. `userFull."
        "common_chats_count` is the count; this is the list."
    ),
    aliases=("user.common-chats",),
    paginated=PageKind.PARTICIPANTS,
    rate_class="bulk",
    tags=frozenset({"mutating-checked"}),
    columns=("id", "title", "kind", "left"),
    headers=("Id", "Title", "Kind", "Left"),
    example={
        "items": [{"id": -1001234, "raw_id": 1234, "kind": "supergroup", "title": "News"}],
        "has_more": False,
    },
    example_args="user chat list @alice",
    covers=("contacts-users.user-leave-common-groups",),
)


# ---------------------------------------------------------------------------
# user link
# ---------------------------------------------------------------------------


class LinkReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Use `me` with --token.")]
    profile: Annotated[
        bool, opt("--profile", help="Add ?profile so clients open the profile, not the chat.")
    ] = False
    text: Annotated[
        str | None, opt("--text", metavar="TEXT", help="Pre-fill a draft (?text=).")
    ] = None
    scheme: Annotated[str, choice("tme", "tg", help="Link flavour.")] = "tme"
    token: Annotated[
        bool, opt("--token", help="For `me`: a t.me/contact/<token> link with no username.")
    ] = False


async def link(ctx: OpContext, req: LinkReq) -> UserLink:
    """Build a link to a user, or my own temporary contact-token link.

    A contact token EXPIRES, so the expiry is reported next to the URL — a
    link with no expiry printed is a link somebody will paste next month.
    """
    from urllib.parse import quote

    from telethon.tl.functions import contacts as fn

    if req.token:
        if req.user.kind not in ("self", "saved"):
            raise UsageError("--token builds a link to your own profile: use `me`", field="user")
        exported = await client_of(ctx)(fn.ExportContactTokenRequest())
        expires = getattr(exported, "expires", None)
        return UserLink(
            url=str(getattr(exported, "url", "") or ""),
            kind="contact-token",
            expires=fmt_dt(expires),
            expires_unix=to_unix(expires),
        )

    target = await input_user(ctx, req.user)
    user = await fetch_user(ctx, target)
    handle = getattr(user, "username", None)
    query: list[str] = []
    if req.profile:
        query.append("profile")
    if req.text:
        # A draft starting with '@' would be read as a username by the
        # clients that honour ?text=, so it is prefixed with a space.
        text = req.text if not req.text.startswith("@") else " " + req.text
        query.append("text=" + quote(text[:4096], safe=""))

    if req.scheme == "tg":
        base = f"tg://user?id={int(user.id)}" if not handle else f"tg://resolve?domain={handle}"
        joined = base + ("&" + "&".join(query) if query else "")
        return UserLink(url=joined, kind="profile" if req.profile else "chat")

    if not handle:
        raise NotFoundError(
            "that user has no public username, so no t.me link exists for them; "
            "use --scheme tg, which addresses them by id"
        )
    joined = f"https://t.me/{handle}" + ("?" + "&".join(query) if query else "")
    return UserLink(url=joined, kind="profile" if req.profile else "chat")


SPEC_LINK = OperationSpec(
    id="user.link",
    request=LinkReq,
    response=UserLink,
    impl=link,
    summary="Build a link to a user (t.me / tg://), or my own temporary profile link",
    description=(
        "Mostly local string building. `--token` is the exception: "
        "`contacts.exportContactToken` mints a t.me/contact/<token> link that "
        "works without a username and EXPIRES, so `expires` is always "
        "reported next to it."
    ),
    columns=("url", "kind", "expires"),
    example={"url": "https://t.me/alice", "kind": "chat"},
    example_args="user link @alice --profile",
    covers=("contacts-users.contact-token-export", "contacts-users.user-link-build"),
)


# ---------------------------------------------------------------------------
# user photo list / set
# ---------------------------------------------------------------------------


class PhotoListReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Whose photos.")]
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Download into this directory."),
    ] = None
    big: Annotated[bool, opt("--big", help="Prefer the largest size when downloading.")] = False


async def photo_list(ctx: OpContext, req: PhotoListReq) -> Page[ProfilePhoto]:
    """A user's profile-photo history.

    Personal and fallback photos are NOT in here — they are `userFull` fields
    and `user get --full` reports them.
    """
    from telethon.tl.functions import photos as pfn

    limit, state = _window(ctx, "user.photo.list", PageKind.PARTICIPANTS, default=50)
    offset = int(state.get("offset", 0) or 0)
    target = await input_user(ctx, req.user)
    result = await client_of(ctx)(
        pfn.GetUserPhotosRequest(user_id=target, offset=offset, max_id=0, limit=limit)
    )
    photos = list(getattr(result, "photos", None) or [])
    total = getattr(result, "count", None)

    rows: list[ProfilePhoto] = []
    for photo in photos:
        date = getattr(photo, "date", None)
        rows.append(
            ProfilePhoto(
                id=int(getattr(photo, "id", 0) or 0),
                date=fmt_dt(date),
                date_unix=to_unix(date),
                sizes=[
                    str(getattr(size, "type", ""))
                    for size in getattr(photo, "sizes", None) or []
                    if getattr(size, "type", None)
                ],
                video=bool(getattr(photo, "video_sizes", None)),
                dc_id=getattr(photo, "dc_id", None),
            )
        )

    if req.download:
        from pathlib import Path

        directory = Path(req.download).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        for photo, row in zip(photos, rows, strict=True):
            try:
                saved = await client_of(ctx).download_media(
                    photo, file=str(directory / f"{row.id}.jpg")
                )
            except Exception as exc:
                ctx.warn(f"could not download photo {row.id}: {exc}")
                continue
            row.file = str(saved) if saved else None

    return build_page(
        rows,
        op="user.photo.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(rows)},
        account=ctx.account,
        limit=limit,
        total=int(total) if total is not None else None,
    )


SPEC_PHOTO_LIST = OperationSpec(
    id="user.photo.list",
    request=PhotoListReq,
    response=Page[ProfilePhoto],
    impl=photo_list,
    summary="A user's profile-photo history",
    aliases=("user.photos",),
    paginated=PageKind.PARTICIPANTS,
    rate_class="file",
    timeout_s=300,
    columns=("id", "date", "video"),
    headers=("Photo", "Taken", "Video"),
    example={"items": [{"id": 55123, "video": False}], "has_more": False},
    example_args="user photo list @alice",
    covers=("contacts-users.user-profile-photos",),
)


class PhotoSetReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Whose card to change.")]
    file: Annotated[
        str | None, arg(1, metavar="FILE", required=False, kind="path", help="Image or video.")
    ] = None
    suggest: Annotated[
        bool, opt("--suggest", help="Send it as a suggestion instead of applying it locally.")
    ] = False
    video: Annotated[bool, opt("--video", help="Upload as a video avatar.")] = False
    reset: Annotated[bool, opt("--reset", help="Remove the personal photo.")] = False


async def photo_set(ctx: OpContext, req: PhotoSetReq) -> PhotoResult:
    """Set, suggest or reset the personal photo you see for a contact.

    One method, three modes: `save` applies a photo only we see, `suggest`
    posts `messageActionSuggestProfilePhoto` to them (visible, hence --yes),
    and neither with no file removes what is there.
    """
    from pathlib import Path

    from telethon.tl.functions import photos as pfn

    target = await input_user(ctx, req.user)
    user_id = int(getattr(target, "user_id", 0) or 0)

    if req.reset or not req.file:
        await client_of(ctx)(pfn.UploadContactProfilePhotoRequest(user_id=target))
        ctx.emit("user_photo_reset", {"user_id": user_id})
        return PhotoResult(user_id=user_id, reset=True)

    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    path = Path(req.file).expanduser()
    if not path.exists():
        raise UsageError(f"{req.file} does not exist", field="file")
    handle = await upload(path)

    result = await client_of(ctx)(
        pfn.UploadContactProfilePhotoRequest(
            user_id=target,
            suggest=req.suggest or None,
            save=None if req.suggest else True,
            file=None if req.video else handle,
            video=handle if req.video else None,
        )
    )
    photo = getattr(result, "photo", None)
    ctx.emit("user_photo_set", {"user_id": user_id, "suggested": req.suggest})
    return PhotoResult(
        user_id=user_id,
        photo_id=int(getattr(photo, "id", 0) or 0) or None,
        suggested=req.suggest,
    )


SPEC_PHOTO_SET = OperationSpec(
    id="user.photo.set",
    request=PhotoSetReq,
    response=PhotoResult,
    impl=photo_set,
    summary="Set, suggest or reset the personal photo you see for a contact",
    description=(
        "`--suggest` posts a visible message to them; without it the photo "
        "is a private override only this account sees."
    ),
    aliases=("user.set-photo",),
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("user_id", "photo_id", "suggested"),
    example={"user_id": 777123, "photo_id": 55123, "suggested": False},
    example_args="user photo set @alice avatar.jpg",
    covers=(
        "contacts-users.user-personal-photo-reset",
        "contacts-users.user-suggest-photo",
        "profile.photo-personal-for-contact",
        "profile.photo-suggest-to-user",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# user birthday set
# ---------------------------------------------------------------------------


class BirthdaySetReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Who to suggest it to.")]
    date: Annotated[str, arg(1, metavar="DATE", help="YYYY-MM-DD or MM-DD.")]


def _birthday(value: str) -> Any:
    from telethon.tl import types

    parts = [p for p in (value or "").replace("/", "-").split("-") if p]
    try:
        numbers = [int(p) for p in parts]
    except ValueError as exc:
        raise UsageError(f"{value!r} is not a date; use YYYY-MM-DD or MM-DD", field="date") from exc
    year: int | None
    if len(numbers) == 3:
        year, month, day = numbers
    elif len(numbers) == 2:
        year, month, day = None, numbers[0], numbers[1]
    else:
        raise UsageError(f"{value!r} is not a date; use YYYY-MM-DD or MM-DD", field="date")
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise UsageError(f"{value!r} is not a real date", field="date")
    return types.Birthday(day=day, month=month, year=year)


async def birthday_set(ctx: OpContext, req: BirthdaySetReq) -> SuggestedBirthday:
    """Suggest a birthday to a contact.

    This sends `messageActionSuggestBirthday` on our behalf — a visible
    message — so it needs --yes. BIRTHDAY_ALREADY means they already have one
    we can see.
    """
    from telethon.tl.functions import users as ufn

    target = await input_user(ctx, req.user)
    birthday = _birthday(req.date)
    await client_of(ctx)(ufn.SuggestBirthdayRequest(id=target, birthday=birthday))
    user_id = int(getattr(target, "user_id", 0) or 0)
    ctx.emit("user_birthday_suggest", {"user_id": user_id})
    return SuggestedBirthday(
        user_id=user_id, birthday=birthday_text(birthday) or req.date, sent=True
    )


SPEC_BIRTHDAY_SET = OperationSpec(
    id="user.birthday.set",
    request=BirthdaySetReq,
    response=SuggestedBirthday,
    impl=birthday_set,
    summary="Suggest a birthday to a contact",
    aliases=("user.suggest-birthday",),
    mutating=True,
    rate_class="send",
    columns=("user_id", "birthday", "sent"),
    example={"user_id": 777123, "birthday": "1990-04-01", "sent": True},
    example_args="user birthday set @alice 1990-04-01",
    covers=(
        "contact.birthday-accept",
        "contact.suggest-birthday",
        "contacts-users.user-suggest-birthday",
        "profile.birthday-suggest",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# user music list / personal-channel get
# ---------------------------------------------------------------------------


class MusicListReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Whose pinned music.")]
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Download into this directory."),
    ] = None


async def music_list(ctx: OpContext, req: MusicListReq) -> Page[MusicTrack]:
    """Music a user pinned to their profile.

    Visibility is governed by `inputPrivacyKeySavedMusic`, so an empty list
    can mean "none pinned" or "not shared with you"; it is not evidence
    either way.
    """
    from telethon.tl.functions import users as ufn

    limit, state = _window(ctx, "user.music.list", PageKind.PARTICIPANTS, default=50)
    offset = int(state.get("offset", 0) or 0)
    target = await input_user(ctx, req.user)
    result = await client_of(ctx)(
        ufn.GetSavedMusicRequest(id=target, offset=offset, limit=limit, hash=0)
    )
    documents = list(getattr(result, "documents", None) or [])

    rows: list[MusicTrack] = []
    for document in documents:
        title = performer = None
        duration = None
        for attribute in getattr(document, "attributes", None) or []:
            if type(attribute).__name__ == "DocumentAttributeAudio":
                title = getattr(attribute, "title", None)
                performer = getattr(attribute, "performer", None)
                duration = getattr(attribute, "duration", None)
        rows.append(
            MusicTrack(
                id=int(getattr(document, "id", 0) or 0),
                title=title,
                performer=performer,
                duration=duration,
                mime_type=getattr(document, "mime_type", None),
                size=getattr(document, "size", None),
            )
        )

    if req.download:
        from pathlib import Path

        directory = Path(req.download).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        for document, row in zip(documents, rows, strict=True):
            try:
                saved = await client_of(ctx).download_media(document, file=str(directory))
            except Exception as exc:
                ctx.warn(f"could not download track {row.id}: {exc}")
                continue
            row.file = str(saved) if saved else None

    return build_page(
        rows,
        op="user.music.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(rows)},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_MUSIC_LIST = OperationSpec(
    id="user.music.list",
    request=MusicListReq,
    response=Page[MusicTrack],
    impl=music_list,
    summary="Music a user pinned to their profile",
    description=(
        "An empty list is not evidence: `inputPrivacyKeySavedMusic` may "
        "simply not include us. Managing your own is `profile music`."
    ),
    paginated=PageKind.PARTICIPANTS,
    rate_class="file",
    timeout_s=300,
    columns=("id", "title", "performer", "duration"),
    headers=("Id", "Title", "Performer", "Seconds"),
    example={"items": [{"id": 991, "title": "Nocturne", "performer": "Chopin"}], "has_more": False},
    example_args="user music list @alice",
    covers=("contacts-users.user-saved-music",),
)


class PersonalChannelReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Whose profile.")]


async def personal_channel(ctx: OpContext, req: PersonalChannelReq) -> PersonalChannel:
    """The channel a user pinned to their profile, with its latest posts.

    `messages.getPersonalChannelHistory` returns the posts without joining
    the channel and without resolving it separately, which is the only reason
    this is one command rather than three.
    """
    from telethon.tl.functions import messages as mfn
    from telethon.tl.functions import users as ufn

    limit = int(getattr(ctx, "limit", None) or 5)
    target = await input_user(ctx, req.user)
    answer = await client_of(ctx)(ufn.GetFullUserRequest(id=target))
    full = getattr(answer, "full_user", None)
    channel_id = getattr(full, "personal_channel_id", None)
    user_id = int(getattr(target, "user_id", 0) or 0)
    if not channel_id:
        raise NotFoundError("that user has no personal channel pinned to their profile")

    channel = None
    for entity in getattr(answer, "chats", None) or []:
        if int(getattr(entity, "id", 0) or 0) == int(channel_id):
            channel = entity_to_peer(entity)

    history = await client_of(ctx)(
        mfn.GetPersonalChannelHistoryRequest(
            user_id=target, limit=limit, max_id=0, min_id=0, hash=0
        )
    )
    posts = [
        message_to_model(message, chat_id=channel.id if channel else None)
        for message in getattr(history, "messages", None) or []
        if getattr(message, "id", None) is not None
    ]
    return PersonalChannel(
        user_id=user_id,
        channel=channel,
        msg_id=getattr(full, "personal_channel_message", None),
        posts=posts,
    )


SPEC_PERSONAL_CHANNEL_GET = OperationSpec(
    id="user.personal-channel.get",
    request=PersonalChannelReq,
    response=PersonalChannel,
    impl=personal_channel,
    summary="The channel a user pinned to their profile, with its latest posts",
    description="Setting your OWN personal channel is `profile update --personal-channel`.",
    columns=("channel.id", "channel.title", "msg_id"),
    example={
        "user_id": 777123,
        "channel": {"id": -1001234, "raw_id": 1234, "kind": "channel", "title": "Alice writes"},
        "posts": [],
    },
    example_args="user personal-channel get @alice",
    covers=(
        "contacts-users.user-personal-channel",
        "dialogs.personal-channel-preview",
        "groups-channels-admin.personal-channel",
    ),
)
