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


def _refused_connection():
    """A Telethon connection class whose every connect is refused, instantly.

    What a laptop with no network looks like to Telethon: `_try_connect`
    catches the `OSError`, sleeps `retry_delay` and tries again.
    """
    from telethon.network.connection import ConnectionTcpFull

    class Refused(ConnectionTcpFull):
        async def _connect(self, timeout=None, ssl=None):
            raise ConnectionRefusedError("network is unreachable (test)")

    return Refused


def _seed_update_state(path: Path, pts: int) -> None:
    import datetime

    from telethon.sessions import SQLiteSession
    from telethon.tl import types

    session = SQLiteSession(str(path))
    stamp = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
    session.set_update_state(0, types.updates.State(pts, 7, stamp, 3, 0))
    session.save()
    session.close()


def _saved_pts(path: Path) -> int:
    from telethon.sessions import SQLiteSession

    session = SQLiteSession(str(path))
    try:
        state = session.get_update_state(0)
        return int(state.pts) if state is not None else -1
    finally:
        session.close()


class TestReconnect:
    """tlgrcli/tlgr#60: catch-up after reconnect, and who owns the backoff."""

    def test_telethon_gets_a_finite_retry_count(self, tlgr_home):
        """`None` is Telethon's "retry forever", not "off"."""
        from tlgr.daemon.session import build_client

        client = build_client(tlgr_home / "work", ClientOptions(api_id=1, api_hash="x"))
        try:
            retries = client._sender._retries
            assert isinstance(retries, int) and 0 <= retries <= 5
        finally:
            client.session.close()

    async def test_starting_offline_degrades_instead_of_hanging(self, tlgr_home):
        """A real Telethon client with every connect refused.

        With `connection_retries=None` `client.connect()` never returned and
        the account sat in `starting` for as long as the network was down.
        """
        from tlgr.daemon.session import build_client

        session = AccountSession(
            "work",
            session_path=tlgr_home / "work",
            lock_path=tlgr_home / "session.lock",
            options=ClientOptions(api_id=1, api_hash="x", connection=_refused_connection()),
            client_factory=build_client,
        )
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.DEGRADED, timeout=15.0)
            assert "ConnectionError" in session.reason
        finally:
            await session.stop()

    async def test_stopping_before_the_first_connect_keeps_the_saved_update_state(self, tlgr_home):
        """Telethon would store its empty `MessageBox` as `pts=0`.

        The next start would then begin from "now" and drop everything that
        arrived while the daemon was down.
        """
        from tlgr.daemon.session import build_client

        path = tlgr_home / "work"
        _seed_update_state(path, 4242)
        session = AccountSession(
            "work",
            session_path=path,
            lock_path=tlgr_home / "session.lock",
            options=ClientOptions(
                api_id=1, api_hash="x", connection=_refused_connection(), connection_retries=0
            ),
            client_factory=build_client,
        )
        await session.start()
        await asyncio.sleep(0.5)
        await session.stop()

        assert _saved_pts(path) == 4242

    async def test_telethons_automatic_reconnect_triggers_a_catch_up(self, tlgr_home, world):
        """The hook must sit where `MTProtoSender` calls it, not on the client."""
        from types import SimpleNamespace

        def factory(path, options):
            client = FakeTelegramClient(world, session=path)
            client._sender = SimpleNamespace(_auto_reconnect_callback=None)
            return client

        session = AccountSession(
            "work",
            session_path=tlgr_home / "session",
            lock_path=tlgr_home / "session.lock",
            options=ClientOptions(api_id=1, api_hash="x"),
            client_factory=factory,
        )
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            before = world.catch_ups
            # What `MTProtoSender._reconnect` does after it reconnects.
            await session.client._sender._auto_reconnect_callback()
            assert world.catch_ups == before + 1
            assert session.reconnects == 1
        finally:
            await session.stop()

    async def test_telethon_giving_up_is_a_drop_and_is_caught_up(self, tlgr_home, world):
        """Telethon resolves `disconnected` with the last error when its retries run out."""
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            first = world.catch_ups
            client = session.client
            client._connected = False
            client.disconnected.set_exception(ConnectionError("Connection to Telegram failed"))
            await _wait_for(lambda: session.state == SessionState.ONLINE and world.connects >= 2)
            assert session.reconnects == 1
            assert world.catch_ups > first
        finally:
            await session.stop()

    async def test_the_dead_connection_is_torn_down_before_reconnecting(self, tlgr_home, world):
        """Otherwise Telethon's stale update loop would run next to a new one."""
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            client = session.client
            order: list[str] = []
            real_disconnect, real_connect = client.disconnect, client.connect

            async def disconnect():
                order.append("disconnect")
                await real_disconnect()

            async def connect():
                order.append("connect")
                return await real_connect()

            client.disconnect, client.connect = disconnect, connect
            client.drop()
            await _wait_for(lambda: session.state == SessionState.ONLINE and world.connects >= 2)
            assert order[:2] == ["disconnect", "connect"]
        finally:
            await session.stop()


