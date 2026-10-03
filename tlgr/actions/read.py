"""Read action: mark the chat read up to the triggering message.

Never `max_id=0` ("everything"), and never past the triggering message: a
message that arrived after it may not have been "seen" yet. `message.read`
picks the RPC for the peer (readHistory for a private chat or basic group,
channels.readHistory for a channel or supergroup, readDiscussion for a forum
topic), so a channel read is never the silent no-op `messages.readHistory`
is there.

Reads coalesce per chat: every read that has come due for a chat goes out
as one call with the highest id, each item still keeping its own delay. A
read never expires; it is a watermark, so doing it late is harmless.

On top of the `delay` range the item waits as long as reading the message
would take, about 250 words a minute plus a few seconds for media, capped
at a minute. `mentions: true` and `reactions: true` also clear those badges
for the chat.
"""

from __future__ import annotations

import random
from typing import Any

from tlgr.actions import register_action
from tlgr.actions.base import (
    Action,
    ActionError,
    MessageFacts,
    Outcome,
    Runtime,
    check_keys,
    require_bool,
)
from tlgr.processors import ProcessorChain

WORDS_PER_MINUTE = 250.0
MEDIA_READ_S = 3.0
READING_CAP_S = 60.0


def reading_seconds(facts: MessageFacts) -> float:
    seconds = len(facts.text.split()) / WORDS_PER_MINUTE * 60.0
    if facts.media is not None:
        seconds += MEDIA_READ_S
    return min(seconds, READING_CAP_S)


@register_action("read")
class Read(Action):
    name = "read"
    cancelled_by = frozenset({"cancel", "cancel_read"})

    def parse(self, config: Any) -> dict[str, Any]:
        if config is None or config is True:
            return {"mentions": False, "reactions": False}
        if not isinstance(config, dict):
            raise ActionError(f"{self.name}: expected {{}} or a mapping, got {config!r}")
        check_keys(self.name, config, {"mentions", "reactions"})
        return {
            "mentions": require_bool(self.name, "mentions", config.get("mentions", False)),
            "reactions": require_bool(self.name, "reactions", config.get("reactions", False)),
        }

    def plan(
        self,
        facts: MessageFacts,
        params: dict[str, Any],
        chain: ProcessorChain | None,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        return [{"mentions": params["mentions"], "reactions": params["reactions"]}]

    def extra_delay(self, facts: MessageFacts, params: dict[str, Any]) -> float:
        return reading_seconds(facts)

    def batch_key(self, item: Any) -> tuple[Any, ...] | None:
        return ("read", item.chat_id, item.topic_id, item.dry_run)

    async def execute(self, items: list[Any], rt: Runtime) -> Outcome:
        first = items[0]
        target = max(item.msg_id for item in items)
        mentions = any(item.payload.get("mentions") for item in items)
        reactions = any(item.payload.get("reactions") for item in items)
        if rt.read_mark(first.chat_id, first.topic_id) >= target and not (mentions or reactions):
            return Outcome()
        request: dict[str, Any] = {"chat": str(first.chat_id), "up_to": target}
        if first.topic_id:
            request["topic"] = first.topic_id
        if mentions:
            request["mentions"] = True
        if reactions:
            request["reactions"] = True
        rt.begin_read(first.chat_id, first.topic_id, target)
        await rt.op("message.read", request)
        rt.note_read(first.chat_id, first.topic_id, target)
        return Outcome()

    def describe(self, items: list[Any]) -> str:
        first = items[0]
        target = max(item.msg_id for item in items)
        return f"read {first.chat_id} up to {target} ({len(items)} message(s))"
