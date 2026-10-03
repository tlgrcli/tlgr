"""How a job action reaches Telegram: through the op layer, in process.

A job used to call Telethon directly (`client.forward_messages`,
`event.reply`), which meant none of what the CLI path guarantees applied to
it: no policy allow/deny, no rate limiter, no flood-wait budget, no
self-origin event on the bus, and none of the per-peer edge cases the ops
handle (`message.read` choosing between three read RPCs, `reaction.add`
reading the current set first). Running every action as an operation through
`dispatch.execute` gives a job the same guarantees as `tlgr message read`
typed by hand.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

__all__ = ["DaemonOpRunner", "OpRunner"]

#: The longest FLOOD_WAIT a job action sleeps off inside the request. A longer
#: wait comes back as RATE_LIMITED and the scheduler reschedules the item,
#: which frees the queue instead of holding it for the whole wait.
JOB_FLOOD_WAIT_MAX = 10


class OpRunner(Protocol):
    async def __call__(self, op: str, request: dict[str, Any]) -> Any: ...


class DaemonOpRunner:
    """Run an operation for one account through the daemon's dispatcher."""

    def __init__(self, daemon: Any, account: str) -> None:
        self.daemon = daemon
        self.account = account

    async def __call__(self, op: str, request: dict[str, Any]) -> Any:
        from tlgr.daemon import dispatch
        from tlgr.models.base import to_builtins
        from tlgr.models.envelope import OpRequest
        from tlgr.version import PROTOCOL, VERSION

        op_request = OpRequest(
            op=op,
            account=self.account,
            request=request,
            request_id=f"job-{uuid.uuid4().hex[:12]}",
            client_version=VERSION,
            protocol=PROTOCOL,
            flood_wait_max=JOB_FLOOD_WAIT_MAX,
        )
        # Counted as a request, so the shutdown drain waits for an action
        # that is half way through instead of cutting it off.
        with dispatch.in_flight(self.daemon):
            _, _, result = await dispatch.execute(self.daemon, op_request)
        return to_builtins(result) if result is not None else None
