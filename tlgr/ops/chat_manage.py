"""Making a chat, and everything on its Manage screen.

`chat create`, `chat edit`, `chat convert`, `chat setting get/set`,
`chat username *`, `chat photo *`, `chat send-as *` and `chat discussion *`.

The Manage screen is one screen in the GUI and roughly twenty separate
MTProto methods underneath, which is why `chat setting set` is a *batch*:
flags are applied in a fixed order, a toggle already in the requested state is
reported as `already` and not sent, and each failure is reported per key so
one refusal — usually a boost level or a capability flag — does not hide the
nine changes that did land. `chat setting get` prints the same key names
without the leading dashes, so its output round-trips back into its input.

Two peer-shape rules run through the module. A basic group accepts only a
title, an about and a photo; everything else needs a supergroup, and the
commands say so and point at `chat convert` rather than migrating a group out
from under its owner. And a colour, an emoji status or an emoji pack is
boost-gated: the server refuses below the level, and the refusal is reported
per key with the level it wanted.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.timefmt import fmt_dt, parse_dt, parse_duration
from tlgr.models.admin import (
    ChatEditResult,
    ChatPhotoResult,
    CreatedChat,
    DiscussionCandidate,
    DiscussionResult,
    MigrateResult,
    MissingInvitee,
    SendAsPeer,
    SendAsResult,
    SettingResult,
    SettingsView,
    UsernameCheck,
    UsernameResult,
)
from tlgr.models.base import Request
from tlgr.models.peer import PeerRef
from tlgr.ops import _admin, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_CHAT: dict[str, Any] = {
    "id": -1001500,
    "type": "supergroup",
    "title": "News",
    "username": "mynews",
}

#: `--main-tab` values → the `ProfileTab*` constructor they name.
_PROFILE_TABS = {
    "posts": "ProfileTabPosts",
    "gifts": "ProfileTabGifts",
    "media": "ProfileTabMedia",
    "files": "ProfileTabFiles",
    "music": "ProfileTabMusic",
    "voice": "ProfileTabVoice",
    "links": "ProfileTabLinks",
    "gifs": "ProfileTabGifs",
}

#: The slow-mode ladder the server accepts. Anything else is rounded up to
#: the next rung and the rounding is reported, because silently accepting
#: `--slow-mode 45s` and applying 60 is a lie about what the chat now does.
_SLOW_MODE_LADDER = (0, 10, 30, 60, 300, 900, 3600)


def _tri(value: str | None) -> bool | None:
    """`on`/`off` → a bool; anything else is a usage error."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("on", "yes", "true", "1", "enable", "enabled"):
        return True
    if text in ("off", "no", "false", "0", "disable", "disabled"):
        return False
    raise UsageError(f"{value!r} is not on or off")


def _geo(value: str | None) -> Any:
    """`--geo 51.5,-0.12` → an `InputGeoPoint`; `off` → `InputGeoPointEmpty`."""
    from telethon.tl import types

    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("off", "none", "clear"):
        return types.InputGeoPointEmpty()
    parts = text.replace(";", ",").split(",")
    if len(parts) != 2:
        raise UsageError("--geo takes 'lat,lon'", field="geo")
    try:
        return types.InputGeoPoint(lat=float(parts[0]), long=float(parts[1]))
    except ValueError as exc:
        raise UsageError("--geo takes 'lat,lon' as two numbers", field="geo") from exc


