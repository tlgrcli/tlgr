"""Stories: the item, its audience, its overlays, and who watched it.

Four shapes carry most of this group and each exists because the API makes a
distinction the GUI hides.

* **`Story`** is one story item. Telegram returns three different TL classes
  for one id — the full `storyItem`, a `storyItemSkipped` placeholder in a
  feed, and `storyItemDeleted` for one that is gone — so the model carries
  `skipped` and `deleted` flags rather than pretending a placeholder is a
  story with no caption.
* **`StoryPrivacy`** is the audience as a *base rule plus exceptions*, which
  is how `inputPrivacyRule*` vectors actually work and how the GUI's
  "Contacts, except Bob" is expressed. Flattening it into a single string
  would make that sentence inexpressible.
* **`MediaArea`** is one overlay pill. All eight TL variants are flattened
  into one struct with a `type` discriminator, because `story get --areas-out`
  has to write JSON that `story post --areas` reads back verbatim: a nested
  one-of would make that round trip depend on the caller knowing the variant.
* **`StoryViewer`** is one row of the viewers screen, which mixes plain views,
  public forwards and public reposts — `kind` says which, so a caller counting
  "people who watched" is not silently counting reposts too.

Coordinates on a media area are percentages of the media (0–100), which is
what the TL type stores; they are never pixels, because a story's rendered
size is a client decision.
"""

from __future__ import annotations

from typing import Any, Literal

from tlgr.models.base import Model
from tlgr.models.message import MediaSummary, Message, MessageEntity
from tlgr.models.peer import Peer, Photo, User

__all__ = [
    "AlbumDeleted",
    "AlbumOrder",
    "BlockedStoryUser",
    "BlocklistChange",
    "LiveStory",
    "MediaArea",
    "StealthMode",
    "StoriesDeleted",
    "Story",
    "StoryAlbum",
    "StoryEvent",
    "StoryExport",
    "StoryFeedPeer",
    "StoryHidden",
    "StoryLimits",
    "StoryPinned",
    "StoryPostCheck",
    "StoryPrivacy",
    "StoryReactionResult",
    "StoryRead",
    "StoryReply",
    "StoryReport",
    "StoryShared",
    "StoryStats",
    "StoryViewer",
    "StoryViews",
]

#: The eight `mediaArea*` variants, as one flat vocabulary.
MediaAreaKind = Literal[
    "geo",
    "venue",
    "reaction",
    "channel_post",
    "url",
    "weather",
    "star_gift",
    "unknown",
]

#: The base audience rule. `selected` is Telegram's "Only these people": an
#: empty allow list with this rule shows the story to nobody.
PrivacyBase = Literal["everyone", "contacts", "close-friends", "selected"]


class MediaArea(Model):
    """One overlay pill on a story, in the round-trippable JSON form.

    `x`/`y` are the centre of the area and `w`/`h` its size, all as
    percentages of the media, exactly as `mediaAreaCoordinates` stores them.
    """

    type: MediaAreaKind = "unknown"
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    rotation: float = 0.0
    radius: float | None = None
    # geo / venue
    latitude: float | None = None
    longitude: float | None = None
    address: dict[str, str] | None = None
    title: str | None = None
    address_text: str | None = None
    provider: str | None = None
    venue_id: str | None = None
    venue_type: str | None = None
    # suggested reaction
    reaction: str | None = None
    dark: bool = False
    flipped: bool = False
    # channel post
    chat_id: int | None = None
    msg_id: int | None = None
    # url
    url: str | None = None
    # weather
    emoji: str | None = None
    temperature_c: float | None = None
    color: int | None = None
    # collectible star gift
    slug: str | None = None


class StoryPrivacy(Model):
    """Who may see a story: a base rule, then the exceptions on top of it.

    The lists are *marked* ids, like every other id tlgr emits. They are only
    populated on your own stories — the server never tells you the audience of
    somebody else's.
    """

    base: PrivacyBase = "everyone"
    allow_users: list[int] = []
    allow_chats: list[int] = []
    disallow_users: list[int] = []
    disallow_chats: list[int] = []


