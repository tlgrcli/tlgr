"""Telegram Premium, boosts, giveaways and gift codes.

The limit table is the part a CLI actually uses: caption length, upload size,
folder count, pinned chats, public usernames all double with Premium, and a
script that guesses them writes a message the server then refuses. It has no
MTProto method of its own — it is assembled from `help.getAppConfig` — which
is why `PremiumLimit` carries `source`.

Buying is absent throughout, as in `ops/payment.py`: `PremiumGiftQuote`
reports the price and says, in `reason`, that tlgr does not sign the form.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "GiftCode",
    "GiftCodeApplied",
    "GiveawayInfo",
    "GiveawayLaunched",
    "GiveawayWinner",
    "PremiumFeatures",
    "PremiumGiftOption",
    "PremiumGiftQuote",
    "PremiumLimit",
    "PremiumStatus",
    "PrepaidGiveaway",
]


class PremiumStatus(Model):
    premium: bool = False
    premium_until: str | None = None
    #: appConfig `premium_purchase_blocked`: the store path is closed here.
    premium_purchase_blocked: bool = True
    invoice_link: str | None = None
    premium_bot: str | None = None
    reason: str = ""


class PremiumLimit(Model):
    """One `*_limit_default` / `*_limit_premium` pair."""

    name: str
    default: int = 0
    premium: int = 0
    source: str = "app-config"


class PremiumFeatures(Model):
    status_text: str = ""
    period_options: list[dict[str, object]] = []
    video_sections: list[str] = []
    limits: list[PremiumLimit] = []
    #: Assembled from `channel_*_level_min` / `group_*_level_min`.
    boost_levels: list[dict[str, object]] = []
    channel_level: int | None = None


class PremiumGiftOption(Model):
    months: int = 0
    users: int = 1
    currency: str = ""
    amount: int = 0
    store_product: str | None = None


class PremiumGiftQuote(Model):
    """The price of gifting Premium, and the refusal to pay it."""

    user_id: int
    months: int = 0
    stars: int = 0
    currency: str = "XTR"
    ok: bool = False
    reason: str = ""
    form_id: int | None = None


class GiftCode(Model):
    """`payments.checkGiftCode`, plus the link it came from."""

    slug: str = ""
    link: str = ""
    from_id: int | None = None
    to_id: int | None = None
    date: str | None = None
    date_unix: int | None = None
    months: int | None = None
    days: int | None = None
    used_date: str | None = None
    via_giveaway: bool = False
    giveaway_msg_id: int | None = None
    used: bool = False


class GiftCodeApplied(Model):
    slug: str
    applied: bool = False
    months: int | None = None
    until_date: str | None = None
    already: bool = False


class GiveawayWinner(Model):
    user_id: int
    user: Peer | None = None
    slug: str | None = None


class GiveawayInfo(Model):
    """`payments.getGiveawayInfo` — the personal "did I win?" answer."""

    chat_id: int = 0
    msg_id: int = 0
    #: ongoing | finished
    state: str = "ongoing"
    start_date: str | None = None
    until_date: str | None = None
    winners_count: int | None = None
    months: int | None = None
    stars: int | None = None
    only_new_subscribers: bool = False
    countries: list[str] = []
    joined: bool = False
    #: participating | already-participating | disallowed-country | admin |
    #: joined-too-early — why this account cannot take part.
    disallowed_reason: str | None = None
    winner: bool = False
    refunded: bool = False
    gift_code_slug: str | None = None
    activated_count: int | None = None
    winners: list[GiveawayWinner] = []
    prize_description: str | None = None


class PrepaidGiveaway(Model):
    id: int
    quantity: int = 0
    months: int | None = None
    stars: int | None = None
    boosts: int | None = None
    date: str | None = None
    date_unix: int | None = None
    slug: str | None = None
    used: bool = False
    from_chat: int | None = None


class GiveawayLaunched(Model):
    chat_id: int
    prepaid_id: int = 0
    msg_id: int | None = None
    winners_count: int = 0
    until_date: str | None = None
