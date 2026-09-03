"""The `auth` group: logging in without a human holding the terminal open.

v1's `account add` was one interactive process that sent a code, blocked on
`input()` while a person read their phone, and signed in from the same
`TelegramClient` — because Telethon keeps `phone_code_hash` in memory on the
client object. That shape cannot be scripted: an agent has no `input()`, and a
second process has lost the hash.

Here the login is a **sequence of ordinary commands**. The pending client and
its `phone_code_hash` live in the daemon (which owns the session file), and
`<account>/login-state.json` mirrors them at 0600 so the steps survive a
daemon restart:

    tlgr auth send-code +989…            # → {"type": "app", "code_hash": "…"}
    tlgr auth verify-code 12345 --password-env TLGR_2FA_PASSWORD

Every terminal state is a *state*, not a stack trace: `authorized`,
`password_required` (exit 4 — supply a password source and re-run), and
`signup_required` (a different command, deliberately).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta
from typing import Annotated, Any

from tlgr.core.errors import (
    AuthenticationError,
    AuthPasswordRequiredError,
    ConfigurationError,
    NotSupportedError,
    UsageError,
)
from tlgr.core.paths import validate_alias
from tlgr.core.timefmt import parse_duration
from tlgr.models.auth import (
    AccountDeletion,
    AutologinUrl,
    LoginCodes,
    LoginEmail,
    LoginResult,
    QrLogin,
    SentCode,
    Terms,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.ops import _auth
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [
    "SPEC_AUTOLOGIN_URL_GET",
    "SPEC_CODE_LIST",
    "SPEC_LOGIN_EMAIL_SET",
    "SPEC_QR",
    "SPEC_RECOVER",
    "SPEC_RESEND_CODE",
    "SPEC_RESET_ACCOUNT",
    "SPEC_SEND_CODE",
    "SPEC_SIGN_UP",
    "SPEC_TOS",
    "SPEC_VERIFY_CODE",
]

#: Telegram's own service account. Login codes for *this* account arrive here,
#: which is what lets one tlgr account onboard another unattended.
SERVICE_CHAT = 777000

#: A login code as it appears in a 777000 message.
_CODE_RE = re.compile(r"\b(\d{5,7})\b")

#: `t.me/login/12345` and `tg://login?code=12345` are both a code, pasted.
_CODE_LINK_RE = re.compile(r"(?:t\.me/login/|[?&]code=)([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _default_alias(phone: str) -> str:
    """The last six digits of the number, which is what v1 chose."""
    digits = "".join(character for character in phone if character.isdigit())
    return digits[-6:] or "default"


def _login_alias(ctx: OpContext, service: Any, explicit: str | None = None) -> str:
    """Which account a login step is about.

    `--alias` wins, then `-a/--account`, then the one login already in
    progress, then the active account. Beyond that it refuses to guess:
    guessing which account a code belongs to is how the wrong session gets
    logged out.
    """
    if explicit:
        return validate_alias(explicit)
    named = (getattr(ctx, "account", "") or "").strip()
    if named:
        return validate_alias(named)
    in_progress = [
        alias for alias in list(getattr(service, "_pending", {})) if service.pending(alias)
    ]
    if len(in_progress) == 1:
        return str(in_progress[0])
    if not in_progress:
        active = _auth.accounts(ctx).get_active()
        if active:
            return validate_alias(str(active))
    raise UsageError(
        f"which account? pass --alias (or -a): {len(in_progress)} logins are in progress",
        field="alias",
    )


async def _caller(ctx: OpContext, service: Any, alias: str) -> Any:
    """The client to talk to: the pending login's, else the live session's.

    `auth recover` is the one flow that runs on both sides of a login — the
    RPCs are identical whether the password was forgotten at the login screen
    or three months into a session.
    """
    pending = service.pending(alias)
    if pending is not None and pending.client is not None:
        return pending.client
    session = await _auth.sessions(ctx).ensure(alias)
    return await session.acquire(timeout=60)


def _credentials(
    ctx: OpContext, alias: str, api_id: int | None, api_hash: str | None
) -> tuple[int, str]:
    """`(api_id, api_hash)` from the flags, the account, the env or the config."""
    import os

    manager = _auth.accounts(ctx)
    stored_id, stored_hash = (None, None)
    if manager.get_account(alias) is not None:
        stored_id, stored_hash = manager.load_credentials(alias)
    resolved_id = api_id or stored_id
    resolved_hash = api_hash or stored_hash
    if not resolved_id:
        env = os.environ.get("TLGR_API_ID") or os.environ.get("TELEGRAM_API_ID")
        resolved_id = int(env) if env and env.isdigit() else None
    if not resolved_hash:
        resolved_hash = os.environ.get("TELEGRAM_API_HASH") or None
    if not resolved_id or not resolved_hash:
        raise ConfigurationError(
            "this account has no API credentials. Get them from my.telegram.org and pass "
            "--api-id with --api-hash-env (never on the command line)."
        )
    return int(resolved_id), str(resolved_hash)


def _pending_login(service: Any, alias: str, phone: str, code_hash: str) -> tuple[str, str]:
    """The phone and hash a step should use: the flags, else the stored state."""
    pending = service.pending(alias)
    state = service.read_state(alias)
    resolved_phone = phone or (pending.phone if pending else "") or str(state.get("phone", ""))
    resolved_hash = (
        code_hash
        or (pending.phone_code_hash if pending else "")
        or str(state.get("phone_code_hash", ""))
    )
    if not resolved_phone or not resolved_hash:
        raise UsageError(
            f"no login is in progress for {alias!r}. Run: tlgr auth send-code <phone>",
            field="alias",
        )
    return resolved_phone, resolved_hash


async def _authorized(ctx: OpContext, service: Any, alias: str, result: Any) -> LoginResult:
    """Turn an `auth.authorization` into the model, and hand over the session."""
    _auth.store_future_token(_auth.accounts(ctx), alias, getattr(result, "future_auth_token", None))
    finished = await service.finish(alias)
    return LoginResult(
        status="authorized",
        alias=alias,
        user_id=finished.get("user_id"),
        username=finished.get("username"),
        setup_password_required=bool(getattr(result, "setup_password_required", False)),
        otherwise_relogin_days=getattr(result, "otherwise_relogin_days", None),
    )


# ---------------------------------------------------------------------------
# auth send-code
# ---------------------------------------------------------------------------


class SendCodeReq(Request):
    phone: Annotated[str, arg(0, metavar="PHONE", help="The number to log in with.")]
    alias: Annotated[
        str | None,
        opt("--alias", help="Account alias to create or resume (default: the last 6 digits)."),
    ] = None
    api_id: Annotated[
        int | None, opt("--api-id", metavar="ID", help="api_id; else TLGR_API_ID, then the config.")
    ] = None
    api_hash: Annotated[
        str | None,
        opt(secret=True, envvar="TLGR_API_HASH", help="api_hash — never on the command line."),
    ] = None
    current_number: Annotated[
        bool, opt("--current-number", help="codeSettings.current_number: this device owns it.")
    ] = False
    allow_missed_call: Annotated[
        bool, opt("--allow-missed-call", help="Permit a missed-call code (read the caller id).")
    ] = False
    allow_flashcall: Annotated[
        bool, opt("--allow-flashcall", help="Permit a flash-call code; you type the number.")
    ] = False
    no_future_tokens: Annotated[
        bool, opt("--no-future-tokens", help="Do not offer stored tokens; force a real code.")
    ] = False
    recaptcha_token: Annotated[
        str | None, opt("--recaptcha-token", metavar="TOKEN", help="A reCAPTCHA token you solved.")
    ] = None
    test_dc: Annotated[bool, opt("--test-dc", help="Log in against the Telegram test DCs.")] = False


async def send_code(ctx: OpContext, req: SendCodeReq) -> SentCode:
    """Ask Telegram to send a login code, and remember what it answered.

    The `phone_code_hash` is written to `login-state.json` (0600) rather than
    kept in this process, which is the whole reason `auth verify-code` can be
    a separate command run half an hour later by something else.

    A third-party `api_id` usually gets `sentCodeTypeApp` — the code arrives
    in another logged-in Telegram session, not by SMS — so the type is
    reported verbatim instead of being described as "we texted you".
    """
    from telethon.tl.functions import auth as fn

    if req.recaptcha_token:
        raise NotSupportedError(
            "invokeWithReCaptcha is layer 229 and the pinned Telethon speaks 227; "
            "complete the challenge in an official client instead"
        )
    service = _auth.preauth(ctx)
    alias = validate_alias(req.alias or _default_alias(req.phone))
    api_id, api_hash = _credentials(ctx, alias, req.api_id, req.api_hash)

    manager = _auth.accounts(ctx)
    if manager.get_account(alias) is None:
        manager.add_account(alias)
    manager.save_credentials(api_id, api_hash, alias)

    client = await service.client_for(alias, api_id=api_id, api_hash=api_hash)
    tokens = [] if req.no_future_tokens else _auth.future_tokens(manager, alias)
    settings = _auth.code_settings(
        current_number=req.current_number,
        allow_flashcall=req.allow_flashcall,
        allow_missed_call=req.allow_missed_call,
        logout_tokens=tokens,
    )
    sent = await client(
        fn.SendCodeRequest(
            phone_number=req.phone, api_id=api_id, api_hash=api_hash, settings=settings
        )
    )
    if type(sent).__name__ == "SentCodeSuccess":
        # A stored future auth token matched: there is no code to type.
        await _authorized(ctx, service, alias, getattr(sent, "authorization", None))
        return SentCode(phone=_auth.masked(req.phone), account=alias, already=True, type="token")

    fields = _auth.sent_code_fields(sent)
    service.remember(
        alias,
        phone=req.phone,
        phone_code_hash=fields["code_hash"],
        code_type=fields["type"],
        test_dc=req.test_dc,
    )
    return SentCode(phone=_auth.masked(req.phone), account=alias, **fields)


SPEC_SEND_CODE = OperationSpec(
    id="auth.send-code",
    request=SendCodeReq,
    response=SentCode,
    impl=send_code,
    summary="Start a phone login: ask Telegram to send a login code",
    description=(
        "Writes the pending login (phone + phone_code_hash + code type) to "
        "`login-state.json` at 0600, so `auth verify-code` is a separate "
        "command in a separate process. `type` is reported verbatim — a "
        "third-party api_id normally gets `app`, meaning the code lands in "
        "another logged-in Telegram session rather than an SMS."
    ),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    timeout_s=120,
    columns=("account", "type", "code_hash", "timeout"),
    headers=("Account", "Code type", "Hash", "Timeout"),
    example={
        "phone": "989…89",
        "account": "456789",
        "type": "app",
        "code_hash": "5f2a…",
        "timeout": 60,
    },
    example_args="auth send-code +989123456789",
    covers=(
        "auth.api-credentials",
        "auth.code-type-call",
        "auth.code-type-fragment",
        "auth.code-type-sms-word-phrase",
        "auth.device-identity",
        "auth.future-auth-tokens",
        "auth.login-flood-limits",
        "auth.phone-banned",
        "auth.phone-login-send-code",
        "auth.recaptcha-verification",
        "auth.test-dc-login",
    ),
    covers_partial=("auth.code-type-app", "auth.code-type-sms"),
    coverage_note=(
        "The code type is owned here; typing the code is `auth verify-code` "
        "and switching delivery is `auth resend-code`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# auth verify-code
# ---------------------------------------------------------------------------


class VerifyCodeReq(Request):
    code: Annotated[
        str,
        arg(
            0,
            metavar="CODE",
            required=False,
            help="Digits, an SMS word/phrase, or a t.me/login/<code> link. '-' reads stdin.",
        ),
    ] = ""
    alias: Annotated[str | None, opt("--alias", help="Which pending login to finish.")] = None
    password: Annotated[
        str | None,
        opt(secret=True, envvar="TLGR_2FA_PASSWORD", help="The 2FA cloud password."),
    ] = None
    phone: Annotated[str | None, opt("--phone", help="Override the pending login state.")] = None
    code_hash: Annotated[
        str | None, opt("--code-hash", metavar="HASH", help="Override the stored phone_code_hash.")
    ] = None
    email_code: Annotated[
        str | None, opt("--email-code", help="Login-email code instead of a phone code.")
    ] = None
    google_token: Annotated[
        str | None, opt("--google-token", help="Google id-token for the login email.")
    ] = None
    apple_token: Annotated[
        str | None, opt("--apple-token", help="Apple id-token for the login email.")
    ] = None


def _normalise_code(raw: str) -> str:
    """Accept a pasted login link; refuse a QR token pasted by mistake.

    A word or phrase code is passed through untouched — validating it as
    digits is how a client breaks `sentCodeTypeSmsWord` for everyone.
    """
    import sys

    value = raw.strip()
    if value == "-":
        value = sys.stdin.read().strip()
    if "token=" in value:
        raise UsageError(
            "that is a QR login token, not a login code. "
            "Approve it with: tlgr account session accept-qr <link>",
            field="code",
        )
    found = _CODE_LINK_RE.search(value)
    return found.group(1) if found else value


async def verify_code(ctx: OpContext, req: VerifyCodeReq) -> LoginResult:
    """Submit the code, and the cloud password when the server asks for one.

    Three terminal states, all of them reportable: `authorized`,
    `password_required` (raised as exit 4, so an agent can add
    `--password-env` and re-run the identical command), and
    `signup_required`, which is a different command on purpose — tlgr never
    creates an account as a side effect of a failed login.
    """
    from telethon.tl import types
    from telethon.tl.functions import auth as fn

    service = _auth.preauth(ctx)
    alias = _login_alias(ctx, service, req.alias)
    phone, code_hash = _pending_login(service, alias, req.phone or "", req.code_hash or "")
    client = await service.client_for(alias)

    verification: Any = None
    if req.email_code:
        verification = types.EmailVerificationCode(code=req.email_code)
    elif req.google_token:
        verification = types.EmailVerificationGoogle(token=req.google_token)
    elif req.apple_token:
        verification = types.EmailVerificationApple(token=req.apple_token)
    elif not req.code:
        raise UsageError(
            "give the code, or --email-code/--google-token/--apple-token", field="code"
        )

    try:
        result = await client(
            fn.SignInRequest(
                phone_number=phone,
                phone_code_hash=code_hash,
                phone_code=_normalise_code(req.code) if req.code and not verification else None,
                email_verification=verification,
            )
        )
    except Exception as exc:
        if type(exc).__name__ != "SessionPasswordNeededError":
            raise
        result = await _sign_in_with_password(client, req.password)

    if type(result).__name__ == "AuthorizationSignUpRequired":
        terms = getattr(result, "terms_of_service", None)
        service.remember(alias, tos_id=getattr(getattr(terms, "id", None), "data", "") or "")
        return LoginResult(
            status="signup_required",
            alias=alias,
            tos_id=getattr(getattr(terms, "id", None), "data", None),
            hint="that number has no account. Register it with: tlgr auth sign-up --first-name …",
        )
    return await _authorized(ctx, service, alias, result)


async def _sign_in_with_password(client: Any, secret: str | None) -> Any:
    """The SRP half of a login, or exit 4 telling the caller how to supply it."""
    from telethon.tl.functions import auth as fn

    state = await _auth.get_password(client)
    if secret is None:
        raise AuthPasswordRequiredError(
            "this account has a cloud password. Re-run with --password-env TLGR_2FA_PASSWORD "
            + (f"(hint: {state.hint}) " if getattr(state, "hint", None) else "")
            + (
                "or recover it with: tlgr auth recover"
                if getattr(state, "has_recovery", False)
                else "— there is no recovery email on this account"
            )
        )
    return await _auth.with_password(
        client, lambda check: fn.CheckPasswordRequest(password=check), secret, state=state
    )


SPEC_VERIFY_CODE = OperationSpec(
    id="auth.verify-code",
    request=VerifyCodeReq,
    response=LoginResult,
    impl=verify_code,
    summary="Finish a pending login: submit the code and, if asked, the password",
    description=(
        "The password never appears in argv. SRP is recomputed against a "
        "fresh `account.getPassword` when the server answers SRP_ID_INVALID, "
        "and a word or phrase code is passed through unchanged — validating "
        "it as digits is how a client breaks `sentCodeTypeSmsWord`."
    ),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("status", "alias", "user_id", "username"),
    example={"status": "authorized", "alias": "work", "user_id": 4242, "username": "me"},
    example_args="auth verify-code 12345",
    covers=(
        "auth.2fa-login",
        "auth.code-type-app",
        "auth.login-code-deep-link",
        "auth.login-email-code",
        "auth.otp-code-input-hygiene",
    ),
    covers_partial=("password.setup-required-after-login",),
    coverage_note=(
        "The `setup_password_required` warning is reported here; the password "
        "itself is set with `account password set`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# auth resend-code
# ---------------------------------------------------------------------------


class ResendCodeReq(Request):
    alias: Annotated[str | None, opt("--alias", help="Which pending login.")] = None
    phone: Annotated[str | None, opt("--phone", help="Override the pending login state.")] = None
    code_hash: Annotated[
        str | None, opt("--code-hash", metavar="HASH", help="Override the stored hash.")
    ] = None
    reason: Annotated[
        str | None, opt("--reason", help="Device-verification failure reason for the server.")
    ] = None
    cancel: Annotated[bool, opt("--cancel", help="Cancel the pending code instead.")] = False
    report_missing: Annotated[
        bool, opt("--report-missing", help="Also call auth.reportMissingCode (needs --mnc).")
    ] = False
    mnc: Annotated[str | None, opt("--mnc", help="Mobile network code for --report-missing.")] = (
        None
    )


async def resend_code(ctx: OpContext, req: ResendCodeReq) -> SentCode:
    """Switch the code to the next delivery method, or cancel it.

    `SEND_CODE_UNAVAILABLE` means every delivery option is exhausted; the
    answer then is QR, not another resend, and the error says so.
    """
    from telethon.tl.functions import auth as fn

    service = _auth.preauth(ctx)
    alias = _login_alias(ctx, service, req.alias)
    phone, code_hash = _pending_login(service, alias, req.phone or "", req.code_hash or "")
    client = await service.client_for(alias)

    if req.cancel:
        await client(fn.CancelCodeRequest(phone_number=phone, phone_code_hash=code_hash))
        service.clear_state(alias)
        return SentCode(phone=_auth.masked(phone), account=alias, cancelled=True)

    if req.report_missing:
        if not req.mnc:
            raise UsageError(
                "--report-missing needs --mnc: a CLI has no SIM to read the network code from",
                field="mnc",
            )
        await client(
            fn.ReportMissingCodeRequest(phone_number=phone, phone_code_hash=code_hash, mnc=req.mnc)
        )

    sent = await client(
        fn.ResendCodeRequest(
            phone_number=phone, phone_code_hash=code_hash, reason=req.reason or None
        )
    )
    fields = _auth.sent_code_fields(sent)
    service.remember(alias, phone_code_hash=fields["code_hash"], code_type=fields["type"])
    return SentCode(phone=_auth.masked(phone), account=alias, **fields)


SPEC_RESEND_CODE = OperationSpec(
    id="auth.resend-code",
    request=ResendCodeReq,
    response=SentCode,
    impl=resend_code,
    summary="Resend the pending login code by the next method, or cancel it",
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("account", "type", "next_type", "timeout"),
    example={"account": "work", "type": "sms", "next_type": "call", "timeout": 120},
    example_args="auth resend-code --alias work",
    covers=(
        "auth.code-cancel",
        "auth.code-resend",
        "auth.code-type-sms",
        "auth.report-missing-code",
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# auth qr
# ---------------------------------------------------------------------------


class QrReq(Request):
    alias: Annotated[str | None, opt("--alias", help="Account alias to create.")] = None
    password: Annotated[
        str | None, opt(secret=True, envvar="TLGR_2FA_PASSWORD", help="The 2FA cloud password.")
    ] = None
    api_id: Annotated[
        int | None, opt("--api-id", metavar="ID", help="api_id for this account.")
    ] = None
    api_hash: Annotated[
        str | None, opt(secret=True, envvar="TLGR_API_HASH", help="api_hash for this account.")
    ] = None
    url_only: Annotated[
        bool, opt("--url-only", help="Print only the tg://login URL (pipe it into qrencode).")
    ] = False
    wait: Annotated[str, opt("--wait", metavar="DURATION", help="Give up after this long.")] = "5m"
    test_dc: Annotated[bool, opt("--test-dc", help="Use the Telegram test DCs.")] = False


async def qr(ctx: OpContext, req: QrReq) -> Any:
    """Stream QR login tokens until one is approved.

    QR is the login method that always works for a third-party `api_id`: the
    code path can answer `UPDATE_APP_TO_LOGIN`, which no amount of retrying
    fixes. Each token lives about thirty seconds, so this yields a frame per
    token and re-exports on expiry rather than printing one dead QR.

    `SESSION_PASSWORD_NEEDED` falls through to the same SRP path as
    `auth verify-code`, which is what makes `--password-env` enough to make a
    QR login unattended too.
    """
    service = _auth.preauth(ctx)
    alias = validate_alias(req.alias or (getattr(ctx, "account", "") or "").strip() or "qr")
    api_id, api_hash = _credentials(ctx, alias, req.api_id, req.api_hash)
    manager = _auth.accounts(ctx)
    if manager.get_account(alias) is None:
        manager.add_account(alias)
    manager.save_credentials(api_id, api_hash, alias)

    client = await service.client_for(alias, api_id=api_id, api_hash=api_hash)
    deadline = time.monotonic() + max(10.0, parse_duration(req.wait) or 300)
    while time.monotonic() < deadline:
        login = await client.qr_login(ignored_ids=service.except_ids())
        token = getattr(login, "token", b"")
        yield Page(
            items=[
                QrLogin(
                    url=login.url,
                    token=_auth.b64(token) if isinstance(token, bytes) else str(token),
                    expires=_auth.iso(getattr(login, "expires", None)),
                    status="pending",
                    alias=alias,
                    ascii=None if req.url_only else _render_qr(ctx, login.url),
                )
            ]
        )
        try:
            await login.wait(timeout=min(35.0, max(1.0, deadline - time.monotonic())))
        except (TimeoutError, asyncio.TimeoutError):
            continue
        except Exception as exc:
            if type(exc).__name__ != "SessionPasswordNeededError":
                raise
            await _sign_in_with_password(client, req.password)
        done = await _authorized(ctx, service, alias, None)
        yield Page(
            items=[QrLogin(status="authorized", alias=alias, user_id=done.user_id, url=login.url)]
        )
        return
    yield Page(items=[QrLogin(status="expired", alias=alias)])


def _render_qr(ctx: OpContext, url: str) -> str | None:
    """The QR as unicode half-blocks, when the optional encoder is installed.

    Degrading to `None` with a warning is deliberate: `tlgr[qr]` is an extra,
    and the URL alone is enough for `--url-only | qrencode`.
    """
    try:
        import qrcode
    except ImportError:
        ctx.warn("install tlgr[qr] for an inline QR, or pipe --url-only into qrencode")
        return None
    matrix = qrcode.QRCode(border=1)
    matrix.add_data(url)
    matrix.make(fit=True)
    grid = matrix.get_matrix()
    lines = []
    for top in range(0, len(grid), 2):
        upper, lower = grid[top], grid[top + 1] if top + 1 < len(grid) else [False] * len(grid[0])
        lines.append(
            "".join(
                {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}[
                    (bool(a), bool(b))
                ]
                for a, b in zip(upper, lower, strict=False)
            )
        )
    return "\n".join(lines)


SPEC_QR = OperationSpec(
    id="auth.qr",
    request=QrReq,
    response=Page[QrLogin],
    impl=qr,
    summary="Log in by QR code: print the tg://login token and wait for approval",
    description=(
        "Streams a frame per token and re-exports on expiry. Telethon follows "
        "`auth.loginTokenMigrateTo` to the target DC itself, which is the "
        "step a hand-rolled QR login usually forgets."
    ),
    mutating=True,
    stream=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    timeout_s=600,
    columns=("status", "url", "expires"),
    example={"items": [{"url": "tg://login?token=AQI…", "status": "pending", "alias": "work"}]},
    example_args="auth qr --alias work",
    covers=("auth.multi-account", "auth.qr-login-generate"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# auth sign-up
# ---------------------------------------------------------------------------


class SignUpReq(Request):
    first_name: Annotated[str, opt("--first-name", help="Required.")] = ""
    last_name: Annotated[str, opt("--last-name", help="Optional.")] = ""
    alias: Annotated[str | None, opt("--alias", help="Which pending login.")] = None
    accept_tos: Annotated[
        bool, opt("--accept-tos", help="Accept the Terms of Service returned by auth.signIn.")
    ] = False
    no_joined_notifications: Annotated[
        bool, opt("--no-joined-notifications", help="Do not tell contacts that you joined.")
    ] = False


async def sign_up(ctx: OpContext, req: SignUpReq) -> LoginResult:
    """Register a new account for a number whose code was already verified.

    Only reachable after `auth verify-code` answered `signup_required`, and
    only with `--accept-tos`: accepting Terms of Service is a legal act and
    tlgr never performs one implicitly (ARCHITECTURE §1.2 — no *silent*
    account creation).
    """
    from telethon.tl import types
    from telethon.tl.functions import auth as fn
    from telethon.tl.functions import help as help_fn

    if not req.first_name:
        raise UsageError("--first-name is required to register an account", field="first_name")
    if not req.accept_tos:
        raise UsageError(
            "registering an account means accepting Telegram's Terms of Service. "
            "Read them with `tlgr auth tos` and pass --accept-tos.",
            field="accept_tos",
        )
    service = _auth.preauth(ctx)
    alias = _login_alias(ctx, service, req.alias)
    phone, code_hash = _pending_login(service, alias, "", "")
    client = await service.client_for(alias)

    result = await client(
        fn.SignUpRequest(
            phone_number=phone,
            phone_code_hash=code_hash,
            first_name=req.first_name,
            last_name=req.last_name,
            no_joined_notifications=req.no_joined_notifications or None,
        )
    )
    tos_id = str(service.read_state(alias).get("tos_id") or "")
    if tos_id:
        await client(help_fn.AcceptTermsOfServiceRequest(id=types.DataJSON(data=tos_id)))
    finished = await _authorized(ctx, service, alias, result)
    finished.tos_id = tos_id or None
    return finished


SPEC_SIGN_UP = OperationSpec(
    id="auth.sign-up",
    request=SignUpReq,
    response=LoginResult,
    impl=sign_up,
    summary="Register a new account for a phone whose code was already verified",
    description=(
        "A separate command, never a fallback: a login that finds no account "
        "stops with `signup_required` rather than creating one. Third-party "
        "api_ids often get PHONE_NUMBER_APP_SIGNUP_FORBIDDEN, reported as-is."
    ),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("status", "alias", "user_id"),
    example={"status": "authorized", "alias": "work", "user_id": 4242},
    example_args="auth sign-up --first-name Ada --accept-tos",
    covers=("auth.sign-up", "auth.signup-notify-contacts"),
)


# ---------------------------------------------------------------------------
# auth recover
# ---------------------------------------------------------------------------


class RecoverReq(Request):
    code: Annotated[
        str | None, opt("--code", help="Recovery code from the email; omit to request one.")
    ] = None
    alias: Annotated[str | None, opt("--alias", help="Which pending login (when logged out).")] = (
        None
    )
    check_only: Annotated[
        bool, opt("--check-only", help="Validate the code without consuming it.")
    ] = False
    new_password: Annotated[
        str | None,
        opt(
            secret=True,
            envvar="TLGR_2FA_NEW_PASSWORD",
            help="Replacement password; omitted removes the password.",
        ),
    ] = None
    hint: Annotated[str | None, opt("--hint", help="Hint for the new password.")] = None


async def recover(ctx: OpContext, req: RecoverReq) -> LoginResult:
    """Recover a forgotten cloud password through the recovery email.

    Works at a login *and* while logged in, because the RPCs are the same
    three. `PASSWORD_RECOVERY_NA` means there is no recovery email at all —
    the remaining doors are `account password reset` (7 days, keeps the
    account) and `auth reset-account` (immediate, deletes it).
    """
    from telethon.tl.functions import auth as fn

    service = _auth.preauth(ctx)
    alias = _login_alias(ctx, service, req.alias)
    pending = service.pending(alias) is not None
    caller = await _caller(ctx, service, alias)

    if not req.code:
        answer = await caller(fn.RequestPasswordRecoveryRequest())
        return LoginResult(
            status="code_sent",
            alias=alias,
            email_pattern=getattr(answer, "email_pattern", None),
        )

    if req.check_only:
        await caller(fn.CheckRecoveryPasswordRequest(code=req.code))
        return LoginResult(status="code_valid", alias=alias)

    settings = None
    if req.new_password:
        state = await _auth.get_password(caller)
        settings = _auth.new_password_settings(
            state, new_password=req.new_password, hint=req.hint or ""
        )
    result = await caller(fn.RecoverPasswordRequest(code=req.code, new_settings=settings))
    if pending and type(result).__name__ == "Authorization":
        return await _authorized(ctx, service, alias, result)
    return LoginResult(status="recovered", alias=alias)


SPEC_RECOVER = OperationSpec(
    id="auth.recover",
    request=RecoverReq,
    response=LoginResult,
    impl=recover,
    summary="Recover a forgotten cloud password through the recovery email",
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("status", "email_pattern"),
    example={"status": "code_sent", "email_pattern": "a**@e*****e.com"},
    example_args="auth recover",
    covers=("auth.2fa-login-recover-email", "password.forgot-recover-logged-in"),
)


# ---------------------------------------------------------------------------
# auth reset-account
# ---------------------------------------------------------------------------


class ResetAccountReq(Request):
    alias: Annotated[str | None, opt("--alias", help="Which pending login.")] = None
    reason: Annotated[str, opt("--reason", help="Free-text reason sent to the server.")] = (
        "Forgot password"
    )
    status: Annotated[
        bool, opt("--status", help="Only report a pending reset and its remaining wait.")
    ] = False
    confirm_phone: Annotated[
        str | None, opt("--confirm-phone", metavar="PHONE", help="Retype the number — required.")
    ] = None


async def reset_account(ctx: OpContext, req: ResetAccountReq) -> AccountDeletion:
    """Delete an account nobody can log into any more. The last resort.

    Irreversible, and gated three ways: `--yes`, the phone number typed back,
    and the server's own `2FA_CONFIRM_WAIT_X` countdown, which is persisted
    so a later run reports how much of the wait is left instead of starting
    it again.
    """
    from telethon.tl.functions import account as fn

    service = _auth.preauth(ctx)
    alias = _login_alias(ctx, service, req.alias)
    state = service.read_state(alias)
    phone = str(state.get("phone", ""))

    if req.status:
        wait = int(state.get("reset_wait", 0) or 0)
        until = state.get("reset_until")
        return AccountDeletion(
            status="pending" if wait else "none",
            wait_seconds=wait or None,
            until=str(until) if until else None,
        )
    if not req.confirm_phone or _digits(req.confirm_phone) != _digits(phone):
        raise UsageError(
            "pass --confirm-phone with the account's own number: this deletes the account",
            field="confirm_phone",
        )
    client = await service.client_for(alias)
    try:
        await client(fn.DeleteAccountRequest(reason=req.reason, password=None))
    except Exception as exc:
        remaining = _wait_seconds(str(exc))
        if remaining is None:
            raise
        until = _auth.iso(_auth.now() + timedelta(seconds=remaining))
        service.remember(alias, reset_wait=remaining, reset_until=until or "")
        return AccountDeletion(
            status="wait",
            wait_seconds=remaining,
            until=until,
            confirm_hint=(
                "Telegram sent a confirmation link to the number. Cancel the reset with: "
                "tlgr account phone set --confirm-hash <hash from the tg://confirmphone link>"
            ),
        )
    service.clear_state(alias)
    return AccountDeletion(deleted=True, status="deleted")


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _wait_seconds(message: str) -> int | None:
    found = re.search(r"2FA_CONFIRM_WAIT_(\d+)", message)
    return int(found.group(1)) if found else None


SPEC_RESET_ACCOUNT = OperationSpec(
    id="auth.reset-account",
    request=ResetAccountReq,
    response=AccountDeletion,
    impl=reset_account,
    summary="Delete an account you can no longer log into (last resort)",
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("status", "wait_seconds", "until"),
    example={"status": "wait", "wait_seconds": 604800, "until": "2026-09-10T09:14:07Z"},
    example_args="auth reset-account --confirm-phone +989123456789",
    covers=("auth.2fa-login-reset-account",),
)


# ---------------------------------------------------------------------------
# auth tos
# ---------------------------------------------------------------------------


class TosReq(Request):
    accept: Annotated[bool, opt("--accept", help="Accept the pending Terms of Service.")] = False
    decline: Annotated[bool, opt("--decline", help="Decline — this deletes the account.")] = False
    delete_account: Annotated[
        bool, opt("--delete-account", help="Acknowledge that declining deletes the account.")
    ] = False
    confirm_age: Annotated[
        int | None, opt("--confirm-age", metavar="YEARS", help="Confirm your age when asked.")
    ] = None


async def tos(ctx: OpContext, req: TosReq) -> Terms:
    """Show, accept or decline the Terms of Service.

    Acceptance is a legal act, so it is never implicit: reading is the
    default and `--accept` is a separate run. Declining calls
    `account.deleteAccount`, which is why it needs `--decline`,
    `--delete-account` and `--yes` together.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as account_fn
    from telethon.tl.functions import help as fn

    from tlgr.ops._serialize import message_entities

    client = _auth.client(ctx)
    update = await client(fn.GetTermsOfServiceUpdateRequest())
    terms = getattr(update, "terms_of_service", None)
    available = terms is not None
    model = Terms(
        update_available=available,
        expires=_auth.iso(getattr(update, "expires", None)),
        id=getattr(getattr(terms, "id", None), "data", None),
        text=getattr(terms, "text", "") or "",
        entities=message_entities(terms) if available else [],
        popup=bool(getattr(terms, "popup", False)),
        min_age_confirm=getattr(terms, "min_age_confirm", None),
    )
    if not (req.accept or req.decline):
        return model
    if not available:
        _auth.already(ctx)
        return model
    if req.accept:
        minimum = model.min_age_confirm
        if minimum and (req.confirm_age or 0) < minimum:
            raise UsageError(
                f"these terms need an age confirmation: pass --confirm-age {minimum} or more",
                field="confirm_age",
            )
        await client(fn.AcceptTermsOfServiceRequest(id=types.DataJSON(data=model.id or "")))
        model.accepted = True
        return model
    if not req.delete_account:
        raise UsageError(
            "declining the Terms of Service deletes the account; "
            "pass --delete-account to acknowledge that",
            field="delete_account",
        )
    await client(account_fn.DeleteAccountRequest(reason="Decline ToS update", password=None))
    model.declined = True
    return model


