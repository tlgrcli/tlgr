"""The `media` group: bytes in, bytes out, and everything that describes them.

The group exists because a file is not a message. v1 had two commands —
`media download <chat> <id>` and `media upload <chat> <path>` — each a single
Telethon call with no resume, no progress, no album, no thumbnail, no size
check and no way to ask what a file *is* before fetching it. Everything here
is organised around three facts that only show up at scale:

* **a `file_reference` expires.** Every path that touches bytes re-fetches its
  source first (`_media.fetch_message`), because the fix for
  `FILE_REFERENCE_EXPIRED` is never a retry.
* **the server's limits are the server's.** `media limit get` reads
  `help.getAppConfig`; `media upload` refuses an oversized file *before* the
  first part goes out rather than after twenty minutes of upload.
* **a transfer is a thing with a lifetime.** `--background` hands it to the
  daemon, `media transfer list` shows it, `media transfer stop` cancels it and
  `media transfer retry` re-fetches the message before restarting. That is
  what the GUI's Downloads panel is, and it is client-local state in both.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from tlgr.core.errors import (
    EXIT_EMPTY,
    NotFoundError,
    NotSupportedError,
    PermissionError_,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.media import (
    AutoDownloadPreset,
    AutoDownloadSaved,
    AutoDownloadSettings,
    AutoSaveException,
    AutoSaveRule,
    AutoSaveSaved,
    AutoSaveSettings,
    ContentSettings,
    ContentSettingsSaved,
    Downloaded,
    ExportResult,
    FileRef,
    MediaEdited,
    MediaEvent,
    MediaInfo,
    MediaItem,
    MediaLimits,
    MediaQuality,
    MediaRead,
    MediaSize,
    PaidItem,
    PaidPost,
    StorageCleared,
    StorageUsage,
    Transfer,
    TransferRestarted,
    TransferStopped,
    Uploaded,
    Wallpaper,
    WallpaperInstalled,
    WallpaperRemoved,
    WallpaperSettings,
    WallpaperUploaded,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _media, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer, message_entities
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The naming pattern `--out-dir` fills in.
DEFAULT_TEMPLATE = "{date}_{id}_{name}"

_EXAMPLE_DOWNLOAD: dict[str, Any] = {
    "msg_id": 12345,
    "chat_id": 777123,
    "path": "/home/u/.tlgr/downloads/alice/2026-09-03_12345_cat.jpg",
    "bytes": 184320,
    "kind": "photo",
    "mime": "image/jpeg",
}

_EXAMPLE_ITEM: dict[str, Any] = {
    "msg_id": 12345,
    "chat_id": 777123,
    "date": "2026-09-03T09:14:07Z",
    "date_unix": 1788340447,
    "kind": "photo",
    "name": "cat.jpg",
    "size": 184320,
    "mime": "image/jpeg",
}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _client(ctx: OpContext) -> Any:
    return _media.client(ctx)


def _downloads_root(ctx: OpContext) -> Path:
    """`~/.tlgr/downloads`, from the daemon's own paths rather than `~`.

    Reading `HOME` here would make a test write into the developer's home
    directory; the daemon knows where its base is and hands it over.
    """
    paths = getattr(ctx, "paths", None)
    if paths is not None and getattr(paths, "downloads", None) is not None:
        return Path(paths.downloads)
    from tlgr.core.config import get_downloads_dir

    return Path(get_downloads_dir())


def _safe_name(name: str) -> str:
    """A server-supplied file name that cannot escape the output directory.

    `../../.ssh/authorized_keys` is a legal `DocumentAttributeFilename`, and
    joining one onto `--out-dir` is how a download becomes an overwrite.
    """
    cleaned = os.path.basename((name or "").replace("\\", "/")).strip()
    cleaned = cleaned.lstrip(".") or "file"
    return cleaned[:180]


def _fill_template(template: str, *, date: str, message_id: int, name: str) -> str:
    try:
        return _safe_name(template.format(date=date[:10] or "0000-00-00", id=message_id, name=name))
    except (KeyError, IndexError) as exc:
        raise UsageError(
            f"--name-template: unknown placeholder {exc}; use {{date}}, {{id}}, {{name}}",
            field="name_template",
        ) from exc


def _unique(target: Path) -> Path:
    """`cat.jpg` → `cat (2).jpg` rather than an overwrite."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return target


def _size_arg(value: str | None, field: str) -> int | None:
    """`20M`, `512k`, `1073741824` → bytes."""
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    factor = 1
    for suffix, scale in (("k", 1024), ("m", 1024**2), ("g", 1024**3)):
        if text.endswith(suffix):
            factor, text = scale, text[:-1]
            break
    try:
        return int(float(text) * factor)
    except ValueError as exc:
        raise UsageError(
            f"--{field.replace('_', '-')}: {value!r} is not a size", field=field
        ) from exc


def _byte_range(value: str | None) -> tuple[int, int | None]:
    """`0-1M`, `5M-`, `1024-2047` → `(offset, limit)`."""
    if not value:
        return 0, None
    head, _, tail = str(value).partition("-")
    start = _size_arg(head or "0", "range") or 0
    end = _size_arg(tail, "range") if tail else None
    return start, (end - start + 1) if end is not None and end >= start else None


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def _is_premium(ctx: OpContext) -> bool:
    """Whether this account is Premium, which doubles most upload limits."""
    session = getattr(ctx, "session", None)
    return bool(getattr(getattr(session, "me", None), "premium", False))


def _transfers(ctx: OpContext) -> Any:
    store = getattr(ctx, "transfers", None)
    if store is None:
        raise NotSupportedError(
            "this build has no transfer store; transfers live in the daemon and "
            "this operation was not reached through it"
        )
    return store


async def _download_bytes(
    ctx: OpContext,
    location: Any,
    target: Path,
    *,
    size: int = 0,
    dc_id: int = 0,
    offset: int = 0,
    limit: int | None = None,
    resume: bool = True,
    part_size: int = 512 * 1024,
    connections: int = 1,
    refresh: Any = None,
    progress: Any = None,
) -> Path:
    """The daemon's download pipeline, reached the way `upload_file` is.

    `ops/` may not import `daemon/` (§2.2), so the pipeline is a service on
    the context — which is also what makes an operation testable without a
    socket.
    """
    download = getattr(ctx, "download_file", None)
    if download is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot download files")
    written: Any = await download(
        location,
        target,
        size=size,
        dc_id=dc_id,
        offset=offset,
        limit=limit,
        resume=resume,
        part_size=part_size,
        connections=connections,
        refresh=refresh,
        progress=progress,
    )
    return Path(written)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# media get
# ---------------------------------------------------------------------------


class GetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat holding the media.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id.")]
    sizes: Annotated[bool, opt("--sizes", help="List every size/thumbnail variant.")] = False
    qualities: Annotated[
        bool, opt("--qualities", help="List the alt_documents video transcodes.")
    ] = False
    stickers: Annotated[
        bool, opt("--stickers", help="List sticker sets baked into the photo/video.")
    ] = False
    ads: Annotated[
        bool, opt("--ads", help="List sponsored inserts (listed only, never viewed).")
    ] = False
    refresh: Annotated[
        bool, opt("--refresh", help="Re-fetch the message so ids and paid state are current.")
    ] = True
    album: Annotated[bool, opt("--album", help="Report every sibling sharing grouped_id.")] = False


def _media_info(message: Any, *, chat_id: int, sizes: bool, qualities: bool) -> MediaInfo:
    """The one function that turns a message's media into `MediaInfo`."""
    media = getattr(message, "media", None)
    document = _media.document_of(media)
    photo = _media.photo_of(media) if document is None else None
    facts = _media.attributes_of(document) if document is not None else {}
    date, date_unix = _media.message_dates(message)

    from tlgr.ops._serialize import media_summary

    summary = media_summary(media)
    info = MediaInfo(
        chat_id=chat_id,
        msg_id=int(getattr(message, "id", 0) or 0),
        kind=summary.kind if summary is not None else "unsupported",
        tl_type=type(media).__name__ if media is not None else "",
        grouped_id=getattr(message, "grouped_id", None),
        mime=getattr(document, "mime_type", None),
        size=getattr(document, "size", None),
        file_name=facts.get("file_name"),
        width=facts.get("width"),
        height=facts.get("height"),
        duration=int(facts["duration"]) if facts.get("duration") is not None else None,
        supports_streaming=bool(facts.get("supports_streaming")),
        nosound=bool(facts.get("nosound")),
        round=bool(facts.get("round")),
        voice=bool(facts.get("voice")),
        waveform=bool(facts.get("waveform")),
        title=facts.get("title"),
        performer=facts.get("performer"),
        sticker=facts.get("sticker"),
        custom_emoji=facts.get("custom_emoji"),
        spoiler=bool(getattr(media, "spoiler", False)),
        ttl_seconds=getattr(media, "ttl_seconds", None),
        video_cover=getattr(getattr(media, "video_cover", None), "id", None),
        video_timestamp=getattr(media, "video_timestamp", None),
        has_stickers=bool(getattr(document, "has_stickers", False))
        or bool(getattr(photo, "has_stickers", False)),
        protected=bool(getattr(message, "noforwards", False)),
        paid=getattr(media, "stars_amount", None),
        dc_id=getattr(document or photo, "dc_id", None),
        doc_id=getattr(document or photo, "id", None),
        access_hash=getattr(document or photo, "access_hash", None),
        file_reference_b64=_media.b64(getattr(document or photo, "file_reference", None)),
        file_id=_media.file_id_of(media),
        date=date,
        date_unix=date_unix,
        caption=getattr(message, "message", "") or "",
        entities=message_entities(message),
    )
    if sizes:
        if photo is not None:
            info.thumbs = _media.photo_sizes(photo)
        else:
            info.thumbs = [
                MediaSize(
                    type=str(getattr(size, "type", "") or ""),
                    width=getattr(size, "w", None),
                    height=getattr(size, "h", None),
                    size=getattr(size, "size", None),
                    bytes_b64=_media.b64(getattr(size, "bytes", None)),
                )
                for size in (getattr(document, "thumbs", None) or [])
            ]
    if qualities:
        info.qualities = [
            MediaQuality(
                doc_id=int(getattr(alt, "id", 0) or 0),
                mime=getattr(alt, "mime_type", None),
                size=getattr(alt, "size", None),
                width=_media.attributes_of(alt).get("width"),
                height=_media.attributes_of(alt).get("height"),
                name=_media.attributes_of(alt).get("file_name"),
            )
            for alt in (getattr(media, "alt_documents", None) or [])
        ]
    return info


async def get(ctx: OpContext, req: GetReq) -> MediaInfo:
    """Everything the media declares, without downloading a byte."""
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    message = await _media.fetch_message(ctx, peer, req.msg_id)
    if getattr(message, "media", None) is None:
        raise NotFoundError(f"message {req.msg_id} carries no media")

    info = _media_info(message, chat_id=chat_id, sizes=req.sizes, qualities=req.qualities)
    if req.stickers:
        info.attached_sets = await _attached_sets(ctx, message)
    if req.ads:
        info.ads = await _video_ads(ctx, peer, req.msg_id)
    if req.album and info.grouped_id is not None:
        info.album = await _album_siblings(ctx, peer, chat_id, message)
    return info


