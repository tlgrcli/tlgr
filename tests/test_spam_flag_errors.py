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

from tlgr.core.errors import (
    EXIT_CODE_MAP,
    EXIT_SPAM_FLAGGED,
    SpamFlagError,
    classify,
    error_body_dict,
    http_status_for,
)


class _Resp:
    """The status and body the daemon would send, in the shape this file reads."""

    def __init__(self, exc):
        self.status = http_status_for(exc)
        self._body = error_body_dict(classify(exc))

    @property
    def body(self):
        return json.dumps(self._body).encode()


def _handle_exception(exc):
    """What the daemon answers for *exc*.

    PR-12 deleted `daemon/ipc.py`, whose `_handle_exception` this was written
    against. The classification was never that route's own — it funnelled
    through `core.errors`, which is what every path uses now — so the claims
    below are unchanged and are made one layer down.
    """
    return _Resp(exc)


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


def test_unrelated_errors_are_generic_not_ipc_error():
    """An unclassified failure is GENERIC/exit 1, not IPC_ERROR/exit 12.

    v1 answered IPC_ERROR for everything it did not recognise, which said
    "the channel between you and the daemon failed" about an error that had
    nothing to do with the channel. COR-06 routes the legacy handler through
    the same table as the v2 dispatcher, and an unknown exception lands on
    the table's GENERIC row.
    """
    resp = _handle_exception(ValueError("boom"))
    assert resp.status == 500
    assert _body(resp)["code"] == "GENERIC"
    assert _body(resp)["exit_code"] == 1


@pytest.mark.parametrize("code", ["PEER_FLOOD", "ACCOUNT_FROZEN"])
def test_spam_flags_share_a_stable_exit_code(code):
    assert EXIT_CODE_MAP[code]["code"] == EXIT_SPAM_FLAGGED == 9


def test_spam_flag_error_is_not_a_rate_limit():
    """A FloodWait can be slept off; a spam flag cannot — callers must not
    conflate them, so SpamFlagError carries no wait_seconds."""
    err = SpamFlagError("Too many requests", code="PEER_FLOOD")
    assert err.exit_code == EXIT_SPAM_FLAGGED
    assert not hasattr(err, "wait_seconds")
