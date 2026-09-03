"""Telethon client wrapper with optimized configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User

from tlgr.core.errors import (
    AuthenticationError,
    ChatNotFoundError,
    SessionError,
    TlgrError,
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

    async def list_contacts(self) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await self.client(GetContactsRequest(hash=0))
        contacts: list[dict[str, Any]] = []
        for u in result.users:
            contacts.append(
                {
                    "id": u.id,
                    "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
                    "username": u.username,
                    "phone": u.phone,
                }
            )
        return contacts

    async def add_contact(self, phone: str, name: str = "") -> dict[str, Any]:
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        parts = name.split(maxsplit=1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        result = await self.client(
            ImportContactsRequest(
                [InputPhoneContact(client_id=0, phone=phone, first_name=first, last_name=last)]
            )
        )
        imported = result.imported
        if imported:
            return {"added": True, "user_id": imported[0].user_id}
        return {"added": False, "error": "Could not import contact"}

    async def rename_contact(
        self,
        user_ref: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict[str, Any]:
        """Save a user as a contact with the given name.

        Works on any user (also non-contacts, e.g. to tag them). Omitted
        name parts keep the user's current profile name.
        """
        from telethon.tl.functions.contacts import AddContactRequest

        entity = await self.client.get_entity(user_ref)
        if not isinstance(entity, User):
            raise TlgrError(f"'{user_ref}' is not a user")
        first = first_name if first_name is not None else (entity.first_name or "")
        last = last_name if last_name is not None else (entity.last_name or "")
        if not first:
            first = "."
        await self.client(
            AddContactRequest(
                id=entity,
                first_name=first,
                last_name=last,
                phone=getattr(entity, "phone", None) or "",
                add_phone_privacy_exception=False,
            )
        )
        return {"saved": True, "user_id": entity.id, "first_name": first, "last_name": last}

    async def remove_contact(self, user_ref: str) -> dict[str, Any]:
        from telethon.tl.functions.contacts import DeleteContactsRequest

        entity = await self.client.get_entity(user_ref)
        await self.client(DeleteContactsRequest(id=[entity]))
        return {"removed": True}

    async def search_contacts(self, query: str) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import SearchRequest

        result = await self.client(SearchRequest(q=query, limit=50))
        return [
            {
                "id": u.id,
                "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
                "username": u.username,
            }
            for u in result.users
        ]

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

    async def download_media(
        self,
        chat_id: int | str,
        msg_id: int,
        *,
        out_dir: str | None = None,
    ) -> dict[str, Any]:
        from tlgr.core.config import get_downloads_dir

        msgs = await self.client.get_messages(chat_id, ids=[msg_id])
        if not msgs or msgs[0] is None:
            raise ChatNotFoundError(f"Message {msg_id} not found")
        msg = msgs[0]
        if not msg.media:
            raise TlgrError("Message has no media")
        dl_dir = Path(out_dir) if out_dir else get_downloads_dir()
        dl_dir.mkdir(parents=True, exist_ok=True)
        path = await self.client.download_media(msg, file=str(dl_dir))
        return {"path": str(path), "msg_id": msg_id}

    async def upload_file(
        self,
        chat_id: int | str,
        file_path: str,
        *,
        caption: str = "",
    ) -> dict[str, Any]:
        msg = await self.client.send_file(chat_id, file_path, caption=caption)
        return {"id": msg.id, "chat_id": chat_id}

    async def get_user_info(self, user_ref: str) -> dict[str, Any]:
        """Get detailed info about a user."""
        from telethon.tl.functions.users import GetFullUserRequest

        entity = await self.client.get_entity(user_ref)
        if not isinstance(entity, User):
            return self._entity_to_dict(entity)

        try:
            full = await self.client(GetFullUserRequest(entity))
            user = full.users[0] if full.users else entity
            about = full.full_user.about or ""
        except Exception:
            user = entity
            about = ""

        status_str = ""
        if hasattr(user, "status") and user.status:
            status_str = type(user.status).__name__.replace("UserStatus", "").lower()

        return {
            "id": user.id,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username,
            "phone": user.phone,
            "bio": about,
            "is_bot": getattr(user, "bot", False),
            "status": status_str,
            # no photo + status "empty" together is the classic signature of
            # an account that blocked you (or an abandoned account)
            "has_photo": getattr(user, "photo", None) is not None,
            "deleted": getattr(user, "deleted", False),
            # Whether THIS account has archived their stories (Telegram's own
            # "Hide Stories" menu item). Read-only here; set it with
            # set_stories_hidden(). Reported so the state is checkable without
            # a write — a toggle you can only set is a toggle you cannot audit.
            "stories_hidden": bool(getattr(user, "stories_hidden", False)),
        }

    async def set_stories_hidden(self, user_ref: int | str, hidden: bool = True) -> dict[str, Any]:
        """Archive (or unarchive) a peer's stories for this account.

        Exactly Telegram's own "Hide Stories" context-menu item: the peer moves
        out of the main stories bar into the collapsed "Hidden" list. It is a
        purely local, per-account preference — the other side is not notified
        and nothing about the chat changes — which is why it is safe to apply
        in bulk to people an outreach campaign has contacted.

        Reports `already` when the flag was already in the requested state, so
        a bulk pass over hundreds of peers costs one RPC each on the first run
        and none on every run after. `stories_hidden` comes from the fresh User
        the resolve returns, not from the session cache.
        """
        from telethon.tl.functions.stories import TogglePeerStoriesHiddenRequest

        entity = await self.client.get_entity(user_ref)
        if not isinstance(entity, User):
            raise TlgrError(f"'{user_ref}' is not a user")
        was = bool(getattr(entity, "stories_hidden", False))
        if was == hidden:
            return {
                "user_id": entity.id,
                "username": entity.username,
                "hidden": hidden,
                "already": True,
            }
        await self.client(TogglePeerStoriesHiddenRequest(peer=entity, hidden=hidden))
        return {
            "user_id": entity.id,
            "username": entity.username,
            "hidden": hidden,
            "already": False,
        }

    # ------------------------------------------------------------------
    # Authoritative history / harvest primitives
    # ------------------------------------------------------------------

    async def dialog_status(
        self,
        user_ref: int | str,
        *,
        max_dialogs: int = 5000,
    ) -> dict[str, Any]:
        """Answer "does THIS account have a dialog with this peer?" — or admit
        it cannot tell.

        The naive probe (list a few messages and read the error) is unsound for
        a *bare numeric id*: `get_input_entity` only consults the local entity
        cache and, for a non-bot account, the network fallback
        (`users.GetUsers` with access_hash=0) returns `UserEmpty` for anyone
        who is not already a contact. So a cold cache raises "Could not find
        the input entity" for ids the account demonstrably HAS talked to, and
        that error is indistinguishable from a genuinely unknown peer. Callers
        that read it as "no history" will happily cold-message someone twice.

        There is no MTProto call that resolves a bare user id to an access
        hash. What *is* server-side and authoritative is the dialog list
        itself, so:

          1. try to get an input peer cheaply (cache / disk / username
             resolution) and, if that works, ask the server directly with
             `messages.GetPeerDialogs` plus an exact server-side message total;
          2. if the peer cannot be resolved, enumerate the account's *complete*
             dialog list from the server and look for the id. Finding it is a
             positive; **exhausting** it is the only thing that licenses a
             negative;
          3. if neither completes — cap hit, FloodWait, RPC failure — report
             ``resolved: false`` and let the caller fail closed. An honest
             "I don't know" is the whole point of this command.

        Caveat, deliberately not papered over: this reports on the dialog
        list. If the account *deleted* the conversation, the history is gone
        server-side too and this correctly says there is no dialog.
        """
        from telethon.tl.functions.messages import GetPeerDialogsRequest
        from telethon.tl.types import InputDialogPeer

        out: dict[str, Any] = {
            "ref": user_ref,
            "id": None,
            "username": None,
            "resolved": False,
            "has_dialog": None,
            "message_count": None,
            "source": "unknown",
            "reason": None,
        }

        target_id: int | None = None
        target_username: str | None = None
        if isinstance(user_ref, int):
            target_id = user_ref
        elif isinstance(user_ref, str):
            s = user_ref.strip()
            if s.lstrip("-").isdigit():
                target_id = int(s)
            else:
                target_username = s.lstrip("@").lower()

        peer: Any = None
        try:
            peer = await self.client.get_input_entity(user_ref)
        except FloodWaitError as e:
            out["reason"] = f"rate limited while resolving entity (wait {e.seconds}s)"
            return out
        except Exception as e:
            # NOT evidence of absence — just a cold cache or an unknown handle.
            out["reason"] = f"entity not resolvable directly: {e}"

        scanned = 0
        if peer is None:
            if target_id is None and target_username is None:
                out["reason"] = f"unusable reference: {user_ref!r}"
                return out
            try:
                async for dialog in self.client.iter_dialogs():
                    scanned += 1
                    ent = dialog.entity
                    ent_id = getattr(ent, "id", None)
                    dlg_id = getattr(dialog, "id", None)
                    uname = (getattr(ent, "username", None) or "").lower()
                    if (target_id is not None and target_id in (ent_id, dlg_id)) or (
                        target_username is not None and uname == target_username
                    ):
                        peer = ent
                        out["source"] = "dialog_scan"
                        break
                    if scanned >= max_dialogs:
                        out["scanned_dialogs"] = scanned
                        out["reason"] = (
                            f"dialog scan hit the {max_dialogs}-dialog cap without a "
                            "match — indeterminate, NOT a negative"
                        )
                        return out
            except Exception as e:
                out["scanned_dialogs"] = scanned
                out["reason"] = f"dialog scan did not complete: {e}"
                return out

            out["scanned_dialogs"] = scanned
            if peer is None:
                # The server handed us every dialog this account has and the
                # peer was not among them. This is the definitive negative.
                out.update(
                    resolved=True,
                    has_dialog=False,
                    message_count=0,
                    source="dialog_scan",
                    reason="absent from the account's complete dialog list",
                )
                out["id"] = target_id
                out["username"] = target_username
                return out

        try:
            input_peer = await self.client.get_input_entity(peer)
            res = await self.client(GetPeerDialogsRequest(peers=[InputDialogPeer(peer=input_peer)]))
            dialogs = list(getattr(res, "dialogs", []) or [])
            top = max((getattr(d, "top_message", 0) or 0) for d in dialogs) if dialogs else 0
            msgs = await self.client.get_messages(input_peer, limit=1)
            total = getattr(msgs, "total", None)
            if total is None:
                total = len(msgs)
            total = int(total)
        except FloodWaitError as e:
            out["reason"] = f"rate limited while querying the server (wait {e.seconds}s)"
            return out
        except Exception as e:
            out["reason"] = f"server dialog query failed: {e}"
            return out

        pid = getattr(peer, "user_id", None)
        if pid is None:
            pid = (
                getattr(peer, "channel_id", None)
                or getattr(peer, "chat_id", None)
                or getattr(peer, "id", None)
            )
        out["id"] = pid if pid is not None else target_id
        out["username"] = getattr(peer, "username", None) or target_username
        out["resolved"] = True
        # Presence in the dialog list is itself the dialog: a scan hit stays a
        # positive even if both sides have since wiped the history.
        out["has_dialog"] = out["source"] == "dialog_scan" or bool(top) or total > 0
        out["message_count"] = total
        if out["source"] != "dialog_scan":
            out["source"] = "peer_dialogs"
        return out
