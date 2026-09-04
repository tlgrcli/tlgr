"""The middleware chain and the dispatcher, in the order §5.2 fixes.

Each test here corresponds to something v1 got wrong once per handler rather
than once: the account it chose for you (COR-02), the `--dry-run` nine of
twenty-one commands honoured (COR-17), the allowlist no handler consulted
(SEC-04), and the timeout none of them had (ROB-03).
"""

from __future__ import annotations

import asyncio
from typing import Any

import msgspec
import pytest

from tlgr.core.errors import (
    EXIT_PERMISSION,
    EXIT_RETRYABLE,
    EXIT_USAGE,
)
from tlgr.daemon import dispatch as dispatch_module
from tlgr.daemon.policy import Policy
from tlgr.models.base import Request
from tlgr.models.envelope import OpRequest
from tlgr.ops._spec import OperationSpec, Surface
from tlgr.registry import ALIASES, REGISTRY

# `asyncio_mode = "auto"` in pyproject collects the async tests; marking
# them explicitly would also mark the synchronous ones in this module.


class Echo(Request):
    text: str = ""


#: Every registered test operation's recorded calls, by op id. `OperationSpec`
#: is a frozen slots dataclass, so the record cannot hang off the spec.
CALLS: dict[str, list[Any]] = {}


@pytest.fixture
def op():
    """Register a throwaway operation for the duration of one test."""
    registered: list[str] = []

    def make(op_id: str, impl: Any = None, **kwargs: Any) -> OperationSpec:
        calls: list[Any] = CALLS.setdefault(op_id, [])

        async def echo(context: Any, payload: Any) -> dict[str, Any]:
            calls.append((context, payload))
            return {"echo": getattr(payload, "text", "")}

        spec = OperationSpec(
            id=op_id,
            request=Echo,
            response=dict,
            impl=impl or echo,
            summary="echo it back",
            surface=kwargs.pop("surface", Surface.LOCAL),
            needs_account=kwargs.pop("needs_account", False),
            **kwargs,
        )
        REGISTRY[spec.id] = spec
        for name in spec.names:
            ALIASES[name] = spec.id
        registered.append(spec.id)
        return spec

    yield make

    for op_id in registered:
        CALLS.pop(op_id, None)
        spec = REGISTRY.pop(op_id, None)
        if spec is not None:
            for name in spec.names:
                ALIASES.pop(name, None)


def _request(op_id: str, **kwargs: Any) -> OpRequest:
    return OpRequest(op=op_id, request_id="test", **kwargs)


async def test_an_unknown_operation_is_a_usage_error(daemon, op):
    from tlgr.core.errors import classify

    with pytest.raises(Exception) as caught:
        await dispatch_module.dispatch(daemon, _request("nope.nothing"))
    assert classify(caught.value).exit_code == EXIT_USAGE


async def test_an_operation_that_needs_an_account_refuses_to_guess(daemon, op):
    """COR-02: v1 used whichever alias came first out of a set."""
    from tlgr.core.errors import classify

    op("test.needs", surface=Surface.LOCAL, needs_account=True)
    with pytest.raises(Exception) as caught:
        await dispatch_module.dispatch(daemon, _request("test.needs"))
    body = classify(caught.value)
    assert body.code == "ACCOUNT_REQUIRED"
    assert body.exit_code == EXIT_USAGE


async def test_the_policy_is_checked_by_canonical_id_including_aliases(daemon, op):
    """SEC-04: an allowlist written against ids also covers the aliases."""
    from tlgr.core.errors import classify

    op("test.blocked", aliases=("blocked",))
    daemon.policy = Policy.parse("test.allowed")

    for name in ("test.blocked", "blocked"):
        with pytest.raises(Exception) as caught:
            await dispatch_module.dispatch(daemon, _request(name))
        assert classify(caught.value).exit_code == EXIT_PERMISSION


async def test_an_allowed_alias_passes_the_policy(daemon, op):
    op("test.allowed", aliases=("allowed",))
    daemon.policy = Policy.parse("test.allowed")
    envelope = await dispatch_module.dispatch(daemon, _request("allowed"))
    assert envelope["ok"] is True


async def test_deny_beats_allow(daemon, op):
    from tlgr.core.errors import classify

    op("test.both")
    daemon.policy = Policy(allow=["*"], deny=["test.both"])
    with pytest.raises(Exception) as caught:
        await dispatch_module.dispatch(daemon, _request("test.both"))
    assert classify(caught.value).exit_code == EXIT_PERMISSION


