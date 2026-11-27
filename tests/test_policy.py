"""The `[policy]` table and `--enable-commands`: one matcher, and no dead entries.

Three failures this file pins:

* a typo in a policy entry or key failed *open*: `deny = ["mesage.delete"]`
  denied nothing and `denny = [...]` was ignored, while the operator believed
  deletion was off;
* `allow = ["*:read"]`, advertised as "read anything, change nothing",
  allowed `account export`, which prints the session;
* the CLI's `--enable-commands` check did not understand class entries, so
  `*:read` blocked everything on one side of the socket and allowed reads on
  the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tlgr.ops  # noqa: F401  (populates the registry)
from tlgr.core.errors import ConfigurationError
from tlgr.policy import Policy, classes_of
from tlgr.registry import REGISTRY, lint, policy_allows

#: Every operation that prints or mints something granting access. Pinned so
#: that removing the tag from one is a deliberate, reviewed change.
CREDENTIAL_OPS = {
    "account.export",  # the session itself
    "account.password.temp",  # a temporary payment password
    "auth.autologin-url.get",  # a signed-in telegram.org URL
    "auth.code.list",  # login codes from the 777000 chat
    "bot.token.export",  # a bot's API token
    "user.link",  # `--token` mints a contact token
    "webhook.get",  # `--show-secret` prints the signing key
}


def _write_config(home: Path, body: str) -> None:
    (home / "config.toml").write_text(body, encoding="utf-8")


class TestEntriesAreCheckedAgainstTheRegistry:
    def test_a_misspelled_deny_entry_is_an_error(self):
        problems = Policy(allow=["*"], deny=["mesage.delete"]).problems()
        assert len(problems) == 1
        assert "mesage.delete" in problems[0]
        assert "message.delete" in problems[0]  # the suggestion

    def test_from_config_refuses_a_dead_entry(self):
        with pytest.raises(ConfigurationError, match=r"mesage\.delete"):
            Policy.from_config(["*"], ["mesage.delete"])

    @pytest.mark.parametrize(
        "entry",
        [
            "*",
            "all",
            "message.delete",  # an id
            "send",  # an alias
            "message send",  # a legacy path
            "message",  # a group
            "chat.invite",  # a sub-group
            "message.*",  # a glob
            "*:read",
            "*:mutating",
            "*:destructive",
            "*:credential",
            "*:stream",
            "*:local",
        ],
    )
    def test_every_documented_spelling_is_live(self, entry):
        assert Policy(allow=[entry], deny=[entry]).problems() == []

    @pytest.mark.parametrize("entry", ["mesage", "message.nope", "mess*", "*:reed", "nope.*", " "])
    def test_entries_that_match_nothing_are_reported(self, entry):
        assert Policy(allow=["*"], deny=[entry]).problems()
        assert Policy(allow=[entry]).problems()

    def test_an_unknown_policy_key_is_a_config_error(self, tlgr_home):
        from tlgr.core.config import load_app_config

        _write_config(tlgr_home, '[policy]\ndenny = ["message.delete"]\n')
        with pytest.raises(ConfigurationError, match="denny"):
            load_app_config(tlgr_home)

    def test_other_sections_stay_lenient(self, tlgr_home):
        from tlgr.core.config import load_app_config

        _write_config(tlgr_home, "[daemon]\nfuture_knob = 1\n")
        load_app_config(tlgr_home)

    def test_the_daemon_refuses_to_start_on_a_dead_entry(self, tlgr_home):
        from tlgr.daemon.app import Daemon

        _write_config(tlgr_home, '[policy]\ndeny = ["mesage.delete"]\n')
        with pytest.raises(ConfigurationError, match=r"mesage\.delete"):
            Daemon(tlgr_home)

    def test_daemon_main_exits_10_before_forking(self, tlgr_home, capsys, monkeypatch):
        from tlgr.daemon import main as daemon_main

        def forked(*args, **kwargs):
            pytest.fail("the daemon forked before refusing the policy")

        monkeypatch.setattr(daemon_main, "daemonize", forked)
        _write_config(tlgr_home, '[policy]\ndeny = ["mesage.delete"]\n')
        # Without --foreground: the refusal comes before the fork, so the
        # message reaches the terminal that started the daemon.
        assert daemon_main.main(["--base", str(tlgr_home)]) == 10
        assert "mesage.delete" in capsys.readouterr().err
        assert not (tlgr_home / "daemon.lock").exists()

    async def test_a_reload_with_a_dead_entry_keeps_the_running_policy(self, daemon, tlgr_home):
        from tlgr.daemon.app import _reload

        _write_config(tlgr_home, '[policy]\ndeny = ["message.delete"]\n')
        await _reload(daemon, ["config"])
        assert daemon.policy.deny == ["message.delete"]

        before = daemon.config
        _write_config(tlgr_home, '[policy]\ndeny = ["mesage.delete"]\n')
        for what in (["config"], ["policy"]):
            with pytest.raises(ConfigurationError, match=r"mesage\.delete"):
                await _reload(daemon, what)
            assert daemon.policy.deny == ["message.delete"]
            assert daemon.config is before

    async def test_config_validate_reports_dead_entries(self, tlgr_home):
        from tlgr.ops.config import ConfigValidateReq, config_validate

        _write_config(tlgr_home, '[policy]\nallow = ["*"]\ndeny = ["mesage.delete"]\n')
        report = await config_validate(_Ctx(), ConfigValidateReq())
        assert not report.ok
        assert [e.key for e in report.errors] == ["policy.deny"]
        assert "message.delete" in report.errors[0].message
        assert not report.warnings

    async def test_config_validate_reports_an_unknown_policy_key(self, tlgr_home):
        from tlgr.ops.config import ConfigValidateReq, config_validate

        _write_config(tlgr_home, '[policy]\ndenny = ["message.delete"]\n')
        report = await config_validate(_Ctx(), ConfigValidateReq())
        assert not report.ok
        assert "denny" in report.errors[0].message


class TestCredentialsAreNotReads:
    def test_the_credential_ops_are_tagged(self):
        tagged = {op_id for op_id, spec in REGISTRY.items() if "bearer-credential" in spec.tags}
        assert tagged == CREDENTIAL_OPS

    @pytest.mark.parametrize("op_id", sorted(CREDENTIAL_OPS))
    def test_read_does_not_cover_a_credential(self, op_id):
        spec = REGISTRY[op_id]
        assert "read" not in classes_of(spec)
        assert "credential" in classes_of(spec)
        assert not Policy(allow=["*:read"]).check(op_id, spec).allowed

    @pytest.mark.parametrize(
        "entry", ["account.export", "account", "account.*", "*:credential", "*"]
    )
    def test_a_credential_op_is_allowed_by_name_group_class_or_star(self, entry):
        spec = REGISTRY["account.export"]
        assert Policy(allow=[entry]).check("account.export", spec).allowed

    def test_reads_are_still_reads(self):
        spec = REGISTRY["message.list"]
        assert Policy(allow=["*:read"]).check("message.list", spec).allowed

    def test_the_lint_catches_an_untagged_credential_op(self):
        """L17: source that mints a credential needs the tag."""
        import msgspec

        from tlgr.ops._spec import OperationSpec
        from tlgr.registry import _lint_spec

        class Req(msgspec.Struct):
            pass

        async def leaks(ctx, req):
            from telethon.sessions import StringSession

            return StringSession.save(ctx.client.session)

        spec = OperationSpec(
            id="test.leak",
            request=Req,
            response=None,
            impl=leaks,
            summary="x",
            example={},
            example_args="test leak",
            tags=frozenset({"infrastructure"}),
        )
        problems: list[str] = []
        _lint_spec(spec, problems)
        assert any("bearer-credential" in p for p in problems)
        assert lint() == []


class TestTheCliAndDaemonAgree:
    @pytest.mark.parametrize(
        ("allowlist", "op_id", "allowed"),
        [
            ("*:read", "message.list", True),
            ("*:read", "message.send", False),
            ("*:read", "account.export", False),
            ("*:read,account.export", "account.export", True),
            ("*:mutating", "message.send", True),
            ("message.*", "message.send", True),
            ("message.*", "chat.list", False),
            ("message", "send", True),
        ],
    )
    def test_policy_allows_matches_the_daemon(self, allowlist, op_id, allowed):
        spec = REGISTRY[_canonical_id(op_id)]
        assert policy_allows(allowlist, op_id) is allowed
        assert Policy.parse(allowlist).check(op_id, spec).allowed is allowed

    def test_enable_commands_star_read_runs_a_read(self):
        from click.testing import CliRunner

        from tlgr.cli import cli

        result = CliRunner().invoke(cli, ["--enable-commands", "*:read", "agent", "exit-codes"])
        assert result.exit_code == 0, result.output

    def test_enable_commands_star_read_blocks_account_export(self):
        from click.testing import CliRunner

        from tlgr.cli import cli

        result = CliRunner().invoke(
            cli, ["--enable-commands", "*:read", "account", "export", "work", "--stdout"]
        )
        assert result.exit_code == 6
        assert "not enabled" in result.output


def _canonical_id(name: str) -> str:
    from tlgr.registry import ALIASES

    return ALIASES.get(name, name)


class _Ctx:
    account = ""
    dry_run = False
    request_id = "t"

    def warn(self, message: str) -> None: ...
