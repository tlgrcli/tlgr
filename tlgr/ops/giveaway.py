"""The `giveaway` group: joining one, checking a code, launching a prepaid one.

Giveaways are a first-class surface in the official clients and they are
almost entirely free to operate from a CLI, which is why they get a noun of
their own rather than living under `gift`:

* **joining** spends a boost slot I already own, not money;
* **redeeming a code** activates a subscription somebody else paid for;
* **launching a prepaid giveaway** spends nothing either — the giveaway was
  bought earlier, and `payments.launchPrepaidGiveaway` only starts it.

Buying a *new* giveaway is a payment and is therefore absent, like every
other purchase in tlgr.

`giveaway get` answers the question the public message cannot: am I eligible,
did I win, and which code did I win. `messageMediaGiveaway` carries the public
half; `payments.getGiveawayInfo` carries the personal half, and one command
reports both.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.admin import BoostApplied
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.premium import (
    GiftCode,
    GiftCodeApplied,
    GiveawayInfo,
    GiveawayLaunched,
    GiveawayWinner,
    PrepaidGiveaway,
)
from tlgr.ops import _settings
from tlgr.ops._common import client, random_id, window
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `giveawayInfo.disallowed_reason` → the word tlgr reports.
DISALLOWED = {
    "GiveawayInfoDisallowedCountry": "disallowed-country",
    "GiveawayInfoDisallowedAdminRequired": "admin",
    "GiveawayInfoDisallowedJoinedTooEarly": "joined-too-early",
}


# ---------------------------------------------------------------------------
# giveaway get
# ---------------------------------------------------------------------------


class GetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The giveaway's channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="The giveaway message.")]
    winners: Annotated[
        bool, opt("--winners", help="Resolve the winner list from the results message.")
    ] = False


async def get(ctx: OpContext, req: GetReq) -> GiveawayInfo:
    """Giveaway status and results: am I eligible, did I win, who won.

    Two sources, one answer. The message media says how many winners there
    are, when it ends and which countries are eligible;
    `payments.getGiveawayInfo` says whether *this* account is in it and, if
    it has finished, which gift code it won.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    peer = await _settings.resolve(ctx, req.chat)
    chat_id = _settings.peer_of(peer)
    raw = await handle(fn.GetGiveawayInfoRequest(peer=peer, msg_id=int(req.msg_id)))
    finished = type(raw).__name__ == "PaymentsGiveawayInfoResults"
    info = GiveawayInfo(
        chat_id=chat_id,
        msg_id=int(req.msg_id),
        state="finished" if finished else "ongoing",
        start_date=fmt_dt(getattr(raw, "start_date", None)),
        joined=bool(getattr(raw, "participating", False)),
        disallowed_reason=_disallowed_word(raw),
        winner=bool(getattr(raw, "winner", False)),
        refunded=bool(getattr(raw, "refunded", False)),
        gift_code_slug=getattr(raw, "gift_code_slug", None),
        activated_count=getattr(raw, "activated_count", None),
        until_date=fmt_dt(getattr(raw, "finish_date", None)),
        stars=getattr(raw, "stars_prize", None),
    )

    media = await _giveaway_media(ctx, peer, int(req.msg_id))
    if media is not None:
        info.winners_count = getattr(media, "quantity", None) or getattr(
            media, "winners_count", None
        )
        info.months = getattr(media, "months", None)
        info.only_new_subscribers = bool(getattr(media, "only_new_subscribers", False))
        info.countries = [str(code) for code in getattr(media, "countries_iso2", None) or []]
        info.prize_description = getattr(media, "prize_description", None)
        if info.until_date is None:
            info.until_date = fmt_dt(getattr(media, "until_date", None))

    if req.winners:
        # Only the *results* media carries the winner vector; an ongoing
        # giveaway has none, and reporting an empty list for one would read
        # as "nobody won" rather than "not drawn yet".
        info.winners = [
            GiveawayWinner(user_id=int(user_id))
            for user_id in getattr(media, "winners", None) or []
        ]
    return info


def _disallowed_word(raw: Any) -> str | None:
    """The `giveawayInfo` reason, whichever of the three flavours it is."""
    for reason in getattr(raw, "disallowed_reason", None) or []:
        word = DISALLOWED.get(type(reason).__name__)
        if word:
            return word
    if getattr(raw, "admin_disallowed", False):
        return "admin"
    if getattr(raw, "joined_too_early_date", None):
        return "joined-too-early"
    if getattr(raw, "disallowed_country", None):
        return "disallowed-country"
    return None


