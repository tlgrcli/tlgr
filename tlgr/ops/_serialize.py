"""Telethon object → model. One function per shape, and only one.

v1 assembled a message dict in three places with slightly different keys, so
`message list`, `message get` and the dialog preview disagreed about what a
message looks like. Everything here is duck-typed on the attributes Telethon
objects carry, which keeps the functions importable (and testable) without
Telethon and without a client.
"""

from __future__ import annotations

import base64
from typing import Any

from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.message import (
    MediaSummary,
    Message,
    MessageEntity,
    ReactionSummary,
    ReplyHeader,
    ServiceAction,
)
from tlgr.models.peer import Peer, Photo

__all__ = [
    "entity_to_peer",
    "marked_id",
    "media_summary",
    "message_entities",
    "message_to_model",
    "photo_summary",
    "reactions_summary",
    "service_action",
    "tl_snake",
]

_CHANNEL_MARK = -1000000000000

#: TL media class → the `MediaKind` it maps to. The document branch decides
#: for itself (see `media_summary`); this table is only for the media types
#: that are what their class name says they are.
_MEDIA_KINDS = {
    "MessageMediaPhoto": "photo",
    "MessageMediaContact": "contact",
    "MessageMediaGeo": "geo",
    "MessageMediaGeoLive": "geo_live",
    "MessageMediaVenue": "venue",
    "MessageMediaPoll": "poll",
    "MessageMediaDice": "dice",
    "MessageMediaGame": "game",
    "MessageMediaInvoice": "invoice",
    "MessageMediaWebPage": "webpage",
    "MessageMediaStory": "story",
    "MessageMediaGiveaway": "giveaway",
    "MessageMediaGiveawayResults": "giveaway",
    "MessageMediaPaidMedia": "paid",
    "MessageMediaToDo": "todo",
    "MessageMediaUnsupported": "unsupported",
    "MessageMediaEmpty": "unsupported",
}


def tl_snake(name: str, prefix: str = "") -> str:
    """`MessageActionChatAddUser` → `chat_add_user`."""
    name = name.removeprefix(prefix)
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def marked_id(raw_id: int, kind: str) -> int:
    """The marked id from `utils.get_peer_id`, without importing Telethon.

    Every peer id on the wire is marked (COR-10): `chat get` returning `123`
    and `chat list` returning `-100…123` for the same channel is the bug this
    closes.
    """
    if raw_id < 0:
        return raw_id
    if kind in ("channel", "supergroup"):
        return _CHANNEL_MARK - raw_id
    if kind == "group":
        return -raw_id
    return raw_id


def peer_id_of(peer: Any) -> int | None:
    """A `PeerUser`/`PeerChat`/`PeerChannel` as its marked id, or None.

    The same arithmetic `utils.get_peer_id` does, kept here so a serialiser
    can read a raw `from_id` without a client and without importing Telethon
    (§2.2 keeps that import out of everything below `ops/`).
    """
    if peer is None:
        return None
    for attribute, kind in (
        ("user_id", "user"),
        ("chat_id", "group"),
        ("channel_id", "channel"),
    ):
        value = getattr(peer, attribute, None)
        if isinstance(value, int):
            return marked_id(value, kind)
    return None


def _photo(obj: Any) -> Photo | None:
    if obj is None:
        return None
    identifier = getattr(obj, "photo_id", None) or getattr(obj, "id", None)
    if identifier is None:
        return None
    stripped = getattr(obj, "stripped_thumb", None)
    return Photo(
        id=int(identifier),
        has_video=bool(getattr(obj, "has_video", False)),
        stripped_thumb_b64=base64.b64encode(stripped).decode() if stripped else None,
        dc_id=getattr(obj, "dc_id", None),
    )


#: The photo shape, exported so an op can build one without reaching for a
#: private name.
photo_summary = _photo


