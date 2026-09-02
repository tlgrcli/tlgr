"""Message and everything hanging off it.

The shape answers, without a second RPC, the questions an agent actually asks
of a message: is it a sticker or a voice note (`media.kind`), did we already
react (`reactions.mine`), is it an event rather than text (`kind`/`action`),
did it come from a forum topic (`reply_to.top_message_id`), may it be
forwarded (`noforwards`). v1 assembled most of that ad hoc in three places as
loose `media_*` keys; here it is one struct built by one function.
"""

from __future__ import annotations

from typing import Any, Literal

from tlgr.models.base import Model
from tlgr.models.peer import Peer, Photo

__all__ = [
    "Button",
    "Forward",
    "MediaKind",
    "MediaSummary",
    "Message",
    "MessageEntity",
    "ReactionSummary",
    "ReplyHeader",
    "ReplyMarkup",
    "ServiceAction",
]

MediaKind = Literal[
    "photo",
    "video",
    "gif",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "file",
    "contact",
    "geo",
    "geo_live",
    "venue",
    "poll",
    "dice",
    "game",
    "invoice",
    "webpage",
    "story",
    "giveaway",
    "paid",
    "todo",
    "unsupported",
]


class MediaSummary(Model):
    """What the media IS, from attributes the message already carries.

    Nothing here downloads a byte. `tl_type` alone says `MessageMediaDocument`
    for a thumbs-up sticker, a voice note, a GIF and a PDF alike — and a
    caption-less one of those IS the message, so the distinction cannot be
    left to the caller.
    """

    kind: MediaKind
    tl_type: str
    mime_type: str | None = None
    file_name: str | None = None
    size: int | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    alt: str | None = None
    sticker_set: str | None = None
    performer: str | None = None
    title: str | None = None
    waveform: bool = False
    is_animated: bool = False
    supports_streaming: bool = False
    spoiler: bool = False
    ttl_seconds: int | None = None
    round: bool = False
    stripped_thumb_b64: str | None = None
    thumbs: list[str] = []
    dc_id: int | None = None
    downloadable: bool = True
    # Non-file media, flattened rather than nested as a one-of: a caller
    # projecting `--select media.latitude` should not have to know the variant.
    latitude: float | None = None
    longitude: float | None = None
    venue_title: str | None = None
    contact_phone: str | None = None
    contact_name: str | None = None
    dice_emoji: str | None = None
    dice_value: int | None = None
    webpage_url: str | None = None
    webpage_title: str | None = None
    story_peer_id: int | None = None
    story_id: int | None = None
    paid_stars: int | None = None


class MessageEntity(Model):
    """Formatting run. Offsets are UTF-16 code units, as Telegram defines them."""

    type: str
    offset: int
    length: int
    url: str | None = None
    user_id: int | None = None
    language: str | None = None
    document_id: int | None = None
    collapsed: bool | None = None


class Button(Model):
    text: str
    type: str
    data_b64: str | None = None
    url: str | None = None
    query: str | None = None
    user_id: int | None = None
    requires_password: bool = False


class ReplyMarkup(Model):
    kind: Literal["inline", "keyboard", "hide", "force_reply"]
    rows: list[list[Button]] = []
    resize: bool | None = None
    single_use: bool | None = None
    selective: bool | None = None
    persistent: bool | None = None
    placeholder: str | None = None


class ReactionSummary(Model):
    """Compact reaction state including whether WE already reacted.

    `mine` matters: Telegram answers a duplicate reaction with
    MESSAGE_NOT_MODIFIED, so without it the only way to learn that a reaction
    is already there is to send one and read the failure. It comes from
    `ReactionCount.chosen_order`, which Telegram sets only for this account.
    """

    counts: dict[str, int] = {}
    mine: list[str] = []
    total: int = 0
    can_see_list: bool | None = None
    as_tags: bool = False
    recent: list[dict[str, Any]] = []
    paid_stars: int | None = None


class Forward(Model):
    from_id: int | None = None
    from_name: str | None = None
    date: str | None = None
    channel_post_id: int | None = None
    post_author: str | None = None
    saved_from_peer_id: int | None = None
    saved_from_msg_id: int | None = None
    imported: bool = False


class ReplyHeader(Model):
    message_id: int | None = None
    peer_id: int | None = None
    top_message_id: int | None = None
    forum_topic: bool = False
    quote_text: str | None = None
    quote_entities: list[MessageEntity] = []
    quote_offset: int | None = None
    story_peer_id: int | None = None
    story_id: int | None = None
    todo_item_id: int | None = None


class ServiceAction(Model):
    """A service message is not "a message with empty text". It is an event."""

    type: str
    tl_type: str
    user_ids: list[int] = []
    title: str | None = None
    photo: Photo | None = None
    duration: int | None = None
    call_id: int | None = None
    ttl_seconds: int | None = None
    boosts: int | None = None
    stars: int | None = None
    payload: dict[str, Any] = {}


class Message(Model):
    id: int
    chat_id: int
    date: str
    date_unix: int
    text: str = ""
    out: bool = False
    kind: Literal["message", "service"] = "message"
    # --- who ---
    sender_id: int | None = None
    sender: Peer | None = None
    post_author: str | None = None
    via_bot_id: int | None = None
    from_rank: str | None = None
    send_as_id: int | None = None
    # --- what ---
    entities: list[MessageEntity] = []
    media: MediaSummary | None = None
    reply_markup: ReplyMarkup | None = None
    reactions: ReactionSummary | None = None
    forward: Forward | None = None
    reply_to: ReplyHeader | None = None
    action: ServiceAction | None = None
    grouped_id: int | None = None
    # --- state ---
    edit_date: str | None = None
    pinned: bool = False
    silent: bool = False
    noforwards: bool = False
    mentioned: bool = False
    media_unread: bool = False
    scheduled: bool = False
    ttl_period: int | None = None
    effect_id: int | None = None
    views: int | None = None
    forwards: int | None = None
    replies_count: int | None = None
    edit_hide: bool = False
    restriction_reason: list[str] = []
    link: str | None = None
