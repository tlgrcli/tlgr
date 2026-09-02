"""Dialogs, drafts and folders — the chat-list side of the model."""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.message import Message, MessageEntity
from tlgr.models.peer import Peer

__all__ = ["Dialog", "Draft", "Folder", "NotifySettings"]


class NotifySettings(Model):
    muted: bool = False
    mute_until: str | None = None
    mute_until_unix: int | None = None
    silent: bool | None = None
    show_previews: bool | None = None
    sound: str | None = None
    stories_muted: bool | None = None


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
    chat: Peer
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    unread_mark: bool = False
    read_inbox_max_id: int = 0
    # The highest message of OURS the other side has read — the only honest
    # answer to "have they seen it?", and not derivable from read_inbox_max_id.
    read_outbox_max_id: int = 0
    top_message_id: int | None = None
    pinned: bool = False
    folder_id: int = 0
    archived: bool = False
    notify: NotifySettings | None = None
    draft: Draft | None = None
    ttl_period: int | None = None
    view_forum_as_messages: bool | None = None
    last_message: Message | None = None


class Folder(Model):
    id: int
    title: str
    emoticon: str | None = None
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