def entity_to_peer(entity: Any) -> Peer:
    """A Telethon User/Chat/Channel as the `Peer` every response embeds."""
    name = type(entity).__name__
    raw_id = int(getattr(entity, "id", 0) or 0)
    usernames = [
        u.username
        for u in (getattr(entity, "usernames", None) or [])
        if getattr(u, "username", None)
    ]

    if name in ("User", "UserEmpty"):
        if getattr(entity, "is_self", False):
            kind, title = "saved", "Saved Messages"
        else:
            kind = "bot" if getattr(entity, "bot", False) else "user"
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            title = f"{first} {last}".strip()
    elif name in ("Chat", "ChatForbidden", "ChatEmpty"):
        kind, title = "group", getattr(entity, "title", "") or ""
    elif name in ("Channel", "ChannelForbidden"):
        kind = "supergroup" if getattr(entity, "megagroup", False) else "channel"
        title = getattr(entity, "title", "") or ""
    else:
        kind, title = "unknown", str(getattr(entity, "title", "") or "")

    return Peer(
        id=marked_id(raw_id, kind),
        raw_id=raw_id,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        username=getattr(entity, "username", None),
        usernames=usernames,
        is_self=bool(getattr(entity, "is_self", False)),
    )


def media_summary(media: Any) -> MediaSummary | None:
    """What the media IS, from the attributes it already carries.

    Ported from v1's `media_details()` with its logic intact: attributes are
    *collected* and the kind decided afterwards, because a GIF carries Video
    AND Animated and a video sticker carries Video AND Sticker — "first
    attribute wins" gets both of them wrong.
    """
    if media is None:
        return None
    tl_type = type(media).__name__

    if getattr(media, "photo", None) is not None and not hasattr(media, "document"):
        photo = media.photo
        return MediaSummary(
            kind="photo",
            tl_type=tl_type,
            spoiler=bool(getattr(media, "spoiler", False)),
            ttl_seconds=getattr(media, "ttl_seconds", None),
            dc_id=getattr(photo, "dc_id", None),
            thumbs=[
                str(getattr(size, "type", ""))
                for size in (getattr(photo, "sizes", None) or [])
                if getattr(size, "type", None)
            ],
        )

    document = getattr(media, "document", None)
    if document is None:
        summary = MediaSummary(
            kind=_MEDIA_KINDS.get(tl_type, "unsupported"),  # type: ignore[arg-type]
            tl_type=tl_type,
            downloadable=False,
            spoiler=bool(getattr(media, "spoiler", False)),
        )
        _fill_non_file(summary, media)
        return summary

    mime = getattr(document, "mime_type", None)
    summary = MediaSummary(
        kind="file",
        tl_type=tl_type,
        mime_type=mime,
        size=getattr(document, "size", None),
        dc_id=getattr(document, "dc_id", None),
        spoiler=bool(getattr(media, "spoiler", False)),
        ttl_seconds=getattr(media, "ttl_seconds", None),
        thumbs=[
            str(getattr(size, "type", ""))
            for size in (getattr(document, "thumbs", None) or [])
            if getattr(size, "type", None)
        ],
    )

    sticker = voice = audio = video = video_note = animated = False
    for attribute in getattr(document, "attributes", None) or []:
        name = type(attribute).__name__
        if name == "DocumentAttributeSticker":
            sticker = True
            summary.alt = getattr(attribute, "alt", None) or None
            stickerset = getattr(attribute, "stickerset", None)
            short_name = getattr(stickerset, "short_name", None)
            if short_name:
                summary.sticker_set = short_name
        elif name == "DocumentAttributeAudio":
            voice = bool(getattr(attribute, "voice", False))
            audio = not voice
            summary.duration = getattr(attribute, "duration", None)
            summary.performer = getattr(attribute, "performer", None)
            summary.title = getattr(attribute, "title", None)
            summary.waveform = getattr(attribute, "waveform", None) is not None
        elif name == "DocumentAttributeVideo":
            video_note = bool(getattr(attribute, "round_message", False))
            video = not video_note
            summary.duration = getattr(attribute, "duration", None)
            summary.width = getattr(attribute, "w", None)
            summary.height = getattr(attribute, "h", None)
            summary.round = video_note
            summary.supports_streaming = bool(getattr(attribute, "supports_streaming", False))
        elif name == "DocumentAttributeAnimated":
            animated = True
        elif name == "DocumentAttributeImageSize":
            summary.width = getattr(attribute, "w", None)
            summary.height = getattr(attribute, "h", None)
        elif name == "DocumentAttributeFilename":
            summary.file_name = getattr(attribute, "file_name", None)

    if sticker:
        summary.kind = "sticker"
    elif voice:
        summary.kind = "voice"
    elif video_note:
        summary.kind = "video_note"
    elif animated or mime == "image/gif":
        summary.kind = "gif"
        summary.is_animated = True
    elif video:
        summary.kind = "video"
    elif audio:
        summary.kind = "audio"
    return summary


