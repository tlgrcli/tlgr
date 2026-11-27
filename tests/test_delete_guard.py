"""The delete guard installed by conftest.py refuses before touching the disk.

Every test here is safe even if the guard were broken: the only file a
refused call could reach is a throwaway victim this module created in a
fresh directory under the system temp dir, outside every allowed root.
Calls aimed at `/` or `$HOME` name files that do not exist, so a broken
guard would only produce `FileNotFoundError`, which fails the test without
deleting anything.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import delete_guard
import pytest
from delete_guard import DeleteOutsideTestRoots


@pytest.fixture
def victim() -> Iterator[Path]:
    """A file outside every allowed root, removed afterwards unguarded.

    The directory is made with `os.mkdir`, not `tempfile.mkdtemp`, so the
    guard does not know it.
    """
    parent = Path(tempfile.gettempdir()).resolve() / f"tlgr-guard-probe-{uuid.uuid4().hex}"
    os.mkdir(parent, 0o700)
    target = parent / "victim.txt"
    target.write_text("keep me")
    assert not any(
        parent == root or parent.is_relative_to(root) for root in delete_guard.roots()
    ), "the probe directory must be outside the allowed roots"
    try:
        yield target
    finally:
        for leftover in parent.iterdir():
            delete_guard.original("unlink")(leftover)
        delete_guard.original("rmdir")(parent)


def _refused(call: Callable[[], object]) -> list[str]:
    with delete_guard.expect_refusals() as seen, pytest.raises(DeleteOutsideTestRoots):
        call()
    return seen


CALLS: dict[str, Callable[[Path, Path], object]] = {
    "os.unlink": lambda v, tmp: os.unlink(v),
    "os.remove": lambda v, tmp: os.remove(v),
    "Path.unlink": lambda v, tmp: v.unlink(),
    "Path.unlink(missing_ok)": lambda v, tmp: v.unlink(missing_ok=True),
    "os.rmdir": lambda v, tmp: os.rmdir(v.parent),
    "Path.rmdir": lambda v, tmp: v.parent.rmdir(),
    "shutil.rmtree": lambda v, tmp: shutil.rmtree(v.parent),
    "shutil.rmtree(ignore_errors)": lambda v, tmp: shutil.rmtree(v.parent, ignore_errors=True),
    "os.rename out": lambda v, tmp: os.rename(v, tmp / "moved"),
    "os.replace out": lambda v, tmp: os.replace(v, tmp / "moved"),
    "Path.rename out": lambda v, tmp: v.rename(tmp / "moved"),
    "Path.replace out": lambda v, tmp: v.replace(tmp / "moved"),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_delete_or_move_outside_the_roots_raises_and_the_file_survives(
    name: str, victim: Path, tmp_path: Path
) -> None:
    seen = _refused(lambda: CALLS[name](victim, tmp_path))
    assert len(seen) == 1
    assert victim.read_text() == "keep me"
    assert not (tmp_path / "moved").exists()


def test_overwriting_a_file_outside_the_roots_by_rename_is_refused(
    victim: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("replacement")
    _refused(lambda: os.replace(source, victim))
    assert victim.read_text() == "keep me"
    assert source.exists()


def test_suppress_oserror_cannot_swallow_the_refusal(victim: Path) -> None:
    def careless_cleanup() -> None:
        with contextlib.suppress(OSError):
            os.unlink(victim)

    _refused(careless_cleanup)
    assert victim.exists()


def test_a_dir_fd_relative_unlink_is_resolved_and_refused(victim: Path) -> None:
    fd = os.open(victim.parent, os.O_RDONLY)
    try:
        _refused(lambda: os.unlink(victim.name, dir_fd=fd))
    finally:
        os.close(fd)
    assert victim.exists()


def test_a_symlinked_directory_does_not_smuggle_a_delete_out(victim: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(victim.parent, target_is_directory=True)
    _refused(lambda: os.unlink(link / victim.name))
    assert victim.exists()
    link.unlink()  # the link itself lives inside tmp_path
    assert victim.exists()


@pytest.mark.parametrize("where", ["filesystem root", "home"])
def test_the_filesystem_root_and_home_are_refused(where: str) -> None:
    base = Path("/") if where == "filesystem root" else Path.home()
    missing = base / f"tlgr-guard-no-such-file-{uuid.uuid4().hex}"
    assert not missing.exists()
    _refused(lambda: os.unlink(missing))


def test_the_refusal_is_recorded_even_when_caught(victim: Path) -> None:
    # The point: even a catch-all around the delete still leaves a record.
    with delete_guard.expect_refusals() as seen, contextlib.suppress(Exception):
        os.unlink(victim)
    assert len(seen) == 1
    assert victim.exists()


def test_deletes_inside_the_roots_still_work(tmp_path: Path) -> None:
    inside = tmp_path / "a.txt"
    inside.write_text("x")
    inside.unlink()
    scratch = Path(tempfile.mkdtemp(prefix="tlgr-guard-ok-"))
    (scratch / "b.txt").write_text("y")
    shutil.rmtree(scratch)
    assert not inside.exists()
    assert not scratch.exists()


def test_a_removed_temp_dir_is_no_longer_allowed() -> None:
    """Roots do not pile up, and a recreated path does not inherit permission."""
    scratch = Path(tempfile.mkdtemp(prefix="tlgr-guard-gone-")).resolve()
    assert scratch in delete_guard.roots()
    shutil.rmtree(scratch)
    assert scratch not in delete_guard.roots()
    os.mkdir(scratch, 0o700)  # same path, but not made by mkdtemp
    try:
        (scratch / "f.txt").write_text("keep me")
        _refused(lambda: os.unlink(scratch / "f.txt"))
        assert (scratch / "f.txt").exists()
    finally:
        delete_guard.original("unlink")(scratch / "f.txt")
        delete_guard.original("rmdir")(scratch)


def test_pytests_bytecode_cache_pattern_is_allowed() -> None:
    """The assertion rewriter writes `<name>.pyc.<pid>`, then replaces."""
    cache = Path(__file__).resolve().parent / "__pycache__"
    cache.mkdir(exist_ok=True)
    final = cache / f"tlgr_guard_probe_{uuid.uuid4().hex}.cpython-0.pyc"
    staging = Path(f"{final}.{os.getpid()}")
    staging.write_bytes(b"x")
    with delete_guard.expect_refusals() as seen:
        os.replace(staging, final)
        final.unlink()
    assert seen == []
    assert not final.exists()


def test_only_bytecode_in_the_repos_pycache_is_allowed(victim: Path) -> None:
    repo_cache = Path(__file__).resolve().parent / "__pycache__"
    outside_cache = victim.parent / "__pycache__"
    cases = [
        repo_cache / "notes.txt",  # not bytecode
        repo_cache / "x.pyc.bak",  # not pytest's staging name
        outside_cache / "x.pyc",  # bytecode, but outside the repository
    ]
    for case in cases:
        _refused(lambda case=case: os.unlink(case))  # none of them exist


def test_pytests_cache_dir_pattern_is_allowed(tmp_path: Path) -> None:
    """mkdtemp, rename into place, then rmtree the old name: what pytest does."""
    with delete_guard.expect_refusals() as seen:
        staging = Path(tempfile.mkdtemp(prefix="tlgr-guard-cache-"))
        (staging / "README.md").write_text("x")
        staging.rename(tmp_path / "cache")
        shutil.rmtree(staging, ignore_errors=True)
    assert seen == []
    assert (tmp_path / "cache" / "README.md").exists()
    assert staging.resolve() not in delete_guard.roots()


def test_a_failed_removal_keeps_the_root(tmp_path: Path) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="tlgr-guard-kept-")).resolve()
    (scratch / "sub").mkdir()
    with pytest.raises(OSError):
        os.rmdir(scratch)  # not empty
    assert scratch in delete_guard.roots()
    shutil.rmtree(scratch)


@pytest.mark.parametrize("environment", ["inherited", "TLGR_ variables stripped"])
def test_a_child_python_process_is_guarded_too(victim: Path, environment: str) -> None:
    """`test_cli_startup` strips every `TLGR_*` variable from its children."""
    env = dict(os.environ)
    if environment != "inherited":
        env = {k: v for k, v in env.items() if not k.startswith("TLGR_")}
    result = subprocess.run(
        [sys.executable, "-c", f"import os; os.unlink({str(victim)!r})"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "DeleteOutsideTestRoots" in result.stderr
    assert victim.read_text() == "keep me"