def _int_or_off(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("off", "none", "clear", ""):
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise UsageError(
            f"--{field.replace('_', '-')} takes a number or 'off'", field=field
        ) from exc


def _slow_mode(value: str | None, ctx: OpContext) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("off", "none", "0"):
        return 0
    seconds = parse_duration(text)
    if seconds is None:
        raise UsageError("--slow-mode takes a duration or 'off'", field="slow_mode")
    wanted = int(seconds)
    for rung in _SLOW_MODE_LADDER:
        if wanted <= rung:
            if rung != wanted:
                ctx.warn(f"slow mode has a fixed ladder; {wanted}s was rounded up to {rung}s")
            return rung
    ctx.warn("slow mode caps at 1h; the request was clamped")
    return _SLOW_MODE_LADDER[-1]


# ---------------------------------------------------------------------------
# chat create
# ---------------------------------------------------------------------------


class CreateReq(Request):
    title: Annotated[str, arg(0, metavar="TITLE", help="The chat's title.")]
    type: Annotated[
        str,
        choice(
            "group",
            "supergroup",
            "channel",
            "forum",
            help="Peer shape. `group` is the legacy basic group.",
        ),
    ] = "supergroup"
    about: Annotated[str, opt("--about", metavar="TEXT", help="Description.")] = ""
    members: Annotated[
        list[PeerRef], opt("--members", metavar="USER", kind="user", help="Seed members.")
    ] = []
    photo: Annotated[
        str | None, opt("--photo", metavar="PATH", kind="path", help="Set the photo afterwards.")
    ] = None
    username: Annotated[
        str | None, opt("--username", metavar="NAME", help="Claim a public username afterwards.")
    ] = None
    ttl: Annotated[
        str | None, opt("--ttl", metavar="DURATION", help="Auto-delete timer at creation.")
    ] = None
    geo: Annotated[
        str | None, opt("--geo", metavar="LAT,LON", help="Create a location-based group.")
    ] = None
    address: Annotated[
        str, opt("--address", metavar="TEXT", help="Street address that goes with --geo.")
    ] = ""
    tabs: Annotated[bool, opt("--tabs", help="With --type forum: the tabbed topic UI.")] = False
    for_import: Annotated[
        bool, opt("--for-import", help="Destination for a history import (see `chat import`).")
    ] = False
    forward_history: Annotated[
        int, opt("--forward-history", metavar="N", help="Basic groups: history new members see.")
    ] = 0


async def create_chat(ctx: OpContext, req: CreateReq) -> CreatedChat:
    """Create a basic group, supergroup, broadcast channel or forum.

    `missing` carries `messages.invitedUsers.missing_invitees` verbatim: a
    creation that quietly added three of five seed members and reported
    success is a creation nobody can trust.
    """
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    from tlgr.ops.chat_member import _missing

    handle = _admin.client(ctx)
    ttl = int(parse_duration(req.ttl) or 0) if req.ttl else None
    if req.type == "group" and (req.username or req.geo):
        raise UsageError(
            "a basic group cannot have a username or a location; "
            "use --type supergroup, or `chat convert <chat> supergroup` later",
            field="type",
        )

    missing: list[MissingInvitee] = []
    if req.type == "group":
        users = [_admin.input_user(await _send.resolve(ctx, ref)) for ref in req.members]
        reply = await handle(msg_fn.CreateChatRequest(users=users, title=req.title, ttl_period=ttl))
        missing = _missing(reply)
        updates = getattr(reply, "updates", reply)
    else:
        updates = await handle(
            chan_fn.CreateChannelRequest(
                title=req.title,
                about=req.about,
                megagroup=req.type in ("supergroup", "forum") or None,
                broadcast=req.type == "channel" or None,
                forum=req.type == "forum" or None,
                for_import=req.for_import or None,
                geo_point=_geo(req.geo),
                address=req.address or None,
                ttl_period=ttl,
            )
        )
    chats = list(getattr(updates, "chats", None) or [])
    entity = chats[0] if chats else None
    chat_id = peer_id_of(entity) if entity is not None else 0
    result = CreatedChat(
        id=chat_id or 0,
        type=req.type,
        title=req.title,
        added=[],
        missing=missing,
    )

    if req.type != "group" and req.members:
        peer = await _send.resolve(ctx, str(result.id))
        users = [_admin.input_user(await _send.resolve(ctx, ref)) for ref in req.members]
        invited = await handle(
            chan_fn.InviteToChannelRequest(channel=_admin.input_channel(peer), users=users)
        )
        result.missing = _missing(invited)
    refused = {item.user_id for item in result.missing}
    result.added = [
        abs(peer_id_of(await _send.resolve(ctx, ref)) or 0)
        for ref in req.members
        if abs(peer_id_of(await _send.resolve(ctx, ref)) or 0) not in refused
    ]

    if req.username and result.id:
        peer = await _send.resolve(ctx, str(result.id))
        await handle(
            chan_fn.UpdateUsernameRequest(channel=_admin.input_channel(peer), username=req.username)
        )
        result.username = req.username
    if req.photo and result.id:
        peer = await _send.resolve(ctx, str(result.id))
        await _set_photo(ctx, peer, file=req.photo)
    ctx.emit("chat_created", {"chat_id": result.id, "type": req.type})
    return result


SPEC_CREATE = OperationSpec(
    id="chat.create",
    request=CreateReq,
    response=CreatedChat,
    impl=create_chat,
    summary="Create a basic group, supergroup, broadcast channel or forum",
    description=(
        "`--type supergroup` is what the GUI creates today; `group` is the "
        "legacy basic group, which cannot have a username or a location. "
        "`missing` carries `messages.invitedUsers.missing_invitees` "
        "verbatim, and the command exits 1 when any seed member was refused."
    ),
    legacy_paths=("chat create",),
    mutating=True,
    columns=("id", "type", "title", "username"),
    example=_EXAMPLE_CHAT,
    example_args="chat create 'Release team' --type supergroup --members @alice",
    covers=(
        "groups-channels-admin.create-basic-group",
        "groups-channels-admin.create-channel",
        "groups-channels-admin.create-forum",
        "groups-channels-admin.create-geo-group",
        "groups-channels-admin.create-supergroup",
        "groups-channels-admin.create-with-autodelete",
        "location.geogroup-create",
    ),
    covers_partial=(
        "groups-channels-admin.add-members",
        "groups-channels-admin.add-members-failure-report",
    ),
    coverage_note="Seed members go in at creation; `chat member add` owns adding them later.",
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# chat edit
# ---------------------------------------------------------------------------


class EditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New title.")] = None
    about: Annotated[
        str | None, opt("--about", metavar="TEXT", help="New description, max 255 chars.")
    ] = None
    geo: Annotated[
        str | None, opt("--geo", metavar="LAT,LON", help="Geogroup location; `off` clears it.")
    ] = None
    address: Annotated[str, opt("--address", metavar="TEXT", help="Street address.")] = ""
    color: Annotated[
        str | None, opt("--color", metavar="ID", help="Message accent palette id, or `off`.")
    ] = None
    color_emoji: Annotated[
        str | None, opt("--color-emoji", metavar="ID", help="Background emoji for --color.")
    ] = None
    profile_color: Annotated[
        str | None, opt("--profile-color", metavar="ID", help="Profile palette id, or `off`.")
    ] = None
    profile_color_emoji: Annotated[
        str | None,
        opt("--profile-color-emoji", metavar="ID", help="Background emoji for --profile-color."),
    ] = None
    emoji_status: Annotated[
        str | None, opt("--emoji-status", metavar="ID", help="Emoji status, or `off`.")
    ] = None
    emoji_status_until: Annotated[
        str | None, opt("--emoji-status-until", metavar="WHEN", help="Expiry for --emoji-status.")
    ] = None
    main_tab: Annotated[
        str | None,
        choice(*_PROFILE_TABS, help="Default profile tab (channels.setMainProfileTab)."),
    ] = None
    palettes: Annotated[
        bool, opt("--palettes", help="Do not edit: print the palettes and the level each needs.")
    ] = False


async def edit_chat(ctx: OpContext, req: EditReq) -> ChatEditResult:
    """One request per changed field, in a fixed order, reported in `changed`."""
    from telethon.tl import types
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import help as help_fn
    from telethon.tl.functions import messages as msg_fn

    handle = _admin.client(ctx)
    if req.palettes:
        colors = await handle(help_fn.GetPeerColorsRequest(hash=0))
        profile = await handle(help_fn.GetPeerProfileColorsRequest(hash=0))
        rows: list[dict[str, Any]] = []
        for kind, reply in (("message", colors), ("profile", profile)):
            for option in getattr(reply, "colors", None) or []:
                rows.append(
                    {
                        "kind": kind,
                        "id": int(getattr(option, "color_id", 0) or 0),
                        "channel_min_level": int(getattr(option, "channel_min_level", 0) or 0),
                        "group_min_level": int(getattr(option, "group_min_level", 0) or 0),
                        "hidden": bool(getattr(option, "hidden", False)),
                    }
                )
        return ChatEditResult(id=0, palettes=rows)

    peer = await _send.resolve(ctx, req.chat)
    chat_id = peer_id_of(peer) or 0
    channel = _admin.input_channel(peer) if _admin.is_channel(peer) else None
    changed: list[str] = []

    if req.title is not None:
        if channel is not None:
            await handle(chan_fn.EditTitleRequest(channel=channel, title=req.title))
        else:
            await handle(
                msg_fn.EditChatTitleRequest(chat_id=_admin.small_chat_id(peer), title=req.title)
            )
        changed.append("title")
    if req.about is not None:
        # `messages.editChatAbout` works for every peer shape, which is why
        # there is no channel/basic-group split here.
        await handle(msg_fn.EditChatAboutRequest(peer=peer, about=req.about))
        changed.append("about")
    if req.geo is not None:
        if channel is None:
            raise UsageError("only a supergroup can have a location", field="geo")
        await handle(
            chan_fn.EditLocationRequest(
                channel=channel, geo_point=_geo(req.geo), address=req.address
            )
        )
        changed.append("geo")
    if req.color is not None or req.color_emoji is not None:
        if channel is None:
            raise UsageError("colours are a channel/supergroup feature", field="color")
        await handle(
            chan_fn.UpdateColorRequest(
                channel=channel,
                color=_int_or_off(req.color, "color"),
                background_emoji_id=_int_or_off(req.color_emoji, "color_emoji"),
            )
        )
        changed.append("color")
    if req.profile_color is not None or req.profile_color_emoji is not None:
        if channel is None:
            raise UsageError("colours are a channel/supergroup feature", field="profile_color")
        await handle(
            chan_fn.UpdateColorRequest(
                channel=channel,
                for_profile=True,
                color=_int_or_off(req.profile_color, "profile_color"),
                background_emoji_id=_int_or_off(req.profile_color_emoji, "profile_color_emoji"),
            )
        )
        changed.append("profile_color")
    if req.emoji_status is not None:
        if channel is None:
            raise UsageError(
                "an emoji status is a channel/supergroup feature", field="emoji_status"
            )
        document = _int_or_off(req.emoji_status, "emoji_status") or 0
        until = None
        if req.emoji_status_until:
            seconds = parse_duration(req.emoji_status_until)
            if seconds is not None:
                from datetime import datetime, timedelta, timezone

                until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            else:
                until = parse_dt(req.emoji_status_until)
        status: Any = (
            types.EmojiStatusEmpty()
            if not document
            else types.EmojiStatus(document_id=document, until=until)
        )
        await handle(chan_fn.UpdateEmojiStatusRequest(channel=channel, emoji_status=status))
        changed.append("emoji_status")
        ctx.warn(
            "a collectible emoji status and a custom profile palette are mutually "
            "exclusive; setting one clears the other"
        )
    if req.main_tab is not None:
        if channel is None:
            raise UsageError("the profile tab is a channel/supergroup feature", field="main_tab")
        await handle(
            chan_fn.SetMainProfileTabRequest(
                channel=channel, tab=getattr(types, _PROFILE_TABS[req.main_tab])()
            )
        )
        changed.append("main_tab")

    if not changed:
        raise UsageError("nothing to change; --palettes prints the colour options", field="title")
    ctx.emit("chat_edited", {"chat_id": chat_id, "changed": changed})
    return ChatEditResult(id=chat_id, changed=changed)


SPEC_EDIT = OperationSpec(
    id="chat.edit",
    request=EditReq,
    response=ChatEditResult,
    impl=edit_chat,
    summary="Edit a group/channel profile: title, about, location, colors, emoji status, tab",
    description=(
        "One request per changed field, applied in a fixed order and "
        "reported in `changed`. Colours, emoji statuses and emoji packs are "
        "boost-gated: `--palettes` prints every option with the level it "
        "needs, without editing anything. A basic group accepts only "
        "`--title` and `--about`."
    ),
    mutating=True,
    columns=("id", "changed"),
    example={"id": -1001500, "changed": ["title", "about"]},
    example_args="chat edit @mygroup --title 'Release team' --about 'Ship it'",
    covers=(
        "groups-channels-admin.channel-emoji-status",
        "groups-channels-admin.edit-about",
        "groups-channels-admin.edit-title",
        "groups-channels-admin.main-profile-tab",
        "groups-channels-admin.peer-color-message",
        "groups-channels-admin.peer-color-profile",
        "groups-channels-admin.set-location",
        "location.channel-geo",
    ),
)


# ---------------------------------------------------------------------------
# chat convert
# ---------------------------------------------------------------------------


class ConvertReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The chat to convert.")]
    target: Annotated[
        str,
        arg(1, metavar="TARGET", help="supergroup (from a basic group) or gigagroup."),
    ] = "supergroup"


async def convert_chat(ctx: OpContext, req: ConvertReq) -> MigrateResult:
    """Basic group → supergroup, or supergroup → gigagroup. One way, both."""
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    target = req.target.strip().lower()
    if target not in ("supergroup", "gigagroup"):
        raise UsageError("target is 'supergroup' or 'gigagroup'", field="target")
    peer = await _send.resolve(ctx, req.chat)
    old_id = peer_id_of(peer) or 0
    handle = _admin.client(ctx)
    if target == "gigagroup":
        updates = await handle(
            chan_fn.ConvertToGigagroupRequest(channel=_admin.input_channel(peer))
        )
        chats = list(getattr(updates, "chats", None) or [])
        new_id = peer_id_of(chats[0]) if chats else old_id
        ctx.emit("chat_converted", {"chat_id": new_id, "type": "gigagroup"})
        return MigrateResult(old_chat_id=old_id, chat_id=new_id or old_id, type="gigagroup")

    if _admin.is_channel(peer):
        raise UsageError(
            "this is already a supergroup or channel; only a basic group migrates",
            field="chat",
        )
    updates = await handle(msg_fn.MigrateChatRequest(chat_id=_admin.small_chat_id(peer)))
    new_id = old_id
    for chat in getattr(updates, "chats", None) or []:
        if type(chat).__name__ == "Channel":
            new_id = peer_id_of(chat) or old_id
    ctx.emit("chat_converted", {"chat_id": new_id, "type": "supergroup"})
    return MigrateResult(old_chat_id=old_id, chat_id=new_id, type="supergroup")


SPEC_CONVERT = OperationSpec(
    id="chat.convert",
    request=ConvertReq,
    response=MigrateResult,
    impl=convert_chat,
    summary="Convert a basic group to a supergroup, or a supergroup to a gigagroup",
    description=(
        "Both conversions are one-way. Both ids are reported, because the "
        "history stays in the old peer (`chat.migrated_to` points at the new "
        "one). Supergroup-only commands offer `--upgrade` rather than "
        "migrating a chat out from under its owner."
    ),
    mutating=True,
    destructive=True,
    columns=("old_chat_id", "chat_id", "type"),
    example={"old_chat_id": -1500, "chat_id": -1001500, "type": "supergroup"},
    example_args="chat convert @mygroup supergroup --yes",
    covers=(
        "groups-channels-admin.convert-to-gigagroup",
        "groups-channels-admin.upgrade-basic-to-supergroup",
    ),
)


# ---------------------------------------------------------------------------
# chat setting get / set
# ---------------------------------------------------------------------------


class SettingGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]


def _reactions_view(raw: Any) -> tuple[str | None, list[str]]:
    kind = type(raw).__name__
    if kind == "ChatReactionsAll":
        return "all", []
    if kind == "ChatReactionsNone":
        return "none", []
    if kind == "ChatReactionsSome":
        names = [
            str(getattr(item, "emoticon", None) or getattr(item, "document_id", ""))
            for item in (getattr(raw, "reactions", None) or [])
        ]
        return "some", names
    return None, []


async def get_settings(ctx: OpContext, req: SettingGetReq) -> SettingsView:
    """Every administrable toggle, keyed the way `chat setting set` spells it."""
    peer = await _send.resolve(ctx, req.chat)
    full, entity, _entities = await _admin.full_chat(ctx, peer)
    rights = _admin_rights_of(entity)
    reactions, reaction_names = _reactions_view(getattr(full, "available_reactions", None))
    sticker = getattr(full, "stickerset", None)
    emojiset = getattr(full, "emojiset", None)
    tab = getattr(full, "main_tab", None)
    view = SettingsView(
        chat_id=peer_id_of(peer) or 0,
        slow_mode=getattr(full, "slowmode_seconds", None),
        prehistory=(
            None
            if getattr(full, "hidden_prehistory", None) is None
            else ("hidden" if full.hidden_prehistory else "visible")
        ),
        join_to_send=getattr(entity, "join_to_send", None),
        join_request=getattr(entity, "join_request", None),
        guard_bot=getattr(full, "guard_bot_id", None),
        noforwards=getattr(entity, "noforwards", None),
        antispam=getattr(full, "antispam", None),
        hidden_members=getattr(full, "participants_hidden", None),
        signatures=getattr(entity, "signatures", None),
        signature_profiles=getattr(entity, "signature_profiles", None),
        forum=getattr(entity, "forum", None),
        forum_tabs=(
            None
            if getattr(entity, "forum", None) is None
            else ("tabs" if getattr(entity, "forum_tabs", False) else "list")
        ),
        view_as=(
            None
            if getattr(full, "view_forum_as_messages", None) is None
            else ("messages" if full.view_forum_as_messages else "topics")
        ),
        autotranslate=getattr(entity, "autotranslation", None),
        ads=(
            None
            if getattr(full, "restricted_sponsored", None) is None
            else not full.restricted_sponsored
        ),
        reactions=reactions,
        reactions_list=reaction_names,
        reactions_limit=getattr(full, "reactions_limit", None),
        paid_reactions=getattr(full, "paid_reactions_available", None),
        sticker_set=getattr(sticker, "short_name", None),
        emoji_set=getattr(emojiset, "short_name", None),
        paid_messages_stars=getattr(full, "send_paid_messages_stars", None),
        direct_messages=getattr(entity, "broadcast_messages_allowed", None),
    )
    if tab is not None:
        view.gated_by["main_tab"] = type(tab).__name__
    change_info = rights.get("change_info", False) or rights.get("creator", False)
    for key in (
        "slow_mode",
        "prehistory",
        "join_to_send",
        "join_request",
        "antispam",
        "hidden_members",
        "signatures",
        "signature_profiles",
        "reactions",
        "reactions_limit",
        "sticker_set",
        "emoji_set",
    ):
        view.available[key] = bool(change_info)
    for key in ("forum", "noforwards", "ads", "direct_messages", "autotranslate"):
        view.available[key] = bool(rights.get("creator", False))
    for key, level in (
        ("autotranslate", "channel_autotranslation_level_min"),
        ("emoji_set", "group_emoji_stickers_level_min"),
        ("reactions_limit", "channel_custom_reactions_level_min"),
        ("ads", "channel_restrict_sponsored_level_min"),
    ):
        view.gated_by[key] = f"boost level: {level}"
    if not getattr(full, "can_set_stickers", False):
        view.gated_by["sticker_set"] = "needs can_set_stickers"
    return view


def _admin_rights_of(entity: Any) -> dict[str, bool]:
    rights = getattr(entity, "admin_rights", None)
    out = {"creator": bool(getattr(entity, "creator", False))}
    for name in ("change_info", "ban_users", "invite_users", "post_messages"):
        out[name] = bool(getattr(rights, name, False)) or out["creator"]
    return out


SPEC_SETTING_GET = OperationSpec(
    id="chat.setting.get",
    request=SettingGetReq,
    response=SettingsView,
    impl=get_settings,
    summary="Print every administrable policy toggle with its current value",
    description=(
        "The key names are exactly `chat setting set`'s flag names without "
        "the leading dashes, so the output round-trips into the input. "
        "`available` says whether *you* may change each key, and `gated_by` "
        "names the capability flag or boost level that blocks it."
    ),
    aliases=("chat.settings",),
    columns=("chat_id", "slow_mode", "prehistory", "forum"),
    example={"chat_id": -1001500, "slow_mode": 30, "prehistory": "visible", "forum": False},
    example_args="chat setting get @mygroup",
    covers=(
        "groups-channels-admin.antispam",
        "groups-channels-admin.autotranslation",
        "groups-channels-admin.content-protection",
        "groups-channels-admin.forum-toggle",
        "groups-channels-admin.gift-notifications",
        "groups-channels-admin.group-emoji-set",
        "groups-channels-admin.hidden-members",
        "groups-channels-admin.prehistory-visibility",
        "groups-channels-admin.reactions-settings",
        "groups-channels-admin.signatures",
        "groups-channels-admin.slow-mode",
        "groups-channels-admin.toggle-join-to-send",
    ),
    covers_partial=(
        "groups-channels-admin.channel-direct-messages",
        "groups-channels-admin.group-sticker-set",
        "groups-channels-admin.paid-messages-price",
        "groups-channels-admin.restrict-sponsored",
        "groups-channels-admin.toggle-join-request",
        "groups-channels-admin.view-forum-as-messages",
    ),
    coverage_note="Reading half of the Manage screen; `chat setting set` writes each key.",
)


class SettingSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    slow_mode: Annotated[
        str | None, opt("--slow-mode", metavar="DURATION", help="Seconds between messages, or off.")
    ] = None
    prehistory: Annotated[
        str | None, choice("visible", "hidden", help="History visibility for new members.")
    ] = None
    join_to_send: Annotated[
        str | None, opt("--join-to-send", metavar="ON|OFF", help="Require joining before sending.")
    ] = None
    join_request: Annotated[
        str | None, opt("--join-request", metavar="ON|OFF", help="Require approval to join.")
    ] = None
    guard_bot: Annotated[
        PeerRef | None,
        opt("--guard-bot", metavar="BOT", kind="user", help="Bot that handles the queue."),
    ] = None
    apply_to_links: Annotated[
        bool, opt("--apply-to-links", help="Apply --join-request to existing invite links too.")
    ] = False
    protect: Annotated[
        str | None,
        opt("--protect", "--noforwards", metavar="ON|OFF", help="Restrict saving and forwarding."),
    ] = None
    antispam: Annotated[
        str | None, opt("--antispam", metavar="ON|OFF", help="Aggressive anti-spam.")
    ] = None
    hidden_members: Annotated[
        str | None,
        opt("--hidden-members", metavar="ON|OFF", help="Hide the member list from non-admins."),
    ] = None
    signatures: Annotated[
        str | None, opt("--signatures", metavar="ON|OFF", help="Sign channel posts.")
    ] = None
    signature_profiles: Annotated[
        str | None,
        opt("--signature-profiles", metavar="ON|OFF", help="Link signatures to profiles."),
    ] = None
    forum: Annotated[
        str | None, opt("--forum", metavar="ON|OFF", help="Enable or disable Topics.")
    ] = None
    forum_tabs: Annotated[str | None, choice("tabs", "list", help="Tabbed vs list topic UI.")] = (
        None
    )
    view_as: Annotated[
        str | None, choice("messages", "topics", help="My own view preference for this forum.")
    ] = None
    autotranslate: Annotated[
        str | None, opt("--autotranslate", metavar="ON|OFF", help="Channel auto-translation.")
    ] = None
    ads: Annotated[
        str | None, opt("--ads", metavar="ON|OFF", help="`off` disables sponsored messages.")
    ] = None
    reactions: Annotated[
        str | None,
        opt("--reactions", metavar="ALL|NONE|LIST", help="Allowed reactions."),
    ] = None
    allow_custom_reactions: Annotated[
        str | None,
        opt("--allow-custom-reactions", metavar="ON|OFF", help="With --reactions all."),
    ] = None
    reactions_limit: Annotated[
        int | None,
        opt("--reactions-limit", metavar="N", help="Max distinct reactions per message."),
    ] = None
    paid_reactions: Annotated[
        str | None, opt("--paid-reactions", metavar="ON|OFF", help="Enable Stars reactions.")
    ] = None
    sticker_set: Annotated[
        str | None, opt("--sticker-set", metavar="NAME", help="Group sticker set short name.")
    ] = None
    emoji_set: Annotated[
        str | None, opt("--emoji-set", metavar="NAME", help="Group custom-emoji pack short name.")
    ] = None
    paid_messages: Annotated[
        str | None, opt("--paid-messages", metavar="N|OFF", help="Stars per incoming message.")
    ] = None
    direct_messages: Annotated[
        str | None,
        opt("--direct-messages", metavar="ON|OFF", help="Enable the channel's direct messages."),
    ] = None
    gift_notifications: Annotated[
        str | None,
        opt("--gift-notifications", metavar="ON|OFF", help="Star-gift notices in the channel."),
    ] = None


def _stickerset(value: str) -> Any:
    from telethon.tl import types

    if value.strip().lower() in ("off", "none", "clear", ""):
        return types.InputStickerSetEmpty()
    return types.InputStickerSetShortName(short_name=value.strip().lstrip("@"))


def _reactions(value: str, allow_custom: bool | None) -> Any:
    from telethon.tl import types

    text = value.strip().lower()
    if text == "all":
        return types.ChatReactionsAll(allow_custom=allow_custom or None)
    if text in ("none", "off"):
        return types.ChatReactionsNone()
    reactions: list[Any] = []
    for item in value.replace(",", " ").split():
        token = item.strip()
        if not token:
            continue
        if token.isdigit():
            reactions.append(types.ReactionCustomEmoji(document_id=int(token)))
        else:
            reactions.append(types.ReactionEmoji(emoticon=token))
    return types.ChatReactionsSome(reactions=reactions)


async def set_settings(ctx: OpContext, req: SettingSetReq) -> SettingResult:
    """Apply the Manage screen's toggles, one request per key, in a fixed order.

    A toggle already in the requested state is not sent and is reported in
    `already`; a key the server refuses is reported in `failed` and the rest
    still run. Aborting on the first refusal would make a boost-gated colour
    hide nine successful changes.
    """
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn
    from telethon.tl.functions import payments as pay_fn

    peer = await _send.resolve(ctx, req.chat)
    current = await get_settings(ctx, SettingGetReq(chat=req.chat))
    handle = _admin.client(ctx)
    channel = _admin.input_channel(peer) if _admin.is_channel(peer) else None
    result = SettingResult(chat_id=peer_id_of(peer) or 0)

    async def apply(key: str, wanted: Any, is_current: Any, build: Any) -> None:
        if wanted is None:
            return
        if wanted == is_current:
            result.already.append(key)
            return
        try:
            await handle(build())
        except Exception as exc:  # one refusal must not hide the other keys
            result.failed[key] = f"{type(exc).__name__}: {exc}"
            return
        result.changed.append(key)

    if channel is None and any(
        value is not None
        for value in (
            req.slow_mode,
            req.prehistory,
            req.join_to_send,
            req.join_request,
            req.antispam,
            req.hidden_members,
            req.signatures,
            req.forum,
        )
    ):
        raise UsageError(
            "these settings need a supergroup or channel; `chat convert <chat> supergroup` first",
            field="chat",
        )

    seconds = _slow_mode(req.slow_mode, ctx)
    await apply(
        "slow_mode",
        seconds,
        current.slow_mode,
        lambda: chan_fn.ToggleSlowModeRequest(channel=channel, seconds=seconds or 0),
    )
    prehistory = None if req.prehistory is None else (req.prehistory == "hidden")
    await apply(
        "prehistory",
        prehistory,
        None if current.prehistory is None else current.prehistory == "hidden",
        lambda: chan_fn.TogglePreHistoryHiddenRequest(channel=channel, enabled=bool(prehistory)),
    )
    join_to_send = _tri(req.join_to_send)
    await apply(
        "join_to_send",
        join_to_send,
        current.join_to_send,
        lambda: chan_fn.ToggleJoinToSendRequest(channel=channel, enabled=bool(join_to_send)),
    )
    join_request = _tri(req.join_request)
    guard = (
        _admin.input_user(await _send.resolve(ctx, req.guard_bot))
        if req.guard_bot is not None
        else None
    )
    await apply(
        "join_request",
        join_request,
        current.join_request,
        lambda: chan_fn.ToggleJoinRequestRequest(
            channel=channel,
            enabled=bool(join_request),
            apply_to_invites=req.apply_to_links or None,
            guard_bot=guard,
        ),
    )
    protect = _tri(req.protect)
    await apply(
        "protect",
        protect,
        current.noforwards,
        lambda: msg_fn.ToggleNoForwardsRequest(peer=peer, enabled=bool(protect)),
    )
    antispam = _tri(req.antispam)
    await apply(
        "antispam",
        antispam,
        current.antispam,
        lambda: chan_fn.ToggleAntiSpamRequest(channel=channel, enabled=bool(antispam)),
    )
    hidden = _tri(req.hidden_members)
    await apply(
        "hidden_members",
        hidden,
        current.hidden_members,
        lambda: chan_fn.ToggleParticipantsHiddenRequest(channel=channel, enabled=bool(hidden)),
    )
    signatures = _tri(req.signatures)
    profiles = _tri(req.signature_profiles)
    if signatures is not None or profiles is not None:
        await apply(
            "signatures",
            (signatures if signatures is not None else True, profiles),
            (current.signatures, current.signature_profiles),
            lambda: chan_fn.ToggleSignaturesRequest(
                channel=channel,
                signatures_enabled=(signatures if signatures is not None else True) or None,
                profiles_enabled=profiles or None,
            ),
        )
    forum = _tri(req.forum)
    tabs = None if req.forum_tabs is None else req.forum_tabs == "tabs"
    if forum is not None or tabs is not None:
        await apply(
            "forum",
            (forum if forum is not None else current.forum, tabs),
            (current.forum, None if current.forum_tabs is None else current.forum_tabs == "tabs"),
            lambda: chan_fn.ToggleForumRequest(
                channel=channel,
                enabled=bool(forum if forum is not None else current.forum),
                tabs=bool(tabs),
            ),
        )
    view_as = None if req.view_as is None else req.view_as == "messages"
    await apply(
        "view_as",
        view_as,
        None if current.view_as is None else current.view_as == "messages",
        lambda: chan_fn.ToggleViewForumAsMessagesRequest(channel=channel, enabled=bool(view_as)),
    )
    autotranslate = _tri(req.autotranslate)
    await apply(
        "autotranslate",
        autotranslate,
        current.autotranslate,
        lambda: chan_fn.ToggleAutotranslationRequest(channel=channel, enabled=bool(autotranslate)),
    )
    ads = _tri(req.ads)
    await apply(
        "ads",
        ads,
        current.ads,
        lambda: chan_fn.RestrictSponsoredMessagesRequest(channel=channel, restricted=not ads),
    )
    if req.reactions is not None or req.reactions_limit is not None or req.paid_reactions:
        allow_custom = _tri(req.allow_custom_reactions)
        available = _reactions(req.reactions or "all", allow_custom)
        await apply(
            "reactions",
            (req.reactions, req.reactions_limit, _tri(req.paid_reactions)),
            (None, None, None),
            lambda: msg_fn.SetChatAvailableReactionsRequest(
                peer=peer,
                available_reactions=available,
                reactions_limit=req.reactions_limit,
                paid_enabled=_tri(req.paid_reactions),
            ),
        )
    if req.sticker_set is not None:
        await apply(
            "sticker_set",
            req.sticker_set,
            current.sticker_set,
            lambda: chan_fn.SetStickersRequest(
                channel=channel, stickerset=_stickerset(req.sticker_set or "")
            ),
        )
    if req.emoji_set is not None:
        await apply(
            "emoji_set",
            req.emoji_set,
            current.emoji_set,
            lambda: chan_fn.SetEmojiStickersRequest(
                channel=channel, stickerset=_stickerset(req.emoji_set or "")
            ),
        )
    if req.paid_messages is not None or req.direct_messages is not None:
        stars = _int_or_off(req.paid_messages, "paid_messages")
        allowed = _tri(req.direct_messages)
        await apply(
            "paid_messages",
            (stars, allowed),
            (current.paid_messages_stars, current.direct_messages),
            lambda: chan_fn.UpdatePaidMessagesPriceRequest(
                channel=channel,
                send_paid_messages_stars=stars or 0,
                broadcast_messages_allowed=allowed,
            ),
        )
    gifts = _tri(req.gift_notifications)
    await apply(
        "gift_notifications",
        gifts,
        current.gift_notifications,
        lambda: pay_fn.ToggleChatStarGiftNotificationsRequest(peer=peer, enabled=gifts),
    )

    if not (result.changed or result.already or result.failed):
        raise UsageError("nothing to change; `chat setting get` lists the keys", field="chat")
    if result.already and not result.changed:
        _admin.already(ctx)
    if result.changed:
        ctx.emit("chat_settings_changed", {"chat_id": result.chat_id, "changed": result.changed})
    return result


SPEC_SETTING_SET = OperationSpec(
    id="chat.setting.set",
    request=SettingSetReq,
    response=SettingResult,
    impl=set_settings,
    summary="Change group/channel policy toggles (the Manage screen)",
    description=(
        "Idempotent: a toggle already in the requested state is reported in "
        "`already` and never sent. Failures are reported per key in `failed` "
        "and do not stop the rest, because a boost-gated refusal must not "
        "hide the changes that did land. `--direct-messages` and "
        "`--paid-messages` are the same call."
    ),
    mutating=True,
    columns=("chat_id", "changed", "already"),
    example={"chat_id": -1001500, "changed": ["slow_mode"], "already": [], "failed": {}},
    example_args="chat setting set @mygroup --slow-mode 30s --hidden-members on",
    covers=(
        "dialogs.channel-autotranslation",
        "dialogs.forum-tabs-mode",
        "emoji.status-channel",
        "groups-channels-admin.channel-direct-messages",
        "groups-channels-admin.group-sticker-set",
        "groups-channels-admin.restrict-sponsored",
        "groups-channels-admin.toggle-join-request",
        "groups-channels-admin.view-forum-as-messages",
        "messages-core.paid-messages-group-price",
        "messages-core.translate-channel-autotranslation",
        "sticker.group-sticker-set",
    ),
    covers_partial=(
        "groups-channels-admin.antispam",
        "groups-channels-admin.autotranslation",
        "groups-channels-admin.channel-sponsored-messages",
        "groups-channels-admin.content-protection",
        "groups-channels-admin.forum-toggle",
        "groups-channels-admin.gift-notifications",
        "groups-channels-admin.group-emoji-set",
        "groups-channels-admin.hidden-members",
        "groups-channels-admin.paid-messages-price",
        "groups-channels-admin.prehistory-visibility",
        "groups-channels-admin.reactions-settings",
        "groups-channels-admin.signatures",
        "groups-channels-admin.slow-mode",
        "groups-channels-admin.toggle-join-to-send",
    ),
    coverage_note=(
        "The writing half of the Manage screen; `chat setting get` reads the "
        "same keys and owns most of these ids."
    ),
)


# ---------------------------------------------------------------------------
# chat username *
# ---------------------------------------------------------------------------


class UsernameGetReq(Request):
    username: Annotated[str, arg(0, metavar="USERNAME", help="The name to check.")]
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Check it for this existing peer."),
    ] = None


