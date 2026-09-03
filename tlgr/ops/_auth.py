"""Plumbing shared by the `auth`, `account` and `passport` groups.

Four things live here because they are the same in fifteen places and getting
any of them wrong once is a security bug rather than a formatting one.

* **SRP.** Every `InputCheckPasswordSRP` method follows one shape: call with
  `inputCheckPasswordEmpty`, and when the server answers
  `PASSWORD_HASH_INVALID` ask for the password and retry. `SRP_ID_INVALID`
  means the challenge went stale and `account.getPassword` has to be
  refetched — a retry with the same `srp_id` fails forever. That loop is
  written once, in `with_password()`.
* **Code types.** `auth.sendCode` answers with one of eleven `sentCodeType*`
  constructors and each one asks the human to look somewhere different. The
  mapping to a stable lowercase name is here so that `auth send-code`,
  `auth resend-code` and `account phone set` all report it identically.
* **Secrets never widen.** A cloud password arrives from env/stdin/file and
  reaches exactly one call. Nothing here logs it, stores it or puts it in a
  model — and `PasswordState` has no field it could land in.
* **The daemon owns the session file.** A login opens a *pre-auth* client
  through `daemon.preauth`, never a second `TelegramClient` on a file the
  daemon may already hold: two live connections on one auth key is
  `AUTH_KEY_DUPLICATED`, and Telegram revokes the session rather than
  refusing the second one.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

from tlgr.core.errors import (
    AuthPasswordRequiredError,
    DaemonError,
    UsageError,
)
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.auth import Passkey, PasswordState, Session, WebSession

__all__ = [
    "SECURE_VALUE_TYPES",
    "accounts",
    "app_config",
    "client",
    "code_settings",
    "code_type",
    "empty_password",
    "get_password",
    "iso",
    "passkey_model",
    "password_state",
    "preauth",
    "resolve_alias",
    "secure_value_type",
    "session_model",
    "web_session_model",
    "with_password",
]

#: 24 hours. `SESSION_TOO_FRESH_X` and `PASSWORD_TOO_FRESH_X` both count to
#: this, and neither error says so.
FRESHNESS_WINDOW = timedelta(hours=24)

#: The default Telegram uses when `help.getAppConfig` carries no
#: `authorization_autoconfirm_period`.
DEFAULT_AUTOCONFIRM_PERIOD = 604800


# ---------------------------------------------------------------------------
# Context accessors
# ---------------------------------------------------------------------------


def client(ctx: Any) -> Any:
    """The connected Telethon client, or a daemon error saying why there is none."""
    found = getattr(ctx, "client", None)
    if found is None:  # pragma: no cover - the daemon always supplies one
        raise DaemonError("this operation needs a connected account")
    return found


def accounts(ctx: Any) -> Any:
    """The alias registry, rooted where this process's tlgr home is.

    Built from `ctx.paths` in the daemon and from `$TLGR_HOME` in the CLI, so
    a local operation and a daemon operation read the same `accounts.json`.
    """
    from tlgr.core.accounts import AccountManager

    base = getattr(getattr(ctx, "paths", None), "base", None)
    return AccountManager(base)


def preauth(ctx: Any) -> Any:
    """The daemon's pre-auth service — the only thing allowed to open a session."""
    service = getattr(getattr(ctx, "daemon", None), "preauth", None)
    if service is None:
        raise DaemonError(
            "logging in runs in the daemon, which owns the session file. "
            "Start it with: tlgr daemon start"
        )
    return service


def resolve_alias(ctx: Any, explicit: str | None = None) -> str:
    """Positional alias > `-a/--account` > the active account.

    An operation that names an account in its request must honour that name
    first: `tlgr account logout work` has to log *work* out even when the
    active account is something else.
    """
    from tlgr.core.errors import AccountRequiredError

    if explicit:
        from tlgr.core.paths import validate_alias

        return validate_alias(explicit)
    on_context = (getattr(ctx, "account", "") or "").strip()
    if on_context:
        return on_context
    active = accounts(ctx).get_active()
    if not active:
        raise AccountRequiredError("no account was given and none is active")
    return str(active)


def masked(phone: str) -> str:
    """`+989123456789` → `989…89`. A phone number is an identifier, not a label."""
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 6:
        return "***"
    return f"{digits[:3]}…{digits[-2:]}"


def iso(value: Any) -> str | None:
    """Any datetime-ish thing as RFC-3339 UTC."""
    return fmt_dt(value) if isinstance(value, datetime) else None


# ---------------------------------------------------------------------------
# Login codes
# ---------------------------------------------------------------------------

