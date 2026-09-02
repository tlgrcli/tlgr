"""`POST /v1/op`: decode → policy → account → dry-run → timeout → impl (§5.2).

One function, in one order, for every operation. That is the whole point of
the registry: v1 had 37 hand-written handlers, so `--dry-run` was honoured by
nine of them (COR-17), the account was resolved differently in three of them
(COR-02), the policy allowlist was checked by none of them (SEC-04), and a
timeout existed in exactly zero (ROB-03).

The order below is not arbitrary:

* **policy before account** — being told "that operation is not enabled"
  should not depend on whether the account exists;
* **account before dry-run** — a dry run that names an account tlgr cannot
  resolve is a lie about what would happen;
* **dry-run before the rate limiter** — a dry run must not spend a token or
  wait on a flood deadline it is never going to hit;
* **the timeout wraps the impl only** — decoding and policy are not the slow
  part, and counting them against the caller's budget makes the timeout mean
  something different for a large request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import msgspec

from tlgr.core.errors import (
    AccountRequiredError,
    RetryableError,
    UsageError,
)
from tlgr.models.base import to_builtins
from tlgr.models.envelope import OpRequest
from tlgr.ops._spec import OperationSpec, Surface

if TYPE_CHECKING:  # pragma: no cover
    from tlgr.daemon.app import Daemon

log = logging.getLogger("tlgr.daemon.dispatch")

__all__ = ["DaemonContext", "decode_request", "dispatch", "resolve_spec"]


@dataclass
class DaemonContext:
    """What an operation implementation is handed inside the daemon.

    Satisfies `ops._spec.OpContext` structurally, and adds the services an
    implementation legitimately needs. Anything an op reaches for that is not
    here is a sign the op is doing the daemon's job.
    """

    account: str
    request_id: str
    dry_run: bool = False
    client: Any = None
    session: Any = None
    #: The per-account entity resolver (§6.6). Every peer an implementation
    #: touches goes through this, so the NOT_FOUND / INDETERMINATE
    #: distinction is made in one place rather than per operation.
    resolver: Any = None
    daemon: Any = None
    limiter: Any = None
    bus: Any = None
    paths: Any = None
    config: Any = None
    flood_wait_max: int | None = None
    limit: int | None = None
    cursor: str | None = None
    fetch_all: bool = False
    warnings: list[str] = field(default_factory=list)
    flood_wait_slept: int = 0
    already: bool = False

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    async def upload_file(self, path: Any, **kwargs: Any) -> Any:
        """Upload a local file and return the `InputFile` handle.

        The file pipeline lives in `daemon/files.py` and `ops/` may not import
        `daemon/` (§2.2), so the daemon hands it over the same way it hands
        over the resolver and the limiter: as a service on the context.
        """
        from pathlib import Path

        from tlgr.daemon.files import UploadPlan, upload

        return await upload(self.client, UploadPlan(source=Path(path), **kwargs))

    def mark_already(self) -> None:
        """Record that the world already looked the way the caller asked for."""
        self.already = True

    def emit(self, event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        """Echo an action tlgr itself performed onto the bus (§6.5).

        Telethon does not dispatch `NewMessage` for our own sends, so without
        this a `tlgr watch` never shows what tlgr just did and a gateway rule
        cannot react to it.
        """
        if self.bus is not None:
            self.bus.emit(self.account, event_type, payload, self_origin=True, **kwargs)


def decode_request(raw: bytes) -> OpRequest:
    try:
        return msgspec.json.decode(raw or b"{}", type=OpRequest)
    except msgspec.ValidationError as exc:
        raise UsageError(str(exc)) from exc
    except msgspec.DecodeError as exc:
        raise UsageError(f"request body is not JSON: {exc}") from exc


def resolve_spec(op_id: str) -> OperationSpec:
    """Canonicalise an id or alias into its spec, or raise USAGE."""
    from tlgr.registry import REGISTRY, canonical

    try:
        resolved = canonical(op_id)
    except Exception as exc:
        raise UsageError(f"unknown operation {op_id!r}", field="op") from exc
    spec = REGISTRY.get(resolved)
    if spec is None:
        raise UsageError(f"unknown operation {op_id!r}", field="op")
    return spec


def decode_payload(spec: OperationSpec, payload: dict[str, Any]) -> Any:
    """Decode the op's request struct, turning a mismatch into a field error.

    msgspec ends a validation message with ` - at $.chat.kind`; that suffix is
    what makes the error actionable, and `classify()` lifts it into
    `error.field` for us.
    """
    try:
        return msgspec.convert(payload, type=spec.request, strict=False)
    except msgspec.ValidationError as exc:
        raise UsageError(str(exc)) from exc


async def dispatch(daemon: Daemon, request: OpRequest) -> dict[str, Any]:
    """Run one operation and return its success envelope."""
    started = time.monotonic()
    spec, context, result = await execute(daemon, request)
    return _envelope(
        spec, context.account, result, context, started, dry_run=context.dry_run and spec.mutating
    )


async def execute(daemon: Daemon, request: OpRequest) -> tuple[OperationSpec, DaemonContext, Any]:
    """The prologue and the implementation, without the envelope.

    Split out so that a streaming response can take the *result* — which may
    be an async page iterator — instead of a dict that has already been
    flattened. Both paths therefore run the identical policy, account,
    dry-run, rate-limit and timeout sequence; there is no second order to get
    wrong.
    """
    spec = resolve_spec(request.op)

    daemon.policy.enforce(spec.id, spec)

    account = (request.account or "").strip()
    if spec.needs_account and not account:
        # The daemon never picks. v1 used "whichever alias came first out of a
        # set", so a two-account user could send from the wrong identity with
        # no signal at all (COR-02).
        raise AccountRequiredError(
            f"{spec.id} needs an account and none was given",
        )

    payload = decode_payload(spec, request.request)

    context = DaemonContext(
        account=account,
        request_id=request.request_id,
        dry_run=request.dry_run,
        daemon=daemon,
        bus=daemon.bus,
        paths=daemon.paths,
        config=daemon.config,
        flood_wait_max=request.flood_wait_max,
        limit=request.limit,
        cursor=request.cursor,
        fetch_all=request.all,
    )

    if request.dry_run and spec.mutating:
        return spec, context, {"dry_run": True, "would": spec.id, "request": to_builtins(payload)}

    if spec.surface is not Surface.LOCAL and spec.needs_account:
        session = await daemon.sessions.ensure(account)
        limiter = daemon.sessions.limiter(account)
        limiter.check(rate_class=spec.rate_class)
        context.session = session
        context.limiter = limiter
        context.client = await session.acquire(timeout=spec.timeout_s)
        resolver = session.resolver
        resolver.limiter = limiter
        context.resolver = resolver
        session.in_flight += 1
    else:
        session = None

    budget: Any = None
    if session is not None and context.limiter is not None:
        budget = context.limiter.sleep_budget(request.flood_wait_max, float(spec.timeout_s))
    try:
        if spec.rate_class != "local" and context.limiter is not None:
            await context.limiter.acquire(spec.rate_class)
        if spec.min_interval_s:
            await asyncio.sleep(spec.min_interval_s)
        with _budget(session, budget):
            call = spec.impl(context, payload)
            # A streaming implementation returns an async iterator rather than
            # a coroutine; the caller walks it, and the timeout then bounds
            # each page rather than the whole walk (which may legitimately
            # take an hour).
            if hasattr(call, "__aiter__"):
                result: Any = call
            else:
                result = await asyncio.wait_for(call, timeout=spec.timeout_s)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise RetryableError(
            f"{spec.id} did not finish within {spec.timeout_s}s and was cancelled"
        ) from exc
    finally:
        if session is not None:
            session.in_flight = max(0, session.in_flight - 1)

    return spec, context, result


def _envelope(
    spec: OperationSpec,
    account: str,
    result: Any,
    context: DaemonContext,
    started: float,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    from tlgr import __version__
    from tlgr.version import PROTOCOL

    body = to_builtins(result) if result is not None else None
    envelope: dict[str, Any] = {
        "ok": True,
        "op": spec.id,
        "account": account or None,
        "result": body,
        "meta": {
            "request_id": context.request_id,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "flood_wait_slept": context.flood_wait_slept,
            "warnings": context.warnings,
            "already": context.already,
            "daemon_version": __version__,
            "protocol": PROTOCOL,
        },
    }
    if dry_run:
        envelope["meta"]["dry_run"] = True
    if spec.paginated is not None and isinstance(body, dict) and "items" in body:
        envelope["result"] = body.get("items")
        envelope["page"] = {
            "has_more": bool(body.get("has_more")),
            "next_cursor": body.get("next_cursor"),
            "total": body.get("total"),
        }
    return envelope


@contextlib.contextmanager
def _budget(session: Any, seconds: int | None) -> Any:
    """`session.flood_budget`, but a no-op for a local operation."""
    if session is None or seconds is None:
        yield
        return
    with session.flood_budget(seconds):
        yield


@contextlib.contextmanager
def in_flight(daemon: Daemon) -> Any:
    """Count a request for idle accounting, and always uncount it (COR-11)."""
    daemon.activity.begin_request()
    try:
        yield
    finally:
        daemon.activity.end_request()
