"""The `bot` group: talking to bots, and running the ones you own.

Two audiences share one noun, and the split matters because it decides which
account can run a command at all.

* **As a user.** `bot get`, `bot start`, `bot command send`, `bot press`,
  `bot permission set`, `bot url-auth get` — everything a person does to a bot
  from a Telegram client. These run on an ordinary account.
* **As the bot.** `bot answer`, `bot command set`, `bot menu set`,
  `bot api send` — the Bot-API surface, which Telegram serves only to a
  session created from a bot token. On a user session they exit 4 with a
  sentence saying how to add one, rather than surfacing Telegram's own
  `BOT_METHOD_INVALID`.

`bot press` is the centre of the group. Telegram has fourteen kinds of button
and one of them (`buy`) starts a payment, four of them disclose personal data,
and two need a layer this build does not speak. One dispatcher handles them
all: it names what it is about to send, refuses the ones that would leak
without their consent flag, refuses `buy` outright, and returns a typed answer
saying which kind actually came back.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import (
    NotFoundError,
    PermissionError_,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.bot import (
    AttachMenuBot,
    BotAccess,
    BotAnswer,
    BotApiResult,
    BotCommand,
    BotCommandSet,
    BotCreated,
    BotEdited,
    BotIds,
    BotInfo,
    BotPermission,
    BotQuery,
    BotRef,
    BotStarted,
    BotStopped,
    BotToken,
    BotUsernameCheck,
    BotUsernames,
    BotVerification,
    BotVerified,
    BotWelcomeMessage,
    BusinessConnection,
    CommandSent,
    DefaultRights,
    EmojiGame,
    EphemeralDeleted,
    EphemeralSent,
    GameSent,
    HighScore,
    MenuButton,
    Pressed,
    PreviewChange,
    PreviewMedia,
    RecentBots,
    ReportOutcome,
    ScoreSet,
    SponsoredRead,
    StarRefProgram,
    StreamProgress,
    ToggledAttachMenu,
    UrlAuth,
    WelcomeDeleted,
    WelcomeSet,
)
from tlgr.models.message import SponsoredMessage
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _bots, _media, _send
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: Reported as the client platform on every mini-app request. Telegram uses
#: it to pick the app's own layout; there is no "cli" value it understands.
PLATFORM = "web"

_EXAMPLE_BOT: dict[str, Any] = {
    "id": 93372553,
    "username": "gif",
    "first_name": "GIF",
    "about": "Send GIFs inline",
    "bot_can_edit": False,
}


async def _full(ctx: OpContext, peer: Any) -> tuple[Any, Any]:
    """`(userFull, user)` for a bot, in one round trip."""
    from telethon import utils
    from telethon.tl.functions import users as fn

    result = await client(ctx)(fn.GetFullUserRequest(id=utils.get_input_user(peer)))
    full = getattr(result, "full_user", None)
    users = {int(getattr(u, "id", 0)): u for u in (getattr(result, "users", None) or [])}
    user = users.get(int(getattr(full, "id", 0) or 0)) if full is not None else None
    return full, user


def _usernames(user: Any) -> list[str]:
    names = [
        str(getattr(entry, "username", "") or "")
        for entry in (getattr(user, "usernames", None) or [])
        if getattr(entry, "username", None)
    ]
    primary = getattr(user, "username", None)
    if primary and primary not in names:
        names.insert(0, str(primary))
    return names


def _menu_button(button: Any, *, user_id: int | None = None) -> MenuButton | None:
    """`botMenuButton*` as the model.

    `botMenuButtonDefault` never reaches a user — the server substitutes the
    commands list — so it is normalised to `commands` rather than leaking a
    third state nobody can act on.
    """
    if button is None:
        return None
    name = type(button).__name__
    if name == "BotMenuButton":
        return MenuButton(
            kind="webapp",
            text=getattr(button, "text", None),
            url=getattr(button, "url", None),
            user_id=user_id,
        )
    return MenuButton(kind="commands", user_id=user_id)


def _commands(
    raw: Any, *, bot_id: int, scope: str | None = None, lang: str | None = None
) -> list[BotCommand]:
    entries = list(
        (getattr(raw, "commands", None) or []) if hasattr(raw, "commands") else (raw or [])
    )
    names = {str(getattr(c, "command", "") or "") for c in entries}
    return [
        BotCommand(
            bot_id=bot_id,
            command=str(getattr(entry, "command", "") or ""),
            description=str(getattr(entry, "description", "") or ""),
            ephemeral=bool(getattr(entry, "ephemeral", False)),
            scope=scope,
            lang=lang,
            has_help="help" in names,
            has_settings="settings" in names,
        )
        for entry in entries
    ]


def _starref(program: Any) -> StarRefProgram | None:
    if program is None:
        return None
    end = getattr(program, "end_date", None)
    revenue = getattr(program, "daily_revenue_per_user", None)
    return StarRefProgram(
        bot_id=int(getattr(program, "bot_id", 0) or 0),
        url=getattr(program, "url", None),
        commission_permille=int(getattr(program, "commission_permille", 0) or 0),
        duration_months=getattr(program, "duration_months", None),
        end_date=fmt_dt(end),
        end_date_unix=to_unix(end),
        participants=getattr(program, "participants", None),
        revenue=int(getattr(revenue, "amount", 0) or 0) if revenue is not None else None,
        revoked=bool(getattr(program, "revoked", False)),
    )


def _verification(full: Any, user: Any) -> BotVerification | None:
    badge = getattr(full, "bot_verification", None)
    verified = bool(getattr(user, "verified", False))
    if badge is None and not verified:
        return None
    return BotVerification(
        verified_by_bot=int(getattr(badge, "bot_id", 0) or 0) or None,
        description=getattr(badge, "description", None),
        icon=getattr(badge, "icon", None),
        telegram_verified=verified,
    )


def _access(settings: Any) -> BotAccess:
    """`bots.accessSettings` as the model. The allow-list is `add_users`."""
    return BotAccess(
        restricted=bool(getattr(settings, "restricted", False)),
        allowed_users=[
            int(getattr(u, "id", 0) or getattr(u, "user_id", 0) or 0)
            for u in (getattr(settings, "add_users", None) or [])
        ],
        allowed_chats=[
            int(getattr(c, "id", 0) or 0) for c in (getattr(settings, "add_chats", None) or [])
        ],
    )


#: `businessBotRights` is its own flag set, not `chatAdminRights`; the fields
#: are listed rather than scanned so a new one cannot appear as a right the
#: bot silently already has.
BUSINESS_RIGHTS: tuple[str, ...] = (
    "reply",
    "read_messages",
    "delete_sent_messages",
    "delete_received_messages",
    "edit_name",
    "edit_bio",
    "edit_profile_photo",
    "edit_username",
    "view_gifts",
    "sell_gifts",
    "change_gift_settings",
    "transfer_and_upgrade_gifts",
    "transfer_stars",
    "manage_stories",
)


def _business_rights(rights: Any) -> list[str]:
    if rights is None:
        return []
    return [name for name in BUSINESS_RIGHTS if bool(getattr(rights, name, False))]


# ---------------------------------------------------------------------------
# bot get
# ---------------------------------------------------------------------------


class GetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="@username, id or t.me link.")]
    lang: Annotated[
        str | None, opt("--lang", metavar="CODE", help="Localized description (owner view).")
    ] = None
    access: Annotated[bool, opt("--access", help="Also fetch managed-bot access settings.")] = False
    refresh: Annotated[
        bool, opt("--refresh", help="Re-resolve the username instead of trusting the cache.")
    ] = False


async def get(ctx: OpContext, req: GetReq) -> BotInfo:
    """A bot's whole profile card.

    `bot_info_version` is the only invalidation signal Telegram gives for the
    commands and the description, so it is reported: a caller that caches this
    card has no other way to know when to refetch it.
    """
    from telethon.tl.functions import bots as bots_fn

    peer = await _resolve_bot(ctx, req.bot, refresh=req.refresh)
    full, user = await _full(ctx, peer)
    info = getattr(full, "bot_info", None)
    bot_id = int(getattr(full, "id", 0) or 0)

    about = getattr(full, "about", None)
    description = getattr(info, "description", None)
    if req.lang:
        localized = await client(ctx)(
            bots_fn.GetBotInfoRequest(lang_code=req.lang, bot=await _bots.input_user(ctx, req.bot))
        )
        about = getattr(localized, "about", about)
        description = getattr(localized, "description", description)

    settings = getattr(info, "app_settings", None)
    verifier = getattr(info, "verifier_settings", None)
    card = BotInfo(
        id=bot_id,
        username=getattr(user, "username", None),
        usernames=_usernames(user),
        first_name=getattr(user, "first_name", None),
        about=about,
        description=description,
        description_photo=_id_of(getattr(info, "description_photo", None)),
        description_document=_id_of(getattr(info, "description_document", None)),
        privacy_policy_url=getattr(info, "privacy_policy_url", None),
        commands=_commands(info, bot_id=bot_id, lang=req.lang),
        menu_button=_menu_button(getattr(info, "menu_button", None)),
        app_settings=_app_settings(settings),
        verifier_settings=(
            {
                "icon": getattr(verifier, "icon", None),
                "company": getattr(verifier, "company", None),
                "can_modify_custom_description": bool(
                    getattr(verifier, "can_modify_custom_description", False)
                ),
                "custom_description": getattr(verifier, "custom_description", None),
            }
            if verifier is not None
            else None
        ),
        bot_verification=_verification(full, user),
        bot_info_version=getattr(user, "bot_info_version", None),
        bot_active_users=getattr(user, "bot_active_users", None),
        bot_can_edit=bool(getattr(user, "bot_can_edit", False)),
        bot_has_main_app=bool(getattr(user, "bot_has_main_app", False)),
        bot_nochats=bool(getattr(user, "bot_nochats", False)),
        bot_business=bool(getattr(user, "bot_business", False)),
        bot_attach_menu=bool(getattr(user, "bot_attach_menu", False)),
        bot_inline_geo=bool(getattr(user, "bot_inline_geo", False)),
        inline_placeholder=getattr(user, "bot_inline_placeholder", None),
        bot_group_admin_rights=_bots.rights_keywords(getattr(full, "bot_group_admin_rights", None)),
        bot_broadcast_admin_rights=_bots.rights_keywords(
            getattr(full, "bot_broadcast_admin_rights", None)
        ),
        has_preview_medias=bool(getattr(info, "has_preview_medias", False)),
        starref_program=_starref(getattr(full, "starref_program", None)),
        blocked=bool(getattr(full, "blocked", False)),
        lang=req.lang,
    )
    if req.access:
        if not card.bot_can_edit:
            raise PermissionError_("access settings are only readable on a bot you administer")
        settings = await client(ctx)(
            bots_fn.GetAccessSettingsRequest(bot=await _bots.input_user(ctx, req.bot))
        )
        card.access = _access(settings)
    return card


def _id_of(value: Any) -> int | None:
    identifier = getattr(value, "id", None)
    return int(identifier) if isinstance(identifier, int) else None


def _app_settings(settings: Any) -> dict[str, Any] | None:
    if settings is None:
        return None
    path = getattr(settings, "placeholder_path", None)
    return {
        "placeholder_path": len(path) if path else None,
        "bg_color": getattr(settings, "background_color", None),
        "bg_dark_color": getattr(settings, "background_dark_color", None),
        "header_color": getattr(settings, "header_color", None),
        "header_dark_color": getattr(settings, "header_dark_color", None),
    }


async def _resolve_bot(ctx: OpContext, ref: PeerRef, *, refresh: bool = False) -> Any:
    """The bot's `InputPeer`, optionally re-resolving the username first.

    `contacts.resolveUsername` is what mints the access hash, and a cached one
    can be stale after a bot changes hands; `--refresh` is the escape hatch
    that does not require deleting the peer cache by hand.
    """
    if refresh and ref.kind == "username":
        from telethon.tl.functions import contacts as fn

        await client(ctx)(fn.ResolveUsernameRequest(username=str(ref.value)))
    return await _send.resolve(ctx, ref)


SPEC_GET = OperationSpec(
    id="bot.get",
    request=GetReq,
    response=BotInfo,
    impl=get,
    summary="Show a bot's profile card",
    description=(
        "Description, about text, commands, menu button, privacy policy, "
        "capability flags, verification badge and mini-app settings, from the "
        "one `users.getFullUser` that carries all of them."
    ),
    aliases=("bot.info",),
    columns=("id", "username", "first_name", "bot_active_users"),
    headers=("ID", "Username", "Name", "Users"),
    example=_EXAMPLE_BOT,
    example_args="bot get @gifbot",
    covers=(
        "bots.bot-info-card",
        "bots.bot-privacy-policy",
        "bots.bot-profile-flags",
        "bots.resolve-bot",
    ),
    covers_partial=(
        "bots.bot-verification-view",
        "bots.menu-button-state",
        "bots.suggested-admin-rights",
        "bots.webapp-placeholder-and-close",
    ),
    coverage_note=(
        "The card shows the menu button, the suggested admin rights, the "
        "verification badge and the mini-app placeholder; setting them is "
        "`bot menu set`, `bot default-rights set`, `bot verification set` and "
        "`webapp get`."
    ),
)


# ---------------------------------------------------------------------------
# bot list
# ---------------------------------------------------------------------------


class ListReq(Request):
    owned: Annotated[bool, opt("--owned", help="Bots I own or administer (default).")] = True
    similar_to: Annotated[
        PeerRef | None,
        opt("--similar-to", metavar="BOT", kind="user", help="Bots recommended next to this bot."),
    ] = None
    popular_apps: Annotated[bool, opt("--popular-apps", help="The Mini App store list.")] = False
    recent: Annotated[bool, opt("--recent", help="Frequently-used bots (top peers).")] = False
    kind: Annotated[
        str, choice("pm", "inline", "app", "guest", help="Top-peer category for --recent.")
    ] = "pm"


_TOP_PEER_FLAGS = {
    "pm": "bots_pm",
    "inline": "bots_inline",
    "app": "bots_app",
    "guest": "bots_guestchat",
}


async def list_bots(ctx: OpContext, req: ListReq) -> Page[BotRef]:
    """Bots, from whichever of the four listings the flags name.

    They share a command because they answer one question — "which bots?" —
    and differ only in where the answer comes from.
    """
    from telethon.tl.functions import bots as fn
    from telethon.tl.functions import contacts as contacts_fn

    limit, state = window(ctx, "bot.list", PageKind.RATE, default=50)
    handle = client(ctx)

    if req.similar_to is not None:
        result = await handle(
            fn.GetBotRecommendationsRequest(bot=await _bots.input_user(ctx, req.similar_to))
        )
        truncated = getattr(result, "count", None)
        items = [
            _bot_ref(user, kind="similar", truncated=truncated)
            for user in (getattr(result, "users", None) or [])
        ]
        return build_page(items[:limit], op="bot.list", kind=PageKind.RATE, has_more=False)

    if req.popular_apps:
        offset = str(state.get("offset", "") or "")
        result = await handle(fn.GetPopularAppBotsRequest(offset=offset, limit=limit))
        items = [_bot_ref(user, kind="app") for user in (getattr(result, "users", None) or [])]
        next_offset = str(getattr(result, "next_offset", "") or "")
        return build_page(
            items,
            op="bot.list",
            kind=PageKind.RATE,
            state={"offset": next_offset},
            account=ctx.account,
            has_more=bool(next_offset),
        )

    if req.recent:
        flag = _TOP_PEER_FLAGS[req.kind]
        result = await handle(
            contacts_fn.GetTopPeersRequest(offset=0, limit=limit, hash=0, **{flag: True})
        )
        if type(result).__name__ == "TopPeersDisabled":
            ctx.warn("frequently-used suggestions are switched off for this account")
            return Page(items=[], has_more=False, total=0)
        users = {int(getattr(u, "id", 0)): u for u in (getattr(result, "users", None) or [])}
        items = []
        for category in getattr(result, "categories", None) or []:
            for entry in getattr(category, "peers", None) or []:
                user = users.get(int(getattr(getattr(entry, "peer", None), "user_id", 0) or 0))
                if user is not None:
                    items.append(
                        _bot_ref(user, kind=req.kind, rating=getattr(entry, "rating", None))
                    )
        return build_page(items[:limit], op="bot.list", kind=PageKind.RATE, has_more=False)

    result = await handle(fn.GetAdminedBotsRequest())
    items = [_bot_ref(user, kind="owned") for user in (result or [])]
    return build_page(items[:limit], op="bot.list", kind=PageKind.RATE, has_more=False)


def _bot_ref(
    user: Any, *, kind: str, truncated: int | None = None, rating: float | None = None
) -> BotRef:
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    return BotRef(
        id=int(getattr(user, "id", 0) or 0),
        username=getattr(user, "username", None),
        title=f"{first} {last}".strip() or None,
        kind=kind,
        active_users=getattr(user, "bot_active_users", None),
        truncated_count=truncated,
        rating=float(rating) if rating is not None else None,
    )


SPEC_LIST = OperationSpec(
    id="bot.list",
    request=ListReq,
    response=Page[BotRef],
    impl=list_bots,
    summary="List bots I own, similar bots, popular mini apps or my recent bots",
    description=(
        "A non-Premium account gets a shortened `--similar-to` list plus the "
        "real count, which is reported as `truncated_count` rather than "
        "silently looking like the whole answer."
    ),
    aliases=("bot.mine",),
    paginated=PageKind.RATE,
    columns=("id", "username", "title", "kind"),
    headers=("ID", "Username", "Title", "Kind"),
    example={"items": [{"id": 93372553, "username": "gif", "kind": "owned"}], "has_more": False},
    example_args="bot list --owned",
    covers=(
        "bots.guest-mode-invoke",
        "bots.list-owned-bots",
        "bots.popular-app-bots",
        "bots.similar-bots",
    ),
    covers_partial=("bots.top-peers-bots",),
    coverage_note="Turning the frequently-used list on or off is `bot recent set`.",
)


# ---------------------------------------------------------------------------
# bot id get
# ---------------------------------------------------------------------------


class IdReq(Request):
    chat: Annotated[
        PeerRef, arg(0, metavar="CHAT", kind="peer", help="@username, MTProto id or Bot-API id.")
    ]


async def id_get(ctx: OpContext, req: IdReq) -> BotIds:
    """Convert between MTProto peer ids and HTTP Bot-API chat ids.

    Pure arithmetic when the ref is already an id: a Bot-API id marks channels
    with `-100…` and basic groups with a plain negative number, and getting
    that conversion wrong is how a script posts into the wrong chat.
    """
    from telethon import utils

    if req.chat.kind == "id":
        marked = int(req.chat.value)
        _raw, kind = utils.resolve_id(marked)
        return BotIds(
            mtproto_id=marked,
            bot_api_id=marked,
            kind=_KIND_NAMES.get(kind.__name__, "user"),
            has_access_hash=False,
        )

    peer = await _send.resolve(ctx, req.chat)
    marked = int(utils.get_peer_id(peer))
    _raw, kind = utils.resolve_id(marked)
    return BotIds(
        mtproto_id=marked,
        bot_api_id=marked,
        kind=_KIND_NAMES.get(kind.__name__, "user"),
        has_access_hash=getattr(peer, "access_hash", None) is not None,
        username=str(req.chat.value) if req.chat.kind == "username" else None,
    )


_KIND_NAMES = {"PeerUser": "user", "PeerChat": "group", "PeerChannel": "channel"}


SPEC_ID_GET = OperationSpec(
    id="bot.id.get",
    request=IdReq,
    response=BotIds,
    impl=id_get,
    summary="Convert between MTProto peer ids and Bot-API chat ids",
    description=(
        "tlgr prints marked ids everywhere (COR-10), which is the same "
        "dialect the HTTP Bot API uses; this command says so out loud and "
        "reports whether an access hash is cached for the peer."
    ),
    columns=("mtproto_id", "bot_api_id", "kind"),
    headers=("MTProto", "Bot API", "Kind"),
    example={"mtproto_id": -1001234567890, "bot_api_id": -1001234567890, "kind": "channel"},
    example_args="bot id get @durov",
    covers=("bots.bot-api-dialog-ids",),
)


# ---------------------------------------------------------------------------
# bot start / stop
# ---------------------------------------------------------------------------


class StartReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot to start.")]
    param: Annotated[str | None, opt("--param", metavar="TEXT", help="Hidden start parameter.")] = (
        None
    )
    referrer: Annotated[
        str | None, opt("--referrer", metavar="TEXT", help="Referral/affiliate start parameter.")
    ] = None
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Start the bot inside this group."),
    ] = None
    channel: Annotated[
        PeerRef | None,
        opt("--channel", metavar="CHAT", kind="peer", help="Add the bot to this channel."),
    ] = None
    admin: Annotated[
        str | None,
        opt("--admin", metavar="RIGHTS", help="'+'-joined admin rights to grant."),
    ] = None
    add: Annotated[bool, opt("--add", help="Add the bot to the chat if it is not a member.")] = (
        False
    )
    restart: Annotated[bool, opt("--restart", help="Unblock the bot before starting it.")] = False


async def start(ctx: OpContext, req: StartReq) -> BotStarted:
    """Send `/start`, optionally with a hidden parameter and inside a chat.

    `messages.startBot` is the only way to send a start parameter the user
    never sees, which is what a deep link is; typing `/start payload` puts the
    payload in the history for everyone in the chat to read.
    """
    from telethon.tl.functions import channels as channels_fn
    from telethon.tl.functions import contacts as contacts_fn
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    if req.referrer and req.bot.kind == "username":
        await handle(
            contacts_fn.ResolveUsernameRequest(username=str(req.bot.value), referer=req.referrer)
        )
    peer = await _send.resolve(ctx, req.bot)
    bot = await _bots.input_user(ctx, req.bot)
    full, user = await _full(ctx, peer)
    bot_id = int(getattr(full, "id", 0) or 0)

    unblocked = False
    if req.restart and getattr(full, "blocked", False):
        await handle(contacts_fn.UnblockRequest(id=peer))
        unblocked = True

    target: Any = peer
    rights: list[str] = []
    if req.chat is not None or req.channel is not None:
        if bool(getattr(user, "bot_nochats", False)):
            raise PermissionError_("this bot refuses to be added to groups (BOT_GROUPS_BLOCKED)")
        where = req.chat if req.chat is not None else req.channel
        target = await _send.resolve(ctx, where)
        if req.add:
            await _add_to_chat(ctx, target, bot)
        granted = _bots.admin_rights(req.admin) or (
            getattr(full, "bot_group_admin_rights", None)
            if req.chat is not None
            else getattr(full, "bot_broadcast_admin_rights", None)
        )
        if granted is not None and (req.admin or req.channel is not None):
            await handle(
                channels_fn.EditAdminRequest(
                    channel=_input_channel(target),
                    user_id=bot,
                    admin_rights=granted,
                    rank="",
                )
            )
            rights = _bots.rights_keywords(granted)

    updates = await handle(
        fn.StartBotRequest(bot=bot, peer=target, start_param=req.param or req.referrer or "")
    )
    message = _send.message_from_updates(updates, chat_id=_send.peer_id_of(target))
    ctx.emit("bot_start", {"bot_id": bot_id, "chat_id": message.chat_id})
    return BotStarted(
        bot_id=bot_id,
        chat_id=message.chat_id,
        msg_id=message.id,
        start_param=req.param or req.referrer,
        admin_rights=rights,
        unblocked=unblocked,
    )


def _input_channel(peer: Any) -> Any:
    from tlgr.ops._common import input_channel

    return input_channel(peer)


async def _add_to_chat(ctx: OpContext, peer: Any, bot: Any) -> None:
    """Invite the bot, tolerating "already a member"."""
    from telethon.tl.functions import channels as channels_fn
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    try:
        if type(peer).__name__ == "InputPeerChat":
            await handle(
                fn.AddChatUserRequest(chat_id=getattr(peer, "chat_id", 0), user_id=bot, fwd_limit=0)
            )
        else:
            await handle(
                channels_fn.InviteToChannelRequest(channel=_input_channel(peer), users=[bot])
            )
    except Exception as exc:  # the server's own "already a member" is not a failure
        if "ALREADY" not in f"{type(exc).__name__} {exc}".upper().replace("_", ""):
            raise


SPEC_START = OperationSpec(
    id="bot.start",
    request=StartReq,
    response=BotStarted,
    impl=start,
    summary="Start a bot, with a deep-link parameter or inside a group",
    tags=frozenset({"visible-to-others"}),
    description=(
        "`--param` is the payload behind a `t.me/<bot>?start=…` link and is "
        "never written into the chat, which is the whole point of a deep "
        "link. `--referrer` additionally re-resolves the username with the "
        "referral attached, because the attribution happens at resolve time."
    ),
    aliases=("bot.restart",),
    mutating=True,
    rate_class="send",
    columns=("bot_id", "chat_id", "msg_id"),
    headers=("Bot", "Chat", "Message"),
    example={"bot_id": 93372553, "chat_id": 93372553, "msg_id": 12},
    example_args="bot start @gifbot",
    covers=(
        "bots.inline-switch-pm",
        "bots.referral-link-import",
        "bots.restart-bot",
        "bots.start-in-channel",
        "bots.start-in-group",
        "bots.start-in-group-as-admin",
        "bots.start-private",
        "bots.start-with-deeplink-param",
    ),
)


class StopReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot to block.")]
    delete_chat: Annotated[bool, opt("--delete-chat", help="Also delete the chat history.")] = False
    report: Annotated[bool, opt("--report", help="Report the bot as spam while blocking.")] = False


async def stop(ctx: OpContext, req: StopReq) -> BotStopped:
    """Block a bot, and optionally delete the whole conversation.

    `deleteHistory` answers with an `AffectedHistory` carrying an offset to
    resume from; calling it once and reporting success is how v1-shaped code
    deletes the first hundred messages and leaves the rest.
    """
    from telethon.tl.functions import contacts as contacts_fn
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    peer = await _send.resolve(ctx, req.bot)
    bot_id = _send.peer_id_of(peer)
    if req.report:
        await handle(fn.ReportSpamRequest(peer=peer))
    await handle(contacts_fn.BlockRequest(id=peer))

    deleted = 0
    if req.delete_chat:
        from tlgr.ops._common import affected_loop

        deleted = await affected_loop(
            ctx,
            lambda offset: fn.DeleteHistoryRequest(peer=peer, max_id=0, revoke=False),
        )
    ctx.emit("bot_stop", {"bot_id": bot_id})
    return BotStopped(bot_id=bot_id, blocked=True, history_deleted=deleted)


SPEC_STOP = OperationSpec(
    id="bot.stop",
    request=StopReq,
    response=BotStopped,
    impl=stop,
    summary="Stop and block a bot",
    mutating=True,
    destructive=True,
    columns=("bot_id", "blocked", "history_deleted"),
    headers=("Bot", "Blocked", "Deleted"),
    example={"bot_id": 93372553, "blocked": True, "history_deleted": 0},
    example_args="bot stop @gifbot",
    covers=("bots.delete-bot-chat-and-block", "bots.stop-bot", "dialogs.bot-stop-restart"),
)


# ---------------------------------------------------------------------------
# bot command list / send / set
# ---------------------------------------------------------------------------


class CommandListReq(Request):
    bot: Annotated[
        PeerRef | None,
        arg(0, metavar="BOT", required=False, kind="user", help="The bot."),
    ] = None
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Every bot's commands in this chat."),
    ] = None
    scope: Annotated[
        str | None,
        choice(
            "default",
            "users",
            "chats",
            "chat-admins",
            "peer",
            "peer-admins",
            "peer-user",
            help="Bot-side scope to read back (bot session).",
        ),
    ] = None
    peer: Annotated[
        PeerRef | None, opt("--peer", metavar="CHAT", kind="peer", help="Peer for a peer* scope.")
    ] = None
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="User for the peer-user scope."),
    ] = None
    lang: Annotated[str | None, opt("--lang", metavar="CODE", help="Language code.")] = None


async def command_list(ctx: OpContext, req: CommandListReq) -> Page[BotCommand]:
    """A bot's slash commands, from whichever side is asking.

    `has_help`/`has_settings` decide whether a GUI shows its "Bot Help" and
    "Bot Settings" entries, so they are computed from the list rather than
    assumed: a bot without `/help` must not get a menu item that does nothing.
    """
    from telethon.tl.functions import bots as fn

    if req.scope is not None:
        await _bots.require_bot_session(ctx, "reading back your own command list")
        scope = await _bots.command_scope(ctx, req.scope, req.peer, req.user)
        result = await client(ctx)(fn.GetBotCommandsRequest(scope=scope, lang_code=req.lang or ""))
        me = await client(ctx).get_me()
        items = _commands(
            result, bot_id=int(getattr(me, "id", 0) or 0), scope=req.scope, lang=req.lang
        )
        return Page(items=items, has_more=False, total=len(items))

    if req.chat is not None and req.bot is None:
        return await _chat_commands(ctx, req.chat)

    if req.bot is None:
        raise UsageError("name a bot, or use --chat to list every bot in a chat", field="bot")
    peer = await _send.resolve(ctx, req.bot)
    full, _user = await _full(ctx, peer)
    info = getattr(full, "bot_info", None)
    items = _commands(info, bot_id=int(getattr(full, "id", 0) or 0), lang=req.lang)
    return Page(items=items, has_more=False, total=len(items))


async def _chat_commands(ctx: OpContext, chat: PeerRef) -> Page[BotCommand]:
    """Every bot's commands in one chat, out of the chat's full info."""
    from telethon.tl.functions import channels as channels_fn
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, chat)
    if type(peer).__name__ == "InputPeerChat":
        result = await client(ctx)(fn.GetFullChatRequest(chat_id=getattr(peer, "chat_id", 0)))
    else:
        result = await client(ctx)(channels_fn.GetFullChannelRequest(channel=_input_channel(peer)))
    full = getattr(result, "full_chat", None)
    items: list[BotCommand] = []
    for info in getattr(full, "bot_info", None) or []:
        items += _commands(info, bot_id=int(getattr(info, "user_id", 0) or 0))
    return Page(items=items, has_more=False, total=len(items))


SPEC_COMMAND_LIST = OperationSpec(
    id="bot.command.list",
    request=CommandListReq,
    response=Page[BotCommand],
    impl=command_list,
    summary="List a bot's slash commands",
    description=(
        "A user reads them out of `botInfo`; a bot session reads its own back "
        "per scope with `bots.getBotCommands`, which is the only way to see "
        "what a scope actually holds."
    ),
    aliases=("bot.commands",),
    columns=("bot_id", "command", "description"),
    headers=("Bot", "Command", "Description"),
    example={
        "items": [{"bot_id": 93372553, "command": "start", "description": "Start the bot"}],
        "has_more": False,
    },
    example_args="bot command list @gifbot",
    covers=(
        "bots.bot-help-settings-shortcuts",
        "bots.get-my-bot-commands",
        "bots.list-commands",
    ),
)


class CommandSendReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot addressed.")]
    command: Annotated[str, arg(1, metavar="COMMAND", help="The command, with or without '/'.")]
    args: Annotated[
        list[str],
        arg(2, metavar="ARGS", required=False, variadic=True, help="Arguments appended after it."),
    ] = []
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Send it in this chat instead."),
    ] = None
    guest: Annotated[
        bool, opt("--guest", help="Address the bot in guest mode by mentioning it.")
    ] = False
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Forum topic id.")
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    business_connection: Annotated[
        str | None,
        opt(
            "--business-connection", metavar="ID", help="Send as a business account (bot session)."
        ),
    ] = None


async def command_send(ctx: OpContext, req: CommandSendReq) -> CommandSent:
    """Send `/command` to a bot, in its chat or in a group.

    In a group with more than one bot the command MUST carry `@botusername`
    or every bot ignores it, so tlgr appends it whenever the destination is
    not the bot's own private chat. Guest mode has no method of its own: the
    trigger is an ordinary message that mentions the bot.
    """
    from telethon.tl.functions import messages as fn

    bot_peer = await _send.resolve(ctx, req.bot)
    target = await _send.resolve(ctx, req.chat) if req.chat is not None else bot_peer
    in_private = req.chat is None or _send.peer_id_of(target) == _send.peer_id_of(bot_peer)

    username = str(req.bot.value) if req.bot.kind == "username" else ""
    if not username and not in_private:
        _full_user, user = await _full(ctx, bot_peer)
        username = str(getattr(user, "username", "") or "")
    name = req.command.lstrip("/")
    if not in_private and username:
        name = f"{name}@{username}"
    text = " ".join([f"/{name}", *req.args]).strip()
    if req.guest and username:
        text = f"@{username} {text}"

    reply_to = await _send.reply_target(ctx, reply_to=req.reply_to, topic=req.topic)
    request = fn.SendMessageRequest(
        peer=target,
        message=text,
        random_id=_random_id(),
        reply_to=reply_to,
        silent=req.silent or None,
    )
    updates = await _invoke_as(ctx, req.business_connection, request)
    message = _send.message_from_updates(updates, chat_id=_send.peer_id_of(target), sent_text=text)
    ctx.emit("bot_command", {"chat_id": message.chat_id, "text": text})
    return CommandSent(
        chat_id=message.chat_id, msg_id=message.id, text=text, via_bot=username or None
    )


def _random_id() -> int:
    from tlgr.ops._common import random_id

    return random_id()


async def _invoke_as(ctx: OpContext, connection_id: str | None, request: Any) -> Any:
    """Send *request*, wrapped in a business connection when one is named.

    The wrapper is not a header: the query has to reach the connection's own
    DC, which is why the connection is looked up first.
    """
    handle = client(ctx)
    if not connection_id:
        return await handle(request)
    await _bots.require_bot_session(ctx, "--business-connection")
    from telethon.tl.functions import InvokeWithBusinessConnectionRequest
    from telethon.tl.functions import account as account_fn

    connection = await handle(
        account_fn.GetBotBusinessConnectionRequest(connection_id=connection_id)
    )
    dc_id = _connection_dc(connection)
    return await _bots.on_dc(
        ctx,
        dc_id,
        InvokeWithBusinessConnectionRequest(connection_id=connection_id, query=request),
    )


def _connection_dc(result: Any) -> int:
    for update in getattr(result, "updates", None) or []:
        connection = getattr(update, "connection", None)
        if connection is not None:
            return int(getattr(connection, "dc_id", 0) or 0)
    return int(getattr(result, "dc_id", 0) or 0)


SPEC_COMMAND_SEND = OperationSpec(
    id="bot.command.send",
    request=CommandSendReq,
    response=CommandSent,
    impl=command_send,
    summary="Send a slash command to a bot",
    tags=frozenset({"visible-to-others"}),
    description=(
        "Driving @BotFather's own conversation with this command and "
        "`bot press` is the only way to reach the toggles Telegram exposes "
        "nowhere else — group privacy mode and bot-to-bot mode."
    ),
    aliases=("bot.cmd",),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "text"),
    headers=("Chat", "Message", "Text"),
    example={"chat_id": 93372553, "msg_id": 12, "text": "/start"},
    example_args="bot command send @gifbot start",
    covers=("bots.bot-privacy-mode", "bots.bot-to-bot-messaging", "bots.send-command"),
    covers_partial=("bots.bot-help-settings-shortcuts", "bots.guest-mode-invoke"),
    coverage_note=(
        "Whether a bot declares /help and /settings is reported by "
        "`bot command list`; the guest-mode bot listing is `bot list --recent "
        "--kind guest`."
    ),
)


class CommandSetReq(Request):
    commands: Annotated[
        str | None,
        arg(0, metavar="COMMANDS", required=False, help="'start:Start,help:Show help'."),
    ] = None
    scope: Annotated[
        str,
        choice(
            "default",
            "users",
            "chats",
            "chat-admins",
            "peer",
            "peer-admins",
            "peer-user",
            help="Command scope.",
        ),
    ] = "default"
    peer: Annotated[
        PeerRef | None, opt("--peer", metavar="CHAT", kind="peer", help="Peer for a peer* scope.")
    ] = None
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="User for the peer-user scope."),
    ] = None
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language code.")] = ""
    file: Annotated[
        str | None,
        opt("--file", metavar="PATH", kind="path", help="Read the list from a JSON file."),
    ] = None
    clear: Annotated[bool, opt("--clear", help="Reset the list for this scope.")] = False


async def command_set(ctx: OpContext, req: CommandSetReq) -> BotCommandSet:
    """Publish (or clear) my bot's command list for one scope and language.

    A human owner does this through @BotFather; the method itself is bot-only,
    which is why the session is checked before the request is built.
    """
    from telethon.tl import types
    from telethon.tl.functions import bots as fn

    await _bots.require_bot_session(ctx, "setting your bot's command list")
    scope = await _bots.command_scope(ctx, req.scope, req.peer, req.user)
    handle = client(ctx)

    if req.clear:
        await handle(fn.ResetBotCommandsRequest(scope=scope, lang_code=req.lang))
        return BotCommandSet(scope=req.scope, lang=req.lang, cleared=True)

    pairs = _command_pairs(req)
    await handle(
        fn.SetBotCommandsRequest(
            scope=scope,
            lang_code=req.lang,
            commands=[
                types.BotCommand(command=name, description=description)
                for name, description in pairs
            ],
        )
    )
    return BotCommandSet(
        scope=req.scope,
        lang=req.lang,
        commands=[
            BotCommand(command=name, description=description, scope=req.scope, lang=req.lang)
            for name, description in pairs
        ],
    )


def _command_pairs(req: CommandSetReq) -> list[tuple[str, str]]:
    if req.file:
        loaded = _bots.load_json(req.file, field="file")
        if not isinstance(loaded, list):
            raise UsageError("--file: expected a JSON list of commands", field="file")
        return [
            (str(entry.get("command", "")).lstrip("/"), str(entry.get("description", "")))
            for entry in loaded
        ]
    if not req.commands:
        raise UsageError("give a command list, --file or --clear", field="commands")
    pairs: list[tuple[str, str]] = []
    for chunk in req.commands.split(","):
        name, _, description = chunk.partition(":")
        if not name.strip():
            continue
        pairs.append((name.strip().lstrip("/"), description.strip()))
    return pairs


SPEC_COMMAND_SET = OperationSpec(
    id="bot.command.set",
    request=CommandSetReq,
    response=BotCommandSet,
    impl=command_set,
    summary="Set or clear my bot's command list for one scope",
    mutating=True,
    columns=("scope", "lang", "cleared"),
    headers=("Scope", "Lang", "Cleared"),
    example={
        "scope": "default",
        "lang": "",
        "commands": [{"command": "start", "description": "Start the bot"}],
    },
    example_args='bot command set "start:Start the bot"',
    covers=("bots.reset-my-bot-commands", "bots.set-my-bot-commands"),
)


# ---------------------------------------------------------------------------
# bot menu get / set
# ---------------------------------------------------------------------------


class MenuGetReq(Request):
    bot: Annotated[
        PeerRef | None, arg(0, metavar="BOT", required=False, kind="user", help="The bot.")
    ] = None
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Per-user override (bot session)."),
    ] = None


async def menu_get(ctx: OpContext, req: MenuGetReq) -> MenuButton:
    """The button left of the message input."""
    from telethon.tl.functions import bots as fn

    if req.user is not None:
        await _bots.require_bot_session(ctx, "reading a per-user menu button")
        button = await client(ctx)(
            fn.GetBotMenuButtonRequest(user_id=await _bots.input_user(ctx, req.user, field="user"))
        )
        return _menu_button(
            button, user_id=_send.peer_id_of(await _send.resolve(ctx, req.user))
        ) or (MenuButton())
    if req.bot is None:
        raise UsageError("name a bot, or use --user on a bot session", field="bot")
    peer = await _send.resolve(ctx, req.bot)
    full, _user = await _full(ctx, peer)
    info = getattr(full, "bot_info", None)
    return _menu_button(getattr(info, "menu_button", None)) or MenuButton(kind="commands")


SPEC_MENU_GET = OperationSpec(
    id="bot.menu.get",
    request=MenuGetReq,
    response=MenuButton,
    impl=menu_get,
    summary="Show a bot's menu button",
    description=(
        "`botMenuButtonDefault` is never what a user sees — the server shows "
        "the commands list instead — so it is normalised to `commands` rather "
        "than reported as a third state nobody can act on."
    ),
    columns=("kind", "text", "url"),
    headers=("Kind", "Text", "URL"),
    example={"kind": "commands"},
    example_args="bot menu get @gifbot",
    covers=("bots.menu-button-state",),
)


class MenuSetReq(Request):
    commands: Annotated[bool, opt("--commands", help="Show the commands list.")] = False
    webapp: Annotated[bool, opt("--webapp", help="Open a mini app.")] = False
    default: Annotated[bool, opt("--default", help="Reset to the default.")] = False
    text: Annotated[str | None, opt("--text", metavar="TEXT", help="Button label.")] = None
    url: Annotated[str | None, opt("--url", metavar="URL", help="Mini app URL.")] = None
    user: Annotated[
        PeerRef | None, opt("--user", metavar="USER", kind="user", help="Apply to this user only.")
    ] = None


async def menu_set(ctx: OpContext, req: MenuSetReq) -> MenuButton:
    """Set my bot's menu button, globally or for one user."""
    from telethon.tl import types
    from telethon.tl.functions import bots as fn

    await _bots.require_bot_session(ctx, "setting the menu button")
    chosen = [name for name in ("commands", "webapp", "default") if getattr(req, name)]
    if len(chosen) != 1:
        raise UsageError("give exactly one of --commands, --webapp or --default", field="commands")

    if req.webapp:
        if not req.text or not req.url:
            raise UsageError("--webapp needs --text and --url", field="url")
        button: Any = types.BotMenuButton(text=req.text, url=req.url)
    elif req.commands:
        button = types.BotMenuButtonCommands()
    else:
        button = types.BotMenuButtonDefault()

    user = (
        await _bots.input_user(ctx, req.user, field="user")
        if req.user is not None
        else types.InputUserEmpty()
    )
    await client(ctx)(fn.SetBotMenuButtonRequest(user_id=user, button=button))
    user_id = _send.peer_id_of(await _send.resolve(ctx, req.user)) if req.user else None
    return MenuButton(
        kind="webapp" if req.webapp else "commands" if req.commands else "default",
        text=req.text,
        url=req.url,
        user_id=user_id,
    )


