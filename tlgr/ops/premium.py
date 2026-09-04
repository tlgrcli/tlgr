"""The `premium` group: subscription status, the limit table, boosts, gifts.

The genuinely useful part for a CLI is `premium feature list --limits`. The
caption length, the upload size, the folder count, the pinned-chat count and
the public-username count all change with Premium, and a script that guesses
them writes a message the server then refuses. There is no MTProto method for
the table — it is assembled from `help.getAppConfig` — which is why every row
carries `source`.

Buying is absent, throughout and by policy. `premium status` prints the
premium bot and the invoice deep link for a human to open; `premium gift
send` fetches the payment form, reports the price and stops. PR-10 settled
this for the `payment` group (`ops/payment.py` names the four methods it will
not call) and this group inherits it rather than opening a second door onto
the same money.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.admin import Boost
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.premium import (
    GiftCode,
    PremiumFeatures,
    PremiumGiftOption,
    PremiumGiftQuote,
    PremiumLimit,
    PremiumStatus,
)
from tlgr.ops import _settings
from tlgr.ops._common import client
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: appConfig suffixes that make a `*_limit_default` / `*_limit_premium` pair.
_LIMIT_DEFAULT = "_limit_default"
_LIMIT_PREMIUM = "_limit_premium"

#: The boost-level keys, which have no method of their own.
_LEVEL_SUFFIXES = ("_level_min",)


# ---------------------------------------------------------------------------
# premium status / feature list
# ---------------------------------------------------------------------------


class StatusReq(Request):
    pass


async def status(ctx: OpContext, req: StatusReq) -> PremiumStatus:
    """My Telegram Premium status, and how a human would buy it.

    Executing a fiat payment from a CLI is prohibited, and store receipts are
    rejected for third-party api_ids anyway — so the useful answer is the
    deep link, not an error.
    """
    from telethon.tl.functions import help as fn

    handle = client(ctx)
    me = await handle.get_me()
    config = await _settings.app_config(ctx)
    result = PremiumStatus(
        premium=bool(getattr(me, "premium", False)),
        premium_purchase_blocked=bool(config.get("premium_purchase_blocked", True)),
        premium_bot=str(config.get("premium_bot_username") or "") or None,
        reason=_settings.NO_SPEND,
    )
    try:
        promo = await handle(fn.GetPremiumPromoRequest())
    except Exception as exc:  # pragma: no cover - promo is optional
        ctx.warn(f"the Premium promo is unavailable: {exc}")
        return result
    for option in getattr(promo, "period_options", None) or []:
        link = getattr(option, "bot_url", None)
        if link:
            result.invoice_link = str(link)
            break
    return result


SPEC_STATUS = OperationSpec(
    id="premium.status",
    request=StatusReq,
    response=PremiumStatus,
    impl=status,
    summary="My Telegram Premium status (and how to buy it, which tlgr never does)",
    idempotent=True,
    columns=("premium", "premium_purchase_blocked", "premium_bot"),
    headers=("Premium", "Purchase blocked", "Bot"),
    example={"premium": True, "premium_purchase_blocked": True, "premium_bot": "PremiumBot"},
    example_args="premium status",
    covers=("premium.status",),
    tags=frozenset({"agent-safe"}),
)


class FeatureListReq(Request):
    limits: Annotated[bool, opt("--limits", help="Only the *_limit_default/_premium table.")] = (
        False
    )
    boost_levels: Annotated[
        bool, opt("--boost-levels", help="The boost-level unlock table from appConfig.")
    ] = False
    channel: Annotated[
        PeerRef | None,
        opt("--channel", metavar="CHAT", kind="peer", help="Show this channel's boost level."),
    ] = None


async def feature_list(ctx: OpContext, req: FeatureListReq) -> PremiumFeatures:
    """Premium features, the promo text and the limit table.

    The limit table is the practically useful half: caption length, upload
    size, folder counts, pinned chats, public usernames. There is no MTProto
    method for the boost-level table either — it is `channel_*_level_min` and
    `group_*_level_min` from appConfig, assembled here.
    """
    from telethon.tl.functions import help as fn
    from telethon.tl.functions import premium as pfn

    handle = client(ctx)
    config = await _settings.app_config(ctx)
    result = PremiumFeatures()

    pairs: dict[str, dict[str, int]] = {}
    for key, value in config.items():
        for suffix, side in ((_LIMIT_DEFAULT, "default"), (_LIMIT_PREMIUM, "premium")):
            if key.endswith(suffix):
                try:
                    pairs.setdefault(key[: -len(suffix)], {})[side] = int(float(value))
                except (TypeError, ValueError):
                    continue
    result.limits = [
        PremiumLimit(name=name, default=sides.get("default", 0), premium=sides.get("premium", 0))
        for name, sides in sorted(pairs.items())
    ]

    if req.boost_levels or not req.limits:
        result.boost_levels = sorted(
            (
                {"key": key, "level": int(float(value))}
                for key, value in config.items()
                if any(key.endswith(suffix) for suffix in _LEVEL_SUFFIXES) and _is_number(value)
            ),
            key=lambda row: (int(row["level"]), str(row["key"])),
        )

    if req.channel is not None:
        peer = await _settings.resolve(ctx, req.channel)
        boosts = await handle(pfn.GetBoostsStatusRequest(peer=peer))
        result.channel_level = int(getattr(boosts, "level", 0) or 0)

    if req.limits or req.boost_levels:
        return result

    promo = await handle(fn.GetPremiumPromoRequest())
    result.status_text = str(getattr(promo, "status_text", "") or "")
    result.period_options = [
        {
            "months": int(getattr(option, "months", 0) or 0),
            "currency": str(getattr(option, "currency", "") or ""),
            "amount": int(getattr(option, "amount", 0) or 0),
            "bot_url": getattr(option, "bot_url", None),
        }
        for option in getattr(promo, "period_options", None) or []
    ]
    result.video_sections = [str(name) for name in getattr(promo, "video_sections", None) or []]
    return result


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


SPEC_FEATURE_LIST = OperationSpec(
    id="premium.feature.list",
    request=FeatureListReq,
    response=PremiumFeatures,
    impl=feature_list,
    summary="Premium features, promo text and the default/premium limit table",
    description=(
        "`--limits` is the part a script needs: it is what decides whether a "
        "caption, an upload or a folder will be accepted before it is sent."
    ),
    aliases=("premium.features",),
    idempotent=True,
    columns=("status_text", "channel_level"),
    headers=("Promo", "Channel level"),
    example={
        "limits": [{"name": "caption_length", "default": 1024, "premium": 2048}],
        "boost_levels": [],
    },
    example_args="premium feature list --limits",
    covers=("content.limits", "premium.boost-level-features", "premium.features-list"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# premium boost list
# ---------------------------------------------------------------------------


class BoostListReq(Request):
    channel: Annotated[
        PeerRef | None,
        arg(0, metavar="CHANNEL", required=False, kind="peer", help="Omit for my own slots."),
    ] = None
    gifts: Annotated[bool, opt("--gifts", help="Only gift and giveaway boosts.")] = False
    of_user: Annotated[
        PeerRef | None,
        opt("--of-user", metavar="USER", kind="user", help="Narrow to one booster."),
    ] = None


async def boost_list(ctx: OpContext, req: BoostListReq) -> Page[Boost]:
    """My boost slots, or the boosts applied to a channel I administer.

    The same listing `boost list` answers with, reached from the Premium
    screen: Premium grants `boosts_per_premium` slots and gifting Premium
    adds more, which is a fact about the subscription rather than about a
    channel.
    """
    from tlgr.ops.chat_stats import BoostListReq as StatsBoostListReq
    from tlgr.ops.chat_stats import list_boosts

    return await list_boosts(
        ctx,
        StatsBoostListReq(
            chat=req.channel,
            user=req.of_user,
            gifts=req.gifts,
            mine=req.channel is None,
        ),
    )


SPEC_BOOST_LIST = OperationSpec(
    id="premium.boost.list",
    request=BoostListReq,
    response=Page[Boost],
    impl=boost_list,
    summary="My boost slots, or the boosts applied to a channel I administer",
    aliases=("boost.slots",),
    paginated=PageKind.PARTICIPANTS,
    idempotent=True,
    columns=("slot", "chat_id", "user_id", "expires", "cooldown_until_date"),
    headers=("Slot", "Chat", "User", "Expires", "Cooldown"),
    example={
        "items": [{"slot": 1, "chat_id": -1001600, "expires": "2026-10-01T00:00:00Z"}],
        "has_more": False,
    },
    example_args="premium boost list",
    covers=("premium.channel-boosts-list", "premium.my-boosts", "stories.boost-status"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# premium gift list / send
# ---------------------------------------------------------------------------


class GiftListReq(Request):
    boost_peer: Annotated[
        PeerRef | None,
        opt(
            "--boost-peer", metavar="CHAT", kind="peer", help="Options tied to boosting a channel."
        ),
    ] = None
    single: Annotated[
        bool, opt("--single/--all-options", help="Only options for a single recipient.")
    ] = True


async def gift_list(ctx: OpContext, req: GiftListReq) -> Page[PremiumGiftOption]:
    """Premium gift price options, in Stars and in fiat.

    A third-party client can only act on the `XTR` (Stars) options; the fiat
    ones exist for the official apps' store flows. Options with `users > 1`
    are giveaway options rather than direct gifts.
    """
    from telethon.tl.functions import payments as fn

    peer = await _settings.resolve(ctx, req.boost_peer) if req.boost_peer else None
    result = await client(ctx)(fn.GetPremiumGiftCodeOptionsRequest(boost_peer=peer))
    rows = [
        PremiumGiftOption(
            months=int(getattr(option, "months", 0) or 0),
            users=int(getattr(option, "users", 1) or 1),
            currency=str(getattr(option, "currency", "") or ""),
            amount=int(getattr(option, "amount", 0) or 0),
            store_product=getattr(option, "store_product", None),
        )
        for option in result or []
    ]
    if req.single:
        rows = [row for row in rows if row.users == 1]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_GIFT_LIST = OperationSpec(
    id="premium.gift.list",
    request=GiftListReq,
    response=Page[PremiumGiftOption],
    impl=gift_list,
    summary="Premium gift price options (Stars and fiat), for a user or a channel giveaway",
    aliases=("premium.gift.options",),
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("months", "users", "currency", "amount", "store_product"),
    headers=("Months", "Users", "Currency", "Amount", "Store"),
    example={
        "items": [{"months": 3, "users": 1, "currency": "XTR", "amount": 1000}],
        "has_more": False,
    },
    example_args="premium gift list",
    covers=("premium.gift-options",),
    tags=frozenset({"agent-safe"}),
)


class GiftSendReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Who to gift Premium to.")]
    months: Annotated[int | None, opt("--months", metavar="N", help="Subscription length.")] = None
    message: Annotated[
        str | None, opt("--message", metavar="TEXT", help="Note attached to the gift.")
    ] = None


async def gift_send(ctx: OpContext, req: GiftSendReq) -> PremiumGiftQuote:
    """Price gifting Premium to a user — and refuse to buy it.

    The form is fetched so the price is visible and the recipient is
    validated; `payments.sendStarsForm` is deliberately absent from tlgr's
    whole surface, so `ok` is false and `reason` says why.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    if not req.months:
        raise UsageError(
            "--months is the subscription length; `premium gift list` shows the options",
            field="months",
        )
    user = await _settings.input_user(ctx, req.user, field="user")
    invoice = types.InputInvoicePremiumGiftStars(
        user_id=user,
        months=int(req.months),
        message=(types.TextWithEntities(text=req.message, entities=[]) if req.message else None),
    )
    form = await client(ctx)(fn.GetPaymentFormRequest(invoice=invoice))
    prices = getattr(getattr(form, "invoice", None), "prices", None) or []
    return PremiumGiftQuote(
        user_id=_settings.peer_of(await _settings.resolve(ctx, req.user)),
        months=int(req.months),
        stars=sum(int(getattr(price, "amount", 0) or 0) for price in prices),
        currency=str(getattr(getattr(form, "invoice", None), "currency", "XTR") or "XTR"),
        ok=False,
        reason=_settings.NO_SPEND,
        form_id=getattr(form, "form_id", None),
    )


