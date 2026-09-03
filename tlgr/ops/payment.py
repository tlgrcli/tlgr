"""The `payment` group: the whole checkout surface, minus the button that pays.

tlgr models payments completely and moves no money. That is a policy, not a
gap, and the shape of this module is what makes it checkable:

* **Reading is implemented.** The form, its prices, its provider, the fields it
  wants, the saved cards it would offer, the receipt afterwards, the Star
  subscriptions on the account. A caller can see everything needed to decide.
* **Asking someone else to pay is implemented.** `payment invoice export` and
  `payment invoice send` create an invoice — that spends nobody's money, it
  requests somebody else's.
* **Spending is absent.** `payments.sendPaymentForm`, `sendStarsForm`,
  `validateRequestedInfo`, `fulfillStarsSubscription` — none of them is behind
  a flag, a confirmation or an environment variable. `payment form get`
  reports `payable_here: false` with the reason, so an agent reading the form
  learns *why* rather than discovering it from an exit code.

Cancelling a subscription is here, because cancelling costs nothing. Resuming
one re-enables future charges, which is why the whole command is confirmed.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_duration, to_unix
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.payment import (
    BankCard,
    Invoice,
    InvoiceLink,
    InvoiceSent,
    PaymentForm,
    PaymentInfo,
    PaymentInfoCleared,
    PriceLine,
    Receipt,
    StarSubscription,
    SubscriptionChange,
)
from tlgr.models.peer import PeerRef
from tlgr.ops import _bots, _send
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The one sentence every unpayable form carries. Written once so the policy
#: reads the same in the JSON, in the docs and in the refusal.
NOT_PAYABLE = (
    "tlgr never spends money: paying a form, validating order info, tipping "
    "and re-fulfilling a lapsed subscription are all absent from the surface"
)


def _prices(values: Any) -> list[PriceLine]:
    return [
        PriceLine(
            label=str(getattr(price, "label", "") or ""),
            amount=int(getattr(price, "amount", 0) or 0),
        )
        for price in (values or [])
    ]


def _invoice(raw: Any) -> Invoice | None:
    if raw is None:
        return None
    prices = _prices(getattr(raw, "prices", None))
    return Invoice(
        currency=str(getattr(raw, "currency", "") or ""),
        total_amount=sum(price.amount for price in prices),
        prices=prices,
        test=bool(getattr(raw, "test", False)),
        name_requested=bool(getattr(raw, "name_requested", False)),
        phone_requested=bool(getattr(raw, "phone_requested", False)),
        email_requested=bool(getattr(raw, "email_requested", False)),
        shipping_address_requested=bool(getattr(raw, "shipping_address_requested", False)),
        flexible=bool(getattr(raw, "flexible", False)),
        recurring=bool(getattr(raw, "recurring", False)),
        terms_url=getattr(raw, "terms_url", None),
        subscription_period=getattr(raw, "subscription_period", None),
        max_tip_amount=getattr(raw, "max_tip_amount", None),
        suggested_tip_amounts=[int(v) for v in (getattr(raw, "suggested_tip_amounts", None) or [])],
    )


def _saved_credentials(values: Any) -> list[dict[str, Any]]:
    """Saved cards, as a masked title and an id. Never a number."""
    return [
        {"id": str(getattr(entry, "id", "") or ""), "title": str(getattr(entry, "title", "") or "")}
        for entry in (values or [])
    ]


def _saved_info(info: Any) -> dict[str, Any] | None:
    if info is None:
        return None
    address = getattr(info, "shipping_address", None)
    return {
        "name": getattr(info, "name", None),
        "phone": getattr(info, "phone", None),
        "email": getattr(info, "email", None),
        "shipping": (
            {
                "street_line1": getattr(address, "street_line1", None),
                "street_line2": getattr(address, "street_line2", None),
                "city": getattr(address, "city", None),
                "state": getattr(address, "state", None),
                "country_iso2": getattr(address, "country_iso2", None),
                "post_code": getattr(address, "post_code", None),
            }
            if address is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# payment form get
# ---------------------------------------------------------------------------


class FormGetReq(Request):
    message: Annotated[
        str | None,
        opt("--message", metavar="CHAT:MSG_ID", help="Invoice message with a Pay button."),
    ] = None
    slug: Annotated[str | None, opt("--slug", metavar="SLUG", help="Invoice deep-link slug.")] = (
        None
    )
    stars: Annotated[
        int | None, opt("--stars", metavar="N", help="Stars top-up form for N Stars.")
    ] = None
    chat_invite: Annotated[
        str | None,
        opt("--chat-invite", metavar="HASH", help="Star-subscription invite hash."),
    ] = None
    business_transfer: Annotated[
        str | None,
        opt("--business-transfer", metavar="BOT:STARS", help="Business → bot Stars transfer."),
    ] = None
    theme: Annotated[
        str | None, opt("--theme", metavar="PATH", kind="path", help="JSON theme params.")
    ] = None


async def _input_invoice(ctx: OpContext, req: FormGetReq) -> Any:
    """One of the five `inputInvoice*` constructors, from one flag each."""
    from telethon.tl import types

    chosen = [
        name
        for name in ("message", "slug", "stars", "chat_invite", "business_transfer")
        if getattr(req, name) is not None
    ]
    if len(chosen) != 1:
        raise UsageError(
            "give exactly one of --message, --slug, --stars, --chat-invite or --business-transfer",
            field="slug",
        )

    if req.slug:
        return types.InputInvoiceSlug(slug=req.slug)
    if req.chat_invite:
        return types.InputInvoiceChatInviteSubscription(hash=req.chat_invite)
    if req.stars is not None:
        return types.InputInvoiceStars(
            purpose=types.InputStorePaymentStarsTopup(
                stars=int(req.stars), currency="USD", amount=0
            )
        )
    if req.business_transfer:
        handle, _, amount = req.business_transfer.rpartition(":")
        if not handle or not amount.isdigit():
            raise UsageError(
                "--business-transfer: expected '<bot>:<stars>'", field="business_transfer"
            )
        return types.InputInvoiceBusinessBotTransferStars(
            bot=await _bots.input_user(ctx, _bots.peer_ref(handle), field="business_transfer"),
            stars=int(amount),
        )

    chat, _, msg_id = str(req.message).rpartition(":")
    if not chat or not msg_id.strip().lstrip("-").isdigit():
        raise UsageError("--message: expected '<chat>:<msg_id>'", field="message")
    return types.InputInvoiceMessage(
        peer=await _send.resolve(ctx, _bots.peer_ref(chat)), msg_id=int(msg_id)
    )


async def form_get(ctx: OpContext, req: FormGetReq) -> PaymentForm:
    """Read a checkout form without paying for it.

    Fetching a form charges nothing, even though it creates a server-side
    `form_id` — that id is what a *payment* would then reference, and tlgr
    never sends one. Star forms expire after about ten minutes; `FORM_EXPIRED`
    just means fetch it again.
    """
    from telethon.tl.functions import payments as fn

    from tlgr.ops.webapp import _theme

    invoice = await _input_invoice(ctx, req)
    result = await client(ctx)(
        fn.GetPaymentFormRequest(invoice=invoice, theme_params=_theme(req.theme))
    )
    name = type(result).__name__
    raw_invoice = getattr(result, "invoice", None)
    model = _invoice(raw_invoice)
    native = getattr(result, "native_params", None)

    form = PaymentForm(
        form_kind={"PaymentForm": "form", "PaymentFormStars": "stars"}.get(name, "gift"),
        form_id=int(getattr(result, "form_id", 0) or 0),
        bot_id=getattr(result, "bot_id", None),
        provider_id=getattr(result, "provider_id", None),
        title=getattr(result, "title", None),
        description=getattr(result, "description", None),
        photo=getattr(getattr(result, "photo", None), "url", None),
        invoice=model,
        currency=model.currency if model else "",
        total_amount=model.total_amount if model else 0,
        prices=model.prices if model else [],
        tip_amounts=model.suggested_tip_amounts if model else [],
        recurring=bool(getattr(raw_invoice, "recurring", False)),
        terms_url=getattr(raw_invoice, "terms_url", None),
        subscription_period=getattr(raw_invoice, "subscription_period", None),
        url=getattr(result, "url", None),
        native_provider=getattr(result, "native_provider", None),
        native_params=getattr(native, "data", None),
        additional_methods=[
            {"url": getattr(m, "url", None), "title": getattr(m, "title", None)}
            for m in (getattr(result, "additional_methods", None) or [])
        ],
        saved_info=_saved_info(getattr(result, "saved_info", None)),
        saved_credentials=_saved_credentials(getattr(result, "saved_credentials", None)),
        can_save_credentials=bool(getattr(result, "can_save_credentials", False)),
        password_missing=bool(getattr(result, "password_missing", False)),
        payable_here=False,
        reason=NOT_PAYABLE,
    )
    return form


SPEC_FORM_GET = OperationSpec(
    id="payment.form.get",
    request=FormGetReq,
    response=PaymentForm,
    impl=form_get,
    summary="Read an invoice's checkout form without paying",
    description=(
        "Price, currency, provider, required fields and saved credentials. "
        "`payable_here` is always false and carries the reason: the shape a "
        "caller needs in order to decide is here, and the call that would "
        "charge is deliberately not."
    ),
    aliases=("pay.form.get",),
    columns=("form_kind", "title", "currency", "total_amount"),
    headers=("Kind", "Title", "Currency", "Amount"),
    example={
        "form_kind": "form",
        "form_id": 555,
        "title": "T-shirt",
        "currency": "USD",
        "total_amount": 1999,
        "payable_here": False,
    },
    example_args="payment form get --slug tshirt-123",
    covers=(
        "bots.get-payment-form",
        "bots.invoice-deeplink",
        "bots.invoice-input-kinds",
        "bots.recurring-payment-terms",
    ),
)


# ---------------------------------------------------------------------------
# payment receipt get
# ---------------------------------------------------------------------------


class ReceiptGetReq(Request):
    chat: Annotated[
        PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat holding the service message.")
    ]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Service message id.")]


async def receipt_get(ctx: OpContext, req: ReceiptGetReq) -> Receipt:
    """A payment receipt.

    The id must be the `messageActionPaymentSent` service message, not the
    invoice that preceded it: those are two different messages and Telegram
    only knows the receipt by the first.
    """
    from telethon.tl.functions import payments as fn

    result = await client(ctx)(
        fn.GetPaymentReceiptRequest(peer=await _send.resolve(ctx, req.chat), msg_id=int(req.msg_id))
    )
    raw_invoice = getattr(result, "invoice", None)
    model = _invoice(raw_invoice)
    date = getattr(result, "date", None)
    return Receipt(
        date=fmt_dt(date),
        date_unix=to_unix(date),
        bot_id=getattr(result, "bot_id", None),
        provider_id=getattr(result, "provider_id", None),
        title=getattr(result, "title", None),
        description=getattr(result, "description", None),
        invoice=model,
        currency=str(getattr(result, "currency", "") or (model.currency if model else "")),
        total_amount=int(getattr(result, "total_amount", 0) or 0),
        tip_amount=getattr(result, "tip_amount", None),
        credentials_title=getattr(result, "credentials_title", None),
        shipping=_shipping(getattr(result, "shipping", None)),
        info=_saved_info(getattr(result, "info", None)),
        transaction_id=getattr(result, "transaction_id", None),
        recurring=bool(getattr(raw_invoice, "recurring", False)),
    )


def _shipping(option: Any) -> dict[str, Any] | None:
    if option is None:
        return None
    return {
        "id": getattr(option, "id", None),
        "title": getattr(option, "title", None),
        "prices": [
            {"label": p.label, "amount": p.amount} for p in _prices(getattr(option, "prices", None))
        ],
    }


SPEC_RECEIPT_GET = OperationSpec(
    id="payment.receipt.get",
    request=ReceiptGetReq,
    response=Receipt,
    impl=receipt_get,
    summary="Show a payment receipt",
    aliases=("pay.receipt",),
    columns=("date", "title", "currency", "total_amount"),
    headers=("Date", "Title", "Currency", "Amount"),
    example={"title": "T-shirt", "currency": "USD", "total_amount": 1999},
    example_args="payment receipt get @shopbot 42",
    covers=("bots.payment-receipt",),
)


# ---------------------------------------------------------------------------
# payment info get / delete
# ---------------------------------------------------------------------------


class InfoGetReq(Request):
    clear: Annotated[bool, opt("--clear", help="Clear the selected parts.")] = False
    credentials: Annotated[bool, opt("--credentials", help="Select saved cards.")] = False
    shipping: Annotated[bool, opt("--shipping", help="Select saved shipping info.")] = False


async def info_get(ctx: OpContext, req: InfoGetReq) -> PaymentInfo:
    """My saved order information and saved cards.

    Card numbers are never here to leak: `paymentSavedCredentialsCard` carries
    an id and a masked title and nothing else, which is the whole reason this
    is safe to print.
    """
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    result = await handle(fn.GetSavedInfoRequest())
    info = _saved_info(getattr(result, "saved_info", None)) or {}
    model = PaymentInfo(
        has_saved_credentials=bool(getattr(result, "has_saved_credentials", False)),
        saved_info=info or None,
        name=info.get("name"),
        phone=info.get("phone"),
        email=info.get("email"),
        shipping=info.get("shipping"),
        has_saved_info=bool(info),
    )
    if req.clear:
        await handle(
            fn.ClearSavedInfoRequest(credentials=req.credentials or None, info=req.shipping or None)
        )
        model.cleared = True
        model.has_saved_credentials = model.has_saved_credentials and not req.credentials
        model.has_saved_info = model.has_saved_info and not req.shipping
    return model


SPEC_INFO_GET = OperationSpec(
    id="payment.info.get",
    request=InfoGetReq,
    response=PaymentInfo,
    impl=info_get,
    summary="Show my saved order information and saved cards",
    aliases=("pay.saved-info.get", "settings.payment-info"),
    mutating=True,
    destructive=True,
    tags=frozenset({"mutating-checked"}),
    columns=("has_saved_info", "has_saved_credentials", "cleared"),
    headers=("Info", "Cards", "Cleared"),
    example={"has_saved_credentials": True, "has_saved_info": False},
    example_args="payment info get",
    covers=("bots.saved-payment-info-get", "privacy.clear-payment-info"),
)


class InfoDeleteReq(Request):
    credentials: Annotated[bool, opt("--credentials", help="Forget saved cards.")] = False
    info: Annotated[bool, opt("--info", help="Forget saved shipping/contact info.")] = False


async def info_delete(ctx: OpContext, req: InfoDeleteReq) -> PaymentInfoCleared:
    """Clear my saved shipping information and/or saved cards.

    Destructive, but not a money movement: forgetting a card does not spend
    anything, which is why it is one of the few write verbs in this group.
    """
    from telethon.tl.functions import payments as fn

    if not req.credentials and not req.info:
        raise UsageError("give --credentials and/or --info", field="credentials")
    await client(ctx)(
        fn.ClearSavedInfoRequest(credentials=req.credentials or None, info=req.info or None)
    )
    return PaymentInfoCleared(credentials_cleared=req.credentials, info_cleared=req.info)


SPEC_INFO_DELETE = OperationSpec(
    id="payment.info.delete",
    request=InfoDeleteReq,
    response=PaymentInfoCleared,
    impl=info_delete,
    summary="Clear my saved shipping information and saved cards",
    aliases=("pay.saved-info.clear",),
    mutating=True,
    destructive=True,
    columns=("credentials_cleared", "info_cleared"),
    headers=("Cards", "Info"),
    example={"credentials_cleared": True, "info_cleared": False},
    example_args="payment info delete --credentials",
    covers=("bots.saved-payment-info-clear",),
)


# ---------------------------------------------------------------------------
# payment card get
# ---------------------------------------------------------------------------


class CardGetReq(Request):
    number: Annotated[str, arg(0, metavar="NUMBER", help="Card number or BIN.")]


async def card_get(ctx: OpContext, req: CardGetReq) -> BankCard:
    """Look up the issuing bank of a card BIN.

    A read-only BIN lookup; it enters nothing into a payment flow. The number
    is not echoed back, not logged, and not put on the event bus.
    """
    from telethon.tl.functions import payments as fn

    result = await client(ctx)(fn.GetBankCardDataRequest(number=req.number.replace(" ", "")))
    return BankCard(
        title=str(getattr(result, "title", "") or ""),
        open_urls=[
            {"name": str(getattr(u, "name", "") or ""), "url": str(getattr(u, "url", "") or "")}
            for u in (getattr(result, "open_urls", None) or [])
        ],
    )


SPEC_CARD_GET = OperationSpec(
    id="payment.card.get",
    request=CardGetReq,
    response=BankCard,
    impl=card_get,
    summary="Look up the issuing bank of a card BIN",
    aliases=("pay.bank-card",),
    columns=("title",),
    headers=("Issuer",),
    example={"title": "Example Bank", "open_urls": []},
    example_args="payment card get 411111",
    covers=("bots.bank-card-data",),
)


# ---------------------------------------------------------------------------
# payment invoice export / send
# ---------------------------------------------------------------------------


class InvoiceExportReq(Request):
    title: Annotated[str, opt("--title", help="Invoice title.")] = ""
    description: Annotated[str, opt("--description", help="Invoice description.")] = ""
    currency: Annotated[
        str, opt("--currency", metavar="ISO", help="ISO currency, or XTR for Stars.")
    ] = ""
    prices: Annotated[str, opt("--prices", metavar="LABEL:AMOUNT,…", help="Price components.")] = ""
    payload: Annotated[str, opt("--payload", metavar="TEXT", help="Opaque bot payload.")] = ""
    provider: Annotated[
        str | None, opt("--provider", metavar="TOKEN", help="Payment provider token (fiat).")
    ] = None
    provider_data: Annotated[
        str | None, opt("--provider-data", metavar="JSON", kind="json", help="Provider JSON.")
    ] = None
    photo: Annotated[str | None, opt("--photo", metavar="URL", help="Invoice photo URL.")] = None
    subscription_period: Annotated[
        str | None,
        opt("--subscription-period", metavar="DURATION", help="Recurring period (Stars only)."),
    ] = None
    tip_max: Annotated[
        int | None, opt("--tip-max", metavar="N", help="Maximum tip the buyer may add.")
    ] = None
    suggested_tips: Annotated[
        str | None, opt("--suggested-tips", metavar="N,…", help="Suggested tip amounts.")
    ] = None
    need: Annotated[
        list[str],
        opt("--need", metavar="FIELD", help="name|phone|email|shipping (repeatable)."),
    ] = []
    flexible: Annotated[bool, opt("--flexible", help="Price depends on the shipping option.")] = (
        False
    )
    recurring_terms: Annotated[
        str | None, opt("--recurring-terms", metavar="URL", help="Terms URL for a recurring one.")
    ] = None


def _price_lines(spec: str) -> list[Any]:
    from telethon.tl import types

    out: list[Any] = []
    for chunk in spec.split(","):
        label, _, amount = chunk.rpartition(":")
        if not label or not amount.strip().lstrip("-").isdigit():
            raise UsageError("--prices: expected 'Label:1999,Shipping:500'", field="prices")
        out.append(types.LabeledPrice(label=label.strip(), amount=int(amount)))
    if not out:
        raise UsageError("--prices is required", field="prices")
    return out


def _tl_invoice(req: InvoiceExportReq) -> Any:
    from telethon.tl import types

    need = {n.strip() for n in req.need}
    unknown = need - {"name", "phone", "email", "shipping"}
    if unknown:
        raise UsageError(
            f"--need: {sorted(unknown)[0]!r} is not a field (name, phone, email, shipping)",
            field="need",
        )
    period = int(parse_duration(req.subscription_period) or 0) if req.subscription_period else None
    return types.Invoice(
        currency=req.currency,
        prices=_price_lines(req.prices),
        name_requested="name" in need or None,
        phone_requested="phone" in need or None,
        email_requested="email" in need or None,
        shipping_address_requested="shipping" in need or None,
        flexible=req.flexible or None,
        recurring=bool(req.recurring_terms) or None,
        terms_url=req.recurring_terms,
        subscription_period=period,
        max_tip_amount=req.tip_max,
        suggested_tip_amounts=(
            [int(v) for v in req.suggested_tips.split(",") if v.strip()]
            if req.suggested_tips
            else None
        ),
    )


def _invoice_media(req: InvoiceExportReq, *, extended: Any = None) -> Any:
    from telethon.tl import types

    if not req.title or not req.description or not req.currency or not req.payload:
        raise UsageError(
            "--title, --description, --currency, --prices and --payload are all required",
            field="title",
        )
    return types.InputMediaInvoice(
        title=req.title,
        description=req.description,
        invoice=_tl_invoice(req),
        payload=_bots.payload_bytes(req.payload, field="payload") or b"",
        provider_data=_bots.data_json(req.provider_data or "{}", field="provider_data"),
        photo=(
            types.InputWebDocument(url=req.photo, size=0, mime_type="image/jpeg", attributes=[])
            if req.photo
            else None
        ),
        provider=req.provider,
        extended_media=extended,
    )


async def invoice_export(ctx: OpContext, req: InvoiceExportReq) -> InvoiceLink:
    """Create an invoice deep link.

    Creating an invoice is not a money movement — it asks somebody else to
    pay — so it is implemented. A Star *subscription* invoice can only exist
    as a link: `messages.sendMedia` rejects one, which is why there is no
    `--subscription-period` on `payment invoice send`.
    """
    from telethon.tl.functions import payments as fn

    await _bots.require_bot_session(ctx, "exporting an invoice link")
    result = await client(ctx)(fn.ExportInvoiceRequest(invoice_media=_invoice_media(req)))
    url = str(getattr(result, "url", "") or "")
    return InvoiceLink(url=url, slug=url.rsplit("/", 1)[-1])


SPEC_INVOICE_EXPORT = OperationSpec(
    id="payment.invoice.export",
    request=InvoiceExportReq,
    response=InvoiceLink,
    impl=invoice_export,
    summary="Create an invoice deep link",
    aliases=("pay.invoice.export",),
    mutating=True,
    columns=("url", "slug"),
    headers=("URL", "Slug"),
    example={"url": "https://t.me/$abc123", "slug": "$abc123"},
    example_args=(
        'payment invoice export --title Shirt --description "A shirt" '
        "--currency USD --prices Shirt:1999 --payload order-1"
    ),
    covers=("bots.bot-subscription-invoice", "bots.export-invoice-link"),
)


class InvoiceSendReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Recipient (private only).")]
    title: Annotated[str, opt("--title", help="Invoice title.")] = ""
    description: Annotated[str, opt("--description", help="Invoice description.")] = ""
    currency: Annotated[
        str, opt("--currency", metavar="ISO", help="ISO currency, or XTR for Stars.")
    ] = ""
    prices: Annotated[str, opt("--prices", metavar="LABEL:AMOUNT,…", help="Price components.")] = ""
    payload: Annotated[str, opt("--payload", metavar="TEXT", help="Opaque bot payload.")] = ""
    provider: Annotated[
        str | None, opt("--provider", metavar="TOKEN", help="Payment provider token (fiat).")
    ] = None
    provider_data: Annotated[
        str | None, opt("--provider-data", metavar="JSON", kind="json", help="Provider JSON.")
    ] = None
    photo: Annotated[str | None, opt("--photo", metavar="URL", help="Invoice photo URL.")] = None
    extended_media: Annotated[
        str | None,
        opt("--extended-media", metavar="PATH", kind="path", help="Paid media behind the invoice."),
    ] = None
    need: Annotated[
        list[str], opt("--need", metavar="FIELD", help="name|phone|email|shipping (repeatable).")
    ] = []
    flexible: Annotated[bool, opt("--flexible", help="Price depends on the shipping option.")] = (
        False
    )
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False


async def invoice_send(ctx: OpContext, req: InvoiceSendReq) -> InvoiceSent:
    """Send an invoice message to a user.

    Invoices only go to private chats — Telegram refuses anything else — and
    `--extended-media` turns the invoice into paid media, where the file is
    hidden until the buyer pays somebody else's bill, never tlgr's.
    """
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "sending an invoice")
    target = await _send.resolve(ctx, req.user)
    if type(target).__name__ not in ("InputPeerUser", "InputPeerSelf"):
        raise UsageError("an invoice can only be sent to a private chat", field="user")

    export = InvoiceExportReq(
        title=req.title,
        description=req.description,
        currency=req.currency,
        prices=req.prices,
        payload=req.payload,
        provider=req.provider,
        provider_data=req.provider_data,
        photo=req.photo,
        need=req.need,
        flexible=req.flexible,
    )
    extended = await _send.input_media(ctx, req.extended_media) if req.extended_media else None
    media = _invoice_media(export, extended=extended)
    from tlgr.ops._common import random_id

    updates = await client(ctx)(
        fn.SendMediaRequest(
            peer=target,
            media=media,
            message="",
            random_id=random_id(),
            silent=req.silent or None,
        )
    )
    message = _send.message_from_updates(updates, chat_id=_send.peer_id_of(target))
    prices = _price_lines(req.prices)
    return InvoiceSent(
        chat_id=message.chat_id,
        msg_id=message.id,
        currency=req.currency,
        total_amount=sum(int(getattr(p, "amount", 0) or 0) for p in prices),
    )


SPEC_INVOICE_SEND = OperationSpec(
    id="payment.invoice.send",
    request=InvoiceSendReq,
    response=InvoiceSent,
    impl=invoice_send,
    summary="Send an invoice message to a user",
    aliases=("pay.invoice.send",),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "currency", "total_amount"),
    headers=("Chat", "Message", "Currency", "Amount"),
    example={"chat_id": 4242, "msg_id": 12, "currency": "USD", "total_amount": 1999},
    example_args=(
        "payment invoice send @alice --title Shirt --description Shirt "
        "--currency USD --prices Shirt:1999 --payload order-1"
    ),
    covers=("bots.send-invoice-message",),
)


# ---------------------------------------------------------------------------
# payment subscription list / set
# ---------------------------------------------------------------------------


class SubscriptionListReq(Request):
    missing_balance: Annotated[
        bool, opt("--missing-balance", help="Only ones that will lapse for lack of Stars.")
    ] = False


def _subscription(entry: Any) -> StarSubscription:
    until = getattr(entry, "until_date", None)
    pricing = getattr(entry, "pricing", None)
    peer = getattr(entry, "peer", None)
    from tlgr.ops._serialize import peer_id_of

    return StarSubscription(
        id=str(getattr(entry, "id", "") or ""),
        peer=peer_id_of(peer),
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
        cancelled=bool(getattr(entry, "canceled", False)),
        can_refulfill=bool(getattr(entry, "can_refulfill", False)),
        missing_balance=bool(getattr(entry, "missing_balance", False)),
        invoice_slug=getattr(entry, "invoice_slug", None),
        chat_invite_hash=getattr(entry, "chat_invite_hash", None),
        title=getattr(entry, "title", None),
        photo=getattr(getattr(entry, "photo", None), "url", None),
    )


async def subscription_list(ctx: OpContext, req: SubscriptionListReq) -> Page[StarSubscription]:
    """My Telegram Star subscriptions.

    `can_refulfill` says the *server* would let a lapsed subscription be
    rejoined. tlgr still will not: re-fulfilling debits Stars, and the row
    carries the flag so a caller learns why rather than getting a bare
    refusal.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    limit, state = window(ctx, "payment.subscription.list", PageKind.RATE, default=50)
    result = await client(ctx)(
        fn.GetStarsSubscriptionsRequest(
            peer=types.InputPeerSelf(),
            offset=str(state.get("offset", "") or ""),
            missing_balance=req.missing_balance or None,
        )
    )
    items = [
        _subscription(entry) for entry in (getattr(result, "subscriptions", None) or [])[:limit]
    ]
    next_offset = str(getattr(result, "subscriptions_next_offset", "") or "")
    return build_page(
        items,
        op="payment.subscription.list",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "subscriptions_missing_balance", None),
    )


