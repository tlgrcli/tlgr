"""Opaque, versioned, op-bound cursors.

A cursor is state the caller is not supposed to read, edit or reuse elsewhere.
v1's cursor was plain base64 JSON that decoded to `{}` on any problem — a
truncated token silently restarted the walk from message 0, which looks like
"the chat only has these 40 messages" rather than like an error.

Here a cursor carries its version, the op it belongs to, an account
fingerprint and an expiry, and is signed. The HMAC is integrity, not secrecy:
it exists so that a hand-edited cursor produces `USAGE: invalid cursor`
instead of a plausible wrong answer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import time
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import msgspec

from tlgr.core.errors import UsageError
from tlgr.models.page import Page

__all__ = [
    "CURSOR_VERSION",
    "PageKind",
    "build_page",
    "cursor_key",
    "decode_cursor",
    "encode_cursor",
]

CURSOR_VERSION = 1

#: Default lifetimes. A LOCAL cursor indexes into a materialised snapshot that
#: goes stale fast; a server-side offset stays meaningful much longer.
_TTL_LOCAL = 3600
_TTL_DEFAULT = 86400

T = TypeVar("T")


class PageKind(str, Enum):
    """Which offset state a paginated op carries (ARCHITECTURE §3.5)."""

    HISTORY = "HISTORY"
    SEARCH = "SEARCH"
    RATE = "RATE"
    DIALOGS = "DIALOGS"
    PARTICIPANTS = "PARTICIPANTS"
    LOCAL = "LOCAL"


#: PageKinds whose offsets are dates as well as ids, so `--since/--until` mean
#: something and the generator injects them.
DATE_OFFSET_KINDS = frozenset({PageKind.HISTORY, PageKind.SEARCH, PageKind.DIALOGS})


def _config_dir() -> Path:
    """Where the cursor key lives. `TLGR_HOME` exists so tests never touch ~."""
    override = os.environ.get("TLGR_HOME", "").strip()
    if override:
        return Path(override)
    from tlgr.core.config import CONFIG_DIR

    return Path(CONFIG_DIR)


def cursor_key(base: Path | None = None) -> bytes:
    """The signing key, generated once at 0600.

    Rotating it invalidates outstanding cursors, which is the correct
    behaviour: they describe a walk of data this installation no longer
    guarantees anything about.
    """
    directory = base or _config_dir()
    path = directory / "cursor.key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    directory.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    # Write through a private temp file so the key is never briefly world-readable.
    tmp = path.with_suffix(".key.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return key


def account_fingerprint(account: str) -> str:
    """First 8 hex of sha256(alias) — enough to catch a cursor crossing accounts.

    The alias itself is not embedded because a cursor is pasted into shell
    history, logs and bug reports.
    """
    return hashlib.sha256(account.encode()).hexdigest()[:8]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes, key: bytes) -> str:
    return _b64(hmac.new(key, payload, hashlib.sha256).digest()[:16])


def encode_cursor(
    *,
    op: str,
    kind: PageKind | str,
    state: dict[str, Any],
    account: str = "",
    ttl: int | None = None,
    key: bytes | None = None,
    now: int | None = None,
) -> str:
    """Sign pagination *state* into a token bound to this op and account."""
    kind_name = kind.value if isinstance(kind, PageKind) else str(kind)
    if ttl is None:
        ttl = _TTL_LOCAL if kind_name == PageKind.LOCAL.value else _TTL_DEFAULT
    payload = msgspec.json.encode(
        {
            "v": CURSOR_VERSION,
            "op": op,
            "kind": kind_name,
            "acct": account_fingerprint(account),
            "st": state,
            "exp": int(now or time.time()) + ttl,
        }
    )
    return f"{_b64(payload)}.{_sign(payload, key or cursor_key())}"


def decode_cursor(
    token: str,
    *,
    op: str,
    kind: PageKind | str | None = None,
    account: str = "",
    key: bytes | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate a token and return its state, or raise USAGE.

    Every rejection is a USAGE error rather than a silent restart: a cursor
    that cannot be trusted must stop the walk, not quietly begin a new one.
    """

    def reject(why: str) -> UsageError:
        return UsageError(f"invalid cursor: {why}", field="cursor")

    head, dot, signature = token.partition(".")
    if not dot:
        raise reject("not a tlgr cursor (missing signature)")
    try:
        payload = _unb64(head)
    except (binascii.Error, ValueError) as exc:
        raise reject("corrupt encoding") from exc
    if not hmac.compare_digest(signature, _sign(payload, key or cursor_key())):
        raise reject("signature does not match (truncated or hand-edited?)")

    try:
        data = msgspec.json.decode(payload, type=dict)
    except msgspec.DecodeError as exc:
        raise reject("corrupt payload") from exc

    if data.get("v") != CURSOR_VERSION:
        raise reject(f"version {data.get('v')!r} is not supported by this tlgr")
    if data.get("op") != op:
        raise reject(f"it belongs to {data.get('op')!r}, not {op!r}")
    if kind is not None:
        want = kind.value if isinstance(kind, PageKind) else str(kind)
        if data.get("kind") != want:
            raise reject(f"it paginates {data.get('kind')!r}, not {want!r}")
    if data.get("acct") != account_fingerprint(account):
        raise reject("it was issued for a different account")
    expiry = data.get("exp")
    if not isinstance(expiry, int) or expiry < int(now or time.time()):
        raise reject("it has expired; start the listing again")

    state = data.get("st")
    if not isinstance(state, dict):
        raise reject("corrupt state")
    return state


def build_page(
    items: list[T],
    *,
    op: str,
    kind: PageKind | str,
    state: dict[str, Any] | None = None,
    account: str = "",
    has_more: bool | None = None,
    limit: int | None = None,
    total: int | None = None,
    key: bytes | None = None,
) -> Page[T]:
    """Assemble a `Page[T]`, emitting a cursor only when there is more to fetch.

    `len(items) >= limit` is the fallback guess for server-paginated endpoints,
    which cannot know whether more rows exist without asking again. A caller
    holding the full set knows the exact answer and passes `has_more`.
    """
    if has_more is None:
        has_more = limit is not None and len(items) >= limit
    page: Page[T] = Page(items=items, has_more=has_more, total=total)
    if has_more and state is not None:
        page.next_cursor = encode_cursor(op=op, kind=kind, state=state, account=account, key=key)
    return page
