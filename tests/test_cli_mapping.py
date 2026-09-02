"""Request field → Click parameter, one case per row of the §4.2 table.

The operations here are fixtures, not shipped ops: the mapping has to be
provable for every field shape before any of them has a real implementation,
and a fixture registry keeps the assertions about the *generator* rather than
about whichever operation happens to use a shape today.
"""

from __future__ import annotations

from typing import Annotated, Literal

import click
import pytest
from click.testing import CliRunner

from tlgr.cli.gen import build_command
from tlgr.models.base import UNSET, Request, Unset
from tlgr.models.peer import PeerRef
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OperationSpec, Surface

CAPTURED: dict[str, object] = {}


class EveryShapeReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", help="Target chat.")]
    text: Annotated[str, arg(1, metavar="TEXT", required=False)] = ""
    ids: Annotated[tuple[int, ...], arg(2, variadic=True, metavar="ID")] = ()

    reply_to: Annotated[int | None, opt("--reply-to", metavar="ID")] = None
    ratio: Annotated[float | None, opt("--ratio")] = None
    files: Annotated[list[str], opt("--file", metavar="PATH", help="Repeat for an album.")] = []
    limit_hint: Annotated[int, opt("-N", "--limit-hint", ge=1, le=100)] = 20
    parse: Annotated[str, choice("md", "html", "none")] = "none"
    kind: Annotated[Literal["a", "b"], opt("--kind")] = "a"
    silent: Annotated[bool, opt("--silent", help="No notification.")] = False
    clear_draft: Annotated[bool, opt(help="Clear the draft after sending.")] = True
    pinned: Annotated[bool | None, opt("--pinned")] = None
    bio: Annotated[Unset[str | None], opt("--bio")] = UNSET
    until: Annotated[int | None, opt("--until", kind="duration")] = None
    at: Annotated[str | None, opt("--at", kind="datetime")] = None
    target: Annotated[PeerRef | None, opt("--send-as", kind="peer")] = None
    who: Annotated[PeerRef | None, opt("--who", kind="user")] = None
    msg: Annotated[int | None, opt("--msg", kind="msg_id")] = None
    payload: Annotated[str | None, opt("--entities", kind="json")] = None
    token: Annotated[str | None, opt("--token", secret=True, envvar="TLGR_TEST_TOKEN")] = None
    hidden_flag: Annotated[bool, opt("--legacy", hidden=True)] = False


async def _impl(ctx, req):
    CAPTURED["request"] = req
    CAPTURED["ctx"] = ctx
    return {"ok": True}


def make_spec(**overrides) -> OperationSpec:
    fields = {
        "id": "fixture.run",
        "request": EveryShapeReq,
        "response": dict,
        "impl": _impl,
        "summary": "Exercise every parameter shape",
        "needs_account": False,
        "surface": Surface.LOCAL,
        "example": {"ok": True},
        "example_args": "fixture run @a",
        "tags": frozenset({"infrastructure"}),
    }
    fields.update(overrides)
    return OperationSpec(**fields)


@pytest.fixture
def command():
    CAPTURED.clear()
    return build_command(make_spec())


@pytest.fixture
def runner():
    return CliRunner()


def invoke(command, args, runner):
    result = runner.invoke(command, args, obj={})
    return result


def flags_of(command) -> set[str]:
    return {opt_ for param in command.params for opt_ in param.opts + param.secondary_opts}


