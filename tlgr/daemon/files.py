"""Download and upload pipelines (§6.7, checklist 13/14).

Telethon's `download_media`/`send_file` are fine for a script and wrong for a
daemon. What is missing, and what this module adds:

* **the message is the source of truth.** A `file_reference` expires in
  minutes to hours. Downloading from a `Message` object a caller fetched an
  hour ago fails with `FILE_REFERENCE_EXPIRED`, and the only fix is to
  re-fetch the message — so the transfer keeps `(chat_id, msg_id)` and can
  refresh itself once, for photos and profile photos too (Telethon only does
  documents).
* **resume.** A 2 GB download that dies at 90 % starts again from zero.
  Parts are written to `<target>.part`, fsynced periodically, and the next
  attempt starts at the existing size.
* **concurrency that matches the server's.** `help.getAppConfig` publishes
  `small/large_queue_max_active_operations_count`; exceeding it earns a flood
  wait. A semaphore per `(account, dc_id)` caps large and small transfers
  separately.
* **uploads in parallel.** Telethon uploads parts strictly sequentially, so a
  large file is bounded by round-trip latency rather than bandwidth. A sliding
  window of 3–4 `upload.saveBigFilePart` calls is the single biggest speed-up
  available, and the part size rules come straight from
  `utils.get_appropriated_part_size`.

Stage C wires these to the `media` operations; what lands here is the
machinery and its error handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core.errors import TlgrError, UsageError

log = logging.getLogger("tlgr.daemon.files")

__all__ = [
    "DownloadPlan",
    "TransferSlots",
    "UploadPlan",
    "download",
    "part_size_for",
    "upload",
]

#: 512 KB is the largest part Telegram accepts and the size every official
#: client uses for anything but a thumbnail.
DEFAULT_PART_SIZE = 512 * 1024
_LARGE_FILE = 10 * 1024 * 1024
_BIG_UPLOAD = 10 * 1024 * 1024
_FSYNC_EVERY = 8 * 1024 * 1024
_PROGRESS_INTERVAL = 1.0
_PROGRESS_BYTES = 1024 * 1024

#: Anything at or above this counts against the "large" transfer budget.
LARGE_TRANSFER_BYTES = 20 * 1024 * 1024


def part_size_for(size: int) -> int:
    """`utils.get_appropriated_part_size`, in the units Telegram requires.

    The part size must divide 1 MB and every part except the last must be the
    same size; getting it wrong is `FILE_PART_SIZE_INVALID` after the upload
    has already spent the bandwidth.
    """
    if size <= 104857600:  # 100 MB
        return 128 * 1024 if size <= 10 * 1024 * 1024 else DEFAULT_PART_SIZE
    return DEFAULT_PART_SIZE


class TransferSlots:
    """Per-DC concurrency caps (§6.7).

    Two budgets, not one: a single 2 GB download must not be able to starve
    the five small thumbnail fetches a chat list needs.
    """

    def __init__(self, *, small: int = 5, large: int = 2) -> None:
        self._small_limit = max(1, small)
        self._large_limit = max(1, large)
        self._small: dict[int, asyncio.Semaphore] = {}
        self._large: dict[int, asyncio.Semaphore] = {}

    def slot(self, dc_id: int, size: int) -> asyncio.Semaphore:
        pool = self._large if size >= LARGE_TRANSFER_BYTES else self._small
        limit = self._large_limit if size >= LARGE_TRANSFER_BYTES else self._small_limit
        if dc_id not in pool:
            pool[dc_id] = asyncio.Semaphore(limit)
        return pool[dc_id]


Progress = Callable[[int, int], Any]


@dataclass
class DownloadPlan:
    """One download, with everything needed to restart or refresh it."""

    target: Path
    chat_id: int | None = None
    message_id: int | None = None
    size: int = 0
    dc_id: int = 0
    offset: int = 0
    limit: int | None = None
    resume: bool = True
    part_size: int = DEFAULT_PART_SIZE

    @property
    def part_file(self) -> Path:
        return self.target.with_suffix(self.target.suffix + ".part")


class _Throttle:
    """Emit progress at most once a second or once a megabyte."""

    def __init__(self) -> None:
        self._last_time = 0.0
        self._last_bytes = 0

    def should(self, done: int) -> bool:
        now = time.monotonic()
        if (
            now - self._last_time >= _PROGRESS_INTERVAL
            or done - self._last_bytes >= _PROGRESS_BYTES
        ):
            self._last_time = now
            self._last_bytes = done
            return True
        return False


async def download(
    client: Any,
    location: Any,
    plan: DownloadPlan,
    *,
    slots: TransferSlots | None = None,
    progress: Progress | None = None,
    refresh: Callable[[], Awaitable[Any]] | None = None,
) -> Path:
    """Stream *location* into `plan.target`, resuming and refreshing as needed.

    `refresh()` re-fetches the source message and returns a fresh location; it
    is called at most once, because a second `FILE_REFERENCE_EXPIRED` after a
    refresh means something other than an expired reference.
    """
    plan.target.parent.mkdir(parents=True, exist_ok=True)
    part = plan.part_file
    start = plan.offset
    if plan.resume and part.exists():
        # Round down to a whole part: resuming mid-part would ask the server
        # for an offset it will reject (`OFFSET_INVALID`).
        existing = part.stat().st_size
        start = max(start, existing - (existing % plan.part_size))
        with contextlib.suppress(OSError):
            os.truncate(part, start)

    slot = (slots or TransferSlots()).slot(plan.dc_id, plan.size)
    done = start
    throttle = _Throttle()
    refreshed = False

    async with slot:
        while True:
            try:
                with open(part, "ab") as handle:
                    since_sync = 0
                    async for chunk in client.iter_download(
                        location,
                        offset=start,
                        request_size=plan.part_size,
                        limit=plan.limit,
                    ):
                        handle.write(chunk)
                        done += len(chunk)
                        since_sync += len(chunk)
                        if since_sync >= _FSYNC_EVERY:
                            handle.flush()
                            os.fsync(handle.fileno())
                            since_sync = 0
                        if progress is not None and throttle.should(done):
                            progress(done, plan.size)
                    handle.flush()
                    os.fsync(handle.fileno())
                break
            except Exception as exc:
                if refreshed or refresh is None or not _is_file_reference_error(exc):
                    raise
                refreshed = True
                log.info("refreshing an expired file reference and retrying once")
                location = await refresh()
                start = done

    if plan.size and done < plan.size:
        raise TlgrError(
            f"the download stopped at {done} of {plan.size} bytes; the file is incomplete"
        )
    os.replace(part, plan.target)
    if progress is not None:
        progress(done, plan.size or done)
    return plan.target


def _is_file_reference_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "FileReference" in name or "FilerefUpgradeNeeded" in name:
        return True
    return "FILE_REFERENCE_" in str(exc).upper()


def file_reference_index(message: str) -> int | None:
    """`FILE_REFERENCE_3_EXPIRED` → 3, so an album refreshes only that item."""
    import re

    match = re.search(r"FILE_REFERENCE_(\d+)_EXPIRED", message.upper())
    return int(match.group(1)) if match else None


@dataclass
class UploadPlan:
    source: Path
    file_name: str = ""
    part_size: int = 0
    parts_in_flight: int = 4
    max_parts: int = 4000
    file_id: int = 0
    attributes: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.file_name:
            self.file_name = self.source.name
        if not self.part_size:
            self.part_size = part_size_for(self.size)
        if not self.file_id:
            self.file_id = int.from_bytes(os.urandom(8), "big", signed=True)

    @property
    def size(self) -> int:
        return self.source.stat().st_size if self.source.exists() else 0

    @property
    def total_parts(self) -> int:
        size = self.size
        return max(1, -(-size // self.part_size))

    @property
    def big(self) -> bool:
        return self.size > _BIG_UPLOAD


async def upload(
    client: Any,
    plan: UploadPlan,
    *,
    progress: Progress | None = None,
    max_parts_allowed: int = 4000,
) -> Any:
    """Upload a file with a sliding window of parts in flight (checklist 14).

    The pre-flight check matters more than the speed: a file that cannot fit
    in `upload_max_fileparts_*` is a USAGE error *before* a byte is sent,
    rather than a failure after twenty minutes of upload.
    """
    from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
    from telethon.tl.types import InputFile, InputFileBig

    if not plan.source.exists():
        raise UsageError(f"{plan.source} does not exist", field="path")
    total = plan.total_parts
    if total > max_parts_allowed:
        raise UsageError(
            f"{plan.source.name} needs {total} parts and this account may upload "
            f"{max_parts_allowed}; the file is too large"
        )

    md5 = hashlib.md5() if not plan.big and plan.size <= _LARGE_FILE else None
    window = asyncio.Semaphore(max(1, plan.parts_in_flight))
    sent = 0
    throttle = _Throttle()
    lock = asyncio.Lock()

    async def send_part(index: int, chunk: bytes) -> None:
        nonlocal sent
        async with window:
            if plan.big:
                request: Any = SaveBigFilePartRequest(
                    file_id=plan.file_id,
                    file_part=index,
                    file_total_parts=total,
                    bytes=chunk,
                )
            else:
                request = SaveFilePartRequest(file_id=plan.file_id, file_part=index, bytes=chunk)
            await client(request)
            async with lock:
                sent += len(chunk)
                if progress is not None and throttle.should(sent):
                    progress(sent, plan.size)

    tasks: list[asyncio.Task[None]] = []
    with open(plan.source, "rb") as handle:
        index = 0
        while True:
            chunk = handle.read(plan.part_size)
            if not chunk:
                break
            if md5 is not None:
                md5.update(chunk)
            tasks.append(asyncio.create_task(send_part(index, chunk)))
            index += 1
            # Keep the window bounded in *memory* too: without this the whole
            # file would be read into a list of pending tasks.
            if len(tasks) >= plan.parts_in_flight * 2:
                await asyncio.gather(*tasks)
                tasks = []
    if tasks:
        await asyncio.gather(*tasks)

    if progress is not None:
        progress(plan.size, plan.size)
    if plan.big:
        return InputFileBig(id=plan.file_id, parts=total, name=plan.file_name)
    return InputFile(
        id=plan.file_id,
        parts=total,
        name=plan.file_name,
        md5_checksum=md5.hexdigest() if md5 else "",
    )


# ---------------------------------------------------------------------------
# Attribute inference
# ---------------------------------------------------------------------------


def infer_attributes(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Best-effort media metadata, and the warnings for what could not be read.

    A video sent with `duration=0, w=1, h=1` renders in every Telegram client
    as a 1×1 black rectangle. That is worse than a plain document, so when
    nothing can read the file the caller is *told* — the warning is the
    feature, not the fallback.
    """
    facts: dict[str, Any] = {}
    warnings: list[str] = []
    suffix = path.suffix.lower()

    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        try:
            from PIL import Image

            with Image.open(path) as image:
                facts["width"], facts["height"] = image.size
        except Exception:
            warnings.append(
                "install the [media] extra (pillow) for image dimensions; sending without them"
            )
        return facts, warnings

    if suffix in (".mp4", ".mkv", ".mov", ".webm", ".mp3", ".m4a", ".ogg", ".oga", ".opus"):
        probed = _ffprobe(path)
        if probed:
            facts.update(probed)
            return facts, warnings
        try:
            from hachoir.metadata import extractMetadata
            from hachoir.parser import createParser

            parser = createParser(str(path))
            if parser is not None:
                with parser:
                    meta = extractMetadata(parser)
                if meta is not None:
                    if meta.has("duration"):
                        facts["duration"] = int(meta.get("duration").total_seconds())
                    if meta.has("width"):
                        facts["width"] = int(meta.get("width"))
                    if meta.has("height"):
                        facts["height"] = int(meta.get("height"))
        except Exception:
            pass
        if not facts:
            warnings.append(
                "no ffprobe and no [media] extra: this file is sent without "
                "duration or dimensions, which some clients render as a 1x1 "
                "placeholder — pass --duration/--width/--height, or install them"
            )
    return facts, warnings


def _ffprobe(path: Path) -> dict[str, Any]:
    import json
    import shutil
    import subprocess

    binary = shutil.which("ffprobe")
    if not binary:
        return {}
    try:
        output = subprocess.run(
            [
                binary,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        data = json.loads(output.stdout or b"{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    facts: dict[str, Any] = {}
    duration = (data.get("format") or {}).get("duration")
    if duration:
        with contextlib.suppress(ValueError, TypeError):
            facts["duration"] = int(float(duration))
    for stream in data.get("streams") or ():
        if stream.get("codec_type") == "video":
            facts["width"] = int(stream.get("width") or 0) or None
            facts["height"] = int(stream.get("height") or 0) or None
            break
    return {k: v for k, v in facts.items() if v}


async def iter_progress_frames(
    transfer: AsyncIterator[tuple[int, int]],
) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover - wired to ops in stage C
    """Turn `(done, total)` updates into the `progress` frames of §5.3."""
    started = time.monotonic()
    async for done, total in transfer:
        elapsed = max(1e-6, time.monotonic() - started)
        yield {
            "type": "progress",
            "done": done,
            "total": total,
            "rate_bps": int(done / elapsed),
        }
