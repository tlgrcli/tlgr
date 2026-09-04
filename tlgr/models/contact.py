"""Contacts, users and blocking — the address-book side of the model.

Three shapes here exist because Telegram's own answers are ambiguous and a
CLI must not pass that ambiguity on as a fact.

* **`UserStatus.by_me`.** `userStatusRecently` is a *coarse bucket*, and the
  reason it is coarse is usually MY last-seen privacy, not theirs. The flag
  is carried through so nothing reports "they hid from you" when the truth is
  "you hid from everyone".
* **`ContactAdded.retry` and `imported`.** An empty `imported` means the
  number has no account **or** its owner refuses phone lookups. Both lists
  are reported rather than collapsed into a boolean.
* **`DialogStatus` is three-valued.** `resolved=false` carries
  `has_dialog=null`, never `false`. AGENT.md freezes this and
  `tests/test_ops_contacts.py` holds the line.

Access hashes never appear. `access_hash_cached` says whether one is held;
the value itself is per-login-session state that has no business in output a
human pastes into a bug report.
"""

from __future__ import annotations

from typing import Any, Literal

from tlgr.models.base import Model
from tlgr.models.message import Message
from tlgr.models.peer import Peer, Photo

__all__ = [
    "BlockResult",
    "BlockedPeer",
    "BlockedSet",
    "CloseFriends",
    "Contact",
    "ContactAdded",
    "ContactImport",
    "ContactModel",
    "ContactNote",
    "ContactRemoved",
    "ContactRenamed",
    "ContactRequirement",
    "ContactShared",
    "ContactSync",
    "DialogStatus",
    "FoundPeer",
    "ImportedPhone",
    "MusicTrack",
    "PersonalChannel",
    "PhoneShared",
    "PhotoResult",
    "ProfilePhoto",
    "SavedPhoneContact",
    "SignUp",
    "SuggestedBirthday",
    "TopPeer",
    "TopPeerState",
    "UserLink",
    "UserProfile",
    "UserStatus",
]

StatusKind = Literal["online", "offline", "recently", "last_week", "last_month", "empty"]


class ContactModel(Model, omit_defaults=False):
    """A contact/user shape that emits every field, including the false ones.

    `Model` drops defaults so that "absent" can mean "not applicable"; here
    the opposite is true. `already: false`, `resolved: false`, `has_dialog:
    null`, `added: false` and `hidden: false` are the *answer*, and AGENT.md
    publishes them — a caller that reads a missing key as "not set" would
    re-introduce exactly the three-valued confusion this group exists to
    remove.
    """


class UserStatus(ContactModel):
    """Online / last-seen, with the honesty flag Telegram attaches to it.

    `by_me` is set on the coarse buckets (`recently`, `last_week`,
    `last_month`) when the reason the answer is coarse is *our own*
    last-seen privacy. Reporting that as "they are hiding from you" is a
    conclusion the data does not support.
    """

    user_id: int = 0
    kind: StatusKind = "empty"
    expires: str | None = None
    expires_unix: int | None = None
    was_online: str | None = None
    was_online_unix: int | None = None
    by_me: bool = False


class Contact(ContactModel):
    """A row of the contact list.

    `phone` is present only where privacy allows it, which is why it is
    nullable rather than empty-string: "hidden" and "has none" are different.
    """

    id: int
    raw_id: int = 0
    first_name: str | None = None
    last_name: str | None = None
    name: str = ""
    username: str | None = None
    usernames: list[str] = []
    phone: str | None = None
    mutual: bool = False
    close_friend: bool = False
    premium: bool = False
    bot: bool = False
    deleted: bool = False
    verified: bool = False
    scam: bool = False
    fake: bool = False
    stories_hidden: bool = False
    has_unseen_stories: bool | None = None
    status: UserStatus | None = None
    birthday: str | None = None
    age: int | None = None
    note: str | None = None
    #: The total size of the server-side phonebook, echoed on every row's
    #: page rather than per row; `contact list --ids-only` reports it alone.
    saved_count: int | None = None


class ContactAdded(ContactModel):
    """The reply to `contact add`, keeping v1's `added`/`user_id` keys.

    `imported` empty with `retry` empty is the ambiguous case: the number has
    no Telegram account, or its owner hides it behind
    `inputPrivacyKeyAddedByPhone`. `reason` says which one we can and cannot
    tell apart.
    """

    added: bool = False
    user_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    imported: list[int] = []
    retry: list[int] = []
    popular_importers: int | None = None
    shared_phone: bool = False
    note: str | None = None
    reason: str | None = None


