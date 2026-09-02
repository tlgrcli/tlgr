"""Calling a method Telethon's layer does not know about (§6.14).

Telethon 1.44 speaks layer 227. When Telegram ships a method at layer 229 that
tlgr needs before Telethon catches up, the choice is: wait for a release, fork
Telethon, or serialise the request by hand. This module is the third option,
written down once so that nobody has to rediscover the constructor-id rules
under deadline.

**Nothing here is used today.** Communities, ephemeral messages and the new
keyboard model are the only layer-229 features, none is on the P0/P1 path, and
Firebase login is Android-only (§1.2). It ships as the base classes and one
tested example, because a recipe you have never run is not a recipe.

The four things that are easy to get wrong:

* `CONSTRUCTOR_ID` is the little-endian CRC32 of the *TL definition line* with
  its parameter names and types, not of the method name. It has to be copied
  from the schema; it cannot be derived from anything you have.
* `SUBCLASS_OF_ID` is `zlib.crc32(b'<ResultType>')` — the **result** type, and
  it is what lets Telethon's reader find the right parser.
* the result type must be registered in `tlobject.alltlobjects` before the
  reply arrives, or the reader raises `TypeNotFoundError` with the id in hex
  and no other clue.
* a request above the negotiated layer must be wrapped in
  `InvokeWithLayerRequest`, because the connection announced layer 227 at
  `initConnection` time and the server enforces it.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

from telethon.extensions import BinaryReader
from telethon.tl.tlobject import TLObject, TLRequest

__all__ = [
    "CustomRequest",
    "CustomType",
    "register",
    "subclass_of",
]


def subclass_of(result_type: str) -> int:
    """`zlib.crc32(b'<ResultType>')` — the id Telethon matches results against."""
    return zlib.crc32(result_type.encode("ascii"))


def register(*types: type[TLObject]) -> None:
    """Teach Telethon's reader about a constructor it does not know.

    Must happen before the reply is read, which in practice means at import
    time of the module that defines the request.
    """
    from telethon.tl import alltlobjects

    for klass in types:
        alltlobjects.tlobjects[klass.CONSTRUCTOR_ID] = klass


class CustomType(TLObject):
    """Base for a result type Telethon cannot parse yet.

    Subclasses set `CONSTRUCTOR_ID` and implement `from_reader`.
    """

    CONSTRUCTOR_ID = 0
    SUBCLASS_OF_ID = 0

    @classmethod
    def from_reader(cls, reader: BinaryReader) -> CustomType:  # pragma: no cover - abstract
        raise NotImplementedError


class CustomRequest(TLRequest):
    """Base for a request above Telethon's layer.

    Subclasses set `CONSTRUCTOR_ID`, `SUBCLASS_OF_ID` and implement `_bytes()`.
    `read_result` is inherited and works as soon as the result type is
    registered.
    """

    CONSTRUCTOR_ID = 0
    SUBCLASS_OF_ID = 0

    def to_dict(self) -> dict[str, Any]:
        return {"_": type(self).__name__}

    def _bytes(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    def __bytes__(self) -> bytes:
        return self._bytes()


# ---------------------------------------------------------------------------
# The worked example: help.getNearestDc, which Telethon *does* have.
# ---------------------------------------------------------------------------


class NearestDc(CustomType):
    """`nearestDc#8e1a1775 country:string this_dc:int nearest_dc:int = NearestDc`.

    Deliberately a method Telethon already supports, so the example can be
    tested against a real reply shape without needing a layer bump. Copy this
    class, change the four constants, and you have the new method.

    >>> NearestDc.CONSTRUCTOR_ID == 0x8E1A1775
    True
    >>> NearestDc.SUBCLASS_OF_ID == subclass_of("NearestDc")
    True
    """

    CONSTRUCTOR_ID = 0x8E1A1775
    SUBCLASS_OF_ID = subclass_of("NearestDc")

    def __init__(self, country: str, this_dc: int, nearest_dc: int) -> None:
        self.country = country
        self.this_dc = this_dc
        self.nearest_dc = nearest_dc

    def to_dict(self) -> dict[str, Any]:
        return {
            "_": "NearestDc",
            "country": self.country,
            "this_dc": self.this_dc,
            "nearest_dc": self.nearest_dc,
        }

    @classmethod
    def from_reader(cls, reader: BinaryReader) -> NearestDc:
        return cls(
            country=reader.tgread_string(),
            this_dc=reader.read_int(),
            nearest_dc=reader.read_int(),
        )


class GetNearestDcRequest(CustomRequest):
    """`help.getNearestDc#1fb33026 = NearestDc`.

    >>> bytes(GetNearestDcRequest()).hex()
    '2630b31f'
    """

    CONSTRUCTOR_ID = 0x1FB33026
    SUBCLASS_OF_ID = subclass_of("NearestDc")

    def _bytes(self) -> bytes:
        return struct.pack("<I", self.CONSTRUCTOR_ID)


# Deliberately *not* registered at import: `NearestDc` is a type Telethon
# already parses, and replacing its entry in `alltlobjects` would make every
# ordinary `help.getNearestDc` return this class instead. A real layer-229
# type has no incumbent, so its own module calls `register()` at import.


async def invoke_with_layer(client: Any, request: TLRequest, layer: int) -> Any:
    """Send *request* announcing a newer layer for this call only.

    The connection negotiated its layer at `initConnection`; a method above it
    is rejected until the wrapper says otherwise, and the wrapper applies to
    exactly one request.
    """
    from telethon.tl.functions import InvokeWithLayerRequest

    return await client(InvokeWithLayerRequest(layer, request))