async def test_a_behaviour_class_can_be_denied_wholesale(daemon, op):
    """ "read anything, change nothing" is one entry, not five hundred."""
    from tlgr.core.errors import classify

    op("test.reads")
    op("test.writes", mutating=True)
    daemon.policy = Policy(allow=["*"], deny=["*:mutating"])

    assert (await dispatch_module.dispatch(daemon, _request("test.reads")))["ok"] is True
    with pytest.raises(Exception) as caught:
        await dispatch_module.dispatch(daemon, _request("test.writes"))
    assert classify(caught.value).exit_code == EXIT_PERMISSION


async def test_dry_run_short_circuits_before_the_implementation(daemon, op):
    """COR-17: uniform, because the dispatcher does it, not each handler."""
    op("test.mutates", mutating=True)
    envelope = await dispatch_module.dispatch(
        daemon, _request("test.mutates", dry_run=True, request={"text": "hi"})
    )
    assert envelope["result"]["dry_run"] is True
    assert envelope["result"]["would"] == "test.mutates"
    assert CALLS["test.mutates"] == [], "the implementation ran during a dry run"


async def test_dry_run_does_not_short_circuit_a_read(daemon, op):
    op("test.reads2")
    envelope = await dispatch_module.dispatch(daemon, _request("test.reads2", dry_run=True))
    assert envelope["ok"] is True
    assert len(CALLS["test.reads2"]) == 1


async def test_a_bad_request_field_is_named(daemon, op):
    """msgspec's ` - at $.text` suffix becomes `error.field`."""
    from tlgr.core.errors import classify

    op("test.typed")
    with pytest.raises(Exception) as caught:
        await dispatch_module.dispatch(daemon, _request("test.typed", request={"text": 5}))
    body = classify(caught.value)
    assert body.code == "USAGE"
    assert body.field == "text"


async def test_an_unknown_request_field_is_rejected(daemon, op):
    """A newer CLI must not silently lose a field against an older daemon."""
    op("test.strict")
    with pytest.raises(Exception):
        await dispatch_module.dispatch(daemon, _request("test.strict", request={"nope": 1}))


async def test_a_timeout_is_retryable_not_generic(daemon, op):
    from tlgr.core.errors import classify

    async def slow(context: Any, payload: Any) -> None:
        await asyncio.sleep(5)

    # The lint requires timeout_s >= 5, so the spec is registered legally and
    # the value is lowered afterwards rather than a bad spec being built.
    spec = op("test.slow", impl=slow)
    object.__setattr__(spec, "timeout_s", 0.05)

    with pytest.raises(Exception) as caught:
        await dispatch_module.dispatch(daemon, _request("test.slow"))
    assert classify(caught.value).exit_code == EXIT_RETRYABLE


async def test_the_envelope_carries_meta(daemon, op):
    op("test.meta")
    envelope = await dispatch_module.dispatch(daemon, _request("test.meta"))
    meta = envelope["meta"]
    assert meta["request_id"] == "test"
    assert meta["protocol"] == 2
    assert "elapsed_ms" in meta


async def test_warnings_reach_the_caller(daemon, op):
    async def warns(context: Any, payload: Any) -> dict[str, Any]:
        context.warn("--schedule interpreted as 2026-09-03T06:00:00Z")
        return {}

    op("test.warns", impl=warns)
    envelope = await dispatch_module.dispatch(daemon, _request("test.warns"))
    assert envelope["meta"]["warnings"] == ["--schedule interpreted as 2026-09-03T06:00:00Z"]


async def test_a_daemon_operation_gets_a_client_and_a_resolver(daemon, op, stub_account):
    seen: dict[str, Any] = {}

    async def impl(context: Any, payload: Any) -> dict[str, Any]:
        seen["client"] = context.client
        seen["resolver"] = context.resolver
        seen["limiter"] = context.limiter
        return {}

    op("test.daemon", impl=impl, surface=Surface.DAEMON, needs_account=True)
    await dispatch_module.dispatch(daemon, _request("test.daemon", account=stub_account))
    assert seen["client"] is not None
    assert seen["resolver"] is not None
    assert seen["limiter"] is not None


