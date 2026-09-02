"""`flock`-based single ownership, for the daemon and for each session file.

Two different things are guarded and they fail differently:

* **the daemon** — one process per `~/.tlgr`. A second one exits *0* with a
  message rather than 1, because under launchd a non-zero exit with
  `KeepAlive.SuccessfulExit=false` means "respawn me", and v1's exit 1 on
  "already running" produced an infinite respawn loop (COR-39).
* **a session file** — one process per `accounts/<alias>/session.session`.
  Two Telethon clients on one session file is how you earn
  `AUTH_KEY_DUPLICATED`, which invalidates the session server-side; the
  daemon holds this lock for the life of the account and the CLI never opens
  a session file at all.

`flock` is per open file description, so the lock is released by closing the
fd and, crucially, is *not* inherited across a `Popen`. That is what lets the
autostart probe hold a lock while spawning a child that takes a different one.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from types import TracebackType

__all__ = ["FileLock", "LockBusy"]


class LockBusy(Exception):
    """Somebody else holds the lock. Carries their pid when we can read it."""

    def __init__(self, path: Path, pid: int | None = None) -> None:
        self.path = path
        self.pid = pid
        holder = f" (held by pid {pid})" if pid else ""
        super().__init__(f"{path} is locked by another process{holder}")


class FileLock:
    """An exclusive advisory lock on a file, held until `release()`.

    The holder writes its pid into the file so that a refusal can name it —
    "the lock is busy" is not an actionable message, "pid 8123 holds it" is.
    """

    def __init__(self, path: Path, *, mode: int = 0o600) -> None:
        self.path = Path(path)
        self.mode = mode
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def holder_pid(self) -> int | None:
        try:
            content = self.path.read_text().strip()
        except OSError:
            return None
        try:
            return int(content)
        except ValueError:
            return None

    def acquire(self, *, blocking: bool = False) -> None:
        """Take the lock, raising `LockBusy` when someone else has it."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, self.mode)
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            pid = self.holder_pid()
            os.close(fd)
            raise LockBusy(self.path, pid) from exc
        self._fd = fd
        self.write_pid()

    def write_pid(self) -> None:
        """(Re)write our pid into the locked file.

        Called again after `daemonize()`: the pid written before the fork
        belongs to a process that no longer exists, and a stale pid in a lock
        file is worse than no pid at all.
        """
        if self._fd is None:
            return
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, f"{os.getpid()}\n".encode())
        os.fsync(self._fd)

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
