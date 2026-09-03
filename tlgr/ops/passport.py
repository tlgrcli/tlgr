"""The `passport` group: Telegram Passport, and the line tlgr will not cross.

Passport stores identity documents — a passport scan, a driving licence, an
address — encrypted end to end under a secret derived from the cloud password.
Telethon carries the RPCs but none of the crypto: no secure-secret KDF, no
`secureCredentialsEncrypted` builder, no AES-256-CBC/SHA-512 padding scheme.

So this group is deliberately lopsided, and says so rather than pretending:

* **reading works** — what is stored (`passport list`), what a service is
  asking for and which of my values would satisfy it (`passport form get`);
* **deleting works** — `account.deleteSecureValue` needs no crypto at all,
  and being able to remove documents matters more than being able to add
  them;
* **verifying a phone or email works** — that exchange is plain MTProto;
* **authorising a service does not.** `passport authorize` is registered so
  that the command exists and explains itself, and it raises NOT_SUPPORTED.
  Half-implementing it would mean sending a service *something* in the
  credentials field; a service that accepted it would be reading identity
  documents tlgr encrypted wrongly, and one that rejected it would leave the
  user with an error nobody can debug.

Nothing here is ever automatic. Handing identity documents to a bot from a
script is exactly the operation that deserves a human in front of it.
"""

from __future__ import annotations

import contextlib
import json
from typing import Annotated, Any