SPEC_MENU_SET = OperationSpec(
    id="bot.menu.set",
    request=MenuSetReq,
    response=MenuButton,
    impl=menu_set,
    summary="Set my bot's menu button",
    mutating=True,
    columns=("kind", "text", "url"),
    headers=("Kind", "Text", "URL"),
    example={"kind": "webapp", "text": "Open", "url": "https://example.org/app"},
    example_args="bot menu set --commands",
    covers=("bots.menu-button-set",),
)


# ---------------------------------------------------------------------------
# bot permission get / set
# ---------------------------------------------------------------------------


class PermissionGetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot.")]


async def permission_get(ctx: OpContext, req: PermissionGetReq) -> BotPermission:
    """What a bot is allowed to do to me."""
    from telethon.tl.functions import bots as fn

    peer = await _send.resolve(ctx, req.bot)
    full, _user = await _full(ctx, peer)
    can_send = bool(
        await client(ctx)(fn.CanSendMessageRequest(bot=await _bots.input_user(ctx, req.bot)))
    )
    return BotPermission(
        bot_id=int(getattr(full, "id", 0) or 0),
        can_send_messages=can_send,
        emoji_status_allowed=bool(getattr(full, "bot_can_manage_emoji_status", False)),
    )


SPEC_PERMISSION_GET = OperationSpec(
    id="bot.permission.get",
    request=PermissionGetReq,
    response=BotPermission,
    impl=permission_get,
    summary="Show what a bot may do to me",
    columns=("bot_id", "can_send_messages", "emoji_status_allowed"),
    headers=("Bot", "May message", "May set status"),
    example={"bot_id": 93372553, "can_send_messages": True, "emoji_status_allowed": False},
    example_args="bot permission get @gifbot",
    covers=("bots.bot-emoji-status-permission",),
    covers_partial=("bots.allow-send-messages",),
    coverage_note="Granting or revoking is `bot permission set`.",
)


class PermissionSetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot.")]
    key: Annotated[str, arg(1, metavar="KEY", help="message or emoji-status.")]
    state: Annotated[str, arg(2, metavar="STATE", help="on to grant, off to revoke.")]


async def permission_set(ctx: OpContext, req: PermissionSetReq) -> BotPermission:
    """Grant or revoke one bot permission.

    "May message me again" is also granted implicitly by `webapp open
    --allow-write` and `bot attach toggle --allow-write`, and by nothing else:
    a permission that can be granted as a side effect of an unrelated command
    is a permission the user did not give.
    """
    from telethon.tl.functions import bots as fn

    if req.key not in ("message", "emoji-status"):
        raise UsageError("key must be 'message' or 'emoji-status'", field="key")
    if req.state not in ("on", "off"):
        raise UsageError("state must be 'on' or 'off'", field="state")

    bot = await _bots.input_user(ctx, req.bot)
    peer = await _send.resolve(ctx, req.bot)
    bot_id = _send.peer_id_of(peer)
    handle = client(ctx)
    already = False

    if req.key == "message":
        if req.state == "off":
            raise UsageError(
                "Telegram has no revoke for 'may message me'; block the bot with `bot stop`",
                field="state",
            )
        if bool(await handle(fn.CanSendMessageRequest(bot=bot))):
            already = True
            from tlgr.ops._common import already as mark_already

            mark_already(ctx)
        else:
            await handle(fn.AllowSendMessageRequest(bot=bot))
    else:
        await handle(fn.ToggleUserEmojiStatusPermissionRequest(bot=bot, enabled=req.state == "on"))

    return BotPermission(
        bot_id=bot_id,
        key=req.key,
        state=req.state,
        already=already,
        can_send_messages=req.key == "message" and req.state == "on",
        emoji_status_allowed=req.key == "emoji-status" and req.state == "on",
    )


SPEC_PERMISSION_SET = OperationSpec(
    id="bot.permission.set",
    request=PermissionSetReq,
    response=BotPermission,
    impl=permission_set,
    summary="Allow or revoke a bot permission",
    mutating=True,
    idempotent=True,
    columns=("bot_id", "key", "state", "already"),
    headers=("Bot", "Key", "State", "Already"),
    example={"bot_id": 93372553, "key": "message", "state": "on", "already": False},
    example_args="bot permission set @gifbot message on",
    covers=("bots.allow-send-messages",),
    covers_partial=("bots.bot-emoji-status-permission",),
    coverage_note="Reading both permissions back is `bot permission get`.",
)


# ---------------------------------------------------------------------------
# bot press
# ---------------------------------------------------------------------------


class PressReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat holding the message.")]
    msg_id: Annotated[
        int | None,
        arg(1, metavar="MSG_ID", required=False, kind="msg_id", help="Message id."),
    ] = None
    button: Annotated[
        str | None,
        opt("--button", metavar="SPEC", help="'<row>,<col>', '<n>' or the button's exact text."),
    ] = None
    data: Annotated[
        str | None,
        opt("--data", metavar="PAYLOAD", help="Address a callback button by its payload."),
    ] = None
    rich_button: Annotated[
        int | None, opt("--rich-button", metavar="N", help="Button in a layer-229 rich message.")
    ] = None
    ephemeral: Annotated[
        int | None, opt("--ephemeral", metavar="ID", help="Button on an ephemeral bot message.")
    ] = None
    webapp_req: Annotated[
        str | None, opt("--webapp-req", metavar="ID", help="Answer a mini app's peer request.")
    ] = None
    password: Annotated[
        str | None,
        opt(secret=True, envvar="TLGR_2FA_PASSWORD", help="2FA password for a guarded button."),
    ] = None
    share_phone: Annotated[
        bool, opt("--share-phone", help="CONSENT: send my phone number to the bot.")
    ] = False
    share_geo: Annotated[
        str | None, opt("--share-geo", metavar="LAT,LON", help="CONSENT: send this location.")
    ] = None
    peers: Annotated[
        list[PeerRef],
        opt("--peers", metavar="PEER", kind="peer", help="CONSENT: peers to share (repeatable)."),
    ] = []
    create_bot: Annotated[
        bool, opt("--create-bot", help="Answer a create-bot request by creating one.")
    ] = False
    name: Annotated[str | None, opt("--name", help="Managed-bot name for --create-bot.")] = None
    username: Annotated[
        str | None, opt("--username", help="Managed-bot username for --create-bot.")
    ] = None
    poll: Annotated[
        str | None, opt("--poll", metavar="SPEC", help="CONSENT: poll 'Question?:A,B,C'.")
    ] = None
    quiz: Annotated[bool, opt("--quiz", help="Make the --poll a quiz.")] = False
    correct: Annotated[
        int | None, opt("--correct", metavar="N", help="0-based correct answer for a quiz.")
    ] = None
    switch_to: Annotated[
        PeerRef | None,
        opt(
            "--switch-to", metavar="CHAT", kind="peer", help="Chat to run a switch-inline query in."
        ),
    ] = None
    business_connection: Annotated[
        str | None,
        opt(
            "--business-connection", metavar="ID", help="Press as a business account (bot session)."
        ),
    ] = None


async def press(ctx: OpContext, req: PressReq) -> Pressed:
    """Press a button, whatever kind it is, and report what came back.

    The consent rule is the reason this is one command and not fourteen.
    Four button kinds hand the bot something the user owns — a phone number, a
    location, a chat, a poll — and Telegram's protocol makes them look like
    every other button. tlgr will not press one without the flag that names
    what is about to leave: without it, it prints what it *would* send and
    exits 2. `buy` is refused outright, because paying is not something an
    agent does on someone's behalf.
    """
    if req.rich_button is not None:
        _bots.unsupported("--rich-button")
    if req.ephemeral is not None:
        _bots.unsupported("--ephemeral (ephemeral.getCallbackAnswer)")

    peer = await _send.resolve(ctx, req.chat)
    if req.webapp_req:
        return await _answer_webapp_request(ctx, req, peer)
    if req.msg_id is None:
        raise UsageError("give a message id, or --webapp-req", field="msg_id")

    message = await _media.fetch_message(ctx, peer, req.msg_id)
    markup = getattr(message, "reply_markup", None)
    flat = _flatten(markup)
    if not flat:
        raise NotFoundError(f"message {req.msg_id} has no buttons")
    row, col, index, button = _pick(flat, req)
    kind = _bots.BUTTON_TYPES.get(type(button).__name__, "unsupported")
    answer = Pressed(kind=kind, row=row, col=col, n=index, text=getattr(button, "text", None))
    return await _dispatch(ctx, req, peer, message, button, answer)


def _flatten(markup: Any) -> list[tuple[int, int, int, Any]]:
    out: list[tuple[int, int, int, Any]] = []
    index = 0
    for row_index, row in enumerate(getattr(markup, "rows", None) or []):
        for col_index, button in enumerate(getattr(row, "buttons", None) or []):
            out.append((row_index, col_index, index, button))
            index += 1
    return out


