"""NDJSON responses: `--all` walks, progress, and the event stream (§5.3/§5.4).

Two rules make a stream trustworthy and both were missing in v1:

* **exactly one `meta` first and exactly one `end` last.** A stream that ends
  without an `end` frame is a failure, not a short result — the client raises
  `RETRYABLE` rather than reporting "there were 400 items" when there were
  4,120 and the connection dropped.
* **the walk happens here, inside the daemon.** v1's `--all` was a client
  loop that re-issued a request per page as fast as the socket allowed, with
  no account pacing between pages (ROB-01). Walking server-side means the
  account's own rate limiter sits between pages, and a 10k-dialog enumeration
  is one request with backpressure instead of a hundred without.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import web

from tlgr.core.errors import classify, error_body_dict
from tlgr.models.base import to_builtins
from tlgr.transport.ndjson import dump_frame
from tlgr.version import PROTOCOL

log = logging.getLogger("tlgr.daemon.stream")

__all__ = ["NdjsonResponse", "walk_pages"]

#: §13.5 caps: a walk that has produced this much is stopped with a warning
#: rather than running until the client gives up.
MAX_WALK_ITEMS = 100_000
MAX_WALK_SECONDS = 3600


class NdjsonResponse:
    """A chunked `application/x-ndjson` response with the framing rules baked in."""

    def __init__(self, request: web.Request) -> None:
        self._request = request
        self._response: web.StreamResponse | None = None
        self._ended = False

    async def prepare(self, **meta: Any) -> None:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/x-ndjson",
                "Cache-Control": "no-store",
            },
        )
        response.enable_chunked_encoding()
        await response.prepare(self._request)
        self._response = response
        await self.write({"type": "meta", "protocol": PROTOCOL, **meta})

    async def write(self, frame: dict[str, Any]) -> None:
        if self._response is None:  # pragma: no cover - prepare() comes first
            raise RuntimeError("the NDJSON response was not prepared")
        await self._response.write(dump_frame(frame))

    async def end(self, **fields: Any) -> web.StreamResponse:
        """Write the single `end` frame and close. Idempotent."""
        if self._response is None:  # pragma: no cover
            raise RuntimeError("the NDJSON response was not prepared")
        if not self._ended:
            self._ended = True
            await self.write({"type": "end", **fields})
            with contextlib.suppress(Exception):
                await self._response.write_eof()
        return self._response

    async def fail(self, exc: BaseException, *, account: str = "") -> web.StreamResponse:
        body = error_body_dict(classify(exc, account=account or None))
        return await self.end(ok=False, error=body)

    @property
    def started(self) -> bool:
        return self._response is not None


async def walk_pages(
    pages: AsyncIterator[Any],
    stream: NdjsonResponse,
    *,
    limiter: Any = None,
    rate_class: str = "read",
    max_items: int = MAX_WALK_ITEMS,
    max_seconds: int = MAX_WALK_SECONDS,
) -> int:
    """Stream every item from an async page iterator, paced by the limiter.

    Returns the number of items written. The caps are not a policy about what
    a user may ask for; they are the difference between a walk that ends and
    one that quietly becomes a permanent background load on the account.
    """
    started = time.monotonic()
    count = 0
    async for page in pages:
        # `_attr` checks the dict case first: `getattr({}, "items")` is the
        # dict *method*, which iterates into a very confusing TypeError.
        items = _attr(page, "items")
        for item in items or ():
            count += 1
            await stream.write({"type": "item", "seq": count, "data": to_builtins(item)})
            if count >= max_items:
                await stream.write(
                    {
                        "type": "page",
                        "has_more": True,
                        "next_cursor": _cursor_of(page),
                        "fetched": count,
                        "truncated": f"stopped at the {max_items} item cap",
                    }
                )
                return count
        await stream.write(
            {
                "type": "page",
                "has_more": bool(_attr(page, "has_more")),
                "next_cursor": _cursor_of(page),
                "fetched": count,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )
        if not _attr(page, "has_more"):
            break
        if time.monotonic() - started > max_seconds:
            await stream.write(
                {"type": "page", "has_more": True, "truncated": "stopped at the time cap"}
            )
            break
        if limiter is not None:
            # Backpressure lives here: the next page waits for the account's
            # own bucket, so a walk paces itself instead of racing the server.
            await limiter.acquire(rate_class)
    return count


def _attr(page: Any, name: str) -> Any:
    if isinstance(page, dict):
        return page.get(name)
    return getattr(page, name, None)


def _cursor_of(page: Any) -> Any:
    return _attr(page, "next_cursor")


async def pump_events(
    stream: NdjsonResponse,
    subscriber: Any,
    *,
    heartbeat: float = 15.0,
    timeout: float = 3600.0,
    shutdown: asyncio.Event | None = None,
) -> str:
    """Deliver events until the timeout, the client leaves, or we shut down.

    Returns the reason, which the caller puts in the `end` frame — "the
    stream ended" without saying why is exactly the ambiguity that makes a
    consumer guess whether to reconnect.
    """
    deadline = time.monotonic() + timeout
    while True:
        if shutdown is not None and shutdown.is_set():
            return "shutdown"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            event = await asyncio.wait_for(
                subscriber.queue.get(), timeout=min(heartbeat, remaining)
            )
        except (TimeoutError, asyncio.TimeoutError):
            await stream.write({"type": "heartbeat", "ts": _now()})
            continue
        lag = subscriber.take_lag()
        if lag:
            await stream.write({"type": "lag", "dropped": lag})
        await stream.write(to_builtins(event))


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
