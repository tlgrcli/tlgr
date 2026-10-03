"""View action: count a channel view, or listen to a voice or round note.

In a broadcast channel a view is `messages.getMessagesViews(increment=true)`
for the post, which is what the official apps send as a post scrolls into
view (`message.view.get --increment`). Views coalesce per chat into one call
with every id that has come due.

In a private chat or a group the only thing to "view" is media with an
unread mark: a voice note or a round video note is marked listened with
`readMessageContents` (the channels variant in a supergroup;
`message.read --contents` picks). Self-destructing and view-once media is
never consumed unless `include_view_once: true`, because listening to it
destroys it. A message with nothing to view counts as skipped.

`view` and `read` are independent; neither implies the other.
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

#: `getMessagesViews` and `readMessageContents` take at most this many ids.
MAX_IDS = 100


@register_action("view")
class View(Action):
    name = "view"
    default_expiry = 24 * HOUR
    cancelled_by = frozenset({"cancel", "cancel_read"})

    def parse(self, config: Any) -> dict[str, Any]:
        if config is None or config is True:
            return {"include_view_once": False}
        if not isinstance(config, dict):
            raise ActionError(f"{self.name}: expected {{}} or a mapping, got {config!r}")
        check_keys(self.name, config, {"include_view_once"})
        return {
            "include_view_once": require_bool(
                self.name, "include_view_once", config.get("include_view_once", False)
            )
        }

    def plan(
        self,
        facts: MessageFacts,
        params: dict[str, Any],
        chain: ProcessorChain | None,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        if facts.service:
            return []
        if facts.peer == "channel":
            return [{"mode": "views"}]
        if facts.voice_or_round and (not facts.view_once or params["include_view_once"]):
            return [{"mode": "contents"}]
        return []

    def batch_key(self, item: Any) -> tuple[Any, ...] | None:
        return ("view", item.chat_id, item.payload.get("mode"), item.dry_run)

    async def execute(self, items: list[Any], rt: Runtime) -> Outcome:
        first = items[0]
        ids = sorted({item.msg_id for item in items})[:MAX_IDS]
        if first.payload.get("mode") == "views":
            await rt.op(
                "message.view.get",
                {"chat": str(first.chat_id), "msg_id": [str(i) for i in ids], "increment": True},
            )
        else:
            await rt.op(
                "message.read", {"chat": str(first.chat_id), "contents": [str(i) for i in ids]}
            )
        return Outcome()

    def describe(self, items: list[Any]) -> str:
        first = items[0]
        ids = sorted({item.msg_id for item in items})
        what = "count views of" if first.payload.get("mode") == "views" else "listen to"
        return f"{what} {ids} in {first.chat_id}"