async def _giveaway_media(ctx: OpContext, peer: Any, msg_id: int) -> Any:
    """The `messageMediaGiveaway*` on the giveaway post, or None."""
    from tlgr.ops import _media

    try:
        message = await _media.fetch_message(ctx, peer, msg_id)
    except Exception as exc:
        ctx.warn(f"could not read the giveaway message: {exc}")
        return None
    media = getattr(message, "media", None)
    if type(media).__name__ in ("MessageMediaGiveaway", "MessageMediaGiveawayResults"):
        return media
    return None


SPEC_GET = OperationSpec(
    id="giveaway.get",
    request=GetReq,
    response=GiveawayInfo,
    impl=get,
    summary="Giveaway status and results: am I eligible, did I win, who won",
    aliases=("giveaway.info",),
    idempotent=True,
    columns=("state", "joined", "winner", "winners_count", "until_date", "gift_code_slug"),
    headers=("State", "Joined", "Won", "Winners", "Until", "Code"),
    example={
        "chat_id": -1001600,
        "msg_id": 42,
        "state": "ongoing",
        "joined": True,
        "winners_count": 10,
    },
    example_args="giveaway get @mychannel 42",
    covers=(
        "giveaway.info",
        "giveaway.results",
        "groups-channels-admin.giveaway-info",
        "premium.giveaway-info",
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# giveaway join
# ---------------------------------------------------------------------------


class JoinReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The channel to boost.")]
    slots: Annotated[
        list[int], opt("--slots", metavar="N", help="Which of my boost slots to use.")
    ] = []


async def join(ctx: OpContext, req: JoinReq) -> BoostApplied:
    """Join a giveaway by boosting the channel.

    Joining *is* boosting: the giveaway counts participants by boost. The
    slot is occupied for a month, which is why the command is confirmed, and
    the boost side of it is `boost add` — one implementation, reached from
    the two places the GUI reaches it from.
    """
    from tlgr.ops.chat_stats import BoostAddReq, add_boost

    return await add_boost(ctx, BoostAddReq(chat=req.chat, slots=list(req.slots)))


SPEC_JOIN = OperationSpec(
    id="giveaway.join",
    request=JoinReq,
    response=BoostApplied,
    impl=join,
    summary="Join a giveaway by boosting the channel",
    description=(
        "Needs Premium (or gifted boost slots). A slot stays occupied for a "
        "month, so `-y` is required off a TTY."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("chat_id", "level", "boosts", "slots", "already"),
    headers=("Chat", "Level", "Boosts", "Slots", "Already"),
    example={"chat_id": -1001600, "level": 4, "boosts": 15, "slots": [1]},
    example_args="giveaway join @mychannel",
    covers=("giveaway.join-by-boosting",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# giveaway list
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="The channel to inspect."),
    ] = None
    prepaid: Annotated[
        bool, opt("--prepaid/--no-prepaid", help="Prepaid giveaways bought for this channel.")
    ] = True
    codes: Annotated[
        bool, opt("--codes", help="My received giveaway gift codes (the inbox side).")
    ] = False


async def list_(ctx: OpContext, req: ListReq) -> Page[PrepaidGiveaway]:
    """Prepaid giveaways on a channel, or the gift codes I have received.

    `payments.getPrepaidGiveaways` has no request class in Telethon 1.44, but
    `premium.getBoostsStatus` carries the same `prepaid_giveaways` vector —
    so the answer is the server's, from the method this build can send.
    """
    from telethon.tl.functions import premium as fn

    handle = client(ctx)
    if req.codes:
        return await _received_codes(ctx)

    if req.chat is None:
        raise UsageError("name a channel, or pass --codes for my received codes", field="chat")
    peer = await _settings.resolve(ctx, req.chat)
    status = await handle(fn.GetBoostsStatusRequest(peer=peer))
    rows = [
        PrepaidGiveaway(
            id=int(getattr(row, "id", 0) or 0),
            quantity=int(getattr(row, "quantity", 0) or 0),
            months=getattr(row, "months", None),
            stars=getattr(row, "stars", None),
            boosts=getattr(row, "boosts", None),
            date=fmt_dt(getattr(row, "date", None)),
            date_unix=to_unix(getattr(row, "date", None)),
            from_chat=_settings.peer_of(peer),
        )
        for row in getattr(status, "prepaid_giveaways", None) or []
    ]
    return Page(items=rows, has_more=False, total=len(rows))


async def _received_codes(ctx: OpContext) -> Page[PrepaidGiveaway]:
    """Gift codes that arrived as `messageActionGiftCode` service messages.

    There is no "my codes" endpoint: the codes are service messages in the
    account's own history, so they are found by scanning and then checked one
    by one, which is exactly what an official client does.
    """
    from telethon.tl.functions import payments as fn

    from tlgr.ops.chat import ListReq as ChatListReq
    from tlgr.ops.chat import list_chats

    handle = client(ctx)
    limit, _ = window(ctx, "giveaway.list", PageKind.LOCAL, default=20)
    rows: list[PrepaidGiveaway] = []
    dialogs = await list_chats(ctx, ChatListReq())
    for dialog in dialogs.items:
        message = getattr(dialog, "last_message", None)
        action = getattr(message, "action", None)
        slug = getattr(action, "slug", None) if action is not None else None
        if not slug:
            continue
        try:
            checked = await handle(fn.CheckGiftCodeRequest(slug=slug))
        except Exception as exc:
            ctx.warn(f"could not check the code {slug}: {exc}")
            continue
        rows.append(
            PrepaidGiveaway(
                id=0,
                quantity=1,
                months=getattr(checked, "months", None),
                slug=slug,
                used=getattr(checked, "used_date", None) is not None,
                date=fmt_dt(getattr(checked, "date", None)),
                date_unix=to_unix(getattr(checked, "date", None)),
                from_chat=getattr(dialog.chat, "id", None) if dialog.chat else None,
            )
        )
        if len(rows) >= limit:
            break
    return build_page(
        rows, op="giveaway.list", kind=PageKind.LOCAL, has_more=False, total=len(rows)
    )


SPEC_LIST = OperationSpec(
    id="giveaway.list",
    request=ListReq,
    response=Page[PrepaidGiveaway],
    impl=list_,
    summary="Prepaid giveaways available on a channel, and the gift codes I received",
    description=(
        "The prepaid list comes from `premium.getBoostsStatus`, which carries "
        "the same vector as the absent `payments.getPrepaidGiveaways`. "
        "`--codes` scans for `messageActionGiftCode` service messages and "
        "checks each slug, because there is no 'my codes' endpoint."
    ),
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("id", "quantity", "months", "stars", "slug", "used"),
    headers=("Id", "Winners", "Months", "Stars", "Code", "Used"),
    example={
        "items": [{"id": 77, "quantity": 10, "months": 3, "from_chat": -1001600}],
        "has_more": False,
    },
    example_args="giveaway list @mychannel",
    covers=("giveaway.gift-code-received", "giveaway.list-prepaid"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# giveaway start
# ---------------------------------------------------------------------------


class StartReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The channel.")]
    prepaid_id: Annotated[int, arg(1, metavar="PREPAID_ID", help="From `giveaway list <chat>`.")]
    winners: Annotated[int | None, opt("--winners", metavar="N", help="Number of winners.")] = None
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", kind="datetime", help="Draw date.")
    ] = None
    only_new: Annotated[
        bool, opt("--only-new", help="Only subscribers who joined after the start.")
    ] = False
    public_winners: Annotated[
        bool, opt("--public-winners", help="Show the winner list when it ends.")
    ] = False
    countries: Annotated[
        str | None, opt("--countries", metavar="LIST", help="ISO-2 codes, comma separated.")
    ] = None
    also_chat: Annotated[
        list[str],
        opt("--also-chat", metavar="CHAT", help="Another channel a participant must join."),
    ] = []
    prize: Annotated[str | None, opt("--prize", metavar="TEXT", help="Prize description.")] = None


async def start(ctx: OpContext, req: StartReq) -> GiveawayLaunched:
    """Launch a giveaway that was already paid for.

    Launching a prepaid giveaway is **not** a payment, which is why it is
    here at all: the Stars or the fiat were spent when the giveaway was
    bought, and this only starts it. Creating a new (bought) giveaway is a
    purchase and is absent.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    peer = await _settings.resolve(ctx, req.chat)
    config = await _settings.app_config(ctx)
    countries = [part.strip().upper() for part in (req.countries or "").split(",") if part.strip()]
    max_countries = int(config.get("giveaway_countries_max") or 0)
    if max_countries and len(countries) > max_countries:
        raise UsageError(f"--countries takes at most {max_countries} entries", field="countries")
    extra = [await _settings.resolve(ctx, ref) for ref in req.also_chat]
    max_peers = int(config.get("giveaway_add_peers_max") or 0)
    if max_peers and len(extra) > max_peers:
        raise UsageError(f"--also-chat takes at most {max_peers} channels", field="also_chat")

    until = parse_dt(req.until) if req.until else None
    purpose = types.InputStorePaymentPremiumGiveaway(
        boost_peer=peer,
        until_date=until,
        currency="XTR",
        amount=0,
        only_new_subscribers=req.only_new or None,
        winners_are_visible=req.public_winners or None,
        additional_peers=extra or None,
        countries_iso2=countries or None,
        prize_description=req.prize,
        random_id=random_id(),
    )
    result = await handle(
        fn.LaunchPrepaidGiveawayRequest(peer=peer, giveaway_id=int(req.prepaid_id), purpose=purpose)
    )
    msg_id = next(
        (
            int(getattr(getattr(update, "message", None), "id", 0) or 0)
            for update in getattr(result, "updates", None) or []
            if getattr(update, "message", None) is not None
        ),
        None,
    )
    chat_id = _settings.peer_of(peer)
    ctx.emit("giveaway_started", {"chat_id": chat_id, "prepaid_id": int(req.prepaid_id)})
    return GiveawayLaunched(
        chat_id=chat_id,
        prepaid_id=int(req.prepaid_id),
        msg_id=msg_id,
        winners_count=int(req.winners or 0),
        until_date=fmt_dt(until),
    )


SPEC_START = OperationSpec(
    id="giveaway.start",
    request=StartReq,
    response=GiveawayLaunched,
    impl=start,
    summary="Launch a giveaway that was already paid for (prepaid)",
    description=(
        "Not a payment: the giveaway was bought earlier and this only starts "
        "it. Creating a new, bought giveaway is a purchase and is absent."
    ),
    aliases=("giveaway.launch",),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "prepaid_id", "msg_id", "winners_count", "until_date"),
    headers=("Chat", "Prepaid", "Post", "Winners", "Until"),
    example={"chat_id": -1001600, "prepaid_id": 77, "msg_id": 42, "winners_count": 10},
    example_args="giveaway start @mychannel 77 --winners 10 --until +7d",
    covers=("groups-channels-admin.giveaway-prepaid-launch", "premium.giveaway-create"),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# giveaway code check / apply
# ---------------------------------------------------------------------------


class CodeCheckReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="The code, or a t.me/giftcode link.")]


async def code_check(ctx: OpContext, req: CodeCheckReq) -> GiftCode:
    """Check a gift code before using it.

    Also how a giveaway admin identifies a winner: `to_id` is who the code
    was issued to, which the public results message does not always say.
    """
    from tlgr.ops.premium import GiftcodeGetReq, giftcode_get

    return await giftcode_get(ctx, GiftcodeGetReq(slug=req.slug))


SPEC_CODE_CHECK = OperationSpec(
    id="giveaway.code.check",
    request=CodeCheckReq,
    response=GiftCode,
    impl=code_check,
    summary="Check a gift code / t.me/giftcode link before using it",
    aliases=("giftcode.check",),
    idempotent=True,
    columns=("slug", "from_id", "to_id", "date", "months", "used_date", "via_giveaway"),
    headers=("Slug", "From", "To", "Date", "Months", "Used", "Giveaway"),
    example={"slug": "abcdef", "from_id": -1001600, "to_id": 777123, "months": 3},
    example_args="giveaway code check abcdef",
    covers=("giftcode.check",),
    tags=frozenset({"agent-safe"}),
)


class CodeApplyReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="The code, or a t.me/giftcode link.")]


async def code_apply(ctx: OpContext, req: CodeApplyReq) -> GiftCodeApplied:
    """Redeem a gift code, activating the Premium subscription it carries.

    Costs nothing: the code was paid for by whoever gave it. A code that is
    already used answers `already: true` rather than failing.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    slug = req.slug.rsplit("/", 1)[-1]
    checked = await handle(fn.CheckGiftCodeRequest(slug=slug))
    if getattr(checked, "used_date", None) is not None:
        mark = getattr(ctx, "mark_already", None)
        if callable(mark):
            mark()
        return GiftCodeApplied(
            slug=slug,
            applied=False,
            months=getattr(checked, "months", None),
            already=True,
        )
    await handle(fn.ApplyGiftCodeRequest(slug=slug))
    ctx.emit("giftcode_applied", {"slug": slug})
    return GiftCodeApplied(slug=slug, applied=True, months=getattr(checked, "months", None))


SPEC_CODE_APPLY = OperationSpec(
    id="giveaway.code.apply",
    request=CodeApplyReq,
    response=GiftCodeApplied,
    impl=code_apply,
    summary="Redeem a gift code (activates the Premium subscription it carries)",
    description="Free: the code is already paid for, so this is not a purchase.",
    aliases=("giftcode.apply",),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("slug", "applied", "months", "already"),
    headers=("Slug", "Applied", "Months", "Already"),
    example={"slug": "abcdef", "applied": True, "months": 3},
    example_args="giveaway code apply abcdef",
    covers=("giftcode.apply", "groups-channels-admin.gift-code-redeem"),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
