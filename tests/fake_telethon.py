"""An in-memory stand-in for `telethon.TelegramClient` (§11.1).

Backed by a small world model rather than by recorded fixtures, so the same
fake serves every group as the surface grows: a test says what exists and what
should go wrong, not which bytes came back in 2026.

Two rules make it worth trusting:

* it returns **real Telethon type objects** (`types.User`, `types.Channel`,
  `types.Message`), so a serialiser tested against it is tested against the
  shapes it will actually meet;
* it **records every request** in `world.calls`, so a test can assert what was
  *sent* — "`mute_until` is within five seconds of now + 3600" is the COR-01
  regression test, and it is not expressible by inspecting the return value.

Stage C adds the message world: a per-chat history the `iter_messages`
offsets actually walk, drafts, pinned state, and default handlers for the
send/edit/delete/forward/pin/read/draft requests. A test therefore asserts
against *history that moved*, not against a canned reply — `message send`
followed by `message list` shows the message, because the fake really stored
it.

Stage E adds the story world: real `types.StoryItem`s per peer, a profile
page and an archive that `story pin`/`story unpin` really move ids between,
albums, viewer rows, the opaque feed state `stories.getAllStories` pages on,
and the stealth-mode object. A story posted through `story post` is therefore
findable with `story list`, and `story hide` really flips the `stories_hidden`
flag the next `get_entity` reports.

Stage D adds the dialog world: real `types.Dialog` rows with unread counters,
notification settings, pin and archive state, plus the chat folders
(`dialogFilter`) the folder group rewrites. `chat archive` therefore *moves*
a row into folder 1 and `chat list --folder archive` finds it there, and
`chat mute --for 8h` writes an absolute `mute_until` a test can compare with
the wall clock — which is COR-01 and is not expressible by inspecting a
return value.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon.tl import types

__all__ = [
    "DH_G",
    "DH_PRIME",
    "AuthWorld",
    "DialogState",
    "FakeTelegramClient",
    "World",
    "fake_client_factory",
    "make_authorization",
    "make_channel",
    "make_document",
    "make_kdf",
    "make_message",
    "make_photo",
    "make_sticker_document",
    "make_sticker_set",
    "make_story",
    "make_user",
    "make_wallpaper",
    "make_web_authorization",
]

#: A real 2048-bit safe prime (`openssl dhparam 2048`, generator 2).
#:
#: It has to be a real one: `ops/_calls.validate_dh` refuses to place a call on
#: parameters it cannot verify, so a made-up prime would mean the call tests
#: only ever exercised the refusal. Generating one per test run costs about
#: forty seconds, which is why it is checked in instead.
DH_PRIME = base64.b64decode(
    "2g+3RJXZHhI9MewrU3whtSPnTbG+SLjAXxxP3+VJWLdkYnVDmWKFY5QDjyDsSYtUWBAgyyLEqu99"
    "QpjIvDiDsYIEYJNjKUhm8dEvl9Zz3W19TCg+OADBCu+O/qr0zbMNALT0B+zZHxcUDtGOAGq8ClKu"
    "B69vWNmwGwo3aQ4WQZWmjc24mY2hbVJBNRLGFRkvGUKYXqBX3Z4nmM6SchvAjKkVtdiJbJehjFVq"
    "D1jFMi0NSFn/9+IHF38RK2pzmE1W0NQeekeSEHsr285hvNSVxt3HfKkOq/2TpVKvdAYBnzYEca02"
    "EqyHyJRYMMmgTpicwNulUGFO2w1LUPVt6aZ9Jw=="
)
DH_G = 2


def _json_object(mapping: dict[str, Any]) -> Any:
    return types.JsonObject(
        value=[types.JsonObjectValue(key=key, value=_json_value(v)) for key, v in mapping.items()]
    )


# ---------------------------------------------------------------------------
# Builders — real Telethon objects, minimally populated
# ---------------------------------------------------------------------------


def make_user(user_id: int, *, username: str | None = None, first: str = "Test") -> types.User:
    return types.User(
        id=user_id,
        is_self=False,
        contact=False,
        mutual_contact=False,
        deleted=False,
        bot=False,
        verified=False,
        restricted=False,
        min=False,
        first_name=first,
        username=username,
        access_hash=user_id * 7,
    )


def _e164(phone: str) -> str:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    return f"+{digits}" if digits else ""


def _peer_for(marked: int) -> Any:
    """`types.Peer*` for a marked id, which is what a contacts reply carries."""
    if marked < -1000000000000:
        return types.PeerChannel(channel_id=-1000000000000 - marked)
    if marked < 0:
        return types.PeerChat(chat_id=-marked)
    return types.PeerUser(user_id=marked)


def make_channel(
    channel_id: int, *, title: str = "Channel", megagroup: bool = False
) -> types.Channel:
    return types.Channel(
        id=channel_id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(timezone.utc),
        creator=False,
        left=False,
        broadcast=not megagroup,
        megagroup=megagroup,
        access_hash=channel_id * 11,
        username=None,
    )


def make_message(
    message_id: int,
    *,
    chat_id: int = -100123,
    text: str = "",
    sender_id: int | None = None,
    out: bool = False,
    date: datetime | None = None,
) -> types.Message:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(channel_id=abs(chat_id) - 1000000000000)
        if chat_id < -1000000000000
        else types.PeerUser(user_id=abs(chat_id)),
        date=date or datetime.now(timezone.utc),
        message=text,
        out=out,
    )
    if sender_id is not None:
        message.from_id = types.PeerUser(user_id=sender_id)
    return message


def make_document(
    doc_id: int,
    *,
    mime: str = "image/jpeg",
    size: int = 1024,
    attributes: list[Any] | None = None,
    dc_id: int = 2,
) -> types.Document:
    """A real `types.Document`, so attribute-driven code is really exercised."""
    return types.Document(
        id=doc_id,
        access_hash=doc_id * 3,
        file_reference=f"ref{doc_id}".encode(),
        date=datetime.now(timezone.utc),
        mime_type=mime,
        size=size,
        dc_id=dc_id,
        attributes=list(attributes or []),
        thumbs=[types.PhotoStrippedSize(type="i", bytes=b"\x01\x02\x03")],
    )


def make_sticker_document(
    doc_id: int, emoji: str = "\U0001f600", *, custom_emoji: bool = False, short_name: str = "pack"
) -> types.Document:
    """A sticker or custom-emoji document, with the attribute that decides which."""
    stickerset = types.InputStickerSetShortName(short_name=short_name)
    attribute: Any = (
        types.DocumentAttributeCustomEmoji(alt=emoji, stickerset=stickerset, free=True)
        if custom_emoji
        else types.DocumentAttributeSticker(alt=emoji, stickerset=stickerset)
    )
    return make_document(
        doc_id,
        mime="application/x-tgsticker",
        size=4096,
        attributes=[attribute, types.DocumentAttributeImageSize(w=512, h=512)],
    )


def make_photo(photo_id: int = 900, *, dc_id: int = 2) -> types.Photo:
    return types.Photo(
        id=photo_id,
        access_hash=photo_id * 3,
        file_reference=f"pref{photo_id}".encode(),
        date=datetime.now(timezone.utc),
        sizes=[
            types.PhotoStrippedSize(type="i", bytes=b"\x01\x02\x03"),
            types.PhotoSize(type="x", w=1280, h=720, size=184320),
        ],
        dc_id=dc_id,
        has_stickers=False,
    )


def make_story(
    story_id: int,
    *,
    caption: str = "",
    media: Any = None,
    date: datetime | None = None,
    expire: datetime | None = None,
    public: bool = True,
    pinned: bool = False,
    close_friends: bool = False,
    noforwards: bool = False,
    out: bool = True,
    privacy: list[Any] | None = None,
    media_areas: list[Any] | None = None,
    views: Any = None,
    albums: list[int] | None = None,
) -> types.StoryItem:
    """A real `types.StoryItem`, so a serialiser meets the shape it will meet."""
    now = date or datetime.now(timezone.utc)
    return types.StoryItem(
        id=story_id,
        date=now,
        expire_date=expire or now,
        media=media or types.MessageMediaPhoto(photo=make_photo(900 + story_id)),
        caption=caption or None,
        pinned=pinned or None,
        public=public or None,
        close_friends=close_friends or None,
        noforwards=noforwards or None,
        out=out or None,
        privacy=privacy,
        media_areas=media_areas,
        views=views,
        albums=albums,
    )


def make_sticker_set(
    short_name: str,
    *,
    set_id: int = 111,
    title: str = "Pack",
    documents: list[Any] | None = None,
    emojis: bool = False,
    masks: bool = False,
    installed: bool = True,
    creator: bool = False,
    archived: bool = False,
) -> types.StickerSet:
    return types.StickerSet(
        id=set_id,
        access_hash=set_id * 5,
        title=title,
        short_name=short_name,
        count=len(documents or []),
        hash=0,
        installed_date=datetime.now(timezone.utc) if installed else None,
        archived=archived,
        official=False,
        masks=masks,
        emojis=emojis,
        creator=creator,
        thumbs=[],
    )


def make_wallpaper(slug: str, *, wallpaper_id: int = 555, pattern: bool = False) -> types.WallPaper:
    return types.WallPaper(
        id=wallpaper_id,
        access_hash=wallpaper_id * 7,
        slug=slug,
        document=make_document(wallpaper_id, mime="image/jpeg", size=2048),
        creator=False,
        default=False,
        pattern=pattern,
        dark=False,
        settings=types.WallPaperSettings(intensity=50, background_color=0xDBDDBB),
    )


# ---------------------------------------------------------------------------
# The auth world (PR-2)
# ---------------------------------------------------------------------------
#
# Sessions, the cloud password, websites, passkeys and Passport values are all
# *state the ops mutate*, so the fake stores them rather than answering with a
# canned reply: `account session terminate` followed by `account session list`
# has to show one fewer row, or the test is asserting against a lie.
#
# The SRP material is real. `telethon.password.compute_check` validates the
# prime, so a made-up `account.Password` would raise inside the helper the ops
# use — which would make every password test pass for the wrong reason.

#: Telegram's public 2048-bit DH prime, the one `check_prime_and_good`
#: fast-paths. Using it means the real SRP helpers run in tests.
GOOD_PRIME = bytes.fromhex(
    "c71caeb9c6b1c9048e6c522f70f13f73980d40238e3e21c14934d037563d930f"
    "48198a0aa7c14058229493d22530f4dbfa336f6e0ac925139543aed44cce7c37"
    "20fd51f69458705ac68cd4fe6b6b13abdc9746512969328454f18faf8c595f64"
    "2477fe96bb2a941d5bcd1d4ac8cc49880708fa9b378e3c4f3a9060bee67cf9a4"
    "a4a695811051907e162753b56b0f6b410dba74d8a84b2a14b3144e0ef1284754"
    "fd17ed950d5965b4b9dd46582db1178d169c6bc465b0d6ff9ca3928fef5b9ae4"
    "e418fc15e83ebea0f87fa9ff5eed70050ded2849f47bf959d956850ce929851f"
    "0d8115f635b105ee2e4e15d04b2454bf6f4fadf034b10403119cd8e3b92fcc5b"
)


def make_kdf(salt1: bytes = b"salt1-salt1-salt", salt2: bytes = b"salt2-salt2-salt"):
    """The KDF algo Telegram uses for cloud passwords."""
    return types.PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow(
        salt1=salt1, salt2=salt2, g=3, p=GOOD_PRIME
    )


def make_authorization(
    hash_: int,
    *,
    device: str = "Desktop",
    app: str = "Telegram Desktop",
    current: bool = False,
    unconfirmed: bool = False,
    password_pending: bool = False,
    created: datetime | None = None,
) -> types.Authorization:
    now = created or datetime.now(timezone.utc)
    return types.Authorization(
        hash=hash_,
        device_model=device,
        platform="linux",
        system_version="6.1",
        api_id=12345,
        app_name=app,
        app_version="1.0",
        date_created=now,
        date_active=now,
        ip="203.0.113.7",
        country="NL",
        region="North Holland",
        current=current or None,
        unconfirmed=unconfirmed or None,
        password_pending=password_pending or None,
    )


def make_web_authorization(hash_: int, *, domain: str = "example.com", bot_id: int = 4242):
    return types.WebAuthorization(
        hash=hash_,
        bot_id=bot_id,
        domain=domain,
        browser="Firefox",
        platform="linux",
        date_created=datetime.now(timezone.utc),
        date_active=datetime.now(timezone.utc),
        ip="203.0.113.7",
        region="NL",
    )


@dataclass
class AuthWorld:
    """Everything the `auth`, `account` and `passport` groups read or write.

    Held on `World.auth` rather than spread across it, so a test that cares
    about sessions does not have to know what a Passport value looks like.
    """

    authorizations: list[Any] = field(default_factory=list)
    web_authorizations: list[Any] = field(default_factory=list)
    passkeys: list[Any] = field(default_factory=list)
    secure_values: list[Any] = field(default_factory=list)
    #: The plaintext of the cloud password, or "" for an account without one.
    password: str = ""
    hint: str = ""
    recovery_email: str = ""
    has_secure_values: bool = False
    pending_reset_date: datetime | None = None
    account_ttl_days: int = 365
    authorization_ttl_days: int = 180
    device_locked_for: int | None = None
    app_config: dict[str, Any] = field(default_factory=dict)
    pending_suggestions: list[str] = field(default_factory=list)
    dismissed_suggestions: list[str] = field(default_factory=list)
    promo_peer: Any = None
    terms: Any = None
    logged_out: bool = False
    future_auth_token: bytes = b"future-token"
    #: Login-code plumbing.
    sent_code_type: Any = None
    signup_required: bool = False
    tmp_password: bytes = b"tmp-password"
    #: What `account.getPassword` should claim; `srp_id` is echoed back.
    srp_id: int = 1234567890
    #: Make the next SRP call answer SRP_ID_INVALID once. That is the branch
    #: `_auth.with_password` exists for: an srp_id is single-use, and a retry
    #: with a stale one fails forever unless the challenge is refetched.
    srp_id_invalid_once: bool = False

    def password_state(self) -> Any:
        algo = make_kdf()
        return types.account.Password(
            new_algo=algo,
            new_secure_algo=types.SecurePasswordKdfAlgoSHA512(salt=b"secure-salt"),
            secure_random=b"0" * 256,
            has_recovery=bool(self.recovery_email) or None,
            has_secure_values=self.has_secure_values or None,
            has_password=bool(self.password) or None,
            current_algo=algo if self.password else None,
            srp_B=b"\x01" * 256 if self.password else None,
            srp_id=self.srp_id if self.password else None,
            hint=self.hint or None,
            pending_reset_date=self.pending_reset_date,
        )


def _passes_filter(message: Any, media_filter: Any) -> bool:
    """Does this message survive an `InputMessagesFilter*`?

    Only the distinction the search tests turn on: an empty filter takes
    everything, any other filter takes the messages that carry media.
    """
    if media_filter is None or isinstance(media_filter, types.InputMessagesFilterEmpty):
        return True
    return getattr(message, "media", None) is not None


def _json_value(value: Any) -> Any:
    """A python value as the `JSONValue` tree `help.getAppConfig` returns."""
    if isinstance(value, dict):
        return types.JsonObject(
            value=[
                types.JsonObjectValue(key=str(k), value=_json_value(v)) for k, v in value.items()
            ]
        )
    if isinstance(value, (list, tuple)):
        return types.JsonArray(value=[_json_value(v) for v in value])
    if isinstance(value, bool):
        return types.JsonBool(value=value)
    if isinstance(value, (int, float)):
        return types.JsonNumber(value=float(value))
    if value is None:
        return types.JsonNull()
    return types.JsonString(value=str(value))


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class DialogState:
    """One chat's dialog row, as a test declares it."""

    chat_id: int
    top_message: int = 0
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    unread_poll_votes_count: int = 0
    unread_mark: bool = False
    pinned: bool = False
    folder_id: int = 0
    mute_until: datetime | None = None
    silent: bool | None = None
    ttl_period: int | None = None


