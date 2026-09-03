"""The shapes the auth, account, session, password and passport groups emit.

Three of them deserve a note, because their fields are the whole point.

* `SentCode` is a *resumable* login. Telegram's `auth.sendCode` answers with a
  `phone_code_hash` that the next call needs, and a code *type* that decides
  what the human should be looking at (another Telegram session, an SMS, a
  Fragment page, an email). v1 kept both in memory and therefore had to hold
  one process open across a human reading their phone; here they are a
  response, so `auth send-code` and `auth verify-code` are two commands.
* `PasswordState` is the read side of 2-step verification and it deliberately
  carries **no** SRP material. `srp_B`, `secure_random` and the KDF salts are
  live cryptographic parameters; printing them buys nothing and leaks the
  shape of the exchange, so they stop at the operation boundary.
* `Session` is one row of the Devices list. `deny_deadline` and
  `sensitive_actions_eligible_at` are derived here rather than left to the
  reader: "unconfirmed" is only actionable while the auto-confirm window is
  open, and `SESSION_TOO_FRESH_X` is a 24-hour rule nobody can guess from
  `date_created` alone.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.message import MessageEntity

__all__ = [
    "AccountDeletion",
    "AccountRecord",
    "AccountState",
    "AccountTtl",
    "AutologinUrl",
    "DeviceLock",
    "LoginCodes",
    "LoginEmail",
    "LoginResult",
    "Passkey",
    "PassportDeletion",
    "PassportForm",
    "PassportRequirement",
    "PassportValue",
    "PassportVerification",
    "PasswordReset",
    "PasswordState",
    "PhoneChange",
    "QrLogin",
    "RecoveryEmail",
    "SentCode",
    "Session",
    "SessionChange",
    "SessionTermination",
    "SmsJobs",
    "Suggestion",
    "SupportInfo",
    "TempPassword",
    "Terms",
    "WebSession",
    "WebSessionRevocation",
]


# ---------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------


class SentCode(Model):
    """What `auth.sendCode` (and the two phone-change flows) answered.

    `type` is reported verbatim — `app`, `sms`, `sms_word`, `sms_phrase`,
    `call`, `flash_call`, `missed_call`, `fragment`, `email`,
    `setup_email_required` — because each one asks the human to look
    somewhere different, and "we sent you a code" is not enough instruction
    for any of them.
    """

    phone: str = ""
    code_hash: str = ""
    type: str = ""
    length: int | None = None
    next_type: str | None = None
    timeout: int | None = None
    #: `sentCodeTypeFragmentSms.url` — the page the code is waiting on.
    fragment_url: str | None = None
    email_pattern: str | None = None
    #: The first characters of a word/phrase SMS, so the human can tell which
    #: message is the login one.
    beginning: str | None = None
    google_signin_allowed: bool = False
    apple_signin_allowed: bool = False
    reset_available_period: int | None = None
    reset_pending_date: str | None = None
    #: A `auth.sentCodeSuccess`: a stored future auth token matched and there
    #: is no code to type at all.
    already: bool = False
    account: str = ""
    cancelled: bool = False
    changed: bool = False


class LoginResult(Model):
    """The terminal state of a login attempt.

    `status` is one of `authorized`, `password_required`, `signup_required`.
    The last two are *not* failures of the command, they are the next step,
    which is why they carry the hint and the recovery pattern an agent needs
    to take it.
    """

    status: str = ""
    user_id: int | None = None
    username: str | None = None
    alias: str = ""
    #: `auth.authorization.setup_password_required`: Telegram will log this
    #: session out after `otherwise_relogin_days` unless a cloud password is
    #: set, which is a loud thing to be quiet about.
    setup_password_required: bool = False
    otherwise_relogin_days: int | None = None
    hint: str | None = None
    has_recovery: bool = False
    email_pattern: str | None = None
    tos_id: str | None = None


class QrLogin(Model):
    """One frame of a QR login: the token to show, and what happened to it."""

    url: str = ""
    token: str = ""
    expires: str | None = None
    status: str = ""
    user_id: int | None = None
    alias: str = ""
    ascii: str | None = None
    png: str | None = None


class LoginEmail(Model):
    """The login email the server demands during a pending login."""

    email_pattern: str | None = None
    verified: bool = False
    sent_code: bool = False
    reset_pending_date: str | None = None
    length: int | None = None


class LoginCodes(Model):
    """Login codes Telegram delivered into the 777000 service chat."""

    codes: list[str] = []
    messages: list[str] = []
    invalidated: list[str] = []


class Terms(Model):
    """A Terms-of-Service document and what was done about it."""

    id: str | None = None
    text: str = ""
    entities: list[MessageEntity] = []
    min_age_confirm: int | None = None
    popup: bool = False
    expires: str | None = None
    update_available: bool = False
    accepted: bool = False
    declined: bool = False


class AccountDeletion(Model):
    """`account.deleteAccount`, whether it happened or started a countdown."""

    deleted: bool = False
    status: str = ""
    wait_seconds: int | None = None
    until: str | None = None
    confirm_hint: str | None = None


# ---------------------------------------------------------------------------
# The local account record
# ---------------------------------------------------------------------------


class AccountRecord(Model):
    """One row of `account list`.

    `alias` and `name` are both here on purpose: v1 printed the alias under
    the key `alias` and the display name under `name`, and §12.4 says a
    documented key does not move.
    """

    alias: str = ""
    name: str = ""
    user_id: int | None = None
    username: str | None = None
    phone: str | None = None
    active: bool = False
    connected: bool = False
    state: str = "unknown"
    kind: str = "user"
    created_at: str | None = None


class AccountState(Model):
    """`account info` / `account check` / `account switch` / `account rename`.

    One struct for the whole local-record surface: the commands differ in
    which subset they fill, and a reader that learns the field names once can
    read all of them.
    """

    alias: str = ""
    account: str = ""
    ok: bool = False
    already: bool = False
    removed: bool = False
    server_logout: bool = False
    logged_out: bool = False
    future_auth_token_stored: bool = False
    imported: bool = False
    authorized: bool = False
    old: str | None = None
    new: str | None = None
    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    phone: str | None = None
    dc_id: int | None = None
    premium: bool = False
    kind: str = "user"
    test_dc: bool = False
    session_path: str | None = None
    created_at: str | None = None
    #: `account check` only.
    state: str = ""
    error: str | None = None
    frozen_since: str | None = None
    frozen_until: str | None = None
    appeal_url: str | None = None
    hint: str | None = None
    #: `account sync` only.
    dialogs: int | None = None
    users: int | None = None
    chats: int | None = None
    pts: int | None = None
    #: `account export` only.
    format: str | None = None
    path: str | None = None
    session: str | None = None


class AccountTtl(Model):
    """The self-destruct timer, in days."""

    days: int = 0


class DeviceLock(Model):
    locked_for: int = 0


# ---------------------------------------------------------------------------
# Sessions (the Devices list)
# ---------------------------------------------------------------------------


class Session(Model):
    """One authorization — a device logged into this account."""

    hash: str = ""
    current: bool = False
    official_app: bool = False
    unconfirmed: bool = False
    password_pending: bool = False
    app_name: str = ""
    app_version: str = ""
    api_id: int | None = None
    device_model: str = ""
    platform: str = ""
    system_version: str = ""
    ip: str = ""
    country: str = ""
    region: str = ""
    date_created: str | None = None
    date_active: str | None = None
    call_requests_disabled: bool = False
    encrypted_requests_disabled: bool = False
    #: `date_created + authorization_autoconfirm_period`: how long a cron
    #: security check still has to deny an unrecognised login.
    deny_deadline: str | None = None
    #: `date_created + 24 h`. What `SESSION_TOO_FRESH_X` is counting down to.
    sensitive_actions_eligible_at: str | None = None
    #: Account-wide, repeated on every row so one `--json` read answers it.
    ttl_days: int | None = None
    #: A business bot connected to the account, which official clients show
    #: in the same list.
    bot: bool = False
    bot_username: str | None = None


class SessionChange(Model):
    hash: str = ""
    confirmed: bool = False
    already: bool = False
    call_requests_disabled: bool | None = None
    encrypted_requests_disabled: bool | None = None
    authorization_ttl_days: int | None = None
    #: `account session accept-qr` — the authorization that was just created.
    device_model: str | None = None
    app_name: str | None = None
    ip: str | None = None
    country: str | None = None


class SessionTermination(Model):
    terminated: int = 0
    hashes: list[str] = []
    advice: str | None = None


class WebSession(Model):
    """A website or bot logged in through Telegram Login."""

    hash: str = ""
    bot: int | None = None
    bot_username: str | None = None
    domain: str = ""
    browser: str = ""
    platform: str = ""
    ip: str = ""
    region: str = ""
    date_created: str | None = None
    date_active: str | None = None


class WebSessionRevocation(Model):
    revoked: int = 0
    hashes: list[str] = []
    blocked: list[int] = []


class Passkey(Model):
    id: str = ""
    name: str = ""
    date: str | None = None
    date_unix: int | None = None
    last_usage_date: str | None = None
    software_emoji_id: int | None = None
    deleted: bool = False


# ---------------------------------------------------------------------------
# The cloud password
# ---------------------------------------------------------------------------


class PasswordState(Model):
    """2-step verification, as much of it as is safe to print.

    Deliberately missing: `srp_B`, `srp_id`, `secure_random` and the KDF
    salts. They are live parameters of an in-flight exchange, not status.
    """

    has_password: bool = False
    has_recovery: bool = False
    has_secure_values: bool = False
    hint: str | None = None
    email_unconfirmed_pattern: str | None = None
    login_email_pattern: str | None = None
    pending_reset_date: str | None = None
    #: Only with the password supplied: `account.getPasswordSettings`.
    recovery_email: str | None = None
    #: Only with `--verify`: did the supplied password check out.
    password_ok: bool | None = None
    #: `PASSWORD_TOO_FRESH_X` counts down to this.
    sensitive_actions_eligible_at: str | None = None
    changed: bool = False


class RecoveryEmail(Model):
    kind: str = "recovery"
    email_pattern: str | None = None
    confirmed: bool = False
    sent_code_length: int | None = None
    cancelled: bool = False
    resent: bool = False


class PasswordReset(Model):
    """`account.resetPassword` — the 7-day path for a password nobody has."""

    status: str = ""
    until_date: str | None = None
    retry_date: str | None = None
    cancelled: bool = False


class TempPassword(Model):
    tmp_password: str = ""
    valid_until: str | None = None


class PhoneChange(Model):
    phone: str = ""
    code_hash: str = ""
    type: str = ""
    timeout: int | None = None
    changed: bool = False
    confirmed: bool = False
    cancelled: bool = False
    resent: bool = False


class AutologinUrl(Model):
    url: str = ""
    domain_allowed: bool = False


class Suggestion(Model):
    """A server-side nudge (`SETUP_PASSKEY`, `VALIDATE_PASSWORD`, a PSA)."""

    suggestion: str = ""
    dismissible: bool = True
    dismissed: bool = False
    promo_peer: int | None = None
    psa_type: str | None = None
    hidden: bool = False


class SupportInfo(Model):
    support_user: int | None = None
    support_name: str | None = None
    support_phone: str | None = None
    faq_url: str | None = None
    privacy_url: str | None = None
    features_url: str | None = None
    invite_text: str | None = None
    my_link: str | None = None
    note: str | None = None
    author: str | None = None
    date: str | None = None
    date_unix: int | None = None


class SmsJobs(Model):
    """The Peer-to-Peer Login Program, control side only."""

    eligible: bool = False
    joined: bool = False
    allow_international: bool = False
    recent_sent: int | None = None
    recent_since: str | None = None
    recent_remains: int | None = None
    terms_url: str | None = None


# ---------------------------------------------------------------------------
# Passport
# ---------------------------------------------------------------------------


class PassportValue(Model):
    """One stored Passport document, as metadata.

    `plain_data` is the only field that can ever be filled without the
    Passport crypto stack: phone and email secure values are stored in the
    clear, every other type is encrypted under a secret derived from the
    cloud password.
    """

    type: str = ""
    hash: str = ""
    has_files: bool = False
    has_translation: bool = False
    plain_data: str | None = None


class PassportRequirement(Model):
    type: str = ""
    native_names: bool = False
    selfie_required: bool = False
    translation_required: bool = False


class PassportForm(Model):
    """What a service is asking for, and what of it we already hold."""

    bot: int | None = None
    required_types: list[PassportRequirement] = []
    privacy_policy_url: str | None = None
    values: list[PassportValue] = []
    errors: list[str] = []
    country_language: str | None = None


class PassportVerification(Model):
    target: str = ""
    sent: bool = False
    verified: bool = False
    code_length: int | None = None
    #: Telegram will not accept the code without the hash that came with it,
    #: and the second call is a different process.
    code_hash: str | None = None


class PassportDeletion(Model):
    deleted: list[str] = []
