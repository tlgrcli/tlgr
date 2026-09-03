"""TL object → JSON-safe builtins, and peer arithmetic.

Two functions, in `core/` because three layers need them and none may import
another: the bus normalises updates with them, `ops/` reads `help.*` replies
with them, and neither is allowed to reach into the other (§2.2).

`tl_to_builtins` is the COR-07 fix stated once. v1 delivered a raw `to_dict()`
through `json.dumps(default=str)`, so a `datetime` became a string in one
place and a `bytes` blew up in another — and a message with media could fail
to serialise *at delivery time*, far from the cause, counted as a delivery
failure rather than as the bug it was.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["CHANNEL_MARK", "peer_marked_id", "tl_to_builtins"]

#: Telegram's channel id offset. A channel's marked id is this minus its id,
#: which is what makes `-100…` recognisable at a glance.
CHANNEL_MARK = -1000000000000

#: How deep `tl_to_builtins` walks before it stops. A `Message` inside a
#: `Story` inside a `WebPage` is real; anything past this is a cycle, or a
#: payload nobody wanted in a stream frame.
_MAX_DEPTH = 8


def tl_to_builtins(value: Any, *, depth: int = 0) -> Any:
    """A TL object tree → JSON-safe builtins, with the class name kept.

    Datetimes become RFC-3339, bytes become hex, and the constructor name
    survives as `_` so a consumer can still branch on it.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if depth >= _MAX_DEPTH:
        return type(value).__name__
    if isinstance(value, (list, tuple, set, frozenset)):
        return [tl_to_builtins(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(k): tl_to_builtins(v, depth=depth + 1) for k, v in value.items()}

    out: dict[str, Any] = {"_": type(value).__name__}
    attributes = getattr(value, "__dict__", None)
    names: list[str]
    if isinstance(attributes, dict) and attributes:
        names = [name for name in attributes if not name.startswith("_")]
    else:
        names = [
            name
            for klass in type(value).__mro__
            for name in getattr(klass, "__slots__", ())
            if not str(name).startswith("_")
        ]
    if not names:
        # A TLObject with nothing set, or an object we have no handle on. Its
        # class name is the honest answer; `str()` would call Telethon's
        # pretty-printer, which re-enters `to_dict()` and can raise.
        return out
    for name in names:
        out[str(name)] = tl_to_builtins(getattr(value, name, None), depth=depth + 1)
    return out


def peer_marked_id(peer: Any) -> int | None:
    """A TL `Peer*` → the marked id tlgr uses everywhere else.

    Four lines of arithmetic rather than a call into Telethon's `utils`,
    because the bus runs this on the update loop's hot path for every event.
    """
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    name = type(peer).__name__
    if name == "PeerUser":
        return _int(getattr(peer, "user_id", None))
    if name == "PeerChat":
        chat_id = _int(getattr(peer, "chat_id", None))
        return -chat_id if chat_id is not None else None
    if name == "PeerChannel":
        channel_id = _int(getattr(peer, "channel_id", None))
        return CHANNEL_MARK - channel_id if channel_id is not None else None
    return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
