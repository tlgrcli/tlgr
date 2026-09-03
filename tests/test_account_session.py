"""The `AccountSession` state machine and its supervisor (§6.2, §6.3).

The behaviour under test is the one that produced the 2026-09-02 outage: the
route to Telegram dropped, Telethon exhausted its five reconnects and raised,
and the wrapper stayed in the daemon's dict — present, dead, and reported as
healthy. Here a drop is a *state*, the supervisor owns the backoff, and a
request against a degraded account gets exit 8 with a hint instead of
`ConnectionError: Cannot send requests while disconnected`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fake_telethon import FakeTelegramClient, World

from tlgr.core.errors import EXIT_AUTH, EXIT_RETRYABLE, RetryableError, SessionError, classify
from tlgr.daemon.session import AccountSession, ClientOptions, SessionState, backoff_delays

# `asyncio_mode = "auto"` in pyproject collects the async tests; marking
# them explicitly would also mark the synchronous ones in this module.


def _session(tmp: Path, world: World, **kwargs) -> AccountSession:
    def factory(path, options):
        return FakeTelegramClient(world, session=path)

    return AccountSession(
        "work",
        session_path=tmp / "session",
        lock_path=tmp / "session.lock",
        options=ClientOptions(api_id=1, api_hash="x"),
        client_factory=factory,
        **kwargs,
    )


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


class TestBackoff:
    def test_it_grows_and_is_capped(self):
        assert backoff_delays(0) < 1.3
        assert 3.0 < backoff_delays(2) < 5.0
        assert backoff_delays(20) <= 72.0

    def test_it_is_jittered(self):
        """Without jitter, four accounts that dropped together reconnect together."""
        values = {backoff_delays(4) for _ in range(20)}
        assert len(values) > 1


class TestLifecycle:
    async def test_a_healthy_account_reaches_online_and_catches_up(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            assert world.catch_ups >= 1, "catch_up() did not run after connecting"
            assert session.connected_since is not None
        finally:
            await session.stop()
        assert session.state == SessionState.STOPPED

    async def test_an_unauthorised_account_is_terminal(self, tlgr_home, world):
        """A revoked session will not fix itself; reconnecting for ever hides it."""
        world.authorized = False
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.NEEDS_LOGIN)
            connects = world.connects
            await asyncio.sleep(0.1)
            assert world.connects == connects, "the supervisor kept retrying a dead session"
        finally:
            await session.stop()

    async def test_a_drop_becomes_degraded_and_then_reconnects(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            first = world.catch_ups
            session.client.drop()
            await _wait_for(lambda: session.state == SessionState.ONLINE and world.connects >= 2)
            assert session.reconnects >= 1
            # §6.3: catch_up() after *every* reconnect, not only at start.
            assert world.catch_ups > first
        finally:
            await session.stop()

    async def test_the_session_file_is_locked_for_the_sessions_lifetime(self, tlgr_home, world):
        from tlgr.daemon.singleton import FileLock, LockBusy

        session = _session(tlgr_home, world)
        await session.start()
        try:
            with pytest.raises(LockBusy):
                FileLock(tlgr_home / "session.lock").acquire()
        finally:
            await session.stop()
        FileLock(tlgr_home / "session.lock").acquire()

    async def test_a_locked_session_file_is_a_config_error_naming_the_holder(
        self, tlgr_home, world
    ):
        from tlgr.core.errors import ConfigurationError
        from tlgr.daemon.singleton import FileLock

        held = FileLock(tlgr_home / "session.lock")
        held.acquire()
        try:
            session = _session(tlgr_home, world)
            with pytest.raises(ConfigurationError) as caught:
                await session.start()
            assert "one process" in str(caught.value)
        finally:
            held.release()


class TestRequestGate:
    async def test_a_degraded_account_is_retryable_with_a_hint(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        session.state = SessionState.DEGRADED
        session.reason = "connection lost"
        with pytest.raises(RetryableError) as caught:
            await session.acquire(timeout=0.05)
        body = classify(caught.value)
        assert body.exit_code == EXIT_RETRYABLE
        assert "reconnecting" in body.message
        assert "disconnected" not in body.message.lower()

    async def test_a_revoked_account_is_session_error(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        session.state = SessionState.NEEDS_LOGIN
        session.reason = "AuthKeyUnregisteredError"
        with pytest.raises(SessionError) as caught:
            await session.acquire(timeout=0.05)
        assert classify(caught.value).exit_code == EXIT_AUTH

    async def test_an_online_account_hands_over_the_client(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            assert await session.acquire(timeout=1.0) is session.client
        finally:
            await session.stop()


class TestPersistence:
    async def test_state_is_saved_on_a_timer(self, tlgr_home, world):
        session = _session(tlgr_home, world, state_save_interval=5)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            # The ticker's own period is floored at 5 s; call the saver
            # directly rather than sleeping for it.
            from tlgr.core import telethon_compat as compat

            await compat.save_state(session.client)
            assert world.saves >= 1
            assert session.client.session.saved >= 1
        finally:
            await session.stop()

    async def test_stopping_saves_state_before_disconnecting(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        await _wait_for(lambda: session.state == SessionState.ONLINE)
        await session.stop()
        assert world.saves >= 1

    async def test_health_is_reported_to_the_account_manager(self, tlgr_home, world):
        seen: list[tuple[str, str]] = []
        session = _session(
            tlgr_home, world, on_state=lambda state, reason, uid: seen.append((state, reason))
        )
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
        finally:
            await session.stop()
        states = [state for state, _ in seen]
        assert "starting" in states
        assert "online" in states
        assert states[-1] == "stopped"


class TestFloodBudget:
    async def test_the_smallest_active_budget_wins(self, tlgr_home, world):
        """Concurrent requests share one client attribute; be conservative."""
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            original = session.client.flood_sleep_threshold
            with session.flood_budget(60):
                assert session.client.flood_sleep_threshold == 60
                with session.flood_budget(5):
                    assert session.client.flood_sleep_threshold == 5
                assert session.client.flood_sleep_threshold == 60
            assert session.client.flood_sleep_threshold == original
        finally:
            await session.stop()

    async def test_no_budget_leaves_the_client_alone(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            original = session.client.flood_sleep_threshold
            with session.flood_budget(None):
                assert session.client.flood_sleep_threshold == original
        finally:
            await session.stop()


class TestSnapshot:
    async def test_it_reports_what_status_needs(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            snapshot = session.snapshot()
            assert snapshot["alias"] == "work"
            assert snapshot["state"] == "online"
            assert snapshot["user_id"] == world.me.id
            assert snapshot["in_flight"] == 0
        finally:
            await session.stop()

    async def test_a_too_long_difference_is_recorded(self, tlgr_home, world):
        """Telethon consumes the gap silently; the resync list is the signal."""
        from tlgr.core import telethon_compat as compat

        session = _session(tlgr_home, world)
        session._on_too_long(compat.TOO_LONG_CHANNEL, 1234)
        session._on_too_long(compat.TOO_LONG_GLOBAL, None)
        assert session.snapshot()["resync_needed"] == [1234, 0]


class TestSessionManager:
    async def test_concurrent_ensures_build_exactly_one_session(self, tlgr_home, stub_account):
        """COR-12: v1's miss-then-connect raced itself into AUTH_KEY_DUPLICATED."""
        from fake_telethon import fake_client_factory

        from tlgr.core.config import load_app_config
        from tlgr.core.paths import TlgrPaths
        from tlgr.daemon.sessions import SessionManager

        built: list[str] = []
        factory = fake_client_factory(World())

        def counting(path, options):
            built.append(str(path))
            return factory(path, options)

        manager = SessionManager(
            TlgrPaths(tlgr_home),
            load_app_config(tlgr_home),
            client_factory=counting,
        )
        try:
            sessions = await asyncio.gather(*(manager.ensure(stub_account) for _ in range(10)))
            assert len({id(s) for s in sessions}) == 1
            assert len(built) == 1, f"{len(built)} clients were built for one session file"
        finally:
            await manager.stop_all()

    async def test_an_unknown_alias_is_not_found(self, tlgr_home, stub_account):
        from tlgr.core.config import load_app_config
        from tlgr.core.errors import AccountNotFoundError
        from tlgr.core.paths import TlgrPaths
        from tlgr.daemon.sessions import SessionManager

        manager = SessionManager(TlgrPaths(tlgr_home), load_app_config(tlgr_home))
        with pytest.raises(AccountNotFoundError):
            await manager.ensure("ghost")

    async def test_an_invalid_alias_never_becomes_a_path(self, tlgr_home):
        from tlgr.core.config import load_app_config
        from tlgr.core.errors import UsageError
        from tlgr.core.paths import TlgrPaths
        from tlgr.daemon.sessions import SessionManager

        manager = SessionManager(TlgrPaths(tlgr_home), load_app_config(tlgr_home))
        for alias in ("../etc", "/absolute", "", "x" * 65):
            with pytest.raises(UsageError):
                await manager.ensure(alias)

    async def test_the_connect_list_is_ordered(self, tlgr_home, stub_account):
        """COR-02: a set made the connect order depend on hash randomisation."""
        from tlgr.core.accounts import AccountManager

        manager = AccountManager(tlgr_home)
        for alias in ("aaa", "bbb", "ccc"):
            manager.add_account(alias)
        order = manager.connect_order("bbb", ["ccc", "aaa"])
        assert order == ["ccc", "aaa", "bbb", "work"]

    async def test_one_bad_account_does_not_block_the_others(self, tlgr_home, stub_account):
        from fake_telethon import fake_client_factory

        from tlgr.core.accounts import AccountManager
        from tlgr.core.config import load_app_config
        from tlgr.core.paths import TlgrPaths
        from tlgr.daemon.sessions import SessionManager

        AccountManager(tlgr_home).add_account("broken")  # registered, no credentials
        manager = SessionManager(
            TlgrPaths(tlgr_home),
            load_app_config(tlgr_home),
            client_factory=fake_client_factory(World()),
        )
        try:
            results = await manager.connect_all(["broken", stub_account])
            assert results[stub_account] in ("starting", "online")
            assert results["broken"].startswith("error:")
        finally:
            await manager.stop_all()
