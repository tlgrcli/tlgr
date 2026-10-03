"""React action: put a reaction on the triggering message.

`react: "👍"`, `react: ["👍", "❤", "🔥"]` (one picked uniformly per message),
`react: {"👍": 3, "🔥": 1}` (weighted), or the long form
`react: {emoji: ..., big: false, percent: 60, delay: 30-300s}`. A custom
(Premium) emoji is written `custom:<document id>`, the spelling `reaction
add` uses.

The job's choice replaces whatever reaction the account already had
(`reaction.add --replace`). A chat that does not allow the emoji is not
checked first: the reaction is sent and REACTION_INVALID lands in the
action's error counter, which is where a misconfigured job should show up.

Reacting implies reading. Before the reaction goes out the chat is read up
to that message (coalesced with any read already pending for the chat), so
the other side never sees a reaction on a message their client still shows
as unread. That happens whether or not the job has a `read` action.

An album (one `grouped_id`, delivered as several messages) gets one reaction,
on the message carrying the caption, else the first one. That is where
Telegram Desktop attaches an album's reactions (`GroupedMedia::itemForText`)
and where Telegram for Android looks (`findPrimaryMessageObject`).
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

_CUSTOM = "custom:"


def _emoji(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionError(f"react: {value!r} is not an emoji")
    value = value.strip()
    if value.startswith(_CUSTOM) and not value[len(_CUSTOM) :].isdigit():
        raise ActionError(f"react: {value!r} is not custom:<document id>")
    return value


def _choices(value: Any) -> list[list[Any]]:
    """`[[emoji, weight], ...]` from a string, a list or a weighted mapping."""
    if isinstance(value, str):
        return [[_emoji(value), 1.0]]
    if isinstance(value, list):
        if not value:
            raise ActionError("react: the emoji list is empty")
        return [[_emoji(item), 1.0] for item in value]
    if isinstance(value, dict):
        if not value:
            raise ActionError("react: the emoji mapping is empty")
        out: list[list[Any]] = []
        for emoji, weight in value.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                raise ActionError(
                    f"react: the weight of {emoji!r} must be a positive number, got {weight!r}. "
                    "Knobs such as delay or percent go in the long form: "
                    "react: {emoji: {...}, delay: ...}"
                )
            out.append([_emoji(emoji), float(weight)])
        return out
    raise ActionError(f"react: {value!r} is not an emoji, a list or a weighted mapping")


@register_action("react")
class React(Action):
    name = "react"
    default_expiry = 24 * HOUR
    cancelled_by = frozenset({"cancel"})
    album_once = True

    def parse(self, config: Any) -> dict[str, Any]:
        big = False
        if isinstance(config, dict) and "emoji" in config:
            check_keys(self.name, config, {"emoji", "big"})
            if "big" in config:
                big = require_bool(self.name, "big", config["big"])
            choices = _choices(config["emoji"])
        else:
            choices = _choices(config)
        return {"choices": choices, "big": big}

    def plan(
        self,
        facts: MessageFacts,
        params: dict[str, Any],
        chain: ProcessorChain | None,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        if facts.service:
            return []
        choices = params["choices"]
        if len(choices) == 1:
            emoji = choices[0][0]
        else:
            emoji = rng.choices([c[0] for c in choices], weights=[c[1] for c in choices])[0]
        return [{"emoji": emoji, "big": params["big"]}]

    async def execute(self, items: list[Any], rt: Runtime) -> Outcome:
        item = items[0]
        await rt.ensure_read(item)
        await rt.op(
            "reaction.add",
            {
                "chat": str(item.chat_id),
                "msg_id": item.msg_id,
                "emoji": [item.payload["emoji"]],
                "replace": True,
                "big": bool(item.payload.get("big")),
            },
        )
        return Outcome()

    def describe(self, items: list[Any]) -> str:
        item = items[0]
        return (
            f"read {item.chat_id} up to {item.msg_id}, "
            f"then react {item.payload.get('emoji')} to msg {item.msg_id}"
        )
