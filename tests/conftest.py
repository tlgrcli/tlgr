"""Shared fixtures: a private `~/.tlgr`, and a real daemon on a temp socket.

The live-daemon fixture is the one that pays for itself. It starts the actual
aiohttp application, on an actual Unix socket, with the actual middleware
chain — and a fake Telethon client behind it. Everything the transport, the
dispatcher and the security controls do is therefore exercised end to end,
with no network and no session file, in a few milliseconds.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_telethon import World, fake_client_factory


@pytest.fixture
def tlgr_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated tlgr home, pointed at by `TLGR_HOME`.

    Deliberately *not* under `tmp_path`: a Unix socket path is capped at
    ~104 bytes on macOS and pytest's per-test directory alone is longer than
    that, so binding `<tmp_path>/tlgr/daemon.sock` fails with "AF_UNIX path
    too long" — which looks like a daemon bug and is not one.
    """
    root = Path(tempfile.mkdtemp(prefix="tlgr-t-", dir=tempfile.gettempdir()))
    home = root / "h"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("TLGR_HOME", str(home))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TLGR_IPC_TOKEN", raising=False)
    from tlgr.transport import client as transport_client

    transport_client.reset_default_client()
    try:
        yield home
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def stub_account(tlgr_home: Path) -> str:
    """A registered account with credentials but no real session file."""
    from tlgr.core.accounts import AccountManager

    manager = AccountManager(tlgr_home)
    manager.add_account("work")
    manager.save_credentials(12345, "0123456789abcdef0123456789abcdef", "work")
    return "work"


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
async def daemon(tlgr_home: Path, stub_account: str, world: World):
    """A `Daemon` with a fake client factory, services started, not bound."""
    from tlgr.daemon.app import Daemon

    factory = fake_client_factory(world)
    instance = Daemon(tlgr_home, client_factory=factory)
    await instance.start_services()
    try:
        yield instance
    finally:
        await instance.shutdown(drain=0.1)


@pytest.fixture
async def live_daemon(tlgr_home: Path, stub_account: str, world: World):
    """A daemon bound to a real Unix socket under `tlgr_home`.

    Yields the `Daemon`; talk to it with `DaemonClient(tlgr_home)`, which is
    what the CLI uses, so the test exercises the same code path a user does.
    """
    from tlgr.daemon.app import Daemon

    factory = fake_client_factory(world)
    instance = Daemon(tlgr_home, client_factory=factory)
    await instance.start_services()
    await instance.bind()
    await instance.sessions.connect_all([stub_account])
    instance.ready = True
    try:
        yield instance
    finally:
        await instance.shutdown(drain=0.1)


@pytest.fixture
def client(tlgr_home: Path):
    """A transport client for the temp home, with auto-start disabled.

    Auto-start is off because a test that accidentally forks a real daemon
    leaves a process behind and takes thirty seconds to say so.
    """
    from tlgr.transport.client import DaemonClient

    return DaemonClient(tlgr_home, timeout=10.0, auto_start=False)


@pytest.fixture
def in_thread():
    """Run a blocking call (the transport is synchronous) off the event loop."""

    async def run(func, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: func(*args, **kwargs))

    return run


@pytest.fixture(autouse=True)
def _restore_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    yield
    os.umask(previous)
