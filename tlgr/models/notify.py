"""The Notifications screen: scopes, per-chat exceptions, reactions, sounds.

Telegram spreads one screen over three unrelated APIs — `getNotifySettings`
for the scopes and the chats, `getReactionsNotifySettings` for the reaction
alerts, `getContactSignUpNotification` for "X joined Telegram" — so the
model, like the command, is one shape with a `target` naming which of them
answered.

`mute_until` is an **absolute UNIX timestamp**, and the second field exists
to say so. v1 computed it from the asyncio event loop's clock, which is an
arbitrary monotonic origin: "mute for an hour" produced a timestamp somewhere
in 1970 and the chat was never muted at all.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.dialog import NotifySettings
from tlgr.models.peer import Peer

__all__ = [
    "ExceptionsCleared",
    "NotifyException",
    "NotifyReset",
    "NotifyTarget",
    "Ringtone",
    "RingtoneSaved",
]


class NotifyTarget(Model):
    """Notification settings for one target, whatever kind of target it is.

    `target` is the word the caller typed (`private`, `groups`, `channels`,
    `stories`, `reactions`, `contact-joined`, or a chat reference), so the
    answer can be piped straight back into `notify set`.
    """

    target: str
    kind: str = "scope"
    chat_id: int | None = None
    chat: Peer | None = None
    topic: int | None = None
    settings: NotifySettings | None = None
    muted: bool | None = None
    mute_until: str | None = None
    mute_until_unix: int | None = None
    show_previews: bool | None = None
    sound: str | None = None
    stories_muted: bool | None = None
    stories_hide_sender: bool | None = None
    stories_sound: str | None = None
    #: `reactions` target: contacts | all | off, one per alert kind.
    messages_from: str | None = None
    stories_from: str | None = None
    poll_votes_from: str | None = None
    #: `contact-joined` target. Stored inverted on the wire (`silent=true`).
    contact_joined: bool | None = None
    changed: list[str] = []
    already: bool = False


class NotifyException(Model):
    """A chat whose settings differ from its scope default."""

    chat_id: int
    chat: Peer | None = None
    title: str = ""
    muted: bool = False
    mute_until: str | None = None
    mute_until_unix: int | None = None
    show_previews: bool | None = None
    sound: str | None = None
    stories_muted: bool | None = None
    scope: str = ""


class ExceptionsCleared(Model):
    cleared: int = 0
    chat_ids: list[int] = []
    scope: str | None = None
    already: bool = False


class NotifyReset(Model):
    ok: bool = True


class Ringtone(Model):
    id: int
    access_hash: int | None = None
    file_name: str = ""
    mime_type: str = ""
    size: int = 0
    duration: int | None = None


class RingtoneSaved(Model):
    id: int | None = None
    file_name: str | None = None
    #: The server may hand back a *new* document id when an existing voice
    #: message is saved as a ringtone; using the old one afterwards fails.
    converted: bool = False
    removed: bool = False
    already: bool = False
