"""`require_account`: what it promises, and what the fallback does without it.

README and AGENT.md say that with `require_account = true` every command must
name its account (`-a <alias>`), with no default-account fallback, and that a
violation exits 2. `resolve_account` did not do that: it fell back to
`[accounts] default` and then to the active alias, and only refused when
nothing at all resolved, so the rail passed on exactly the state it exists to
distrust.

The other half was the opposite bug. Without `require_account`, `run_op`
resolved the fallback account and then kept it in a local variable: the
daemon dispatcher reads `state.account`, so the request went out with
`account: ""` and the daemon (which never picks one, COR-02) answered
ACCOUNT_REQUIRED. The documented fallback order worked on paper only.

The behaviour pinned here:

* `require_account` on: only the positional, `-a/--account` and
  `TLGR_ACCOUNT` (the documented environment spelling of `-a`) count. The
  configured default and the active alias are never consulted, and a command
  without an explicit account is refused locally with ACCOUNT_REQUIRED, exit
  2, before anything is dispatched.
* `require_account` off: positional, `-a`, `TLGR_ACCOUNT`, `[accounts]
  default`, then the active alias, and the alias that wins is the one the
  daemon receives.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tlgr.cli import cli, gen
from tlgr.cli.globals import CliState, resolve_account
from tlgr.core.accounts import AccountManager
from tlgr.core.errors import EXIT_USAGE, AccountRequiredError
from tlgr.core.paths import TlgrPaths
from tlgr.models.base import Request
from tlgr.ops._spec import OperationSpec, Surface


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with two accounts; `active` was added first, so it is active."""
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setenv("TLGR_HOME", str(base))
    for key in ("TLGR_ACCOUNT", "TLGR_REQUIRE_ACCOUNT"):
        monkeypatch.delenv(key, raising=False)
    manager = AccountManager(base)
    manager.add_account("active")
    manager.add_account("other")
    assert manager.get_active() == "active"
    return base


def _config(base: Path, text: str) -> None:
    TlgrPaths(base).config.write_text(text)


def _strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLGR_REQUIRE_ACCOUNT", "1")


# ---------------------------------------------------------------------------
# resolve_account itself
# ---------------------------------------------------------------------------