def _pick(flat: list[tuple[int, int, int, Any]], req: PressReq) -> tuple[int, int, int, Any]:
    """The addressed button: by payload, by coordinates, by index, or by text."""
    if req.data is not None:
        wanted = _bots.payload_bytes(req.data, field="data")
        for row, col, index, button in flat:
            if getattr(button, "data", None) == wanted:
                return row, col, index, button
        raise NotFoundError("no callback button on that message carries that payload")

    if req.button is None:
        if len(flat) == 1:
            return flat[0]
        raise UsageError(
            f"the message has {len(flat)} buttons; name one with --button or --data",
            field="button",
        )

    spec = req.button.strip()
    if "," in spec:
        head, _, tail = spec.partition(",")
        try:
            want = (int(head), int(tail))
        except ValueError as exc:
            raise UsageError("--button: expected '<row>,<col>'", field="button") from exc
        for row, col, index, button in flat:
            if (row, col) == want:
                return row, col, index, button
        raise NotFoundError(f"there is no button at row {want[0]}, column {want[1]}")
    if spec.isdigit():
        number = int(spec)
        for row, col, index, button in flat:
            if index == number:
                return row, col, index, button
        raise NotFoundError(f"there is no button {number}; the message has {len(flat)}")

    exact = [e for e in flat if str(getattr(e[3], "text", "")) == spec]
    if len(exact) == 1:
        return exact[0]
    lowered = spec.lower()
    partial = [e for e in flat if lowered in str(getattr(e[3], "text", "")).lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise NotFoundError(f"no button matches {spec!r}")
    raise UsageError(
        f"{spec!r} matches {len(partial)} buttons; use --button '<row>,<col>' instead",
        field="button",
    )


def _refuse(what: str, flag: str) -> Any:
    raise UsageError(
        f"this button would {what}; pass {flag} to allow it. Nothing was sent.",
        field=flag.lstrip("-").replace("-", "_"),
    )


async def _dispatch(
    ctx: OpContext, req: PressReq, peer: Any, message: Any, button: Any, answer: Pressed
) -> Pressed:
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    chat_id = _send.peer_id_of(peer)
    kind = answer.kind

    if kind == "buy":
        raise PermissionError_(
            "a Pay button starts a payment, and tlgr never spends money on your behalf"
        )
    if kind == "unsupported":
        raise UsageError(f"tlgr cannot press a {type(button).__name__}", field="button")

    if kind == "text":
        sent = await _invoke_as(
            ctx,
            req.business_connection,
            fn.SendMessageRequest(
                peer=peer, message=str(getattr(button, "text", "")), random_id=_random_id()
            ),
        )
        answer.sent_message_id = _send.message_from_updates(sent, chat_id=chat_id).id
        return answer

    if kind in ("callback", "game"):
        return await _press_callback(ctx, req, peer, message, button, answer)

    if kind == "url":
        answer.url = getattr(button, "url", None)
        return answer

    if kind == "copy":
        answer.copy_text = getattr(button, "copy_text", None)
        return answer

    if kind == "url_auth":
        result = await handle(
            fn.RequestUrlAuthRequest(
                peer=peer, msg_id=int(message.id), button_id=int(getattr(button, "button_id", 0))
            )
        )
        auth = _url_auth_model(result)
        answer.auth = _bots_to_dict(auth)
        answer.url = auth.url
        return answer

    if kind == "user_profile":
        user_id = int(getattr(button, "user_id", 0) or 0)
        answer.user = {"id": user_id}
        return answer

    if kind == "switch_inline":
        target = req.switch_to if req.switch_to is not None else req.chat
        where = await _send.resolve(ctx, target)
        bot = await _bot_of(ctx, message)
        if bot is None:
            raise NotFoundError("the message does not say which bot to query")
        results = await handle(
            fn.GetInlineBotResultsRequest(
                bot=bot, peer=where, query=str(getattr(button, "query", "") or ""), offset=""
            )
        )
        answer.query_id = str(getattr(results, "query_id", "") or "")
        answer.results = [
            {"id": str(getattr(entry, "id", "")), "type": str(getattr(entry, "type", ""))}
            for entry in (getattr(results, "results", None) or [])
        ]
        return answer

    if kind in ("webview", "simple_webview"):
        bot = await _bot_of(ctx, message) or await _bots.input_user(ctx, req.chat)
        url = str(getattr(button, "url", "") or "")
        if kind == "webview":
            result = await handle(
                fn.RequestWebViewRequest(peer=peer, bot=bot, platform=PLATFORM, url=url)
            )
        else:
            result = await handle(
                fn.RequestSimpleWebViewRequest(bot=bot, platform=PLATFORM, url=url)
            )
        answer.url = str(getattr(result, "url", "") or "")
        query_id = getattr(result, "query_id", None)
        answer.query_id = str(query_id) if query_id else None
        return answer

    if kind == "request_phone":
        if not req.share_phone:
            _refuse("send the bot your phone number", "--share-phone")
        me = await handle.get_me()
        sent = await handle(
            fn.SendMediaRequest(
                peer=peer,
                media=types.InputMediaContact(
                    phone_number=str(getattr(me, "phone", "") or ""),
                    first_name=str(getattr(me, "first_name", "") or ""),
                    last_name=str(getattr(me, "last_name", "") or ""),
                    vcard="",
                ),
                message="",
                random_id=_random_id(),
            )
        )
        answer.sent_message_id = _send.message_from_updates(sent, chat_id=chat_id).id
        return answer

    if kind == "request_geo":
        if not req.share_geo:
            _refuse("send the bot your location", "--share-geo")
        lat, lon = _latlon(str(req.share_geo))
        sent = await handle(
            fn.SendMediaRequest(
                peer=peer,
                media=types.InputMediaGeoPoint(geo_point=types.InputGeoPoint(lat=lat, long=lon)),
                message="",
                random_id=_random_id(),
            )
        )
        answer.sent_message_id = _send.message_from_updates(sent, chat_id=chat_id).id
        return answer

    if kind == "request_poll":
        if not req.poll:
            _refuse("create a poll in this chat", "--poll")
        sent = await handle(
            fn.SendMediaRequest(
                peer=peer, media=_poll_media(req), message="", random_id=_random_id()
            )
        )
        answer.sent_message_id = _send.message_from_updates(sent, chat_id=chat_id).id
        return answer

    if kind == "request_peer":
        return await _press_request_peer(ctx, req, peer, message, button, answer)

    raise UsageError(f"tlgr cannot press a {kind} button", field="button")


def _bots_to_dict(model: Any) -> dict[str, Any]:
    from tlgr.models.base import to_builtins

    value = to_builtins(model)
    return value if isinstance(value, dict) else {}


async def _bot_of(ctx: OpContext, message: Any) -> Any:
    """The `InputUser` of the bot that owns a message's buttons.

    Resolved through the account's own resolver rather than assembled from
    the message: an `InputUser` needs an access hash, and the one on a message
    object is frequently absent (`min` peers carry none at all).
    """
    for attribute in ("via_bot_id", "from_id", "peer_id"):
        value = getattr(message, attribute, None)
        user_id = value if isinstance(value, int) else getattr(value, "user_id", None)
        if user_id:
            return await _bots.input_user(ctx, _bots.peer_ref(str(int(user_id))), field="chat")
    return None


def _latlon(value: str) -> tuple[float, float]:
    head, _, tail = str(value).partition(",")
    try:
        return float(head), float(tail)
    except ValueError as exc:
        raise UsageError("--share-geo: expected 'lat,lon'", field="share_geo") from exc


def _poll_media(req: PressReq) -> Any:
    from telethon.tl import types

    question, _, options = str(req.poll).partition(":")
    answers = [a.strip() for a in options.split(",") if a.strip()]
    if len(answers) < 2:
        raise UsageError("--poll: expected 'Question?:A,B,C'", field="poll")
    if req.quiz and req.correct is None:
        raise UsageError("--quiz needs --correct", field="correct")
    return types.InputMediaPoll(
        poll=types.Poll(
            id=0,
            hash=0,
            question=types.TextWithEntities(text=question.strip(), entities=[]),
            answers=[
                types.PollAnswer(
                    text=types.TextWithEntities(text=text, entities=[]),
                    option=bytes([index]),
                )
                for index, text in enumerate(answers)
            ],
            quiz=req.quiz or None,
        ),
        correct_answers=[bytes([int(req.correct)])] if req.correct is not None else None,
    )


async def _press_callback(
    ctx: OpContext, req: PressReq, peer: Any, message: Any, button: Any, answer: Pressed
) -> Pressed:
    """A callback (or game) button, with the SRP dance when it needs one.

    `BOT_RESPONSE_TIMEOUT` is not an error: it means the bot is not running.
    Reporting it as a failure would make "the bot is offline" indistinguishable
    from "the press was rejected", so the answer comes back with a null message.
    """
    from telethon.tl.functions import messages as fn

    from tlgr.ops import _auth

    handle = client(ctx)
    is_game = answer.kind == "game"

    def build(check: Any) -> Any:
        return fn.GetBotCallbackAnswerRequest(
            peer=peer,
            msg_id=int(message.id),
            game=is_game or None,
            data=None if is_game else getattr(button, "data", None),
            password=check,
        )

    try:
        if getattr(button, "requires_password", False):
            if req.password is None:
                raise UsageError(
                    "this button is protected by your 2FA password; "
                    "pass it with --password-env or --password-stdin",
                    field="password",
                )
            result = await _auth.with_password(handle, build, req.password)
        else:
            result = await handle(build(None))
    except Exception as exc:  # one specific server answer is not a failure
        if "BOTRESPONSETIMEOUT" not in f"{type(exc).__name__} {exc}".upper().replace("_", ""):
            raise
        ctx.warn("the bot did not answer in time; it is probably offline")
        return answer

    answer.message = getattr(result, "message", None)
    answer.alert = bool(getattr(result, "alert", False))
    answer.url = getattr(result, "url", None)
    answer.native_ui = bool(getattr(result, "native_ui", False))
    answer.cache_time = getattr(result, "cache_time", None)
    return answer


async def _press_request_peer(
    ctx: OpContext, req: PressReq, peer: Any, message: Any, button: Any, answer: Pressed
) -> Pressed:
    """Answer a request-peer button, or create the bot it asked for."""
    from telethon.tl.functions import bots as bots_fn
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    peer_type = getattr(button, "peer_type", None)
    if type(peer_type).__name__ == "RequestPeerTypeCreateBot":
        if not req.create_bot:
            _refuse("create a bot owned by you", "--create-bot")
        if not req.name or not req.username:
            raise UsageError("--create-bot needs --name and --username", field="name")
        created = await handle(
            bots_fn.CreateBotRequest(
                name=req.name,
                username=req.username,
                manager_id=await _bot_of(ctx, message) or await _bots.input_user(ctx, req.chat),
            )
        )
        answer.peers = [
            int(getattr(u, "id", 0) or 0) for u in (getattr(created, "users", None) or [])
        ]
        return answer

    if not req.peers:
        _refuse("disclose one of your chats to the bot", "--peers")
    resolved = [await _send.resolve(ctx, value) for value in req.peers]
    await handle(
        fn.SendBotRequestedPeerRequest(
            peer=peer,
            msg_id=int(message.id),
            button_id=int(getattr(button, "button_id", 0) or 0),
            requested_peers=resolved,
        )
    )
    answer.peers = [_send.peer_id_of(entry) for entry in resolved]
    return answer


async def _answer_webapp_request(ctx: OpContext, req: PressReq, peer: Any) -> Pressed:
    """Answer a mini app's peer request, addressed by `webapp_req_id`."""
    from telethon.tl.functions import bots as bots_fn
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    bot = await _bots.input_user(ctx, req.chat)
    button = await handle(
        bots_fn.GetRequestedWebViewButtonRequest(bot=bot, webapp_req_id=str(req.webapp_req))
    )
    answer = Pressed(kind="request_peer", text=getattr(button, "text", None))
    if not req.peers:
        _refuse("disclose one of your chats to the mini app", "--peers")
    resolved = [await _send.resolve(ctx, value) for value in req.peers]
    await handle(
        fn.SendBotRequestedPeerRequest(
            peer=peer,
            button_id=int(getattr(button, "button_id", 0) or 0),
            requested_peers=resolved,
            webapp_req_id=str(req.webapp_req),
        )
    )
    answer.peers = [_send.peer_id_of(entry) for entry in resolved]
    return answer


SPEC_PRESS = OperationSpec(
    id="bot.press",
    request=PressReq,
    response=Pressed,
    impl=press,
    summary="Press a button on a message",
    tags=frozenset({"visible-to-others"}),
    description=(
        "One dispatcher for every button kind, returning a typed answer: a "
        "callback toast, a URL, a signed mini-app session, inline results, a "
        "peer prompt or copy text. A button that would disclose your phone "
        "number, your location, a chat or a new poll is not pressed without "
        "the flag that names it — tlgr prints what it would send and exits 2. "
        "A Pay button is refused outright (exit 6)."
    ),
    aliases=("bot.click", "bot.button.press"),
    mutating=True,
    rate_class="send",
    columns=("kind", "n", "message", "url"),
    headers=("Kind", "#", "Answer", "URL"),
    example={"kind": "callback", "n": 0, "message": "Saved", "alert": False},
    example_args="bot press @gifbot 12 --button 0",
    covers=(
        "bots.bot-ownership-transfer",
        "bots.button-request-location",
        "bots.button-request-peer",
        "bots.button-request-phone",
        "bots.button-request-poll",
        "bots.callback-button-press",
        "bots.callback-button-with-password",
        "bots.copy-text-button",
        "bots.managed-bot-request-button",
        "bots.play-game",
        "bots.reply-keyboard-press-text",
        "bots.url-button",
        "bots.user-profile-button",
        "bots.webapp-request-phone",
    ),
    covers_partial=(
        "bots.attach-webapp-open",
        "bots.bot-privacy-mode",
        "bots.button-request-peer-from-miniapp",
        "bots.login-url-button",
        "bots.switch-inline-button",
        "bots.webapp-switch-inline-query",
    ),
    coverage_note=(
        "Pressing surfaces each of these; completing them is `webapp open`, "
        "`bot url-auth accept`, `inline query` and `inline send`."
    ),
)


# ---------------------------------------------------------------------------
# bot url-auth
# ---------------------------------------------------------------------------


def _url_auth_model(result: Any) -> UrlAuth:
    name = type(result).__name__
    if name == "UrlAuthResultAccepted":
        return UrlAuth(result="accepted", url=getattr(result, "url", None))
    if name == "UrlAuthResultDefault":
        return UrlAuth(result="default")
    bot = getattr(result, "bot", None)
    return UrlAuth(
        result="request",
        bot=str(getattr(bot, "username", "") or getattr(bot, "id", "") or "") or None,
        domain=getattr(result, "domain", None),
        verified_app_name=getattr(result, "verified_app_name", None),
        is_app=bool(getattr(result, "is_app", False)),
        browser=getattr(result, "browser", None),
        platform=getattr(result, "platform", None),
        ip=getattr(result, "ip", None),
        region=getattr(result, "region", None),
        request_write_access=bool(getattr(result, "request_write_access", False)),
        request_phone_number=bool(getattr(result, "request_phone_number", False)),
        match_codes=bool(getattr(result, "match_codes", False)),
        match_codes_first=bool(getattr(result, "match_codes_first", False)),
        user_id_hint=getattr(result, "user_id_hint", None),
    )


class UrlAuthGetReq(Request):
    target: Annotated[
        str, arg(0, metavar="TARGET", help="Chat holding the button, or the OAuth deep link.")
    ]
    msg_id: Annotated[
        int | None, opt("--msg-id", metavar="ID", kind="msg_id", help="Message id for a button.")
    ] = None
    button_id: Annotated[
        int | None, opt("--button-id", metavar="N", help="Button id from the reply markup.")
    ] = None
    in_app_origin: Annotated[
        str | None, opt("--in-app-origin", metavar="ORIGIN", help="Origin of a mini-app request.")
    ] = None
    check_code: Annotated[
        str | None, opt("--check-code", metavar="CODE", help="Pre-validate this emoji match code.")
    ] = None


async def _url_auth_request(
    ctx: OpContext, target: str, msg_id: int | None, button_id: int | None, origin: str | None
) -> Any:
    """The one `requestUrlAuth` with three addressing modes."""
    from telethon.tl.functions import messages as fn

    if target.startswith(("http://", "https://", "tg://")):
        return await client(ctx)(fn.RequestUrlAuthRequest(url=target, in_app_origin=origin))
    if msg_id is None or button_id is None:
        raise UsageError("addressing a button needs --msg-id and --button-id", field="msg_id")
    peer = await _send.resolve(ctx, _bots.peer_ref(target))
    return await client(ctx)(
        fn.RequestUrlAuthRequest(peer=peer, msg_id=int(msg_id), button_id=int(button_id))
    )


async def url_auth_get(ctx: OpContext, req: UrlAuthGetReq) -> UrlAuth:
    """Inspect a seamless-login request without accepting it.

    Three addressing modes reach one method: a keyboard button, a
    `url_auth_domains` URL, and an OAuth deep link with the origin it came
    from. Whichever it was, nothing is granted here — this command exists so
    the domain, the browser and the IP can be *read* before the decision.
    """
    from telethon.tl.functions import messages as fn

    result = await _url_auth_request(ctx, req.target, req.msg_id, req.button_id, req.in_app_origin)
    model = _url_auth_model(result)
    if req.check_code:
        if not (model.match_codes and model.match_codes_first):
            raise UsageError(
                "--check-code only applies when the request sets match_codes_first",
                field="check_code",
            )
        model.code_valid = bool(
            await client(ctx)(
                fn.CheckUrlAuthMatchCodeRequest(url=req.target, match_code=req.check_code)
            )
        )
    return model


SPEC_URL_AUTH_GET = OperationSpec(
    id="bot.url-auth.get",
    request=UrlAuthGetReq,
    response=UrlAuth,
    impl=url_auth_get,
    summary="Inspect a seamless-login request without accepting it",
    description=(
        "Telegram Login hands a website your identity. What it is about to "
        "hand over — the domain (or the verified app name), the browser, the "
        "platform, the IP and the region — is printed here first, and "
        "accepting is a separate command."
    ),
    aliases=("bot.url_auth.get", "bot.login-url.get", "link.auth", "auth.url-login"),
    mutating=True,
    tags=frozenset({"mutating-checked"}),
    columns=("result", "domain", "bot", "request_write_access"),
    headers=("Result", "Domain", "Bot", "Wants write"),
    example={"result": "request", "domain": "example.org", "bot": "examplebot"},
    example_args="bot url-auth get @examplebot --msg-id 12 --button-id 0",
    covers=(
        "auth.oauth-deep-link",
        "auth.url-auth-bot-button",
        "contacts-users.url-auth-login",
        "messages-core.url-authorization",
    ),
    covers_partial=(
        "bots.login-url-button",
        "bots.oauth-deeplink-login",
        "bots.url-auth-match-code",
        "bots.webapp-oauth-request",
    ),
    coverage_note=(
        "Inspecting is this command; granting is `bot url-auth accept` and "
        "refusing is `bot url-auth decline`."
    ),
)


class UrlAuthAcceptReq(Request):
    target: Annotated[
        str, arg(0, metavar="TARGET", help="Chat holding the button, or the OAuth deep link.")
    ]
    msg_id: Annotated[
        int | None, opt("--msg-id", metavar="ID", kind="msg_id", help="Message id for a button.")
    ] = None
    button_id: Annotated[
        int | None, opt("--button-id", metavar="N", help="Button id from the reply markup.")
    ] = None
    write_allowed: Annotated[
        bool, opt("--write-allowed", help="CONSENT: let the linked bot message me.")
    ] = False
    share_phone: Annotated[
        bool, opt("--share-phone", help="CONSENT: give the site my phone number.")
    ] = False
    match_code: Annotated[
        str | None, opt("--match-code", metavar="CODE", help="The emoji shown on the login page.")
    ] = None


async def url_auth_accept(ctx: OpContext, req: UrlAuthAcceptReq) -> UrlAuth:
    """Complete a seamless login and print the authorized URL.

    The request is inspected first, always: a match code that the server marks
    `match_codes_first` has to be verified *before* accepting, and both
    consent flags default off and are never inferred from the request having
    asked for them.
    """
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    inspected = _url_auth_model(
        await _url_auth_request(ctx, req.target, req.msg_id, req.button_id, None)
    )
    if inspected.match_codes and not req.match_code:
        raise UsageError(
            "this login shows an emoji match code; pass it with --match-code", field="match_code"
        )
    if inspected.match_codes_first and req.match_code:
        ok = bool(
            await handle(fn.CheckUrlAuthMatchCodeRequest(url=req.target, match_code=req.match_code))
        )
        if not ok:
            raise PermissionError_("the match code does not match; nothing was authorized")

    kwargs: dict[str, Any] = {
        "write_allowed": req.write_allowed or None,
        "share_phone_number": req.share_phone or None,
        "match_code": req.match_code,
    }
    if req.target.startswith(("http://", "https://", "tg://")):
        kwargs["url"] = req.target
    else:
        kwargs["peer"] = await _send.resolve(ctx, _bots.peer_ref(req.target))
        kwargs["msg_id"] = req.msg_id
        kwargs["button_id"] = req.button_id
    result = await handle(fn.AcceptUrlAuthRequest(**kwargs))
    model = _url_auth_model(result)
    model.domain = model.domain or inspected.domain
    model.bot = model.bot or inspected.bot
    model.write_allowed = req.write_allowed
    model.phone_shared = req.share_phone
    ctx.emit("bot_url_auth", {"domain": model.domain})
    return model


SPEC_URL_AUTH_ACCEPT = OperationSpec(
    id="bot.url-auth.accept",
    request=UrlAuthAcceptReq,
    response=UrlAuth,
    impl=url_auth_accept,
    summary="Complete a seamless login and print the authorized URL",
    description=(
        "Destructive in the sense that matters: it logs you into a "
        "third-party site under your Telegram identity, which cannot be taken "
        "back from here. `--write-allowed` and `--share-phone` default off."
    ),
    aliases=("bot.url_auth.accept",),
    mutating=True,
    destructive=True,
    columns=("result", "domain", "url"),
    headers=("Result", "Domain", "URL"),
    example={"result": "accepted", "url": "https://example.org/login?token=…"},
    example_args="bot url-auth accept @examplebot --msg-id 12 --button-id 0",
    covers=("bots.login-url-button", "bots.url-auth-match-code", "bots.webapp-oauth-request"),
    covers_partial=("bots.oauth-deeplink-login",),
    coverage_note="Refusing an OAuth deep link is `bot url-auth decline`.",
)


class UrlAuthDeclineReq(Request):
    url: Annotated[str, arg(0, metavar="URL", help="The OAuth deep link to decline.")]


async def url_auth_decline(ctx: OpContext, req: UrlAuthDeclineReq) -> UrlAuth:
    """Refuse a seamless-login request."""
    from telethon.tl.functions import messages as fn

    await client(ctx)(fn.DeclineUrlAuthRequest(url=req.url))
    return UrlAuth(result="declined", declined=True, url=req.url)


SPEC_URL_AUTH_DECLINE = OperationSpec(
    id="bot.url-auth.decline",
    request=UrlAuthDeclineReq,
    response=UrlAuth,
    impl=url_auth_decline,
    summary="Refuse a seamless-login request",
    aliases=("bot.url_auth.decline",),
    mutating=True,
    columns=("result", "declined"),
    headers=("Result", "Declined"),
    example={"result": "declined", "declined": True},
    example_args="bot url-auth decline tg://oauth?domain=example.org",
    covers=("bots.oauth-deeplink-login", "bots.url-auth-decline"),
)


# ---------------------------------------------------------------------------
# bot answer
# ---------------------------------------------------------------------------


class AnswerReq(Request):
    kind: Annotated[
        str,
        arg(
            0,
            metavar="KIND",
            help="callback|inline|shipping|precheckout|guest|webapp|webhook.",
        ),
    ]
    query_id: Annotated[str, arg(1, metavar="QUERY_ID", help="Query id being answered.")]
    text: Annotated[str | None, opt("--text", help="callback: toast or alert text.")] = None
    alert: Annotated[bool, opt("--alert", help="callback: show a modal alert.")] = False
    url: Annotated[str | None, opt("--url", metavar="URL", help="callback: deep link.")] = None
    cache_time: Annotated[
        int | None, opt("--cache-time", metavar="SECONDS", help="Seconds clients may cache it.")
    ] = None
    results: Annotated[
        str | None,
        opt("--results", metavar="PATH", kind="path", help="inline|guest|webapp: JSON results."),
    ] = None
    next_offset: Annotated[
        str | None, opt("--next-offset", metavar="TOKEN", help="inline: offset for the next page.")
    ] = None
    gallery: Annotated[bool, opt("--gallery", help="inline: render results as a grid.")] = False
    private: Annotated[bool, opt("--private", help="inline: cache per user.")] = False
    switch_pm: Annotated[
        str | None, opt("--switch-pm", metavar="TEXT:PARAM", help="inline: a button above them.")
    ] = None
    switch_webview: Annotated[
        str | None, opt("--switch-webview", metavar="TEXT:URL", help="inline: mini-app button.")
    ] = None
    options: Annotated[
        str | None,
        opt("--options", metavar="PATH", kind="path", help="shipping: JSON shipping options."),
    ] = None
    ok: Annotated[bool, opt("--ok", help="shipping|precheckout: accept.")] = False
    error: Annotated[
        str | None, opt("--error", metavar="TEXT", help="shipping|precheckout: rejection.")
    ] = None
    data: Annotated[
        str | None, opt("--data", metavar="JSON", kind="json", help="webhook: JSON payload.")
    ] = None


_ANSWER_FLAGS = {
    "callback": {"text", "alert", "url", "cache_time"},
    "inline": {
        "results",
        "next_offset",
        "gallery",
        "private",
        "switch_pm",
        "switch_webview",
        "cache_time",
    },
    "shipping": {"options", "ok", "error"},
    "precheckout": {"ok", "error"},
    "guest": {"results"},
    "webapp": {"results"},
    "webhook": {"data"},
}


async def answer(ctx: OpContext, req: AnswerReq) -> BotAnswer:
    """Answer one pending bot query.

    Seven query kinds, seven methods, one command — because a caller reading
    `bot query list` has one loop to write, not seven. A flag that belongs to
    another kind is a usage error rather than a silently ignored argument.

    Answering a pre-checkout query is not a payment: it approves or rejects one
    the buyer has already started, and refusing to do it would leave that buyer
    stuck.
    """
    from telethon.tl.functions import bots as bots_fn
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "answering a bot query")
    allowed = _ANSWER_FLAGS.get(req.kind)
    if allowed is None:
        raise UsageError(f"{req.kind!r} is not a query kind", field="kind")
    supplied = {
        name
        for name in set().union(*_ANSWER_FLAGS.values())
        if getattr(req, name, None) not in (None, False)
    }
    stray = sorted(supplied - allowed)
    if stray:
        raise UsageError(
            f"{', '.join('--' + s.replace('_', '-') for s in stray)} "
            f"does not belong to a {req.kind} answer",
            field=stray[0],
        )

    handle = client(ctx)
    query_id = _query_id(req.query_id)
    if req.kind == "callback":
        await handle(
            fn.SetBotCallbackAnswerRequest(
                query_id=query_id,
                cache_time=int(req.cache_time or 0),
                alert=req.alert or None,
                message=req.text,
                url=req.url,
            )
        )
    elif req.kind == "inline":
        await handle(
            fn.SetInlineBotResultsRequest(
                query_id=query_id,
                results=_inline_results(req.results),
                cache_time=int(req.cache_time or 0),
                gallery=req.gallery or None,
                private=req.private or None,
                next_offset=req.next_offset,
                switch_pm=_switch_pm(req.switch_pm),
                switch_webview=_switch_webview(req.switch_webview),
            )
        )
    elif req.kind == "shipping":
        await handle(
            fn.SetBotShippingResultsRequest(
                query_id=query_id,
                error=req.error,
                shipping_options=_shipping_options(req.options),
            )
        )
    elif req.kind == "precheckout":
        await handle(
            fn.SetBotPrecheckoutResultsRequest(
                query_id=query_id, success=req.ok or None, error=req.error
            )
        )
    elif req.kind == "guest":
        await handle(
            fn.SetBotGuestChatResultRequest(
                query_id=query_id, result=_inline_results(req.results)[0]
            )
        )
    elif req.kind == "webapp":
        await handle(
            fn.SendWebViewResultMessageRequest(
                bot_query_id=str(req.query_id), result=_inline_results(req.results)[0]
            )
        )
    else:
        payload = _bots.data_json(req.data, field="data")
        if payload is None:
            raise UsageError("a webhook answer needs --data", field="data")
        await handle(bots_fn.AnswerWebhookJSONQueryRequest(query_id=query_id, data=payload))

    return BotAnswer(query_id=str(req.query_id), kind=req.kind, answered=True)