class StoryViews(Model):
    """The counters under a story.

    `has_viewers` is the honest half: a non-Premium account loses the viewer
    list `story_viewers_expire_period` seconds after the story expires, and
    an empty list then means "no longer available", not "nobody watched".
    """

    views_count: int = 0
    forwards_count: int | None = None
    reactions_count: int | None = None
    has_viewers: bool = False
    recent_viewers: list[int] = []
    reactions: dict[str, int] = {}


class StoryFwdHeader(Model):
    """Where a reposted story came from."""

    from_id: int | None = None
    from_name: str | None = None
    story_id: int | None = None
    modified: bool = False


class Story(Model):
    """One story.

    `skipped` and `deleted` are the two placeholder states the API has:
    a feed hands back `storyItemSkipped` (id, dates and the close-friends flag
    only) and a gone story comes back as `storyItemDeleted`. Reporting either
    as an ordinary story with empty fields is how a caller ends up believing a
    deleted story is a caption-less one.
    """

    id: int
    peer_id: int = 0
    peer: Peer | None = None
    date: str | None = None
    date_unix: int | None = None
    expire_date: str | None = None
    expire_date_unix: int | None = None
    caption: str = ""
    entities: list[MessageEntity] = []
    media: MediaSummary | None = None
    media_areas: list[MediaArea] = []
    privacy: StoryPrivacy | None = None
    public: bool = False
    close_friends: bool = False
    contacts: bool = False
    selected_contacts: bool = False
    pinned: bool = False
    noforwards: bool = False
    edited: bool = False
    out: bool = False
    min: bool = False
    #: A placeholder row in a feed: only id, dates and `close_friends` are set.
    skipped: bool = False
    #: The story is gone; nothing but `id` is meaningful.
    deleted: bool = False
    #: A live story (an ongoing broadcast rather than a recorded item).
    live: bool = False
    fwd_from: StoryFwdHeader | None = None
    sent_reaction: str | None = None
    albums: list[int] = []
    music: MediaSummary | None = None
    views: StoryViews | None = None
    link: str | None = None
    translation: str | None = None


class StoryAlbum(Model):
    """A profile album — the named chips above the story grid."""

    id: int
    title: str = ""
    icon: Photo | None = None
    stories: list[int] = []
    stories_count: int | None = None


class AlbumDeleted(Model):
    peer: int = 0
    album_id: int = 0
    ok: bool = True


class AlbumOrder(Model):
    peer: int = 0
    order: list[int] = []
    ok: bool = True


class BlockedStoryUser(Model):
    """A row of "Hide my stories from" — a blocklist of its own."""

    user_id: int
    username: str | None = None
    name: str = ""
    date: str | None = None
    date_unix: int | None = None


class BlocklistChange(Model):
    added: list[int] = []
    removed: list[int] = []
    total: int | None = None
    already: bool = False


class StoryLimits(Model):
    """The app-config numbers that decide what a story may be.

    Every one of them is read from `help.getAppConfig`; none is hardcoded,
    because Telegram changes them without changing the layer.
    """

    expiring_limit: int | None = None
    sent_weekly_limit: int | None = None
    sent_monthly_limit: int | None = None
    caption_length_limit: int | None = None
    suggested_reactions_limit: int | None = None
    area_url_max: int | None = None
    albums_limit: int | None = None
    album_stories_limit: int | None = None
    pinned_to_top_max: int | None = None
    viewers_expire_period: int | None = None
    stealth_past_period: int | None = None
    stealth_future_period: int | None = None
    stealth_cooldown_period: int | None = None
    #: Which of the above this account's Premium status unlocks.
    premium_unlocks: list[str] = []


class StoryPostCheck(Model):
    """`story can-post`: the pre-flight the GUI runs before opening the camera."""

    can_post: bool = False
    reason: str = ""
    #: Seconds to wait, for a weekly/monthly flood; boosts missing, for a channel.
    retry_after: int | None = None
    boosts_required: int | None = None
    free_slots: int | None = None
    count_remains: int | None = None
    premium: bool = False
    limits: StoryLimits | None = None
    chats: list[Peer] = []


class StoryHiddenPeer(Model):
    """One row of a bulk `story hide`, in the same keys the single call uses."""

    user_id: int = 0
    username: str | None = None
    peer_id: int = 0
    hidden: bool = False
    already: bool = False


