"""Every row of the ARCHITECTURE §7.2 table, exception in → code/exit out."""

from __future__ import annotations

import asyncio

import msgspec
import pytest

from tlgr.core.errors import (
    ERROR_MAP,
    EXIT_AUTH,
    EXIT_CODE_MAP,
    EXIT_GENERIC,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_RATE_LIMITED,
    EXIT_RETRYABLE,
    EXIT_SPAM_FLAGGED,
    EXIT_SUCCESS,
    EXIT_USAGE,
    NOT_MODIFIED,
    AccountRequiredError,
    ErrorRule,
    IndeterminateError,
    RateLimitError,
    TlgrError,
    classify,
    error_body_dict,
    error_envelope,
    format_error_json,
    http_status_for,
    is_not_an_error,
    rule_for,
    strip_numeric_suffix,
)


class _Inner(msgspec.Struct):
    kind: str


class _Outer(msgspec.Struct):
    chat: _Inner


def _fake(name: str, base: type[Exception] = Exception, **attrs: object) -> Exception:
    """Build an exception whose class *name* is what the table keys on.

    Telethon is deliberately not imported: the table is keyed by name exactly
    so that classification needs no Telethon, and the test proves it.
    """
    klass = type(name, (base,), {})
    exc = klass(f"{name} raised")
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


class TestTable:
    @pytest.mark.parametrize(("name", "rule"), sorted(ERROR_MAP.items()))
    def test_every_row_classifies(self, name, rule: ErrorRule):
        body = classify(_fake(name))
        assert body.code == rule.code
        assert body.exit_code == rule.exit_code
        assert body.retryable is rule.retryable

    @pytest.mark.parametrize(("name", "rule"), sorted(ERROR_MAP.items()))
    def test_every_code_has_an_exit_entry(self, name, rule: ErrorRule):
        if rule.code == NOT_MODIFIED:
            assert rule.exit_code == EXIT_SUCCESS
            return
        assert EXIT_CODE_MAP[rule.code]["code"] == rule.exit_code

    @pytest.mark.parametrize(
        ("name", "code", "exit_code"),
        [
            ("FloodWaitError", "RATE_LIMITED", EXIT_RATE_LIMITED),
            ("SlowModeWaitError", "RATE_LIMITED", EXIT_RATE_LIMITED),
            ("PeerFloodError", "PEER_FLOOD", EXIT_SPAM_FLAGGED),
            ("FrozenMethodInvalidError", "ACCOUNT_FROZEN", EXIT_SPAM_FLAGGED),
            ("AuthKeyUnregisteredError", "SESSION_ERROR", EXIT_AUTH),
            ("SessionPasswordNeededError", "AUTH_PASSWORD_REQUIRED", EXIT_AUTH),
            ("PhoneCodeInvalidError", "AUTH_ERROR", EXIT_AUTH),
            ("UsernameNotOccupiedError", "NOT_FOUND", EXIT_NOT_FOUND),
            ("ChatAdminRequiredError", "PERMISSION_DENIED", EXIT_PERMISSION),
            ("PremiumAccountRequiredError", "PERMISSION_DENIED", EXIT_PERMISSION),
            ("BalanceTooLowError", "PERMISSION_DENIED", EXIT_PERMISSION),
            ("MessageTooLongError", "USAGE", EXIT_USAGE),
            ("FileReferenceExpiredError", "RETRYABLE", EXIT_RETRYABLE),
            ("ServerError", "RETRYABLE", EXIT_RETRYABLE),
            ("RPCError", "GENERIC", EXIT_GENERIC),
        ],
    )
    def test_representative_rows(self, name, code, exit_code):
        body = classify(_fake(name))
        assert (body.code, body.exit_code) == (code, exit_code)

    def test_unknown_exception_is_generic(self):
        body = classify(RuntimeError("boom"))
        assert (body.code, body.exit_code) == ("GENERIC", EXIT_GENERIC)

    def test_subclasses_inherit_their_row(self):
        """A Telethon *ForbiddenError we have never heard of still denies."""
        base = type("ForbiddenError", (Exception,), {})
        exc = type("ChatSendSomethingNewForbiddenError", (base,), {})("nope")
        assert classify(exc).exit_code == EXIT_PERMISSION

    def test_http_statuses_come_from_the_table(self):
        assert http_status_for(_fake("FloodWaitError")) == 429
        assert http_status_for(_fake("ChannelPrivateError")) == 403
        assert http_status_for(_fake("PeerIdInvalidError")) == 404
        assert http_status_for(RuntimeError("x")) == 500


