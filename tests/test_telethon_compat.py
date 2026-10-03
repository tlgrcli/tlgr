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
