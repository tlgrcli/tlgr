"""The narrow client view a background job is handed.

v1 passed the job engine a `ClientWrapper` — a 460-line object that owned a
Telethon client, logged in, logged out, serialised messages and answered the
v1 IPC routes. PR-12 deleted it: the daemon owns the connection now, and a
job has no business logging anything in or out.

What a job genuinely needs is two things, and this Protocol is exactly those
two: the raw Telethon client to attach handlers to and to send with, and a
resolver so a YAML file can name a destination as `@channel` rather than as a
marked id. Anything that satisfies them is a job client; the daemon's session
supplies one (`daemon/session.py`), and a test can supply one in four lines.

A Protocol rather than a base class because `jobs/` must not import
`daemon/`: the layering lint in `tests/test_layering.py` is what keeps the
job engine testable without a socket.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["JobClient"]


@runtime_checkable
class JobClient(Protocol):
    """What a background job may do with the account it runs on."""

    @property
    def client(self) -> Any:
        """The connected Telethon client, owned by whoever supplied it."""
        ...

    async def resolve_chat(self, chat_ref: str) -> int:
        """`@channel`, a marked id or a t.me link → the marked chat id."""
        ...