class ContactRenamed(ContactModel):
    """v1's shape, unchanged: this is only *our* view of their name."""

    saved: bool = True
    user_id: int = 0
    first_name: str = ""
    last_name: str = ""


class ContactRemoved(ContactModel):
    removed: bool = False
    user_ids: list[int] = []
    phones: list[str] = []


class ContactNote(ContactModel):
    user_id: int = 0
    note: str | None = None
    cleared: bool = False


class ImportedPhone(ContactModel):
    """One line of a phonebook import, and what the server made of it."""

    phone: str = ""
    first_name: str = ""
    last_name: str = ""
    user_id: int | None = None
    importers: int | None = None
    retry: bool = False


class ContactImport(ContactModel):
    """`contact import`, reporting the retry list rather than swallowing it.

    `retry` is not an error list: the server asks for those numbers to be
    sent again later, and a caller that drops them silently loses contacts.
    """

    parsed: int = 0
    imported: list[ImportedPhone] = []
    retry: list[ImportedPhone] = []
    popular_invites: list[ImportedPhone] = []
    batches: int = 0
    flood_waits: int = 0
    dry_run: bool = False


class ContactSync(ContactModel):
    """The diff between a local phonebook file and the server's list."""

    to_import: list[ImportedPhone] = []
    to_delete: list[str] = []
    applied: bool = False
    imported: int = 0
    deleted: int = 0


class SavedPhoneContact(ContactModel):
    """A number this account once uploaded, whether or not it has an account."""

    phone: str = ""
    first_name: str = ""
    last_name: str = ""
    date: str | None = None
    date_unix: int | None = None
    has_account: bool | None = None
    invite_text: str | None = None


class BlockedPeer(ContactModel):
    peer: Peer
    date: str | None = None
    date_unix: int | None = None
    kind: Literal["main", "stories"] = "main"


class BlockResult(ContactModel):
    """`user block` / `user unblock`. `already` means no RPC was needed."""

    peer_id: int = 0
    blocked: bool = False
    stories_only: bool = False
    already: bool = False
    deleted: bool = False
    reported: bool = False


class BlockedSet(ContactModel):
    """`contact blocked set` — a replacement, so the diff is the answer."""

    count: int = 0
    blocked: list[int] = []
    unblocked: list[int] = []
    kind: Literal["main", "stories"] = "main"
    applied: bool = False


class CloseFriends(ContactModel):
    user_ids: list[int] = []
    count: int = 0
    contacts: list[Contact] = []


class SignUp(ContactModel):
    """A contact who joined Telegram, found as a service message."""

    user_id: int = 0
    name: str = ""
    username: str | None = None
    chat_id: int = 0
    msg_id: int = 0
    date: str | None = None
    date_unix: int | None = None
    #: The account-wide "X joined Telegram" notification switch, echoed on
    #: the page so `--notify on|off` has somewhere to report its result.
    notify: bool | None = None


class TopPeer(ContactModel):
    peer: Peer
    category: str = "correspondents"
    rating: float = 0.0


class TopPeerState(ContactModel):
    enabled: bool | None = None
    reset_peer: int | None = None
    category: str | None = None
    disabled_by_user: bool = False


class FoundPeer(ContactModel):
    """A `contacts.search` hit, labelled with where it came from.

    `source` is the whole point: `mine` is a contact or an already-known
    peer, `global` is a public username match, `recent` is local search
    history and `sponsored` is an ad. Merging them into one list without the
    label is how a CLI ends up presenting an advert as a contact.
    """

    peer: Peer
    source: Literal["mine", "global", "recent", "sponsored", "tme"] = "mine"
    sponsored: bool = False
    random_id: str | None = None
    url: str | None = None


class ContactRequirement(ContactModel):
    """Can I message this user, and at what price?"""

    user_id: int = 0
    result: Literal["free", "premium", "paid", "unknown"] = "unknown"
    stars_amount: int | None = None
    contact_require_premium: bool | None = None


class DialogStatus(ContactModel):
    """SEMANTICS FROZEN (AGENT.md). Three answers, never conflated.

    `resolved=true, has_dialog=true`  — a dialog exists; `message_count` is
    the server's exact total.
    `resolved=true, has_dialog=false` — definitively none, because the
    account's *complete* dialog list was enumerated.
    `resolved=false, has_dialog=null` — could not be established; exit 13.

    `has_dialog` is deliberately `bool | None`: there is no third boolean,
    and a caller that reads `null` as `false` re-introduces the cold-contact
    bug this command exists to remove.

    The server's dialog object carries more than "does it exist", and the
    three read-state fields are echoed verbatim rather than reduced to a
    convenience boolean: `read_outbox_max_id` (the highest message of OURS
    the peer has read), `unread_count` and `top_message`. They are always
    present — a *missing* key is what makes an unanswerable question look
    answered, since a caller reading it back gets `null` and cannot tell
    "not read" from "never reported". `read_outbox_max_id >= top_message`
    only means "they saw our last message" when the last message is in fact
    ours, so that comparison is left to the caller, which is the only party
    that knows.
    """

    ref: str = ""
    id: int | None = None
    username: str | None = None
    resolved: bool = False
    has_dialog: bool | None = None
    message_count: int | None = None
    read_outbox_max_id: int | None = None
    unread_count: int | None = None
    top_message: int | None = None
    source: str = "unknown"
    reason: str | None = None
    scanned_dialogs: int | None = None


