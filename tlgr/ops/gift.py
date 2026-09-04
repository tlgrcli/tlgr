"""The `gift` group: the catalogue, the gifts a profile holds, collectibles.

A gift is addressed by a **reference**, and `ref` is the same string
everywhere here: `msg:<id>` for one received in a private chat,
`<peer>:<saved_id>` for one a channel holds, or a bare collectible slug (a
`t.me/nft/<slug>` link is accepted). One spelling, so a `ref` read out of
`gift list` can be handed straight to `gift set`, `gift convert`, `gift
transfer` or `gift upgrade` without a lookup table.

The dividing line in this group is **cost**, not danger:

* free operations are performed — displaying, pinning, wearing, converting a
  gift back into Stars, a free transfer, a prepaid upgrade, listing a
  collectible for sale, declining an offer, crafting;
* anything whose completion needs a payment form signed is priced and
  refused, with `refused_reason` saying so. `payments.sendStarsForm` is
  absent from tlgr's whole surface (`ops/payment.py`), and this group does
  not open a second door onto it.

`gift craft` is the one free operation that still needs `--yes`: it burns
every input gift whatever the outcome, which is `rm -rf` semantics without a
payment anywhere near it.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, fmt_unix, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.gift import (
    GiftAttribute,
    GiftAuction,
    GiftAuctionState,
    GiftCollection,
    GiftConverted,
    GiftCrafted,
    GiftDisplay,
    GiftListing,
    GiftOfferResolved,
    GiftTransferred,
    GiftUpgraded,
    GiftVariant,
    OwnedGift,
    ResaleGift,
    StarGift,
    UniqueGift,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _settings
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]


# ---------------------------------------------------------------------------
# Shared serialisation
# ---------------------------------------------------------------------------


def _attribute(raw: Any) -> GiftAttribute:
    from tlgr.ops._serialize import peer_id_of

    name = type(raw).__name__.removeprefix("StarGiftAttribute")
    kind = {
        "Model": "model",
        "Pattern": "pattern",
        "Backdrop": "backdrop",
        "OriginalDetails": "original-details",
    }.get(name, name.lower())
    rarity = getattr(raw, "rarity", None)
    document = getattr(raw, "document", None)
    message = getattr(raw, "message", None)
    sender = getattr(raw, "sender_id", None)
    recipient = getattr(raw, "recipient_id", None)
    return GiftAttribute(
        kind=kind,
        name=str(getattr(raw, "name", "") or ""),
        document_id=getattr(document, "id", None),
        rarity_permille=getattr(rarity, "permille", None),
        backdrop_id=getattr(raw, "backdrop_id", None),
        center_color=getattr(raw, "center_color", None),
        edge_color=getattr(raw, "edge_color", None),
        pattern_color=getattr(raw, "pattern_color", None),
        text_color=getattr(raw, "text_color", None),
        crafted=bool(getattr(raw, "crafted", False)),
        sender_id=peer_id_of(sender) if sender is not None else None,
        recipient_id=peer_id_of(recipient) if recipient is not None else None,
        message=getattr(message, "text", None),
        date=fmt_dt(getattr(raw, "date", None)),
    )


def _unique(raw: Any, known: dict[int, Any] | None = None) -> UniqueGift:
    from tlgr.ops._serialize import peer_id_of

    owner = getattr(raw, "owner_id", None)
    owner_id = peer_id_of(owner) if owner is not None else None
    resale = getattr(raw, "resell_amount", None) or []
    stars = next((a for a in resale if type(a).__name__ == "StarsAmount"), None)
    ton = next((a for a in resale if type(a).__name__ == "StarsTonAmount"), None)
    slug = str(getattr(raw, "slug", "") or "")
    return UniqueGift(
        slug=slug,
        gift_id=int(getattr(raw, "gift_id", 0) or 0),
        id=int(getattr(raw, "id", 0) or 0),
        title=str(getattr(raw, "title", "") or ""),
        num=int(getattr(raw, "num", 0) or 0),
        owner_id=owner_id,
        owner=_settings.peer_model((known or {}).get(abs(owner_id)) if owner_id else None),
        owner_name=getattr(raw, "owner_name", None),
        owner_address=getattr(raw, "owner_address", None),
        gift_address=getattr(raw, "gift_address", None),
        availability_issued=getattr(raw, "availability_issued", None),
        availability_total=getattr(raw, "availability_total", None),
        attributes=[_attribute(a) for a in getattr(raw, "attributes", None) or []],
        resell_stars=_settings.stars_of(stars)[0] if stars is not None else None,
        resell_ton=_settings.stars_of(ton)[0] if ton is not None else None,
        resale_ton_only=bool(getattr(raw, "resale_ton_only", False)),
        value_stars=getattr(raw, "value_amount", None),
        value_currency=getattr(raw, "value_currency", None),
        value_usd=getattr(raw, "value_usd_amount", None),
        hosted=getattr(raw, "host_id", None) is not None,
        burned=bool(getattr(raw, "burned", False)),
        crafted=bool(getattr(raw, "crafted", False)),
        theme_available=bool(getattr(raw, "theme_available", False)),
        peer_color_available=getattr(raw, "peer_color", None) is not None,
        offer_min_stars=getattr(raw, "offer_min_stars", None),
        craft_chance_permille=getattr(raw, "craft_chance_permille", None),
        link=f"https://t.me/nft/{slug}" if slug else None,
    )


def _catalog_gift(raw: Any) -> StarGift:
    return StarGift(
        gift_id=int(getattr(raw, "id", 0) or 0),
        title=str(getattr(raw, "title", "") or ""),
        stars=int(getattr(raw, "stars", 0) or 0),
        convert_stars=getattr(raw, "convert_stars", None),
        upgrade_stars=getattr(raw, "upgrade_stars", None),
        limited=bool(getattr(raw, "limited", False)),
        sold_out=bool(getattr(raw, "sold_out", False)),
        birthday=bool(getattr(raw, "birthday", False)),
        require_premium=bool(getattr(raw, "require_premium", False)),
        availability_remains=getattr(raw, "availability_remains", None),
        availability_total=getattr(raw, "availability_total", None),
        availability_resale=getattr(raw, "availability_resale", None),
        first_sale_date=fmt_dt(getattr(raw, "first_sale_date", None)),
        last_sale_date=fmt_dt(getattr(raw, "last_sale_date", None)),
        document_id=getattr(getattr(raw, "sticker", None), "id", None),
        resell_min_stars=getattr(raw, "resell_min_stars", None),
        per_user_total=getattr(raw, "per_user_total", None),
        per_user_remains=getattr(raw, "per_user_remains", None),
    )


def _owned(raw: Any, known: dict[int, Any]) -> OwnedGift:
    from tlgr.ops._serialize import peer_id_of

    gift = getattr(raw, "gift", None)
    unique = _unique(gift, known) if type(gift).__name__ == "StarGiftUnique" else None
    sender = getattr(raw, "from_id", None)
    from_id = peer_id_of(sender) if sender is not None else None
    message = getattr(raw, "message", None)
    date = getattr(raw, "date", None)
    return OwnedGift(
        ref=_settings.gift_ref_text(raw),
        kind="collectible" if unique is not None else "gift",
        gift_id=int(getattr(gift, "id", 0) or 0) or None,
        slug=unique.slug if unique is not None else None,
        title=str(getattr(gift, "title", "") or ""),
        num=getattr(raw, "gift_num", None) or (unique.num if unique is not None else None),
        from_id=from_id,
        from_peer=_settings.peer_model(known.get(abs(from_id)) if from_id else None),
        name_hidden=bool(getattr(raw, "name_hidden", False)),
        message=getattr(message, "text", None),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        msg_id=getattr(raw, "msg_id", None),
        saved_id=getattr(raw, "saved_id", None),
        pinned=bool(getattr(raw, "pinned_to_top", False)),
        displayed=not bool(getattr(raw, "unsaved", False)),
        refunded=bool(getattr(raw, "refunded", False)),
        can_upgrade=bool(getattr(raw, "can_upgrade", False)),
        convert_stars=getattr(raw, "convert_stars", None),
        upgrade_stars=getattr(raw, "upgrade_stars", None),
        transfer_stars=getattr(raw, "transfer_stars", None),
        can_export_at=fmt_unix(getattr(raw, "can_export_at", None) or 0) or None,
        can_transfer_at=fmt_unix(getattr(raw, "can_transfer_at", None) or 0) or None,
        can_resell_at=fmt_unix(getattr(raw, "can_resell_at", None) or 0) or None,
        can_craft_at=fmt_unix(getattr(raw, "can_craft_at", None) or 0) or None,
        collection_ids=[int(v) for v in getattr(raw, "collection_id", None) or []],
        hosted=unique.hosted if unique is not None else False,
        attributes=unique.attributes if unique is not None else [],
        unique=unique,
        resell_stars=unique.resell_stars if unique is not None else None,
        resell_ton=unique.resell_ton if unique is not None else None,
    )


async def _peer_or_self(ctx: OpContext, ref: PeerRef | None) -> Any:
    from telethon.tl import types

    if ref is None:
        return types.InputPeerSelf()
    return await _settings.resolve(ctx, ref)


# ---------------------------------------------------------------------------
# gift catalog
# ---------------------------------------------------------------------------


class CatalogReq(Request):
    until: Annotated[
        PeerRef | None,
        opt("--until", metavar="PEER", kind="peer", help="Annotate each gift with can_send."),
    ] = None
    limited: Annotated[bool, opt("--limited", help="Only limited-supply gifts.")] = False
    available: Annotated[bool, opt("--available", help="Hide sold-out gifts.")] = False
    refresh: Annotated[bool, opt("--refresh", help="Ignore the cached hash.")] = False


async def catalog(ctx: OpContext, req: CatalogReq) -> Page[StarGift]:
    """Browse the gifts on sale.

    Price discovery is free and automatic; *buying* one is not here, like
    every other purchase. `--until` would annotate each row with whether the
    named recipient accepts it, and needs `payments.canSendStarGift`, which
    Telethon 1.44 has no request class for.
    """
    from telethon.tl.functions import payments as fn

    if req.until is not None:
        _settings.method_gap("gift catalog --until", "payments.canSendStarGift")

    limit, state = window(ctx, "gift.catalog", PageKind.LOCAL, default=20)
    result = await client(ctx)(fn.GetStarGiftsRequest(hash=0))
    rows = [_catalog_gift(gift) for gift in getattr(result, "gifts", None) or []]
    if req.limited:
        rows = [row for row in rows if row.limited]
    if req.available:
        rows = [row for row in rows if not row.sold_out]
    offset = int(state.get("offset", 0) or 0)
    window_rows = rows[offset : offset + limit]
    return build_page(
        window_rows,
        op="gift.catalog",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window_rows)},
        account=ctx.account,
        has_more=offset + len(window_rows) < len(rows),
        total=len(rows),
    )


SPEC_CATALOG = OperationSpec(
    id="gift.catalog",
    request=CatalogReq,
    response=Page[StarGift],
    impl=catalog,
    summary="Browse the gifts on sale",
    description=(
        "Reading the catalogue is free; buying from it is absent, like every "
        "purchase in tlgr. `--until` needs `payments.canSendStarGift`, which "
        "this Telethon has no request class for, and refuses with exit 13."
    ),
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("gift_id", "title", "stars", "limited", "sold_out", "availability_remains"),
    headers=("Id", "Title", "Stars", "Limited", "Sold out", "Left"),
    example={
        "items": [{"gift_id": 5100, "title": "Plush Pepe", "stars": 500, "limited": True}],
        "has_more": False,
    },
    example_args="gift catalog --available",
    covers=("gift.catalog", "gifts.catalog"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# gift list / get
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="PEER", required=False, kind="peer", help="Whose profile; default me."),
    ] = None
    collection: Annotated[
        int | None, opt("--collection", metavar="ID", help="Only this collection.")
    ] = None
    sort: Annotated[str, opt("--sort", metavar="ORDER", help="date | value.")] = "date"
    exclude_unsaved: Annotated[
        bool, opt("--exclude-unsaved", help="Hide gifts not displayed on the profile.")
    ] = False
    exclude_unique: Annotated[bool, opt("--exclude-unique", help="Hide collectibles.")] = False
    exclude_hosted: Annotated[
        bool, opt("--exclude-hosted", help="Hide TON-hosted collectibles.")
    ] = False
    only_unique: Annotated[bool, opt("--only-unique", help="Only collectibles.")] = False


async def list_(ctx: OpContext, req: ListReq) -> Page[OwnedGift]:
    """Gifts a profile holds — mine, or somebody else's.

    Another profile's gifts are only visible when they chose to display them,
    so an empty list is not evidence that they have none.
    """
    from telethon.tl.functions import payments as fn

    limit, state = window(ctx, "gift.list", PageKind.RATE, default=20)
    peer = await _peer_or_self(ctx, req.chat)
    result = await client(ctx)(
        fn.GetSavedStarGiftsRequest(
            peer=peer,
            offset=str(state.get("offset", "") or ""),
            limit=limit,
            exclude_unsaved=req.exclude_unsaved or None,
            exclude_unique=req.exclude_unique or None,
            exclude_hosted=req.exclude_hosted or None,
            exclude_unlimited=None,
            sort_by_value=(req.sort == "value") or None,
            collection_id=req.collection,
        )
    )
    known = _settings.entity_map(result)
    rows = [_owned(row, known) for row in getattr(result, "gifts", None) or []]
    if req.only_unique:
        rows = [row for row in rows if row.kind == "collectible"]
    next_offset = str(getattr(result, "next_offset", "") or "")
    return build_page(
        rows,
        op="gift.list",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


SPEC_LIST = OperationSpec(
    id="gift.list",
    request=ListReq,
    response=Page[OwnedGift],
    impl=list_,
    summary="Gifts received by a profile (mine or someone else's)",
    description=(
        "`ref` is the handle every other gift command takes: `msg:<id>`, "
        "`<peer>:<saved_id>` or a collectible slug."
    ),
    paginated=PageKind.RATE,
    idempotent=True,
    columns=("ref", "kind", "title", "num", "from_id", "displayed", "pinned"),
    headers=("Ref", "Kind", "Title", "#", "From", "Shown", "Pinned"),
    example={
        "items": [{"ref": "msg:120", "kind": "gift", "title": "Plush Pepe", "convert_stars": 250}],
        "has_more": False,
    },
    example_args="gift list",
    covers=("gift.hosted", "gift.received-list"),
    tags=frozenset({"agent-safe"}),
)


class GetReq(Request):
    ref: Annotated[str, arg(0, metavar="REF", help="msg:<id>, <peer>:<saved_id>, or a slug.")]


async def get(ctx: OpContext, req: GetReq) -> OwnedGift:
    """One owned gift, with every time gate the server publishes.

    "Can I transfer this?" has three answers — yes, not yet (and here is
    when), never — so `can_transfer_at`, `can_resell_at`, `can_export_at`,
    `can_craft_at` and `can_upgrade` are all reported rather than collapsed
    into one boolean.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    stargift = await _settings.input_gift(ctx, req.ref)
    if type(stargift).__name__ == "InputSavedStarGiftSlug":
        result = await handle(fn.GetUniqueStarGiftRequest(slug=stargift.slug))
        known = _settings.entity_map(result)
        unique = _unique(getattr(result, "gift", None), known)
        return OwnedGift(
            ref=req.ref,
            kind="collectible",
            gift_id=unique.gift_id,
            slug=unique.slug,
            title=unique.title,
            num=unique.num,
            attributes=unique.attributes,
            unique=unique,
            hosted=unique.hosted,
            resell_stars=unique.resell_stars,
            resell_ton=unique.resell_ton,
        )
    result = await handle(fn.GetSavedStarGiftRequest(stargift=[stargift]))
    known = _settings.entity_map(result)
    gifts = getattr(result, "gifts", None) or []
    if not gifts:
        raise NotFoundError(f"no gift matches {req.ref!r}")
    return _owned(gifts[0], known)


