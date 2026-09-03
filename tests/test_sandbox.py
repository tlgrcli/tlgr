"""Tests for subcommand-level --enable-commands sandboxing."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tlgr.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestEnableCommands:
    """Two exit codes on purpose, until every group is generated.

    A registry-generated command enforces the allowlist by canonical op id and
    answers PERMISSION_DENIED (exit 6, §7.2). The v1 path matching in
    `TlgrGroup.resolve_command` still answers exit 2, and goes when the last
    hand-written group does.
    """

    def test_top_level_block(self, runner):
        result = runner.invoke(cli, ["--enable-commands", "chat", "message", "list", "test"])
        assert result.exit_code == 6
        assert "not enabled" in result.output

    def test_legacy_top_level_block_still_exits_2(self, runner):
        """`profile` is the last hand-written group; `contact` is generated in PR-5."""
        result = runner.invoke(cli, ["--enable-commands", "message", "profile", "get"])
        assert result.exit_code == 2
        assert "not enabled" in result.output

    def test_a_generated_group_blocks_with_permission_denied(self, runner):
        """`contact` is generated now, so its block is PERMISSION_DENIED (6)."""
        result = runner.invoke(cli, ["--enable-commands", "message", "contact", "list"])
        assert result.exit_code == 6
        assert "not enabled" in result.output

    def test_top_level_allow(self, runner):
        result = runner.invoke(cli, ["--enable-commands", "agent", "agent", "exit-codes"])
        assert result.exit_code == 0

    def test_subcommand_block(self, runner):
        result = runner.invoke(
            cli,
            [
                "--enable-commands",
                "message.list",
                "message",
                "send",
                "test",
                "hello",
            ],
        )
        assert result.exit_code == 6
        assert "not enabled" in result.output

    def test_subcommand_allow(self, runner):
        result = runner.invoke(
            cli,
            [
                "--enable-commands",
                "agent.exit-codes",
                "agent",
                "exit-codes",
            ],
        )
        assert result.exit_code == 0

    def test_wildcard_allows_all(self, runner):
        result = runner.invoke(cli, ["--enable-commands", "*", "agent", "exit-codes"])
        assert result.exit_code == 0

    def test_all_keyword_allows_all(self, runner):
        result = runner.invoke(cli, ["--enable-commands", "all", "agent", "exit-codes"])
        assert result.exit_code == 0

    def test_group_allows_all_subcommands(self, runner):
        result = runner.invoke(cli, ["--enable-commands", "agent", "agent", "exit-codes"])
        assert result.exit_code == 0

    def test_no_sandboxing_by_default(self, runner):
        result = runner.invoke(cli, ["agent", "exit-codes"])
        assert result.exit_code == 0
