"""What the four media modules share: references, documents and set names.

Three things are hard about media and all three are reference handling:

* **a `file_reference` expires.** Every `InputDocument`/`InputPhoto` tlgr
  builds is derived from an object fetched *now* — a message re-read, a
  sticker set re-fetched, the saved-GIF list re-listed — because a stale
  reference fails with `FILE_REFERENCE_EXPIRED` and the fix is never a retry,
  it is a re-fetch (§api file-references).
* **a document does not say what it is.** `MessageMediaDocument` is the same
  class for a thumbs-up sticker, a voice note, a GIF and a PDF; the answer is
  in the attributes, and only after *all* of them are collected.
* **a sticker set has four spellings** — short name, t.me link, numeric id and
  the system sets the clients use internally — and every command in the group
  accepts all four.

Telethon is imported inside functions, never at module scope: importing the
registry builds `tlgr --help`, and that must not pull in Telethon (§2.2).
"""

from __future__ import annotations

import base64
from typing import Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, decode_cursor
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.media import MediaFile, MediaSize
from tlgr.models.sticker import SavedGif, Sticker, StickerSet
from tlgr.ops._spec import OpContext

__all__ = [
    "MEDIA_FILTERS",
    "SYSTEM_SETS",
    "app_config",
    "b64",
    "client",
    "document_of",
    "file_id_of",
    "gif_model",
    "input_document",
    "input_photo",
    "media_file",
    "media_filter",
    "set_link",
    "sticker_model",
    "sticker_set_model",
    "sticker_set_ref",
    "window",
]

#: `--type` → the `inputMessagesFilter*` that implements a shared-media tab.
#: `chat-photo` is the group/channel avatar history: chats have no
#: `photos.getUserPhotos` equivalent, so the filter is the only way to it.
MEDIA_FILTERS: dict[str, str] = {
    "photo": "InputMessagesFilterPhotos",
    "video": "InputMessagesFilterVideo",
    "media": "InputMessagesFilterPhotoVideo",
    "file": "InputMessagesFilterDocument",
    "link": "InputMessagesFilterUrl",
    "music": "InputMessagesFilterMusic",
    "voice": "InputMessagesFilterVoice",
    "gif": "InputMessagesFilterGif",
    "round": "InputMessagesFilterRoundVideo",
    "chat-photo": "InputMessagesFilterChatPhotos",
    "all": "InputMessagesFilterEmpty",
}

#: The sets a client holds without ever installing them: dice faces, the
#: animated-emoji assets, the default status and topic-icon packs.
SYSTEM_SETS: dict[str, str] = {
    "animated-emoji": "InputStickerSetAnimatedEmoji",
    "emoji-animations": "InputStickerSetAnimatedEmojiAnimations",
    "premium-gifts": "InputStickerSetPremiumGifts",
    "generic-animations": "InputStickerSetEmojiGenericAnimations",
    "default-statuses": "InputStickerSetEmojiDefaultStatuses",
    "channel-statuses": "InputStickerSetEmojiChannelDefaultStatuses",
    "topic-icons": "InputStickerSetEmojiDefaultTopicIcons",
}


def client(ctx: OpContext) -> Any:
    handle = getattr(ctx, "client", None)
    if handle is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return handle


def window(ctx: OpContext, op: str, kind: PageKind, default: int = 50) -> tuple[int, Any]:
    """`(limit, cursor state)` — `--limit`/`--cursor` are transport-level (L5)."""
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 1000), state