SPEC_GET = OperationSpec(
    id="gift.get",
    request=GetReq,
    response=OwnedGift,
    impl=get,
    summary="Details of one owned gift, including every time gate",
    idempotent=True,
    columns=("ref", "kind", "title", "convert_stars", "can_transfer_at", "can_resell_at"),
    headers=("Ref", "Kind", "Title", "Convert", "Transfer at", "Resell at"),
    example={"ref": "msg:120", "kind": "gift", "title": "Plush Pepe", "convert_stars": 250},
    example_args="gift get msg:120",
    covers=("gift.get-one", "gifts.get-one"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# gift set
# ---------------------------------------------------------------------------


class SetReq(Request):
    ref: Annotated[tuple[str, ...], arg(0, metavar="REF", variadic=True, help="Gift references.")]
    peer: Annotated[
        PeerRef | None,
        opt("--peer", metavar="PEER", kind="peer", help="Profile the gift lives on."),
    ] = None
    save: Annotated[bool, opt("--save", help="Display it on the profile.")] = False
    unsave: Annotated[bool, opt("--unsave", help="Hide it from the profile.")] = False
    pin: Annotated[bool, opt("--pin", help="Pin it to the top of the profile.")] = False
    unpin: Annotated[bool, opt("--unpin", help="Unpin it.")] = False
    pin_order: Annotated[
        str | None,
        opt("--pin-order", metavar="LIST", help="Replace the whole pinned set, in this order."),
    ] = None
    wear: Annotated[bool, opt("--wear", help="Wear the collectible as my emoji status.")] = False
    wear_off: Annotated[bool, opt("--wear-off", help="Stop wearing it.")] = False
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", kind="datetime", help="Wear until this time.")
    ] = None


async def set_(ctx: OpContext, req: SetReq) -> GiftDisplay:
    """Show, hide, pin or wear a gift.

    Pinning is a *set* operation on the server — `toggleStarGiftsPinnedToTop`
    replaces the pinned collection — so `--pin` reads the current set and
    adds to it, and `--pin-order` is the raw replace for when you mean it.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    peer = await _peer_or_self(ctx, req.peer)
    result = GiftDisplay(ref=req.ref[0] if req.ref else "", refs=list(req.ref))

    if req.save or req.unsave:
        for ref in req.ref:
            await handle(
                fn.SaveStarGiftRequest(
                    stargift=await _settings.input_gift(ctx, ref), unsave=req.unsave or None
                )
            )
        result.displayed = not req.unsave

    if req.pin or req.unpin or req.pin_order is not None:
        wanted: list[str]
        if req.pin_order is not None:
            wanted = [part.strip() for part in req.pin_order.split(",") if part.strip()]
        else:
            page = await list_(ctx, ListReq(chat=req.peer))
            pinned = [row.ref for row in page.items if row.pinned]
            wanted = (
                [ref for ref in pinned if ref not in req.ref] + list(req.ref)
                if req.pin
                else [ref for ref in pinned if ref not in req.ref]
            )
        await handle(
            fn.ToggleStarGiftsPinnedToTopRequest(
                peer=peer,
                stargift=[await _settings.input_gift(ctx, ref) for ref in wanted],
            )
        )
        result.pinned = bool(req.pin)

    if req.wear or req.wear_off:
        if req.wear_off:
            await handle(afn.UpdateEmojiStatusRequest(emoji_status=types.EmojiStatusEmpty()))
            result.worn = False
        else:
            gift = await get(ctx, GetReq(ref=req.ref[0]))
            if gift.unique is None:
                raise UsageError("only a collectible can be worn as an emoji status", field="ref")
            until = parse_dt(req.until) if req.until else None
            await handle(
                afn.UpdateEmojiStatusRequest(
                    emoji_status=types.InputEmojiStatusCollectible(
                        collectible_id=gift.unique.id, until=until
                    )
                )
            )
            result.worn = True
            result.until = fmt_dt(until)

    if result.displayed is None and result.pinned is None and result.worn is None:
        raise UsageError(
            "give --save/--unsave, --pin/--unpin/--pin-order or --wear/--wear-off",
            field="save",
        )
    ctx.emit("gift_display", {"refs": list(req.ref)})
    return result


SPEC_SET = OperationSpec(
    id="gift.set",
    request=SetReq,
    response=GiftDisplay,
    impl=set_,
    summary="Profile display state of a gift: show/hide, pin, or wear it as my emoji status",
    aliases=(
        "gift.save",
        "gift.unsave",
        "gift.show",
        "gift.hide",
        "gift.pin",
        "gift.unpin",
        "gift.wear",
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("ref", "displayed", "pinned", "worn", "until"),
    headers=("Ref", "Shown", "Pinned", "Worn", "Until"),
    example={"ref": "msg:120", "displayed": True},
    example_args="gift set msg:120 --save",
    covers=(
        "gift.as-emoji-status",
        "gift.display-toggle",
        "gift.pin",
        "gifts.drop-original-details",
        "gifts.save-unsave",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# gift convert / upgrade / transfer / craft
# ---------------------------------------------------------------------------


class ConvertReq(Request):
    ref: Annotated[str, arg(0, metavar="REF", help="The gift to convert.")]


async def convert(ctx: OpContext, req: ConvertReq) -> GiftConverted:
    """Convert a received gift back into Stars.

    Destructive but free: the gift is destroyed and Stars are credited, which
    is why it is confirmed like a spend even though it earns.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    before = await get(ctx, GetReq(ref=req.ref))
    await handle(fn.ConvertStarGiftRequest(stargift=await _settings.input_gift(ctx, req.ref)))
    ctx.emit("gift_converted", {"ref": req.ref})
    balance = None
    try:
        from tlgr.ops.stars import BalanceGetReq, balance_get

        balance = (await balance_get(ctx, BalanceGetReq())).stars
    except Exception as exc:  # pragma: no cover - the balance is a nicety
        ctx.warn(f"could not read the Stars balance afterwards: {exc}")
    return GiftConverted(
        ref=req.ref, stars_received=before.convert_stars or 0, balance_after=balance
    )


SPEC_CONVERT = OperationSpec(
    id="gift.convert",
    request=ConvertReq,
    response=GiftConverted,
    impl=convert,
    summary="Convert a received gift back into Stars",
    description="Free, and irreversible: the gift is gone and cannot be un-converted.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("ref", "stars_received", "balance_after"),
    headers=("Ref", "Stars", "Balance"),
    example={"ref": "msg:120", "stars_received": 250, "balance_after": 500},
    example_args="gift convert msg:120",
    covers=("gift.convert-to-stars",),
)


class UpgradeReq(Request):
    ref: Annotated[str, arg(0, metavar="REF", help="The gift to upgrade.")]
    keep_original_details: Annotated[
        bool, opt("--keep-original-details", help="Keep the sender and message on it.")
    ] = False


async def upgrade(ctx: OpContext, req: UpgradeReq) -> GiftUpgraded:
    """Upgrade a gift into a collectible, on the free or prepaid path.

    When the upgrade is prepaid — `savedStarGift.upgrade_stars` is set, or
    the service message carried `prepaid_upgrade` — it is a plain method
    call and tlgr makes it. When it would need a payment form signed, tlgr
    prints the price and refuses, because signing forms is absent from the
    whole surface.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    before = await get(ctx, GetReq(ref=req.ref))
    if before.upgrade_stars is None and not before.can_upgrade:
        return GiftUpgraded(
            ref=req.ref,
            upgraded=False,
            price_stars=before.upgrade_stars,
            refused_reason=(
                "this gift's upgrade is not prepaid, so it needs a payment form. "
                + _settings.NO_SPEND
            ),
        )
    result = await handle(
        fn.UpgradeStarGiftRequest(
            stargift=await _settings.input_gift(ctx, req.ref),
            keep_original_details=req.keep_original_details or None,
        )
    )
    unique = _find_unique(result)
    ctx.emit("gift_upgraded", {"ref": req.ref})
    return GiftUpgraded(
        ref=req.ref,
        slug=unique.slug if unique is not None else None,
        num=unique.num if unique is not None else None,
        attributes=unique.attributes if unique is not None else [],
        upgraded=True,
        price_stars=before.upgrade_stars,
    )


def _find_unique(updates: Any) -> UniqueGift | None:
    """The `starGiftUnique` an `Updates` container carries, if any."""
    for update in getattr(updates, "updates", None) or []:
        for holder in (update, getattr(update, "message", None)):
            action = getattr(holder, "action", None)
            gift = getattr(action, "gift", None) or getattr(holder, "gift", None)
            if type(gift).__name__ == "StarGiftUnique":
                return _unique(gift)
    return None


SPEC_UPGRADE = OperationSpec(
    id="gift.upgrade",
    request=UpgradeReq,
    response=GiftUpgraded,
    impl=upgrade,
    summary="Upgrade a gift into a collectible (free/prepaid path only)",
    mutating=True,
    rate_class="send",
    columns=("ref", "slug", "num", "upgraded", "price_stars", "refused_reason"),
    headers=("Ref", "Slug", "#", "Upgraded", "Price", "Why not"),
    example={"ref": "msg:120", "slug": "PlushPepe-42", "num": 42, "upgraded": True},
    example_args="gift upgrade msg:120",
    covers=("gift.upgrade", "gifts.upgrade", "gifts.upgrade-preview"),
)


class TransferReq(Request):
    ref: Annotated[str, arg(0, metavar="REF", help="The collectible to transfer.")]
    chat: Annotated[PeerRef, arg(1, metavar="PEER", kind="peer", help="Who to transfer it to.")]


async def transfer(ctx: OpContext, req: TransferReq) -> GiftTransferred:
    """Transfer a collectible to another peer, when the transfer is free.

    A paid transfer goes through `inputInvoiceStarGiftTransfer` and a payment
    form; tlgr prints the price and refuses that path, as it does everywhere
    else.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    before = await get(ctx, GetReq(ref=req.ref))
    to_peer = await _settings.resolve(ctx, req.chat)
    if before.transfer_stars:
        return GiftTransferred(
            ref=req.ref,
            to=_settings.peer_of(to_peer),
            transferred=False,
            price_stars=before.transfer_stars,
            can_transfer_at=before.can_transfer_at,
            refused_reason="this transfer costs Stars. " + _settings.NO_SPEND,
        )
    await handle(
        fn.TransferStarGiftRequest(stargift=await _settings.input_gift(ctx, req.ref), to_id=to_peer)
    )
    ctx.emit("gift_transferred", {"ref": req.ref, "to": _settings.peer_of(to_peer)})
    return GiftTransferred(
        ref=req.ref,
        to=_settings.peer_of(to_peer),
        transferred=True,
        can_transfer_at=before.can_transfer_at,
    )


SPEC_TRANSFER = OperationSpec(
    id="gift.transfer",
    request=TransferReq,
    response=GiftTransferred,
    impl=transfer,
    summary="Transfer a collectible gift to another peer (free transfers only)",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("ref", "to", "transferred", "price_stars", "refused_reason"),
    headers=("Ref", "To", "Done", "Price", "Why not"),
    example={"ref": "PlushPepe-42", "to": 777123, "transferred": True},
    example_args="gift transfer PlushPepe-42 @alice",
    covers=("gift.transfer",),
    tags=frozenset({"visible-to-others"}),
)


class CraftReq(Request):
    ref: Annotated[
        tuple[str, ...],
        arg(0, metavar="REF", required=False, variadic=True, help="The gifts to melt down."),
    ] = ()
    candidates: Annotated[
        int | None,
        opt("--candidates", metavar="GIFT_ID", help="List my gifts usable to craft this one."),
    ] = None


async def craft(ctx: OpContext, req: CraftReq) -> GiftCrafted:
    """Craft (combine) collectible gifts.

    `payments.craftStarGift` burns **every** input gift regardless of the
    outcome, which is why this is destructive and confirmed even though it
    costs nothing: a failed craft is still four gifts gone.
    """
    from telethon.tl.functions import payments as fn

    if req.candidates is not None:
        _settings.method_gap("gift craft --candidates", "payments.getStarGiftCraftCandidates")
    if not req.ref:
        raise UsageError("give the gift references to melt down", field="ref")

    handle = client(ctx)
    inputs = [await _settings.input_gift(ctx, ref) for ref in req.ref]
    result = await handle(fn.CraftStarGiftRequest(stargift=inputs))
    unique = _find_unique(result)
    ctx.emit("gift_crafted", {"burned": list(req.ref)})
    return GiftCrafted(
        ref=f"slug:{unique.slug}" if unique is not None else None,
        slug=unique.slug if unique is not None else None,
        burned=list(req.ref),
        crafted=True,
    )


SPEC_CRAFT = OperationSpec(
    id="gift.craft",
    request=CraftReq,
    response=GiftCrafted,
    impl=craft,
    summary="Craft (combine) collectible gifts",
    description=(
        "Every input gift is burned whatever the outcome, so `--yes` is "
        "required and `--dry-run` prints exactly what would be consumed."
    ),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("slug", "burned", "crafted"),
    headers=("Result", "Burned", "Crafted"),
    example={"slug": "PlushPepe-77", "burned": ["msg:120", "msg:121"], "crafted": True},
    example_args="gift craft msg:120 msg:121",
    covers=("gift.craft", "gifts.craft"),
    covers_partial=("gift.craft-candidates",),
    coverage_note=(
        "Listing the eligible ingredients needs `payments.getStarGiftCraftCandidates`, "
        "which Telethon 1.44 has no request class for; the flag refuses with exit 13."
    ),
)


# ---------------------------------------------------------------------------
# gift resale list / set, offer approve
# ---------------------------------------------------------------------------


class ResaleListReq(Request):
    gift_id: Annotated[int, arg(0, metavar="GIFT_ID", help="The gift type to browse.")]
    sort: Annotated[str, opt("--sort", metavar="ORDER", help="price | num | date.")] = "price"
    ton_only: Annotated[bool, opt("--ton-only", help="Only TON listings.")] = False
    stars_only: Annotated[bool, opt("--stars-only", help="Only Stars listings.")] = False
    attr: Annotated[
        tuple[str, ...],
        opt("--attr", metavar="KIND=ID", help="Filter by model/pattern/backdrop."),
    ] = ()


async def resale_list(ctx: OpContext, req: ResaleListReq) -> Page[ResaleGift]:
    """Browse the collectible marketplace for one gift.

    Reading prices is free; buying from the marketplace is a purchase and is
    absent, so this is where a price check ends.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    limit, state = window(ctx, "gift.resale.list", PageKind.RATE, default=20)
    attributes = []
    for entry in req.attr:
        kind, _, value = entry.partition("=")
        if not value.isdigit():
            raise UsageError("--attr takes model=<id>, pattern=<id> or backdrop=<id>", field="attr")
        builder = {
            "model": types.StarGiftAttributeIdModel,
            "pattern": types.StarGiftAttributeIdPattern,
            "backdrop": types.StarGiftAttributeIdBackdrop,
        }.get(kind.strip().lower())
        if builder is None:
            raise UsageError("--attr takes model=, pattern= or backdrop=", field="attr")
        attributes.append(
            builder(backdrop_id=int(value))
            if kind.strip().lower() == "backdrop"
            else builder(document_id=int(value))
        )

    result = await client(ctx)(
        fn.GetResaleStarGiftsRequest(
            gift_id=int(req.gift_id),
            offset=str(state.get("offset", "") or ""),
            limit=limit,
            sort_by_price=(req.sort == "price") or None,
            sort_by_num=(req.sort == "num") or None,
            stars_only=req.stars_only or None,
            attributes=attributes or None,
        )
    )
    known = _settings.entity_map(result)
    rows = []
    for gift in getattr(result, "gifts", None) or []:
        unique = _unique(gift, known)
        if req.ton_only and unique.resell_ton is None:
            continue
        rows.append(
            ResaleGift(
                slug=unique.slug,
                num=unique.num,
                price_stars=unique.resell_stars,
                price_ton=unique.resell_ton,
                seller_id=unique.owner_id,
                attributes=unique.attributes,
            )
        )
    next_offset = str(getattr(result, "next_offset", "") or "")
    return build_page(
        rows,
        op="gift.resale.list",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


SPEC_RESALE_LIST = OperationSpec(
    id="gift.resale.list",
    request=ResaleListReq,
    response=Page[ResaleGift],
    impl=resale_list,
    summary="Browse the collectible marketplace for one gift",
    paginated=PageKind.RATE,
    idempotent=True,
    columns=("slug", "num", "price_stars", "price_ton", "seller_id"),
    headers=("Slug", "#", "Stars", "TON", "Seller"),
    example={
        "items": [{"slug": "PlushPepe-42", "num": 42, "price_stars": 12000}],
        "has_more": False,
    },
    example_args="gift resale list 5100",
    covers=("gift.resale-browse", "gifts.resale-price"),
    tags=frozenset({"agent-safe"}),
)


class ResaleSetReq(Request):
    ref: Annotated[str, arg(0, metavar="REF", help="My collectible.")]
    stars: Annotated[int | None, opt("--stars", metavar="N", help="Asking price in Stars.")] = None
    ton: Annotated[int | None, opt("--ton", metavar="NANO", help="Asking price in nanotons.")] = (
        None
    )
    unlist: Annotated[bool, opt("--unlist", help="Take it off the market.")] = False


async def resale_set(ctx: OpContext, req: ResaleSetReq) -> GiftListing:
    """Put one of my collectibles up for sale, or take it off the market.

    Listing is free — it earns rather than spends — and the sale itself
    happens when a buyer pays, which is somebody else's payment form and not
    tlgr's.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    if req.unlist:
        amount: Any = types.StarsAmount(amount=0, nanos=0)
    elif req.ton is not None:
        amount = types.StarsTonAmount(amount=int(req.ton))
    elif req.stars is not None:
        amount = types.StarsAmount(amount=int(req.stars), nanos=0)
    else:
        raise UsageError("give --stars, --ton, or --unlist", field="stars")

    before = await get(ctx, GetReq(ref=req.ref))
    await handle(
        fn.UpdateStarGiftPriceRequest(
            stargift=await _settings.input_gift(ctx, req.ref), resell_amount=amount
        )
    )
    ctx.emit("gift_listed", {"ref": req.ref, "unlist": req.unlist})
    return GiftListing(
        ref=req.ref,
        listed=not req.unlist,
        price_stars=None if req.unlist else req.stars,
        price_ton=None if req.unlist else req.ton,
        can_resell_at=before.can_resell_at,
    )


SPEC_RESALE_SET = OperationSpec(
    id="gift.resale.set",
    request=ResaleSetReq,
    response=GiftListing,
    impl=resale_set,
    summary="Put one of my collectibles up for sale, or take it off the market",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("ref", "listed", "price_stars", "price_ton", "can_resell_at"),
    headers=("Ref", "Listed", "Stars", "TON", "Sellable at"),
    example={"ref": "PlushPepe-42", "listed": True, "price_stars": 12000},
    example_args="gift resale set PlushPepe-42 --stars 12000",
    covers=("gift.resale-list-mine", "gifts.resale-buy"),
    tags=frozenset({"visible-to-others"}),
)


class OfferApproveReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where the offer arrived.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="The offer message.")]
    deny: Annotated[bool, opt("--deny", help="Decline the offer (always free).")] = False