async def check_username(ctx: OpContext, req: UsernameGetReq) -> UsernameCheck:
    """Availability, and — when Fragment sells it — how to buy it."""
    from telethon.tl import types
    from telethon.tl.functions import channels as fn
    from telethon.tl.functions import fragment as frag_fn

    handle = _admin.client(ctx)
    name = req.username.lstrip("@")
    channel: Any = types.InputChannelEmpty()
    if req.chat is not None:
        channel = _admin.input_channel(await _send.resolve(ctx, req.chat))
    try:
        available = bool(await handle(fn.CheckUsernameRequest(channel=channel, username=name)))
        status = "available" if available else "occupied"
    except Exception as exc:
        message = str(exc)
        if "USERNAME_PURCHASE_AVAILABLE" in message:
            status, available = "purchasable", False
        elif "USERNAME_INVALID" in message:
            status, available = "invalid", False
        elif "USERNAME_OCCUPIED" in message:
            status, available = "occupied", False
        else:
            raise
    collectible = None
    if status == "purchasable":
        try:
            info = await handle(
                frag_fn.GetCollectibleInfoRequest(
                    collectible=types.InputCollectibleUsername(username=name)
                )
            )
            collectible = {
                "purchase_date": fmt_dt(getattr(info, "purchase_date", None)),
                "currency": str(getattr(info, "currency", "") or ""),
                "amount": int(getattr(info, "amount", 0) or 0),
                "crypto_currency": str(getattr(info, "crypto_currency", "") or ""),
                "crypto_amount": int(getattr(info, "crypto_amount", 0) or 0),
                "url": str(getattr(info, "url", "") or ""),
            }
        except Exception as exc:  # pragma: no cover - Fragment is optional
            ctx.warn(f"Fragment did not answer for @{name}: {exc}")
    return UsernameCheck(username=name, status=status, available=available, collectible=collectible)


