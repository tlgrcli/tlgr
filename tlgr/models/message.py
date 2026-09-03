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
    "ComposeResult",
    "DeleteResult",
    "DiceCatalog",
    "DraftCleared",
    "EditResult",
    "Effect",
    "EntityReport",
    "FactCheck",
    "Forward",
    "ForwardedMessage",
    "GameInfo",
    "GameScore",
    "LinkResult",
    "MediaKind",
    "MediaSummary",
    "Message",
    "MessageEntity",
    "PaidMessageSettings",
    "PinResult",
    "ReactResult",
    "ReactionSummary",
    "ReadReceipts",
    "ReadResult",
    "ReplyHeader",
    "ReplyMarkup",
    "ReportResult",
    "ScheduledSent",
    "ServiceAction",
    "SponsoredHidden",
    "SponsoredMessage",
    "SuggestedPostState",
    "SummaryResult",
    "Tone",
    "Transcription",
    "Translation",
    "ViewCount",
    "WebPagePreview",
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


class FactCheck(Model):
    """A country-scoped fact-check attached to a message.

    Readable by everyone; writable only by accounts Telegram has flagged as
    independent fact-checkers for that country, which is why the write half
    of `message fact-check set` usually fails with PERMISSION_DENIED.
    """

    msg_id: int | None = None
    country: str | None = None
    text: str = ""
    entities: list[MessageEntity] = []
    hash: int | None = None
    need_check: bool = False


class DeleteResult(Model):
    chat_id: int
    deleted: int = 0
    ids: list[int] = []
    pts: int | None = None
    #: `messages.AffectedHistory.offset` loops: how many messages the server
    #: reported as affected across every round trip.
    affected: int | None = None
    scheduled: bool = False


class EditResult(Model):
    """The edited message, or — with `--check` — whether it may be edited.

    `can_edit` and `edit_time_limit` come from `messages.getMessageEditData`
    and are the only honest answer to "can I still fix this?": the 48-hour
    window does not apply to pinned, scheduled or Saved-Messages posts, so it
    cannot be computed from the date on the client side.
    """

    id: int
    chat_id: int
    #: v1's `{"edited": true, …}`, kept for the shape AGENT.md documents.
    #: False by default so `omit_defaults` does not drop it when it is true.
    edited: bool = False
    edit_date: str | None = None
    text: str = ""
    entities: list[MessageEntity] = []
    can_edit: bool | None = None
    edit_time_limit: int | None = None
    caption: bool | None = None
    already: bool = False


class DraftCleared(Model):
    """What `draft clear` reports: v1's `{"cleared": true, "chat_id": …}`."""

    cleared: bool = False
    chat_id: int | None = None
    count: int = 0


class ForwardedMessage(Model):
    id: int
    chat_id: int
    date: str = ""
    date_unix: int = 0
    from_chat_id: int | None = None
    from_msg_id: int | None = None


class PinResult(Model):
    chat_id: int
    msg_id: int | None = None
    pinned: bool = False
    #: `unpin --all` reports how many were unpinned; a single unpin reports 1.
    unpinned: int | None = None
    count: int | None = None
    already: bool = False


class ReadResult(Model):
    chat_id: int
    #: v1 answered `{"read": true, "chat_id": …}` and AGENT.md documents it,
    #: so the flag stays. Defaulted to False rather than True because
    #: `omit_defaults` would drop a field whose value equals its default —
    #: and a compatibility key that is absent is not a compatibility key.
    read: bool = False
    read_up_to: int | None = None
    still_unread: int | None = None
    mentions_read: int | None = None
    reactions_read: int | None = None
    contents_read: list[int] = []


class ReadReceipts(Model):
    """Who read it, and when.

    `unavailable_reason` exists because Telegram refuses this in three
    different ways — too many members, too long ago, either side hiding read
    dates — and none of them is an error the caller can fix by retrying.
    """

    msg_id: int
    readers: list[int] = []
    users: list[Peer] = []
    read_date: str | None = None
    read_date_unix: int | None = None
    expired: bool = False
    unavailable_reason: str | None = None


class ReactResult(Model):
    chat_id: int
    msg_id: int
    emoji: str = ""
    reacted: bool = False
    already: bool = False
    reactions: ReactionSummary | None = None


class LinkResult(Model):
    link: str
    public: bool = False
    thread: bool = False
    chat_id: int = 0
    msg_id: int = 0


class ViewCount(Model):
    msg_id: int
    views: int | None = None
    forwards: int | None = None
    replies: int | None = None


class Translation(Model):
    msg_id: int | None = None
    text: str = ""
    entities: list[MessageEntity] = []
    lang: str = ""


