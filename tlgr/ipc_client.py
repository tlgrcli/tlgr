"""v1's IPC entry point, kept as a thin shim over `tlgr.transport` (§12.4).

The hand-rolled HTTP client that used to live here is gone: `ipc_request` now
encodes its body with msgspec, builds query strings with `urlencode`, reads the
reply with `http.client` and raises the exception the daemon classified. Every
unmigrated v1 command therefore gets COR-04, COR-31, COR-32 and COR-06 fixed
without being touched.

This module is deleted at PR-12, when the last legacy command moves to the
registry. Until then it exists so that `from tlgr.ipc_client import
ipc_request` keeps working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tlgr.transport.client import DEFAULT_TIMEOUT, legacy_request

__all__ = ["ipc_request"]


def ipc_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    base: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Send one v1 IPC request and return its decoded body.

    `params` is the encoding-safe way to pass a query: v1 call sites built one
    with an f-string, which is why a Persian search term, a `#` in a chat title
    or a `+` in a phone number never survived the trip.
    """
    return legacy_request(method, path, body=body, params=params, base=base, timeout=timeout)
