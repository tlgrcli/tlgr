"""Turning what a human typed into an `InputPeer` (§6.6).

Access hashes are **per account**. A resolver cache shared between accounts
hands account B a hash minted for account A, and the server answers
`PEER_ID_INVALID` for a peer that plainly exists — so there is one resolver
per account and the cache file lives under that account's directory.

The strategy order is cheapest-first, and each step exists because the one
before it cannot answer:

1. `me`/`saved` — free, and the only correct answer for `InputPeerSelf`.
2. the client's own entity cache and session table — free, no network.
3. `contacts.resolveUsername` — one round trip, cached for 24 h. Rate class
   `resolve`, because this method floods at roughly fifty calls in a short
   period and a chat list that resolves every `@handle` will hit it.
4. `contacts.resolvePhone` — works for a non-contact only if their privacy
   allows it; falls back to scanning contacts.
5. `messages.checkChatInvite` for `t.me/+hash` — read-only, reports the chat
   *without joining it*. Joining is `chat join`, which is a different verb
   for a reason.
6. `t.me/c/<id>/<msg>` — pure arithmetic, no network.
7. a bare int — `getPeerDialogs` when a hash is cached, otherwise a bounded
   dialog scan.
8. **min entities** — a user seen only inside a channel message has no usable
   access hash, and Telethon gives up. Remembering `(chat_id, msg_id)` lets
   the resolver build `InputPeerUserFromMessage` by hand, which is what makes
   `chat posters` → `user get` work for a stranger.

The failure modes are deliberately two, not one. A peer the strategies
*exhausted* is `NOT_FOUND` (exit 5). A peer whose search was truncated,
flooded or errored is `INDETERMINATE` (exit 13) — "we could not establish
this" must never be reported as "no".
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core.errors import IndeterminateError, NotFoundError, UsageError
from tlgr.core.paths import write_private
from tlgr.models.peer import PeerRef, parse_peer_ref

log = logging.getLogger("tlgr.core.peers")

__all__ = ["PeerCache", "PeerResolver", "channel_id_from_link"]

#: A resolved username is stable for a day; an access hash is stable until the
#: account is re-logged-in, which is what makes this cache worth having.
USERNAME_TTL = 24 * 3600

_CHANNEL_MARK = -1000000000000
_LINK_C = re.compile(r"(?:t\.me|telegram\.me)/c/(\d+)(?:/(\d+))?")


def channel_id_from_link(text: str) -> tuple[int, int | None] | None:
    """`t.me/c/1234/56` → `(-1000000001234, 56)`. No network involved."""
    match = _LINK_C.search(text)
    if not match:
        return None
    raw = int(match.group(1))
    message_id = int(match.group(2)) if match.group(2) else None
    return _CHANNEL_MARK - raw, message_id


@dataclass
class CachedPeer:
    peer_id: int
    kind: str
    access_hash: int = 0
    username: str = ""
    resolved_at: float = 0.0
    #: Where this user was seen, for `InputPeerUserFromMessage` (§6.6 step 8).
    from_chat: int = 0
    from_message: int = 0

    @property
    def fresh(self) -> bool:
        return not self.username or (time.time() - self.resolved_at) < USERNAME_TTL


class PeerCache:
    """The per-account resolver cache, persisted as JSON.

    JSON rather than SQLite because it is a few thousand rows read at start
    and written on change; a second database file to keep consistent with the
    session's is a cost with no benefit at this size.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.by_username: dict[str, CachedPeer] = {}
        self.by_id: dict[int, CachedPeer] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for entry in raw.get("peers", []) if isinstance(raw, dict) else []:
            with contextlib.suppress(TypeError, ValueError):
                peer = CachedPeer(**entry)
                self.by_id[peer.peer_id] = peer
                if peer.username:
                    self.by_username[peer.username.lower()] = peer

    def save(self) -> None:
        if self.path is None or not self._dirty:
            return
        payload = {"peers": [vars(peer) for peer in self.by_id.values()]}
        with contextlib.suppress(OSError):
            write_private(self.path, json.dumps(payload))
        self._dirty = False

    def put(self, peer: CachedPeer) -> CachedPeer:
        self.by_id[peer.peer_id] = peer
        if peer.username:
            self.by_username[peer.username.lower()] = peer
        self._dirty = True
        return peer

    def get_username(self, username: str) -> CachedPeer | None:
        peer = self.by_username.get(username.lower())
        return peer if peer is not None and peer.fresh else None

    def get_id(self, peer_id: int) -> CachedPeer | None:
        return self.by_id.get(peer_id)

    def remember_min(self, user_id: int, chat_id: int, message_id: int) -> None:
        """Record where a `min` user was seen so it can be addressed later."""
        peer = self.by_id.get(user_id) or CachedPeer(peer_id=user_id, kind="user")
        peer.from_chat = chat_id
        peer.from_message = message_id
        self.put(peer)


