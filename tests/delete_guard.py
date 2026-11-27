"""Refuse every delete, rename or move outside the test run's own temp dirs.

On 2026-11-27 a regression test for `media storage clear --chat /` was run
against the unfixed code to watch it fail. The code joined `/` onto the
downloads root, which yields `/`, and walked and unlinked every file the
developer owned. The test's only protection was that it expected an
exception. This module is the protection that does not depend on the code
under test behaving: it is installed by `conftest.py` before any test is
collected, and every delete or move whose target is outside an allowed root
raises `DeleteOutsideTestRoots` *before* the disk is touched.

Allowed roots are pytest's own temp area (`pytest-of-<user>`, or
`--basetemp`), pytest's cache dir when it lies inside the repository (it is
created by a rename into place), and every directory or file created through
`tempfile.mkdtemp`/`tempfile.mkstemp` during the run, until it is removed
(which covers `TemporaryDirectory` and the `tlgr_home` fixture, which
deliberately lives outside `tmp_path` because of the macOS socket path
limit).

Guarded: `os.remove`, `os.unlink`, `os.rmdir`, `os.rename`, `os.replace`,
`shutil.rmtree`, and `Path.unlink`, `Path.rmdir`, `Path.rename`,
`Path.replace` (plus `Path.move` where it exists). `dir_fd` forms are
resolved through the descriptor's path. The refusal deliberately does not
inherit from `OSError`, so a `contextlib.suppress(OSError)` around a delete
cannot hide it, and every refusal is also recorded so the session fails even
if something catches it.

Child Python processes started by tests inherit the guard through
`tests/_guard_site/sitecustomize.py` (on `PYTHONPATH`; it finds this module
by its own location, so no other variable is needed) and the allowed roots
through `PYTEST_DELETE_GUARD_ROOTS`. Neither name starts with `TLGR_`, so
tests that strip `TLGR_*` from a child's environment keep the guard. A child
that loses the roots variable falls back to the strict default: only
pytest's temp area and the directories it creates itself.

Bytecode caches are also allowed: a `.pyc` or `.pyc.<pid>` file directly
in a `__pycache__` directory inside the repository, which pytest's
assertion rewriter writes and renames into place on a fresh checkout.

Not guarded: opening a file for writing (truncation), deletes through a
reference captured before installation (for example `from os import
unlink` at import time), and non-Python child processes.
"""

from __future__ import annotations

import contextlib
import functools
import getpass
import os
import re
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ENV_ROOTS = "PYTEST_DELETE_GUARD_ROOTS"
SITE_DIR = Path(__file__).resolve().parent / "_guard_site"
#: The repository. Bytecode caches inside it may be written (see `_is_bytecode`).
REPO = Path(__file__).resolve().parent.parent
_BYTECODE_NAME = re.compile(r"[^/]+\.pyc(\.\d+)?")

__all__ = [
    "ENV_ROOTS",
    "DeleteOutsideTestRoots",
    "allow",
    "expect_refusals",
    "install",
    "original",
    "refusals",
    "roots",
]


class DeleteOutsideTestRoots(Exception):
    """A test tried to delete or move something outside the run's temp roots.

    Not an `OSError` on purpose: `contextlib.suppress(OSError)` must not be
    able to swallow it.
    """


_lock = threading.RLock()
# Resolved root paths as strings. Lookups walk the target's parents and test
# set membership, so a check costs the path's depth, not the number of roots.
# Roots from `tempfile.mkdtemp`/`mkstemp` are temporary: they are forgotten
# once they are removed, which only ever takes permission away. Without that
# a full run accumulated tens of thousands of roots and slowed to a crawl.
_permanent: set[str] = set()
_temporary: set[str] = set()
_state = threading.local()
_originals: dict[str, Callable[..., Any]] = {}
#: Every refusal of the run, in order. `conftest.py` fails the session when
#: this is not empty at the end.
refusals: list[str] = []


def _real(path: str) -> Path:
    return Path(os.path.realpath(path))


def _publish() -> None:
    """Hand the current roots to child processes. Call with `_lock` held."""
    os.environ[ENV_ROOTS] = os.pathsep.join(sorted(_permanent | _temporary))


