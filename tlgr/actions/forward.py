"""Forward action: relay messages to one or more destinations.

Without processors it is a native forward (`message.forward`, with
`--no-author` when `drop_author` is set). With processors the text is
rewritten, so a forward would carry the original; instead the processed text
is re-sent (`message.send`), or, for a photo or document, the same media is
re-sent with the processed caption (`media.upload --from-message`). Text is
taken and sent as markdown, the client's parse mode, so formatting survives
the rewrite as it always has.

Each destination is its own pending item, so one destination refusing the
message (CHAT_WRITE_FORBIDDEN, a private channel) does not stop the others,
and each is retried or counted on its own.

Forwards never expire, are never held by quiet hours and are never cancelled
by a manual takeover: a relay that silently drops posts is broken.
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


@register_action("forward")
class Forward(Action):
    name = "forward"
    quiet_hold = False
    accepts_processors = True

    def parse(self, config: Any) -> dict[str, Any]:
        drop_author = False
        if isinstance(config, dict):
            check_keys(self.name, config, {"to", "drop_author"})
            to = config.get("to")
            if "drop_author" in config:
                drop_author = require_bool(self.name, "drop_author", config["drop_author"])
        else:
            to = config
        destinations = to if isinstance(to, list) else [to] if to not in (None, "") else []
        if not destinations or any(
            isinstance(d, bool) or not isinstance(d, (str, int)) or d == "" for d in destinations
        ):
            raise ActionError(f"{self.name}: `to` must name one or more chats, got {to!r}")
        return {"to": [str(d) for d in destinations], "drop_author": drop_author}

    def plan(
        self,
        facts: MessageFacts,
        params: dict[str, Any],
        chain: ProcessorChain | None,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        # What `is_forwardable` refused before: service messages, empty ones,
        # and self-destructing media.
        if facts.service or facts.view_once or (not facts.text and facts.media is None):
            return []
        resend: dict[str, Any] | None = None
        if chain:
            text = chain.apply(facts.text) if facts.text else ""
            if facts.media in ("photo", "document"):
                resend = {"text": text, "media": True}
            elif text:
                # A link preview is regenerated from the text; it is not media
                # that can be re-sent.
                resend = {"text": text, "media": False}
            else:
                return []
        return [
            {"to": destination, "drop_author": params["drop_author"], "resend": resend}
            for destination in params["to"]
        ]

    def key_extra(self, payload: dict[str, Any]) -> str:
        return str(payload.get("to", ""))

    async def execute(self, items: list[Any], rt: Runtime) -> Outcome:
        item = items[0]
        payload = item.payload
        resend = payload.get("resend")
        if not resend:
            result = await rt.op(
                "message.forward",
                {
                    "chat": str(item.chat_id),
                    "msg_id": [str(item.msg_id)],
                    "to": [payload["to"]],
                    "no_author": bool(payload.get("drop_author")),
                },
            )
            for row in (result or {}).get("items") or []:
                rt.note_sent(int(row.get("chat_id") or 0), [int(row.get("id") or 0)])
        elif resend.get("media"):
            request: dict[str, Any] = {
                "chat": payload["to"],
                "from_message": f"{item.chat_id}:{item.msg_id}",
                "parse": "md",
            }
            if resend.get("text"):
                request["caption"] = [resend["text"]]
            await rt.op("media.upload", request)
        else:
            await rt.op(
                "message.send", {"chat": payload["to"], "text": resend["text"], "parse": "md"}
            )
        return Outcome()

    def describe(self, items: list[Any]) -> str:
        item = items[0]
        how = "re-send" if item.payload.get("resend") else "forward"
        return f"{how} msg {item.msg_id} from {item.chat_id} to {item.payload.get('to')}"
