"""The `stars` group: the balance, the ledger, subscriptions and revenue.

Everything here reads. Acquiring Stars, moving them and withdrawing them are
financial transfers, and tlgr performs none of them — `stars url get` prints
the Fragment URL and leaves the transfer to a human in a browser, which is
what "control-only" means in the catalog and what the whole group is shaped
around.

Two details are load-bearing.

* **A Stars amount is `(amount, nanos)`.** TON arrives in the same shape with
  nine decimals, and both halves are reported. A ledger that rounds is a
  ledger that cannot be reconciled.
* **The transactions cursor is an opaque string**, not an integer offset.
  `next_offset` comes back from the server and goes back to it unchanged;
  tlgr signs it into a normal `--cursor` token so it cannot be spliced onto
  another account or another op.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.payment import StarSubscription
from tlgr.models.peer import PeerRef
from tlgr.models.stars import (
    StarsBalance,
    StarsRating,
    StarsRefulfill,
    StarsRevenue,
    StarsTransaction,
    StarsUrl,
)
from tlgr.ops import _settings
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_PASSWORD = opt(
    secret=True, envvar="TLGR_2FA_PASSWORD", help="The 2FA cloud password (never in argv)."
)

#: `starsTransactionPeer*` → the one word that says who the other side was.
PEER_KINDS = {
    "StarsTransactionPeer": "peer",
    "StarsTransactionPeerAppStore": "app-store",
    "StarsTransactionPeerPlayMarket": "play-market",
    "StarsTransactionPeerPremiumBot": "premium-bot",
    "StarsTransactionPeerFragment": "fragment",
    "StarsTransactionPeerAds": "ads",
    "StarsTransactionPeerAPI": "api",
    "StarsTransactionPeerUnsupported": "unsupported",
}

#: The boolean flags a transaction carries, in the order a reader wants them.
KINDS = (
    "gift",
    "reaction",
    "stargift_upgrade",
    "stargift_resale",
    "stargift_auction_bid",
    "business_transfer",
    "posts_search",
    "offer",
)


# ---------------------------------------------------------------------------
# stars balance get
# ---------------------------------------------------------------------------


class BalanceGetReq(Request):
    ton: Annotated[bool, opt("--ton", help="The TON balance instead (amounts are nanotons).")] = (
        False
    )


async def balance_get(ctx: OpContext, req: BalanceGetReq) -> StarsBalance:
    """My Telegram Stars balance, or my TON balance.

    Read-only on purpose: topping up and withdrawing happen on Fragment or in
    an official app, and a CLI that could do either would be a CLI that could
    lose money by accident.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    result = await client(ctx)(
        fn.GetStarsStatusRequest(peer=types.InputPeerSelf(), ton=req.ton or None)
    )
    amount, nanos = _settings.stars_of(getattr(result, "balance", None))
    missing = [
        row
        for row in getattr(result, "subscriptions", None) or []
        if getattr(row, "missing_balance", False)
    ]
    return StarsBalance(
        stars=0 if req.ton else amount,
        nanos=nanos,
        ton=amount if req.ton else None,
        currency="TON" if req.ton else "XTR",
        subscriptions_missing_balance=len(missing) or None,
    )


