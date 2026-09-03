"""NDJSON framing — bytes in, bytes out, one object per line.

Kept separate from the client because both ends need it and because framing
bugs are easier to see when the framing is thirty lines with no sockets in it.

Two invariants:

* a frame is written as compact UTF-8 JSON followed by `\\n`, and never
  contains a raw newline (msgspec's encoder escapes them), so a reader can
  split on `\\n` without a state machine;
* the reader works on **bytes**. Decoding the stream to `str` first is how v1
  corrupted a Persian message split across two TCP reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import msgspec

__all__ = ["dump_frame", "iter_frames", "parse_frame"]


def dump_frame(value: Any) -> bytes:
    """Encode one frame, newline-terminated."""
    return msgspec.json.encode(value) + b"\n"


def parse_frame(line: bytes) -> dict[str, Any]:
    """Decode one frame. A frame that is not a JSON object is a protocol error."""
    decoded = msgspec.json.decode(line)
    if not isinstance(decoded, dict):
        raise msgspec.DecodeError("NDJSON frame is not an object")
    return decoded


def iter_frames(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Decode a stream of lines, skipping the blank ones a flush can produce."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        yield parse_frame(stripped)
