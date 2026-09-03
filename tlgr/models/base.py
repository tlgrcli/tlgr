"""Shared msgspec configuration for every wire shape tlgr emits or accepts.

`models/` is the only place a wire shape is defined, and it imports nothing
else from tlgr — not even the error types — so that `import tlgr.models` works
without Telethon, without click and without a config file. The import lint in
`tests/test_layering.py` keeps it that way.
"""

from __future__ import annotations

from typing import Any, TypeVar, Union

import msgspec

__all__ = ["UNSET", "Model", "Request", "Unset", "decode", "encode", "to_builtins"]


class Model(
    msgspec.Struct,
    kw_only=True,
    omit_defaults=True,
    forbid_unknown_fields=False,
):
    """A response/domain model.

    Unknown fields are tolerated on decode: an older client talking to a newer
    daemon must keep working, and dropping a field it does not understand is
    the only forward-compatible answer.

    `omit_defaults=True` keeps the JSON small and — more importantly — makes
    "absent" meaningful: a field that is missing was not applicable or not
    requested, a field that is `null` is known to be empty.
    """


class Request(
    msgspec.Struct,
    kw_only=True,
    omit_defaults=True,
    forbid_unknown_fields=True,
):
    """An operation request.

    Unknown fields are a USAGE error, the opposite of `Model`: a newer CLI must
    not silently lose a field against an older daemon. The version handshake
    catches the mismatch first; this is the backstop.
    """


UNSET = msgspec.UNSET
"""Sentinel for "the caller did not supply this field at all".

Distinct from an explicit `null`, which means "clear it". That tri-state is
what makes one `edit` operation able to express leave-alone, set and clear
without three flags per field.
"""

_T = TypeVar("_T")

# PEP 695's `type Unset[T] = ...` needs 3.12 and we target 3.10.
Unset = Union[_T, msgspec.UnsetType]
"""`Unset[str]` is "a string, or nothing was supplied".

`Unset[str | None]` adds "explicitly cleared" — the `| None` is what makes a
clear expressible at all, because `Unset[str]` alone rejects a literal null.
"""


def encode(value: Any) -> bytes:
    """Encode any model (or builtin) to compact UTF-8 JSON."""
    return msgspec.json.encode(value)


def decode(data: bytes | str, type: type[_T]) -> _T:
    """Decode JSON into *type*, raising `msgspec.ValidationError` on a mismatch."""
    return msgspec.json.decode(data, type=type)


def to_builtins(value: Any) -> Any:
    """Convert a model tree to plain dicts/lists, honouring `omit_defaults`."""
    return msgspec.to_builtins(value)
