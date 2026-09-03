"""The rest of the group/channel surface: the parts nobody puts in one menu.

`chat similar`, `chat sponsored`, `chat suggestion`, `chat suggested-post`,
`chat verification`, `chat direct` (channel DMs), `chat affiliate` (Stars
referral bots), and the two layer-229 surfaces — `chat community` and
`chat welcome` — that are registered and refuse.

Two deliberate refusals live here.

* **tlgr never views or clicks a sponsored message.** `chat sponsored list`
  reports them as data and stops there; `messages.viewSponsoredMessage` and
  `clickSponsoredMessage` are impressions, and a headless CLI has no
  viewport, so sending them would be reporting an impression that did not
  happen. The owner-side switch is `chat setting set --ads off`.
* **Communities and welcome messages are layer 229.** Telethon 1.44 speaks
  layer 227 and has no request class for `communities.*` or the
  `ephemeral.*` welcome methods, so those seven commands are registered and
  exit 13 with the reason (`ops/_layer.py`). Registering them is the point:
  a command that refuses with an explanation teaches an agent what is
  missing, and a command that is absent teaches it nothing.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.admin import (
    AffiliateBot,
    AffiliateResult,
    CommunityResult,
    CommunityRow,
    DirectBanResult,
    DirectDialog,
    SimilarChat,
    SponsoredReport,
    SuggestedPostResult,
    SuggestionResult,
    VerificationResult,
    WelcomeMessage,
    WelcomeResult,
)
from tlgr.models.base import Request
from tlgr.models.message import SponsoredMessage
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _admin, _layer, _rights, _send
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import message_entities, peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_SIMILAR: dict[str, Any] = {
    "id": -1001600,
    "title": "Telegram Tips",
    "username": "telegramtips",
    "participants_count": 1200000,
}


def _blob(value: str, field: str) -> bytes:
    """A server-issued opaque id, given back the way it was printed."""
    text = str(value).strip()
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise UsageError(
                f"{field} is the opaque id `chat sponsored list` printed", field=field
            ) from exc


# ---------------------------------------------------------------------------
# chat similar
# ---------------------------------------------------------------------------


class SimilarReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", kind="peer", required=False, help="Channel, or omit for mine."),
    ] = None
    bots: Annotated[bool, opt("--bots", help="Bots similar to this bot instead.")] = False


async def list_similar(ctx: OpContext, req: SimilarReq) -> Page[SimilarChat]:
    """Channels similar to one channel, or the account's own recommendations."""
    from telethon.tl.functions import bots as bot_fn
    from telethon.tl.functions import channels as fn

    limit, _state = _admin.window(ctx, "chat.similar.list", PageKind.PARTICIPANTS)
    handle = _admin.client(ctx)
    if req.bots:
        if req.chat is None:
            raise UsageError("--bots needs the bot to be similar to", field="chat")
        peer = await _send.resolve(ctx, req.chat)
        reply = await handle(bot_fn.GetBotRecommendationsRequest(bot=_admin.input_user(peer)))
    else:
        channel = None
        if req.chat is not None:
            channel = _admin.input_channel(await _send.resolve(ctx, req.chat))
        reply = await handle(fn.GetChannelRecommendationsRequest(channel=channel))
    rows = [
        SimilarChat(
            id=_admin.entity_id(chat),
            title=str(getattr(chat, "title", "") or ""),
            username=getattr(chat, "username", None),
            participants_count=getattr(chat, "participants_count", None),
        )
        for chat in (getattr(reply, "chats", None) or [])
    ]
    # A `chatsSlice` means the server truncated the list for a non-Premium
    # account; reporting `total` is how a script can see the cut instead of
    # concluding there are only five similar channels.
    total = int(getattr(reply, "count", len(rows)) or len(rows))
    return build_page(
        rows[:limit],
        op="chat.similar.list",
        kind=PageKind.PARTICIPANTS,
        account=ctx.account,
        has_more=len(rows) > limit,
        total=total,
    )


SPEC_SIMILAR_LIST = OperationSpec(
    id="chat.similar.list",
    request=SimilarReq,
    response=Page[SimilarChat],
    impl=list_similar,
    summary="Channels similar to this one (or global recommendations)",
    description=(
        "A non-Premium account gets a truncated `messages.chatsSlice`; "
        "`total` reports the full count so the cut is visible. There is no "
        "bare `chat similar` alias: Click cannot hold a command and a group "
        "under one name, and `chat similar list` is the canonical path."
    ),
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "title", "username", "participants_count"),
    example={"items": [_EXAMPLE_SIMILAR], "has_more": False},
    example_args="chat similar list @somechannel",
    covers=(
        "contacts-users.people-you-may-know",
        "dialogs.recommended-channels",
        "groups-channels-admin.similar-channels",
    ),
)


# ---------------------------------------------------------------------------
# chat sponsored
# ---------------------------------------------------------------------------


class SponsoredListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]


