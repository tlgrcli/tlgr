"""`install_too_long_hook` against Telethon's real `MessageBox`.

The fake client has no message box, so nothing caught that `MessageBox`
declares `__slots__` and refuses instance assignment: every account logged
`AttributeError: 'MessageBox' object attribute 'apply_difference' is
read-only` on its first connect, burned one reconnect, and ran without the
hook.
"""

from __future__ import annotations

import contextlib
import logging

from telethon._updates.messagebox import MessageBox
from telethon.tl import types

from tlgr.core import telethon_compat as compat


class _Client:
    def __init__(self) -> None:
        self._message_box = MessageBox(logging.getLogger("test.messagebox"))


def _too_long() -> types.updates.DifferenceTooLong:
    return types.updates.DifferenceTooLong(pts=10)


class TestTooLongHook:
    def test_it_installs_on_a_real_message_box(self):
        client = _Client()
        assert compat.install_too_long_hook(client, lambda scope, channel: None)
        assert isinstance(client._message_box, MessageBox)

    def test_it_reports_a_global_gap(self):
        client = _Client()
        seen: list[tuple[str, int | None]] = []
        compat.install_too_long_hook(client, lambda scope, channel: seen.append((scope, channel)))

        # Telethon's own handling of the diff is not under test.
        with contextlib.suppress(Exception):
            client._message_box.apply_difference(_too_long(), None)

        assert seen == [(compat.TOO_LONG_GLOBAL, None)]

    def test_one_clients_hook_does_not_fire_for_another(self):
        hooked, plain = _Client(), _Client()
        seen: list[str] = []
        compat.install_too_long_hook(hooked, lambda scope, channel: seen.append(scope))

        with contextlib.suppress(Exception):
            plain._message_box.apply_difference(_too_long(), None)

        assert seen == []
        assert type(plain._message_box) is MessageBox


def _real_client(session: object = None):
    """A real, never-connected `TelegramClient`. Nothing here touches the network."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession

    return TelegramClient(session if session is not None else MemorySession(), 1, "x")


class TestReconnectHook:
    """tlgrcli/tlgr#60: the hook must reach the callback the sender actually runs."""

    async def test_the_senders_reconnect_callback_reaches_tlgr(self):
        client = _real_client()
        calls: list[str] = []

        async def on_reconnect() -> None:
            calls.append("reconnected")

        assert compat.install_reconnect_hook(client, on_reconnect)
        # `MTProtoSender._reconnect` runs exactly this after a successful
        # reconnect. Telethon's own handler inside it calls `get_me()`, which
        # fails here (never connected) and is logged by Telethon itself.
        await client._sender._auto_reconnect_callback()
        assert calls == ["reconnected"]

    async def test_telethons_own_handler_still_runs_first(self, monkeypatch):
        client = _real_client()
        order: list[str] = []

        async def telethon_handler() -> None:
            order.append("telethon")

        monkeypatch.setattr(client._sender, "_auto_reconnect_callback", telethon_handler)
        compat.install_reconnect_hook(client, lambda: order.append("tlgr"))
        await client._sender._auto_reconnect_callback()
        assert order == ["telethon", "tlgr"]

    def test_a_client_without_a_sender_degrades(self):
        class Bare:
            pass

        assert compat.install_reconnect_hook(Bare(), lambda: None) is False


class TestStateGuard:
    """Telethon writes an empty `MessageBox` over the session's update state."""

    @staticmethod
    def _seed(path) -> None:
        import datetime

        from telethon.sessions import SQLiteSession

        session = SQLiteSession(str(path))
        stamp = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
        session.set_update_state(0, types.updates.State(100, 7, stamp, 3, 0))
        session.save()
        session.close()

    @staticmethod
    def _pts(path) -> int:
        from telethon.sessions import SQLiteSession

        session = SQLiteSession(str(path))
        try:
            state = session.get_update_state(0)
            return int(state.pts) if state is not None else -1
        finally:
            session.close()

    async def test_disconnecting_a_never_connected_client_keeps_the_saved_pts(self, tmp_path):
        path = tmp_path / "work"
        self._seed(path)
        client = _real_client(str(path))
        assert compat.install_state_guard(client)
        assert not compat.update_state_loaded(client)

        await client.disconnect()

        assert self._pts(path) == 100

    async def test_save_state_skips_an_empty_box(self, tmp_path):
        path = tmp_path / "work"
        self._seed(path)
        client = _real_client(str(path))

        assert await compat.save_state(client) is False
        client.session.close()

        assert self._pts(path) == 100

    async def test_a_loaded_box_is_saved(self, tmp_path):
        """The guard must not stop the real periodic save."""
        from telethon._updates.session import SessionState

        path = tmp_path / "work"
        self._seed(path)
        client = _real_client(str(path))
        compat.install_state_guard(client)
        client._message_box.load(SessionState(0, 0, False, 250, 7, 1_790_000_000, 3, None), [])
        assert compat.update_state_loaded(client)

        await client._save_states_and_entities()
        client.session.save()
        client.session.close()

        assert self._pts(path) == 250
