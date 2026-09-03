"""Dialogs, drafts and folders — the chat-list side of the model.

Everything the chat list shows is here: the dialog row itself, the chat
folders that re-slice it, the notification exception behind the little
bell, the action bar Telegram puts above a stranger's chat, and the
per-chat decorations (theme, wallpaper, auto-delete timer).

Two shapes are deliberate.

* **A dialog carries its peer, not a flattened copy of it.** v1 spelled the
  chat three different ways depending on which command answered; here every
  row embeds the same `Peer`, so `chat.id` means one thing everywhere.
* **A notification setting is a tri-state.** `peerNotifySettings` fields are
  *unset* when the chat inherits the scope default, and `None` here means
  exactly that — not "off". Collapsing the two is how a client ends up
  reporting a muted chat as unmuted.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model
from tlgr.models.message import Message, MessageEntity
from tlgr.models.peer import Peer

__all__ = [
    "ActionBar",
    "ArchiveResult",
    "ArchiveSettings",
    "Badge",
    "Catchup",
    "CatchupChat",
    "ChatInfo",
    "ChatSwitches",
    "ChatTheme",
    "ChatlistInvite",
    "ChatlistJoin",
    "ChatlistUpdates",
    "ClearResult",
    "DeleteChatResult",
    "Dialog",
    "Draft",
    "Folder",
    "FolderBadge",
    "FolderDeleted",
    "FolderList",
    "FolderOrder",
    "ImportState",
    "LeaveResult",
    "MuteResult",
    "NotifySettings",
    "NotifyView",
    "OpenResult",
    "PeerResult",
    "PinnedDialogs",
    "Poster",
    "PosterReport",
    "Promo",
    "ReadChats",
    "SavedDialog",
    "SecretChat",
    "ShareDeleted",
    "SuggestedFolder",
    "ThemeResult",
    "TranslateResult",
    "TtlResult",
    "TypingResult",
    "UnreadResult",
    "Wallpaper",
    "WallpaperResult",
]


class NotifySettings(Model):
    """One chat's notification exception.

    `muted` is the answer to the question a human asks; the fields under it
    are what Telegram actually stores, and a `None` among them means "not
    set here, inherit the scope default".
    """

    muted: bool = False
    mute_until: str | None = None
    mute_until_unix: int | None = None
    silent: bool | None = None
    show_previews: bool | None = None
    sound: str | None = None
    stories_muted: bool | None = None
    stories_hide_sender: bool | None = None
    stories_sound: str | None = None


class NotifyView(Model):
    """`chat notify get`: the exception, and what it resolves to.

    Both halves are reported because they answer different questions. The
    exception is what you must send back to change one field without
    clobbering the others; the effective value is what the user is asking
    about when they say "is this chat muted?".
    """

    chat_id: int
    settings: NotifySettings
    effective: NotifySettings | None = None
    scope: str = ""
    scope_default: NotifySettings | None = None
    #: Field names taken from the scope default rather than set on the chat.
    inherited: list[str] = []


class Draft(Model):
    chat_id: int
    chat: Peer | None = None
    text: str = ""
    entities: list[MessageEntity] = []
    reply_to_msg_id: int | None = None
    top_msg_id: int | None = None
    no_webpage: bool = False
    effect_id: int | None = None
    date: str | None = None
    empty: bool = False


class Dialog(Model):
    """One row of the chat list."""

    chat: Peer
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    unread_poll_votes_count: int = 0
    unread_mark: bool = False
    read_inbox_max_id: int = 0
    # The highest message of OURS the other side has read — the only honest
    # answer to "have they seen it?", and not derivable from read_inbox_max_id.
    read_outbox_max_id: int = 0
    top_message_id: int | None = None
    pinned: bool = False
    pinned_order: int | None = None
    folder_id: int = 0
    archived: bool = False
    #: Ids of the chat *folders* (dialog filters) this chat falls into. Not
    #: the same thing as `folder_id`, which is Telegram's peer-folder (0 or 1).
    folders: list[int] = []
    notify: NotifySettings | None = None
    draft: Draft | None = None
    ttl_period: int | None = None
    view_forum_as_messages: bool | None = None
    requests_pending: int | None = None
    restricted: bool = False
    restriction_reason: list[str] = []
    participants_count: int | None = None
    #: `channels.getInactiveChannels` only: when the chat was last active.
    inactive_since: str | None = None
    last_message: Message | None = None


class SavedDialog(Model):
    """A Saved-Messages sublist, or a channel direct-message topic.

    The same TL family answers both, which is why one model does: a monoforum
    topic *is* a saved dialog whose parent peer is the channel.
    """

    origin_peer: Peer | None = None
    origin_id: int = 0
    parent_peer: int | None = None
    top_message_id: int | None = None
    pinned: bool = False
    unread_count: int = 0
    unread_mark: bool = False
    date: str | None = None
    date_unix: int | None = None


class Folder(Model):
    """A chat folder (`dialogFilter`), or a shared one (`dialogFilterChatlist`).

    A shared folder carries no type flags at all — Telegram will not accept
    them on one — so `is_chatlist` is the flag that says which half of this
    model is meaningful.
    """

    id: int
    title: str
    emoticon: str | None = None
    color: int | None = None
    title_noanimate: bool = False
    include_peers: list[int] = []
    exclude_peers: list[int] = []
    pinned_peers: list[int] = []
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False
    is_chatlist: bool = False
    has_my_invites: bool = False
    is_default: bool = False
    #: Populated by `folder list --with-counts`.
    chats: int | None = None
    unread_chats: int | None = None
    unread_messages: int | None = None


class FolderList(Model):
    tags_enabled: bool = False
    folders: list[Folder] = []


class FolderOrder(Model):
    order: list[int] = []


class FolderDeleted(Model):
    id: int
    deleted: bool = False
    left_chats: list[int] = []
    suggested: list[int] = []


class SuggestedFolder(Model):
    title: str
    description: str = ""
    emoticon: str | None = None
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False
    #: Set when `--add` created the folder from this suggestion.
    added_id: int | None = None


class ChatlistInvite(Model):
    slug: str
    url: str = ""
    title: str = ""
    peers: list[int] = []


class ChatlistJoin(Model):
    slug: str
    title: str = ""
    emoticon: str | None = None
    already_member: bool = False
    filter_id: int | None = None
    peers: list[int] = []
    missing_peers: list[int] = []
    already_peers: list[int] = []
    joined: list[int] = []


class ChatlistUpdates(Model):
    id: int
    missing_peers: list[int] = []
    joined: list[int] = []
    dismissed: bool = False


class ShareDeleted(Model):
    slug: str
    deleted: bool = False


# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------


class ActionBar(Model):
    """`peerSettings` — the bar Telegram shows above a chat with a stranger.

    The four date-ish fields at the bottom are the strongest cold-outreach
    triage signals a client is given, and no other call reports them.
    """

    chat_id: int
    report_spam: bool = False
    add_contact: bool = False
    block_contact: bool = False
    share_contact: bool = False
    need_contacts_exception: bool = False
    report_geo: bool = False
    autoarchived: bool = False
    invite_members: bool = False
    request_chat_title: str | None = None
    request_chat_date: str | None = None
    request_chat_broadcast: bool = False
    business_bot_id: int | None = None
    business_bot_paused: bool = False
    business_bot_can_reply: bool = False
    charge_paid_message_stars: int | None = None
    registration_month: str | None = None
    phone_country: str | None = None
    name_change_date: str | None = None
    photo_change_date: str | None = None
    geo_distance: int | None = None
    hidden: bool = False


class ChatTheme(Model):
    emoticon: str | None = None
    gift_id: int | None = None
    gift_slug: str | None = None
    title: str = ""
    premium_required: bool = False


class Wallpaper(Model):
    id: int | None = None
    slug: str | None = None
    dark: bool = False
    pattern: bool = False
    blur: bool = False
    intensity: int | None = None
    background_color: int | None = None
    second_background_color: int | None = None
    emoticon: str | None = None


class ChatInfo(Model):
    """`chat get`: the dialog row, the peer, and whatever `--full` added.

    One flat object rather than three nested ones because this is the command
    a script runs to answer a single question, and `--select` is how it picks
    the field it wants.
    """

    id: int
    raw_id: int = 0
    type: str = "unknown"
    title: str = ""
    username: str | None = None
    usernames: list[str] = []
    # -- dialog record --
    folder_id: int | None = None
    pinned: bool | None = None
    unread: int | None = None
    unread_mentions: int | None = None
    unread_reactions: int | None = None
    unread_mark: bool | None = None
    read_inbox_max_id: int | None = None
    read_outbox_max_id: int | None = None
    top_message_id: int | None = None
    notify_settings: NotifySettings | None = None
    draft: Draft | None = None
    ttl_period: int | None = None
    theme: ChatTheme | None = None
    wallpaper: Wallpaper | None = None
    translations_disabled: bool | None = None
    view_forum_as_messages: bool | None = None
    settings: ActionBar | None = None
    # -- full info --
    blocked: bool | None = None
    blocked_my_stories_from: bool | None = None
    requests_pending: int | None = None
    recent_requesters: list[int] = []
    common_chats_count: int | None = None
    personal_channel_id: int | None = None
    online_count: int | None = None
    restricted: bool | None = None
    restriction_reason: list[str] = []
    default_send_as: int | None = None
    noforwards: bool | None = None
    about: str | None = None
    participants_count: int | None = None
    admins_count: int | None = None
    kicked_count: int | None = None
    banned_count: int | None = None
    creator: bool | None = None
    left: bool | None = None
    forum: bool | None = None
    gigagroup: bool | None = None
    join_to_send: bool | None = None
    join_request: bool | None = None
    slowmode_seconds: int | None = None
    hidden_prehistory: bool | None = None
    participants_hidden: bool | None = None
    antispam: bool | None = None
    linked_chat_id: int | None = None
    linked_monoforum_id: int | None = None
    location: str | None = None
    level: int | None = None
    boosts_applied: int | None = None
    sticker_set: str | None = None
    emoji_set: str | None = None
    available_reactions: list[str] | None = None
    default_banned_rights: dict[str, Any] | None = None
    admin_rights: dict[str, Any] | None = None
    my_rank: str | None = None
    stats_dc: int | None = None
    can_view_stats: bool | None = None
    can_view_participants: bool | None = None
    can_set_stickers: bool | None = None
    can_set_location: bool | None = None
    can_delete_channel: bool | None = None
    can_view_revenue: bool | None = None
    can_view_stars_revenue: bool | None = None
    paid_reactions_available: bool | None = None
    has_welcome_messages: bool | None = None
    exported_invite: str | None = None
    pending_suggestions: list[str] = []
    main_tab: str | None = None
    #: v1's `chat get` spelled the title `name`; kept so a v1 script still reads.
    name: str = ""


class ChatSwitches(Model):
    chat_id: int
    noforwards: bool | None = None
    view_forum_as_messages: bool | None = None
    default_send_as: int | None = None
    send_as_options: list[Peer] = []


class TtlResult(Model):
    chat_id: int
    ttl_period: int | None = None
    set: bool = False


class ThemeResult(Model):
    chat_id: int
    theme: ChatTheme | None = None


class WallpaperResult(Model):
    chat_id: int
    wallpaper: Wallpaper | None = None
    for_both: bool = False
    overridden: bool = False


class TranslateResult(Model):
    chat_id: int
    translations_disabled: bool = False


class TypingResult(Model):
    chat_id: int
    action: str = "typing"
    duration: float = 0.0
    #: v1 answered `{"typing": true, "chat_id": …}`; AGENT.md documents it.
    typing: bool = False


class UnreadResult(Model):
    chat_id: int
    unread: bool = False


class PeerResult(Model):
    """One peer's outcome inside a bulk operation."""

    chat_id: int
    ok: bool = False
    error: str | None = None