@dataclass
class PeerResolver:
    """One resolver per account. Never share one between accounts."""

    client: Any
    account: str = ""
    cache: PeerCache = field(default_factory=PeerCache)
    dialog_scan_max: int = 5000
    #: Set by the daemon so a resolve is paced like the expensive call it is.
    limiter: Any = None

    async def resolve(
        self,
        ref: PeerRef | str,
        *,
        allow_network: bool = True,
        want: str = "peer",
    ) -> Any:
        """The `InputPeer` for *ref*, or a classified failure."""
        # An int is accepted as well as a string because ids come back from
        # the models already parsed, and re-stringifying at each call site is
        # a chance to drop the sign of a channel id.
        parsed = parse_peer_ref(str(ref)) if isinstance(ref, (str, int)) else ref
        kind = parsed.kind

        if kind in ("self", "saved"):
            from telethon.tl.types import InputPeerSelf

            # Saved Messages *is* the self peer; there is no separate entity,
            # which is why `me` and `saved` cannot be told apart downstream.
            return InputPeerSelf()

        if kind == "link":
            # A `t.me/c/<id>/<msg>` link is arithmetic, not a lookup.
            found = channel_id_from_link(parsed.raw)
            if found is not None:
                return await self._by_id(found[0], allow_network=allow_network)

        if kind == "username":
            return await self._by_username(parsed.value, allow_network=allow_network, want=want)

        if kind == "phone":
            return await self._by_phone(parsed.value, allow_network=allow_network)

        if kind == "id":
            return await self._by_id(int(parsed.value), allow_network=allow_network)

        if kind == "invite":
            return await self._resolve_invite(parsed, allow_network=allow_network)

        raise UsageError(f"cannot interpret {parsed.raw!r} as a peer", field="chat")

    # -- strategies --------------------------------------------------------

    async def _cached_input(self, ref: Any) -> Any | None:
        """Telethon's own caches: the entity cache, then the session table."""
        try:
            return await self.client.get_input_entity(ref)
        except (ValueError, TypeError):
            return None
        except Exception as exc:
            if _is_transport(exc):
                raise
            return None

    async def _by_username(self, username: str, *, allow_network: bool, want: str = "peer") -> Any:
        handle = username.lstrip("@")
        cached = self.cache.get_username(handle)
        if cached is not None:
            built = self._build(cached)
            if built is not None:
                return built
        found = await self._cached_input(f"@{handle}")
        if found is not None:
            return found
        if not allow_network:
            raise IndeterminateError(
                f"@{handle} is not in the local cache and network resolution was not allowed"
            )

        from telethon.tl.functions.contacts import ResolveUsernameRequest

        await self._pace("resolve")
        try:
            result = await self.client(ResolveUsernameRequest(handle))
        except Exception as exc:
            if type(exc).__name__ in ("UsernameNotOccupiedError", "UsernameInvalidError"):
                raise NotFoundError(f"no Telegram account or chat named @{handle}") from exc
            raise
        entity = _first_entity(result)
        if entity is None:
            raise NotFoundError(f"no Telegram account or chat named @{handle}")
        self._remember(entity, username=handle)
        return await self._cached_input(entity) or entity

    async def _by_phone(self, phone: str, *, allow_network: bool) -> Any:
        digits = "+" + "".join(c for c in phone if c.isdigit())
        found = await self._cached_input(digits)
        if found is not None:
            return found
        if not allow_network:
            raise IndeterminateError(f"{_mask(digits)} is not cached and network was not allowed")

        from telethon.tl.functions.contacts import ResolvePhoneRequest

        await self._pace("resolve")
        try:
            result = await self.client(ResolvePhoneRequest(digits))
        except Exception as exc:
            name = type(exc).__name__
            if name in ("PhoneNotOccupiedError", "PhoneNumberInvalidError"):
                raise NotFoundError(f"no Telegram account for {_mask(digits)}") from exc
            # A privacy refusal is *not* proof the number has no account: the
            # honest answer is "we could not establish it" (exit 13).
            raise IndeterminateError(
                f"could not resolve {_mask(digits)}: {exc}. "
                "The number may exist but hide itself from lookups."
            ) from exc
        entity = _first_entity(result)
        if entity is None:
            raise NotFoundError(f"no Telegram account for {_mask(digits)}")
        self._remember(entity)
        return await self._cached_input(entity) or entity

    async def _by_id(self, peer_id: int, *, allow_network: bool) -> Any:
        found = await self._cached_input(peer_id)
        if found is not None:
            return found
        cached = self.cache.get_id(peer_id)
        if cached is not None:
            built = self._build(cached)
            if built is not None:
                return built
        if not allow_network:
            raise IndeterminateError(f"{peer_id} is not cached and network was not allowed")
        return await self._scan_dialogs(peer_id)

    async def _scan_dialogs(self, peer_id: int) -> Any:
        """The last resort, and the only thing that licenses a negative answer.

        A scan that was cut short by the cap, a flood or an RPC error has not
        proved anything, so it raises INDETERMINATE rather than NOT_FOUND.
        """
        from telethon import utils

        seen = 0
        truncated = False
        try:
            async for dialog in self.client.iter_dialogs(limit=self.dialog_scan_max):
                seen += 1
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                if utils.get_peer_id(entity) == peer_id:
                    self._remember(entity)
                    return await self._cached_input(entity) or entity
            truncated = seen >= self.dialog_scan_max
        except Exception as exc:
            raise IndeterminateError(
                f"the dialog scan for {peer_id} failed after {seen} dialogs: {exc}"
            ) from exc
        if truncated:
            raise IndeterminateError(
                f"{peer_id} was not among the first {self.dialog_scan_max} dialogs; "
                "raise [limits] dialog_scan_max to search further"
            )
        raise NotFoundError(f"no chat or user with id {peer_id} is reachable from this account")

    async def _resolve_invite(self, ref: PeerRef, *, allow_network: bool) -> Any:
        """Read an invite link without joining it."""
        if not allow_network:
            raise IndeterminateError("an invite link cannot be resolved without the network")

        from telethon.tl.functions.messages import CheckChatInviteRequest

        await self._pace("resolve")
        try:
            result = await self.client(CheckChatInviteRequest(ref.value))
        except Exception as exc:
            if type(exc).__name__ == "InviteHashExpiredError":
                raise NotFoundError("that invite link has expired") from exc
            if type(exc).__name__ == "InviteHashInvalidError":
                raise NotFoundError("that invite link is not valid") from exc
            raise
        chat = getattr(result, "chat", None)
        if chat is None:
            # `ChatInvite` (not `ChatInviteAlready`) describes a chat we are
            # not in: there is nothing to address, and saying so beats
            # inventing an InputPeer that every later call rejects.
            raise NotFoundError(
                "that invite is for a chat this account has not joined; run 'tlgr chat join' first"
            )
        self._remember(chat)
        return await self._cached_input(chat) or chat

    # -- min entities ------------------------------------------------------

    def remember_from_message(self, user_id: int, chat_id: int, message_id: int) -> None:
        self.cache.remember_min(user_id, chat_id, message_id)
        self.cache.save()

    def _build(self, cached: CachedPeer) -> Any | None:
        """An `InputPeer` from the cache, including the `*FromMessage` forms."""
        from telethon.tl.types import (
            InputPeerChannel,
            InputPeerChat,
            InputPeerUser,
            InputPeerUserFromMessage,
        )

        if cached.access_hash:
            if cached.kind == "user":
                return InputPeerUser(_raw_id(cached.peer_id), cached.access_hash)
            if cached.kind in ("channel", "supergroup"):
                return InputPeerChannel(_raw_id(cached.peer_id), cached.access_hash)
        if cached.kind == "group":
            return InputPeerChat(_raw_id(cached.peer_id))
        if cached.kind == "user" and cached.from_chat and cached.from_message:
            # The whole point of remembering where a `min` user was seen:
            # Telethon never builds this, so a stranger who posted in a
            # channel is unaddressable without it.
            peer = self.cache.get_id(cached.from_chat)
            container = self._build(peer) if peer else None
            if container is not None:
                return InputPeerUserFromMessage(
                    peer=container,
                    msg_id=cached.from_message,
                    user_id=_raw_id(cached.peer_id),
                )
        return None

    def _remember(self, entity: Any, *, username: str = "") -> CachedPeer:
        from telethon import utils

        kind = type(entity).__name__.lower()
        if kind == "channel":
            kind = "supergroup" if getattr(entity, "megagroup", False) else "channel"
        elif kind == "chat":
            kind = "group"
        peer = CachedPeer(
            peer_id=utils.get_peer_id(entity),
            kind=kind,
            access_hash=int(getattr(entity, "access_hash", 0) or 0),
            username=username or (getattr(entity, "username", "") or ""),
            resolved_at=time.time(),
        )
        self.cache.put(peer)
        self.cache.save()
        return peer

    async def _pace(self, rate_class: str) -> None:
        if self.limiter is not None:
            await self.limiter.acquire(rate_class)


def _raw_id(marked: int) -> int:
    """The unmarked id Telethon's `Input*` constructors want."""
    if marked < _CHANNEL_MARK:
        return _CHANNEL_MARK - marked
    if marked < 0:
        return -marked
    return marked


def _first_entity(result: Any) -> Any | None:
    for attribute in ("users", "chats"):
        found = getattr(result, attribute, None) or ()
        if found:
            return found[0]
    return None


def _is_transport(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionError) or "disconnected" in str(exc).lower()


def _mask(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return f"+{digits[:3]}…{digits[-2:]}" if len(digits) >= 6 else "+***"
