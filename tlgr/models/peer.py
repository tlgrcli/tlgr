"""Peers: how one is addressed on input (`PeerRef`) and identified on output.

Parsing lives here rather than in the CLI because the daemon has to accept the
same vocabulary over the wire, and because "is this a channel id?" must have
exactly one answer in the codebase. Nothing here touches the network: a
`PeerRef` is a *syntactic* classification, resolution happens later against a
connected account.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from tlgr.models.base import Model

__all__ = [
    "Chat",
    "Peer",
    "PeerKind",
    "PeerRef",
    "PeerRefKind",
    "Photo",
    "Rights",
    "User",
    "UserRef",
    "parse_message_link",
    "parse_peer_ref",
    "parse_user_ref",
]

PeerKind = Literal["user", "bot", "saved", "group", "supergroup", "channel", "unknown"]
PeerRefKind = Literal["id", "username", "phone", "link", "invite", "self", "saved"]

# Telegram's own marking scheme, reproduced here so models/ stays Telethon-free.
_CHANNEL_MARK = -1000000000000
_MAX_USER_ID = 0x7FFFFFFFFFFFFFFF

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{4,32}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-()]{4,24}$")
_INT_RE = re.compile(r"^-?\d+$")


class PeerRefError(ValueError):
    """A peer reference that cannot be parsed at all.

    Raised as a plain ValueError subclass so that `models/` keeps importing
    nothing from tlgr; `core.errors.classify` maps it to USAGE.
    """


class PeerRef(Model):
    """How a peer is *addressed* on input.

    `raw` is exactly what the user typed, kept so that an error message can
    quote it back; `kind` and `value` are the normalised form the resolver
    actually uses.
    """

    raw: str
    kind: PeerRefKind
    value: str | int


UserRef = PeerRef
"""Same shape, narrower parser (`parse_user_ref`): channel ids are refused."""


class Photo(Model):
    id: int
    has_video: bool = False
    stripped_thumb_b64: str | None = None
    dc_id: int | None = None


class Peer(Model):
    """How a peer is *identified* on output.

    `id` is always the marked id and `raw_id`/`kind` sit next to it, so no
    consumer ever has to parse the sign to learn what it is holding (COR-10).
    """

    id: int
    raw_id: int
    kind: PeerKind
    title: str = ""
    username: str | None = None
    usernames: list[str] = []
    is_self: bool = False


class User(Model):
    id: int
    raw_id: int
    kind: Literal["user", "bot"] = "user"
    first_name: str | None = None
    last_name: str | None = None
    title: str = ""
    username: str | None = None
    usernames: list[str] = []
    phone: str | None = None
    is_self: bool = False
    is_contact: bool = False
    is_mutual_contact: bool = False
    is_deleted: bool = False
    is_bot: bool = False
    is_verified: bool = False
    is_scam: bool = False
    is_fake: bool = False
    is_premium: bool = False
    is_support: bool = False
    is_blocked: bool | None = None
    restricted: bool = False
    restriction_reason: list[str] = []
    lang_code: str | None = None
    status: str | None = None
    status_expires: str | None = None
    last_seen: str | None = None
    stories_hidden: bool = False
    emoji_status_id: int | None = None
    photo: Photo | None = None
    # Full-profile fields: only populated when the op fetched users.getFullUser.
    bio: str | None = None
    birthday: str | None = None
    common_chats_count: int | None = None
    personal_channel_id: int | None = None
    business_hours: dict[str, Any] | None = None
    min: bool = False


class Rights(Model):
    """Flattened ChatAdminRights / ChatBannedRights.

    `True` means *allowed*, everywhere, including for banned rights. Telegram
    stores banned rights inverted (`send_messages=True` means "cannot send");
    normalising once here is the difference between one confusing field and a
    confusing field per caller.
    """

    change_info: bool | None = None
    post_messages: bool | None = None
    edit_messages: bool | None = None
    delete_messages: bool | None = None
    ban_users: bool | None = None
    invite_users: bool | None = None
    pin_messages: bool | None = None
    add_admins: bool | None = None
    manage_call: bool | None = None
    manage_topics: bool | None = None
    post_stories: bool | None = None
    edit_stories: bool | None = None
    delete_stories: bool | None = None
    manage_direct_messages: bool | None = None
    manage_ranks: bool | None = None
    anonymous: bool | None = None
    other: bool | None = None
    view_messages: bool | None = None
    send_messages: bool | None = None
    send_media: bool | None = None
    send_photos: bool | None = None
    send_videos: bool | None = None
    send_audios: bool | None = None
    send_voices: bool | None = None
    send_roundvideos: bool | None = None
    send_docs: bool | None = None
    send_stickers: bool | None = None
    send_gifs: bool | None = None
    send_games: bool | None = None
    send_inline: bool | None = None
    send_polls: bool | None = None
    send_plain: bool | None = None
    send_reactions: bool | None = None
    embed_links: bool | None = None
    edit_rank: bool | None = None
    until: str | None = None
    until_unix: int | None = None


class Chat(Model):
    id: int
    raw_id: int
    kind: PeerKind
    title: str = ""
    username: str | None = None
    usernames: list[str] = []
    is_creator: bool = False
    is_admin: bool = False
    is_broadcast: bool = False
    is_forum: bool = False
    is_gigagroup: bool = False
    is_verified: bool = False
    is_scam: bool = False
    is_fake: bool = False
    is_restricted: bool = False
    noforwards: bool = False
    join_to_send: bool | None = None
    join_request: bool | None = None
    signatures: bool | None = None
    slowmode_seconds: int | None = None
    slowmode_next_send: str | None = None
    participants_count: int | None = None
    online_count: int | None = None
    photo: Photo | None = None
    date: str | None = None
    left: bool = False
    # Full-chat fields: only from channels.getFullChannel / messages.getFullChat.
    about: str | None = None
    pinned_message_id: int | None = None
    linked_chat_id: int | None = None
    migrated_from_chat_id: int | None = None
    available_reactions: list[str] | None = None
    default_rights: Rights | None = None
    my_rights: Rights | None = None
    ttl_period: int | None = None
    stats_dc: int | None = None
    can_view_participants: bool | None = None
    hidden_prehistory: bool | None = None
    antispam: bool | None = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _strip_scheme(text: str) -> str | None:
    """Return the path/query part of a t.me or tg:// reference, else None."""
    low = text.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            text = text[len(prefix) :]
            low = text.lower()
    if low.startswith("tg://"):
        return text[len("tg://") :]
    for host in ("t.me/", "telegram.me/", "telegram.dog/"):
        if low.startswith(host):
            return text[len(host) :]
    return None