from tlgr.core.errors import NotSupportedError, UsageError
from tlgr.core.pagination import PageKind
from tlgr.models.auth import (
    PassportDeletion,
    PassportForm,
    PassportRequirement,
    PassportValue,
    PassportVerification,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _auth, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [
    "SPEC_AUTHORIZE",
    "SPEC_DELETE",
    "SPEC_FORM_GET",
    "SPEC_LIST",
    "SPEC_VERIFY",
]

#: The message every "we cannot do the crypto" path prints. One string, so a
#: reader who hits it twice recognises it as the same wall.
_NO_CRYPTO = (
    "Telegram Passport values are encrypted with a secret derived from the cloud "
    "password (AES-256-CBC plus a SHA-512 KDF). Telethon 1.44 implements none of "
    "it, and tlgr will not ship a half-correct implementation of a format that "
    "carries identity documents. Use an official client for this step."
)


def _value_model(raw: Any) -> PassportValue:
    plain = getattr(raw, "plain_data", None)
    return PassportValue(
        type=_auth.secure_value_name(getattr(raw, "type", None)),
        hash=_auth.b64(getattr(raw, "hash", b"")),
        has_files=bool(getattr(raw, "files", None) or getattr(raw, "front_side", None)),
        has_translation=bool(getattr(raw, "translation", None)),
        plain_data=getattr(plain, "phone", None) or getattr(plain, "email", None),
    )


# ---------------------------------------------------------------------------
# passport list
# ---------------------------------------------------------------------------


class ListReq(Request):
    type: Annotated[
        tuple[str, ...],
        opt("--type", metavar="TYPE", help="Restrict to these document types (repeatable)."),
    ] = ()
    decrypt: Annotated[
        bool, opt("--decrypt", help="Attempt to decrypt the values (needs the crypto stack).")
    ] = False


async def list_values(ctx: OpContext, req: ListReq) -> Page[PassportValue]:
    """List the Passport documents stored on this account, as metadata.

    `phone` and `email` values are stored in the clear and come back filled;
    every other type is end-to-end encrypted, so what is reported is the type,
    the hash and whether it carries files. That is enough to audit what a
    service could be given, which is the question worth asking from a CLI.
    """
    from telethon.tl.functions import account as fn

    if req.decrypt:
        raise NotSupportedError(_NO_CRYPTO)
    client = _auth.client(ctx)
    if req.type:
        wanted = [_auth.secure_value_type(name) for name in req.type]
        raw = await client(fn.GetSecureValueRequest(types=wanted))
    else:
        raw = await client(fn.GetAllSecureValuesRequest())
    items = [_value_model(value) for value in raw or []]
    return Page(items=items, has_more=False, total=len(items))


SPEC_LIST = OperationSpec(
    id="passport.list",
    request=ListReq,
    response=Page[PassportValue],
    impl=list_values,
    summary="List the Telegram Passport documents stored on my account",
    description=(
        "Metadata only. The values are encrypted under a secret derived from "
        "the cloud password, and tlgr does not implement that KDF — which is "
        "why this feature is catalogued as partial rather than claimed."
    ),
    paginated=PageKind.LOCAL,
    rate_class="read",
    columns=("type", "has_files", "plain_data"),
    headers=("Type", "Files", "Plain value"),
    example={"items": [{"type": "phone", "hash": "3q2-7w", "plain_data": "+989123456789"}]},
    example_args="passport list",
    covers=("passport.list",),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# passport form get
# ---------------------------------------------------------------------------


class FormGetReq(Request):
    bot: Annotated[
        PeerRef | None,
        arg(0, metavar="BOT", required=False, kind="peer", help="The service's bot."),
    ] = None
    scope: Annotated[
        str | None, opt("--scope", metavar="JSON", kind="json", help="Scope JSON from the link.")
    ] = None
    public_key: Annotated[
        str | None,
        opt("--public-key", metavar="PATH", kind="path", help="The service's RSA key (PEM)."),
    ] = None
    nonce: Annotated[str | None, opt("--nonce", help="Nonce from the request.")] = None
    country_language: Annotated[
        str | None,
        opt("--country-language", metavar="ISO2", help="Only look up a country's native-name tag."),
    ] = None


async def form_get(ctx: OpContext, req: FormGetReq) -> PassportForm:
    """Show what a service is asking for, and what of it I already hold.

    Read-only inspection of the request: the document types demanded, whether
    each needs a selfie or a translation, the privacy policy, and the values
    already on the account that would satisfy it. `--country-language` is the
    lookup that decides whether a country's forms want native-language
    fields, which is otherwise a silent validation failure later.
    """
    from telethon.tl.functions import help as help_fn

    client = _auth.client(ctx)

    if req.country_language:
        config = await client(help_fn.GetPassportConfigRequest(hash=0))
        mapping: dict[str, Any] = {}
        raw = getattr(getattr(config, "countries_langs", None), "data", "")
        if raw:
            with contextlib.suppress(ValueError):
                mapping = json.loads(raw)
        return PassportForm(country_language=mapping.get(req.country_language.upper()))

    if not (req.bot and req.scope and req.public_key):
        raise UsageError(
            "reading a form needs the bot, --scope and --public-key from the service's link",
            field="bot",
        )
    from pathlib import Path

    from telethon.tl.functions import account as fn

    entity = await _send.resolve(ctx, req.bot)
    bot_id = int(getattr(entity, "user_id", 0) or 0)
    form = await client(
        fn.GetAuthorizationFormRequest(
            bot_id=bot_id,
            scope=req.scope,
            public_key=Path(req.public_key).read_text(encoding="utf-8"),
        )
    )
    return PassportForm(
        bot=bot_id,
        required_types=[
            PassportRequirement(
                type=_auth.secure_value_name(getattr(item, "type", None)),
                native_names=bool(getattr(item, "native_names", False)),
                selfie_required=bool(getattr(item, "selfie_required", False)),
                translation_required=bool(getattr(item, "translation_required", False)),
            )
            for item in getattr(form, "required_types", None) or []
        ],
        privacy_policy_url=getattr(form, "privacy_policy_url", None),
        values=[_value_model(value) for value in getattr(form, "values", None) or []],
        errors=[str(getattr(err, "text", err)) for err in getattr(form, "errors", None) or []],
    )


SPEC_FORM_GET = OperationSpec(
    id="passport.form.get",
    request=FormGetReq,
    response=PassportForm,
    impl=form_get,
    summary="Show what a service is asking for through Telegram Passport",
    rate_class="read",
    columns=("bot", "privacy_policy_url"),
    example={
        "bot": 4242,
        "required_types": [{"type": "passport", "selfie_required": True}],
        "privacy_policy_url": "https://example.com/privacy",
    },
    example_args="passport form get --country-language DE",
    covers=("passport.country-language",),
    covers_partial=("auth.passport-authorize", "passport.authorization"),
    coverage_note=(
        "The request can be read in full; accepting it needs the Passport "
        "secure-value crypto Telethon does not provide (see `passport authorize`)."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# passport authorize — registered, and refused
# ---------------------------------------------------------------------------


class AuthorizeReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="peer", help="The service's bot.")]
    password: Annotated[
        str | None,
        opt(secret=True, envvar="TLGR_2FA_PASSWORD", help="The 2FA cloud password."),
    ] = None
    scope: Annotated[
        str | None, opt("--scope", metavar="JSON", kind="json", help="Scope JSON from the service.")
    ] = None
    public_key: Annotated[
        str | None,
        opt("--public-key", metavar="PATH", kind="path", help="The service's RSA key (PEM)."),
    ] = None
    values: Annotated[
        str | None,
        opt(
            "--values", metavar="JSON", kind="json", help="Which stored value satisfies each type."
        ),
    ] = None


async def authorize(ctx: OpContext, req: AuthorizeReq) -> PassportForm:
    """Refuse to share identity documents with a half-built crypto stack.

    `account.acceptAuthorization` takes a `secureCredentialsEncrypted` blob:
    the requested values, re-encrypted under the *service's* RSA key with a
    secret derived from the cloud password. Telethon builds none of that, and
    a wrong blob is not a failed command — it is either a service reading
    documents encrypted incorrectly, or an error message that nobody outside
    Telegram can diagnose.

    Exit 13 (NOT_SUPPORTED), not 6: nothing refused this, it was never asked.
    Read the request with `passport form get` and complete the share in an
    official client.
    """
    raise NotSupportedError(
        f"{_NO_CRYPTO} Inspect what the service wants with: tlgr passport form get {req.bot.value}"
    )


SPEC_AUTHORIZE = OperationSpec(
    id="passport.authorize",
    request=AuthorizeReq,
    response=PassportForm,
    impl=authorize,
    summary="Authorize a service with Telegram Passport (not supported: see the help)",
    description=(
        "Registered so the command exists and explains itself, and refused "
        "with NOT_SUPPORTED. Sharing identity documents through a crypto "
        "stack tlgr does not implement is worse than not offering it."
    ),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("bot",),
    example={"bot": 4242},
    example_args="passport authorize @examplebot",
    covers_partial=("passport.authorization",),
    coverage_note=(
        "The request is readable (`passport form get`); acceptance needs the "
        "Passport secure-value crypto and raises NOT_SUPPORTED."
    ),
    tags=frozenset({"not-supported"}),
)


# ---------------------------------------------------------------------------
# passport delete
# ---------------------------------------------------------------------------


class DeleteReq(Request):
    type: Annotated[
        tuple[str, ...],
        arg(0, metavar="TYPE", variadic=True, help="Document types to delete."),
    ]


async def delete(ctx: OpContext, req: DeleteReq) -> PassportDeletion:
    """Delete stored Passport documents. Needs no crypto, and is irreversible."""
    from telethon.tl.functions import account as fn

    if not req.type:
        raise UsageError(
            f"name the document types to delete: {', '.join(sorted(_auth.SECURE_VALUE_TYPES))}",
            field="type",
        )
    client = _auth.client(ctx)
    wanted = [_auth.secure_value_type(name) for name in req.type]
    await client(fn.DeleteSecureValueRequest(types=wanted))
    deleted = [_auth.secure_value_name(item) for item in wanted]
    ctx.emit("passport_deleted", {"types": deleted})
    return PassportDeletion(deleted=deleted)


SPEC_DELETE = OperationSpec(
    id="passport.delete",
    request=DeleteReq,
    response=PassportDeletion,
    impl=delete,
    summary="Delete stored Passport documents",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("deleted",),
    example={"deleted": ["passport"]},
    example_args="passport delete passport",
    covers=("passport.save-delete",),
    coverage_note="Saving a value needs the encryption stack; deleting one does not.",
)


# ---------------------------------------------------------------------------
# passport verify
# ---------------------------------------------------------------------------


class VerifyReq(Request):
    phone: Annotated[str | None, opt("--phone", help="Phone number to verify.")] = None
    email: Annotated[str | None, opt("--email", help="Email address to verify.")] = None
    code: Annotated[str | None, opt("--code", help="Code that arrived; omit to request one.")] = (
        None
    )
    code_hash: Annotated[
        str | None,
        opt("--code-hash", metavar="HASH", help="The hash the first call returned (--phone only)."),
    ] = None
    purpose: Annotated[
        str | None,
        choice("passport", "login-setup", "login-change", help="EmailVerifyPurpose."),
    ] = "passport"


async def verify(ctx: OpContext, req: VerifyReq) -> PassportVerification:
    """Verify an extra phone number or email.

    The code exchange is plain MTProto and works fully; only *storing* the
    result as a Passport secure value needs the encryption stack, which is
    why this command stops at "verified".
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if bool(req.phone) == bool(req.email):
        raise UsageError("give exactly one of --phone or --email", field="phone")

    if req.phone:
        if not req.code:
            sent = await client(
                fn.SendVerifyPhoneCodeRequest(
                    phone_number=req.phone, settings=_auth.code_settings()
                )
            )
            fields = _auth.sent_code_fields(sent)
            return PassportVerification(
                target=_auth.masked(req.phone),
                sent=True,
                code_length=fields.get("length"),
                code_hash=fields["code_hash"],
            )
        if not req.code_hash:
            raise UsageError(
                "pass --code-hash with the value the first call returned; Telegram will not "
                "accept a code without the hash that came with it",
                field="code_hash",
            )
        await client(
            fn.VerifyPhoneRequest(
                phone_number=req.phone,
                phone_code_hash=req.code_hash,
                phone_code=req.code,
            )
        )
        return PassportVerification(target=_auth.masked(req.phone), verified=True)

    purposes = {
        "passport": types.EmailVerifyPurposePassport,
        "login-setup": types.EmailVerifyPurposeLoginSetup,
        "login-change": types.EmailVerifyPurposeLoginChange,
    }
    if (req.purpose or "passport") != "passport":
        raise UsageError(
            "login-setup and login-change belong to the login flow: "
            "use `tlgr auth login-email set` or `tlgr account email set --kind login`",
            field="purpose",
        )
    purpose = purposes["passport"]()
    if not req.code:
        sent = await client(fn.SendVerifyEmailCodeRequest(purpose=purpose, email=req.email or ""))
        return PassportVerification(
            target=getattr(sent, "email_pattern", req.email) or "",
            sent=True,
            code_length=getattr(sent, "length", None),
        )
    verified = await client(
        fn.VerifyEmailRequest(
            purpose=purpose, verification=types.EmailVerificationCode(code=req.code)
        )
    )
    return PassportVerification(target=getattr(verified, "email", req.email) or "", verified=True)


SPEC_VERIFY = OperationSpec(
    id="passport.verify",
    request=VerifyReq,
    response=PassportVerification,
    impl=verify,
    summary="Verify an extra phone number or email",
    mutating=True,
    rate_class="send",
    columns=("target", "sent", "verified"),
    example={"target": "a**@e*****e.com", "sent": True, "code_length": 6},
    example_args="passport verify --email ada@example.com",
    covers=("auth.passport-verify-phone-email",),
    covers_partial=("account.verify-phone-email",),
    coverage_note="The recovery/login addresses are owned by `account email set`.",
)