class StoryHidden(Model):
    """The per-account "Hide Stories" toggle, in v1's keys.

    `user_id`/`username` are what `tlgr user hide-stories` printed and stay
    spelled that way; `peer_id` is the marked id, for the channels the same
    RPC accepts. A single target answers exactly as v1 did; extra targets
    appear in `peers`, so a bulk pass stays one command without changing the
    shape the documented single-peer call returns.
    """

    user_id: int = 0
    username: str | None = None
    peer_id: int = 0
    hidden: bool = False
    already: bool = False
    #: Set instead of the peer fields when `--all` collapsed the whole bar.
    all: bool = False
    peers: list[StoryHiddenPeer] = []


class StoriesDeleted(Model):
    peer: int = 0
    deleted_ids: list[int] = []


class StoryPinned(Model):
    peer: int = 0
    ids: list[int] = []
    pinned: bool = False
    pinned_to_top: list[int] = []


class StoryRead(Model):
    peer: int = 0
    max_id: int = 0
    ids: list[int] = []
    ok: bool = True
    already: bool = False
    #: Ids whose view counter was incremented (`--register-view`).
    viewed_ids: list[int] = []


class StoryReactionResult(Model):
    peer: int = 0
    story_id: int = 0
    reaction: str = ""
    removed: bool = False
    #: Set when `--as-message` sent an ordinary reply instead.
    msg_id: int | None = None


class StoryReply(Model):
    chat_id: int = 0
    msg_id: int = 0
    reply_to_story: int = 0
    text: str = ""
    message: Message | None = None


class StoryShared(Model):
    sent: list[Message] = []
    story_id: int = 0
    peer: int = 0


class StoryReport(Model):
    """One step of the multi-step report flow."""

    result: str = ""
    title: str = ""
    options: list[dict[str, str]] = []
    comment_required: bool = False
    reported: bool = False


class StealthMode(Model):
    active_until_date: str | None = None
    active_until_unix: int | None = None
    cooldown_until_date: str | None = None
    cooldown_until_unix: int | None = None
    past: bool = False
    future: bool = False
    active: bool = False


class StoryViewer(Model):
    """One row of the viewers screen."""

    kind: Literal["view", "forward", "repost"] = "view"
    user_id: int = 0
    user: User | None = None
    peer: Peer | None = None
    date: str | None = None
    date_unix: int | None = None
    reaction: str | None = None
    blocked: bool = False
    blocked_my_stories_from: bool = False
    #: For a forward/repost row: where it landed.
    msg_id: int | None = None
    story_id: int | None = None


class StoryFeedPeer(Model):
    """One peer in the stories bar."""

    peer_id: int = 0
    peer: Peer | None = None
    max_read_id: int = 0
    stories: list[Story] = []
    unread_count: int = 0
    has_unread: bool = False
    live: bool = False
    hidden: bool = False
    #: Set by `--peers`: the compact `stories.getPeerMaxIDs` answer.
    max_id: int | None = None


class LiveStory(Model):
    """A live story: the ongoing-broadcast form of a story."""

    story_id: int = 0
    peer: int = 0
    live: bool = True
    date: str | None = None
    expire_date: str | None = None
    call_id: int | None = None
    participants_count: int | None = None
    streamer: int | None = None
    rtmp_stream: bool = False
    rtmp_url: str | None = None
    rtmp_key: str | None = None
    stream_dc_id: int | None = None
    messages_enabled: bool | None = None
    send_paid_messages_stars: int | None = None
    record_start_date: str | None = None
    listeners_hidden: bool | None = None
    pinned: bool = False
    noforwards: bool = False


class StoryExport(Model):
    count: int = 0
    out_dir: str = ""
    files: list[str] = []
    stories: list[Story] = []


class StoryStats(Model):
    views_graph: dict[str, Any] | None = None
    reactions_by_emotion_graph: dict[str, Any] | None = None
    forwards: list[dict[str, Any]] = []


class StoryEvent(Model):
    """One frame of `story watch`."""

    event: str = "story"
    kind: str = ""
    peer: int = 0
    story_id: int | None = None
    ids: list[int] = []
    reaction: str | None = None
    max_read_id: int | None = None
    stealth_mode: StealthMode | None = None
    at: str = ""
