"""A virtual clock for the job-action scheduler: time moves only when a test says.

`sleep()` parks the caller on a heap of wake times; `advance()` walks the heap
in order, setting `now` to each wake time before releasing the sleeper, and
lets the event loop run between steps. A pacer that waits four seconds
therefore waits exactly four virtual seconds, and a test about a whole hour of
reactions takes milliseconds.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools

__all__ = ["FakeClock", "settle"]


async def settle(rounds: int = 200) -> None:
    """Let every runnable task run until the loop goes quiet."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = float(start)
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._now + max(0.0, seconds), next(self._seq), future))
        await future

    async def advance(self, seconds: float, *, rounds: int = 200) -> None:
        target = self._now + seconds
        await settle(rounds)
        while self._sleepers and self._sleepers[0][0] <= target:
            wake, _, future = heapq.heappop(self._sleepers)
            if future.done():
                continue
            self._now = max(self._now, wake)
            future.set_result(None)
            await settle(rounds)
        self._now = target
        await settle(rounds)

    async def run_for(self, seconds: float, *, step: float = 0.5) -> None:
        """Advance in small steps, so work that sleeps again keeps getting woken."""
        elapsed = 0.0
        while elapsed < seconds:
            chunk = min(step, seconds - elapsed)
            await self.advance(chunk)
            elapsed += chunk