async def _attached_sets(ctx: OpContext, message: Any) -> list[str]:
    """`messages.getAttachedStickers` — sets composited into the image."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    media = getattr(message, "media", None)
    document = _media.document_of(media)
    photo = _media.photo_of(media) if document is None else None
    if document is not None:
        stickered: Any = types.InputStickeredMediaDocument(id=_media.input_document(document))
    elif photo is not None:
        stickered = types.InputStickeredMediaPhoto(id=_media.input_photo(photo))
    else:
        return []
    result = await _client(ctx)(fn.GetAttachedStickersRequest(media=stickered))
    names: list[str] = []
    for covered in result or []:
        inner = getattr(covered, "set", covered)
        name = getattr(inner, "short_name", None)
        if name:
            names.append(str(name))
    return names


async def _video_ads(ctx: OpContext, peer: Any, message_id: int) -> list[dict[str, Any]]:
    """Sponsored inserts, listed and never viewed.

    `viewSponsoredMessage`/`clickSponsoredMessage` are deliberately absent:
    calling either would inflate an advertiser's metrics on the user's behalf.
    """
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetSponsoredMessagesRequest(peer=peer, msg_id=message_id))
    out: list[dict[str, Any]] = []
    for item in getattr(result, "messages", None) or []:
        out.append(
            {
                "title": getattr(item, "title", None),
                "message": getattr(item, "message", None),
                "url": getattr(item, "url", None),
                "sponsor": getattr(item, "sponsor_info", None),
            }
        )
    return out


async def _album_siblings(ctx: OpContext, peer: Any, chat_id: int, message: Any) -> list[MediaInfo]:
    """The other messages of this media group.

    Album membership is invisible without `grouped_id`, and the ids are not
    contiguous in general — so the window around the message is read and
    filtered rather than guessed.
    """
    grouped = getattr(message, "grouped_id", None)
    if grouped is None:
        return []
    message_id = int(getattr(message, "id", 0))
    ids = [i for i in range(max(1, message_id - 10), message_id + 11) if i != message_id]
    found = await _client(ctx).get_messages(peer, ids=ids)
    return [
        _media_info(sibling, chat_id=chat_id, sizes=False, qualities=False)
        for sibling in (found or [])
        if sibling is not None and getattr(sibling, "grouped_id", None) == grouped
    ]


SPEC_GET = OperationSpec(
    id="media.get",
    request=GetReq,
    response=MediaInfo,
    impl=get,
    summary="Everything a message's media declares, without downloading a byte",
    description=(
        "The JSON contract the whole group leans on: kind, mime, dimensions, "
        "duration, album membership, ids and access hash, protection and paid "
        "state. --sizes lists every thumbnail variant (the stripped and vector "
        "ones cost no request at all), --qualities the alt_documents "
        "transcodes, --stickers the sets baked into the image, --ads the "
        "sponsored inserts (listed, never viewed)."
    ),
    aliases=("media.info",),
    columns=("kind", "mime", "size", "file_name"),
    headers=("Kind", "MIME", "Size", "Name"),
    empty_exit=EXIT_EMPTY,
    example={
        "chat_id": 777123,
        "msg_id": 12345,
        "kind": "video",
        "mime": "video/mp4",
        "size": 8412300,
        "width": 1280,
        "height": 720,
        "duration": 42,
        "supports_streaming": True,
    },
    example_args="media get @alice 12345",
    covers=(
        "media.attached-stickers",
        "media.dc-routing",
        "media.info",
        "media.stripped-vector-thumbnails",
        "media.video-message-ads",
        "media.video-quality-select",
    ),
    covers_partial=("media.file-id-export-import", "media.paid-media-inspect"),
    coverage_note=(
        "file_id is reported here; refreshing an expired one is `media file-id get`. "
        "Paid posts are listed and refreshed by `media paid list`."
    ),
    tags=frozenset({"read-only"}),
)


# ---------------------------------------------------------------------------
# media list
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to read.")]
    type: Annotated[
        str,
        choice(*_media.MEDIA_FILTERS, help="Which shared-media tab."),
    ] = "media"
    query: Annotated[
        str | None,
        opt("-q", "--query", metavar="TEXT", help="Server-side search over name and caption."),
    ] = None
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Only at/after this time.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Only before this time.")
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Restrict to a forum topic.")
    ] = None
    counts: Annotated[bool, opt("--counts", help="Per-type counters instead of items.")] = False
    calendar: Annotated[
        bool, opt("--calendar", help="Per-day counts and jump ids instead of items.")
    ] = False
    month: Annotated[
        str | None, opt("--month", metavar="YYYY-MM", help="Month for --calendar.")
    ] = None
    ids_only: Annotated[
        bool, opt("--ids-only", help="Bare message ids, to pipe into another command.")
    ] = False
    positions: Annotated[bool, opt("--positions", help="Sparse scrollbar positions.")] = False


def _item_from_message(message: Any, *, chat_id: int, chat: Any = None) -> MediaItem:
    media = getattr(message, "media", None)
    document = _media.document_of(media)
    facts = _media.attributes_of(document) if document is not None else {}
    date, date_unix = _media.message_dates(message)
    from tlgr.ops._serialize import media_summary

    summary = media_summary(media)
    return MediaItem(
        msg_id=int(getattr(message, "id", 0) or 0),
        chat_id=chat_id,
        date=date,
        date_unix=date_unix,
        kind=summary.kind if summary is not None else None,
        name=facts.get("file_name") or facts.get("title"),
        size=getattr(document, "size", None),
        duration=int(facts["duration"]) if facts.get("duration") is not None else None,
        mime=getattr(document, "mime_type", None),
        from_id=getattr(message, "sender_id", None),
        grouped_id=getattr(message, "grouped_id", None),
        file_id=_media.file_id_of(media),
        chat=chat,
    )


async def list_media(ctx: OpContext, req: ListReq) -> Page[MediaItem]:
    """One shared-media tab of a chat, one signed page at a time."""
    limit, state = _media.window(ctx, "media.list", PageKind.SEARCH)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.counts:
        return await _search_counters(ctx, peer, req)
    if req.calendar:
        return await _search_calendar(ctx, peer, req, limit)
    if req.positions:
        return await _search_positions(ctx, peer, req, chat_id, limit)

    kwargs: dict[str, Any] = {
        "filter": _media.media_filter(req.type),
        "offset_id": int(state.get("offset_id", 0)),
        "limit": limit,
    }
    if req.query:
        kwargs["search"] = req.query
    if req.topic is not None:
        kwargs["reply_to"] = req.topic
    if req.until:
        kwargs["offset_date"] = parse_dt(req.until)

    items: list[MediaItem] = []
    floor = to_unix(parse_dt(req.since)) if req.since else None
    async for message in _client(ctx).iter_messages(peer, **kwargs):
        if message is None:
            continue
        item = _item_from_message(message, chat_id=chat_id)
        if floor is not None and item.date_unix < floor:
            continue
        if req.ids_only:
            item = MediaItem(msg_id=item.msg_id, chat_id=chat_id)
        items.append(item)

    next_state = {"offset_id": items[-1].msg_id} if items else {}
    return build_page(
        items,
        op="media.list",
        kind=PageKind.SEARCH,
        state=next_state,
        account=ctx.account,
        limit=limit,
        has_more=None if items else False,
    )


async def _search_counters(ctx: OpContext, peer: Any, req: ListReq) -> Page[MediaItem]:
    """`messages.getSearchCounters` — the numbers above the media tabs."""
    from telethon.tl.functions import messages as fn

    names = [name for name in _media.MEDIA_FILTERS if name != "all"]
    result = await _client(ctx)(
        fn.GetSearchCountersRequest(
            peer=peer,
            filters=[_media.media_filter(name) for name in names],
            top_msg_id=req.topic,
        )
    )
    items = [
        MediaItem(type=name, count=int(getattr(entry, "count", 0) or 0))
        for name, entry in zip(names, result or [], strict=False)
    ]
    return Page(items=items, has_more=False, total=len(items))


async def _search_calendar(ctx: OpContext, peer: Any, req: ListReq, limit: int) -> Page[MediaItem]:
    """`messages.getSearchResultsCalendar` — per-day counts and jump ids."""
    from telethon.tl.functions import messages as fn

    offset_date = parse_dt(f"{req.month}-01") if req.month else None
    result = await _client(ctx)(
        fn.GetSearchResultsCalendarRequest(
            peer=peer,
            filter=_media.media_filter(req.type),
            offset_id=0,
            offset_date=offset_date,
        )
    )
    items: list[MediaItem] = []
    for period in getattr(result, "periods", None) or []:
        stamp = fmt_dt(getattr(period, "date", None)) or ""
        items.append(
            MediaItem(
                msg_id=int(getattr(period, "min_msg_id", 0) or 0),
                date=stamp,
                date_unix=to_unix(getattr(period, "date", None)) or 0,
                period=stamp[:10],
                count=int(getattr(period, "count", 0) or 0),
            )
        )
    return Page(items=items[:limit], has_more=False, total=int(getattr(result, "count", 0) or 0))


async def _search_positions(
    ctx: OpContext, peer: Any, req: ListReq, chat_id: int, limit: int
) -> Page[MediaItem]:
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(
        fn.GetSearchResultsPositionsRequest(
            peer=peer, filter=_media.media_filter(req.type), offset_id=0, limit=limit
        )
    )
    items = [
        MediaItem(
            msg_id=int(getattr(position, "msg_id", 0) or 0),
            chat_id=chat_id,
            count=int(getattr(position, "offset", 0) or 0),
            date=fmt_dt(getattr(position, "date", None)) or "",
        )
        for position in getattr(result, "positions", None) or []
    ]
    return Page(items=items, has_more=False, total=int(getattr(result, "count", 0) or 0))


SPEC_LIST = OperationSpec(
    id="media.list",
    request=ListReq,
    response=Page[MediaItem],
    impl=list_media,
    summary="Shared media of a chat, one media type at a time",
    description=(
        "--type maps one-to-one onto Telegram's own tabs, including "
        "`chat-photo`, which is a group or channel's avatar history. --counts "
        "returns the per-tab counters, --calendar the per-day counts with a "
        "jump id, --ids-only bare ids so the result can drive `message "
        "forward`, `message delete` or `media download`."
    ),
    paginated=PageKind.SEARCH,
    columns=("msg_id", "date", "kind", "name", "size"),
    headers=("ID", "Date", "Kind", "Name", "Size"),
    example={"items": [_EXAMPLE_ITEM], "has_more": False},
    example_args="media list @alice --type photo",
    covers=(
        "chat.photo-history",
        "media.music-player-queue",
        "media.saved-messages-drive",
        "media.shared-media-calendar",
        "media.shared-media-counters",
        "media.shared-media-list",
        "media.shared-media-search",
    ),
    covers_partial=("media.shared-media-bulk-actions",),
    coverage_note="--ids-only feeds the bulk verbs; `media export` owns the bulk download.",
)


# ---------------------------------------------------------------------------
# media search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", required=False, help="Text to find.")] = ""
    type: Annotated[
        str,
        choice(
            "photo",
            "video",
            "media",
            "file",
            "link",
            "music",
            "voice",
            "gif",
            help="Content-type tab.",
        ),
    ] = "media"
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Only at/after this time.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Only before this time.")
    ] = None
    sent: Annotated[
        bool, opt("--sent", help="Only your own recently-sent media (one capped page).")
    ] = False
    source: Annotated[
        str,
        choice("any", "saved", "chats", "inline", help="Music picker source."),
    ] = "any"
    bot: Annotated[
        PeerRef | None,
        opt("--bot", metavar="USER", kind="user", help="Inline bot for --source inline."),
    ] = None
    folder: Annotated[
        int | None, opt("--folder", metavar="ID", help="Restrict to a chat folder.")
    ] = None
    broadcasts_only: Annotated[bool, opt("--broadcasts-only", help="Only channels.")] = False


async def search(ctx: OpContext, req: SearchReq) -> Page[MediaItem]:
    """Search media across every chat — the GUI's global media tabs."""
    limit, state = _media.window(ctx, "media.search", PageKind.SEARCH)
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.sent:
        return await _search_sent(ctx, req, limit)
    if req.source == "saved":
        return await _saved_music(ctx, limit)
    if req.source == "inline":
        return await _inline_music(ctx, req, limit)

    offset_peer: Any = types.InputPeerEmpty()
    if state.get("offset_peer"):
        from tlgr.models.peer import parse_peer_ref

        with contextlib.suppress(Exception):
            offset_peer = await _send.resolve(ctx, parse_peer_ref(str(state["offset_peer"])))

    result = await _client(ctx)(
        fn.SearchGlobalRequest(
            q=req.query or "",
            filter=_media.media_filter(req.type),
            min_date=parse_dt(req.since) if req.since else None,
            max_date=parse_dt(req.until) if req.until else None,
            offset_rate=int(state.get("offset_rate", 0)),
            offset_peer=offset_peer,
            offset_id=int(state.get("offset_id", 0)),
            limit=limit,
            broadcasts_only=req.broadcasts_only or None,
            folder_id=req.folder,
        )
    )
    peers = {
        entity.id: entity_to_peer(entity)
        for entity in [
            *(getattr(result, "users", None) or []),
            *(getattr(result, "chats", None) or []),
        ]
    }
    items: list[MediaItem] = []
    last: Any = None
    for message in getattr(result, "messages", None) or []:
        from tlgr.ops._serialize import peer_id_of as marked_of

        chat_id = marked_of(getattr(message, "peer_id", None)) or 0
        raw_id = abs(chat_id) if chat_id > 0 else abs(chat_id) % 1000000000000
        items.append(_item_from_message(message, chat_id=chat_id, chat=peers.get(raw_id)))
        last = message

    # searchGlobal paginates on the *triple*; an offset_id alone silently loops.
    next_state: dict[str, Any] = {}
    if last is not None:
        next_state = {
            "offset_rate": int(getattr(result, "next_rate", 0) or 0),
            "offset_id": int(getattr(last, "id", 0) or 0),
            "offset_peer": items[-1].chat_id,
        }
    return build_page(
        items,
        op="media.search",
        kind=PageKind.SEARCH,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


async def _search_sent(ctx: OpContext, req: SearchReq, limit: int) -> Page[MediaItem]:
    """`messages.searchSentMedia` — the attach dialog's "Recent files".

    One capped page by design: the endpoint takes no offset at all, so a
    cursor would be a promise the server cannot keep.
    """
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(
        fn.SearchSentMediaRequest(
            q=req.query or "", filter=_media.media_filter(req.type), limit=limit
        )
    )
    from tlgr.ops._serialize import peer_id_of as marked_of

    items = [
        _item_from_message(message, chat_id=marked_of(getattr(message, "peer_id", None)) or 0)
        for message in (getattr(result, "messages", None) or [])
    ]
    return Page(items=items, has_more=False, total=len(items))


async def _saved_music(ctx: OpContext, limit: int) -> Page[MediaItem]:
    """`users.getSavedMusic` — the tracks saved on this profile."""
    from telethon.tl import types
    from telethon.tl.functions import users as fn

    result = await _client(ctx)(
        fn.GetSavedMusicRequest(id=types.InputUserSelf(), offset=0, limit=limit, hash=0)
    )
    items: list[MediaItem] = []
    for document in getattr(result, "documents", None) or []:
        facts = _media.attributes_of(document)
        items.append(
            MediaItem(
                kind="audio",
                name=facts.get("title") or facts.get("file_name"),
                size=getattr(document, "size", None),
                duration=int(facts["duration"]) if facts.get("duration") is not None else None,
                mime=getattr(document, "mime_type", None),
                file_id=_media.file_id_of(document),
            )
        )
    return Page(items=items, has_more=False, total=len(items))


async def _inline_music(ctx: OpContext, req: SearchReq, limit: int) -> Page[MediaItem]:
    """The inline half of the music picker."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.bot is None:
        raise UsageError("--source inline needs --bot", field="bot")
    bot = await _send.resolve(ctx, req.bot)
    result = await _client(ctx)(
        fn.GetInlineBotResultsRequest(
            bot=bot, peer=types.InputPeerEmpty(), query=req.query or "", offset=""
        )
    )
    items: list[MediaItem] = []
    for entry in (getattr(result, "results", None) or [])[:limit]:
        document = getattr(entry, "document", None)
        facts = _media.attributes_of(document) if document is not None else {}
        items.append(
            MediaItem(
                kind="audio",
                name=getattr(entry, "title", None) or facts.get("file_name"),
                mime=getattr(document, "mime_type", None),
                duration=int(facts["duration"]) if facts.get("duration") is not None else None,
                file_id=_media.file_id_of(document) if document is not None else None,
            )
        )
    return Page(items=items, has_more=False, total=len(items))


SPEC_SEARCH = OperationSpec(
    id="media.search",
    request=SearchReq,
    response=Page[MediaItem],
    impl=search,
    summary="Search media across every chat",
    description=(
        "`messages.searchGlobal` paginates on the triple (offset_rate, "
        "offset_peer, offset_id) and a plain offset_id silently loops, so the "
        "cursor carries all three. An empty query with a --type is legal and "
        "is how the GUI fills its tabs. --sent is the attach dialog's recent "
        "files: one capped page, because the endpoint takes no offset."
    ),
    paginated=PageKind.SEARCH,
    columns=("msg_id", "chat_id", "date", "kind", "name"),
    headers=("ID", "Chat", "Date", "Kind", "Name"),
    example={"items": [_EXAMPLE_ITEM], "has_more": False},
    example_args="media search invoice --type file",
    covers=(
        "media.attach-music-picker",
        "media.global-media-search",
        "media.recent-sent-media-search",
    ),
)


# ---------------------------------------------------------------------------
# media download
# ---------------------------------------------------------------------------


class DownloadReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="Chat holding the media."),
    ] = None
    msg_id: Annotated[
        list[str],
        arg(1, metavar="MSG_ID", required=False, variadic=True, help="Message ids, or `-`."),
    ] = []
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Write to this exact file.")
    ] = None
    out_dir: Annotated[
        str | None, opt("--out-dir", metavar="DIR", kind="path", help="Write into this directory.")
    ] = None
    stdout: Annotated[
        bool, opt("--stdout", help="Spool the bytes and report the path (see the note).")
    ] = False
    play: Annotated[
        str | None, opt("--play", metavar="CMD", help="Refused: the daemon spawns no players.")
    ] = None
    name_template: Annotated[
        str, opt("--name-template", metavar="PATTERN", help="Naming pattern for --out-dir.")
    ] = DEFAULT_TEMPLATE
    skip_existing: Annotated[
        bool, opt("--skip-existing", help="Skip when the target exists at the right size.")
    ] = False
    overwrite: Annotated[
        bool, opt("--overwrite", help="Overwrite instead of uniquifying the name.")
    ] = False
    album: Annotated[bool, opt("--album", help="Also fetch the message's album siblings.")] = False
    every: Annotated[
        bool, opt("--all", help="Every item in the chat matching --type/--from/--since.")
    ] = False
    max_items: Annotated[
        int,
        opt("--max", metavar="N", help="Cap the items fetched with --all.", ge=1, le=10000),
    ] = 100
    type: Annotated[str, choice(*_media.MEDIA_FILTERS, help="Media filter for --all.")] = "all"
    from_user: Annotated[
        PeerRef | None,
        opt("--from", metavar="USER", kind="user", help="Only media sent by this user."),
    ] = None
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Only after this time.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Only before this time.")
    ] = None
    thumb: Annotated[
        str | None,
        opt("--thumb", metavar="SIZE", help="Fetch a thumbnail: stripped, vector, or a type."),
    ] = None
    quality: Annotated[
        str | None, opt("--quality", metavar="Q", help="Pick an alt_documents transcode.")
    ] = None
    range: Annotated[
        str | None, opt("--range", metavar="START-END", help="Byte range (`0-1M`, `5M-`).")
    ] = None
    resume: Annotated[bool, opt("--resume", help="Continue from the .part sidecar.")] = True
    verify: Annotated[
        bool, opt("--verify", help="Check the bytes against upload.getFileHashes.")
    ] = False
    connections: Annotated[
        int, opt("--connections", metavar="N", help="Parallel ranged readers.", ge=1, le=8)
    ] = 1
    part_size: Annotated[
        int, opt("--part-size", metavar="KB", help="Request size in KB.", ge=4, le=512)
    ] = 512
    background: Annotated[
        bool, opt("--background", help="Hand the transfer to the daemon and print a job id.")
    ] = False
    read: Annotated[
        bool, opt("--read", help="Mark the media consumed afterwards (irreversible).")
    ] = False
    profile: Annotated[
        PeerRef | None,
        opt("--profile", metavar="PEER", kind="peer", help="Download a peer's avatar."),
    ] = None
    small: Annotated[bool, opt("--small", help="Small avatar variant.")] = False
    story: Annotated[
        str | None, opt("--story", metavar="PEER:ID", help="Download a story's media.")
    ] = None
    file_id: Annotated[
        str | None, opt("--file-id", metavar="ID", help="Download by portable file id.")
    ] = None
    web: Annotated[
        str | None, opt("--web", metavar="URL", help="Fetch a web document through Telegram.")
    ] = None
    map: Annotated[
        str | None, opt("--map", metavar="LAT,LON", help="Fetch a static map preview.")
    ] = None
    zoom: Annotated[int, opt("--zoom", metavar="N", help="Map zoom.", ge=1, le=20)] = 15
    size: Annotated[str, opt("--size", metavar="WxH", help="Map/thumb size.")] = "600x400"
    allow_protected: Annotated[
        bool,
        opt(
            "--allow-protected",
            help="Download content the chat marks as protected (noforwards).",
        ),
    ] = False
    no_cdn: Annotated[bool, opt("--no-cdn", help="Debug: read from the master DC.")] = False


def _ids_from(values: list[str]) -> list[int]:
    """`-` reads ids from stdin, one per line or as a JSON page."""
    import json
    import sys

    out: list[int] = []
    for value in values:
        text = str(value).strip()
        if text != "-":
            try:
                out.append(int(text))
            except ValueError as exc:
                raise UsageError(f"{text!r} is not a message id", field="msg_id") from exc
            continue
        if sys.stdin is None or sys.stdin.isatty():
            raise UsageError("'-' was given but stdin is a terminal", field="msg_id")
        raw = sys.stdin.read().strip()
        if raw.startswith(("{", "[")):
            data = json.loads(raw)
            rows = data.get("items", data) if isinstance(data, dict) else data
            out.extend(int(row["msg_id"] if isinstance(row, dict) else row) for row in rows)
        else:
            out.extend(int(line) for line in raw.split() if line.strip())
    return out


async def download(ctx: OpContext, req: DownloadReq) -> Page[Downloaded]:
    """Fetch bytes: message media, an avatar, a story, a file id or a web file."""
    if req.play:
        raise NotSupportedError(
            "--play is refused: the transfer runs inside the daemon, which does not "
            "spawn processes on your behalf. Download with --out and pipe the file."
        )
    if req.background:
        return await _background_download(ctx, req)

    started = time.monotonic()
    if req.profile is not None:
        return Page(items=[await _download_profile(ctx, req, started)], has_more=False)
    if req.story:
        return Page(items=[await _download_story(ctx, req, started)], has_more=False)
    if req.file_id:
        return Page(items=[await _download_file_id(ctx, req, started)], has_more=False)
    if req.web or req.map:
        return Page(items=[await _download_web(ctx, req, started)], has_more=False)

    if req.chat is None:
        raise UsageError(
            "a chat is required unless --profile/--story/--file-id/--web/--map is given",
            field="chat",
        )
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    messages = await _download_targets(ctx, req, peer)
    if not messages:
        raise NotFoundError("nothing here matches what you asked for")

    items: list[Downloaded] = []
    for message in messages:
        items.append(await _download_one(ctx, req, message, chat_id=chat_id, started=started))
    if req.read:
        await _mark_read(ctx, peer, [int(m.id) for m in messages])
    return Page(items=items, has_more=False, total=len(items))


async def _download_targets(ctx: OpContext, req: DownloadReq, peer: Any) -> list[Any]:
    """Which messages this invocation is about, always freshly fetched."""
    if req.every:
        kwargs: dict[str, Any] = {
            "filter": _media.media_filter(req.type),
            "limit": req.max_items,
        }
        if req.from_user is not None:
            kwargs["from_user"] = await _send.resolve(ctx, req.from_user)
        if req.until:
            kwargs["offset_date"] = parse_dt(req.until)
        floor = to_unix(parse_dt(req.since)) if req.since else None
        found = [
            message
            async for message in _client(ctx).iter_messages(peer, **kwargs)
            if message is not None and getattr(message, "media", None) is not None
        ]
        if floor is not None:
            found = [m for m in found if (to_unix(getattr(m, "date", None)) or 0) >= floor]
        return found

    ids = _ids_from(req.msg_id)
    if not ids:
        raise UsageError("give at least one message id, or --every", field="msg_id")
    found = [m for m in (await _client(ctx).get_messages(peer, ids=ids) or []) if m is not None]
    if req.album:
        found = await _with_album(ctx, peer, found)
    return found


async def _with_album(ctx: OpContext, peer: Any, messages: list[Any]) -> list[Any]:
    seen = {int(m.id) for m in messages}
    out = list(messages)
    for message in messages:
        grouped = getattr(message, "grouped_id", None)
        if grouped is None:
            continue
        message_id = int(message.id)
        window = [i for i in range(max(1, message_id - 10), message_id + 11) if i not in seen]
        for sibling in await _client(ctx).get_messages(peer, ids=window) or []:
            if sibling is None or getattr(sibling, "grouped_id", None) != grouped:
                continue
            seen.add(int(sibling.id))
            out.append(sibling)
    return sorted(out, key=lambda m: int(getattr(m, "id", 0)))


def _target_for(ctx: OpContext, req: DownloadReq, message: Any, chat_id: int) -> Path:
    """Where this item lands, with a server-supplied name that cannot escape."""
    media = getattr(message, "media", None)
    document = _media.document_of(media)
    facts = _media.attributes_of(document) if document is not None else {}
    date, _ = _media.message_dates(message)
    name = _safe_name(facts.get("file_name") or _default_name(media))
    if req.out and not req.stdout:
        return Path(os.path.expanduser(req.out))
    directory = (
        Path(os.path.expanduser(req.out_dir))
        if req.out_dir
        else _downloads_root(ctx) / str(chat_id)
    )
    return directory / _fill_template(
        req.name_template, date=date, message_id=int(getattr(message, "id", 0) or 0), name=name
    )


def _default_name(media: Any) -> str:
    document = _media.document_of(media)
    if document is None:
        return "photo.jpg"
    mime = getattr(document, "mime_type", "") or ""
    extension = {
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "application/x-tgsticker": ".tgs",
    }.get(mime, ".bin")
    return f"{getattr(document, 'id', 'file')}{extension}"


async def _download_one(
    ctx: OpContext, req: DownloadReq, message: Any, *, chat_id: int, started: float
) -> Downloaded:
    media = getattr(message, "media", None)
    if media is None:
        raise NotFoundError(f"message {getattr(message, 'id', '?')} carries no media")
    if getattr(message, "noforwards", False) and not req.allow_protected:
        raise PermissionError_(
            f"message {message.id} is in a chat that forbids saving content; "
            "pass --allow-protected to download it anyway"
        )

    target = _target_for(ctx, req, message, chat_id)
    document = _media.document_of(media)
    photo = _media.photo_of(media) if document is None else None
    from tlgr.ops._serialize import media_summary

    summary = media_summary(media)
    kind = summary.kind if summary is not None else "file"

    if req.thumb:
        path = await _download_thumb(ctx, req, message, target)
        return Downloaded(
            msg_id=int(message.id),
            chat_id=chat_id,
            path=str(path),
            bytes=path.stat().st_size if path.exists() else 0,
            kind="thumb",
            mime=getattr(document, "mime_type", None),
            elapsed_s=_elapsed(started),
        )

    location = _quality_pick(media, req.quality) if req.quality else (document or photo)
    size = int(getattr(location, "size", 0) or 0)
    if req.skip_existing and target.exists() and (not size or target.stat().st_size == size):
        return Downloaded(
            msg_id=int(message.id),
            chat_id=chat_id,
            path=str(target),
            bytes=target.stat().st_size,
            kind=kind,
            skipped=True,
        )
    if not req.overwrite:
        target = _unique(target)

    offset, limit = _byte_range(req.range)
    peer_ref = getattr(message, "peer_id", None)

    async def refresh() -> Any:
        fresh = await _media.fetch_message(ctx, peer_ref, int(message.id))
        return _media.document_of(fresh.media) or _media.photo_of(fresh.media)

    path = await _download_bytes(
        ctx,
        location,
        target,
        size=size,
        dc_id=int(getattr(location, "dc_id", 0) or 0),
        offset=offset,
        limit=limit,
        resume=req.resume,
        part_size=req.part_size * 1024,
        connections=req.connections,
        refresh=refresh,
    )
    digest = None
    if req.verify:
        digest = await _verify(ctx, location, path)
    if req.stdout:
        ctx.warn(
            "the daemon cannot write bytes to your terminal through the IPC socket; "
            "the file was spooled and its path is in `path`"
        )
    return Downloaded(
        msg_id=int(message.id),
        chat_id=chat_id,
        path=str(path),
        bytes=path.stat().st_size if path.exists() else 0,
        kind=kind,
        mime=getattr(document, "mime_type", None),
        sha256=digest,
        file_id=_media.file_id_of(media),
        elapsed_s=_elapsed(started),
    )


def _quality_pick(media: Any, wanted: str) -> Any:
    """`--quality 720` → the matching `alt_documents` transcode."""
    alternatives = list(getattr(media, "alt_documents", None) or [])
    if not alternatives:
        return _media.document_of(media)
    if wanted in ("original", "best"):
        return _media.document_of(media)
    if wanted == "smallest":
        return min(alternatives, key=lambda d: int(getattr(d, "size", 0) or 0))
    for document in alternatives:
        facts = _media.attributes_of(document)
        if str(facts.get("height") or "") == wanted or str(facts.get("width") or "") == wanted:
            return document
    raise UsageError(f"--quality {wanted!r}: this video has no such transcode", field="quality")


async def _download_thumb(ctx: OpContext, req: DownloadReq, message: Any, target: Path) -> Path:
    """A thumbnail, including the two that need no request at all.

    `photoStrippedSize` and `photoPathSize` are already in the message: the
    stripped one inflates to a JPEG locally and the vector one is an SVG
    outline. Fetching them over the network would be a round trip for bytes
    already in hand.
    """
    from telethon import utils

    media = getattr(message, "media", None)
    document = _media.document_of(media)
    photo = _media.photo_of(media) if document is None else None
    sizes = list(getattr(photo or document, "sizes", None) or []) or list(
        getattr(document, "thumbs", None) or []
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    if req.thumb in ("stripped", "vector"):
        wanted = "PhotoStrippedSize" if req.thumb == "stripped" else "PhotoPathSize"
        for size in sizes:
            if type(size).__name__ != wanted:
                continue
            raw = getattr(size, "bytes", b"") or b""
            data = utils.stripped_photo_to_jpg(raw) if req.thumb == "stripped" else raw
            target.write_bytes(data)
            return target
        raise NotFoundError(f"this media carries no {req.thumb} thumbnail")

    selector: Any = req.thumb
    if str(req.thumb).lstrip("-").isdigit():
        selector = int(str(req.thumb))
    elif req.thumb == "smallest":
        selector = 0
    elif req.thumb == "largest":
        selector = -1
    written = await _client(ctx).download_media(message, file=str(target), thumb=selector)
    return Path(written or target)


async def _verify(ctx: OpContext, location: Any, path: Path) -> str:
    """Check the file against `upload.getFileHashes`, block by block."""
    from telethon.tl.functions import upload as fn

    hashes = await _client(ctx)(fn.GetFileHashesRequest(location=location, offset=0))
    with open(path, "rb") as handle:
        for entry in hashes or []:
            offset = int(getattr(entry, "offset", 0) or 0)
            limit = int(getattr(entry, "limit", 0) or 0)
            handle.seek(offset)
            block = handle.read(limit)
            if hashlib.sha256(block).digest() != getattr(entry, "hash", b""):
                raise NotSupportedError(
                    f"the bytes at offset {offset} do not match the server's hash; "
                    "the download is corrupt"
                )
    return _sha256(path)


async def _download_profile(ctx: OpContext, req: DownloadReq, started: float) -> Downloaded:
    peer = await _send.resolve(ctx, req.profile)
    directory = (
        Path(os.path.expanduser(req.out_dir)) if req.out_dir else _downloads_root(ctx) / "profile"
    )
    target = Path(os.path.expanduser(req.out)) if req.out else directory / "avatar.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    written = await _client(ctx).download_profile_photo(
        peer, file=str(target), download_big=not req.small
    )
    if written is None:
        raise NotFoundError("that peer has no profile photo")
    path = Path(written)
    return Downloaded(
        chat_id=_send.peer_id_of(peer),
        path=str(path),
        bytes=path.stat().st_size if path.exists() else 0,
        kind="profile_photo",
        elapsed_s=_elapsed(started),
    )


async def _download_story(ctx: OpContext, req: DownloadReq, started: float) -> Downloaded:
    """`--story peer:id` — a story's media is an ordinary photo or document.

    The reference inside a `StoryItem` expires like any other, which is why
    the story is fetched here rather than taken from a cached listing.
    """
    from telethon.tl.functions import stories as fn

    reference, _, story_id = str(req.story).rpartition(":")
    if not reference or not story_id.lstrip("-").isdigit():
        raise UsageError("--story takes PEER:ID, e.g. --story @alice:42", field="story")
    from tlgr.models.peer import parse_peer_ref

    peer = await _send.resolve(ctx, parse_peer_ref(reference))
    result = await _client(ctx)(fn.GetStoriesByIDRequest(peer=peer, id=[int(story_id)]))
    stories = list(getattr(result, "stories", None) or [])
    if not stories:
        raise NotFoundError(f"story {story_id} is not available")
    media = getattr(stories[0], "media", None)
    document = _media.document_of(media) or _media.photo_of(media)
    directory = (
        Path(os.path.expanduser(req.out_dir)) if req.out_dir else _downloads_root(ctx) / "stories"
    )
    target = Path(os.path.expanduser(req.out)) if req.out else directory / f"story_{story_id}.bin"
    path = await _download_bytes(
        ctx,
        document,
        target,
        size=int(getattr(document, "size", 0) or 0),
        dc_id=int(getattr(document, "dc_id", 0) or 0),
        resume=req.resume,
        part_size=req.part_size * 1024,
        connections=req.connections,
    )
    return Downloaded(
        path=str(path),
        bytes=path.stat().st_size if path.exists() else 0,
        kind="story",
        msg_id=int(story_id),
        chat_id=_send.peer_id_of(peer),
        elapsed_s=_elapsed(started),
    )


async def _download_file_id(ctx: OpContext, req: DownloadReq, started: float) -> Downloaded:
    from telethon import utils

    try:
        location = utils.resolve_bot_file_id(req.file_id)
    except Exception as exc:
        raise UsageError(f"--file-id: {exc}", field="file_id") from exc
    if location is None:
        raise UsageError("--file-id: that is not a Telegram file id", field="file_id")
    directory = (
        Path(os.path.expanduser(req.out_dir)) if req.out_dir else _downloads_root(ctx) / "file-id"
    )
    target = (
        Path(os.path.expanduser(req.out))
        if req.out
        else directory / f"{getattr(location, 'id', 'file')}.bin"
    )
    path = await _download_bytes(
        ctx,
        location,
        target,
        size=int(getattr(location, "size", 0) or 0),
        dc_id=int(getattr(location, "dc_id", 0) or 0),
        resume=req.resume,
        part_size=req.part_size * 1024,
        connections=req.connections,
    )
    return Downloaded(
        path=str(path),
        bytes=path.stat().st_size if path.exists() else 0,
        kind="file_id",
        file_id=req.file_id,
        elapsed_s=_elapsed(started),
    )


async def _download_web(ctx: OpContext, req: DownloadReq, started: float) -> Downloaded:
    """`upload.getWebFile`: an inline-bot document, or a static map preview."""
    from telethon.tl import types
    from telethon.tl.functions import upload as fn

    width, _, height = req.size.lower().partition("x")
    if req.map:
        latitude, _, longitude = str(req.map).partition(",")
        try:
            point = types.InputGeoPoint(lat=float(latitude), long=float(longitude))
        except ValueError as exc:
            raise UsageError("--map takes LAT,LON", field="map") from exc
        location: Any = types.InputWebFileGeoPointLocation(
            geo_point=point,
            access_hash=0,
            w=int(width or 600),
            h=int(height or 400),
            zoom=req.zoom,
            scale=1,
        )
        name = f"map_{latitude}_{longitude}.png"
    else:
        location = types.InputWebFileLocation(url=str(req.web), access_hash=0)
        name = _safe_name(str(req.web).rsplit("/", 1)[-1] or "webfile.bin")

    directory = (
        Path(os.path.expanduser(req.out_dir)) if req.out_dir else _downloads_root(ctx) / "web"
    )
    target = Path(os.path.expanduser(req.out)) if req.out else directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    chunk_size = 512 * 1024
    written = 0
    with open(target, "wb") as handle:
        offset = 0
        while True:
            part = await _client(ctx)(
                fn.GetWebFileRequest(location=location, offset=offset, limit=chunk_size)
            )
            data = getattr(part, "bytes", b"") or b""
            handle.write(data)
            written += len(data)
            if len(data) < chunk_size:
                break
            offset += len(data)
    return Downloaded(
        path=str(target),
        bytes=written,
        kind="web",
        mime=getattr(part, "mime_type", None),
        elapsed_s=_elapsed(started),
    )


async def _mark_read(ctx: OpContext, peer: Any, ids: list[int]) -> None:
    from telethon import utils
    from telethon.tl.functions import channels as ch
    from telethon.tl.functions import messages as fn

    try:
        channel = utils.get_input_channel(peer)
    except (TypeError, ValueError):
        channel = None
    if channel is not None:
        await _client(ctx)(ch.ReadMessageContentsRequest(channel=channel, id=ids))
    else:
        await _client(ctx)(fn.ReadMessageContentsRequest(id=ids))


async def _background_download(ctx: OpContext, req: DownloadReq) -> Page[Downloaded]:
    """Hand the transfer to the daemon and answer with a job id."""
    store = _transfers(ctx)
    background = req.__class__(**{**_as_dict(req), "background": False})

    async def run() -> Any:
        return await download(ctx, background)

    record = store.submit(
        direction="download",
        name=str(req.out or req.out_dir or "download"),
        chat_id=_send.peer_id_of(await _send.resolve(ctx, req.chat)) if req.chat else None,
        factory=run,
    )
    return Page(
        items=[Downloaded(job_id=record.job_id, kind="queued", path="")],
        has_more=False,
        total=1,
    )


def _as_dict(request: Any) -> dict[str, Any]:
    """A request's own fields, so a copy can flip one of them."""
    import msgspec

    return {field.name: getattr(request, field.name) for field in msgspec.structs.fields(request)}


SPEC_DOWNLOAD = OperationSpec(
    id="media.download",
    request=DownloadReq,
    response=Page[Downloaded],
    impl=download,
    summary="Download media from messages, profile photos, stories or a file id",
    description=(
        "The message is re-fetched before a byte is read, because a "
        "file_reference expires and the fix is a re-fetch rather than a "
        "retry. --resume continues from the .part sidecar, --connections runs "
        "parallel ranged readers, --verify checks every block against "
        "upload.getFileHashes, and --thumb stripped/vector costs no request at "
        "all. --read is opt-in: without it a view-once photo stays unviewed "
        "for the sender, and with it the consumption is irreversible."
    ),
    aliases=("dl",),
    legacy_paths=("media download", "dl"),
    rate_class="file",
    timeout_s=900,
    columns=("msg_id", "path", "bytes", "kind"),
    headers=("ID", "Path", "Bytes", "Kind"),
    example={"items": [_EXAMPLE_DOWNLOAD], "has_more": False},
    example_args="media download @alice 12345",
    covers=(
        "media.download-album",
        "media.download-cdn-redirect",
        "media.download-message-media",
        "media.download-profile-photo",
        "media.download-range-resume",
        "media.download-thumbnail",
        "media.download-verify-hashes",
        "media.download-web-file",
        "media.parallel-transfer",
        "stories.download-media",
        "stories.story-video-alt-quality",
    ),
    covers_partial=(
        "media.background-transfer-jobs",
        "media.dc-routing",
        "media.download-batch",
        "media.download-stream-stdout",
        "media.file-id-export-import",
        "media.file-reference-refresh",
        "media.music-player-queue",
        "media.shared-media-bulk-actions",
        "media.stripped-vector-thumbnails",
        "media.transfer-progress",
        "media.video-quality-select",
        "media.view-self-destructing",
    ),
    coverage_note=(
        "The daemon owns the connection, so it cannot write bytes to the caller's "
        "terminal: --stdout spools the file and reports its path, and --play is "
        "refused rather than having the daemon spawn a player."
    ),
)


# ---------------------------------------------------------------------------
# media upload
# ---------------------------------------------------------------------------


class UploadReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to send it.")]
    path: Annotated[
        list[str],
        arg(
            1,
            metavar="PATH",
            required=False,
            variadic=True,
            kind="path",
            help="Files; `-` is stdin.",
        ),
    ] = []
    send_as_kind: Annotated[
        str,
        choice(
            "auto",
            "photo",
            "file",
            "video",
            "gif",
            "audio",
            "voice",
            "round",
            "sticker",
            help="Force the media kind instead of sniffing it.",
        ),
    ] = "auto"
    album: Annotated[bool, opt("--album", help="Group 2..10 items into one message group.")] = True
    name: Annotated[
        str | None, opt("--name", metavar="NAME", help="Override the remote file name.")
    ] = None
    mime: Annotated[str | None, opt("--mime", metavar="TYPE", help="Override the MIME type.")] = (
        None
    )
    url: Annotated[
        str | None, opt("--url", metavar="URL", help="Let Telegram fetch the media itself.")
    ] = None
    fetch_local: Annotated[
        bool, opt("--fetch-local", help="With --url: download locally first, then upload.")
    ] = False
    file_id: Annotated[
        str | None, opt("--file-id", metavar="ID", help="Re-send media already on Telegram.")
    ] = None
    from_message: Annotated[
        str | None,
        opt("--from-message", metavar="CHAT:ID", help="Re-send an existing message's media."),
    ] = None
    dice: Annotated[
        str | None, opt("--dice", metavar="EMOJI", help="Send an animated dice instead of a file.")
    ] = None
    thumb: Annotated[
        str | None, opt("--thumb", metavar="PATH", help="Cover thumbnail; `auto` extracts a frame.")
    ] = None
    cover: Annotated[str | None, opt("--cover", metavar="PATH", help="Video cover photo.")] = None
    start_at: Annotated[
        int | None,
        opt("--start-at", metavar="SECONDS", kind="duration", help="Video start offset."),
    ] = None
    duration: Annotated[
        int | None, opt("--duration", metavar="SECONDS", kind="duration", help="Explicit duration.")
    ] = None
    width: Annotated[int | None, opt("--width", metavar="PX", help="Explicit width.")] = None
    height: Annotated[int | None, opt("--height", metavar="PX", help="Explicit height.")] = None
    streaming: Annotated[bool, opt("--streaming", help="Mark the video streamable.")] = True
    no_sound: Annotated[
        bool, opt("--no-sound", help="Silent MP4 that must stay a video, not become a GIF.")
    ] = False
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="Audio title.")] = None
    performer: Annotated[
        str | None, opt("--performer", metavar="TEXT", help="Audio performer.")
    ] = None
    waveform: Annotated[str, choice("auto", "none", help="Voice-note waveform.")] = "auto"
    spoiler: Annotated[bool, opt("--spoiler", help="Blur until tapped.")] = False
    ttl: Annotated[
        int | None, opt("--ttl", metavar="SECONDS", kind="duration", help="Self-destruct timer.")
    ] = None
    once: Annotated[bool, opt("--once", help="View-once / play-once.")] = False
    quality: Annotated[
        str, choice("high", "standard", help="Original bytes, or downscale first.")
    ] = "high"
    live_video: Annotated[
        str | None,
        opt("--live-video", metavar="PATH", kind="path", help="Companion Live Photo video."),
    ] = None
    attached_sticker: Annotated[
        list[str],
        opt(
            "--attached-sticker",
            metavar="STICKER",
            help="Sticker already composited in (<set>/<n>).",
        ),
    ] = []
    dedupe: Annotated[
        bool, opt("--dedupe", help="Ask the server for the file by hash and skip the upload.")
    ] = False
    no_send: Annotated[
        bool, opt("--no-send", help="Upload only, and print the reusable file id.")
    ] = False
    part_size: Annotated[
        int, opt("--part-size", metavar="KB", help="Upload part size in KB.", ge=4, le=512)
    ] = 512
    connections: Annotated[
        int, opt("--connections", metavar="N", help="Parallel part uploaders.", ge=1, le=8)
    ] = 4
    progress: Annotated[
        bool, opt("--progress", help="Emit progress onto the event bus while uploading.")
    ] = False
    background: Annotated[
        bool, opt("--background", help="Upload inside the daemon and print a job id.")
    ] = False
    no_action: Annotated[
        bool, opt("--no-action", help="Do not broadcast the 'sending a photo…' chat action.")
    ] = False
    wait: Annotated[
        bool, opt("--wait", help="Wait for server-side video conversion to finish.")
    ] = False
    caption: Annotated[
        list[str],
        opt("--caption", metavar="TEXT", help="Caption; repeat once per album item."),
    ] = []
    caption_file: Annotated[
        str | None, opt("--caption-file", metavar="PATH", kind="path", help="Caption from a file.")
    ] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Caption parse mode.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    caption_above: Annotated[
        bool, opt("--caption-above", help="Render the caption above the media.")
    ] = False
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    quote: Annotated[
        str | None, opt("--quote", metavar="TEXT", help="Quoted fragment of the reply target.")
    ] = None
    clear_draft: Annotated[bool, opt("--clear-draft", help="Clear the chat draft on success.")] = (
        True
    )