SPEC_TOS = OperationSpec(
    id="auth.tos",
    request=TosReq,
    response=Terms,
    impl=tos,
    summary="Show, accept or decline the Terms of Service",
    aliases=("account.terms",),
    description=(
        "The daemon polls `help.getTermsOfServiceUpdate` at the returned "
        "`expires` and flags a pending update in its status; accepting is "
        "always an explicit run of this command."
    ),
    mutating=True,
    rate_class="read",
    columns=("update_available", "id", "accepted"),
    example={"update_available": False, "expires": "2026-09-10T09:14:07Z"},
    example_args="auth tos",
    covers=("auth.terms-of-service", "updates.config-terms-of-service"),
)


# ---------------------------------------------------------------------------
# auth login-email set
# ---------------------------------------------------------------------------


class LoginEmailReq(Request):
    email: Annotated[
        str | None,
        arg(
            0, metavar="EMAIL", required=False, help="Address to attach; omit with --code/--reset."
        ),
    ] = None
    alias: Annotated[str | None, opt("--alias", help="Which pending login.")] = None
    code: Annotated[str | None, opt("--code", help="Verification code from the address.")] = None
    google_token: Annotated[
        str | None, opt("--google-token", help="Google id-token instead of a code.")
    ] = None
    apple_token: Annotated[
        str | None, opt("--apple-token", help="Apple id-token instead of a code.")
    ] = None
    reset: Annotated[
        bool, opt("--reset", help="Start auth.resetLoginEmail for an address you cannot read.")
    ] = False


