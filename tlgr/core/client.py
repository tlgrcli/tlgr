"""Telethon client wrapper with optimized configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User

from tlgr.core.errors import (
    AuthenticationError,
    ChatNotFoundError,
    SessionError,
)

DEFAULT_FLOOD_WAIT_MAX = 120


def media_details(media: Any) -> dict[str, Any]:
    """What a media message actually IS, from the attributes it already carries.

    `media_type` says only `MessageMediaDocument`, which is the same label for
    a thumbs-up sticker, a voice note, a video note, a GIF and a PDF — and a
    caption-less one of those is the whole message. For anything judging an
    empty `text` ("did they react, or are they talking to us?") those are
    opposite facts wearing one shape, the same gap `media_type` itself was
    added to close for text-vs-media. The document's own attributes answer it;
    nothing here downloads a byte.

    Returns `{"kind": ...}` plus whatever the attributes make free: a
    sticker's `alt` emoji (which IS its content), an audio/video `duration`,
    a file's `file_name`, and the document `mime_type`.
    """
    out: dict[str, Any] = {}
    if media is None:
        return out
    name = type(media).__name__
    if hasattr(media, "photo") and getattr(media, "photo", None) is not None:
        out["kind"] = "photo"
        return out
    doc = getattr(media, "document", None)
    if doc is None:
        # webpage previews, geo, contacts, polls… — the class name is the
        # only honest answer, minus the MessageMedia prefix.
        out["kind"] = (
            name[len("MessageMedia") :].lower() if name.startswith("MessageMedia") else name
        )
        return out

    mime = getattr(doc, "mime_type", None)
    if mime:
        out["mime_type"] = mime

    # Collect first, decide after: a GIF carries Video AND Animated, and a
    # video sticker carries Video AND Sticker, so "first attribute wins" gets
    # both of them wrong.
    sticker = voice = audio = video = video_note = animated = False
    for attr in getattr(doc, "attributes", None) or []:
        a = type(attr).__name__
        if a == "DocumentAttributeSticker":
            sticker = True
            alt = getattr(attr, "alt", None)
            if alt:
                out["alt"] = alt
        elif a == "DocumentAttributeAudio":
            voice = bool(getattr(attr, "voice", False))
            audio = not voice
            dur = getattr(attr, "duration", None)
            if dur is not None:
                out["duration"] = dur
        elif a == "DocumentAttributeVideo":
            video_note = bool(getattr(attr, "round_message", False))
            video = not video_note
            dur = getattr(attr, "duration", None)
            if dur is not None:
                out["duration"] = dur
        elif a == "DocumentAttributeAnimated":
            animated = True
        elif a == "DocumentAttributeFilename":
            fn = getattr(attr, "file_name", None)
            if fn:
                out["file_name"] = fn

    if sticker:
        out["kind"] = "sticker"
    elif voice:
        out["kind"] = "voice"
    elif video_note:
        out["kind"] = "video_note"
    elif animated or mime == "image/gif":
        out["kind"] = "gif"
    elif video:
        out["kind"] = "video"
    elif audio:
        out["kind"] = "audio"
    else:
        out["kind"] = "file"
    return out


def create_client(
    session_path: Path,
    api_id: int,
    api_hash: str,
    flood_wait_max: int = DEFAULT_FLOOD_WAIT_MAX,
) -> TelegramClient:
    return TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        flood_sleep_threshold=flood_wait_max,
        request_retries=5,
        connection_retries=5,
        retry_delay=1,
        auto_reconnect=True,
        sequential_updates=True,
    )


class ClientWrapper:
    def __init__(
        self,
        session_path: Path,
        api_id: int,
        api_hash: str,
        flood_wait_max: int = DEFAULT_FLOOD_WAIT_MAX,
    ):
        self.session_path = session_path
        self.api_id = api_id
        self.api_hash = api_hash
        self.flood_wait_max = flood_wait_max
        self._client: TelegramClient | None = None
        self._me: User | None = None

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise SessionError("Client not initialised. Call connect() first.")
        return self._client

    @property
    def me(self) -> User:
        if self._me is None:
            raise SessionError("Not logged in.")
        return self._me

    @property
    def is_connected(self) -> bool:
        """Whether the underlying client currently holds a live connection.

        A wrapper outlives its connection: when Telethon exhausts its reconnect
        budget it raises and leaves this object in place, so the wrapper existing
        says nothing about whether Telegram is reachable. Every send through it
        will fail with "Cannot send requests while disconnected" until it is
        reconnected. Ask this, not `client is not None`.
        """
        return self._client is not None and self._client.is_connected()

    async def connect(self) -> bool:
        """Connect. Returns True if already authorised."""
        self._client = create_client(
            self.session_path, self.api_id, self.api_hash, self.flood_wait_max
        )
        await self._client.connect()
        if await self._client.is_user_authorized():
            self._me = await self._client.get_me()
            return True
        return False

    async def login(
        self,
        phone: str | None = None,
        code_callback=None,
        password_callback=None,
    ) -> User:
        if self._client is None:
            await self.connect()
        try:
            if phone is None:
                phone = input("Phone number (with country code): ").strip()
            await self._client.send_code_request(phone)  # type: ignore[union-attr]
            code = code_callback() if code_callback else input("Verification code: ").strip()
            try:
                await self._client.sign_in(phone, code)  # type: ignore[union-attr]
            except SessionPasswordNeededError:
                import getpass

                password = (
                    password_callback() if password_callback else getpass.getpass("2FA password: ")
                )
                await self._client.sign_in(password=password)  # type: ignore[union-attr]
            self._me = await self._client.get_me()  # type: ignore[union-attr]
            return self._me
        except Exception as e:
            raise AuthenticationError(f"Login failed: {e}")

    async def logout(self) -> None:
        if self._client:
            try:
                await self._client.log_out()
            except Exception:
                pass
            await self._client.disconnect()
        self._client = None
        self._me = None

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()

    async def resolve_chat(self, chat_ref: str) -> int:
        """Resolve @username or numeric id to peer id."""
        try:
            return int(chat_ref)
        except ValueError:
            pass
        if not chat_ref.startswith("@"):
            chat_ref = f"@{chat_ref}"
        try:
            entity = await self.client.get_entity(chat_ref)
            return utils.get_peer_id(entity)
        except Exception as e:
            raise ChatNotFoundError(f"Cannot resolve '{chat_ref}': {e}")

    def _entity_to_dict(self, entity: Any, dialog: Any = None) -> dict[str, Any]:
        if isinstance(entity, User):
            if entity.is_self:
                t, name = "saved", "Saved Messages"
            else:
                t = "bot" if entity.bot else "user"
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            info = {
                "id": dialog.id if dialog else entity.id,
                "name": name,
                "type": t,
                "username": entity.username,
            }
        elif isinstance(entity, Chat):
            info = {
                "id": dialog.id if dialog else entity.id,
                "name": entity.title,
                "type": "group",
                "username": None,
            }
        elif isinstance(entity, Channel):
            t = "channel" if not entity.megagroup else "supergroup"
            info = {
                "id": dialog.id if dialog else entity.id,
                "name": entity.title,
                "type": t,
                "username": entity.username,
            }
        else:
            info = {
                "id": dialog.id if dialog else getattr(entity, "id", 0),
                "name": str(dialog.name) if dialog else str(entity),
                "type": "unknown",
                "username": None,
            }
        return info

    @staticmethod
    def _reactions_summary(msg: Any) -> dict[str, Any] | None:
        """Compact reaction state, including whether WE already reacted.

        Returns None when the message carries no reactions, so the field only
        appears where it means something. Shape:

            {"counts": {"❤": 2, "👍": 1}, "mine": ["❤"]}

        `mine` is the part that matters to a caller deciding whether to react:
        Telegram answers a duplicate reaction with MESSAGE_NOT_MODIFIED, which
        surfaces as a generic error, so without this field the only way to learn
        that a reaction is already there is to send one and read the failure.
        It is derived from ReactionCount.chosen_order, which Telegram sets only
        on the reactions this account made.
        """
        r = getattr(msg, "reactions", None)
        if not r:
            return None
        counts: dict[str, int] = {}
        mine: list[str] = []
        for rc in getattr(r, "results", None) or []:
            reaction = getattr(rc, "reaction", None)
            # Emoji reactions carry .emoticon; custom (premium) ones carry only
            # a document id, so name them rather than dropping them silently.
            emoji = getattr(reaction, "emoticon", None)
            if emoji is None:
                doc = getattr(reaction, "document_id", None)
                emoji = f"custom:{doc}" if doc is not None else "?"
            counts[emoji] = counts.get(emoji, 0) + int(getattr(rc, "count", 0) or 0)
            if getattr(rc, "chosen_order", None) is not None:
                mine.append(emoji)
        if not counts:
            return None
        return {"counts": counts, "mine": mine}

    async def get_messages(
        self,
        chat_id: int | str,
        *,
        limit: int = 20,
        offset_id: int = 0,
        include_sender: bool = False,
        include_media: bool = False,
        include_reactions: bool = False,
        include_entities: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async for msg in self.client.iter_messages(chat_id, limit=limit, offset_id=offset_id):
            d: dict[str, Any] = {
                "id": msg.id,
                "date": str(msg.date),
                "text": msg.text or "",
                "out": bool(getattr(msg, "out", False)),
                "reply_to": getattr(msg, "reply_to_msg_id", None),
            }
            action = getattr(msg, "action", None)
            if action is not None:
                d["service"] = type(action).__name__
            if getattr(msg, "media", None) is not None:
                d["media_type"] = type(msg.media).__name__
                d.update({"media_" + k: v for k, v in media_details(msg.media).items()})
            if include_sender and msg.sender:
                d["sender"] = {
                    "id": msg.sender_id,
                    "name": getattr(msg.sender, "first_name", None)
                    or getattr(msg.sender, "title", ""),
                    "username": getattr(msg.sender, "username", None),
                }
            if include_media and msg.media:
                d["media"] = {
                    "type": type(msg.media).__name__,
                    "has_file": hasattr(msg.media, "document") or hasattr(msg.media, "photo"),
                    **media_details(msg.media),
                }
            # Always present when the message has reactions: a caller cannot
            # opt into a field it does not know to ask for, and "have we already
            # reacted?" is not an optional detail for anything that reacts.
            summary = self._reactions_summary(msg)
            if summary is not None:
                d["reactions"] = summary
            if include_reactions and getattr(msg, "reactions", None):
                d["reactions_raw"] = str(msg.reactions)
            if include_entities and msg.entities:
                d["entities"] = [
                    {"type": type(e).__name__, "offset": e.offset, "length": e.length}
                    for e in msg.entities
                ]
            result.append(d)
        return result

    async def get_message(self, chat_id: int | str, msg_id: int) -> dict[str, Any]:
        msgs = await self.client.get_messages(chat_id, ids=[msg_id])
        if not msgs or msgs[0] is None:
            raise ChatNotFoundError(f"Message {msg_id} not found")
        msg = msgs[0]
        d: dict[str, Any] = {
            "id": msg.id,
            "date": str(msg.date),
            "text": msg.text or "",
            "out": bool(getattr(msg, "out", False)),
            "reply_to": getattr(msg, "reply_to_msg_id", None),
        }
        action = getattr(msg, "action", None)
        if action is not None:
            d["service"] = type(action).__name__
        if getattr(msg, "media", None) is not None:
            d["media_type"] = type(msg.media).__name__
            d.update({"media_" + k: v for k, v in media_details(msg.media).items()})
        summary = self._reactions_summary(msg)
        if summary is not None:
            d["reactions"] = summary
        if msg.sender:
            d["sender"] = {
                "id": msg.sender_id,
                "name": getattr(msg.sender, "first_name", None) or getattr(msg.sender, "title", ""),
                "username": getattr(msg.sender, "username", None),
            }
        if msg.media:
            d["media"] = {
                "type": type(msg.media).__name__,
                **media_details(msg.media),
            }
        if msg.entities:
            d["entities"] = [
                {"type": type(e).__name__, "offset": e.offset, "length": e.length}
                for e in msg.entities
            ]
        if getattr(msg, "reactions", None):
            d["reactions_raw"] = str(msg.reactions)
        if msg.reply_to:
            d["reply_to_msg_id"] = msg.reply_to.reply_to_msg_id
        if msg.forward:
            d["forward"] = True
        return d

    async def react_to_message(self, chat_id: int | str, msg_id: int, emoji: str) -> dict[str, Any]:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        try:
            await self.client(
                SendReactionRequest(
                    peer=chat_id,
                    msg_id=msg_id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                )
            )
        except Exception as e:
            # Telegram answers a reaction that is already there with
            # MESSAGE_NOT_MODIFIED. That is the desired end state, not a
            # failure — reporting it as a generic error made the only way to
            # ask "did we already react?" look like a broken send.
            if "not modified" not in str(e).lower():
                raise
            return {"reacted": True, "msg_id": msg_id, "emoji": emoji, "already": True}
        return {"reacted": True, "msg_id": msg_id, "emoji": emoji, "already": False}

    async def list_participants(
        self,
        chat_id: int | str,
        *,
        limit: int | None = None,
        admins_only: bool = False,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        from telethon.tl.types import ChannelParticipantsAdmins

        kwargs: dict[str, Any] = {}
        if admins_only:
            kwargs["filter"] = ChannelParticipantsAdmins
        if search:
            kwargs["search"] = search
        users: list[dict[str, Any]] = []
        async for u in self.client.iter_participants(chat_id, limit=limit, **kwargs):
            if not isinstance(u, User):
                continue
            users.append(
                {
                    "id": u.id,
                    "first_name": u.first_name or "",
                    "last_name": u.last_name or "",
                    "username": u.username,
                    "is_bot": bool(u.bot),
                    "is_deleted": bool(u.deleted),
                    "is_contact": bool(u.contact),
                    "is_self": bool(u.is_self),
                }
            )
        return users

    async def create_chat(
        self,
        name: str,
        *,
        chat_type: str = "group",
        members: list[str] | None = None,
    ) -> dict[str, Any]:
        if chat_type == "channel":
            from telethon.tl.functions.channels import CreateChannelRequest

            result = await self.client(
                CreateChannelRequest(
                    title=name,
                    about="",
                    megagroup=False,
                )
            )
            ch = result.chats[0]
            return {"id": utils.get_peer_id(ch), "name": name, "type": "channel"}
        else:
            users = members or []
            result = await self.client.create_group(name, users)
            return {"id": result.id if hasattr(result, "id") else 0, "name": name, "type": "group"}

    async def get_profile(self) -> dict[str, Any]:
        me = await self.client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "username": me.username,
            "phone": me.phone,
            "bio": "",
        }

    async def update_profile(
        self,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        bio: str | None = None,
        photo: str | None = None,
    ) -> dict[str, Any]:
        from telethon.tl.functions.account import UpdateProfileRequest

        kwargs: dict[str, Any] = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
        if last_name is not None:
            kwargs["last_name"] = last_name
        if bio is not None:
            kwargs["about"] = bio
        if kwargs:
            await self.client(UpdateProfileRequest(**kwargs))
        if photo:
            await self.client.upload_profile_photo(file=photo)
        return {"updated": True}

    # ------------------------------------------------------------------
    # Authoritative history / harvest primitives
    # ------------------------------------------------------------------