class MuteResult(Model):
    muted: bool = False
    chat_id: int | None = None
    chat_ids: list[int] = []
    mute_until: str | None = None
    mute_until_unix: int | None = None
    stories: bool = False
    results: list[PeerResult] = []


class ArchiveResult(Model):
    archived: bool = False
    chat_id: int | None = None
    chat_ids: list[int] = []
    bar_hidden: bool = False


class ArchiveSettings(Model):
    archive_and_mute_new_noncontact_peers: bool = False
    keep_archived_unmuted: bool = False
    keep_archived_folders: bool = False


class PinnedDialogs(Model):
    pinned: bool = False
    chat_id: int | None = None
    chat_ids: list[int] = []
    folder: str = "main"
    order: list[int] = []


class ReadChats(Model):
    read: bool = False
    chat_id: int | None = None
    results: list[PeerResult] = []
    mentions_read: int | None = None
    reactions_read: int | None = None
    polls_read: bool | None = None


class ClearResult(Model):
    chat_id: int
    cleared: bool = False
    messages_affected: int = 0


class DeleteChatResult(Model):
    chat_id: int
    deleted: bool = False
    #: `me` | `both` | `everyone` — who the chat is gone for.
    scope: str = "me"
    left: bool = False


class LeaveResult(Model):
    left: bool = False
    chat_id: int | None = None
    chat_ids: list[int] = []
    errors: list[PeerResult] = []
    #: `messages.getFutureChatCreatorAfterLeave` — who inherits a basic group.
    successor: int | None = None