SPEC_BALANCE_GET = OperationSpec(
    id="stars.balance.get",
    request=BalanceGetReq,
    response=StarsBalance,
    impl=balance_get,
    summary="My Telegram Stars balance (and the TON balance with --ton)",
    description=(
        "`nanos` is the fractional part the wire carries; TON amounts are "
        "nanotons. Neither is rounded, because a rounded ledger cannot be "
        "reconciled."
    ),
    idempotent=True,
    columns=("stars", "nanos", "ton", "subscriptions_missing_balance"),
    headers=("Stars", "Nanos", "TON", "Lapsing subs"),
    example={"stars": 250, "nanos": 0, "currency": "XTR"},
    example_args="stars balance get",
    covers=(
        "bots.bot-stars-balance",
        "bots.stars-topup-deeplink",
        "bots.stars-topup-options",
        "stars.balance",
        "stars.topup-options",
    ),
    covers_partial=("stars.ton-balance",),
    coverage_note="The TON ledger itself is `stars transaction list --ton`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# stars transaction list
# ---------------------------------------------------------------------------


class TransactionListReq(Request):
    inbound: Annotated[bool, opt("--in", help="Incoming only.")] = False
    outbound: Annotated[bool, opt("--out", help="Outgoing only.")] = False
    ton: Annotated[bool, opt("--ton", help="The TON ledger instead of the Stars one.")] = False
    peer: Annotated[
        PeerRef | None,
        opt("--peer", metavar="CHAT", kind="peer", help="Transactions with one bot or channel."),
    ] = None
    subscription: Annotated[
        str | None, opt("--subscription", metavar="ID", help="Only one subscription's charges.")
    ] = None
    ascending: Annotated[bool, opt("--ascending", help="Oldest first.")] = False
    id: Annotated[
        str | None, opt("--id", metavar="LIST", help="Fetch specific transactions by id.")
    ] = None


async def transaction_list(ctx: OpContext, req: TransactionListReq) -> Page[StarsTransaction]:
    """Star (or TON) transaction history.

    The cursor here is the server's opaque `next_offset` string rather than a
    number: passing an integer where the API wants a token silently restarts
    the walk from the beginning, which is how a ledger export ends up with
    the first page repeated.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    limit, state = window(ctx, "stars.transaction.list", PageKind.RATE, default=50)
    peer = await _settings.resolve(ctx, req.peer) if req.peer is not None else types.InputPeerSelf()

    if req.id:
        wanted = [
            types.InputStarsTransaction(id=part.strip())
            for part in req.id.split(",")
            if part.strip()
        ]
        result = await handle(
            fn.GetStarsTransactionsByIDRequest(peer=peer, id=wanted, ton=req.ton or None)
        )
        rows = [_transaction(row, result) for row in getattr(result, "history", None) or []]
        return Page(items=rows, has_more=False, total=len(rows))

    result = await handle(
        fn.GetStarsTransactionsRequest(
            peer=peer,
            offset=str(state.get("offset", "") or ""),
            limit=limit,
            inbound=req.inbound or None,
            outbound=req.outbound or None,
            ascending=req.ascending or None,
            ton=req.ton or None,
            subscription_id=req.subscription,
        )
    )
    rows = [_transaction(row, result) for row in getattr(result, "history", None) or []]
    next_offset = str(getattr(result, "next_offset", "") or "")
    return build_page(
        rows,
        op="stars.transaction.list",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
    )


def _transaction(raw: Any, envelope: Any) -> StarsTransaction:
    from tlgr.ops._serialize import peer_id_of

    amount, nanos = _settings.stars_of(getattr(raw, "amount", None))
    holder = getattr(raw, "peer", None)
    inner = getattr(holder, "peer", None)
    known = _settings.entity_map(envelope)
    peer_id = peer_id_of(inner) if inner is not None else None
    date = getattr(raw, "date", None)
    return StarsTransaction(
        id=str(getattr(raw, "id", "") or ""),
        date=fmt_dt(date),
        date_unix=to_unix(date),
        stars=amount,
        nanos=nanos,
        refund=bool(getattr(raw, "refund", False)),
        pending=bool(getattr(raw, "pending", False)),
        failed=bool(getattr(raw, "failed", False)),
        peer=peer_id,
        peer_kind=PEER_KINDS.get(type(holder).__name__, ""),
        peer_ref=_settings.peer_model(known.get(abs(peer_id)) if peer_id else None),
        title=getattr(raw, "title", None),
        description=getattr(raw, "description", None),
        msg_id=getattr(raw, "msg_id", None),
        subscription_period=getattr(raw, "subscription_period", None),
        transaction_url=getattr(raw, "transaction_url", None),
        kind=next((name for name in KINDS if getattr(raw, name, False)), ""),
    )


SPEC_TRANSACTION_LIST = OperationSpec(
    id="stars.transaction.list",
    request=TransactionListReq,
    response=Page[StarsTransaction],
    impl=transaction_list,
    summary="Star (or TON) transaction history",
    aliases=("stars.transactions",),
    paginated=PageKind.RATE,
    idempotent=True,
    columns=("id", "date", "stars", "peer", "title", "kind", "refund"),
    headers=("Id", "Date", "Stars", "Peer", "Title", "Kind", "Refund"),
    example={
        "items": [
            {"id": "tx1", "stars": -25, "peer": 5000001, "title": "Sticker pack", "kind": "gift"}
        ],
        "has_more": False,
    },
    example_args="stars transaction list --out",
    covers=("stars.ton-balance", "stars.transactions"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# stars subscription list / refulfill
# ---------------------------------------------------------------------------


class SubscriptionListReq(Request):
    missing_balance: Annotated[
        bool, opt("--missing-balance", help="Only the ones about to lapse for want of Stars.")
    ] = False


async def subscription_list(ctx: OpContext, req: SubscriptionListReq) -> Page[StarSubscription]:
    """My Star subscriptions.

    `can_refulfill` means the *server* would allow a re-join; tlgr still
    refuses to make the charge, which is what `stars subscription refulfill`
    reports.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    from tlgr.ops._serialize import peer_id_of

    _, state = window(ctx, "stars.subscription.list", PageKind.RATE, default=50)
    result = await client(ctx)(
        fn.GetStarsSubscriptionsRequest(
            peer=types.InputPeerSelf(),
            offset=str(state.get("offset", "") or ""),
            missing_balance=req.missing_balance or None,
        )
    )
    rows = []
    for raw in getattr(result, "subscriptions", None) or []:
        pricing = getattr(raw, "pricing", None)
        until = getattr(raw, "until_date", None)
        rows.append(
            StarSubscription(
                id=str(getattr(raw, "id", "") or ""),
                peer=peer_id_of(getattr(raw, "peer", None)),
                until_date=fmt_dt(until),
                until_date_unix=to_unix(until),
                pricing=(
                    {
                        "period": int(getattr(pricing, "period", 0) or 0),
                        "amount": int(getattr(pricing, "amount", 0) or 0),
                    }
                    if pricing is not None
                    else None
                ),
                cancelled=getattr(raw, "canceled", None),
                can_refulfill=getattr(raw, "can_refulfill", None),
                missing_balance=getattr(raw, "missing_balance", None),
                invoice_slug=getattr(raw, "invoice_slug", None),
                chat_invite_hash=getattr(raw, "chat_invite_hash", None),
                title=getattr(raw, "title", None),
            )
        )
    next_offset = str(getattr(result, "subscriptions_next_offset", "") or "")
    return build_page(
        rows,
        op="stars.subscription.list",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
    )


SPEC_SUBSCRIPTION_LIST = OperationSpec(
    id="stars.subscription.list",
    request=SubscriptionListReq,
    response=Page[StarSubscription],
    impl=subscription_list,
    summary="My Star subscriptions",
    paginated=PageKind.RATE,
    idempotent=True,
    columns=("id", "peer", "until_date", "cancelled", "missing_balance"),
    headers=("Id", "Peer", "Until", "Cancelled", "Lapsing"),
    example={
        "items": [{"id": "sub1", "peer": -1001600, "until_date": "2026-10-01T00:00:00Z"}],
        "has_more": False,
    },
    example_args="stars subscription list",
    covers=(
        "groups-channels-admin.channel-subscription-manage",
        "stars.subscriptions-list",
    ),
    tags=frozenset({"agent-safe"}),
)


class SubscriptionRefulfillReq(Request):
    id: Annotated[str, arg(0, metavar="ID", help="The subscription id.")]


async def subscription_refulfill(ctx: OpContext, req: SubscriptionRefulfillReq) -> StarsRefulfill:
    """Report whether a lapsed Star subscription could be re-joined — and refuse to.

    Re-joining debits Stars. `payments.fulfillStarsSubscription` is one of the
    four methods `ops/payment.py` names as deliberately absent from tlgr's
    surface, and this command exists to say so with the subscription's own
    state attached rather than to be a second way in.
    """
    page = await subscription_list(ctx, SubscriptionListReq())
    for row in page.items:
        if row.id == req.id:
            return StarsRefulfill(
                id=req.id,
                ok=False,
                can_refulfill=row.can_refulfill,
                stars=(row.pricing or {}).get("amount"),
                reason=_settings.NO_SPEND,
            )
    raise UsageError(
        f"no Star subscription with the id {req.id!r}; `stars subscription list` shows them",
        field="id",
    )


SPEC_SUBSCRIPTION_REFULFILL = OperationSpec(
    id="stars.subscription.refulfill",
    request=SubscriptionRefulfillReq,
    response=StarsRefulfill,
    impl=subscription_refulfill,
    summary="Report whether a lapsed Star subscription can be re-joined (tlgr does not charge)",
    idempotent=True,
    columns=("id", "ok", "can_refulfill", "stars", "reason"),
    headers=("Id", "Done", "Allowed", "Stars", "Why not"),
    example={"id": "sub1", "ok": False, "can_refulfill": True, "stars": 100},
    example_args="stars subscription refulfill sub1",
    covers_partial=("stars.subscription-refulfill",),
    coverage_note=(
        "Whether the server would allow it, and what it would cost, are "
        "reported; the charge itself is absent from tlgr's surface by policy."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# stars rating get / revenue get / url get
# ---------------------------------------------------------------------------


class RatingGetReq(Request):
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Whose rating (default: me)."),
    ] = None


async def rating_get(ctx: OpContext, req: RatingGetReq) -> StarsRating:
    """The Star rating badge: level, progress and what is still pending."""
    from telethon.tl import types
    from telethon.tl.functions import users as fn

    target = (
        await _settings.input_user(ctx, req.user) if req.user is not None else types.InputUserSelf()
    )
    answer = await client(ctx)(fn.GetFullUserRequest(id=target))
    full = getattr(answer, "full_user", None)
    rating = getattr(full, "stars_rating", None)
    pending = getattr(full, "stars_my_pending_rating", None)
    config = await _settings.app_config(ctx)
    return StarsRating(
        level=int(getattr(rating, "level", 0) or 0),
        stars=int(getattr(rating, "stars", 0) or 0),
        current_level_stars=int(getattr(rating, "current_level_stars", 0) or 0),
        next_level_stars=getattr(rating, "next_level_stars", None),
        pending_stars=getattr(pending, "stars", None),
        pending_date=fmt_dt(getattr(full, "stars_my_pending_rating_date", None)),
        learnmore_url=str(config.get("stars_rating_learnmore_url") or "") or None,
    )


SPEC_RATING_GET = OperationSpec(
    id="stars.rating.get",
    request=RatingGetReq,
    response=StarsRating,
    impl=rating_get,
    summary="Star rating badge (level and progress)",
    idempotent=True,
    columns=("level", "stars", "current_level_stars", "next_level_stars", "pending_stars"),
    headers=("Level", "Stars", "This level", "Next level", "Pending"),
    example={"level": 3, "stars": 1200, "current_level_stars": 1000, "next_level_stars": 2000},
    example_args="stars rating get",
    covers=("stars.rating",),
    tags=frozenset({"agent-safe"}),
)


class RevenueGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="A channel or bot I own.")]
    ton: Annotated[bool, opt("--ton", help="TON revenue instead of Stars.")] = False
    dark: Annotated[bool, opt("--dark", help="Dark-theme graph tokens.")] = False