class TestParameterShapes:
    def test_positionals_are_in_order(self, command):
        arguments = [p for p in command.params if isinstance(p, click.Argument)]
        assert [p.name for p in arguments] == ["chat", "text", "ids"]
        assert arguments[0].required is True
        assert arguments[1].required is False
        assert arguments[2].nargs == -1

    def test_derived_flag_names(self, command):
        assert "--clear-draft" in flags_of(command)
        assert "--no-clear-draft" in flags_of(command)

    def test_explicit_flags_override_the_derived_name(self, command):
        assert {"-N", "--limit-hint"} <= flags_of(command)
        assert "--limit_hint" not in flags_of(command)

    def test_false_default_boolean_is_a_plain_flag(self, command):
        silent = next(p for p in command.params if p.name == "silent")
        assert silent.is_flag and not silent.secondary_opts

    def test_true_default_boolean_is_paired(self, command):
        paired = next(p for p in command.params if p.name == "clear_draft")
        assert paired.secondary_opts == ["--no-clear-draft"]
        assert paired.default is True

    def test_tristate_boolean_defaults_to_none(self, command):
        pinned = next(p for p in command.params if p.name == "pinned")
        assert pinned.default is None
        assert pinned.secondary_opts == ["--no-pinned"]

    def test_choices_become_a_click_choice(self, command):
        parse = next(p for p in command.params if p.name == "parse")
        assert isinstance(parse.type, click.Choice)
        assert list(parse.type.choices) == ["md", "html", "none"]

    def test_literals_become_a_click_choice(self, command):
        kind = next(p for p in command.params if p.name == "kind")
        assert isinstance(kind.type, click.Choice)
        assert list(kind.type.choices) == ["a", "b"]

    def test_lists_are_repeatable(self, command):
        files = next(p for p in command.params if p.name == "files")
        assert files.multiple

    def test_secret_has_no_value_flag(self, command):
        """A secret is never a bare argv value (STYLE §3)."""
        flags = flags_of(command)
        assert "--token" not in flags
        assert {"--token-env", "--token-stdin", "--token-file"} <= flags

    def test_hidden_stays_hidden(self, command):
        assert next(p for p in command.params if p.name == "hidden_flag").hidden

    def test_help_text_comes_from_the_annotation(self, command):
        files = next(p for p in command.params if p.name == "files")
        assert files.help == "Repeat for an album."

    def test_metavar_is_carried(self, command):
        chat = next(p for p in command.params if p.name == "chat")
        assert chat.make_metavar(click.Context(command)) == "CHAT"


