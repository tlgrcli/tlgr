"""Daemon status must distinguish a held client from a connected one.

On 2026-09-02 the machine's route to Telegram dropped mid-wake. Telethon
exhausted its reconnect budget on all three campaign accounts and raised
`ConnectionError: Connection to Telegram failed 5 time(s)`, leaving every
client object in the daemon's table — present, and dead. Every request then
failed with "Cannot send requests while disconnected".

The documented remedy for exactly that situation ("`tlgr status` if things
look dead", CLAUDE.md) reported:

    {"running": true, "accounts": ["Mr", "Pouri2048", "Pouri16", "Pouri256"],
     "jobs": [... all four "running": true]}

`accounts` was the dict's keys, and the agent-facing field was even named
`accounts_connected`. So the health check named in the runbook returned the
most reassuring possible answer while nothing worked, and the only way to
learn otherwise was to attempt a real send and read the failure.

The object existing and the link being usable are different facts. PR-12
deleted the `ClientWrapper` this was first written against, so the claim is
made where it now lives: `AccountSession.connected` asks the client, and
`SessionManager.snapshot()` — what `daemon status` answers from — carries a
state per account rather than a list of keys.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tlgr.daemon.session import AccountSession, SessionState


def _session(alias: str, *, client=None, state: str = SessionState.ONLINE) -> AccountSession:
    """A session with a client and nothing else; no loop, no socket."""
    session = AccountSession.__new__(AccountSession)
    session.alias = alias
    session.client = client
    session.state = state
    session.me = None
    session.reason = ""
    session.since = None
    session.connected_since = None
    session.last_update = None
    session.reconnects = 0
    session.catch_up_pending = False
    session.in_flight = 0
    session.resync_needed = set()
    return session


LIVE = SimpleNamespace(is_connected=lambda: True)
DEAD = SimpleNamespace(is_connected=lambda: False)


# -- AccountSession.connected ------------------------------------------------


def test_a_session_that_never_connected_is_not_connected():
    assert _session("work", client=None).connected is False


def test_a_session_with_a_live_client_is_connected():
    assert _session("work", client=LIVE).connected is True


def test_a_session_survives_its_connection():
    """The exact shape of the incident: the object is there, the link is not."""
    session = _session("work", client=DEAD)
    assert session.client is not None  # what status() used to key off
    assert session.connected is False  # what it keys off now


# -- what `daemon status` answers from ---------------------------------------


def _snapshot(*sessions: AccountSession) -> list[dict]:
    return [session.snapshot() for session in sessions]


def test_every_row_carries_a_state_not_just_a_name():
    """The field that lied was a list of keys. A row cannot be just a name."""
    rows = _snapshot(_session("Pouri2048", client=LIVE), _session("Mr", client=LIVE))
    assert sorted(row["alias"] for row in rows) == ["Mr", "Pouri2048"]
    assert all(row["state"] == SessionState.ONLINE for row in rows)


def test_a_dead_account_says_so_in_its_own_row():
    rows = _snapshot(
        _session("Mr", client=DEAD, state=SessionState.DEGRADED),
        _session("Pouri16", client=LIVE),
    )
    by_alias = {row["alias"]: row for row in rows}
    assert by_alias["Mr"]["state"] == SessionState.DEGRADED
    assert by_alias["Pouri16"]["state"] == SessionState.ONLINE


@pytest.mark.parametrize("state", [SessionState.DEGRADED, SessionState.STOPPED])
def test_a_session_that_is_not_online_reports_when_it_stopped_being(state):
    """ "Since when" is the difference between a blip and an outage."""
    row = _session("Mr", client=DEAD, state=state).snapshot()
    assert "since" in row