async def revenue_get(ctx: OpContext, req: RevenueGetReq) -> StarsRevenue:
    """Star (or ad) revenue statistics for a channel or bot I own.

    The graphs come back as `statsGraphAsync` tokens that need a second load;
    the token is reported rather than resolved, because loading it is the
    stats domain's job and doing it here would double every call.
    """
    from telethon.tl.functions import payments as fn

    peer = await _settings.resolve(ctx, req.chat)
    result = await client(ctx)(
        fn.GetStarsRevenueStatsRequest(peer=peer, dark=req.dark or None, ton=req.ton or None)
    )
    status = getattr(result, "status", None)
    current, _ = _settings.stars_of(getattr(status, "current_balance", None))
    available, _ = _settings.stars_of(getattr(status, "available_balance", None))
    overall, _ = _settings.stars_of(getattr(status, "overall_revenue", None))
    return StarsRevenue(
        chat_id=_settings.peer_of(peer),
        current_balance=current,
        available_balance=available,
        overall_revenue=overall,
        withdrawal_enabled=bool(getattr(status, "withdrawal_enabled", False)),
        next_withdrawal_at=fmt_dt(getattr(status, "next_withdrawal_at", None)),
        usd_rate=getattr(result, "usd_rate", None),
        revenue_graph=_graph(getattr(result, "revenue_graph", None)),
        top_hours_graph=_graph(getattr(result, "top_hours_graph", None)),
    )