class TestParsing:
    def test_minimal_invocation(self, command, runner):
        assert invoke(command, ["@alice"], runner).exit_code == 0
        request = CAPTURED["request"]
        assert request.chat.kind == "username"
        assert request.chat.value == "alice"
        assert request.text == ""
        assert request.ids == ()

    def test_variadic_ids(self, command, runner):
        invoke(command, ["@alice", "hi", "1", "2", "3"], runner)
        assert CAPTURED["request"].ids == (1, 2, 3)

    def test_negative_ids_after_a_double_dash(self, command, runner):
        """`--` is how a `-100…` id gets past Click's option parser (STYLE §2)."""
        result = invoke(command, ["--", "-1001234567890", "hi"], runner)
        assert result.exit_code == 0, result.output
        assert CAPTURED["request"].chat.value == -1001234567890

    def test_persian_text_survives(self, command, runner):
        """COR-04: v1's hand-rolled transport could not carry this at all."""
        invoke(command, ["@alice", "سلام دنیا"], runner)
        assert CAPTURED["request"].text == "سلام دنیا"

    def test_emoji_text_survives(self, command, runner):
        invoke(command, ["@alice", "👍 ok"], runner)
        assert CAPTURED["request"].text == "👍 ok"

    def test_repeated_file_flags(self, command, runner):
        invoke(command, ["@alice", "--file", "one.jpg", "--file", "two.jpg"], runner)
        assert CAPTURED["request"].files == ["one.jpg", "two.jpg"]

    def test_duration_is_seconds(self, command, runner):
        invoke(command, ["@alice", "--until", "2h"], runner)
        assert CAPTURED["request"].until == 7200

    def test_forever_is_none(self, command, runner):
        invoke(command, ["@alice", "--until", "forever"], runner)
        assert CAPTURED["request"].until is None

    def test_datetime_becomes_rfc3339_utc(self, command, runner):
        invoke(command, ["@alice", "--at", "2026-09-02T09:14:07Z"], runner)
        assert CAPTURED["request"].at == "2026-09-02T09:14:07Z"

    def test_message_link_becomes_an_id(self, command, runner):
        invoke(command, ["@alice", "--msg", "https://t.me/c/1234567890/1042"], runner)
        assert CAPTURED["request"].msg == 1042

    def test_peer_flags_parse_to_refs(self, command, runner):
        invoke(command, ["@alice", "--send-as", "-1001111", "--who", "me"], runner)
        assert CAPTURED["request"].target.value == -1001111
        assert CAPTURED["request"].who.kind == "self"

    def test_user_flag_rejects_a_channel(self, command, runner):
        result = invoke(command, ["@alice", "--who", "-1001111"], runner)
        assert result.exit_code == 2
        assert "wants a user" in result.output

    def test_json_flag_validates(self, command, runner):
        assert invoke(command, ["@alice", "--entities", "[]"], runner).exit_code == 0
        bad = invoke(command, ["@alice", "--entities", "{oops"], runner)
        assert bad.exit_code == 2

    def test_unset_field_is_omitted_when_absent(self, command, runner):
        invoke(command, ["@alice"], runner)
        assert CAPTURED["request"].bio is UNSET

    def test_unset_field_carries_a_value(self, command, runner):
        invoke(command, ["@alice", "--bio", "hi"], runner)
        assert CAPTURED["request"].bio == "hi"

    def test_paired_flag_negative(self, command, runner):
        invoke(command, ["@alice", "--no-clear-draft"], runner)
        assert CAPTURED["request"].clear_draft is False

    def test_tristate_stays_none_when_untouched(self, command, runner):
        invoke(command, ["@alice"], runner)
        assert CAPTURED["request"].pinned is None
        invoke(command, ["@alice", "--no-pinned"], runner)
        assert CAPTURED["request"].pinned is False

    def test_constraints_are_enforced(self, command, runner):
        assert invoke(command, ["@alice", "-N", "500"], runner).exit_code == 2

    def test_bad_peer_is_a_usage_error(self, command, runner):
        result = invoke(command, ["!!!"], runner)
        assert result.exit_code == 2

    def test_secret_from_env(self, command, runner, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "s3cr3t")
        invoke(command, ["@alice", "--token-env", "MY_TOKEN"], runner)
        assert CAPTURED["request"].token == "s3cr3t"

    def test_secret_env_that_is_unset_is_a_usage_error(self, command, runner, monkeypatch):
        monkeypatch.delenv("MISSING_TOKEN", raising=False)
        result = invoke(command, ["@alice", "--token-env", "MISSING_TOKEN"], runner)
        assert result.exit_code == 2

    def test_secret_from_file(self, command, runner, tmp_path):
        path = tmp_path / "t.txt"
        path.write_text("filesecret\n")
        invoke(command, ["@alice", "--token-file", str(path)], runner)
        assert CAPTURED["request"].token == "filesecret"


class TestShortFlags:
    def test_n_is_dry_run_when_not_paginated(self):
        command = build_command(make_spec())
        dry = next(p for p in command.params if p.name == "dry_run")
        assert "-n" in dry.opts

    def test_n_is_limit_when_paginated(self):
        from tlgr.core.pagination import PageKind
        from tlgr.models.message import Message
        from tlgr.models.page import Page

        command = build_command(
            make_spec(
                id="fixture.list",
                paginated=PageKind.HISTORY,
                response=Page[Message],
                example={"items": []},
                example_args="fixture list @a",
            )
        )
        limit = next(p for p in command.params if p.name == "limit")
        dry = next(p for p in command.params if p.name == "dry_run")
        assert "-n" in limit.opts
        assert "-n" not in dry.opts
        assert {"--cursor", "--all", "--since", "--until"} <= flags_of(command)
