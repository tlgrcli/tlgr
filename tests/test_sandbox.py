"""Tests for subcommand-level --enable-commands sandboxing."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tlgr.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestEnableCommands:
    """One exit code, now that every group is generated.

    Every command is registry-generated now, so every block is
    PERMISSION_DENIED (exit 6, §7.2). The v1 path matching in
    `TlgrGroup.resolve_command` that answered exit 2 went with the last
    hand-written group in PR-12.
    """

    def test_top_level_block(self, runner):
        result = runner.invoke(cli, ["--enable-commands", "chat", "message", "list", "test"])
        assert result.exit_code == 6
        assert "not enabled" in result.output

    def test_the_last_hand_written_group_now_blocks_the_same_way(self, runner):
        """`profile` was the last hand-written group and answered exit 2.

        PR-12 generated it, so it answers PERMISSION_DENIED like everything
        else — which is the point of the migration: one refusal, one code.
        """
        result = runner.invoke(cli, ["--enable-commands", "message", "profile", "get"])
        assert result.exit_code == 6
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
