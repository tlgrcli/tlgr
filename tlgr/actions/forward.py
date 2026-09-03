"""Forward action — relay messages to one or more destinations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import errors

from tlgr.actions import register_action
from tlgr.core.client import ClientWrapper
from tlgr.filters.message import is_forwardable
from tlgr.gateway.event import Event
from tlgr.processors import ProcessorChain

log = logging.getLogger("tlgr.actions.forward")


@register_action("forward")
async def action_forward(
    event: Event,
    config: Any,
    client: ClientWrapper,
    chain: ProcessorChain | None = None,
) -> None:
    if event.source != "telegram":
        log.warning("forward action only supports telegram events")
        return

    message = event.raw.message

    ok, reason = is_forwardable(message)
    if not ok:
        log.debug("message not forwardable: %s", reason)
        return

    if isinstance(config, str):
        destinations = [config]
        drop_author = False
    elif isinstance(config, dict):
        to = config.get("to", [])
        destinations = to if isinstance(to, list) else [to]
        drop_author = config.get("drop_author", False)
    else:
        log.warning("invalid forward config: %r", config)
        return

    for i, dest_ref in enumerate(destinations):
        try:
            dest_id = await client.resolve_chat(dest_ref)

            if chain:
                original = message.text or getattr(message, "message", "") or ""
                transformed = chain.apply(original) if original else ""
                if message.media:
                    await client.client.send_file(dest_id, message.media, caption=transformed)
                else:
                    await client.client.send_message(dest_id, transformed)
            else:
                await client.client.forward_messages(
                    dest_id,
                    message,
                    drop_author=drop_author,
                )
        except errors.ChatWriteForbiddenError:
            log.warning("cannot write to %s", dest_ref)
        except errors.ChannelPrivateError:
            log.warning("channel %s is private", dest_ref)
        except Exception as e:
            log.error("forward to %s failed: %s", dest_ref, e)

        if i < len(destinations) - 1:
            await asyncio.sleep(0.3)
