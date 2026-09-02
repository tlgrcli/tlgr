"""Gateway engine — generic event-driven pipeline.

Replaces AutoforwardJob and AutoreplyJob with a single class that runs:
    event -> filters -> processors -> actions
"""

from __future__ import annotations

import asyncio
import logging

from telethon import events

from tlgr.actions import get_action
from tlgr.core.client import ClientWrapper
from tlgr.filters.compose import evaluate
from tlgr.gateway.config import ActionConfig, GatewayConfig
from tlgr.gateway.event import Event
from tlgr.jobs.base import BaseJob

log = logging.getLogger("tlgr.gateway")


class _GatewayJobConfig:
    """Minimal shim so Gateway can sit on top of BaseJob.

    BaseJob expects a config object with ``.name``, ``.type``, and
    ``.enabled`` attributes.
    """

    def __init__(self, gw: GatewayConfig) -> None:
        self.name = gw.name
        self.type = "gateway"
        self.enabled = gw.enabled
        self.account = gw.account


_EVENT_TYPE_MAP = {
    "new_message": (events.NewMessage, {}),
    "message_edited": (events.MessageEdited, {}),
    "message_deleted": (events.MessageDeleted, {}),
    "chat_action": (events.ChatAction, {}),
    "user_joined": (events.UserUpdate, {}),
    "message_read": (events.MessageRead, {}),
}


class Gateway(BaseJob):
    """Generic pipeline job: filters -> processors -> actions."""

    def __init__(
        self,
        config: GatewayConfig,
        client: ClientWrapper,
        webhook=None,
    ) -> None:
        self._gw = config
        shim = _GatewayJobConfig(config)
        super().__init__(shim, client, webhook)  # type: ignore[arg-type]
        self._handlers: list = []
        self._stats: dict[str, int] = {"matched": 0, "skipped": 0, "errors": 0}

    async def setup(self) -> None:
        log.info(
            "[%s] events=%s filters=%s actions=%s",
            self.name,
            self._gw.events,
            "yes" if self._gw.filters else "none",
            [a.name for a in self._gw.actions],
        )

    async def run(self) -> None:
        for event_type_name in self._gw.events:
            mapping = _EVENT_TYPE_MAP.get(event_type_name)
            if not mapping:
                log.warning("[%s] unknown event type: %s", self.name, event_type_name)
                continue
            event_cls, kwargs = mapping
            if event_type_name == "new_message":
                kwargs = {"incoming": True}

            et = event_type_name

            @self.client.client.on(event_cls(**kwargs))
            async def handler(tg_event, _et=et):
                await self._handle(tg_event, _et)

            self._handlers.append(handler)

        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    async def teardown(self) -> None:
        for h in self._handlers:
            self.client.client.remove_event_handler(h)
        self._handlers.clear()
        log.info(
            "[%s] stopped — matched=%d skipped=%d errors=%d",
            self.name,
            self._stats["matched"],
            self._stats["skipped"],
            self._stats["errors"],
        )

    async def _handle(self, tg_event, event_type: str = "new_message") -> None:
        envelope = Event(
            source="telegram",
            raw=tg_event,
            account=self._gw.account,
            event_type=event_type,
        )

        ok, reason = evaluate(self._gw.filters, envelope)
        if not ok:
            self._stats["skipped"] += 1
            return

        self._stats["matched"] += 1

        for action_cfg in self._gw.actions:
            await self._run_action(action_cfg, envelope)

    async def _run_action(self, ac: ActionConfig, envelope: Event) -> None:
        if ac.filters:
            ok, reason = evaluate(ac.filters, envelope)
            if not ok:
                return

        func = get_action(ac.name)
        if func is None:
            log.warning("[%s] unknown action: %s", self.name, ac.name)
            self._stats["errors"] += 1
            return

        chain = ac.processors or self._gw.processors

        try:
            await func(envelope, ac.config, self.client, chain)
        except Exception as e:
            log.warning("[%s] action '%s' failed: %s", self.name, ac.name, e)
            self._stats["errors"] += 1
