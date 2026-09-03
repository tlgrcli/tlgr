"""The media domain: what a file IS, where it went, and what it cost.

`MediaSummary` (models/message.py) answers "what is attached to this message"
in the space a message listing can afford. The shapes here answer the
questions the `media` group is actually asked — which sizes exist, what the
document id and access hash are, how far a transfer got, what the account's
own upload limits are — and they are deliberately separate models rather than
a wider `MediaSummary`, because a chat listing must not carry an access hash
and a file reference for every row.

Two conventions run through the file:

* **a byte string never crosses the wire raw.** `file_reference` and a
  stripped thumbnail are base64, because JSON has no bytes and a caller that
  round-trips one has to get the same bytes back.
* **an id is a *marked* id** (`chat_id`), and a document id is not — a
  document has no peer arithmetic to disambiguate.
"""

from __future__ import annotations

from typing import Any, Literal

from tlgr.models.base import Model
from tlgr.models.message import MediaKind, MessageEntity
from tlgr.models.peer import Peer

__all__ = [
    "AutoDownloadPreset",
    "AutoDownloadSaved",
    "AutoDownloadSettings",
    "AutoSaveException",
    "AutoSaveRule",
    "AutoSaveSaved",
    "AutoSaveSettings",
    "ContentSettings",
    "ContentSettingsSaved",
    "Downloaded",
    "FileRef",
    "MediaEdited",
    "MediaEvent",
    "MediaExportResult",
    "MediaFile",
    "MediaInfo",
    "MediaItem",
    "MediaLimits",
    "MediaQuality",
    "MediaRead",
    "MediaSize",
    "PaidItem",
    "PaidPost",
    "StorageCleared",
    "StorageUsage",
    "Transfer",
    "TransferRestarted",
    "TransferStopped",
    "Uploaded",
    "Wallpaper",
    "WallpaperInstalled",
    "WallpaperRemoved",
    "WallpaperSettings",
    "WallpaperUploaded",
]

#: How a transfer is going. `cancelled` and `failed` are distinct because only
#: one of them is worth retrying automatically.
TransferState = Literal["queued", "running", "done", "failed", "cancelled"]


class MediaFile(Model):
    """The handle half of a document: enough to fetch or re-send it.

    Kept apart from the descriptive fields so that a listing can embed it
    without every row growing an access hash it has no use for.
    """

    doc_id: int
    access_hash: int | None = None
    file_reference_b64: str | None = None
    dc_id: int | None = None
    mime: str | None = None
    size: int | None = None
    file_id: str | None = None


class MediaSize(Model):
    """One `PhotoSize`/`VideoSize`/thumbnail variant.

    `bytes_b64` is filled only for the inline sizes (`photoStrippedSize`,
    `photoPathSize`), which are already in the message and cost no request:
    that is the whole point of reporting them.
    """

    type: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    video: bool = False
    bytes_b64: str | None = None


class MediaQuality(Model):
    """One `alt_documents` transcode of a channel video."""

    doc_id: int
    mime: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    name: str | None = None


class MediaInfo(Model):
    """Everything a message's media declares, without downloading a byte.

    A strict superset of v1's `media_details()`: the kind is still decided
    after collecting every `DocumentAttribute*` (a GIF carries Video *and*
    Animated, so first-attribute-wins gets it wrong), and the ids, sizes,
    album membership and protection state that a caller needs in order to do
    anything with the file are added beside it.
    """

    chat_id: int
    msg_id: int
    kind: MediaKind
    tl_type: str = ""
    grouped_id: int | None = None
    mime: str | None = None
    size: int | None = None
    file_name: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    supports_streaming: bool = False
    nosound: bool = False
    round: bool = False
    voice: bool = False
    waveform: bool = False
    title: str | None = None
    performer: str | None = None
    sticker: str | None = None
    custom_emoji: str | None = None
    spoiler: bool = False
    ttl_seconds: int | None = None
    video_cover: int | None = None
    video_timestamp: int | None = None
    has_stickers: bool = False
    protected: bool = False
    paid: int | None = None
    dc_id: int | None = None
    doc_id: int | None = None
    access_hash: int | None = None
    file_reference_b64: str | None = None
    file_id: str | None = None
    thumbs: list[MediaSize] = []
    qualities: list[MediaQuality] = []
    date: str = ""
    date_unix: int = 0
    caption: str = ""
    entities: list[MessageEntity] = []
    #: `--stickers`: sets baked into the photo/video.
    attached_sets: list[str] = []
    #: `--ads`: sponsored inserts, listed and never viewed or clicked.
    ads: list[dict[str, Any]] = []
    #: `--album`: the sibling messages sharing `grouped_id`.
    album: list[MediaInfo] = []


