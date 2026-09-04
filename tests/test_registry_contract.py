"""The contract every registered operation satisfies, for free.

This is the reason the registry pays for itself: one new `OperationSpec`
arrives with these tests already written, and the failure modes that produced
COR-17, COR-33, SEC-04 and UX-01 cannot be reintroduced one command at a time.
"""

from __future__ import annotations

import importlib
import shlex
from pathlib import Path

import msgspec
import pytest
from click.testing import CliRunner

import tlgr.ops  # importing it populates the registry
from tlgr.cli import cli
from tlgr.cli.gen import build_click_tree
from tlgr.ops._spec import OperationSpec
from tlgr.registry import ALIASES, REGISTRY, canonical, lint, policy_allows
from tlgr.schema import build_schema

SPECS = sorted(REGISTRY.values(), key=lambda s: s.id)
IDS = [spec.id for spec in SPECS]


@pytest.fixture
def runner():
    return CliRunner()


def _walk(root, path):
    node = root
    for token in path:
        node = node.commands.get(token) if hasattr(node, "commands") else None
        if node is None:
            return None
    return node


def test_registry_is_not_empty():
    assert SPECS, "the registry has no operations; the ops package did not import"


def test_lint_is_clean():
    assert lint() == []


def test_only_the_schema_document_is_json_only():
    """`json-only` prints an envelope where a person asked for a table.

    Right for exactly one operation — a JSON Schema document has no table
    shape — and wrong for every other, most of all for the ones an operator
    reads when something is broken.
    """
    assert {spec.id for spec in SPECS if "json-only" in spec.tags} == {"agent.schema"}