@dataclass
class World:
    """What exists, and what should go wrong."""

    me: types.User = field(
        default_factory=lambda: types.User(
            id=777,
            is_self=True,
            contact=False,
            mutual_contact=False,
            deleted=False,
            bot=False,
            verified=False,
            restricted=False,
            min=False,
            first_name="Me",
            username="me",
            access_hash=1,
        )
    )
    authorized: bool = True
    users: dict[int, types.User] = field(default_factory=dict)
    chats: dict[int, Any] = field(default_factory=dict)
    dialogs: list[Any] = field(default_factory=list)
    #: marked chat id → history, newest last.
    messages: dict[int, list[types.Message]] = field(default_factory=dict)
    #: marked chat id → the draft saved there.
    drafts: dict[int, types.DraftMessage] = field(default_factory=dict)
    #: marked chat id → read_inbox_max_id / read_outbox_max_id.
    read_inbox: dict[int, int] = field(default_factory=dict)
    read_outbox: dict[int, int] = field(default_factory=dict)
    pinned: dict[int, set[int]] = field(default_factory=dict)
    #: The update state a session would hold: `{entity_id: (pts, qts, seq)}`,
    #: with entity 0 as the common box. `sync status` and `sync reset` read
    #: and write it exactly where Telethon's SQLiteSession keeps it.
    update_state: dict[int, tuple[int, int, int]] = field(
        default_factory=lambda: {0: (91824, 12, 4410)}
    )
    #: Peers the session has an access hash for. A channel missing from here
    #: is one `catch_up()` silently skips.
    entities: set[int] = field(default_factory=set)
    #: marked chat id → its dialog row. Ordered newest-first by top_message,
    #: which is how the server orders `messages.getDialogs`.
    dialogs_by_id: dict[int, DialogState] = field(default_factory=dict)
    #: The chat folders (`dialogFilter`), in display order.
    filters: list[Any] = field(default_factory=list)
    tags_enabled: bool = False
    #: The `peerSettings` action bar, per chat.
    peer_settings: dict[int, Any] = field(default_factory=dict)
    global_privacy: Any = None

    # -- the contact world -------------------------------------------------
    #
    # Stage E. The address book is a *world* too: adding a contact really
    # flips `user.contact`, blocking really puts the peer on the list
    # `contacts.getBlocked` answers with, and hiding stories really sets
    # `stories_hidden` — so the idempotent second pass can be asserted
    # against state that moved rather than against a canned reply.

    #: user id → mutual. Membership of the server-side contact list.
    contacts: dict[int, bool] = field(default_factory=dict)
    #: user id → the date it was blocked, per list.
    blocked: dict[int, datetime] = field(default_factory=dict)
    blocked_stories: dict[int, datetime] = field(default_factory=dict)
    #: user id → the private note attached to the contact.
    contact_notes: dict[int, str] = field(default_factory=dict)
    #: user id → `types.Birthday`, as birthday privacy lets us see it.
    birthdays: dict[int, Any] = field(default_factory=dict)
    #: Phone numbers this account ever uploaded (`contacts.getSaved`).
    saved_contacts: list[Any] = field(default_factory=list)
    #: phone (E.164) → the user id it imports to. A missing number is the
    #: ambiguous case: no account, or a privacy refusal.
    phonebook: dict[str, int] = field(default_factory=dict)
    #: category name → [(user id, rating)].
    top_peers: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    top_peers_enabled: bool = True
    #: `users.getRequirementsToContact`: user id → free | premium | paid:N.
    contact_requirements: dict[int, str] = field(default_factory=dict)
    #: user id → profile photo history, newest first.
    user_photos: dict[int, list[Any]] = field(default_factory=dict)
    #: user id → the documents pinned to their profile.
    saved_music: dict[int, list[Any]] = field(default_factory=dict)
    #: user id → `userFull` overrides, merged over the defaults.
    user_full: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: The "X joined Telegram" notification switch (silent=True means off).
    contact_signup_silent: bool = False
    #: Peers `contacts.search` answers with, split the way the server does.
    search_mine: list[int] = field(default_factory=list)
    search_global: list[int] = field(default_factory=list)
    sponsored_peers: list[int] = field(default_factory=list)
    #: The story read marks `stories.getAllReadPeerStories` reports.
    stories_read: dict[int, int] = field(default_factory=dict)
    #: `contacts.exportContactToken`.
    contact_token: str = "AbCdEfToken"
    #: `help.getDeepLinkInfo` for an unknown tg:// path.
    deep_link_message: str = "Update your app to open this link."
    all_stories_hidden: bool = False

    #: request type name → callable(request) -> result, or a plain value.
    raw: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    next_message_id: int = 1000
    next_poll_id: int = 5_000_000_000
    #: poll id → `[(user_id, option bytes, when)]`, the public-voter list.
    poll_votes: dict[int, list[tuple[int, bytes, datetime]]] = field(default_factory=dict)
    #: `(chat id, message id)` → who reacted, as `MessagePeerReaction`s.
    reaction_users: dict[tuple[int, int], list[Any]] = field(default_factory=dict)
    #: The chat's reaction policy, as `chatFull.available_reactions` holds it.
    chat_reactions: dict[int, Any] = field(default_factory=dict)
    reactions_limit: dict[int, int] = field(default_factory=dict)
    paid_enabled: dict[int, bool] = field(default_factory=dict)
    saved_tags: dict[str, str | None] = field(default_factory=dict)
    default_reaction: Any = None
    paid_privacy: str = "default"
    star_balance: int = 1000
    #: What `contacts.getLocated` should answer with.
    nearby: list[Any] = field(default_factory=list)
    self_expires: int | None = None
    #: The bytes `upload.getWebFile` hands back for a map thumbnail.
    web_file: bytes = b"\x89PNG\r\n\x1a\nfake"
    webfile_dc_id: int = 4
    venue_search_username: str = "foursquare"
    gif_search_username: str = "gif"
    #: Venues `messages.getInlineBotResults` should return for a geo query.
    venues: list[Any] = field(default_factory=list)
    #: Public posts `channels.searchPosts` should return.
    public_posts: list[Any] = field(default_factory=list)
    search_flood_remains: int = 3
    search_flood_stars: int = 10
    #: Sessions, the cloud password, websites, passkeys and Passport values.
    auth: AuthWorld = field(default_factory=AuthWorld)

    # -- the media world ---------------------------------------------------
    #
    # Documents are real `types.Document` objects with real attributes, so a
    # kind decided from them here is decided the same way it will be against
    # Telegram. `file_bytes` is what `iter_download` actually serves, which is
    # what makes a resumed or striped download testable at all.

    #: document id → `types.Document`.
    documents: dict[int, Any] = field(default_factory=dict)
    #: document id → the bytes a download receives.
    file_bytes: dict[int, bytes] = field(default_factory=dict)
    #: short name → {"set": types.StickerSet, "documents": [...], "packs": [...]}
    sticker_sets: dict[str, Any] = field(default_factory=dict)
    installed_sets: list[str] = field(default_factory=list)
    archived_sets: list[str] = field(default_factory=list)
    featured_sets: list[str] = field(default_factory=list)
    featured_unread: list[int] = field(default_factory=list)
    my_sets: list[str] = field(default_factory=list)
    faved: list[int] = field(default_factory=list)
    recent_stickers: list[int] = field(default_factory=list)
    saved_gifs: list[int] = field(default_factory=list)
    #: slug → `types.WallPaper`.
    wallpapers: dict[str, Any] = field(default_factory=dict)
    saved_wallpapers: list[str] = field(default_factory=list)
    installed_wallpaper: str | None = None
    sensitive_enabled: bool = False
    sensitive_can_change: bool = True
    auto_download: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_save: dict[str, Any] = field(default_factory=dict)
    #: profile photo bytes, per marked chat id.
    profile_photos: dict[int, bytes] = field(default_factory=dict)
    # -- the story world ---------------------------------------------------
    #
    # Keyed by *marked* peer id throughout, like every other id the fake
    # stores, so a story world assertion and a dialog world assertion talk
    # about the same number.

    #: marked peer id → {story id: `types.StoryItem`}.
    peer_stories: dict[int, dict[int, Any]] = field(default_factory=dict)
    #: marked peer id → the ids kept on the profile page.
    story_pinned: dict[int, set[int]] = field(default_factory=dict)
    #: marked peer id → the pinned-to-top set, in order.
    story_pinned_top: dict[int, list[int]] = field(default_factory=dict)
    #: marked peer id → the ids in the private archive, newest last.
    story_archive: dict[int, list[int]] = field(default_factory=dict)
    #: marked peer id → {album id: {"title": str, "stories": [ids]}}.
    story_albums: dict[int, dict[int, Any]] = field(default_factory=dict)
    #: marked peer id → the album order shown on the profile.
    story_album_order: dict[int, list[int]] = field(default_factory=dict)
    #: (marked peer id, story id) → the rows the viewers screen shows.
    story_viewers: dict[tuple[int, int], list[Any]] = field(default_factory=dict)
    #: marked peer id → the highest story id this account has read.
    story_read: dict[int, int] = field(default_factory=dict)
    #: The peers whose stories are collapsed into the archive bar.
    stories_hidden_peers: set[int] = field(default_factory=set)
    all_stories_hidden: bool = False
    #: The opaque state `stories.getAllStories` pages on.
    story_feed_state: str = "feed-state-1"
    story_feed_has_more: bool = False
    #: `None` means "the fake answers a fresh AllStories"; set it to a state
    #: string to make the server answer `storiesAllStoriesNotModified`.
    story_feed_not_modified: str | None = None
    stealth_mode: Any = None
    #: The story-only blocklist ("Hide my stories from"), as raw user ids.
    story_blocklist: list[int] = field(default_factory=list)
    #: What `stories.canSendStory` answers; a count means "yes".
    can_send_story: Any = None
    story_albums_hash: int = 4242
    #: `types.FoundStory` rows `stories.searchPosts` should return.
    public_stories: list[Any] = field(default_factory=list)
    #: marked peer id → the channels `stories.getChatsToSend` lists.
    chats_to_send: list[Any] = field(default_factory=list)
    next_story_id: int = 100
    inline_results: list[Any] = field(default_factory=list)
    saved_gif_limit: int = 200
    next_document_id: int = 7000
    faved_limit: int = 5

    # -- the call world ----------------------------------------------------
    #
    # Calls are held the way Telegram holds them: a group call belongs to a
    # chat (or to nobody, for a conference), a 1:1 call exists only as the
    # constructor an update carried. A test therefore says "this chat has a
    # video chat" and the ops resolve it exactly as they would in production.

    #: call id → `types.GroupCall`.
    group_calls: dict[int, Any] = field(default_factory=dict)
    #: marked chat id → the group call id running in it.
    chat_calls: dict[int, int] = field(default_factory=dict)
    #: call id → its `types.GroupCallParticipant` list.
    participants: dict[int, list[Any]] = field(default_factory=dict)
    #: call id → the `phoneCall*` constructor a 1:1 call is currently in.
    phone_calls: dict[int, Any] = field(default_factory=dict)
    #: The service messages `messages.search` returns for the Calls tab.
    call_log: list[Any] = field(default_factory=list)
    #: `help.getAppConfig`, reduced to the keys the call surface reads.
    app_config: dict[str, Any] = field(
        default_factory=lambda: {
            "call_requests_disabled": False,
            "conference_call_size_limit": 200,
            "group_call_message_length_limit": 128,
            "group_call_message_ttl": 10,
            "groupcall_video_participants_max": 30,
        }
    )
    #: The E2E chain, newest last, as raw blocks.
    chain_blocks: list[bytes] = field(default_factory=lambda: [b"block-0", b"block-1"])
    #: What one `upload.getFile` against a stream location returns.
    stream_chunk: bytes = b"OggS" + b"\0" * 60
    rtmp_url: str = "rtmps://dc4-1.rtmp.t.me/s/"
    rtmp_key: str = "0123456789abcdef"
    #: Set when the peer should look uncallable to `call start --check`.
    calls_available: bool = True

    # -- the administration world (Stage E) --------------------------------
    #
    # Everything the `chat member/admin/invite/topic/setting/stats` group
    # touches, kept as state rather than as canned replies — so a test can
    # ban somebody and then *list* them among the banned, which is the only
    # way to catch a mask that was written back incomplete.

    #: marked chat id → {user id: raw ChannelParticipant*}. Named `members`
    #: because `participants` is the call world's, and one World holds both.
    members: dict[int, dict[int, Any]] = field(default_factory=dict)
    #: marked chat id → {user id: ChatBannedRights}, the restricted/removed set
    banned: dict[int, dict[int, Any]] = field(default_factory=dict)
    #: marked chat id → the settings `chat setting get` reads out of chatFull
    settings: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: marked chat id → [chatInviteExported]
    invites: dict[int, list[Any]] = field(default_factory=dict)
    #: marked chat id → [chatInviteImporter] (join requests carry requested=True)
    importers: dict[int, list[Any]] = field(default_factory=dict)
    #: invite hash → what `messages.checkChatInvite` should answer
    invite_previews: dict[str, Any] = field(default_factory=dict)
    #: marked chat id → {topic id: forumTopic}
    topics: dict[int, dict[int, Any]] = field(default_factory=dict)
    #: marked chat id → [channelAdminLogEvent]
    admin_log: dict[int, list[Any]] = field(default_factory=dict)
    #: marked chat id → [boost]
    boosts: dict[int, list[Any]] = field(default_factory=dict)
    #: my premium boost slots
    my_boosts: list[Any] = field(default_factory=list)
    #: marked chat id → the default banned mask every member inherits
    default_banned: dict[int, Any] = field(default_factory=dict)
    #: usernames the server reports as already taken
    taken_usernames: set[str] = field(default_factory=set)

    # -- behaviour knobs ---------------------------------------------------

    _fail_next: dict[str, BaseException] = field(default_factory=dict)
    latency_ms: float = 0.0
    #: Drop the connection after this many requests. 0 disables.
    disconnect_after: int = 0
    connect_error: BaseException | None = None
    catch_ups: int = 0
    connects: int = 0
    saves: int = 0
    differences: int = 0
    takeout_calls: list[str] = field(default_factory=list)
    #: How far ahead of the local pts `updates.getState` answers.
    server_ahead: int = 0

    def fail_next(self, request_name: str, exc: BaseException) -> None:
        """Make the next request of that type raise, once."""
        self._fail_next[request_name] = exc

    def flood(self, request_name: str, seconds: int) -> None:
        from telethon.errors import FloodWaitError

        self._fail_next[request_name] = FloodWaitError(types.InputPeerSelf, capture=seconds)

    def add_user(self, user: types.User) -> types.User:
        self.users[user.id] = user
        return user

    def add_channel(self, channel: Any) -> Any:
        self.chats[channel.id] = channel
        return channel

    def called(self, request_name: str) -> list[Any]:
        return [payload for name, payload in self.calls if name == request_name]

    # -- the message world -------------------------------------------------

    def history(self, chat_id: int) -> list[types.Message]:
        return self.messages.setdefault(chat_id, [])

    def add_message(
        self,
        chat_id: int,
        text: str = "",
        *,
        message_id: int | None = None,
        out: bool = False,
        sender_id: int | None = None,
        date: datetime | None = None,
        media: Any = None,
    ) -> types.Message:
        """Put a message in a chat's history and return it."""
        if message_id is None:
            self.next_message_id += 1
            message_id = self.next_message_id
        message = make_message(
            message_id,
            chat_id=chat_id,
            text=text,
            out=out,
            sender_id=sender_id,
            date=date,
        )
        if media is not None:
            message.media = media
        self.history(chat_id).append(message)
        return message

    # -- the media world ---------------------------------------------------

    def add_document(self, document: Any, content: bytes = b"") -> Any:
        """Register a document and the bytes a download of it should return."""
        self.documents[int(document.id)] = document
        self.file_bytes[int(document.id)] = content or bytes(
            (i % 251) for i in range(int(getattr(document, "size", 0) or 16))
        )
        return document

    def add_sticker_set(
        self,
        short_name: str,
        documents: list[Any],
        *,
        set_id: int = 111,
        installed: bool = True,
        emojis: bool = False,
        masks: bool = False,
        creator: bool = False,
        archived: bool = False,
        featured: bool = False,
    ) -> Any:
        for document in documents:
            self.add_document(document)
        entry = {
            "set": make_sticker_set(
                short_name,
                set_id=set_id,
                documents=documents,
                emojis=emojis,
                masks=masks,
                installed=installed,
                creator=creator,
                archived=archived,
            ),
            "documents": list(documents),
        }
        self.sticker_sets[short_name] = entry
        if installed and short_name not in self.installed_sets:
            self.installed_sets.append(short_name)
        if archived and short_name not in self.archived_sets:
            self.archived_sets.append(short_name)
        if creator and short_name not in self.my_sets:
            self.my_sets.append(short_name)
        if featured and short_name not in self.featured_sets:
            self.featured_sets.append(short_name)
        return entry["set"]

    def set_for(self, stickerset: Any) -> Any:
        """Resolve any `InputStickerSet*` spelling to a registered entry."""
        name = getattr(stickerset, "short_name", None)
        if name is not None:
            return self.sticker_sets.get(str(name))
        set_id = getattr(stickerset, "id", None)
        if set_id is not None:
            for entry in self.sticker_sets.values():
                if entry["set"].id == int(set_id):
                    return entry
        emoticon = getattr(stickerset, "emoticon", None)
        marker = f"system:{type(stickerset).__name__}" + (f":{emoticon}" if emoticon else "")
        return self.sticker_sets.get(marker)

    def add_media_message(
        self,
        chat_id: int,
        *,
        document: Any = None,
        photo: Any = None,
        message_id: int | None = None,
        text: str = "",
        noforwards: bool = False,
        grouped_id: int | None = None,
    ) -> Any:
        """A message whose media is a real document or photo."""
        if document is not None:
            self.add_document(document)
            media: Any = types.MessageMediaDocument(document=document)
        else:
            photo = photo if photo is not None else make_photo()
            self.file_bytes[int(photo.id)] = b"jpegbytes" * 64
            media = types.MessageMediaPhoto(photo=photo)
        message = self.add_message(chat_id, text, message_id=message_id, media=media)
        message.noforwards = noforwards
        message.grouped_id = grouped_id
        return message

    def find(self, chat_id: int, message_id: int) -> types.Message | None:
        for message in self.history(chat_id):
            if message.id == message_id:
                return message
        return None

    # -- the dialog world --------------------------------------------------

    # -- the story world ---------------------------------------------------

    def add_story(
        self,
        peer_id: int,
        story: Any = None,
        *,
        pinned: bool = False,
        archived: bool = False,
        album: int | None = None,
        **fields: Any,
    ) -> Any:
        """Put a story on a peer, and on the shelves it belongs to.

        `pinned` and `archived` are the two *places* a story can also be, not
        properties of the item: the profile page and the private archive are
        separate RPCs, and a test that wants `story list --archive` to find
        something has to say so.
        """
        if story is None:
            story_id = fields.pop("story_id", None)
            if story_id is None:
                self.next_story_id += 1
                story_id = self.next_story_id
            story = make_story(int(story_id), **fields)
        self.peer_stories.setdefault(peer_id, {})[story.id] = story
        if pinned:
            self.story_pinned.setdefault(peer_id, set()).add(story.id)
            story.pinned = True
        if archived:
            self.story_archive.setdefault(peer_id, []).append(story.id)
        if album is not None:
            self.story_albums.setdefault(peer_id, {}).setdefault(
                album, {"title": f"Album {album}", "stories": []}
            )["stories"].append(story.id)
        return story

    def stories_of(self, peer_id: int) -> dict[int, Any]:
        return self.peer_stories.setdefault(peer_id, {})

    def add_album(self, peer_id: int, album_id: int, title: str, stories: list[int]) -> Any:
        self.story_albums.setdefault(peer_id, {})[album_id] = {
            "title": title,
            "stories": list(stories),
        }
        self.story_album_order.setdefault(peer_id, []).append(album_id)
        return self.story_albums[peer_id][album_id]

    def add_story_viewer(
        self, peer_id: int, story_id: int, user_id: int, *, reaction: str | None = None
    ) -> Any:
        row = types.StoryView(
            user_id=user_id,
            date=datetime.now(timezone.utc),
            reaction=types.ReactionEmoji(emoticon=reaction) if reaction else None,
        )
        self.story_viewers.setdefault((peer_id, story_id), []).append(row)
        return row

    def add_dialog(self, chat_id: int, **state: Any) -> DialogState:
        """Give a chat a dialog row. Anything unset is the server's default."""
        row = DialogState(chat_id=chat_id, **state)
        if not row.top_message:
            history = self.history(chat_id)
            row.top_message = history[-1].id if history else 0
        self.dialogs_by_id[chat_id] = row
        return row

    def dialog(self, chat_id: int) -> DialogState:
        row = self.dialogs_by_id.get(chat_id)
        if row is None:
            row = self.add_dialog(chat_id)
        return row

    def entity_for(self, chat_id: int) -> Any:
        """The user or chat a marked id names, or None."""
        raw_id = abs(chat_id)
        if chat_id < -1000000000000:
            raw_id = -1000000000000 - chat_id
        if chat_id == self.me.id:
            return self.me
        return self.users.get(raw_id) or self.chats.get(raw_id)

    def notify_of(self, chat_id: int) -> types.PeerNotifySettings:
        row = self.dialog(chat_id)
        return types.PeerNotifySettings(mute_until=row.mute_until, silent=row.silent)

    # -- the contact world -------------------------------------------------

    def add_contact(self, user: types.User, *, mutual: bool = False, **flags: Any) -> types.User:
        """Put a user in the address book, with the flags a contact carries."""
        self.add_user(user)
        user.contact = True
        user.mutual_contact = mutual
        for name, value in flags.items():
            setattr(user, name, value)
        self.contacts[int(user.id)] = mutual
        if getattr(user, "phone", None):
            self.phonebook.setdefault(_e164(user.phone), int(user.id))
        return user

    def block(self, user_id: int, *, stories: bool = False) -> None:
        target = self.blocked_stories if stories else self.blocked
        target[int(user_id)] = datetime.now(timezone.utc)

    def add_saved_contact(self, phone: str, first: str = "", last: str = "") -> Any:
        entry = types.SavedPhoneContact(
            phone=_e164(phone),
            first_name=first,
            last_name=last,
            date=datetime.now(timezone.utc),
        )
        self.saved_contacts.append(entry)
        return entry

    def add_folder(self, folder_id: int, title: str, **kwargs: Any) -> Any:
        """A `dialogFilter` in the world, addressed by id or by title."""
        folder = types.DialogFilter(
            id=folder_id,
            title=types.TextWithEntities(text=title, entities=[]),
            pinned_peers=kwargs.pop("pinned_peers", []),
            include_peers=kwargs.pop("include_peers", []),
            exclude_peers=kwargs.pop("exclude_peers", []),
            **kwargs,
        )
        self.filters.append(folder)
        return folder

    # -- the call world ----------------------------------------------------

    def add_group_call(
        self,
        call_id: int = 900100,
        *,
        chat_id: int | None = None,
        title: str | None = "standup",
        participants_count: int = 2,
        **flags: Any,
    ) -> types.GroupCall:
        """Put a group call in the world, optionally running in a chat."""
        call = types.GroupCall(
            id=call_id,
            access_hash=call_id * 3,
            participants_count=participants_count,
            unmuted_video_limit=flags.pop("unmuted_video_limit", 10),
            version=flags.pop("version", 1),
            title=title,
            **flags,
        )
        self.group_calls[call_id] = call
        if chat_id is not None:
            self.chat_calls[chat_id] = call_id
        return call

    def add_participant(
        self, call_id: int, user_id: int, *, source: int = 0, **flags: Any
    ) -> types.GroupCallParticipant:
        participant = types.GroupCallParticipant(
            peer=types.PeerUser(user_id=user_id),
            date=datetime.now(timezone.utc),
            source=source or user_id,
            **flags,
        )
        self.participants.setdefault(call_id, []).append(participant)
        return participant

    def add_phone_call(
        self, call_id: int = 4815162342, *, state: str = "Waiting", **flags: Any
    ) -> Any:
        """A `phoneCall*` constructor the daemon can be told about."""
        common = {
            "id": call_id,
            "access_hash": call_id * 5,
            "date": datetime.now(timezone.utc),
            "admin_id": flags.pop("admin_id", self.me.id),
            "participant_id": flags.pop("participant_id", 4242),
            "protocol": types.PhoneCallProtocol(
                min_layer=65, max_layer=92, library_versions=["2.4.4"]
            ),
        }
        if state == "Requested":
            call = types.PhoneCallRequested(g_a_hash=b"\x01" * 32, **common, **flags)
        elif state == "Accepted":
            call = types.PhoneCallAccepted(g_b=(2**2047).to_bytes(256, "big"), **common, **flags)
        else:
            call = types.PhoneCallWaiting(**common, **flags)
        self.phone_calls[call_id] = call
        return call

    def add_call_log_entry(
        self,
        message_id: int,
        *,
        chat_id: int = 4242,
        video: bool = False,
        reason: str = "hangup",
        duration: int = 42,
        out: bool = False,
        conference: bool = False,
    ) -> Any:
        """One Calls-tab row: a service message with a call action."""
        reasons = {
            "hangup": types.PhoneCallDiscardReasonHangup(),
            "missed": types.PhoneCallDiscardReasonMissed(),
            "busy": types.PhoneCallDiscardReasonBusy(),
        }
        action: Any
        if conference:
            action = types.MessageActionConferenceCall(
                call_id=message_id * 10,
                video=video,
                duration=duration,
                missed=reason == "missed",
                other_participants=[types.PeerUser(user_id=4243)],
            )
        else:
            action = types.MessageActionPhoneCall(
                call_id=message_id * 10,
                video=video,
                reason=reasons[reason],
                duration=duration,
            )
        message = types.MessageService(
            id=message_id,
            peer_id=types.PeerUser(user_id=abs(chat_id)),
            date=datetime.now(timezone.utc),
            out=out,
            action=action,
        )
        self.call_log.append(message)
        return message

    # -- the administration world ------------------------------------------

    def add_member(
        self,
        chat_id: int,
        user_id: int,
        *,
        status: str = "member",
        rank: str | None = None,
        admin_rights: Any = None,
        inviter_id: int | None = None,
        promoted_by: int | None = None,
        can_edit: bool | None = None,
    ) -> Any:
        """Put a real `ChannelParticipant*` in a chat.

        The wrapper, not a bare user: "is this person an admin, and who
        promoted them" is exactly what v1's member list threw away, so the
        fake has to be able to answer it.
        """
        now = datetime.now(timezone.utc)
        if status == "creator":
            row: Any = types.ChannelParticipantCreator(
                user_id=user_id,
                admin_rights=admin_rights or types.ChatAdminRights(change_info=True),
                rank=rank,
            )
        elif status == "admin":
            row = types.ChannelParticipantAdmin(
                user_id=user_id,
                promoted_by=promoted_by or self.me.id,
                date=now,
                admin_rights=admin_rights or types.ChatAdminRights(ban_users=True),
                can_edit=True if can_edit is None else can_edit,
                inviter_id=inviter_id,
                rank=rank,
            )
        elif status == "self":
            row = types.ChannelParticipantSelf(
                user_id=user_id, inviter_id=inviter_id or self.me.id, date=now
            )
        else:
            row = types.ChannelParticipant(user_id=user_id, date=now, rank=rank)
        self.members.setdefault(chat_id, {})[user_id] = row
        return row

    def add_invite(self, chat_id: int, link: str, **kwargs: Any) -> Any:
        invite = types.ChatInviteExported(
            link=link,
            admin_id=kwargs.pop("admin_id", self.me.id),
            date=kwargs.pop("date", datetime.now(timezone.utc)),
            **kwargs,
        )
        self.invites.setdefault(chat_id, []).append(invite)
        return invite

    def add_importer(
        self, chat_id: int, user_id: int, *, requested: bool = False, **kw: Any
    ) -> Any:
        row = types.ChatInviteImporter(
            user_id=user_id,
            date=kw.pop("date", datetime.now(timezone.utc)),
            requested=requested or None,
            **kw,
        )
        self.importers.setdefault(chat_id, []).append(row)
        return row

    def add_topic(self, chat_id: int, topic_id: int, title: str, **kwargs: Any) -> Any:
        topic = types.ForumTopic(
            id=topic_id,
            date=kwargs.pop("date", datetime.now(timezone.utc)),
            peer=types.PeerChannel(channel_id=-1000000000000 - chat_id),
            title=title,
            icon_color=kwargs.pop("icon_color", 0x6FB9F0),
            top_message=kwargs.pop("top_message", 0),
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=kwargs.pop("unread_count", 0),
            unread_mentions_count=0,
            unread_reactions_count=0,
            unread_poll_votes_count=0,
            from_id=types.PeerUser(user_id=self.me.id),
            notify_settings=types.PeerNotifySettings(),
            **kwargs,
        )
        self.topics.setdefault(chat_id, {})[topic_id] = topic
        return topic

    def add_admin_log(self, chat_id: int, event_id: int, action: Any, *, user_id: int = 0) -> Any:
        event = types.ChannelAdminLogEvent(
            id=event_id,
            date=datetime.now(timezone.utc),
            user_id=user_id or self.me.id,
            action=action,
        )
        self.admin_log.setdefault(chat_id, []).append(event)
        return event

    def settings_of(self, chat_id: int) -> dict[str, Any]:
        return self.settings.setdefault(chat_id, {})