#: `sentCodeTypeX` → the name tlgr prints. Stable: an agent branches on it.
_CODE_TYPES = {
    "SentCodeTypeApp": "app",
    "SentCodeTypeSms": "sms",
    "SentCodeTypeSmsWord": "sms_word",
    "SentCodeTypeSmsPhrase": "sms_phrase",
    "SentCodeTypeCall": "call",
    "SentCodeTypeFlashCall": "flash_call",
    "SentCodeTypeMissedCall": "missed_call",
    "SentCodeTypeFragmentSms": "fragment",
    "SentCodeTypeEmailCode": "email",
    "SentCodeTypeSetUpEmailRequired": "setup_email_required",
    "SentCodeTypeFirebaseSms": "firebase",
    "CodeTypeSms": "sms",
    "CodeTypeCall": "call",
    "CodeTypeFlashCall": "flash_call",
    "CodeTypeMissedCall": "missed_call",
    "CodeTypeFragmentSms": "fragment",
}


def code_type(value: Any) -> str:
    """The stable lowercase name of a `sentCodeType*` / `codeType*` object."""
    if value is None:
        return ""
    name = type(value).__name__
    return _CODE_TYPES.get(name, name.removeprefix("SentCodeType").removeprefix("CodeType").lower())


def code_settings(
    *,
    current_number: bool = False,
    allow_flashcall: bool = False,
    allow_missed_call: bool = False,
    logout_tokens: list[bytes] | None = None,
) -> Any:
    """`codeSettings`, with `allow_app_hash` always off.

    `allow_app_hash` promises the server that the client can read the SMS
    itself; a CLI cannot, and claiming otherwise makes Telegram choose a
    delivery route nobody will ever see.
    """
    from telethon.tl import types

    return types.CodeSettings(
        current_number=current_number or None,
        allow_flashcall=allow_flashcall or None,
        allow_missed_call=allow_missed_call or None,
        logout_tokens=list(logout_tokens) if logout_tokens else None,
    )


def sent_code_fields(sent: Any) -> dict[str, Any]:
    """The type-specific half of a `auth.sentCode`, flattened for the model."""
    kind = getattr(sent, "type", None)
    fields: dict[str, Any] = {
        "type": code_type(kind),
        "next_type": code_type(getattr(sent, "next_type", None)) or None,
        "timeout": getattr(sent, "timeout", None),
        "code_hash": getattr(sent, "phone_code_hash", "") or "",
    }
    for name, target in (
        ("length", "length"),
        ("url", "fragment_url"),
        ("email_pattern", "email_pattern"),
        ("beginning", "beginning"),
        ("reset_available_period", "reset_available_period"),
    ):
        value = getattr(kind, name, None)
        if value is not None:
            fields[target] = value
    fields["google_signin_allowed"] = bool(getattr(kind, "google_signin_allowed", False))
    fields["apple_signin_allowed"] = bool(getattr(kind, "apple_signin_allowed", False))
    pending = getattr(kind, "reset_pending_date", None)
    if pending is not None:
        fields["reset_pending_date"] = iso(pending)
    return fields


# ---------------------------------------------------------------------------
# The cloud password (SRP)
# ---------------------------------------------------------------------------


async def get_password(caller: Any) -> Any:
    """`account.getPassword` — the challenge every SRP call is built from."""
    from telethon.tl.functions import account as fn

    return await caller(fn.GetPasswordRequest())


def empty_password() -> Any:
    """`inputCheckPasswordEmpty`: "I have no password", not "I forgot it"."""
    from telethon.tl import types

    return types.InputCheckPasswordEmpty()


def _srp(state: Any, secret: str | None) -> Any:
    if secret is None:
        return empty_password()
    from telethon import password as srp_module

    return srp_module.compute_check(state, secret)


async def with_password(caller: Any, build: Any, secret: str | None, *, state: Any = None) -> Any:
    """Run an `InputCheckPasswordSRP` request, refetching the challenge once.

    *build* takes the computed check object and returns the request. The
    retry exists because `srp_id` is single-use: a second attempt with a
    stale one answers `SRP_ID_INVALID` forever, and the only cure is a fresh
    `account.getPassword`.
    """
    current = state if state is not None else await get_password(caller)
    try:
        return await caller(build(_srp(current, secret)))
    except Exception as exc:
        message = str(exc)
        if "SRP_ID_INVALID" in message or "SRP_PASSWORD_CHANGED" in message:
            refreshed = await get_password(caller)
            return await caller(build(_srp(refreshed, secret)))
        if "PASSWORD_HASH_INVALID" in message and secret is None:
            raise AuthPasswordRequiredError(
                "this account has a cloud password; supply it with "
                "--password-env TLGR_2FA_PASSWORD (or --password-stdin/--password-file)"
            ) from exc
        raise