SPEC_SUBSCRIPTION_LIST = OperationSpec(
    id="payment.subscription.list",
    request=SubscriptionListReq,
    response=Page[StarSubscription],
    impl=subscription_list,
    summary="List my Telegram Star subscriptions",
    aliases=("pay.subscription.list", "stars.subs.list"),
    paginated=PageKind.RATE,
    columns=("id", "peer", "until_date", "cancelled"),
    headers=("ID", "Peer", "Until", "Cancelled"),
    example={"items": [{"id": "sub1", "peer": 4242, "cancelled": False}], "has_more": False},
    example_args="payment subscription list",
    covers=("bots.stars-subscriptions-list",),
)


class SubscriptionSetReq(Request):
    subscription_id: Annotated[
        str | None,
        arg(0, metavar="SUBSCRIPTION_ID", required=False, help="My subscription id."),
    ] = None
    auto_renew: Annotated[str, choice("on", "off", help="Resume or cancel auto-renewal.")] = "off"
    user: Annotated[
        PeerRef | None, opt("--user", metavar="USER", kind="user", help="Bot side: the subscriber.")
    ] = None
    charge_id: Annotated[
        str | None, opt("--charge-id", metavar="ID", help="Bot side: provider charge id.")
    ] = None


async def subscription_set(ctx: OpContext, req: SubscriptionSetReq) -> SubscriptionChange:
    """Turn a Star subscription's auto-renewal on or off.

    Deliberately *not* `payments.fulfillStarsSubscription`: that one pays for
    a lapsed period and is absent from the surface. Cancelling costs nothing;
    resuming re-enables future charges, which is what the confirmation on this
    command is for.
    """
    from telethon.tl import types
    from telethon.tl.functions import payments as fn

    handle = client(ctx)
    resume = req.auto_renew == "on"

    if req.user is not None or req.charge_id is not None:
        await _bots.require_bot_session(ctx, "cancelling a user's subscription")
        if req.user is None or not req.charge_id:
            raise UsageError("--user and --charge-id go together", field="charge_id")
        await handle(
            fn.BotCancelStarsSubscriptionRequest(
                user_id=await _bots.input_user(ctx, req.user, field="user"),
                charge_id=req.charge_id,
                restore=resume or None,
            )
        )
        return SubscriptionChange(
            user_id=_send.peer_id_of(await _send.resolve(ctx, req.user)),
            charge_id=req.charge_id,
            cancelled=not resume,
        )

    if not req.subscription_id:
        raise UsageError(
            "give a subscription id, or --user with --charge-id on a bot session",
            field="subscription_id",
        )
    await handle(
        fn.ChangeStarsSubscriptionRequest(
            peer=types.InputPeerSelf(),
            subscription_id=req.subscription_id,
            canceled=not resume,
        )
    )
    return SubscriptionChange(subscription_id=req.subscription_id, cancelled=not resume)


SPEC_SUBSCRIPTION_SET = OperationSpec(
    id="payment.subscription.set",
    request=SubscriptionSetReq,
    response=SubscriptionChange,
    impl=subscription_set,
    summary="Turn a Star subscription's auto-renewal on or off",
    aliases=("pay.subscription.set",),
    mutating=True,
    destructive=True,
    columns=("subscription_id", "cancelled"),
    headers=("Subscription", "Cancelled"),
    example={"subscription_id": "sub1", "cancelled": True},
    example_args="payment subscription set sub1 --auto-renew off",
    covers=("bots.bot-cancel-user-subscription", "bots.stars-subscription-cancel"),
)
