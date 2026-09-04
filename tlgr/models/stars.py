"""Telegram Stars: the balance, the ledger, subscriptions and revenue.

A Stars amount is `(amount, nanos)` on the wire, and both halves are kept:
collapsing them to a float loses the ninth digit that a ledger reconciliation
depends on, and TON amounts arrive in the same shape with nine decimals of
their own.

Nothing here moves value. The withdrawal command produces a Fragment URL for
a human to open, which is why `StarsUrl` is a URL and not a receipt.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "StarsBalance",
    "StarsRating",
    "StarsRefulfill",
    "StarsRevenue",
    "StarsTransaction",
    "StarsUrl",
]


class StarsBalance(Model):
    """`payments.getStarsStatus`. `ton` is in nanotons, never rounded."""

    stars: int = 0
    nanos: int = 0
    ton: int | None = None
    currency: str = "XTR"
    subscriptions_missing_balance: int | None = None


class StarsTransaction(Model):
    id: str = ""
    date: str | None = None
    date_unix: int | None = None
    #: Signed: negative is money leaving the balance.
    stars: int = 0
    nanos: int = 0
    refund: bool = False
    pending: bool = False
    failed: bool = False
    peer: int | None = None
    peer_kind: str = ""
    peer_ref: Peer | None = None
    title: str | None = None
    description: str | None = None
    msg_id: int | None = None
    subscription_period: int | None = None
    transaction_url: str | None = None
    #: gift | reaction | subscription | resale | upgrade | ads | …
    kind: str = ""


class StarsRating(Model):
    level: int = 0
    stars: int = 0
    current_level_stars: int = 0
    next_level_stars: int | None = None
    pending_stars: int | None = None
    pending_date: str | None = None
    learnmore_url: str | None = None


class StarsRevenue(Model):
    chat_id: int = 0
    current_balance: int | None = None
    available_balance: int | None = None
    overall_revenue: int | None = None
    withdrawal_enabled: bool = False
    next_withdrawal_at: str | None = None
    usd_rate: float | None = None
    revenue_graph: dict[str, Any] | None = None
    top_hours_graph: dict[str, Any] | None = None


class StarsUrl(Model):
    """A Fragment URL. Opening it — and the transfer — is the human's job."""

    url: str = ""
    #: withdrawal | ads
    kind: str = "withdrawal"
    chat_id: int = 0
    amount: int | None = None
    ton: bool = False


class StarsRefulfill(Model):
    """Re-joining a lapsed Star subscription, reported and not performed."""

    id: str
    ok: bool = False
    can_refulfill: bool | None = None
    stars: int | None = None
    reason: str = ""