async def list_sponsored(ctx: OpContext, req: SponsoredListReq) -> list[SponsoredMessage]:
    """Sponsored messages the server wants shown here — as data, nothing else.

    tlgr never calls `messages.viewSponsoredMessage` or
    `clickSponsoredMessage`: those report an impression, a headless CLI has
    no viewport, and faking one would be dishonest to both the advertiser and
    the channel owner.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    reply = await _admin.client(ctx)(fn.GetSponsoredMessagesRequest(peer=peer))
    out: list[SponsoredMessage] = []
    for row in getattr(reply, "messages", None) or []:
        random_id = getattr(row, "random_id", b"") or b""
        out.append(
            SponsoredMessage(
                random_id=base64.b64encode(random_id).decode(),
                title=getattr(row, "title", None),
                message=str(getattr(row, "message", "") or ""),
                entities=message_entities(row),
                url=getattr(row, "url", None),
                button_text=getattr(row, "button_text", None),
                sponsor_info=getattr(row, "sponsor_info", None),
                additional_info=getattr(row, "additional_info", None),
                recommended=bool(getattr(row, "recommended", False)),
                can_report=bool(getattr(row, "can_report", False)),
                viewed=False,
            )
        )
    return out


SPEC_SPONSORED_LIST = OperationSpec(
    id="chat.sponsored.list",
    request=SponsoredListReq,
    response=list[SponsoredMessage],
    impl=list_sponsored,
    summary="Sponsored messages the server wants shown in this channel",
    description=(
        "Exposed as data only; `viewed` is always false because tlgr never "
        "reports an impression it did not make. Results are cached five "
        "minutes server-side, which is the API's own contract. Turning them "
        "off for your channel is `chat setting set --ads off`."
    ),
    columns=("random_id", "title", "url"),
    example=[{"random_id": "AQID", "title": "Sponsor", "message": "Try this", "url": "https://x"}],
    example_args="chat sponsored list @somechannel",
    empty_exit=EXIT_EMPTY,
    covers_partial=("groups-channels-admin.channel-sponsored-messages",),
    coverage_note="Reading them; `chat sponsored report` owns the id and reports one.",
)


class SponsoredReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    random_id: Annotated[
        str, arg(1, metavar="RANDOM_ID", help="The opaque id from `chat sponsored list`.")
    ]
    option: Annotated[
        str | None,
        opt("--option", metavar="BLOB", help="Server-provided option for the next step."),
    ] = None
    comment: Annotated[str, opt("--comment", metavar="TEXT", help="Free-text comment.")] = ""


async def report_sponsored(ctx: OpContext, req: SponsoredReportReq) -> SponsoredReport:
    """Report a sponsored message. The reason menu is the server's, not ours."""
    from telethon.tl.functions import messages as fn

    await _send.resolve(ctx, req.chat)
    reply = await _admin.client(ctx)(
        fn.ReportSponsoredMessageRequest(
            random_id=_blob(req.random_id, "random_id"),
            option=_blob(req.option, "option") if req.option else b"",
        )
    )
    kind = type(reply).__name__
    if kind == "SponsoredMessageReportResultChooseOption":
        return SponsoredReport(
            result="choose-option",
            title=str(getattr(reply, "title", "") or ""),
            options=[
                {
                    "text": str(getattr(item, "text", "") or ""),
                    "option": base64.b64encode(getattr(item, "option", b"") or b"").decode(),
                }
                for item in (getattr(reply, "options", None) or [])
            ],
        )
    if kind == "SponsoredMessageReportResultAdsHidden":
        return SponsoredReport(result="ads-hidden")
    return SponsoredReport(result="reported")


SPEC_SPONSORED_REPORT = OperationSpec(
    id="chat.sponsored.report",
    request=SponsoredReportReq,
    response=SponsoredReport,
    impl=report_sponsored,
    summary="Report a sponsored message",
    description=(
        "The reason menu is server-driven: with no `--option` the command "
        "prints the option blobs and exits 0, so a script can walk the tree "
        "one level at a time."
    ),
    mutating=True,
    columns=("result", "title"),
    example={"result": "choose-option", "title": "What is wrong?", "options": []},
    example_args="chat sponsored report @somechannel AQID",
    covers=("groups-channels-admin.channel-sponsored-messages",),
)


# ---------------------------------------------------------------------------
# chat suggestion / suggested-post / verification
# ---------------------------------------------------------------------------


class SuggestionDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    key: Annotated[str, arg(1, metavar="KEY", help="The suggestion key to dismiss.")]


async def dismiss_suggestion(ctx: OpContext, req: SuggestionDeleteReq) -> SuggestionResult:
    """Dismiss one server-suggested admin action, and report what is left."""
    from telethon.tl.functions import help as fn

    peer = await _send.resolve(ctx, req.chat)
    await _admin.client(ctx)(fn.DismissSuggestionRequest(peer=peer, suggestion=req.key))
    full, _entity, _entities = await _admin.full_chat(ctx, peer)
    return SuggestionResult(
        chat_id=peer_id_of(peer) or 0,
        key=req.key,
        pending_suggestions=list(getattr(full, "pending_suggestions", None) or []),
    )


