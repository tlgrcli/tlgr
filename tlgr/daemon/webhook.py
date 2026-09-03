"""Outbound webhook delivery — a bus subscriber, not an update handler.

Four things were wrong with v1 and all four are structural:

* **COR-07.** The payload was built with `json.dumps(..., default=str)` over
  a raw Telethon `to_dict()`. A `datetime` became a string in one place and a
  `bytes` blew up in another, so a message with media could fail to serialise
  *at delivery time*, be counted as a delivery failure, and be retried three
  times before being dead-lettered. Payloads are now models encoded with
  msgspec, and a serialisation failure is logged as a bug — never retried.
* **ROB-02.** The POST happened inside the Telethon handler. One unreachable
  endpoint with three retries and exponential backoff held the update loop for
  ~97 s, during which every account was deaf. Delivery now happens on the
  bus's worker lanes, behind a bounded queue.
* **SEC-08.** There was no signature: any process that learned the URL could
  forge events. Every delivery now carries `X-Tlgr-Signature: sha256=<hmac of
  the exact body>` and a monotonic `seq`, so a receiver can both authenticate
  and order what it gets.
* **SEC-06.** Dead letters were appended to a world-readable file that grew
  forever, with full message text in it. They are 0600, rotated, and capped.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

import msgspec

from tlgr.core.config import CONFIG_DIR, WebhookConfig
from tlgr.core.paths import write_private
from tlgr.models.event import EventEnvelope

log = logging.getLogger("tlgr.webhook")

__all__ = ["WebhookPusher", "sign_body"]

DEAD_LETTER_FILE = CONFIG_DIR / "dead_letter.jsonl"

#: 16 MB across four files is enough to diagnose an outage and small enough
#: that an endpoint that has been down for a week cannot fill a disk.
_DEAD_LETTER_MAX_BYTES = 16 * 1024 * 1024
_DEAD_LETTER_BACKUPS = 3


def sign_body(secret: str, body: bytes) -> str:
    """`sha256=<hex>` over the exact bytes that go on the wire.

    Over the *bytes*, not over a re-encoded dict: a receiver verifies what it
    received, and any re-encoding (key order, whitespace, escaping) makes an
    honest signature fail.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookPusher:
    """Bounded queue + worker pool + HMAC + dead letter."""

    def __init__(self, config: WebhookConfig, base: Path | None = None) -> None:
        self.config = config
        self.base = Path(base) if base is not None else CONFIG_DIR
        self._session: Any = None
        self._resolved_chat_ids: set[int] = set()
        self._dead_letter_path = self.base / "dead_letter.jsonl"
        self._queue: asyncio.Queue[tuple[bytes, dict[str, str]]] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._filter_node: Any = None
        self.delivered = 0
        self.failed = 0
        self.dead_letters = 0
        self.dropped = 0
        if config.filters.raw:
            from tlgr.filters.compose import parse_filter_config

            self._filter_node = parse_filter_config(config.filters.raw)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not self.config.enabled:
            return
        import aiohttp

        self._session = aiohttp.ClientSession()
        self._queue = asyncio.Queue(maxsize=max(16, self.config.queue_size))
        self._workers = [
            asyncio.create_task(self._worker(), name=f"tlgr-webhook-{index}")
            for index in range(max(1, self.config.workers))
        ]
        if self.config.url.startswith("http://") and not _is_loopback(self.config.url):
            log.warning(
                "webhook URL is plain http:// to a non-loopback host; "
                "event payloads and the signature travel in clear text",
            )
        log.info("webhook pusher started")

    async def stop(self, *, drain: float = 10.0) -> None:
        """Flush what we can, dead-letter the rest (§6.11 step 4)."""
        if self._queue is not None:
            deadline = time.monotonic() + drain
            while not self._queue.empty() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            while not self._queue.empty():
                body, headers = self._queue.get_nowait()
                self._dead_letter(body, headers, "daemon shutting down")
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        self._workers = []
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- filtering ---------------------------------------------------------

    def set_resolved_chats(self, chat_ids: set[int]) -> None:
        self._resolved_chat_ids = chat_ids

    def should_push(
        self, event_type: str, chat_id: int | None = None, tg_event: Any = None
    ) -> bool:
        if not self.config.enabled:
            return False
        if event_type not in self.config.events:
            return False
        if self.config.filters.chats and chat_id is not None:
            if chat_id not in self._resolved_chat_ids:
                return False
        if self._filter_node is not None and tg_event is not None:
            from tlgr.filters.compose import evaluate
            from tlgr.gateway.event import Event

            envelope = Event(source="telegram", raw=tg_event, event_type=event_type)
            ok, _ = evaluate(self._filter_node, envelope)
            if not ok:
                return False
        return True

    # -- the bus handler ---------------------------------------------------

    async def on_event(self, event: EventEnvelope, raw: Any = None) -> None:
        """The bus handler. Encodes, signs and enqueues; never sends inline."""
        if not self.should_push(event.type, event.chat_id, raw):
            return
        self.enqueue(event)

    def enqueue(self, event: EventEnvelope) -> None:
        delivery_id = uuid.uuid4().hex
        try:
            body = msgspec.json.encode({"event": event, "delivery_id": delivery_id})
        except (TypeError, msgspec.EncodeError) as exc:
            # A payload we cannot encode is a *bug in tlgr*, not a delivery
            # failure: retrying it three times and dead-lettering it hides the
            # defect behind an endpoint that looks flaky (COR-07).
            log.error(
                "BUG: webhook payload for %s could not be encoded (%s): %s",
                event.type,
                type(exc).__name__,
                exc,
                extra={"event_type": event.type, "account": event.account},
            )
            return

        headers = {
            "Content-Type": "application/json",
            "X-Tlgr-Delivery": delivery_id,
            "X-Tlgr-Seq": str(event.seq),
            "X-Tlgr-Event": event.type,
            "X-Tlgr-Account": event.account,
        }
        secret = self.config.signing_key
        if secret:
            headers["X-Tlgr-Signature"] = sign_body(secret, body)
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"

        if self._queue is None:
            self._dead_letter(body, headers, "webhook is not running")
            return
        try:
            self._queue.put_nowait((body, headers))
        except asyncio.QueueFull:
            self.dropped += 1
            self._dead_letter(body, headers, "webhook queue is full")

    # -- delivery ----------------------------------------------------------

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            body, headers = await self._queue.get()
            await self._deliver(body, headers)

    async def _deliver(self, body: bytes, headers: dict[str, str]) -> None:
        import aiohttp

        retry = self.config.retry
        attempts = retry.max_attempts if retry.enabled else 1
        last = ""
        for attempt in range(attempts):
            try:
                assert self._session is not None
                async with self._session.post(
                    self.config.url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:
                    if response.status < 400:
                        self.delivered += 1
                        return
                    last = f"HTTP {response.status}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            self.failed += 1
            if attempt + 1 < attempts:
                # Jittered, so a restarted endpoint is not hit by every
                # pending delivery in the same millisecond.
                delay = (retry.backoff_base**attempt) * (1.0 + random.uniform(-0.2, 0.2))
                await asyncio.sleep(delay)
        self._dead_letter(body, headers, last or "delivery failed")

    # -- dead letters ------------------------------------------------------

    def _dead_letter(self, body: bytes, headers: dict[str, str], reason: str) -> None:
        self.dead_letters += 1
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record = {
            "ts": now,
            "reason": reason,
            # `source` names the consumer that failed. One store is shared by
            # the pusher and the gateway actions, and an operator draining it
            # has to be able to re-drive one without the other.
            "source": "webhook",
            "attempts": max(1, self.config.retry.max_attempts if self.config.retry.enabled else 1),
            "first_failed_at": now,
            "delivery_id": headers.get("X-Tlgr-Delivery", ""),
            "seq": headers.get("X-Tlgr-Seq", ""),
            "event": headers.get("X-Tlgr-Event", ""),
            "account": headers.get("X-Tlgr-Account", ""),
            "body": body.decode("utf-8", errors="replace"),
        }
        try:
            self._rotate_if_needed()
            path = self._dead_letter_path
            if not path.exists():
                write_private(path, "")
            with open(os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("could not write a dead letter: %s", exc)

    def _rotate_if_needed(self) -> None:
        path = self._dead_letter_path
        try:
            if not path.exists() or path.stat().st_size < _DEAD_LETTER_MAX_BYTES:
                return
        except OSError:
            return
        for index in range(_DEAD_LETTER_BACKUPS, 0, -1):
            older = path.with_suffix(path.suffix + f".{index}")
            newer = path if index == 1 else path.with_suffix(path.suffix + f".{index - 1}")
            if newer.exists():
                with contextlib.suppress(OSError):
                    newer.replace(older)
        with contextlib.suppress(OSError):
            write_private(path, "")

    def read_dead_letters(self) -> list[dict[str, Any]]:
        if not self._dead_letter_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self._dead_letter_path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    @property
    def dead_letter_path(self) -> Path:
        return self._dead_letter_path

    def write_dead_letters(self, entries: list[dict[str, Any]]) -> None:
        """Replace the store. Private mode, one write, no partial file."""
        body = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
        write_private(self._dead_letter_path, body)
        self.dead_letters = len(entries)

    async def deliver_once(self, entry: dict[str, Any], *, url: str = "") -> tuple[bool, str]:
        """One delivery attempt for a stored entry. Returns `(ok, error)`.

        Re-delivery reuses the original `X-Tlgr-Delivery` id, so a receiver
        keyed on it sees a duplicate rather than a new event — which is the
        difference between a safe replay and a double-processed message.
        """
        import aiohttp

        target = url or self.config.url
        if not target:
            return False, "no webhook URL is configured"
        body = str(entry.get("body", "")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Tlgr-Delivery": str(entry.get("delivery_id", "")),
            "X-Tlgr-Seq": str(entry.get("seq", "")),
            "X-Tlgr-Event": str(entry.get("event", "")),
            "X-Tlgr-Account": str(entry.get("account", "")),
            "X-Tlgr-Redelivery": "1",
        }
        secret = self.config.signing_key
        if secret:
            headers["X-Tlgr-Signature"] = sign_body(secret, body)
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        session = self._session
        close_after = session is None
        if session is None:
            session = aiohttp.ClientSession()
        try:
            async with session.post(
                target,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:
                if response.status < 400:
                    self.delivered += 1
                    return True, ""
                return False, f"HTTP {response.status}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            if close_after:
                await session.close()

    def purge_dead_letters(self) -> int:
        if not self._dead_letter_path.exists():
            return 0
        count = len(self.read_dead_letters())
        self._dead_letter_path.unlink(missing_ok=True)
        self.dead_letters = 0
        return count

    # -- v1 compatibility --------------------------------------------------

    async def push(self, event_type: str, data: dict[str, Any], account: str = "") -> None:
        """v1's entry point, kept for the gateway jobs that still call it.

        It builds an envelope and hands it to the same queue, so a legacy
        caller gets the signature, the bounded queue and the dead-letter
        handling without being rewritten.
        """
        if not self.config.enabled:
            return
        self.enqueue(
            EventEnvelope(
                seq=0,
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                account=account,
                type=event_type,
                payload=data,
                chat_id=data.get("chat_id") if isinstance(data, dict) else None,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "delivered": self.delivered,
            "failed": self.failed,
            "dead_letters": self.dead_letters,
            "dropped": self.dropped,
        }


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")
