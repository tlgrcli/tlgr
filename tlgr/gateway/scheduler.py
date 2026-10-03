"""The job-action scheduler: one per account, shared by every job on it.

A job's bus handler must never sleep: handlers run on per-chat worker lanes,
so a handler waiting out a 90 second delay would stall every later event in
that chat. A job therefore only *plans* (filters, percent roll, payloads) and
hands the result here as `PendingItem`s. Everything slow happens on this
scheduler's own tasks:

* **delay** - each item has an absolute due time, measured from when the
  daemon received the event, not from the message date, so a backlog replayed
  by catch-up is not "overdue";
* **pacing** - one worker and one `Pacer` per action kind, so a backlog of
  reactions never delays a forward;
* **coalescing** - reads (per chat and topic) and views (per chat) that are
  due together go out as one call;
* **react implies read** - see `ensure_read`;
* **quiet hours** - read, view, react and reply are held until the window
  ends, then released through the pacer;
* **expiry** - react, view and reply give up 24 hours after the event;
* **manual takeover** - when the account reads the chat or writes in it from
  another device, pending items for messages up to that point are dropped
  according to their `on_takeover` mode. tlgr's own reads and sends are told
  apart by the ids it recorded doing them;
* **retries** - FLOOD_WAIT reschedules after the wait and slows the queue;
  a transient failure retries with backoff; anything else is an error;
* **persistence** - every change is written (debounced) to
  `accounts/<alias>/pending.json`, and `resume()` reloads it at boot.

Time comes from an injected clock, so tests run hours of pacing in
milliseconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import logging
import random
import re
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from tlgr.actions import get_builtin
from tlgr.actions.base import Action, Outcome
from tlgr.gateway.knobs import QuietHours
from tlgr.gateway.pacer import DEFAULT_PACING, Pacer, PacingRule, SystemClock
from tlgr.gateway.pending import MAX_PENDING, RECENT_KEYS, PendingItem, PendingStore
from tlgr.gateway.presence import PresenceManager

log = logging.getLogger("tlgr.gateway.scheduler")

__all__ = ["KINDS", "AccountPacing", "ActionScheduler"]

KINDS: tuple[str, ...] = tuple(DEFAULT_PACING)

#: Transient failures: retried this many times, after these waits (seconds).
RETRY_BACKOFF_S = (5.0, 30.0, 120.0)
#: FLOOD_WAITs an item survives before it is counted as an error.
MAX_FLOOD_RETRIES = 10
#: An outgoing message within this long of tlgr's own send is tlgr's echo.
OWN_SEND_WINDOW_S = 10.0
#: Items released at the end of quiet hours are spread over this long.
QUIET_RELEASE_SPREAD_S = 300.0
#: Writes to `pending.json` are coalesced over this long.
SAVE_DEBOUNCE_S = 1.0
_ALBUM_MEMORY = 2000

COUNTERS = ("done", "skipped", "superseded", "expired", "errors")


@dataclass
class AccountPacing:
    """The `pacing.<alias>` block of `jobs.yaml`, parsed."""

    rules: dict[str, PacingRule] = field(default_factory=dict)
    #: Kind -> seconds, or None for "never". Absent kinds use the default.
    expire: dict[str, float | None] = field(default_factory=dict)


@dataclass
class _Album:
    roll: bool
    item_id: str | None = None
    has_caption: bool = False


class ActionScheduler:
    """Delays, paces, persists and executes one account's job actions."""

    def __init__(
        self,
        account: str,
        runner: Callable[[str, dict[str, Any]], Any],
        *,
        store: PendingStore | None = None,
        clock: Any = None,
        rng: random.Random | None = None,
        tz: tzinfo | None = None,
        presence_enabled: bool = True,
    ) -> None:
        self.account = account
        self.runner = runner
        self.store = store
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self.tz = tz
        self.items: dict[str, PendingItem] = {}
        self.pacers: dict[str, Pacer] = {
            kind: Pacer(rule, rng=self.rng) for kind, rule in DEFAULT_PACING.items()
        }
        self.expire: dict[str, float | None] = {}
        self.stats: dict[tuple[str, int], dict[str, Any]] = {}
        self.presence = PresenceManager(
            self._run_op,
            self.clock,
            enabled=presence_enabled,
            session_due_soon=self._session_due_soon,
        )
        self._heaps: dict[str, list[tuple[float, int, str]]] = {kind: [] for kind in KINDS}
        self._wake: dict[str, asyncio.Event] = {kind: asyncio.Event() for kind in KINDS}
        self._seq = itertools.count()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._keys: dict[str, str] = {}
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._albums: OrderedDict[tuple[Any, ...], _Album] = OrderedDict()
        self._quiet: dict[str, QuietHours] = {}
        #: `(chat, topic) -> highest id known read`, by tlgr or by the user.
        self._read_marks: dict[tuple[int, int | None], int] = {}
        #: `(chat, topic) -> highest id tlgr itself asked to read`.
        self._own_read: dict[tuple[int, int | None], int] = {}
        self._own_sent: dict[int, deque[int]] = {}
        self._own_send_at: dict[int, float] = {}
        self._save_handle: asyncio.TimerHandle | None = None
        self._resumed = False
        self._closed = False
        self._full_logged = False

    # -- configuration ----------------------------------------------------

    def configure(self, pacing: AccountPacing | None) -> None:
        pacing = pacing or AccountPacing()
        for kind, default in DEFAULT_PACING.items():
            self.pacers[kind].configure(pacing.rules.get(kind, default))
        self.expire = dict(pacing.expire)

    def expiry_for(self, action: Action) -> float | None:
        if action.name in self.expire:
            return self.expire[action.name]
        return action.default_expiry

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        for kind in KINDS:
            task = self._workers.get(kind)
            if task is None or task.done():
                self._workers[kind] = asyncio.create_task(
                    self._worker(kind), name=f"tlgr-actions:{self.account}:{kind}"
                )

    def close(self) -> None:
        """Start no new action; the one running now may finish."""
        self._closed = True
        for event in self._wake.values():
            event.set()

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Let a running action finish (briefly), stop, and persist the queue."""
        self.close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._running and loop.time() < deadline:
            await asyncio.sleep(0.05)
        for task in [*self._workers.values(), *self._tasks]:
            task.cancel()
        for task in [*self._workers.values(), *self._tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workers.clear()
        self._tasks.clear()
        await self.presence.stop()
        self.flush()

    def resume(self, active_jobs: Iterable[str]) -> int:
        """Reload the persisted queue once, keeping items of jobs that still exist."""
        if self._resumed or self.store is None:
            self._resumed = True
            return 0
        self._resumed = True
        items, recent = self.store.load()
        for key in recent:
            self._recent[key] = None
        active = set(active_jobs)
        restored = dropped = 0
        for item in items:
            if item.job not in active or get_builtin(item.action) is None:
                dropped += 1
                continue
            if item.id in self.items or item.key in self._keys:
                continue
            self._insert(item)
            restored += 1
        if dropped:
            log.info(
                "dropped %d pending action(s) of jobs that no longer exist on %s",
                dropped,
                self.account,
            )
        if restored:
            log.info("resumed %d pending action(s) on %s", restored, self.account)
            self._dirty()
        return restored

    # -- submission -----------------------------------------------------------

    def album_roll(self, key: tuple[Any, ...], percent: int) -> bool:
        """One percent roll per album, so a ten-photo album is not ten rolls."""
        album = self._albums.get(key)
        if album is None:
            album = _Album(roll=self.roll(percent))
            self._albums[key] = album
            while len(self._albums) > _ALBUM_MEMORY:
                self._albums.popitem(last=False)
        return album.roll

    def roll(self, percent: int) -> bool:
        if percent >= 100:
            return True
        if percent <= 0:
            return False
        return self.rng.randrange(100) < percent

    def submit(
        self, item: PendingItem, *, album: tuple[Any, ...] | None = None, has_caption: bool = False
    ) -> str:
        """Queue *item*. Returns `scheduled`, `duplicate`, `album` or `full`."""
        if item.key and (item.key in self._keys or item.key in self._recent):
            return "duplicate"
        if album is not None:
            memo = self._albums.get(album)
            if memo is not None and memo.item_id is not None:
                existing = self.items.get(memo.item_id)
                if (
                    existing is not None
                    and existing.state == "pending"
                    and has_caption
                    and not memo.has_caption
                ):
                    # The caption message is where the album's reaction lives.
                    existing.msg_id = item.msg_id
                    memo.has_caption = True
                    self._dirty()
                self.count(item.job, item.index, item.action, "skipped")
                return "album"
        if len(self.items) >= MAX_PENDING:
            if not self._full_logged:
                log.error(
                    "the action queue for %s is full (%d items); refusing new ones",
                    self.account,
                    MAX_PENDING,
                )
                self._full_logged = True
            self.count(item.job, item.index, item.action, "errors", error="queue full")
            return "full"
        self._full_logged = False
        if album is not None:
            memo = self._albums.setdefault(album, _Album(roll=True))
            memo.item_id = item.id
            memo.has_caption = has_caption
        self._insert(item)
        self._dirty()
        return "scheduled"

    def _insert(self, item: PendingItem) -> None:
        item.state = "pending"
        self.items[item.id] = item
        if item.key:
            self._keys[item.key] = item.id
        self.stats_for(item.job, item.index, item.action)
        self._push(item)

    def _push(self, item: PendingItem) -> None:
        kind = item.action
        heapq.heappush(self._heaps[kind], (item.due_at, next(self._seq), item.id))
        self._wake[kind].set()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:10]

    # -- counters -------------------------------------------------------------

    def stats_for(self, job: str, index: int, action: str) -> dict[str, Any]:
        key = (job, index)
        found = self.stats.get(key)
        if found is None or found.get("action") != action:
            found = {"action": action, **dict.fromkeys(COUNTERS, 0), "last_error": None}
            self.stats[key] = found
        return found

    def count(
        self, job: str, index: int, action: str, outcome: str, *, error: str | None = None
    ) -> None:
        stats = self.stats_for(job, index, action)
        stats[outcome] = int(stats.get(outcome) or 0) + 1
        if error:
            stats["last_error"] = error

    def job_stats(self, job: str) -> list[dict[str, Any]]:
        pending: dict[int, int] = {}
        for item in self.items.values():
            if item.job == job:
                pending[item.index] = pending.get(item.index, 0) + 1
        rows = []
        for (name, index), stats in sorted(self.stats.items(), key=lambda kv: kv[0][1]):
            if name != job:
                continue
            rows.append({"index": index, **stats, "pending": pending.get(index, 0)})
        return rows

    def forget_job(self, job: str) -> int:
        """A job was removed: drop its pending items and its counters."""
        dropped = [item for item in self.items.values() if item.job == job]
        for item in dropped:
            if item.id not in self._running:
                self._remove(item)
        for key in [key for key in self.stats if key[0] == job]:
            del self.stats[key]
        if dropped:
            self._dirty()
        return len(dropped)

    # -- the queue as data ------------------------------------------------------

    def pending(self) -> list[PendingItem]:
        return sorted(self.items.values(), key=lambda item: item.due_at)

    def cancel(
        self,
        *,
        ids: Iterable[str] = (),
        chat_id: int | None = None,
        job: str | None = None,
        everything: bool = False,
    ) -> list[PendingItem]:
        """Drop matching items that are not running; they count as superseded."""
        wanted = set(ids)
        cancelled: list[PendingItem] = []
        for item in list(self.items.values()):
            if item.id in self._running:
                continue
            if not everything:
                if wanted and item.id not in wanted:
                    continue
                if chat_id is not None and item.chat_id != chat_id:
                    continue
                if job is not None and item.job != job:
                    continue
                if not wanted and chat_id is None and job is None:
                    continue
            self._finish(item, "superseded")
            cancelled.append(item)
        return cancelled

    def snapshot(self) -> dict[str, Any]:
        by_kind = dict.fromkeys(KINDS, 0)
        for item in self.items.values():
            by_kind[item.action] = by_kind.get(item.action, 0) + 1
        return {
            "pending": len(self.items),
            "by_action": by_kind,
            "running": len(self._running),
            "presence_online": self.presence.online,
            "pacers": {kind: pacer.snapshot() for kind, pacer in self.pacers.items()},
        }

    # -- persistence ------------------------------------------------------------

    def _dirty(self) -> None:
        if self.store is None or self._save_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        self._save_handle = loop.call_later(SAVE_DEBOUNCE_S, self.flush)

    def flush(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        if self.store is None:
            return
        self.store.save(self.pending(), list(self._recent))

    # -- the workers --------------------------------------------------------------

    def _peek(self, kind: str) -> PendingItem | None:
        heap = self._heaps[kind]
        while heap:
            due, _, item_id = heap[0]
            item = self.items.get(item_id)
            if (
                item is None
                or item.state != "pending"
                or item.due_at != due
                or item.id in self._running
            ):
                heapq.heappop(heap)
                continue
            return item
        return None

    async def _wait(self, kind: str, timeout: float | None) -> None:
        event = self._wake[kind]
        if timeout is None:
            await event.wait()
            return
        if timeout <= 0:
            return
        sleeper = asyncio.ensure_future(self.clock.sleep(timeout))
        waiter = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waiter.cancel()

    async def _worker(self, kind: str) -> None:
        pacer = self.pacers[kind]
        while not self._closed:
            self._wake[kind].clear()
            item = self._peek(kind)
            if item is None:
                await self._wait(kind, None)
                continue
            now = self.clock.now()
            if item.due_at > now:
                await self._wait(kind, item.due_at - now)
                continue
            action = get_builtin(item.action)
            if action is None:
                self._finish(item, "errors", error=f"unknown action {item.action!r}")
                continue
            if item.expires_at is not None and now >= item.expires_at:
                self._finish(item, "expired")
                continue
            if self._hold_for_quiet_hours(item, action, now):
                continue
            if not item.dry_run:
                wait = pacer.delay(now)
                if wait > 0:
                    await self._wait(kind, wait)
                    continue
            batch = self._batch_for(item, action, now)
            if not item.dry_run:
                pacer.consume(now)
            await self._execute(action, batch)

    def _batch_for(self, item: PendingItem, action: Action, now: float) -> list[PendingItem]:
        key = action.batch_key(item)
        if key is None:
            return [item]
        batch = [
            other
            for other in self.items.values()
            if other.action == item.action
            and other.state == "pending"
            and other.id not in self._running
            and other.due_at <= now
            and action.batch_key(other) == key
        ]
        batch.sort(key=lambda other: other.due_at)
        batch = batch[:100]
        return batch if any(other is item for other in batch) else [item, *batch[:99]]

    def _hold_for_quiet_hours(self, item: PendingItem, action: Action, now: float) -> bool:
        if not action.quiet_hold or not item.quiet_hours:
            return False
        quiet = self._quiet.get(item.quiet_hours)
        if quiet is None:
            try:
                quiet = QuietHours.parse(item.quiet_hours)
            except ValueError:
                return False
            self._quiet[item.quiet_hours] = quiet
        local = datetime.fromtimestamp(now, self.tz)
        if not quiet.contains(local):
            return False
        release = quiet.window_end(local).timestamp()
        self._reschedule(item, release + self.rng.uniform(0.0, QUIET_RELEASE_SPREAD_S))
        return True

    def _reschedule(self, item: PendingItem, due_at: float) -> None:
        item.due_at = due_at
        item.state = "pending"
        self._push(item)
        self._dirty()

    async def _execute(self, action: Action, batch: list[PendingItem]) -> None:
        for item in batch:
            item.state = "running"
            self._running.add(item.id)
        first = batch[0]
        try:
            if first.dry_run:
                log.info("[%s] dry run: would %s", first.job, action.describe(batch))
                outcome = Outcome()
            else:
                await self.presence.before(first.presence)
                try:
                    outcome = await action.execute(batch, _Runtime(self))
                finally:
                    await self.presence.after(first.presence)
        except asyncio.CancelledError:
            for item in batch:
                item.state = "pending"
            raise
        except Exception as exc:
            self._failed(action, batch, exc)
            return
        finally:
            for item in batch:
                self._running.discard(item.id)
        for item in batch:
            if outcome.status == "later" and outcome.due_at is not None:
                self._reschedule(item, outcome.due_at)
            else:
                self._finish(item, "skipped" if outcome.status == "skipped" else "done")

    def _failed(self, action: Action, batch: list[PendingItem], exc: BaseException) -> None:
        from tlgr.core.errors import classify

        body = classify(exc)
        now = self.clock.now()
        message = f"{body.code}: {_rpc_name(exc)}{body.message}"
        if body.code == "RATE_LIMITED":
            wait = float(body.wait_seconds or 30)
            self.pacers[action.name].penalize(now, wait)
            log.warning(
                "[%s] %s hit FLOOD_WAIT %ss on %s; rescheduled",
                batch[0].job,
                action.name,
                int(wait),
                self.account,
            )
            for item in batch:
                item.attempts += 1
                item.last_error = message
                if item.attempts > MAX_FLOOD_RETRIES:
                    self._finish(item, "errors", error=message)
                else:
                    self._reschedule(item, now + wait + self.rng.uniform(1.0, 5.0))
            return
        for item in batch:
            item.attempts += 1
            item.last_error = message
            if body.retryable and item.attempts <= len(RETRY_BACKOFF_S):
                backoff = RETRY_BACKOFF_S[item.attempts - 1] * self.rng.uniform(1.0, 1.5)
                log.info(
                    "[%s] %s failed (%s); retrying in %ds",
                    item.job,
                    action.name,
                    message,
                    int(backoff),
                )
                self._reschedule(item, now + backoff)
            else:
                log.warning("[%s] %s failed: %s", item.job, action.name, message)
                self._finish(item, "errors", error=message)

    def _finish(self, item: PendingItem, outcome: str, *, error: str | None = None) -> None:
        self._remove(item)
        if item.key:
            self._recent[item.key] = None
            while len(self._recent) > RECENT_KEYS:
                self._recent.popitem(last=False)
        self.count(item.job, item.index, item.action, outcome, error=error)
        self._dirty()

    def _remove(self, item: PendingItem) -> None:
        self.items.pop(item.id, None)
        if item.key and self._keys.get(item.key) == item.id:
            del self._keys[item.key]

    def _session_due_soon(self, within: float) -> bool:
        horizon = self.clock.now() + within
        return any(
            item.presence == "session" and not item.dry_run and item.due_at <= horizon
            for item in self.items.values()
        )

    async def _run_op(self, op: str, request: dict[str, Any]) -> Any:
        result = self.runner(op, request)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            return await result
        return result

    def _spawn(self, coro: Any, what: str) -> None:
        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s failed: %s", what, exc)

        task = asyncio.create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- reads, sends and takeover ----------------------------------------------------

    def read_mark(self, chat_id: int, topic_id: int | None) -> int:
        return self._read_marks.get((chat_id, topic_id), 0)

    def begin_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None:
        key = (chat_id, topic_id)
        # Before the RPC: the server's echo can arrive before the answer does.
        self._own_read[key] = max(self._own_read.get(key, 0), max_id)

    def note_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None:
        key = (chat_id, topic_id)
        self._read_marks[key] = max(self._read_marks.get(key, 0), max_id)

    def begin_send(self, chat_id: int) -> None:
        self._own_send_at[chat_id] = self.clock.now()

    def note_sent(self, chat_id: int, ids: list[int]) -> None:
        if not chat_id:
            return
        sent = self._own_sent.setdefault(chat_id, deque(maxlen=200))
        sent.extend(i for i in ids if i)
        self._own_send_at[chat_id] = self.clock.now()

    async def ensure_read(self, item: PendingItem) -> None:
        """Read the chat up to *item* before acting on it (react implies read).

        Paced on the read queue, and coalesced: reads already pending for the
        chat at or below this message are satisfied by this one and finish.
        """
        key = (item.chat_id, item.topic_id)
        target = item.msg_id
        if self._read_marks.get(key, 0) < target:
            pacer = self.pacers["read"]
            while (wait := pacer.delay(self.clock.now())) > 0:
                await self.clock.sleep(wait)
            pacer.consume(self.clock.now())
            request: dict[str, Any] = {"chat": str(item.chat_id), "up_to": target}
            if item.topic_id:
                request["topic"] = item.topic_id
            self.begin_read(item.chat_id, item.topic_id, target)
            await self._run_op("message.read", request)
            self.note_read(item.chat_id, item.topic_id, target)
        for other in list(self.items.values()):
            if (
                other.action == "read"
                and not other.dry_run
                and other.id not in self._running
                and (other.chat_id, other.topic_id) == key
                and other.msg_id <= target
                and not other.payload.get("mentions")
                and not other.payload.get("reactions")
            ):
                self._finish(other, "done")

    async def on_bus(self, envelope: Any, raw: Any) -> None:
        """Watch for the account acting in a chat from another device."""
        if raw is None or getattr(envelope, "account", None) != self.account:
            return
        name = type(raw).__name__
        if name == "UpdateReadHistoryInbox":
            from telethon import utils

            chat = int(utils.get_peer_id(raw.peer))
            self.external_read(chat, getattr(raw, "top_msg_id", None), int(raw.max_id))
        elif name == "UpdateReadChannelInbox":
            self.external_read(-1000000000000 - int(raw.channel_id), None, int(raw.max_id))
        elif name == "UpdateReadChannelDiscussionInbox":
            self.external_read(
                -1000000000000 - int(raw.channel_id), int(raw.top_msg_id), int(raw.read_max_id)
            )
        elif name in ("UpdateNewMessage", "UpdateNewChannelMessage"):
            message = raw.message
            if type(message).__name__ == "Message" and getattr(message, "out", False):
                from telethon import utils

                self.outgoing(int(utils.get_peer_id(message.peer_id)), int(message.id))
        elif name == "UpdateShortMessage" and getattr(raw, "out", False):
            self.outgoing(int(raw.user_id), int(raw.id))
        elif name == "UpdateShortChatMessage" and getattr(raw, "out", False):
            self.outgoing(-int(raw.chat_id), int(raw.id))

    def external_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None:
        key = (chat_id, topic_id)
        self._read_marks[key] = max(self._read_marks.get(key, 0), max_id)
        if max_id <= self._own_read.get(key, 0):
            return  # the echo of a read tlgr sent
        self._takeover(chat_id, max_id, "read elsewhere")

    def outgoing(self, chat_id: int, msg_id: int) -> None:
        if msg_id in self._own_sent.get(chat_id, ()):
            return
        sent_at = self._own_send_at.get(chat_id)
        if sent_at is not None and self.clock.now() - sent_at < OWN_SEND_WINDOW_S:
            return
        self._takeover(chat_id, msg_id, "sent from another device")

    def _takeover(self, chat_id: int, up_to: int, why: str) -> None:
        dropped = 0
        for item in list(self.items.values()):
            if item.chat_id != chat_id or item.msg_id > up_to or item.id in self._running:
                continue
            action = get_builtin(item.action)
            if action is None or item.on_takeover not in action.cancelled_by:
                continue
            self._finish(item, "superseded")
            dropped += 1
        if dropped:
            log.info(
                "manual takeover in %s (%s): dropped %d pending action(s)", chat_id, why, dropped
            )


def _rpc_name(exc: BaseException) -> str:
    """`ReactionInvalidError` -> `"REACTION_INVALID: "`, the name the docs use."""
    name = type(exc).__name__
    if getattr(exc, "code", None) is None or not name.endswith("Error") or name == "RPCError":
        return ""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name[: -len("Error")]).upper() + ": "


class _Runtime:
    """The `actions.base.Runtime` an executing action is handed."""

    def __init__(self, scheduler: ActionScheduler) -> None:
        self._s = scheduler
        self.rng = scheduler.rng

    def now(self) -> float:
        return float(self._s.clock.now())

    async def op(self, op: str, request: dict[str, Any]) -> Any:
        return await self._s._run_op(op, request)

    async def ensure_read(self, item: Any) -> None:
        await self._s.ensure_read(item)

    def read_mark(self, chat_id: int, topic_id: int | None) -> int:
        return self._s.read_mark(chat_id, topic_id)

    def begin_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None:
        self._s.begin_read(chat_id, topic_id, max_id)

    def note_read(self, chat_id: int, topic_id: int | None, max_id: int) -> None:
        self._s.note_read(chat_id, topic_id, max_id)

    def begin_send(self, chat_id: int) -> None:
        self._s.begin_send(chat_id)

    def note_sent(self, chat_id: int, ids: list[int]) -> None:
        self._s.note_sent(chat_id, ids)

    def spawn(self, coro: Any, *, what: str) -> None:
        self._s._spawn(coro, what)
