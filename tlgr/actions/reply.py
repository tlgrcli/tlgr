"""Reply action: answer the triggering message, after showing "typing...".

The typing indicator is on by default (`typing: false` turns it off) and
lasts about as long as typing the text would take at 40 characters a second,
between 2 and 15 seconds. It runs as two steps of one pending item: the
`chat.typing` operation is started, the item is re-queued for the moment
typing ends, and then `message.send` goes out as a reply. The reply queue is
therefore never blocked for the length of somebody's typing. A broadcast
channel gets no indicator: nobody can see one there.

Processors apply to the reply text. The text is sent as markdown, which is
what `event.reply()` did with the client's default parse mode.
"""

from __future__ import annotations

import random
from typing import Any

from tlgr.actions import register_action
from tlgr.actions.base import (
    HOUR,
    Action,
    ActionError,
    MessageFacts,
    Outcome,
    Runtime,
    check_keys,
    require_bool,
)
from tlgr.processors import ProcessorChain

CHARS_PER_SECOND = 40.0
TYPING_MIN_S = 2.0
TYPING_MAX_S = 15.0


def typing_seconds(text: str) -> float:
    return max(TYPING_MIN_S, min(TYPING_MAX_S, len(text) / CHARS_PER_SECOND))


@register_action("reply")
class Reply(Action):
    name = "reply"
    default_expiry = 24 * HOUR
    cancelled_by = frozenset({"cancel"})
    album_once = True
    accepts_processors = True

    def parse(self, config: Any) -> dict[str, Any]:
        typing = True
        if isinstance(config, dict):
            check_keys(self.name, config, {"text", "typing"})
            text = config.get("text")
            if "typing" in config:
                typing = require_bool(self.name, "typing", config["typing"])
        else:
            text = config
        if not isinstance(text, str) or not text.strip():
            raise ActionError(f"{self.name}: needs a non-empty `text`, got {text!r}")
        return {"text": text, "typing": typing}

    def plan(
        self,
        facts: MessageFacts,
        params: dict[str, Any],
        chain: ProcessorChain | None,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        text = chain.apply(params["text"]) if chain else params["text"]
        if not text.strip():
            return []
        return [{"text": text, "typing": params["typing"], "typing_s": typing_seconds(text)}]

    async def execute(self, items: list[Any], rt: Runtime) -> Outcome:
        item = items[0]
        payload = item.payload
        if item.phase == "" and payload.get("typing") and item.peer != "channel":
            seconds = float(payload.get("typing_s") or TYPING_MIN_S)
            request: dict[str, Any] = {"chat": str(item.chat_id), "duration": seconds}
            if item.topic_id:
                request["topic"] = item.topic_id
            rt.spawn(rt.op("chat.typing", request), what=f"typing in {item.chat_id}")
            item.phase = "send"
            return Outcome(status="later", due_at=rt.now() + seconds)
        rt.begin_send(item.chat_id)
        result = await rt.op(
            "message.send",
            {
                "chat": str(item.chat_id),
                "text": payload["text"],
                "reply_to": item.msg_id,
                "parse": "md",
            },
        )
        sent = (result or {}).get("id")
        if sent:
            rt.note_sent(item.chat_id, [int(sent)])
        return Outcome()

    def describe(self, items: list[Any]) -> str:
        item = items[0]
        return f"reply to msg {item.msg_id} in {item.chat_id}: {item.payload.get('text')!r}"
