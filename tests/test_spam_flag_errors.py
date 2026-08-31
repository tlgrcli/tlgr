"""PeerFlood / FROZEN must be distinguishable from generic IPC failures.

Regression for the 2026-08-31 outreach incident: a real PeerFloodError came
back over IPC as a plain 500 {"code": "IPC_ERROR"}, identical to any other
daemon failure. The campaign's most important safety signal was only
identifiable by reading Telethon's source to learn that PeerFloodError
formats itself as "Too many requests (caused by SendMessageRequest)".
"""

import json

import pytest
from telethon.errors import FloodWaitError, PeerFloodError, RPCError
from telethon.tl.functions.messages import SendMessageRequest

from tlgr.core.errors import EXIT_CODE_MAP, EXIT_SPAM_FLAGGED, SpamFlagError
from tlgr.daemon.ipc import _handle_exception


def _body(resp):
    return json.loads(resp.body.decode())


def test_peer_flood_gets_its_own_code_not_ipc_error():
    resp = _handle_exception(PeerFloodError(SendMessageRequest))
    assert resp.status == 403
    assert _body(resp)["code"] == "PEER_FLOOD"


def test_peer_flood_message_is_the_one_telethon_actually_emits():
    # The exact string that was indistinguishable from a generic error.
    err = PeerFloodError(SendMessageRequest)
    assert "Too many requests" in str(err)
    assert _body(_handle_exception(err))["error"] == str(err)


def test_frozen_account_is_classified_as_spam_flag():
    resp = _handle_exception(RPCError(SendMessageRequest, "FROZEN_METHOD_INVALID"))
    assert resp.status == 403
    assert _body(resp)["code"] == "ACCOUNT_FROZEN"


def test_flood_wait_still_rate_limited_with_wait_seconds():
    resp = _handle_exception(FloodWaitError(SendMessageRequest, capture=42))
    assert resp.status == 429
    body = _body(resp)
    assert body["code"] == "RATE_LIMITED"
    assert body["wait_seconds"] == 42


def test_unrelated_errors_stay_generic():
    resp = _handle_exception(ValueError("boom"))
    assert resp.status == 500
    assert _body(resp)["code"] == "IPC_ERROR"


@pytest.mark.parametrize("code", ["PEER_FLOOD", "ACCOUNT_FROZEN"])
def test_spam_flags_share_a_stable_exit_code(code):
    assert EXIT_CODE_MAP[code]["code"] == EXIT_SPAM_FLAGGED == 9


def test_spam_flag_error_is_not_a_rate_limit():
    """A FloodWait can be slept off; a spam flag cannot — callers must not
    conflate them, so SpamFlagError carries no wait_seconds."""
    err = SpamFlagError("Too many requests", code="PEER_FLOOD")
    assert err.exit_code == EXIT_SPAM_FLAGGED
    assert not hasattr(err, "wait_seconds")