async def test_in_flight_is_counted_and_released(daemon, op, stub_account):
    """COR-11: the counter is what stops the idle monitor mid-scan."""
    depth: list[int] = []

    async def impl(context: Any, payload: Any) -> dict[str, Any]:
        depth.append(context.session.in_flight)
        return {}

    op("test.inflight", impl=impl, surface=Surface.DAEMON, needs_account=True)
    await dispatch_module.dispatch(daemon, _request("test.inflight", account=stub_account))
    assert depth == [1]
    assert daemon.sessions.get(stub_account).in_flight == 0


def test_decode_request_rejects_garbage():
    with pytest.raises(Exception):
        dispatch_module.decode_request(b"not json")


def test_decode_request_needs_an_op():
    """`op` has no default: an empty body is a USAGE error naming the field."""
    from tlgr.core.errors import classify

    with pytest.raises(Exception) as caught:
        dispatch_module.decode_request(b"{}")
    assert classify(caught.value).code == "USAGE"


class TestMiddleware:
    """The chain applies to the v1 routes too (§12.4)."""

    async def test_a_wrong_uid_is_refused(self, live_daemon, client, in_thread, monkeypatch):
        from tlgr.core.errors import EXIT_PERMISSION, classify
        from tlgr.daemon import app as app_module
        from tlgr.daemon.peercred import Peer

        monkeypatch.setattr(app_module, "peer_of", lambda sock: Peer(uid=999999, pid=4242))
        client._ready = True  # skip the probe; the refusal is what we are testing
        with pytest.raises(Exception) as caught:
            await in_thread(client.request, "GET", "/v1/status")
        assert classify(caught.value).exit_code == EXIT_PERMISSION
        assert "own user" in str(caught.value)

    async def test_requests_are_refused_while_shutting_down(self, live_daemon, client, in_thread):
        from tlgr.core.errors import classify

        live_daemon.shutting_down.set()
        try:
            with pytest.raises(Exception) as caught:
                await in_thread(client.request, "POST", "/v1/admin/reload", body={})
            assert classify(caught.value).exit_code == EXIT_RETRYABLE
        finally:
            live_daemon.shutting_down.clear()

    async def test_status_answers_even_while_shutting_down(self, live_daemon, client, in_thread):
        """Otherwise a CLI cannot tell "stopping" from "crashed"."""
        live_daemon.shutting_down.set()
        try:
            status = await in_thread(client.status)
            assert status["daemon"]["shutting_down"] is True
        finally:
            live_daemon.shutting_down.clear()

    async def test_an_older_client_protocol_is_refused(self, live_daemon, tlgr_home, in_thread):
        from tlgr.core.errors import EXIT_DAEMON, classify
        from tlgr.transport.client import DaemonClient
        from tlgr.version import HEADER_PROTOCOL

        client = DaemonClient(tlgr_home, auto_start=False)
        client._ready = True
        original = client._headers
        client._headers = lambda rid, *, body: {**original(rid, body=body), HEADER_PROTOCOL: "1"}

        with pytest.raises(Exception) as caught:
            await in_thread(client.request, "POST", "/v1/admin/reload", body={})
        assert classify(caught.value).exit_code == EXIT_DAEMON


async def test_the_only_error_shape_is_the_wrapped_one(live_daemon, client, in_thread):
    """One surface, one shape (§7.1).

    Until PR-12 this asserted two: the v1 routes answered a flat
    `{code, exit_code, error}` body and `/v1/op` answered the wrapped one.
    The v1 routes are gone, so the flat shape has nothing left to describe —
    what survives is that an unknown operation is a USAGE error inside the
    envelope, and that a v1 path is simply not a route any more.

    The raw body is read here rather than through `client.request`, which
    would raise, and everything goes through `in_thread`: the transport is
    synchronous and would otherwise block the loop the daemon runs on.
    """
    import json

    def raw(method: str, path: str, body: bytes | None = None) -> tuple[int, Any]:
        conn, response = client._open(method, path, body=body)
        try:
            payload = response.read()
            try:
                return response.status, json.loads(payload)
            except json.JSONDecodeError:
                return response.status, payload
        finally:
            conn.close()

    client._ready = True
    status, _ = await in_thread(raw, "GET", "/profile/get?account=nope")
    assert status == 404, "a v1 route must not be served at all"

    _, v2 = await in_thread(raw, "POST", "/v1/op", msgspec.json.encode({"op": "nope.nothing"}))
    assert v2["ok"] is False
    assert v2["error"]["code"] == "USAGE"