def _fill_non_file(summary: MediaSummary, media: Any) -> None:
    """Flatten the non-file media variants onto the summary."""
    geo = getattr(media, "geo", None)
    if geo is not None:
        summary.latitude = getattr(geo, "lat", None)
        summary.longitude = getattr(geo, "long", None)
    summary.venue_title = getattr(media, "title", None)
    summary.contact_phone = getattr(media, "phone_number", None)
    first = getattr(media, "first_name", None)
    if first is not None:
        last = getattr(media, "last_name", "") or ""
        summary.contact_name = f"{first} {last}".strip()
    summary.dice_emoji = getattr(media, "emoticon", None)
    summary.dice_value = getattr(media, "value", None)
    webpage = getattr(media, "webpage", None)
    if webpage is not None:
        summary.webpage_url = getattr(webpage, "url", None)
        summary.webpage_title = getattr(webpage, "title", None)
    story_id = getattr(media, "id", None)
    if summary.kind == "story" and story_id is not None:
        summary.story_id = int(story_id)
    stars = getattr(media, "stars_amount", None)
    if stars is not None:
        summary.paid_stars = int(stars)


def reactions_summary(message: Any) -> ReactionSummary | None:
    """Compact reaction state, including whether WE already reacted.

    Returns None when the message carries no reactions, so the field only
    appears where it means something. `mine` comes from
    `ReactionCount.chosen_order`, which Telegram sets only for this account —
    without it the only way to learn that a reaction is already there is to
    send one and read MESSAGE_NOT_MODIFIED as a failure.
    """
    raw = getattr(message, "reactions", None)
    if not raw:
        return None
    counts: dict[str, int] = {}
    mine: list[str] = []
    for result in getattr(raw, "results", None) or []:
        reaction = getattr(result, "reaction", None)
        emoji = getattr(reaction, "emoticon", None)
        if emoji is None:
            # Custom (premium) reactions carry only a document id; naming them
            # is better than dropping them silently.
            document = getattr(reaction, "document_id", None)
            emoji = f"custom:{document}" if document is not None else "?"
        counts[emoji] = counts.get(emoji, 0) + int(getattr(result, "count", 0) or 0)
        if getattr(result, "chosen_order", None) is not None:
            mine.append(emoji)
    if not counts:
        return None
    return ReactionSummary(
        counts=counts,
        mine=mine,
        total=sum(counts.values()),
        can_see_list=getattr(raw, "can_see_list", None),
        as_tags=bool(getattr(raw, "reactions_as_tags", False)),
    )


def message_entities(message: Any) -> list[MessageEntity]:
    """Formatting runs, with their UTF-16 offsets untouched."""
    out: list[MessageEntity] = []
    for entity in getattr(message, "entities", None) or []:
        user_id = getattr(entity, "user_id", None)
        out.append(
            MessageEntity(
                type=tl_snake(type(entity).__name__, "MessageEntity"),
                offset=int(getattr(entity, "offset", 0)),
                length=int(getattr(entity, "length", 0)),
                url=getattr(entity, "url", None),
                user_id=getattr(user_id, "user_id", user_id),
                language=getattr(entity, "language", None),
                document_id=getattr(entity, "document_id", None),
                collapsed=getattr(entity, "collapsed", None),
            )
        )
    return out


def service_action(action: Any) -> ServiceAction | None:
    """A service message is an event, not "a message with empty text"."""
    if action is None:
        return None
    tl_type = type(action).__name__
    users = getattr(action, "users", None) or []
    single = getattr(action, "user_id", None)
    if single is not None:
        users = [*users, single]
    return ServiceAction(
        type=tl_snake(tl_type, "MessageAction"),
        tl_type=tl_type,
        user_ids=[int(u) for u in users],
        title=getattr(action, "title", None),
        photo=_photo(getattr(action, "photo", None)),
        duration=getattr(action, "duration", None),
        call_id=getattr(action, "call_id", None),
        ttl_seconds=getattr(action, "period", None) or getattr(action, "ttl_seconds", None),
        boosts=getattr(action, "boosts", None),
        stars=getattr(action, "stars", None),
    )