class _BlockingDialogs:
    """An `iter_dialogs` that does not finish until the test says so."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0
        self.fail = False

    def __call__(self, limit=None, **_):
        self.started += 1
        return self._iterate()

    async def _iterate(self):
        await self.release.wait()
        if self.fail:
            raise ConnectionError("dropped mid-sweep")
        yield object()


class TestDialogWarmup:
    """tlgrcli/tlgr#60: the dialog sweep must not gate `online`."""

    def _session(self, tmp: Path, world: World, dialogs: _BlockingDialogs) -> AccountSession:
        def factory(path, options):
            client = FakeTelegramClient(world, session=path)
            client.iter_dialogs = dialogs
            return client

        return AccountSession(
            "work",
            session_path=tmp / "session",
            lock_path=tmp / "session.lock",
            options=ClientOptions(api_id=1, api_hash="x"),
            client_factory=factory,
        )

    async def test_the_account_is_online_while_the_sweep_runs(self, tlgr_home, world):
        dialogs = _BlockingDialogs()
        session = self._session(tlgr_home, world, dialogs)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            assert world.catch_ups >= 1
            await _wait_for(lambda: dialogs.started == 1)
            assert await session.acquire(timeout=0.1) is session.client
            dialogs.release.set()
            await _wait_for(lambda: session._warmed)
        finally:
            await session.stop()

    async def test_a_finished_sweep_is_not_repeated_after_a_reconnect(self, tlgr_home, world):
        dialogs = _BlockingDialogs()
        dialogs.release.set()
        session = self._session(tlgr_home, world, dialogs)
        await session.start()
        try:
            await _wait_for(lambda: session._warmed)
            session.client.drop()
            await _wait_for(lambda: session.state == SessionState.ONLINE and world.connects >= 2)
            assert dialogs.started == 1
        finally:
            await session.stop()

    async def test_a_failed_sweep_runs_again_on_the_next_connect(self, tlgr_home, world):
        dialogs = _BlockingDialogs()
        dialogs.fail = True
        dialogs.release.set()
        session = self._session(tlgr_home, world, dialogs)
        await session.start()
        try:
            await _wait_for(lambda: session._warmup is not None and session._warmup.done())
            assert not session._warmed
            dialogs.fail = False
            session.client.drop()
            await _wait_for(lambda: session._warmed)
            assert dialogs.started == 2
        finally:
            await session.stop()

    async def test_stop_cancels_a_running_sweep(self, tlgr_home, world):
        dialogs = _BlockingDialogs()
        session = self._session(tlgr_home, world, dialogs)
        await session.start()
        await _wait_for(lambda: session._warmup is not None)
        task = session._warmup
        await session.stop()
        assert task is not None and task.cancelled()


class TestPeriodicCatchUp:
    """`[daemon] catch_up_interval`, off by default, floored at 30 s."""

    def test_the_interval_is_off_by_default_and_floored(self):
        from tlgr.core.config import DaemonConfig
        from tlgr.daemon.session import MIN_CATCH_UP_INTERVAL, normalise_catch_up_interval

        assert DaemonConfig().catch_up_interval == 0
        assert MIN_CATCH_UP_INTERVAL == 30
        assert normalise_catch_up_interval(0) == 0
        assert normalise_catch_up_interval(-5) == 0
        assert normalise_catch_up_interval(None) == 0
        assert normalise_catch_up_interval(1) == 30
        assert normalise_catch_up_interval(45) == 45

    async def test_no_timer_runs_when_it_is_off(self, tlgr_home, world):
        session = _session(tlgr_home, world)
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            names = {task.get_name() for task in session._tickers}
            assert names
            assert "tlgr-catchup-work" not in names
        finally:
            await session.stop()

    async def test_the_timer_catches_up_while_online(self, tlgr_home, world):
        session = _session(tlgr_home, world, catch_up_interval=45)
        assert session.catch_up_interval == 45
        # The floor protects Telegram, not the test: shorten it after construction.
        session.catch_up_interval = 0.02
        await session.start()
        try:
            await _wait_for(lambda: session.state == SessionState.ONLINE)
            first = world.catch_ups
            await _wait_for(lambda: world.catch_ups >= first + 2)
        finally:
            await session.stop()

    async def test_the_session_manager_passes_the_config_through(self, tlgr_home, stub_account):
        from fake_telethon import fake_client_factory

        from tlgr.core.config import load_app_config
        from tlgr.core.paths import TlgrPaths
        from tlgr.daemon.sessions import SessionManager

        (tlgr_home / "config.toml").write_text("[daemon]\ncatch_up_interval = 10\n")
        manager = SessionManager(
            TlgrPaths(tlgr_home),
            load_app_config(tlgr_home),
            client_factory=fake_client_factory(World()),
        )
        try:
            session = await manager.ensure(stub_account)
            assert session.catch_up_interval == 30
        finally:
            await manager.stop_all()
