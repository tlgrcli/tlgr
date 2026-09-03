"""Request and response envelopes — data only; the transport lives elsewhere.

These are defined in `models/` (not `transport/`) because both ends of the
socket and the CLI renderer need the same shape, and because a test should be
able to build one without opening a socket.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model, Request
from tlgr.models.error import ErrorBody
from tlgr.models.page import PageInfo

__all__ = ["ErrEnvelope", "Meta", "OkEnvelope", "OpRequest"]


class OpRequest(Request):
    """`POST /v1/op` body.

    `limit`/`cursor`/`all` sit beside the request rather than inside it so
    that pagination is uniform across every op and no request struct has to
    redeclare it (registry lint L5 forbids those field names).
    """

    op: str
    account: str = ""
    request: dict[str, Any] = {}
    dry_run: bool = False
    flood_wait_max: int | None = None
    request_id: str = ""
    client_version: str = ""
    protocol: int = 2
    stream: bool = False
    limit: int | None = None
    cursor: str | None = None
    all: bool = False


class Meta(Model):
    request_id: str = ""
    elapsed_ms: int = 0
    flood_wait_slept: int = 0
    warnings: list[str] = []
    # `already: true` is how an idempotent no-op is reported. It is not an
    # error: MESSAGE_NOT_MODIFIED means the world already looks the way the
    # caller asked for, which is success.
    already: bool = False
    daemon_version: str = ""
    protocol: int = 2


class OkEnvelope(Model):
    ok: bool = True
    op: str = ""
    account: str | None = None
    result: Any = None
    page: PageInfo | None = None
    meta: Meta | None = None


class ErrEnvelope(Model):
    error: ErrorBody
    ok: bool = False
    op: str = ""
    account: str | None = None
    meta: Meta | None = None
