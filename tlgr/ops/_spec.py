"""`OperationSpec` — the one artefact per operation.

Adding an operation means adding one of these. The Click command, the daemon
dispatch entry, the JSON Schema, the reference docs and the contract tests are
all derived from it, so there is no second place for the description of an
operation to drift away from the first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeAlias, runtime_checkable

import msgspec

from tlgr.core.errors import EXIT_SUCCESS
from tlgr.core.pagination import PageKind

__all__ = [
    "Impl",
    "OpContext",
    "OperationSpec",
    "PageKind",
    "Surface",
]

RATE_CLASSES = frozenset({"read", "send", "resolve", "bulk", "file", "local"})


class Surface(str, Enum):
    """Where an operation runs."""

    DAEMON = "daemon"
    LOCAL = "local"
    EITHER = "either"


@runtime_checkable
class OpContext(Protocol):
    """What an implementation is handed besides its request.

    Deliberately a Protocol: `ops/` must not import `daemon/`, and a test must
    be able to build one without a socket or a Telethon client.
    """

    account: str
    dry_run: bool
    request_id: str

    def warn(self, message: str) -> None:
        """Add a non-fatal advisory to `meta.warnings`."""
        ...


Impl: TypeAlias = Callable[[Any, Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    # ---- identity ----
    id: str
    request: type[msgspec.Struct]
    response: Any
    impl: Impl
    summary: str
    description: str = ""
    #: Canonicalised to `id` before any policy check — in the CLI *and* the
    #: daemon. SEC-04 was exactly this gap: `--enable-commands message`
    #: allowed `message send` but not the `send` alias for the same op.
    aliases: tuple[str, ...] = ()
    # ---- behaviour flags ----
    mutating: bool = False
    destructive: bool = False
    paginated: PageKind | None = None
    stream: bool = False
    needs_account: bool = True
    needs_auth: bool = True
    surface: Surface = Surface.DAEMON
    idempotent: bool = False
    # ---- policy / limits ----
    timeout_s: int = 120
    rate_class: str = "read"
    min_interval_s: float = 0.0
    # ---- presentation ----
    columns: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    #: 0 or EXIT_EMPTY. Settles COR-36 centrally: lists exit 0 with an empty
    #: result; only point lookups and harvests that found nothing opt in to 3.
    empty_exit: int = EXIT_SUCCESS
    example: Any = None
    example_args: str = ""
    # ---- parity ----
    covers: tuple[str, ...] = ()
    covers_partial: tuple[str, ...] = ()
    coverage_note: str = ""
    # ---- migration ----
    #: v1 CLI paths this op replaces. They stay invocable, so no documented
    #: command path ever disappears (§12.4).
    legacy_paths: tuple[str, ...] = ()
    since: str = "2.0"
    deprecated: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def group(self) -> str:
        """The first path segment: `chat` for `chat.member.ban`."""
        return self.id.split(".")[0]

    @property
    def path(self) -> tuple[str, ...]:
        """The command path: `("chat", "member", "ban")`."""
        return tuple(self.id.split("."))

    @property
    def verb(self) -> str:
        return self.id.split(".")[-1]

    @property
    def cli_path(self) -> str:
        """How a human types it: `chat member ban`."""
        return " ".join(self.path)

    @property
    def names(self) -> tuple[str, ...]:
        """Every name this op answers to, canonical id first, deduplicated.

        A legacy path usually *is* the canonical path (`agent exit-codes`),
        and an alias may repeat one; both are normal, not a conflict.
        """
        seen: dict[str, None] = {}
        for name in (self.id, *self.aliases, *(p.replace(" ", ".") for p in self.legacy_paths)):
            seen.setdefault(name, None)
        return tuple(seen)