def _query_id(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise UsageError(
            "query-id must be the numeric id from `bot query list`", field="query_id"
        ) from exc


def _switch_pm(value: str | None) -> Any:
    if not value:
        return None
    from telethon.tl import types

    text, _, param = value.partition(":")
    return types.InlineBotSwitchPM(text=text, start_param=param)


def _switch_webview(value: str | None) -> Any:
    if not value:
        return None
    from telethon.tl import types

    text, _, url = value.partition(":")
    return types.InlineBotWebView(text=text, url=url)


def _shipping_options(path: str | None) -> Any:
    if not path:
        return None
    from telethon.tl import types

    loaded = _bots.load_json(path, field="options")
    return [
        types.ShippingOption(
            id=str(entry.get("id", "")),
            title=str(entry.get("title", "")),
            prices=[
                types.LabeledPrice(label=str(p.get("label", "")), amount=int(p.get("amount", 0)))
                for p in entry.get("prices", [])
            ],
        )
        for entry in loaded or []
    ]


def _inline_results(path: str | None) -> list[Any]:
    """The `--results` JSON file as `InputBotInlineResult` objects."""
    from telethon.tl import types

    if not path:
        raise UsageError("this answer needs --results", field="results")
    loaded = _bots.load_json(path, field="results")
    if isinstance(loaded, dict):
        loaded = [loaded]
    if not loaded:
        raise UsageError("--results: the file holds no results", field="results")
    out: list[Any] = []
    for entry in loaded:
        message = entry.get("message") or {}
        out.append(
            types.InputBotInlineResult(
                id=str(entry.get("id", "")),
                type=str(entry.get("type", "article")),
                send_message=types.InputBotInlineMessageText(
                    message=str(message.get("text", "")),
                    no_webpage=bool(message.get("no_preview")) or None,
                    reply_markup=_bots.keyboard_tl(message.get("reply_markup"), field="results"),
                ),
                title=entry.get("title"),
                description=entry.get("description"),
                url=entry.get("url"),
            )
        )
    return out


SPEC_ANSWER = OperationSpec(
    id="bot.answer",
    request=AnswerReq,
    response=BotAnswer,
    impl=answer,
    summary="Answer a pending bot query",
    tags=frozenset({"visible-to-others"}),
    description=(
        "Callback, inline, shipping, pre-checkout, guest, mini-app and "
        "webhook queries, one flag set per kind. Answering a pre-checkout "
        "query approves or rejects a payment the buyer already started, which "
        "is why it is here and `payments.sendPaymentForm` is not."
    ),
    mutating=True,
    rate_class="send",
    columns=("query_id", "kind", "answered"),
    headers=("Query", "Kind", "Answered"),
    example={"query_id": "123456", "kind": "callback", "answered": True},
    example_args="bot answer callback 123456 --text Saved",
    covers=(
        "bots.answer-callback-query",
        "bots.answer-inline-query",
        "bots.answer-precheckout-query",
        "bots.answer-shipping-query",
        "bots.guest-mode-answer",
        "bots.send-webview-result-message",
    ),
    covers_partial=("bots.send-custom-request",),
    coverage_note="An arbitrary Bot-API method is `bot api send`.",
)


# ---------------------------------------------------------------------------
# bot query list
# ---------------------------------------------------------------------------


class QueryListReq(Request):
    kind: Annotated[
        str | None,
        choice(
            "callback",
            "inline",
            "inline-send",
            "shipping",
            "precheckout",
            "guest",
            "webapp",
            "webhook",
            help="Filter by query kind.",
        ),
    ] = None
    since: Annotated[
        str | None, opt("--since", metavar="WHEN", kind="datetime", help="Only newer than this.")
    ] = None
    resolve_message: Annotated[
        bool, opt("--resolve-message", help="Also fetch a callback's source message.")
    ] = True


async def query_list(ctx: OpContext, req: QueryListReq) -> Page[BotQuery]:
    """The bot queries the daemon is holding.

    The buffer is filled by `watch --bot-updates`, which belongs to the
    updates group; until that is running this is an empty page rather than an
    error, because "no queries" and "nobody is listening" look the same from
    here and the honest answer is the empty one plus a warning.
    """
    await _bots.require_bot_session(ctx, "listing bot queries")
    limit, _state = window(ctx, "bot.query.list", PageKind.LOCAL, default=50)
    buffer = getattr(getattr(ctx, "daemon", None), "bot_queries", None)
    if buffer is None:
        ctx.warn(
            "the daemon is not buffering bot updates; start one with "
            "`tlgr watch --bot-updates` to fill this list"
        )
        return Page(items=[], has_more=False, total=0)

    since = _parse_since(req.since)
    items: list[BotQuery] = []
    for entry in list(buffer)[:limit]:
        row = _query_row(entry)
        if req.kind and row.kind != req.kind:
            continue
        if since and (row.expires_at or "") < since:
            continue
        items.append(row)
    return Page(items=items, has_more=False, total=len(items))


def _parse_since(value: str | None) -> str:
    if not value:
        return ""
    from tlgr.core.timefmt import parse_dt

    parsed = parse_dt(value)
    return fmt_dt(parsed) or ""


def _query_row(entry: Any) -> BotQuery:
    data = entry if isinstance(entry, dict) else {}
    return BotQuery(
        query_id=str(data.get("query_id", "")),
        kind=str(data.get("kind", "")),
        user_id=data.get("user_id"),
        peer_id=data.get("peer_id"),
        msg_id=data.get("msg_id"),
        inline_msg_id=data.get("inline_msg_id"),
        data=data.get("data"),
        query=data.get("query"),
        payload=data.get("payload"),
        answered=bool(data.get("answered")),
        expires_at=data.get("expires_at"),
        message=data.get("message"),
    )


SPEC_QUERY_LIST = OperationSpec(
    id="bot.query.list",
    request=QueryListReq,
    response=Page[BotQuery],
    impl=query_list,
    summary="List the bot queries the daemon is holding",
    description=(
        "An inline callback carries an `InputBotInlineMessageID` rather than a "
        "message id and cannot be fetched at all, so its `message` is null "
        "rather than missing."
    ),
    paginated=PageKind.LOCAL,
    columns=("query_id", "kind", "user_id", "answered"),
    headers=("Query", "Kind", "User", "Answered"),
    example={"items": [{"query_id": "123456", "kind": "callback"}], "has_more": False},
    example_args="bot query list --kind callback",
    covers=("bots.callback-query-message-get",),
)


# ---------------------------------------------------------------------------
# bot api send / connection
# ---------------------------------------------------------------------------


class ApiSendReq(Request):
    method: Annotated[str, arg(0, metavar="METHOD", help="Bot-API method name.")]
    params: Annotated[
        str, opt("--params", metavar="JSON", kind="json", help="JSON parameters.")
    ] = "{}"


async def api_send(ctx: OpContext, req: ApiSendReq) -> BotApiResult:
    """Call an arbitrary HTTP Bot-API method over MTProto.

    The escape hatch for the Bot-API surface tlgr has not modelled. The reply
    is an opaque `DataJSON` and is passed through verbatim: parsing it would
    be inventing a schema for a method tlgr does not know.
    """
    from telethon.tl.functions import bots as fn

    await _bots.require_bot_session(ctx, "bot api send")
    payload = _bots.data_json(req.params, field="params")
    result = await client(ctx)(
        fn.SendCustomRequestRequest(custom_method=req.method, params=payload)
    )
    return BotApiResult(method=req.method, result=_data_json(result))


def _data_json(result: Any) -> Any:
    import json

    text = getattr(result, "data", None)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


SPEC_API_SEND = OperationSpec(
    id="bot.api.send",
    request=ApiSendReq,
    response=BotApiResult,
    impl=api_send,
    summary="Call an arbitrary Bot-API method through the MTProto session",
    mutating=True,
    columns=("method",),
    headers=("Method",),
    example={"method": "getMe", "result": {"id": 93372553}},
    example_args='bot api send getMe --params "{}"',
    covers=("bots.send-custom-request",),
)


class ConnectionGetReq(Request):
    connection_id: Annotated[str, arg(0, metavar="CONNECTION_ID", help="Business connection id.")]


async def connection_get(ctx: OpContext, req: ConnectionGetReq) -> BusinessConnection:
    """A business connection my bot acts through.

    The `dc_id` is not decoration: every wrapped call has to be sent *there*,
    which is why it is reported rather than hidden inside the wrapper.
    """
    from telethon.tl.functions import account as fn

    await _bots.require_bot_session(ctx, "reading a business connection")
    result = await client(ctx)(fn.GetBotBusinessConnectionRequest(connection_id=req.connection_id))
    connection = None
    for update in getattr(result, "updates", None) or []:
        connection = getattr(update, "connection", None) or connection
    date = getattr(connection, "date", None)
    return BusinessConnection(
        connection_id=str(getattr(connection, "connection_id", req.connection_id)),
        user_id=getattr(connection, "user_id", None),
        dc_id=getattr(connection, "dc_id", None),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        rights=_business_rights(getattr(connection, "rights", None)),
        disabled=bool(getattr(connection, "disabled", False)),
    )


SPEC_CONNECTION_GET = OperationSpec(
    id="bot.connection.get",
    request=ConnectionGetReq,
    response=BusinessConnection,
    impl=connection_get,
    summary="Show a business connection my bot is acting through",
    columns=("connection_id", "user_id", "dc_id", "disabled"),
    headers=("Connection", "User", "DC", "Disabled"),
    example={"connection_id": "abc123", "user_id": 4242, "dc_id": 2},
    example_args="bot connection get abc123",
    covers=("bots.business-connection-info",),
)


class ConnectionInvokeReq(Request):
    connection_id: Annotated[str, arg(0, metavar="CONNECTION_ID", help="Business connection id.")]
    command: Annotated[
        list[str],
        arg(1, metavar="COMMAND", variadic=True, help="The tlgr command to wrap."),
    ] = []


async def connection_invoke(ctx: OpContext, req: ConnectionInvokeReq) -> BusinessConnection:
    """Run another tlgr command on behalf of a business account.

    Wrapping is not a flag on a request: the wrapped query must be sent to the
    connection's own DC through an exported sender, which is why the
    connection is fetched first and the DC reported back.

    The wrapper is also available inline as `--business-connection` on
    `bot command send`, `bot press` and `inline send`; this command exists for
    the operations that do not carry the flag yet.
    """
    if not req.command:
        raise UsageError("give a tlgr command to wrap", field="command")
    raise _bots.unsupported(
        "bot connection invoke",
        "wrapping an arbitrary tlgr operation needs the daemon's own dispatcher, "
        "which `ops/` may not import (§2.2); use the --business-connection flag on "
        "bot command send, bot press or inline send instead",
    )


SPEC_CONNECTION_INVOKE = OperationSpec(
    id="bot.connection.invoke",
    request=ConnectionInvokeReq,
    response=BusinessConnection,
    impl=connection_invoke,
    summary="Run another tlgr command on behalf of a business account",
    description=(
        "Registered and refused with exit 13 rather than left out: the "
        "wrapper itself works and is reachable as `--business-connection` on "
        "the commands that carry it, but re-entering the dispatcher from "
        "inside an operation would break the layering rule that keeps `ops/` "
        "importable without the daemon."
    ),
    mutating=True,
    columns=("connection_id",),
    headers=("Connection",),
    example={"connection_id": "abc123"},
    example_args="bot connection invoke abc123 message send @alice hi",
    covers_partial=("bots.business-invoke-with-connection", "updates.invoke-business-connection"),
    coverage_note=(
        "The wrapper is implemented on `bot command send`, `bot press` and "
        "`inline send`; wrapping an arbitrary command is refused with exit 13."
    ),
)


# ---------------------------------------------------------------------------
# bot stream send
# ---------------------------------------------------------------------------


class StreamSendReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Destination chat.")]
    draft_id: Annotated[int, opt("--draft-id", metavar="ID", help="Draft random_id.")] = 0
    text: Annotated[str | None, opt("--text", help="Next text chunk.")] = None
    rich_file: Annotated[
        str | None, opt("--rich-file", metavar="PATH", kind="path", help="Next chunk, rich.")
    ] = None
    file: Annotated[
        str | None,
        opt("--file", metavar="PATH", kind="path", help="Read chunks from a file, one per line."),
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Forum topic id.")
    ] = None
    can_stop: Annotated[bool, opt("--can-stop", help="Let the user stop the generation.")] = False
    keep_on_stop: Annotated[
        bool, opt("--keep-on-stop", help="Keep the partial answer if the user stops it.")
    ] = False
    stop: Annotated[bool, opt("--stop", help="End the stream.")] = False


async def stream_send(ctx: OpContext, req: StreamSendReq) -> StreamProgress:
    """Stream a live draft — an answer being generated — into a chat.

    The server allows 20 calls per 5 s and 40 per 30 s *per peer* and answers
    a burst with a one-to-three second FloodWait. Retrying that is the wrong
    shape: the chunks would arrive late and out of order. tlgr paces itself
    through the session limiter and coalesces what it cannot send in time.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "streaming a live draft")
    if req.can_stop or req.keep_on_stop or req.stop:
        _bots.unsupported("--can-stop/--keep-on-stop/--stop")
    if not req.draft_id:
        raise UsageError("--draft-id is required; it is what keys the stream", field="draft_id")

    chunks = _stream_chunks(req)
    if not chunks:
        raise UsageError("give --text, --rich-file or --file", field="text")

    peer = await _send.resolve(ctx, req.chat)
    handle = client(ctx)
    limiter = getattr(ctx, "limiter", None)
    for chunk in chunks:
        action = types.SendMessageTextDraftAction(
            text=types.TextWithEntities(text=chunk, entities=[]), random_id=req.draft_id
        )
        await handle(fn.SetTypingRequest(peer=peer, action=action, top_msg_id=req.topic))
        if limiter is not None:
            await limiter.acquire("send")
    return StreamProgress(
        chat_id=_send.peer_id_of(peer), draft_id=req.draft_id, chunks_sent=len(chunks)
    )


def _stream_chunks(req: StreamSendReq) -> list[str]:
    import os
    from pathlib import Path

    if req.text:
        return [req.text]
    source = req.rich_file or req.file
    if not source:
        return []
    path = Path(os.path.expanduser(source))
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"--file: {exc.strerror or exc}", field="file") from exc
    if req.rich_file:
        return [body]
    return [line for line in body.splitlines() if line.strip()]


SPEC_STREAM_SEND = OperationSpec(
    id="bot.stream.send",
    request=StreamSendReq,
    response=StreamProgress,
    impl=stream_send,
    summary="Stream a live draft into a chat",
    tags=frozenset({"visible-to-others"}),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "draft_id", "chunks_sent"),
    headers=("Chat", "Draft", "Chunks"),
    example={"chat_id": 4242, "draft_id": 99, "chunks_sent": 3},
    example_args="bot stream send @alice --draft-id 99 --text Thinking…",
    covers=("bots.ai-live-draft-streaming", "bots.rich-message-draft-stream"),
)


# ---------------------------------------------------------------------------
# bot create / edit / username / token
# ---------------------------------------------------------------------------


class CreateReq(Request):
    name: Annotated[str, opt("--name", help="Display name.")] = ""
    username: Annotated[str, opt("--username", help="Username (must end in 'bot').")] = ""
    manager: Annotated[
        PeerRef | None,
        opt("--manager", metavar="USER", kind="user", help="Manager bot that owns the token."),
    ] = None
    about: Annotated[str | None, opt("--about", help="Short about text.")] = None
    check_only: Annotated[
        bool, opt("--check-only", help="Only report whether the username is free.")
    ] = False


async def create(ctx: OpContext, req: CreateReq) -> BotCreated:
    """Create a managed bot without going through @BotFather.

    The username is checked first, always: `bots.createBot` consumes one of a
    small per-account quota, and burning one on a name that was never free is
    not recoverable.
    """
    from telethon.tl import types
    from telethon.tl.functions import bots as fn

    if not req.name or not req.username:
        raise UsageError("--name and --username are both required", field="username")

    handle = client(ctx)
    free = bool(await handle(fn.CheckUsernameRequest(username=req.username)))
    if not free:
        raise UsageError(f"@{req.username} is not available", field="username")
    if req.check_only:
        return BotCreated(username=req.username, token_available=False)

    manager = (
        await _bots.input_user(ctx, req.manager, field="manager")
        if req.manager is not None
        else types.InputUserSelf()
    )
    result = await handle(
        fn.CreateBotRequest(name=req.name, username=req.username, manager_id=manager)
    )
    users = getattr(result, "users", None) or []
    bot_id = int(getattr(users[0], "id", 0) or 0) if users else 0
    if req.about:
        await handle(
            fn.SetBotInfoRequest(
                lang_code="",
                bot=types.InputUser(
                    user_id=bot_id, access_hash=int(getattr(users[0], "access_hash", 0) or 0)
                ),
                about=req.about,
            )
        )
    ctx.emit("bot_create", {"bot_id": bot_id, "username": req.username})
    return BotCreated(
        bot_id=bot_id,
        username=req.username,
        manager=_send.peer_id_of(manager) if req.manager is not None else None,
        token_available=True,
    )


SPEC_CREATE = OperationSpec(
    id="bot.create",
    request=CreateReq,
    response=BotCreated,
    impl=create,
    summary="Create a managed bot without BotFather",
    description=(
        "A managed bot's token is exported with `bot token export`, which is "
        "what makes this worth having: the whole lifecycle stays in one tool."
    ),
    mutating=True,
    columns=("bot_id", "username", "token_available"),
    headers=("Bot", "Username", "Token"),
    example={"bot_id": 5000001, "username": "my_helper_bot", "token_available": True},
    example_args="bot create --name Helper --username my_helper_bot",
    covers=("bots.create-managed-bot",),
)


class EditReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]
    name: Annotated[str | None, opt("--name", help="Display name.")] = None
    about: Annotated[str | None, opt("--about", help="Short about text (profile).")] = None
    description: Annotated[
        str | None, opt("--description", help="Long description shown in an empty chat.")
    ] = None
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language these values apply to.")] = ""
    photo: Annotated[
        str | None, opt("--photo", metavar="PATH", kind="path", help="Profile photo or video.")
    ] = None
    video: Annotated[bool, opt("--video", help="Treat --photo as a video.")] = False
    video_start: Annotated[
        float | None, opt("--video-start", metavar="SECONDS", help="Video cover timestamp.")
    ] = None
    remove_photo: Annotated[
        bool, opt("--remove-photo", help="Delete the current profile photo.")
    ] = False


async def edit(ctx: OpContext, req: EditReq) -> BotEdited:
    """Edit my bot's name, about text, description and profile photo.

    `bot=` is what makes this the *owner* side of `bots.setBotInfo`; omitting
    it would edit the calling account instead, which is a bug you only notice
    after your own profile changed.
    """
    from telethon.tl.functions import bots as fn
    from telethon.tl.functions import photos as photos_fn

    bot = await _bots.input_user(ctx, req.bot)
    peer = await _send.resolve(ctx, req.bot)
    handle = client(ctx)
    bot_id = _send.peer_id_of(peer)

    if req.name or req.about or req.description:
        await handle(
            fn.SetBotInfoRequest(
                lang_code=req.lang,
                bot=bot,
                name=req.name,
                about=req.about,
                description=req.description,
            )
        )

    photo_id: int | None = None
    if req.remove_photo:
        full, _user = await _full(ctx, peer)
        current = getattr(full, "profile_photo", None)
        if current is not None:
            from telethon.tl import types

            await handle(
                photos_fn.DeletePhotosRequest(
                    id=[
                        types.InputPhoto(
                            id=int(getattr(current, "id", 0) or 0),
                            access_hash=int(getattr(current, "access_hash", 0) or 0),
                            file_reference=getattr(current, "file_reference", b"") or b"",
                        )
                    ]
                )
            )
    elif req.photo:
        import os
        from pathlib import Path

        upload = getattr(ctx, "upload_file", None)
        if upload is None:  # pragma: no cover - the daemon always supplies one
            raise UsageError("this context cannot upload files")
        path = Path(os.path.expanduser(req.photo))
        if not path.exists():
            raise UsageError(f"--photo: {path} does not exist", field="photo")
        handle_file = await upload(path)
        result = await handle(
            photos_fn.UploadProfilePhotoRequest(
                bot=bot,
                file=None if req.video else handle_file,
                video=handle_file if req.video else None,
                video_start_ts=req.video_start,
            )
        )
        photo_id = _id_of(getattr(result, "photo", None))

    ctx.emit("bot_edit", {"bot_id": bot_id})
    return BotEdited(
        bot_id=bot_id,
        name=req.name,
        about=req.about,
        description=req.description,
        lang=req.lang or None,
        photo_id=photo_id,
    )


SPEC_EDIT = OperationSpec(
    id="bot.edit",
    request=EditReq,
    response=BotEdited,
    impl=edit,
    summary="Edit my bot's name, about text, description and photo",
    mutating=True,
    rate_class="file",
    columns=("bot_id", "name", "lang"),
    headers=("Bot", "Name", "Lang"),
    example={"bot_id": 5000001, "name": "Helper", "lang": "en"},
    example_args="bot edit @my_helper_bot --name Helper",
    covers=(
        "bot.profile-photo-set",
        "bots.bot-forums",
        "bots.set-bot-info",
        "bots.set-bot-photo",
    ),
)


class UsernameCheckReq(Request):
    username: Annotated[str, arg(0, metavar="USERNAME", help="Candidate username.")]


async def username_check(ctx: OpContext, req: UsernameCheckReq) -> BotUsernameCheck:
    """Is a bot username free?"""
    from telethon.tl.functions import bots as fn

    try:
        free = bool(await client(ctx)(fn.CheckUsernameRequest(username=req.username)))
    except Exception as exc:  # the server's reason IS the answer here
        name = type(exc).__name__.upper()
        if "USERNAME" not in name:
            raise
        return BotUsernameCheck(username=req.username, available=False, reason=type(exc).__name__)
    return BotUsernameCheck(username=req.username, available=free)


SPEC_USERNAME_CHECK = OperationSpec(
    id="bot.username.check",
    request=UsernameCheckReq,
    response=BotUsernameCheck,
    impl=username_check,
    summary="Check whether a bot username is available",
    columns=("username", "available", "reason"),
    headers=("Username", "Free", "Reason"),
    example={"username": "my_helper_bot", "available": True},
    example_args="bot username check my_helper_bot",
    covers=("bots.check-bot-username",),
)


class UsernameSetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]
    enable: Annotated[
        list[str], opt("--enable", metavar="NAME", help="Usernames to activate.")
    ] = []
    disable: Annotated[
        list[str], opt("--disable", metavar="NAME", help="Usernames to deactivate.")
    ] = []
    order: Annotated[str | None, opt("--order", metavar="A,B,C", help="New display order.")] = None


async def username_set(ctx: OpContext, req: UsernameSetReq) -> BotUsernames:
    """Enable, disable and reorder my bot's public usernames."""
    from telethon.tl.functions import bots as fn

    bot = await _bots.input_user(ctx, req.bot)
    handle = client(ctx)
    for name in req.enable:
        await handle(fn.ToggleUsernameRequest(bot=bot, username=name.lstrip("@"), active=True))
    for name in req.disable:
        await handle(fn.ToggleUsernameRequest(bot=bot, username=name.lstrip("@"), active=False))
    if req.order:
        order = [n.strip().lstrip("@") for n in req.order.split(",") if n.strip()]
        await handle(fn.ReorderUsernamesRequest(bot=bot, order=order))

    peer = await _send.resolve(ctx, req.bot)
    _full_user, user = await _full(ctx, peer)
    return BotUsernames(bot_id=_send.peer_id_of(peer), usernames=_usernames(user))


SPEC_USERNAME_SET = OperationSpec(
    id="bot.username.set",
    request=UsernameSetReq,
    response=BotUsernames,
    impl=username_set,
    summary="Enable, disable and reorder my bot's usernames",
    mutating=True,
    columns=("bot_id", "usernames"),
    headers=("Bot", "Usernames"),
    example={"bot_id": 5000001, "usernames": ["my_helper_bot"]},
    example_args="bot username set @my_helper_bot --enable my_helper_bot",
    covers=("bots.bot-usernames",),
)


class TokenExportReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The managed bot.")]
    revoke: Annotated[
        bool, opt("--revoke", help="Invalidate the old token and issue a new one.")
    ] = False
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Write it here, mode 0600.")
    ] = None
    show: Annotated[bool, opt("--show", help="Print the token; it is redacted by default.")] = False