async def offer_approve(ctx: OpContext, req: OfferApproveReq) -> GiftOfferResolved:
    """Decline somebody's offer to buy my collectible — or report the price.

    Declining is free and is performed. Accepting sells the asset for Stars:
    that is a financial transfer, and tlgr reports the offer instead of
    completing it, in the same way it never signs a payment form.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    await _settings.resolve(ctx, req.chat)
    if not req.deny:
        return GiftOfferResolved(
            msg_id=int(req.msg_id),
            state="refused",
            reason="accepting an offer sells the collectible for Stars. " + _settings.NO_SPEND,
        )
    await handle(fn.ResolveStarGiftOfferRequest(offer_msg_id=int(req.msg_id), decline=True))
    ctx.emit("gift_offer", {"msg_id": int(req.msg_id), "declined": True})
    return GiftOfferResolved(msg_id=int(req.msg_id), state="declined")


SPEC_OFFER_APPROVE = OperationSpec(
    id="gift.offer.approve",
    request=OfferApproveReq,
    response=GiftOfferResolved,
    impl=offer_approve,
    summary="Decline an offer to buy my collectible (accepting sells an asset and is refused)",
    aliases=("gift.offer.deny", "gift.offer.decline"),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("msg_id", "state", "price_stars", "reason"),
    headers=("Message", "State", "Stars", "Why"),
    example={"msg_id": 512, "state": "declined"},
    example_args="gift offer approve @alice 512 --deny",
    covers_partial=("gift.offer-resolve", "gifts.purchase-offer"),
    coverage_note=(
        "Declining is performed; accepting transfers an asset for money and is "
        "reported rather than done, like every other value transfer in tlgr."
    ),
)


# ---------------------------------------------------------------------------
# gift unique get / variant list
# ---------------------------------------------------------------------------


class UniqueGetReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="A slug or a t.me/nft/ link.")]
    value: Annotated[bool, opt("--value", help="Include the valuation the gift carries.")] = False


async def unique_get(ctx: OpContext, req: UniqueGetReq) -> UniqueGift:
    """Look up a collectible by link or slug.

    The full valuation breakdown — floor price, last sale — needs
    `payments.getStarGiftValueInfo`, which this Telethon has no request class
    for; the gift itself carries `value_amount`/`value_currency`, and that is
    what `--value` reports, with a warning saying what is missing.
    """
    from telethon.tl.functions import payments as fn

    result = await client(ctx)(fn.GetUniqueStarGiftRequest(slug=_settings.slug_of(req.slug)))
    known = _settings.entity_map(result)
    gift = _unique(getattr(result, "gift", None), known)
    if req.value:
        ctx.warn(
            "the floor price and last sale need payments.getStarGiftValueInfo, which "
            "Telethon 1.44 has no request class for; the gift's own valuation is reported"
        )
    return gift


SPEC_UNIQUE_GET = OperationSpec(
    id="gift.unique.get",
    request=UniqueGetReq,
    response=UniqueGift,
    impl=unique_get,
    summary="Look up a collectible by link or slug, with the valuation it carries",
    idempotent=True,
    columns=("slug", "num", "title", "owner_id", "value_stars", "resell_stars"),
    headers=("Slug", "#", "Title", "Owner", "Value", "For sale"),
    example={"slug": "PlushPepe-42", "num": 42, "title": "Plush Pepe", "value_stars": 15000},
    example_args="gift unique get PlushPepe-42",
    covers=("gift.unique-info", "gifts.unique-info"),
    covers_partial=("gift.unique-value",),
    coverage_note=(
        "The gift's own `value_amount`/`value_currency` are reported; the floor "
        "price and last sale need `payments.getStarGiftValueInfo`, absent from "
        "Telethon 1.44."
    ),
    tags=frozenset({"agent-safe"}),
)


class VariantListReq(Request):
    gift_id: Annotated[int, arg(0, metavar="GIFT_ID", help="The gift type.")]
    preview: Annotated[
        bool, opt("--preview", help="Sample attributes an upgrade could produce.")
    ] = False
    craft_only: Annotated[
        bool, opt("--craft-only", help="Only variants reachable by crafting.")
    ] = False


async def variant_list(ctx: OpContext, req: VariantListReq) -> Page[GiftVariant]:
    """Possible collectible variants of a gift, and their rarities.

    `payments.getStarGiftUpgradePreview` is what this build can send; the
    full attribute table (`payments.getStarGiftAttributes`) has no request
    class here, so `--craft-only`, which only that method can answer,
    refuses rather than returning a filtered guess.
    """
    from telethon.tl.functions import payments as fn

    if req.craft_only:
        _settings.method_gap("gift variant list --craft-only", "payments.getStarGiftAttributes")

    limit, state = window(ctx, "gift.variant.list", PageKind.LOCAL, default=20)
    result = await client(ctx)(fn.GetStarGiftUpgradePreviewRequest(gift_id=int(req.gift_id)))
    rows = [
        GiftVariant(
            kind=attribute.kind,
            name=attribute.name,
            document_id=attribute.document_id,
            rarity_permille=attribute.rarity_permille,
            sample=True,
        )
        for attribute in (
            _attribute(raw) for raw in getattr(result, "sample_attributes", None) or []
        )
    ]
    offset = int(state.get("offset", 0) or 0)
    page_rows = rows[offset : offset + limit]
    return build_page(
        page_rows,
        op="gift.variant.list",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(page_rows)},
        account=ctx.account,
        has_more=offset + len(page_rows) < len(rows),
        total=len(rows),
    )


SPEC_VARIANT_LIST = OperationSpec(
    id="gift.variant.list",
    request=VariantListReq,
    response=Page[GiftVariant],
    impl=variant_list,
    summary="Possible collectible variants of a gift, and a preview of an upgrade",
    aliases=("gift.variants",),
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("kind", "name", "rarity_permille", "document_id", "sample"),
    headers=("Kind", "Name", "Rarity ‰", "Document", "Sample"),
    example={
        "items": [{"kind": "model", "name": "Golden", "rarity_permille": 5, "sample": True}],
        "has_more": False,
    },
    example_args="gift variant list 5100 --preview",
    covers=("gift.upgrade-preview",),
    covers_partial=("gift.upgrade-attributes",),
    coverage_note=(
        "The upgrade preview is the sample the server offers; the exhaustive "
        "attribute table needs `payments.getStarGiftAttributes`, absent from "
        "Telethon 1.44."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# gift collection list / create / edit / delete
# ---------------------------------------------------------------------------


def _collection(raw: Any, order: int) -> GiftCollection:
    return GiftCollection(
        id=int(getattr(raw, "collection_id", 0) or 0),
        title=str(getattr(raw, "title", "") or ""),
        count=int(getattr(raw, "gifts_count", 0) or 0),
        icon_document_id=getattr(getattr(raw, "icon", None), "id", None),
        order=order,
    )


class CollectionListReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="PEER", required=False, kind="peer", help="Whose profile; default me."),
    ] = None
    refresh: Annotated[bool, opt("--refresh", help="Ignore the cached hash.")] = False


async def collection_list(ctx: OpContext, req: CollectionListReq) -> Page[GiftCollection]:
    """Gift collections on a profile, in display order."""
    from telethon.tl.functions import payments as fn

    peer = await _peer_or_self(ctx, req.chat)
    result = await client(ctx)(fn.GetStarGiftCollectionsRequest(peer=peer, hash=0))
    rows = [
        _collection(raw, index)
        for index, raw in enumerate(getattr(result, "collections", None) or [])
    ]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_COLLECTION_LIST = OperationSpec(
    id="gift.collection.list",
    request=CollectionListReq,
    response=Page[GiftCollection],
    impl=collection_list,
    summary="Gift collections on a profile",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("id", "title", "count", "order"),
    headers=("Id", "Title", "Gifts", "Order"),
    example={"items": [{"id": 1, "title": "Favourites", "count": 4}], "has_more": False},
    example_args="gift collection list",
    covers=("gift.collections-list", "gifts.collections"),
    tags=frozenset({"agent-safe"}),
)


class CollectionCreateReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="PEER", kind="peer", help="Whose profile.")]
    title: Annotated[str, arg(1, metavar="TITLE", help="The collection's name.")]
    refs: Annotated[
        tuple[str, ...],
        arg(2, metavar="REF", required=False, variadic=True, help="Gifts to put in it."),
    ] = ()


async def collection_create(ctx: OpContext, req: CollectionCreateReq) -> GiftCollection:
    """Create a gift collection. `stargifts_collections_max` bounds how many."""
    from telethon.tl.functions import payments as fn

    peer = await _settings.resolve(ctx, req.chat)
    result = await client(ctx)(
        fn.CreateStarGiftCollectionRequest(
            peer=peer,
            title=req.title,
            stargift=[await _settings.input_gift(ctx, ref) for ref in req.refs],
        )
    )
    ctx.emit("gift_collection", {"title": req.title})
    return _collection(result, 0)


SPEC_COLLECTION_CREATE = OperationSpec(
    id="gift.collection.create",
    request=CollectionCreateReq,
    response=GiftCollection,
    impl=collection_create,
    summary="Create a gift collection",
    mutating=True,
    rate_class="send",
    columns=("id", "title", "count"),
    headers=("Id", "Title", "Gifts"),
    example={"id": 1, "title": "Favourites", "count": 2},
    example_args="gift collection create me Favourites msg:120 msg:121",
    covers=("gift.collection-create",),
)


class CollectionEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="PEER", kind="peer", help="Whose profile.")]
    id: Annotated[
        int | None,
        arg(1, metavar="ID", required=False, help="Collection id; omit with --order-collections."),
    ] = None
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New title.")] = None
    add: Annotated[str | None, opt("--add", metavar="LIST", help="Gifts to add.")] = None
    remove: Annotated[str | None, opt("--remove", metavar="LIST", help="Gifts to remove.")] = None
    order: Annotated[
        str | None, opt("--order", metavar="LIST", help="New order of the gifts inside it.")
    ] = None
    order_collections: Annotated[
        str | None,
        opt("--order-collections", metavar="LIST", help="New order of the collections."),
    ] = None


async def collection_edit(ctx: OpContext, req: CollectionEditReq) -> GiftCollection:
    """Rename a collection, add or remove gifts, or reorder either level.

    Removing a gift from a collection never deletes the gift — the two are
    different operations and only `gift convert` destroys anything.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    peer = await _settings.resolve(ctx, req.chat)

    if req.order_collections is not None:
        order = [int(p) for p in req.order_collections.split(",") if p.strip().isdigit()]
        if not order:
            raise UsageError("--order-collections wants every collection id", field="order")
        await handle(fn.ReorderStarGiftCollectionsRequest(peer=peer, order=order))
        return GiftCollection(id=order[0], title="", count=0, order=0)

    if req.id is None:
        raise UsageError("give a collection id, or --order-collections", field="id")

    async def refs(value: str | None) -> list[Any] | None:
        if value is None:
            return None
        return [
            await _settings.input_gift(ctx, part.strip())
            for part in value.split(",")
            if part.strip()
        ]

    result = await handle(
        fn.UpdateStarGiftCollectionRequest(
            peer=peer,
            collection_id=int(req.id),
            title=req.title,
            add_stargift=await refs(req.add),
            delete_stargift=await refs(req.remove),
            order=await refs(req.order),
        )
    )
    ctx.emit("gift_collection_edit", {"id": int(req.id)})
    return _collection(result, 0)