def allow(path: str | bytes | os.PathLike[Any], *, temporary: bool = False) -> None:
    """Allow deletes at or below *path* (resolved).

    A *temporary* root is dropped again once the path itself is removed.
    """
    resolved = str(_real(os.fsdecode(os.fspath(path))))
    with _lock:
        if resolved in _permanent or resolved in _temporary:
            return
        (_temporary if temporary else _permanent).add(resolved)
        _publish()


def _forget_removed(target: Path) -> None:
    """Drop temporary roots at or below *target* that no longer exist."""
    prefix = str(target)
    with _lock:
        gone = {
            r
            for r in _temporary
            if (r == prefix or r.startswith(prefix + os.sep)) and not os.path.lexists(r)
        }
        if gone:
            _temporary.difference_update(gone)
            _publish()


def _is_bytecode(target: Path) -> bool:
    """A `.pyc` (or pytest's `.pyc.<pid>` staging file) in a `__pycache__`
    directory inside the repository.

    pytest's assertion rewriter writes the cache for every test module as
    `<name>.pyc.<pid>` and `os.replace`s it into place, so a fresh checkout
    (every CI run) needs this. Nothing else matches: not another file in
    `__pycache__`, not a `__pycache__` outside the repository.
    """
    return (
        target.parent.name == "__pycache__"
        and _BYTECODE_NAME.fullmatch(target.name) is not None
        and target.is_relative_to(REPO)
    )


def _allowed(target: Path) -> bool:
    if _is_bytecode(target):
        return True
    current = str(target)
    with _lock:
        while True:
            if current in _permanent or current in _temporary:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent


def roots() -> tuple[Path, ...]:
    with _lock:
        return tuple(Path(r) for r in sorted(_permanent | _temporary))


def original(name: str) -> Callable[..., Any]:
    """The unguarded function, for the guard's own tests to clean up with."""
    return _originals[name]


@contextlib.contextmanager
def expect_refusals() -> Iterator[list[str]]:
    """Collect the refusals made inside the block and drop them from the run.

    Only for tests of the guard itself, which refuse on purpose.
    """
    with _lock:
        start = len(refusals)
    seen: list[str] = []
    try:
        yield seen
    finally:
        with _lock:
            seen.extend(refusals[start:])
            del refusals[start:]


def _fd_path(fd: int) -> str | None:
    """The directory an open descriptor refers to, or None when unknown."""
    try:
        if sys.platform == "darwin":
            import fcntl

            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(raw.split(b"\0", 1)[0])
        if os.path.isdir("/proc/self/fd"):
            return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
    return None


def _target(path: Any, *, dir_fd: int | None, follow_last: bool) -> Path | None:
    """The real location a call on *path* would act on.

    The parent is resolved (symlinks and `..` included, in the kernel's order)
    and the last component is kept as written, because unlinking or renaming
    a symlink acts on the link itself, not on what it points to.
    """
    raw = os.fsdecode(os.fspath(path))
    if not os.path.isabs(raw):
        base = os.getcwd() if dir_fd is None else _fd_path(dir_fd)
        if base is None:
            return None
        raw = os.path.join(base, raw)
    head, tail = os.path.split(raw)
    if follow_last or tail in ("", ".", ".."):
        return _real(raw)
    return _real(head) / tail


def _check(
    op: str, path: Any, *, dir_fd: int | None = None, follow_last: bool = False
) -> Path | None:
    """Raise unless *path* is inside an allowed root; return where it points."""
    if dir_fd is not None and getattr(_state, "rmtree_depth", 0):
        # Inside a `shutil.rmtree` whose top directory was already checked:
        # its fd-relative unlinks never leave that tree (it does not follow
        # symlinks).
        return None
    target = _target(path, dir_fd=dir_fd, follow_last=follow_last)
    if target is not None and _allowed(target):
        return target
    shown = target if target is not None else f"{os.fsdecode(os.fspath(path))} (dir_fd={dir_fd})"
    message = (
        f"{op}({shown}) refused by tests/delete_guard.py: the target is outside the test "
        f"run's temp roots {[str(r) for r in roots()]}"
    )
    with _lock:
        refusals.append(message)
    raise DeleteOutsideTestRoots(message)


