"""Takeout (data export) shapes.

A takeout session is a *mode*, not a request: once `account.initTakeoutSession`
returns an id, every subsequent call — `upload.getFile` included — has to be
wrapped in `invokeWithTakeout`, and `file_max_size` can never be changed. The
model therefore records the scope it was opened with, because a caller that
forgets it will simply get nothing back and no error.
"""

from __future__ import annotations

from tlgr.models.base import Model

__all__ = [
    "ExportResult",
    "ExportedFile",
    "MessageRange",
    "TakeoutSession",
    "TakeoutStatus",
]


class TakeoutSession(Model):
    takeout_id: int | None = None
    scope: list[str] = []
    started_at: str | None = None
    expires: str | None = None
    max_file_size: int = 0
    #: TAKEOUT_INIT_DELAY_X: another logged-in session has to approve the
    #: export first (24 h if there is none). Reported, never slept through
    #: silently.
    approval_required: bool = False
    retry_after: int | None = None
    already: bool = False


class MessageRange(Model):
    min_id: int = 0
    max_id: int = 0


class TakeoutStatus(Model):
    active: bool = False
    takeout_id: int | None = None
    started_at: str | None = None
    scope: list[str] = []
    max_file_size: int = 0
    ranges: list[MessageRange] = []


class ExportedFile(Model):
    path: str
    kind: str = ""
    bytes: int = 0


class ExportResult(Model):
    written: int = 0
    files: list[ExportedFile] = []
    out: str = ""
    finished: bool = False
    takeout_id: int | None = None
    success: bool = True
    skipped: list[str] = []
