"""Resolution results: what a reference, a link or a cache entry turns into.

`resolve` is the one group whose whole job is to be honest about *how* an
answer was reached, so every shape here carries provenance:

* `ResolvedRef.source` says which strategy answered (cache, username, phone,
  dialog scan, arithmetic on a `t.me/c/` link).
* `ResolvedPhone.reason` exists because `PHONE_NOT_OCCUPIED` is genuinely
  ambiguous — no account, or an owner who refuses phone lookups — and the op
  exits 13 rather than claiming "not found".
* `ResolvedLink.delegated_to` names the command that would *act* on a link.
  Resolution never joins, starts, installs, boosts or redeems anything; it
  says what the link is and which verb would.

No access hash is ever emitted, only `access_hash_cached`.
"""

from __future__ import annotations

from typing import Any, Literal

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "CachedPeerRow",
    "LinkKind",
    "ResolvedLink",
    "ResolvedPhone",
    "ResolvedRef",
    "ResolvedUsername",
]

#: Every shape a t.me / tg:// reference can take. `unknown` is a real answer:
#: Telegram adds deep links faster than any client learns them, and reporting
#: `unknown` with the raw path beats guessing wrong.
LinkKind = Literal[
    "public-username",
    "phone",
    "invite",
    "chatlist-invite",
    "message",
    "private-post",
    "story",
    "bot-start",
    "bot-startgroup",
    "bot-startchannel",
    "webapp",
    "business-chat-link",
    "contact-token",
    "proxy",
    "boost",
    "giftcode",
    "unique-gift",
    "stars-topup",
    "wallpaper",
    "theme",
    "stickerset",
    "emojiset",
    "share-url",
    "settings-section",
    "contacts-section",
    "login-code",
    "confirm-phone",
    "invoice",
    "premium-offer",
    "folder",
    "unknown",
]


class ResolvedRef(Model, omit_defaults=False):
    """One `resolve peer` answer, with the strategy that produced it."""

    ref: str = ""
    kind: str = ""
    id: int | None = None
    marked_id: int | None = None
    botapi_id: int | None = None
    type: str = ""
    title: str = ""
    username: str | None = None
    access_hash_cached: bool = False
    min: bool = False
    source: str = ""
    resolved: bool = False
    reason: str | None = None


class ResolvedUsername(Model, omit_defaults=False):
    kind: str = ""
    peer: Peer | None = None
    username: str = ""
    access_hash_cached: bool = False


class ResolvedPhone(Model, omit_defaults=False):
    """A phone lookup. `resolved=false` with a `reason` is exit 13, not 5."""

    phone: str = ""
    e164: str = ""
    country: str | None = None
    prefix: str | None = None
    pattern: str | None = None
    resolved: bool = False
    peer: Peer | None = None
    reason: str | None = None
    countries: list[dict[str, Any]] = []


class ResolvedLink(Model):
    """A t.me / tg:// link, classified and (optionally) read.

    One discriminated shape rather than one command per link type: a human
    pasting a link does not know which of twenty kinds it is, and that is
    precisely the question.
    """

    kind: LinkKind
    raw_url: str = ""
    scheme: str = ""
    peer: Peer | None = None
    username: str | None = None
    phone: str | None = None
    invite_hash: str | None = None
    chatlist_slug: str | None = None
    msg_id: int | None = None
    thread_id: int | None = None
    comment_id: int | None = None
    story_id: int | None = None
    bot: str | None = None
    start_param: str | None = None
    start_target: str | None = None
    boost: bool | None = None
    gift: str | None = None
    stars: int | None = None
    proxy: dict[str, Any] | None = None
    theme: str | None = None
    wallpaper: str | None = None
    stickerset: str | None = None
    share: dict[str, Any] | None = None
    contact_token: str | None = None
    section: str | None = None
    deeplink_info: str | None = None
    title: str | None = None
    #: Set only with `--open`: the follow-up read for the classified kind.
    opened: dict[str, Any] | None = None
    requires_action: bool = False
    #: The command that would act on this link. Naming it is the alternative
    #: to acting: joining, starting a bot or redeeming a gift are separate,
    #: confirmed verbs in their own groups.
    delegated_to: str | None = None
    draft_saved: bool = False


class CachedPeerRow(Model, omit_defaults=False):
    """One entry of the per-account resolver cache.

    `min_context` is the `(chat, message)` where a `min` user was seen —
    Telethon stores none, and without it a stranger who posted in a channel
    is unaddressable.
    """

    id: int = 0
    marked_id: int = 0
    type: str = ""
    username: str | None = None
    phone: str | None = None
    access_hash_cached: bool = False
    min: bool = False
    min_context: str | None = None
    seen_at: str | None = None
    seen_at_unix: int | None = None
    refreshed: bool = False
    purged: bool = False
