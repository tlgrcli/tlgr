"""NDJSON framing and the server-side `--all` walk (§5.3, ROB-01).

`--all` in v1 was a client loop that re-issued a request per page as fast as
the socket allowed, with no pacing between pages. Walking inside the daemon
means the account's own rate limiter sits between pages, and the caps here are
the difference between a walk that ends and one that becomes a permanent
background load on the account.
"""

from __future__ import annotations

from typing import Any

from tlgr.daemon.stream import MAX_WALK_ITEMS, NdjsonResponse, walk_pages
from tlgr.transport.ndjson import parse_frame

# `asyncio_mode = "auto"` in pyproject collects the async tests; marking
# them explicitly would also mark the synchronous ones in this module.


class _Recorder(NdjsonResponse):
    """An `NdjsonResponse` that keeps its frames instead of writing them."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._ended = False

    async def prepare(self, **meta: Any) -> None:
        await self.write({"type": "meta", **meta})

    async def write(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    async def end(self, **fields: Any) -> Any:
        if not self._ended:
            self._ended = True
            await self.write({"type": "end", **fields})
        return None


class _Page:
    def __init__(self, items: list[Any], has_more: bool, cursor: str | None = None) -> None:
        self.items = items
        self.has_more = has_more
        self.next_cursor = cursor


async def _pages(*pages: _Page) -> Any:
    for page in pages:
        yield page


class _Limiter:
    def __init__(self) -> None:
        self.acquired: list[str] = []

    async def acquire(self, rate_class: str) -> None:
        self.acquired.append(rate_class)


class TestWalk:
    async def test_every_item_is_streamed_with_a_running_sequence(self):
        stream = _Recorder()
        count = await walk_pages(_pages(_Page([1, 2], True, "c1"), _Page([3], False)), stream)
        items = [f for f in stream.frames if f["type"] == "item"]
        assert count == 3
        assert [f["seq"] for f in items] == [1, 2, 3]
        assert [f["data"] for f in items] == [1, 2, 3]

    async def test_each_page_is_announced(self):
        stream = _Recorder()
        await walk_pages(_pages(_Page([1], True, "c1"), _Page([2], False)), stream)
        pages = [f for f in stream.frames if f["type"] == "page"]
        assert pages[0]["has_more"] is True
        assert pages[0]["next_cursor"] == "c1"
        assert pages[-1]["has_more"] is False

    async def test_the_walk_stops_when_the_server_says_there_is_no_more(self):
        stream = _Recorder()
        pages_seen: list[int] = []

        async def source() -> Any:
            for index in range(5):
                pages_seen.append(index)
                yield _Page([index], index < 1)

        await walk_pages(source(), stream)
        assert pages_seen == [0, 1], "the walk kept asking after has_more was false"

    async def test_the_limiter_paces_between_pages(self):
        """ROB-01: the backpressure that v1's client-side loop did not have."""
        limiter = _Limiter()
        stream = _Recorder()
        await walk_pages(
            _pages(_Page([1], True), _Page([2], True), _Page([3], False)),
            stream,
            limiter=limiter,
            rate_class="bulk",
        )
        assert limiter.acquired == ["bulk", "bulk"]

    async def test_the_item_cap_truncates_and_says_so(self):
        stream = _Recorder()

        async def endless() -> Any:
            while True:
                yield _Page(list(range(100)), True, "c")

        count = await walk_pages(endless(), stream, max_items=250)
        assert count == 250
        last = [f for f in stream.frames if f["type"] == "page"][-1]
        assert last["has_more"] is True
        assert "cap" in last["truncated"]

    def test_the_default_cap_is_the_documented_one(self):
        assert MAX_WALK_ITEMS == 100_000

    async def test_a_dict_page_works_as_well_as_a_model(self):
        stream = _Recorder()
        count = await walk_pages(
            _pages({"items": [1, 2], "has_more": False}),
            stream,  # type: ignore[arg-type]
        )
        assert count == 2


class TestFraming:
    async def test_end_is_written_once(self):
        stream = _Recorder()
        await stream.prepare(op="x")
        await stream.end(ok=True, count=0)
        await stream.end(ok=True, count=0)
        assert [f["type"] for f in stream.frames].count("end") == 1

    async def test_every_frame_survives_a_round_trip(self):
        from tlgr.transport.ndjson import dump_frame

        stream = _Recorder()
        await stream.prepare(op="message.list", account="work")
        await walk_pages(_pages(_Page(["سلام #1\nsecond line"], False)), stream)
        await stream.end(ok=True, count=1)
        for frame in stream.frames:
            assert parse_frame(dump_frame(frame).strip()) == frame
