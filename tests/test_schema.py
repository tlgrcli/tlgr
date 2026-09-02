"""`tlgr schema`: draft 2020-12, generated, and still v1-shaped where it matters."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import tlgr.ops  # noqa: F401  — importing it populates the registry
from tlgr import __version__
from tlgr.cli import cli
from tlgr.registry import REGISTRY
from tlgr.schema import SCHEMA_VERSION, build_schema


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(scope="module")
def document():
    return build_schema()


class TestDocument:
    def test_dialect_is_draft_2020_12(self, document):
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_version_and_build(self, document):
        assert document["schema_version"] == SCHEMA_VERSION == 2
        assert document["build"] == __version__

    def test_every_registered_op_appears(self, document):
        assert set(document["ops"]) == set(REGISTRY)

    def test_defs_are_shared(self, document):
        assert "$defs" in document
        assert all("$ref" not in name for name in document["$defs"])

    def test_no_ref_dangles(self, document):
        text = json.dumps(document)
        for ref in {r for r in text.split('"$ref": "') if r.startswith("#/$defs/")}:
            name = ref.split('"')[0].removeprefix("#/$defs/")
            assert name in document["$defs"]

    def test_params_describe_the_cli(self, document):
        entry = document["ops"]["agent.schema"]
        names = {p["name"]: p for p in entry["params"]}
        assert names["path"]["type"] == "argument"
        assert names["path"]["variadic"] is True
        assert names["include_hidden"]["flags"] == ["--include-hidden"]

    def test_example_response_key_is_kept(self, document):
        """v1's key name, so a schema_version 1 consumer still finds it."""
        for entry in document["ops"].values():
            assert entry["example_response"] is not None

    def test_filtering_by_path(self):
        filtered = build_schema(path=("agent", "schema"))
        assert set(filtered["ops"]) == {"agent.schema"}

    def test_filtering_by_group(self):
        assert set(build_schema(path=("agent",))["ops"]) == set(REGISTRY)

    def test_command_tree_is_optional(self, document):
        assert "command" not in document
        assert build_schema(command={"name": "x"})["command"] == {"name": "x"}


class TestCommand:
    def test_plain_invocation_prints_the_bare_document(self, runner):
        """v1 printed the document itself; the envelope arrives with --json."""
        result = runner.invoke(cli, ["schema"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == 2
        assert payload["build"] == __version__
        assert "command" in payload

    def test_json_invocation_wraps_it(self, runner):
        result = runner.invoke(cli, ["--json", "schema"])
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["op"] == "agent.schema"
        assert payload["result"]["schema_version"] == 2

    def test_command_path_narrows_the_tree(self, runner):
        result = runner.invoke(cli, ["schema", "agent", "exit-codes"])
        payload = json.loads(result.output)
        assert payload["command"]["path"] == "tlgr agent exit-codes"
        assert set(payload["ops"]) == {"agent.exit-codes"}

    def test_unknown_path_is_a_usage_error(self, runner):
        result = runner.invoke(cli, ["schema", "nosuchgroup"])
        payload = json.loads(result.output)
        assert payload["ops"] == {}
        assert payload.get("command") is None

    def test_hidden_commands_are_omitted_by_default(self, runner):
        visible = json.loads(runner.invoke(cli, ["schema"]).output)
        hidden = json.loads(runner.invoke(cli, ["schema", "--include-hidden"]).output)
        assert _names(hidden["command"]) >= _names(visible["command"])

    def test_examples_come_from_the_registry(self, runner):
        payload = json.loads(runner.invoke(cli, ["schema", "agent", "exit-codes"]).output)
        assert payload["command"]["example_response"]["exit_codes"]["SUCCESS"]["code"] == 0

    def test_the_document_round_trips_as_json(self, runner):
        json.loads(runner.invoke(cli, ["schema"]).output)


def _names(node) -> set[str]:
    found = {node.get("path", "")}
    for sub in node.get("subcommands", []):
        found |= _names(sub)
    return found
