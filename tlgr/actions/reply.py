"""Reply action — send a static text reply to the triggering message."""

from __future__ import annotations

import logging
from typing import Any

from tlgr.actions import register_action
from tlgr.gateway.event import Event
from tlgr.jobs.client import JobClient
from tlgr.processors import ProcessorChain

log = logging.getLogger("tlgr.actions.reply")


@register_action("reply")
async def action_reply(
    event: Event,
    config: Any,
    client: JobClient,
    chain: ProcessorChain | None = None,
) -> None:
    if event.source != "telegram":
        log.warning("reply action only supports telegram events")
        return

    reply_text = str(config) if isinstance(config, str) else config.get("text", str(config))

    if chain:
        reply_text = chain.apply(reply_text)

    await event.raw.reply(reply_text)