SPEC_COLLECTION_EDIT = OperationSpec(
    id="gift.collection.edit",
    request=CollectionEditReq,
    response=GiftCollection,
    impl=collection_edit,
    summary="Rename a collection, add or remove gifts, or reorder either level",
    mutating=True,
    rate_class="send",
    columns=("id", "title", "count", "order"),
    headers=("Id", "Title", "Gifts", "Order"),
    example={"id": 1, "title": "Favourites", "count": 3},
    example_args="gift collection edit me 1 --add msg:122",
    covers=("gift.collection-reorder", "gift.collection-update"),
)


class CollectionDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="PEER", kind="peer", help="Whose profile.")]
    id: Annotated[int, arg(1, metavar="ID", help="The collection to delete.")]


async def collection_delete(ctx: OpContext, req: CollectionDeleteReq) -> GiftCollection:
    """Delete a gift collection. The gifts themselves are untouched."""
    from telethon.tl.functions import payments as fn

    peer = await _settings.resolve(ctx, req.chat)
    await client(ctx)(fn.DeleteStarGiftCollectionRequest(peer=peer, collection_id=int(req.id)))
    ctx.emit("gift_collection_deleted", {"id": int(req.id)})
    return GiftCollection(id=int(req.id), title="", count=0)


SPEC_COLLECTION_DELETE = OperationSpec(
    id="gift.collection.delete",
    request=CollectionDeleteReq,
    response=GiftCollection,
    impl=collection_delete,
    summary="Delete a gift collection (the gifts stay)",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("id", "title"),
    headers=("Id", "Title"),
    example={"id": 1, "title": ""},
    example_args="gift collection delete me 1",
    covers=("gift.collection-delete",),
)