SPEC_SUGGESTION_DELETE = OperationSpec(
    id="chat.suggestion.delete",
    request=SuggestionDeleteReq,
    response=SuggestionResult,
    impl=dismiss_suggestion,
    summary="Dismiss a server-suggested admin action",
    description="The pending keys come from `chat get --full` (`channelFull.pending_suggestions`).",
    mutating=True,
    columns=("chat_id", "key", "pending_suggestions"),
    example={"chat_id": -1001500, "key": "CONVERT_GIGAGROUP", "pending_suggestions": []},
    example_args="chat suggestion delete @mygroup CONVERT_GIGAGROUP",
    covers=("groups-channels-admin.pending-suggestions",),
)


class SuggestedPostApproveReq(Request):
    channel: Annotated[PeerRef, arg(0, metavar="CHANNEL", kind="peer", help="The channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="The suggested post.")]
    at: Annotated[
        str | None, opt("--at", metavar="WHEN", help="Publish then instead of when proposed.")
    ] = None


async def approve_suggested_post(
    ctx: OpContext, req: SuggestedPostApproveReq
) -> SuggestedPostResult:
    """Approve a post suggested to a channel, optionally rescheduling it."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.channel)
    when = parse_dt(req.at) if req.at else None
    if req.at and when is None:
        raise UsageError("--at takes a timestamp", field="at")
    await _admin.client(ctx)(
        fn.ToggleSuggestedPostApprovalRequest(peer=peer, msg_id=req.msg_id, schedule_date=when)
    )
    return SuggestedPostResult(
        channel_id=peer_id_of(peer) or 0,
        msg_id=req.msg_id,
        approved=True,
        schedule_date=fmt_dt(when),
    )


SPEC_SUGGESTED_POST_APPROVE = OperationSpec(
    id="chat.suggested-post.approve",
    request=SuggestedPostApproveReq,
    response=SuggestedPostResult,
    impl=approve_suggested_post,
    summary="Approve a post suggested to a channel",
    description=(
        "`--at` doubles as the accept-and-reschedule counter-offer. "
        "Accepting a priced post debits the payer, not you."
    ),
    mutating=True,
    columns=("channel_id", "msg_id", "approved"),
    example={"channel_id": -1001600, "msg_id": 918, "approved": True},
    example_args="chat suggested-post approve @mychannel 918",
    covers_partial=("groups-channels-admin.suggested-post-approve",),
    coverage_note="Approving; `chat suggested-post deny` rejects and owns the id.",
    tags=frozenset({"visible-to-others"}),
)


class SuggestedPostDenyReq(Request):
    channel: Annotated[PeerRef, arg(0, metavar="CHANNEL", kind="peer", help="The channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="The suggested post.")]
    comment: Annotated[
        str, opt("--comment", metavar="TEXT", help="Reason sent back to the author.")
    ] = ""


async def deny_suggested_post(ctx: OpContext, req: SuggestedPostDenyReq) -> SuggestedPostResult:
    """Reject a suggested post, with a reason the author sees."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.channel)
    await _admin.client(ctx)(
        fn.ToggleSuggestedPostApprovalRequest(
            peer=peer, msg_id=req.msg_id, reject=True, reject_comment=req.comment or None
        )
    )
    return SuggestedPostResult(channel_id=peer_id_of(peer) or 0, msg_id=req.msg_id, rejected=True)


SPEC_SUGGESTED_POST_DENY = OperationSpec(
    id="chat.suggested-post.deny",
    request=SuggestedPostDenyReq,
    response=SuggestedPostResult,
    impl=deny_suggested_post,
    summary="Reject a post suggested to a channel",
    mutating=True,
    destructive=True,
    columns=("channel_id", "msg_id", "rejected"),
    example={"channel_id": -1001600, "msg_id": 918, "rejected": True},
    example_args="chat suggested-post deny @mychannel 918 --comment 'off topic' --yes",
    covers=("groups-channels-admin.suggested-post-approve",),
    tags=frozenset({"visible-to-others"}),
)


class VerificationSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The peer to badge.")]
    bot: Annotated[
        PeerRef | None,
        opt("--bot", metavar="BOT", kind="user", help="Your bot with verifier_settings."),
    ] = None
    description: Annotated[
        str, opt("--description", metavar="TEXT", help="Custom description for the badge.")
    ] = ""
    off: Annotated[bool, opt("--off", help="Remove the badge instead.")] = False


async def set_verification(ctx: OpContext, req: VerificationSetReq) -> VerificationResult:
    """Attach or remove a third-party (bot) verification badge.

    Not the blue Telegram check: `channel.verified` is server-assigned and
    cannot be set through the API at all.
    """
    from telethon.tl.functions import bots as fn

    peer = await _send.resolve(ctx, req.chat)
    bot = _admin.input_user(await _send.resolve(ctx, req.bot)) if req.bot is not None else None
    await _admin.client(ctx)(
        fn.SetCustomVerificationRequest(
            peer=peer,
            bot=bot,
            enabled=None if req.off else True,
            custom_description=req.description or None,
        )
    )
    return VerificationResult(
        chat_id=peer_id_of(peer) or 0,
        bot_id=abs(peer_id_of(await _send.resolve(ctx, req.bot)) or 0) if req.bot else None,
        enabled=not req.off,
    )


SPEC_VERIFICATION_SET = OperationSpec(
    id="chat.verification.set",
    request=VerificationSetReq,
    response=VerificationResult,
    impl=set_verification,
    summary="Attach or remove a third-party (bot) verification badge",
    description=(
        "This is a bot's badge, not Telegram's blue check — `channel.verified` "
        "is server-assigned and has no API to set."
    ),
    mutating=True,
    columns=("chat_id", "bot_id", "enabled"),
    example={"chat_id": -1001500, "bot_id": 8800, "enabled": True},
    example_args="chat verification set @mygroup --bot @myverifierbot",
    covers=("groups-channels-admin.verify-peer",),
)


# ---------------------------------------------------------------------------
# chat direct (the channel direct-messages monoforum)
# ---------------------------------------------------------------------------


async def _monoforum(ctx: OpContext, ref: PeerRef) -> tuple[Any, Any, int]:
    """`(channel peer, monoforum peer, monoforum id)` for a channel's DMs.

    Every `chat direct` verb acts on `channel.linked_monoforum_id`, not on
    the channel — banning somebody from a channel's DMs while banning them
    from the channel are different things, and conflating them would be a
    moderation action nobody asked for.
    """
    peer = await _send.resolve(ctx, ref)
    _full, entity, _entities = await _admin.full_chat(ctx, peer)
    linked = getattr(entity, "linked_monoforum_id", None)
    if not linked:
        raise NotFoundError(
            "this channel has no direct-messages conversation; turn it on with "
            "`tlgr chat setting set <channel> --direct-messages on`"
        )
    from tlgr.ops._serialize import marked_id

    monoforum = await _send.resolve(ctx, str(marked_id(int(linked), "channel")))
    return peer, monoforum, peer_id_of(monoforum) or 0


class DirectListReq(Request):
    channel: Annotated[PeerRef, arg(0, metavar="CHANNEL", kind="peer", help="The channel.")]


async def list_direct(ctx: OpContext, req: DirectListReq) -> Page[DirectDialog]:
    """Browse a channel's direct-message conversations."""
    from datetime import datetime, timezone

    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    limit, state = _admin.window(ctx, "chat.direct.list", PageKind.PARTICIPANTS)
    _peer, monoforum, _mono_id = await _monoforum(ctx, req.channel)
    offset_date = state.get("date")
    reply = await _admin.client(ctx)(
        fn.GetSavedDialogsRequest(
            parent_peer=monoforum,
            offset_date=datetime.fromtimestamp(offset_date, tz=timezone.utc)
            if offset_date
            else None,
            offset_id=int(state.get("id", 0) or 0),
            offset_peer=types.InputPeerEmpty(),
            limit=limit,
            hash=0,
        )
    )
    entities = _admin.entity_map(reply)
    rows: list[DirectDialog] = []
    for row in getattr(reply, "dialogs", None) or []:
        saved = peer_id_of(getattr(row, "peer", None)) or 0
        entity = entities.get(saved)
        rows.append(
            DirectDialog(
                saved_peer_id=saved,
                name=_admin.display_name(entity),
                top_message=int(getattr(row, "top_message", 0) or 0) or None,
                unread_count=int(getattr(row, "unread_count", 0) or 0),
                unread_reactions_count=int(getattr(row, "unread_reactions_count", 0) or 0),
                nopaid_messages_exception=getattr(row, "nopaid_messages_exception", None),
            )
        )
    next_state = {"id": rows[-1].top_message or 0} if rows else {}
    return build_page(
        rows,
        op="chat.direct.list",
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", len(rows)) or len(rows)),
    )


SPEC_DIRECT_LIST = OperationSpec(
    id="chat.direct.list",
    request=DirectListReq,
    response=Page[DirectDialog],
    impl=list_direct,
    summary="Browse a channel's direct-message conversations",
    description=(
        "Each conversation is a `monoForumDialog` keyed by `saved_peer_id` "
        "(the user). Reading and replying inside one is "
        "`message list/send --direct <user>`."
    ),
    aliases=("chat.monoforum.list",),
    paginated=PageKind.PARTICIPANTS,
    columns=("saved_peer_id", "name", "unread_count"),
    example={"items": [{"saved_peer_id": 4242, "name": "Alice", "unread_count": 1}]},
    example_args="chat direct list @mychannel",
    covers=("groups-channels-admin.monoforum-topic-list",),
)


class DirectBanReq(Request):
    channel: Annotated[PeerRef, arg(0, metavar="CHANNEL", kind="peer", help="The channel.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Who to ban from the DMs.")]


async def ban_direct(ctx: OpContext, req: DirectBanReq) -> DirectBanResult:
    """Ban a user from a channel's direct messages, not from the channel."""
    from telethon.tl.functions import channels as fn

    peer, monoforum, mono_id = await _monoforum(ctx, req.channel)
    user = await _send.resolve(ctx, req.user)
    await _admin.client(ctx)(
        fn.EditBannedRequest(
            channel=_admin.input_channel(monoforum),
            participant=user,
            banned_rights=_rights.build_banned_rights([]),
        )
    )
    return DirectBanResult(
        channel_id=peer_id_of(peer) or 0,
        monoforum_id=mono_id,
        user_id=abs(peer_id_of(user) or 0),
        banned=True,
    )


SPEC_DIRECT_BAN = OperationSpec(
    id="chat.direct.ban",
    request=DirectBanReq,
    response=DirectBanResult,
    impl=ban_direct,
    summary="Ban a user from a channel's direct messages",
    description=(
        "Acts on `channel.linked_monoforum_id`, not on the channel: banning "
        "somebody from your DMs and banning them from your channel are "
        "different decisions."
    ),
    aliases=("chat.monoforum.ban",),
    mutating=True,
    destructive=True,
    columns=("channel_id", "user_id", "banned"),
    example={"channel_id": -1001600, "monoforum_id": -1001700, "user_id": 4242, "banned": True},
    example_args="chat direct ban @mychannel @spammer --yes",
    covers_partial=("groups-channels-admin.monoforum-ban",),
    coverage_note="Banning; `chat direct unban` lifts it and owns the id.",
)


async def unban_direct(ctx: OpContext, req: DirectBanReq) -> DirectBanResult:
    """Lift a direct-messages ban."""
    from telethon.tl.functions import channels as fn

    peer, monoforum, mono_id = await _monoforum(ctx, req.channel)
    user = await _send.resolve(ctx, req.user)
    await _admin.client(ctx)(
        fn.EditBannedRequest(
            channel=_admin.input_channel(monoforum),
            participant=user,
            banned_rights=_rights.build_banned_rights(_rights.all_allowed()),
        )
    )
    return DirectBanResult(
        channel_id=peer_id_of(peer) or 0,
        monoforum_id=mono_id,
        user_id=abs(peer_id_of(user) or 0),
        banned=False,
    )


SPEC_DIRECT_UNBAN = OperationSpec(
    id="chat.direct.unban",
    request=DirectBanReq,
    response=DirectBanResult,
    impl=unban_direct,
    summary="Unban a user from a channel's direct messages",
    description=(
        "`chat member edit --free-messages on` waives the Stars price for "
        "one user without touching the ban."
    ),
    aliases=("chat.monoforum.unban",),
    mutating=True,
    columns=("channel_id", "user_id", "banned"),
    example={"channel_id": -1001600, "monoforum_id": -1001700, "user_id": 4242, "banned": False},
    example_args="chat direct unban @mychannel @alice",
    covers=("groups-channels-admin.monoforum-ban",),
)


# ---------------------------------------------------------------------------
# chat affiliate
# ---------------------------------------------------------------------------


class AffiliateListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The channel.")]
    suggested: Annotated[
        bool, opt("--suggested", help="Suggested affiliate bots instead of the connected ones.")
    ] = False


async def list_affiliates(ctx: OpContext, req: AffiliateListReq) -> Page[AffiliateBot]:
    """Affiliate (Stars referral) bots connected to a channel, or suggested."""
    from datetime import datetime, timezone

    from telethon.tl.functions import payments as fn

    limit, state = _admin.window(ctx, "chat.affiliate.list", PageKind.PARTICIPANTS)
    peer = await _send.resolve(ctx, req.chat)
    handle = _admin.client(ctx)
    rows: list[AffiliateBot] = []
    next_state: dict[str, Any] = {}
    if req.suggested:
        reply = await handle(
            fn.GetSuggestedStarRefBotsRequest(
                peer=peer, offset=str(state.get("offset", "") or ""), limit=limit
            )
        )
        for row in getattr(reply, "suggested_bots", None) or []:
            rows.append(
                AffiliateBot(
                    bot_id=int(getattr(row, "bot_id", 0) or 0),
                    commission_permille=int(getattr(row, "commission_permille", 0) or 0),
                    duration_months=getattr(row, "duration_months", None),
                )
            )
        next_state = {"offset": str(getattr(reply, "next_offset", "") or "")}
    else:
        offset_date = state.get("date")
        reply = await handle(
            fn.GetConnectedStarRefBotsRequest(
                peer=peer,
                limit=limit,
                offset_date=datetime.fromtimestamp(offset_date, tz=timezone.utc)
                if offset_date
                else None,
                offset_link=state.get("link") or None,
            )
        )
        for row in getattr(reply, "connected_bots", None) or []:
            date = getattr(row, "date", None)
            rows.append(
                AffiliateBot(
                    bot_id=int(getattr(row, "bot_id", 0) or 0),
                    url=str(getattr(row, "url", "") or ""),
                    commission_permille=int(getattr(row, "commission_permille", 0) or 0),
                    duration_months=getattr(row, "duration_months", None),
                    participants=getattr(row, "participants", None),
                    revenue=getattr(row, "revenue", None),
                    date=fmt_dt(date),
                    date_unix=to_unix(date),
                )
            )
        if rows:
            next_state = {"date": rows[-1].date_unix or 0, "link": rows[-1].url}
    return build_page(
        rows,
        op="chat.affiliate.list",
        kind=PageKind.PARTICIPANTS,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=int(getattr(reply, "count", len(rows)) or len(rows)),
    )


SPEC_AFFILIATE_LIST = OperationSpec(
    id="chat.affiliate.list",
    request=AffiliateListReq,
    response=Page[AffiliateBot],
    impl=list_affiliates,
    summary="Affiliate (Star referral) bots connected to a channel, plus suggestions",
    paginated=PageKind.PARTICIPANTS,
    columns=("bot_id", "commission_permille", "participants", "revenue"),
    example={"items": [{"bot_id": 8800, "commission_permille": 200}], "has_more": False},
    example_args="chat affiliate list @mychannel",
    covers_partial=("groups-channels-admin.affiliate-program",),
    coverage_note="Listing; `chat affiliate set` connects one and owns the id.",
)


class AffiliateSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The channel.")]
    bot: Annotated[PeerRef, arg(1, metavar="BOT", kind="user", help="The affiliate bot.")]
    off: Annotated[bool, opt("--off", help="Revoke the affiliate link instead.")] = False


async def set_affiliate(ctx: OpContext, req: AffiliateSetReq) -> AffiliateResult:
    """Connect or revoke an affiliate bot for a channel."""
    from telethon.tl.functions import payments as fn

    peer = await _send.resolve(ctx, req.chat)
    bot = await _send.resolve(ctx, req.bot)
    handle = _admin.client(ctx)
    if req.off:
        connected = await list_affiliates(ctx, AffiliateListReq(chat=req.chat))
        bot_id = abs(peer_id_of(bot) or 0)
        link = next((row.url for row in connected.items if row.bot_id == bot_id), "")
        if not link:
            raise NotFoundError("that bot is not connected to this channel")
        reply = await handle(fn.EditConnectedStarRefBotRequest(peer=peer, link=link, revoked=True))
    else:
        reply = await handle(fn.ConnectStarRefBotRequest(peer=peer, bot=_admin.input_user(bot)))
    connected_bots = list(getattr(reply, "connected_bots", None) or [])
    row = connected_bots[0] if connected_bots else None
    return AffiliateResult(
        chat_id=peer_id_of(peer) or 0,
        bot_id=abs(peer_id_of(bot) or 0),
        url=str(getattr(row, "url", "") or ""),
        commission_permille=int(getattr(row, "commission_permille", 0) or 0),
        revoked=req.off,
    )


SPEC_AFFILIATE_SET = OperationSpec(
    id="chat.affiliate.set",
    request=AffiliateSetReq,
    response=AffiliateResult,
    impl=set_affiliate,
    summary="Connect or disconnect an affiliate bot for a channel",
    description="Connecting is free; the Stars commission comes out of the bot's revenue.",
    mutating=True,
    columns=("chat_id", "bot_id", "url", "revoked"),
    example={"chat_id": -1001600, "bot_id": 8800, "url": "https://t.me/mybot?start=ref"},
    example_args="chat affiliate set @mychannel @refbot",
    covers=("groups-channels-admin.affiliate-program",),
)


# ---------------------------------------------------------------------------
# chat community — layer 229, registered and refusing
# ---------------------------------------------------------------------------


class CommunityCreateReq(Request):
    title: Annotated[str, arg(0, metavar="TITLE", help="The community's title.")]
    about: Annotated[str, opt("--about", metavar="TEXT", help="Description.")] = ""
    hidden: Annotated[bool, opt("--hidden", help="Do not show the community publicly.")] = False
    peer: Annotated[
        list[PeerRef], opt("--peer", metavar="CHAT", kind="peer", help="Seed it with this chat.")
    ] = []


async def create_community(ctx: OpContext, req: CommunityCreateReq) -> CommunityResult:
    """Reserved: `communities.create` is layer 229 and Telethon speaks 227."""
    _layer.community_gap("chat community create", "communities.create")
    raise AssertionError  # pragma: no cover - community_gap always raises


SPEC_COMMUNITY_CREATE = OperationSpec(
    id="chat.community.create",
    request=CommunityCreateReq,
    response=CommunityResult,
    impl=create_community,
    summary="Create a Community (a hub grouping several chats)",
    description=(
        "Registered and refusing with NOT_SUPPORTED (exit 13): the whole "
        "Community surface arrived in MTProto layer 229 and Telethon 1.44 "
        "speaks 227, so there is no request class to send. The command shape "
        "is settled and will start working with the layer uplift."
    ),
    mutating=True,
    columns=("community_id", "title"),
    example={"community_id": 0, "title": "Release hub"},
    example_args="chat community create 'Release hub'",
    covers=("groups-channels-admin.community-create",),
    tags=frozenset({"layer-gap"}),
)


class CommunityListReq(Request):
    community: Annotated[
        PeerRef | None,
        arg(0, metavar="COMMUNITY", kind="peer", required=False, help="One community."),
    ] = None
    requests: Annotated[bool, opt("--requests", help="Pending link requests instead of chats.")] = (
        False
    )
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Which of its chats this member joined."),
    ] = None
    collapse: Annotated[
        str | None, opt("--collapse", metavar="ID=ON|OFF", help="Collapse it in the chat list.")
    ] = None
    mute: Annotated[
        str | None, opt("--mute", metavar="ID=WHEN", help="Community-wide notify settings.")
    ] = None


async def list_communities(ctx: OpContext, req: CommunityListReq) -> Page[CommunityRow]:
    """Reserved: `communities.getJoinedCommunities` is layer 229."""
    _layer.community_gap(
        "chat community list",
        "communities.getJoinedCommunities / getPeerLinkRequests / getParticipantJoinedChats",
    )
    raise AssertionError  # pragma: no cover - community_gap always raises


SPEC_COMMUNITY_LIST = OperationSpec(
    id="chat.community.list",
    request=CommunityListReq,
    response=Page[CommunityRow],
    impl=list_communities,
    summary="List my communities, a community's chats, or its pending link requests",
    description="Registered and refusing with NOT_SUPPORTED (exit 13): layer 229.",
    paginated=PageKind.PARTICIPANTS,
    columns=("id", "title", "type"),
    example={"items": [], "has_more": False},
    example_args="chat community list",
    covers=("dialogs.community-collapse", "dialogs.notify-community"),
    covers_partial=(
        "groups-channels-admin.community-link-requests",
        "groups-channels-admin.community-manage-links",
    ),
    coverage_note=(
        "The whole Community surface is layer-229 and refuses with a reason; "
        "`chat community set` and `chat community ban` own the two ids."
    ),
    tags=frozenset({"layer-gap"}),
)


class CommunitySetReq(Request):
    community: Annotated[PeerRef, arg(0, metavar="COMMUNITY", kind="peer", help="The community.")]
    chat: Annotated[PeerRef, arg(1, metavar="CHAT", kind="peer", help="The chat to link.")]
    state: Annotated[
        str | None,
        arg(2, metavar="STATE", required=False, help="visible | hidden | removed."),
    ] = None
    approve: Annotated[bool, opt("--approve", help="Approve this chat's link request.")] = False
    deny: Annotated[bool, opt("--deny", help="Reject this chat's link request.")] = False
    everything: Annotated[bool, opt("--all", help="Answer every pending request.")] = False
    collapsed: Annotated[
        str | None, opt("--collapsed", metavar="ON|OFF", help="Collapse it in my dialog list.")
    ] = None


async def set_community(ctx: OpContext, req: CommunitySetReq) -> CommunityResult:
    """Reserved: `communities.togglePeerLink` is layer 229."""
    _layer.community_gap(
        "chat community set",
        "communities.togglePeerLink / togglePeerLinkRequestApproval / "
        "toggleAllPeerLinkRequestApproval / toggleCommunityCollapsedInDialogs",
    )
    raise AssertionError  # pragma: no cover - community_gap always raises


SPEC_COMMUNITY_SET = OperationSpec(
    id="chat.community.set",
    request=CommunitySetReq,
    response=CommunityResult,
    impl=set_community,
    summary="Add, hide, remove a chat in a community, or answer a link request",
    description="Registered and refusing with NOT_SUPPORTED (exit 13): layer 229.",
    mutating=True,
    columns=("community_id", "chat_id", "state"),
    example={"community_id": 0, "chat_id": -1001500, "state": "visible"},
    example_args="chat community set @myhub @mygroup visible",
    covers=("groups-channels-admin.community-manage-links",),
    covers_partial=("groups-channels-admin.community-link-requests",),
    coverage_note="Layer-229 surface; `chat community ban` owns the link-requests id.",
    tags=frozenset({"layer-gap"}),
)


class CommunityBanReq(Request):
    community: Annotated[PeerRef, arg(0, metavar="COMMUNITY", kind="peer", help="The community.")]
    user: Annotated[PeerRef, arg(1, metavar="USER", kind="user", help="Who to ban.")]
    off: Annotated[bool, opt("--off", help="Unban instead.")] = False


async def ban_community(ctx: OpContext, req: CommunityBanReq) -> CommunityResult:
    """Reserved: `communities.toggleParticipantBanned` is layer 229."""
    _layer.community_gap("chat community ban", "communities.toggleParticipantBanned")
    raise AssertionError  # pragma: no cover - community_gap always raises


SPEC_COMMUNITY_BAN = OperationSpec(
    id="chat.community.ban",
    request=CommunityBanReq,
    response=CommunityResult,
    impl=ban_community,
    summary="Ban a member from a whole community",
    description="Registered and refusing with NOT_SUPPORTED (exit 13): layer 229.",
    mutating=True,
    destructive=True,
    columns=("community_id", "user_id", "banned"),
    example={"community_id": 0, "user_id": 4242, "banned": True},
    example_args="chat community ban @myhub @spammer --yes",
    covers=("groups-channels-admin.community-link-requests",),
    tags=frozenset({"layer-gap"}),
)


# ---------------------------------------------------------------------------
# chat welcome — layer 229, registered and refusing
# ---------------------------------------------------------------------------


class WelcomeListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]