SPEC_USERNAME_GET = OperationSpec(
    id="chat.username.get",
    request=UsernameGetReq,
    response=UsernameCheck,
    impl=check_username,
    summary="Check a username: availability, owner, and Fragment collectible info",
    description=(
        "`status` is available, occupied, invalid or purchasable. On "
        "USERNAME_PURCHASE_AVAILABLE the Fragment purchase date, currency, "
        "amount and URL are attached — buying happens on fragment.com, not "
        "through the API."
    ),
    columns=("username", "status", "available"),
    example={"username": "mynews", "status": "available", "available": True},
    example_args="chat username get mynews",
    covers=(
        "groups-channels-admin.check-username",
        "groups-channels-admin.collectible-username-info",
    ),
)


class UsernameSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    username: Annotated[
        str | None, arg(1, metavar="USERNAME", required=False, help="The public link to claim.")
    ] = None
    order: Annotated[
        str | None, opt("--order", metavar="LIST", help="Set the display order of the usernames.")
    ] = None
    upgrade: Annotated[
        bool, opt("--upgrade", help="Migrate a basic group to a supergroup first, if needed.")
    ] = False


async def set_username(ctx: OpContext, req: UsernameSetReq) -> UsernameResult:
    """Claim the public link, or reorder the additional usernames."""
    from telethon.tl.functions import channels as fn

    if bool(req.username) == bool(req.order):
        raise UsageError("give exactly one of <username> and --order", field="username")
    peer = await _send.resolve(ctx, req.chat)
    if not _admin.is_channel(peer):
        if not req.upgrade:
            raise UsageError(
                "a basic group cannot have a username; pass --upgrade to migrate it "
                "to a supergroup first (this is one-way)",
                field="chat",
            )
        migrated = await convert_chat(ctx, ConvertReq(chat=req.chat, target="supergroup"))
        peer = await _send.resolve(ctx, str(migrated.chat_id))
    channel = _admin.input_channel(peer)
    handle = _admin.client(ctx)
    if req.order:
        order = [item.strip().lstrip("@") for item in req.order.replace(",", " ").split() if item]
        await handle(fn.ReorderUsernamesRequest(channel=channel, order=order))
        return UsernameResult(chat_id=peer_id_of(peer) or 0, usernames=order)
    name = (req.username or "").lstrip("@")
    await handle(fn.UpdateUsernameRequest(channel=channel, username=name))
    ctx.emit("chat_username_changed", {"chat_id": peer_id_of(peer) or 0, "username": name})
    return UsernameResult(chat_id=peer_id_of(peer) or 0, username=name, link=f"https://t.me/{name}")


