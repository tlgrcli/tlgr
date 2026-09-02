"""Message text: parse modes, spoilers, and entity JSON.

Two rules shape this module.

*Parsing is input-only.* Telethon's markdown/HTML parsers are used to turn
what a user typed into `(text, entities)`, and never to turn a received
message back into markup: `unparse` is lossy — it cannot express overlapping
runs, custom emoji or a URL containing the delimiter it would have to escape —
so tlgr sends the raw `text` plus `entities` and lets the consumer decide.

*Spoilers are ours.* Telethon 1.44 drops `||x||` and `<tg-spoiler>` silently:
the markers survive as literal text in markdown and vanish without an entity
in HTML. Either way the user's intent is lost with no error, so the markers
are lifted out here, before and after the parser runs.
"""

from __future__ import annotations

import json
from typing import Any

from tlgr.core.errors import UsageError
from tlgr.models.message import MessageEntity

__all__ = [
    "PARSE_MODES",
    "default_parse_mode",
    "entities_from_json",
    "entities_to_json",
    "parse_text",
    "utf16_len",
]

PARSE_MODES = ("md", "html", "none")

#: Private-use sentinels: the parsers pass them through untouched, so a
#: spoiler's boundaries survive markdown/HTML parsing and can be converted to
#: real offsets afterwards. They are stripped from user input first.
_SPOILER_OPEN = ""
_SPOILER_CLOSE = ""


def default_parse_mode() -> str:
    """`[defaults] parse_mode`, itself defaulting to `none` (COR-21)."""
    try:
        from tlgr.core.config import load_app_config

        mode = str(getattr(load_app_config().defaults, "parse_mode", "none") or "none")
    except Exception:
        mode = "none"
    return mode if mode in PARSE_MODES else "none"


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram measures offsets in.

    An emoji is one Python character and two UTF-16 units, which is why
    counting characters puts every entity after the first emoji in the wrong
    place.
    """
    return len(text.encode("utf-16-le")) // 2


def _strip_sentinels(text: str) -> str:
    return text.replace(_SPOILER_OPEN, "").replace(_SPOILER_CLOSE, "")


def _mark_spoilers(text: str, mode: str) -> str:
    """Replace the mode's spoiler markup with sentinels, before parsing."""
    if mode == "md":
        parts = text.split("||")
        if len(parts) < 3:
            return text
        out: list[str] = [parts[0]]
        for index, part in enumerate(parts[1:], start=1):
            # Odd boundaries open, even boundaries close; a trailing unmatched
            # `||` is left as typed rather than guessed at.
            out.append(_SPOILER_OPEN if index % 2 else _SPOILER_CLOSE)
            out.append(part)
        if len(parts) % 2 == 0:
            out[-2] = "||"
        return "".join(out)
    if mode == "html":
        for tag in ("tg-spoiler", "spoiler"):
            text = text.replace(f"<{tag}>", _SPOILER_OPEN).replace(f"</{tag}>", _SPOILER_CLOSE)
        return text
    return text


def _extract_spoilers(text: str, entities: list[MessageEntity]) -> tuple[str, list[MessageEntity]]:
    """Remove sentinels from parsed text, shifting offsets, emitting spoilers."""
    if _SPOILER_OPEN not in text and _SPOILER_CLOSE not in text:
        return text, entities

    out: list[str] = []
    removed = 0  # UTF-16 units removed so far
    shifts: list[tuple[int, int]] = []  # (original utf-16 offset, units removed before it)
    open_at: int | None = None
    spoilers: list[tuple[int, int]] = []  # (start, length) in *final* offsets
    position = 0

    for char in text:
        if char in (_SPOILER_OPEN, _SPOILER_CLOSE):
            removed += 1
            shifts.append((position, removed))
            if char == _SPOILER_OPEN:
                open_at = position - removed + 1
            elif open_at is not None:
                spoilers.append((open_at, (position - removed + 1) - open_at))
                open_at = None
        else:
            out.append(char)
        position += utf16_len(char)

    def shift(offset: int) -> int:
        total = 0
        for at, cumulative in shifts:
            if at < offset:
                total = cumulative
        return offset - total

    moved = [
        MessageEntity(
            type=entity.type,
            offset=shift(entity.offset),
            length=shift(entity.offset + entity.length) - shift(entity.offset),
            url=entity.url,
            user_id=entity.user_id,
            language=entity.language,
            document_id=entity.document_id,
            collapsed=entity.collapsed,
        )
        for entity in entities
    ]
    moved.extend(
        MessageEntity(type="spoiler", offset=start, length=length)
        for start, length in spoilers
        if length > 0
    )
    moved.sort(key=lambda e: (e.offset, e.length))
    return "".join(out), moved


def _tl_entity_type(obj: Any) -> str:
    """`MessageEntityTextUrl` → `text_url`."""
    name = type(obj).__name__.removeprefix("MessageEntity")
    result: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _from_tl(obj: Any) -> MessageEntity:
    return MessageEntity(
        type=_tl_entity_type(obj),
        offset=int(getattr(obj, "offset", 0)),
        length=int(getattr(obj, "length", 0)),
        url=getattr(obj, "url", None),
        user_id=getattr(getattr(obj, "user_id", None), "user_id", getattr(obj, "user_id", None)),
        language=getattr(obj, "language", None),
        document_id=getattr(obj, "document_id", None),
        collapsed=getattr(obj, "collapsed", None),
    )


def parse_text(text: str, mode: str = "none") -> tuple[str, list[MessageEntity]]:
    """Turn user input into `(plain text, entities)`.

    Telethon is imported lazily so that `import tlgr.core.text` — which the
    CLI does to validate `--parse` — stays free of it (§2.2).
    """
    if mode not in PARSE_MODES:
        raise UsageError(f"unknown parse mode {mode!r}: expected md, html or none", field="parse")

    clean = _strip_sentinels(text)
    if mode == "none":
        return clean, []

    marked = _mark_spoilers(clean, mode)
    from telethon.extensions import html as tl_html
    from telethon.extensions import markdown as tl_markdown

    parser = tl_markdown if mode == "md" else tl_html
    parsed, raw_entities = parser.parse(marked)
    entities = [_from_tl(entity) for entity in raw_entities or []]
    return _extract_spoilers(parsed, entities)


def entities_to_json(entities: list[MessageEntity]) -> str:
    """Entities as the JSON `--entities` accepts back."""
    import msgspec

    return msgspec.json.encode(entities).decode()


def entities_from_json(raw: str) -> list[MessageEntity]:
    """Decode `--entities JSON`, rejecting anything malformed as USAGE."""
    import msgspec

    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise UsageError(f"--entities is not valid JSON: {exc}", field="entities") from exc
    if not isinstance(loaded, list):
        raise UsageError("--entities must be a JSON array of entity objects", field="entities")
    try:
        return msgspec.convert(loaded, type=list[MessageEntity])
    except msgspec.ValidationError as exc:
        raise UsageError(f"--entities: {exc}", field="entities") from exc
