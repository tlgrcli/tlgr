"""Starting, not starting twice, and stopping cleanly (§5.8, §6.1, §6.11).

The stress test at the bottom is the one that matters. Two `tlgr` commands in
a shell pipeline could produce two daemons in v1, each holding the same
session files, which is how an account earns `AUTH_KEY_DUPLICATED`. Twenty
simultaneous probes must produce exactly one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tlgr.core.errors import DaemonNotRunningError, DaemonVersionMismatchError
from tlgr.daemon.singleton import FileLock, LockBusy
from tlgr.transport import autostart
from tlgr.transport.client import DaemonClient


class TestSingleton:
    def test_a_second_lock_is_refused_and_names_the_holder(self, tlgr_home: Path):
        first = FileLock(tlgr_home / "daemon.lock")
        first.acquire()
        try:
            with pytest.raises(LockBusy) as caught:
                FileLock(tlgr_home / "daemon.lock").acquire()
            assert caught.value.pid == os.getpid()
        finally:
            first.release()

    def test_the_lock_is_reusable_after_release(self, tlgr_home: Path):
        lock = FileLock(tlgr_home / "daemon.lock")
        lock.acquire()
        lock.release()
        lock.acquire()
        assert lock.held
        lock.release()

    def test_the_lock_file_is_private(self, tlgr_home: Path):
        lock = FileLock(tlgr_home / "daemon.lock")
        lock.acquire()
        try:
            assert (tlgr_home / "daemon.lock").stat().st_mode & 0o777 == 0o600
        finally:
            lock.release()


class TestPidFiles:
    def test_a_permission_error_is_not_not_running(self, tlgr_home: Path, monkeypatch):
        """COR-14c: EPERM means the pid is *taken*, by someone else."""
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        paths.pid.write_text("4242\n")

        def refuse(pid, sig):
            raise PermissionError("not yours")

        monkeypatch.setattr(os, "kill", refuse)
        assert autostart.read_pid(paths) == 4242
        assert paths.pid.exists(), "a pid file we may not signal was deleted"

    def test_a_dead_pid_is_not_running_and_is_left_alone(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        paths.pid.write_text("999999999\n")
        assert autostart.read_pid(paths) is None
        # v1 unlinked here. Reading is a read.
        assert paths.pid.exists()

    def test_a_live_daemons_socket_is_never_removed(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        paths.socket.touch()
        paths.pid.write_text(f"{os.getpid()}\n")
        autostart._remove_stale_socket(paths)
        assert paths.socket.exists()

    def test_a_socket_with_no_owner_is_removed(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        paths.socket.touch()
        autostart._remove_stale_socket(paths)
        assert not paths.socket.exists()


class TestAutoStart:
    def test_it_refuses_to_start_when_auto_start_is_off(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths

        with pytest.raises(DaemonNotRunningError):
            autostart.ensure_running(
                TlgrPaths(tlgr_home), lambda: None, auto_start=False, start_timeout=1
            )

    def test_it_does_not_fork_a_supervised_daemon(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths, write_private

        paths = TlgrPaths(tlgr_home)
        write_private(paths.state, '{"managed_by": "launchd", "protocol": 2}')
        with pytest.raises(DaemonNotRunningError) as caught:
            autostart.ensure_running(paths, lambda: None, auto_start=True, start_timeout=1)
        assert "launchd" in str(caught.value)

    def test_twenty_concurrent_probes_spawn_exactly_one_daemon(self, tlgr_home: Path):
        """The stress test from §12.3 item 7, with the spawn stubbed out."""
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        spawns: list[float] = []
        ready = {"value": False}

        def fake_spawn(base, python=None):
            spawns.append(time.monotonic())
            # A real daemon takes the singleton lock and starts answering.
            time.sleep(0.05)
            ready["value"] = True
            return None

        def probe():
            return {"daemon": {"protocol": 2}} if ready["value"] else None

        original = autostart.spawn_daemon
        autostart.spawn_daemon = fake_spawn  # type: ignore[assignment]
        try:
            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(
                    pool.map(
                        lambda _: autostart.ensure_running(
                            paths, probe, auto_start=True, start_timeout=5
                        ),
                        range(20),
                    )
                )
        finally:
            autostart.spawn_daemon = original  # type: ignore[assignment]

        assert len(spawns) == 1, f"{len(spawns)} daemons were spawned, not one"
        assert all(r["daemon"]["protocol"] == 2 for r in results)

    def test_readiness_is_an_http_200_not_a_socket_file(self, tlgr_home: Path):
        """ROB-07: the socket exists from bind(), long before the daemon works."""
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        paths.socket.touch()
        with pytest.raises(Exception):
            autostart.ensure_running(paths, lambda: None, auto_start=False, start_timeout=0.1)


class TestHandshake:
    def test_matching_protocols_proceed(self):
        assert autostart.check_protocol({"daemon": {"protocol": 2}}, client_protocol=2) == 0

    def test_an_older_daemon_asks_for_a_restart(self):
        assert autostart.check_protocol({"daemon": {"protocol": 1}}, client_protocol=2) == -1

    def test_a_newer_daemon_is_never_killed(self):
        """Whatever started it knows more than this CLI does."""
        with pytest.raises(DaemonVersionMismatchError):
            autostart.check_protocol({"daemon": {"protocol": 3}}, client_protocol=2)

    def test_no_daemon_restart_is_exit_11(self, tlgr_home: Path):
        from tlgr.core.errors import EXIT_DAEMON

        client = DaemonClient(tlgr_home, no_restart=True, auto_start=False)
        with pytest.raises(DaemonVersionMismatchError) as caught:
            client._restart_older_daemon({"daemon": {"protocol": 1}})
        assert caught.value.exit_code == EXIT_DAEMON
        assert "--no-daemon-restart" in str(caught.value)

    def test_a_restart_is_attempted_at_most_once(self, tlgr_home: Path, monkeypatch):
        client = DaemonClient(tlgr_home, auto_start=False)
        client._restarted = True
        with pytest.raises(DaemonVersionMismatchError) as caught:
            client._restart_older_daemon({"daemon": {"protocol": 1}})
        assert "once" in str(caught.value)


@pytest.mark.asyncio
class TestShutdown:
    async def test_the_socket_and_state_file_are_removed(self, live_daemon):
        socket = live_daemon.paths.socket
        state = live_daemon.paths.state
        assert socket.exists() and state.exists()
        await live_daemon.shutdown(drain=0.1)
        assert not socket.exists()
        assert not state.exists()

    async def test_it_waits_for_an_in_flight_request(self, live_daemon):
        """COR-11: a ten-minute scan is drained, not killed at second 599."""
        live_daemon.activity.in_flight = 1
        started = time.monotonic()

        async def release() -> None:
            import asyncio

            await asyncio.sleep(0.15)
            live_daemon.activity.in_flight = 0

        import asyncio

        await asyncio.gather(live_daemon.shutdown(drain=2.0), release())
        assert time.monotonic() - started >= 0.15

    async def test_the_drain_deadline_is_respected(self, live_daemon):
        """A request that never finishes must not hold the daemon for ever."""
        live_daemon.activity.in_flight = 1
        started = time.monotonic()
        await live_daemon.shutdown(drain=0.1)
        assert time.monotonic() - started < 2.0

    async def test_shutdown_is_idempotent(self, live_daemon):
        await live_daemon.shutdown(drain=0.1)
        await live_daemon.shutdown(drain=0.1)


@pytest.mark.asyncio
async def test_ready_is_false_before_the_accounts_connect(tlgr_home, stub_account, world):
    """§12.3 item 6: "process alive" and "daemon works" are different (COR-37)."""
    from fake_telethon import fake_client_factory

    from tlgr.daemon.app import Daemon

    daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
    await daemon.start_services()
    await daemon.bind()
    try:
        assert daemon.v1_status()["daemon"]["ready"] is False
        await daemon.connect_accounts()
        daemon.ready = True
        assert daemon.v1_status()["daemon"]["ready"] is True
    finally:
        await daemon.shutdown(drain=0.1)


def test_a_second_daemon_exits_zero(tlgr_home: Path):
    """COR-39: exit 1 under launchd's KeepAlive means "respawn me", for ever."""
    lock = FileLock(tlgr_home / "daemon.lock")
    lock.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tlgr.daemon.main", "--base", str(tlgr_home)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "already running" in result.stdout
    finally:
        lock.release()


def test_the_daemon_module_alias_still_exists():
    """`python -m tlgr.daemon.server` is in a v1 plist and in shell history."""
    from tlgr.daemon.server import Daemon, DaemonServer, main

    assert DaemonServer is Daemon
    assert callable(main)
