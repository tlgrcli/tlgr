"""My own profile — the Settings ▸ Edit Profile screen, as data.

Everything here describes *this* account rather than somebody else's: the
name and bio the world sees, the usernames (including the Fragment
collectibles), the avatar history, the accent colours, the emoji status and
the presence switch.

Two shapes are deliberate.

* **`ProfileFull` is one object, not a union of three RPCs.** `users.getUsers`
  answers the name, `users.getFullUser` the bio, birthday, personal channel
  and gift counters. v1 fetched only the first and hard-coded `bio` to `""`,
  which is a wrong answer dressed as a real one. One model, both calls.
* **A username is a row, not a string.** A collectible bought on Fragment can
  be active or parked, and only the *first* active one is the main handle;
  flattening the vector to `username` is how a script deactivates the wrong
  name.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "AdminedChannel",
    "ColorPalette",
    "ColorSet",
    "EmojiStatusItem",
    "EmojiStatusSet",
    "PhotosDeleted",
    "PresenceSet",
    "ProfileFull",
    "ProfileLink",
    "ProfilePhotoSet",
    "ProfileUpdated",
    "ProfileUsername",
    "UsernameSet",
]


class ProfileUsername(Model):
    """One entry of `user.usernames`.

    `main` is derived rather than reported: the server marks the basic
    username with `editable`, and the first *active* entry is what a link
    resolves to. Both facts matter and neither is the other.
    """

    username: str
    active: bool = True
    editable: bool = False
    main: bool = False


class ProfileFull(Model):
    """My profile as the Edit Profile screen shows it."""

    id: int = 0
    first_name: str = ""
    last_name: str = ""
    username: str | None = None
    usernames: list[ProfileUsername] = []
    phone: str | None = None
    premium: bool = False
    bot: bool = False
    #: Only present with `--full`; `""` means "fetched and empty", `None`
    #: means "not fetched", which v1 could not tell apart.
    bio: str | None = None
    birthday: str | None = None
    personal_channel_id: int | None = None
    personal_channel: Peer | None = None
    emoji_status: int | None = None
    emoji_status_collectible_id: int | None = None
    emoji_status_until: str | None = None
    color: int | None = None
    color_collectible_id: int | None = None
    background_emoji_id: int | None = None
    profile_color: int | None = None
    profile_background_emoji_id: int | None = None
    #: Which tab the profile page opens on (`stargifts`, `posts`, …).
    main_tab: str | None = None
    stargifts_count: int | None = None
    stars_rating: int | None = None
    ttl_period: int | None = None
    sponsored_enabled: bool | None = None
    #: Gift categories this account refuses, from `disallowed_gifts`.
    disallowed_gifts: list[str] = []
    photo_id: int | None = None
    fallback_photo_id: int | None = None
    contacts_count: int | None = None
    common_chats_count: int | None = None


class ProfileUpdated(Model):
    """What `profile update` actually changed, field by field.

    Only the fields the caller named appear, because the command spans three
    RPCs and "I asked for a birthday and got a name back" is a report nobody
    can act on.
    """

    first_name: str | None = None
    last_name: str | None = None
    bio: str | None = None
    birthday: str | None = None
    personal_channel_id: int | None = None
    photo_id: int | None = None
    changed: list[str] = []
    already: bool = False


class UsernameSet(Model):
    """`profile username set`, in all five of its moods."""

    username: str | None = None
    active: bool | None = None
    #: Only for `--check`: whether the name may be taken right now.
    available: bool | None = None
    #: Set when the server says `USERNAME_PURCHASE_AVAILABLE`: the name is
    #: free only on Fragment, which is a different answer from "taken".
    purchasable: bool = False
    usernames: list[ProfileUsername] = []
    already: bool = False


class ColorPalette(Model):
    """One entry of `help.getPeerColors` / `getPeerProfileColors`."""

    color_id: int
    colors: list[str] = []
    dark_colors: list[str] = []
    min_level: int = 0
    hidden: bool = False
    channel_min_level: int | None = None
    group_min_level: int | None = None
    #: True for palettes 0-6, whose colours every client hard-codes.
    builtin: bool = False


class ColorSet(Model):
    """`profile color set`: the palette now in force."""

    color: int | None = None
    collectible_id: int | None = None
    background_emoji_id: int | None = None
    for_profile: bool = False
    already: bool = False


class EmojiStatusItem(Model):
    """A wearable emoji status, from any of the four suggestion lists."""

    document_id: int = 0
    collectible_id: int | None = None
    title: str | None = None
    slug: str | None = None
    group: str = ""
    until: str | None = None


class EmojiStatusSet(Model):
    document_id: int | None = None
    collectible_id: int | None = None
    until: str | None = None
    until_unix: int | None = None
    cleared: bool = False
    already: bool = False


class PresenceSet(Model):
    """`profile presence set`. `online` is what was *reported*, not measured."""

    online: bool
    already: bool = False


class ProfilePhotoSet(Model):
    photo_id: int | None = None
    is_video: bool = False
    fallback: bool = False
    #: Set when the avatar was built server-side from a custom emoji.
    emoji_markup: bool = False


class PhotosDeleted(Model):
    deleted: int = 0
    photo_ids: list[int] = []
    already: bool = False


class ProfileLink(Model):
    """`profile link`: the public handle, and what Fragment knows about it."""

    link: str = ""
    username: str | None = None
    user_id: int | None = None
    #: A unicode-block QR, when `--qr` was given.
    qr: str | None = None
    qr_path: str | None = None
    #: `fragment.getCollectibleInfo`, when `--collectible` was given.
    collectible: dict[str, object] | None = None
    resolvable_by_strangers: bool = True


class AdminedChannel(Model):
    """A public channel eligible to be shown on the profile."""

    id: int
    title: str = ""
    username: str | None = None
    participants_count: int | None = None
    current: bool = False
