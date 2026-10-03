"""Pending job actions, and the file that keeps them across a restart.

An action that is waiting (for its delay, for its pacer slot, for quiet hours
to end) is a `PendingItem`. It carries everything needed to run it again
without the Telethon event that produced it: the chat, the message, the album
it belongs to, the forum topic, and the action's own payload (the emoji, the
reply text after processors, the forward destination). A restart therefore
loses nothing: the daemon reloads the file and each item resumes with its
absolute due time, so an item due at 10:03 still runs at 10:03, and one that
fell due while the daemon was down runs as soon as its pacer allows.

The file is `accounts/<alias>/pending.json`, written with `write_private`
(a temp file, chmod 0600, fsync, rename), so a crash mid-write leaves the
previous version rather than half of a new one. It also keeps the keys of
recently finished items: an update replayed after a crash, before the update
state was saved, is recognised and not acted on twice.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger("tlgr.gateway.pending")

__all__ = ["MAX_PENDING", "PendingItem", "PendingStore"]

#: Per account. A backlog this deep means something is wrong upstream; the
#: next item is refused (and counted as an error) rather than growing the file
#: without bound.
MAX_PENDING = 10_000
#: How many finished-item keys are remembered for replay de-duplication.
RECENT_KEYS = 5_000

_VERSION = 1


@dataclass
class PendingItem:
    id: str
    job: str
    action: str
    index: int
    account: str
    chat_id: int
    msg_id: int
    received_at: float
    due_at: float
    expires_at: float | None = None
    #: `user`, `chat` (basic group), `megagroup` or `channel` (broadcast).
    peer: str = "user"
    topic_id: int | None = None
    grouped_id: int | None = None
    #: A multi-step action's progress; `reply` is `""` then `send`.
    phase: str = ""
    attempts: int = 0
    presence: str = "leave"
    quiet_hours: str | None = None
    on_takeover: str = "cancel"
    dry_run: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    #: Identifies the work, not the attempt: `job|index|chat|msg|extra`.
    key: str = ""
    last_error: str | None = None
    #: Runtime only: `pending` or `running`. Never persisted, so an item that
    #: was running when the daemon died is simply pending again.
    state: str = field(default="pending", compare=False)

    def to_json(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("state", None)
        return body

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> PendingItem:
        known = {f.name for f in fields(cls)} - {"state"}
        return cls(**{k: v for k, v in body.items() if k in known})


class PendingStore:
    """`accounts/<alias>/pending.json`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[list[PendingItem], list[str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], []
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cannot read %s, starting with an empty queue: %s", self.path, exc)
            return [], []
        if not isinstance(raw, dict):
            return [], []
        items: list[PendingItem] = []
        for body in raw.get("items") or []:
            if not isinstance(body, dict):
                continue
            try:
                items.append(PendingItem.from_json(body))
            except TypeError as exc:
                log.warning("skipping an unreadable pending item in %s: %s", self.path, exc)
        recent = [str(key) for key in raw.get("recent") or [] if isinstance(key, str)]
        return items[:MAX_PENDING], recent[-RECENT_KEYS:]

    def save(self, items: list[PendingItem], recent: list[str]) -> None:
        from tlgr.core.paths import write_private

        body = {
            "version": _VERSION,
            "items": [item.to_json() for item in items[:MAX_PENDING]],
            "recent": recent[-RECENT_KEYS:],
        }
        with contextlib.suppress(OSError):
            write_private(self.path, json.dumps(body, ensure_ascii=False, separators=(",", ":")))