SPEC_USERNAME_SET = OperationSpec(
    id="chat.username.set",
    request=UsernameSetReq,
    response=UsernameResult,
    impl=set_username,
    summary="Set the public link, or the display order of the additional usernames",
    description=(
        "Exactly one of `<username>` and `--order` is required. A basic "
        "group has no username at all: `--upgrade` migrates it first, and "
        "never silently. Public groups are forced to a visible prehistory by "
        "the server, and the public-peer count is capped per account."
    ),
    mutating=True,
    columns=("chat_id", "username", "link"),
    example={"chat_id": -1001500, "username": "mynews", "link": "https://t.me/mynews"},
    example_args="chat username set @mygroup mynews",
    covers=(
        "groups-channels-admin.public-private-toggle",
        "groups-channels-admin.set-username",
        "groups-channels-admin.username-reorder",
    ),
    tags=frozenset({"visible-to-others"}),
)


class UsernameToggleReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    username: Annotated[str, arg(1, metavar="USERNAME", help="The additional username.")]
    state: Annotated[str, arg(2, metavar="ON|OFF", help="Activate or deactivate it.")]


async def toggle_username(ctx: OpContext, req: UsernameToggleReq) -> UsernameResult:
    """Activate or deactivate one additional (collectible) username."""
    from telethon.tl.functions import channels as fn

    peer = await _send.resolve(ctx, req.chat)
    active = _tri(req.state)
    name = req.username.lstrip("@")
    await _admin.client(ctx)(
        fn.ToggleUsernameRequest(
            channel=_admin.input_channel(peer), username=name, active=bool(active)
        )
    )
    return UsernameResult(chat_id=peer_id_of(peer) or 0, usernames=[name] if active else [])


