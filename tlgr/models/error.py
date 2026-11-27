"""The wire error shape and the stable machine names that go in it."""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = ["ErrorBody"]


class ErrorBody(Model):
    """One error, everywhere: daemon response body, CLI JSON output, event.

    `code` is the stable machine name and `exit_code` is what the CLI exits
    with; the two are looked up together from a single table (`core.errors`)
    so a new error can never arrive with a plausible-looking wrong exit.
    """

    code: str
    message: str
    exit_code: int
    retryable: bool = False
    wait_seconds: int | None = None
    field: str | None = None
    hint: str | None = None
    rpc: dict[str, Any] | None = None
    account: str | None = None
    request_id: str | None = None
    reason: str | None = None
    #: Structured data for a caller that acts on the error, for example the
    #: alias candidates of PEER_NEEDS_USER. Absent unless the error has some.
    details: dict[str, Any] | None = None