def _reply_header(message: Any) -> ReplyHeader | None:
    reply = getattr(message, "reply_to", None)
    if reply is None:
        message_id = getattr(message, "reply_to_msg_id", None)
        return ReplyHeader(message_id=message_id) if message_id else None
    peer = getattr(reply, "reply_to_peer_id", None)
    return ReplyHeader(
        message_id=getattr(reply, "reply_to_msg_id", None),
        peer_id=getattr(peer, "channel_id", None) or getattr(peer, "user_id", None),
        top_message_id=getattr(reply, "reply_to_top_id", None),
        forum_topic=bool(getattr(reply, "forum_topic", False)),
        quote_text=getattr(reply, "quote_text", None),
        quote_offset=getattr(reply, "quote_offset", None),
        story_id=getattr(reply, "story_id", None),
    )


def _forward(message: Any) -> Any:
    from tlgr.models.message import Forward

    forward = getattr(message, "fwd_from", None) or getattr(message, "forward", None)
    if forward is None:
        return None
    peer = getattr(forward, "from_id", None)
    from_id = None
    if peer is not None:
        for attribute, kind in (
            ("user_id", "user"),
            ("channel_id", "channel"),
            ("chat_id", "group"),
        ):
            value = getattr(peer, attribute, None)
            if value is not None:
                from_id = marked_id(int(value), kind)
                break
    return Forward(
        from_id=from_id,
        from_name=getattr(forward, "from_name", None),
        date=fmt_dt(getattr(forward, "date", None)),
        channel_post_id=getattr(forward, "channel_post", None),
        post_author=getattr(forward, "post_author", None),
        saved_from_msg_id=getattr(forward, "saved_from_msg_id", None),
        imported=bool(getattr(forward, "imported", False)),
    )


def _text_of(message: Any) -> str:
    """The message body, whether or not the object is bound to a client.

    Telethon's `Message.text` applies the client's parse mode and returns
    `None` when there is no client — which is every message tlgr builds from
    a raw `Updates` reply or receives through a fake. Falling back to
    `raw_text`/`message` is the difference between `message send` reporting
    the text it sent and reporting an empty string.
    """
    for attribute in ("text", "raw_text", "message"):
        value = getattr(message, attribute, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _sender_of(message: Any) -> int | None:
    """Who sent it, computed from `from_id` when Telethon has not.

    `Message.sender_id` is filled in by `_finish_init`, which only runs for a
    client-bound message. Every message tlgr builds from a raw `Updates` reply
    therefore reports no sender unless it is derived here, and "who sent this"
    is not an optional field.
    """
    known = getattr(message, "sender_id", None)
    if isinstance(known, int):
        return known
    peer = getattr(message, "from_id", None) or getattr(message, "peer_id", None)
    return peer_id_of(peer)


def message_to_model(
    message: Any, *, chat_id: int | None = None, link: str | None = None
) -> Message:
    """The one function that turns a Telethon message into a `Message`."""
    action = getattr(message, "action", None)
    date = getattr(message, "date", None)
    if chat_id is None:
        chat_id = int(getattr(message, "chat_id", 0) or 0)
    return Message(
        id=int(getattr(message, "id", 0)),
        chat_id=chat_id,
        date=fmt_dt(date) or "",
        date_unix=to_unix(date) or 0,
        text=_text_of(message),
        out=bool(getattr(message, "out", False)),
        kind="service" if action is not None else "message",
        sender_id=_sender_of(message),
        post_author=getattr(message, "post_author", None),
        via_bot_id=getattr(message, "via_bot_id", None),
        from_rank=getattr(message, "from_rank", None) if hasattr(message, "from_rank") else None,
        entities=message_entities(message),
        media=media_summary(getattr(message, "media", None)),
        reactions=reactions_summary(message),
        forward=_forward(message),
        reply_to=_reply_header(message),
        action=service_action(action),
        grouped_id=getattr(message, "grouped_id", None),
        edit_date=fmt_dt(getattr(message, "edit_date", None)),
        pinned=bool(getattr(message, "pinned", False)),
        silent=bool(getattr(message, "silent", False)),
        noforwards=bool(getattr(message, "noforwards", False)),
        mentioned=bool(getattr(message, "mentioned", False)),
        media_unread=bool(getattr(message, "media_unread", False)),
        ttl_period=getattr(message, "ttl_period", None),
        effect_id=getattr(message, "effect", None),
        views=getattr(message, "views", None),
        forwards=getattr(message, "forwards", None),
        edit_hide=bool(getattr(message, "edit_hide", False)),
        link=link,
    )
