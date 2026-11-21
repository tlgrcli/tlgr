"""What `tlgr` imports and builds before it does anything.

Every invocation used to import all operation modules, lint the registry and
build all thousand Click commands, parameters included, which was close to a
second of CPU before the first byte reached the daemon. These tests pin the
shape of the fix rather than a wall-clock number, which would be flaky:

* `--version` imports no operation module, no registry and no generator;
* the command tree is built on first use, and its leaves build their
  parameters only when somebody reads them, so `--help` and a single command
  evaluate one request's annotations at most;
* the registry lint did not disappear when it left the import path: the
  daemon and `tools/gen_docs.py` still refuse a broken registry.

The import checks run in a fresh interpreter, because this test session has
long since imported everything.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import click
import pytest

ROOT = Path(__file__).resolve().parent.parent

_PROBE = textwrap.dedent(
    """
    import json, sys
    import click
    from click.testing import CliRunner
    from tlgr.cli import cli

    result = CliRunner().invoke(cli, json.loads(sys.argv[1]))
    # Read before walking the tree: walking it would load it.
    modules = sorted(m for m in sys.modules if m.startswith("tlgr."))

    def built(group):
        # Commands whose parameters exist. Read only after the tree is loaded.
        count = 0
        for command in group.commands.values():
            if isinstance(command, click.Group):
                count += built(command)
            elif hasattr(command, "_params_pending"):
                # Generated operation commands only: a hand-written command
                # such as `batch` has a few static options and no lazy build.
                count += not command._params_pending
        return count

    print(json.dumps({
        "exit_code": result.exit_code,
        "output": result.output,
        "modules": modules,
        "built": built(cli) if "tlgr.cli.gen" in modules else 0,
    }))
    """
)


def _probe(args: list[str], tmp_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(args)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**_clean_env(), "TLGR_HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _clean_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("TLGR_")}


def test_version_imports_no_operation_module(tmp_path: Path):
    from tlgr import __version__

    probe = _probe(["--version"], tmp_path)
    assert probe["exit_code"] == 0
    assert probe["output"] == f"tlgr, version {__version__}\n"
    modules = probe["modules"]
    assert "tlgr.ops" not in modules
    assert "tlgr.registry" not in modules
    assert "tlgr.cli.gen" not in modules
    assert not [m for m in modules if m.startswith("tlgr.ops.")]


def test_help_lists_every_group_without_building_a_single_command(tmp_path: Path):
    probe = _probe(["--help"], tmp_path)
    assert probe["exit_code"] == 0
    assert "message" in probe["output"]
    assert "tlgr.ops" in probe["modules"]
    assert probe["built"] == 0


def test_a_command_builds_only_its_own_parameters(tmp_path: Path):
    probe = _probe(["message", "send", "--help"], tmp_path)
    assert probe["exit_code"] == 0
    assert "--reply-to" in probe["output"]
    assert probe["built"] == 1


def test_lazy_parameters_match_and_are_built_once():
    """Reading `params` twice must not attach the global flags twice."""
    from tlgr.cli import cli

    send = cli.commands["message"].commands["send"]
    first = [p.name for p in send.params]
    assert first == [p.name for p in send.params]
    assert len(first) == len(set(first))
    assert "account" in first and "dry_run" in first
    assert isinstance(send, click.Command)


def test_the_daemon_still_refuses_a_broken_registry(tlgr_home: Path, monkeypatch):
    import tlgr.registry
    from tlgr.core.paths import PRODUCTION_MARKER
    from tlgr.daemon.main import main

    # The marker makes `main` return 10 before it starts anything, so if the
    # lint ever stops running first this fails instead of running a daemon.
    (tlgr_home / PRODUCTION_MARKER).write_text("fixture\n")
    monkeypatch.delenv("TLGR_ALLOW_PRODUCTION_HOME", raising=False)
    monkeypatch.setattr(tlgr.registry, "lint", lambda: ["fixture.op: summary is empty"])
    with pytest.raises(RuntimeError, match="operation registry lint failed"):
        main(["--base", str(tlgr_home), "--foreground"])
    assert not (tlgr_home / "daemon.lock").exists()


def test_gen_docs_still_refuses_a_broken_registry(monkeypatch):
    import importlib.util

    import tlgr.registry

    spec = importlib.util.spec_from_file_location("gen_docs_probe", ROOT / "tools" / "gen_docs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(tlgr.registry, "lint", lambda: ["fixture.op: summary is empty"])
    with pytest.raises(RuntimeError, match="operation registry lint failed"):
        module.main(["--check"])
