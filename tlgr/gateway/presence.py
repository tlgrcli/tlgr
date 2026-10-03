"""Online status around job actions, per account.

Telethon never calls `account.updateStatus`, so an account driven by tlgr
reads as offline while it reacts and replies, which is exactly the pattern
that makes automation obvious. The modes:

* `leave` (the default): never touch presence;
* `blip`: online just before the action, offline about five seconds after.
  Overlapping blips merge, so a burst does not flap online/offline;
* `session`: online while any action of this account is running or due
  within a few seconds, offline about five seconds after the last.

Presence is one fact per account, so two jobs asking for different modes have
to agree. The rule: the most "online" request wins while its actions run. A
`leave` action never sends anything; it neither turns presence on nor ends an
online stretch a `blip` or `session` action started. Offline is only sent once
no `blip`/`session` action is running and, for `session`, none is due soon.

If the account-level `[presence] mode` is not `off`, the daemon already owns
the account's status and this manager stays silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("tlgr.gateway.presence")

__all__ = ["PresenceManager"]

#: How long the account stays online after its last action.
LINGER_S = 5.0
#: A `session` stays online if another `session` action is due this soon.
LOOKAHEAD_S = 5.0


class PresenceManager:
    def __init__(
        self,
        run_op: Callable[[str, dict[str, Any]], Awaitable[Any]],
        clock: Any,
        *,
        enabled: bool = True,
        session_due_soon: Callable[[float], bool] | None = None,
    ) -> None:
        self._run_op = run_op
        self._clock = clock
        self.enabled = enabled
        self._session_due_soon = session_due_soon or (lambda _within: False)
        self.online = False
        self._holds = 0
        self._linger_until = 0.0
        self._offline_task: asyncio.Task[None] | None = None

    async def before(self, mode: str) -> None:
        if not self.enabled or mode == "leave":
            return
        self._holds += 1
        if not self.online:
            await self._set(online=True)

    async def after(self, mode: str) -> None:
        if not self.enabled or mode == "leave":
            return
        self._holds = max(0, self._holds - 1)
        self._linger_until = max(self._linger_until, self._clock.now() + LINGER_S)
        if self._offline_task is None or self._offline_task.done():
            self._offline_task = asyncio.create_task(self._go_offline_later())

    async def _go_offline_later(self) -> None:
        while self.online:
            wait = self._linger_until - self._clock.now()
            if wait > 0:
                await self._clock.sleep(wait)
                continue
            if self._holds > 0:
                return  # the running action's `after` starts a new countdown
            if self._session_due_soon(LOOKAHEAD_S):
                self._linger_until = self._clock.now() + LINGER_S
                continue
            await self._set(online=False)

    async def _set(self, *, online: bool) -> None:
        self.online = online
        try:
            await self._run_op("profile.presence.set", {"state": "online" if online else "offline"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("presence update failed: %s", exc)

    async def stop(self) -> None:
        task, self._offline_task = self._offline_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.enabled and self.online:
            await self._set(online=False)