def b64(data: Any) -> str | None:
    """Bytes as base64, because JSON has none and a caller round-trips these."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    return base64.b64encode(bytes(data)).decode()


def unb64(text: str | None) -> bytes:
    if not text:
        return b""
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError) as exc:
        raise UsageError(f"{text!r} is not base64", field="file_reference") from exc


def media_filter(name: str | None) -> Any:
    """`--type photo` → `InputMessagesFilterPhotos()`."""
    class_name = MEDIA_FILTERS.get(name or "media")
    if class_name is None:
        raise UsageError(
            f"--type {name!r} is not a media tab; expected one of {', '.join(MEDIA_FILTERS)}",
            field="type",
        )
    from telethon.tl import types

    return getattr(types, class_name)()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def document_of(media: Any) -> Any:
    """The `Document` behind whatever wrapper is holding it."""
    if media is None:
        return None
    if type(media).__name__ == "Document":
        return media
    return getattr(media, "document", None)


def photo_of(media: Any) -> Any:
    if media is None:
        return None
    if type(media).__name__ == "Photo":
        return media
    return getattr(media, "photo", None)


def input_document(document: Any) -> Any:
    """`InputDocument` from a live `Document`, reference and all."""
    from telethon.tl import types

    if document is None:
        raise NotFoundError("this media carries no document")
    if type(document).__name__ == "InputDocument":
        return document
    return types.InputDocument(
        id=int(getattr(document, "id", 0)),
        access_hash=int(getattr(document, "access_hash", 0) or 0),
        file_reference=getattr(document, "file_reference", b"") or b"",
    )


def input_photo(photo: Any) -> Any:
    from telethon.tl import types

    if photo is None:
        raise NotFoundError("this media carries no photo")
    if type(photo).__name__ == "InputPhoto":
        return photo
    return types.InputPhoto(
        id=int(getattr(photo, "id", 0)),
        access_hash=int(getattr(photo, "access_hash", 0) or 0),
        file_reference=getattr(photo, "file_reference", b"") or b"",
    )


def file_id_of(media: Any) -> str | None:
    """`telethon.utils.pack_bot_file_id` — a convenience handle, not an identity.

    The Bot-API-shaped string carries **no** `file_reference`, so it is only
    usable while tlgr can refresh one for it (`media file-id get`). Returning
    None rather than raising: a media type that has no packed form is normal.
    """
    if media is None:
        return None
    try:
        from telethon import utils

        return utils.pack_bot_file_id(media) or None
    except Exception:
        return None


def attributes_of(document: Any) -> dict[str, Any]:
    """Every `DocumentAttribute*` collapsed into one dict.

    Collected before anything is decided: a GIF carries Video **and**
    Animated and a video sticker carries Video **and** Sticker, so
    "first attribute wins" gets both of them wrong (v1's bug, kept fixed).
    """
    facts: dict[str, Any] = {}
    for attribute in getattr(document, "attributes", None) or []:
        name = type(attribute).__name__
        if name == "DocumentAttributeSticker":
            facts["sticker"] = getattr(attribute, "alt", "") or ""
            facts["mask"] = bool(getattr(attribute, "mask", False))
            facts["stickerset"] = getattr(attribute, "stickerset", None)
        elif name == "DocumentAttributeCustomEmoji":
            facts["custom_emoji"] = getattr(attribute, "alt", "") or ""
            facts["free"] = bool(getattr(attribute, "free", False))
            facts["text_color"] = bool(getattr(attribute, "text_color", False))
            facts["stickerset"] = getattr(attribute, "stickerset", None)
        elif name == "DocumentAttributeAudio":
            facts["voice"] = bool(getattr(attribute, "voice", False))
            facts["duration"] = getattr(attribute, "duration", None)
            facts["title"] = getattr(attribute, "title", None)
            facts["performer"] = getattr(attribute, "performer", None)
            facts["waveform"] = getattr(attribute, "waveform", None) is not None
        elif name == "DocumentAttributeVideo":
            facts["round"] = bool(getattr(attribute, "round_message", False))
            facts["video"] = True
            facts["duration"] = getattr(attribute, "duration", None)
            facts["width"] = getattr(attribute, "w", None)
            facts["height"] = getattr(attribute, "h", None)
            facts["supports_streaming"] = bool(getattr(attribute, "supports_streaming", False))
            facts["nosound"] = bool(getattr(attribute, "nosound", False))
        elif name == "DocumentAttributeAnimated":
            facts["animated"] = True
        elif name == "DocumentAttributeImageSize":
            facts["width"] = getattr(attribute, "w", None)
            facts["height"] = getattr(attribute, "h", None)
        elif name == "DocumentAttributeFilename":
            facts["file_name"] = getattr(attribute, "file_name", None)
    return facts


def kind_of(document: Any, facts: dict[str, Any] | None = None) -> str:
    """`sticker`/`voice`/`video_note`/`gif`/`video`/`audio`/`file`."""
    facts = attributes_of(document) if facts is None else facts
    mime = getattr(document, "mime_type", None)
    if "custom_emoji" in facts:
        return "sticker"
    if "sticker" in facts:
        return "sticker"
    if facts.get("voice"):
        return "voice"
    if facts.get("round"):
        return "video_note"
    if facts.get("animated") or mime == "image/gif":
        return "gif"
    if facts.get("video"):
        return "video"
    if "duration" in facts and not facts.get("video"):
        return "audio"
    return "file"


def photo_sizes(photo: Any) -> list[MediaSize]:
    """Every `PhotoSize` variant, with the inline bytes where there are any."""
    out: list[MediaSize] = []
    for size in getattr(photo, "sizes", None) or []:
        name = type(size).__name__
        out.append(
            MediaSize(
                type=str(getattr(size, "type", "") or ""),
                width=getattr(size, "w", None),
                height=getattr(size, "h", None),
                size=getattr(size, "size", None),
                bytes_b64=b64(getattr(size, "bytes", None))
                if name in ("PhotoStrippedSize", "PhotoPathSize")
                else None,
            )
        )
    for size in getattr(photo, "video_sizes", None) or []:
        out.append(
            MediaSize(
                type=str(getattr(size, "type", "") or ""),
                width=getattr(size, "w", None),
                height=getattr(size, "h", None),
                size=getattr(size, "size", None),
                video=True,
            )
        )
    return out


def media_file(document: Any) -> MediaFile | None:
    if document is None:
        return None
    return MediaFile(
        doc_id=int(getattr(document, "id", 0) or 0),
        access_hash=getattr(document, "access_hash", None),
        file_reference_b64=b64(getattr(document, "file_reference", None)),
        dc_id=getattr(document, "dc_id", None),
        mime=getattr(document, "mime_type", None),
        size=getattr(document, "size", None),
        file_id=file_id_of(document),
    )


def sticker_model(
    document: Any,
    *,
    index: int | None = None,
    set_short_name: str | None = None,
    set_title: str | None = None,
    keywords: list[str] | None = None,
    premium: bool = False,
) -> Sticker:
    """One sticker or custom-emoji document as the row every list prints."""
    facts = attributes_of(document)
    stickerset = facts.get("stickerset")
    return Sticker(
        doc_id=int(getattr(document, "id", 0) or 0),
        access_hash=getattr(document, "access_hash", None),
        file_reference_b64=b64(getattr(document, "file_reference", None)),
        emoji=facts.get("custom_emoji") or facts.get("sticker") or None,
        keywords=list(keywords or []),
        index=index,
        set_id=getattr(stickerset, "id", None),
        set_short_name=set_short_name or getattr(stickerset, "short_name", None),
        set=set_title,
        mime=getattr(document, "mime_type", None),
        size=getattr(document, "size", None),
        width=facts.get("width"),
        height=facts.get("height"),
        dc_id=getattr(document, "dc_id", None),
        premium=premium,
        mask=bool(facts.get("mask")),
        custom_emoji="custom_emoji" in facts,
        free=facts.get("free"),
        text_color=facts.get("text_color"),
        file_id=file_id_of(document),
    )


def gif_model(document: Any, index: int | None = None) -> SavedGif:
    facts = attributes_of(document)
    return SavedGif(
        doc_id=int(getattr(document, "id", 0) or 0),
        access_hash=getattr(document, "access_hash", None),
        file_reference_b64=b64(getattr(document, "file_reference", None)),
        index=index,
        mime=getattr(document, "mime_type", None),
        size=getattr(document, "size", None),
        duration=int(facts["duration"]) if facts.get("duration") is not None else None,
        width=facts.get("width"),
        height=facts.get("height"),
        file_id=file_id_of(document),
    )


# ---------------------------------------------------------------------------
# Sticker sets
# ---------------------------------------------------------------------------


def set_link(short_name: str, kind: str = "sticker") -> str | None:
    """`t.me/addstickers/<name>`, or `addemoji` for an emoji set.

    String formatting, not a request: the share link has never needed one.
    """
    if not short_name:
        return None
    path = "addemoji" if kind == "emoji" else "addstickers"
    return f"https://t.me/{path}/{short_name}"


def _short_name_from_link(text: str) -> str | None:
    lowered = text.lower()
    for marker in ("addstickers/", "addemoji/", "stickerset/"):
        if marker in lowered:
            tail = text[lowered.index(marker) + len(marker) :]
            return tail.split("?")[0].split("/")[0].strip()
    return None


def sticker_set_ref(text: str, *, field: str = "set") -> Any:
    """Any of the four spellings of a set as one `InputStickerSet`.

    A bare numeric id is deliberately **not** accepted: `InputStickerSetID`
    needs an access hash the id alone does not carry, and guessing one
    produces `STICKERSET_INVALID` rather than a useful message. `id:hash`
    is accepted for a caller that has both (that is what `sticker set list`
    prints), and everything else must be a short name or a link.
    """
    from telethon.tl import types

    value = (text or "").strip()
    if not value:
        raise UsageError("a sticker set is required", field=field)

    lowered = value.lower()
    if lowered.startswith("dice:"):
        emoticon = value.split(":", 1)[1].strip()
        if not emoticon:
            raise UsageError("dice: needs an emoji, e.g. dice:🎲", field=field)
        return types.InputStickerSetDice(emoticon=emoticon)
    system = SYSTEM_SETS.get(lowered)
    if system is not None:
        return getattr(types, system)()

    link = _short_name_from_link(value)
    if link:
        return types.InputStickerSetShortName(short_name=link)

    if ":" in value:
        head, _, tail = value.partition(":")
        if head.lstrip("-").isdigit() and tail.lstrip("-").isdigit():
            return types.InputStickerSetID(id=int(head), access_hash=int(tail))
    if value.lstrip("-").isdigit():
        raise UsageError(
            f"{value!r} is a set id without its access hash; name the set by its short "
            "name or its t.me link, or pass <id>:<access_hash>",
            field=field,
        )
    return types.InputStickerSetShortName(short_name=value.lstrip("@"))


def set_kind(set_obj: Any) -> str:
    if getattr(set_obj, "emojis", False):
        return "emoji"
    if getattr(set_obj, "masks", False):
        return "mask"
    return "sticker"


def sticker_set_model(set_obj: Any, *, covers: list[Any] | None = None) -> StickerSet:
    """A `StickerSet` TL object as the metadata row every listing prints."""
    kind = set_kind(set_obj)
    short_name = str(getattr(set_obj, "short_name", "") or "")
    thumbs = getattr(set_obj, "thumbs", None) or []
    thumb = None
    if thumbs:
        first = thumbs[0]
        thumb = MediaFile(
            doc_id=int(getattr(set_obj, "thumb_document_id", 0) or 0),
            dc_id=getattr(set_obj, "thumb_dc_id", None),
            size=getattr(first, "size", None),
        )
    return StickerSet(
        id=int(getattr(set_obj, "id", 0) or 0),
        access_hash=getattr(set_obj, "access_hash", None),
        short_name=short_name,
        title=str(getattr(set_obj, "title", "") or ""),
        count=int(getattr(set_obj, "count", 0) or 0),
        type=kind,
        installed=getattr(set_obj, "installed_date", None) is not None,
        archived=bool(getattr(set_obj, "archived", False)),
        official=bool(getattr(set_obj, "official", False)),
        creator=bool(getattr(set_obj, "creator", False)),
        text_color=bool(getattr(set_obj, "text_color", False)),
        installed_date=fmt_dt(getattr(set_obj, "installed_date", None)),
        thumb=thumb,
        link=set_link(short_name, kind),
        covers=[sticker_model(doc) for doc in (covers or [])],
    )


def covered_set(covered: Any) -> StickerSet:
    """`stickerSetCovered` / `stickerSetMultiCovered` → one `StickerSet`."""
    inner = getattr(covered, "set", covered)
    covers = list(getattr(covered, "covers", None) or [])
    single = getattr(covered, "cover", None)
    if single is not None:
        covers = [single]
    return sticker_set_model(inner, covers=covers)


async def fetch_set(ctx: OpContext, ref: Any, *, field: str = "set") -> Any:
    """`messages.getStickerSet` for any spelling, with a NOT_FOUND on a miss."""
    from telethon.tl.functions import messages as fn

    stickerset = ref if not isinstance(ref, str) else sticker_set_ref(ref, field=field)
    try:
        return await client(ctx)(fn.GetStickerSetRequest(stickerset=stickerset, hash=0))
    except Exception as exc:
        if "STICKERSET_INVALID" in str(exc).upper():
            raise NotFoundError(f"no sticker set matches {ref!r}") from exc
        raise


async def resolve_stickers(
    ctx: OpContext,
    refs: list[str],
    *,
    from_message: tuple[Any, int] | None = None,
) -> list[Any]:
    """`<set>/<index>`, `<set>/<emoji>` or a document id → live `Document`s.

    Always through a fresh `messages.getStickerSet`: an `InputDocument` built
    from a cached id carries a dead `file_reference`, and every
    `faveSticker`/`saveRecentSticker`/`removeStickerFromSet` call would fail
    with `FILE_REFERENCE_EXPIRED` a few hours after the id was printed.
    """
    out: list[Any] = []
    if from_message is not None:
        peer, message_id = from_message
        message = await fetch_message(ctx, peer, message_id)
        document = document_of(getattr(message, "media", None))
        if document is None:
            raise NotFoundError(f"message {message_id} carries no sticker")
        out.append(document)

    cache: dict[str, Any] = {}
    for ref in refs:
        value = str(ref).strip()
        if "/" in value:
            set_name, _, selector = value.rpartition("/")
            if set_name not in cache:
                cache[set_name] = await fetch_set(ctx, set_name, field="sticker")
            documents = list(getattr(cache[set_name], "documents", None) or [])
            out.append(_pick_sticker(documents, selector, value))
            continue
        raise UsageError(
            f"{value!r} is not a sticker; use <set>/<index>, <set>/<emoji> or --from-message",
            field="sticker",
        )
    if not out:
        raise UsageError("no sticker was given", field="sticker")
    return out


def _pick_sticker(documents: list[Any], selector: str, spelling: str) -> Any:
    if selector.lstrip("-").isdigit():
        index = int(selector)
        if not 0 <= index < len(documents):
            raise NotFoundError(f"{spelling}: the set has {len(documents)} stickers")
        return documents[index]
    for document in documents:
        if attributes_of(document).get("sticker") == selector:
            return document
        if attributes_of(document).get("custom_emoji") == selector:
            return document
    raise NotFoundError(f"{spelling}: no sticker in the set carries that emoji")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def fetch_message(ctx: OpContext, peer: Any, message_id: int) -> Any:
    """Re-read one message so its `file_reference` is current.

    Every byte-touching path starts here. Telethon refreshes a reference
    mid-download only when it was handed a `Message`; nothing refreshes one
    for a send, so tlgr fetches the message itself and builds the input media
    from what came back.
    """
    found = await client(ctx).get_messages(peer, ids=[int(message_id)])
    message = (found or [None])[0]
    if message is None or not getattr(message, "id", 0):
        raise NotFoundError(f"message {message_id} does not exist here")
    return message


def message_dates(message: Any) -> tuple[str, int]:
    date = getattr(message, "date", None)
    return fmt_dt(date) or "", to_unix(date) or 0


async def app_config(ctx: OpContext) -> dict[str, Any]:
    """`help.getAppConfig` as plain Python. Never hardcode a server limit."""
    from telethon.tl.functions import help as help_fn

    config = await client(ctx)(help_fn.GetAppConfigRequest(hash=0))

    def unwrap(node: Any) -> Any:
        name = type(node).__name__
        if name == "JsonObject":
            return {str(v.key): unwrap(v.value) for v in (getattr(node, "value", None) or [])}
        if name == "JsonArray":
            return [unwrap(v) for v in (getattr(node, "value", None) or [])]
        if name == "JsonNull":
            return None
        return getattr(node, "value", node)

    return unwrap(getattr(config, "config", config)) or {}


def config_int(values: dict[str, Any], name: str, default: int = 0) -> int:
    raw = values.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