class TestOperationContract:
    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_example_validates(self, spec):
        """The documented example decodes into the declared response type."""
        if spec.response in (dict, None):
            assert isinstance(spec.example, dict)
            return
        msgspec.convert(msgspec.to_builtins(spec.example), type=spec.response)

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_example_args_start_with_the_command(self, spec):
        """The example must invoke this op — under any of its paths."""
        heads = {name.replace(".", " ") for name in spec.names}
        assert any(spec.example_args.startswith(head) for head in heads), spec.example_args

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_example_args_parse(self, spec, runner):
        """`example_args` must actually be an invocation of this command."""
        result = runner.invoke(cli, [*shlex.split(spec.example_args), "--help"])
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_cli_command_exists(self, spec):
        assert _walk(cli, spec.path) is not None

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_aliases_and_legacy_paths_resolve(self, spec):
        for name in spec.names:
            assert canonical(name) == spec.id
            assert canonical(name.replace(".", " ")) == spec.id
        for alias in spec.aliases:
            assert _walk(cli, tuple(alias.split("."))) is not None, alias
        for legacy in spec.legacy_paths:
            assert _walk(cli, tuple(legacy.replace(".", " ").split())) is not None, legacy

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_globals_attached_after_the_arguments(self, spec, runner):
        """UX-01: the flags work at the end of the line, not only at the front."""
        command = _walk(cli, spec.path)
        flags = {opt for param in command.params for opt in param.opts}
        assert {"--json", "--plain", "-a", "--results-only", "--select"} <= flags

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_dry_run_is_accepted_everywhere(self, spec):
        command = _walk(cli, spec.path)
        flags = {opt for param in command.params for opt in param.opts}
        assert "--dry-run" in flags
        # `-n` belongs to --limit on a paginated command, so --dry-run has no
        # short form there and the COR-45 ambiguity cannot arise.
        short = {opt for param in command.params for opt in param.opts if opt == "-n"}
        if spec.paginated is not None:
            assert short and "--limit" in flags

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_dry_run_never_reaches_a_mutating_impl(self, spec, runner):
        """COR-17: the short-circuit is in `run_op`, above every implementation.

        Asserted through the CLI rather than by patching the spec (which is
        frozen, deliberately): no dispatcher is installed in this test
        process, so an operation that actually tried to run would fail with a
        daemon error instead of reporting the dry run.
        """
        if not spec.mutating:
            return
        result = runner.invoke(
            cli, [*shlex.split(spec.example_args), "--dry-run", "--yes", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert '"dry_run": true' in result.output.replace(" ", " ")
        assert f'"would": "{spec.id}"' in result.output

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_policy_blocks_by_canonical_id(self, spec):
        """An allowlist of other ops blocks this one, however it is spelled."""
        other = "some.other-op"
        assert not policy_allows(other, spec.id)
        for name in spec.names:
            assert not policy_allows(other, name)
            assert policy_allows(spec.id, name), f"{name} should be allowed by {spec.id}"

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_policy_allows_by_group(self, spec):
        assert policy_allows(spec.group, spec.id)

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_schema_generates(self, spec):
        document = build_schema()
        entry = document["ops"][spec.id]
        assert entry["summary"] == spec.summary
        assert entry["request_schema"]
        assert entry["example_response"] is not None
        for schema in (entry["request_schema"], entry.get("response_schema") or {}):
            ref = schema.get("$ref")
            if ref:
                assert ref.removeprefix("#/$defs/") in document["$defs"]

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_columns_resolve(self, spec):
        """Covered by the lint, asserted here so the failure names the op."""
        assert [p for p in lint() if p.startswith(f"{spec.id}: column")] == []

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_timeout_sane(self, spec):
        assert 5 <= spec.timeout_s <= 900


class TestModuleDiscovery:
    """`tlgr.ops` finds its operation modules instead of listing them.

    The list it replaced was the file every group PR had to edit and every
    rebase had to merge by hand. What the list was really for is checkable
    directly: a module that ships without reaching the registry is a group of
    commands that silently does not exist.
    """

    def test_discovery_finds_every_operation_module(self):
        directory = Path(tlgr.ops.__file__).parent
        on_disk = sorted(
            path.stem
            for path in directory.glob("*.py")
            if not path.stem.startswith("_")  # plumbing, not specs
        )
        assert list(tlgr.ops.op_module_names()) == on_disk

    def test_discovery_is_sorted_so_registration_order_is_stable(self):
        names = tlgr.ops.op_module_names()
        assert list(names) == sorted(names)

    def test_importing_the_package_registers_every_module(self):
        """Every discovered module's `SPEC_*` objects are in the registry."""
        for name in tlgr.ops.op_module_names():
            module = importlib.import_module(f"tlgr.ops.{name}")
            declared = {
                spec.id
                for attr in dir(module)
                if attr.startswith("SPEC_")
                and isinstance(spec := getattr(module, attr), OperationSpec)
            }
            assert declared, f"tlgr/ops/{name}.py declares no operations"
            assert declared <= set(REGISTRY), f"{name}: {sorted(declared - set(REGISTRY))}"

    def test_every_registered_op_comes_from_a_discovered_module(self):
        """Checked against the module a spec's `impl` lives in, not its noun.

        The first segment of the id was a usable proxy only while every module
        owned exactly one noun. `chat_stats` registers `chat stats`, `chat
        revenue` *and* `boost`, because they read the same statistics DC — so
        the proxy would fail on a layout that is correct. The module the
        implementation is defined in is the thing discovery actually finds.
        """
        discovered = set(tlgr.ops.op_module_names())
        for spec in SPECS:
            module = spec.impl.__module__
            assert module.startswith("tlgr.ops."), f"{spec.id} is implemented in {module}"
            assert module.rsplit(".", 1)[-1] in discovered, f"{spec.id} came from {module}"


class TestTreeShape:
    def test_no_path_is_deeper_than_three(self):
        for spec in SPECS:
            assert 2 <= len(spec.path) <= 3, spec.id

    def test_aliases_are_hidden_and_legacy_paths_are_not(self):
        """A habit keeps working; --help does not list the same command twice."""
        for spec in SPECS:
            for alias in spec.aliases:
                if alias.replace(".", " ") in spec.legacy_paths:
                    continue
                assert _walk(cli, tuple(alias.split("."))).hidden, alias
            for legacy in spec.legacy_paths:
                path = tuple(legacy.replace(".", " ").split())
                assert not _walk(cli, path).hidden, legacy

    def test_build_click_tree_is_deterministic(self):
        first = sorted(build_click_tree())
        assert first == sorted(build_click_tree())

    def test_every_alias_maps_to_a_real_op(self):
        for alias, op_id in ALIASES.items():
            assert op_id in REGISTRY, alias
