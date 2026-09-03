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
    "AuthWorld",
    "DialogState",
    "FakeTelegramClient",
    "World",
    "fake_client_factory",
    "make_authorization",
    "make_channel",
    "make_kdf",
    "make_message",
    "make_user",
    "make_web_authorization",
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
    #: Venues `messages.getInlineBotResults` should return for a geo query.
    venues: list[Any] = field(default_factory=list)
    #: Public posts `channels.searchPosts` should return.
    public_posts: list[Any] = field(default_factory=list)
    search_flood_remains: int = 3
    search_flood_stars: int = 10
    #: Sessions, the cloud password, websites, passkeys and Passport values.
    auth: AuthWorld = field(default_factory=AuthWorld)

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
                found.append(message)
        found.sort(key=lambda m: m.id, reverse=True)
        return self._slice(found[: int(request.limit)], next_rate=42)

    def _raw_SearchSentMediaRequest(self, request: Any) -> Any:
        found = [
            message
            for history in self.world.messages.values()
            for message in history
            if getattr(message, "out", False)
        ]
        return self._slice(found[: int(request.limit)])

    def _raw_SearchRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.peer)
        found = [
            message
            for message in self.world.history(chat_id)
            if not request.q or request.q.lower() in (message.message or "").lower()
        ]
        return self._slice(sorted(found, key=lambda m: m.id, reverse=True)[: int(request.limit)])

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

    def _full_channel(self, chat_id: int) -> Any:
        return types.ChannelFull(
            id=abs(chat_id) - 1000000000000 if chat_id < -1000000000000 else abs(chat_id),
            about="",
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            chat_photo=types.PhotoEmpty(id=0),
            notify_settings=types.PeerNotifySettings(),
            bot_info=[],
            pts=1,
            available_reactions=self.world.chat_reactions.get(chat_id) or types.ChatReactionsNone(),
            reactions_limit=self.world.reactions_limit.get(chat_id),
            paid_reactions_available=self.world.paid_enabled.get(chat_id),
        )

    def _raw_GetFullChannelRequest(self, request: Any) -> Any:
        chat_id = self._chat_id(request.channel)
        return types.messages.ChatFull(full_chat=self._full_channel(chat_id), chats=[], users=[])

    def _raw_GetFullChatRequest(self, request: Any) -> Any:
        chat_id = int(request.chat_id)
        return types.messages.ChatFull(
            full_chat=types.ChatFull(
                id=chat_id,
                about="",
                participants=types.ChatParticipantsForbidden(chat_id=chat_id),
                notify_settings=types.PeerNotifySettings(),
                available_reactions=self.world.chat_reactions.get(-chat_id)
                or types.ChatReactionsNone(),
                reactions_limit=self.world.reactions_limit.get(-chat_id),
            ),
            chats=[],
            users=[],
        )

    def _raw_SetChatAvailableReactionsRequest(self, request: Any) -> types.Updates:
        chat_id = self._chat_id(request.peer)
        self.world.chat_reactions[chat_id] = request.available_reactions
        if request.reactions_limit is not None:
            self.world.reactions_limit[chat_id] = int(request.reactions_limit)
        if request.paid_enabled is not None:
            self.world.paid_enabled[chat_id] = bool(request.paid_enabled)
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
            users=[],
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
            date=0,
            expires=0,
            test_mode=False,
            this_dc=2,
            dc_options=[],
            dc_txt_domain_name="",
            chat_size_max=200,
            megagroup_size_max=200000,
            forwarded_count_max=100,
            online_update_period_ms=1000,
            offline_blur_timeout_ms=1000,
            offline_idle_timeout_ms=1000,
            online_cloud_timeout_ms=1000,
            notify_cloud_delay_ms=1000,
            notify_default_delay_ms=1000,
            push_chat_period_ms=1000,
            push_chat_limit=1,
            edit_time_limit=172800,
            revoke_time_limit=172800,
            revoke_pm_time_limit=172800,
            rating_e_decay=1,
            stickers_recent_limit=200,
            channels_read_media_period=1,
            call_receive_timeout_ms=1,
            call_ring_timeout_ms=1,
            call_connect_timeout_ms=1,
            call_packet_timeout_ms=1,
            me_url_prefix="https://t.me/",
            caption_length_max=1024,
            message_length_max=4096,
            webfile_dc_id=self.world.webfile_dc_id,
            venue_search_username=self.world.venue_search_username,
            reactions_default=self.world.default_reaction,
        )
        return config

    def _raw_GetInlineBotResultsRequest(self, request: Any) -> Any:
        return types.messages.BotResults(
            query_id=1,
            results=list(self.world.venues),
            cache_time=0,
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
        """`help.getAppConfig`, for both the auth world and the chat limits.

        The chat group reads its dialog and channel limits out of the same
        call the auth group reads its suggestions out of, so the defaults for
        both are seeded here and a test overrides either through
        `world.auth.app_config`.
        """
        config = dict(self.auth.app_config)
        config.setdefault("pending_suggestions", list(self.auth.pending_suggestions))
        config.setdefault("dismissed_suggestions", list(self.auth.dismissed_suggestions))
        config.setdefault("dialogs_pinned_limit_default", 5)
        config.setdefault("channels_limit_default", 500)
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
        self.auth.dismissed_suggestions.append(request.suggestion)
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
        return types.updates.State(
            pts=90210, qts=0, date=datetime.now(timezone.utc), seq=0, unread_count=0
        )

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
    """Enough of a Telethon session that `StringSession.save()` works on it.

    `account export` reads the live session rather than the file on disk, so
    a fake without an auth key would make that operation untestable.
    """

    def __init__(self) -> None:
        from telethon.crypto import AuthKey

        self.saved = 0
        self.dc_id = 4
        self.server_address = "149.154.167.51"
        self.port = 443
        self.auth_key = AuthKey(bytes(range(256)))
        self.takeout_id = None

    def save(self) -> None:
        self.saved += 1


def fake_client_factory(world: World | None = None) -> Any:
    """A `client_factory` for `SessionManager` that ignores the session path."""
    shared = world or World()

    def factory(session_path: Any, options: Any) -> FakeTelegramClient:
        return FakeTelegramClient(shared, session=session_path)

    factory.world = shared  # type: ignore[attr-defined]
    return factory