def _upload_sources(req: UploadReq) -> list[Path]:
    """The local files, with `-` spooled off stdin.

    Spooling rather than streaming: a photo cannot be uploaded with an unknown
    part count, and the temp file is also what makes `--dedupe` and the
    pre-flight size check possible at all.
    """
    import sys
    import tempfile

    out: list[Path] = []
    for entry in req.path:
        if entry != "-":
            path = Path(os.path.expanduser(entry))
            if not path.exists():
                raise UsageError(f"{entry} does not exist", field="path")
            out.append(path)
            continue
        if sys.stdin is None or sys.stdin.isatty():
            raise UsageError("'-' was given for the file but stdin is a terminal", field="path")
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below, kept on disk
            prefix="tlgr-", suffix=Path(req.name or "stdin.txt").suffix or ".bin", delete=False
        )
        handle.write(sys.stdin.buffer.read())
        handle.close()
        out.append(Path(handle.name))
    return out


_PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic")


def _kind_for(path: Path, wanted: str) -> str:
    """`auto` → the kind the bytes are, and every explicit choice untouched."""
    if wanted != "auto":
        return wanted
    import mimetypes

    suffix = path.suffix.lower()
    if suffix in _PHOTO_SUFFIXES:
        return "photo"
    mime = mimetypes.guess_type(path.name)[0] or ""
    if suffix == ".gif" or mime == "image/gif":
        return "gif"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _attributes_for(ctx: OpContext, req: UploadReq, path: Path, kind: str) -> tuple[list[Any], str]:
    """Document attributes plus a mime type, probed rather than guessed.

    A video sent with `duration=0, w=1, h=1` renders as a 1×1 black rectangle
    in every client, so when nothing on the machine can read the file the
    caller is *warned* rather than silently sent a broken message.
    """
    import mimetypes

    from telethon.tl import types

    from tlgr.core.media import infer_attributes

    facts, warnings = infer_attributes(path)
    for warning in warnings:
        ctx.warn(warning)
    duration = int(req.duration if req.duration is not None else facts.get("duration") or 0)
    width = int(req.width if req.width is not None else facts.get("width") or 0)
    height = int(req.height if req.height is not None else facts.get("height") or 0)
    mime = req.mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    attributes: list[Any] = [
        types.DocumentAttributeFilename(file_name=_safe_name(req.name or path.name))
    ]

    if kind == "voice":
        attributes.append(types.DocumentAttributeAudio(duration=duration, voice=True))
        mime = req.mime or "audio/ogg"
        if req.waveform == "auto":
            ctx.warn("a waveform needs ffmpeg; the voice note is sent without one")
    elif kind == "round":
        attributes.append(
            types.DocumentAttributeVideo(
                duration=duration, w=width or 384, h=height or 384, round_message=True
            )
        )
        mime = req.mime or "video/mp4"
    elif kind == "video":
        attributes.append(
            types.DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=req.streaming,
                nosound=req.no_sound or None,
            )
        )
    elif kind == "gif":
        attributes.append(types.DocumentAttributeAnimated())
        attributes.append(
            types.DocumentAttributeVideo(
                duration=duration, w=width, h=height, nosound=req.no_sound or None
            )
        )
    elif kind == "audio":
        attributes.append(
            types.DocumentAttributeAudio(
                duration=duration, title=req.title, performer=req.performer
            )
        )
    elif kind == "sticker":
        from telethon.tl.types import InputStickerSetEmpty

        attributes.append(types.DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty()))
        mime = req.mime or "image/webp"
    return attributes, mime