class TestParameters:
    def test_flood_wait_seconds_from_attribute(self):
        body = classify(_fake("FloodWaitError", seconds=42))
        assert body.wait_seconds == 42
        assert body.retryable is True
        assert "42" in (body.hint or "")

    def test_flood_wait_seconds_from_message(self):
        exc = type("RPCError", (Exception,), {})("FLOOD_WAIT_17")
        body = classify(exc)
        assert (body.code, body.wait_seconds) == ("RATE_LIMITED", 17)

    def test_numeric_suffixes_are_parameters(self):
        assert strip_numeric_suffix("FLOOD_WAIT_42") == ("FLOOD_WAIT_X", 42)
        assert strip_numeric_suffix("FILE_PART_7_MISSING") == ("FILE_PART_7_MISSING", None)

    def test_not_modified_is_not_an_error(self):
        body = classify(_fake("MessageNotModifiedError"))
        assert body.code == NOT_MODIFIED
        assert body.exit_code == EXIT_SUCCESS
        assert is_not_an_error(body)

    def test_missing_entity_value_error_is_not_found(self):
        body = classify(ValueError("Could not find the input entity for PeerUser(user_id=1)"))
        assert (body.code, body.exit_code) == ("NOT_FOUND", EXIT_NOT_FOUND)

    def test_disconnected_value_error_is_retryable(self):
        body = classify(ValueError("Cannot send requests while disconnected"))
        assert (body.code, body.exit_code) == ("RETRYABLE", EXIT_RETRYABLE)

    def test_frozen_rpc_message_wins_over_the_class(self):
        exc = type("RPCError", (Exception,), {})("FROZEN_METHOD_INVALID")
        assert classify(exc).code == "ACCOUNT_FROZEN"

    def test_msgspec_validation_error_is_usage_with_a_field(self):
        with pytest.raises(msgspec.ValidationError) as excinfo:
            msgspec.json.decode(b'{"chat":{"kind":1}}', type=_Outer)
        body = classify(excinfo.value)
        assert (body.code, body.exit_code) == ("USAGE", EXIT_USAGE)
        assert body.field == "chat.kind"

    def test_asyncio_timeout_is_retryable(self):
        assert classify(asyncio.TimeoutError()).code == "RETRYABLE"

    def test_oserror_is_retryable(self):
        assert classify(OSError("socket gone")).code == "RETRYABLE"

    def test_rpc_details_are_carried(self):
        exc = _fake("ChatAdminRequiredError", code=400, message="CHAT_ADMIN_REQUIRED")
        body = classify(exc)
        assert body.rpc == {"code": 400, "message": "CHAT_ADMIN_REQUIRED"}

    def test_indeterminate_reason_survives(self):
        exc = IndeterminateError("could not establish")
        exc.reason = "scan truncated"
        assert classify(exc).reason == "scan truncated"


class TestTlgrErrorTree:
    def test_tlgr_errors_classify_from_themselves(self):
        body = classify(AccountRequiredError("no account"))
        assert (body.code, body.exit_code) == ("ACCOUNT_REQUIRED", EXIT_USAGE)
        assert body.hint

    def test_rule_for_a_tlgr_error_uses_its_own_attributes(self):
        rule = rule_for(RateLimitError("slow", wait_seconds=5))
        assert (rule.code, rule.exit_code, rule.http, rule.retryable) == (
            "RATE_LIMITED",
            EXIT_RATE_LIMITED,
            429,
            True,
        )

    def test_account_is_attached_when_known(self):
        body = classify(TlgrError("x"), account="work", request_id="01J")
        assert (body.account, body.request_id) == ("work", "01J")


class TestRendering:
    def test_envelope_shape(self):
        env = error_envelope(
            RateLimitError("slow", wait_seconds=42), op="message.send", account="w"
        )
        assert env["ok"] is False
        assert env["op"] == "message.send"
        assert env["error"]["code"] == "RATE_LIMITED"
        assert env["error"]["exit_code"] == EXIT_RATE_LIMITED
        assert env["error"]["wait_seconds"] == 42

    def test_inner_object_keeps_v1_keys(self):
        """--results-only prints this object; v1 consumers read all three keys."""
        inner = error_body_dict(classify(RateLimitError("slow", wait_seconds=42)))
        assert inner["error"] == "slow"
        assert inner["code"] == "RATE_LIMITED"
        assert inner["exit_code"] == EXIT_RATE_LIMITED

    def test_v1_formatter_is_unchanged(self):
        assert format_error_json(TlgrError("broke")) == {
            "error": "broke",
            "code": "TLGR_ERROR",
            "exit_code": EXIT_GENERIC,
        }
