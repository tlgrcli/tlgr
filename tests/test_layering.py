"""The import lint from ARCHITECTURE §2.2.

Twenty lines of `ast` walking is what keeps the tree honest at 500 operations.
Each rule exists for a reason that is not aesthetic:

* `models/` imports nothing from tlgr, so a wire shape can be decoded by
  anything, including a test with no Telethon installed;
* `ops/` must not import click, because an operation is not a command;
* `cli/` must not import Telethon or the daemon, because `tlgr --help` has to
  be fast and has to work on a machine that never connects to Telegram.

There are no exemptions any more. `cli/legacy/` held the v1 modules that were
moved verbatim and deleted one group PR at a time; PR-12 deleted the last of
them, so every module under `tlgr/` is held to the rule.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "tlgr"


def _modules(package: str) -> list[Path]:
    return sorted(
        path for path in (ROOT / package).rglob("*.py") if "__pycache__" not in path.parts
    )


def _imports(path: Path) -> set[str]:
    """Every module name imported at any level in *path*."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def _violates(imported: str, forbidden: str) -> bool:
    return imported == forbidden or imported.startswith(f"{forbidden}.")


@pytest.mark.parametrize("path", _modules("models"), ids=lambda p: p.name)
def test_models_import_nothing_from_tlgr(path: Path):
    for name in _imports(path):
        if name.startswith("tlgr"):
            assert name.startswith("tlgr.models"), f"{path.name} imports {name}"
        assert not _violates(name, "telethon"), f"{path.name} imports {name}"
        assert not _violates(name, "click"), f"{path.name} imports {name}"


@pytest.mark.parametrize("path", _modules("ops"), ids=lambda p: p.name)
def test_ops_import_no_click_and_no_daemon_or_cli(path: Path):
    for name in _imports(path):
        for forbidden in ("click", "tlgr.cli", "tlgr.daemon"):
            assert not _violates(name, forbidden), f"{path.name} imports {name}"


@pytest.mark.parametrize("path", _modules("cli"), ids=lambda p: p.name)
def test_cli_imports_no_telethon_and_no_daemon(path: Path):
    for name in _imports(path):
        for forbidden in ("telethon", "tlgr.daemon"):
            assert not _violates(name, forbidden), f"{path.name} imports {name}"


def test_core_errors_is_the_only_module_naming_telethon_exceptions():
    """§7.1: exception classes are named in one place, and as strings.

    Naming them as strings is what lets `cli/` stay Telethon-free while
    `core/errors` still classifies every Telethon exception.
    """
    source = (ROOT / "core" / "errors.py").read_text(encoding="utf-8")
    assert "import telethon" not in source
    assert "FloodWaitError" in source


def test_registry_and_schema_do_not_import_click():
    for module in ("registry.py", "schema.py"):
        for name in _imports(ROOT / module):
            assert not _violates(name, "click"), f"{module} imports {name}"


def test_models_import_without_telethon():
    """models/ must decode a message on a machine with no Telethon at all.

    Run in a subprocess with `telethon` blocked at the meta-path level: doing
    it in-process would leave the test session holding half-unloaded modules.
    """
    program = textwrap.dedent(
        """
        import sys

        class Block:
            def find_module(self, name, path=None):
                if name.split(".")[0] == "telethon":
                    raise ImportError("telethon is not installed")
                return None

            def find_spec(self, name, path=None, target=None):
                return self.find_module(name, path)

        sys.meta_path.insert(0, Block())
        import msgspec
        from tlgr.models.message import Message
        from tlgr.models.peer import parse_peer_ref

        assert msgspec.json.decode(
            b'{"id":1,"chat_id":2,"date":"x","date_unix":0}', type=Message
        ).id == 1
        assert parse_peer_ref("@alice").value == "alice"
        assert "telethon" not in sys.modules
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(ROOT.parent),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
