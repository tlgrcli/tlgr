"""Daemon status must distinguish a held client from a connected one.

On 2026-09-02 the machine's route to Telegram dropped mid-wake. Telethon
exhausted its reconnect budget on all three campaign accounts and raised
`ConnectionError: Connection to Telegram failed 5 time(s)`, leaving every
`ClientWrapper` in `Daemon._clients` — present, and dead. Every request then
failed with "Cannot send requests while disconnected".

The documented remedy for exactly that situation ("`tlgr status` if things
look dead", CLAUDE.md) reported:

    {"running": true, "accounts": ["Mr", "Pouri2048", "Pouri16", "Pouri256"],
     "jobs": [... all four "running": true]}

`accounts` was the dict's keys, and the agent-facing field was even named
`accounts_connected`. So the health check named in the runbook returned the
most reassuring possible answer while nothing worked, and the only way to
learn otherwise was to attempt a real send and read the failure.

The wrapper existing and the wrapper being usable are different facts; status
now reports the second one.
"""

from __future__ import annotations

from types import SimpleNamespace

from tlgr.core.client import ClientWrapper


def _wrapper(*, client=None):
    w = ClientWrapper.__new__(ClientWrapper)
    w._client = client
    w._me = None
    return w


class _FakeDaemon:
    """Just enough of Daemon to exercise status() without a running loop."""

    def __init__(self, clients):
        import os
        import time

        self._clients = clients
        self._start_time = time.time()
        self._job_runner = SimpleNamespace(list_jobs=lambda: [])
        self._os_getpid = os.getpid

    status = None  # bound below


def _status(clients):
    from tlgr.daemon.server import DaemonServer

    d = _FakeDaemon(clients)
    return DaemonServer.status(d)


# -- ClientWrapper.is_connected --


def test_wrapper_never_connected_is_not_connected():
    assert _wrapper(client=None).is_connected is False


def test_wrapper_with_live_client_is_connected():
    live = SimpleNamespace(is_connected=lambda: True)
    assert _wrapper(client=live).is_connected is True


def test_wrapper_survives_its_connection():
    """The exact shape of the incident: the object is there, the link is not."""
    dead = SimpleNamespace(is_connected=lambda: False)
    w = _wrapper(client=dead)
    assert w._client is not None  # what status() used to key off
    assert w.is_connected is False  # what it keys off now


# -- Daemon.status() --


def test_status_reports_all_connected_as_healthy():
    live = SimpleNamespace(is_connected=lambda: True)
    st = _status({"Pouri2048": _wrapper(client=live), "Mr": _wrapper(client=live)})

    assert st["healthy"] is True
    assert st["disconnected"] == []
    assert st["connections"] == {"Pouri2048": True, "Mr": True}


def test_status_reports_a_fully_dead_daemon_as_unhealthy():
    dead = SimpleNamespace(is_connected=lambda: False)
    clients = {a: _wrapper(client=dead) for a in ("Mr", "Pouri2048", "Pouri16", "Pouri256")}
    st = _status(clients)

    # The field that lied: still complete, still every account, unchanged shape.
    assert sorted(st["accounts"]) == ["Mr", "Pouri16", "Pouri2048", "Pouri256"]
    # The fields that tell the truth.
    assert st["healthy"] is False
    assert st["disconnected"] == ["Mr", "Pouri16", "Pouri2048", "Pouri256"]
    assert not any(st["connections"].values())


def test_status_reports_a_partial_outage():
    live = SimpleNamespace(is_connected=lambda: True)
    dead = SimpleNamespace(is_connected=lambda: False)
    st = _status({"Pouri2048": _wrapper(client=live), "Pouri16": _wrapper(client=dead)})

    assert st["healthy"] is False
    assert st["disconnected"] == ["Pouri16"]
    assert st["connections"] == {"Pouri2048": True, "Pouri16": False}


def test_status_with_no_clients_is_healthy_not_broken():
    """No accounts loaded yet is a different thing from accounts that died."""
    st = _status({})

    assert st["healthy"] is True
    assert st["disconnected"] == []
    assert st["connections"] == {}