# ---------------------------------------------------------------------------
# gift auction list / get
# ---------------------------------------------------------------------------


class AuctionListReq(Request):
    won: Annotated[bool, opt("--won", help="Gifts I acquired in auctions.")] = False
    gift_id: Annotated[
        int | None, opt("--gift-id", metavar="ID", help="With --won: only this gift type.")
    ] = None


async def auction_list(ctx: OpContext, req: AuctionListReq) -> Page[GiftAuction]:
    """Gift auctions I am bidding in, or gifts I won in one.

    Read-only, and not because of an API gap: a bid cannot be retracted, so
    tlgr never places one.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    if req.won:
        if req.gift_id is None:
            raise UsageError("--won needs --gift-id <id>", field="gift_id")
        result = await handle(fn.GetStarGiftAuctionAcquiredGiftsRequest(gift_id=int(req.gift_id)))
        known = _settings.entity_map(result)
        rows = [
            GiftAuction(
                auction=str(getattr(gift, "slug", "") or ""),
                gift_id=int(req.gift_id),
                slug=_unique(gift, known).slug,
                state="won",
            )
            for gift in getattr(result, "gifts", None) or []
        ]
        return Page(items=rows, has_more=False, total=len(rows))

    result = await handle(fn.GetStarGiftActiveAuctionsRequest(hash=0))
    rows = []
    for raw in getattr(result, "auctions", None) or []:
        gift = getattr(raw, "gift", None)
        my_bid, _ = _settings.stars_of(getattr(raw, "my_bid", None))
        min_bid, _ = _settings.stars_of(getattr(raw, "min_bid_amount", None))
        rows.append(
            GiftAuction(
                auction=str(getattr(gift, "slug", "") or getattr(raw, "gift_id", "") or ""),
                gift_id=getattr(raw, "gift_id", None) or getattr(gift, "gift_id", None),
                slug=getattr(gift, "slug", None),
                my_bid=my_bid or None,
                min_bid=min_bid or None,
                ends_at=fmt_dt(getattr(raw, "end_date", None)),
                state=type(raw).__name__.removeprefix("StarGiftAuction").lower() or "active",
            )
        )
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_AUCTION_LIST = OperationSpec(
    id="gift.auction.list",
    request=AuctionListReq,
    response=Page[GiftAuction],
    impl=auction_list,
    summary="Gift auctions: the ones I am bidding in, and the gifts I won",
    description="Read-only on purpose: a bid cannot be retracted, so tlgr never places one.",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("auction", "gift_id", "slug", "my_bid", "min_bid", "ends_at", "state"),
    headers=("Auction", "Gift", "Slug", "My bid", "Min bid", "Ends", "State"),
    example={
        "items": [{"auction": "PlushPepe-42", "my_bid": 5000, "min_bid": 5500}],
        "has_more": False,
    },
    example_args="gift auction list",
    covers=("auction.acquired-gifts", "auction.active-list", "gifts.auctions"),
    tags=frozenset({"agent-safe"}),
)


class AuctionGetReq(Request):
    auction: Annotated[str, arg(0, metavar="AUCTION", help="A gift id or a collectible slug.")]
    with_position: Annotated[
        bool, opt("--with-position", help="Estimate my position in the ladder.")
    ] = False
    watch: Annotated[
        bool, opt("--watch", help="Keep the subscription alive and stream updates.")
    ] = False
    version: Annotated[int, opt("--version", metavar="N", help="Last seen state version.")] = 0


async def auction_get(ctx: OpContext, req: AuctionGetReq) -> Any:
    """Auction state, bid ladder and my position — optionally as a stream.

    `getStarGiftAuctionState` doubles as an update subscription that lasts
    `timeout` seconds, so `--watch` re-invokes it to stay subscribed. A new
    state is applied only when `version` increases, and a finished state
    always wins — otherwise a slow reply can overwrite a newer one.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    text = req.auction.strip()
    auction = (
        types.InputStarGiftAuction(gift_id=int(text))
        if text.isdigit()
        else types.InputStarGiftAuctionSlug(slug=_settings.slug_of(text))
    )
    version = int(req.version)
    while True:
        raw = await handle(fn.GetStarGiftAuctionStateRequest(auction=auction, version=version))
        state = _auction_state(text, raw)
        if state.version >= version:
            version = state.version
            yield state
        if not req.watch or state.finished:
            return