def _query_value(query: str, key: str) -> str | None:
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name.lower() == key:
            return value
    return None


def _parse_link(rest: str, raw: str) -> PeerRef:
    """Classify the part of a t.me/tg:// reference after the host."""
    head, _, query = rest.partition("?")
    head = head.strip("/")

    # tg://resolve?domain=x  /  tg://join?invite=hash
    if query and head.lower() in ("resolve", "join", "privatepost", "openmessage"):
        verb = head.lower()
        if verb == "resolve":
            domain = _query_value(query, "domain")
            if domain:
                return PeerRef(raw=raw, kind="username", value=domain.lower())
        elif verb == "join":
            invite = _query_value(query, "invite")
            if invite:
                return PeerRef(raw=raw, kind="invite", value=invite)
        elif verb == "privatepost":
            channel = _query_value(query, "channel")
            if channel and _INT_RE.match(channel):
                return PeerRef(raw=raw, kind="id", value=_mark_channel(int(channel)))
        elif verb == "openmessage":
            user = _query_value(query, "user_id")
            if user and _INT_RE.match(user):
                return PeerRef(raw=raw, kind="id", value=int(user))
        return PeerRef(raw=raw, kind="link", value=rest)

    segments = [s for s in head.split("/") if s]
    if not segments:
        raise PeerRefError(f"{raw!r} is not a usable Telegram reference")

    first = segments[0]
    if first.startswith("+") or first.lower() == "joinchat":
        # t.me/+AbCdEf  and the older t.me/joinchat/AbCdEf
        invite = first[1:] if first.startswith("+") else (segments[1] if len(segments) > 1 else "")
        if not invite:
            raise PeerRefError(f"{raw!r} has no invite hash")
        # A `+` followed by digits only is a phone link, not an invite hash.
        if first.startswith("+") and invite.isdigit():
            return PeerRef(raw=raw, kind="phone", value="+" + invite)
        return PeerRef(raw=raw, kind="invite", value=invite)

    if first.lower() == "c" and len(segments) > 1 and _INT_RE.match(segments[1]):
        # t.me/c/<internal channel id>/<message id>
        return PeerRef(raw=raw, kind="id", value=_mark_channel(int(segments[1])))

    if first.lower() == "s" and len(segments) > 1:
        first = segments[1]  # t.me/s/<username> is the preview view of a channel

    if _USERNAME_RE.match(first):
        return PeerRef(raw=raw, kind="username", value=first.lower())

    return PeerRef(raw=raw, kind="link", value=rest)


