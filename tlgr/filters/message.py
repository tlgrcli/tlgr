"""Message-attribute filters — media type, reply, forward status."""

from __future__ import annotations

from enum import Enum
from typing import Any

from tlgr.filters import register_filter
from tlgr.gateway.event import Event


class MessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    STICKER = "sticker"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"
    AUDIO = "audio"
    POLL = "poll"
    LOCATION = "location"
    LIVE_LOCATION = "live_location"
    CONTACT = "contact"
    GAME = "game"
    INVOICE = "invoice"
    DICE = "dice"
    GIF = "gif"
    WEBPAGE = "webpage"
    UNSUPPORTED = "unsupported"


def detect_message_type(message) -> MessageType:
    from telethon.tl.types import (
        DocumentAttributeAnimated,
        DocumentAttributeAudio,
        DocumentAttributeSticker,
        DocumentAttributeVideo,
        MessageMediaContact,
        MessageMediaDice,
        MessageMediaDocument,
        MessageMediaGame,
        MessageMediaGeo,
        MessageMediaGeoLive,
        MessageMediaInvoice,
        MessageMediaPhoto,
        MessageMediaPoll,
        MessageMediaWebPage,
    )

    if not message.media:
        return MessageType.TEXT
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        return MessageType.PHOTO
    if isinstance(media, MessageMediaPoll):
        return MessageType.POLL
    if isinstance(media, MessageMediaGeoLive):
        return MessageType.LIVE_LOCATION
    if isinstance(media, MessageMediaGeo):
        return MessageType.LOCATION
    if isinstance(media, MessageMediaContact):
        return MessageType.CONTACT
    if isinstance(media, MessageMediaGame):
        return MessageType.GAME
    if isinstance(media, MessageMediaInvoice):
        return MessageType.INVOICE
    if isinstance(media, MessageMediaDice):
        return MessageType.DICE
    if isinstance(media, MessageMediaWebPage):
        return MessageType.WEBPAGE
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc is None:
            return MessageType.UNSUPPORTED
        is_animated = False
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeAnimated):
                is_animated = True
            if isinstance(attr, DocumentAttributeSticker):
                return MessageType.STICKER
            if isinstance(attr, DocumentAttributeVideo):
                if attr.round_message:
                    return MessageType.VIDEO_NOTE
                if is_animated:
                    return MessageType.GIF
                return MessageType.VIDEO
            if isinstance(attr, DocumentAttributeAudio):
                if attr.voice:
                    return MessageType.VOICE
                return MessageType.AUDIO
        if is_animated or (hasattr(doc, "mime_type") and doc.mime_type == "image/gif"):
            return MessageType.GIF
        return MessageType.DOCUMENT
    return MessageType.UNSUPPORTED


def is_forwardable(message) -> tuple[bool, str]:
    """Quick sanity check used by the forward action."""
    if message.action is not None:
        return False, f"Service message: {type(message.action).__name__}"
    if not message.text and not message.media:
        return False, "Empty message"
    if message.media:
        if hasattr(message.media, "ttl_seconds") and message.media.ttl_seconds:
            return False, "Self-destructing message"
    return True, ""


@register_filter("types")
def filter_types(event: Event, value: Any) -> tuple[bool, str]:
    """Message type must be in *value*.  Value: list[str]."""
    if event.source != "telegram":
        return False, "types requires telegram source"
    mt = detect_message_type(event.raw.message)
    allowed = value if isinstance(value, list) else [value]
    if mt.value in allowed:
        return True, f"type={mt.value}"
    return False, f"type {mt.value} not in {allowed}"


@register_filter("exclude_types")
def filter_exclude_types(event: Event, value: Any) -> tuple[bool, str]:
    """Message type must NOT be in *value*.  Value: list[str]."""
    if event.source != "telegram":
        return False, "exclude_types requires telegram source"
    mt = detect_message_type(event.raw.message)
    excluded = value if isinstance(value, list) else [value]
    if mt.value in excluded:
        return False, f"type {mt.value} excluded"
    return True, f"type {mt.value} allowed"


@register_filter("has_media")
def filter_has_media(event: Event, value: Any) -> tuple[bool, str]:
    if event.source != "telegram":
        return False, "has_media requires telegram source"
    has = event.raw.message.media is not None
    if has == bool(value):
        return True, "media filter matched"
    return False, "media filter"


@register_filter("is_reply")
def filter_is_reply(event: Event, value: Any) -> tuple[bool, str]:
    if event.source != "telegram":
        return False, "is_reply requires telegram source"
    is_reply = event.raw.message.reply_to is not None
    if is_reply == bool(value):
        return True, "reply filter matched"
    return False, "reply filter"


@register_filter("is_forward")
def filter_is_forward(event: Event, value: Any) -> tuple[bool, str]:
    if event.source != "telegram":
        return False, "is_forward requires telegram source"
    is_fwd = event.raw.message.forward is not None
    if is_fwd == bool(value):
        return True, "forward filter matched"
    return False, "forward filter"