SPEC_GIFT_SEND = OperationSpec(
    id="premium.gift.send",
    request=GiftSendReq,
    response=PremiumGiftQuote,
    impl=gift_send,
    summary="Price gifting Telegram Premium to a user (tlgr reads the form, never signs it)",
    idempotent=True,
    columns=("user_id", "months", "stars", "currency", "ok"),
    headers=("User", "Months", "Stars", "Currency", "Sent"),
    example={"user_id": 777123, "months": 3, "stars": 1000, "currency": "XTR", "ok": False},
    example_args="premium gift send @alice --months 3",
    covers_partial=("premium.gift-to-user",),
    coverage_note=(
        "The recipient, the length and the price are reported; signing the "
        "payment form is absent from tlgr's whole surface by policy."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# premium giftcode get
# ---------------------------------------------------------------------------


def _code_model(slug: str, raw: Any) -> GiftCode:
    """`payments.checkedGiftCode` as a model.

    The wire says `days`; every client and every price option says *months*,
    so both are reported and neither is invented — `months` is the whole
    months the day count buys.
    """
    date = getattr(raw, "date", None)
    days = getattr(raw, "days", None)
    return GiftCode(
        slug=slug,
        link=f"https://t.me/giftcode/{slug}",
        from_id=_peer_id(getattr(raw, "from_id", None)),
        to_id=getattr(raw, "to_id", None),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        months=int(days) // 30 if days else None,
        days=int(days) if days else None,
        used_date=fmt_dt(getattr(raw, "used_date", None)),
        via_giveaway=bool(getattr(raw, "via_giveaway", False)),
        giveaway_msg_id=getattr(raw, "giveaway_msg_id", None),
        used=getattr(raw, "used_date", None) is not None,
    )


def _peer_id(peer: Any) -> int | None:
    from tlgr.ops._serialize import peer_id_of

    return peer_id_of(peer) if peer is not None else None


class GiftcodeGetReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="The gift code, or a t.me/giftcode link.")]
    redeem: Annotated[bool, opt("--redeem", help="Apply the code to this account (free).")] = False


