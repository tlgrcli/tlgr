"""The `~/.tlgr` layout, in one module.

Every path tlgr writes is built here, and every one of them is built from a
*validated* alias. SEC-02 was the absence of that: an alias went straight into
`accounts_dir / alias`, so `-a ../../etc` addressed a directory outside the
tree, and a read path called `mkdir(parents=True)` on the way, so merely
asking about an account created one.

Two rules follow from that and are enforced here rather than remembered at
each call site:

* `validate_alias()` is called before an alias becomes part of a path;
* a *read* path never creates anything. Only the handful of functions whose
  name says so (`ensure_*`) make a directory.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
from pathlib import Path

from tlgr.core.errors import ConfigurationError, UsageError

__all__ = [
    "ALIAS_RE",
    "PRODUCTION_MARKER",
    "TlgrPaths",
    "audit_permissions",
    "default_base",
    "is_production_home",
    "refuse_production_home",
    "secure_session_files",
    "validate_alias",
    "write_private",
]

#: The one alias grammar. Anything outside it never reaches a path join.
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_HOME_ENV = "TLGR_HOME"


def default_base() -> Path:
    """`$TLGR_HOME` when set, else `~/.tlgr`.

    Read at call time rather than captured at import so a test (and a user
    with two profiles) can point the whole tree somewhere else.
    """
    override = os.environ.get(_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tlgr"


#: A home directory carrying this marker belongs to a live deployment.
PRODUCTION_MARKER = ".production"
_ALLOW_PRODUCTION_ENV = "TLGR_ALLOW_PRODUCTION_HOME"


def is_production_home(base: Path) -> bool:
    """Is *base* marked as a live deployment?"""
    try:
        return (base / PRODUCTION_MARKER).exists()
    except OSError:  # pragma: no cover - an unreadable home is not ours to judge
        return False


def refuse_production_home(base: Path) -> None:
    """Refuse to operate on a home marked as production unless told to.

    A development checkout that resolves to the same `~/.tlgr` as the
    installed, running tlgr is a live deploy by accident: on 2026-09-03 a dev
    daemon started from a worktree bound the production socket, held the
    production session files (``database is locked``) and served a registry
    without the message routes the running agent needed. `TLGR_HOME` already
    lets a dev point elsewhere; this makes forgetting to set it fail fast
    instead of silently taking over. Touch ``<home>/.production`` on the
    deployment; set ``TLGR_ALLOW_PRODUCTION_HOME=1`` only from the installed
    tlgr that is meant to run there.
    """
    if os.environ.get(_ALLOW_PRODUCTION_ENV, "").strip() == "1":
        return
    if (base / PRODUCTION_MARKER).exists():
        raise ConfigurationError(
            f"{base} is marked as a production home ({PRODUCTION_MARKER} present). "
            "Set TLGR_HOME to a separate directory for development, or "
            f"{_ALLOW_PRODUCTION_ENV}=1 when this really is the deployed tlgr."
        )


def validate_alias(alias: str) -> str:
    """Return *alias* if it can safely become a path segment, else raise.

    Rejects the empty string, anything with a separator, `.`/`..`, and
    anything over 64 characters — before the value is joined onto a path,
    which is the only point at which the check is worth anything.
    """
    if not isinstance(alias, str) or not ALIAS_RE.match(alias):
        raise UsageError(
            f"invalid account alias {alias!r}: use 1-64 characters from A-Z, a-z, 0-9, '_' and '-'",
            field="account",
        )
    return alias


def write_private(path: Path, data: bytes | str, *, mode: int = 0o600) -> None:
    """Write *data* to *path* so that it is never briefly world-readable.

    The write goes to a temporary file in the same directory, is chmod-ed
    before it is named, and is then atomically renamed into place. Writing
    the file first and fixing the mode afterwards (SEC-07) leaves a window in
    which a session, a token or a config with an `api_hash` in it is readable
    by every process on the machine.
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def secure_session_files(session_path: Path) -> None:
    """chmod 600 the session db and every sqlite sibling Telethon creates.

    A session file is a complete account credential. v1 chmod-ed the one
    imported by `account import` and left the one written by `account add`
    at whatever the umask allowed — 0644 on a default Ubuntu box, which is
    every other user on the machine holding the account.

    Telethon appends `.session` and creates `-journal`/`-wal`/`-shm`
    siblings at runtime, so the glob is the point: securing only the file
    whose name we know leaves the write-ahead log readable.
    """
    parent = session_path.parent
    if not parent.exists():
        return
    for path in parent.glob(session_path.name + "*"):
        with contextlib.suppress(OSError):
            path.chmod(0o600)