async def list_welcome(ctx: OpContext, req: WelcomeListReq) -> list[WelcomeMessage]:
    """Reserved: `ephemeral.getWelcomeMessages` is layer 229."""
    _layer.welcome_gap("chat welcome list", "ephemeral.getWelcomeMessages")
    raise AssertionError  # pragma: no cover - welcome_gap always raises


SPEC_WELCOME_LIST = OperationSpec(
    id="chat.welcome.list",
    request=WelcomeListReq,
    response=list[WelcomeMessage],
    impl=list_welcome,
    summary="List a group/channel's welcome messages",
    description=(
        "Registered and refusing with NOT_SUPPORTED (exit 13). "
        "`chatFull.has_welcome_messages` advertises them, but the "
        "`ephemeral.*` methods that read them are layer 229 and Telethon "
        "1.44 speaks 227."
    ),
    columns=("id", "text"),
    example=[{"id": 1, "text": "Welcome!"}],
    example_args="chat welcome list @mygroup",
    covers=("messages-core.chat-welcome-messages",),
    covers_partial=("groups-channels-admin.welcome-messages",),
    coverage_note="Layer-229 surface; `chat welcome set` owns the writing id.",
    tags=frozenset({"layer-gap"}),
)


class WelcomeSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    text: Annotated[str | None, arg(1, metavar="TEXT", required=False, help="The message.")] = None
    id: Annotated[
        int | None, opt("--id", metavar="ID", help="Edit this welcome message instead of adding.")
    ] = None
    file: Annotated[
        list[str], opt("--file", metavar="PATH", kind="path", help="Attach media.")
    ] = []
    parse: Annotated[
        str | None, opt("--parse", metavar="MODE", help="Markup of the text: md, html or none.")
    ] = None


