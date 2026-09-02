"""Outbound webhook pusher — POSTs Telegram events to an external URL."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from tlgr.core.config import CONFIG_DIR, WebhookConfig
from tlgr.filters.compose import FilterNode, evaluate, parse_filter_config
from tlgr.gateway.event import Event

log = logging.getLogger("tlgr.webhook")

DEAD_LETTER_FILE = CONFIG_DIR / "dead_letter.jsonl"


class WebhookPusher:
    def __init__(self, config: WebhookConfig, base: Path | None = None):
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._resolved_chat_ids: set[int] = set()
        self._dead_letter_path = (base or CONFIG_DIR) / "dead_letter.jsonl"
        self._filter_node: FilterNode | None = None
        if config.filters.raw:
            self._filter_node = parse_filter_config(config.filters.raw)

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self._session = aiohttp.ClientSession()
        log.info("Webhook pusher started → %s", self.config.url)

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def set_resolved_chats(self, chat_ids: set[int]) -> None:
        """Set resolved numeric chat IDs for filtering."""
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
        if self._filter_node and tg_event is not None:
            envelope = Event(source="telegram", raw=tg_event, event_type=event_type)
            ok, _ = evaluate(self._filter_node, envelope)
            if not ok:
                return False
        return True

    async def push(
        self,
        event_type: str,
        data: dict[str, Any],
        account: str = "",
    ) -> None:
        if not self._session:
            return

        payload = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "data": data,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.token}",
        }

        retry = self.config.retry
        max_attempts = retry.max_attempts if retry.enabled else 1

        for attempt in range(max_attempts):
            try:
                async with self._session.post(
                    self.config.url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        log.debug("Webhook push OK (%s): %s", resp.status, event_type)
                        return
                    body = await resp.text()
                    log.warning(
                        "Webhook push failed (%s): %s",
                        resp.status,
                        body[:200],
                    )
            except Exception as e:
                log.warning("Webhook push error (attempt %d): %s", attempt + 1, e)

            if attempt < max_attempts - 1:
                wait = retry.backoff_base**attempt
                log.debug("Retrying webhook in %ds", wait)
                await asyncio.sleep(wait)

        log.error(
            "Webhook push exhausted retries for event %s — writing to dead letter", event_type
        )
        self._write_dead_letter(payload)

    def _write_dead_letter(self, payload: dict[str, Any]) -> None:
        """Append a failed event to the dead-letter file."""
        try:
            self._dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._dead_letter_path, "a") as f:
                f.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error("Failed to write dead letter: %s", e)

    def read_dead_letters(self) -> list[dict[str, Any]]:
        """Read all events from the dead-letter file."""
        if not self._dead_letter_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self._dead_letter_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries

    def purge_dead_letters(self) -> int:
        """Delete all dead-letter entries. Returns count removed."""
        if not self._dead_letter_path.exists():
            return 0
        count = len(self.read_dead_letters())
        self._dead_letter_path.unlink(missing_ok=True)
        return count