class TlgrPaths:
    """Every path under one base directory (§10.1)."""

    __slots__ = ("base",)

    def __init__(self, base: Path | str | None = None) -> None:
        self.base = Path(base) if base is not None else default_base()
        refuse_production_home(self.base)

    # -- top level ---------------------------------------------------------

    @property
    def config(self) -> Path:
        return self.base / "config.toml"

    @property
    def accounts_file(self) -> Path:
        return self.base / "accounts.json"

    @property
    def jobs(self) -> Path:
        return self.base / "jobs.yaml"

    @property
    def webhook(self) -> Path:
        return self.base / "webhook.toml"

    @property
    def socket(self) -> Path:
        return self.base / "daemon.sock"

    @property
    def pid(self) -> Path:
        return self.base / "daemon.pid"

    @property
    def lock(self) -> Path:
        return self.base / "daemon.lock"

    @property
    def state(self) -> Path:
        return self.base / "daemon.state"

    @property
    def token(self) -> Path:
        return self.base / "ipc.token"

    @property
    def cursor_key(self) -> Path:
        return self.base / "cursor.key"

    @property
    def identity(self) -> Path:
        return self.base / "identity.json"

    @property
    def dead_letter(self) -> Path:
        return self.base / "dead_letter.jsonl"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "daemon.log"

    @property
    def downloads(self) -> Path:
        return self.base / "downloads"

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    @property
    def accounts(self) -> Path:
        return self.base / "accounts"

    # -- per account -------------------------------------------------------

    def account_dir(self, alias: str) -> Path:
        return self.accounts / validate_alias(alias)

    def session(self, alias: str) -> Path:
        return self.account_dir(alias) / "session"

    def session_file(self, alias: str) -> Path:
        return self.account_dir(alias) / "session.session"

    def session_lock(self, alias: str) -> Path:
        return self.account_dir(alias) / "session.lock"

    def credentials(self, alias: str) -> Path:
        return self.account_dir(alias) / "config.json"

    def peers_db(self, alias: str) -> Path:
        return self.account_dir(alias) / "peers.json"

    def flood(self, alias: str) -> Path:
        return self.account_dir(alias) / "flood.json"

    def events_state(self, alias: str) -> Path:
        return self.account_dir(alias) / "events.state"

    def outbox(self, alias: str) -> Path:
        return self.account_dir(alias) / "outbox.jsonl"

    # -- creation ----------------------------------------------------------

    def ensure_base(self) -> Path:
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.base

    def ensure_logs(self) -> Path:
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.logs

    def ensure_downloads(self) -> Path:
        self.downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.downloads

    def ensure_cache(self) -> Path:
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.cache

    def ensure_account_dir(self, alias: str) -> Path:
        directory = self.account_dir(alias)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory


#: Files whose leak is equivalent to handing over the account.
_SECRET_GLOBS = (
    "accounts/*/session*",
    "accounts/*/config.json",
    "ipc.token",
    "cursor.key",
    "webhook.toml",
)

_SECRET_MODE = 0o600
_DIR_MODE = 0o700


def audit_permissions(base: Path, *, fix: bool = True) -> list[str]:
    """Check (and optionally fix) the modes of everything secret under *base*.

    Returns the list of problems that could **not** be fixed; the daemon
    refuses to start when it is non-empty rather than opening a session file
    that the rest of the machine can read. Ownership is checked too: a file
    owned by someone else cannot be chmod-ed by us and is always a refusal.
    """
    problems: list[str] = []
    uid = os.getuid()

    def check(path: Path, want: int) -> None:
        try:
            info = path.lstat()
        except OSError:
            return
        if info.st_uid != uid:
            problems.append(f"{path} is owned by uid {info.st_uid}, not {uid}")
            return
        actual = stat.S_IMODE(info.st_mode)
        if actual & ~want:
            if not fix:
                problems.append(f"{path} is mode {actual:04o}, want {want:04o}")
                return
            try:
                os.chmod(path, want)
            except OSError as exc:
                problems.append(f"{path} is mode {actual:04o}; chmod {want:04o} failed: {exc}")

    if base.exists():
        check(base, _DIR_MODE)
    accounts = base / "accounts"
    if accounts.exists():
        check(accounts, _DIR_MODE)
        for child in sorted(accounts.iterdir()):
            if child.is_dir():
                check(child, _DIR_MODE)
    for pattern in _SECRET_GLOBS:
        for path in sorted(base.glob(pattern)):
            if path.is_file():
                check(path, _SECRET_MODE)
    return problems


def require_safe_permissions(base: Path) -> None:
    """Raise `ConfigurationError` naming the exact `chmod` when the tree is unsafe."""
    problems = audit_permissions(base, fix=True)
    if problems:
        listing = "\n  ".join(problems)
        raise ConfigurationError(
            "refusing to start: files under "
            f"{base} are not private:\n  {listing}\n"
            f"Fix with: chmod -R go-rwx {base}"
        )