def _guard_one_path(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def guarded(path: Any, *args: Any, dir_fd: int | None = None, **kwargs: Any) -> Any:
        target = _check(name, path, dir_fd=dir_fd)
        result = func(path, *args, dir_fd=dir_fd, **kwargs)
        if target is not None:
            _forget_removed(target)
        return result

    return guarded


def _guard_two_paths(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def guarded(
        src: Any,
        dst: Any,
        *args: Any,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        **kwargs: Any,
    ) -> Any:
        _check(name, src, dir_fd=src_dir_fd)
        _check(name, dst, dir_fd=dst_dir_fd)
        # A rename does not forget a temporary root: the owner may still
        # clean up the old name (pytest renames its cache dir into place,
        # then calls rmtree on the old name).
        return func(src, dst, *args, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, **kwargs)

    return guarded


def _guard_rmtree(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def guarded(path: Any, *args: Any, **kwargs: Any) -> Any:
        target = _check("shutil.rmtree", path, dir_fd=kwargs.get("dir_fd"))
        depth = getattr(_state, "rmtree_depth", 0)
        _state.rmtree_depth = depth + 1
        try:
            return func(path, *args, **kwargs)
        finally:
            _state.rmtree_depth = depth
            if target is not None:
                # Also after a failure or `ignore_errors`: only roots that
                # are actually gone are forgotten.
                _forget_removed(target)

    return guarded


def _guard_path_method(name: str, func: Callable[..., Any], *, two: bool) -> Callable[..., Any]:
    @functools.wraps(func)
    def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
        target = _check(name, self)
        if two and args:
            _check(name, args[0])
        elif two and "target" in kwargs:
            _check(name, kwargs["target"])
        result = func(self, *args, **kwargs)
        if not two and target is not None:
            _forget_removed(target)  # see `_guard_two_paths` for renames
        return result

    return guarded


def _register_mkdtemp(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def registering(*args: Any, **kwargs: Any) -> Any:
        created = func(*args, **kwargs)
        allow(created, temporary=True)
        return created

    return registering


def _register_mkstemp(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def registering(*args: Any, **kwargs: Any) -> Any:
        fd, created = func(*args, **kwargs)
        allow(created, temporary=True)
        return fd, created

    return registering


def _pytest_temp_root() -> Path:
    """Where pytest keeps `tmp_path` when `--basetemp` is not given."""
    from_env = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    temproot = Path(from_env or tempfile.gettempdir()).resolve()
    try:
        user = getpass.getuser()
    except (ImportError, KeyError, OSError):
        user = "unknown"
    return temproot / f"pytest-of-{user}"


def install() -> None:
    """Patch the delete and move functions. Idempotent."""
    with _lock:
        if _originals:
            return
        for entry in os.environ.get(ENV_ROOTS, "").split(os.pathsep):
            if entry:
                allow(entry)
        allow(_pytest_temp_root())

        for name in ("remove", "unlink", "rmdir"):
            func = getattr(os, name)
            _originals[name] = func
            setattr(os, name, _guard_one_path(f"os.{name}", func))
        for name in ("rename", "replace"):
            func = getattr(os, name)
            _originals[name] = func
            setattr(os, name, _guard_two_paths(f"os.{name}", func))
        _originals["rmtree"] = shutil.rmtree
        shutil.rmtree = _guard_rmtree(shutil.rmtree)  # type: ignore[assignment]

        for name, two in (
            ("unlink", False),
            ("rmdir", False),
            ("rename", True),
            ("replace", True),
            ("move", True),
        ):
            func = getattr(Path, name, None)
            if func is None:
                continue
            _originals[f"Path.{name}"] = func
            setattr(Path, name, _guard_path_method(f"Path.{name}", func, two=two))

        _originals["mkdtemp"] = tempfile.mkdtemp
        tempfile.mkdtemp = _register_mkdtemp(tempfile.mkdtemp)  # type: ignore[assignment]
        _originals["mkstemp"] = tempfile.mkstemp
        tempfile.mkstemp = _register_mkstemp(tempfile.mkstemp)  # type: ignore[assignment]

        # Child Python processes load the same guard.
        site = str(SITE_DIR)
        existing = os.environ.get("PYTHONPATH", "")
        if site not in existing.split(os.pathsep):
            os.environ["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
