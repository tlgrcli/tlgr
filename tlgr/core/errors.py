"""Error types, stable exit codes, and the one Telethon-exception table.

Everything that turns a raised exception into something a caller can act on
happens here: the machine name (`code`), the process exit status, the HTTP
status the daemon answers with, whether a retry is worth attempting, and the
hint a human reads. v1 spread that decision across four modules and collapsed
most of it to IPC_ERROR/exit 12 on the way out (COR-06); one table cannot
disagree with itself.

**Why the table is keyed by class *name*.** ARCHITECTURE §2.2 requires that
`cli/` never import Telethon — `tlgr --help` has to stay fast and work on a
machine with no Telethon installed — while §7.1 requires that this module be
the only place Telethon exception classes are named. Both hold only if the
names are strings: `classify()` walks `type(exc).__mro__` and looks each class
name up, so nothing is imported to classify anything.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any

import msgspec

from tlgr.models.error import ErrorBody

# Stable exit codes for automation/agent consumption.
EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_EMPTY = 3
EXIT_AUTH = 4
EXIT_NOT_FOUND = 5
EXIT_PERMISSION = 6
EXIT_RATE_LIMITED = 7
EXIT_RETRYABLE = 8
EXIT_SPAM_FLAGGED = 9
EXIT_CONFIG = 10
EXIT_DAEMON = 11
EXIT_IPC = 12
EXIT_INDETERMINATE = 13
EXIT_CANCELLED = 130

#: `MessageNotModifiedError` is not a failure: the world already looks the way
#: the caller asked for. `classify()` returns this code with `exit_code == 0`,
#: and the dispatcher turns it into `ok: true` with `meta.already = true`.
NOT_MODIFIED = "NOT_MODIFIED"

EXIT_CODE_MAP: dict[str, dict[str, Any]] = {
    "SUCCESS": {"code": EXIT_SUCCESS, "description": "Success"},
    "GENERIC": {"code": EXIT_GENERIC, "description": "Generic failure"},
    "USAGE": {"code": EXIT_USAGE, "description": "Usage or parse error"},
    "EMPTY": {"code": EXIT_EMPTY, "description": "Empty results"},
    "AUTH_ERROR": {"code": EXIT_AUTH, "description": "Authentication required"},
    "AUTH_PASSWORD_REQUIRED": {
        "code": EXIT_AUTH,
        "description": "Two-factor password required to complete sign-in",
    },
    "SESSION_ERROR": {"code": EXIT_AUTH, "description": "Session error (re-auth needed)"},
    "CHAT_NOT_FOUND": {"code": EXIT_NOT_FOUND, "description": "Chat or entity not found"},
    "NOT_FOUND": {
        "code": EXIT_NOT_FOUND,
        "description": "Chat, user, message or account not found",
    },
    "ACCOUNT_NOT_FOUND": {"code": EXIT_NOT_FOUND, "description": "Account alias is not registered"},
    "ACCOUNT_REQUIRED": {"code": EXIT_USAGE, "description": "No account given and none inferable"},
    "PERMISSION_DENIED": {"code": EXIT_PERMISSION, "description": "Permission denied"},
    "RATE_LIMITED": {"code": EXIT_RATE_LIMITED, "description": "Rate limited (retry later)"},
    "RETRYABLE": {"code": EXIT_RETRYABLE, "description": "Transient/retryable error"},
    "PEER_FLOOD": {
        "code": EXIT_SPAM_FLAGGED,
        "description": "Account spam-flagged for messaging strangers (PeerFlood) — stop sending",
    },
    "ACCOUNT_FROZEN": {
        "code": EXIT_SPAM_FLAGGED,
        "description": "Account frozen/restricted by Telegram — stop sending",
    },
    "CONFIG_ERROR": {"code": EXIT_CONFIG, "description": "Configuration error"},
    "DAEMON_ERROR": {"code": EXIT_DAEMON, "description": "Daemon error"},
    "DAEMON_NOT_RUNNING": {"code": EXIT_DAEMON, "description": "Daemon is not running"},
    "DAEMON_VERSION_MISMATCH": {
        "code": EXIT_DAEMON,
        "description": "Daemon speaks a different protocol version than this CLI",
    },
    "IPC_ERROR": {"code": EXIT_IPC, "description": "IPC communication error"},
    "NOT_SUPPORTED": {
        "code": EXIT_INDETERMINATE,
        "description": (
            "tlgr cannot perform this — the API layer or Telethon build lacks it, "
            "not a failure of the request"
        ),
    },
    "INDETERMINATE": {
        "code": EXIT_INDETERMINATE,
        "description": (
            "Question could not be answered authoritatively — treat as unknown, never as a negative"
        ),
    },
    "CANCELLED": {"code": EXIT_CANCELLED, "description": "Interrupted (SIGINT)"},
}


# ---------------------------------------------------------------------------
# The exception tree
# ---------------------------------------------------------------------------


class TlgrError(Exception):
    """Base error for all tlgr errors."""

    code: str = "TLGR_ERROR"
    exit_code: int = EXIT_GENERIC
    hint: str = ""
    http: int = 500
    retryable: bool = False

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class UsageError(TlgrError):
    code = "USAGE"
    exit_code = EXIT_USAGE
    http = 400

    def __init__(self, message: str, code: str | None = None, field: str | None = None):
        super().__init__(message, code)
        self.field = field


class AuthenticationError(TlgrError):
    code = "AUTH_ERROR"
    exit_code = EXIT_AUTH
    http = 401
    hint = "Run: tlgr account add <phone>"


class AuthPasswordRequiredError(AuthenticationError):
    code = "AUTH_PASSWORD_REQUIRED"
    hint = "Supply the 2FA password with --password-env TLGR_2FA_PASSWORD"


class SessionError(TlgrError):
    code = "SESSION_ERROR"
    exit_code = EXIT_AUTH
    http = 401
    hint = "Session expired. Run: tlgr account add <phone>"


class ConfigurationError(TlgrError):
    code = "CONFIG_ERROR"
    exit_code = EXIT_CONFIG
    http = 400
    hint = "Run: tlgr config init"


class NotFoundError(TlgrError):
    code = "NOT_FOUND"
    exit_code = EXIT_NOT_FOUND
    http = 404


class ChatNotFoundError(NotFoundError):
    code = "CHAT_NOT_FOUND"
    hint = "Run: tlgr chat list  to find available chats"


class AccountNotFoundError(NotFoundError):
    code = "ACCOUNT_NOT_FOUND"
    hint = "Run: tlgr account list  to see registered aliases"


class AccountRequiredError(TlgrError):
    """No account was given and the daemon refuses to pick one for you.

    v1 used "whichever alias came first out of a set" (COR-02), which meant a
    two-account user could send from the wrong identity without any signal.
    """

    code = "ACCOUNT_REQUIRED"
    exit_code = EXIT_USAGE
    http = 400
    hint = "Pass -a <alias>, set TLGR_ACCOUNT, or set [accounts] default in config.toml"


class PermissionError_(TlgrError):
    code = "PERMISSION_DENIED"
    exit_code = EXIT_PERMISSION
    http = 403


class RateLimitError(TlgrError):
    code = "RATE_LIMITED"
    exit_code = EXIT_RATE_LIMITED
    http = 429
    retryable = True

    def __init__(self, message: str, wait_seconds: int = 0):
        super().__init__(message, code="RATE_LIMITED")
        self.wait_seconds = wait_seconds
        if wait_seconds:
            self.hint = f"Rate limited. Retry after {wait_seconds}s"


class SpamFlagError(TlgrError):
    """Telegram has restricted this account from messaging strangers.

    Distinct from RateLimitError: a FloodWait clears after `wait_seconds`,
    whereas PeerFlood/FROZEN is an account-level spam flag with no advertised
    expiry. Callers running outreach must stop ALL outgoing traffic for the
    account rather than back off and retry.
    """

    code = "PEER_FLOOD"
    exit_code = EXIT_SPAM_FLAGGED
    http = 403
    hint = "Account is spam-flagged. Stop sending from it and let it rest."


class AccountFrozenError(SpamFlagError):
    code = "ACCOUNT_FROZEN"
    hint = "Account is frozen by Telegram. Appeal before sending anything else."


class RetryableError(TlgrError):
    code = "RETRYABLE"
    exit_code = EXIT_RETRYABLE
    http = 503
    retryable = True


class IndeterminateError(TlgrError):
    """The answer could not be established — and must never be reported as "no".

    Exit 13 exists because a truncated scan, a flood mid-harvest or an RPC
    failure during a *negative* proof are all "we do not know", and a caller
    that reads them as "no" acts on a fact nobody established.
    """

    code = "INDETERMINATE"
    exit_code = EXIT_INDETERMINATE
    http = 200


class NotSupportedError(TlgrError):
    """tlgr cannot do this, and no retry will change that.

    Shares exit 13 with INDETERMINATE because both mean "do not read this as
    a no about the world": a feature Telethon's layer does not carry has not
    been refused by Telegram, it was never asked. The code is distinct so an
    agent can tell "unavailable in this build" from "could not establish".
    """

    code = "NOT_SUPPORTED"
    exit_code = EXIT_INDETERMINATE
    http = 501


class DaemonError(TlgrError):
    code = "DAEMON_ERROR"
    exit_code = EXIT_DAEMON
    http = 500
    hint = "Run: tlgr daemon start"


class DaemonNotRunningError(DaemonError):
    code = "DAEMON_NOT_RUNNING"
    exit_code = EXIT_DAEMON
    hint = "Daemon is not running. Start it with: tlgr daemon start"


class DaemonVersionMismatchError(DaemonError):
    code = "DAEMON_VERSION_MISMATCH"
    http = 409
    hint = "Run: tlgr daemon restart  to pick up the new protocol"


class IPCError(TlgrError):
    code = "IPC_ERROR"
    exit_code = EXIT_IPC
    http = 500
    retryable = True
    hint = "Check daemon status with: tlgr daemon status"


class CancelledError(TlgrError):
    code = "CANCELLED"
    exit_code = EXIT_CANCELLED


# ---------------------------------------------------------------------------
# The mapping table (ARCHITECTURE §7.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorRule:
    """One row of the §7.2 table."""

    code: str
    exit_code: int
    http: int
    retryable: bool = False
    hint: str = ""


_RATE = ErrorRule("RATE_LIMITED", EXIT_RATE_LIMITED, 429, True)
_SESSION = ErrorRule(
    "SESSION_ERROR", EXIT_AUTH, 401, False, "Re-authenticate: tlgr account add <phone>"
)
_AUTH = ErrorRule("AUTH_ERROR", EXIT_AUTH, 401)
_NOT_FOUND = ErrorRule("NOT_FOUND", EXIT_NOT_FOUND, 404)
_DENIED = ErrorRule("PERMISSION_DENIED", EXIT_PERMISSION, 403)
_PREMIUM = ErrorRule("PERMISSION_DENIED", EXIT_PERMISSION, 403, False, "Requires Telegram Premium")
_PAYMENT = ErrorRule(
    "PERMISSION_DENIED", EXIT_PERMISSION, 403, False, "Not enough Stars/balance for this operation"
)
_USAGE = ErrorRule("USAGE", EXIT_USAGE, 400)
_RETRY = ErrorRule("RETRYABLE", EXIT_RETRYABLE, 503, True)

#: Telethon (and internal) exception class name -> rule. Keyed by name so that
#: importing this module never imports Telethon; see the module docstring.
ERROR_MAP: dict[str, ErrorRule] = {
    # --- waits -------------------------------------------------------------
    "FloodWaitError": _RATE,
    "SlowModeWaitError": _RATE,
    "FloodPremiumWaitError": _RATE,
    "FloodTestPhoneWaitError": _RATE,
    "TakeoutInitDelayError": _RATE,
    "PreviousChatImportActiveWaitXminError": _RATE,
    "TwoFaConfirmWaitError": _RATE,
    "_2faConfirmWaitError": _RATE,
    # --- account-level stop signs -----------------------------------------
    "PeerFloodError": ErrorRule(
        "PEER_FLOOD",
        EXIT_SPAM_FLAGGED,
        403,
        False,
        "Account is spam-flagged. Stop sending from it and let it rest.",
    ),
    "FrozenMethodInvalidError": ErrorRule(
        "ACCOUNT_FROZEN", EXIT_SPAM_FLAGGED, 403, False, "Account is frozen; appeal before retrying"
    ),
    # --- session / auth ----------------------------------------------------
    "AuthKeyUnregisteredError": _SESSION,
    "AuthKeyInvalidError": _SESSION,
    "AuthKeyPermEmptyError": _SESSION,
    "SessionRevokedError": _SESSION,
    "SessionExpiredError": _SESSION,
    "AuthKeyDuplicatedError": _SESSION,
    "UserDeactivatedError": _SESSION,
    "UserDeactivatedBanError": _SESSION,
    "AuthKeyNotFound": _SESSION,
    "SessionPasswordNeededError": ErrorRule(
        "AUTH_PASSWORD_REQUIRED",
        EXIT_AUTH,
        401,
        False,
        "Supply the 2FA password with --password-env TLGR_2FA_PASSWORD",
    ),
    "PhoneCodeInvalidError": _AUTH,
    "PhoneCodeExpiredError": _AUTH,
    "PhoneNumberInvalidError": _AUTH,
    "PhoneNumberBannedError": _AUTH,
    "PhoneNumberUnoccupiedError": _AUTH,
    "PasswordHashInvalidError": _AUTH,
    "UpdateAppToLoginError": ErrorRule(
        "AUTH_ERROR", EXIT_AUTH, 401, False, "This login method is retired; use QR login instead"
    ),
    # --- not found ---------------------------------------------------------
    "UsernameNotOccupiedError": _NOT_FOUND,
    "UsernameInvalidError": _NOT_FOUND,
    "PeerIdInvalidError": _NOT_FOUND,
    "ChannelInvalidError": _NOT_FOUND,
    "ChatIdInvalidError": _NOT_FOUND,
    "UserIdInvalidError": _NOT_FOUND,
    "MessageIdInvalidError": _NOT_FOUND,
    "MsgIdInvalidError": _NOT_FOUND,
    "InviteHashExpiredError": _NOT_FOUND,
    "InviteHashInvalidError": _NOT_FOUND,
    "StickersetInvalidError": _NOT_FOUND,
    # --- permission --------------------------------------------------------
    "ChatAdminRequiredError": _DENIED,
    "ChatWriteForbiddenError": _DENIED,
    "ChatSendMediaForbiddenError": _DENIED,
    "ChatSendStickersForbiddenError": _DENIED,
    "ChatSendGifsForbiddenError": _DENIED,
    "ChatSendGameForbiddenError": _DENIED,
    "ChatSendInlineForbiddenError": _DENIED,
    "ChatSendPollForbiddenError": _DENIED,
    "ChatSendPhotosForbiddenError": _DENIED,
    "ChatSendVideosForbiddenError": _DENIED,
    "ChatSendAudiosForbiddenError": _DENIED,
    "ChatSendVoicesForbiddenError": _DENIED,
    "ChatSendRoundvideosForbiddenError": _DENIED,
    "ChatSendDocsForbiddenError": _DENIED,
    "ChatSendPlainForbiddenError": _DENIED,
    "ChannelPrivateError": _DENIED,
    "UserPrivacyRestrictedError": _DENIED,
    "UserIsBlockedError": _DENIED,
    "UserBannedInChannelError": _DENIED,
    "UserNotParticipantError": _DENIED,
    "MessageDeleteForbiddenError": _DENIED,
    "MessageAuthorRequiredError": _DENIED,
    "MessageEditTimeExpiredError": _DENIED,
    "RightForbiddenError": _DENIED,
    "ChatForwardsRestrictedError": _DENIED,
    "TopicClosedError": _DENIED,
    "BroadcastForbiddenError": _DENIED,
    "ForbiddenError": _DENIED,
    "PremiumAccountRequiredError": _PREMIUM,
    "PrivacyPremiumRequiredError": _PREMIUM,
    "BoostsRequiredError": _PREMIUM,
    "BalanceTooLowError": _PAYMENT,
    "AllowPaymentRequiredError": _PAYMENT,
    "StarsFormAmountMismatchError": _PAYMENT,
    "FormExpiredError": _PAYMENT,
    # --- not an error ------------------------------------------------------
    "MessageNotModifiedError": ErrorRule(NOT_MODIFIED, EXIT_SUCCESS, 200),
    # --- usage -------------------------------------------------------------
    "MessageEmptyError": _USAGE,
    "MessageTooLongError": _USAGE,
    "MediaEmptyError": _USAGE,
    "MediaInvalidError": _USAGE,
    "PhotoInvalidDimensionsError": _USAGE,
    "ContactIdInvalidError": _USAGE,
    "UserAlreadyParticipantError": _USAGE,
    "UsersTooMuchError": _USAGE,
    "BotMethodInvalidError": _USAGE,
    "BannedRightsInvalidError": _USAGE,
    "ScheduleDateInvalidError": _USAGE,
    "BadRequestError": _USAGE,
    "ValidationError": _USAGE,
    "UsageError": _USAGE,
    # --- transient ---------------------------------------------------------
    "FileReferenceExpiredError": _RETRY,
    "FileReferenceInvalidError": _RETRY,
    "FilerefUpgradeNeededError": _RETRY,
    "ServerError": _RETRY,
    "RpcCallFailError": _RETRY,
    "RpcMcgetFailError": _RETRY,
    "InterdcCallErrorError": _RETRY,
    "TimedOutError": _RETRY,
    "PersistentTimestampOutdatedError": _RETRY,
    "TimeoutError": _RETRY,
    "ConnectionError": _RETRY,
    "OSError": _RETRY,
    # --- everything else ---------------------------------------------------
    "RPCError": ErrorRule("GENERIC", EXIT_GENERIC, 500),
    "KeyboardInterrupt": ErrorRule("CANCELLED", EXIT_CANCELLED, 500),
}

#: RPC message patterns that outrank the class name. Telethon has no generated
#: class for every string in the 780-entry error DB, and `FROZEN_*` in
#: particular arrives as a bare RPCError.
_MESSAGE_RULES: tuple[tuple[re.Pattern[str], ErrorRule], ...] = (
    (
        # Anywhere in the message, not only at the start: Telethon renders an
        # unknown RPC error as "RPCError None: FROZEN_METHOD_INVALID (caused
        # by …)", so anchoring this pattern made it match only the bare code.
        re.compile(r"\bFROZEN_[A-Z_]+"),
        ErrorRule("ACCOUNT_FROZEN", EXIT_SPAM_FLAGGED, 403, False, "Account is frozen by Telegram"),
    ),
    (re.compile(r"^FLOOD_WAIT_\d+$"), _RATE),
    (re.compile(r"^SLOWMODE_WAIT_\d+$"), _RATE),
    (re.compile(r"Cannot send requests while disconnected"), _RETRY),
    (re.compile(r"Could not find the input entity"), _NOT_FOUND),
    (re.compile(r"^AUTH_KEY_(UNREGISTERED|INVALID|DUPLICATED|PERM_EMPTY)$"), _SESSION),
)

_GENERIC = ErrorRule("GENERIC", EXIT_GENERIC, 500)

#: Trailing numbers are parameters, not part of the name: FLOOD_WAIT_42 and
#: FLOOD_WAIT_3 are the same error with a different wait.
_SUFFIX_RE = re.compile(r"_(\d+)$")

#: msgspec ends a validation message with " - at $.chat.kind"; that suffix is
#: what turns a USAGE error into an actionable one (`error.field`).
_FIELD_RE = re.compile(r"\s-\s+at\s+`?\$\.?([^`]+)`?\s*$")


def strip_numeric_suffix(rpc_message: str) -> tuple[str, int | None]:
    """Split `FLOOD_WAIT_42` into `("FLOOD_WAIT_X", 42)`."""
    m = _SUFFIX_RE.search(rpc_message)
    if not m:
        return rpc_message, None
    return rpc_message[: m.start()] + "_X", int(m.group(1))


def rule_for(exc: BaseException) -> ErrorRule:
    """Find the §7.2 row for *exc*, most specific class first.

    Walks the MRO by name so that a Telethon subclass we have never heard of
    still lands on its base's row (every `*ForbiddenError` under
    `ForbiddenError` becomes PERMISSION_DENIED rather than GENERIC).
    """
    if isinstance(exc, TlgrError):
        return ErrorRule(exc.code, exc.exit_code, exc.http, exc.retryable, exc.hint)

    message = str(exc)
    for pattern, message_rule in _MESSAGE_RULES:
        if pattern.search(message):
            return message_rule

    for klass in type(exc).__mro__:
        found = ERROR_MAP.get(klass.__name__)
        if found is not None:
            return found
    return _GENERIC


def classify(exc: BaseException, *, account: str | None = None, request_id: str = "") -> ErrorBody:
    """Turn any exception into the wire error shape.

    This is the single funnel: the daemon calls it before answering, the
    legacy IPC handler calls it during migration, and the CLI calls it for
    anything raised locally, so an unmigrated command gets the right exit code
    on day one (COR-06).
    """
    rule = rule_for(exc)
    body = ErrorBody(
        code=rule.code,
        message=str(exc) or type(exc).__name__,
        exit_code=rule.exit_code,
        retryable=rule.retryable,
        hint=rule.hint or getattr(exc, "hint", "") or None,
        account=account,
        request_id=request_id or None,
    )

    # FloodWaitError carries the wait as an attribute; a bare RPCError carries
    # it in the message (FLOOD_WAIT_42). Both must reach the caller, because
    # "retry later" without "how much later" is not actionable.
    seconds = getattr(exc, "seconds", None)
    if not isinstance(seconds, int):
        seconds = getattr(exc, "wait_seconds", None) or None
    if rule.code == "RATE_LIMITED":
        if not isinstance(seconds, int):
            _, parsed = strip_numeric_suffix(str(exc).strip())
            seconds = parsed
        if isinstance(seconds, int):
            body.wait_seconds = seconds
            if not body.hint:
                body.hint = f"Retry after {seconds}s, or raise --flood-wait-max."

    if rule.code == "USAGE":
        field = getattr(exc, "field", None)
        if not field:
            m = _FIELD_RE.search(str(exc))
            if m:
                field = m.group(1).strip()
        body.field = field or None

    rpc_code = getattr(exc, "code", None)
    rpc_message = getattr(exc, "message", None)
    if isinstance(rpc_code, int) and isinstance(rpc_message, str):
        rpc: dict[str, Any] = {"code": rpc_code, "message": rpc_message}
        method = getattr(exc, "request", None)
        if method is not None:
            rpc["method"] = type(method).__name__
        body.rpc = rpc

    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        body.reason = reason

    return body


#: The codes that mean "this session will never work again without a human".
#: Distinguished from a transport failure because the supervisor must stop
#: reconnecting rather than back off — retrying an unregistered auth key is
#: how v1 span forever while reporting "degraded".
FATAL_AUTH_CODES = frozenset({"SESSION_ERROR", "AUTH_ERROR", "AUTH_PASSWORD_REQUIRED"})


def is_fatal_auth(exc: BaseException) -> bool:
    """True when *exc* means the account needs a human to log in again."""
    return rule_for(exc).code in FATAL_AUTH_CODES


def http_status_for(exc: BaseException) -> int:
    """The HTTP status the daemon answers *exc* with."""
    return rule_for(exc).http


def is_not_an_error(body: ErrorBody) -> bool:
    """True for MESSAGE_NOT_MODIFIED, which is success wearing an exception."""
    return body.code == NOT_MODIFIED


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def exit_code_for(error: BaseException) -> int:
    """Return the stable exit code for an error."""
    if isinstance(error, TlgrError):
        return error.exit_code
    return EXIT_GENERIC


def error_body_dict(body: ErrorBody) -> dict[str, Any]:
    """ErrorBody as JSON, plus v1's `error` key aliasing `message`.

    Keeping the alias is what lets `--results-only` emit exactly the object v1
    printed (`error`/`code`/`exit_code`) while the envelope carries the full
    modern body (§12.4).
    """
    data: dict[str, Any] = msgspec.to_builtins(body)
    data["error"] = body.message
    return data


def error_envelope(
    exc: BaseException, *, op: str = "", account: str | None = None, request_id: str = ""
) -> dict[str, Any]:
    """The v2 failure envelope: `{"ok": false, "op": …, "error": {…}}`."""
    body = classify(exc, account=account, request_id=request_id)
    envelope: dict[str, Any] = {"ok": False, "error": error_body_dict(body)}
    if op:
        envelope["op"] = op
    if account:
        envelope["account"] = account
    return envelope


def format_error_json(error: BaseException) -> dict[str, Any]:
    """v1's flat error object — kept verbatim as a compatibility contract.

    This is the shape `--results-only` still prints, so every v1 consumer
    that reads `error`/`code`/`exit_code` off stdout keeps working. New code
    wants `error_envelope()`.
    """
    code = getattr(error, "code", "UNKNOWN_ERROR")
    result: dict[str, Any] = {
        "error": str(error),
        "code": code,
        "exit_code": exit_code_for(error),
    }
    if isinstance(error, RateLimitError) and error.wait_seconds:
        result["wait_seconds"] = error.wait_seconds
    return result


def emit_error(error: BaseException, use_json: bool = False) -> None:
    """Emit an error to stderr (human) and optionally stdout (JSON)."""
    if use_json:
        json.dump(format_error_json(error), sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()

    hint = getattr(error, "hint", "")
    if hint:
        print(f"Error: {error}\n  {hint}", file=sys.stderr)
    else:
        print(f"Error: {error}", file=sys.stderr)
