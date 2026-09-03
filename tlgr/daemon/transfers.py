"""The Downloads panel: transfers the daemon is running, or just ran.

There is no server-side list of a user's transfers, in tlgr or in any official
client — the GUI's Downloads section is client-local state too. What the
daemon adds over "just await the coroutine" is the three things a long
transfer needs and a request/response cycle cannot give it:

* **a name for it while it runs.** `--background` returns a job id
  immediately, and `media transfer list` is how you find out how far it got.
* **cancellation between chunks.** A download stops cleanly and keeps its
  `.part` file, so `--resume` continues it; an upload cannot be resumed at
  all (saved parts expire server-side), so it restarts with a fresh file id.
* **a retry that re-fetches first.** A transfer that sat in the failed queue
  for an hour is holding an expired `file_reference`; retrying with the stale
  one fails identically, so the factory is re-run rather than the task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("tlgr.daemon.transfers")

__all__ = ["TransferRecord", "TransferStore"]

#: How many finished transfers stay visible. The panel is a recent history,
#: not an archive; keeping every transfer forever would make the daemon's
#: memory a function of how much the user has ever downloaded.
KEEP_FINISHED = 100


@dataclass
class TransferRecord:
    job_id: str
    direction: str
    name: str = ""
    chat_id: int | None = None
    msg_id: int | None = None
    bytes_done: int = 0
    bytes_total: int = 0
    state: str = "queued"
    path: str | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    started_at: str = ""
    task: asyncio.Task[Any] | None = None
    factory: Callable[[], Awaitable[Any]] | None = None
    result: Any = None

    @property
    def pct(self) -> float:
        if not self.bytes_total:
            return 0.0
        return round(100.0 * self.bytes_done / self.bytes_total, 1)

    @property
    def bps(self) -> int:
        elapsed = max(1e-6, time.monotonic() - self.started)
        return int(self.bytes_done / elapsed)

    @property
    def eta_s(self) -> int | None:
        rate = self.bps
        if not rate or not self.bytes_total or self.bytes_done >= self.bytes_total:
            return None
        return int((self.bytes_total - self.bytes_done) / rate)

    def progress(self, done: int, total: int) -> None:
        self.bytes_done = done
        self.bytes_total = total or self.bytes_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "direction": self.direction,
            "chat_id": self.chat_id,
            "msg_id": self.msg_id,
            "name": self.name,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "pct": self.pct,
            "bps": self.bps,
            "eta_s": self.eta_s,
            "state": self.state,
            "path": self.path,
            "error": self.error,
            "started": self.started_at or None,
        }


class TransferStore:
    """Every transfer this daemon started, keyed by job id."""

    def __init__(self) -> None:
        self._records: dict[str, TransferRecord] = {}

    # -- writing -----------------------------------------------------------

    def submit(
        self,
        *,
        direction: str,
        name: str,
        factory: Callable[[], Awaitable[Any]],
        chat_id: int | None = None,
        msg_id: int | None = None,
    ) -> TransferRecord:
        """Register a transfer and start it in the background."""
        from datetime import datetime, timezone

        record = TransferRecord(
            job_id=secrets.token_hex(3),
            direction=direction,
            name=name,
            chat_id=chat_id,
            msg_id=msg_id,
            factory=factory,
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._records[record.job_id] = record
        self._start(record)
        self._trim()
        return record

    def _start(self, record: TransferRecord) -> None:
        record.state = "running"
        record.error = None
        record.started = time.monotonic()

        async def run() -> None:
            try:
                record.result = await record.factory()  # type: ignore[misc]
            except asyncio.CancelledError:
                record.state = "cancelled"
                raise
            except Exception as exc:
                record.state = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
                log.warning("transfer %s failed: %s", record.job_id, exc)
            else:
                record.state = "done"
                path = getattr(record.result, "path", None)
                if isinstance(path, str):
                    record.path = path

        record.task = asyncio.get_event_loop().create_task(run(), name=f"tlgr-tx-{record.job_id}")

    def _trim(self) -> None:
        finished = [r for r in self._records.values() if r.state in ("done", "failed", "cancelled")]
        for record in sorted(finished, key=lambda r: r.started)[:-KEEP_FINISHED]:
            self._records.pop(record.job_id, None)

    async def cancel(self, job_ids: list[str], *, keep_partial: bool = True) -> int:
        """Stop the named transfers, and forget their `.part` when asked to."""
        cancelled = 0
        for job_id in job_ids:
            record = self._records.get(job_id)
            if record is None or record.state not in ("queued", "running"):
                continue
            if record.task is not None:
                record.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await record.task
            record.state = "cancelled"
            cancelled += 1
            if not keep_partial and record.path:
                from pathlib import Path

                with contextlib.suppress(OSError):
                    Path(record.path + ".part").unlink()
        return cancelled

    def retry(self, job_ids: list[str], *, from_scratch: bool = False) -> tuple[int, int]:
        """Re-run the factory, which re-fetches the source before it reads bytes."""
        restarted = 0
        resumed_from = 0
        for job_id in job_ids:
            record = self._records.get(job_id)
            if record is None or record.factory is None:
                continue
            if record.state in ("queued", "running"):
                continue
            resumed_from = max(resumed_from, 0 if from_scratch else record.bytes_done)
            record.bytes_done = 0 if from_scratch else record.bytes_done
            self._start(record)
            restarted += 1
        return restarted, resumed_from

    # -- reading -----------------------------------------------------------

    def get(self, job_id: str) -> TransferRecord | None:
        return self._records.get(job_id)

    def snapshot(self, *, active: bool = False, failed: bool = False) -> list[dict[str, Any]]:
        records = list(self._records.values())
        if active:
            records = [r for r in records if r.state in ("queued", "running")]
        if failed:
            records = [r for r in records if r.state == "failed"]
        return [record.to_dict() for record in sorted(records, key=lambda r: -r.started)]

    async def settle(self, *, timeout: float = 30.0) -> None:
        """Wait for the running transfers, so `--watch` can report the end."""
        tasks = [r.task for r in self._records.values() if r.task is not None and not r.task.done()]
        if not tasks:
            return
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)

    async def stop_all(self) -> None:
        """Cancel everything still running, at shutdown."""
        await self.cancel([r.job_id for r in self._records.values()], keep_partial=True)
