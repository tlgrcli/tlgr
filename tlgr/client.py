"""`tlgr.Client`: the daemon, from Python, without a process per call.

Every `tlgr --json ...` invocation pays for a Python start and the CLI's
imports before it reaches the socket: about a second, against a daemon round
trip of a fifth of that. A script that sends two thousand messages through the
CLI spends most of its time starting Python. This module is the supported way
to pay that once: build one `Client`, then call `op()` as often as you like.

It is a thin layer over `tlgr.transport.client.DaemonClient`, the same
transport the CLI uses, so the wire, the envelope and the errors are the
CLI's own:

* `op()` returns the envelope `--json` prints (`ok`, `op`, `account`,
  `result`, `page`, `meta`);
* a failure raises the exception the daemon classified (`RateLimitError` with
  `wait_seconds`, `SpamFlagError`, `NotFoundError`, ...), each carrying the
  `code` and `exit_code` the CLI would have exited with;
* `stream()` yields the NDJSON frames of a streaming operation or a walk.

The guards travel with it rather than being left behind in the CLI:

* a home marked `.production` is refused unless `TLGR_ALLOW_PRODUCTION_HOME=1`
  (that check lives in `TlgrPaths`, which the transport builds);
* the account is never guessed. There is no fallback to `TLGR_ACCOUNT`, the
  configured default or the active alias: an operation that needs an account
  and was given none is refused by the daemon with `AccountRequiredError`.
  That is `require_account = true`, always;
* `TLGR_ENABLE_COMMANDS` (or `enable_commands=`) is honoured with the same
  canonicalising check the CLI applies, before the request leaves.

Importing this module does not import the CLI, the operation registry, click,
Telethon or aiohttp. `tests/test_client.py` holds that line.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from tlgr.core.errors import (
    AccountFrozenError,
    AccountNotFoundError,
    AccountRequiredError,
    AuthenticationError,
    ChatNotFoundError,
    ConfigurationError,
    DaemonError,
    DaemonNotRunningError,
    DaemonVersionMismatchError,
    IndeterminateError,
    IPCError,
    NotFoundError,
    NotSupportedError,
    PermissionError_,
    RateLimitError,
    RetryableError,
    SessionError,
    SpamFlagError,
    TlgrError,
    UsageError,
)
from tlgr.core.paths import validate_alias
from tlgr.transport.client import DaemonClient, RemoteError, _collect, error_from_body

__all__ = [
    "DEFAULT_TIMEOUT",
    "AccountFrozenError",
    "AccountNotFoundError",
    "AccountRequiredError",
    "AuthenticationError",
    "ChatNotFoundError",
    "Client",
    "ConfigurationError",
    "DaemonError",
    "DaemonNotRunningError",
    "DaemonVersionMismatchError",
    "IPCError",
    "IndeterminateError",
    "NotFoundError",
    "NotSupportedError",
    "PermissionError_",
    "RateLimitError",
    "RemoteError",
    "RetryableError",
    "SessionError",
    "SpamFlagError",
    "TlgrError",
    "UsageError",
]

#: How long a call waits for the daemon when no `timeout` is given.
#:
#: The CLI waits for each operation's own `timeout_s`, which it reads from the
#: registry; this module does not load the registry (that is most of the
#: startup it exists to avoid). The daemon enforces `timeout_s` around the
#: implementation either way and answers with a classified error, so the
#: client-side bound only has to be no shorter than the longest of them: a
#: client that gave up first would report "unknown" for a send the daemon
#: then completed. `tests/test_client.py` checks this against the registry.
DEFAULT_TIMEOUT = 930.0

_ENABLE_COMMANDS_ENV = "TLGR_ENABLE_COMMANDS"


class Client:
    """A long-lived connection story to the tlgr daemon.

    Holds no socket between calls (each request is one short-lived Unix
    socket connection, as in the CLI), so it is cheap to keep for the life of
    a process and safe to share between threads: the daemon serves requests
    concurrently and paces each account itself.

    Args:
        account: The alias every call uses unless it passes its own
            `account=`. Never inferred from the environment or config.
        home: The tlgr home (`TLGR_HOME`, else `~/.tlgr`).
        timeout: Seconds to wait for each call. Defaults to
            `DEFAULT_TIMEOUT`, which is no shorter than any operation's own.
        flood_wait_max: The default `--flood-wait-max` for every call.
        enable_commands: An allowlist of operation ids, as
            `--enable-commands`; defaults to `TLGR_ENABLE_COMMANDS`. Empty
            allows everything.
        auto_start: Start the daemon if it is not running. Defaults to the
            `[daemon] auto_start` setting, exactly as the CLI does.
        allow_daemon_restart: Restart a running daemon that speaks an older
            protocol. Off by default (the CLI's default is on): a library
            embedded in someone's process should not restart their daemon
            unasked, so this raises `DaemonVersionMismatchError` instead.
        token: The IPC token; defaults to `TLGR_IPC_TOKEN`, else the home's
            token file, as the CLI reads it.
    """

    def __init__(
        self,
        account: str | None = None,
        *,
        home: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        flood_wait_max: int | None = None,
        enable_commands: str | None = None,
        auto_start: bool | None = None,
        allow_daemon_restart: bool = False,
        token: str | None = None,
    ) -> None:
        self.account = _checked_alias(account)
        self.timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        self.flood_wait_max = flood_wait_max
        if enable_commands is None:
            enable_commands = os.environ.get(_ENABLE_COMMANDS_ENV, "")
        self.enable_commands = enable_commands.strip()
        # Building the transport builds `TlgrPaths`, which is where a
        # `.production` home is refused. Doing it here rather than on the
        # first call makes the refusal happen at the line that chose the home.
        self._transport = DaemonClient(
            Path(home).expanduser() if home is not None else None,
            timeout=self.timeout,
            auto_start=auto_start,
            no_restart=not allow_daemon_restart,
            token=token,
        )

    @property
    def home(self) -> Path:
        """The tlgr home this client talks to."""
        return self._transport.paths.base

    def __repr__(self) -> str:
        return f"Client(account={self.account!r}, home={str(self.home)!r})"

    # -- operations --------------------------------------------------------

    def op(
        self,
        op_id: str,
        params: Mapping[str, Any] | Any = None,
        *,
        account: str | None = None,
        dry_run: bool = False,
        flood_wait_max: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        fetch_all: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run one operation and return its envelope, as `--json` prints it.

        Args:
            op_id: An operation id (`message.send`) or one of its aliases.
            params: The request fields, as a dict (`{"chat": "@alice",
                "text": "hi"}`) or the operation's request struct. Peers may
                be given as strings, exactly as on the command line.
            account: This call's alias; overrides the client's.
            dry_run: Preview a mutating operation without running it.
            flood_wait_max: Seconds the daemon may sleep on a FloodWait
                before raising `RateLimitError` instead.
            limit, cursor: Pagination, as `--limit` / `--cursor`.
            fetch_all: Walk every page (`--all`) and return one envelope.
            timeout: Seconds to wait for this call.

        Raises:
            TlgrError: The daemon's classified error, as its subclass
                (`RateLimitError`, `SpamFlagError`, ...). A mutating
                operation is never retried: if the connection breaks after
                the request was sent, you get `IPCError`, not a second send.
        """
        common = self._common(op_id, account, dry_run, flood_wait_max, limit, cursor)
        if fetch_all:
            frames = self._transport.op_stream(
                op_id,
                params,
                fetch_all=True,
                timeout=self._timeout(timeout),
                **common,
            )
            return _collect(frames, op_id)
        return self._transport.op(op_id, params, timeout=self._timeout(timeout), **common)

    def result(self, op_id: str, params: Mapping[str, Any] | Any = None, **kwargs: Any) -> Any:
        """`op()` and return only `result`, as `--results-only` prints it."""
        return self.op(op_id, params, **kwargs).get("result")

    def stream(
        self,
        op_id: str,
        params: Mapping[str, Any] | Any = None,
        *,
        account: str | None = None,
        dry_run: bool = False,
        flood_wait_max: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        fetch_all: bool = False,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield an operation's NDJSON frames as they arrive.

        Frames are `meta`, then `item` (or the operation's own vocabulary,
        such as events and `heartbeat` for `events.watch`), then `page` and a
        final `end`. A stream that fails raises its classified error after the
        frames that arrived before it, and one that drops without an `end`
        frame raises `RetryableError`: neither is ever a short success.

        `timeout` bounds the wait between frames; for a live stream such as
        `events.watch` give it more than the stream's own duration.
        """
        # The checks run here, not on the first `next()`: a bad alias or a
        # refused operation is reported at the call that made the mistake.
        common = self._common(op_id, account, dry_run, flood_wait_max, limit, cursor)
        frames = self._transport.op_stream(
            op_id,
            params,
            fetch_all=fetch_all,
            timeout=timeout,
            **common,
        )
        return _raising(frames)

    def status(self) -> dict[str, Any]:
        """`GET /v1/status`: the daemon and its accounts."""
        return self._transport.status()

    # -- internals ---------------------------------------------------------

    def _timeout(self, timeout: float | None) -> float:
        return self.timeout if timeout is None else float(timeout)

    def _common(
        self,
        op_id: str,
        account: str | None,
        dry_run: bool,
        flood_wait_max: int | None,
        limit: int | None,
        cursor: str | None,
    ) -> dict[str, Any]:
        """The checks every call makes before the request leaves."""
        if not isinstance(op_id, str) or not op_id.strip():
            raise UsageError("an operation id is required, e.g. 'message.send'", field="op")
        self._check_policy(op_id)
        return {
            "account": self.account if account is None else _checked_alias(account),
            "dry_run": bool(dry_run),
            "flood_wait_max": self.flood_wait_max if flood_wait_max is None else flood_wait_max,
            "limit": limit,
            "cursor": cursor,
        }

    def _check_policy(self, op_id: str) -> None:
        """`--enable-commands`, with the CLI's own canonicalising check.

        Loading the registry is what canonicalises an alias, so an allowlist
        written against ids cannot be side-stepped by calling an alias (SEC-04).
        It is paid only when an allowlist is set, and once per process.
        """
        if not self.enable_commands:
            return
        import tlgr.ops  # noqa: F401  (populates the registry's alias table)
        from tlgr.registry import policy_allows

        if not policy_allows(self.enable_commands, op_id):
            raise PermissionError_(
                f"operation {op_id!r} is not enabled (add it to --enable-commands to allow it)",
            )


def _raising(frames: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Pass frames through, turning a failed `end` frame into its exception."""
    for frame in frames:
        if frame.get("type") == "end" and frame.get("ok") is False:
            raise error_from_body(frame.get("error") or {})
        yield frame


def _checked_alias(alias: str | None) -> str:
    """An alias that can address an account, or "" for none at all."""
    if alias is None:
        return ""
    alias = alias.strip()
    return validate_alias(alias) if alias else ""