async def login_email_set(ctx: OpContext, req: LoginEmailReq) -> LoginEmail:
    """Set, verify or reset the login email the server demands mid-login.

    tlgr cannot run the Google or Apple consent screen — no browser, and no
    business holding one — so `--google-token`/`--apple-token` only *forward*
    a token the user obtained themselves, and only when the sent code
    advertised that it would be accepted.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn
    from telethon.tl.functions import auth as auth_fn

    service = _auth.preauth(ctx)
    alias = _login_alias(ctx, service, req.alias)
    phone, code_hash = _pending_login(service, alias, "", "")
    client = await service.client_for(alias)
    purpose = types.EmailVerifyPurposeLoginSetup(phone_number=phone, phone_code_hash=code_hash)

    if req.reset:
        sent = await client(
            auth_fn.ResetLoginEmailRequest(phone_number=phone, phone_code_hash=code_hash)
        )
        fields = _auth.sent_code_fields(sent)
        service.remember(alias, phone_code_hash=fields["code_hash"])
        return LoginEmail(sent_code=True, email_pattern=fields.get("email_pattern"))

    verification: Any = None
    if req.code:
        verification = types.EmailVerificationCode(code=req.code)
    elif req.google_token:
        verification = types.EmailVerificationGoogle(token=req.google_token)
    elif req.apple_token:
        verification = types.EmailVerificationApple(token=req.apple_token)

    if verification is None:
        if not req.email:
            raise UsageError("give an email address, or --code to verify one", field="email")
        sent = await client(fn.SendVerifyEmailCodeRequest(purpose=purpose, email=req.email))
        return LoginEmail(
            sent_code=True,
            email_pattern=getattr(sent, "email_pattern", None),
            length=getattr(sent, "length", None),
        )

    verified = await client(fn.VerifyEmailRequest(purpose=purpose, verification=verification))
    sent_code = getattr(verified, "sent_code", None)
    if sent_code is not None:
        # `account.emailVerifiedLogin` carries a fresh code for the login that
        # is still pending; it replaces the stored hash.
        service.remember(alias, phone_code_hash=getattr(sent_code, "phone_code_hash", ""))
    return LoginEmail(verified=True, email_pattern=getattr(verified, "email", None))


SPEC_LOGIN_EMAIL_SET = OperationSpec(
    id="auth.login-email.set",
    request=LoginEmailReq,
    response=LoginEmail,
    impl=login_email_set,
    summary="Set, verify or reset the login email the server demands during login",
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("email_pattern", "verified", "sent_code"),
    example={"email_pattern": "a**@e*****e.com", "sent_code": True},
    example_args="auth login-email set ada@example.com",
    covers=(
        "auth.login-email-change",
        "auth.login-email-google-apple-signin",
        "auth.login-email-reset",
        "auth.login-email-setup-required",
    ),
)


# ---------------------------------------------------------------------------
# auth code list
# ---------------------------------------------------------------------------


class CodeListReq(Request):
    wait: Annotated[bool, opt("--wait", help="Block until a fresh code arrives.")] = False
    wait_timeout: Annotated[
        str, opt("--wait-timeout", metavar="DURATION", help="Give up waiting after this long.")
    ] = "2m"
    scan: Annotated[
        int, opt("--limit", metavar="N", help="How many service messages to scan.", ge=1, le=100)
    ] = 10
    invalidate: Annotated[
        tuple[str, ...],
        opt("--invalidate", metavar="CODE", help="Invalidate these codes (repeatable)."),
    ] = ()


async def code_list(ctx: OpContext, req: CodeListReq) -> LoginCodes:
    """Read the login codes Telegram delivered into this account's 777000 chat.

    This is what makes scripted multi-account onboarding possible: account B
    reads the code Telegram sent for account A's new login. `--invalidate`
    burns a code that has leaked — the same hardening any client that reads
    this chat should do.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if req.invalidate:
        await client(fn.InvalidateSignInCodesRequest(codes=list(req.invalidate)))

    deadline = time.monotonic() + ((parse_duration(req.wait_timeout) or 120) if req.wait else 0)
    seen: list[str] = []
    texts: list[str] = []
    while True:
        texts = []
        seen = []
        for message in await client.get_messages(SERVICE_CHAT, limit=req.scan):
            body = getattr(message, "message", "") or ""
            if not body:
                continue
            texts.append(body)
            seen.extend(_CODE_RE.findall(body))
        if seen or not req.wait or time.monotonic() >= deadline:
            break
        await asyncio.sleep(2.0)
    return LoginCodes(codes=seen, messages=texts, invalidated=list(req.invalidate))