class _Dialog:
    """The shape `iter_dialogs` yields, reduced to what tlgr reads."""

    def __init__(self, entity: Any, *, unread: int = 0) -> None:
        self.entity = entity
        self.id = getattr(entity, "id", 0)
        self.name = getattr(entity, "title", None) or getattr(entity, "first_name", "")
        self.unread_count = unread
        self.message = None
        self.archived = False
        self.pinned = False


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class FakeTelegramClient:
    """Everything the daemon touches on a Telethon client, and nothing else."""

    def __init__(self, world: World | None = None, session: Any = None, **_: Any) -> None:
        self.world = world or World()
        self.session = _FakeSession(self.world)
        self.session_path = session
        self.flood_sleep_threshold = 120
        self._connected = False
        self._handlers: list[tuple[Any, Any]] = []
        self.disconnected: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._requests = 0

    # -- connection --------------------------------------------------------

    async def connect(self) -> bool:
        if self.world.connect_error is not None:
            error, self.world.connect_error = self.world.connect_error, None
            raise error
        self.world.connects += 1
        self._connected = True
        if self.disconnected.done():
            self.disconnected = asyncio.get_event_loop().create_future()
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._resolve_disconnected()

    def _resolve_disconnected(self) -> None:
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    def drop(self) -> None:
        """Simulate the transport going away without a clean disconnect."""
        self._connected = False
        self._resolve_disconnected()

    def is_connected(self) -> bool:
        return self._connected

    async def is_user_authorized(self) -> bool:
        return self.world.authorized

    async def get_me(self) -> types.User:
        return self.world.me

    async def catch_up(self) -> None:
        self.world.catch_ups += 1

    # -- login -------------------------------------------------------------

    async def send_code_request(self, phone: str, force_sms: bool = False) -> Any:
        self.world.calls.append(("send_code_request", phone))
        failure = self.world._fail_next.pop("send_code_request", None)
        if failure is not None:
            raise failure
        return _SentCode(phone_code_hash=f"hash-{phone[-4:]}", timeout=60)

    async def sign_in(self, phone: str = "", code: str = "", **kwargs: Any) -> Any:
        self.world.calls.append(("sign_in", {"phone": phone, "code": code, **kwargs}))
        failure = self.world._fail_next.pop("sign_in", None)
        if failure is not None:
            raise failure
        self.world.authorized = True
        return self.world.me

    async def qr_login(self, ignored_ids: Any = None) -> Any:
        self.world.calls.append(("qr_login", {"ignored_ids": list(ignored_ids or [])}))
        return _QrLogin(self.world)

    async def log_out(self) -> bool:
        self.world.authorized = False
        await self.disconnect()
        return True

    async def _save_states_and_entities(self) -> None:
        self.world.saves += 1

    # -- events ------------------------------------------------------------

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self._handlers.append((callback, event))

    def remove_event_handler(self, callback: Any, event: Any = None) -> None:
        self._handlers = [(cb, ev) for cb, ev in self._handlers if cb is not callback]

    def on(self, event: Any) -> Any:  # pragma: no cover - decorator form
        def decorator(callback: Any) -> Any:
            self.add_event_handler(callback, event)
            return callback

        return decorator

    async def feed(self, event: Any) -> None:
        """Deliver *event* to every registered handler, in registration order."""
        for callback, _builder in list(self._handlers):
            result = callback(event)
            if hasattr(result, "__await__"):
                await result

    # -- requests ----------------------------------------------------------

    async def __call__(self, request: Any, ordered: bool = False) -> Any:
        name = type(request).__name__
        self.world.calls.append((name, request))
        self._requests += 1

        failure = self.world._fail_next.pop(name, None)
        if failure is not None:
            raise failure
        if self.world.latency_ms:
            await asyncio.sleep(self.world.latency_ms / 1000.0)
        if self.world.disconnect_after and self._requests >= self.world.disconnect_after:
            self.drop()
            raise ConnectionError("Cannot send requests while disconnected")

        handler = self.world.raw.get(name)
        if callable(handler):
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        if handler is not None:
            return handler
        default = getattr(self, f"_raw_{name}", None)
        if default is not None:
            result = default(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)

    # -- default raw handlers ---------------------------------------------
    #
    # A test may still override any of them through `world.raw[name]`. They
    # exist so that a message op is exercised against a world that *changes*:
    # asserting a send worked by reading the history back is a much stronger
    # test than asserting the request object looked right.

    def _updates(self, *messages: types.Message) -> types.Updates:
        return types.Updates(
            updates=[
                types.UpdateNewMessage(message=message, pts=index + 1, pts_count=1)
                for index, message in enumerate(messages)
            ],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _store(self, request: Any, text: str = "", media: Any = None) -> types.Message:
        chat_id = self._chat_id(request.peer)
        message = self.world.add_message(
            chat_id, text, out=True, sender_id=self.world.me.id, media=media
        )
        reply = getattr(request, "reply_to", None)
        if reply is not None:
            message.reply_to = types.MessageReplyHeader(
                reply_to_msg_id=getattr(reply, "reply_to_msg_id", None),
                reply_to_top_id=getattr(reply, "top_msg_id", None),
            )
        message.entities = list(getattr(request, "entities", None) or [])
        message.silent = bool(getattr(request, "silent", False))
        if getattr(request, "schedule_date", None) is not None:
            message._scheduled = True
        return message

    def _raw_SendMessageRequest(self, request: Any) -> types.Updates:
        return self._updates(self._store(request, request.message))

    def _raw_SendMediaRequest(self, request: Any) -> types.Updates:
        return self._updates(
            self._store(request, request.message, media=self.realise(request.media))
        )

    def _raw_SendMultiMediaRequest(self, request: Any) -> types.Updates:
        produced = [
            self._store(request, item.message, media=types.MessageMediaEmpty())
            for item in request.multi_media
        ]
        return self._updates(*produced)

    def _raw_SendScreenshotNotificationRequest(self, request: Any) -> types.Updates:
        return self._updates(self._store(request))

    def _raw_UploadMediaRequest(self, request: Any) -> Any:
        """Turn an uploaded input media into the real media it becomes.

        Returning the *input* would be a lie the album path immediately trips
        over: `sendMultiMedia` needs the `InputDocument`/`InputPhoto` the
        server minted, which only exists because this call happened.
        """
        media = request.media
        name = type(media).__name__
        if name == "InputMediaUploadedPhoto":
            self.world.next_document_id += 1
            photo = make_photo(self.world.next_document_id)
            self.world.file_bytes[photo.id] = b"uploaded"
            return types.MessageMediaPhoto(photo=photo)
        if name == "InputMediaUploadedDocument":
            self.world.next_document_id += 1
            document = make_document(
                self.world.next_document_id,
                mime=getattr(media, "mime_type", "application/octet-stream"),
                size=64,
                attributes=list(getattr(media, "attributes", None) or []),
            )
            self.world.add_document(document)
            return types.MessageMediaDocument(document=document)
        return media

    def _raw_SaveFilePartRequest(self, request: Any) -> bool:
        return True

    def _raw_SaveBigFilePartRequest(self, request: Any) -> bool:
        return True

    def _raw_EditMessageRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        message = self.world.find(chat_id, int(request.id))
        if message is None:
            from telethon.errors import MessageIdInvalidError

            raise MessageIdInvalidError(request)
        if request.message is not None:
            message.message = request.message
        if getattr(request, "media", None) is not None:
            message.media = self.realise(request.media, existing=message.media)
        message.entities = list(request.entities or [])
        message.edit_date = datetime.now(timezone.utc)
        return self._updates(message)

    def _raw_ForwardMessagesRequest(self, request: Any) -> types.Updates:
        source = self._chat_id(request.from_peer)
        target = self._chat_id(request.to_peer)
        produced = []
        for message_id in request.id:
            original = self.world.find(source, int(message_id))
            copy = self.world.add_message(target, original.message if original else "", out=True)
            copy.fwd_from = types.MessageFwdHeader(
                date=datetime.now(timezone.utc), from_id=types.PeerUser(user_id=self.world.me.id)
            )
            produced.append(copy)
        return self._updates(*produced)

    def _raw_DeleteMessagesRequest(self, request: Any) -> Any:
        return types.messages.AffectedMessages(pts=1, pts_count=len(request.id))

    def _raw_DeleteScheduledMessagesRequest(self, request: Any) -> types.Updates:
        return self._updates()

    def _raw_SendScheduledMessagesRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        produced = []
        for message_id in request.id:
            message = self.world.find(chat_id, int(message_id))
            if message is not None:
                message._scheduled = False
                produced.append(message)
        return self._updates(*produced)

    def _raw_UpdatePinnedMessageRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        pinned = self.world.pinned.setdefault(chat_id, set())
        if getattr(request, "unpin", False):
            pinned.discard(int(request.id))
        else:
            pinned.add(int(request.id))
        message = self.world.find(chat_id, int(request.id))
        if message is not None:
            message.pinned = not getattr(request, "unpin", False)
        return self._updates()

    def _raw_UnpinAllMessagesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        count = len(self.world.pinned.get(chat_id, set()))
        self.world.pinned[chat_id] = set()
        return types.messages.AffectedHistory(pts=1, pts_count=count, offset=0)

    def _affected_history(self, count: int = 0) -> Any:
        return types.messages.AffectedHistory(pts=1, pts_count=count, offset=0)

    def _raw_ReadMentionsRequest(self, request: Any) -> Any:
        return self._affected_history()

    def _raw_ReadReactionsRequest(self, request: Any) -> Any:
        return self._affected_history()

    def _raw_DeleteParticipantHistoryRequest(self, request: Any) -> Any:
        return self._affected_history()

    def _raw_ReadHistoryRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        self.world.read_inbox[chat_id] = int(request.max_id)
        return types.messages.AffectedMessages(pts=1, pts_count=0)

    def _raw_SaveDraftRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(request.peer)
        if not request.message:
            self.world.drafts.pop(chat_id, None)
            return True
        reply = getattr(request, "reply_to", None)
        self.world.drafts[chat_id] = types.DraftMessage(
            message=request.message,
            date=datetime.now(timezone.utc),
            no_webpage=bool(getattr(request, "no_webpage", False)),
            reply_to=reply,
            entities=list(request.entities or []),
        )
        return True

    def _raw_ClearAllDraftsRequest(self, request: Any) -> bool:
        self.world.drafts.clear()
        return True

    def _raw_GetPeerDialogsRequest(self, request: Any) -> Any:
        dialogs = []
        for item in request.peers:
            peer = getattr(item, "peer", item)
            chat_id = self._chat_id(peer)
            dialogs.append(self._dialog_row(self.world.dialog(chat_id)))
        return types.messages.PeerDialogs(
            dialogs=dialogs,
            messages=[],
            chats=[],
            users=[],
            state=types.updates.State(pts=1, qts=0, date=None, seq=0, unread_count=0),
        )

    # -- calls, video chats and conferences --------------------------------
    #
    # The call ops build raw requests and read structured answers, so the fake
    # answers with the real `phone.*` result types rather than with `Updates`.
    # A test then asserts on a *decoded* answer, which is the only way the
    # serialisers get exercised at all.

    def _raw_GetDhConfigRequest(self, request: Any) -> Any:
        return types.messages.DhConfig(
            g=DH_G, p=DH_PRIME, version=2, random=b"\x11" * (request.random_length or 0)
        )

    def _raw_GetCallConfigRequest(self, request: Any) -> Any:
        return types.DataJSON(data='{"audio_max_bitrate": 32000}')

    def _phone_call(self, call: Any) -> Any:
        return types.phone.PhoneCall(phone_call=call, users=list(self.world.users.values()))

    def _raw_RequestCallRequest(self, request: Any) -> Any:
        call = self.world.add_phone_call(video=bool(getattr(request, "video", False)))
        return self._phone_call(call)

    def _raw_AcceptCallRequest(self, request: Any) -> Any:
        call = self.world.phone_calls.get(request.peer.id) or self.world.add_phone_call(
            request.peer.id
        )
        return self._phone_call(call)

    def _raw_ConfirmCallRequest(self, request: Any) -> Any:
        call = types.PhoneCall(
            id=request.peer.id,
            access_hash=request.peer.access_hash,
            date=datetime.now(timezone.utc),
            admin_id=self.world.me.id,
            participant_id=4242,
            g_a_or_b=b"\x02" * 256,
            key_fingerprint=1,
            protocol=request.protocol,
            connections=[],
            start_date=datetime.now(timezone.utc),
            conference_supported=True,
        )
        self.world.phone_calls[request.peer.id] = call
        return self._phone_call(call)

    def _raw_ReceivedCallRequest(self, request: Any) -> bool:
        return True

    def _raw_DiscardCallRequest(self, request: Any) -> types.Updates:
        discarded = types.PhoneCallDiscarded(
            id=request.peer.id,
            need_rating=True,
            need_debug=False,
            reason=request.reason,
            duration=request.duration,
        )
        self.world.phone_calls[request.peer.id] = discarded
        return types.Updates(
            updates=[types.UpdatePhoneCall(phone_call=discarded)],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_SetCallRatingRequest(self, request: Any) -> types.Updates:
        return self._updates()

    def _raw_SaveCallDebugRequest(self, request: Any) -> bool:
        return True

    def _raw_SaveCallLogRequest(self, request: Any) -> bool:
        return True

    def _raw_SendSignalingDataRequest(self, request: Any) -> bool:
        return True

    def _raw_GetFullUserRequest(self, request: Any) -> Any:
        user = self._user_of(request.id)
        if user is None:
            from telethon.errors import RPCError

            raise RPCError(request, "USER_ID_INVALID", 400)
        uid = int(user.id)
        overrides = dict(self.world.user_full.get(uid, {}))
        note = self.world.contact_notes.get(uid)
        available = self.world.calls_available
        full = types.UserFull(
            id=uid,
            settings=self.world.peer_settings.get(uid) or types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=overrides.pop("common_chats_count", 0),
            about=overrides.pop("about", None),
            blocked=uid in self.world.blocked or None,
            blocked_my_stories_from=uid in self.world.blocked_stories or None,
            birthday=self.world.birthdays.get(uid),
            note=types.TextWithEntities(text=note, entities=[]) if note else None,
            phone_calls_available=available,
            video_calls_available=available,
            phone_calls_private=not available,
            **overrides,
        )
        return types.users.UserFull(
            full_user=full, chats=list(self.world.chats.values()), users=[user]
        )

    # -- group calls -------------------------------------------------------

    def _call_of(self, ref: Any) -> Any:
        """The stored `groupCall` an `InputGroupCall*` names."""
        name = type(ref).__name__
        if name == "InputGroupCallSlug":
            for call in self.world.group_calls.values():
                if (getattr(call, "invite_link", "") or "").endswith(ref.slug):
                    return call
            return next(iter(self.world.group_calls.values()), None)
        if name == "InputGroupCallInviteMessage":
            return next(iter(self.world.group_calls.values()), None)
        return self.world.group_calls.get(getattr(ref, "id", 0))

    def _group_updates(self, call: Any) -> types.Updates:
        return types.Updates(
            updates=[types.UpdateGroupCall(call=call)],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_CreateGroupCallRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        call = self.world.add_group_call(
            900200,
            chat_id=chat_id,
            title=request.title,
            schedule_date=request.schedule_date,
            rtmp_stream=getattr(request, "rtmp_stream", None),
        )
        return self._group_updates(call)

    def _raw_GetGroupCallRequest(self, request: Any) -> Any:
        call = self._call_of(request.call)
        if call is None:
            from telethon.errors.rpcerrorlist import GroupcallInvalidError

            raise GroupcallInvalidError(request)
        return types.phone.GroupCall(
            call=call,
            participants=self.world.participants.get(call.id, [])[:3],
            participants_next_offset="",
            chats=list(self.world.chats.values()),
            users=list(self.world.users.values()),
        )

    def _raw_GetGroupParticipantsRequest(self, request: Any) -> Any:
        call = self._call_of(request.call)
        everyone = self.world.participants.get(getattr(call, "id", 0), [])
        offset = int(request.offset or 0)
        window = everyone[offset : offset + request.limit]
        next_offset = str(offset + len(window)) if offset + len(window) < len(everyone) else ""
        return types.phone.GroupParticipants(
            count=len(everyone),
            participants=window,
            next_offset=next_offset,
            chats=list(self.world.chats.values()),
            users=list(self.world.users.values()),
            version=1,
        )

    def _raw_JoinGroupCallRequest(self, request: Any) -> types.Updates:
        return types.Updates(
            updates=[
                types.UpdateGroupCallConnection(params=types.DataJSON(data='{"stream": true}'))
            ],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_DiscardGroupCallRequest(self, request: Any) -> types.Updates:
        call = self._call_of(request.call)
        discarded = types.GroupCallDiscarded(
            id=getattr(call, "id", 0), access_hash=getattr(call, "access_hash", 0), duration=900
        )
        self.world.group_calls[discarded.id] = discarded
        return self._group_updates(discarded)

    def _raw_EditGroupCallTitleRequest(self, request: Any) -> types.Updates:
        call = self._call_of(request.call)
        if call is not None:
            call.title = request.title
        return self._group_updates(call) if call is not None else self._updates()

    def _raw_ToggleGroupCallSettingsRequest(self, request: Any) -> types.Updates:
        call = self._call_of(request.call)
        if call is not None:
            for name in ("join_muted", "messages_enabled", "send_paid_messages_stars"):
                value = getattr(request, name, None)
                if value is not None:
                    setattr(call, name, value)
        return self._group_updates(call) if call is not None else self._updates()

    def _raw_ToggleGroupCallRecordRequest(self, request: Any) -> types.Updates:
        call = self._call_of(request.call)
        if call is not None:
            call.record_start_date = datetime.now(timezone.utc) if request.start else None
            call.record_video_active = bool(request.video) if request.start else False
        return self._group_updates(call) if call is not None else self._updates()

    def _raw_ExportGroupCallInviteRequest(self, request: Any) -> Any:
        suffix = "speaker" if getattr(request, "can_self_unmute", False) else "listener"
        return types.phone.ExportedGroupCallInvite(link=f"https://t.me/c/5150?voicechat={suffix}")

    def _raw_GetGroupCallStreamRtmpUrlRequest(self, request: Any) -> Any:
        return types.phone.GroupCallStreamRtmpUrl(url=self.world.rtmp_url, key=self.world.rtmp_key)

    def _raw_GetGroupCallStreamChannelsRequest(self, request: Any) -> Any:
        return types.phone.GroupCallStreamChannels(
            channels=[types.GroupCallStreamChannel(channel=1, scale=0, last_timestamp_ms=5000)]
        )

    def _raw_GetGroupCallJoinAsRequest(self, request: Any) -> Any:
        return types.phone.JoinAsPeers(
            peers=[types.PeerUser(user_id=self.world.me.id)],
            chats=list(self.world.chats.values()),
            users=[self.world.me],
        )

    def _raw_CheckGroupCallRequest(self, request: Any) -> list[int]:
        return list(request.sources[:1])

    def _raw_GetGroupCallStarsRequest(self, request: Any) -> Any:
        return types.phone.GroupCallStars(
            total_stars=250, top_donors=[], chats=[], users=list(self.world.users.values())
        )

    def _raw_SendGroupCallMessageRequest(self, request: Any) -> types.Updates:
        message = types.MessageService(
            id=77,
            peer_id=types.PeerUser(user_id=self.world.me.id),
            date=datetime.now(timezone.utc),
        )
        return types.Updates(
            updates=[types.UpdateNewMessage(message=message, pts=1, pts_count=1)],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_GetGroupCallChainBlocksRequest(self, request: Any) -> types.Updates:
        blocks = self.world.chain_blocks
        if request.offset == -1:
            blocks = blocks[-request.limit :]
        return types.Updates(
            updates=[
                types.UpdateGroupCallChainBlocks(
                    call=request.call,
                    sub_chain_id=request.sub_chain_id,
                    blocks=list(blocks),
                    next_offset=len(self.world.chain_blocks),
                )
            ],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_CreateConferenceCallRequest(self, request: Any) -> types.Updates:
        call = self.world.add_group_call(
            900300,
            title=None,
            conference=True,
            creator=True,
            invite_link="https://t.me/call/AbCdEf",
        )
        return self._group_updates(call)

    def _raw_DeletePhoneCallHistoryRequest(self, request: Any) -> Any:
        removed = [row.id for row in self.world.call_log]
        self.world.call_log.clear()
        return types.messages.AffectedFoundMessages(
            pts=1, pts_count=len(removed), offset=0, messages=removed
        )

    def _raw_GetFileRequest(self, request: Any) -> Any:
        return types.upload.File(
            type=types.storage.FileUnknown(), mtime=0, bytes=self.world.stream_chunk
        )

    def _raw_SendReactionRequest(self, request: Any) -> types.Updates:
        # `messages.sendReaction` and `stories.sendReaction` share a class
        # name; the story one carries `story_id` and no `msg_id`.
        if hasattr(request, "story_id"):
            return self._story_reaction(request)
        chat_id = self._chat_id(request.peer)
        message = self.world.find(chat_id, int(request.msg_id))
        if message is not None:
            chosen = list(request.reaction or [])
            message.reactions = types.MessageReactions(
                results=[
                    types.ReactionCount(reaction=item, count=1, chosen_order=0) for item in chosen
                ],
                can_see_list=True,
            )
            return self._updates(message)
        return self._updates()

    # -- the dialog world --------------------------------------------------
    #
    # These are the calls the `chat` and `folder` groups make. Each one moves
    # the world: archiving a chat really changes its `folder_id`, so
    # `chat list --folder archive` finds it there afterwards.

    def _dialog_row(self, state: Any) -> types.Dialog:
        from telethon import utils

        entity = self.world.entity_for(state.chat_id)
        peer = utils.get_peer(entity) if entity is not None else types.PeerUser(state.chat_id)
        return types.Dialog(
            peer=peer,
            top_message=state.top_message,
            read_inbox_max_id=self.world.read_inbox.get(state.chat_id, 0),
            read_outbox_max_id=self.world.read_outbox.get(state.chat_id, 0),
            unread_count=state.unread_count,
            unread_mentions_count=state.unread_mentions_count,
            unread_reactions_count=state.unread_reactions_count,
            unread_poll_votes_count=state.unread_poll_votes_count,
            notify_settings=self.world.notify_of(state.chat_id),
            pinned=state.pinned or None,
            unread_mark=state.unread_mark or None,
            folder_id=state.folder_id or None,
            draft=self.world.drafts.get(state.chat_id),
            ttl_period=state.ttl_period,
        )

    def _dialog_payload(self, rows: list[Any]) -> Any:
        """`messages.dialogs` with the entities and top messages it must carry."""
        chats: list[Any] = []
        users: list[Any] = []
        messages: list[Any] = []
        for state in rows:
            entity = self.world.entity_for(state.chat_id)
            if isinstance(entity, types.User):
                users.append(entity)
            elif entity is not None:
                chats.append(entity)
            top = self.world.find(state.chat_id, state.top_message)
            if top is not None:
                messages.append(top)
        return types.messages.Dialogs(
            dialogs=[self._dialog_row(state) for state in rows],
            messages=messages,
            chats=chats,
            users=users,
        )

    def _ordered_dialogs(self) -> list[Any]:
        """Newest first, which is the order the server answers in."""
        rows = list(self.world.dialogs_by_id.values())
        return sorted(rows, key=lambda row: -row.top_message)

    def _raw_GetDialogsRequest(self, request: Any) -> Any:
        folder_id = getattr(request, "folder_id", None) or 0
        rows = [row for row in self._ordered_dialogs() if (row.folder_id or 0) == folder_id]
        if getattr(request, "exclude_pinned", None):
            rows = [row for row in rows if not row.pinned]
        offset_id = int(getattr(request, "offset_id", 0) or 0)
        if offset_id:
            rows = [row for row in rows if row.top_message < offset_id]
        return self._dialog_payload(rows[: int(getattr(request, "limit", 100) or 100)])

    def _raw_GetPinnedDialogsRequest(self, request: Any) -> Any:
        folder_id = int(getattr(request, "folder_id", 0) or 0)
        rows = [
            row
            for row in self._ordered_dialogs()
            if row.pinned and (row.folder_id or 0) == folder_id
        ]
        return types.messages.PeerDialogs(
            dialogs=[self._dialog_row(row) for row in rows],
            messages=[],
            chats=[],
            users=[],
            state=types.updates.State(pts=1, qts=0, date=None, seq=0, unread_count=0),
        )

    def _raw_ToggleDialogPinRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(getattr(request.peer, "peer", request.peer))
        self.world.dialog(chat_id).pinned = bool(getattr(request, "pinned", False))
        return True

    def _raw_ReorderPinnedDialogsRequest(self, request: Any) -> bool:
        wanted = [
            self._chat_id(getattr(item, "peer", item)) for item in getattr(request, "order", [])
        ]
        for chat_id, row in self.world.dialogs_by_id.items():
            row.pinned = chat_id in wanted
        return True

    def _raw_MarkDialogUnreadRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(getattr(request.peer, "peer", request.peer))
        self.world.dialog(chat_id).unread_mark = bool(getattr(request, "unread", False))
        return True

    def _raw_EditPeerFoldersRequest(self, request: Any) -> types.Updates:
        for item in getattr(request, "folder_peers", []):
            chat_id = self._chat_id(item.peer)
            self.world.dialog(chat_id).folder_id = int(item.folder_id)
        return self._updates()

    def _raw_UpdateNotifySettingsRequest(self, request: Any) -> bool:
        peer = getattr(request.peer, "peer", None)
        if peer is None:
            return True
        row = self.world.dialog(self._chat_id(peer))
        settings = request.settings
        if getattr(settings, "mute_until", None) is not None:
            row.mute_until = settings.mute_until
        if getattr(settings, "silent", None) is not None:
            row.silent = settings.silent
        return True

    def _raw_GetNotifySettingsRequest(self, request: Any) -> types.PeerNotifySettings:
        peer = getattr(request.peer, "peer", None)
        if peer is None:
            return types.PeerNotifySettings(silent=False)
        return self.world.notify_of(self._chat_id(peer))

    def _raw_SetHistoryTTLRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.dialog(chat_id).ttl_period = int(request.period) or None
        return self._updates()

    def _raw_GetPeerSettingsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        settings = self.world.peer_settings.get(chat_id) or types.PeerSettings()
        return types.messages.PeerSettings(settings=settings, chats=[], users=[])

    def _raw_HidePeerSettingsBarRequest(self, request: Any) -> bool:
        self.world.peer_settings[self._chat_id(request.peer)] = types.PeerSettings()
        return True

    def _raw_GetGlobalPrivacySettingsRequest(self, request: Any) -> Any:
        if self.world.global_privacy is None:
            self.world.global_privacy = types.GlobalPrivacySettings()
        return self.world.global_privacy

    def _raw_SetGlobalPrivacySettingsRequest(self, request: Any) -> Any:
        self.world.global_privacy = request.settings
        return request.settings

    def _raw_DeleteHistoryRequest(self, request: Any) -> Any:
        peer = getattr(request, "peer", None) or getattr(request, "channel", None)
        chat_id = self._chat_id(peer)
        removed = len(self.world.history(chat_id))
        max_id = int(getattr(request, "max_id", 0) or 0)
        if max_id:
            kept = [m for m in self.world.history(chat_id) if m.id > max_id]
            removed -= len(kept)
            self.world.messages[chat_id] = kept
        else:
            self.world.messages[chat_id] = []
        return types.messages.AffectedHistory(pts=1, pts_count=removed, offset=0)

    def _raw_DeleteChatUserRequest(self, request: Any) -> types.Updates:
        return self._updates()

    def _raw_GetUnreadMentionsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [m for m in self.world.history(chat_id) if getattr(m, "mentioned", False)]
        return types.messages.Messages(messages=found, chats=[], users=[], topics=[])

    def _raw_GetUnreadReactionsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [m for m in self.world.history(chat_id) if getattr(m, "reactions", None)]
        return types.messages.Messages(messages=found, chats=[], users=[], topics=[])

    def _raw_GetSavedDialogsRequest(self, request: Any) -> Any:
        rows = [
            types.SavedDialog(
                peer=types.PeerUser(user_id=abs(chat_id)), top_message=state.top_message
            )
            for chat_id, state in self.world.dialogs_by_id.items()
        ]
        return types.messages.SavedDialogs(dialogs=rows, messages=[], chats=[], users=[])

    def _raw_GetPinnedSavedDialogsRequest(self, request: Any) -> Any:
        return types.messages.SavedDialogs(dialogs=[], messages=[], chats=[], users=[])

    # -- folders -----------------------------------------------------------

    def _raw_GetDialogFiltersRequest(self, request: Any) -> Any:
        return types.messages.DialogFilters(
            filters=list(self.world.filters), tags_enabled=self.world.tags_enabled or None
        )

    def _raw_UpdateDialogFilterRequest(self, request: Any) -> bool:
        filter_id = int(request.id)
        self.world.filters = [
            f for f in self.world.filters if int(getattr(f, "id", -1)) != filter_id
        ]
        if request.filter is not None:
            self.world.filters.append(request.filter)
        return True

    def _raw_UpdateDialogFiltersOrderRequest(self, request: Any) -> bool:
        order = {int(value): index for index, value in enumerate(request.order)}
        self.world.filters.sort(key=lambda f: order.get(int(getattr(f, "id", 0) or 0), 99))
        return True

    def _raw_ToggleDialogFilterTagsRequest(self, request: Any) -> bool:
        self.world.tags_enabled = bool(request.enabled)
        return True

    def _raw_GetSuggestedDialogFiltersRequest(self, request: Any) -> list[Any]:
        return [
            types.DialogFilterSuggested(
                filter=types.DialogFilter(
                    id=0,
                    title=types.TextWithEntities(text="Unread", entities=[]),
                    pinned_peers=[],
                    include_peers=[],
                    exclude_peers=[],
                    exclude_read=True,
                ),
                description="Unread chats",
            )
        ]

    def _raw_GetExportedInvitesRequest(self, request: Any) -> Any:
        return types.chatlists.ExportedInvites(invites=[], chats=[], users=[])

    def _raw_ExportChatlistInviteRequest(self, request: Any) -> Any:
        return types.chatlists.ExportedChatlistInvite(
            filter=types.DialogFilterChatlist(
                id=request.chatlist.filter_id,
                title=types.TextWithEntities(text=request.title, entities=[]),
                pinned_peers=[],
                include_peers=list(request.peers),
            ),
            invite=types.ExportedChatlistInvite(
                title=request.title,
                url="https://t.me/addlist/AbCdEf",
                peers=[types.PeerUser(user_id=4242)],
            ),
        )

    def _raw_CheckChatlistInviteRequest(self, request: Any) -> Any:
        return types.chatlists.ChatlistInvite(
            title=types.TextWithEntities(text="Shared", entities=[]),
            peers=[types.PeerUser(user_id=4242)],
            chats=[],
            users=list(self.world.users.values()),
            emoticon="📁",
        )

    def _raw_JoinChatlistInviteRequest(self, request: Any) -> types.Updates:
        return self._updates()

    def _raw_GetLeaveChatlistSuggestionsRequest(self, request: Any) -> list[Any]:
        return [types.PeerUser(user_id=4242)]

    def _raw_LeaveChatlistRequest(self, request: Any) -> types.Updates:
        filter_id = int(request.chatlist.filter_id)
        self.world.filters = [
            f for f in self.world.filters if int(getattr(f, "id", -1)) != filter_id
        ]
        return self._updates()

    def _raw_GetChatlistUpdatesRequest(self, request: Any) -> Any:
        return types.chatlists.ChatlistUpdates(
            missing_peers=[types.PeerUser(user_id=4242)],
            chats=[],
            users=list(self.world.users.values()),
        )

    # -- odds and ends the chat group reads --------------------------------

    def _raw_GetChatThemesRequest(self, request: Any) -> Any:
        return types.account.ChatThemes(
            hash=0, themes=[types.ChatTheme(emoticon="🌷")], chats=[], users=[]
        )

    def _raw_GetCommonChatsRequest(self, request: Any) -> Any:
        return types.messages.Chats(chats=list(self.world.chats.values()))

    def _raw_GetInactiveChannelsRequest(self, request: Any) -> Any:
        return types.messages.InactiveChats(
            dates=[datetime.now(timezone.utc)] * len(self.world.chats),
            chats=list(self.world.chats.values()),
            users=[],
        )

    def _raw_ReportRequest(self, request: Any) -> Any:
        if not request.option:
            return types.ReportResultChooseOption(
                title="What is wrong?",
                options=[types.MessageReportOption(text="Spam", option=b"\x01")],
            )
        return types.ReportResultReported()

    # -- content: polls, checklists, locations, reactions -------------------
    #
    # Stage D. The rule is the same one the message world follows: a request
    # *changes the world*, so a test asserts against state that moved. A vote
    # really lands on the stored poll, ticking a task really flips the
    # completion, and `location live stop` really rewrites the media.

    def realise(self, media: Any, existing: Any = None) -> Any:
        """`InputMedia*` → the `MessageMedia*` the server would store.

        The interesting half is polls: the server, not the client, assigns
        each answer its opaque `option` bytes, and every later request
        addresses the answer by them. The fake assigns them the same way so a
        test exercises the real index → bytes resolution rather than a lie.
        """
        name = type(media).__name__
        if name == "InputMediaPoll":
            return self._realise_poll(media, existing)
        if name == "InputMediaTodo":
            completions = list(getattr(existing, "completions", None) or [])
            return types.MessageMediaToDo(todo=media.todo, completions=completions)
        if name == "InputMediaGeoPoint":
            return types.MessageMediaGeo(geo=self._geo(media.geo_point))
        if name == "InputMediaGeoLive":
            if getattr(media, "stopped", False):
                previous = getattr(existing, "geo", None) or self._geo(media.geo_point)
                stopped = types.MessageMediaGeoLive(geo=previous, period=0)
                stopped.stopped = True
                return stopped
            return types.MessageMediaGeoLive(
                geo=self._geo(media.geo_point),
                period=int(getattr(media, "period", 0) or 0),
                heading=getattr(media, "heading", None),
                proximity_notification_radius=getattr(media, "proximity_notification_radius", None),
            )
        if name == "InputMediaVenue":
            return types.MessageMediaVenue(
                geo=self._geo(media.geo_point),
                title=media.title,
                address=media.address,
                provider=media.provider,
                venue_id=media.venue_id,
                venue_type=media.venue_type,
            )
        return types.MessageMediaEmpty()

    @staticmethod
    def _geo(point: Any) -> Any:
        return types.GeoPoint(
            long=float(getattr(point, "long", 0.0) or 0.0),
            lat=float(getattr(point, "lat", 0.0) or 0.0),
            access_hash=12345,
            accuracy_radius=getattr(point, "accuracy_radius", None),
        )

    def _realise_poll(self, media: Any, existing: Any) -> Any:
        source = media.poll
        previous = getattr(existing, "poll", None)
        keep = {
            bytes(getattr(item, "option", b"")): item
            for item in (getattr(getattr(existing, "results", None), "results", None) or [])
        }
        answers: list[Any] = []
        for index, answer in enumerate(source.answers):
            option = bytes(getattr(answer, "option", None) or bytes([index]))
            answers.append(
                types.PollAnswer(
                    text=answer.text,
                    option=option,
                    media=getattr(answer, "media", None),
                    added_by=getattr(answer, "added_by", None),
                    date=getattr(answer, "date", None),
                )
            )
        correct = set(getattr(media, "correct_answers", None) or [])
        results = [
            keep.get(
                answer.option,
                types.PollAnswerVoters(
                    option=answer.option,
                    voters=0,
                    correct=index in correct or None,
                ),
            )
            for index, answer in enumerate(answers)
        ]
        poll_id = int(getattr(source, "id", 0) or 0)
        if not poll_id:
            self.world.next_poll_id += 1
            poll_id = self.world.next_poll_id
        poll = types.Poll(
            id=poll_id,
            question=source.question,
            answers=answers,
            hash=getattr(source, "hash", 0) or 0,
            closed=getattr(source, "closed", None),
            public_voters=getattr(source, "public_voters", None)
            or getattr(previous, "public_voters", None),
            multiple_choice=getattr(source, "multiple_choice", None)
            or getattr(previous, "multiple_choice", None),
            quiz=getattr(source, "quiz", None) or getattr(previous, "quiz", None),
            open_answers=getattr(source, "open_answers", None)
            or getattr(previous, "open_answers", None),
            revoting_disabled=getattr(source, "revoting_disabled", None),
            shuffle_answers=getattr(source, "shuffle_answers", None),
            hide_results_until_close=getattr(source, "hide_results_until_close", None),
            subscribers_only=getattr(source, "subscribers_only", None),
            close_period=getattr(source, "close_period", None),
            close_date=getattr(source, "close_date", None),
            countries_iso2=getattr(source, "countries_iso2", None),
        )
        return types.MessageMediaPoll(
            poll=poll,
            results=types.PollResults(
                results=results,
                total_voters=sum(int(r.voters or 0) for r in results),
                solution=getattr(media, "solution", None)
                or getattr(getattr(existing, "results", None), "solution", None),
                solution_entities=getattr(media, "solution_entities", None),
            ),
            attached_media=getattr(existing, "attached_media", None),
        )

    def _poll_of(self, request: Any) -> tuple[int, Any, Any]:
        chat_id = self._chat_id(request.peer)
        message = self.world.find(chat_id, int(getattr(request, "msg_id", 0) or request.id))
        media = getattr(message, "media", None)
        if getattr(media, "poll", None) is None:
            from telethon.errors import MessageIdInvalidError

            raise MessageIdInvalidError(request)
        return chat_id, message, media

    def _raw_GetPollResultsRequest(self, request: Any) -> types.Updates:
        _, message, _ = self._poll_of(request)
        return self._updates(message)

    def _raw_SendVoteRequest(self, request: Any) -> types.Updates:
        _, message, media = self._poll_of(request)
        chosen = {bytes(option) for option in request.options}
        votes = self.world.poll_votes.setdefault(int(media.poll.id), [])
        votes[:] = [row for row in votes if row[0] != self.world.me.id]
        for row in media.results.results:
            was = bool(row.chosen)
            row.chosen = row.option in chosen or None
            row.voters = max(0, int(row.voters or 0) + (1 if row.chosen else 0) - (1 if was else 0))
            if row.chosen:
                votes.append((self.world.me.id, row.option, datetime.now(timezone.utc)))
        media.results.total_voters = sum(int(r.voters or 0) for r in media.results.results)
        return self._updates(message)

    def _raw_AddPollAnswerRequest(self, request: Any) -> types.Updates:
        _, message, media = self._poll_of(request)
        option = bytes([len(media.poll.answers)])
        media.poll.answers.append(
            types.PollAnswer(
                text=request.answer.text,
                option=option,
                media=getattr(request.answer, "media", None),
                added_by=types.PeerUser(user_id=self.world.me.id),
                date=datetime.now(timezone.utc),
            )
        )
        media.results.results.append(types.PollAnswerVoters(option=option, voters=0))
        return self._updates(message)

    def _raw_DeletePollAnswerRequest(self, request: Any) -> types.Updates:
        _, message, media = self._poll_of(request)
        option = bytes(request.option)
        media.poll.answers = [a for a in media.poll.answers if a.option != option]
        media.results.results = [r for r in media.results.results if r.option != option]
        media.results.total_voters = sum(int(r.voters or 0) for r in media.results.results)
        return self._updates(message)

    def _raw_GetPollVotesRequest(self, request: Any) -> Any:
        _, _, media = self._poll_of(request)
        rows = list(self.world.poll_votes.get(int(media.poll.id), []))
        if request.option is not None:
            rows = [row for row in rows if row[1] == bytes(request.option)]
        return types.messages.VotesList(
            count=len(rows),
            votes=[
                types.MessagePeerVote(
                    peer=types.PeerUser(user_id=user_id), option=option, date=when
                )
                for user_id, option, when in rows[: int(request.limit)]
            ],
            chats=[],
            users=[],
            next_offset=None,
        )

    def _raw_GetUnreadPollVotesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [
            message
            for message in self.world.history(chat_id)
            if getattr(getattr(message, "media", None), "poll", None) is not None
        ]
        return types.messages.Messages(
            messages=found[: int(request.limit)], topics=[], chats=[], users=[]
        )

    def _raw_ReadPollVotesRequest(self, request: Any) -> Any:
        return self._affected_history()

    def _raw_GetPollStatsRequest(self, request: Any) -> Any:
        return types.stats.PollStats(
            votes_graph=types.StatsGraph(json=types.DataJSON(data='{"columns":[]}'))
        )

    # -- search ------------------------------------------------------------

    def _slice(self, messages: list[Any], *, next_rate: int | None = None) -> Any:
        """A `messagesSlice`, which is what every global search answers with."""
        return types.messages.MessagesSlice(
            count=len(messages),
            messages=messages,
            topics=[],
            chats=list(self.world.chats.values()),
            users=list(self.world.users.values()),
            next_rate=next_rate,
        )

    def _raw_SearchGlobalRequest(self, request: Any) -> Any:
        found: list[Any] = []
        for history in self.world.messages.values():
            for message in history:
                if request.q and request.q.lower() not in (message.message or "").lower():
                    continue
                if request.offset_id and message.id >= request.offset_id:
                    continue
                if not _passes_filter(message, getattr(request, "filter", None)):
                    continue
                found.append(message)
        found.sort(key=lambda m: m.id, reverse=True)
        return self._slice(found[: int(request.limit)], next_rate=42)

    def _raw_SearchSentMediaRequest(self, request: Any) -> Any:
        found = [
            message
            for history in self.world.messages.values()
            for message in history
            if getattr(message, "out", False)
            and _passes_filter(message, getattr(request, "filter", None))
        ]
        return self._slice(found[: int(request.limit)])

    def _raw_SearchRequest(self, request: Any) -> Any:
        # `messages.search` and `contacts.search` share a class name, and the
        # fake dispatches on that name alone. The peer is what tells them
        # apart: a contact search has none.
        if getattr(request, "peer", None) is None:
            return self._contacts_search(request)
        if type(getattr(request, "filter", None)).__name__ == "InputMessagesFilterPhoneCalls":
            return self._call_log_search(request)
        chat_id = self._chat_id(request.peer)
        found = [
            message
            for message in self.world.history(chat_id)
            if not request.q or request.q.lower() in (message.message or "").lower()
        ]
        return self._slice(sorted(found, key=lambda m: m.id, reverse=True)[: int(request.limit)])

    def _call_log_search(self, request: Any) -> Any:
        """The Calls tab: `messages.search` under `inputMessagesFilterPhoneCalls`."""
        rows = list(self.world.call_log)
        if getattr(request.filter, "missed", False):
            rows = [
                row
                for row in rows
                if type(getattr(row.action, "reason", None)).__name__
                == "PhoneCallDiscardReasonMissed"
            ]
        if request.offset_id:
            rows = [row for row in rows if row.id < request.offset_id]
        rows = sorted(rows, key=lambda row: row.id, reverse=True)[: request.limit]
        return types.messages.Messages(
            messages=rows, topics=[], chats=[], users=list(self.world.users.values())
        )

    def _raw_SearchPostsRequest(self, request: Any) -> Any:
        return self._slice(list(self.world.public_posts)[: int(request.limit)], next_rate=7)

    def _raw_CheckSearchPostsFloodRequest(self, request: Any) -> Any:
        return types.SearchPostsFlood(
            total_daily=10,
            remains=self.world.search_flood_remains,
            query_is_free=self.world.search_flood_remains > 0 or None,
            stars_amount=self.world.search_flood_stars,
        )

    # -- reaction policy, tags and paid reactions --------------------------

    def _running_call(self, chat_id: int) -> Any:
        """The `inputGroupCall` a chat's full info advertises, if one is running."""
        call_id = self.world.chat_calls.get(chat_id)
        call = self.world.group_calls.get(call_id) if call_id else None
        if call is None:
            return None
        return types.InputGroupCall(id=call.id, access_hash=call.access_hash)

    def _raw_SetChatAvailableReactionsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.chat_reactions[chat_id] = request.available_reactions
        if request.reactions_limit is not None:
            self.world.reactions_limit[chat_id] = int(request.reactions_limit)
        if request.paid_enabled is not None:
            self.world.paid_enabled[chat_id] = bool(request.paid_enabled)
        # The admin world reads the policy back out of `chatFull`, so it is
        # written to both places: `chat reaction get` reads the first, `chat
        # setting get` the second, and they must not be able to disagree.
        settings = self.world.settings_of(chat_id)
        settings["available_reactions"] = request.available_reactions
        settings["reactions_limit"] = request.reactions_limit
        return self._updates()

    def _raw_SetDefaultReactionRequest(self, request: Any) -> bool:
        self.world.default_reaction = request.reaction
        return True

    def _raw_GetSavedReactionTagsRequest(self, request: Any) -> Any:
        return types.messages.SavedReactionTags(
            tags=[
                types.SavedReactionTag(
                    reaction=types.ReactionEmoji(emoticon=emoji), count=1, title=title
                )
                for emoji, title in self.world.saved_tags.items()
            ],
            hash=0,
        )

    def _raw_GetDefaultTagReactionsRequest(self, request: Any) -> Any:
        return types.messages.Reactions(hash=0, reactions=[types.ReactionEmoji(emoticon="📌")])

    def _raw_UpdateSavedReactionTagRequest(self, request: Any) -> bool:
        emoji = getattr(request.reaction, "emoticon", None) or str(
            getattr(request.reaction, "document_id", "")
        )
        self.world.saved_tags[emoji] = request.title
        return True

    def _raw_GetPaidReactionPrivacyRequest(self, request: Any) -> types.Updates:
        mapping = {
            "anonymous": types.PaidReactionPrivacyAnonymous(),
            "default": types.PaidReactionPrivacyDefault(),
        }
        return types.Updates(
            updates=[
                types.UpdatePaidReactionPrivacy(
                    private=mapping.get(self.world.paid_privacy, types.PaidReactionPrivacyDefault())
                )
            ],
            users=[],
            chats=[],
            date=None,
            seq=0,
        )

    def _raw_TogglePaidReactionPrivacyRequest(self, request: Any) -> bool:
        name = type(request.private).__name__
        self.world.paid_privacy = (
            "anonymous"
            if name.endswith("Anonymous")
            else ("peer" if name.endswith("Peer") else "default")
        )
        return True

    def _raw_SendPaidReactionRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.star_balance -= int(request.count)
        return types.Updates(
            updates=[
                types.UpdateMessageReactions(
                    peer=types.PeerChannel(channel_id=abs(chat_id)),
                    msg_id=int(request.msg_id),
                    reactions=types.MessageReactions(
                        results=[
                            types.ReactionCount(
                                reaction=types.ReactionPaid(), count=int(request.count)
                            )
                        ],
                        top_reactors=[
                            types.MessageReactor(
                                count=int(request.count),
                                my=True,
                                peer_id=types.PeerUser(user_id=self.world.me.id),
                            )
                        ],
                    ),
                )
            ],
            users=[],
            chats=[],
            date=None,
            seq=0,
        )

    def _raw_GetSendAsRequest(self, request: Any) -> Any:
        return types.channels.SendAsPeers(
            peers=[types.SendAsPeer(peer=types.PeerUser(user_id=self.world.me.id))],
            chats=[],
            users=[self.world.me],
        )

    def _raw_GetMessagesReactionsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        updates = []
        for message_id in request.id:
            message = self.world.find(chat_id, int(message_id))
            reactions = getattr(message, "reactions", None) if message is not None else None
            if reactions is None:
                reactions = types.MessageReactions(results=[], can_see_list=True)
            updates.append(
                types.UpdateMessageReactions(
                    peer=types.PeerUser(user_id=abs(chat_id)),
                    msg_id=int(message_id),
                    reactions=reactions,
                )
            )
        return types.Updates(updates=updates, users=[], chats=[], date=None, seq=0)

    # -- checklists --------------------------------------------------------

    def _todo_of(self, request: Any) -> tuple[int, Any, Any]:
        chat_id = self._chat_id(request.peer)
        message = self.world.find(chat_id, int(request.msg_id))
        media = getattr(message, "media", None)
        if getattr(media, "todo", None) is None:
            from telethon.errors import MessageIdInvalidError

            raise MessageIdInvalidError(request)
        return chat_id, message, media

    def _raw_ToggleTodoCompletedRequest(self, request: Any) -> types.Updates:
        _, message, media = self._todo_of(request)
        completions = [
            item for item in (media.completions or []) if item.id not in set(request.incompleted)
        ]
        done = {item.id for item in completions}
        for task_id in request.completed:
            if task_id not in done:
                completions.append(
                    types.TodoCompletion(
                        id=int(task_id),
                        completed_by=types.PeerUser(user_id=self.world.me.id),
                        date=datetime.now(timezone.utc),
                    )
                )
        media.completions = completions
        return self._updates(message)

    def _raw_AppendTodoListRequest(self, request: Any) -> types.Updates:
        _, message, media = self._todo_of(request)
        media.todo.list.extend(request.list)
        return self._updates(message)

    # -- locations ---------------------------------------------------------

    def _raw_GetRecentLocationsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [
            message
            for message in self.world.history(chat_id)
            if type(getattr(message, "media", None)).__name__ == "MessageMediaGeoLive"
        ]
        return types.messages.Messages(messages=found, topics=[], chats=[], users=[])

    def _raw_GetLocatedRequest(self, request: Any) -> types.Updates:
        peers = list(self.world.nearby)
        if request.self_expires is not None:
            self.world.self_expires = int(request.self_expires)
            peers = [
                *peers,
                types.PeerSelfLocated(expires=datetime.now(timezone.utc)),
            ]
        return types.Updates(
            updates=[types.UpdatePeerLocated(peers=peers)],
            users=list(self.world.users.values()),
            chats=list(self.world.chats.values()),
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_GetWebFileRequest(self, request: Any) -> Any:
        return types.upload.WebFile(
            size=len(self.world.web_file),
            mime_type="image/png",
            file_type=types.storage.FileJpeg(),
            mtime=0,
            bytes=self.world.web_file,
        )

    def _raw_GetConfigRequest(self, request: Any) -> Any:
        config = types.Config(
            date=datetime(2026, 9, 3, 9, 0, 0, tzinfo=timezone.utc),
            expires=datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc),
            test_mode=False,
            this_dc=4,
            dc_options=[
                types.DcOption(id=4, ip_address="149.154.167.91", port=443),
                types.DcOption(id=4, ip_address="2001:67c::b0e", port=443, ipv6=True),
                types.DcOption(id=2, ip_address="149.154.167.51", port=443, media_only=True),
            ],
            dc_txt_domain_name="apv3.stel.com",
            chat_size_max=200,
            megagroup_size_max=200000,
            forwarded_count_max=100,
            online_update_period_ms=120000,
            offline_blur_timeout_ms=5000,
            offline_idle_timeout_ms=30000,
            online_cloud_timeout_ms=300000,
            notify_cloud_delay_ms=30000,
            notify_default_delay_ms=1500,
            push_chat_period_ms=60000,
            push_chat_limit=2,
            edit_time_limit=172800,
            revoke_time_limit=172800,
            revoke_pm_time_limit=172800,
            rating_e_decay=2419200,
            stickers_recent_limit=200,
            channels_read_media_period=604800,
            call_receive_timeout_ms=20000,
            call_ring_timeout_ms=90000,
            call_connect_timeout_ms=30000,
            call_packet_timeout_ms=10000,
            me_url_prefix="https://t.me/",
            caption_length_max=1024,
            message_length_max=4096,
            webfile_dc_id=self.world.webfile_dc_id,
            gif_search_username=self.world.gif_search_username,
            venue_search_username=self.world.venue_search_username,
            reactions_default=self.world.default_reaction,
        )
        return config

    def _raw_GetInlineBotResultsRequest(self, request: Any) -> Any:
        """Two callers share this endpoint, told apart by the geo point.

        `location venue` attaches one and wants the venue list; `gif search`
        attaches none and wants whatever the GIF bot has.
        """
        if getattr(request, "geo_point", None) is not None:
            return types.messages.BotResults(
                query_id=1,
                results=list(self.world.venues),
                cache_time=0,
                users=[],
                next_offset=None,
            )
        return types.messages.BotResults(
            query_id=987654321,
            results=list(self.world.inline_results),
            cache_time=300,
            users=[],
            next_offset=None,
        )

    async def _borrow_exported_sender(self, dc_id: int) -> Any:
        """`stats.*` and `upload.getWebFile` live on another DC.

        The fake hands back itself with the same `send` surface, so an
        implementation that forgets to migrate is still distinguishable from
        one that does: `world.calls` records the request either way, and the
        borrow/return pair is recorded too.
        """
        self.world.calls.append(("borrow_exported_sender", {"dc_id": dc_id}))
        return _ExportedSender(self)

    async def _return_exported_sender(self, sender: Any) -> None:
        self.world.calls.append(("return_exported_sender", {}))

    # -- the auth world (PR-2) --------------------------------------------
    #
    # These model the *protocol*, not Telegram's cryptography: the fake
    # cannot verify an SRP proof, so it accepts any `inputCheckPasswordSRP`
    # carrying the current `srp_id` and rejects `inputCheckPasswordEmpty`
    # when a password is set. That is the branch the operations actually
    # have to get right; "was this the correct password" is tested by making
    # the request fail (`world.fail_next(..., PasswordHashInvalidError)`),
    # which is the only honest way to model a check we do not perform.

    @property
    def auth(self) -> AuthWorld:
        return self.world.auth

    def _check_password(self, given: Any) -> None:
        from telethon.errors import RPCError

        if not self.auth.password:
            return
        if type(given).__name__ == "InputCheckPasswordEmpty":
            raise RPCError("request", "PASSWORD_HASH_INVALID", 400)
        if self.auth.srp_id_invalid_once:
            self.auth.srp_id_invalid_once = False
            self.auth.srp_id += 1
            raise RPCError("request", "SRP_ID_INVALID", 400)
        if int(getattr(given, "srp_id", 0)) != self.auth.srp_id:
            raise RPCError("request", "SRP_ID_INVALID", 400)

    # sessions -------------------------------------------------------------

    def _raw_GetAuthorizationsRequest(self, request: Any) -> Any:
        return types.account.Authorizations(
            authorization_ttl_days=self.auth.authorization_ttl_days,
            authorizations=list(self.auth.authorizations),
        )

    def _raw_ResetAuthorizationRequest(self, request: Any) -> bool:
        before = len(self.auth.authorizations)
        self.auth.authorizations = [
            a for a in self.auth.authorizations if int(a.hash) != int(request.hash)
        ]
        return before != len(self.auth.authorizations)

    def _raw_ResetAuthorizationsRequest(self, request: Any) -> bool:
        self.auth.authorizations = [
            a for a in self.auth.authorizations if getattr(a, "current", False)
        ]
        return True

    def _raw_ChangeAuthorizationSettingsRequest(self, request: Any) -> bool:
        for auth in self.auth.authorizations:
            if int(auth.hash) != int(request.hash):
                continue
            if request.confirmed:
                auth.unconfirmed = None
            if request.call_requests_disabled is not None:
                auth.call_requests_disabled = request.call_requests_disabled
            if request.encrypted_requests_disabled is not None:
                auth.encrypted_requests_disabled = request.encrypted_requests_disabled
        return True

    def _raw_SetAuthorizationTTLRequest(self, request: Any) -> bool:
        self.auth.authorization_ttl_days = int(request.authorization_ttl_days)
        return True

    def _raw_GetConnectedBotsRequest(self, request: Any) -> Any:
        return types.account.ConnectedBots(connected_bots=[], users=[])

    def _raw_AcceptLoginTokenRequest(self, request: Any) -> Any:
        created = make_authorization(90210, device="Web", app="Telegram Web")
        self.auth.authorizations.append(created)
        return created

    def _raw_ExportLoginTokenRequest(self, request: Any) -> Any:
        return types.auth.LoginToken(expires=None, token=b"fake-token")

    # websites -------------------------------------------------------------

    def _raw_GetWebAuthorizationsRequest(self, request: Any) -> Any:
        return types.account.WebAuthorizations(
            authorizations=list(self.auth.web_authorizations),
            users=[make_user(4242, username="examplebot")],
        )

    def _raw_ResetWebAuthorizationRequest(self, request: Any) -> bool:
        self.auth.web_authorizations = [
            a for a in self.auth.web_authorizations if int(a.hash) != int(request.hash)
        ]
        return True

    def _raw_ResetWebAuthorizationsRequest(self, request: Any) -> bool:
        self.auth.web_authorizations = []
        return True

    def _raw_BlockRequest(self, request: Any) -> bool:
        marked = self._chat_id(request.id)
        self._blocked_target(request)[abs(marked)] = datetime.now(timezone.utc)
        return True

    # passkeys -------------------------------------------------------------

    def _raw_GetPasskeysRequest(self, request: Any) -> Any:
        return types.account.Passkeys(passkeys=list(self.auth.passkeys))

    def _raw_DeletePasskeyRequest(self, request: Any) -> bool:
        self.auth.passkeys = [p for p in self.auth.passkeys if p.id != request.id]
        return True

    # the cloud password ---------------------------------------------------

    def _raw_GetPasswordRequest(self, request: Any) -> Any:
        return self.auth.password_state()

    def _raw_GetPasswordSettingsRequest(self, request: Any) -> Any:
        self._check_password(request.password)
        return types.account.PasswordSettings(email=self.auth.recovery_email or None)

    def _raw_UpdatePasswordSettingsRequest(self, request: Any) -> bool:
        from telethon.errors import RPCError

        self._check_password(request.password)
        settings = request.new_settings
        if settings.email and not self.auth.recovery_email:
            self.auth.recovery_email = settings.email
            raise RPCError("request", "EMAIL_UNCONFIRMED_6", 400)
        if settings.new_password_hash == b"" and settings.new_algo is None:
            self.auth.password = ""
            self.auth.hint = ""
            self.auth.has_secure_values = False
        elif settings.new_password_hash:
            self.auth.password = "set"
        if settings.hint is not None:
            self.auth.hint = settings.hint
        return True

    def _raw_ConfirmPasswordEmailRequest(self, request: Any) -> bool:
        return True

    def _raw_ResendPasswordEmailRequest(self, request: Any) -> bool:
        return True

    def _raw_CancelPasswordEmailRequest(self, request: Any) -> bool:
        self.auth.recovery_email = ""
        return True

    def _raw_GetTmpPasswordRequest(self, request: Any) -> Any:
        self._check_password(request.password)
        return types.account.TmpPassword(
            tmp_password=self.auth.tmp_password,
            valid_until=datetime.now(timezone.utc),
        )

    def _raw_ResetPasswordRequest(self, request: Any) -> Any:
        return types.account.ResetPasswordRequestedWait(until_date=datetime.now(timezone.utc))

    def _raw_DeclinePasswordResetRequest(self, request: Any) -> bool:
        self.auth.pending_reset_date = None
        return True

    def _raw_CheckPasswordRequest(self, request: Any) -> Any:
        self._check_password(request.password)
        self.world.authorized = True
        return types.auth.Authorization(user=self.world.me)

    def _raw_RequestPasswordRecoveryRequest(self, request: Any) -> Any:
        return types.auth.PasswordRecovery(email_pattern="a**@e*****e.com")

    def _raw_CheckRecoveryPasswordRequest(self, request: Any) -> bool:
        return True

    def _raw_RecoverPasswordRequest(self, request: Any) -> Any:
        self.auth.password = ""
        return types.auth.Authorization(user=self.world.me)

    # account-level switches -----------------------------------------------

    def _raw_GetAccountTTLRequest(self, request: Any) -> Any:
        return types.AccountDaysTTL(days=self.auth.account_ttl_days)

    def _raw_SetAccountTTLRequest(self, request: Any) -> bool:
        self.auth.account_ttl_days = int(request.ttl.days)
        return True

    def _raw_UpdateDeviceLockedRequest(self, request: Any) -> bool:
        self.auth.device_locked_for = int(request.period)
        return True

    def _raw_DeleteAccountRequest(self, request: Any) -> bool:
        self.auth.logged_out = True
        return True

    def _raw_InvalidateSignInCodesRequest(self, request: Any) -> bool:
        return True

    # phone / email --------------------------------------------------------

    def _sent_code(self, kind: Any = None) -> Any:
        return types.auth.SentCode(
            type=kind or self.auth.sent_code_type or types.auth.SentCodeTypeApp(length=5),
            phone_code_hash="hash-abcd",
            next_type=types.auth.CodeTypeSms(),
            timeout=60,
        )

    def _raw_SendCodeRequest(self, request: Any) -> Any:
        self.world.calls.append(("send_code_request", request.phone_number))
        return self._sent_code()

    def _raw_ResendCodeRequest(self, request: Any) -> Any:
        return self._sent_code(types.auth.SentCodeTypeSms(length=5))

    def _raw_CancelCodeRequest(self, request: Any) -> bool:
        return True

    def _raw_ReportMissingCodeRequest(self, request: Any) -> bool:
        return True

    def _raw_SignInRequest(self, request: Any) -> Any:
        if self.auth.signup_required:
            return types.auth.AuthorizationSignUpRequired(terms_of_service=self.auth.terms)
        self.world.authorized = True
        return types.auth.Authorization(
            user=self.world.me, future_auth_token=self.auth.future_auth_token
        )

    def _raw_SignUpRequest(self, request: Any) -> Any:
        self.world.authorized = True
        return types.auth.Authorization(user=self.world.me)

    def _raw_ImportBotAuthorizationRequest(self, request: Any) -> Any:
        self.world.authorized = True
        return types.auth.Authorization(user=self.world.me)

    def _raw_LogOutRequest(self, request: Any) -> Any:
        self.world.authorized = False
        self.auth.logged_out = True
        return types.auth.LoggedOut(future_auth_token=self.auth.future_auth_token)

    def _raw_SendChangePhoneCodeRequest(self, request: Any) -> Any:
        return self._sent_code()

    def _raw_ChangePhoneRequest(self, request: Any) -> Any:
        return self.world.me

    def _raw_SendConfirmPhoneCodeRequest(self, request: Any) -> Any:
        return self._sent_code()

    def _raw_ConfirmPhoneRequest(self, request: Any) -> bool:
        return True

    def _raw_SendVerifyPhoneCodeRequest(self, request: Any) -> Any:
        return self._sent_code()

    def _raw_VerifyPhoneRequest(self, request: Any) -> bool:
        return True

    def _raw_SendVerifyEmailCodeRequest(self, request: Any) -> Any:
        return types.account.SentEmailCode(email_pattern="a**@e*****e.com", length=6)

    def _raw_VerifyEmailRequest(self, request: Any) -> Any:
        return types.account.EmailVerified(email="ada@example.com")

    def _raw_ResetLoginEmailRequest(self, request: Any) -> Any:
        return self._sent_code(
            types.auth.SentCodeTypeEmailCode(email_pattern="a**@e*****e.com", length=6)
        )

    # help.* ---------------------------------------------------------------

    def _raw_GetAppConfigRequest(self, request: Any) -> Any:
        """`help.getAppConfig`, for the auth, chat and media worlds alike.

        One endpoint carries the suggestion list, the pin and channel limits
        and the sticker/GIF limits, so the defaults for all three are seeded
        here and a test overrides either through `world.app_config` or
        `world.auth.app_config`.
        """
        config: dict[str, Any] = {
            "upload_max_fileparts_default": 4000,
            "caption_length_limit_default": 1024,
            "stickers_faved_limit_default": self.world.faved_limit,
            "saved_gifs_limit_default": self.world.saved_gif_limit,
            **self.world.app_config,
            **self.auth.app_config,
        }
        config.setdefault("pending_suggestions", list(self.auth.pending_suggestions))
        config.setdefault("dismissed_suggestions", list(self.auth.dismissed_suggestions))
        config.setdefault("dialogs_pinned_limit_default", 5)
        config.setdefault("channels_limit_default", 500)
        config.setdefault("reactions_user_max_default", 1)
        config.setdefault("freeze_appeal_url", "https://t.me/spambot")
        config.setdefault("stories_pinned_to_top_count_max", 3)
        return types.help.AppConfig(hash=1, config=_json_value(config))

    def _raw_GetPromoDataRequest(self, request: Any) -> Any:
        pending = list(self.auth.pending_suggestions) or ["VALIDATE_PHONE_NUMBER"]
        if self.auth.promo_peer is None:
            return types.help.PromoData(
                expires=None,
                pending_suggestions=pending,
                dismissed_suggestions=list(self.auth.dismissed_suggestions),
                chats=[],
                users=[],
            )
        return types.help.PromoData(
            expires=datetime.now(timezone.utc),
            pending_suggestions=pending,
            dismissed_suggestions=list(self.auth.dismissed_suggestions),
            chats=[],
            users=[],
            peer=self.auth.promo_peer,
            psa_type="covid",
        )

    def _raw_HidePromoDataRequest(self, request: Any) -> bool:
        self.auth.promo_peer = None
        return True

    def _raw_DismissSuggestionRequest(self, request: Any) -> bool:
        # An account-level suggestion carries `inputPeerEmpty`; a chat's own
        # pending suggestion carries the chat. Two different lists.
        peer = getattr(request, "peer", None)
        if peer is None or type(peer).__name__ == "InputPeerEmpty":
            self.auth.dismissed_suggestions.append(request.suggestion)
            return True
        chat_id = self._chat_id(peer)
        settings = self.world.settings_of(chat_id)
        settings["pending_suggestions"] = [
            key for key in (settings.get("pending_suggestions") or []) if key != request.suggestion
        ]
        return True

    def _raw_GetTermsOfServiceUpdateRequest(self, request: Any) -> Any:
        if self.auth.terms is None:
            return types.help.TermsOfServiceUpdateEmpty(expires=datetime.now(timezone.utc))
        return types.help.TermsOfServiceUpdate(
            expires=datetime.now(timezone.utc), terms_of_service=self.auth.terms
        )

    def _raw_AcceptTermsOfServiceRequest(self, request: Any) -> bool:
        self.auth.terms = None
        return True

    def _raw_GetSupportRequest(self, request: Any) -> Any:
        return types.help.Support(phone_number="+42777", user=make_user(333000, first="Support"))

    def _raw_GetSupportNameRequest(self, request: Any) -> Any:
        return types.help.SupportName(name="Telegram Support")

    def _raw_GetInviteTextRequest(self, request: Any) -> Any:
        return types.help.InviteText(message="Join me on Telegram!")

    def _raw_GetUserInfoRequest(self, request: Any) -> Any:
        return types.help.UserInfo(
            message="a note", entities=[], author="agent", date=datetime.now(timezone.utc)
        )

    def _raw_EditUserInfoRequest(self, request: Any) -> Any:
        return types.help.UserInfo(
            message=request.message, entities=[], author="agent", date=datetime.now(timezone.utc)
        )

    def _raw_GetPassportConfigRequest(self, request: Any) -> Any:
        return types.help.PassportConfig(
            hash=1, countries_langs=types.DataJSON(data='{"DE": "de", "RU": "ru"}')
        )

    # smsjobs --------------------------------------------------------------

    def _raw_GetStatusRequest(self, request: Any) -> Any:
        return types.smsjobs.Status(
            recent_sent=0,
            recent_since=None,
            recent_remains=100,
            total_sent=0,
            total_since=None,
            terms_url="https://telegram.org/tos/sms",
            allow_international=None,
        )

    def _raw_IsEligibleToJoinRequest(self, request: Any) -> Any:
        return types.smsjobs.EligibleToJoin(
            terms_url="https://telegram.org/tos/sms", monthly_sent_sms=0
        )

    def _raw_JoinRequest(self, request: Any) -> bool:
        return True

    def _raw_LeaveRequest(self, request: Any) -> bool:
        return True

    def _raw_UpdateSettingsRequest(self, request: Any) -> bool:
        return True

    # passport -------------------------------------------------------------

    def _raw_GetAllSecureValuesRequest(self, request: Any) -> Any:
        return list(self.auth.secure_values)

    def _raw_GetSecureValueRequest(self, request: Any) -> Any:
        wanted = {type(t).__name__ for t in request.types}
        return [v for v in self.auth.secure_values if type(v.type).__name__ in wanted]

    def _raw_DeleteSecureValueRequest(self, request: Any) -> bool:
        wanted = {type(t).__name__ for t in request.types}
        self.auth.secure_values = [
            v for v in self.auth.secure_values if type(v.type).__name__ not in wanted
        ]
        return True

    def _raw_GetAuthorizationFormRequest(self, request: Any) -> Any:
        return types.account.AuthorizationForm(
            required_types=[
                types.SecureRequiredType(type=types.SecureValueTypePassport(), selfie_required=True)
            ],
            values=list(self.auth.secure_values),
            errors=[],
            users=[],
            privacy_policy_url="https://example.com/privacy",
        )

    # updates --------------------------------------------------------------

    def _raw_GetStateRequest(self, request: Any) -> Any:
        from datetime import datetime, timezone

        pts, qts, seq = self.world.update_state.get(0, (0, 0, 0))
        return types.updates.State(
            pts=pts + self.world.server_ahead,
            qts=qts,
            date=datetime(2026, 9, 3, 9, 14, 7, tzinfo=timezone.utc),
            seq=seq,
            unread_count=0,
        )

    # -- the sync, network and takeout world -------------------------------

    def _raw_GetNearestDcRequest(self, request: Any) -> Any:
        return types.NearestDc(country="GB", this_dc=4, nearest_dc=4)

    def _raw_GetDifferenceRequest(self, request: Any) -> Any:
        from datetime import datetime, timezone

        self.world.differences += 1
        return types.updates.DifferenceEmpty(
            date=datetime(2026, 9, 3, 9, 14, 7, tzinfo=timezone.utc), seq=4410
        )

    def _raw_GetChannelDifferenceRequest(self, request: Any) -> Any:
        self.world.differences += 1
        return types.updates.ChannelDifferenceEmpty(pts=42, final=True, timeout=30)

    def _raw_GetCountriesListRequest(self, request: Any) -> Any:
        return types.help.CountriesList(
            countries=[
                types.help.Country(
                    iso2="GB",
                    default_name="United Kingdom",
                    name="United Kingdom",
                    country_codes=[
                        types.help.CountryCode(
                            country_code="44", prefixes=["7"], patterns=["XXXX XXXXXX"]
                        )
                    ],
                ),
                types.help.Country(
                    iso2="ES",
                    default_name="Spain",
                    country_codes=[types.help.CountryCode(country_code="34")],
                ),
                types.help.Country(
                    iso2="US",
                    default_name="United States",
                    country_codes=[
                        types.help.CountryCode(country_code="1", patterns=["XXX XXX XXXX"])
                    ],
                ),
            ],
            hash=0,
        )

    def _raw_GetTimezonesListRequest(self, request: Any) -> Any:
        return types.help.TimezonesList(
            timezones=[types.Timezone(id="Europe/London", name="London", utc_offset=0)],
            hash=0,
        )

    def _raw_InitTakeoutSessionRequest(self, request: Any) -> Any:
        return types.account.Takeout(id=1234567890)

    def _raw_FinishTakeoutSessionRequest(self, request: Any) -> bool:
        return True

    def _raw_GetSplitRangesRequest(self, request: Any) -> Any:
        return [types.MessageRange(min_id=1, max_id=1000)]

    async def _raw_InvokeWithTakeoutRequest(self, request: Any) -> Any:
        """Unwrap and run the inner request, recording that it was wrapped.

        Recording the wrapping is the point: a takeout that forgets
        `invokeWithTakeout` on one call gets a *smaller* export rather than an
        error, so the test has to be able to assert it was there.
        """
        self.world.takeout_calls.append(type(request.query).__name__)
        return await self(request.query)

    # -- the contact world -------------------------------------------------
    #
    # Every one of these moves the world: `contacts.addContact` really flips
    # `user.contact`, `contacts.block` really lands the peer on the list
    # `getBlocked` answers with, and `stories.togglePeerStoriesHidden` really
    # sets the flag `user get` reads back. That is what makes the idempotent
    # second pass — `already: true`, no RPC — assertable at all.

    def _user_of(self, ref: Any) -> Any:
        for attribute in ("user_id", "id"):
            value = getattr(ref, attribute, None)
            if isinstance(value, int):
                found = self.world.users.get(value)
                if found is not None:
                    return found
        if type(ref).__name__ in ("InputUserSelf", "InputPeerSelf"):
            return self.world.me
        return None

    def _contact_users(self) -> list[types.User]:
        return [self.world.users[uid] for uid in self.world.contacts if uid in self.world.users]

    def _raw_GetContactsRequest(self, request: Any) -> Any:
        return types.contacts.Contacts(
            contacts=[
                types.Contact(user_id=uid, mutual=mutual)
                for uid, mutual in self.world.contacts.items()
            ],
            saved_count=len(self.world.saved_contacts) or len(self.world.contacts),
            users=self._contact_users(),
        )

    def _raw_GetContactIDsRequest(self, request: Any) -> list[int]:
        return sorted(self.world.contacts)

    def _raw_GetStatusesRequest(self, request: Any) -> list[Any]:
        return [
            types.ContactStatus(user_id=uid, status=user.status)
            for uid, user in self.world.users.items()
            if uid in self.world.contacts and getattr(user, "status", None) is not None
        ]

    def _raw_AddContactRequest(self, request: Any) -> types.Updates:
        user = self._user_of(request.id)
        if user is None:
            from telethon.errors import RPCError

            raise RPCError(request, "CONTACT_ID_INVALID", 400)
        if not request.first_name:
            from telethon.errors import RPCError

            raise RPCError(request, "CONTACT_NAME_EMPTY", 400)
        user.first_name = request.first_name
        user.last_name = request.last_name
        user.contact = True
        self.world.contacts.setdefault(int(user.id), False)
        note = getattr(request, "note", None)
        if note is not None:
            self.world.contact_notes[int(user.id)] = getattr(note, "text", "") or ""
        return self._updates()

    def _raw_ImportContactsRequest(self, request: Any) -> Any:
        imported, retry, popular = [], [], []
        for item in request.contacts:
            phone = _e164(item.phone)
            user_id = self.world.phonebook.get(phone)
            if user_id is None:
                # Neither imported nor retried: the ambiguous answer.
                continue
            if user_id < 0:  # a test asking for the retry branch
                retry.append(int(item.client_id))
                continue
            imported.append(types.ImportedContact(user_id=user_id, client_id=int(item.client_id)))
            self.world.contacts.setdefault(user_id, False)
            user = self.world.users.get(user_id)
            if user is not None:
                user.contact = True
            popular.append(types.PopularContact(client_id=int(item.client_id), importers=3))
        return types.contacts.ImportedContacts(
            imported=imported,
            popular_invites=popular,
            retry_contacts=retry,
            users=[self.world.users[i.user_id] for i in imported if i.user_id in self.world.users],
        )

    def _raw_DeleteContactsRequest(self, request: Any) -> types.Updates:
        removed = []
        for ref in request.id:
            user = self._user_of(ref)
            if user is None:
                continue
            self.world.contacts.pop(int(user.id), None)
            user.contact = False
            removed.append(user)
        return types.Updates(
            updates=[], users=removed, chats=[], date=datetime.now(timezone.utc), seq=0
        )

    def _raw_DeleteByPhonesRequest(self, request: Any) -> bool:
        for phone in request.phones:
            self.world.saved_contacts = [
                entry for entry in self.world.saved_contacts if entry.phone != _e164(phone)
            ]
            user_id = self.world.phonebook.pop(_e164(phone), None)
            if user_id is not None:
                self.world.contacts.pop(user_id, None)
        return True

    def _raw_UpdateContactNoteRequest(self, request: Any) -> types.Updates:
        user = self._user_of(request.id)
        if user is None or int(user.id) not in self.world.contacts:
            from telethon.errors import RPCError

            raise RPCError(request, "CONTACT_MISSING", 400)
        text = getattr(request.note, "text", "") or ""
        if text:
            self.world.contact_notes[int(user.id)] = text
        else:
            self.world.contact_notes.pop(int(user.id), None)
        return self._updates()

    def _raw_AcceptContactRequest(self, request: Any) -> types.Updates:
        return self._updates()

    def _contacts_search(self, request: Any) -> Any:
        query = (request.q or "").lower()

        def matches(uid: int) -> bool:
            entity = self.world.entity_for(uid) or self.world.users.get(uid)
            if entity is None:
                return False
            haystack = " ".join(
                str(getattr(entity, key, "") or "")
                for key in ("first_name", "last_name", "title", "username")
            ).lower()
            return query in haystack

        mine = [uid for uid in self.world.search_mine if matches(uid)]
        found = [uid for uid in self.world.search_global if matches(uid)]
        entities = {uid: self.world.entity_for(uid) for uid in mine + found}
        return types.contacts.Found(
            my_results=[_peer_for(uid) for uid in mine],
            results=[_peer_for(uid) for uid in found],
            chats=[e for e in entities.values() if e is not None and not isinstance(e, types.User)],
            users=[e for e in entities.values() if isinstance(e, types.User)],
        )

    def _raw_GetSponsoredPeersRequest(self, request: Any) -> Any:
        if not self.world.sponsored_peers:
            return types.contacts.SponsoredPeersEmpty()
        return types.contacts.SponsoredPeers(
            peers=[
                types.SponsoredPeer(peer=_peer_for(uid), random_id=b"\x01\x02")
                for uid in self.world.sponsored_peers
            ],
            chats=[],
            users=[
                self.world.users[uid]
                for uid in self.world.sponsored_peers
                if uid in self.world.users
            ],
        )

    def _raw_GetRecentMeUrlsRequest(self, request: Any) -> Any:
        return types.help.RecentMeUrls(urls=[], chats=[], users=[])

    # -- blocking ----------------------------------------------------------

    def _raw_GetBlockedRequest(self, request: Any) -> Any:
        source = (
            self.world.blocked_stories
            if getattr(request, "my_stories_from", None)
            else self.world.blocked
        )
        rows = sorted(source.items())
        window = rows[request.offset : request.offset + request.limit]
        return types.contacts.BlockedSlice(
            count=len(rows),
            blocked=[types.PeerBlocked(peer_id=_peer_for(uid), date=date) for uid, date in window],
            chats=[],
            users=[self.world.users[uid] for uid, _ in window if uid in self.world.users],
        )

    def _blocked_target(self, request: Any) -> dict[int, Any]:
        return (
            self.world.blocked_stories
            if getattr(request, "my_stories_from", None)
            else self.world.blocked
        )

    def _raw_UnblockRequest(self, request: Any) -> bool:
        marked = abs(self._chat_id(request.id))
        return self._blocked_target(request).pop(marked, None) is not None

    def _raw_SetBlockedRequest(self, request: Any) -> bool:
        target = self._blocked_target(request)
        target.clear()
        for peer in request.id:
            target[abs(self._chat_id(peer))] = datetime.now(timezone.utc)
        return True

    def _raw_BlockFromRepliesRequest(self, request: Any) -> types.Updates:
        return self._updates()

    # -- close friends, birthdays, top peers -------------------------------

    def _raw_EditCloseFriendsRequest(self, request: Any) -> bool:
        wanted = {int(i) for i in request.id}
        for uid, user in self.world.users.items():
            user.close_friend = uid in wanted
        return True

    def _raw_GetBirthdaysRequest(self, request: Any) -> Any:
        return types.contacts.ContactBirthdays(
            contacts=[
                types.ContactBirthday(contact_id=uid, birthday=birthday)
                for uid, birthday in self.world.birthdays.items()
            ],
            users=[
                self.world.users[uid] for uid in self.world.birthdays if uid in self.world.users
            ],
        )

    def _raw_SuggestBirthdayRequest(self, request: Any) -> types.Updates:
        return self._updates()

    def _raw_GetTopPeersRequest(self, request: Any) -> Any:
        if not self.world.top_peers_enabled:
            return types.contacts.TopPeersDisabled()
        wanted = {
            "correspondents": "TopPeerCategoryCorrespondents",
            "bots_pm": "TopPeerCategoryBotsPM",
            "phone_calls": "TopPeerCategoryPhoneCalls",
            "groups": "TopPeerCategoryGroups",
            "channels": "TopPeerCategoryChannels",
        }
        categories = []
        users: list[Any] = []
        for flag, constructor in wanted.items():
            if not getattr(request, flag, None):
                continue
            rows = self.world.top_peers.get(flag.replace("_", "-"), [])
            categories.append(
                types.TopPeerCategoryPeers(
                    category=getattr(types, constructor)(),
                    count=len(rows),
                    peers=[
                        types.TopPeer(peer=_peer_for(uid), rating=rating) for uid, rating in rows
                    ],
                )
            )
            users += [self.world.users[uid] for uid, _ in rows if uid in self.world.users]
        return types.contacts.TopPeers(categories=categories, chats=[], users=users)

    def _raw_ToggleTopPeersRequest(self, request: Any) -> bool:
        self.world.top_peers_enabled = bool(request.enabled)
        if not request.enabled:
            self.world.top_peers.clear()
        return True

    def _raw_ResetTopPeerRatingRequest(self, request: Any) -> bool:
        return True

    # -- users -------------------------------------------------------------

    def _raw_GetUsersRequest(self, request: Any) -> list[Any]:
        out = []
        for ref in request.id:
            user = self._user_of(ref)
            out.append(user if user is not None else types.UserEmpty(id=0))
        return out

    def _raw_GetRequirementsToContactRequest(self, request: Any) -> list[Any]:
        out = []
        for ref in request.id:
            user = self._user_of(ref)
            rule = self.world.contact_requirements.get(int(getattr(user, "id", 0) or 0), "free")
            if rule == "premium":
                out.append(types.RequirementToContactPremium())
            elif rule.startswith("paid:"):
                out.append(types.RequirementToContactPaidMessages(stars_amount=int(rule[5:])))
            else:
                out.append(types.RequirementToContactEmpty())
        return out

    def _raw_ExportContactTokenRequest(self, request: Any) -> Any:
        return types.ExportedContactToken(
            url=f"https://t.me/contact/{self.world.contact_token}",
            expires=datetime.now(timezone.utc),
        )

    def _raw_ImportContactTokenRequest(self, request: Any) -> Any:
        return next(iter(self.world.users.values()), self.world.me)

    def _raw_GetUserPhotosRequest(self, request: Any) -> Any:
        user = self._user_of(request.user_id)
        photos = self.world.user_photos.get(int(getattr(user, "id", 0) or 0), [])
        window = photos[request.offset : request.offset + request.limit]
        return types.photos.PhotosSlice(count=len(photos), photos=window, users=[])

    def _raw_UploadContactProfilePhotoRequest(self, request: Any) -> Any:
        if request.file is None and request.video is None:
            return types.photos.Photo(photo=types.PhotoEmpty(id=0), users=[])
        return types.photos.Photo(
            photo=types.Photo(
                id=5150,
                access_hash=1,
                file_reference=b"",
                date=datetime.now(timezone.utc),
                sizes=[],
                dc_id=2,
            ),
            users=[],
        )

    def _raw_GetSavedMusicRequest(self, request: Any) -> Any:
        user = self._user_of(request.id)
        documents = self.world.saved_music.get(int(getattr(user, "id", 0) or 0), [])
        return types.users.SavedMusic(count=len(documents), documents=documents)

    def _raw_GetPersonalChannelHistoryRequest(self, request: Any) -> Any:
        user = self._user_of(request.user_id)
        overrides = self.world.user_full.get(int(getattr(user, "id", 0) or 0), {})
        channel_id = overrides.get("personal_channel_id")
        history = list(reversed(self.world.history(-1000000000000 - int(channel_id or 0))))
        return types.messages.ChannelMessages(
            pts=1,
            count=len(history),
            messages=history[: request.limit],
            topics=[],
            chats=list(self.world.chats.values()),
            users=[],
        )

    def _raw_GetSavedRequest(self, request: Any) -> list[Any]:
        return list(self.world.saved_contacts)

    def _raw_GetContactSignUpNotificationRequest(self, request: Any) -> bool:
        return self.world.contact_signup_silent

    def _raw_SetContactSignUpNotificationRequest(self, request: Any) -> bool:
        self.world.contact_signup_silent = bool(request.silent)
        return True

    # -- resolution --------------------------------------------------------

    def _resolved(self, entity: Any) -> Any:
        from telethon import utils

        return types.contacts.ResolvedPeer(
            peer=utils.get_peer(entity),
            chats=[] if isinstance(entity, types.User) else [entity],
            users=[entity] if isinstance(entity, types.User) else [],
        )

    def _raw_ResolveUsernameRequest(self, request: Any) -> Any:
        entity = self._lookup(request.username)
        if entity is None:
            from telethon.errors import UsernameNotOccupiedError

            raise UsernameNotOccupiedError(request)
        return self._resolved(entity)

    def _raw_ResolvePhoneRequest(self, request: Any) -> Any:
        user_id = self.world.phonebook.get(_e164(request.phone))
        entity = self.world.users.get(user_id or 0)
        if entity is None:
            from telethon.errors import RPCError

            raise RPCError(request, "PHONE_NOT_OCCUPIED", 400)
        return self._resolved(entity)

    def _raw_GetDeepLinkInfoRequest(self, request: Any) -> Any:
        return types.help.DeepLinkInfo(message=self.world.deep_link_message)

    # -- the reads `resolve link --open` performs ---------------------------
    #
    # Every one of them is a *read*: `resolve link` classifies and reports,
    # and the acting verb lives in another group. These exist so that the
    # dispatcher's per-kind branch is exercised rather than assumed.

    def _raw_CheckChatInviteRequest(self, request: Any) -> Any:
        answer = self.world.invite_previews.get(request.hash)
        if answer is not None:
            return answer
        return types.ChatInvite(
            title="Shared group",
            photo=types.PhotoEmpty(id=0),
            participants_count=12,
            color=0,
            about="A private group",
        )

    def _raw_GetBoostsStatusRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = self.world.boosts.get(chat_id)
        if not rows:
            return types.premium.BoostsStatus(
                level=3,
                current_level_boosts=10,
                boosts=12,
                boost_url="https://t.me/boost/news",
            )
        return types.premium.BoostsStatus(
            level=len(rows) // 2,
            current_level_boosts=len(rows),
            boosts=len(rows),
            boost_url=f"https://t.me/boost?c={abs(chat_id)}",
            my_boost=any(getattr(b, "user_id", 0) == self.world.me.id for b in rows) or None,
            next_level_boosts=len(rows) + 3,
        )

    def _raw_CheckGiftCodeRequest(self, request: Any) -> Any:
        return types.payments.CheckedGiftCode(
            date=datetime.now(timezone.utc),
            days=90,
            chats=[],
            users=[],
            used_date=datetime.now(timezone.utc),
        )

    def _raw_GetThemeRequest(self, request: Any) -> Any:
        return types.Theme(id=1, access_hash=1, slug="Slug", title="Midnight")

    # -- stories -----------------------------------------------------------

    def _raw_TogglePeerStoriesHiddenRequest(self, request: Any) -> bool:
        marked = abs(self._chat_id(request.peer))
        user = self.world.users.get(marked)
        if user is not None:
            user.stories_hidden = bool(request.hidden)
        return True

    def _raw_ToggleAllStoriesHiddenRequest(self, request: Any) -> bool:
        self.world.all_stories_hidden = bool(request.hidden)
        return True

    def _raw_GetAllReadPeerStoriesRequest(self, request: Any) -> types.Updates:
        return types.Updates(
            updates=[
                types.UpdateReadStories(peer=_peer_for(uid), max_id=max_id)
                for uid, max_id in self.world.stories_read.items()
            ],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    # -- the administration world ------------------------------------------
    #
    # Written as state, not as canned replies: `chat member ban` really moves
    # the person into `world.banned`, so `chat member list --filter banned`
    # finds them there afterwards and a mask that was written back incomplete
    # shows up as a wrong answer rather than as a plausible request object.

    def _participants(self, chat_id: int) -> dict[int, Any]:
        return self.world.members.setdefault(chat_id, {})

    def _banned_row(self, chat_id: int, user_id: int) -> Any:
        rights = self.world.banned[chat_id][user_id]
        return types.ChannelParticipantBanned(
            peer=types.PeerUser(user_id=user_id),
            kicked_by=self.world.me.id,
            date=datetime.now(timezone.utc),
            banned_rights=rights,
            left=bool(getattr(rights, "view_messages", False)),
        )

    def _people(self, ids: list[int]) -> list[Any]:
        return [self.world.users[i] for i in ids if i in self.world.users]

    def _raw_GetParticipantsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.channel)
        name = type(request.filter).__name__
        rows: list[Any] = []
        if name in ("ChannelParticipantsKicked", "ChannelParticipantsBanned"):
            rows = [
                self._banned_row(chat_id, user_id) for user_id in self.world.banned.get(chat_id, {})
            ]
            if name == "ChannelParticipantsKicked":
                rows = [row for row in rows if row.left]
            else:
                rows = [row for row in rows if not row.left]
        else:
            rows = list(self._participants(chat_id).values())
            if name == "ChannelParticipantsAdmins":
                rows = [
                    row
                    for row in rows
                    if type(row).__name__
                    in ("ChannelParticipantAdmin", "ChannelParticipantCreator")
                ]
            elif name == "ChannelParticipantsBots":
                rows = [
                    row
                    for row in rows
                    if getattr(self.world.users.get(getattr(row, "user_id", 0)), "bot", False)
                ]
            query = (getattr(request.filter, "q", "") or "").lower()
            if query:
                rows = [
                    row
                    for row in rows
                    if query
                    in (
                        getattr(self.world.users.get(getattr(row, "user_id", 0)), "first_name", "")
                        or ""
                    ).lower()
                ]
        total = len(rows)
        window = rows[int(request.offset) : int(request.offset) + int(request.limit)]
        ids = [
            getattr(row, "user_id", None) or abs(self._chat_id(getattr(row, "peer", None)))
            for row in window
        ]
        return types.channels.ChannelParticipants(
            count=total, participants=window, chats=[], users=self._people(ids)
        )

    def _raw_GetParticipantRequest(self, request: Any) -> Any:
        from telethon.errors import UserNotParticipantError

        chat_id = self._chat_id(request.channel)
        user_id = abs(self._chat_id(request.participant))
        # A restricted member is still a member; the banned mask is the more
        # specific answer, so it wins — which is what makes a second
        # `chat member restrict` patch the mask instead of resetting it.
        row = None
        if user_id in self.world.banned.get(chat_id, {}):
            row = self._banned_row(chat_id, user_id)
        if row is None:
            row = self._participants(chat_id).get(user_id)
        if row is None:
            raise UserNotParticipantError(request)
        return types.channels.ChannelParticipant(
            participant=row, chats=[], users=self._people([user_id])
        )

    def _raw_EditBannedRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        user_id = abs(self._chat_id(request.participant))
        rights = request.banned_rights
        flags = [
            name
            for name in dir(rights)
            if not name.startswith("_") and isinstance(getattr(rights, name, None), bool)
        ]
        store = self.world.banned.setdefault(chat_id, {})
        if any(getattr(rights, name, False) for name in flags):
            store[user_id] = rights
            if getattr(rights, "view_messages", False):
                self._participants(chat_id).pop(user_id, None)
        else:
            store.pop(user_id, None)
        return self._updates()

    def _raw_EditAdminRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        user_id = abs(self._chat_id(request.user_id))
        rights = request.admin_rights
        granted = any(
            getattr(rights, name, False)
            for name in dir(rights)
            if not name.startswith("_") and isinstance(getattr(rights, name, None), bool)
        )
        if granted:
            self.world.add_member(
                chat_id,
                user_id,
                status="admin",
                admin_rights=rights,
                rank=request.rank or None,
            )
        else:
            self.world.add_member(chat_id, user_id, status="member")
        return self._updates()

    def _raw_InviteToChannelRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.channel)
        missing = []
        for user in request.users:
            user_id = abs(self._chat_id(user))
            if user_id in getattr(self.world, "_privacy_blocked", ()):  # pragma: no cover
                missing.append(types.MissingInvitee(user_id=user_id))
                continue
            self.world.add_member(chat_id, user_id)
        return types.messages.InvitedUsers(updates=self._updates(), missing_invitees=missing)

    def _raw_CreateChatRequest(self, request: Any) -> Any:
        return types.messages.InvitedUsers(updates=self._updates(), missing_invitees=[])

    def _raw_EditChatAdminRequest(self, request: Any) -> bool:
        return True

    def _raw_ReportSpamRequest(self, request: Any) -> bool:
        return True

    def _raw_ReportAntiSpamFalsePositiveRequest(self, request: Any) -> bool:
        return True

    def _raw_EditChatParticipantRankRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        user_id = abs(self._chat_id(request.participant))
        row = self._participants(chat_id).get(user_id)
        if row is not None:
            row.rank = request.rank
        return self._updates()

    def _raw_ToggleNoPaidMessagesExceptionRequest(self, request: Any) -> bool:
        return True

    def _raw_EditChatDefaultBannedRightsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.default_banned[chat_id] = request.banned_rights
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.default_banned_rights = request.banned_rights
        return self._updates()

    # -- full chat ---------------------------------------------------------

    def _channel_full(self, chat_id: int) -> Any:
        settings = self.world.settings_of(chat_id)
        return types.ChannelFull(
            id=abs(chat_id) - 1000000000000 if chat_id < -1000000000000 else abs(chat_id),
            about=str(settings.get("about", "")),
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            chat_photo=types.PhotoEmpty(id=0),
            notify_settings=types.PeerNotifySettings(),
            bot_info=[],
            pts=1,
            participants_count=len(self._participants(chat_id)) or None,
            exported_invite=(self.world.invites.get(chat_id) or [None])[0],
            slowmode_seconds=settings.get("slowmode_seconds"),
            hidden_prehistory=settings.get("hidden_prehistory"),
            antispam=settings.get("antispam"),
            participants_hidden=settings.get("participants_hidden"),
            can_view_stats=settings.get("can_view_stats"),
            can_set_stickers=settings.get("can_set_stickers"),
            linked_chat_id=settings.get("linked_chat_id"),
            pending_suggestions=settings.get("pending_suggestions"),
            available_reactions=(
                self.world.chat_reactions.get(chat_id)
                or settings.get("available_reactions")
                or types.ChatReactionsNone()
            ),
            reactions_limit=self.world.reactions_limit.get(
                chat_id, settings.get("reactions_limit")
            ),
            paid_reactions_available=self.world.paid_enabled.get(chat_id),
            call=self._running_call(chat_id),
            send_paid_messages_stars=settings.get("send_paid_messages_stars"),
            view_forum_as_messages=settings.get("view_forum_as_messages"),
            restricted_sponsored=settings.get("restricted_sponsored"),
            stats_dc=settings.get("stats_dc"),
            default_send_as=settings.get("default_send_as"),
        )

    def _raw_GetFullChannelRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        return types.messages.ChatFull(
            full_chat=self._channel_full(chat_id),
            chats=[entity] if entity is not None else [],
            users=list(self.world.users.values()),
        )

    def _raw_GetFullChatRequest(self, request: Any) -> Any:
        chat_id = -int(request.chat_id)
        entity = self.world.entity_for(chat_id)
        rows = [
            types.ChatParticipant(
                user_id=user_id, inviter_id=self.world.me.id, date=datetime.now(timezone.utc)
            )
            if type(row).__name__ != "ChannelParticipantAdmin"
            else types.ChatParticipantAdmin(
                user_id=user_id, inviter_id=self.world.me.id, date=datetime.now(timezone.utc)
            )
            for user_id, row in self._participants(chat_id).items()
        ]
        full = types.ChatFull(
            id=abs(chat_id),
            about=str(self.world.settings_of(chat_id).get("about", "")),
            participants=types.ChatParticipants(chat_id=abs(chat_id), participants=rows, version=1),
            notify_settings=types.PeerNotifySettings(),
            available_reactions=self.world.chat_reactions.get(chat_id) or types.ChatReactionsNone(),
            reactions_limit=self.world.reactions_limit.get(chat_id),
            call=self._running_call(chat_id),
        )
        return types.messages.ChatFull(
            full_chat=full,
            chats=[entity] if entity is not None else [],
            users=list(self.world.users.values()),
        )

    # -- invites -----------------------------------------------------------

    def _raw_ExportChatInviteRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        existing = self.world.invites.setdefault(chat_id, [])
        link = f"https://t.me/+fake{len(existing) + 1}"
        return self.world.add_invite(
            chat_id,
            link,
            title=getattr(request, "title", None),
            expire_date=getattr(request, "expire_date", None),
            usage_limit=getattr(request, "usage_limit", None),
            request_needed=getattr(request, "request_needed", None),
            subscription_pricing=getattr(request, "subscription_pricing", None),
            permanent=not existing or None,
        )

    def _find_invite(self, chat_id: int, link: str) -> Any:
        for invite in self.world.invites.get(chat_id, []):
            if invite.link == link:
                return invite
        return None

    def _raw_EditExportedChatInviteRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        invite = self._find_invite(chat_id, request.link)
        if invite is None:
            invite = self.world.add_invite(chat_id, request.link)
        for name in ("title", "expire_date", "usage_limit", "request_needed", "revoked"):
            value = getattr(request, name, None)
            if value is not None:
                setattr(invite, name, value)
        return types.messages.ExportedChatInvite(
            invite=invite, users=list(self.world.users.values())
        )

    def _raw_GetExportedChatInviteRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        invite = self._find_invite(chat_id, request.link)
        if invite is None:
            from telethon.errors import RPCError

            raise RPCError(request, "INVITE_HASH_EXPIRED", 400)
        return types.messages.ExportedChatInvite(
            invite=invite, users=list(self.world.users.values())
        )

    def _raw_GetExportedChatInvitesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        wanted = bool(getattr(request, "revoked", False))
        rows = [i for i in self.world.invites.get(chat_id, []) if bool(i.revoked) == wanted]
        return types.messages.ExportedChatInvites(
            count=len(rows),
            invites=rows[: int(request.limit)],
            users=list(self.world.users.values()),
        )

    def _raw_GetAdminsWithInvitesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = self.world.invites.get(chat_id, [])
        by_admin: dict[int, list[Any]] = {}
        for invite in rows:
            by_admin.setdefault(int(invite.admin_id), []).append(invite)
        return types.messages.ChatAdminsWithInvites(
            admins=[
                types.ChatAdminWithInvites(
                    admin_id=admin_id,
                    invites_count=len([i for i in items if not i.revoked]),
                    revoked_invites_count=len([i for i in items if i.revoked]),
                )
                for admin_id, items in by_admin.items()
            ],
            users=list(self.world.users.values()),
        )

    def _raw_DeleteExportedChatInviteRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(request.peer)
        self.world.invites[chat_id] = [
            i for i in self.world.invites.get(chat_id, []) if i.link != request.link
        ]
        return True

    def _raw_DeleteRevokedExportedChatInvitesRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(request.peer)
        self.world.invites[chat_id] = [
            i for i in self.world.invites.get(chat_id, []) if not i.revoked
        ]
        return True

    def _raw_GetChatInviteImportersRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        want_requests = bool(getattr(request, "requested", False))
        rows = [
            row
            for row in self.world.importers.get(chat_id, [])
            if bool(getattr(row, "requested", False)) == want_requests
        ]
        window = rows[: int(request.limit)]
        return types.messages.ChatInviteImporters(
            count=len(rows),
            importers=window,
            users=self._people([int(row.user_id) for row in window]),
        )

    def _raw_HideChatJoinRequestRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        user_id = abs(self._chat_id(request.user_id))
        self.world.importers[chat_id] = [
            row
            for row in self.world.importers.get(chat_id, [])
            if int(row.user_id) != user_id or not getattr(row, "requested", False)
        ]
        if getattr(request, "approved", False):
            self.world.add_member(chat_id, user_id)
        return self._updates()

    def _raw_HideAllChatJoinRequestsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        pending = [
            row for row in self.world.importers.get(chat_id, []) if getattr(row, "requested", False)
        ]
        self.world.importers[chat_id] = [
            row
            for row in self.world.importers.get(chat_id, [])
            if not getattr(row, "requested", False)
        ]
        if getattr(request, "approved", False):
            for row in pending:
                self.world.add_member(chat_id, int(row.user_id))
        return self._updates()

    def _raw_ImportChatInviteRequest(self, request: Any) -> types.Updates:
        chats = list(self.world.chats.values())
        return types.Updates(
            updates=[], users=[], chats=chats[:1], date=datetime.now(timezone.utc), seq=0
        )

    def _raw_JoinChannelRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        return types.Updates(
            updates=[],
            users=[],
            chats=[entity] if entity is not None else [],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    # -- topics ------------------------------------------------------------

    def _raw_CreateForumTopicRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.next_message_id += 1
        topic_id = self.world.next_message_id
        self.world.add_topic(
            chat_id, topic_id, request.title, icon_emoji_id=getattr(request, "icon_emoji_id", None)
        )
        message = make_message(topic_id, chat_id=chat_id)
        return types.Updates(
            updates=[types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_EditForumTopicRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        topic = self.world.topics.setdefault(chat_id, {}).get(int(request.topic_id))
        if topic is None:
            topic = self.world.add_topic(chat_id, int(request.topic_id), "General")
        for name in ("title", "icon_emoji_id", "closed", "hidden"):
            value = getattr(request, name, None)
            if value is not None:
                setattr(topic, name, value)
        return self._updates()

    def _forum_topics(self, chat_id: int, rows: list[Any]) -> Any:
        return types.messages.ForumTopics(
            count=len(rows),
            topics=rows,
            messages=[],
            chats=[],
            users=[],
            pts=1,
        )

    def _raw_GetForumTopicsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = list(self.world.topics.get(chat_id, {}).values())
        query = (getattr(request, "q", "") or "").lower()
        if query:
            rows = [row for row in rows if query in (row.title or "").lower()]
        offset_topic = int(getattr(request, "offset_topic", 0) or 0)
        if offset_topic:
            rows = [row for row in rows if row.id > offset_topic]
        return self._forum_topics(chat_id, rows[: int(request.limit)])

    def _raw_GetForumTopicsByIDRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        known = self.world.topics.get(chat_id, {})
        rows = [
            known[int(topic_id)]
            if int(topic_id) in known
            else types.ForumTopicDeleted(id=int(topic_id))
            for topic_id in request.topics
        ]
        return self._forum_topics(chat_id, rows)

    def _raw_UpdatePinnedForumTopicRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        topic = self.world.topics.get(chat_id, {}).get(int(request.topic_id))
        if topic is not None:
            topic.pinned = bool(request.pinned) or None
        return self._updates()

    def _raw_ReorderPinnedForumTopicsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        wanted = {int(t) for t in request.order}
        for topic_id, topic in self.world.topics.get(chat_id, {}).items():
            if topic_id in wanted:
                topic.pinned = True
            elif getattr(request, "force", False):
                topic.pinned = None
        return self._updates()

    def _raw_DeleteTopicHistoryRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        self.world.topics.get(chat_id, {}).pop(int(request.top_msg_id), None)
        return types.messages.AffectedHistory(pts=1, pts_count=1, offset=0)

    def _raw_ReadDiscussionRequest(self, request: Any) -> bool:
        return True

    def _raw_ExportMessageLinkRequest(self, request: Any) -> Any:
        """Only a public chat gets a server-minted link.

        A private one answers with an empty link, which is what makes the
        caller fall back to the `t.me/c/<raw id>/<msg>` form — the shape the
        message group already relies on.
        """
        chat_id = self._chat_id(request.channel)
        name = getattr(self.world.entity_for(chat_id), "username", None)
        if not name:
            return types.ExportedMessageLink(link="", html="")
        return types.ExportedMessageLink(link=f"https://t.me/{name}/{request.id}", html="")

    # -- the admin log -----------------------------------------------------

    def _raw_GetAdminLogRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.channel)
        rows = sorted(self.world.admin_log.get(chat_id, []), key=lambda e: -e.id)
        max_id = int(getattr(request, "max_id", 0) or 0)
        if max_id:
            rows = [row for row in rows if row.id < max_id]
        window = rows[: int(request.limit)]
        return types.channels.AdminLogResults(
            events=window, chats=[], users=list(self.world.users.values())
        )

    # -- settings ----------------------------------------------------------

    def _setting_toggle(self, request: Any, key: str, attribute: str = "enabled") -> types.Updates:
        chat_id = self._chat_id(getattr(request, "channel", None) or getattr(request, "peer", None))
        self.world.settings_of(chat_id)[key] = getattr(request, attribute, None)
        return self._updates()

    def _raw_ToggleSlowModeRequest(self, request: Any) -> types.Updates:
        return self._setting_toggle(request, "slowmode_seconds", "seconds")

    def _raw_TogglePreHistoryHiddenRequest(self, request: Any) -> types.Updates:
        return self._setting_toggle(request, "hidden_prehistory")

    def _raw_ToggleAntiSpamRequest(self, request: Any) -> types.Updates:
        return self._setting_toggle(request, "antispam")

    def _raw_ToggleParticipantsHiddenRequest(self, request: Any) -> types.Updates:
        return self._setting_toggle(request, "participants_hidden")

    def _raw_ToggleJoinToSendRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.join_to_send = request.enabled
        return self._updates()

    def _raw_ToggleJoinRequestRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.join_request = request.enabled
        return self._updates()

    def _raw_ToggleForumRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.forum = request.enabled
            entity.forum_tabs = request.tabs
        return self._updates()

    def _raw_ToggleSignaturesRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.signatures = request.signatures_enabled
            entity.signature_profiles = request.profiles_enabled
        return self._updates()

    def _raw_ToggleAutotranslationRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.autotranslation = request.enabled
        return self._updates()

    def _raw_RestrictSponsoredMessagesRequest(self, request: Any) -> types.Updates:
        return self._setting_toggle(request, "restricted_sponsored", "restricted")

    def _raw_ToggleNoForwardsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.noforwards = request.enabled
        return self._updates()

    def _raw_UpdatePaidMessagesPriceRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        self.world.settings_of(chat_id)["send_paid_messages_stars"] = (
            request.send_paid_messages_stars
        )
        return self._updates()

    def _raw_CheckUsernameRequest(self, request: Any) -> bool:
        return request.username.lower() not in self.world.taken_usernames

    def _raw_UpdateUsernameRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.channel)
        entity = self.world.entity_for(chat_id)
        if entity is not None:
            entity.username = request.username or None
        return self._updates()

    def _raw_GetGroupsForDiscussionRequest(self, request: Any) -> Any:
        return types.messages.Chats(chats=list(self.world.chats.values()))

    def _raw_SetDiscussionGroupRequest(self, request: Any) -> bool:
        return True

    def _raw_GetChannelRecommendationsRequest(self, request: Any) -> Any:
        return types.messages.Chats(chats=list(self.world.chats.values()))

    def _raw_ReportSponsoredMessageRequest(self, request: Any) -> Any:
        if not request.option:
            return types.channels.SponsoredMessageReportResultChooseOption(
                title="Why?",
                options=[types.SponsoredMessageReportOption(text="Spam", option=b"\x09")],
            )
        return types.channels.SponsoredMessageReportResultReported()

    def _raw_SetCustomVerificationRequest(self, request: Any) -> bool:
        return True

    # -- boosts, stats and revenue ----------------------------------------

    def _raw_GetBoostsListRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = self.world.boosts.get(chat_id, [])
        return types.premium.BoostsList(
            count=len(rows), boosts=rows[: int(request.limit)], users=[], next_offset=None
        )

    def _raw_GetUserBoostsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        user_id = abs(self._chat_id(request.user_id))
        rows = [
            b for b in self.world.boosts.get(chat_id, []) if getattr(b, "user_id", 0) == user_id
        ]
        return types.premium.BoostsList(count=len(rows), boosts=rows, users=[], next_offset=None)

    def _raw_GetMyBoostsRequest(self, request: Any) -> Any:
        return types.premium.MyBoosts(my_boosts=list(self.world.my_boosts), chats=[], users=[])

    def _raw_ApplyBoostRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = self.world.boosts.setdefault(chat_id, [])
        for slot in request.slots or []:
            rows.append(
                types.Boost(
                    id=f"slot{slot}",
                    date=datetime.now(timezone.utc),
                    expires=datetime.now(timezone.utc),
                    user_id=self.world.me.id,
                )
            )
        return self._raw_GetBoostsStatusRequest(request)

    def _graph(self, token: str = "") -> Any:
        if token:
            return types.StatsGraphAsync(token=token)
        return types.StatsGraph(json=types.DataJSON(data='{"columns": []}'))

    def _abs_prev(self, current: float, previous: float) -> Any:
        return types.StatsAbsValueAndPrev(current=current, previous=previous)

    def _raw_GetBroadcastStatsRequest(self, request: Any) -> Any:
        return types.stats.BroadcastStats(
            period=types.StatsDateRangeDays(
                min_date=datetime.now(timezone.utc), max_date=datetime.now(timezone.utc)
            ),
            followers=self._abs_prev(120.0, 100.0),
            views_per_post=self._abs_prev(50.0, 40.0),
            shares_per_post=self._abs_prev(5.0, 4.0),
            reactions_per_post=self._abs_prev(9.0, 8.0),
            views_per_story=self._abs_prev(0.0, 0.0),
            shares_per_story=self._abs_prev(0.0, 0.0),
            reactions_per_story=self._abs_prev(0.0, 0.0),
            enabled_notifications=types.StatsPercentValue(part=60.0, total=120.0),
            growth_graph=self._graph("growth-token"),
            followers_graph=self._graph(),
            mute_graph=self._graph(),
            top_hours_graph=self._graph(),
            interactions_graph=self._graph(),
            iv_interactions_graph=self._graph(),
            views_by_source_graph=self._graph(),
            new_followers_by_source_graph=self._graph(),
            languages_graph=self._graph(),
            reactions_by_emotion_graph=self._graph(),
            story_interactions_graph=self._graph(),
            story_reactions_by_emotion_graph=self._graph(),
            recent_posts_interactions=[
                types.PostInteractionCountersMessage(msg_id=918, views=100, forwards=3, reactions=7)
            ],
        )

    def _raw_LoadAsyncGraphRequest(self, request: Any) -> Any:
        return types.StatsGraph(json=types.DataJSON(data='{"columns": ["x"]}'))

    def _raw_GetMessagePublicForwardsRequest(self, request: Any) -> Any:
        chats = list(self.world.chats.values())
        forwards = [
            types.PublicForwardMessage(message=make_message(12, chat_id=-1000000000000 - 5150))
        ]
        return types.stats.PublicForwards(
            count=1, forwards=forwards, chats=chats, users=[], next_offset=None
        )

    def _raw_GetStarsRevenueStatsRequest(self, request: Any) -> Any:
        return types.payments.StarsRevenueStats(
            revenue_graph=self._graph(),
            status=types.StarsRevenueStatus(
                current_balance=types.StarsAmount(amount=120, nanos=0),
                available_balance=types.StarsAmount(amount=100, nanos=0),
                overall_revenue=types.StarsAmount(amount=900, nanos=0),
                withdrawal_enabled=True,
            ),
            usd_rate=0.013,
        )

    def _raw_GetStarsTransactionsRequest(self, request: Any) -> Any:
        return types.payments.StarsStatus(
            balance=types.StarsAmount(amount=120, nanos=0),
            chats=[],
            users=[],
            history=[
                types.StarsTransaction(
                    id="tx1",
                    amount=types.StarsAmount(amount=50, nanos=0),
                    date=datetime.now(timezone.utc),
                    peer=types.StarsTransactionPeerFragment(),
                    title="Subscription",
                )
            ],
            next_offset=None,
        )

    def _raw_GetConnectedStarRefBotsRequest(self, request: Any) -> Any:
        return types.payments.ConnectedStarRefBots(
            count=1,
            connected_bots=[
                types.ConnectedBotStarRef(
                    url="https://t.me/refbot?start=x",
                    date=datetime.now(timezone.utc),
                    bot_id=8800,
                    commission_permille=200,
                    participants=3,
                    revenue=500,
                )
            ],
            users=[],
        )

    def _raw_ConnectStarRefBotRequest(self, request: Any) -> Any:
        return self._raw_GetConnectedStarRefBotsRequest(request)

    def _raw_EditConnectedStarRefBotRequest(self, request: Any) -> Any:
        return self._raw_GetConnectedStarRefBotsRequest(request)

    # -- entities ----------------------------------------------------------

    async def get_entity(self, ref: Any) -> Any:
        entity = self._lookup(ref)
        if entity is None:
            raise ValueError(f"Could not find the input entity for {ref!r}")
        return entity

    async def get_input_entity(self, ref: Any) -> Any:
        if isinstance(
            ref,
            (
                types.InputPeerUser,
                types.InputPeerChannel,
                types.InputPeerChat,
                types.InputPeerSelf,
            ),
        ):
            return ref
        entity = await self.get_entity(ref)
        if isinstance(entity, types.User):
            return types.InputPeerUser(entity.id, entity.access_hash or 0)
        if isinstance(entity, types.Channel):
            return types.InputPeerChannel(entity.id, entity.access_hash or 0)
        return types.InputPeerChat(entity.id)

    def _lookup(self, ref: Any) -> Any:
        if isinstance(ref, types.InputPeerSelf):
            return self.world.me
        for attribute in ("user_id", "chat_id", "channel_id"):
            value = getattr(ref, attribute, None)
            if isinstance(value, int) and not isinstance(ref, int):
                return self.world.users.get(value) or self.world.chats.get(value)
        if ref in ("me", "self"):
            return self.world.me
        if isinstance(ref, str):
            handle = ref.lstrip("@").lower()
            for user in self.world.users.values():
                if (user.username or "").lower() == handle:
                    return user
            for chat in self.world.chats.values():
                if (getattr(chat, "username", None) or "").lower() == handle:
                    return chat
            if ref.lstrip("-").isdigit():
                return self._lookup(int(ref))
            return None
        if isinstance(ref, int):
            raw = abs(ref)
            if ref < -1000000000000:
                raw = -1000000000000 - ref
            return self.world.users.get(raw) or self.world.chats.get(raw)
        return ref

    # -- messages ----------------------------------------------------------

    def _chat_id(self, ref: Any) -> int:
        """The marked id for whatever a caller addressed the chat with."""
        from telethon import utils

        if isinstance(ref, int):
            return ref
        if isinstance(ref, types.InputPeerSelf):
            return self.world.me.id
        try:
            return int(utils.get_peer_id(ref))
        except (TypeError, ValueError):
            entity = self._lookup(ref)
            return int(utils.get_peer_id(entity)) if entity is not None else 0

    def iter_messages(
        self,
        entity: Any,
        limit: float | None = None,
        *,
        offset_id: int = 0,
        add_offset: int = 0,
        max_id: int = 0,
        min_id: int = 0,
        offset_date: Any = None,
        search: str | None = None,
        filter: Any = None,
        from_user: Any = None,
        ids: Any = None,
        reverse: bool = False,
        reply_to: int | None = None,
        scheduled: bool = False,
        **_: Any,
    ) -> Any:
        """The real offset semantics, because the ops depend on them.

        `offset_id` is exclusive and means "older than this" by default;
        `add_offset` shifts the window, which is how `--around` centres a page
        and how `reverse` walks forwards. Getting this wrong in the fake would
        make the cursor tests pass against a lie.
        """
        chat_id = self._chat_id(entity)
        self.world.calls.append(
            ("iter_messages", {"chat_id": chat_id, "limit": limit, "offset_id": offset_id})
        )
        failure = self.world._fail_next.pop("iter_messages", None)
        if failure is not None:
            return _AsyncFailure(failure)
        history = sorted(self.world.history(chat_id), key=lambda m: m.id)

        if ids is not None:
            wanted = [ids] if isinstance(ids, int) else list(ids)
            by_id = {m.id: m for m in history}
            return _AsyncList([by_id.get(i) for i in wanted])

        if scheduled:
            history = [m for m in history if getattr(m, "_scheduled", False)]
        else:
            history = [m for m in history if not getattr(m, "_scheduled", False)]

        if search:
            history = [m for m in history if search.lower() in (m.message or "").lower()]
        if from_user is not None:
            sender = self._chat_id(from_user)
            history = [
                m
                for m in history
                if getattr(getattr(m, "from_id", None), "user_id", None) == abs(sender)
            ]
        if reply_to is not None:
            history = [
                m
                for m in history
                if getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None) == reply_to
            ]
        if filter is not None:
            name = type(filter).__name__
            if name == "InputMessagesFilterPinned":
                pinned = self.world.pinned.get(chat_id, set())
                history = [m for m in history if m.id in pinned]
            else:
                history = [m for m in history if getattr(m, "media", None) is not None]
        if min_id:
            history = [m for m in history if m.id > min_id]
        if max_id:
            history = [m for m in history if m.id < max_id]
        if offset_date is not None:
            history = [m for m in history if m.date and m.date <= offset_date]

        ordered = history if reverse else list(reversed(history))
        if offset_id:
            ordered = [m for m in ordered if (m.id > offset_id if reverse else m.id < offset_id)]
        if add_offset:
            # A negative add_offset walks *back* into the newer side, which is
            # what centres a page on an id.
            back = -add_offset
            source = history if reverse else list(reversed(history))
            start = max(0, source.index(ordered[0]) - back) if ordered else 0
            ordered = source[start:]
        if limit is not None:
            ordered = ordered[: int(limit)]
        return _AsyncList(list(ordered))

    async def get_messages(self, entity: Any, ids: Any = None, **kwargs: Any) -> Any:
        found = [m async for m in self.iter_messages(entity, ids=ids, **kwargs)]
        if isinstance(ids, int):
            return found[0] if found else None
        return found

    async def send_message(self, entity: Any, message: str = "", **kwargs: Any) -> types.Message:
        chat_id = self._chat_id(entity)
        self.world.calls.append(("send_message", {"chat_id": chat_id, "message": message}))
        return self.world.add_message(chat_id, message, out=True, sender_id=self.world.me.id)

    async def send_file(self, entity: Any, file: Any, **kwargs: Any) -> types.Message:
        chat_id = self._chat_id(entity)
        self.world.calls.append(("send_file", {"chat_id": chat_id, "file": file}))
        return self.world.add_message(
            chat_id, kwargs.get("caption", "") or "", out=True, sender_id=self.world.me.id
        )

    async def edit_message(self, entity: Any, message: Any, text: str = "", **kwargs: Any) -> Any:
        chat_id = self._chat_id(entity)
        found = self.world.find(chat_id, int(message))
        if found is not None:
            found.message = text
            found.edit_date = datetime.now(timezone.utc)
        self.world.calls.append(("edit_message", {"chat_id": chat_id, "id": message}))
        return found

    async def delete_messages(self, entity: Any, message_ids: Any, revoke: bool = True) -> Any:
        chat_id = self._chat_id(entity)
        wanted = {int(i) for i in ([message_ids] if isinstance(message_ids, int) else message_ids)}
        history = self.world.history(chat_id)
        removed = [m for m in history if m.id in wanted]
        self.world.messages[chat_id] = [m for m in history if m.id not in wanted]
        self.world.calls.append(
            ("delete_messages", {"chat_id": chat_id, "ids": sorted(wanted), "revoke": revoke})
        )
        return [types.messages.AffectedMessages(pts=len(removed), pts_count=len(removed))]

    async def forward_messages(self, entity: Any, messages: Any, from_peer: Any = None) -> Any:
        target = self._chat_id(entity)
        source = self._chat_id(from_peer)
        wanted = [messages] if isinstance(messages, int) else list(messages)
        out = []
        for message_id in wanted:
            original = self.world.find(source, int(message_id))
            out.append(
                self.world.add_message(target, original.message if original else "", out=True)
            )
        return out

    async def pin_message(self, entity: Any, message: Any = None, **kwargs: Any) -> Any:
        chat_id = self._chat_id(entity)
        self.world.pinned.setdefault(chat_id, set()).add(int(message))
        return None

    async def unpin_message(self, entity: Any, message: Any = None, **kwargs: Any) -> Any:
        chat_id = self._chat_id(entity)
        self.world.pinned.setdefault(chat_id, set()).discard(int(message or 0))
        return None

    async def send_read_acknowledge(self, entity: Any, **kwargs: Any) -> bool:
        chat_id = self._chat_id(entity)
        maximum = int(kwargs.get("max_id") or 0)
        self.world.read_inbox[chat_id] = maximum
        self.world.calls.append(("send_read_acknowledge", {"chat_id": chat_id, "max_id": maximum}))
        return True

    async def get_drafts(self, entity: Any = None) -> list[Any]:
        return [
            _Draft(chat_id, draft, self._entity_for(chat_id))
            for chat_id, draft in self.world.drafts.items()
        ]

    def _entity_for(self, chat_id: int) -> Any:
        raw = abs(chat_id)
        if chat_id < -1000000000000:
            raw = -1000000000000 - chat_id
        return self.world.users.get(raw) or self.world.chats.get(raw)

    def action(self, entity: Any, action: str, **_: Any) -> Any:
        self.world.calls.append(("action", {"chat_id": self._chat_id(entity), "action": action}))
        return _NullAction()

    def iter_dialogs(self, limit: int | None = None, **_: Any) -> Any:
        entities = list(self.world.dialogs) or [
            _Dialog(entity) for entity in self.world.chats.values()
        ]
        if limit is not None:
            entities = entities[:limit]
        return _AsyncList(entities)

    # -- files -------------------------------------------------------------
    #
    # `iter_download` serves the bytes registered in `world.file_bytes`, with
    # the real offset/limit semantics: a test that asserts a resumed download
    # continued at the right byte is asserting against the same arithmetic the
    # server does.

    def iter_download(
        self,
        location: Any,
        *,
        offset: int = 0,
        request_size: int = 512 * 1024,
        limit: int | None = None,
        **_: Any,
    ) -> Any:
        doc_id = self._document_id(location)
        data = self.world.file_bytes.get(doc_id, b"")
        self.world.calls.append(
            ("iter_download", {"doc_id": doc_id, "offset": offset, "limit": limit})
        )
        end = len(data) if limit is None else min(len(data), offset + limit)
        chunks = [
            data[start : min(start + request_size, end)]
            for start in range(offset, end, request_size)
        ]
        return _AsyncList([chunk for chunk in chunks if chunk])

    def _document_id(self, location: Any) -> int:
        for attribute in ("id",):
            value = getattr(location, attribute, None)
            if isinstance(value, int):
                return value
            inner = getattr(value, "id", None)
            if isinstance(inner, int):
                return inner
        return 0

    async def download_media(
        self, message: Any, file: Any = None, thumb: Any = None, **_: Any
    ) -> Any:
        media = getattr(message, "media", message)
        document = getattr(media, "document", None) or getattr(media, "photo", None) or media
        doc_id = self._document_id(document)
        self.world.calls.append(("download_media", {"doc_id": doc_id, "thumb": thumb}))
        data = self.world.file_bytes.get(doc_id, b"")
        if thumb is not None:
            data = b"thumbnail"
        path = Path(str(file))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    async def download_profile_photo(self, entity: Any, file: Any = None, **kwargs: Any) -> Any:
        chat_id = self._chat_id(entity)
        self.world.calls.append(("download_profile_photo", {"chat_id": chat_id, **kwargs}))
        data = self.world.profile_photos.get(chat_id)
        if data is None:
            return None
        path = Path(str(file))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def _raw_GetFileHashesRequest(self, request: Any) -> Any:
        import hashlib

        doc_id = self._document_id(request.location)
        data = self.world.file_bytes.get(doc_id, b"")
        return [types.FileHash(offset=0, limit=len(data), hash=hashlib.sha256(data).digest())]

    def _raw_GetDocumentByHashRequest(self, request: Any) -> Any:
        return None

    # -- shared media ------------------------------------------------------

    def _raw_GetSearchCountersRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        count = len([m for m in self.world.history(chat_id) if getattr(m, "media", None)])
        return [types.messages.SearchCounter(filter=f, count=count) for f in request.filters]

    def _raw_GetSearchResultsCalendarRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        history = [m for m in self.world.history(chat_id) if getattr(m, "media", None)]
        return types.messages.SearchResultsCalendar(
            count=len(history),
            min_date=history[0].date if history else datetime.now(timezone.utc),
            min_msg_id=history[0].id if history else 0,
            periods=[
                types.SearchResultsCalendarPeriod(
                    date=message.date, min_msg_id=message.id, max_msg_id=message.id, count=1
                )
                for message in history
            ],
            messages=[],
            chats=[],
            users=[],
        )

    def _raw_GetSearchResultsPositionsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        history = [m for m in self.world.history(chat_id) if getattr(m, "media", None)]
        return types.messages.SearchResultsPositions(
            count=len(history),
            positions=[
                types.SearchResultPosition(msg_id=message.id, date=message.date, offset=index)
                for index, message in enumerate(history)
            ],
        )

    def _raw_GetExtendedMediaRequest(self, request: Any) -> Any:
        return self._updates()

    def _raw_GetAttachedStickersRequest(self, request: Any) -> Any:
        return [self._covered(entry) for entry in self.world.sticker_sets.values()][:1]

    def _raw_ReadMessageContentsRequest(self, request: Any) -> Any:
        return types.messages.AffectedMessages(pts=1, pts_count=len(request.id))

    def _raw_ReportMusicListenRequest(self, request: Any) -> Any:
        return True

    def _raw_GetSponsoredMessagesRequest(self, request: Any) -> Any:
        return types.messages.SponsoredMessages(
            messages=[
                types.SponsoredMessage(
                    url="https://example.invalid",
                    title="Sponsor",
                    message="An advert",
                    button_text="Open",
                    random_id=b"\x01\x02\x03",
                    can_report=True,
                )
            ],
            chats=[],
            users=[],
        )

    # -- the story world ---------------------------------------------------
    #
    # Stage E. Same rule as everywhere else: a request moves the world, so a
    # test asserts against state that changed. `story pin` really adds the id
    # to the profile page, `story delete` really removes the item, and
    # `story hide` really flips the flag the next `get_entity` reports.

    def _stories(self, peer: Any) -> dict[int, Any]:
        return self.world.stories_of(self._chat_id(peer))

    def _story_page(self, items: list[Any], *, pinned_to_top: list[int] | None = None) -> Any:
        return types.stories.Stories(
            count=len(items), stories=items, chats=[], users=[], pinned_to_top=pinned_to_top
        )

    def _raw_GetStoriesByIDRequest(self, request: Any) -> Any:
        stored = self._stories(request.peer)
        return self._story_page([stored[i] for i in request.id if i in stored])

    def _raw_GetPeerStoriesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        stored = self.world.stories_of(chat_id)
        archived = set(self.world.story_archive.get(chat_id, []))
        active = [story for sid, story in sorted(stored.items()) if sid not in archived]
        return types.stories.PeerStories(
            stories=types.PeerStories(
                peer=self._peer_of(chat_id),
                stories=active,
                max_read_id=self.world.story_read.get(chat_id, 0),
            ),
            chats=list(self.world.chats.values()),
            users=list(self.world.users.values()),
        )

    def _raw_GetPinnedStoriesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        stored = self.world.stories_of(chat_id)
        ids = sorted(self.world.story_pinned.get(chat_id, set()))
        if request.offset_id:
            ids = [i for i in ids if i < request.offset_id]
        ids = ids[: request.limit]
        return self._story_page(
            [stored[i] for i in ids if i in stored],
            pinned_to_top=list(self.world.story_pinned_top.get(chat_id, [])),
        )

    def _raw_GetStoriesArchiveRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        stored = self.world.stories_of(chat_id)
        ids = sorted(self.world.story_archive.get(chat_id, []), reverse=True)
        if request.offset_id:
            ids = [i for i in ids if i < request.offset_id]
        return self._story_page([stored[i] for i in ids[: request.limit] if i in stored])

    def _raw_GetAlbumStoriesRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        album = self.world.story_albums.get(chat_id, {}).get(request.album_id)
        stored = self.world.stories_of(chat_id)
        ids = list(album["stories"]) if album else []
        window = ids[request.offset : request.offset + request.limit]
        return self._story_page([stored[i] for i in window if i in stored])

    def _raw_SendStoryRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.next_story_id += 1
        story = make_story(
            self.world.next_story_id,
            caption=request.caption or "",
            pinned=bool(request.pinned),
            noforwards=bool(request.noforwards),
            privacy=None,
            media_areas=list(request.media_areas or []) or None,
            albums=list(request.albums or []) or None,
        )
        story.entities = list(request.entities or []) or None
        self.world.peer_stories.setdefault(chat_id, {})[story.id] = story
        if request.pinned:
            self.world.story_pinned.setdefault(chat_id, set()).add(story.id)
        for album_id in request.albums or []:
            self.world.story_albums.setdefault(chat_id, {}).setdefault(
                album_id, {"title": f"Album {album_id}", "stories": []}
            )["stories"].append(story.id)
        return types.Updates(
            updates=[
                types.UpdateStoryID(id=story.id, random_id=request.random_id),
                types.UpdateStory(peer=self._peer_of(chat_id), story=story),
            ],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_EditStoryRequest(self, request: Any) -> types.Updates:
        story = self._stories(request.peer).get(request.id)
        if story is None:
            raise ValueError(f"no story {request.id}")
        if request.caption is not None:
            story.caption = request.caption
            story.entities = list(request.entities or []) or None
        if request.media_areas is not None:
            story.media_areas = list(request.media_areas)
        if request.media is not None:
            story.media = self.realise(request.media, existing=story.media)
        story.edited = True
        return self._updates()

    def _raw_DeleteStoriesRequest(self, request: Any) -> list[int]:
        chat_id = self._chat_id(request.peer)
        stored = self.world.stories_of(chat_id)
        gone = [i for i in request.id if stored.pop(i, None) is not None]
        self.world.story_pinned.get(chat_id, set()).difference_update(gone)
        self.world.story_archive[chat_id] = [
            i for i in self.world.story_archive.get(chat_id, []) if i not in gone
        ]
        return gone

    def _raw_TogglePinnedRequest(self, request: Any) -> list[int]:
        chat_id = self._chat_id(request.peer)
        shelf = self.world.story_pinned.setdefault(chat_id, set())
        changed: list[int] = []
        for story_id in request.id:
            if request.pinned and story_id not in shelf:
                shelf.add(story_id)
                changed.append(story_id)
            elif not request.pinned and story_id in shelf:
                shelf.discard(story_id)
                changed.append(story_id)
        return changed

    def _raw_TogglePinnedToTopRequest(self, request: Any) -> bool:
        self.world.story_pinned_top[self._chat_id(request.peer)] = list(request.id)
        return True

    def _raw_TogglePeerStoriesHiddenRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(request.peer)
        entity = self._lookup(request.peer)
        if entity is not None:
            entity.stories_hidden = bool(request.hidden)
        if request.hidden:
            self.world.stories_hidden_peers.add(chat_id)
        else:
            self.world.stories_hidden_peers.discard(chat_id)
        return True

    def _raw_ToggleAllStoriesHiddenRequest(self, request: Any) -> bool:
        self.world.all_stories_hidden = bool(request.hidden)
        return True

    def _raw_ReadStoriesRequest(self, request: Any) -> list[int]:
        chat_id = self._chat_id(request.peer)
        was = self.world.story_read.get(chat_id, 0)
        if request.max_id <= was:
            return []
        self.world.story_read[chat_id] = request.max_id
        return [i for i in sorted(self.world.stories_of(chat_id)) if was < i <= request.max_id]

    def _raw_IncrementStoryViewsRequest(self, request: Any) -> bool:
        return True

    def _story_reaction(self, request: Any) -> types.Updates:
        story = self._stories(request.peer).get(request.story_id)
        if story is not None:
            empty = type(request.reaction).__name__ == "ReactionEmpty"
            story.sent_reaction = None if empty else request.reaction
        return self._updates()

    def _raw_CanSendStoryRequest(self, request: Any) -> Any:
        return self.world.can_send_story or types.stories.CanSendStoryCount(count_remains=3)

    def _raw_GetChatsToSendRequest(self, request: Any) -> Any:
        return types.messages.Chats(chats=list(self.world.chats_to_send))

    def _raw_ExportStoryLinkRequest(self, request: Any) -> Any:
        entity = self._lookup(request.peer)
        username = getattr(entity, "username", None) or "someone"
        return types.ExportedStoryLink(link=f"https://t.me/{username}/s/{request.id}")

    def _story_views(self, peer_id: int, story_id: int) -> types.StoryViews:
        rows = self.world.story_viewers.get((peer_id, story_id), [])
        return types.StoryViews(
            views_count=len(rows),
            has_viewers=True,
            reactions_count=sum(1 for row in rows if getattr(row, "reaction", None)),
            recent_viewers=[getattr(row, "user_id", 0) for row in rows][:3],
        )

    def _raw_GetStoriesViewsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        return types.stories.StoryViews(
            views=[self._story_views(chat_id, i) for i in request.id], users=[]
        )

    def _raw_GetStoryViewsListRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = list(self.world.story_viewers.get((chat_id, request.id), []))
        if request.q:
            wanted = request.q.lower()
            rows = [
                row
                for row in rows
                if wanted
                in (
                    (self.world.users.get(getattr(row, "user_id", 0)) or make_user(0)).username
                    or ""
                )
            ]
        window = rows[: request.limit]
        return types.stories.StoryViewsList(
            count=len(rows),
            views_count=len(rows),
            forwards_count=0,
            reactions_count=sum(1 for row in rows if getattr(row, "reaction", None)),
            views=window,
            chats=[],
            users=list(self.world.users.values()),
            next_offset="page2" if len(rows) > len(window) else None,
        )

    def _raw_GetStoryReactionsListRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        rows = [
            types.StoryReaction(
                peer_id=types.PeerUser(user_id=getattr(row, "user_id", 0)),
                date=getattr(row, "date", None),
                reaction=getattr(row, "reaction", None) or types.ReactionEmoji(emoticon="👍"),
            )
            for row in self.world.story_viewers.get((chat_id, request.id), [])
        ]
        return types.stories.StoryReactionsList(
            count=len(rows),
            reactions=rows[: request.limit],
            chats=[],
            users=list(self.world.users.values()),
            next_offset=None,
        )

    # albums ---------------------------------------------------------------

    def _album_type(self, album_id: int, entry: dict[str, Any]) -> Any:
        return types.StoryAlbum(album_id=album_id, title=entry["title"])

    def _raw_CreateAlbumRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        albums = self.world.story_albums.setdefault(chat_id, {})
        album_id = max(albums, default=0) + 1
        albums[album_id] = {"title": request.title, "stories": list(request.stories)}
        self.world.story_album_order.setdefault(chat_id, []).append(album_id)
        return self._album_type(album_id, albums[album_id])

    def _raw_DeleteAlbumRequest(self, request: Any) -> bool:
        chat_id = self._chat_id(request.peer)
        self.world.story_albums.get(chat_id, {}).pop(request.album_id, None)
        order = self.world.story_album_order.get(chat_id, [])
        self.world.story_album_order[chat_id] = [i for i in order if i != request.album_id]
        return True

    def _raw_UpdateAlbumRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        entry = self.world.story_albums.setdefault(chat_id, {}).setdefault(
            request.album_id, {"title": "", "stories": []}
        )
        if request.title is not None:
            entry["title"] = request.title
        for story_id in request.add_stories or []:
            if story_id not in entry["stories"]:
                entry["stories"].append(story_id)
        for story_id in request.delete_stories or []:
            if story_id in entry["stories"]:
                entry["stories"].remove(story_id)
        if request.order:
            entry["stories"] = list(request.order)
        return self._album_type(request.album_id, entry)

    def _raw_GetAlbumsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        if request.hash and request.hash == self.world.story_albums_hash:
            return types.stories.AlbumsNotModified()
        albums = self.world.story_albums.get(chat_id, {})
        order = self.world.story_album_order.get(chat_id) or sorted(albums)
        return types.stories.Albums(
            hash=self.world.story_albums_hash,
            albums=[self._album_type(i, albums[i]) for i in order if i in albums],
        )

    def _raw_ReorderAlbumsRequest(self, request: Any) -> bool:
        self.world.story_album_order[self._chat_id(request.peer)] = list(request.order)
        return True

    # the feed -------------------------------------------------------------

    def _raw_GetAllStoriesRequest(self, request: Any) -> Any:
        stealth = self.world.stealth_mode or types.StoriesStealthMode()
        if self.world.story_feed_not_modified is not None:
            return types.stories.AllStoriesNotModified(
                state=self.world.story_feed_not_modified, stealth_mode=stealth
            )
        hidden = bool(getattr(request, "hidden", False))
        rows = []
        for chat_id, stored in sorted(self.world.peer_stories.items()):
            is_hidden = chat_id in self.world.stories_hidden_peers
            if is_hidden != hidden:
                continue
            rows.append(
                types.PeerStories(
                    peer=self._peer_of(chat_id),
                    stories=[stored[i] for i in sorted(stored)],
                    max_read_id=self.world.story_read.get(chat_id, 0),
                )
            )
        return types.stories.AllStories(
            count=len(rows),
            state=self.world.story_feed_state,
            peer_stories=rows,
            chats=list(self.world.chats.values()),
            users=list(self.world.users.values()),
            stealth_mode=stealth,
            has_more=self.world.story_feed_has_more or None,
        )

    def _raw_GetPeerMaxIDsRequest(self, request: Any) -> Any:
        out = []
        for peer in request.id:
            stored = self.world.stories_of(self._chat_id(peer))
            out.append(types.RecentStory(max_id=max(stored, default=0)))
        return out

    def _raw_GetAllReadPeerStoriesRequest(self, request: Any) -> Any:
        return types.Updates(
            updates=[
                types.UpdateReadStories(peer=self._peer_of(chat_id), max_id=max_id)
                for chat_id, max_id in sorted(self.world.story_read.items())
            ],
            users=list(self.world.users.values()),
            chats=list(self.world.chats.values()),
            date=datetime.now(timezone.utc),
            seq=0,
        )

    # stealth, search, blocklist, live -------------------------------------

    def _raw_ActivateStealthModeRequest(self, request: Any) -> types.Updates:
        now = datetime.now(timezone.utc)
        self.world.stealth_mode = types.StoriesStealthMode(
            active_until_date=now + timedelta(minutes=5),
            cooldown_until_date=now + timedelta(hours=1),
        )
        return self._updates()

    def _raw_SearchPostsRequest(self, request: Any) -> Any:
        rows = list(self.world.public_stories)
        return types.stories.FoundStories(
            count=len(rows),
            stories=rows[: request.limit],
            chats=list(self.world.chats.values()),
            users=list(self.world.users.values()),
            next_offset="page2" if len(rows) > request.limit else None,
        )

    def _raw_GetBlockedRequest(self, request: Any) -> Any:
        if not getattr(request, "my_stories_from", False):
            return types.contacts.Blocked(blocked=[], chats=[], users=[])
        rows = self.world.story_blocklist[request.offset : request.offset + request.limit]
        return types.contacts.Blocked(
            blocked=[
                types.PeerBlocked(
                    peer_id=types.PeerUser(user_id=user_id), date=datetime.now(timezone.utc)
                )
                for user_id in rows
            ],
            chats=[],
            users=[self.world.users[u] for u in rows if u in self.world.users],
        )

    def _raw_UnblockRequest(self, request: Any) -> bool:
        raw = self._chat_id(request.id)
        if raw in self.world.story_blocklist:
            self.world.story_blocklist.remove(raw)
            return True
        return False

    def _raw_SetBlockedRequest(self, request: Any) -> bool:
        self.world.story_blocklist = [self._chat_id(peer) for peer in request.id]
        return True

    def _raw_StartLiveRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.next_story_id += 1
        story = make_story(self.world.next_story_id, caption=request.caption or "")
        self.world.peer_stories.setdefault(chat_id, {})[story.id] = story
        return types.Updates(
            updates=[types.UpdateStoryID(id=story.id, random_id=request.random_id)],
            users=[],
            chats=[],
            date=datetime.now(timezone.utc),
            seq=0,
        )

    def _raw_GetGroupCallStreamRtmpUrlRequest(self, request: Any) -> Any:
        return types.phone.GroupCallStreamRtmpUrl(url="rtmps://dc.tg/s/", key="secret-key")

    # statistics -----------------------------------------------------------

    def _raw_GetStoryStatsRequest(self, request: Any) -> Any:
        return types.stats.StoryStats(
            views_graph=types.StatsGraph(json=types.DataJSON(data='{"columns": []}')),
            reactions_by_emotion_graph=types.StatsGraphAsync(token="graph-token"),
        )

    def _raw_LoadAsyncGraphRequest(self, request: Any) -> Any:
        return types.StatsGraph(json=types.DataJSON(data='{"columns": ["reactions"]}'))

    def _raw_GetStoryPublicForwardsRequest(self, request: Any) -> Any:
        return types.stats.PublicForwards(
            count=1,
            forwards=[
                types.PublicForwardStory(
                    peer=types.PeerChannel(channel_id=555),
                    story=make_story(7, caption="repost"),
                )
            ],
            chats=list(self.world.chats.values()),
            users=[],
            next_offset=None,
        )

    def _peer_of(self, chat_id: int) -> Any:
        """A marked id back as the `Peer*` the TL types carry."""
        if chat_id < -1000000000000:
            return types.PeerChannel(channel_id=-1000000000000 - chat_id)
        if chat_id < 0:
            return types.PeerChat(chat_id=-chat_id)
        return types.PeerUser(user_id=chat_id)

    # -- sticker sets ------------------------------------------------------

    def _covered(self, entry: Any) -> Any:
        documents = entry["documents"]
        return types.StickerSetCovered(
            set=entry["set"], cover=documents[0] if documents else make_document(1)
        )

    def _sticker_set_reply(self, short_name: str) -> Any:
        """The `messages.stickerSet` reply for one set, by name.

        Every pack-authoring call answers with the set it changed, so they
        reach for this rather than re-entering the request handler with a
        hand-made request object.
        """
        return self._raw_GetStickerSetRequest(types.InputStickerSetShortName(short_name=short_name))

    def _raw_GetStickerSetRequest(self, request: Any) -> Any:
        stickerset = getattr(request, "stickerset", request)
        entry = self.world.set_for(stickerset)
        if entry is None:
            from telethon.errors import StickersetInvalidError

            raise StickersetInvalidError(request)
        documents = entry["documents"]
        packs: dict[str, list[int]] = {}
        for document in documents:
            for attribute in document.attributes:
                emoji = getattr(attribute, "alt", None)
                if emoji:
                    packs.setdefault(emoji, []).append(document.id)
        return types.messages.StickerSet(
            set=entry["set"],
            packs=[
                types.StickerPack(emoticon=emoji, documents=ids) for emoji, ids in packs.items()
            ],
            keywords=[],
            documents=documents,
        )

    def _installed_entries(self, kind: str) -> list[Any]:
        out = []
        for name in self.world.installed_sets:
            entry = self.world.sticker_sets.get(name)
            if entry is None:
                continue
            is_emoji = bool(getattr(entry["set"], "emojis", False))
            is_mask = bool(getattr(entry["set"], "masks", False))
            if kind == "emoji" and not is_emoji:
                continue
            if kind == "mask" and not is_mask:
                continue
            if kind == "sticker" and (is_emoji or is_mask):
                continue
            out.append(entry)
        return out

    def _raw_GetAllStickersRequest(self, request: Any) -> Any:
        return types.messages.AllStickers(
            hash=0, sets=[entry["set"] for entry in self._installed_entries("sticker")]
        )

    def _raw_GetMaskStickersRequest(self, request: Any) -> Any:
        return types.messages.AllStickers(
            hash=0, sets=[entry["set"] for entry in self._installed_entries("mask")]
        )

    def _raw_GetEmojiStickersRequest(self, request: Any) -> Any:
        return types.messages.AllStickers(
            hash=0, sets=[entry["set"] for entry in self._installed_entries("emoji")]
        )

    def _raw_GetArchivedStickersRequest(self, request: Any) -> Any:
        entries = [self.world.sticker_sets[n] for n in self.world.archived_sets]
        return types.messages.ArchivedStickers(
            count=len(entries), sets=[self._covered(entry) for entry in entries]
        )

    def _raw_GetFeaturedStickersRequest(self, request: Any) -> Any:
        entries = [self.world.sticker_sets[n] for n in self.world.featured_sets]
        return types.messages.FeaturedStickers(
            hash=0,
            count=len(entries),
            sets=[self._covered(entry) for entry in entries],
            unread=list(self.world.featured_unread),
            premium=False,
        )

    def _raw_GetFeaturedEmojiStickersRequest(self, request: Any) -> Any:
        return self._raw_GetFeaturedStickersRequest(request)

    def _raw_GetOldFeaturedStickersRequest(self, request: Any) -> Any:
        entries = [self.world.sticker_sets[n] for n in self.world.featured_sets]
        return types.messages.FeaturedStickers(
            hash=0,
            count=len(entries),
            sets=[self._covered(entry) for entry in entries],
            unread=[],
            premium=False,
        )

    def _raw_ReadFeaturedStickersRequest(self, request: Any) -> Any:
        self.world.featured_unread = [
            i for i in self.world.featured_unread if i not in set(request.id)
        ]
        return True

    def _raw_InstallStickerSetRequest(self, request: Any) -> Any:
        entry = self.world.set_for(request.stickerset)
        if entry is None:
            from telethon.errors import StickersetInvalidError

            raise StickersetInvalidError(request)
        name = entry["set"].short_name
        target = self.world.archived_sets if request.archived else self.world.installed_sets
        if name not in target:
            target.append(name)
        return types.messages.StickerSetInstallResultSuccess()

    def _raw_UninstallStickerSetRequest(self, request: Any) -> Any:
        entry = self.world.set_for(request.stickerset)
        if entry is not None:
            name = entry["set"].short_name
            if name in self.world.installed_sets:
                self.world.installed_sets.remove(name)
        return True

    def _raw_ToggleStickerSetsRequest(self, request: Any) -> Any:
        for stickerset in request.stickersets:
            entry = self.world.set_for(stickerset)
            if entry is None:
                continue
            name = entry["set"].short_name
            if getattr(request, "archive", False):
                if name in self.world.installed_sets:
                    self.world.installed_sets.remove(name)
                if name not in self.world.archived_sets:
                    self.world.archived_sets.append(name)
            elif getattr(request, "unarchive", False):
                if name in self.world.archived_sets:
                    self.world.archived_sets.remove(name)
                if name not in self.world.installed_sets:
                    self.world.installed_sets.append(name)
            elif getattr(request, "uninstall", False) and name in self.world.installed_sets:
                self.world.installed_sets.remove(name)
        return True

    def _raw_ReorderStickerSetsRequest(self, request: Any) -> Any:
        by_id = {entry["set"].id: name for name, entry in self.world.sticker_sets.items()}
        self.world.installed_sets = [by_id[i] for i in request.order if i in by_id] + [
            n
            for n in self.world.installed_sets
            if self.world.sticker_sets[n]["set"].id not in set(request.order)
        ]
        return True

    def _raw_SearchStickerSetsRequest(self, request: Any) -> Any:
        matches = [
            entry
            for entry in self.world.sticker_sets.values()
            if request.q.lower() in entry["set"].title.lower()
            or request.q.lower() in entry["set"].short_name.lower()
        ]
        return types.messages.FoundStickerSets(
            hash=0, sets=[self._covered(entry) for entry in matches]
        )

    def _raw_SearchEmojiStickerSetsRequest(self, request: Any) -> Any:
        return self._raw_SearchStickerSetsRequest(request)

    def _raw_GetMyStickersRequest(self, request: Any) -> Any:
        entries = [self.world.sticker_sets[n] for n in self.world.my_sets]
        return types.messages.MyStickers(
            count=len(entries), sets=[self._covered(entry) for entry in entries]
        )

    def _raw_SearchStickersRequest(self, request: Any) -> Any:
        stickers = [
            document
            for entry in self.world.sticker_sets.values()
            for document in entry["documents"]
        ]
        return types.messages.FoundStickers(
            hash=0, next_offset=None, stickers=stickers[: request.limit]
        )

    def _raw_GetStickersRequest(self, request: Any) -> Any:
        stickers = [
            document
            for entry in self.world.sticker_sets.values()
            for document in entry["documents"]
            if any(getattr(a, "alt", None) == request.emoticon for a in document.attributes)
        ]
        return types.messages.Stickers(hash=0, stickers=stickers)

    # -- favourites and recents -------------------------------------------

    def _documents_by_id(self, ids: list[int]) -> list[Any]:
        return [self.world.documents[i] for i in ids if i in self.world.documents]

    def _raw_GetFavedStickersRequest(self, request: Any) -> Any:
        return types.messages.FavedStickers(
            hash=0, packs=[], stickers=self._documents_by_id(self.world.faved)
        )

    def _raw_FaveStickerRequest(self, request: Any) -> Any:
        doc_id = int(request.id.id)
        if request.unfave:
            if doc_id in self.world.faved:
                self.world.faved.remove(doc_id)
        else:
            if doc_id not in self.world.faved:
                self.world.faved.append(doc_id)
            while len(self.world.faved) > self.world.faved_limit:
                self.world.faved.pop(0)
        return True

    def _raw_GetRecentStickersRequest(self, request: Any) -> Any:
        return types.messages.RecentStickers(
            hash=0, packs=[], stickers=self._documents_by_id(self.world.recent_stickers), dates=[]
        )

    def _raw_SaveRecentStickerRequest(self, request: Any) -> Any:
        doc_id = int(request.id.id)
        if request.unsave:
            if doc_id in self.world.recent_stickers:
                self.world.recent_stickers.remove(doc_id)
        elif doc_id not in self.world.recent_stickers:
            self.world.recent_stickers.append(doc_id)
        return True

    def _raw_ClearRecentStickersRequest(self, request: Any) -> Any:
        self.world.recent_stickers = []
        return True

    # -- gifs --------------------------------------------------------------

    def _raw_GetSavedGifsRequest(self, request: Any) -> Any:
        return types.messages.SavedGifs(hash=0, gifs=self._documents_by_id(self.world.saved_gifs))

    def _raw_SaveGifRequest(self, request: Any) -> Any:
        doc_id = int(request.id.id)
        if request.unsave:
            if doc_id in self.world.saved_gifs:
                self.world.saved_gifs.remove(doc_id)
        else:
            if doc_id not in self.world.saved_gifs:
                self.world.saved_gifs.append(doc_id)
            while len(self.world.saved_gifs) > self.world.saved_gif_limit:
                self.world.saved_gifs.pop(0)
        return True

    def _raw_SendInlineBotResultRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        message = self.world.add_message(chat_id, "", out=True, sender_id=self.world.me.id)
        return self._updates(message)

    # -- custom emoji ------------------------------------------------------

    def _raw_GetCustomEmojiDocumentsRequest(self, request: Any) -> Any:
        return self._documents_by_id([int(i) for i in request.document_id])

    def _raw_GetEmojiGroupsRequest(self, request: Any) -> Any:
        return types.messages.EmojiGroups(
            hash=0,
            groups=[
                types.EmojiGroup(
                    title="Smileys", icon_emoji_id=1, emoticons=["\U0001f600", "\U0001f603"]
                )
            ],
        )

    def _raw_GetEmojiStickerGroupsRequest(self, request: Any) -> Any:
        return self._raw_GetEmojiGroupsRequest(request)

    def _raw_GetEmojiStatusGroupsRequest(self, request: Any) -> Any:
        return self._raw_GetEmojiGroupsRequest(request)

    def _raw_GetEmojiProfilePhotoGroupsRequest(self, request: Any) -> Any:
        return self._raw_GetEmojiGroupsRequest(request)

    def _raw_GetDefaultProfilePhotoEmojisRequest(self, request: Any) -> Any:
        return types.EmojiList(hash=0, document_id=list(self.world.documents)[:2])

    def _raw_GetDefaultGroupPhotoEmojisRequest(self, request: Any) -> Any:
        return self._raw_GetDefaultProfilePhotoEmojisRequest(request)

    def _raw_GetDefaultBackgroundEmojisRequest(self, request: Any) -> Any:
        return self._raw_GetDefaultProfilePhotoEmojisRequest(request)

    def _raw_GetEmojiKeywordsLanguagesRequest(self, request: Any) -> Any:
        return [types.EmojiLanguage(lang_code="en")]

    def _raw_GetEmojiKeywordsRequest(self, request: Any) -> Any:
        return types.EmojiKeywordsDifference(
            lang_code=request.lang_code,
            from_version=0,
            version=1,
            keywords=[
                types.EmojiKeyword(keyword="grinning", emoticons=["\U0001f600"]),
                types.EmojiKeyword(keyword="cat", emoticons=["\U0001f431"]),
            ],
        )

    def _raw_GetEmojiKeywordsDifferenceRequest(self, request: Any) -> Any:
        return self._raw_GetEmojiKeywordsRequest(request)

    def _raw_GetEmojiURLRequest(self, request: Any) -> Any:
        return types.EmojiURL(url="https://telegram.org/emoji/suggest")

    def _raw_SearchCustomEmojiRequest(self, request: Any) -> Any:
        return types.EmojiList(
            hash=0,
            document_id=[
                document.id
                for entry in self.world.sticker_sets.values()
                for document in entry["documents"]
            ][:2],
        )

    # -- pack authoring ----------------------------------------------------

    def _raw_SuggestShortNameRequest(self, request: Any) -> Any:
        return types.stickers.SuggestedShortName(
            short_name=request.title.lower().replace(" ", "_") + "_by_tlgr"
        )

    def _raw_CheckShortNameRequest(self, request: Any) -> Any:
        return request.short_name not in self.world.sticker_sets

    def _raw_CreateStickerSetRequest(self, request: Any) -> Any:
        documents = [
            self.world.documents.get(int(item.document.id), make_document(int(item.document.id)))
            for item in request.stickers
        ]
        self.world.add_sticker_set(
            request.short_name,
            documents,
            set_id=abs(hash(request.short_name)) % 10**12,
            creator=True,
            emojis=bool(getattr(request, "emojis", False)),
            masks=bool(getattr(request, "masks", False)),
        )
        return self._sticker_set_reply(request.short_name)

    def _raw_AddStickerToSetRequest(self, request: Any) -> Any:
        entry = self.world.set_for(request.stickerset)
        if entry is None:
            from telethon.errors import StickersetInvalidError

            raise StickersetInvalidError(request)
        document = self.world.documents.get(
            int(request.sticker.document.id), make_document(int(request.sticker.document.id))
        )
        entry["documents"].append(document)
        entry["set"].count = len(entry["documents"])
        return self._sticker_set_reply(entry["set"].short_name)

    def _raw_ReplaceStickerRequest(self, request: Any) -> Any:
        old_id = int(request.sticker.id)
        for entry in self.world.sticker_sets.values():
            documents = entry["documents"]
            for index, document in enumerate(documents):
                if document.id == old_id:
                    documents[index] = self.world.documents.get(
                        int(request.new_sticker.document.id),
                        make_document(int(request.new_sticker.document.id)),
                    )
                    return self._sticker_set_reply(entry["set"].short_name)
        from telethon.errors import StickersetInvalidError

        raise StickersetInvalidError(request)

    def _raw_RemoveStickerFromSetRequest(self, request: Any) -> Any:
        doc_id = int(request.sticker.id)
        for entry in self.world.sticker_sets.values():
            before = len(entry["documents"])
            entry["documents"] = [d for d in entry["documents"] if d.id != doc_id]
            if len(entry["documents"]) != before:
                entry["set"].count = len(entry["documents"])
                return self._sticker_set_reply(entry["set"].short_name)
        return True

    def _raw_ChangeStickerPositionRequest(self, request: Any) -> Any:
        return True

    def _raw_ChangeStickerRequest(self, request: Any) -> Any:
        return True

    def _raw_RenameStickerSetRequest(self, request: Any) -> Any:
        entry = self.world.set_for(request.stickerset)
        if entry is None:
            from telethon.errors import StickersetInvalidError

            raise StickersetInvalidError(request)
        entry["set"].title = request.title
        return self._sticker_set_reply(entry["set"].short_name)

    def _raw_SetStickerSetThumbRequest(self, request: Any) -> Any:
        entry = self.world.set_for(request.stickerset)
        if entry is None:
            from telethon.errors import StickersetInvalidError

            raise StickersetInvalidError(request)
        return self._sticker_set_reply(entry["set"].short_name)

    def _raw_DeleteStickerSetRequest(self, request: Any) -> Any:
        entry = self.world.set_for(request.stickerset)
        if entry is None:
            from telethon.errors import StickersetInvalidError

            raise StickersetInvalidError(request)
        name = entry["set"].short_name
        self.world.sticker_sets.pop(name, None)
        for shelf in (self.world.installed_sets, self.world.archived_sets, self.world.my_sets):
            if name in shelf:
                shelf.remove(name)
        return True

    # -- wallpapers --------------------------------------------------------

    def _raw_GetWallPapersRequest(self, request: Any) -> Any:
        return types.account.WallPapers(hash=0, wallpapers=list(self.world.wallpapers.values()))

    def _raw_GetWallPaperRequest(self, request: Any) -> Any:
        slug = getattr(request.wallpaper, "slug", None)
        found = self.world.wallpapers.get(str(slug))
        if found is None:
            from telethon.errors import WallpaperInvalidError

            raise WallpaperInvalidError(request)
        return found

    def _raw_InstallWallPaperRequest(self, request: Any) -> Any:
        slug = getattr(request.wallpaper, "slug", None)
        self.world.installed_wallpaper = str(slug) if slug else "fill"
        if slug and str(slug) not in self.world.saved_wallpapers:
            self.world.saved_wallpapers.append(str(slug))
        return True

    def _raw_SaveWallPaperRequest(self, request: Any) -> Any:
        slug = str(getattr(request.wallpaper, "slug", "") or "")
        if request.unsave:
            if slug in self.world.saved_wallpapers:
                self.world.saved_wallpapers.remove(slug)
        elif slug and slug not in self.world.saved_wallpapers:
            self.world.saved_wallpapers.append(slug)
        return True

    def _raw_ResetWallPapersRequest(self, request: Any) -> Any:
        self.world.saved_wallpapers = []
        self.world.installed_wallpaper = None
        return True

    def _raw_UploadWallPaperRequest(self, request: Any) -> Any:
        wallpaper = make_wallpaper("uploaded", wallpaper_id=777)
        self.world.wallpapers["uploaded"] = wallpaper
        return wallpaper

    # -- settings ----------------------------------------------------------

    def _auto_download(self, name: str) -> Any:
        stored = self.world.auto_download.get(name, {})
        return types.AutoDownloadSettings(
            photo_size_max=stored.get("photo_size_max", 1048576),
            video_size_max=stored.get("video_size_max", 10485760),
            file_size_max=stored.get("file_size_max", 10485760),
            video_upload_maxbitrate=stored.get("video_upload_maxbitrate", 100),
            small_queue_active_operations_max=5,
            large_queue_active_operations_max=2,
            disabled=stored.get("disabled"),
        )

    def _raw_GetAutoDownloadSettingsRequest(self, request: Any) -> Any:
        return types.account.AutoDownloadSettings(
            low=self._auto_download("low"),
            medium=self._auto_download("medium"),
            high=self._auto_download("high"),
        )

    def _raw_SaveAutoDownloadSettingsRequest(self, request: Any) -> Any:
        name = (
            "low"
            if getattr(request, "low", False)
            else ("high" if getattr(request, "high", False) else "medium")
        )
        self.world.auto_download[name] = {
            "photo_size_max": request.settings.photo_size_max,
            "video_size_max": request.settings.video_size_max,
            "file_size_max": request.settings.file_size_max,
            "disabled": request.settings.disabled,
        }
        return True

    def _raw_GetAutoSaveSettingsRequest(self, request: Any) -> Any:
        def rule(name: str) -> Any:
            stored = self.world.auto_save.get(name, {})
            return types.AutoSaveSettings(photos=stored.get("photos"), videos=stored.get("videos"))

        return types.account.AutoSaveSettings(
            users_settings=rule("users"),
            chats_settings=rule("chats"),
            broadcasts_settings=rule("broadcasts"),
            exceptions=[],
            chats=[],
            users=[],
        )

    def _raw_SaveAutoSaveSettingsRequest(self, request: Any) -> Any:
        name = (
            "users"
            if getattr(request, "users", False)
            else "chats"
            if getattr(request, "chats", False)
            else "broadcasts"
            if getattr(request, "broadcasts", False)
            else "peer"
        )
        self.world.auto_save[name] = {
            "photos": request.settings.photos,
            "videos": request.settings.videos,
        }
        return True

    def _raw_DeleteAutoSaveExceptionsRequest(self, request: Any) -> Any:
        self.world.auto_save.pop("peer", None)
        return True

    def _raw_GetContentSettingsRequest(self, request: Any) -> Any:
        return types.account.ContentSettings(
            sensitive_enabled=self.world.sensitive_enabled,
            sensitive_can_change=self.world.sensitive_can_change,
        )

    def _raw_SetContentSettingsRequest(self, request: Any) -> Any:
        self.world.sensitive_enabled = bool(request.sensitive_enabled)
        return True


class _AsyncFailure:
    """An async iterator that raises on the first step.

    `chat posters` has to keep the partial harvest when a flood cuts the scan
    short, and that is only testable if the *iteration* can fail.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __aiter__(self) -> _AsyncFailure:
        return self

    async def __anext__(self) -> Any:
        raise self._error


class _ExportedSender:
    """What `_borrow_exported_sender` hands back: `.send(request)`."""

    def __init__(self, client: FakeTelegramClient) -> None:
        self._client = client

    async def send(self, request: Any) -> Any:
        return await self._client(request)


class _AsyncList:
    """`async for` over a list, which is all `iter_*` needs to be here."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> _AsyncList:
        self._iter = iter(self._items)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _Draft:
    """What `get_drafts()` yields: the draft plus the chat it belongs to."""

    def __init__(self, chat_id: int, draft: Any, entity: Any) -> None:
        self.entity_id = chat_id
        self.entity = entity
        self.draft = draft
        self.text = getattr(draft, "message", "")


class _NullAction:
    """`client.action(...)` as an async context manager that does nothing."""

    async def __aenter__(self) -> _NullAction:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False


class _SentCode:
    """What `auth.sendCode` returns, reduced to what the daemon reads."""

    def __init__(self, phone_code_hash: str, timeout: int) -> None:
        self.phone_code_hash = phone_code_hash
        self.timeout = timeout
        self.type = None
        self.next_type = None


class _QrLogin:
    def __init__(self, world: World) -> None:
        self.world = world
        self.url = "tg://login?token=ZmFrZQ"
        self.token = b"fake"
        self.expires = None

    async def recreate(self) -> str:
        """A QR token lives ~30 s; a client that never re-exports shows a dead one."""
        self.world.calls.append(("qr_recreate", None))
        return self.url

    async def wait(self, timeout: float | None = None) -> Any:
        failure = self.world._fail_next.pop("qr_wait", None)
        if failure is not None:
            raise failure
        self.world.authorized = True
        return self.world.me


class _FakeSession:
    """Enough of `SQLiteSession` that both worlds can read what they read.

    `account export` reads the live session rather than the file on disk, so
    a fake without an auth key would make that operation untestable. `sync
    status`, `sync reset` and `daemon save-state` go through
    `telethon_compat`, which reaches into the session's `update_state` and
    `entities` tables because Telethon 1.44 exposes no accessor for either.
    Faking the tables rather than the compat layer is what makes those tests
    prove the real read path.
    """

    def __init__(self, world: Any = None) -> None:
        from telethon.crypto import AuthKey

        self.saved = 0
        self.dc_id = 4
        self.server_address = "149.154.167.51"
        self.port = 443
        self.auth_key = AuthKey(bytes(range(256)))
        self.takeout_id = None
        self._world = world

    def save(self) -> None:
        self.saved += 1

    def get_update_states(self) -> list[tuple[int, Any]]:
        from datetime import datetime, timezone

        if self._world is None:
            return []
        out = []
        for entity_id, (pts, qts, seq) in self._world.update_state.items():
            out.append(
                (
                    entity_id,
                    types.updates.State(
                        pts=pts,
                        qts=qts,
                        date=datetime(2026, 9, 3, 9, 14, 7, tzinfo=timezone.utc),
                        seq=seq,
                        unread_count=0,
                    ),
                )
            )
        return out

    def set_update_state(self, entity_id: int, state: Any) -> None:
        if self._world is None:
            return
        self._world.update_state[int(entity_id)] = (
            int(getattr(state, "pts", 0) or 0),
            int(getattr(state, "qts", 0) or 0),
            int(getattr(state, "seq", 0) or 0),
        )


def fake_client_factory(world: World | None = None) -> Any:
    """A `client_factory` for `SessionManager` that ignores the session path."""
    shared = world or World()

    def factory(session_path: Any, options: Any) -> FakeTelegramClient:
        return FakeTelegramClient(shared, session=session_path)

    factory.world = shared  # type: ignore[attr-defined]
    return factory
