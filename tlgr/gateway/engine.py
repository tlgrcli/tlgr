"""Gateway engine — generic event-driven pipeline.

    event -> filters -> processors -> actions

Where the events come from changed in v2. A job used to register its own
Telethon handlers, so a rule whose action posted to a slow endpoint ran
*inside* the update loop and, with `sequential_updates=True`, made every
account deaf until it returned (ROB-02). A job now subscribes to the daemon's
event bus, which runs handlers on bounded worker lanes keyed by chat: per-chat
order is preserved, the update loop is never blocked, and a job that falls
behind is bounded rather than unbounded.

The pipeline itself is unchanged, and filters still read a high-level
Telethon event (`NewMessage.Event` and friends). The bus carries the raw TL
update beside the normalised envelope, so the job builds that event itself
with the same builder a Telethon handler would have used, including
`NewMessage(incoming=True)`, so a job never answers the account's own
messages. The full move to model-based filters belongs to the updates group
(PR-4).

Without a bus — a unit test, or a daemon that has not started one — the job
falls back to registering Telethon handlers exactly as v1 did.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from telethon import events

from tlgr.actions import get_action
from tlgr.filters.compose import FilterNode, Op, evaluate
from tlgr.gateway.config import ActionConfig, GatewayConfig
from tlgr.gateway.event import Event
from tlgr.jobs.base import BaseJob
from tlgr.jobs.client import JobClient

log = logging.getLogger("tlgr.gateway")

#: How long a job waits before retrying a `@chat` ref it could not resolve,
#: typically because its account was still offline when the job started.
_RESOLVE_RETRY_SECONDS = 60.0


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


#: v1's job event names → the bus taxonomy. The pipeline still labels
#: envelopes with v1's names, taken from the builder that matched. The expansion is the taxonomy's own
#: alias table (`core.eventtypes.ALIASES`), so a job and a `watch` accept the
#: same words; a job may also name any v2 type directly.
def _bus_types(names: list[str]) -> set[str]:
    from tlgr.core import eventtypes

    wanted: set[str] = set()
    for name in names:
        wanted.update(eventtypes.ALIASES.get(name, (name,)))
    return wanted


_EVENT_TYPE_MAP = {
    "new_message": (events.NewMessage, {}),
    "message_edited": (events.MessageEdited, {}),
    "message_deleted": (events.MessageDeleted, {}),
    "chat_action": (events.ChatAction, {}),
    "user_joined": (events.UserUpdate, {}),
    "message_read": (events.MessageRead, {}),
}


def _builder_for(event_type_name: str) -> Any | None:
    mapping = _EVENT_TYPE_MAP.get(event_type_name)
    if not mapping:
        return None
    event_cls, kwargs = mapping
    if event_type_name == "new_message":
        kwargs = {"incoming": True}
    return event_cls(**kwargs)


def _builders(names: list[str]) -> list[tuple[str, Any]]:
    """One Telethon event builder per v1 event name, in the job's order."""
    out: list[tuple[str, Any]] = []
    for name in names:
        builder = _builder_for(name)
        if builder is not None:
            out.append((name, builder))
    return out


def _needs_resolving(ref: Any) -> bool:
    if not isinstance(ref, str):
        return False
    try:
        int(ref)
    except ValueError:
        return True
    return False


def _chat_id_leaves(nodes: list[FilterNode | None]) -> list[FilterNode]:
    leaves: list[FilterNode] = []
    stack = [node for node in nodes if node is not None]
    while stack:
        node = stack.pop()
        if node.op is Op.LEAF:
            if node.filter_name == "chat_id":
                leaves.append(node)
        else:
            stack.extend(node.children)
    return leaves


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
        self._refs_pending = False
        self._next_resolve = 0.0
        self._unresolved_logged: set[str] = set()

    async def setup(self) -> None:
        await self._resolve_chat_refs()
        log.info(
            "[%s] events=%s filters=%s actions=%s",
            self.name,
            self._gw.events,
            "yes" if self._gw.filters else "none",
            [a.name for a in self._gw.actions],
        )

    async def _resolve_chat_refs(self) -> None:
        """Turn every `chat_id: "@name"` into the marked id the filter compares.

        `filter_chat_id` compares `event.chat_id`, an int, and skips string
        refs on the promise that the gateway resolves them first. Nothing
        did, so a job filtered on `@channel` could never match. A ref that
        fails (the account is offline at boot, say) stays a string and is
        retried from `_handle`, at most once a minute.
        """
        nodes = [self._gw.filters] + [action.filters for action in self._gw.actions]
        self._refs_pending = False
        self._next_resolve = time.monotonic() + _RESOLVE_RETRY_SECONDS
        for leaf in _chat_id_leaves(nodes):
            refs = leaf.filter_value if isinstance(leaf.filter_value, list) else [leaf.filter_value]
            resolved: list = []
            for ref in refs:
                if not _needs_resolving(ref):
                    resolved.append(ref)
                    continue
                try:
                    resolved.append(await self.client.resolve_chat(str(ref)))
                except Exception as exc:
                    level = logging.DEBUG if ref in self._unresolved_logged else logging.WARNING
                    self._unresolved_logged.add(ref)
                    log.log(level, "[%s] cannot resolve chat %s: %s", self.name, ref, exc)
                    self._refs_pending = True
                    resolved.append(ref)
            leaf.filter_value = resolved if isinstance(leaf.filter_value, list) else resolved[0]

    async def run(self) -> None:
        if self._bus is not None:
            await self._run_on_bus()
            return
        await self._run_on_client()

    async def _run_on_bus(self) -> None:
        """Subscribe to the daemon's bus instead of the update loop (ROB-02)."""
        wanted = _bus_types(list(self._gw.events))
        account = self._gw.account
        builders = _builders(list(self._gw.events))

        async def on_event(envelope, raw) -> None:
            if envelope.type not in wanted:
                return
            if account and envelope.account != account:
                return
            if raw is None:
                # A self-origin echo or a synthesised event: the filters read
                # the raw Telethon object, so there is nothing to evaluate.
                return
            built = await self._build(builders, raw)
            if built is None:
                return
            tg_event, event_type = built
            await self._handle(tg_event, event_type)

        self._bus_handler = on_event
        self._bus.add_handler(on_event)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    async def _build(self, builders: list[tuple[str, Any]], update: Any) -> tuple[Any, str] | None:
        """The high-level event Telethon would have handed a handler, or None.

        This mirrors `EventBuilderDict` in Telethon's `_dispatch_update`: build,
        attach the update's entities and the client, then apply the builder's
        own filter, which is where `incoming=True` drops outgoing messages.
        """
        client = self.client.client
        self_id = getattr(client, "_self_id", None)
        for event_type, builder in builders:
            event = builder.build(update, None, self_id)
            if not event:
                continue
            if isinstance(event, events.common.EventCommon):
                event.original_update = update
                event._entities = getattr(update, "_entities", None) or {}
                event._set_client(client)
            if not builder.resolved:
                await builder.resolve(client)
            passed = builder.filter(event)
            if inspect.isawaitable(passed):
                passed = await passed
            if passed:
                return event, event_type
        return None

    async def _run_on_client(self) -> None:
        for event_type_name in self._gw.events:
            builder = _builder_for(event_type_name)
            if builder is None:
                log.warning("[%s] unknown event type: %s", self.name, event_type_name)
                continue

            et = event_type_name

            @self.client.client.on(builder)
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
        if self._refs_pending and time.monotonic() >= self._next_resolve:
            await self._resolve_chat_refs()

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