class MediaItem(Model):
    """One row of a shared-media listing.

    The same model carries the `--counts` and `--calendar` projections, with
    `count`/`period` filled instead of `msg_id`: they are three views of one
    query and splitting them into three response types would make
    `--select` mean something different per flag.
    """

    msg_id: int = 0
    chat_id: int = 0
    date: str = ""
    date_unix: int = 0
    kind: MediaKind | None = None
    name: str | None = None
    size: int | None = None
    duration: int | None = None
    mime: str | None = None
    from_id: int | None = None
    grouped_id: int | None = None
    file_id: str | None = None
    chat: Peer | None = None
    #: `--counts` / `--calendar`: the media tab and how many it holds.
    type: str | None = None
    count: int | None = None
    period: str | None = None


class Downloaded(Model):
    """One file that landed on disk (or on stdout)."""

    msg_id: int = 0
    chat_id: int = 0
    path: str = ""
    bytes: int = 0
    kind: str = ""
    mime: str | None = None
    sha256: str | None = None
    file_id: str | None = None
    elapsed_s: float = 0.0
    skipped: bool = False
    job_id: str | None = None


class Uploaded(Model):
    """What a send produced: one message, an album, or just a file id."""

    chat_id: int = 0
    msg_id: int = 0
    msg_ids: list[int] = []
    grouped_id: int | None = None
    kind: str = ""
    file_id: str | None = None
    doc_id: int | None = None
    bytes: int = 0
    elapsed_s: float = 0.0
    #: A big-channel video the server is still converting.
    processing: bool = False
    scheduled_id: int | None = None
    job_id: str | None = None


class MediaEdited(Model):
    chat_id: int
    msg_id: int
    kind: str = ""
    file_id: str | None = None
    edit_date: str | None = None
    caption: str = ""
    changed: list[str] = []


class FileRef(Model):
    """A portable file id, resolved and (where needed) refreshed.

    `refreshed` is the interesting field: a file id carries a
    `file_reference` that expires in hours, so the honest answer to "is this
    id still usable" is "it is now, and here is whether I had to go and get
    a new reference for it".
    """

    file_id: str
    doc_id: int | None = None
    access_hash: int | None = None
    file_reference_b64: str | None = None
    dc_id: int | None = None
    kind: str = ""
    mime: str | None = None
    size: int | None = None
    source: str | None = None
    refreshed: bool = False
    path: str | None = None


class MediaExportResult(Model):
    job_id: str | None = None
    chat_id: int = 0
    planned: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    manifest: str | None = None


class Transfer(Model):
    """One entry of the Downloads panel — client-local state, as in the GUI."""

    job_id: str
    direction: Literal["download", "upload"] = "download"
    chat_id: int | None = None
    msg_id: int | None = None
    name: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    pct: float = 0.0
    bps: int = 0
    eta_s: int | None = None
    state: TransferState = "queued"
    path: str | None = None
    error: str | None = None
    started: str | None = None


class TransferStopped(Model):
    job_ids: list[str] = []
    cancelled: int = 0
    kept_partial: bool = True
    already: bool = False


class TransferRestarted(Model):
    job_ids: list[str] = []
    restarted: int = 0
    resumed_from: int = 0


class MediaRead(Model):
    chat_id: int
    msg_ids: list[int] = []
    marked: int = 0
    already: bool = False


class MediaLimits(Model):
    """The server's own limits, never hardcoded.

    Read before an upload rather than after it: `upload_max_fileparts` is the
    difference between a named refusal and a `FILE_PARTS_INVALID` twenty
    minutes into a 2 GB transfer.
    """

    premium: bool = False
    upload_max_fileparts: int = 0
    upload_max_bytes: int = 0
    part_size: int = 0
    caption_length_limit: int = 0
    message_length_limit: int = 0
    album_size_max: int = 0
    stickers_installed_limit: int = 0
    stickers_faved_limit: int = 0
    stickers_recent_limit: int = 0
    saved_gifs_limit: int = 0
    ringtone_size_max: int = 0
    ringtone_duration_max: int = 0
    stars_paid_post_amount_max: int = 0
    premium_speedup_upload: int = 0
    premium_speedup_download: int = 0


