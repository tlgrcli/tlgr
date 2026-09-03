"""Reading a media file's own metadata, without sending it anywhere.

Lives in `core/` rather than in the daemon's file pipeline because the send
path needs it too and `ops/` may not import `daemon/` (§2.2). It is also the
one place that knows the difference between "this file has no duration" and
"nothing on this machine can read a duration": a video sent with
`duration=0, w=1, h=1` renders in every Telegram client as a 1x1 black
rectangle, which is worse than sending it as a plain document.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

__all__ = ["infer_attributes", "probe", "probe_warnings"]


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


def probe(path: Path) -> dict[str, Any]:
    """The facts only, for a caller that reports its own warnings."""
    return infer_attributes(path)[0]


def probe_warnings(path: Path, *, voice: bool = False, video_note: bool = False) -> list[str]:
    """The warnings only.

    A voice note or a round video with no duration is the case that actually
    hurts — the client draws an empty waveform and the message looks broken —
    so it is called out separately rather than folded into the generic
    "install ffprobe" line.
    """
    facts, warnings = infer_attributes(path)
    if (voice or video_note) and not facts.get("duration"):
        warnings.append(
            "no duration could be read for this file; a voice note or round video "
            "without one shows an empty waveform in every client"
        )
    return warnings
