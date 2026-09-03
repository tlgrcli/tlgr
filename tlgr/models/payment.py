"""Payments, read-only by design.

tlgr models the whole checkout surface — the invoice, the form, the receipt,
the subscription — and implements every verb that does *not* move money.
`PaymentForm.payable_here` is therefore always false and carries the reason:
the shape a caller needs in order to decide is here, the button that would
charge is deliberately not, and saying so in the payload is better than
leaving a caller to discover it from an exit code.

Amounts are integers in the smallest unit of `currency`, exactly as Telegram
sends them (`XTR` means Telegram Stars, whose smallest unit is one Star).
Rounding them into a float here would lose money in the last decimal on every
currency that has three of them.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "BankCard",
    "Invoice",
    "InvoiceLink",
    "InvoiceSent",
    "PaymentForm",
    "PaymentInfo",
    "PaymentInfoCleared",
    "PriceLine",
    "Receipt",
    "StarSubscription",
    "SubscriptionChange",
]


class PriceLine(Model):
    label: str = ""
    amount: int = 0


class Invoice(Model):
    """The invoice itself: what is being charged for, and what it needs."""

    currency: str = ""
    total_amount: int = 0
    prices: list[PriceLine] = []
    test: bool = False
    name_requested: bool = False
    phone_requested: bool = False
    email_requested: bool = False
    shipping_address_requested: bool = False
    flexible: bool = False
    recurring: bool = False
    terms_url: str | None = None
    subscription_period: int | None = None
    max_tip_amount: int | None = None
    suggested_tip_amounts: list[int] = []


class PaymentForm(Model):
    """A checkout form, read without paying for it."""

    form_kind: str = "form"
    form_id: int = 0
    bot_id: int | None = None
    provider_id: int | None = None
    title: str | None = None
    description: str | None = None
    photo: str | None = None
    invoice: Invoice | None = None
    currency: str = ""
    total_amount: int = 0
    prices: list[PriceLine] = []
    tip_amounts: list[int] = []
    recurring: bool = False
    terms_url: str | None = None
    subscription_period: int | None = None
    url: str | None = None
    native_provider: str | None = None
    native_params: Any = None
    additional_methods: list[dict[str, Any]] = []
    saved_info: dict[str, Any] | None = None
    saved_credentials: list[dict[str, Any]] = []
    can_save_credentials: bool = False
    password_missing: bool = False
    expires_at: str | None = None
    #: Always false. `reason` says which policy refuses to charge here.
    payable_here: bool = False
    reason: str = ""


class Receipt(Model):
    date: str | None = None
    date_unix: int | None = None
    bot_id: int | None = None
    provider_id: int | None = None
    title: str | None = None
    description: str | None = None
    invoice: Invoice | None = None
    currency: str = ""
    total_amount: int = 0
    tip_amount: int | None = None
    credentials_title: str | None = None
    shipping: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    transaction_id: str | None = None
    recurring: bool = False
    refunded: bool = False


class PaymentInfo(Model):
    """My saved order information and saved cards. Never a card number."""

    has_saved_credentials: bool = False
    credentials: list[dict[str, Any]] = []
    saved_info: dict[str, Any] | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    shipping: dict[str, Any] | None = None
    has_saved_info: bool = False
    cleared: bool = False


class PaymentInfoCleared(Model):
    credentials_cleared: bool = False
    info_cleared: bool = False


class InvoiceLink(Model):
    url: str = ""
    slug: str = ""


class InvoiceSent(Model):
    chat_id: int = 0
    msg_id: int = 0
    slug: str | None = None
    currency: str = ""
    total_amount: int = 0


class BankCard(Model):
    title: str = ""
    open_urls: list[dict[str, str]] = []


class StarSubscription(Model):
    id: str = ""
    peer: int | None = None
    until_date: str | None = None
    until_date_unix: int | None = None
    pricing: dict[str, int] | None = None
    cancelled: bool = False
    #: Re-joining a lapsed subscription debits Stars, so tlgr reports that the
    #: server would allow it and still refuses to do it.
    can_refulfill: bool = False
    missing_balance: bool = False
    invoice_slug: str | None = None
    chat_invite_hash: str | None = None
    title: str | None = None
    photo: str | None = None


class SubscriptionChange(Model):
    subscription_id: str | None = None
    user_id: int | None = None
    charge_id: str | None = None
    cancelled: bool = False
    until_date: str | None = None