async def token_export(ctx: OpContext, req: TokenExportReq) -> BotToken:
    """Export (or revoke and re-export) a managed bot's API token.

    The returned string is a full credential: anyone holding it *is* the bot.
    It is therefore not printed unless `--show` or `--out` says so, and
    `--out` writes with mode 0600 rather than leaving it in shell history.
    """
    import os
    from pathlib import Path

    from telethon.tl.functions import bots as fn

    bot = await _bots.input_user(ctx, req.bot)
    peer = await _send.resolve(ctx, req.bot)
    exported = await client(ctx)(fn.ExportBotTokenRequest(bot=bot, revoke=req.revoke))
    token = str(getattr(exported, "token", "") or "")
    result = BotToken(bot_id=_send.peer_id_of(peer), revoked=req.revoke)

    if req.out:
        path = Path(os.path.expanduser(req.out))
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, token.encode())
        finally:
            os.close(descriptor)
        result.path = str(path)
    if req.show:
        result.token = token
    elif not req.out:
        ctx.warn("the token is redacted; pass --show to print it or --out to write it to a file")
    return result


SPEC_TOKEN_EXPORT = OperationSpec(
    id="bot.token.export",
    request=TokenExportReq,
    response=BotToken,
    impl=token_export,
    summary="Export a managed bot's API token",
    description=(
        "`--revoke` breaks every deployment still using the old token, which "
        "is why it is confirmed like a deletion."
    ),
    mutating=True,
    destructive=True,
    columns=("bot_id", "revoked", "path"),
    headers=("Bot", "Revoked", "Path"),
    example={"bot_id": 5000001, "revoked": False},
    example_args="bot token export @my_helper_bot --out ./token",
    covers=("bots.managed-bot-token",),
)


# ---------------------------------------------------------------------------
# bot access get / set
# ---------------------------------------------------------------------------


class AccessGetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The managed bot.")]


async def access_get(ctx: OpContext, req: AccessGetReq) -> BotAccess:
    """Who is allowed to use a managed bot."""
    from telethon.tl.functions import bots as fn

    settings = await client(ctx)(
        fn.GetAccessSettingsRequest(bot=await _bots.input_user(ctx, req.bot))
    )
    return _access(settings)


SPEC_ACCESS_GET = OperationSpec(
    id="bot.access.get",
    request=AccessGetReq,
    response=BotAccess,
    impl=access_get,
    summary="Show who may use a managed bot",
    columns=("restricted", "allowed_users", "allowed_chats"),
    headers=("Restricted", "Users", "Chats"),
    example={"restricted": True, "allowed_users": [4242], "allowed_chats": []},
    example_args="bot access get @my_helper_bot",
    covers_partial=("bots.managed-bot-access-settings",),
    coverage_note="Changing the list is `bot access set`.",
)


class AccessSetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The managed bot.")]
    restricted: Annotated[
        bool, opt("--restricted", help="Only the listed peers may use the bot.")
    ] = False
    open_to_all: Annotated[bool, opt("--open", help="Anyone may use the bot.")] = False
    add: Annotated[
        list[PeerRef], opt("--add", metavar="USER", kind="user", help="Peers to allow.")
    ] = []
    remove: Annotated[
        list[PeerRef], opt("--remove", metavar="USER", kind="user", help="Peers to disallow.")
    ] = []


async def access_set(ctx: OpContext, req: AccessSetReq) -> BotAccess:
    """Restrict or open who may use a managed bot.

    `bots.editAccessSettings` takes the whole allow-list, not a delta, so
    `--add`/`--remove` are applied to the list the server currently holds
    rather than replacing it — otherwise adding one user would silently drop
    everybody else.
    """
    from telethon.tl.functions import bots as fn

    if req.restricted and req.open_to_all:
        raise UsageError("--restricted and --open contradict each other", field="restricted")

    bot = await _bots.input_user(ctx, req.bot)
    handle = client(ctx)
    current = _access(await handle(fn.GetAccessSettingsRequest(bot=bot)))

    allowed = list(current.allowed_users)
    for ref in req.add:
        peer_id = _send.peer_id_of(await _send.resolve(ctx, ref))
        if peer_id not in allowed:
            allowed.append(peer_id)
    for ref in req.remove:
        peer_id = _send.peer_id_of(await _send.resolve(ctx, ref))
        allowed = [entry for entry in allowed if entry != peer_id]

    users = [
        await _bots.input_user(ctx, _bots.peer_ref(str(peer_id)), field="add")
        for peer_id in allowed
    ]
    restricted = True if req.restricted else False if req.open_to_all else current.restricted
    await handle(
        fn.EditAccessSettingsRequest(
            bot=bot, restricted=restricted or None, add_users=users or None
        )
    )
    return BotAccess(
        restricted=restricted, allowed_users=allowed, allowed_chats=current.allowed_chats
    )


SPEC_ACCESS_SET = OperationSpec(
    id="bot.access.set",
    request=AccessSetReq,
    response=BotAccess,
    impl=access_set,
    summary="Restrict or open who may use a managed bot",
    mutating=True,
    columns=("restricted", "allowed_users"),
    headers=("Restricted", "Users"),
    example={"restricted": True, "allowed_users": [4242]},
    example_args="bot access set @my_helper_bot --restricted --add @alice",
    covers=("bots.managed-bot-access-settings",),
)


# ---------------------------------------------------------------------------
# bot default-rights set
# ---------------------------------------------------------------------------


class DefaultRightsReq(Request):
    group: Annotated[
        str | None, opt("--group", metavar="RIGHTS", help="'+'-joined rights for groups.")
    ] = None
    channel: Annotated[
        str | None, opt("--channel", metavar="RIGHTS", help="'+'-joined rights for channels.")
    ] = None


async def default_rights_set(ctx: OpContext, req: DefaultRightsReq) -> DefaultRights:
    """The admin rights clients pre-tick when my bot is added somewhere.

    A suggestion, not a grant: the person adding the bot still confirms it.
    Reading them back is `bot get`.
    """
    from telethon.tl.functions import bots as fn

    await _bots.require_bot_session(ctx, "setting suggested admin rights")
    if not req.group and not req.channel:
        raise UsageError("give --group and/or --channel", field="group")

    handle = client(ctx)
    result = DefaultRights()
    if req.group:
        rights = _bots.admin_rights(req.group, field="group")
        await handle(fn.SetBotGroupDefaultAdminRightsRequest(admin_rights=rights))
        result.group_rights = _bots.rights_keywords(rights)
    if req.channel:
        rights = _bots.admin_rights(req.channel, field="channel")
        await handle(fn.SetBotBroadcastDefaultAdminRightsRequest(admin_rights=rights))
        result.channel_rights = _bots.rights_keywords(rights)
    return result


SPEC_DEFAULT_RIGHTS_SET = OperationSpec(
    id="bot.default-rights.set",
    request=DefaultRightsReq,
    response=DefaultRights,
    impl=default_rights_set,
    summary="Set the admin rights clients pre-tick for my bot",
    aliases=("bot.default_rights.set",),
    mutating=True,
    columns=("group_rights", "channel_rights"),
    headers=("Group", "Channel"),
    example={"group_rights": ["delete_messages"], "channel_rights": []},
    example_args="bot default-rights set --group delete_messages+invite_users",
    covers=("bots.suggested-admin-rights",),
)


# ---------------------------------------------------------------------------
# bot verification get / set
# ---------------------------------------------------------------------------


class VerificationGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="User, bot or channel.")]


async def verification_get(ctx: OpContext, req: VerificationGetReq) -> BotVerification:
    """A peer's third-party verification badge.

    Distinct from Telegram's own blue check: a bot-issued badge says a company
    vouches for this peer, which is a different claim, so both are reported.
    """
    from telethon.tl.functions import channels as channels_fn

    peer = await _send.resolve(ctx, req.chat)
    if type(peer).__name__ == "InputPeerChannel":
        result = await client(ctx)(channels_fn.GetFullChannelRequest(channel=_input_channel(peer)))
        full = getattr(result, "full_chat", None)
        chats = {int(getattr(c, "id", 0)): c for c in (getattr(result, "chats", None) or [])}
        entity = chats.get(int(getattr(full, "id", 0) or 0))
    else:
        full, entity = await _full(ctx, peer)
    return _verification(full, entity) or BotVerification()


SPEC_VERIFICATION_GET = OperationSpec(
    id="bot.verification.get",
    request=VerificationGetReq,
    response=BotVerification,
    impl=verification_get,
    summary="Show a peer's third-party verification badge",
    columns=("verified_by_bot", "description", "telegram_verified"),
    headers=("By bot", "Description", "Telegram"),
    example={"verified_by_bot": 5000001, "description": "Verified merchant"},
    example_args="bot verification get @alice",
    covers=("bots.bot-verification-view",),
)


class VerificationSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Peer to verify.")]
    bot: Annotated[
        PeerRef | None, opt("--bot", metavar="BOT", kind="user", help="My verifier bot.")
    ] = None
    description: Annotated[str | None, opt("--description", help="Custom badge description.")] = (
        None
    )
    remove: Annotated[bool, opt("--remove", help="Remove the verification.")] = False


async def verification_set(ctx: OpContext, req: VerificationSetReq) -> BotVerified:
    """Verify or unverify a peer with my verifier bot."""
    from telethon.tl.functions import bots as fn

    if req.bot is None:
        raise UsageError("--bot names the verifier bot and is required", field="bot")
    peer = await _send.resolve(ctx, req.chat)
    await client(ctx)(
        fn.SetCustomVerificationRequest(
            peer=peer,
            enabled=None if req.remove else True,
            bot=await _bots.input_user(ctx, req.bot),
            custom_description=req.description,
        )
    )
    return BotVerified(
        peer_id=_send.peer_id_of(peer),
        verified=not req.remove,
        description=req.description,
    )


SPEC_VERIFICATION_SET = OperationSpec(
    id="bot.verification.set",
    request=VerificationSetReq,
    response=BotVerified,
    impl=verification_set,
    summary="Verify or unverify a peer with my verifier bot",
    tags=frozenset({"visible-to-others"}),
    mutating=True,
    destructive=True,
    columns=("peer_id", "verified", "description"),
    headers=("Peer", "Verified", "Description"),
    example={"peer_id": 4242, "verified": True},
    example_args="bot verification set @alice --bot @my_verifier_bot",
    covers=("bots.bot-verification-set",),
)


# ---------------------------------------------------------------------------
# bot preview list / add / edit / delete
# ---------------------------------------------------------------------------


class PreviewListReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot.")]
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language code.")] = ""
    owner: Annotated[bool, opt("--owner", help="Owner view, including per-language sets.")] = False


def _preview(media: Any, index: int, lang: str | None) -> PreviewMedia:
    inner = getattr(media, "media", media)
    date = getattr(media, "date", None)
    document = getattr(inner, "document", None)
    photo = getattr(inner, "photo", None)
    target = document if document is not None else photo
    return PreviewMedia(
        index=index,
        kind="video" if document is not None else "photo",
        date=fmt_dt(date),
        date_unix=to_unix(date),
        lang=lang or None,
        file_id=_id_of(target),
        size=getattr(target, "size", None),
    )


async def preview_list(ctx: OpContext, req: PreviewListReq) -> Page[PreviewMedia]:
    """The mini-app preview gallery on a bot's profile.

    `userFull.has_preview_medias` says whether this call is worth making at
    all, and `bot get` reports it — asking for a gallery that does not exist
    is a round trip for an empty list.
    """
    from telethon.tl.functions import bots as fn

    bot = await _bots.input_user(ctx, req.bot)
    if req.owner:
        result = await client(ctx)(fn.GetPreviewInfoRequest(bot=bot, lang_code=req.lang))
        media = getattr(result, "media", None) or []
    else:
        media = await client(ctx)(fn.GetPreviewMediasRequest(bot=bot)) or []
    items = [_preview(entry, index, req.lang) for index, entry in enumerate(media)]
    return Page(items=items, has_more=False, total=len(items))


SPEC_PREVIEW_LIST = OperationSpec(
    id="bot.preview.list",
    request=PreviewListReq,
    response=Page[PreviewMedia],
    impl=preview_list,
    summary="List a bot's mini-app preview media",
    columns=("index", "kind", "lang", "file_id"),
    headers=("#", "Kind", "Lang", "File"),
    example={"items": [{"index": 0, "kind": "photo"}], "has_more": False},
    example_args="bot preview list @my_helper_bot",
    covers=("bot.media-previews", "bots.preview-info-per-language", "bots.preview-medias-list"),
)


class PreviewAddReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]
    file: Annotated[str, arg(1, metavar="FILE", kind="path", help="Image or video.")]
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language code.")] = ""


async def _uploaded_media(ctx: OpContext, source: str) -> Any:
    """A local file as the `InputMedia` a `bots.*PreviewMedia*` call wants.

    `messages.uploadMedia` is the step that turns an uploaded file handle into
    a document the server already holds; handing the raw handle to
    `addPreviewMedia` would upload it again for every call.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    media = await _send.input_media(ctx, source)
    stored = await client(ctx)(fn.UploadMediaRequest(peer=types.InputPeerSelf(), media=media))
    document = _media.document_of(stored)
    if document is not None:
        return types.InputMediaDocument(id=_media.input_document(document))
    photo = getattr(stored, "photo", None)
    if photo is None:
        raise NotFoundError("the server did not accept that file as preview media")
    return types.InputMediaPhoto(id=_media.input_photo(photo))


async def preview_add(ctx: OpContext, req: PreviewAddReq) -> PreviewChange:
    """Add one preview media to my bot's mini-app gallery."""
    from telethon.tl.functions import bots as fn

    bot = await _bots.input_user(ctx, req.bot)
    media = await _uploaded_media(ctx, req.file)
    result = await client(ctx)(fn.AddPreviewMediaRequest(bot=bot, lang_code=req.lang, media=media))
    document = getattr(getattr(result, "media", result), "document", None)
    return PreviewChange(
        index=0, kind="video" if document is not None else "photo", lang=req.lang or None
    )


SPEC_PREVIEW_ADD = OperationSpec(
    id="bot.preview.add",
    request=PreviewAddReq,
    response=PreviewChange,
    impl=preview_add,
    summary="Add a preview media to my bot's mini-app gallery",
    mutating=True,
    rate_class="file",
    columns=("index", "kind", "lang"),
    headers=("#", "Kind", "Lang"),
    example={"index": 0, "kind": "photo"},
    example_args="bot preview add @my_helper_bot ./shot.png",
    covers=("bots.preview-media-add",),
)


class PreviewEditReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]
    index: Annotated[int | None, opt("--index", metavar="N", help="Position to replace.")] = None
    file: Annotated[
        str | None, opt("--file", metavar="PATH", kind="path", help="New media for --index.")
    ] = None
    order: Annotated[
        str | None, opt("--order", metavar="2,0,1", help="New order for the whole gallery.")
    ] = None
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language code.")] = ""


async def _current_previews(ctx: OpContext, bot: Any) -> list[Any]:
    from telethon.tl.functions import bots as fn

    return list(await client(ctx)(fn.GetPreviewMediasRequest(bot=bot)) or [])


def _as_input_media(entry: Any) -> Any:
    from telethon.tl import types

    inner = getattr(entry, "media", entry)
    document = getattr(inner, "document", None)
    if document is not None:
        return types.InputMediaDocument(id=_media.input_document(document))
    return types.InputMediaPhoto(id=_media.input_photo(getattr(inner, "photo", None)))


async def preview_edit(ctx: OpContext, req: PreviewEditReq) -> PreviewChange:
    """Replace one preview media, or reorder the gallery.

    Both take the *current* media as their handle, so the gallery is fetched
    first: an index alone means nothing to the server.
    """
    from telethon.tl.functions import bots as fn

    if (req.index is None) == (req.order is None):
        raise UsageError("give either --index with --file, or --order", field="index")
    index = req.index if req.index is not None else -1

    bot = await _bots.input_user(ctx, req.bot)
    current = await _current_previews(ctx, bot)
    handle = client(ctx)

    if req.order is not None:
        try:
            positions = [int(p) for p in req.order.split(",") if p.strip()]
        except ValueError as exc:
            raise UsageError("--order: expected a comma-separated list", field="order") from exc
        if sorted(positions) != list(range(len(current))):
            raise UsageError(
                f"--order must name every position exactly once (0..{len(current) - 1})",
                field="order",
            )
        await handle(
            fn.ReorderPreviewMediasRequest(
                bot=bot,
                lang_code=req.lang,
                order=[_as_input_media(current[p]) for p in positions],
            )
        )
        return PreviewChange(order=positions, lang=req.lang or None)

    if not req.file:
        raise UsageError("--index needs --file", field="file")
    if not 0 <= index < len(current):
        raise NotFoundError(f"there is no preview media at position {index}")
    await handle(
        fn.EditPreviewMediaRequest(
            bot=bot,
            lang_code=req.lang,
            media=_as_input_media(current[index]),
            new_media=await _uploaded_media(ctx, req.file),
        )
    )
    return PreviewChange(index=req.index, lang=req.lang or None)


SPEC_PREVIEW_EDIT = OperationSpec(
    id="bot.preview.edit",
    request=PreviewEditReq,
    response=PreviewChange,
    impl=preview_edit,
    summary="Replace one preview media, or reorder the gallery",
    mutating=True,
    rate_class="file",
    columns=("index", "order", "lang"),
    headers=("#", "Order", "Lang"),
    example={"index": 0, "lang": "en"},
    example_args="bot preview edit @my_helper_bot --order 1,0",
    covers=("bots.preview-media-edit", "bots.preview-media-reorder"),
)


class PreviewDeleteReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]
    index: Annotated[list[int], opt("--index", metavar="N", help="Positions to delete.")] = []
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language code.")] = ""


async def preview_delete(ctx: OpContext, req: PreviewDeleteReq) -> PreviewChange:
    """Delete preview media from my bot's gallery."""
    from telethon.tl.functions import bots as fn

    if not req.index:
        raise UsageError("name at least one --index", field="index")
    bot = await _bots.input_user(ctx, req.bot)
    current = await _current_previews(ctx, bot)
    missing = [i for i in req.index if not 0 <= i < len(current)]
    if missing:
        raise NotFoundError(f"there is no preview media at position {missing[0]}")
    await client(ctx)(
        fn.DeletePreviewMediaRequest(
            bot=bot,
            lang_code=req.lang,
            media=[_as_input_media(current[i]) for i in req.index],
        )
    )
    return PreviewChange(
        deleted=len(req.index), remaining=len(current) - len(req.index), lang=req.lang or None
    )


SPEC_PREVIEW_DELETE = OperationSpec(
    id="bot.preview.delete",
    request=PreviewDeleteReq,
    response=PreviewChange,
    impl=preview_delete,
    summary="Delete preview media from my bot's gallery",
    mutating=True,
    destructive=True,
    columns=("deleted", "remaining"),
    headers=("Deleted", "Remaining"),
    example={"deleted": 1, "remaining": 2},
    example_args="bot preview delete @my_helper_bot --index 0",
    covers=("bots.preview-media-delete",),
)


# ---------------------------------------------------------------------------
# bot affiliate
# ---------------------------------------------------------------------------


class AffiliateSetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]
    commission_permille: Annotated[
        int, opt("--commission-permille", metavar="N", help="Commission in permille.")
    ] = 0
    duration_months: Annotated[
        int | None,
        opt("--duration-months", metavar="N", help="Program duration; omit for unlimited."),
    ] = None


async def affiliate_set(ctx: OpContext, req: AffiliateSetReq) -> StarRefProgram:
    """Create or raise my bot's affiliate (star-ref) program.

    Commission and duration may only ever be *raised*: Telegram will not let a
    program get worse for the affiliates already in it. The bounds come from
    the server's own config keys rather than from constants here, because they
    change without a client release.
    """
    from telethon.tl.functions import bots as fn

    config = await _media.app_config(ctx)
    if not bool(config.get("starref_program_allowed", True)):
        raise PermissionError_("affiliate programs are switched off for this account")
    low = _media.config_int(config, "starref_min_commission_permille", 1)
    high = _media.config_int(config, "starref_max_commission_permille", 800)
    if not low <= req.commission_permille <= high:
        raise UsageError(
            f"--commission-permille must be between {low} and {high}", field="commission_permille"
        )

    result = await client(ctx)(
        fn.UpdateStarRefProgramRequest(
            bot=await _bots.input_user(ctx, req.bot),
            commission_permille=req.commission_permille,
            duration_months=req.duration_months,
        )
    )
    return _starref(result) or StarRefProgram(
        commission_permille=req.commission_permille, duration_months=req.duration_months
    )


