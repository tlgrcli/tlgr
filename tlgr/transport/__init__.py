"""The client half of the v2 wire protocol (§5).

`cli/` reaches the daemon only through this package: one connection story,
one place timeouts and retries are decided, one place an error envelope
becomes an exception.
"""

from __future__ import annotations

from tlgr.transport.client import (
    DaemonClient,
    admin,
    events,
    legacy_request,
    make_dispatcher,
    make_stream_dispatcher,
    op,
    set_default_flood_wait_max,
    status,
    stream,
)

__all__ = [
    "DaemonClient",
    "admin",
    "events",
    "legacy_request",
    "make_dispatcher",
    "make_stream_dispatcher",
    "op",
    "set_default_flood_wait_max",
    "status",
    "stream",
]