def _ttl_seconds(req: UploadReq) -> int | None:
    """`--once` is the sentinel 0x7FFFFFFF, not a very long timer."""
    if req.once:
        return 0x7FFFFFFF
    return req.ttl


async def _uploaded_handle(ctx: OpContext, req: UploadReq, path: Path) -> Any:
    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    return await upload(
        path,
        part_size=req.part_size * 1024,
        parts_in_flight=req.connections,
        file_name=_safe_name(req.name or path.name),
    )


async def _thumb_handle(ctx: OpContext, req: UploadReq) -> Any:
    if not req.thumb:
        return None
    if req.thumb == "auto":
        ctx.warn("--thumb auto needs ffmpeg; the document is sent without a cover")
        return None
    path = Path(os.path.expanduser(req.thumb))
    if not path.exists():
        raise UsageError(f"--thumb: {req.thumb} does not exist", field="thumb")
    upload = getattr(ctx, "upload_file", None)
    return await upload(path) if upload is not None else None


async def _input_media_for(ctx: OpContext, req: UploadReq, path: Path) -> tuple[Any, str]:
    """One local file as an `InputMedia`, with every flag Telethon lacks."""
    from telethon.tl import types

    kind = _kind_for(path, req.send_as_kind)
    if req.quality == "standard":
        raise NotSupportedError(
            "--quality standard re-encodes with ffmpeg/Pillow, which this build does "
            "not do; send the original with --quality high"
        )
    stickers = await _attached_stickers(ctx, req)
    ttl = _ttl_seconds(req)

    if kind == "photo":
        handle = await _uploaded_handle(ctx, req, path)
        video = None
        if req.live_video:
            video = await _live_photo_video(ctx, req)
        return (
            types.InputMediaUploadedPhoto(
                file=handle,
                spoiler=req.spoiler or None,
                ttl_seconds=ttl,
                stickers=stickers or None,
                live_photo=bool(req.live_video) or None,
                video=video,
            ),
            kind,
        )

    attributes, mime = _attributes_for(ctx, req, path, kind)
    handle = await _uploaded_handle(ctx, req, path)
    return (
        types.InputMediaUploadedDocument(
            file=handle,
            mime_type=mime,
            attributes=attributes,
            thumb=await _thumb_handle(ctx, req),
            force_file=kind == "file" and req.send_as_kind == "file",
            nosound_video=req.no_sound or None,
            spoiler=req.spoiler or None,
            stickers=stickers or None,
            video_cover=await _cover_photo(ctx, req),
            video_timestamp=req.start_at,
            ttl_seconds=ttl,
        ),
        kind,
    )


async def _attached_stickers(ctx: OpContext, req: UploadReq) -> list[Any]:
    """`--attached-sticker <set>/<n>` — declared, never composited.

    tlgr cannot draw a sticker onto an image; it can only declare stickers the
    caller already burned in, which is what the flag means in every client.
    """
    if not req.attached_sticker:
        return []
    documents = await _media.resolve_stickers(ctx, list(req.attached_sticker))
    return [_media.input_document(document) for document in documents]


async def _live_photo_video(ctx: OpContext, req: UploadReq) -> Any:
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    path = Path(os.path.expanduser(str(req.live_video)))
    if not path.exists():
        raise UsageError(f"--live-video: {path} does not exist", field="live_video")
    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    handle = await upload(path)
    result = await _client(ctx)(
        fn.UploadMediaRequest(
            peer=types.InputPeerSelf(),
            media=types.InputMediaUploadedDocument(
                file=handle, mime_type="video/mp4", attributes=[]
            ),
        )
    )
    document = _media.document_of(getattr(result, "document", result))
    return _media.input_document(document) if document is not None else None


async def _cover_photo(ctx: OpContext, req: UploadReq) -> Any:
    """`--cover` must be an `InputPhoto`, so the file is uploaded first."""
    if not req.cover:
        return None
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    path = Path(os.path.expanduser(req.cover))
    if not path.exists():
        raise UsageError(f"--cover: {req.cover} does not exist", field="cover")
    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    handle = await upload(path)
    result = await _client(ctx)(
        fn.UploadMediaRequest(
            peer=types.InputPeerSelf(), media=types.InputMediaUploadedPhoto(file=handle)
        )
    )
    photo = _media.photo_of(getattr(result, "photo", result))
    return _media.input_photo(photo) if photo is not None else None


async def _existing_media(ctx: OpContext, req: UploadReq) -> Any:
    """`--file-id` / `--from-message` / `--url` / `--dice`: no bytes move."""
    from telethon.tl import types

    ttl = _ttl_seconds(req)
    if req.dice:
        return types.InputMediaDice(emoticon=req.dice)
    if req.url:
        if req.send_as_kind == "photo":
            return types.InputMediaPhotoExternal(
                url=req.url, spoiler=req.spoiler or None, ttl_seconds=ttl
            )
        return types.InputMediaDocumentExternal(
            url=req.url, spoiler=req.spoiler or None, ttl_seconds=ttl
        )
    if req.file_id:
        from telethon import utils

        try:
            resolved = utils.resolve_bot_file_id(req.file_id)
        except Exception as exc:
            raise UsageError(f"--file-id: {exc}", field="file_id") from exc
        if resolved is None:
            raise UsageError("--file-id: that is not a Telegram file id", field="file_id")
        if type(resolved).__name__ == "Photo":
            return types.InputMediaPhoto(
                id=_media.input_photo(resolved), spoiler=req.spoiler or None
            )
        return types.InputMediaDocument(
            id=_media.input_document(resolved), spoiler=req.spoiler or None, ttl_seconds=ttl
        )
    if req.from_message:
        reference, _, message_id = str(req.from_message).rpartition(":")
        if not reference or not message_id.lstrip("-").isdigit():
            raise UsageError("--from-message takes CHAT:ID", field="from_message")
        from tlgr.models.peer import parse_peer_ref

        source = await _send.resolve(ctx, parse_peer_ref(reference))
        message = await _media.fetch_message(ctx, source, int(message_id))
        media = getattr(message, "media", None)
        document = _media.document_of(media)
        if document is not None:
            return types.InputMediaDocument(
                id=_media.input_document(document), spoiler=req.spoiler or None, ttl_seconds=ttl
            )
        photo = _media.photo_of(media)
        if photo is None:
            raise NotFoundError(f"message {message_id} carries no re-sendable media")
        return types.InputMediaPhoto(id=_media.input_photo(photo), spoiler=req.spoiler or None)
    return None


async def _dedupe_media(ctx: OpContext, path: Path) -> Any:
    """`messages.getDocumentByHash` — skip the upload when the server has it."""
    import mimetypes

    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    digest = hashlib.sha256(path.read_bytes()).digest()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        document = await _client(ctx)(
            fn.GetDocumentByHashRequest(sha256=digest, size=path.stat().st_size, mime_type=mime)
        )
    except Exception:
        return None
    if document is None or type(document).__name__ == "DocumentEmpty":
        return None
    return types.InputMediaDocument(id=_media.input_document(document))


def _caption_for(req: UploadReq, index: int) -> tuple[str, list[Any]]:
    """The caption for item *index*, parsed once."""
    raw = ""
    if req.caption_file:
        raw = Path(os.path.expanduser(req.caption_file)).read_text(encoding="utf-8")
    elif req.caption:
        raw = (
            req.caption[index] if index < len(req.caption) else req.caption[0] if index == 0 else ""
        )
    text, entities = _send.body(raw, parse=req.parse, entities=req.entities)
    return text, _send.tl_entities(entities) or []