class OpenResult(Model):
    chat_id: int
    marked_read: bool = False
    messages: list[Message] = []


class CatchupChat(Model):
    id: int
    chat: Peer | None = None
    name: str = ""
    unread_count: int = 0
    unread_mark: bool = False
    messages: list[Message] = []


class Catchup(Model):
    chats: list[CatchupChat] = []


class FolderBadge(Model):
    id: int
    title: str = ""
    chats: int = 0
    messages: int = 0


class Badge(Model):
    """The number on the app icon, computed the way a client computes it."""

    chats: int = 0
    messages: int = 0
    muted_chats: int = 0
    muted_messages: int = 0
    mentions: int = 0
    reactions: int = 0
    folders: list[FolderBadge] = []
    limits: dict[str, Any] | None = None


class Poster(Model):
    """One sender in a chat's recent history."""

    user_id: int
    #: v1 spelled it `id`; both are emitted so a v1 script keeps working.
    id: int = 0
    username: str | None = None
    name: str = ""
    count: int = 0
    is_bot: bool = False
    is_deleted: bool = False
    last_msg_id: int | None = None
    date: str | None = None
    date_unix: int | None = None


class PosterReport(Model):
    posters: list[Poster] = []
    scanned_messages: int = 0
    distinct_posters: int = 0
    #: True when a flood wait cut the scan short: the harvest is a prefix of
    #: the truth, and reporting it as complete is the bug this flag prevents.
    partial: bool = False
    flood_wait: int | None = None


class Promo(Model):
    pending_suggestions: list[str] = []
    dismissed_suggestions: list[str] = []
    promo_peer: int | None = None
    psa_type: str | None = None
    psa_message: str | None = None
    birthdays_today: list[int] = []
    hidden: bool = False


class SecretChat(Model):
    id: int
    peer: int | None = None
    state: str = "unknown"
    layer: int | None = None
    created: str | None = None
    key_fingerprint: str | None = None
    ttl: int | None = None
    discarded: bool = False


class ImportState(Model):
    chat_id: int
    import_id: int | None = None
    media_count: int = 0
    started: bool = False
    messages: int = 0
    media: int = 0
    state: str = "checked"
