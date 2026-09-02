"""One account, one client, one supervisor (§6.2, §6.3).

v1 constructed a `TelegramClient` with `connection_retries=5` and treated the
resulting object as permanently usable. When the route to Telegram dropped for
longer than five retries, Telethon raised, left the client in the daemon's
dict, and every subsequent request failed with `ConnectionError: Cannot send
requests while disconnected` — while `tlgr daemon status` reported the account
as present and healthy. That is COR-13 and it is the reason this file exists.

The rules it enforces:

* **the supervisor owns backoff**, not Telethon (`connection_retries=None`),
  so a drop is a state transition rather than an exception thrown at whoever
  happened to be making a request;
* **`catch_up()` runs after every reconnect**, not only at construction.
  Telethon's own reconnect handler calls `get_me()` and nothing else, so an
  account that was down for ten minutes silently misses everything that
  happened. It also runs after a wall-clock jump, which is what a laptop lid
  looks like from inside the process;
* **update state is persisted every minute**, not only on a clean
  `disconnect()`, so a SIGKILL costs at most a minute of `pts` progress;
* **a fatal auth error is terminal.** `AUTH_KEY_UNREGISTERED` will not fix
  itself; reconnecting forever while reporting "degraded" (v1) hides a
  revoked session behind a transient-looking state;
* **the state is written where the CLI can read it**, so `tlgr account list`
  is honest even with the daemon down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core import telethon_compat as compat
from tlgr.core.errors import (
    ConfigurationError,
    RetryableError,
    SessionError,
    is_fatal_auth,
)
from tlgr.daemon.singleton import FileLock, LockBusy

log = logging.getLogger("tlgr.daemon.session")

__all__ = ["AccountSession", "SessionState", "backoff_delays"]

#: 1, 2, 4, 8, 16, 32, 60, 60… with ±20 % jitter, capped at a minute (§6.3).
_BACKOFF_CAP = 60.0
_JITTER = 0.2

#: How far a wall-clock/monotonic divergence has to go before it counts as a
#: sleep rather than as scheduling noise.
_CLOCK_JUMP_SECONDS = 60.0
_CLOCK_TICK_SECONDS = 30.0

#: The belt-and-braces resync interval for an account that has heard nothing.
_SILENCE_CATCHUP_SECONDS = 900.0


class SessionState:
    STARTING = "starting"
    ONLINE = "online"
    DEGRADED = "degraded"
    NEEDS_LOGIN = "needs_login"
    FROZEN = "frozen"
    STOPPING = "stopping"
    STOPPED = "stopped"


def backoff_delays(attempt: int, *, cap: float = _BACKOFF_CAP) -> float:
    """The delay before reconnect attempt *attempt* (0-based), with jitter.

    Jitter matters with more than one account: without it, four accounts that
    dropped together reconnect together, and the reconnection storm is itself
    a reason to be rate limited.
    """
    base = min(cap, 2.0**attempt)
    return base * (1.0 + random.uniform(-_JITTER, _JITTER))


@dataclass
class ClientOptions:
    """Everything `TelegramClient(...)` needs that is not the session path."""

    api_id: int
    api_hash: str
    entity_cache_limit: int = 20000
    request_retries: int = 5
    connect_timeout: int = 10
    flood_sleep_threshold: int = 120
    device_model: str = ""
    system_version: str = ""
    app_version: str = ""
    lang_code: str = "en"
    system_lang_code: str = "en"
    proxy: Any = None
    use_ipv6: bool = False
    connection: Any = None
    params: dict[str, int] = field(default_factory=dict)


def build_client(session_path: Path, options: ClientOptions) -> Any:
    """Construct the Telethon client tlgr wants (§6.2).

    `connection_retries=None` is the load-bearing argument: it means "retry
    forever inside Telethon" is *off*, and the supervisor below decides what a
    disconnection means.
    """
    from telethon import TelegramClient

    kwargs: dict[str, Any] = {
        "catch_up": True,
        "sequential_updates": True,
        "raise_last_call_error": True,
        "entity_cache_limit": int(options.entity_cache_limit),
        "connection_retries": None,
        "retry_delay": 1,
        "request_retries": int(options.request_retries),
        "auto_reconnect": True,
        "timeout": int(options.connect_timeout),
        "flood_sleep_threshold": int(options.flood_sleep_threshold),
        "device_model": options.device_model or None,
        "system_version": options.system_version or None,
        "app_version": options.app_version or None,
        "lang_code": options.lang_code or "en",
        "system_lang_code": options.system_lang_code or "en",
        "use_ipv6": bool(options.use_ipv6),
    }
    if options.proxy:
        kwargs["proxy"] = options.proxy
    if options.connection is not None:
        kwargs["connection"] = options.connection
    return TelegramClient(str(session_path), options.api_id, options.api_hash, **kwargs)


class AccountSession:
    """The state machine of §6.2, plus the supervisor of §6.3."""

    def __init__(
        self,
        alias: str,
        *,
        session_path: Path,
        lock_path: Path,
        options: ClientOptions,
        client_factory: Callable[[Path, ClientOptions], Any] = build_client,
        on_state: Callable[[str, str, int | None], None] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        state_save_interval: int = 60,
        presence: str = "off",
        resync_depth: int = 50,
        peers_path: Path | None = None,
        dialog_scan_max: int = 5000,
    ) -> None:
        self.alias = alias
        self.session_path = session_path
        self.lock = FileLock(lock_path)
        self.options = options
        self._factory = client_factory
        self._on_state = on_state
        self._on_event = on_event
        self.state_save_interval = state_save_interval
        self.presence = presence
        self.resync_depth = resync_depth
        self.peers_path = peers_path
        self.dialog_scan_max = dialog_scan_max
        self._resolver: Any = None

        self.client: Any = None
        self.me: Any = None
        self.state: str = SessionState.STOPPED
        self.reason: str = ""
        self.since: float = time.time()
        self.connected_since: float | None = None
        self.last_update: float = 0.0
        self.reconnects: int = 0
        self.in_flight: int = 0
        self.resync_needed: list[int] = []
        self.catch_up_pending: bool = False
        self._attempt = 0
        self._stopping = False
        self._ready = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._tickers: list[asyncio.Task[None]] = []
        self._wrapper: Any = None

    # -- state -------------------------------------------------------------

    def _set_state(self, state: str, reason: str = "") -> None:
        if state == self.state and reason == self.reason:
            return
        self.state = state
        self.reason = reason
        self.since = time.time()
        if state == SessionState.ONLINE:
            self._ready.set()
        else:
            self._ready.clear()
        log.info(
            "account %s is %s%s",
            self.alias,
            state,
            f" ({reason})" if reason else "",
            extra={"account": self.alias, "state": state, "reason": reason},
        )
        if self._on_state is not None:
            user_id = getattr(self.me, "id", None)
            with contextlib.suppress(Exception):
                self._on_state(state, reason, user_id)

    @property
    def healthy(self) -> bool:
        return self.state == SessionState.ONLINE

    @property
    def connected(self) -> bool:
        return bool(self.client is not None and self.client.is_connected())

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Take the session lock and run the supervisor in the background."""
        if self._supervisor is not None:
            return
        try:
            self.lock.acquire()
        except LockBusy as exc:
            self._set_state(SessionState.STOPPED, "session file is owned by another process")
            raise ConfigurationError(
                f"account {self.alias!r}: {exc}. Only one process may hold a session file; "
                "stop the other daemon (tlgr daemon stop) and try again."
            ) from exc
        self._stopping = False
        self._set_state(SessionState.STARTING)
        self._supervisor = asyncio.create_task(self._supervise(), name=f"tlgr-session-{self.alias}")

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Shut down cleanly: presence, state save, disconnect, unlock (§6.11)."""
        self._stopping = True
        self._set_state(SessionState.STOPPING)
        for task in self._tickers:
            task.cancel()
        self._tickers = []
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        if self.client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._graceful_client_shutdown(), timeout)
        self.client = None
        self.lock.release()
        self._set_state(SessionState.STOPPED)

    async def _graceful_client_shutdown(self) -> None:
        if self.presence == "online":
            await self._set_presence(offline=True)
        await compat.save_state(self.client)
        await self.client.disconnect()

    # -- the supervisor ----------------------------------------------------

    async def _supervise(self) -> None:
        while not self._stopping:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_fatal_auth(exc):
                    self._set_state(SessionState.NEEDS_LOGIN, type(exc).__name__)
                    return
                self._set_state(SessionState.DEGRADED, f"{type(exc).__name__}: {exc}")
                delay = backoff_delays(self._attempt)
                self._attempt += 1
                log.warning(
                    "account %s reconnecting in %.1fs (attempt %d)",
                    self.alias,
                    delay,
                    self._attempt,
                    extra={"account": self.alias, "attempt": self._attempt},
                )
                await asyncio.sleep(delay)
                continue

            if self.state == SessionState.NEEDS_LOGIN:
                return
            # `_connect_once` returned because the connection dropped.
            if self._stopping:
                return
            self.reconnects += 1
            self._set_state(SessionState.DEGRADED, "connection lost")
            delay = backoff_delays(self._attempt)
            self._attempt += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        """Connect, authorise, catch up, then wait for the connection to end."""
        if self.client is None:
            self.client = self._factory(self.session_path, self.options)
            self._install_hooks()

        await self.client.connect()
        if not await self.client.is_user_authorized():
            self._set_state(SessionState.NEEDS_LOGIN, "not authorized")
            raise SessionError(f"account {self.alias!r} is not logged in")

        self.me = await self.client.get_me()
        self._attempt = 0
        self.connected_since = time.time()
        await self._after_connect()
        self._set_state(SessionState.ONLINE)
        self._start_tickers()

        disconnected = getattr(self.client, "disconnected", None)
        if disconnected is None:
            return
        await disconnected

    async def _after_connect(self) -> None:
        """Warm the entity cache, then catch up (§6.3).

        The warm-up is not an optimisation: `catch_up()` skips channels whose
        `pts` it has never seen, so without one `iter_dialogs` pass a fresh
        session silently gets no channel updates at all.
        """
        await self._warm_entity_cache()
        await self.catch_up()
        if self.presence == "online":
            await self._set_presence(offline=False)

    async def _warm_entity_cache(self) -> None:
        iterator = getattr(self.client, "iter_dialogs", None)
        if iterator is None:
            return
        count = 0
        try:
            async for _dialog in iterator(limit=None):
                count += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("entity cache warm-up failed for %s: %s", self.alias, exc)
        else:
            log.debug("warmed %d dialogs for %s", count, self.alias)

    async def catch_up(self) -> None:
        catch_up = getattr(self.client, "catch_up", None)
        if catch_up is None:
            return
        self.catch_up_pending = True
        try:
            await catch_up()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "catch_up failed for %s: %s", self.alias, exc, extra={"account": self.alias}
            )
        finally:
            self.catch_up_pending = False

    # -- hooks -------------------------------------------------------------

    def _install_hooks(self) -> None:
        compat.install_reconnect_hook(self.client, self._on_reconnect)
        compat.install_too_long_hook(self.client, self._on_too_long)

    async def _on_reconnect(self) -> None:
        """Telethon reconnected under us; the state it holds is stale."""
        self.reconnects += 1
        await self.catch_up()

    def _on_too_long(self, scope: str, channel_id: int | None) -> None:
        """The server said our gap is unrecoverable (checklist 9)."""
        if scope == compat.TOO_LONG_CHANNEL and channel_id is not None:
            if channel_id not in self.resync_needed:
                self.resync_needed.append(int(channel_id))
        else:
            if 0 not in self.resync_needed:
                self.resync_needed.append(0)
        log.warning(
            "difference too long for %s (%s); a resync is needed",
            self.alias,
            channel_id if channel_id is not None else "account",
            extra={"account": self.alias},
        )

    # -- periodic work -----------------------------------------------------

    def _start_tickers(self) -> None:
        if self._tickers:
            return
        self._tickers = [
            asyncio.create_task(self._state_saver(), name=f"tlgr-state-{self.alias}"),
            asyncio.create_task(self._clock_watcher(), name=f"tlgr-clock-{self.alias}"),
        ]

    async def _state_saver(self) -> None:
        while not self._stopping:
            await asyncio.sleep(max(5, self.state_save_interval))
            if self.client is not None and self.connected:
                await compat.save_state(self.client)

    async def _clock_watcher(self) -> None:
        """Catch up after a wall-clock jump, which is what sleep looks like.

        Comparing `time.time()` against `time.monotonic()` is the only portable
        way to notice that the machine was suspended: no timer fires while it
        is, so the daemon wakes believing thirty seconds passed.
        """
        wall = time.time()
        mono = time.monotonic()
        quiet_since = time.monotonic()
        while not self._stopping:
            await asyncio.sleep(_CLOCK_TICK_SECONDS)
            new_wall, new_mono = time.time(), time.monotonic()
            drift = (new_wall - wall) - (new_mono - mono)
            wall, mono = new_wall, new_mono
            if abs(drift) >= _CLOCK_JUMP_SECONDS and self.connected:
                log.info(
                    "wall clock jumped %.0fs on %s; catching up",
                    drift,
                    self.alias,
                    extra={"account": self.alias},
                )
                await self.catch_up()
                quiet_since = time.monotonic()
                continue
            if (
                self.connected
                and self.last_update
                and time.monotonic() - quiet_since >= _SILENCE_CATCHUP_SECONDS
            ):
                await self.catch_up()
                quiet_since = time.monotonic()

    async def _set_presence(self, *, offline: bool) -> None:
        """`account.updateStatus`. Telethon never sends it, so tlgr reads as offline."""
        try:
            from telethon.tl.functions.account import UpdateStatusRequest

            await self.client(UpdateStatusRequest(offline=offline))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("presence update failed for %s: %s", self.alias, exc)

    @property
    def resolver(self) -> Any:
        """The account's entity resolver, built once the client exists.

        One per account, never shared: an access hash minted for one account
        is meaningless to another, and handing it over produces
        `PEER_ID_INVALID` for a peer that plainly exists (§6.6).
        """
        from tlgr.core.peers import PeerCache, PeerResolver

        if self._resolver is None or self._resolver.client is not self.client:
            self._resolver = PeerResolver(
                client=self.client,
                account=self.alias,
                cache=PeerCache(self.peers_path),
                dialog_scan_max=self.dialog_scan_max,
            )
        return self._resolver

    # -- request gate ------------------------------------------------------

    async def acquire(self, *, timeout: float) -> Any:
        """Return a usable client, or raise the error the state table demands.

        This is the one place `Cannot send requests while disconnected` is
        prevented from reaching a user: a degraded account waits briefly for a
        reconnect and then answers `RETRYABLE` with a hint, and a revoked one
        answers `SESSION_ERROR` immediately.
        """
        if self.state == SessionState.NEEDS_LOGIN:
            raise SessionError(
                f"account {self.alias!r} needs to log in again ({self.reason or 'session invalid'})"
            )
        if self.state in (SessionState.STOPPING, SessionState.STOPPED):
            raise RetryableError(f"account {self.alias!r} is not running")
        if self.state == SessionState.ONLINE and self.connected:
            return self.client

        wait = min(timeout, 15.0) if self.state == SessionState.DEGRADED else min(timeout, 10.0)
        try:
            await asyncio.wait_for(self._ready.wait(), wait)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            if self.state == SessionState.NEEDS_LOGIN:
                raise SessionError(
                    f"account {self.alias!r} needs to log in again ({self.reason})"
                ) from exc
            raise RetryableError(
                f"account {self.alias!r} is {self.state}"
                + (f" ({self.reason})" if self.reason else "")
                + " — the daemon is reconnecting; retry in a few seconds"
            ) from exc
        return self.client

    def note_update(self) -> None:
        self.last_update = time.time()

    # -- legacy bridge -----------------------------------------------------

    @property
    def wrapper(self) -> Any:
        """A `ClientWrapper` view of this session, for unmigrated v1 handlers.

        The wrapper is a v1 object with a `_client`/`_me` pair; building one
        around the supervised client lets the legacy routes keep working
        unchanged while the connection is owned here. It goes at PR-12 with
        `ClientWrapper` itself.
        """
        from tlgr.core.client import ClientWrapper

        if self._wrapper is None:
            self._wrapper = ClientWrapper.__new__(ClientWrapper)
            self._wrapper.session_path = self.session_path
            self._wrapper.api_id = self.options.api_id
            self._wrapper.api_hash = self.options.api_hash
            self._wrapper.flood_wait_max = self.options.flood_sleep_threshold
        self._wrapper._client = self.client
        self._wrapper._me = self.me
        return self._wrapper

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        from datetime import datetime, timezone

        def stamp(value: float | None) -> str | None:
            if not value:
                return None
            return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        info: dict[str, Any] = {
            "alias": self.alias,
            "state": self.state,
            "user_id": getattr(self.me, "id", None),
            "username": getattr(self.me, "username", None),
            "connected_since": stamp(self.connected_since),
            "last_update": stamp(self.last_update),
            "reconnects": self.reconnects,
            "catch_up_pending": self.catch_up_pending,
            "in_flight": self.in_flight,
            "resync_needed": list(self.resync_needed),
        }
        if self.reason:
            info["reason"] = self.reason
        if self.state != SessionState.ONLINE:
            info["since"] = stamp(self.since)
        return info


async def call_with_flood_budget(
    client: Any,
    request: Any,
    *,
    budget: int,
) -> tuple[Any, int]:
    """Run one raw request with a per-call flood budget (COR-15, ROB-06).

    Telethon's `flood_sleep_threshold` is a client-wide attribute, so v1's
    `--flood-wait-max` could not be honoured per request: whatever the last
    caller set applied to everyone. Setting it around the call, and restoring
    it after, is the only way to make the flag mean what it says.
    """
    previous = getattr(client, "flood_sleep_threshold", None)
    slept_from = time.monotonic()
    try:
        client.flood_sleep_threshold = budget
        result = await client(request)
    finally:
        if previous is not None:
            client.flood_sleep_threshold = previous
    slept = int(max(0.0, time.monotonic() - slept_from))
    return result, slept


Supervisor = Callable[[], Awaitable[None]]
