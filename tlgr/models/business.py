"""Telegram Business: opening hours, location, intro, quick replies, links,
and the chatbot that may act on the account's behalf.

Two of these carry real risk and the models say so out loud.

* **`BotRights` is a full enumeration, never a mask.** Connecting a business
  bot hands another program the ability to read, reply, edit the profile and
  move Stars. A right that is absent from the model is a right nobody can
  audit, so every flag of `businessBotRights` is named.
* **Opening hours are minutes-of-week, and that arithmetic is lossy.**
  `BusinessOpen` keeps the server's raw minute offsets *and* the human
  spelling, because "mon 09:00-18:00" cannot represent an interval that
  crosses Sunday midnight and the server's vector can.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model
from tlgr.models.message import MessageEntity
from tlgr.models.peer import Peer

__all__ = [
    "BotConnection",
    "BotPaused",
    "BotRights",
    "BusinessAway",
    "BusinessGreeting",
    "BusinessIntro",
    "BusinessLocation",
    "BusinessMessage",
    "BusinessOpen",
    "BusinessProfile",
    "BusinessRecipients",
    "BusinessSet",
    "ChatLink",
    "ChatLinkSet",
    "QuickReply",
    "QuickReplyMessage",
    "QuickReplySent",
    "QuickReplySet",
    "StarsTransferQuote",
    "WorkHours",
]


class BusinessOpen(Model):
    """One opening interval.

    `start_minute`/`end_minute` are minutes since Monday 00:00 in the
    business's own timezone, which is what the server stores; `day` and the
    two clock strings are the same interval said the way a human wrote it.
    """

    start_minute: int
    end_minute: int
    day: str = ""
    open: str = ""
    close: str = ""


class WorkHours(Model):
    timezone_id: str = ""
    weekly_open: list[BusinessOpen] = []
    #: Server-set and never sent back: whether the business is open right now.
    open_now: bool | None = None


class BusinessLocation(Model):
    address: str = ""
    lat: float | None = None
    lon: float | None = None


class BusinessIntro(Model):
    title: str = ""
    description: str = ""
    sticker_id: int | None = None


class BusinessRecipients(Model):
    """Who a greeting/away message or a connected bot applies to."""

    contacts: bool = False
    non_contacts: bool = False
    existing_chats: bool = False
    new_chats: bool = False
    exclude_selected: bool = False
    users: list[int] = []
    exclude_users: list[int] = []


class BusinessGreeting(Model):
    shortcut_id: int = 0
    shortcut: str | None = None
    no_activity_days: int = 0
    recipients: BusinessRecipients | None = None
    enabled: bool = True


class BusinessAway(Model):
    shortcut_id: int = 0
    shortcut: str | None = None
    #: always | outside-hours | custom
    schedule: str = "always"
    since: str | None = None
    until: str | None = None
    offline_only: bool = False
    recipients: BusinessRecipients | None = None
    enabled: bool = True


class BusinessMessage(Model):
    """`business message set`, whichever of the two it configured.

    One shape for both, because a caller that just switched a greeting on
    should not have to know that the away message answers with a different
    struct — and the difference between them is two fields, not two ideas.
    """

    #: greeting | away
    kind: str = "greeting"
    shortcut_id: int = 0
    shortcut: str | None = None
    #: away only: always | outside-hours | custom
    schedule: str | None = None
    since: str | None = None
    until: str | None = None
    offline_only: bool = False
    #: greeting only.
    no_activity_days: int | None = None
    recipients: BusinessRecipients | None = None
    enabled: bool = True


class BotRights(Model):
    """`businessBotRights`, every flag named. Absent means "not granted"."""

    reply: bool = False
    read_messages: bool = False
    delete_sent_messages: bool = False
    delete_received_messages: bool = False
    edit_name: bool = False
    edit_bio: bool = False
    edit_username: bool = False
    edit_profile_photo: bool = False
    view_gifts: bool = False
    sell_gifts: bool = False
    change_gift_settings: bool = False
    transfer_and_upgrade_gifts: bool = False
    transfer_stars: bool = False
    manage_stories: bool = False


class BotConnection(Model):
    """A chatbot connected to (or pending on) this account."""

    bot_id: int = 0
    bot: Peer | None = None
    connection_id: str | None = None
    recipients: BusinessRecipients | None = None
    rights: BotRights | None = None
    paused: bool = False
    #: layer 229: a connection stays inert until the user confirms it.
    confirmed: bool = True
    disabled: bool = False
    deleted: bool = False
    date: str | None = None
    dc_id: int | None = None


class BotPaused(Model):
    chat_id: int
    paused: bool | None = None
    removed: bool = False
    already: bool = False


class ChatLink(Model):
    slug: str = ""
    link: str = ""
    title: str | None = None
    message: str = ""
    entities: list[MessageEntity] = []
    views: int | None = None


class ChatLinkSet(Model):
    slug: str | None = None
    link: str | None = None
    title: str | None = None
    message: str | None = None
    deleted: bool = False


class QuickReplyMessage(Model):
    id: int
    text: str = ""
    entities: list[MessageEntity] = []
    media: str | None = None
    date: str | None = None


class QuickReply(Model):
    shortcut_id: int
    shortcut: str = ""
    count: int = 0
    top_message: int | None = None
    messages: list[QuickReplyMessage] = []


class QuickReplySet(Model):
    shortcut_id: int | None = None
    shortcut: str | None = None
    msg_id: int | None = None
    msg_ids: list[int] = []
    deleted: int = 0
    order: list[int] = []
    already: bool = False


class QuickReplySent(Model):
    chat_id: int
    shortcut_id: int = 0
    message_ids: list[int] = []


class BusinessProfile(Model):
    """`business get`: the whole Business screen in one object."""

    work_hours: WorkHours | None = None
    open_now: bool | None = None
    location: BusinessLocation | None = None
    greeting: BusinessGreeting | None = None
    away: BusinessAway | None = None
    intro: BusinessIntro | None = None
    sponsored_enabled: bool | None = None
    connected_bots: list[BotConnection] = []
    chat_links: list[ChatLink] = []
    timezones: list[dict[str, Any]] = []
    premium: bool = False


class BusinessSet(Model):
    work_hours: WorkHours | None = None
    location: BusinessLocation | None = None
    intro: BusinessIntro | None = None
    changed: list[str] = []
    already: bool = False


class StarsTransferQuote(Model):
    """What a Stars transfer to a business bot *would* cost.

    `ok` is false and `reason` says why: tlgr reads the payment form and
    never signs it (see `ops/payment.py`).
    """

    bot_id: int
    stars: int = 0
    currency: str = "XTR"
    ok: bool = False
    reason: str = ""
    form_id: int | None = None