SPEC_USERNAME_TOGGLE = OperationSpec(
    id="chat.username.toggle",
    request=UsernameToggleReq,
    response=UsernameResult,
    impl=toggle_username,
    summary="Activate or deactivate one additional username",
    mutating=True,
    columns=("chat_id", "usernames"),
    example={"chat_id": -1001500, "usernames": ["mynews"]},
    example_args="chat username toggle @mygroup mynews off",
    covers=("groups-channels-admin.username-toggle",),
)


class UsernameUnsetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    everything: Annotated[
        bool, opt("--all", help="Deactivate every username; collectibles stay reserved.")
    ] = False


async def unset_username(ctx: OpContext, req: UsernameUnsetReq) -> UsernameResult:
    """Make a chat private, and print the invite link that keeps it reachable."""
    from telethon.tl.functions import channels as fn
    from telethon.tl.functions import messages as msg_fn

    peer = await _send.resolve(ctx, req.chat)
    channel = _admin.input_channel(peer)
    handle = _admin.client(ctx)
    if req.everything:
        await handle(fn.DeactivateAllUsernamesRequest(channel=channel))
    else:
        await handle(fn.UpdateUsernameRequest(channel=channel, username=""))
    invite = await handle(msg_fn.ExportChatInviteRequest(peer=peer))
    return UsernameResult(
        chat_id=peer_id_of(peer) or 0,
        usernames=[],
        invite_link=str(getattr(invite, "link", "") or ""),
    )