async def set_welcome(ctx: OpContext, req: WelcomeSetReq) -> WelcomeResult:
    """Reserved: `ephemeral.sendMessage(welcome=true)` is layer 229."""
    _layer.welcome_gap("chat welcome set", "ephemeral.sendMessage / ephemeral.editMessage")
    raise AssertionError  # pragma: no cover - welcome_gap always raises


SPEC_WELCOME_SET = OperationSpec(
    id="chat.welcome.set",
    request=WelcomeSetReq,
    response=WelcomeResult,
    impl=set_welcome,
    summary="Add or edit a welcome message",
    description="Registered and refusing with NOT_SUPPORTED (exit 13): layer 229.",
    mutating=True,
    columns=("chat_id", "id", "text"),
    example={"chat_id": -1001500, "id": 1, "text": "Welcome!"},
    example_args="chat welcome set @mygroup 'Welcome aboard'",
    covers=("groups-channels-admin.welcome-messages",),
    tags=frozenset({"layer-gap"}),
)


class WelcomeDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    id: Annotated[
        int | None, arg(1, metavar="ID", required=False, help="Which welcome message.")
    ] = None
    everything: Annotated[bool, opt("--all", help="Delete every welcome message.")] = False


async def delete_welcome(ctx: OpContext, req: WelcomeDeleteReq) -> WelcomeResult:
    """Reserved: `ephemeral.deleteWelcomeMessage` is layer 229."""
    _layer.welcome_gap(
        "chat welcome delete",
        "ephemeral.deleteWelcomeMessage / ephemeral.deleteAllWelcomeMessages",
    )
    raise AssertionError  # pragma: no cover - welcome_gap always raises


SPEC_WELCOME_DELETE = OperationSpec(
    id="chat.welcome.delete",
    request=WelcomeDeleteReq,
    response=WelcomeResult,
    impl=delete_welcome,
    summary="Delete one or all welcome messages",
    description="Registered and refusing with NOT_SUPPORTED (exit 13): layer 229.",
    mutating=True,
    destructive=True,
    columns=("chat_id", "deleted"),
    example={"chat_id": -1001500, "deleted": 1},
    example_args="chat welcome delete @mygroup --all --yes",
    covers_partial=("groups-channels-admin.welcome-messages",),
    coverage_note="Layer-229 surface; `chat welcome set` owns the id.",
    tags=frozenset({"layer-gap"}),
)