def _graph(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    return {
        "kind": type(raw).__name__.removeprefix("StatsGraph").lower() or "graph",
        "token": getattr(raw, "token", None),
        "json": getattr(getattr(raw, "json", None), "data", None),
        "error": getattr(raw, "error", None),
    }


SPEC_REVENUE_GET = OperationSpec(
    id="stars.revenue.get",
    request=RevenueGetReq,
    response=StarsRevenue,
    impl=revenue_get,
    summary="Star / ad revenue statistics for a channel or bot I own",
    description="Needs `channelFull.can_view_stars_revenue`; the graphs are async tokens.",
    idempotent=True,
    columns=("chat_id", "current_balance", "available_balance", "withdrawal_enabled"),
    headers=("Chat", "Balance", "Available", "Withdrawable"),
    example={"chat_id": -1001600, "current_balance": 4200, "withdrawal_enabled": True},
    example_args="stars revenue get @mychannel",
    covers=("bots.bot-revenue-stats", "stars.revenue-stats"),
    tags=frozenset({"agent-safe"}),
)


class UrlGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="The channel or bot.")]
    password: Annotated[str | None, _PASSWORD] = None
    ads: Annotated[bool, opt("--ads", help="The ads-account URL instead of a withdrawal URL.")] = (
        False
    )
    amount: Annotated[int | None, opt("--amount", metavar="STARS", help="Amount to withdraw.")] = (
        None
    )
    ton: Annotated[bool, opt("--ton", help="Withdraw TON instead of Stars.")] = False


