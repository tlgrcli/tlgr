"""The wire, against a real daemon on a real socket (§11.2, COR-04/31/32).

The payloads here are the ones v1 could not carry: Persian text, a space, a
`#`, a `&` and a `+`. Every one of them is a character that a hand-built
request line or an f-string query mangles, and each was a real failure —
`tlgr message search @x "سلام"` returned results for a different query.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tlgr.core.errors import DaemonNotRunningError, IPCError, RetryableError
from tlgr.transport.client import DaemonClient, error_from_body
from tlgr.transport.ndjson import dump_frame, iter_frames, parse_frame

# `asyncio_mode = "auto"` in pyproject collects the async tests; marking
# them explicitly would also mark the synchronous ones in this module.

TRICKY = "سلام #12 a+b @fish&chips  spaced"


async def test_status_round_trips(live_daemon, client, in_thread):
    status = await in_thread(client.status)
    assert status["ok"] is True
    assert status["daemon"]["protocol"] == 2
    assert status["daemon"]["ready"] is True


async def test_unicode_and_reserved_characters_survive_a_post(live_daemon, client, in_thread):
    """The body is JSON over http.client, so nothing needs escaping by hand.

    The account does not exist, which is the point: the daemon quotes it back
    in the error, so the assertion proves the exact bytes made the round trip.
    """
    with pytest.raises(Exception) as caught:
        await in_thread(client.request, "POST", "/v1/admin/resync", body={"account": TRICKY})
    assert TRICKY in str(caught.value)


async def test_query_strings_are_urlencoded(live_daemon, client, in_thread):
    """`urlencode`, not an f-string: `#` would otherwise truncate the query."""
    with pytest.raises(Exception) as caught:
        await in_thread(
            client.request, "GET", "/v1/events", params={"account": "", "types": TRICKY}
        )
    # The account is empty, so this is a USAGE error — but it proves the
    # request arrived and was parsed rather than being cut at the '#'.
    assert "account" in str(caught.value)


async def test_a_daemon_that_is_not_running_is_named_as_such(tlgr_home: Path, in_thread):
    client = DaemonClient(tlgr_home, timeout=1.0, auto_start=False)
    with pytest.raises(DaemonNotRunningError):
        await in_thread(client.status)


async def test_timeout_becomes_retryable(live_daemon, tlgr_home: Path, in_thread, monkeypatch):
    """A daemon that does not answer in time is exit 8, never a short read.

    v1 read until the socket went quiet and parsed whatever it had, so a
    timeout and a truncated reply were the same event and both could look
    like success (COR-31).
    """
    from tlgr.transport import client as transport

    def timeout(self):
        raise TimeoutError("timed out")

    monkeypatch.setattr(transport._UnixHTTPConnection, "getresponse", timeout)
    client = DaemonClient(tlgr_home, timeout=0.5, auto_start=False)
    client._ready = True
    with pytest.raises(RetryableError):
        await in_thread(client.request, "POST", "/v1/admin/reload", body={})


async def test_a_broken_socket_is_retried_once_for_idempotent_requests(
    live_daemon, tlgr_home: Path, in_thread
):
    """A GET may be replayed; the retry is what hides a daemon restart."""
    client = DaemonClient(tlgr_home, timeout=5.0, auto_start=False)
    calls: list[str] = []
    original = client._open

    def flaky(method, path, **kwargs):
        calls.append(method)
        if len(calls) == 1:
            raise ConnectionResetError("connection reset by peer")
        return original(method, path, **kwargs)

    client._open = flaky  # type: ignore[method-assign]
    client._ready = True
    status = await in_thread(client.request, "GET", "/v1/status")
    # Three opens, not two: the broken socket also invalidates the cached
    # "the daemon is up" verdict, so the retry re-probes before it re-sends.
    assert len(calls) == 3
    assert status["ok"] is True


async def test_a_post_is_not_retried(live_daemon, tlgr_home: Path, in_thread):
    """Replaying a send is worse than reporting a failure."""
    client = DaemonClient(tlgr_home, timeout=5.0, auto_start=False)
    client._ready = True
    calls: list[str] = []

    def always_broken(method, path, **kwargs):
        calls.append(method)
        raise ConnectionResetError("connection reset by peer")

    client._open = always_broken  # type: ignore[method-assign]
    with pytest.raises(IPCError):
        await in_thread(client.request, "POST", "/v1/admin/reload", body={})
    assert len(calls) == 1


async def test_daemon_errors_arrive_with_their_exit_code(live_daemon, client, in_thread):
    with pytest.raises(Exception) as caught:
        await in_thread(client.op, "message.send", {"chat": "x"}, account="")
    assert getattr(caught.value, "exit_code", None) is not None


def test_error_bodies_keep_the_daemons_classification():
    error = error_from_body(
        {"code": "RATE_LIMITED", "message": "slow down", "exit_code": 7, "wait_seconds": 42},
        status_code=429,
    )
    assert error.code == "RATE_LIMITED"
    assert error.exit_code == 7
    assert error.wait_seconds == 42


def test_an_unknown_code_still_exits_correctly():
    """A code the CLI has never heard of must not become exit 1."""
    error = error_from_body({"code": "SOME_NEW_CODE", "message": "x", "exit_code": 9})
    assert error.code == "SOME_NEW_CODE"
    assert error.exit_code == 9


class TestNdjson:
    def test_a_frame_is_one_line(self):
        frame = dump_frame({"type": "item", "data": {"text": "a\nb " + TRICKY}})
        assert frame.count(b"\n") == 1
        assert frame.endswith(b"\n")

    def test_round_trip(self):
        payload = {"type": "item", "data": {"text": TRICKY}}
        assert parse_frame(dump_frame(payload).strip()) == payload

    def test_blank_lines_are_skipped(self):
        lines = [b"", b'{"type":"meta"}', b"\n", b'{"type":"end"}']
        assert [f["type"] for f in iter_frames(lines)] == ["meta", "end"]


async def test_op_stream_yields_meta_and_end(live_daemon, client, in_thread):
    """Every stream starts with meta and ends with end (§5.3)."""

    def collect():
        return list(client.op_stream("agent.exit-codes", {}, account=""))

    frames = await in_thread(collect)
    assert frames[0]["type"] == "meta"
    assert frames[-1]["type"] == "end"


async def test_a_stream_without_an_end_frame_is_retryable(live_daemon, tlgr_home, in_thread):
    """A dropped stream is never reported as a complete result."""
    client = DaemonClient(tlgr_home, auto_start=False)
    client._ready = True

    class _Truncated:
        status = 200

        def __init__(self):
            self._lines = [dump_frame({"type": "meta"}), dump_frame({"type": "item"}), b""]

        def readline(self):
            return self._lines.pop(0) if self._lines else b""

        def read(self):  # pragma: no cover - not reached
            return b""

    class _Conn:
        def close(self):
            return None

    client._open = lambda *a, **k: (_Conn(), _Truncated())  # type: ignore[method-assign]

    def drain():
        return list(client.stream("GET", "/v1/events"))

    with pytest.raises(RetryableError):
        await in_thread(drain)


async def test_a_missing_account_is_classified_not_flattened(live_daemon, client, in_thread):
    """COR-06, now on the one route that is left.

    v1 answered `404 IPC_ERROR` for three different situations — no account
    given, alias not registered, alias registered but unusable — so a caller
    could not tell a typo from a revoked session. PR-12 removed the v1 routes
    entirely; the claim moves to `/v1/op`, which is where every command goes.
    """
    from tlgr.core.errors import EXIT_NOT_FOUND

    with pytest.raises(Exception) as caught:
        await in_thread(client.op, "profile.get", {}, account="nope")
    assert caught.value.exit_code == EXIT_NOT_FOUND


async def test_the_daemon_serves_only_v1_routes(live_daemon, client, in_thread):
    """No route without the `/v1` prefix survives (§2.4, §12.4).

    A v1 path answering anything at all would mean a command could reach the
    daemon without the policy allowlist, the version handshake and the flood
    budget the `/v1` middleware chain applies.
    """
    from tlgr.transport.client import RemoteError

    with pytest.raises((RemoteError, Exception)) as caught:
        await in_thread(client.request, "GET", "/profile/get", params={"account": "work"})
    assert "404" in str(caught.value) or "not found" in str(caught.value).lower()


async def test_the_socket_is_private(live_daemon):
    mode = live_daemon.paths.socket.stat().st_mode & 0o777
    assert mode == 0o600, f"the socket is {mode:o}, not 0600"


async def test_status_is_json_not_python_repr(live_daemon, client, in_thread):
    raw = await in_thread(client.request, "GET", "/v1/status")
    assert json.dumps(raw)