class TestFallbackMode:
    def test_flag_wins(self, home):
        assert resolve_account(CliState(account="other")) == "other"

    def test_positional_beats_flag(self, home):
        assert resolve_account(CliState(account="other"), positional="third") == "third"

    def test_env_beats_config_default(self, home, monkeypatch):
        _config(home, '[accounts]\ndefault = "other"\n')
        monkeypatch.setenv("TLGR_ACCOUNT", "third")
        assert resolve_account(CliState()) == "third"

    def test_config_default_beats_active(self, home):
        _config(home, '[accounts]\ndefault = "other"\n')
        assert resolve_account(CliState()) == "other"

    def test_active_alias_is_the_last_resort(self, home):
        assert resolve_account(CliState()) == "active"

    def test_nothing_resolvable_is_left_to_the_daemon(self, tmp_path, monkeypatch):
        """ "" goes out and the daemon refuses; a local refusal would break --dry-run."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("TLGR_HOME", str(empty))
        monkeypatch.delenv("TLGR_ACCOUNT", raising=False)
        monkeypatch.delenv("TLGR_REQUIRE_ACCOUNT", raising=False)
        assert resolve_account(CliState()) == ""


class TestStrictMode:
    def test_active_alias_is_refused(self, home, monkeypatch):
        _strict(monkeypatch)
        with pytest.raises(AccountRequiredError) as caught:
            resolve_account(CliState())
        assert caught.value.code == "ACCOUNT_REQUIRED"
        assert caught.value.exit_code == EXIT_USAGE
        assert "require_account" in str(caught.value)

    def test_config_default_is_refused(self, home, monkeypatch):
        _config(home, '[accounts]\ndefault = "other"\n')
        _strict(monkeypatch)
        with pytest.raises(AccountRequiredError):
            resolve_account(CliState())

    def test_config_switch_alone_turns_it_on(self, home):
        _config(home, "[defaults]\nrequire_account = true\n")
        with pytest.raises(AccountRequiredError):
            resolve_account(CliState())

    def test_env_off_overrides_the_config_switch(self, home, monkeypatch):
        _config(home, "[defaults]\nrequire_account = true\n")
        monkeypatch.setenv("TLGR_REQUIRE_ACCOUNT", "0")
        assert resolve_account(CliState()) == "active"

    def test_explicit_flag_passes(self, home, monkeypatch):
        _strict(monkeypatch)
        assert resolve_account(CliState(account="other")) == "other"

    def test_explicit_positional_passes(self, home, monkeypatch):
        _strict(monkeypatch)
        assert resolve_account(CliState(), positional="other") == "other"

    def test_tlgr_account_counts_as_explicit(self, home, monkeypatch):
        """AGENT.md documents `TLGR_ACCOUNT` as the environment form of `-a`."""
        _strict(monkeypatch)
        monkeypatch.setenv("TLGR_ACCOUNT", "other")
        assert resolve_account(CliState()) == "other"

    def test_require_argument_overrides_the_setting(self, home, monkeypatch):
        with pytest.raises(AccountRequiredError):
            resolve_account(CliState(), require=True)
        _strict(monkeypatch)
        assert resolve_account(CliState(), require=False) == "active"


# ---------------------------------------------------------------------------
# Through the CLI, with the dispatcher replaced
# ---------------------------------------------------------------------------


class _EmptyReq(Request):
    pass


async def _never(ctx: Any, req: Any) -> dict[str, Any]:
    raise AssertionError("a daemon op must not run locally")


SPEC = OperationSpec(
    id="fixture.whoami",
    request=_EmptyReq,
    response=dict,
    impl=_never,
    summary="Report the account the request was sent for",
    needs_account=True,
    surface=Surface.DAEMON,
    example={"ok": True},
    example_args="fixture whoami",
    tags=frozenset({"infrastructure"}),
)


class _Recorder:
    def __init__(self) -> None:
        self.accounts: list[str] = []

    def __call__(self, spec: Any, request: Any, state: CliState) -> dict[str, Any]:
        self.accounts.append(state.account)
        return {"ok": True, "op": spec.id, "account": state.account, "result": {}}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    sent = _Recorder()
    monkeypatch.setattr(gen, "_dispatch", sent)
    return sent


def _invoke(args: list[str]) -> Any:
    return CliRunner().invoke(gen.build_command(SPEC), args, obj={})


class TestThroughTheCli:
    def test_the_fallback_account_reaches_the_dispatcher(self, home, recorder):
        """The `run_op` bug: the resolved alias stayed in a local variable."""
        result = _invoke(["--json"])
        assert result.exit_code == 0, result.output
        assert recorder.accounts == ["active"]

    def test_the_config_default_reaches_the_dispatcher(self, home, recorder):
        _config(home, '[accounts]\ndefault = "other"\n')
        result = _invoke(["--json"])
        assert result.exit_code == 0, result.output
        assert recorder.accounts == ["other"]

    def test_an_explicit_flag_reaches_the_dispatcher(self, home, recorder):
        result = _invoke(["--json", "-a", "other"])
        assert result.exit_code == 0, result.output
        assert recorder.accounts == ["other"]

    def test_strict_mode_refuses_locally_with_the_documented_error(
        self, home, recorder, monkeypatch
    ):
        _strict(monkeypatch)
        result = _invoke(["--json"])
        assert result.exit_code == EXIT_USAGE
        envelope = json.loads(result.stdout.splitlines()[0])
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "ACCOUNT_REQUIRED"
        assert envelope["error"]["exit_code"] == EXIT_USAGE
        assert recorder.accounts == [], "nothing may be dispatched"

    def test_strict_mode_refuses_even_with_a_config_default(self, home, recorder, monkeypatch):
        _config(home, '[accounts]\ndefault = "other"\n')
        _strict(monkeypatch)
        result = _invoke(["--json"])
        assert result.exit_code == EXIT_USAGE
        assert recorder.accounts == []

    def test_strict_mode_passes_an_explicit_flag(self, home, recorder, monkeypatch):
        _strict(monkeypatch)
        result = _invoke(["--json", "-a", "other"])
        assert result.exit_code == 0, result.output
        assert recorder.accounts == ["other"]

    def test_strict_mode_also_guards_live_streams(self, home, monkeypatch):
        frames: list[str] = []

        def stream(spec: Any, request: Any, state: CliState):
            frames.append(state.account)
            return iter(())

        monkeypatch.setattr(gen, "_stream_dispatch", stream)
        _strict(monkeypatch)
        live = dataclasses.replace(
            SPEC, id="fixture.watch", tags=frozenset({"infrastructure", "live-stream"})
        )
        result = CliRunner().invoke(gen.build_command(live), ["--json"], obj={})
        assert result.exit_code == EXIT_USAGE
        assert frames == []


# ---------------------------------------------------------------------------
# End to end, against a real daemon on a temp socket
# ---------------------------------------------------------------------------


def _cli(args: list[str]) -> Any:
    return CliRunner().invoke(cli, args)


async def test_the_active_alias_reaches_a_real_daemon(
    live_daemon, stub_account, in_thread, monkeypatch
):
    """Before the fix this answered ACCOUNT_REQUIRED from the daemon."""
    monkeypatch.delenv("TLGR_ACCOUNT", raising=False)
    monkeypatch.delenv("TLGR_REQUIRE_ACCOUNT", raising=False)
    result = await in_thread(_cli, ["--json", "profile", "get", "--no-full"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["account"] == stub_account


async def test_strict_mode_never_reaches_a_real_daemon(
    live_daemon, stub_account, world, in_thread, monkeypatch
):
    monkeypatch.delenv("TLGR_ACCOUNT", raising=False)
    monkeypatch.setenv("TLGR_REQUIRE_ACCOUNT", "1")
    result = await in_thread(_cli, ["--json", "profile", "get"])
    assert result.exit_code == EXIT_USAGE
    envelope = json.loads(result.stdout.splitlines()[0])
    assert envelope["error"]["code"] == "ACCOUNT_REQUIRED"
    assert world.called("GetFullUserRequest") == []

    explicit = await in_thread(_cli, ["--json", "-a", stub_account, "profile", "get"])
    assert explicit.exit_code == 0, explicit.output
    assert json.loads(explicit.stdout)["account"] == stub_account
    assert len(world.called("GetFullUserRequest")) == 1
