"""Inline mode: what a bot answers a query with, and what sending one produces.

`InlineResult` flattens the two constructors Telegram uses — `botInlineResult`
(a URL and a `WebDocument` thumbnail the client has to fetch) and
`botInlineMediaResult` (a `Photo`/`Document` already on Telegram) — into one
row, because the difference is about where the bytes live and not about what
the caller is choosing between. `content` names which of the two it was, so a
caller that does care can still tell.

`query_id` travels on every row on purpose: it is only valid *paired* with a
result id, and only for `cache_time` seconds, so a row that carried the id
alone would be a row that cannot be sent.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "InlineEdited",
    "InlineResult",
    "InlineSent",
    "PreparedMessage",
    "PreparedSaved",
]


class InlineResult(Model):
    """One result out of `messages.getInlineBotResults`."""

    #: Flat row-major index within the page — what `inline send --pick` takes.
    n: int = 0
    id: str = ""
    type: str = ""
    title: str | None = None
    description: str | None = None
    url: str | None = None
    thumb: str | None = None
    #: `url` for a `botInlineResult`, `media` for a `botInlineMediaResult`.
    content: str = "url"
    #: The kind of message this result would send: text, media_auto, geo,
    #: venue, contact, invoice, webpage, game or rich.
    send_message: str | None = None
    query_id: str = ""
    doc_id: int | None = None
    photo_id: int | None = None
    gallery: bool = False
    cache_time: int | None = None
    next_offset: str | None = None
    switch_pm: dict[str, Any] | None = None
    switch_webview: dict[str, Any] | None = None


class InlineSent(Model):
    chat_id: int = 0
    msg_id: int = 0
    result_id: str = ""
    via_bot_id: int | None = None
    quick_reply: str | None = None


class InlineEdited(Model):
    inline_msg_id: str = ""
    edited: bool = False


class PreparedMessage(Model):
    """A message a mini app prepared for the user to share."""

    query_id: str = ""
    result: InlineResult | None = None
    peer_types: list[str] = []
    cache_time: int | None = None
    expires_at: str | None = None


class PreparedSaved(Model):
    id: str = ""
    expires_at: str | None = None