SPEC_AFFILIATE_SET = OperationSpec(
    id="bot.affiliate.set",
    request=AffiliateSetReq,
    response=StarRefProgram,
    impl=affiliate_set,
    summary="Create or raise my bot's affiliate program",
    mutating=True,
    columns=("bot_id", "commission_permille", "duration_months"),
    headers=("Bot", "Permille", "Months"),
    example={"bot_id": 5000001, "commission_permille": 200},
    example_args="bot affiliate set @my_helper_bot --commission-permille 200",
    covers=("bots.affiliate-program-set",),
)


class AffiliateUnsetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot I own.")]


async def affiliate_unset(ctx: OpContext, req: AffiliateUnsetReq) -> StarRefProgram:
    """End my bot's affiliate program.

    A commission of zero schedules termination roughly a day out, and no new
    program can be created before that date — which is why this is confirmed
    like a deletion even though nothing disappears immediately.
    """
    from telethon.tl.functions import bots as fn

    result = await client(ctx)(
        fn.UpdateStarRefProgramRequest(
            bot=await _bots.input_user(ctx, req.bot), commission_permille=0
        )
    )
    return _starref(result) or StarRefProgram(commission_permille=0)


SPEC_AFFILIATE_UNSET = OperationSpec(
    id="bot.affiliate.unset",
    request=AffiliateUnsetReq,
    response=StarRefProgram,
    impl=affiliate_unset,
    summary="End my bot's affiliate program",
    mutating=True,
    destructive=True,
    columns=("bot_id", "end_date"),
    headers=("Bot", "Ends"),
    example={"bot_id": 5000001, "commission_permille": 0},
    example_args="bot affiliate unset @my_helper_bot",
    covers=("bots.affiliate-program-end",),
)


class AffiliateJoinReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot's program to join.")]
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Join as me, a bot or a channel."),
    ] = None


async def affiliate_join(ctx: OpContext, req: AffiliateJoinReq) -> StarRefProgram:
    """Join a bot's affiliate program and get my referral link."""
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    config = await _media.app_config(ctx)
    if not bool(config.get("starref_connect_allowed", True)):
        raise PermissionError_("joining affiliate programs is switched off for this account")
    peer = (
        await _send.resolve(ctx, req.send_as) if req.send_as is not None else types.InputPeerSelf()
    )
    result = await client(ctx)(
        fn.ConnectStarRefBotRequest(peer=peer, bot=await _bots.input_user(ctx, req.bot))
    )
    connected = (getattr(result, "connected_bots", None) or [None])[0]
    return _connected_ref(connected) or StarRefProgram()


def _connected_ref(entry: Any) -> StarRefProgram | None:
    if entry is None:
        return None
    date = getattr(entry, "date", None)
    return StarRefProgram(
        bot_id=int(getattr(entry, "bot_id", 0) or 0),
        url=getattr(entry, "url", None),
        commission_permille=int(getattr(entry, "commission_permille", 0) or 0),
        duration_months=getattr(entry, "duration_months", None),
        participants=getattr(entry, "participants", None),
        revenue=int(getattr(entry, "revenue", 0) or 0) or None,
        date=fmt_dt(date),
        date_unix=to_unix(date),
        revoked=bool(getattr(entry, "revoked", False)),
    )


SPEC_AFFILIATE_JOIN = OperationSpec(
    id="bot.affiliate.join",
    request=AffiliateJoinReq,
    response=StarRefProgram,
    impl=affiliate_join,
    summary="Join a bot's affiliate program and get my referral link",
    mutating=True,
    columns=("bot_id", "url", "commission_permille"),
    headers=("Bot", "Link", "Permille"),
    example={
        "bot_id": 5000001,
        "url": "https://t.me/my_helper_bot?start=ref",
        "commission_permille": 200,
    },
    example_args="bot affiliate join @my_helper_bot",
    covers=("bots.affiliate-connect",),
)


class AffiliateListReq(Request):
    suggested: Annotated[
        bool, opt("--suggested", help="Browse mini apps with an open program.")
    ] = False
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Act as me, a bot or a channel."),
    ] = None
    bot: Annotated[
        PeerRef | None,
        opt("--bot", metavar="BOT", kind="user", help="Only the program connected to this bot."),
    ] = None
    by: Annotated[str, choice("revenue", "date", help="Sort order for --suggested.")] = "revenue"


async def affiliate_list(ctx: OpContext, req: AffiliateListReq) -> Page[StarRefProgram]:
    """Affiliate programs: mine, one of mine, or ones on offer.

    Connected programs page by `(offset_date, offset_link)` *together* — two
    values, not one — so tlgr packs both into the single opaque cursor every
    other listing uses. A caller that had to carry two offsets by hand would
    be the only place in tlgr where pagination looks different.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    limit, state = window(ctx, "bot.affiliate.list", PageKind.RATE, default=50)
    peer = (
        await _send.resolve(ctx, req.send_as) if req.send_as is not None else types.InputPeerSelf()
    )
    handle = client(ctx)

    if req.suggested:
        result = await handle(
            fn.GetSuggestedStarRefBotsRequest(
                peer=peer,
                offset=str(state.get("offset", "") or ""),
                limit=limit,
                order_by_revenue=req.by == "revenue" or None,
                order_by_date=req.by == "date" or None,
            )
        )
        items = [
            _starref(entry) or StarRefProgram()
            for entry in (getattr(result, "suggested_bots", None) or [])
        ]
        next_offset = str(getattr(result, "next_offset", "") or "")
        return build_page(
            items,
            op="bot.affiliate.list",
            kind=PageKind.RATE,
            state={"offset": next_offset},
            account=ctx.account,
            has_more=bool(next_offset),
        )

    if req.bot is not None:
        result = await handle(
            fn.GetConnectedStarRefBotRequest(peer=peer, bot=await _bots.input_user(ctx, req.bot))
        )
        entry = _connected_ref(getattr(result, "connected_bot", None))
        return Page(items=[entry] if entry else [], has_more=False, total=1 if entry else 0)

    from tlgr.core.timefmt import parse_dt

    offset_date = parse_dt(str(state["date"])) if state.get("date") else None
    result = await handle(
        fn.GetConnectedStarRefBotsRequest(
            peer=peer,
            limit=limit,
            offset_date=offset_date,
            offset_link=state.get("link") or None,
        )
    )
    entries = getattr(result, "connected_bots", None) or []
    items = [ref for ref in (_connected_ref(entry) for entry in entries) if ref is not None]
    last = items[-1] if items else None
    return build_page(
        items,
        op="bot.affiliate.list",
        kind=PageKind.RATE,
        state={"date": last.date if last else None, "link": last.url if last else None},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_AFFILIATE_LIST = OperationSpec(
    id="bot.affiliate.list",
    request=AffiliateListReq,
    response=Page[StarRefProgram],
    impl=affiliate_list,
    summary="List affiliate programs I joined, or ones on offer",
    paginated=PageKind.RATE,
    columns=("bot_id", "url", "commission_permille", "revenue"),
    headers=("Bot", "Link", "Permille", "Revenue"),
    example={"items": [{"bot_id": 5000001, "commission_permille": 200}], "has_more": False},
    example_args="bot affiliate list",
    covers=("bots.affiliate-list-connected", "bots.affiliate-suggested"),
)


class AffiliateRevokeReq(Request):
    link: Annotated[str, arg(0, metavar="LINK", help="The referral link to revoke.")]
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Peer the link belongs to."),
    ] = None


async def affiliate_revoke(ctx: OpContext, req: AffiliateRevokeReq) -> StarRefProgram:
    """Revoke one of my affiliate links.

    `STARREF_EXPIRED` means the link is already dead, which is the state the
    caller asked for — reported as `already` rather than as a failure.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    peer = (
        await _send.resolve(ctx, req.send_as) if req.send_as is not None else types.InputPeerSelf()
    )
    try:
        result = await client(ctx)(
            fn.EditConnectedStarRefBotRequest(peer=peer, link=req.link, revoked=True)
        )
    except Exception as exc:  # one server answer means "already done"
        if "STARREFEXPIRED" not in f"{type(exc).__name__} {exc}".upper().replace("_", ""):
            raise
        from tlgr.ops._common import already as mark_already

        mark_already(ctx)
        return StarRefProgram(url=req.link, revoked=True)
    return _connected_ref(getattr(result, "connected_bot", None)) or StarRefProgram(
        url=req.link, revoked=True
    )


SPEC_AFFILIATE_REVOKE = OperationSpec(
    id="bot.affiliate.revoke",
    request=AffiliateRevokeReq,
    response=StarRefProgram,
    impl=affiliate_revoke,
    summary="Revoke one of my affiliate links",
    mutating=True,
    destructive=True,
    idempotent=True,
    columns=("url", "revoked"),
    headers=("Link", "Revoked"),
    example={"url": "https://t.me/my_helper_bot?start=ref", "revoked": True},
    example_args="bot affiliate revoke https://t.me/my_helper_bot?start=ref",
    covers=("bots.affiliate-revoke",),
)


# ---------------------------------------------------------------------------
# bot attach list / toggle, bot recent set
# ---------------------------------------------------------------------------


class AttachListReq(Request):
    bot: Annotated[
        PeerRef | None,
        opt("--bot", metavar="BOT", kind="user", help="Inspect one bot's entry."),
    ] = None


def _attach_bot(entry: Any) -> AttachMenuBot:
    return AttachMenuBot(
        bot_id=int(getattr(entry, "bot_id", 0) or 0),
        short_name=getattr(entry, "short_name", None),
        peer_types=[
            _bots.BUTTON_TYPES.get(
                type(p).__name__, type(p).__name__.removeprefix("AttachMenuPeerType").lower()
            )
            for p in (getattr(entry, "peer_types", None) or [])
        ],
        inactive=bool(getattr(entry, "inactive", False)),
        request_write_access=bool(getattr(entry, "request_write_access", False)),
        show_in_attach_menu=bool(getattr(entry, "show_in_attach_menu", False)),
        show_in_side_menu=bool(getattr(entry, "show_in_side_menu", False)),
        side_menu_disclaimer_needed=bool(getattr(entry, "side_menu_disclaimer_needed", False)),
    )


async def attach_list(ctx: OpContext, req: AttachListReq) -> Page[AttachMenuBot]:
    """The bots installed in my attachment and side menus."""
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    if req.bot is not None:
        result = await handle(fn.GetAttachMenuBotRequest(bot=await _bots.input_user(ctx, req.bot)))
        entry = getattr(result, "bot", None)
        rows = [_attach_bot(entry)] if entry is not None else []
        _name_bots(rows, getattr(result, "users", None) or [])
        return Page(items=rows, has_more=False, total=len(rows))

    result = await handle(fn.GetAttachMenuBotsRequest(hash=0))
    if type(result).__name__ == "AttachMenuBotsNotModified":  # pragma: no cover - hash is 0
        return Page(items=[], has_more=False, total=0)
    rows = [_attach_bot(entry) for entry in (getattr(result, "bots", None) or [])]
    _name_bots(rows, getattr(result, "users", None) or [])
    return Page(items=rows, has_more=False, total=len(rows))


def _name_bots(rows: list[AttachMenuBot], users: list[Any]) -> None:
    by_id = {int(getattr(u, "id", 0) or 0): u for u in users}
    for row in rows:
        user = by_id.get(row.bot_id)
        if user is not None:
            row.username = getattr(user, "username", None)


SPEC_ATTACH_LIST = OperationSpec(
    id="bot.attach.list",
    request=AttachListReq,
    response=Page[AttachMenuBot],
    impl=attach_list,
    summary="List the bots in my attachment and side menus",
    columns=("bot_id", "username", "short_name", "show_in_attach_menu"),
    headers=("Bot", "Username", "Name", "Attach"),
    example={"items": [{"bot_id": 5000001, "short_name": "Helper"}], "has_more": False},
    example_args="bot attach list",
    covers=("attach.menu-bots", "bots.attach-menu-list"),
)


class AttachToggleReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot.")]
    state: Annotated[str, arg(1, metavar="STATE", help="on = install, off = remove.")]
    allow_write: Annotated[
        bool, opt("--allow-write", help="CONSENT: also let the bot message me.")
    ] = False
    accept_tos: Annotated[
        bool, opt("--accept-tos", help="Required when the bot needs a side-menu disclaimer.")
    ] = False


async def attach_toggle(ctx: OpContext, req: AttachToggleReq) -> ToggledAttachMenu:
    """Install or remove a bot from the attachment and side menu.

    `write_allowed` is never set implicitly: installing a mini app and letting
    its bot message you are two different decisions, and Telegram's own API
    puts them in one call.
    """
    from telethon.tl.functions import messages as fn

    if req.state not in ("on", "off"):
        raise UsageError("state must be 'on' or 'off'", field="state")
    bot = await _bots.input_user(ctx, req.bot)
    handle = client(ctx)

    if req.state == "on":
        entry = getattr(await handle(fn.GetAttachMenuBotRequest(bot=bot)), "bot", None)
        if bool(getattr(entry, "side_menu_disclaimer_needed", False)) and not req.accept_tos:
            raise UsageError(
                "this bot requires you to accept its terms first; pass --accept-tos",
                field="accept_tos",
            )
    await handle(
        fn.ToggleBotInAttachMenuRequest(
            bot=bot, enabled=req.state == "on", write_allowed=req.allow_write or None
        )
    )
    peer = await _send.resolve(ctx, req.bot)
    return ToggledAttachMenu(
        bot_id=_send.peer_id_of(peer),
        installed=req.state == "on",
        write_allowed=req.allow_write,
    )


SPEC_ATTACH_TOGGLE = OperationSpec(
    id="bot.attach.toggle",
    request=AttachToggleReq,
    response=ToggledAttachMenu,
    impl=attach_toggle,
    summary="Install or remove a bot from the attachment menu",
    mutating=True,
    destructive=True,
    columns=("bot_id", "installed", "write_allowed"),
    headers=("Bot", "Installed", "May message"),
    example={"bot_id": 5000001, "installed": True, "write_allowed": False},
    example_args="bot attach toggle @my_helper_bot on",
    covers=("bots.attach-menu-toggle", "bots.miniapp-panel-menu", "bots.webapp-write-access"),
)


class RecentSetReq(Request):
    state: Annotated[
        str | None, arg(0, metavar="STATE", required=False, help="on|off for the whole feature.")
    ] = None
    forget: Annotated[
        PeerRef | None,
        opt("--forget", metavar="BOT", kind="user", help="Reset the rating of one bot."),
    ] = None
    forget_all: Annotated[bool, opt("--forget-all", help="Reset the whole category.")] = False
    kind: Annotated[
        str, choice("pm", "inline", "app", "guest", help="Category the reset applies to.")
    ] = "pm"


_TOP_PEER_CATEGORIES = {
    "pm": "TopPeerCategoryBotsPM",
    "inline": "TopPeerCategoryBotsInline",
    "app": "TopPeerCategoryBotsApp",
    "guest": "TopPeerCategoryBotsGuestChat",
}


async def recent_set(ctx: OpContext, req: RecentSetReq) -> RecentBots:
    """Turn frequently-used-bot suggestions on or off, or forget one bot."""
    from telethon.tl import types
    from telethon.tl.functions import contacts as fn

    handle = client(ctx)
    forgotten: list[int] = []
    enabled = req.state != "off"

    if req.state is not None:
        if req.state not in ("on", "off"):
            raise UsageError("state must be 'on' or 'off'", field="state")
        await handle(fn.ToggleTopPeersRequest(enabled=req.state == "on"))

    category_name = _TOP_PEER_CATEGORIES[req.kind]
    category: Any = getattr(types, category_name, None)
    if category is None:  # pragma: no cover - layer 227 has every category we name
        _bots.unsupported(f"--kind {req.kind}")
    if req.forget is not None:
        peer = await _send.resolve(ctx, req.forget)
        await handle(fn.ResetTopPeerRatingRequest(category=category(), peer=peer))
        forgotten = [_send.peer_id_of(peer)]
    elif req.forget_all:
        await handle(fn.ResetTopPeerRatingRequest(category=category(), peer=types.InputPeerEmpty()))

    if req.state is None and req.forget is None and not req.forget_all:
        raise UsageError("give a state, --forget or --forget-all", field="state")
    return RecentBots(enabled=enabled, kind=req.kind, forgotten=forgotten)


SPEC_RECENT_SET = OperationSpec(
    id="bot.recent.set",
    request=RecentSetReq,
    response=RecentBots,
    impl=recent_set,
    summary="Turn frequently-used-bot suggestions on or off",
    mutating=True,
    columns=("enabled", "kind", "forgotten"),
    headers=("Enabled", "Kind", "Forgotten"),
    example={"enabled": True, "kind": "pm", "forgotten": []},
    example_args="bot recent set off",
    covers=("bots.top-peers-bots",),
)


# ---------------------------------------------------------------------------
# bot report, bot ad
# ---------------------------------------------------------------------------


class ReportReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot or mini app owner.")]
    app: Annotated[str | None, opt("--app", help="Report a mini app by short name.")] = None
    message: Annotated[
        int | None, opt("--message", metavar="ID", kind="msg_id", help="Report one message.")
    ] = None
    ephemeral: Annotated[
        int | None, opt("--ephemeral", metavar="ID", help="Report an ephemeral message.")
    ] = None
    option: Annotated[
        str | None, opt("--option", metavar="BYTES", help="Option from the previous step.")
    ] = None
    comment: Annotated[str | None, opt("--comment", help="Free-text comment.")] = None


async def report(ctx: OpContext, req: ReportReq) -> ReportOutcome:
    """Report a bot, a mini app or one of its messages.

    Telegram's report flow is a state machine, not a form: the first call
    returns a list of options, each option leads to another list or to a
    comment box. One call per step is what lets a caller drive it without
    tlgr guessing which category they meant.
    """
    from telethon.tl.functions import messages as fn

    if req.ephemeral is not None:
        _bots.unsupported("--ephemeral (ephemeral.reportMessage)")

    peer = await _send.resolve(ctx, req.bot)
    ids = [int(req.message)] if req.message is not None else []
    result = await client(ctx)(
        fn.ReportRequest(
            peer=peer,
            id=ids,
            option=_bots.option_bytes(req.option),
            message=req.comment or "",
        )
    )
    outcome = _bots.report_outcome(result)
    if outcome.reported:
        ctx.emit("bot_report", {"bot_id": _send.peer_id_of(peer)})
    return outcome


SPEC_REPORT = OperationSpec(
    id="bot.report",
    request=ReportReq,
    response=ReportOutcome,
    impl=report,
    summary="Report a bot or a mini app",
    mutating=True,
    columns=("result", "title", "reported"),
    headers=("Step", "Title", "Done"),
    example={"result": "choose_option", "title": "What is wrong?", "options": []},
    example_args="bot report @spam_bot",
    covers=("bots.report-bot-or-app",),
    covers_partial=("bots.miniapp-panel-menu",),
    coverage_note=(
        "Installing and removing a mini app is `bot attach toggle`; reporting "
        "an ephemeral message needs layer 229 and exits 13."
    ),
)


class AdListReq(Request):
    bot: Annotated[
        PeerRef | None,
        arg(0, metavar="BOT", required=False, kind="user", help="The bot chat."),
    ] = None
    search: Annotated[
        str | None,
        opt("--search", metavar="QUERY", help="Sponsored chats a search for QUERY would show."),
    ] = None


async def ad_list(ctx: OpContext, req: AdListReq) -> Page[SponsoredMessage]:
    """The sponsored messages a bot chat — or a search — would show.

    Opt-in, like `message sponsored list`: tlgr never mixes ads into a message
    listing or a search result, and never reports an impression that nobody
    saw — that is `bot ad read`. Both surfaces are here because both are ads,
    and splitting them would hide one of the two places they appear.
    """
    from telethon.tl.functions import contacts as contacts_fn
    from telethon.tl.functions import messages as fn

    if req.search is not None:
        return await _sponsored_peers(ctx, req.search, contacts_fn)
    if req.bot is None:
        raise UsageError("name a bot chat, or use --search", field="bot")

    peer = await _send.resolve(ctx, req.bot)
    result = await client(ctx)(fn.GetSponsoredMessagesRequest(peer=peer))
    items = [
        SponsoredMessage(
            random_id=_bots.key_text(getattr(entry, "random_id", b"")),
            title=getattr(entry, "title", None),
            message=str(getattr(entry, "message", "") or ""),
            url=getattr(entry, "url", None),
            button_text=getattr(entry, "button_text", None),
            sponsor_info=getattr(entry, "sponsor_info", None),
            additional_info=getattr(entry, "additional_info", None),
            recommended=bool(getattr(entry, "recommended", False)),
            can_report=bool(getattr(entry, "can_report", False)),
        )
        for entry in (getattr(result, "messages", None) or [])
    ]
    return Page(items=items, has_more=False, total=len(items))