SPEC_CODE_LIST = OperationSpec(
    id="auth.code.list",
    request=CodeListReq,
    response=LoginCodes,
    impl=code_list,
    summary="Read login codes Telegram delivered to this session, and burn leaked ones",
    mutating=False,
    rate_class="read",
    timeout_s=300,
    columns=("codes",),
    example={"codes": ["12345"], "messages": ["Login code: 12345. Do not give this code…"]},
    example_args="auth code list",
    covers=("auth.login-codes-from-service-chat", "privacy.invalidate-sign-in-codes"),
    tags=frozenset({"agent-safe", "mutating-checked"}),
)


# ---------------------------------------------------------------------------
# auth autologin-url get
# ---------------------------------------------------------------------------


class AutologinReq(Request):
    url: Annotated[str, arg(0, metavar="URL", help="A telegram.org URL to sign into.")]


async def autologin_url_get(ctx: OpContext, req: AutologinReq) -> AutologinUrl:
    """Append `autologin_token` to a telegram.org URL so it opens signed in.

    Refused outside `help.getAppConfig.autologin_domains`: the token is a
    bearer credential and handing it to an arbitrary host is handing over the
    account's web session.
    """
    from urllib.parse import urlparse, urlunparse

    client = _auth.client(ctx)
    config = await _auth.app_config(client)
    domains = [str(d) for d in (config.get("autologin_domains") or [])]
    parsed = urlparse(req.url if "://" in req.url else f"https://{req.url}")
    host = (parsed.hostname or "").lower()
    allowed = any(host == d.lower() or host.endswith(f".{d.lower()}") for d in domains)
    if not allowed:
        raise UsageError(
            f"{host or req.url!r} is not in autologin_domains ({', '.join(domains) or 'none'}); "
            "the autologin token is a bearer credential and is never sent elsewhere",
            field="url",
        )
    token = str(config.get("autologin_token") or "")
    if not token:
        raise AuthenticationError("the server issued no autologin_token for this account")
    query = (
        f"{parsed.query}&autologin_token={token}" if parsed.query else f"autologin_token={token}"
    )
    return AutologinUrl(url=urlunparse(parsed._replace(query=query)), domain_allowed=True)


SPEC_AUTOLOGIN_URL_GET = OperationSpec(
    id="auth.autologin-url.get",
    request=AutologinReq,
    response=AutologinUrl,
    impl=autologin_url_get,
    summary="Append the autologin token to a telegram.org URL",
    rate_class="read",
    columns=("url", "domain_allowed"),
    example={"url": "https://telegram.org/faq?autologin_token=…", "domain_allowed": True},
    example_args="auth autologin-url get https://telegram.org/faq",
    covers=("auth.autologin-token",),
)