def _auction_state(name: str, raw: Any) -> GiftAuctionState:
    my_bid, _ = _settings.stars_of(getattr(raw, "my_bid", None))
    min_bid, _ = _settings.stars_of(getattr(raw, "min_bid_amount", None))
    kind = type(raw).__name__.removeprefix("StarGiftAuctionState").lower()
    return GiftAuctionState(
        auction=name,
        state=kind or "active",
        version=int(getattr(raw, "version", 0) or 0),
        min_bid_amount=min_bid or None,
        my_bid=my_bid or None,
        position=getattr(raw, "position", None),
        ends_at=fmt_dt(getattr(raw, "end_date", None)),
        timeout=getattr(raw, "timeout", None),
        finished=kind == "finished",
    )


SPEC_AUCTION_GET = OperationSpec(
    id="gift.auction.get",
    request=AuctionGetReq,
    response=GiftAuctionState,
    impl=auction_get,
    summary="Auction state, bid ladder and my position",
    stream=True,
    idempotent=True,
    columns=("auction", "state", "version", "min_bid_amount", "my_bid", "position", "ends_at"),
    headers=("Auction", "State", "Version", "Min bid", "My bid", "Place", "Ends"),
    example={"auction": "PlushPepe-42", "state": "active", "version": 3, "min_bid_amount": 5500},
    example_args="gift auction get PlushPepe-42",
    covers=("auction.position-estimate", "auction.state"),
    tags=frozenset({"agent-safe"}),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