class Transcription(Model):
    msg_id: int
    text: str = ""
    pending: bool = False
    transcription_id: int | None = None
    rated: str | None = None


class SummaryResult(Model):
    msg_id: int
    text: str = ""
    entities: list[MessageEntity] = []
    quota_left: int | None = None


class ComposeResult(Model):
    """An AI rewrite, plus the diff entities a proofread produces.

    `diff_*` is only populated when proofreading is the sole mode: Telegram
    emits `messageEntityDiffInsert/Delete/Replace` against the original text,
    and mixing a translation into the same call makes the diff meaningless.
    """

    text: str = ""
    entities: list[MessageEntity] = []
    diff_text: str | None = None
    diff_entities: list[MessageEntity] = []
    quota_left: int | None = None
    sent: Message | None = None


class EntityReport(Model):
    """What `--parse` did to a piece of text, in the units Telegram counts in."""

    text: str = ""
    entities: list[MessageEntity] = []
    #: Entities the *server* re-derives (url, email, mention, hashtag,
    #: cashtag, bot_command, phone, bank card). They must not be sent, which
    #: is why they are reported separately rather than merged in.
    auto_entities: list[MessageEntity] = []
    length_utf16: int = 0
    would_split: int = 1
    rendered: str | None = None


class Effect(Model):
    id: int
    emoticon: str = ""
    premium_required: bool = False
    static_icon_id: int | None = None
    effect_animation_id: int | None = None
    effect_sticker_id: int | None = None


class DiceCatalog(Model):
    """Which dice emoji exist and what counts as a win, read from appConfig.

    Never hardcoded: `emojies_send_dice` and `emojies_send_dice_success` are
    server-side, and a client that hardcodes them reports a new dice emoji as
    "unsupported media" the day Telegram adds one.
    """

    emojis: list[str] = []
    success_values: dict[str, int] = {}
    stake: dict[str, Any] | None = None


class WebPagePreview(Model):
    url: str = ""
    type: str | None = None
    site_name: str | None = None
    title: str | None = None
    description: str | None = None
    photo: Photo | None = None
    document: MediaSummary | None = None
    has_large_media: bool = False
    cached_page: bool = False
    pending: bool = False


class ReportResult(Model):
    """A report, or the next menu of options it wants.

    `messages.report` is a state machine: the first call answers with a list
    of options, and each `--option` walks one level deeper until the server
    reports the report was accepted.
    """

    ok: bool = False
    title: str | None = None
    options: list[dict[str, Any]] = []
    comment_required: bool = False
    already: bool = False


class ScheduledSent(Model):
    id: int
    chat_id: int
    date: str = ""
    date_unix: int = 0


class PaidMessageSettings(Model):
    user_id: int | None = None
    exempt: bool | None = None
    refunded_stars: int | None = None
    revenue_stars: int | None = None
    revenue_ton: int | None = None


class SponsoredMessage(Model):
    random_id: str
    title: str | None = None
    message: str = ""
    entities: list[MessageEntity] = []
    url: str | None = None
    button_text: str | None = None
    sponsor_info: str | None = None
    additional_info: str | None = None
    recommended: bool = False
    can_report: bool = False
    viewed: bool = False


class SponsoredHidden(Model):
    hidden: bool = False
    chat_id: int | None = None
    already: bool = False


class SuggestedPostState(Model):
    chat_id: int
    msg_id: int
    state: str = ""
    publish_at: str | None = None
    price: dict[str, Any] | None = None


class Tone(Model):
    slug: str
    title: str = ""
    prompt: str | None = None
    emoji_id: int | None = None
    installed: bool = False
    author: str | None = None
    examples: list[str] = []


class GameScore(Model):
    position: int | None = None
    user_id: int | None = None
    score: int = 0


class GameInfo(Model):
    title: str = ""
    short_name: str = ""
    description: str | None = None
    url: str | None = None
    scores: list[GameScore] = []


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
    paid_stars: int | None = None
    factcheck: FactCheck | None = None
    # Only `message thread list` fills these: a comment lives in the linked
    # discussion group, not in the channel the caller named, and saying so is
    # the difference between "id 42" and "id 42 *of what*".
    thread_root: int | None = None
    discussion_chat_id: int | None = None
    # `--with-reply` / `--context` hang the neighbouring messages off the one
    # that was asked for, rather than making the caller issue three requests.
    reply: Message | None = None
    context: list[Message] = []
    raw: dict[str, Any] | None = None
    #: `--delivery`: sent / read, derived from the *dialog's*
    #: `read_outbox_max_id` rather than from the message, which carries no
    #: such field.
    delivery: str | None = None
    #: The sibling ids one send produced — album items, or the parts of a
    #: `--split` text. Empty for the ordinary one-message send.
    batch: list[int] = []