def new_password_settings(
    state: Any,
    *,
    new_password: str | None = None,
    hint: str | None = None,
    email: str | None = None,
) -> Any:
    """`passwordInputSettings` for set / change / remove.

    Removing is the same call with an empty `new_password_hash`, which is why
    there is no separate builder for it.
    """
    from telethon import password as srp_module
    from telethon.tl.types import account as account_types

    if new_password:
        digest = srp_module.compute_digest(state.new_algo, new_password)
        return account_types.PasswordInputSettings(
            new_algo=state.new_algo,
            new_password_hash=digest,
            hint=hint or "",
            email=email or None,
        )
    if hint is not None and new_password is None and email is None:
        return account_types.PasswordInputSettings(hint=hint)
    if email is not None and new_password is None:
        return account_types.PasswordInputSettings(email=email)
    # No new password: turn 2FA off.
    return account_types.PasswordInputSettings(
        new_algo=None, new_password_hash=b"", hint="", email=None
    )


def password_state(state: Any, settings: Any = None) -> PasswordState:
    """`account.Password` as the model, without a byte of SRP material."""
    created = getattr(state, "pending_reset_date", None)
    return PasswordState(
        has_password=bool(getattr(state, "has_password", False)),
        has_recovery=bool(getattr(state, "has_recovery", False)),
        has_secure_values=bool(getattr(state, "has_secure_values", False)),
        hint=getattr(state, "hint", None) or None,
        email_unconfirmed_pattern=getattr(state, "email_unconfirmed_pattern", None) or None,
        login_email_pattern=getattr(state, "login_email_pattern", None) or None,
        pending_reset_date=iso(created),
        recovery_email=getattr(settings, "email", None) if settings is not None else None,
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def app_config(caller: Any) -> dict[str, Any]:
    """`help.getAppConfig` as a plain dict, or `{}` when it cannot be read.

    Empty rather than raising: every caller uses it for a *derived* field
    (the auto-confirm deadline, a freeze appeal URL), and losing a derived
    field is not worth failing the command the user actually asked for.
    """
    from telethon.tl.functions import help as fn

    try:
        answer = await caller(fn.GetAppConfigRequest(hash=0))
    except Exception:
        return {}
    return _json_value(getattr(answer, "config", None)) or {}


def _json_value(node: Any) -> Any:
    """A `JSONValue` tree as builtins."""
    name = type(node).__name__
    if name == "JsonObject":
        return {item.key: _json_value(item.value) for item in node.value}
    if name == "JsonArray":
        return [_json_value(item) for item in node.value]
    if name in ("JsonString", "JsonNumber", "JsonBool"):
        return node.value
    if name == "JsonNull":
        return None
    return None


def session_model(
    auth: Any, *, ttl_days: int | None = None, autoconfirm_period: int = 0
) -> Session:
    """One `authorization` as a row, with the two deadlines nobody can guess.

    `deny_deadline` is when an unconfirmed login stops being deniable
    (Telegram confirms it for you), and `sensitive_actions_eligible_at` is
    what `SESSION_TOO_FRESH_X` counts down to.
    """
    created = getattr(auth, "date_created", None)
    period = autoconfirm_period or DEFAULT_AUTOCONFIRM_PERIOD
    deny = created + timedelta(seconds=period) if isinstance(created, datetime) else None
    eligible = created + FRESHNESS_WINDOW if isinstance(created, datetime) else None
    return Session(
        hash=str(getattr(auth, "hash", 0)),
        current=bool(getattr(auth, "current", False)),
        official_app=bool(getattr(auth, "official_app", False)),
        unconfirmed=bool(getattr(auth, "unconfirmed", False)),
        password_pending=bool(getattr(auth, "password_pending", False)),
        app_name=getattr(auth, "app_name", "") or "",
        app_version=getattr(auth, "app_version", "") or "",
        api_id=getattr(auth, "api_id", None),
        device_model=getattr(auth, "device_model", "") or "",
        platform=getattr(auth, "platform", "") or "",
        system_version=getattr(auth, "system_version", "") or "",
        ip=getattr(auth, "ip", "") or "",
        country=getattr(auth, "country", "") or "",
        region=getattr(auth, "region", "") or "",
        date_created=iso(created),
        date_active=iso(getattr(auth, "date_active", None)),
        call_requests_disabled=bool(getattr(auth, "call_requests_disabled", False)),
        encrypted_requests_disabled=bool(getattr(auth, "encrypted_requests_disabled", False)),
        deny_deadline=iso(deny) if getattr(auth, "unconfirmed", False) else None,
        sensitive_actions_eligible_at=iso(eligible),
        ttl_days=ttl_days,
    )


def web_session_model(auth: Any, users: dict[int, Any] | None = None) -> WebSession:
    bot_id = getattr(auth, "bot_id", None)
    bot = (users or {}).get(int(bot_id)) if bot_id else None
    return WebSession(
        hash=str(getattr(auth, "hash", 0)),
        bot=int(bot_id) if bot_id else None,
        bot_username=getattr(bot, "username", None),
        domain=getattr(auth, "domain", "") or "",
        browser=getattr(auth, "browser", "") or "",
        platform=getattr(auth, "platform", "") or "",
        ip=getattr(auth, "ip", "") or "",
        region=getattr(auth, "region", "") or "",
        date_created=iso(getattr(auth, "date_created", None)),
        date_active=iso(getattr(auth, "date_active", None)),
    )


def passkey_model(raw: Any) -> Passkey:
    created = getattr(raw, "date", None)
    return Passkey(
        id=str(getattr(raw, "id", "")),
        name=getattr(raw, "name", "") or "",
        date=iso(created),
        date_unix=to_unix(created) if isinstance(created, datetime) else None,
        last_usage_date=iso(getattr(raw, "last_usage_date", None)),
        software_emoji_id=getattr(raw, "software_emoji_id", None),
    )


def parse_hash(value: str, *, field: str = "hash") -> int:
    """A session hash is a signed 64-bit integer, however it was typed."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{value!r} is not a session hash", field=field) from exc


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Passport
# ---------------------------------------------------------------------------

#: The thirteen `secureValueType*` constructors, by the name a user types.
SECURE_VALUE_TYPES: dict[str, str] = {
    "personal_details": "SecureValueTypePersonalDetails",
    "passport": "SecureValueTypePassport",
    "driver_license": "SecureValueTypeDriverLicense",
    "identity_card": "SecureValueTypeIdentityCard",
    "internal_passport": "SecureValueTypeInternalPassport",
    "address": "SecureValueTypeAddress",
    "utility_bill": "SecureValueTypeUtilityBill",
    "bank_statement": "SecureValueTypeBankStatement",
    "rental_agreement": "SecureValueTypeRentalAgreement",
    "passport_registration": "SecureValueTypePassportRegistration",
    "temporary_registration": "SecureValueTypeTemporaryRegistration",
    "phone": "SecureValueTypePhone",
    "email": "SecureValueTypeEmail",
}


def secure_value_name(value: Any) -> str:
    """`SecureValueTypeDriverLicense` → `driver_license`."""
    raw = type(value).__name__.removeprefix("SecureValueType")
    out = []
    for index, character in enumerate(raw):
        if character.isupper() and index:
            out.append("_")
        out.append(character.lower())
    return "".join(out)


def secure_value_type(name: str) -> Any:
    """The constructor for a document type a user named on the command line."""
    from telethon.tl import types

    key = name.strip().lower().replace("-", "_")
    class_name = SECURE_VALUE_TYPES.get(key)
    if class_name is None:
        raise UsageError(
            f"unknown Passport document type {name!r}; "
            f"one of: {', '.join(sorted(SECURE_VALUE_TYPES))}",
            field="type",
        )
    return getattr(types, class_name)()


def b64(data: bytes | None) -> str:
    """URL-safe base64 without padding — how Telegram spells a login token."""
    if not data:
        return ""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    """The inverse, tolerant of the padding a copy-paste may have dropped."""
    cleaned = text.strip()
    if "token=" in cleaned:
        cleaned = cleaned.split("token=", 1)[1].split("&", 1)[0]
    padding = "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(cleaned + padding)
    except Exception as exc:
        raise UsageError(f"{text!r} is not a tg://login token", field="link") from exc


# ---------------------------------------------------------------------------
# Future auth tokens
# ---------------------------------------------------------------------------
#
# `auth.loggedOut.future_auth_token` lets the same device log back in without
# a code. It is a bearer credential: 0600 next to the session, capped at 20
# (Telegram's own limit), and dropped when the account is removed.

TOKEN_FILE = "future-auth-tokens"
MAX_TOKENS = 20


def token_path(manager: Any, alias: str) -> Any:
    return manager.paths.account_dir(alias) / TOKEN_FILE


def future_tokens(manager: Any, alias: str) -> list[bytes]:
    """The stored tokens, newest last. Unreadable or absent means none."""
    path = token_path(manager, alias)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").split()
    except OSError:
        return []
    out: list[bytes] = []
    for line in lines[-MAX_TOKENS:]:
        try:
            out.append(unb64(line))
        except Exception:
            continue
    return out


def store_future_token(manager: Any, alias: str, token: bytes | None) -> bool:
    """Append a token at 0600, evicting past the cap. Returns whether it stored."""
    if not token:
        return False
    from tlgr.core.paths import write_private

    path = token_path(manager, alias)
    existing = path.read_text(encoding="utf-8").split() if path.exists() else []
    write_private(path, "\n".join([*existing, b64(token)][-MAX_TOKENS:]))
    return True


def drop_future_tokens(manager: Any, alias: str) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        token_path(manager, alias).unlink(missing_ok=True)


def already(ctx: Any) -> None:
    """Flag `meta.already`: the world already looked the way the caller asked."""
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()