def _mark_channel(raw_id: int) -> int:
    """Turn an unmarked channel id into the marked form Telegram links use."""
    return _CHANNEL_MARK - abs(raw_id) if raw_id > 0 else raw_id


def parse_peer_ref(text: str) -> PeerRef:
    """Classify a chat/user reference. Syntax only — nothing is resolved here.

    Accepts `@username`, a marked or raw id (including the `-100…` channel
    form, and negative ids that need a `--` separator on the command line),
    `+phone`, `me`/`saved`, and t.me / tg:// links and invites.
    """
    raw = text.strip()
    if not raw:
        raise PeerRefError("empty peer reference")

    low = raw.lower()
    if low in ("me", "self"):
        return PeerRef(raw=raw, kind="self", value="me")
    if low in ("saved", "savedmessages", "saved_messages"):
        return PeerRef(raw=raw, kind="saved", value="saved")

    link_rest = _strip_scheme(raw)
    if link_rest is not None:
        return _parse_link(link_rest, raw)

    if raw.startswith("@"):
        name = raw[1:]
        if not _USERNAME_RE.match(name):
            raise PeerRefError(f"{raw!r} is not a valid username")
        return PeerRef(raw=raw, kind="username", value=name.lower())

    if _INT_RE.match(raw):
        value = int(raw)
        if value == 0:
            raise PeerRefError("0 is not a peer id")
        return PeerRef(raw=raw, kind="id", value=value)

    if raw.startswith("+") and _PHONE_RE.match(raw):
        digits = re.sub(r"[^0-9]", "", raw)
        if not digits:
            raise PeerRefError(f"{raw!r} is not a valid phone number")
        return PeerRef(raw=raw, kind="phone", value="+" + digits)

    if _USERNAME_RE.match(raw):
        # A bare username is accepted because half the world types it that way.
        return PeerRef(raw=raw, kind="username", value=raw.lower())

    raise PeerRefError(f"{raw!r} is not a chat, user, id, phone or link")


def parse_user_ref(text: str) -> PeerRef:
    """`parse_peer_ref` restricted to things that can name a user.

    A channel or group id here is almost always a copy/paste mistake, and
    saying so is more useful than letting the resolver fail with
    PEER_ID_INVALID three layers later.
    """
    ref = parse_peer_ref(text)
    if ref.kind == "id" and isinstance(ref.value, int) and ref.value < 0:
        raise PeerRefError(
            f"{ref.raw!r} is a group or channel id; this argument wants a user "
            "(@username, numeric user id, +phone or 'me')"
        )
    if ref.kind == "invite":
        raise PeerRefError(f"{ref.raw!r} is a chat invite link; this argument wants a user")
    return ref


def parse_message_link(text: str) -> tuple[PeerRef, int] | None:
    """Split a t.me message link into its chat and message id, or None.

    This is what lets a `<chat> <msg-id>` pair be given as a single pasted
    link anywhere in the CLI (STYLE §2).
    """
    rest = _strip_scheme(text.strip())
    if rest is None:
        return None
    head, _, query = rest.partition("?")
    segments = [s for s in head.strip("/").split("/") if s]

    if head.lower().strip("/") == "privatepost" and query:
        channel = _query_value(query, "channel")
        post = _query_value(query, "post")
        if channel and post and _INT_RE.match(channel) and _INT_RE.match(post):
            return PeerRef(raw=text, kind="id", value=_mark_channel(int(channel))), int(post)
        return None

    if len(segments) >= 3 and segments[0].lower() == "c" and _INT_RE.match(segments[1]):
        if _INT_RE.match(segments[2]):
            ref = PeerRef(raw=text, kind="id", value=_mark_channel(int(segments[1])))
            return ref, int(segments[2])
        return None

    if len(segments) >= 2 and _USERNAME_RE.match(segments[0]) and _INT_RE.match(segments[1]):
        return PeerRef(raw=text, kind="username", value=segments[0].lower()), int(segments[1])

    return None