async def _check_limits(ctx: OpContext, req: UploadReq, sources: list[Path]) -> None:
    """Refuse before the bandwidth is spent, not after."""
    values = await _media.app_config(ctx)
    premium = _is_premium(ctx)
    key = "upload_max_fileparts_premium" if premium else "upload_max_fileparts_default"
    max_parts = _media.config_int(values, key, 4000)
    part_size = req.part_size * 1024
    for path in sources:
        parts = max(1, -(-path.stat().st_size // part_size))
        if parts > max_parts:
            raise UsageError(
                f"{path.name} needs {parts} parts and this account may upload {max_parts}; "
                "the file is too large",
                field="path",
            )
    caption_limit = _media.config_int(
        values, "caption_length_limit_premium" if premium else "caption_length_limit_default", 1024
    )
    for index in range(max(1, len(sources))):
        text, _ = _caption_for(req, index)
        if caption_limit and len(text) > caption_limit:
            raise UsageError(
                f"the caption is {len(text)} characters and this account may send "
                f"{caption_limit}; send the text as a message or as a .txt file",
                field="caption",
            )


async def upload(ctx: OpContext, req: UploadReq) -> Uploaded:
    """Send files as photo, video, document, audio, voice, round video or GIF."""
    started = time.monotonic()
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    if req.background:
        return await _background_upload(ctx, req, chat_id)

    existing = await _existing_media(ctx, req)
    sources: list[Path] = [] if existing is not None else _upload_sources(req)
    if existing is None and not sources:
        raise UsageError(
            "give at least one file, or --url/--file-id/--from-message/--dice", field="path"
        )
    if sources:
        await _check_limits(ctx, req, sources)

    total_bytes = sum(path.stat().st_size for path in sources)
    action = None if req.no_action else _action_for(req, sources)
    if existing is not None:
        return await _send_one(ctx, req, peer, chat_id, existing, "existing", started, 0)

    if len(sources) == 1:
        media = None
        if req.dedupe:
            media = await _dedupe_media(ctx, sources[0])
            if media is not None:
                ctx.warn("the server already had these bytes; nothing was uploaded")
        kind = _kind_for(sources[0], req.send_as_kind)
        if media is None:
            async with _typing(ctx, peer, action):
                media, kind = await _input_media_for(ctx, req, sources[0])
        if req.no_send:
            return await _upload_only(ctx, media, kind, total_bytes, started)
        return await _send_one(ctx, req, peer, chat_id, media, kind, started, total_bytes)

    return await _send_album(ctx, req, peer, chat_id, sources, started, total_bytes)


def _action_for(req: UploadReq, sources: list[Path]) -> str:
    kind = _kind_for(sources[0], req.send_as_kind) if sources else "file"
    return {
        "photo": "photo",
        "video": "video",
        "gif": "video",
        "audio": "audio",
        "voice": "audio",
        "round": "round",
    }.get(kind, "document")


@contextlib.asynccontextmanager
async def _typing(ctx: OpContext, peer: Any, action: str | None) -> Any:
    """Broadcast `sending a photo…` for the duration of the upload."""
    if action is None:
        yield
        return
    client = _client(ctx)
    try:
        async with client.action(peer, action):
            yield
    except Exception:
        yield


async def _upload_only(
    ctx: OpContext, media: Any, kind: str, total_bytes: int, started: float
) -> Uploaded:
    """`--no-send`: stop after `messages.uploadMedia` and print the file id."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.UploadMediaRequest(peer=types.InputPeerSelf(), media=media))
    document = _media.document_of(result) or _media.photo_of(result)
    return Uploaded(
        kind=kind,
        doc_id=int(getattr(document, "id", 0) or 0) or None,
        file_id=_media.file_id_of(result),
        bytes=total_bytes,
        elapsed_s=_elapsed(started),
    )


async def _reply_to(ctx: OpContext, req: UploadReq) -> Any:
    return await _send.reply_target(ctx, reply_to=req.reply_to, quote=req.quote, topic=req.topic)


async def _send_one(
    ctx: OpContext,
    req: UploadReq,
    peer: Any,
    chat_id: int,
    media: Any,
    kind: str,
    started: float,
    total_bytes: int,
) -> Uploaded:
    from telethon.tl.functions import messages as fn

    if req.paid_stars is not None:
        from telethon.tl import types

        media = types.InputMediaPaidMedia(stars_amount=req.paid_stars, extended_media=[media])
    text, entities = _caption_for(req, 0)
    result = await _client(ctx)(
        fn.SendMediaRequest(
            peer=peer,
            media=media,
            message=text,
            entities=entities or None,
            reply_to=await _reply_to(ctx, req),
            silent=req.silent or None,
            noforwards=req.protect or None,
            invert_media=req.caption_above or None,
            clear_draft=req.clear_draft or None,
            schedule_date=_send.schedule_at(req.schedule),
            send_as=await _send.resolve(ctx, req.send_as) if req.send_as else None,
            effect=_send.effect_id(req.effect),
            random_id=_random_id(),
        )
    )
    message = _send.message_from_updates(result, chat_id=chat_id, sent_text=text)
    processing = _is_processing(result)
    if req.wait and processing:
        ctx.warn("the server is still converting this video; the id may change")
    ctx.emit("media.sent", {"chat_id": chat_id, "msg_id": message.id, "kind": kind})
    return Uploaded(
        chat_id=chat_id,
        msg_id=message.id,
        msg_ids=[message.id],
        kind=kind,
        file_id=_media.file_id_of(getattr(message, "media", None)),
        bytes=total_bytes,
        elapsed_s=_elapsed(started),
        processing=processing,
        scheduled_id=message.id if req.schedule else None,
    )


def _is_processing(updates: Any) -> bool:
    for update in getattr(updates, "updates", None) or []:
        if type(update).__name__ == "UpdateMessageExtendedMedia":
            return True
    return False


async def _send_album(
    ctx: OpContext,
    req: UploadReq,
    peer: Any,
    chat_id: int,
    sources: list[Path],
    started: float,
    total_bytes: int,
) -> Uploaded:
    """2..10 items as one media group.

    Every item goes through `messages.uploadMedia` first: `sendMultiMedia`
    takes already-uploaded media, and doing it in two steps is also what makes
    `FILE_REFERENCE_%d_EXPIRED` name the failing index.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if len(sources) > 10:
        raise UsageError(
            f"an album holds at most 10 items and {len(sources)} were given", field="path"
        )
    if not req.album:
        raise UsageError(
            "several files were given but --no-album was set; send them one at a time",
            field="album",
        )

    singles: list[Any] = []
    for index, path in enumerate(sources):
        media, _ = await _input_media_for(ctx, req, path)
        uploaded = await _client(ctx)(fn.UploadMediaRequest(peer=peer, media=media))
        document = _media.document_of(uploaded)
        photo = _media.photo_of(uploaded) if document is None else None
        ready: Any = (
            types.InputMediaDocument(id=_media.input_document(document))
            if document is not None
            else types.InputMediaPhoto(id=_media.input_photo(photo))
        )
        text, entities = _caption_for(req, index)
        singles.append(
            types.InputSingleMedia(
                media=ready, message=text, entities=entities or None, random_id=_random_id()
            )
        )

    result = await _client(ctx)(
        fn.SendMultiMediaRequest(
            peer=peer,
            multi_media=singles,
            reply_to=await _reply_to(ctx, req),
            silent=req.silent or None,
            noforwards=req.protect or None,
            invert_media=req.caption_above or None,
            clear_draft=req.clear_draft or None,
            schedule_date=_send.schedule_at(req.schedule),
            send_as=await _send.resolve(ctx, req.send_as) if req.send_as else None,
            effect=_send.effect_id(req.effect),
        )
    )
    messages = _send.messages_from_updates(result, chat_id=chat_id)
    ids = [message.id for message in messages]
    ctx.emit("media.sent", {"chat_id": chat_id, "msg_ids": ids, "kind": "album"})
    return Uploaded(
        chat_id=chat_id,
        msg_id=ids[0] if ids else 0,
        msg_ids=ids,
        grouped_id=next((m.grouped_id for m in messages if m.grouped_id), None),
        kind="album",
        bytes=total_bytes,
        elapsed_s=_elapsed(started),
    )


def _random_id() -> int:
    return int.from_bytes(os.urandom(8), "big", signed=True)


async def _background_upload(ctx: OpContext, req: UploadReq, chat_id: int) -> Uploaded:
    store = _transfers(ctx)
    foreground = req.__class__(**{**_as_dict(req), "background": False})

    async def run() -> Any:
        return await upload(ctx, foreground)

    record = store.submit(
        direction="upload",
        name=", ".join(req.path) or "upload",
        chat_id=chat_id,
        factory=run,
    )
    return Uploaded(chat_id=chat_id, kind="queued", job_id=record.job_id)


SPEC_UPLOAD = OperationSpec(
    id="media.upload",
    request=UploadReq,
    response=Uploaded,
    impl=upload,
    summary="Send files as photo, video, document, audio, voice, round video or GIF",
    description=(
        "The media half of the composer `message send --file` shares. The kind "
        "is sniffed from the bytes unless --as says otherwise; attributes are "
        "probed rather than guessed, because a video sent with duration 0 and "
        "1x1 dimensions renders as a black rectangle everywhere. Two to ten "
        "paths become an album, uploaded item by item and sent as one group. "
        "--no-send stops after messages.uploadMedia and prints a file id the "
        "same bytes can be re-sent with."
    ),
    aliases=("up", "media.send"),
    legacy_paths=("media upload", "up"),
    mutating=True,
    rate_class="file",
    timeout_s=900,
    columns=("chat_id", "msg_id", "kind", "bytes"),
    headers=("Chat", "ID", "Kind", "Bytes"),
    example={
        "chat_id": 777123,
        "msg_id": 12346,
        "msg_ids": [12346],
        "kind": "photo",
        "bytes": 184320,
    },
    example_args="media upload @alice photo.jpg --caption 'the cat'",
    covers=(
        "emoji.custom-send",
        "media.big-file-upload",
        "media.caption-formatting",
        "media.resend-existing-media",
        "media.secret-chat-media",
        "media.send-album",
        "media.send-audio",
        "media.send-by-url",
        "media.send-dice",
        "media.send-document",
        "media.send-gif",
        "media.send-live-photo",
        "media.send-options-with-media",
        "media.send-paid-media",
        "media.send-permission-check",
        "media.send-photo",
        "media.send-photo-uncompressed",
        "media.send-self-destruct",
        "media.send-spoiler",
        "media.send-sticker",
        "media.send-text-as-file",
        "media.send-thumbnail",
        "media.send-video",
        "media.send-video-note",
        "media.send-video-quality",
        "media.send-voice-note",
        "media.streamed-upload",
        "media.upload-action-broadcast",
        "media.upload-by-hash",
        "media.upload-only",
        "media.video-processing-pending",
    ),
    covers_partial=(
        "media.attached-stickers",
        "media.background-transfer-jobs",
        "media.caption-above-media",
        "media.file-id-export-import",
        "media.file-reference-refresh",
        "media.limits-config",
        "media.parallel-transfer",
        "media.saved-messages-drive",
        "media.send-video-cover-and-timestamp",
        "media.transfer-progress",
    ),
    coverage_note=(
        "Secret-chat peers are refused rather than pretended at: Telethon implements "
        "no MTProto 2.0 E2E layer. `-` spools stdin to a temp file rather than "
        "streaming with an unknown part count."
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# media edit
# ---------------------------------------------------------------------------


class EditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message to edit.")]
    file: Annotated[
        str | None, opt("--file", metavar="PATH", kind="path", help="Replace with a new upload.")
    ] = None
    file_id: Annotated[
        str | None, opt("--file-id", metavar="ID", help="Replace with media already on Telegram.")
    ] = None
    url: Annotated[
        str | None, opt("--url", metavar="URL", help="Replace with server-fetched media.")
    ] = None
    send_as_kind: Annotated[
        str,
        choice("auto", "photo", "file", "video", "gif", "audio", help="Kind for the replacement."),
    ] = "auto"
    caption: Annotated[
        str | None, opt("--caption", metavar="TEXT", help="New caption; `-` reads stdin.")
    ] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Caption parse mode.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    caption_above: Annotated[
        bool | None,
        opt("--caption-above/--caption-below", help="Move the caption above or below the media."),
    ] = None
    spoiler: Annotated[
        bool | None, opt("--spoiler/--no-spoiler", help="Toggle the blur without re-uploading.")
    ] = None
    ttl: Annotated[
        int | None, opt("--ttl", metavar="SECONDS", kind="duration", help="Change the timer.")
    ] = None
    cover: Annotated[str | None, opt("--cover", metavar="PATH", help="Change the video cover.")] = (
        None
    )
    start_at: Annotated[
        int | None,
        opt("--start-at", metavar="SECONDS", kind="duration", help="Video start offset."),
    ] = None
    paid_stars: Annotated[
        int | None, opt("--paid-stars", metavar="N", help="Re-price an already-posted paid post.")
    ] = None
    thumb: Annotated[
        str | None, opt("--thumb", metavar="PATH", kind="path", help="New thumbnail.")
    ] = None


async def edit(ctx: OpContext, req: EditReq) -> MediaEdited:
    """Replace the media, the caption or a media flag of a sent message.

    Three very different costs hide behind one command. A caption-only or
    flag-only edit passes the *existing* media back with the one field
    changed, so no bytes move; `--file` re-uploads; `--file-id`/`--url` do
    not. Naming that here is what stops a spoiler toggle from costing a
    2 GB round trip.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    message = await _media.fetch_message(ctx, peer, req.msg_id)
    changed: list[str] = []
    media: Any = None

    if req.file or req.file_id or req.url:
        upload_req = UploadReq(
            chat=req.chat,
            path=[req.file] if req.file else [],
            file_id=req.file_id,
            url=req.url,
            send_as_kind=req.send_as_kind,
            thumb=req.thumb,
            cover=req.cover,
            start_at=req.start_at,
            spoiler=bool(req.spoiler),
            ttl=req.ttl,
        )
        media = await _existing_media(ctx, upload_req)
        if media is None:
            media, _ = await _input_media_for(
                ctx, upload_req, Path(os.path.expanduser(str(req.file)))
            )
        changed.append("media")
    elif any(
        value is not None
        for value in (req.spoiler, req.ttl, req.cover, req.start_at, req.paid_stars)
    ):
        media = _existing_input_media(message, req)
        changed.append("flags")

    if req.paid_stars is not None:
        media = types.InputMediaPaidMedia(
            stars_amount=req.paid_stars, extended_media=[media] if media is not None else []
        )
        changed.append("price")

    text: str | None = None
    entities: list[Any] | None = None
    if req.caption is not None:
        parsed, models = _send.body(req.caption, parse=req.parse, entities=req.entities)
        text, entities = parsed, _send.tl_entities(models)
        changed.append("caption")
    if req.caption_above is not None:
        changed.append("caption_above")
    if not changed:
        raise UsageError("nothing to change; give a caption, a file or a flag", field="caption")

    result = await _client(ctx)(
        fn.EditMessageRequest(
            peer=peer,
            id=req.msg_id,
            media=media,
            message=text,
            entities=entities,
            invert_media=req.caption_above,
        )
    )
    edited = _send.message_from_updates(result, chat_id=chat_id, sent_text=text or "")
    return MediaEdited(
        chat_id=chat_id,
        msg_id=req.msg_id,
        kind=(edited.media.kind if edited.media is not None else ""),
        file_id=_media.file_id_of(getattr(message, "media", None)),
        edit_date=edited.edit_date,
        caption=edited.text,
        changed=changed,
    )


def _existing_input_media(message: Any, req: EditReq) -> Any:
    """The media that is already there, with one field changed.

    This is the whole trick behind "toggle a spoiler without re-uploading":
    `editMessage` accepts the existing `InputPhoto`/`InputDocument` back, and
    the changed flag rides along with it.
    """
    from telethon.tl import types

    media = getattr(message, "media", None)
    document = _media.document_of(media)
    if document is not None:
        return types.InputMediaDocument(
            id=_media.input_document(document),
            spoiler=req.spoiler,
            ttl_seconds=req.ttl,
            video_timestamp=req.start_at,
        )
    photo = _media.photo_of(media)
    if photo is None:
        raise NotFoundError(f"message {req.msg_id} carries no media to change")
    return types.InputMediaPhoto(
        id=_media.input_photo(photo), spoiler=req.spoiler, ttl_seconds=req.ttl
    )


SPEC_EDIT = OperationSpec(
    id="media.edit",
    request=EditReq,
    response=MediaEdited,
    impl=edit,
    summary="Replace the media, caption or media flags of a sent message",
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "changed"),
    headers=("Chat", "ID", "Changed"),
    example={
        "chat_id": 777123,
        "msg_id": 12345,
        "kind": "photo",
        "caption": "the cat, again",
        "changed": ["caption"],
    },
    example_args="media edit @alice 12345 --caption 'the cat, again'",
    covers=(
        "media.caption-above-media",
        "media.edit-caption",
        "media.edit-message-media",
        "media.edit-spoiler-ttl-without-reupload",
        "media.paid-media-edit-price",
        "media.send-video-cover-and-timestamp",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# media export
# ---------------------------------------------------------------------------


class ExportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to archive.")]
    out_dir: Annotated[
        str | None, opt("--out-dir", metavar="DIR", kind="path", help="Destination root.")
    ] = None
    type: Annotated[
        list[str], opt("--type", metavar="TAB", help="Which shared-media tabs to walk.")
    ] = []
    from_user: Annotated[
        PeerRef | None,
        opt("--from", metavar="USER", kind="user", help="Only media from this sender."),
    ] = None
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Start of the window.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="End of the window.")
    ] = None
    name_template: Annotated[
        str, opt("--name-template", metavar="PATTERN", help="Output naming pattern.")
    ] = DEFAULT_TEMPLATE
    manifest: Annotated[
        str | None, opt("--manifest", metavar="PATH", kind="path", help="JSONL manifest path.")
    ] = None
    skip_existing: Annotated[
        bool, opt("--skip-existing", help="Resume: skip what the ledger already has.")
    ] = True
    dedupe: Annotated[
        bool, opt("--dedupe", help="Write each document once and link duplicates.")
    ] = True
    max_size: Annotated[
        str | None, opt("--max-size", metavar="SIZE", help="Skip items larger than this.")
    ] = None
    max_items: Annotated[int | None, opt("--max", metavar="N", help="Stop after N items.")] = None
    connections: Annotated[
        int, opt("--connections", metavar="N", help="Parallel readers per file.", ge=1, le=8)
    ] = 1
    jobs: Annotated[int, opt("--jobs", metavar="N", help="Concurrent files.", ge=1, le=8)] = 2
    background: Annotated[
        bool, opt("--background", help="Run in the daemon and return a job id.")
    ] = True


async def export(ctx: OpContext, req: ExportReq) -> ExportResult:
    """Archive a chat's media with a resumable ledger.

    Always a *plan* first: a big channel is tens of thousands of
    `upload.getFile` calls, and `--dry-run` answering with the byte total is
    the difference between an informed export and a surprised one.
    """
    import json

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    root = (
        Path(os.path.expanduser(req.out_dir))
        if req.out_dir
        else _downloads_root(ctx).parent / "export" / str(chat_id)
    )
    tabs = list(req.type or ["all"])
    ceiling = _size_arg(req.max_size, "max_size")

    planned: list[tuple[str, Any]] = []
    for tab in tabs:
        kwargs: dict[str, Any] = {"filter": _media.media_filter(tab), "limit": req.max_items or 200}
        if req.from_user is not None:
            kwargs["from_user"] = await _send.resolve(ctx, req.from_user)
        if req.until:
            kwargs["offset_date"] = parse_dt(req.until)
        floor = to_unix(parse_dt(req.since)) if req.since else None
        async for message in _client(ctx).iter_messages(peer, **kwargs):
            if message is None or getattr(message, "media", None) is None:
                continue
            if floor is not None and (to_unix(getattr(message, "date", None)) or 0) < floor:
                continue
            planned.append((tab, message))

    result = ExportResult(chat_id=chat_id, planned=len(planned), manifest=None)
    if getattr(ctx, "dry_run", False):
        result.bytes = sum(
            int(getattr(_media.document_of(m.media), "size", 0) or 0) for _, m in planned
        )
        return result

    ledger_path = root / ".tlgr-export.json"
    ledger: dict[str, str] = {}
    if ledger_path.exists():
        with contextlib.suppress(ValueError, OSError):
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    manifest_path = (
        Path(os.path.expanduser(req.manifest)) if req.manifest else root / "manifest.jsonl"
    )
    root.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, "a", encoding="utf-8") as manifest:
        for tab, message in planned:
            document = _media.document_of(getattr(message, "media", None))
            size = int(getattr(document, "size", 0) or 0)
            key = str(getattr(document, "id", message.id))
            if ceiling is not None and size > ceiling:
                result.skipped += 1
                continue
            if req.skip_existing and key in ledger and Path(ledger[key]).exists():
                result.skipped += 1
                continue
            date, _ = _media.message_dates(message)
            name = _safe_name(
                _media.attributes_of(document).get("file_name") or _default_name(message.media)
            )
            target = (
                root
                / tab
                / _fill_template(
                    req.name_template, date=date, message_id=int(message.id), name=name
                )
            )
            try:
                path = await _download_bytes(
                    ctx,
                    document or _media.photo_of(message.media),
                    target,
                    size=size,
                    dc_id=int(getattr(document, "dc_id", 0) or 0),
                    connections=req.connections,
                )
            except Exception as exc:  # one bad item must not end the archive
                result.failed += 1
                ctx.warn(f"message {message.id}: {exc}")
                continue
            ledger[key] = str(path)
            result.downloaded += 1
            result.bytes += path.stat().st_size if path.exists() else 0
            manifest.write(
                json.dumps(
                    {"msg_id": int(message.id), "type": tab, "path": str(path), "bytes": size}
                )
                + "\n"
            )
    ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    result.manifest = str(manifest_path)
    return result


SPEC_EXPORT = OperationSpec(
    id="media.export",
    request=ExportReq,
    response=ExportResult,
    impl=export,
    summary="Archive a chat's media to disk with a resumable ledger",
    description=(
        "The ledger under the output root maps document id to path, so an "
        "interrupted export resumes instead of starting again. --dry-run "
        "answers with the plan and its byte total before anything is fetched."
    ),
    rate_class="file",
    timeout_s=900,
    columns=("chat_id", "planned", "downloaded", "skipped", "bytes"),
    headers=("Chat", "Planned", "Got", "Skipped", "Bytes"),
    example={
        "chat_id": 777123,
        "planned": 412,
        "downloaded": 412,
        "skipped": 0,
        "failed": 0,
        "bytes": 918273645,
        "manifest": "/home/u/.tlgr/export/777123/manifest.jsonl",
    },
    example_args="media export @alice --type photo",
    covers=("media.download-batch", "media.shared-media-bulk-actions"),
)


# ---------------------------------------------------------------------------
# media file-id get
# ---------------------------------------------------------------------------


class FileIdReq(Request):
    file_id: Annotated[str, arg(0, metavar="FILE_ID", help="The portable file id.")]
    source: Annotated[
        str | None,
        opt(
            "--source",
            metavar="REF",
            help="Where to re-harvest the reference: `chat:msg-id`, `set:<short-name>` or `gif`.",
        ),
    ] = None
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Also download the media.")
    ] = None


async def file_id_get(ctx: OpContext, req: FileIdReq) -> FileRef:
    """Resolve a portable file id, refreshing its expired file reference.

    A Bot-API-shaped id carries **no** reference at all, and even a packed one
    goes stale in hours. The fix is never a retry: it is to re-fetch the
    object from the source it was seen in and swap the reference, which is
    what --source names.
    """
    from telethon import utils

    try:
        resolved = utils.resolve_bot_file_id(req.file_id)
    except Exception as exc:
        raise UsageError(
            f"{req.file_id!r} is not a Telegram file id: {exc}", field="file_id"
        ) from exc
    if resolved is None:
        raise UsageError(f"{req.file_id!r} is not a Telegram file id", field="file_id")

    refreshed = False
    if req.source:
        resolved = await _reharvest(ctx, req.source) or resolved
        refreshed = True

    reference = getattr(resolved, "file_reference", b"") or b""
    ref = FileRef(
        file_id=req.file_id,
        doc_id=int(getattr(resolved, "id", 0) or 0),
        access_hash=getattr(resolved, "access_hash", None),
        file_reference_b64=_media.b64(reference),
        dc_id=getattr(resolved, "dc_id", None),
        kind=_media.kind_of(resolved) if getattr(resolved, "attributes", None) else "photo",
        mime=getattr(resolved, "mime_type", None),
        size=getattr(resolved, "size", None),
        source=req.source,
        refreshed=refreshed,
    )
    if not reference and not refreshed:
        ctx.warn(
            "this file id carries no file_reference (Bot-API ids never do); pass --source "
            "so tlgr can fetch a live one before using it"
        )
    if req.out:
        target = Path(os.path.expanduser(req.out))
        path = await _download_bytes(
            ctx,
            resolved,
            target,
            size=int(getattr(resolved, "size", 0) or 0),
            dc_id=int(getattr(resolved, "dc_id", 0) or 0),
        )
        ref.path = str(path)
    return ref


async def _reharvest(ctx: OpContext, source: str) -> Any:
    """Fetch the object the id came from, so its reference is live again."""
    head, _, tail = source.partition(":")
    if head == "set" and tail:
        result = await _media.fetch_set(ctx, tail, field="source")
        documents = list(getattr(result, "documents", None) or [])
        return documents[0] if documents else None
    if head == "gif":
        from telethon.tl.functions import messages as fn

        result = await _client(ctx)(fn.GetSavedGifsRequest(hash=0))
        gifs = list(getattr(result, "gifs", None) or [])
        return gifs[0] if gifs else None
    if tail.lstrip("-").isdigit():
        from tlgr.models.peer import parse_peer_ref

        peer = await _send.resolve(ctx, parse_peer_ref(head))
        message = await _media.fetch_message(ctx, peer, int(tail))
        media = getattr(message, "media", None)
        return _media.document_of(media) or _media.photo_of(media)
    raise UsageError("--source takes `chat:msg-id`, `set:<short-name>` or `gif`", field="source")


SPEC_FILE_ID_GET = OperationSpec(
    id="media.file-id.get",
    request=FileIdReq,
    response=FileRef,
    impl=file_id_get,
    summary="Resolve a portable file id, refreshing its expired file reference",
    rate_class="resolve",
    columns=("file_id", "doc_id", "kind", "refreshed"),
    headers=("File id", "Doc", "Kind", "Refreshed"),
    example={
        "file_id": "CAACAgIAAxkBAAEB",
        "doc_id": 5312836234,
        "dc_id": 2,
        "kind": "sticker",
        "refreshed": True,
        "source": "@alice:12345",
    },
    example_args="media file-id get CAACAgIAAxkBAAEB --source @alice:12345",
    covers=("media.file-id-export-import", "media.file-reference-refresh"),
)


# ---------------------------------------------------------------------------
# media read
# ---------------------------------------------------------------------------


class ReadReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        list[int], arg(1, metavar="MSG_ID", variadic=True, kind="msg_id", help="Message ids.")
    ] = []
    listened: Annotated[
        int | None,
        opt(
            "--listened", metavar="SECONDS", kind="duration", help="Report an audio play duration."
        ),
    ] = None


async def read(ctx: OpContext, req: ReadReq) -> MediaRead:
    """Tell the server the media was consumed.

    This is what clears `media_unread`, so the sender's blue dot goes and a
    self-destruct timer starts. It is irreversible for view-once media, which
    is exactly why `media download` keeps it behind `--read` instead of doing
    it implicitly.
    """
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    ids = [int(value) for value in req.msg_id]
    if not ids:
        raise UsageError("give at least one message id", field="msg_id")
    await _mark_read(ctx, peer, ids)

    if req.listened is not None:
        from telethon.tl.functions import messages as fn

        message = await _media.fetch_message(ctx, peer, ids[0])
        document = _media.document_of(getattr(message, "media", None))
        if document is None:
            raise NotFoundError(f"message {ids[0]} carries no audio document")
        await _client(ctx)(
            fn.ReportMusicListenRequest(
                id=_media.input_document(document), listened_duration=int(req.listened)
            )
        )
    return MediaRead(chat_id=chat_id, msg_ids=ids, marked=len(ids))


SPEC_READ = OperationSpec(
    id="media.read",
    request=ReadReq,
    response=MediaRead,
    impl=read,
    summary="Mark media consumed: voice played, view-once opened, track listened",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("chat_id", "msg_ids", "marked"),
    headers=("Chat", "IDs", "Marked"),
    example={"chat_id": 777123, "msg_ids": [12345], "marked": 1},
    example_args="media read @alice 12345",
    covers=(
        "media.mark-voice-listened",
        "media.report-music-listen",
        "media.view-self-destructing",
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# media paid list
# ---------------------------------------------------------------------------


class PaidListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel to read.")]
    msg_id: Annotated[
        list[int], opt("--msg-id", metavar="ID", kind="msg_id", help="Refresh these posts' state.")
    ] = []
    unlocked: Annotated[bool, opt("--unlocked", help="Only posts already unlocked.")] = False
    locked: Annotated[bool, opt("--locked", help="Only posts still behind the paywall.")] = False
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Only at/after this time.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Only before this time.")
    ] = None


def _paid_post(message: Any, chat_id: int) -> PaidPost | None:
    media = getattr(message, "media", None)
    if type(media).__name__ != "MessageMediaPaidMedia":
        return None
    items: list[PaidItem] = []
    unlocked = False
    for entry in getattr(media, "extended_media", None) or []:
        name = type(entry).__name__
        if name == "MessageExtendedMediaPreview":
            items.append(
                PaidItem(
                    kind="preview",
                    width=getattr(entry, "w", None),
                    height=getattr(entry, "h", None),
                    duration=getattr(entry, "video_duration", None),
                )
            )
            continue
        unlocked = True
        inner = getattr(entry, "media", None)
        document = _media.document_of(inner) or _media.photo_of(inner)
        items.append(PaidItem(kind="media", doc_id=getattr(document, "id", None), unlocked=True))
    date, date_unix = _media.message_dates(message)
    return PaidPost(
        chat_id=chat_id,
        msg_id=int(getattr(message, "id", 0) or 0),
        stars_amount=int(getattr(media, "stars_amount", 0) or 0),
        item_count=len(items),
        unlocked=unlocked,
        items=items,
        date=date,
        date_unix=date_unix,
        caption=getattr(message, "message", "") or "",
    )


async def paid_list(ctx: OpContext, req: PaidListReq) -> Page[PaidPost]:
    """Paid-media posts and their unlock state.

    Read-only by design: buying is a payment, and tlgr does not spend Stars on
    the user's behalf. `updateMessageExtendedMedia` carries no `pts`, so a
    client that was offline learns about a purchase made elsewhere only by
    asking — which is what `--msg-id` does.
    """
    limit, state = _media.window(ctx, "media.paid.list", PageKind.SEARCH)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.msg_id:
        from telethon.tl.functions import messages as fn

        result = await _client(ctx)(
            fn.GetExtendedMediaRequest(peer=peer, id=[int(i) for i in req.msg_id])
        )
        found = _send.messages_from_updates(result, chat_id=chat_id)
        refreshed = await _client(ctx).get_messages(peer, ids=[int(i) for i in req.msg_id])
        one_by_one: list[PaidPost] = [
            post
            for message in (refreshed or [])
            if message is not None and (post := _paid_post(message, chat_id)) is not None
        ]
        _ = found
        return Page(items=one_by_one, has_more=False, total=len(one_by_one))

    posts: list[PaidPost] = []
    floor = to_unix(parse_dt(req.since)) if req.since else None
    kwargs: dict[str, Any] = {"limit": limit, "offset_id": int(state.get("offset_id", 0))}
    if req.until:
        kwargs["offset_date"] = parse_dt(req.until)
    async for message in _client(ctx).iter_messages(peer, **kwargs):
        if message is None:
            continue
        post = _paid_post(message, chat_id)
        if post is None:
            continue
        if floor is not None and post.date_unix < floor:
            continue
        if req.unlocked and not post.unlocked:
            continue
        if req.locked and post.unlocked:
            continue
        posts.append(post)

    next_state = {"offset_id": posts[-1].msg_id} if posts else {}
    return build_page(
        posts,
        op="media.paid.list",
        kind=PageKind.SEARCH,
        state=next_state,
        account=ctx.account,
        limit=limit,
        has_more=None if posts else False,
    )


SPEC_PAID_LIST = OperationSpec(
    id="media.paid.list",
    request=PaidListReq,
    response=Page[PaidPost],
    impl=paid_list,
    summary="Paid-media posts and their unlock state",
    description=(
        "Buying is deliberately absent: spending Stars is a payment tlgr will "
        "not execute for you. Posting is `media upload --paid-stars N` and "
        "re-pricing is `media edit --paid-stars N`."
    ),
    paginated=PageKind.SEARCH,
    columns=("msg_id", "stars_amount", "item_count", "unlocked"),
    headers=("ID", "Stars", "Items", "Unlocked"),
    example={
        "items": [
            {
                "chat_id": -1000000123,
                "msg_id": 890,
                "stars_amount": 50,
                "item_count": 3,
                "unlocked": False,
            }
        ],
        "has_more": False,
    },
    example_args="media paid list @channel",
    covers=("media.paid-media-inspect",),
)


# ---------------------------------------------------------------------------
# media limit get
# ---------------------------------------------------------------------------


class LimitReq(Request):
    refresh: Annotated[bool, opt("--refresh", help="Bypass the cached help.getAppConfig hash.")] = (
        False
    )


async def limit_get(ctx: OpContext, req: LimitReq) -> MediaLimits:
    """The server's own media limits. Never hardcode one of these.

    `media upload` reads the same numbers before the first part goes out, so
    an oversized file is a named refusal rather than `FILE_PARTS_INVALID`
    after the bandwidth has been spent.
    """
    values = await _media.app_config(ctx)
    premium = _is_premium(ctx)
    suffix = "premium" if premium else "default"

    def number(name: str, fallback: int = 0) -> int:
        return _media.config_int(
            values, f"{name}_{suffix}", _media.config_int(values, name, fallback)
        )

    part_size = 512 * 1024
    parts = number("upload_max_fileparts", 4000)
    return MediaLimits(
        premium=premium,
        upload_max_fileparts=parts,
        upload_max_bytes=parts * part_size,
        part_size=part_size,
        caption_length_limit=number("caption_length_limit", 1024),
        message_length_limit=number("message_length_limit", 4096),
        album_size_max=_media.config_int(values, "album_size_max", 10),
        stickers_installed_limit=number("stickers_installed_limit", 200),
        stickers_faved_limit=number("stickers_faved_limit", 5),
        stickers_recent_limit=_media.config_int(values, "stickers_recent_limit", 20),
        saved_gifs_limit=number("saved_gifs_limit", 200),
        ringtone_size_max=_media.config_int(values, "ringtone_size_max", 307200),
        ringtone_duration_max=_media.config_int(values, "ringtone_duration_max", 5),
        stars_paid_post_amount_max=_media.config_int(values, "stars_paid_post_amount_max", 0),
        premium_speedup_upload=_media.config_int(values, "upload_premium_speedup_upload", 0),
        premium_speedup_download=_media.config_int(values, "upload_premium_speedup_download", 0),
    )


SPEC_LIMIT_GET = OperationSpec(
    id="media.limit.get",
    request=LimitReq,
    response=MediaLimits,
    impl=limit_get,
    summary="Server-side media limits for this account",
    columns=("premium", "upload_max_bytes", "caption_length_limit", "album_size_max"),
    headers=("Premium", "Max upload", "Caption", "Album"),
    example={
        "premium": False,
        "upload_max_fileparts": 4000,
        "upload_max_bytes": 2097152000,
        "part_size": 524288,
        "caption_length_limit": 1024,
        "album_size_max": 10,
    },
    example_args="media limit get",
    covers=("media.limits-config",),
)


# ---------------------------------------------------------------------------
# media transfer
# ---------------------------------------------------------------------------


class TransferListReq(Request):
    active: Annotated[bool, opt("--active", help="Only running transfers.")] = False
    failed: Annotated[bool, opt("--failed", help="Only failed transfers.")] = False
    watch: Annotated[
        bool, opt("--watch", help="Follow progress until every listed transfer settles.")
    ] = False


async def transfer_list(ctx: OpContext, req: TransferListReq) -> Page[Transfer]:
    """The Downloads panel: client-local state, exactly as it is in the GUI.

    There is no server-side downloads list to read; a transfer exists because
    this daemon started it.
    """
    limit, _state = _media.window(ctx, "media.transfer.list", PageKind.LOCAL)
    store = _transfers(ctx)
    if req.watch:
        await store.settle(timeout=30.0)
    items = [
        Transfer(**record)
        for record in store.snapshot(active=req.active, failed=req.failed)[:limit]
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_TRANSFER_LIST = OperationSpec(
    id="media.transfer.list",
    request=TransferListReq,
    response=Page[Transfer],
    impl=transfer_list,
    summary="Active and recent uploads and downloads",
    paginated=PageKind.LOCAL,
    columns=("job_id", "direction", "name", "pct", "state"),
    headers=("Job", "Way", "Name", "%", "State"),
    example={
        "items": [
            {
                "job_id": "9f2c1a",
                "direction": "download",
                "name": "cat.mp4",
                "bytes_done": 1048576,
                "bytes_total": 8412300,
                "pct": 12.5,
                "state": "running",
            }
        ],
        "has_more": False,
    },
    example_args="media transfer list --active",
    covers=("media.downloads-list", "media.transfer-progress"),
    covers_partial=("media.background-transfer-jobs",),
    coverage_note="Retrying and cancelling are `media transfer retry` and `media transfer stop`.",
)


class TransferStopReq(Request):
    job_id: Annotated[
        list[str],
        arg(0, metavar="JOB_ID", required=False, variadic=True, help="Transfers to stop."),
    ] = []
    every: Annotated[bool, opt("--all", help="Cancel every running transfer.")] = False
    keep_partial: Annotated[
        bool, opt("--keep-partial/--discard-partial", help="Keep the .part file for --resume.")
    ] = True


async def transfer_stop(ctx: OpContext, req: TransferStopReq) -> TransferStopped:
    """Stop an in-flight transfer.

    A download stops cleanly between chunks and leaves its `.part` file, so
    `--resume` continues it. An upload cannot be resumed at all — saved parts
    expire server-side — so a cancelled upload is restarted with a fresh file
    id rather than continued.
    """
    store = _transfers(ctx)
    ids = list(req.job_id)
    if req.every:
        ids = [record["job_id"] for record in store.snapshot(active=True)]
    if not ids:
        raise UsageError("name a transfer, or pass --all", field="job_id")
    cancelled = await store.cancel(ids, keep_partial=req.keep_partial)
    return TransferStopped(
        job_ids=ids,
        cancelled=cancelled,
        kept_partial=req.keep_partial,
        already=cancelled == 0,
    )


SPEC_TRANSFER_STOP = OperationSpec(
    id="media.transfer.stop",
    request=TransferStopReq,
    response=TransferStopped,
    impl=transfer_stop,
    summary="Stop an in-flight upload or download",
    aliases=("media.transfer.cancel",),
    mutating=True,
    destructive=True,
    columns=("job_ids", "cancelled", "kept_partial"),
    headers=("Jobs", "Cancelled", "Kept"),
    example={"job_ids": ["9f2c1a"], "cancelled": 1, "kept_partial": True},
    example_args="media transfer stop 9f2c1a",
    covers=("media.transfer-cancel",),
    covers_partial=("media.background-transfer-jobs",),
    coverage_note="The job store lives in the daemon; `media transfer list` is its view.",
)


class TransferRetryReq(Request):
    job_id: Annotated[
        list[str],
        arg(0, metavar="JOB_ID", required=False, variadic=True, help="Transfers to restart."),
    ] = []
    every: Annotated[bool, opt("--all", help="Retry every failed transfer.")] = False
    from_scratch: Annotated[
        bool, opt("--from-scratch", help="Ignore the partial file and start at byte 0.")
    ] = False


async def transfer_retry(ctx: OpContext, req: TransferRetryReq) -> TransferRestarted:
    """Restart a failed or cancelled transfer.

    The source is re-fetched first: a transfer that sat in the failed queue
    for an hour is holding an expired `file_reference`, and retrying with the
    stale one fails identically.
    """
    store = _transfers(ctx)
    ids = list(req.job_id)
    if req.every:
        ids = [record["job_id"] for record in store.snapshot(failed=True)]
    if not ids:
        raise UsageError("name a transfer, or pass --all", field="job_id")
    restarted, resumed_from = store.retry(ids, from_scratch=req.from_scratch)
    return TransferRestarted(job_ids=ids, restarted=restarted, resumed_from=resumed_from)


SPEC_TRANSFER_RETRY = OperationSpec(
    id="media.transfer.retry",
    request=TransferRetryReq,
    response=TransferRestarted,
    impl=transfer_retry,
    summary="Restart a failed or cancelled transfer",
    mutating=True,
    rate_class="file",
    columns=("job_ids", "restarted", "resumed_from"),
    headers=("Jobs", "Restarted", "From"),
    example={"job_ids": ["9f2c1a"], "restarted": 1, "resumed_from": 1048576},
    example_args="media transfer retry 9f2c1a",
    covers=("media.background-transfer-jobs",),
)


# ---------------------------------------------------------------------------
# media watch
# ---------------------------------------------------------------------------


class WatchReq(Request):
    chat: Annotated[
        list[PeerRef],
        arg(0, metavar="CHAT", required=False, variadic=True, kind="peer", help="Chats to watch."),
    ] = []
    type: Annotated[list[str], opt("--type", metavar="KIND", help="Media kinds to report.")] = []
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Auto-download matching media there."),
    ] = None
    max_size: Annotated[
        str | None, opt("--max-size", metavar="SIZE", help="Skip auto-download above this size.")
    ] = None
    use_cloud_settings: Annotated[
        bool, opt("--use-cloud-settings", help="Honour the account's auto-download preset.")
    ] = False
    from_user: Annotated[
        PeerRef | None,
        opt("--from", metavar="USER", kind="user", help="Only media from this sender."),
    ] = None
    min_duration: Annotated[
        int | None,
        opt("--min-duration", metavar="SECONDS", kind="duration", help="Only longer audio/video."),
    ] = None


async def watch(ctx: OpContext, req: WatchReq) -> Any:
    """Stream incoming media events, optionally downloading them.

    A media-shaped view of the daemon's existing update bus, not a second
    update loop: the daemon already holds the client and the MessageBox
    state, and this subscribes to it. Service messages never reach
    `events.NewMessage`, so a changed chat photo is not reported here —
    `media list --type chat-photo` is.
    """
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise NotSupportedError("this build has no event bus to watch")

    chats: list[int] = []
    for reference in req.chat:
        chats.append(_send.peer_id_of(await _send.resolve(ctx, reference)))
    ceiling = _size_arg(req.max_size, "max_size")
    if req.use_cloud_settings and ceiling is None:
        ceiling = await _cloud_ceiling(ctx)
    wanted = {kind for kind in req.type if kind != "all"}
    sender = _send.peer_id_of(await _send.resolve(ctx, req.from_user)) if req.from_user else None

    subscriber = bus.subscribe(ctx.account, types=("message_new",), chats=chats)
    try:
        while True:
            event = await subscriber.queue.get()
            frame = _media_event(event, wanted, sender, req.min_duration)
            if frame is None:
                continue
            if req.download and (ceiling is None or (frame.size or 0) <= ceiling):
                frame.path = await _watch_download(ctx, event, req)
            yield Page(items=[frame], has_more=True)
    finally:
        bus.unsubscribe(subscriber)


def _media_event(
    event: Any, wanted: set[str], sender: int | None, min_duration: int | None
) -> MediaEvent | None:
    payload = getattr(event, "payload", None) or {}
    media = payload.get("media")
    if not isinstance(media, dict):
        return None
    kind = str(media.get("kind") or "")
    if wanted and kind not in wanted:
        return None
    if sender is not None and payload.get("sender_id") != sender:
        return None
    duration = media.get("duration")
    if min_duration is not None and int(duration or 0) < min_duration:
        return None
    return MediaEvent(
        event="media",
        chat_id=int(payload.get("chat_id") or event.chat_id or 0),
        msg_id=int(payload.get("id") or 0),
        kind=kind,
        mime=media.get("mime_type"),
        size=media.get("size"),
        duration=duration,
        from_id=payload.get("sender_id"),
        date=str(payload.get("date") or ""),
    )


async def _cloud_ceiling(ctx: OpContext) -> int | None:
    """The account's own auto-download cap, as the size limit."""
    settings = await auto_download_get(ctx, AutoDownloadGetReq(preset="medium"))
    for preset in settings.presets:
        if preset.preset == "medium":
            return max(preset.photo_size_max, preset.video_size_max, preset.file_size_max) or None
    return None


async def _watch_download(ctx: OpContext, event: Any, req: WatchReq) -> str | None:
    payload = getattr(event, "payload", None) or {}
    chat_id = int(payload.get("chat_id") or 0)
    message_id = int(payload.get("id") or 0)
    if not chat_id or not message_id:
        return None
    try:
        message = await _media.fetch_message(ctx, chat_id, message_id)
        document = _media.document_of(message.media) or _media.photo_of(message.media)
        target = Path(os.path.expanduser(str(req.download))) / _safe_name(
            _media.attributes_of(document).get("file_name") or _default_name(message.media)
        )
        path = await _download_bytes(
            ctx,
            document,
            target,
            size=int(getattr(document, "size", 0) or 0),
            dc_id=int(getattr(document, "dc_id", 0) or 0),
        )
        return str(path)
    except Exception as exc:  # a failed auto-download must not end the stream
        ctx.warn(f"auto-download of {message_id} failed: {exc}")
        return None


SPEC_WATCH = OperationSpec(
    id="media.watch",
    request=WatchReq,
    response=Page[MediaEvent],
    impl=watch,
    summary="Stream incoming media events and optionally auto-download them",
    stream=True,
    timeout_s=900,
    columns=("chat_id", "msg_id", "kind", "size"),
    headers=("Chat", "ID", "Kind", "Size"),
    example={
        "items": [
            {
                "event": "media",
                "chat_id": 777123,
                "msg_id": 12347,
                "kind": "photo",
                "size": 184320,
            }
        ],
        "has_more": True,
    },
    example_args="media watch @alice --type photo",
    covers=("media.receive-event-watch",),
)


# ---------------------------------------------------------------------------
# media wallpaper
# ---------------------------------------------------------------------------


def _colour(value: str) -> int:
    text = value.strip().lstrip("#")
    try:
        return int(text, 16)
    except ValueError as exc:
        raise UsageError(f"{value!r} is not a hex colour", field="colors") from exc


def _colour_text(value: int | None) -> str | None:
    return f"#{value:06x}" if isinstance(value, int) else None


def _wallpaper_settings(settings: Any) -> WallpaperSettings:
    colours = [
        _colour_text(getattr(settings, name, None))
        for name in (
            "background_color",
            "second_background_color",
            "third_background_color",
            "fourth_background_color",
        )
    ]
    return WallpaperSettings(
        blur=bool(getattr(settings, "blur", False)),
        motion=bool(getattr(settings, "motion", False)),
        intensity=getattr(settings, "intensity", None),
        rotation=getattr(settings, "rotation", None),
        colors=[c for c in colours if c],
    )


def _wallpaper_model(raw: Any) -> Wallpaper:
    settings = getattr(raw, "settings", None)
    parsed = _wallpaper_settings(settings) if settings is not None else WallpaperSettings()
    document = getattr(raw, "document", None)
    slug = getattr(raw, "slug", None)
    pattern = bool(getattr(raw, "pattern", False))
    kind: Literal["image", "pattern", "fill"] = (
        "fill" if document is None else ("pattern" if pattern else "image")
    )
    return Wallpaper(
        id=int(getattr(raw, "id", 0) or 0),
        access_hash=getattr(raw, "access_hash", None),
        slug=slug,
        kind=kind,
        pattern=pattern,
        dark=bool(getattr(raw, "dark", False)),
        creator=bool(getattr(raw, "creator", False)),
        default=bool(getattr(raw, "default", False)),
        colors=parsed.colors,
        blur=parsed.blur,
        motion=parsed.motion,
        intensity=parsed.intensity,
        rotation=parsed.rotation,
        document=_media.media_file(document),
        link=_wallpaper_link(slug, parsed),
    )


def _wallpaper_link(slug: str | None, settings: WallpaperSettings) -> str | None:
    """`t.me/bg/<slug>?…` — string formatting, never a request."""
    if not slug:
        return None
    modes = [name for name, on in (("blur", settings.blur), ("motion", settings.motion)) if on]
    query: list[str] = []
    if modes:
        query.append("mode=" + "+".join(modes))
    if settings.intensity is not None:
        query.append(f"intensity={settings.intensity}")
    if settings.colors:
        query.append("bg_color=" + "-".join(c.lstrip("#") for c in settings.colors))
    if settings.rotation is not None:
        query.append(f"rotation={settings.rotation}")
    return f"https://t.me/bg/{slug}" + ("?" + "&".join(query) if query else "")


def _wallpaper_ref(text: str) -> Any:
    from telethon.tl import types

    value = (text or "").strip()
    if "t.me/bg/" in value:
        value = value.split("t.me/bg/", 1)[1].split("?")[0]
    if value.lstrip("-").isdigit():
        raise UsageError(
            f"{value!r} is a wallpaper id without its access hash; name it by slug or "
            "by its t.me/bg link",
            field="wallpaper",
        )
    return types.InputWallPaperSlug(slug=value)


class WallpaperListReq(Request):
    saved: Annotated[bool, opt("--saved", help="Only the ones saved to the account.")] = False
    default: Annotated[bool, opt("--default", help="Only the preinstalled gallery.")] = False
    patterns: Annotated[bool, opt("--patterns", help="Only pattern and fill wallpapers.")] = False


async def wallpaper_list(ctx: OpContext, req: WallpaperListReq) -> Page[Wallpaper]:
    """The account's wallpapers.

    A hash-cached list, not an offset-paginated one: the server answers
    `wallPapersNotModified` when nothing changed, so there are no pages to
    walk and `--limit` only trims what came back.
    """
    from telethon.tl.functions import account as fn

    limit, _state = _media.window(ctx, "media.wallpaper.list", PageKind.LOCAL)
    result = await _client(ctx)(fn.GetWallPapersRequest(hash=0))
    items = [_wallpaper_model(raw) for raw in getattr(result, "wallpapers", None) or []]
    if req.saved:
        items = [item for item in items if not item.default]
    if req.default:
        items = [item for item in items if item.default]
    if req.patterns:
        items = [item for item in items if item.kind in ("pattern", "fill")]
    return Page(items=items[:limit], has_more=False, total=len(items))


SPEC_WALLPAPER_LIST = OperationSpec(
    id="media.wallpaper.list",
    request=WallpaperListReq,
    response=Page[Wallpaper],
    impl=wallpaper_list,
    summary="Wallpapers available to this account",
    paginated=PageKind.LOCAL,
    columns=("slug", "kind", "dark", "colors"),
    headers=("Slug", "Kind", "Dark", "Colours"),
    example={
        "items": [{"id": 5947530738857476, "slug": "Ycb0FfC6", "kind": "pattern", "dark": False}],
        "has_more": False,
    },
    example_args="media wallpaper list",
    covers=("dialogs.wallpaper-gallery", "wallpaper.list"),
)


class WallpaperGetReq(Request):
    wallpaper: Annotated[
        str, arg(0, metavar="WALLPAPER", help="Slug, or a https://t.me/bg/<slug> link.")
    ]
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Download the image.")
    ] = None
    link: Annotated[bool, opt("--link", help="Print only the shareable link.")] = False


async def wallpaper_get(ctx: OpContext, req: WallpaperGetReq) -> Wallpaper:
    """One wallpaper, and the share link its settings encode."""
    from telethon.tl.functions import account as fn

    result = await _client(ctx)(fn.GetWallPaperRequest(wallpaper=_wallpaper_ref(req.wallpaper)))
    model = _wallpaper_model(result)
    if req.out and model.document is not None:
        document = getattr(result, "document", None)
        path = await _download_bytes(
            ctx,
            document,
            Path(os.path.expanduser(req.out)),
            size=int(getattr(document, "size", 0) or 0),
            dc_id=int(getattr(document, "dc_id", 0) or 0),
        )
        model.document.file_id = model.document.file_id or str(path)
    return model


SPEC_WALLPAPER_GET = OperationSpec(
    id="media.wallpaper.get",
    request=WallpaperGetReq,
    response=Wallpaper,
    impl=wallpaper_get,
    summary="One wallpaper by slug or t.me/bg link",
    columns=("slug", "kind", "colors", "link"),
    headers=("Slug", "Kind", "Colours", "Link"),
    empty_exit=EXIT_EMPTY,
    example={
        "id": 5947530738857476,
        "slug": "Ycb0FfC6",
        "kind": "pattern",
        "colors": ["#dbddbb", "#6ba587"],
        "link": "https://t.me/bg/Ycb0FfC6?intensity=50",
    },
    example_args="media wallpaper get Ycb0FfC6",
    covers=("wallpaper.share-link",),
    covers_partial=("wallpaper.list",),
    coverage_note="The gallery itself is `media wallpaper list`.",
)


class WallpaperSetReq(Request):
    wallpaper: Annotated[
        str | None,
        arg(0, metavar="WALLPAPER", required=False, help="Slug or link; omit for a colour fill."),
    ] = None
    blur: Annotated[bool, opt("--blur", help="Apply blurred.")] = False
    motion: Annotated[bool, opt("--motion", help="Apply with parallax motion.")] = False
    intensity: Annotated[
        int | None, opt("--intensity", metavar="N", help="Pattern intensity, -100..100.")
    ] = None
    colors: Annotated[list[str], opt("--colors", metavar="HEX", help="1-4 hex fill colours.")] = []
    save_only: Annotated[
        bool, opt("--save-only", help="Add to the saved list without installing it.")
    ] = False
    reset: Annotated[bool, opt("--reset", help="Restore the server's default list.")] = False


def _settings_from(req: WallpaperSetReq) -> Any:
    from telethon.tl import types

    colours = [_colour(value) for value in req.colors][:4]
    padded = colours + [None] * (4 - len(colours))
    return types.WallPaperSettings(
        blur=req.blur or None,
        motion=req.motion or None,
        background_color=padded[0],
        second_background_color=padded[1],
        third_background_color=padded[2],
        fourth_background_color=padded[3],
        intensity=req.intensity,
    )


async def wallpaper_set(ctx: OpContext, req: WallpaperSetReq) -> WallpaperInstalled:
    """Make a wallpaper the account default.

    Only the cloud *choice* is synced; rendering is a client concern a
    terminal has no use for, so tlgr syncs the selection and stops there.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    if req.reset:
        await _client(ctx)(fn.ResetWallPapersRequest())
        return WallpaperInstalled(reset=True, installed=False, saved=False)

    settings = _settings_from(req)
    if req.wallpaper:
        reference: Any = _wallpaper_ref(req.wallpaper)
    elif req.colors:
        # A pure colour or gradient is a wallpaper with no file at all.
        reference = types.InputWallPaperNoFile(id=0)
    else:
        raise UsageError("name a wallpaper, or give --colors for a fill", field="wallpaper")

    if req.save_only:
        await _client(ctx)(
            fn.SaveWallPaperRequest(wallpaper=reference, unsave=False, settings=settings)
        )
        return WallpaperInstalled(
            slug=req.wallpaper, installed=False, saved=True, settings=_wallpaper_settings(settings)
        )

    await _client(ctx)(fn.InstallWallPaperRequest(wallpaper=reference, settings=settings))
    return WallpaperInstalled(
        slug=req.wallpaper,
        installed=True,
        saved=True,
        settings=_wallpaper_settings(settings),
    )


SPEC_WALLPAPER_SET = OperationSpec(
    id="media.wallpaper.set",
    request=WallpaperSetReq,
    response=WallpaperInstalled,
    impl=wallpaper_set,
    summary="Make a wallpaper the account default",
    mutating=True,
    columns=("slug", "installed", "saved"),
    headers=("Slug", "Installed", "Saved"),
    example={"slug": "Ycb0FfC6", "installed": True, "saved": True},
    example_args="media wallpaper set Ycb0FfC6 --blur",
    covers=("wallpaper.save-install",),
)


class WallpaperRemoveReq(Request):
    wallpaper: Annotated[
        list[str], arg(0, metavar="WALLPAPER", variadic=True, help="Slugs or links to unsave.")
    ] = []


async def wallpaper_remove(ctx: OpContext, req: WallpaperRemoveReq) -> WallpaperRemoved:
    """Remove wallpapers from the saved gallery.

    Not the same as changing the current one: `media wallpaper set --reset`
    is the "restore defaults" button.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    slugs = list(req.wallpaper)
    if not slugs:
        raise UsageError("name at least one wallpaper", field="wallpaper")
    for slug in slugs:
        await _client(ctx)(
            fn.SaveWallPaperRequest(
                wallpaper=_wallpaper_ref(slug), unsave=True, settings=types.WallPaperSettings()
            )
        )
    return WallpaperRemoved(slugs=slugs, removed=len(slugs))


SPEC_WALLPAPER_REMOVE = OperationSpec(
    id="media.wallpaper.remove",
    request=WallpaperRemoveReq,
    response=WallpaperRemoved,
    impl=wallpaper_remove,
    summary="Remove a wallpaper from the saved list",
    mutating=True,
    destructive=True,
    columns=("slugs", "removed"),
    headers=("Slugs", "Removed"),
    example={"slugs": ["Ycb0FfC6"], "removed": 1},
    example_args="media wallpaper remove Ycb0FfC6",
    covers_partial=("wallpaper.save-install",),
    coverage_note="Installing is `media wallpaper set`; this is only the saved gallery.",
)


class WallpaperUploadReq(Request):
    path: Annotated[str, arg(0, metavar="PATH", kind="path", help="Image to upload.")]
    pattern: Annotated[bool, opt("--pattern", help="Treat the image as a tintable pattern.")] = (
        False
    )
    colors: Annotated[list[str], opt("--colors", metavar="HEX", help="1-4 hex fill colours.")] = []
    blur: Annotated[bool, opt("--blur", help="Store blurred.")] = False
    motion: Annotated[bool, opt("--motion", help="Store with parallax motion.")] = False
    intensity: Annotated[
        int, opt("--intensity", metavar="N", help="Pattern intensity.", ge=-100, le=100)
    ] = 50
    for_chat: Annotated[bool, opt("--for-chat", help="Upload for use as a per-chat wallpaper.")] = (
        False
    )


async def wallpaper_upload(ctx: OpContext, req: WallpaperUploadReq) -> WallpaperUploaded:
    """Upload a custom wallpaper image or pattern.

    The slug that comes back is what `media wallpaper set` and `chat wallpaper
    set` consume; `--for-chat` is required for the latter, and the server
    rejects the wrong one rather than silently ignoring it.
    """
    import mimetypes

    from telethon.tl import types
    from telethon.tl.functions import account as fn

    path = Path(os.path.expanduser(req.path))
    if not path.exists():
        raise UsageError(f"{req.path} does not exist", field="path")
    upload_service = getattr(ctx, "upload_file", None)
    if upload_service is None:  # pragma: no cover
        raise UsageError("this context cannot upload files")
    handle = await upload_service(path)

    colours = [_colour(value) for value in req.colors][:4]
    padded = colours + [None] * (4 - len(colours))
    settings = types.WallPaperSettings(
        blur=req.blur or None,
        motion=req.motion or None,
        background_color=padded[0],
        second_background_color=padded[1],
        third_background_color=padded[2],
        fourth_background_color=padded[3],
        intensity=req.intensity if req.pattern else None,
    )
    result = await _client(ctx)(
        fn.UploadWallPaperRequest(
            file=handle,
            mime_type=mimetypes.guess_type(path.name)[0] or "image/jpeg",
            settings=settings,
            for_chat=req.for_chat or None,
        )
    )
    parsed = _wallpaper_settings(settings)
    slug = getattr(result, "slug", None)
    return WallpaperUploaded(
        id=int(getattr(result, "id", 0) or 0),
        access_hash=getattr(result, "access_hash", None),
        slug=slug,
        link=_wallpaper_link(slug, parsed),
        settings=parsed,
    )


SPEC_WALLPAPER_UPLOAD = OperationSpec(
    id="media.wallpaper.upload",
    request=WallpaperUploadReq,
    response=WallpaperUploaded,
    impl=wallpaper_upload,
    summary="Upload a custom wallpaper image or pattern",
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("slug", "link"),
    headers=("Slug", "Link"),
    example={
        "id": 5947530738857476,
        "slug": "Ycb0FfC6",
        "link": "https://t.me/bg/Ycb0FfC6",
    },
    example_args="media wallpaper upload background.jpg",
    covers_partial=("wallpaper.save-install",),
    coverage_note="Installing the uploaded slug is `media wallpaper set`.",
)


# ---------------------------------------------------------------------------
# media auto-download / auto-save / sensitive
# ---------------------------------------------------------------------------


class AutoDownloadGetReq(Request):
    preset: Annotated[
        str | None, choice("low", "medium", "high", help="Show one preset instead of all three.")
    ] = None


def _preset_model(name: str, settings: Any) -> AutoDownloadPreset:
    return AutoDownloadPreset(
        preset=name,
        disabled=bool(getattr(settings, "disabled", False)),
        photo_size_max=int(getattr(settings, "photo_size_max", 0) or 0),
        video_size_max=int(getattr(settings, "video_size_max", 0) or 0),
        file_size_max=int(getattr(settings, "file_size_max", 0) or 0),
        video_upload_maxbitrate=int(getattr(settings, "video_upload_maxbitrate", 0) or 0),
        video_preload_large=bool(getattr(settings, "video_preload_large", False)),
        audio_preload_next=bool(getattr(settings, "audio_preload_next", False)),
        phonecalls_less_data=bool(getattr(settings, "phonecalls_less_data", False)),
        stories_preload=bool(getattr(settings, "stories_preload", False)),
    )


async def auto_download_get(ctx: OpContext, req: AutoDownloadGetReq) -> AutoDownloadSettings:
    """The cloud auto-download presets.

    A synced *preference*, not behaviour: the downloading itself is the
    client's, and in tlgr that is `media watch --download
    --use-cloud-settings`. The GUI's mobile/wifi/roaming rows are these three.
    """
    from telethon.tl.functions import account as fn

    result = await _client(ctx)(fn.GetAutoDownloadSettingsRequest())
    presets = [
        _preset_model(name, getattr(result, name, None))
        for name in ("low", "medium", "high")
        if getattr(result, name, None) is not None
    ]
    if req.preset:
        presets = [preset for preset in presets if preset.preset == req.preset]
    return AutoDownloadSettings(presets=presets)


SPEC_AUTO_DOWNLOAD_GET = OperationSpec(
    id="media.auto-download.get",
    request=AutoDownloadGetReq,
    response=AutoDownloadSettings,
    impl=auto_download_get,
    summary="Read the cloud auto-download presets",
    example={
        "presets": [
            {"preset": "low", "photo_size_max": 1048576, "video_size_max": 512000},
            {"preset": "medium", "photo_size_max": 1048576, "video_size_max": 10485760},
        ]
    },
    example_args="media auto-download get",
    covers_partial=("media.auto-download-settings",),
    coverage_note="Writing is `media auto-download set`.",
)


class AutoDownloadSetReq(Request):
    preset: Annotated[str, choice("low", "medium", "high", help="Which preset to write.")] = (
        "medium"
    )
    disabled: Annotated[
        bool | None, opt("--disabled/--enabled", help="Turn auto-download off for this preset.")
    ] = None
    photo_max: Annotated[str | None, opt("--photo-max", metavar="SIZE", help="Photo cap.")] = None
    video_max: Annotated[str | None, opt("--video-max", metavar="SIZE", help="Video cap.")] = None
    file_max: Annotated[str | None, opt("--file-max", metavar="SIZE", help="File cap.")] = None
    preload_large_video: Annotated[
        bool | None,
        opt("--preload-large-video/--no-preload-large-video", help="Preload big videos."),
    ] = None
    preload_next_audio: Annotated[
        bool | None,
        opt("--preload-next-audio/--no-preload-next-audio", help="Preload the next track."),
    ] = None
    preload_stories: Annotated[
        bool | None, opt("--preload-stories/--no-preload-stories", help="Preload stories.")
    ] = None
    less_call_data: Annotated[
        bool | None, opt("--less-call-data/--no-less-call-data", help="Use less data for calls.")
    ] = None
    reset: Annotated[bool, opt("--reset", help="Restore the server defaults.")] = False


async def auto_download_set(ctx: OpContext, req: AutoDownloadSetReq) -> AutoDownloadSaved:
    """Write one auto-download preset.

    Read-modify-write, because the request carries a whole
    `autoDownloadSettings`: writing one field from scratch would blank the
    rest of that preset.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    current = await auto_download_get(ctx, AutoDownloadGetReq(preset=req.preset))
    base = current.presets[0] if current.presets else AutoDownloadPreset(preset=req.preset)
    if req.reset:
        base = AutoDownloadPreset(preset=req.preset)

    settings = types.AutoDownloadSettings(
        photo_size_max=_size_arg(req.photo_max, "photo_max") or base.photo_size_max,
        video_size_max=_size_arg(req.video_max, "video_max") or base.video_size_max,
        file_size_max=_size_arg(req.file_max, "file_max") or base.file_size_max,
        video_upload_maxbitrate=base.video_upload_maxbitrate,
        small_queue_active_operations_max=5,
        large_queue_active_operations_max=2,
        disabled=(base.disabled if req.disabled is None else req.disabled) or None,
        video_preload_large=(
            base.video_preload_large if req.preload_large_video is None else req.preload_large_video
        )
        or None,
        audio_preload_next=(
            base.audio_preload_next if req.preload_next_audio is None else req.preload_next_audio
        )
        or None,
        phonecalls_less_data=(
            base.phonecalls_less_data if req.less_call_data is None else req.less_call_data
        )
        or None,
        stories_preload=(
            base.stories_preload if req.preload_stories is None else req.preload_stories
        )
        or None,
    )
    await _client(ctx)(
        fn.SaveAutoDownloadSettingsRequest(
            settings=settings,
            low=req.preset == "low" or None,
            high=req.preset == "high" or None,
        )
    )
    return AutoDownloadSaved(
        preset=req.preset, ok=True, settings=_preset_model(req.preset, settings)
    )


SPEC_AUTO_DOWNLOAD_SET = OperationSpec(
    id="media.auto-download.set",
    request=AutoDownloadSetReq,
    response=AutoDownloadSaved,
    impl=auto_download_set,
    summary="Write the cloud auto-download presets",
    mutating=True,
    columns=("preset", "ok"),
    headers=("Preset", "OK"),
    example={"preset": "medium", "ok": True},
    example_args="media auto-download set --preset medium --video-max 10M",
    covers=("media.auto-download-settings",),
)


class AutoSaveGetReq(Request):
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Show the exception for one chat."),
    ] = None


def _auto_save_rule(settings: Any) -> AutoSaveRule | None:
    if settings is None:
        return None
    return AutoSaveRule(
        photos=bool(getattr(settings, "photos", False)),
        videos=bool(getattr(settings, "videos", False)),
        video_max_size=getattr(settings, "video_max_size", None),
    )


async def auto_save_get(ctx: OpContext, req: AutoSaveGetReq) -> AutoSaveSettings:
    """The cloud save-to-gallery rules.

    A synced preference tlgr honours in `media watch --download
    --use-cloud-settings`; it keeps no gallery of its own.
    """
    from telethon.tl.functions import account as fn

    result = await _client(ctx)(fn.GetAutoSaveSettingsRequest())
    peers = {
        entity.id: entity_to_peer(entity)
        for entity in [
            *(getattr(result, "users", None) or []),
            *(getattr(result, "chats", None) or []),
        ]
    }
    exceptions: list[AutoSaveException] = []
    from tlgr.ops._serialize import peer_id_of as marked_of

    for entry in getattr(result, "exceptions", None) or []:
        chat_id = marked_of(getattr(entry, "peer", None)) or 0
        exceptions.append(
            AutoSaveException(
                chat_id=chat_id,
                chat=peers.get(abs(chat_id) % 1000000000000),
                rule=_auto_save_rule(getattr(entry, "settings", None)),
            )
        )
    if req.chat is not None:
        wanted = _send.peer_id_of(await _send.resolve(ctx, req.chat))
        exceptions = [entry for entry in exceptions if entry.chat_id == wanted]
    return AutoSaveSettings(
        users=_auto_save_rule(getattr(result, "users_settings", None)),
        chats=_auto_save_rule(getattr(result, "chats_settings", None)),
        broadcasts=_auto_save_rule(getattr(result, "broadcasts_settings", None)),
        exceptions=exceptions,
    )


SPEC_AUTO_SAVE_GET = OperationSpec(
    id="media.auto-save.get",
    request=AutoSaveGetReq,
    response=AutoSaveSettings,
    impl=auto_save_get,
    summary="Read the cloud save-to-gallery rules",
    example={
        "users": {"photos": True, "videos": False},
        "chats": {"photos": False, "videos": False},
        "exceptions": [],
    },
    example_args="media auto-save get",
    covers_partial=("media.auto-save-to-gallery-settings",),
    coverage_note="Writing is `media auto-save set`.",
)


class AutoSaveSetReq(Request):
    scope: Annotated[
        str,
        choice("users", "groups", "channels", help="Which category to write."),
    ] = "users"
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Write an exception for one chat."),
    ] = None
    photos: Annotated[bool | None, opt("--photos/--no-photos", help="Auto-save photos.")] = None
    videos: Annotated[bool | None, opt("--videos/--no-videos", help="Auto-save videos.")] = None
    video_max: Annotated[str | None, opt("--video-max", metavar="SIZE", help="Video size cap.")] = (
        None
    )
    clear_exceptions: Annotated[
        bool, opt("--clear-exceptions", help="Delete every per-chat exception.")
    ] = False


async def auto_save_set(ctx: OpContext, req: AutoSaveSetReq) -> AutoSaveSaved:
    """Write one save-to-gallery scope.

    The API takes exactly one scope per call — users, groups, channels or a
    single peer — so this is one `--scope` choice rather than three flags that
    could contradict each other.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    if req.clear_exceptions:
        await _client(ctx)(fn.DeleteAutoSaveExceptionsRequest())
        return AutoSaveSaved(scope="exceptions", ok=True, cleared_exceptions=True)

    settings = types.AutoSaveSettings(
        photos=req.photos,
        videos=req.videos,
        video_max_size=_size_arg(req.video_max, "video_max"),
    )
    peer = await _send.resolve(ctx, req.chat) if req.chat is not None else None
    await _client(ctx)(
        fn.SaveAutoSaveSettingsRequest(
            settings=settings,
            users=(req.scope == "users" and peer is None) or None,
            chats=(req.scope == "groups" and peer is None) or None,
            broadcasts=(req.scope == "channels" and peer is None) or None,
            peer=peer,
        )
    )
    return AutoSaveSaved(
        scope="chat" if peer is not None else req.scope,
        ok=True,
        settings=_auto_save_rule(settings),
    )


SPEC_AUTO_SAVE_SET = OperationSpec(
    id="media.auto-save.set",
    request=AutoSaveSetReq,
    response=AutoSaveSaved,
    impl=auto_save_set,
    summary="Write the cloud save-to-gallery rules",
    mutating=True,
    columns=("scope", "ok"),
    headers=("Scope", "OK"),
    example={"scope": "users", "ok": True, "settings": {"photos": True, "videos": True}},
    example_args="media auto-save set --scope users --photos",
    covers=("media.auto-save-to-gallery-settings",),
)


class SensitiveGetReq(Request):
    pass


async def sensitive_get(ctx: OpContext, req: SensitiveGetReq) -> ContentSettings:
    """Whether 18+ media is shown, and whether that can be changed from here.

    `sensitive_can_change = false` is the interesting case: in some regions
    the toggle only unlocks after an age check inside a Telegram-designated
    mini app, which a terminal cannot render. tlgr reports that and names the
    bot rather than retrying a write the server will refuse.
    """
    from telethon.tl.functions import account as fn

    result = await _client(ctx)(fn.GetContentSettingsRequest())
    can_change = bool(getattr(result, "sensitive_can_change", False))
    settings = ContentSettings(
        sensitive_enabled=bool(getattr(result, "sensitive_enabled", False)),
        sensitive_can_change=can_change,
    )
    if not can_change:
        values = await _media.app_config(ctx)
        bot = values.get("verify_age_bot_username")
        settings.age_verification_required = bool(values.get("verify_age_min") or bot)
        settings.age_verification_bot = f"@{bot}" if bot else None
        settings.reason = (
            "this account cannot change the setting from an API client; the toggle "
            "unlocks after an age check inside Telegram's own verification mini app"
        )
    return settings


SPEC_SENSITIVE_GET = OperationSpec(
    id="media.sensitive.get",
    request=SensitiveGetReq,
    response=ContentSettings,
    impl=sensitive_get,
    summary="Whether 18+ media is shown, and whether that can be changed here",
    columns=("sensitive_enabled", "sensitive_can_change"),
    headers=("Enabled", "Can change"),
    example={"sensitive_enabled": False, "sensitive_can_change": True},
    example_args="media sensitive get",
    covers=("media.age-verification",),
    covers_partial=("media.sensitive-content-setting",),
    coverage_note="Writing the toggle is `media sensitive set`.",
)


class SensitiveSetReq(Request):
    state: Annotated[str, arg(0, metavar="STATE", help="on or off.")]


async def sensitive_set(ctx: OpContext, req: SensitiveSetReq) -> ContentSettingsSaved:
    """Show or hide 18+ media, after checking that this account may.

    The pre-flight is the point: letting the server reject the write produces
    a confusing failure, while `sensitive_can_change` answers "you cannot do
    this from here, and here is why" before anything is sent.
    """
    from telethon.tl.functions import account as fn

    wanted = str(req.state).strip().lower()
    if wanted not in ("on", "off", "true", "false", "1", "0"):
        raise UsageError("STATE is `on` or `off`", field="state")
    enable = wanted in ("on", "true", "1")

    current = await sensitive_get(ctx, SensitiveGetReq())
    if current.sensitive_enabled == enable:
        ctx.mark_already() if hasattr(ctx, "mark_already") else None
        return ContentSettingsSaved(sensitive_enabled=enable, ok=True, already=True)
    if not current.sensitive_can_change:
        raise PermissionError_(
            current.reason
            or "this account may not change the sensitive-content setting from an API client"
        )
    await _client(ctx)(fn.SetContentSettingsRequest(sensitive_enabled=enable or None))
    return ContentSettingsSaved(sensitive_enabled=enable, ok=True)


SPEC_SENSITIVE_SET = OperationSpec(
    id="media.sensitive.set",
    request=SensitiveSetReq,
    response=ContentSettingsSaved,
    impl=sensitive_set,
    summary="Show or hide 18+ media",
    mutating=True,
    idempotent=True,
    columns=("sensitive_enabled", "ok", "already"),
    headers=("Enabled", "OK", "Already"),
    example={"sensitive_enabled": True, "ok": True},
    example_args="media sensitive set on",
    covers=("media.sensitive-content-setting",),
    covers_partial=("media.age-verification",),
    coverage_note="The verification flow itself runs in Telegram's mini app, not here.",
)


# ---------------------------------------------------------------------------
# media storage (local)
# ---------------------------------------------------------------------------


_KIND_SUFFIXES = {
    "photo": (".jpg", ".jpeg", ".png", ".webp", ".heic"),
    "video": (".mp4", ".mkv", ".mov", ".webm"),
    "music": (".mp3", ".m4a", ".flac"),
    "voice": (".ogg", ".oga", ".opus"),
    "gif": (".gif",),
    "thumb": (".thumb.jpg",),
    "partial": (".part",),
}


def _kind_of_file(path: Path) -> str:
    suffix = path.suffix.lower()
    for kind, suffixes in _KIND_SUFFIXES.items():
        if suffix in suffixes:
            return kind
    return "file"


def _storage_root(ctx: OpContext) -> Path:
    from tlgr.core.config import get_downloads_dir

    return Path(get_downloads_dir())


class StorageGetReq(Request):
    by_chat: Annotated[bool, opt("--by-chat", help="Break the total down per chat.")] = False
    by_type: Annotated[bool, opt("--by-type", help="Break the total down per media type.")] = False


async def storage_get(ctx: OpContext, req: StorageGetReq) -> StorageUsage:
    """Disk used by tlgr's own downloads.

    Purely local: Telegram stores no cache statistics, and the scope is the
    configured downloads root and nothing else — never the user's own
    directories.
    """
    root = _storage_root(ctx)
    usage = StorageUsage(root=str(root))
    if not root.exists():
        return usage
    oldest: float | None = None
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        usage.files += 1
        usage.bytes += size
        if path.suffix == ".part":
            usage.partials += 1
        stamp = path.stat().st_mtime
        oldest = stamp if oldest is None else min(oldest, stamp)
        if req.by_chat:
            chat = path.relative_to(root).parts[0]
            usage.by_chat[chat] = usage.by_chat.get(chat, 0) + size
        if req.by_type:
            kind = _kind_of_file(path)
            usage.by_type[kind] = usage.by_type.get(kind, 0) + size
    if oldest is not None:
        from datetime import datetime, timezone

        usage.oldest = datetime.fromtimestamp(oldest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return usage


SPEC_STORAGE_GET = OperationSpec(
    id="media.storage.get",
    request=StorageGetReq,
    response=StorageUsage,
    impl=storage_get,
    summary="Disk usage of tlgr's downloads and cache",
    surface=Surface.LOCAL,
    needs_account=False,
    needs_auth=False,
    rate_class="local",
    columns=("root", "files", "bytes", "partials"),
    headers=("Root", "Files", "Bytes", "Partial"),
    example={
        "root": "/home/u/.tlgr/downloads",
        "bytes": 918273645,
        "files": 412,
        "partials": 1,
    },
    example_args="media storage get --by-type",
    covers=("media.storage-cache",),
)


class StorageClearReq(Request):
    older_than: Annotated[
        int | None,
        opt(
            "--older-than", metavar="DURATION", kind="duration", help="Only files older than this."
        ),
    ] = None
    type: Annotated[list[str], opt("--type", metavar="KIND", help="Only these kinds.")] = []
    chat: Annotated[
        str | None, opt("--chat", metavar="CHAT", help="Only this chat's directory.")
    ] = None
    keep_days: Annotated[
        int | None, opt("--keep-days", metavar="N", help="Persist a keep-media TTL in days.")
    ] = None


async def storage_clear(ctx: OpContext, req: StorageClearReq) -> StorageCleared:
    """Delete cached downloads, and only inside tlgr's own root.

    Anything outside the configured downloads directory is out of scope by
    construction: the walk starts there and never follows a link out of it.
    """
    root = _storage_root(ctx)
    cleared = StorageCleared(keep_days=req.keep_days)
    if not root.exists():
        return cleared
    cutoff = time.time() - req.older_than if req.older_than else None
    wanted = set(req.type)
    scope = root / req.chat if req.chat else root

    for path in scope.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if cutoff is not None and path.stat().st_atime > cutoff:
            cleared.kept_files += 1
            continue
        if wanted and _kind_of_file(path) not in wanted:
            cleared.kept_files += 1
            continue
        size = path.stat().st_size
        with contextlib.suppress(OSError):
            path.unlink()
            cleared.deleted_files += 1
            cleared.freed_bytes += size
    return cleared


SPEC_STORAGE_CLEAR = OperationSpec(
    id="media.storage.clear",
    request=StorageClearReq,
    response=StorageCleared,
    impl=storage_clear,
    summary="Delete cached downloads",
    surface=Surface.LOCAL,
    needs_account=False,
    needs_auth=False,
    mutating=True,
    destructive=True,
    rate_class="local",
    columns=("deleted_files", "freed_bytes", "kept_files"),
    headers=("Deleted", "Freed", "Kept"),
    example={"deleted_files": 128, "freed_bytes": 419430400, "kept_files": 284},
    example_args="media storage clear --older-than 30d",
    covers_partial=("media.storage-cache",),
    coverage_note="Reporting the usage is `media storage get`.",
)