SPEC_USERNAME_UNSET = OperationSpec(
    id="chat.username.unset",
    request=UsernameUnsetReq,
    response=UsernameResult,
    impl=unset_username,
    summary="Make a group/channel private by clearing its username(s)",
    description=(
        "Prints the private invite link afterwards, so the chat stays "
        "reachable rather than becoming unfindable in one command."
    ),
    mutating=True,
    columns=("chat_id", "invite_link"),
    example={"chat_id": -1001500, "usernames": [], "invite_link": "https://t.me/+AbCdEf"},
    example_args="chat username unset @mygroup",
    covers=("groups-channels-admin.username-deactivate-all",),
    covers_partial=("groups-channels-admin.public-private-toggle",),
    coverage_note="Going private is here; `chat username set` goes public and owns the id.",
)


# ---------------------------------------------------------------------------
# chat photo set / delete
# ---------------------------------------------------------------------------


async def _set_photo(
    ctx: OpContext,
    peer: Any,
    *,
    file: str | None = None,
    video: str | None = None,
    video_start: float | None = None,
    emoji_markup: int | None = None,
    emoji_bg: str = "",
) -> int | None:
    from telethon.tl import types
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    handle = _admin.client(ctx)
    markup = None
    if emoji_markup:
        colors = [int(c) for c in emoji_bg.replace(",", " ").split() if c.strip().isdigit()]
        markup = types.VideoSizeEmojiMarkup(
            emoji_id=emoji_markup, background_colors=colors or [0xFFFFFF]
        )
    # The file pipeline is a service on the context, not an import: `ops/`
    # may not reach into `daemon/` (§2.2).
    upload: Any = getattr(ctx, "upload_file", None)
    if (file or video) and upload is None:  # pragma: no cover - the daemon supplies it
        raise UsageError("this context cannot upload files")
    uploaded_file = await upload(file) if file else None
    uploaded_video = await upload(video) if video else None
    photo: Any = types.InputChatUploadedPhoto(
        file=uploaded_file,
        video=uploaded_video,
        video_start_ts=video_start,
        video_emoji_markup=markup,
    )
    if _admin.is_channel(peer):
        updates = await handle(
            chan_fn.EditPhotoRequest(channel=_admin.input_channel(peer), photo=photo)
        )
    else:
        updates = await handle(
            msg_fn.EditChatPhotoRequest(chat_id=_admin.small_chat_id(peer), photo=photo)
        )
    for chat in getattr(updates, "chats", None) or []:
        candidate = getattr(getattr(chat, "photo", None), "photo_id", None)
        if candidate:
            return int(candidate)
    return None


class PhotoSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    file: Annotated[
        str | None, arg(1, metavar="FILE", kind="path", required=False, help="The still image.")
    ] = None
    video: Annotated[
        str | None, opt("--video", metavar="PATH", kind="path", help="Animated avatar.")
    ] = None
    video_start: Annotated[
        float | None, opt("--video-start", metavar="SECONDS", help="Still frame from --video.")
    ] = None
    emoji_markup: Annotated[
        int | None, opt("--emoji-markup", metavar="ID", help="Build the avatar from an emoji.")
    ] = None
    emoji_bg: Annotated[
        str, opt("--emoji-bg", metavar="COLORS", help="Background palette for --emoji-markup.")
    ] = ""


async def set_photo(ctx: OpContext, req: PhotoSetReq) -> ChatPhotoResult:
    """Set the chat photo: a still, a video avatar, or a custom emoji."""
    given = [bool(req.file), bool(req.video), bool(req.emoji_markup)]
    if sum(given) != 1:
        raise UsageError("give exactly one of <file>, --video and --emoji-markup", field="file")
    peer = await _send.resolve(ctx, req.chat)
    photo_id = await _set_photo(
        ctx,
        peer,
        file=req.file,
        video=req.video,
        video_start=req.video_start,
        emoji_markup=req.emoji_markup,
        emoji_bg=req.emoji_bg,
    )
    return ChatPhotoResult(chat_id=peer_id_of(peer) or 0, photo_id=photo_id)


SPEC_PHOTO_SET = OperationSpec(
    id="chat.photo.set",
    request=PhotoSetReq,
    response=ChatPhotoResult,
    impl=set_photo,
    summary="Set the group/channel photo (still, video or emoji avatar)",
    description="Exactly one of `<file>`, `--video` and `--emoji-markup` is required.",
    mutating=True,
    rate_class="file",
    columns=("chat_id", "photo_id"),
    example={"chat_id": -1001500, "photo_id": 5522},
    example_args="chat photo set @mygroup ./logo.png",
    covers=("chat.photo-set", "groups-channels-admin.edit-photo"),
    tags=frozenset({"visible-to-others"}),
)


class PhotoDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]


async def delete_photo(ctx: OpContext, req: PhotoDeleteReq) -> ChatPhotoResult:
    """Remove the chat photo."""
    from telethon.tl import types
    from telethon.tl.functions import channels as chan_fn
    from telethon.tl.functions import messages as msg_fn

    peer = await _send.resolve(ctx, req.chat)
    empty = types.InputChatPhotoEmpty()
    if _admin.is_channel(peer):
        await _admin.client(ctx)(
            chan_fn.EditPhotoRequest(channel=_admin.input_channel(peer), photo=empty)
        )
    else:
        await _admin.client(ctx)(
            msg_fn.EditChatPhotoRequest(chat_id=_admin.small_chat_id(peer), photo=empty)
        )
    return ChatPhotoResult(chat_id=peer_id_of(peer) or 0, ok=True)


SPEC_PHOTO_DELETE = OperationSpec(
    id="chat.photo.delete",
    request=PhotoDeleteReq,
    response=ChatPhotoResult,
    impl=delete_photo,
    summary="Remove the group/channel photo",
    mutating=True,
    columns=("chat_id", "ok"),
    example={"chat_id": -1001500, "ok": True},
    example_args="chat photo delete @mygroup",
    covers=("groups-channels-admin.remove-photo",),
)


# ---------------------------------------------------------------------------
# chat send-as list / set
# ---------------------------------------------------------------------------


class SendAsListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]