async def giftcode_get(ctx: OpContext, req: GiftcodeGetReq) -> GiftCode:
    """Check a Premium gift code, and optionally redeem it.

    Redeeming involves no payment: the code was already bought by whoever
    sent it, so this is one of the few `payments.*` writes tlgr performs.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    slug = req.slug.rsplit("/", 1)[-1]
    result = await handle(fn.CheckGiftCodeRequest(slug=slug))
    model = _code_model(slug, result)
    if req.redeem:
        if model.used:
            _already(ctx)
        else:
            await handle(fn.ApplyGiftCodeRequest(slug=slug))
            ctx.emit("giftcode_applied", {"slug": slug})
            model.used = True
    return model


def _already(ctx: OpContext) -> None:
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


SPEC_GIFTCODE_GET = OperationSpec(
    id="premium.giftcode.get",
    request=GiftcodeGetReq,
    response=GiftCode,
    impl=giftcode_get,
    summary="Check a Premium gift code, and optionally redeem it",
    description="Redeeming costs nothing — the code is already paid for.",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("slug", "from_id", "to_id", "months", "used_date", "via_giveaway"),
    headers=("Slug", "From", "To", "Months", "Used", "Giveaway"),
    example={"slug": "abcdef", "from_id": -1001600, "months": 3, "via_giveaway": True},
    example_args="premium giftcode get abcdef",
    covers=("premium.giftcode-apply", "premium.giftcode-check"),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
