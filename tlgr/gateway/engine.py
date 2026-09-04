"""Gateway engine — generic event-driven pipeline.

    event -> filters -> processors -> actions

Where the events come from changed in v2. A job used to register its own
Telethon handlers, so a rule whose action posted to a slow endpoint ran
*inside* the update loop and, with `sequential_updates=True`, made every
account deaf until it returned (ROB-02). A job now subscribes to the daemon's
event bus, which runs handlers on bounded worker lanes keyed by chat: per-chat
order is preserved, the update loop is never blocked, and a job that falls
behind is bounded rather than unbounded.

The pipeline itself is unchanged, and filters still read the raw Telethon
event, which is why the bus carries it beside the normalised envelope. The
full move to model-based filters belongs to the updates group (PR-4).

Without a bus — a unit test, or a daemon that has not started one — the job
falls back to registering Telethon handlers exactly as v1 did.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import events

from tlgr.actions import get_action
from tlgr.filters.compose import evaluate
from tlgr.gateway.config import ActionConfig, GatewayConfig
from tlgr.gateway.event import Event
from tlgr.jobs.base import BaseJob
from tlgr.jobs.client import JobClient

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


#: v1's job event names → the bus taxonomy, and back for the pipeline, which
#: still labels envelopes with v1's names. The expansion is the taxonomy's own
#: alias table (`core.eventtypes.ALIASES`), so a job and a `watch` accept the
#: same words; a job may also name any v2 type directly.
def _bus_types(names: list[str]) -> set[str]:
    from tlgr.core import eventtypes

    wanted: set[str] = set()
    for name in names:
        wanted.update(eventtypes.ALIASES.get(name, (name,)))
    return wanted


_V1_TYPE_MAP = {
    "message_new": "new_message",
    "message_edited": "message_edited",
    "message_deleted": "message_deleted",
    "message_service": "chat_action",
    "user_status": "user_joined",
    "read_inbox": "message_read",
    "read_outbox": "message_read",
}

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
        client: JobClient,
        webhook=None,
        bus=None,
    ) -> None:
        self._gw = config
        shim = _GatewayJobConfig(config)
        super().__init__(shim, client, webhook)  # type: ignore[arg-type]
        self._handlers: list = []
        self._bus = bus
        self._bus_handler = None
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
        if self._bus is not None:
            await self._run_on_bus()
            return
        await self._run_on_client()

    async def _run_on_bus(self) -> None:
        """Subscribe to the daemon's bus instead of the update loop (ROB-02)."""
        wanted = _bus_types(list(self._gw.events))
        account = self._gw.account

        async def on_event(envelope, raw) -> None:
            if envelope.type not in wanted:
                return
            if account and envelope.account != account:
                return
            if raw is None:
                # A self-origin echo or a synthesised event: the filters read
                # the raw Telethon object, so there is nothing to evaluate.
                return
            await self._handle(raw, _V1_TYPE_MAP.get(envelope.type, envelope.type))

        self._bus_handler = on_event
        self._bus.add_handler(on_event)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    async def _run_on_client(self) -> None:
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
        if self._bus is not None and self._bus_handler is not None:
            self._bus.remove_handler(self._bus_handler)
            self._bus_handler = None
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