async def url_get(ctx: OpContext, req: UrlGetReq) -> StarsUrl:
    """Print the Fragment URL for withdrawing revenue, or for the ads account.

    Control-only by design. The withdrawal needs the cloud password as an SRP
    proof, and what comes back is a URL a human opens in a browser — tlgr
    prints it and stops, and does not drive the ad-purchase flow either.
    """
    from telethon.tl.functions import payments as fn

    from tlgr.ops import _auth

    handle = client(ctx)
    peer = await _settings.resolve(ctx, req.chat)
    if req.ads:
        result = await handle(fn.GetStarsRevenueAdsAccountUrlRequest(peer=peer))
        return StarsUrl(
            url=str(getattr(result, "url", "") or ""),
            kind="ads",
            chat_id=_settings.peer_of(peer),
        )

    result = await _auth.with_password(
        handle,
        lambda srp: fn.GetStarsRevenueWithdrawalUrlRequest(
            peer=peer, password=srp, ton=req.ton or None, amount=req.amount
        ),
        req.password,
    )
    return StarsUrl(
        url=str(getattr(result, "url", "") or ""),
        kind="withdrawal",
        chat_id=_settings.peer_of(peer),
        amount=req.amount,
        ton=req.ton,
    )


SPEC_URL_GET = OperationSpec(
    id="stars.url.get",
    request=UrlGetReq,
    response=StarsUrl,
    impl=url_get,
    summary="Get the Fragment URL for withdrawing revenue, or for buying ads with Stars",
    description=(
        "Control-only: the URL is printed and the human completes the "
        "transfer in a browser. tlgr moves no money."
    ),
    idempotent=True,
    rate_class="send",
    columns=("kind", "url", "amount", "ton"),
    headers=("Kind", "URL", "Amount", "TON"),
    example={"kind": "withdrawal", "url": "https://fragment.com/stars/withdraw?…"},
    example_args="stars url get @mychannel --amount 1000",
    covers=("gifts.withdraw-ton", "stars.ads-account", "stars.withdraw"),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