async def _sponsored_peers(ctx: OpContext, query: str, contacts_fn: Any) -> Page[SponsoredMessage]:
    """`contacts.getSponsoredPeers` as the same row a bot-chat ad produces.

    A sponsored *peer* is an ad for a chat rather than a message in one, so it
    has a title and no body; reporting it in the same shape is what lets one
    `bot ad read` mark either kind as seen.
    """
    from tlgr.ops._serialize import entity_to_peer

    result = await client(ctx)(contacts_fn.GetSponsoredPeersRequest(q=query))
    chats = {int(getattr(c, "id", 0)): c for c in (getattr(result, "chats", None) or [])}
    users = {int(getattr(u, "id", 0)): u for u in (getattr(result, "users", None) or [])}
    items: list[SponsoredMessage] = []
    for entry in getattr(result, "peers", None) or []:
        peer = getattr(entry, "peer", None)
        raw_id = int(
            getattr(peer, "channel_id", 0)
            or getattr(peer, "user_id", 0)
            or getattr(peer, "chat_id", 0)
            or 0
        )
        entity = chats.get(raw_id) or users.get(raw_id)
        items.append(
            SponsoredMessage(
                random_id=_bots.key_text(getattr(entry, "random_id", b"")),
                title=entity_to_peer(entity).title if entity is not None else None,
                message="",
                sponsor_info=getattr(entry, "sponsor_info", None),
                additional_info=getattr(entry, "additional_info", None),
            )
        )
    return Page(items=items, has_more=False, total=len(items))


SPEC_AD_LIST = OperationSpec(
    id="bot.ad.list",
    request=AdListReq,
    response=Page[SponsoredMessage],
    impl=ad_list,
    summary="List the sponsored messages shown inside a bot chat",
    description=(
        "Telegram's API terms require a third-party client that shows bot or "
        "channel content to support sponsored messages; tlgr does so by "
        "making them a command of their own instead of hiding them in a feed."
    ),
    columns=("random_id", "title", "message"),
    headers=("ID", "Title", "Text"),
    example={"items": [{"random_id": "abc", "message": "An ad"}], "has_more": False},
    example_args="bot ad list @my_helper_bot",
    covers=("bots.bot-ads-account", "dialogs.sponsored-search-peers"),
    covers_partial=("bots.sponsored-message-in-bot-chat",),
    coverage_note="Reporting an impression or a click is `bot ad read`.",
)


class AdReadReq(Request):
    random_id: Annotated[str, arg(0, metavar="RANDOM_ID", help="random_id from `bot ad list`.")]
    click: Annotated[bool, opt("--click", help="Also record a click.")] = False
    media: Annotated[bool, opt("--media", help="The click was on the ad's media.")] = False
    fullscreen: Annotated[bool, opt("--fullscreen", help="The click was in fullscreen.")] = False


async def ad_read(ctx: OpContext, req: AdReadReq) -> SponsoredRead:
    """Report that a sponsored message was seen, or clicked."""
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    raw = _bots.option_bytes(req.random_id, field="random_id")
    await handle(fn.ViewSponsoredMessageRequest(random_id=raw))
    if req.click:
        await handle(
            fn.ClickSponsoredMessageRequest(
                random_id=raw, media=req.media or None, fullscreen=req.fullscreen or None
            )
        )
    return SponsoredRead(random_id=req.random_id, viewed=True, clicked=req.click)


SPEC_AD_READ = OperationSpec(
    id="bot.ad.read",
    request=AdReadReq,
    response=SponsoredRead,
    impl=ad_read,
    summary="Mark a sponsored message as seen, or as clicked",
    aliases=("bot.ad.view",),
    mutating=True,
    columns=("random_id", "viewed", "clicked"),
    headers=("ID", "Viewed", "Clicked"),
    example={"random_id": "abc", "viewed": True, "clicked": False},
    example_args="bot ad read abc",
    covers=("bots.sponsored-message-in-bot-chat",),
)


class AdReportReq(Request):
    random_id: Annotated[str, arg(0, metavar="RANDOM_ID", help="random_id from `bot ad list`.")]
    option: Annotated[
        str | None, opt("--option", metavar="BYTES", help="Option from the previous step.")
    ] = None
    comment: Annotated[str | None, opt("--comment", help="Free-text comment.")] = None


async def ad_report(ctx: OpContext, req: AdReportReq) -> ReportOutcome:
    """Report a sponsored message, walking the same option tree as `bot report`."""
    from telethon.tl.functions import messages as fn

    result = await client(ctx)(
        fn.ReportSponsoredMessageRequest(
            random_id=_bots.option_bytes(req.random_id, field="random_id"),
            option=_bots.option_bytes(req.option),
        )
    )
    return _bots.report_outcome(result)


SPEC_AD_REPORT = OperationSpec(
    id="bot.ad.report",
    request=AdReportReq,
    response=ReportOutcome,
    impl=ad_report,
    summary="Report a sponsored message in a bot chat",
    mutating=True,
    columns=("result", "title", "reported"),
    headers=("Step", "Title", "Done"),
    example={"result": "reported", "reported": True},
    example_args="bot ad report abc",
    covers_partial=("bots.sponsored-message-in-bot-chat",),
    coverage_note="Listing and viewing the ads themselves is `bot ad list`/`bot ad read`.",
)


# ---------------------------------------------------------------------------
# bot game get / send, bot score list / set
# ---------------------------------------------------------------------------


class GameGetReq(Request):
    emoji: Annotated[
        str | None, opt("--emoji", metavar="EMOJI", help="Dice emoji this report is about.")
    ] = None


async def game_get(ctx: OpContext, req: GameGetReq) -> EmojiGame:
    """Emoji-dice game parameters. Inspect only.

    Staking TON on an emoji game moves money, so tlgr reads the parameters and
    stops there. `messages.getEmojiGameInfo` takes no arguments — `--emoji` is
    recorded on the answer so a caller can tell which game they asked about.
    """
    from telethon.tl.functions import messages as fn

    result = await client(ctx)(fn.GetEmojiGameInfoRequest())
    if type(result).__name__ == "EmojiGameUnavailable":
        return EmojiGame(emoticon=req.emoji or "", available=False)
    return EmojiGame(
        emoticon=req.emoji or "",
        available=True,
        game_hash=getattr(result, "game_hash", None),
        prev_stake=getattr(result, "prev_stake", None),
        current_streak=getattr(result, "current_streak", None),
        params=[int(p) for p in (getattr(result, "params", None) or [])],
        plays_left=getattr(result, "plays_left", None),
    )


SPEC_GAME_GET = OperationSpec(
    id="bot.game.get",
    request=GameGetReq,
    response=EmojiGame,
    impl=game_get,
    summary="Show emoji-dice game parameters",
    columns=("emoticon", "available", "current_streak"),
    headers=("Emoji", "Available", "Streak"),
    example={"emoticon": "🎲", "available": True, "current_streak": 0},
    example_args="bot game get --emoji 🎲",
    covers=("bots.emoji-games",),
)


class GameSendReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot that owns the game.")]
    short_name: Annotated[str, arg(1, metavar="SHORT_NAME", help="Game short name.")]
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="Destination chat.")
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    schedule: Annotated[str | None, opt("--schedule", metavar="TS", help="Schedule the send.")] = (
        None
    )


async def game_send(ctx: OpContext, req: GameSendReq) -> GameSent:
    """Send an HTML5 game to a chat.

    Only the owning bot may send by short name; a user can forward an existing
    game message but cannot mint one, which is why this is a bot-session
    command rather than a refusal from the server three steps later.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "sending a game by short name")
    if req.chat is None:
        raise UsageError("--chat is required", field="chat")
    target = await _send.resolve(ctx, req.chat)
    updates = await client(ctx)(
        fn.SendMediaRequest(
            peer=target,
            media=types.InputMediaGame(
                id=types.InputGameShortName(
                    bot_id=await _bots.input_user(ctx, req.bot), short_name=req.short_name
                )
            ),
            message="",
            random_id=_random_id(),
            reply_to=await _send.reply_target(ctx, reply_to=req.reply_to),
            silent=req.silent or None,
            schedule_date=_send.schedule_at(req.schedule),
        )
    )
    message = _send.message_from_updates(updates, chat_id=_send.peer_id_of(target))
    game = getattr(getattr(message, "media", None), "game", None)
    return GameSent(
        chat_id=message.chat_id,
        msg_id=message.id,
        game_id=_id_of(game),
        short_name=req.short_name,
    )


SPEC_GAME_SEND = OperationSpec(
    id="bot.game.send",
    request=GameSendReq,
    response=GameSent,
    impl=game_send,
    summary="Send an HTML5 game to a chat",
    tags=frozenset({"visible-to-others"}),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "short_name"),
    headers=("Chat", "Message", "Game"),
    example={"chat_id": 4242, "msg_id": 12, "short_name": "tetris"},
    example_args="bot game send @my_helper_bot tetris --chat @alice",
    covers=("bots.send-game",),
)


class ScoreListReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="Chat holding the game."),
    ] = None
    msg_id: Annotated[
        int | None, arg(1, metavar="MSG_ID", required=False, kind="msg_id", help="Game message.")
    ] = None
    inline_id: Annotated[
        str | None, opt("--inline-id", metavar="DC:ID:HASH", help="Inline message id.")
    ] = None
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Centre the table on this user."),
    ] = None


async def score_list(ctx: OpContext, req: ScoreListReq) -> Page[HighScore]:
    """A game's high-score table.

    The inline variant has to be sent to the DC the inline message lives on;
    sending it home answers with an error that never mentions data centres.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    user = (
        await _bots.input_user(ctx, req.user, field="user")
        if req.user is not None
        else types.InputUserSelf()
    )
    if req.inline_id:
        identifier = _bots.inline_message_id(req.inline_id)
        result = await _bots.on_dc(
            ctx,
            int(getattr(identifier, "dc_id", 0) or 0),
            fn.GetInlineGameHighScoresRequest(id=identifier, user_id=user),
        )
    else:
        if req.chat is None or req.msg_id is None:
            raise UsageError("give a chat and a message id, or --inline-id", field="msg_id")
        result = await client(ctx)(
            fn.GetGameHighScoresRequest(
                peer=await _send.resolve(ctx, req.chat), id=int(req.msg_id), user_id=user
            )
        )
    items = [
        HighScore(
            position=int(getattr(score, "pos", 0) or 0),
            user_id=int(getattr(score, "user_id", 0) or 0),
            score=int(getattr(score, "score", 0) or 0),
        )
        for score in (getattr(result, "scores", None) or [])
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_SCORE_LIST = OperationSpec(
    id="bot.score.list",
    request=ScoreListReq,
    response=Page[HighScore],
    impl=score_list,
    summary="Show a game's high-score table",
    columns=("position", "user_id", "score"),
    headers=("#", "User", "Score"),
    example={"items": [{"position": 1, "user_id": 4242, "score": 900}], "has_more": False},
    example_args="bot score list @alice 12",
    covers=("bots.game-high-scores", "bots.inline-game-high-scores"),
)


class ScoreSetReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="Chat holding the game."),
    ] = None
    msg_id: Annotated[
        int | None, arg(1, metavar="MSG_ID", required=False, kind="msg_id", help="Game message.")
    ] = None
    inline_id: Annotated[
        str | None, opt("--inline-id", metavar="DC:ID:HASH", help="Inline message id.")
    ] = None
    user: Annotated[
        PeerRef | None, opt("--user", metavar="USER", kind="user", help="The player.")
    ] = None
    score: Annotated[int, opt("--score", metavar="N", help="New score.")] = 0
    edit_message: Annotated[bool, opt("--edit-message", help="Also update the game message.")] = (
        False
    )
    allow_lower: Annotated[
        bool, opt("--allow-lower", help="Allow the score to decrease (force).")
    ] = False


async def score_set(ctx: OpContext, req: ScoreSetReq) -> ScoreSet:
    """Report a game score for a user.

    `--allow-lower` is Telegram's `force`: without it the server keeps the
    player's best score, which is almost always what a leaderboard wants.
    It is spelled out rather than borrowed from the global `--yes`, which an
    operation never sees.
    """
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "setting a game score")
    if req.user is None:
        raise UsageError("--user names the player and is required", field="user")
    user = await _bots.input_user(ctx, req.user, field="user")

    if req.inline_id:
        identifier = _bots.inline_message_id(req.inline_id)
        await _bots.on_dc(
            ctx,
            int(getattr(identifier, "dc_id", 0) or 0),
            fn.SetInlineGameScoreRequest(
                id=identifier,
                user_id=user,
                score=req.score,
                edit_message=req.edit_message or None,
                force=req.allow_lower or None,
            ),
        )
    else:
        if req.chat is None or req.msg_id is None:
            raise UsageError("give a chat and a message id, or --inline-id", field="msg_id")
        await client(ctx)(
            fn.SetGameScoreRequest(
                peer=await _send.resolve(ctx, req.chat),
                id=int(req.msg_id),
                user_id=user,
                score=req.score,
                edit_message=req.edit_message or None,
                force=req.allow_lower or None,
            )
        )
    return ScoreSet(user_id=_send.peer_id_of(await _send.resolve(ctx, req.user)), score=req.score)


SPEC_SCORE_SET = OperationSpec(
    id="bot.score.set",
    request=ScoreSetReq,
    response=ScoreSet,
    impl=score_set,
    summary="Report a game score for a user",
    tags=frozenset({"visible-to-others"}),
    mutating=True,
    columns=("user_id", "score", "position"),
    headers=("User", "Score", "#"),
    example={"user_id": 4242, "score": 900},
    example_args="bot score set @alice 12 --user @alice --score 900",
    covers=("bots.set-game-score",),
)


# ---------------------------------------------------------------------------
# Layer 229: ephemeral messages and bot welcome messages
# ---------------------------------------------------------------------------


class EphemeralSendReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat it lives in.")]
    text: Annotated[str, arg(1, metavar="TEXT", help="Message text, or a /command.")]
    bot: Annotated[
        PeerRef | None,
        opt("--bot", metavar="BOT", kind="user", help="Bot the conversation belongs to."),
    ] = None
    receiver: Annotated[
        PeerRef | None,
        opt("--receiver", metavar="USER", kind="user", help="Who alone will see it (bot side)."),
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", help="Ephemeral message being replied to.")
    ] = None
    query_id: Annotated[
        str | None, opt("--query-id", metavar="ID", help="Guest/callback query this answers.")
    ] = None
    keyboard: Annotated[
        str | None, opt("--keyboard", metavar="PATH", kind="path", help="JSON keyboard.")
    ] = None
    rich_file: Annotated[
        str | None, opt("--rich-file", metavar="PATH", kind="path", help="Send a rich body.")
    ] = None
    anchor: Annotated[bool, opt("--anchor", help="Pin it to the triggering message.")] = False
    welcome: Annotated[bool, opt("--welcome", help="Store it as a welcome template.")] = False
    edit: Annotated[
        int | None, opt("--edit", metavar="ID", help="Edit this ephemeral message instead.")
    ] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Text formatting.")] = None


async def ephemeral_send(ctx: OpContext, req: EphemeralSendReq) -> EphemeralSent:
    """Send an "only you can see this" bot message.

    `ephemeral.sendMessage#ba8d5f35` and `ephemeral.editMessage#cf9c725b` are
    layer-229 constructors and the pinned Telethon speaks 227. The operation
    is registered rather than omitted so that `tlgr agent capabilities` can
    say the surface exists and is unavailable — which is a different answer
    from "no such command", and the one an agent can act on.
    """
    _bots.unsupported("bot ephemeral send")
    raise AssertionError  # pragma: no cover - unreachable; keeps mypy happy


SPEC_EPHEMERAL_SEND = OperationSpec(
    id="bot.ephemeral.send",
    request=EphemeralSendReq,
    response=EphemeralSent,
    impl=ephemeral_send,
    summary="Send an ephemeral ('only you can see this') bot message",
    description=(
        "Layer 229. Exits 13 (NOT_SUPPORTED) until the pinned Telethon speaks "
        "it: hand-rolling the request would mean guessing at constructor ids "
        "for parameters nobody has published."
    ),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "ephemeral_id"),
    headers=("Chat", "Ephemeral"),
    example={"chat_id": 4242, "ephemeral_id": 0},
    example_args="bot ephemeral send @alice Hello",
    tags=frozenset({"not-supported"}),
)


class EphemeralDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat it lives in.")]
    id: Annotated[
        list[int], arg(1, metavar="ID", variadic=True, help="Ephemeral message ids.")
    ] = []
    receiver: Annotated[
        PeerRef | None,
        opt("--receiver", metavar="USER", kind="user", help="Whose copy is deleted (bot side)."),
    ] = None
    dismiss: Annotated[bool, opt("--dismiss", help="Only clear it locally.")] = False


async def ephemeral_delete(ctx: OpContext, req: EphemeralDeleteReq) -> EphemeralDeleted:
    """Delete or dismiss an ephemeral bot message. Layer 229; exits 13."""
    _bots.unsupported("bot ephemeral delete")
    raise AssertionError  # pragma: no cover - unreachable; keeps mypy happy


SPEC_EPHEMERAL_DELETE = OperationSpec(
    id="bot.ephemeral.delete",
    request=EphemeralDeleteReq,
    response=EphemeralDeleted,
    impl=ephemeral_delete,
    summary="Delete or dismiss an ephemeral bot message",
    mutating=True,
    destructive=True,
    columns=("chat_id", "deleted"),
    headers=("Chat", "Deleted"),
    example={"chat_id": 4242, "deleted": 0},
    example_args="bot ephemeral delete @alice 12",
    tags=frozenset({"not-supported"}),
)


class WelcomeListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The chat.")]


async def welcome_list(ctx: OpContext, req: WelcomeListReq) -> Page[BotWelcomeMessage]:
    """A chat's bot welcome-message templates. Layer 229; exits 13."""
    _bots.unsupported("bot welcome list")
    raise AssertionError  # pragma: no cover - unreachable; keeps mypy happy


SPEC_WELCOME_LIST = OperationSpec(
    id="bot.welcome.list",
    request=WelcomeListReq,
    response=Page[BotWelcomeMessage],
    impl=welcome_list,
    summary="List a chat's bot welcome-message templates",
    paginated=PageKind.LOCAL,
    columns=("id", "text"),
    headers=("ID", "Text"),
    example={"items": [], "has_more": False},
    example_args="bot welcome list @mygroup",
    tags=frozenset({"not-supported"}),
)


class WelcomeSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The chat.")]
    text: Annotated[str, arg(1, metavar="TEXT", help="Welcome text.")]
    id: Annotated[int | None, opt("--id", metavar="ID", help="Edit this one instead.")] = None
    keyboard: Annotated[
        str | None, opt("--keyboard", metavar="PATH", kind="path", help="JSON keyboard.")
    ] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Text formatting.")] = None


async def welcome_set(ctx: OpContext, req: WelcomeSetReq) -> WelcomeSet:
    """Add or edit a chat's bot welcome message. Layer 229; exits 13."""
    _bots.unsupported("bot welcome set")
    raise AssertionError  # pragma: no cover - unreachable; keeps mypy happy


SPEC_WELCOME_SET = OperationSpec(
    id="bot.welcome.set",
    request=WelcomeSetReq,
    response=WelcomeSet,
    impl=welcome_set,
    summary="Add or edit a chat's bot welcome message",
    mutating=True,
    columns=("chat_id", "id", "text"),
    headers=("Chat", "ID", "Text"),
    example={"chat_id": -1001, "id": 0, "text": "Welcome!"},
    example_args="bot welcome set @mygroup Welcome!",
    tags=frozenset({"not-supported"}),
)


class WelcomeDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The chat.")]
    id: Annotated[
        list[int], arg(1, metavar="ID", required=False, variadic=True, help="Welcome message ids.")
    ] = []
    delete_all: Annotated[bool, opt("--all", help="Delete every welcome message.")] = False


async def welcome_delete(ctx: OpContext, req: WelcomeDeleteReq) -> WelcomeDeleted:
    """Delete a chat's bot welcome messages. Layer 229; exits 13."""
    _bots.unsupported("bot welcome delete")
    raise AssertionError  # pragma: no cover - unreachable; keeps mypy happy


SPEC_WELCOME_DELETE = OperationSpec(
    id="bot.welcome.delete",
    request=WelcomeDeleteReq,
    response=WelcomeDeleted,
    impl=welcome_delete,
    summary="Delete one or all of a chat's bot welcome messages",
    mutating=True,
    destructive=True,
    columns=("chat_id", "deleted"),
    headers=("Chat", "Deleted"),
    example={"chat_id": -1001, "deleted": 0},
    example_args="bot welcome delete @mygroup 1",
    tags=frozenset({"not-supported"}),
)