async def list_send_as(ctx: OpContext, req: SendAsListReq) -> list[SendAsPeer]:
    """The peers you may post as here, with the current default marked."""
    from telethon.tl.functions import channels as fn

    peer = await _send.resolve(ctx, req.chat)
    reply = await _admin.client(ctx)(fn.GetSendAsRequest(peer=peer))
    entities = _admin.entity_map(reply)
    full, _entity, _entities = await _admin.full_chat(ctx, peer)
    default_raw = getattr(full, "default_send_as", None)
    default_id = peer_id_of(default_raw) if default_raw is not None else None
    out: list[SendAsPeer] = []
    for row in getattr(reply, "peers", None) or []:
        marked = peer_id_of(getattr(row, "peer", None)) or 0
        entity = entities.get(marked)
        out.append(
            SendAsPeer(
                id=marked,
                type=("user" if marked > 0 else "channel"),
                title=_admin.display_name(entity),
                premium_required=bool(getattr(row, "premium_required", False)),
                default=marked == default_id,
            )
        )
    return out


SPEC_SEND_AS_LIST = OperationSpec(
    id="chat.send-as.list",
    request=SendAsListReq,
    response=list[SendAsPeer],
    impl=list_send_as,
    summary="Peers I may post as in this chat",
    columns=("id", "type", "title", "default"),
    example=[{"id": -1001500, "type": "channel", "title": "News", "default": True}],
    example_args="chat send-as list @mygroup",
    covers_partial=("groups-channels-admin.send-as",),
    coverage_note="Listing half; `chat send-as set` writes the default and owns the id.",
)


class SendAsSetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Group or channel.")]
    peer: Annotated[PeerRef, arg(1, metavar="PEER", kind="peer", help="Who to post as.")]


async def set_send_as(ctx: OpContext, req: SendAsSetReq) -> SendAsResult:
    """Set the default identity for this chat. Reactions follow it too."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    identity = await _send.resolve(ctx, req.peer)
    await _admin.client(ctx)(fn.SaveDefaultSendAsRequest(peer=peer, send_as=identity))
    return SendAsResult(chat_id=peer_id_of(peer) or 0, send_as=peer_id_of(identity) or 0)


SPEC_SEND_AS_SET = OperationSpec(
    id="chat.send-as.set",
    request=SendAsSetReq,
    response=SendAsResult,
    impl=set_send_as,
    summary="Set the default peer I post as in this chat",
    description=(
        "Applies to reactions as well as messages. Every send command still "
        "takes a one-off `--send-as`. The `anonymous` admin right forces the "
        "group itself."
    ),
    mutating=True,
    columns=("chat_id", "send_as"),
    example={"chat_id": -1001500, "send_as": -1001600},
    example_args="chat send-as set @mygroup @mychannel",
    covers=("groups-channels-admin.send-as",),
)


# ---------------------------------------------------------------------------
# chat discussion list / set / unset
# ---------------------------------------------------------------------------


class DiscussionListReq(Request):
    pass


async def list_discussion_candidates(
    ctx: OpContext, req: DiscussionListReq
) -> list[DiscussionCandidate]:
    """Groups that could become a channel's discussion group."""
    from telethon.tl.functions import channels as fn

    reply = await _admin.client(ctx)(fn.GetGroupsForDiscussionRequest())
    out: list[DiscussionCandidate] = []
    for chat in getattr(reply, "chats", None) or []:
        is_basic = type(chat).__name__ == "Chat"
        out.append(
            DiscussionCandidate(
                id=peer_id_of(chat) or 0,
                title=str(getattr(chat, "title", "") or ""),
                type="group" if is_basic else "supergroup",
                needs_migration=is_basic,
            )
        )
    return out


SPEC_DISCUSSION_LIST = OperationSpec(
    id="chat.discussion.list",
    request=DiscussionListReq,
    response=list[DiscussionCandidate],
    impl=list_discussion_candidates,
    summary="Groups eligible to become a channel's discussion group",
    description=(
        "A basic group in this list must be converted first "
        "(`chat convert <chat> supergroup`); `needs_migration` says which."
    ),
    columns=("id", "title", "type", "needs_migration"),
    example=[{"id": -1001500, "title": "News chat", "type": "supergroup"}],
    example_args="chat discussion list",
    covers=("groups-channels-admin.discussion-candidates",),
)


class DiscussionSetReq(Request):
    channel: Annotated[PeerRef, arg(0, metavar="CHANNEL", kind="peer", help="The broadcast.")]
    group: Annotated[PeerRef, arg(1, metavar="GROUP", kind="peer", help="The discussion group.")]
    unhide_prehistory: Annotated[
        bool, opt("--unhide-prehistory", help="Make the group's prehistory visible first.")
    ] = False


async def set_discussion(ctx: OpContext, req: DiscussionSetReq) -> DiscussionResult:
    """Link a discussion group to a channel."""
    from telethon.tl.functions import channels as fn

    channel = _admin.input_channel(await _send.resolve(ctx, req.channel))
    group_peer = await _send.resolve(ctx, req.group)
    group = _admin.input_channel(group_peer)
    handle = _admin.client(ctx)
    if req.unhide_prehistory:
        await handle(fn.TogglePreHistoryHiddenRequest(channel=group, enabled=False))
    try:
        await handle(fn.SetDiscussionGroupRequest(broadcast=channel, group=group))
    except Exception as exc:
        if "LINK_NOT_MODIFIED" not in str(exc):
            raise
        _admin.already(ctx)
        return DiscussionResult(
            channel_id=peer_id_of(await _send.resolve(ctx, req.channel)) or 0,
            linked_chat_id=peer_id_of(group_peer),
            already=True,
        )
    return DiscussionResult(
        channel_id=peer_id_of(await _send.resolve(ctx, req.channel)) or 0,
        linked_chat_id=peer_id_of(group_peer),
    )


SPEC_DISCUSSION_SET = OperationSpec(
    id="chat.discussion.set",
    request=DiscussionSetReq,
    response=DiscussionResult,
    impl=set_discussion,
    summary="Link a discussion group to a channel",
    description=(
        "The server refuses a group whose prehistory is hidden; "
        "`--unhide-prehistory` makes it visible first, and says so rather "
        "than doing it silently. LINK_NOT_MODIFIED reports `already: true`."
    ),
    mutating=True,
    columns=("channel_id", "linked_chat_id"),
    example={"channel_id": -1001600, "linked_chat_id": -1001500},
    example_args="chat discussion set @mychannel @mygroup",
    covers=("groups-channels-admin.discussion-link",),
)


class DiscussionUnsetReq(Request):
    channel: Annotated[PeerRef, arg(0, metavar="CHANNEL", kind="peer", help="The broadcast.")]


async def unset_discussion(ctx: OpContext, req: DiscussionUnsetReq) -> DiscussionResult:
    """Unlink a channel's discussion group."""
    from telethon.tl import types
    from telethon.tl.functions import channels as fn

    peer = await _send.resolve(ctx, req.channel)
    await _admin.client(ctx)(
        fn.SetDiscussionGroupRequest(
            broadcast=_admin.input_channel(peer), group=types.InputChannelEmpty()
        )
    )
    return DiscussionResult(channel_id=peer_id_of(peer) or 0, linked_chat_id=None)


SPEC_DISCUSSION_UNSET = OperationSpec(
    id="chat.discussion.unset",
    request=DiscussionUnsetReq,
    response=DiscussionResult,
    impl=unset_discussion,
    summary="Unlink a channel's discussion group",
    mutating=True,
    columns=("channel_id", "linked_chat_id"),
    example={"channel_id": -1001600},
    example_args="chat discussion unset @mychannel",
    covers=("groups-channels-admin.discussion-unlink",),
)
