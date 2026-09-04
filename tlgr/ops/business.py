"""The `business` group: Telegram Business, and the bot that may act as me.

Six sub-nouns behind one Settings ▸ Telegram Business screen: opening hours,
location and chat intro (`business set`), the greeting and away messages
(`business message set`), the quick replies they send (`business reply *`),
the public chat links (`business link *`) and the connected chatbot
(`business bot *`).

Two things deserve their own paragraph.

**The connected bot is the most dangerous switch in tlgr.** `businessBotRights`
grants another program the ability to read my messages, reply as me, edit my
name, bio and photo, manage my gifts and *transfer my Stars*. So every right
is opt-in by name — there is no `--all` — the command is destructive (so `-y`
is required off a TTY), and the reply enumerates exactly what was granted.

**Opening hours are minutes-of-week arithmetic, and it is easy to get wrong.**
The server stores intervals as minutes since Monday 00:00 in the business's
own timezone; an interval that runs past midnight on Sunday wraps. `_hours`
sorts, merges and clamps, and `open_now` is server-set and never sent back.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind
from tlgr.core.timefmt import fmt_dt, parse_dt
from tlgr.models.base import Request
from tlgr.models.business import (
    BotConnection,
    BotPaused,
    BotRights,
    BusinessAway,
    BusinessGreeting,
    BusinessIntro,
    BusinessLocation,
    BusinessMessage,
    BusinessOpen,
    BusinessProfile,
    BusinessRecipients,
    BusinessSet,
    ChatLink,
    ChatLinkSet,
    QuickReply,
    QuickReplyMessage,
    QuickReplySent,
    QuickReplySet,
    StarsTransferQuote,
    WorkHours,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _settings
from tlgr.ops._common import client, random_id
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK_MINUTES = 7 * 24 * 60

#: `--flag` → the `businessBotRights` field it grants. Spelled out rather than
#: generated, because this is the list a person audits before saying yes.
BOT_RIGHTS: dict[str, str] = {
    "reply_to": "reply",
    "read": "read_messages",
    "delete_sent": "delete_sent_messages",
    "delete_received": "delete_received_messages",
    "edit_name": "edit_name",
    "edit_bio": "edit_bio",
    "edit_username": "edit_username",
    "edit_photo": "edit_profile_photo",
    "manage_gifts": "view_gifts",
    "transfer_stars": "transfer_stars",
    "manage_stories": "manage_stories",
}


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


def _minutes(text: str, *, field: str) -> int:
    """`09:00` → 540. A bare hour (`9`) is accepted; anything else is an error."""
    raw = text.strip()
    hours, _, mins = raw.partition(":")
    if not hours.isdigit() or (mins and not mins.isdigit()):
        raise UsageError(f"{text!r} is not a time of day (HH:MM)", field=field)
    total = int(hours) * 60 + int(mins or 0)
    if total > 24 * 60:
        raise UsageError(f"{text!r} is not a time of day (HH:MM)", field=field)
    return total


def _hours(entries: list[str]) -> list[Any]:
    """`['mon 09:00-18:00', 'tue-fri 09:00-13:00,14:00-18:00']` as weekly opens.

    Sorted and merged, because the server rejects overlapping intervals and a
    human writing two lines for the same day is the normal way to produce
    them. Intervals are clamped to one week; an interval that ends before it
    starts is a usage error rather than a silent wrap, since "22:00-02:00"
    almost always means the next day and guessing is worse than asking.
    """
    from telethon.tl import types

    spans: list[tuple[int, int]] = []
    for entry in entries:
        days_part, _, ranges_part = entry.strip().partition(" ")
        if not ranges_part:
            raise UsageError(
                f"--open takes '<day[-day]> HH:MM-HH:MM[,HH:MM-HH:MM]'; got {entry!r}",
                field="open",
            )
        days = _days_of(days_part)
        for span in ranges_part.split(","):
            start_text, _, end_text = span.partition("-")
            if not end_text:
                raise UsageError(f"{span!r} is not a time range", field="open")
            start, end = _minutes(start_text, field="open"), _minutes(end_text, field="open")
            if end <= start:
                raise UsageError(
                    f"{span!r} ends before it starts; write the two days separately",
                    field="open",
                )
            for day in days:
                spans.append((day * 24 * 60 + start, day * 24 * 60 + end))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [
        types.BusinessWeeklyOpen(start_minute=start, end_minute=min(end, WEEK_MINUTES))
        for start, end in merged
    ]


def _days_of(text: str) -> list[int]:
    """`mon`, `mon-fri` or `mon,wed` as day indices."""
    out: list[int] = []
    for part in text.lower().split(","):
        start, _, end = part.partition("-")
        if start not in DAYS:
            raise UsageError(f"{part!r} is not a weekday (mon…sun)", field="open")
        if not end:
            out.append(DAYS.index(start))
            continue
        if end not in DAYS:
            raise UsageError(f"{part!r} is not a weekday range", field="open")
        first, last = DAYS.index(start), DAYS.index(end)
        out.extend(range(first, last + 1) if first <= last else [])
    return sorted(set(out))


def _hours_model(raw: Any) -> WorkHours | None:
    if raw is None:
        return None
    opens = []
    for entry in getattr(raw, "weekly_open", None) or []:
        start = int(getattr(entry, "start_minute", 0) or 0)
        end = int(getattr(entry, "end_minute", 0) or 0)
        opens.append(
            BusinessOpen(
                start_minute=start,
                end_minute=end,
                day=DAYS[min(start // (24 * 60), 6)],
                open=f"{start % (24 * 60) // 60:02d}:{start % 60:02d}",
                close=f"{end % (24 * 60) // 60:02d}:{end % 60:02d}",
            )
        )
    return WorkHours(
        timezone_id=str(getattr(raw, "timezone_id", "") or ""),
        weekly_open=opens,
        open_now=getattr(raw, "open_now", None),
    )


def _recipients_model(raw: Any) -> BusinessRecipients | None:
    if raw is None:
        return None
    return BusinessRecipients(
        contacts=bool(getattr(raw, "contacts", False)),
        non_contacts=bool(getattr(raw, "non_contacts", False)),
        existing_chats=bool(getattr(raw, "existing_chats", False)),
        new_chats=bool(getattr(raw, "new_chats", False)),
        exclude_selected=bool(getattr(raw, "exclude_selected", False)),
        users=[int(v) for v in getattr(raw, "users", None) or []],
        exclude_users=[int(v) for v in getattr(raw, "exclude_users", None) or []],
    )


def _rights_model(raw: Any) -> BotRights | None:
    if raw is None:
        return None
    return BotRights(
        **{field: bool(getattr(raw, field, False)) for field in BotRights.__struct_fields__}
    )


def _bot_model(raw: Any, known: dict[int, Any]) -> BotConnection:
    bot_id = int(getattr(raw, "bot_id", 0) or 0)
    return BotConnection(
        bot_id=bot_id,
        bot=_settings.peer_model(known.get(bot_id)),
        connection_id=getattr(raw, "connection_id", None),
        recipients=_recipients_model(getattr(raw, "recipients", None)),
        rights=_rights_model(getattr(raw, "rights", None)),
        paused=bool(getattr(raw, "paused", False)),
        confirmed=not bool(getattr(raw, "can_reply", None) is False),
        disabled=bool(getattr(raw, "disabled", False)),
        date=fmt_dt(getattr(raw, "date", None)),
        dc_id=getattr(raw, "dc_id", None),
    )


async def _self_full(ctx: OpContext) -> Any:
    from telethon.tl import types
    from telethon.tl.functions import users as fn

    answer = await client(ctx)(fn.GetFullUserRequest(id=types.InputUserSelf()))
    return getattr(answer, "full_user", None)


def _greeting_model(raw: Any) -> BusinessGreeting | None:
    if raw is None:
        return None
    return BusinessGreeting(
        shortcut_id=int(getattr(raw, "shortcut_id", 0) or 0),
        no_activity_days=int(getattr(raw, "no_activity_days", 0) or 0),
        recipients=_recipients_model(getattr(raw, "recipients", None)),
    )


def _away_model(raw: Any) -> BusinessAway | None:
    if raw is None:
        return None
    schedule = getattr(raw, "schedule", None)
    name = type(schedule).__name__
    word = {
        "BusinessAwayMessageScheduleAlways": "always",
        "BusinessAwayMessageScheduleOutsideWorkHours": "outside-hours",
        "BusinessAwayMessageScheduleCustom": "custom",
    }.get(name, "always")
    return BusinessAway(
        shortcut_id=int(getattr(raw, "shortcut_id", 0) or 0),
        schedule=word,
        since=fmt_dt(getattr(schedule, "start_date", None)),
        until=fmt_dt(getattr(schedule, "end_date", None)),
        offline_only=bool(getattr(raw, "offline_only", False)),
        recipients=_recipients_model(getattr(raw, "recipients", None)),
    )


def _link_model(raw: Any) -> ChatLink:
    from tlgr.ops._serialize import message_entities

    slug = str(getattr(raw, "link", "") or "").rsplit("/", 1)[-1]
    return ChatLink(
        slug=slug,
        link=str(getattr(raw, "link", "") or ""),
        title=getattr(raw, "title", None),
        message=str(getattr(raw, "message", "") or ""),
        entities=message_entities(raw),
        views=getattr(raw, "views", None),
    )


# ---------------------------------------------------------------------------
# business get / set
# ---------------------------------------------------------------------------


class GetReq(Request):
    timezones: Annotated[
        bool, opt("--timezones", help="Also print the timezone ids `business set --tz` takes.")
    ] = False


async def get(ctx: OpContext, req: GetReq) -> BusinessProfile:
    """My Telegram Business configuration, in one object.

    Everything but the chat links and the connected bots lives in `userFull`
    on self, which is why one command can answer the whole screen. Business
    needs Premium; connected bots are the exception and work without it.
    """
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import help as hfn

    handle = client(ctx)
    full = await _self_full(ctx)
    profile = BusinessProfile(
        work_hours=_hours_model(getattr(full, "business_work_hours", None)),
        location=_location_model(getattr(full, "business_location", None)),
        greeting=_greeting_model(getattr(full, "business_greeting_message", None)),
        away=_away_model(getattr(full, "business_away_message", None)),
        intro=_intro_model(getattr(full, "business_intro", None)),
        sponsored_enabled=getattr(full, "sponsored_enabled", None),
    )
    if profile.work_hours is not None:
        profile.open_now = profile.work_hours.open_now

    try:
        bots = await handle(afn.GetConnectedBotsRequest())
    except Exception as exc:
        ctx.warn(f"connected bots are unavailable on this account: {exc}")
    else:
        known = _settings.entity_map(bots)
        profile.connected_bots = [
            _bot_model(row, known) for row in getattr(bots, "connected_bots", None) or []
        ]

    try:
        links = await handle(afn.GetBusinessChatLinksRequest())
    except Exception as exc:
        ctx.warn(f"business chat links are unavailable on this account: {exc}")
    else:
        profile.chat_links = [_link_model(row) for row in getattr(links, "links", None) or []]

    if req.timezones:
        zones = await handle(hfn.GetTimezonesListRequest(hash=0))
        profile.timezones = [
            {
                "id": getattr(zone, "id", ""),
                "name": getattr(zone, "name", ""),
                "utc_offset": getattr(zone, "utc_offset", 0),
            }
            for zone in getattr(zones, "timezones", None) or []
        ]
    me = await handle.get_me()
    profile.premium = bool(getattr(me, "premium", False))
    return profile


def _location_model(raw: Any) -> BusinessLocation | None:
    if raw is None:
        return None
    geo = getattr(raw, "geo_point", None)
    return BusinessLocation(
        address=str(getattr(raw, "address", "") or ""),
        lat=getattr(geo, "lat", None),
        lon=getattr(geo, "long", None),
    )


def _intro_model(raw: Any) -> BusinessIntro | None:
    if raw is None:
        return None
    return BusinessIntro(
        title=str(getattr(raw, "title", "") or ""),
        description=str(getattr(raw, "description", "") or ""),
        sticker_id=getattr(getattr(raw, "sticker", None), "id", None),
    )


SPEC_GET = OperationSpec(
    id="business.get",
    request=GetReq,
    response=BusinessProfile,
    impl=get,
    summary="Show my Telegram Business configuration",
    idempotent=True,
    columns=("premium", "open_now", "work_hours.timezone_id", "location.address"),
    headers=("Premium", "Open now", "Timezone", "Address"),
    example={
        "premium": True,
        "open_now": True,
        "work_hours": {"timezone_id": "Europe/Amsterdam", "weekly_open": []},
    },
    example_args="business get",
    covers=("business.overview",),
    tags=frozenset({"agent-safe"}),
)


class SetReq(Request):
    tz: Annotated[
        str | None, opt("--tz", metavar="ID", help="Timezone id from `business get --timezones`.")
    ] = None
    open: Annotated[
        tuple[str, ...],
        opt("--open", metavar="SPEC", help="Repeatable: 'mon 09:00-18:00'."),
    ] = ()
    clear_hours: Annotated[bool, opt("--clear-hours", help="Remove the opening hours.")] = False
    address: Annotated[
        str | None, opt("--address", metavar="TEXT", help="Business address (<= 96 chars).")
    ] = None
    lat: Annotated[float | None, opt("--lat", metavar="DEG", help="Latitude.")] = None
    lon: Annotated[float | None, opt("--lon", metavar="DEG", help="Longitude.")] = None
    clear_location: Annotated[bool, opt("--clear-location", help="Remove the location.")] = False
    intro_title: Annotated[
        str | None, opt("--intro-title", metavar="TEXT", help="Chat intro title.")
    ] = None
    intro_text: Annotated[
        str | None, opt("--intro-text", metavar="TEXT", help="Chat intro description.")
    ] = None
    intro_sticker: Annotated[
        str | None,
        opt(
            "--intro-sticker",
            metavar="SET/REF",
            help="Sticker as <set>/<index> or <set>/<emoji>.",
        ),
    ] = None
    clear_intro: Annotated[
        bool, opt("--clear-intro", help="Revert to the random default intro.")
    ] = False


async def set_(ctx: OpContext, req: SetReq) -> BusinessSet:
    """Set opening hours, business location and chat intro.

    Three RPCs behind one screen, and each `--clear-*` sends the constructor
    *without* its field — which is how the API deletes one. Omitting a flag
    leaves that struct untouched, so the three are independent.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    result = BusinessSet()

    if req.clear_hours:
        await handle(fn.UpdateBusinessWorkHoursRequest(business_work_hours=None))
        result.changed.append("work_hours")
    elif req.open:
        if not req.tz:
            raise UsageError("--open needs --tz <timezone id>", field="tz")
        hours = types.BusinessWorkHours(timezone_id=req.tz, weekly_open=_hours(list(req.open)))
        await handle(fn.UpdateBusinessWorkHoursRequest(business_work_hours=hours))
        result.work_hours = _hours_model(hours)
        result.changed.append("work_hours")

    if req.clear_location:
        await handle(fn.UpdateBusinessLocationRequest())
        result.changed.append("location")
    elif req.address or req.lat is not None or req.lon is not None:
        if not req.address:
            raise UsageError("a business location needs --address", field="address")
        if len(req.address) > 96:
            raise UsageError("--address is at most 96 characters", field="address")
        geo = (
            types.InputGeoPoint(lat=req.lat, long=req.lon)
            if req.lat is not None and req.lon is not None
            else None
        )
        await handle(fn.UpdateBusinessLocationRequest(geo_point=geo, address=req.address))
        result.location = BusinessLocation(address=req.address, lat=req.lat, lon=req.lon)
        result.changed.append("location")

    if req.clear_intro:
        await handle(fn.UpdateBusinessIntroRequest(intro=None))
        result.changed.append("intro")
    elif req.intro_title is not None or req.intro_text is not None:
        sticker = None
        sticker_id = None
        if req.intro_sticker:
            from tlgr.ops import _media

            # Always through a fresh `getStickerSet`: an InputDocument built
            # from a remembered id carries a dead file_reference.
            document = (await _media.resolve_stickers(ctx, [req.intro_sticker]))[0]
            sticker = _media.input_document(document)
            sticker_id = int(getattr(document, "id", 0) or 0)
        intro = types.InputBusinessIntro(
            title=req.intro_title or "", description=req.intro_text or "", sticker=sticker
        )
        await handle(fn.UpdateBusinessIntroRequest(intro=intro))
        result.intro = BusinessIntro(
            title=intro.title, description=intro.description, sticker_id=sticker_id
        )
        result.changed.append("intro")

    if not result.changed:
        raise UsageError(
            "nothing to change: give --open/--tz, --address or --intro-title", field="open"
        )
    ctx.emit("business_set", {"changed": result.changed})
    return result


