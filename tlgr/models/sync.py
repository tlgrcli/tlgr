"""Update-state shapes: pts/qts/seq, per-channel boxes, and difference runs.

`sync` is deliberately a different noun from `catchup`. `chat catchup` is the
unread digest a human reads; `sync catch-up` is `updates.getDifference`, the
plumbing that decides whether an event ever existed for the daemon at all.
Confusing them is how v1 ended up with an idle timeout that guaranteed a
permanent sync hole and a `catchup` command that could not close it.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "BackfillPage",
    "CatchUpResult",
    "ChannelState",
    "DifferenceResult",
    "ResetResult",
    "SyncStatus",
]


class ChannelState(Model):
    chat_id: int
    pts: int = 0
    #: False means catch-up will silently *skip* this channel: Telethon needs
    #: an access hash in the session to call getChannelDifference at all.
    access_hash_known: bool = False
    last_difference_at: str | None = None
    title: str | None = None


class SyncStatus(Model):
    account: str = ""
    pts: int | None = None
    qts: int | None = None
    seq: int | None = None
    date: str | None = None
    date_unix: int | None = None
    unread_count: int | None = None
    server_pts: int | None = None
    server_seq: int | None = None
    behind_pts: int | None = None
    behind_seconds: int | None = None
    phase: str = "unknown"
    getting_difference: bool = False
    channels: list[ChannelState] = []
    last_update_at: str | None = None
    no_updates_for_seconds: int | None = None


class CatchUpResult(Model):
    account: str = ""
    events_replayed: int = 0
    pts_before: int | None = None
    pts_after: int | None = None
    duration_ms: int = 0
    too_long: bool = False
    timed_out: bool = False


class DifferenceResult(Model):
    """One `updates.getDifference` / `getChannelDifference` run, reported raw.

    Without `--apply` this is a *probe*: it does not advance the stored pts,
    so running it cannot create the gap it was meant to diagnose.
    """

    kind: str = "common"
    final: bool = True
    new_pts: int | None = None
    new_qts: int | None = None
    new_seq: int | None = None
    new_date: str | None = None
    messages: int = 0
    other_updates: int = 0
    users: int = 0
    chats: int = 0
    timeout: int | None = None
    too_long: bool = False
    applied: bool = False
    dry_run: bool = False
    requests: list[dict[str, Any]] = []


class ResetResult(Model):
    account: str = ""
    reset: bool = False
    pts_before: int | None = None
    pts_after: int | None = None
    channels_reset: list[int] = []
    dry_run: bool = False


class BackfillPage(Model):
    """The extra field a plain `Page[Message]` cannot carry: what was missing."""

    fetched: int = 0
    missing_ids: list[int] = []