class UserProfile(ContactModel):
    """`user get` — v1's keys, plus everything `users.getFullUser` carries.

    v1's `id`, `first_name`, `last_name`, `username`, `phone`, `bio`,
    `is_bot`, `status`, `has_photo`, `deleted` and `stories_hidden` are all
    still here and still mean the same thing; `status` stays the short
    lowercase string v1 printed and `status_detail` carries the structured
    form.
    """

    id: int
    raw_id: int = 0
    kind: Literal["user", "bot"] = "user"
    first_name: str = ""
    last_name: str = ""
    name: str = ""
    username: str | None = None
    usernames: list[str] = []
    phone: str | None = None
    bio: str = ""
    bio_translated: str | None = None
    note: str | None = None
    birthday: str | None = None
    status: str = ""
    status_detail: UserStatus | None = None
    is_self: bool = False
    is_bot: bool = False
    is_contact: bool = False
    is_mutual_contact: bool = False
    is_close_friend: bool = False
    is_premium: bool = False
    is_support: bool = False
    is_verified: bool = False
    is_scam: bool = False
    is_fake: bool = False
    deleted: bool = False
    restricted: bool = False
    restriction_reason: list[str] = []
    has_photo: bool = False
    stories_hidden: bool = False
    lang_code: str | None = None
    photo: Photo | None = None
    personal_photo: Photo | None = None
    fallback_photo: Photo | None = None
    emoji_status_id: int | None = None
    colors: dict[str, Any] | None = None
    #: True only when `users.getFullUser` ran. Everything below it is null
    #: until it does, and this flag is how "not asked" is told apart from
    #: "asked, and the answer was nothing".
    full: bool = False
    blocked: bool | None = None
    blocked_my_stories_from: bool | None = None
    common_chats_count: int | None = None
    personal_channel_id: int | None = None
    personal_channel_message_id: int | None = None
    contact_require_premium: bool | None = None
    send_paid_messages_stars: int | None = None
    stargifts_count: int | None = None
    stars_rating: int | None = None
    main_tab: str | None = None
    unofficial_security_risk: bool | None = None
    business_hours: dict[str, Any] | None = None
    business_location: str | None = None
    business_intro: dict[str, Any] | None = None
    wallpaper: str | None = None
    action_bar: dict[str, Any] | None = None
    #: Whether this account holds a usable access hash for them. The hash
    #: itself is never printed: it is per-login-session and worthless (and
    #: dangerous) anywhere else.
    access_hash_cached: bool = False
    min: bool = False


class SuggestedBirthday(ContactModel):
    user_id: int = 0
    birthday: str = ""
    sent: bool = False


class UserLink(ContactModel):
    url: str = ""
    kind: str = "profile"
    expires: str | None = None
    expires_unix: int | None = None


class MusicTrack(ContactModel):
    id: int = 0
    title: str | None = None
    performer: str | None = None
    duration: int | None = None
    mime_type: str | None = None
    size: int | None = None
    file: str | None = None


class ProfilePhoto(ContactModel):
    id: int = 0
    date: str | None = None
    date_unix: int | None = None
    sizes: list[str] = []
    video: bool = False
    dc_id: int | None = None
    file: str | None = None
    #: Only filled for my own history (`profile photo list`): which of these
    #: is the avatar in force. Absent on another user's photos, because the
    #: server does not say.
    current: bool = False


class PhotoResult(ContactModel):
    user_id: int = 0
    photo_id: int | None = None
    suggested: bool = False
    reset: bool = False


class PersonalChannel(ContactModel):
    """The channel a user pinned to their profile, with a post preview."""

    user_id: int = 0
    channel: Peer | None = None
    msg_id: int | None = None
    posts: list[Message] = []


class ContactShared(ContactModel):
    chat_id: int = 0
    msg_id: int = 0
    contact: Contact | None = None


class PhoneShared(ContactModel):
    user_id: int = 0
    shared: bool = False
