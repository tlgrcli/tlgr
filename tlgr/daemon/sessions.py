"""alias → `AccountSession`, with a lock per alias (COR-12).

v1's `ensure_client()` was `if alias not in self._clients: await connect()`.
Two concurrent requests for an account that was not yet connected both saw the
miss, both constructed a `TelegramClient` on the same session file, and the
second one's connect invalidated the first's auth key — `AUTH_KEY_DUPLICATED`,
which Telegram treats as a compromised session and revokes.

The fix is boring and complete: one `asyncio.Lock` per alias, a **double
check** inside it (the whole point — the first waiter has already done the
work by the time the second gets in), and a `flock` on the session file so
that even a second *process* cannot do what the second coroutine could not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from tlgr.core.accounts import AccountManager
from tlgr.core.config import AppConfig
from tlgr.core.errors import AccountNotFoundError, ConfigurationError
from tlgr.core.identity import load_identity
from tlgr.core.paths import TlgrPaths, validate_alias
from tlgr.daemon.ratelimit import RateLimiter
from tlgr.daemon.session import AccountSession, ClientOptions, SessionState, build_client

log = logging.getLogger("tlgr.daemon.sessions")

__all__ = ["SessionManager"]


class SessionManager:
    def __init__(
        self,
        paths: TlgrPaths,
        config: AppConfig,
        *,
        accounts: AccountManager | None = None,
        client_factory: Any = build_client,
        on_session_ready: Any = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.accounts = accounts or AccountManager(paths.base)
        self._factory = client_factory
        self._on_ready = on_session_ready
        self._sessions: dict[str, AccountSession] = {}
        self._limiters: dict[str, RateLimiter] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- accessors ---------------------------------------------------------

    def __contains__(self, alias: str) -> bool:
        return alias in self._sessions

    def get(self, alias: str) -> AccountSession | None:
        return self._sessions.get(alias)

    @property
    def aliases(self) -> list[str]:
        return list(self._sessions)

    def limiter(self, alias: str) -> RateLimiter:
        """The account's rate limiter, created on first use.

        Created lazily rather than with the session because a flood deadline
        outlives a connection: an account that failed to connect still owes the
        wait it earned before it dropped.
        """
        if alias not in self._limiters:
            validate_alias(alias)
            buckets = {
                name: (bucket.rate, bucket.burst) for name, bucket in self.config.rate.items()
            }
            self._limiters[alias] = RateLimiter(
                buckets=buckets,
                flood_path=self.paths.flood(alias),
                persist=self.config.flood.persist,
                sleep_threshold=self.config.flood.sleep_threshold,
                max_wait=self.config.flood.max_wait,
            )
        return self._limiters[alias]

    def _lock(self, alias: str) -> asyncio.Lock:
        if alias not in self._locks:
            self._locks[alias] = asyncio.Lock()
        return self._locks[alias]

    # -- construction ------------------------------------------------------

    def _options(self, alias: str) -> ClientOptions:
        api_id, api_hash = self.accounts.load_credentials(alias)
        if not api_id or not api_hash:
            raise ConfigurationError(
                f"account {alias!r} has no API credentials. Run: tlgr account add --alias {alias}"
            )
        identity = load_identity(
            self.paths.base,
            device_model=self.config.identity.device_model,
            system_version=self.config.identity.system_version,
            lang_code=self.config.identity.lang_code,
            system_lang_code=self.config.identity.system_lang_code,
        )
        return ClientOptions(
            api_id=int(api_id),
            api_hash=str(api_hash),
            entity_cache_limit=self.config.limits.entity_cache,
            request_retries=self.config.limits.request_retries,
            connect_timeout=self.config.network.connect_timeout,
            flood_sleep_threshold=self.config.flood.sleep_threshold,
            device_model=identity.device_model,
            system_version=identity.system_version,
            app_version=identity.app_version,
            lang_code=identity.lang_code,
            system_lang_code=identity.system_lang_code,
            proxy=_proxy_tuple(self.config.network.proxy),
            use_ipv6=self.config.network.ipv6,
            params=identity.params() if self.config.identity.tz_offset else {},
        )

    def _make(self, alias: str) -> AccountSession:
        def on_state(state: str, reason: str, user_id: int | None) -> None:
            with contextlib.suppress(Exception):
                self.accounts.set_health(alias, state, reason=reason, user_id=user_id)

        return AccountSession(
            alias,
            session_path=self.paths.session(alias),
            lock_path=self.paths.session_lock(alias),
            options=self._options(alias),
            client_factory=self._factory,
            on_state=on_state,
            state_save_interval=self.config.daemon.state_save_interval,
            presence=self.config.presence.mode,
            resync_depth=self.config.daemon.resync_depth,
            peers_path=self.paths.peers_db(alias),
            dialog_scan_max=self.config.limits.dialog_scan_max,
        )

    # -- the one entry point -----------------------------------------------

    async def ensure(self, alias: str) -> AccountSession:
        """Return the account's session, connecting it on demand — exactly once."""
        validate_alias(alias)
        existing = self._sessions.get(alias)
        if existing is not None:
            return existing

        async with self._lock(alias):
            # The double check is the fix: by the time a second caller reaches
            # here, the first has already built and started the session.
            existing = self._sessions.get(alias)
            if existing is not None:
                return existing
            if not self.accounts.get_account(alias) and not self.paths.account_dir(alias).exists():
                raise AccountNotFoundError(f"no account named {alias!r}. Run: tlgr account list")
            session = self._make(alias)
            await session.start()
            self._sessions[alias] = session
            if self._on_ready is not None:
                with contextlib.suppress(Exception):
                    await self._on_ready(session)
            return session

    async def connect_all(self, aliases: list[str]) -> dict[str, str]:
        """Connect the start-up list concurrently; failures do not block readiness.

        The list is ordered (§6.1) and the results are reported per alias, so
        one account with a revoked session cannot stop the others from working
        — which is what "connect them in a loop and raise" did in v1.
        """
        results: dict[str, str] = {}

        async def connect(alias: str) -> None:
            try:
                session = await self.ensure(alias)
                results[alias] = session.state
            except Exception as exc:
                results[alias] = f"error: {exc}"
                log.warning(
                    "could not connect account %s: %s", alias, exc, extra={"account": alias}
                )

        await asyncio.gather(*(connect(alias) for alias in aliases))
        return results

    # -- shutdown ----------------------------------------------------------

    async def stop_all(self, *, timeout: float = 10.0) -> None:
        await asyncio.gather(
            *(session.stop(timeout=timeout) for session in list(self._sessions.values())),
            return_exceptions=True,
        )
        self._sessions.clear()
        for limiter in self._limiters.values():
            limiter.flood.save()

    async def release(self, alias: str) -> None:
        session = self._sessions.pop(alias, None)
        if session is not None:
            await session.stop()

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """One row per account, in the order they were connected."""
        rows: list[dict[str, Any]] = []
        for alias, session in self._sessions.items():
            row = session.snapshot()
            row.update(self.limiter(alias).snapshot())
            rows.append(row)
        for info in self.accounts.list_accounts():
            if info.alias in self._sessions:
                continue
            health = info.health
            rows.append(
                {
                    "alias": info.alias,
                    "state": health.state if health.state != "unknown" else "not_connected",
                    "reason": health.reason or None,
                    "since": health.since or None,
                    "user_id": info.user_id,
                }
            )
        return rows

    @property
    def in_flight(self) -> int:
        return sum(session.in_flight for session in self._sessions.values())

    @property
    def online(self) -> list[str]:
        return [
            alias
            for alias, session in self._sessions.items()
            if session.state == SessionState.ONLINE
        ]


def _proxy_tuple(spec: str) -> Any:
    """`socks5://user:pass@host:1080` → the tuple Telethon wants, or None.

    Returned as a dict for `python-socks`-style proxies because the tuple form
    silently ignores authentication on some Telethon versions.
    """
    if not spec:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(spec)
    scheme = (parsed.scheme or "").lower()
    if not parsed.hostname or not parsed.port:
        raise ConfigurationError(f"[network] proxy is not a URL with a host and port: {spec!r}")
    if scheme in ("socks5", "socks4", "http"):
        proxy: dict[str, Any] = {
            "proxy_type": scheme,
            "addr": parsed.hostname,
            "port": int(parsed.port),
            "rdns": True,
        }
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy
    if scheme == "mtproxy":
        secret = (parsed.fragment or "").strip()
        if not secret:
            raise ConfigurationError("an mtproxy:// proxy needs its secret after a '#'")
        return (parsed.hostname, int(parsed.port), secret)
    raise ConfigurationError(f"unsupported proxy scheme {scheme!r}")
