"""Reactions: what is on a message, who put it there, and what a chat allows.

One spelling for a reaction across the whole group: a unicode emoji is itself
(`"👍"`) and a custom (Premium) emoji is `"custom:<document_id>"`. The pair
`(emoji, custom)` is accepted on input and the same string comes back on
output, so a reaction read from `reaction list` can be handed straight back to
`reaction remove` without the caller knowing which kind it was.
"""

from __future__ import annotations

from typing import Literal

from tlgr.models.base import Model
from tlgr.models.message import ReactionSummary

__all__ = [
    "AvailableReaction",
    "ChatReactions",
    "MessageReactionState",
    "PaidReactionResult",
    "ReactionPrivacy",
    "ReactionPurge",
    "ReactionReport",
    "ReactionResult",
    "ReactionTag",
    "ReactionUser",
    "TopReactor",
]


class ReactionResult(Model):
    """What `reaction add` / `reaction remove` did.

    `emoji` and `reacted` are v1's `message react` keys and stay spelled that
    way; `mine` is the full set my account now holds, because `sendReaction`
    takes the whole desired state and a caller that wants to add a second
    reaction has to know what the first one was.
    """

    chat_id: int
    msg_id: int
    emoji: str = ""
    reacted: bool = False
    already: bool = False
    mine: list[str] = []
    reactions: ReactionSummary | None = None


class TopReactor(Model):
    """A row of the Star-reaction leaderboard on a channel post."""

    user_id: int | None = None
    stars: int = 0
    anonymous: bool = False
    mine: bool = False


class MessageReactionState(Model):
    """The reaction state of one message, refreshed from the server."""

    chat_id: int = 0
    msg_id: int = 0
    reactions: ReactionSummary | None = None
    can_see_list: bool = False
    as_tags: bool = False
    top_reactors: list[TopReactor] = []


class ReactionUser(Model):
    """Who reacted, with what, and when."""

    user_id: int
    reaction: str = ""
    date: str | None = None
    date_unix: int | None = None
    big: bool = False
    unread: bool = False
    mine: bool = False


class AvailableReaction(Model):
    """One entry of the reaction catalogue.

    `source` says which list it came from, because the standard catalogue,
    the featured list and my own recent reactions are three different
    questions with three different endpoints and the same row shape.
    """

    emoticon: str
    title: str = ""
    premium: bool = False
    inactive: bool = False
    source: Literal["available", "top", "recent"] = "available"
    static_icon_id: int | None = None
    select_animation_id: int | None = None


class ChatReactions(Model):
    """What a chat (or one message in it) allows.

    `mode` is the `chatReactions*` variant flattened to a word, because
    `setChatAvailableReactions` demands the whole value on every write: a
    caller changing only `reactions_limit` still has to resend the mode.
    """

    chat_id: int = 0
    msg_id: int | None = None
    mode: Literal["all", "some", "none"] = "none"
    reactions: list[str] = []
    allow_custom: bool = False
    reactions_limit: int | None = None
    paid_enabled: bool | None = None


class ReactionTag(Model):
    """A Saved Messages reaction tag, and the name I gave it."""

    reaction: str
    title: str | None = None
    count: int = 0
    suggested: bool = False


class ReactionPrivacy(Model):
    """How my paid reactions are attributed."""

    privacy: Literal["default", "anonymous", "peer"] = "default"
    peer_id: int | None = None
    msg_id: int | None = None


class PaidReactionResult(Model):
    """A Star reaction that was actually paid for."""

    chat_id: int
    msg_id: int
    stars_sent: int = 0
    privacy: Literal["default", "anonymous", "peer"] = "default"
    top_reactors: list[TopReactor] = []
    #: `reaction pay --senders`: the peers I may pay as, listed without paying.
    senders: list[int] = []


class ReactionPurge(Model):
    """A moderator's removal of somebody else's reactions."""

    chat_id: int
    user_id: int
    msg_id: int | None = None
    deleted: bool = False
    scope: Literal["message", "chat"] = "message"


class ReactionReport(Model):
    ok: bool = False
    banned: bool = False
    chat_id: int = 0
    msg_id: int = 0
    user_id: int = 0