SPEC_SET = OperationSpec(
    id="business.set",
    request=SetReq,
    response=BusinessSet,
    impl=set_,
    summary="Set opening hours, business location and chat intro",
    description=(
        "`--clear-*` sends the constructor without its field, which is how "
        "the API deletes one; a flag you omit leaves that struct alone."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("changed", "work_hours.timezone_id", "location.address"),
    headers=("Changed", "Timezone", "Address"),
    example={"changed": ["work_hours"], "work_hours": {"timezone_id": "Europe/Amsterdam"}},
    example_args="business set --tz Europe/Amsterdam --open 'mon-fri 09:00-18:00'",
    covers=(
        "business.intro",
        "business.location",
        "business.working-hours",
        "location.business-address",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# business message set
# ---------------------------------------------------------------------------


class MessageSetReq(Request):
    kind: Annotated[str, arg(0, metavar="KIND", help="greeting or away.")]
    shortcut: Annotated[
        str | None, opt("--shortcut", metavar="NAME", help="Quick-reply shortcut to send.")
    ] = None
    schedule: Annotated[
        str | None,
        opt("--schedule", metavar="WHEN", help="away: always | outside-hours | custom."),
    ] = None
    since: Annotated[
        str | None, opt("--since", metavar="WHEN", kind="datetime", help="custom schedule start.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", kind="datetime", help="custom schedule end.")
    ] = None
    offline_only: Annotated[
        bool, opt("--offline-only", help="away: only send while I am offline.")
    ] = False
    no_activity_days: Annotated[
        int, opt("--no-activity-days", metavar="N", help="greeting: silence after N quiet days.")
    ] = 7
    contacts: Annotated[bool, opt("--contacts", help="Include contacts.")] = False
    non_contacts: Annotated[bool, opt("--non-contacts", help="Include non-contacts.")] = False
    existing_chats: Annotated[bool, opt("--existing-chats", help="Include existing chats.")] = False
    new_chats: Annotated[bool, opt("--new-chats", help="Include new chats.")] = False
    users: Annotated[
        str | None, opt("--users", metavar="LIST", help="Explicit recipient users.")
    ] = None
    exclude: Annotated[bool, opt("--exclude", help="Treat the selection as an exclusion.")] = False
    exclude_users: Annotated[
        str | None, opt("--exclude-users", metavar="LIST", help="away: users to exclude.")
    ] = None
    off: Annotated[bool, opt("--off", help="Disable this message.")] = False


async def _recipients_tl(ctx: OpContext, req: MessageSetReq, *, bot: bool = False) -> Any:
    from telethon.tl import types

    users = [
        await _settings.input_user(ctx, part.strip(), field="users")
        for part in (req.users or "").split(",")
        if part.strip()
    ]
    kwargs: dict[str, Any] = {
        "existing_chats": req.existing_chats or None,
        "new_chats": req.new_chats or None,
        "contacts": req.contacts or None,
        "non_contacts": req.non_contacts or None,
        "exclude_selected": req.exclude or None,
        "users": users or None,
    }
    if bot:
        kwargs["exclude_users"] = [
            await _settings.input_user(ctx, part.strip(), field="exclude_users")
            for part in (req.exclude_users or "").split(",")
            if part.strip()
        ] or None
        return types.InputBusinessBotRecipients(**kwargs)
    return types.InputBusinessRecipients(**kwargs)


async def message_set(ctx: OpContext, req: MessageSetReq) -> BusinessMessage:
    """Configure the greeting message or the away message.

    Both are "send this quick reply to these people", which is why they share
    one command and one recipients shape. Omitting `--shortcut` (or passing
    `--off`) disables the feature — the API expresses that by sending no
    message at all.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    kind = req.kind.strip().lower()
    if kind not in ("greeting", "away"):
        raise UsageError("KIND is `greeting` or `away`", field="kind")

    if req.off or not req.shortcut:
        request = (
            fn.UpdateBusinessGreetingMessageRequest
            if kind == "greeting"
            else fn.UpdateBusinessAwayMessageRequest
        )
        await handle(request(message=None))
        ctx.emit("business_message", {"kind": kind, "enabled": False})
        return BusinessMessage(kind=kind, enabled=False)

    shortcut_id = await _shortcut_id(ctx, req.shortcut)
    recipients = await _recipients_tl(ctx, req)

    if kind == "greeting":
        message = types.InputBusinessGreetingMessage(
            shortcut_id=shortcut_id,
            recipients=recipients,
            no_activity_days=req.no_activity_days,
        )
        await handle(fn.UpdateBusinessGreetingMessageRequest(message=message))
        ctx.emit("business_message", {"kind": kind, "shortcut_id": shortcut_id})
        return BusinessMessage(
            kind=kind,
            shortcut_id=shortcut_id,
            shortcut=req.shortcut,
            no_activity_days=req.no_activity_days,
            recipients=_recipients_model(recipients),
        )

    word = (req.schedule or "always").strip().lower()
    if word == "always":
        schedule: Any = types.BusinessAwayMessageScheduleAlways()
    elif word == "outside-hours":
        schedule = types.BusinessAwayMessageScheduleOutsideWorkHours()
    elif word == "custom":
        if not req.since or not req.until:
            raise UsageError("--schedule custom needs --since and --until", field="since")
        schedule = types.BusinessAwayMessageScheduleCustom(
            start_date=parse_dt(req.since), end_date=parse_dt(req.until)
        )
    else:
        raise UsageError("--schedule is always, outside-hours or custom", field="schedule")

    message = types.InputBusinessAwayMessage(
        shortcut_id=shortcut_id,
        schedule=schedule,
        recipients=recipients,
        offline_only=req.offline_only or None,
    )
    await handle(fn.UpdateBusinessAwayMessageRequest(message=message))
    ctx.emit("business_message", {"kind": kind, "shortcut_id": shortcut_id})
    return BusinessMessage(
        kind=kind,
        shortcut_id=shortcut_id,
        shortcut=req.shortcut,
        schedule=word,
        since=fmt_dt(getattr(schedule, "start_date", None)),
        until=fmt_dt(getattr(schedule, "end_date", None)),
        offline_only=req.offline_only,
        recipients=_recipients_model(recipients),
    )


async def _shortcut_id(ctx: OpContext, name: str) -> int:
    """A quick-reply shortcut's id, from its name or its id."""
    if name.strip().isdigit():
        return int(name)
    page = await reply_list(ctx, ReplyListReq())
    for row in page.items:
        if row.shortcut == name.strip():
            return row.shortcut_id
    raise NotFoundError(
        f"no quick-reply shortcut named {name!r}; create one with `business reply add`"
    )


SPEC_MESSAGE_SET = OperationSpec(
    id="business.message.set",
    request=MessageSetReq,
    response=BusinessMessage,
    impl=message_set,
    summary="Configure the greeting message or the away message",
    description=(
        "Both need an existing quick-reply shortcut; `--schedule outside-hours` "
        "additionally needs opening hours. Omitting `--shortcut` disables the "
        "feature, which is how the API expresses 'off'."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("kind", "shortcut_id", "shortcut", "schedule", "enabled"),
    headers=("Kind", "Shortcut", "Name", "Schedule", "Enabled"),
    example={
        "kind": "greeting",
        "shortcut_id": 3,
        "shortcut": "hello",
        "no_activity_days": 7,
        "enabled": True,
    },
    example_args="business message set greeting --shortcut hello --new-chats",
    covers=(
        "business.away-message",
        "business.greeting-message",
        "contacts-users.user-business-greeting-away",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# business reply list / add / edit / delete / send
# ---------------------------------------------------------------------------


class ReplyListReq(Request):
    shortcut: Annotated[
        str | None,
        arg(0, metavar="SHORTCUT", required=False, help="Show the messages inside one shortcut."),
    ] = None


async def reply_list(ctx: OpContext, req: ReplyListReq) -> Page[QuickReply]:
    """List quick-reply shortcuts, or the messages inside one.

    Quick-reply messages have their own id sequence, unrelated to chat message
    ids — `msg_id` here is only meaningful inside its shortcut.
    """
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    result = await handle(fn.GetQuickRepliesRequest(hash=0))
    rows = [
        QuickReply(
            shortcut_id=int(getattr(row, "shortcut_id", 0) or 0),
            shortcut=str(getattr(row, "shortcut", "") or ""),
            count=int(getattr(row, "count", 0) or 0),
            top_message=getattr(row, "top_message", None),
        )
        for row in getattr(result, "quick_replies", None) or []
    ]
    if req.shortcut:
        wanted = req.shortcut.strip()
        rows = [row for row in rows if row.shortcut == wanted or str(row.shortcut_id) == wanted]
        if not rows:
            raise NotFoundError(f"no quick-reply shortcut named {req.shortcut!r}")
        messages = await handle(
            fn.GetQuickReplyMessagesRequest(shortcut_id=rows[0].shortcut_id, hash=0)
        )
        rows[0].messages = [_quick_message(m) for m in getattr(messages, "messages", None) or []]
    return Page(items=rows, has_more=False, total=len(rows))


def _quick_message(raw: Any) -> QuickReplyMessage:
    from tlgr.ops._serialize import media_summary, message_entities

    summary = media_summary(getattr(raw, "media", None))
    return QuickReplyMessage(
        id=int(getattr(raw, "id", 0) or 0),
        text=str(getattr(raw, "message", "") or ""),
        entities=message_entities(raw),
        media=summary.kind if summary is not None else None,
        date=fmt_dt(getattr(raw, "date", None)),
    )


SPEC_REPLY_LIST = OperationSpec(
    id="business.reply.list",
    request=ReplyListReq,
    response=Page[QuickReply],
    impl=reply_list,
    summary="List quick-reply shortcuts, or the messages inside one",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("shortcut_id", "shortcut", "count", "top_message"),
    headers=("Id", "Shortcut", "Messages", "Top"),
    example={
        "items": [{"shortcut_id": 3, "shortcut": "hello", "count": 1}],
        "has_more": False,
    },
    example_args="business reply list",
    covers=(
        "business.quick-replies-list",
        "business.quick-reply-messages",
        "messages-core.quick-reply-list",
    ),
    tags=frozenset({"agent-safe"}),
)


class ReplyAddReq(Request):
    shortcut: Annotated[str, arg(0, metavar="SHORTCUT", help="Shortcut name (created if new).")]
    text: Annotated[str | None, opt("--text", metavar="TEXT", help="Message text.")] = None
    file: Annotated[
        tuple[str, ...],
        opt("--file", metavar="PATH", kind="path", help="Attach a file (repeatable)."),
    ] = ()
    parse: Annotated[str, opt("--parse", metavar="MODE", help="md | html | none.")] = "md"
    copy_from: Annotated[
        str | None,
        opt("--copy-from", metavar="CHAT:ID", help="Copy an existing message into the shortcut."),
    ] = None


async def reply_add(ctx: OpContext, req: ReplyAddReq) -> QuickReplySet:
    """Add a message to a quick-reply shortcut, creating the shortcut if new.

    The shortcut is addressed by *name* the first time and by *id* afterwards;
    `messages.checkQuickReplyShortcut` is what tells the two apart, and it is
    also what enforces `quick_replies_limit` before anything is sent.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    from tlgr.ops import _send

    handle = client(ctx)
    name = req.shortcut.strip()
    existing = {
        row.shortcut: row.shortcut_id for row in (await reply_list(ctx, ReplyListReq())).items
    }
    if name in existing:
        shortcut: Any = types.InputQuickReplyShortcutId(shortcut_id=existing[name])
    else:
        await handle(fn.CheckQuickReplyShortcutRequest(shortcut=name))
        shortcut = types.InputQuickReplyShortcut(shortcut=name)

    if req.copy_from:
        chat, _, msg_id = req.copy_from.rpartition(":")
        if not chat or not msg_id.strip().lstrip("-").isdigit():
            raise UsageError("--copy-from wants '<chat>:<msg_id>'", field="copy_from")
        await handle(
            fn.ForwardMessagesRequest(
                from_peer=await _settings.resolve(ctx, chat),
                id=[int(msg_id)],
                random_id=[random_id()],
                to_peer=types.InputPeerSelf(),
                quick_reply_shortcut=shortcut,
            )
        )
        return QuickReplySet(shortcut=name, shortcut_id=existing.get(name))

    if not req.text and not req.file:
        raise UsageError("give --text, --file or --copy-from", field="text")

    text, entities = _send.body(req.text or "", parse=req.parse, entities=None)
    if req.file:
        media = await _send.input_media(ctx, str(req.file[0]))
        result = await handle(
            fn.SendMediaRequest(
                peer=types.InputPeerSelf(),
                media=media,
                message=text,
                entities=entities,
                random_id=random_id(),
                quick_reply_shortcut=shortcut,
            )
        )
    else:
        result = await handle(
            fn.SendMessageRequest(
                peer=types.InputPeerSelf(),
                message=text,
                entities=entities,
                random_id=random_id(),
                quick_reply_shortcut=shortcut,
            )
        )
    ctx.emit("business_reply", {"shortcut": name})
    ids = [
        int(getattr(getattr(update, "message", None), "id", 0) or 0)
        for update in getattr(result, "updates", None) or []
        if getattr(update, "message", None) is not None
    ]
    return QuickReplySet(
        shortcut=name,
        shortcut_id=existing.get(name),
        msg_id=ids[0] if ids else None,
        msg_ids=ids,
    )


SPEC_REPLY_ADD = OperationSpec(
    id="business.reply.add",
    request=ReplyAddReq,
    response=QuickReplySet,
    impl=reply_add,
    summary="Add a message to a quick-reply shortcut (creating the shortcut if needed)",
    mutating=True,
    rate_class="send",
    columns=("shortcut", "shortcut_id", "msg_id"),
    headers=("Shortcut", "Id", "Message"),
    example={"shortcut": "hello", "shortcut_id": 3, "msg_id": 1},
    example_args='business reply add hello --text "Hi! I will reply shortly."',
    covers=("business.quick-reply-add",),
)


class ReplyEditReq(Request):
    shortcut: Annotated[
        str | None, arg(0, metavar="SHORTCUT", required=False, help="The shortcut to edit.")
    ] = None
    msg_id: Annotated[
        int | None, arg(1, metavar="MSG_ID", required=False, help="A message inside it.")
    ] = None
    text: Annotated[str | None, opt("--text", metavar="TEXT", help="New message text.")] = None
    parse: Annotated[str, opt("--parse", metavar="MODE", help="md | html | none.")] = "md"
    rename: Annotated[str | None, opt("--rename", metavar="NAME", help="New shortcut name.")] = None
    order: Annotated[
        str | None, opt("--order", metavar="LIST", help="Every shortcut, in the wanted order.")
    ] = None


async def reply_edit(ctx: OpContext, req: ReplyEditReq) -> QuickReplySet:
    """Edit a quick-reply message, rename a shortcut, or reorder the list.

    `--order` wants the *complete* list of shortcut ids; the API replaces the
    order rather than moving one entry, and a partial list would silently
    drop the rest.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    from tlgr.ops import _send

    handle = client(ctx)
    rows = {row.shortcut: row.shortcut_id for row in (await reply_list(ctx, ReplyListReq())).items}

    if req.order is not None:
        order = [
            rows.get(part.strip(), int(part.strip()) if part.strip().isdigit() else 0)
            for part in req.order.split(",")
            if part.strip()
        ]
        if not order or 0 in order:
            raise UsageError("--order wants every shortcut, by name or id", field="order")
        await handle(fn.ReorderQuickRepliesRequest(order=order))
        return QuickReplySet(order=order)

    if not req.shortcut:
        raise UsageError("give a shortcut, or --order", field="shortcut")
    shortcut_id = await _shortcut_id(ctx, req.shortcut)

    if req.rename is not None:
        await handle(fn.EditQuickReplyShortcutRequest(shortcut_id=shortcut_id, shortcut=req.rename))
        return QuickReplySet(shortcut_id=shortcut_id, shortcut=req.rename)

    if req.msg_id is None or req.text is None:
        raise UsageError("editing a message needs MSG_ID and --text", field="msg_id")
    text, entities = _send.body(req.text, parse=req.parse, entities=None)
    await handle(
        fn.EditMessageRequest(
            peer=types.InputPeerSelf(),
            id=req.msg_id,
            message=text,
            entities=entities,
            quick_reply_shortcut_id=shortcut_id,
        )
    )
    ctx.emit("business_reply_edit", {"shortcut_id": shortcut_id, "msg_id": req.msg_id})
    return QuickReplySet(shortcut_id=shortcut_id, shortcut=req.shortcut, msg_id=req.msg_id)


SPEC_REPLY_EDIT = OperationSpec(
    id="business.reply.edit",
    request=ReplyEditReq,
    response=QuickReplySet,
    impl=reply_edit,
    summary="Edit a quick-reply message, rename a shortcut or reorder the shortcut list",
    mutating=True,
    rate_class="send",
    columns=("shortcut_id", "shortcut", "msg_id", "order"),
    headers=("Id", "Shortcut", "Message", "Order"),
    example={"shortcut_id": 3, "shortcut": "hello", "msg_id": 1},
    example_args='business reply edit hello 1 --text "Hello!"',
    covers=(
        "business.quick-reply-edit",
        "business.quick-reply-rename",
        "business.quick-reply-reorder",
        "messages-core.quick-reply-manage",
    ),
)


class ReplyDeleteReq(Request):
    shortcut: Annotated[str, arg(0, metavar="SHORTCUT", help="The shortcut to delete from.")]
    msg_id: Annotated[
        tuple[int, ...],
        arg(1, metavar="MSG_ID", required=False, variadic=True, help="Messages to delete."),
    ] = ()


async def reply_delete(ctx: OpContext, req: ReplyDeleteReq) -> QuickReplySet:
    """Delete a quick-reply shortcut, or single messages inside it."""
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    shortcut_id = await _shortcut_id(ctx, req.shortcut)
    if req.msg_id:
        await handle(
            fn.DeleteQuickReplyMessagesRequest(
                shortcut_id=shortcut_id, id=[int(v) for v in req.msg_id]
            )
        )
        return QuickReplySet(
            shortcut_id=shortcut_id, shortcut=req.shortcut, deleted=len(req.msg_id)
        )
    await handle(fn.DeleteQuickReplyShortcutRequest(shortcut_id=shortcut_id))
    ctx.emit("business_reply_delete", {"shortcut_id": shortcut_id})
    return QuickReplySet(shortcut_id=shortcut_id, shortcut=req.shortcut, deleted=1)


SPEC_REPLY_DELETE = OperationSpec(
    id="business.reply.delete",
    request=ReplyDeleteReq,
    response=QuickReplySet,
    impl=reply_delete,
    summary="Delete a quick-reply shortcut, or single messages inside it",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("shortcut_id", "shortcut", "deleted"),
    headers=("Id", "Shortcut", "Deleted"),
    example={"shortcut_id": 3, "shortcut": "hello", "deleted": 1},
    example_args="business reply delete hello",
    covers=("business.quick-reply-delete", "business.quick-reply-delete-messages"),
)


class ReplySendReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Private chat to send to.")]
    shortcut: Annotated[str, arg(1, metavar="SHORTCUT", help="The shortcut to send.")]
    only: Annotated[
        str | None, opt("--only", metavar="LIST", help="Only these message ids from the shortcut.")
    ] = None


async def reply_send(ctx: OpContext, req: ReplySendReq) -> QuickReplySent:
    """Send a quick reply into a private chat. Users only, private chats only."""
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    peer = await _settings.resolve(ctx, req.chat)
    shortcut_id = await _shortcut_id(ctx, req.shortcut)
    page = await reply_list(ctx, ReplyListReq(shortcut=req.shortcut))
    available = [message.id for message in page.items[0].messages]
    wanted = [int(part) for part in req.only.split(",") if part.strip()] if req.only else available
    if not wanted:
        raise UsageError(f"shortcut {req.shortcut!r} has no messages", field="shortcut")
    result = await handle(
        fn.SendQuickReplyMessagesRequest(
            peer=peer,
            shortcut_id=shortcut_id,
            id=wanted,
            random_id=[random_id() for _ in wanted],
        )
    )
    ids = [
        int(getattr(getattr(update, "message", None), "id", 0) or 0)
        for update in getattr(result, "updates", None) or []
        if getattr(update, "message", None) is not None
    ]
    chat_id = _settings.peer_of(peer)
    ctx.emit("business_reply_sent", {"chat_id": chat_id, "shortcut_id": shortcut_id})
    return QuickReplySent(chat_id=chat_id, shortcut_id=shortcut_id, message_ids=ids)


SPEC_REPLY_SEND = OperationSpec(
    id="business.reply.send",
    request=ReplySendReq,
    response=QuickReplySent,
    impl=reply_send,
    summary="Send a quick reply into a private chat",
    aliases=("quickreply.send",),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "shortcut_id", "message_ids"),
    headers=("Chat", "Shortcut", "Messages"),
    example={"chat_id": 777123, "shortcut_id": 3, "message_ids": [4242]},
    example_args="business reply send @alice hello",
    covers=("business.quick-reply-send",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# business link list / set
# ---------------------------------------------------------------------------


class LinkListReq(Request):
    slug: Annotated[
        str | None,
        opt("--slug", metavar="SLUG", help="Resolve one link, including other people's."),
    ] = None


async def link_list(ctx: OpContext, req: LinkListReq) -> Page[ChatLink]:
    """My business chat links, or a resolved one.

    Creating and editing needs Premium; *resolving* somebody else's link does
    not, which is why `--slug` is the one half of this command that works on
    any account.
    """
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    if req.slug:
        slug = req.slug.rsplit("/", 1)[-1]
        resolved = await handle(fn.ResolveBusinessChatLinkRequest(slug=slug))
        from tlgr.ops._serialize import message_entities

        peer_id = getattr(getattr(resolved, "peer", None), "user_id", None)
        return Page(
            items=[
                ChatLink(
                    slug=slug,
                    link=f"https://t.me/m/{slug}",
                    message=str(getattr(resolved, "message", "") or ""),
                    entities=message_entities(resolved),
                    title=str(peer_id) if peer_id else None,
                )
            ],
            has_more=False,
            total=1,
        )
    result = await handle(fn.GetBusinessChatLinksRequest())
    rows = [_link_model(row) for row in getattr(result, "links", None) or []]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_LINK_LIST = OperationSpec(
    id="business.link.list",
    request=LinkListReq,
    response=Page[ChatLink],
    impl=link_list,
    summary="List my business chat links, or resolve someone's link",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("slug", "link", "title", "views"),
    headers=("Slug", "Link", "Title", "Views"),
    example={
        "items": [{"slug": "abc", "link": "https://t.me/m/abc", "message": "Hi!"}],
        "has_more": False,
    },
    example_args="business link list",
    covers=("business.chat-links", "dialogs.business-link-list"),
    tags=frozenset({"agent-safe"}),
)


class LinkSetReq(Request):
    slug: Annotated[
        str | None, arg(0, metavar="SLUG", required=False, help="Omit to create a new link.")
    ] = None
    text: Annotated[str | None, opt("--text", metavar="TEXT", help="Prefilled message.")] = None
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="Link title.")] = None
    parse: Annotated[str, opt("--parse", metavar="MODE", help="md | html | none.")] = "md"
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", help="Explicit entities.")
    ] = None
    delete: Annotated[bool, opt("--delete", help="Delete the link.")] = False


async def link_set(ctx: OpContext, req: LinkSetReq) -> ChatLinkSet:
    """Create, edit or delete a business chat link.

    `CHATLINKS_TOO_MUCH` means the `business_chat_links_limit` from appConfig
    is reached; the server says which, so tlgr passes it through rather than
    counting the links itself and guessing.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    from tlgr.ops import _send

    handle = client(ctx)
    if req.delete:
        if not req.slug:
            raise UsageError("--delete needs the slug to delete", field="slug")
        await handle(fn.DeleteBusinessChatLinkRequest(slug=req.slug))
        ctx.emit("business_link_deleted", {"slug": req.slug})
        return ChatLinkSet(slug=req.slug, deleted=True)

    text, entities = _send.body(req.text or "", parse=req.parse, entities=req.entities)
    link = types.InputBusinessChatLink(message=text, entities=entities, title=req.title)
    if req.slug:
        result = await handle(fn.EditBusinessChatLinkRequest(slug=req.slug, link=link))
    else:
        result = await handle(fn.CreateBusinessChatLinkRequest(link=link))
    model = _link_model(result)
    ctx.emit("business_link", {"slug": model.slug})
    return ChatLinkSet(slug=model.slug, link=model.link, title=model.title, message=model.message)


SPEC_LINK_SET = OperationSpec(
    id="business.link.set",
    request=LinkSetReq,
    response=ChatLinkSet,
    impl=link_set,
    summary="Create, edit or delete a business chat link",
    mutating=True,
    rate_class="send",
    columns=("slug", "link", "title", "deleted"),
    headers=("Slug", "Link", "Title", "Deleted"),
    example={"slug": "abc", "link": "https://t.me/m/abc", "message": "Hi!"},
    example_args='business link set --text "Hi! How can I help?"',
    covers=(
        "dialogs.business-link-create",
        "dialogs.business-link-delete",
        "dialogs.business-link-edit",
    ),
    covers_partial=("business.chat-links",),
    coverage_note="Listing and resolving links is `business link list`.",
)


# ---------------------------------------------------------------------------
# business bot list / set / toggle
# ---------------------------------------------------------------------------


class BotListReq(Request):
    connection: Annotated[
        str | None,
        opt("--connection", metavar="ID", help="Bot side: inspect one business connection."),
    ] = None


async def bot_list(ctx: OpContext, req: BotListReq) -> Page[BotConnection]:
    """Chatbots connected to my account, or one connection in detail.

    `--connection` is the *bot's* view and only works from a bot session; a
    user account asking for it gets `BOT_METHOD_INVALID`, which is why the
    flag says so rather than the error doing it.
    """
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    if req.connection:
        from tlgr.ops import _bots

        await _bots.require_bot_session(ctx, "business bot list --connection")
        result = await handle(fn.GetBotBusinessConnectionRequest(connection_id=req.connection))
        known = _settings.entity_map(result)
        rows = [
            _bot_model(getattr(update, "connection", update), known)
            for update in getattr(result, "updates", None) or []
            if getattr(update, "connection", None) is not None
        ]
        return Page(items=rows, has_more=False, total=len(rows))

    result = await handle(fn.GetConnectedBotsRequest())
    known = _settings.entity_map(result)
    rows = [_bot_model(row, known) for row in getattr(result, "connected_bots", None) or []]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_BOT_LIST = OperationSpec(
    id="business.bot.list",
    request=BotListReq,
    response=Page[BotConnection],
    impl=bot_list,
    summary="List chatbots connected to my account (and inspect one connection)",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("bot_id", "connection_id", "paused", "confirmed", "disabled"),
    headers=("Bot", "Connection", "Paused", "Confirmed", "Disabled"),
    example={
        "items": [{"bot_id": 5000001, "paused": False, "rights": {"reply": True}}],
        "has_more": False,
    },
    example_args="business bot list",
    covers=(
        "bots.business-bots-list",
        "business.bot-connection",
        "business.connected-bots",
        "dialogs.business-bot-bar",
    ),
    tags=frozenset({"agent-safe"}),
)


class BotSetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot to connect.")]
    reply_to: Annotated[bool, opt("--reply-to", help="Right: reply to messages.")] = False
    read: Annotated[bool, opt("--read", help="Right: read messages.")] = False
    delete_sent: Annotated[bool, opt("--delete-sent", help="Right: delete messages it sent.")] = (
        False
    )
    delete_received: Annotated[
        bool, opt("--delete-received", help="Right: delete messages it received.")
    ] = False
    edit_name: Annotated[bool, opt("--edit-name", help="Right: edit my name.")] = False
    edit_bio: Annotated[bool, opt("--edit-bio", help="Right: edit my bio.")] = False
    edit_username: Annotated[bool, opt("--edit-username", help="Right: edit my username.")] = False
    edit_photo: Annotated[bool, opt("--edit-photo", help="Right: edit my profile photo.")] = False
    manage_gifts: Annotated[
        bool, opt("--manage-gifts", help="Right: view and manage gifts and Stars.")
    ] = False
    transfer_stars: Annotated[
        bool, opt("--transfer-stars", help="Right: transfer Stars to the bot.")
    ] = False
    manage_stories: Annotated[bool, opt("--manage-stories", help="Right: manage stories.")] = False
    contacts: Annotated[bool, opt("--contacts", help="Recipients: contacts.")] = False
    non_contacts: Annotated[bool, opt("--non-contacts", help="Recipients: non-contacts.")] = False
    existing_chats: Annotated[bool, opt("--existing-chats", help="Recipients: existing chats.")] = (
        False
    )
    new_chats: Annotated[bool, opt("--new-chats", help="Recipients: new chats.")] = False
    users: Annotated[
        str | None, opt("--users", metavar="LIST", help="Explicit recipient users.")
    ] = None
    exclude_users: Annotated[
        str | None, opt("--exclude-users", metavar="LIST", help="Users to exclude.")
    ] = None
    exclude: Annotated[bool, opt("--exclude", help="Invert the selection.")] = False
    confirm: Annotated[bool, opt("--confirm", help="Activate a pending connection.")] = False
    disconnect: Annotated[bool, opt("--disconnect", help="Remove the bot.")] = False


async def bot_set(ctx: OpContext, req: BotSetReq) -> BotConnection:
    """Connect, re-scope, confirm or disconnect a business chatbot.

    Every right is opt-in **by name**. There is deliberately no `--all`: this
    is the command that lets another program read your messages, rewrite your
    profile and move your Stars, and the reply enumerates exactly what was
    granted so an audit does not have to trust the flags that were typed.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    bot = await _settings.input_user(ctx, req.bot, field="bot")

    if req.confirm:
        await handle(fn.ConfirmBotConnectionRequest(bot_id=bot))
        ctx.emit("business_bot_confirmed", {})
        page = await bot_list(ctx, BotListReq())
        return page.items[0] if page.items else BotConnection(confirmed=True)

    recipients = types.InputBusinessBotRecipients(
        existing_chats=req.existing_chats or None,
        new_chats=req.new_chats or None,
        contacts=req.contacts or None,
        non_contacts=req.non_contacts or None,
        exclude_selected=req.exclude or None,
        users=[
            await _settings.input_user(ctx, part.strip(), field="users")
            for part in (req.users or "").split(",")
            if part.strip()
        ]
        or None,
        exclude_users=[
            await _settings.input_user(ctx, part.strip(), field="exclude_users")
            for part in (req.exclude_users or "").split(",")
            if part.strip()
        ]
        or None,
    )

    granted = {field: True for flag, field in BOT_RIGHTS.items() if getattr(req, flag)}
    rights = None if req.disconnect else types.BusinessBotRights(**granted)
    await handle(
        fn.UpdateConnectedBotRequest(
            bot=bot,
            recipients=recipients,
            deleted=req.disconnect or None,
            rights=rights,
        )
    )
    ctx.emit(
        "business_bot",
        {"granted": sorted(granted), "deleted": req.disconnect},
    )
    return BotConnection(
        bot_id=_settings.peer_of(await _settings.resolve(ctx, req.bot)),
        recipients=_recipients_model(recipients),
        rights=_rights_model(rights),
        deleted=req.disconnect,
    )


SPEC_BOT_SET = OperationSpec(
    id="business.bot.set",
    request=BotSetReq,
    response=BotConnection,
    impl=bot_set,
    summary="Connect, re-scope, confirm or disconnect a business chatbot",
    description=(
        "Rights default to none and each one is named explicitly, because a "
        "connected bot can read, reply, rewrite the profile and move Stars. "
        "Acting *as* the bot on somebody's account "
        "(`invokeWithBusinessConnection`) is a bot-side surface and out of "
        "scope for the user-side CLI."
    ),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("bot_id", "rights", "deleted"),
    headers=("Bot", "Rights", "Removed"),
    example={"bot_id": 5000001, "rights": {"reply": True, "read_messages": True}},
    example_args="business bot set @mybot --reply-to --read --new-chats",
    covers=(
        "bots.business-bot-connect",
        "bots.business-bot-disconnect",
        "bots.business-bot-remove-from-chat",
        "business.account-edit-via-bot",
        "business.confirm-bot-connection",
        "stories.business-story",
    ),
    covers_partial=("business.connected-bots",),
    coverage_note="Listing the connections is `business bot list`.",
    tags=frozenset({"visible-to-others"}),
)


class BotToggleReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The chat to change.")]
    resume: Annotated[bool, opt("--resume", help="Un-pause the bot in this chat.")] = False
    remove: Annotated[bool, opt("--remove", help="Remove the bot from this chat entirely.")] = False


async def bot_toggle(ctx: OpContext, req: BotToggleReq) -> BotPaused:
    """Pause, resume or exclude the connected bot in one chat."""
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    peer = await _settings.resolve(ctx, req.chat)
    chat_id = _settings.peer_of(peer)
    if req.remove:
        await handle(fn.DisablePeerConnectedBotRequest(peer=peer))
        ctx.emit("business_bot_chat", {"chat_id": chat_id, "removed": True})
        return BotPaused(chat_id=chat_id, removed=True)
    paused = not req.resume
    await handle(fn.ToggleConnectedBotPausedRequest(peer=peer, paused=paused))
    ctx.emit("business_bot_chat", {"chat_id": chat_id, "paused": paused})
    return BotPaused(chat_id=chat_id, paused=paused)


SPEC_BOT_TOGGLE = OperationSpec(
    id="business.bot.toggle",
    request=BotToggleReq,
    response=BotPaused,
    impl=bot_toggle,
    summary="Pause, resume or exclude the connected bot in one chat",
    aliases=("business.bot.pause",),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("chat_id", "paused", "removed"),
    headers=("Chat", "Paused", "Removed"),
    example={"chat_id": 777123, "paused": True},
    example_args="business bot toggle @alice",
    covers=("business.bot-pause-chat", "business.bot-remove-chat"),
)


# ---------------------------------------------------------------------------
# business stars transfer
# ---------------------------------------------------------------------------


class StarsTransferReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The connected bot.")]
    amount: Annotated[int | None, opt("--amount", metavar="STARS", help="Stars to transfer.")] = (
        None
    )


async def stars_transfer(ctx: OpContext, req: StarsTransferReq) -> StarsTransferQuote:
    """Price a Stars transfer to a business bot — and refuse to make it.

    `payments.sendStarsForm` is absent from tlgr's surface by policy (see
    `ops/payment.py`), and this PR does not open a second door onto the same
    money. The form is fetched so the price is visible, and `ok` is false with
    the reason attached.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    if req.amount is None or req.amount <= 0:
        raise UsageError("--amount is the number of Stars to transfer", field="amount")
    bot = await _settings.input_user(ctx, req.bot, field="bot")
    invoice = types.InputInvoiceBusinessBotTransferStars(bot=bot, stars=int(req.amount))
    form = await client(ctx)(fn.GetPaymentFormRequest(invoice=invoice))
    prices = getattr(getattr(form, "invoice", None), "prices", None) or []
    return StarsTransferQuote(
        bot_id=_settings.peer_of(await _settings.resolve(ctx, req.bot)),
        stars=sum(int(getattr(price, "amount", 0) or 0) for price in prices) or int(req.amount),
        currency=str(getattr(getattr(form, "invoice", None), "currency", "XTR") or "XTR"),
        ok=False,
        reason=_settings.NO_SPEND,
        form_id=getattr(form, "form_id", None),
    )


SPEC_STARS_TRANSFER = OperationSpec(
    id="business.stars.transfer",
    request=StarsTransferReq,
    response=StarsTransferQuote,
    impl=stars_transfer,
    summary="Price a Stars transfer from a business account to its bot",
    description=(
        "Reads `payments.getPaymentForm` and stops there. tlgr never signs a "
        "payment form — PR-10 settled that for the `payment` group and this "
        "group inherits it rather than opening a second door onto the money."
    ),
    idempotent=True,
    columns=("bot_id", "stars", "currency", "ok", "reason"),
    headers=("Bot", "Stars", "Currency", "Sent", "Why not"),
    example={"bot_id": 5000001, "stars": 100, "currency": "XTR", "ok": False},
    example_args="business stars transfer @mybot --amount 100",
    covers_partial=("stars.business-bot-transfer",),
    coverage_note=(
        "The price and the form are reported; signing the form is deliberately "
        "absent from tlgr's whole surface."
    ),
    tags=frozenset({"agent-safe"}),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
