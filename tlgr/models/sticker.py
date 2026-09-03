"""Stickers, GIFs and custom emoji.

Three surfaces that look like three products in the GUI and are one thing on
the wire: a `Document` carrying `DocumentAttributeSticker` or
`DocumentAttributeCustomEmoji`, grouped into sets. Modelling them once is why
`sticker set list --type emoji` and `emoji set list` can be the same
operation, and why a saved GIF and a sticker share the file handle.

The split that *is* real is `set` versus `pack`: a **set** is somebody's
collection you install, archive or reorder; a **pack** is one you created and
can edit. Every model here keeps that boundary, because the API does: the
`stickers.*` namespace answers only to a set's creator.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.media import MediaFile

__all__ = [
    "EmojiGroup",
    "EmojiKeyword",
    "FaveResult",
    "GifResult",
    "GifSaved",
    "GifSent",
    "PackCreated",
    "PackDeleted",
    "PackEdited",
    "PackStickerAdded",
    "PackStickerRemoved",
    "RecentResult",
    "SavedGif",
    "Sticker",
    "StickerSet",
    "StickerSetOrder",
    "StickerSetsChanged",
]


class Sticker(Model):
    """One sticker or custom-emoji document.

    `emoji` is `DocumentAttributeSticker.alt` — the plain emoji the sticker
    stands for — and `index` is its position in the set, which is what
    `message send --sticker <set>/<n>` consumes.
    """

    doc_id: int
    access_hash: int | None = None
    file_reference_b64: str | None = None
    emoji: str | None = None
    keywords: list[str] = []
    index: int | None = None
    set_id: int | None = None
    set_short_name: str | None = None
    set: str | None = None
    mime: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    dc_id: int | None = None
    premium: bool = False
    mask: bool = False
    #: Custom emoji only: `free` says a non-Premium account may send it, and
    #: `text_color` that it renders in the message's text colour.
    custom_emoji: bool = False
    free: bool | None = None
    text_color: bool | None = None
    file_id: str | None = None
    path: str | None = None


class StickerSet(Model):
    """A sticker, mask or custom-emoji set.

    `stickers`, `packs` and `keywords` are filled only by `sticker set get`;
    a listing carries the metadata and, for a search result, the cover
    stickers the server sent with it.
    """

    id: int
    access_hash: int | None = None
    short_name: str = ""
    title: str = ""
    count: int = 0
    type: str = "sticker"
    installed: bool = False
    archived: bool = False
    official: bool = False
    creator: bool = False
    unread: bool = False
    text_color: bool = False
    installed_date: str | None = None
    thumb: MediaFile | None = None
    link: str | None = None
    stickers: list[Sticker] = []
    covers: list[Sticker] = []
    #: emoji → the document ids in this set that carry it.
    packs: dict[str, list[int]] = {}
    #: document id → its search keywords.
    keywords: dict[str, list[str]] = {}


class StickerSetsChanged(Model):
    """The result of installing, removing, archiving or unarchiving sets.

    Counts rather than booleans because every one of these verbs is variadic,
    and `already` because asking for a state the account is already in is a
    no-op, not a failure.
    """

    short_names: list[str] = []
    installed: int = 0
    removed: int = 0
    archived: int = 0
    unarchived: int = 0
    already: bool = False
    #: Sets the server archived to keep under the installed-sets limit.
    archived_sets: list[str] = []


class StickerSetOrder(Model):
    type: str = "sticker"
    order: list[int] = []
    ok: bool = True


class FaveResult(Model):
    doc_ids: list[int] = []
    faved: int = 0
    unfaved: int = 0
    already: bool = False
    evicted: list[int] = []


class RecentResult(Model):
    doc_ids: list[int] = []
    removed: int = 0
    cleared: bool = False
    already: bool = False


class PackCreated(Model):
    id: int = 0
    short_name: str = ""
    title: str = ""
    type: str = "sticker"
    count: int = 0
    link: str | None = None
    stickers: list[Sticker] = []
    #: `--dry-run`: the short name the server would accept.
    available: bool | None = None


class PackStickerAdded(Model):
    short_name: str
    doc_id: int = 0
    position: int | None = None
    count: int = 0
    replaced: int | None = None


class PackStickerRemoved(Model):
    short_name: str
    removed: list[int] = []
    count: int = 0


class PackEdited(Model):
    short_name: str
    title: str | None = None
    thumb: MediaFile | None = None
    sticker: int | None = None
    changed: list[str] = []


class PackDeleted(Model):
    short_name: str
    deleted: bool = False


class SavedGif(Model):
    doc_id: int
    access_hash: int | None = None
    file_reference_b64: str | None = None
    index: int | None = None
    mime: str | None = None
    size: int | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    file_id: str | None = None
    path: str | None = None


class GifSaved(Model):
    doc_ids: list[int] = []
    saved: int = 0
    removed: int = 0
    already: bool = False
    evicted: list[int] = []


class GifResult(Model):
    """One inline-bot result from the GIF search bot."""

    result_id: str = ""
    query_id: int = 0
    type: str = "gif"
    title: str | None = None
    url: str | None = None
    thumb: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    doc_id: int | None = None


class GifSent(Model):
    chat_id: int
    msg_id: int = 0
    doc_id: int | None = None
    via_bot: str | None = None
    saved: bool = False


class EmojiGroup(Model):
    """A category chip above the sticker / emoji / GIF search box."""

    kind: str = "groups"
    title: str = ""
    icon_emoji_id: int | None = None
    emoticons: list[str] = []
    document_ids: list[int] = []


class EmojiKeyword(Model):
    emoticon: str = ""
    keyword: str = ""
    lang: str = ""
    doc_id: int | None = None
    set_short_name: str | None = None
