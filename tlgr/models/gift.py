"""Star gifts: the catalogue, the gifts a profile holds, collectibles,
collections, the resale market and the auctions.

A gift is addressed by a **reference**, not an id, and `ref` is that string
everywhere in this module: `msg:<id>` for a gift I received in a private
chat, `<peer>:<saved_id>` for one held by a channel, or a bare collectible
slug. One spelling, so a `ref` read from a listing can be handed straight to
`gift set`, `gift convert` or `gift transfer` without a lookup table.

Every time gate the server publishes is surfaced rather than collapsed into
a boolean. "Can I transfer this?" has three different answers — yes, not yet
(and here is when), never — and a client that reports only the first two
sends its user to wait for a date that will not come.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "GiftAttribute",
    "GiftAuction",
    "GiftAuctionState",
    "GiftCollection",
    "GiftConverted",
    "GiftCrafted",
    "GiftDisplay",
    "GiftListing",
    "GiftOfferResolved",
    "GiftTransferred",
    "GiftUpgraded",
    "GiftVariant",
    "OwnedGift",
    "ResaleGift",
    "StarGift",
    "UniqueGift",
]


class GiftAttribute(Model):
    """One model / pattern / backdrop of a collectible, with its rarity."""

    #: model | pattern | backdrop | original-details
    kind: str = ""
    name: str = ""
    document_id: int | None = None
    rarity_permille: int | None = None
    backdrop_id: int | None = None
    center_color: int | None = None
    edge_color: int | None = None
    pattern_color: int | None = None
    text_color: int | None = None
    crafted: bool = False
    sender_id: int | None = None
    recipient_id: int | None = None
    message: str | None = None
    date: str | None = None


class StarGift(Model):
    """A gift as the catalogue offers it."""

    gift_id: int = 0
    title: str = ""
    stars: int = 0
    convert_stars: int | None = None
    upgrade_stars: int | None = None
    limited: bool = False
    sold_out: bool = False
    birthday: bool = False
    require_premium: bool = False
    availability_remains: int | None = None
    availability_total: int | None = None
    availability_resale: int | None = None
    first_sale_date: str | None = None
    last_sale_date: str | None = None
    document_id: int | None = None
    resell_min_stars: int | None = None
    #: Only filled when `--until` named a recipient.
    can_send: bool | None = None
    can_send_reason: str | None = None
    per_user_total: int | None = None
    per_user_remains: int | None = None


class UniqueGift(Model):
    """A collectible: the upgraded, numbered, tradable form of a gift."""

    slug: str = ""
    gift_id: int = 0
    id: int = 0
    title: str = ""
    num: int = 0
    owner_id: int | None = None
    owner: Peer | None = None
    owner_name: str | None = None
    owner_address: str | None = None
    gift_address: str | None = None
    availability_issued: int | None = None
    availability_total: int | None = None
    attributes: list[GiftAttribute] = []
    resell_stars: int | None = None
    resell_ton: int | None = None
    resale_ton_only: bool = False
    value_stars: int | None = None
    value_ton: int | None = None
    value_currency: str | None = None
    value_usd: int | None = None
    #: A TON-hosted collectible lives outside Telegram's own custody.
    hosted: bool = False
    burned: bool = False
    crafted: bool = False
    theme_available: bool = False
    peer_color_available: bool = False
    offer_min_stars: int | None = None
    craft_chance_permille: int | None = None
    link: str | None = None


class OwnedGift(Model):
    """A gift a profile holds, with every gate that decides what may be done."""

    ref: str = ""
    #: gift | collectible
    kind: str = "gift"
    gift_id: int | None = None
    slug: str | None = None
    title: str = ""
    num: int | None = None
    from_id: int | None = None
    from_peer: Peer | None = None
    name_hidden: bool = False
    message: str | None = None
    date: str | None = None
    date_unix: int | None = None
    msg_id: int | None = None
    saved_id: int | None = None
    pinned: bool = False
    displayed: bool = True
    refunded: bool = False
    can_upgrade: bool = False
    convert_stars: int | None = None
    upgrade_stars: int | None = None
    transfer_stars: int | None = None
    can_export_at: str | None = None
    can_transfer_at: str | None = None
    can_resell_at: str | None = None
    can_craft_at: str | None = None
    locked_until_date: str | None = None
    resell_stars: int | None = None
    resell_ton: int | None = None
    collection_ids: list[int] = []
    hosted: bool = False
    attributes: list[GiftAttribute] = []
    unique: UniqueGift | None = None


class GiftCollection(Model):
    id: int
    title: str = ""
    count: int = 0
    icon_document_id: int | None = None
    order: int | None = None


class GiftDisplay(Model):
    """The profile-display state of one or more gifts after `gift set`."""

    ref: str = ""
    refs: list[str] = []
    displayed: bool | None = None
    pinned: bool | None = None
    worn: bool | None = None
    until: str | None = None
    already: bool = False


class GiftConverted(Model):
    ref: str
    stars_received: int
    balance_after: int | None = None


class GiftUpgraded(Model):
    ref: str
    upgraded: bool
    slug: str | None = None
    num: int | None = None
    attributes: list[GiftAttribute] = []
    price_stars: int | None = None
    refused_reason: str | None = None


class GiftTransferred(Model):
    ref: str
    transferred: bool
    to: int | None = None
    price_stars: int | None = None
    can_transfer_at: str | None = None
    refused_reason: str | None = None


class GiftListing(Model):
    """A collectible put on (or taken off) the resale market."""

    ref: str
    listed: bool
    price_stars: int | None = None
    price_ton: int | None = None
    can_resell_at: str | None = None
    already: bool = False


class ResaleGift(Model):
    slug: str = ""
    num: int = 0
    price_stars: int | None = None
    price_ton: int | None = None
    seller_id: int | None = None
    attributes: list[GiftAttribute] = []


class GiftVariant(Model):
    """One possible outcome of an upgrade, with how likely it is."""

    #: model | pattern | backdrop
    kind: str = ""
    name: str = ""
    document_id: int | None = None
    rarity_permille: int | None = None
    count: int | None = None
    sample: bool = False


class GiftCrafted(Model):
    crafted: bool
    ref: str | None = None
    slug: str | None = None
    burned: list[str] = []
    candidates: list[OwnedGift] = []


class GiftOfferResolved(Model):
    msg_id: int
    #: accepted | declined | refused
    state: str
    price_stars: int | None = None
    buyer: int | None = None
    reason: str | None = None


class GiftAuction(Model):
    auction: str = ""
    gift_id: int | None = None
    slug: str | None = None
    my_bid: int | None = None
    min_bid: int | None = None
    ends_at: str | None = None
    state: str = ""


class GiftAuctionState(Model):
    state: str
    version: int
    auction: str = ""
    min_bid_amount: int | None = None
    my_bid: int | None = None
    position: int | None = None
    ends_at: str | None = None
    timeout: int | None = None
    finished: bool = False
    raw: dict[str, Any] | None = None