class AutoDownloadPreset(Model):
    preset: str
    disabled: bool = False
    photo_size_max: int = 0
    video_size_max: int = 0
    file_size_max: int = 0
    video_upload_maxbitrate: int = 0
    video_preload_large: bool = False
    audio_preload_next: bool = False
    phonecalls_less_data: bool = False
    stories_preload: bool = False


class AutoDownloadSettings(Model):
    presets: list[AutoDownloadPreset] = []


class AutoDownloadSaved(Model):
    preset: str
    ok: bool = True
    settings: AutoDownloadPreset | None = None


class AutoSaveRule(Model):
    photos: bool = False
    videos: bool = False
    video_max_size: int | None = None


class AutoSaveException(Model):
    chat_id: int
    chat: Peer | None = None
    rule: AutoSaveRule | None = None


class AutoSaveSettings(Model):
    users: AutoSaveRule | None = None
    chats: AutoSaveRule | None = None
    broadcasts: AutoSaveRule | None = None
    exceptions: list[AutoSaveException] = []


class AutoSaveSaved(Model):
    scope: str
    ok: bool = True
    settings: AutoSaveRule | None = None
    cleared_exceptions: bool = False


class ContentSettings(Model):
    # Neither field carries a default: both states of a two-state setting
    # matter, and `omit_defaults` would drop whichever one happened to equal
    # the default — leaving a caller unable to tell "off" from "not reported".
    sensitive_enabled: bool
    sensitive_can_change: bool
    age_verification_required: bool = False
    age_verification_bot: str | None = None
    reason: str | None = None


class ContentSettingsSaved(Model):
    sensitive_enabled: bool
    ok: bool = True
    already: bool = False


class StorageUsage(Model):
    root: str
    bytes: int = 0
    files: int = 0
    partials: int = 0
    oldest: str | None = None
    by_chat: dict[str, int] = {}
    by_type: dict[str, int] = {}
    keep_days: int | None = None


class StorageCleared(Model):
    deleted_files: int = 0
    freed_bytes: int = 0
    kept_files: int = 0
    keep_days: int | None = None
    dry_run: bool = False


class PaidItem(Model):
    kind: str = "preview"
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    doc_id: int | None = None
    unlocked: bool = False


class PaidPost(Model):
    chat_id: int
    msg_id: int
    stars_amount: int = 0
    item_count: int = 0
    unlocked: bool = False
    items: list[PaidItem] = []
    date: str = ""
    date_unix: int = 0
    caption: str = ""


class WallpaperSettings(Model):
    blur: bool = False
    motion: bool = False
    intensity: int | None = None
    rotation: int | None = None
    colors: list[str] = []


class Wallpaper(Model):
    id: int
    access_hash: int | None = None
    slug: str | None = None
    kind: Literal["image", "pattern", "fill"] = "image"
    pattern: bool = False
    dark: bool = False
    creator: bool = False
    default: bool = False
    colors: list[str] = []
    blur: bool = False
    motion: bool = False
    intensity: int | None = None
    rotation: int | None = None
    document: MediaFile | None = None
    link: str | None = None


class WallpaperInstalled(Model):
    slug: str | None = None
    installed: bool = False
    saved: bool = False
    reset: bool = False
    settings: WallpaperSettings | None = None
    already: bool = False


class WallpaperRemoved(Model):
    slugs: list[str] = []
    removed: int = 0
    already: bool = False


class WallpaperUploaded(Model):
    id: int
    access_hash: int | None = None
    slug: str | None = None
    link: str | None = None
    settings: WallpaperSettings | None = None


class MediaEvent(Model):
    """One frame of `media watch`."""

    event: str = "media"
    chat_id: int = 0
    msg_id: int = 0
    kind: str = ""
    mime: str | None = None
    size: int | None = None
    duration: int | None = None
    from_id: int | None = None
    file_id: str | None = None
    path: str | None = None
    date: str = ""
