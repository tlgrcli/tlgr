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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telethon.tl import types

__all__ = [
    "DialogState",
    "FakeTelegramClient",
    "World",
    "fake_client_factory",
    "make_channel",
    "make_message",
    "make_user",
]


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
    #: marked chat id → its dialog row. Ordered newest-first by top_message,
    #: which is how the server orders `messages.getDialogs`.
    dialogs_by_id: dict[int, DialogState] = field(default_factory=dict)
    #: The chat folders (`dialogFilter`), in display order.
    filters: list[Any] = field(default_factory=list)
    tags_enabled: bool = False
    #: The `peerSettings` action bar, per chat.
    peer_settings: dict[int, Any] = field(default_factory=dict)
    global_privacy: Any = None
    #: request type name → callable(request) -> result, or a plain value.
    raw: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    next_message_id: int = 1000

    # -- behaviour knobs ---------------------------------------------------

    _fail_next: dict[str, BaseException] = field(default_factory=dict)
    latency_ms: float = 0.0
    #: Drop the connection after this many requests. 0 disables.
    disconnect_after: int = 0
    connect_error: BaseException | None = None
    catch_ups: int = 0
    connects: int = 0
    saves: int = 0

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

    def find(self, chat_id: int, message_id: int) -> types.Message | None:
        for message in self.history(chat_id):
            if message.id == message_id:
                return message
        return None

    # -- the dialog world --------------------------------------------------

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
        self.session = _FakeSession()
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

    async def qr_login(self) -> Any:
        self.world.calls.append(("qr_login", None))
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
            return default(request)
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
        return self._updates(self._store(request, request.message, media=types.MessageMediaEmpty()))

    def _raw_SendMultiMediaRequest(self, request: Any) -> types.Updates:
        produced = [
            self._store(request, item.message, media=types.MessageMediaEmpty())
            for item in request.multi_media
        ]
        return self._updates(*produced)

    def _raw_SendScreenshotNotificationRequest(self, request: Any) -> types.Updates:
        return self._updates(self._store(request))

    def _raw_UploadMediaRequest(self, request: Any) -> Any:
        return request.media

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

    def _raw_SendReactionRequest(self, request: Any) -> types.Updates:
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

    def _raw_ReadPollVotesRequest(self, request: Any) -> bool:
        return True

    def _raw_GetUnreadMentionsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [m for m in self.world.history(chat_id) if getattr(m, "mentioned", False)]
        return types.messages.Messages(messages=found, chats=[], users=[], topics=[])

    def _raw_GetUnreadReactionsRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [m for m in self.world.history(chat_id) if getattr(m, "reactions", None)]
        return types.messages.Messages(messages=found, chats=[], users=[], topics=[])

    def _raw_GetUnreadPollVotesRequest(self, request: Any) -> Any:
        return types.messages.Messages(messages=[], chats=[], users=[], topics=[])

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

    def _raw_GetPromoDataRequest(self, request: Any) -> Any:
        return types.help.PromoData(
            expires=None,
            pending_suggestions=["VALIDATE_PHONE_NUMBER"],
            dismissed_suggestions=[],
            chats=[],
            users=[],
        )

    def _raw_GetAppConfigRequest(self, request: Any) -> Any:
        entries = [
            types.JsonObjectValue(
                key="dialogs_pinned_limit_default", value=types.JsonNumber(value=5)
            ),
            types.JsonObjectValue(key="channels_limit_default", value=types.JsonNumber(value=500)),
        ]
        return types.help.AppConfig(hash=0, config=types.JsonObject(value=entries))

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
        self.expires = None

    async def wait(self, timeout: float | None = None) -> Any:
        failure = self.world._fail_next.pop("qr_wait", None)
        if failure is not None:
            raise failure
        self.world.authorized = True
        return self.world.me


class _FakeSession:
    def __init__(self) -> None:
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


def fake_client_factory(world: World | None = None) -> Any:
    """A `client_factory` for `SessionManager` that ignores the session path."""
    shared = world or World()

    def factory(session_path: Any, options: Any) -> FakeTelegramClient:
        return FakeTelegramClient(shared, session=session_path)

    factory.world = shared  # type: ignore[attr-defined]
    return factory
