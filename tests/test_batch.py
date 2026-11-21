"""`tlgr batch`: many operations in one process, and not a way around anything.

Most of this file drives the real `batch` command with a fake dispatcher in
place of the daemon, because what is being pinned is the CLI's own behaviour:
the line format, the error policy, the exit code, and that every guard the
single command enforces still runs per line. The last class runs it against
the live test daemon (fake Telethon behind a real socket), so the transport
path is exercised end to end as well.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from tlgr.cli import cli, gen
from tlgr.core.errors import (
    AccountFrozenError,
    DaemonNotRunningError,
    NotFoundError,
    RateLimitError,
    SpamFlagError,
)


def _lines(*records: Any) -> str:
    return "".join(
        (r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n" for r in records
    )


def _out(result: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


class _Fake:
    """A dispatcher that records what reached the daemon and answers on cue."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []
        self.answers: dict[str, Any] = {}

    def __call__(self, spec: Any, request: Any, state: Any) -> dict[str, Any]:
        self.calls.append((spec.id, request, state))
        answer = self.answers.get(spec.id)
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else None
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(spec, request, state)
        envelope: dict[str, Any] = {
            "ok": True,
            "op": spec.id,
            "account": state.account,
            "result": answer if answer is not None else {"id": len(self.calls)},
            "meta": {"request_id": "r", "warnings": []},
        }
        return envelope

    @property
    def ops(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Fake:
    dispatcher = _Fake()
    monkeypatch.setattr(gen, "_dispatch", dispatcher)
    monkeypatch.delenv("TLGR_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.delenv("TLGR_ENABLE_COMMANDS", raising=False)
    monkeypatch.delenv("TLGR_ACCOUNT", raising=False)
    return dispatcher


def run(args: list[str], stdin: str = "", **kwargs: Any) -> Any:
    return CliRunner().invoke(cli, args, input=stdin.encode(), **kwargs)


SEND = {"op": "message.send", "params": {"chat": "@alice", "text": "hi"}}


class TestFormat:
    def test_one_record_per_line_in_order_with_line_and_id(self, fake):
        stdin = _lines({**SEND, "id": "a"}, "", {**SEND, "id": 7}, SEND)
        result = run(["-a", "work", "batch"], stdin)
        assert result.exit_code == 0, result.output
        records = _out(result)
        assert [r["line"] for r in records] == [1, 3, 4]
        assert [r.get("id") for r in records] == ["a", 7, None]
        assert "id" not in records[2], "an id is echoed only when one was given"
        assert all(r["ok"] and r["exit_code"] == 0 for r in records)
        assert all(r["op"] == "message.send" and r["account"] == "work" for r in records)
        assert fake.ops == ["message.send"] * 3

    def test_the_record_is_the_json_envelope_plus_three_keys(self, fake):
        result = run(["-a", "work", "batch"], _lines({**SEND, "id": "x"}))
        (record,) = _out(result)
        assert set(record) == {"line", "id", "ok", "op", "account", "result", "meta", "exit_code"}

    def test_stdout_is_only_records_and_the_summary_is_on_stderr(self, fake):
        result = run(["-a", "work", "batch"], _lines(SEND, SEND))
        assert len(result.stdout.splitlines()) == 2
        assert "batch: 2 line(s), 2 ok, 0 failed; exit 0" in result.stderr

    def test_empty_input_is_success_with_no_output(self, fake):
        result = run(["-a", "work", "batch"], "")
        assert result.exit_code == 0
        assert result.stdout == ""

    def test_an_alias_runs_the_canonical_operation(self, fake):
        result = run(["-a", "work", "batch"], _lines({"op": "send", "params": SEND["params"]}))
        assert _out(result)[0]["op"] == "message.send"
        assert fake.ops == ["message.send"]

    def test_params_are_parsed_by_the_same_types_as_the_flags(self, fake):
        line = {
            "op": "message.delete",
            "params": {"chat": "@alice", "msg-id": ["5", 6], "for_everyone": True},
            "yes": True,
        }
        result = run(["-a", "work", "batch"], _lines(line))
        assert result.exit_code == 0, result.output
        _, request, _ = fake.calls[0]
        assert request.chat.kind == "username" and request.chat.value == "alice"
        assert tuple(request.msg_id) == ("5", "6"), "ids or ranges: strings, as typed"
        assert request.for_everyone is True

    def test_persian_and_emoji_survive(self, fake):
        text = "سلام دنیا 👋"
        run(
            ["-a", "work", "batch"],
            _lines({"op": "message.send", "params": {"chat": "@alice", "text": text}}),
        )
        assert fake.calls[0][1].text == text

    def test_select_projects_each_result(self, fake):
        fake.answers["message.send"] = {"id": 1, "text": "hi", "chat_id": 9}
        result = run(["-a", "work", "batch", "--select", "id"], _lines(SEND))
        assert _out(result)[0]["result"] == {"id": 1}

    def test_a_file_argument_is_read(self, fake, tmp_path):
        path = tmp_path / "ops.jsonl"
        path.write_text(_lines(SEND, SEND), encoding="utf-8")
        result = run(["-a", "work", "batch", str(path)])
        assert result.exit_code == 0
        assert len(_out(result)) == 2

    def test_a_missing_file_is_a_usage_error_before_anything_runs(self, fake, tmp_path):
        result = run(["--json", "-a", "work", "batch", str(tmp_path / "nope.jsonl")])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "USAGE"
        assert fake.calls == []


class TestLineErrors:
    @pytest.mark.parametrize(
        ("raw", "field"),
        [
            ("{not json", None),
            ("[1, 2]", None),
            ('{"params": {}}', "op"),
            ('{"op": "message.send", "param": {}}', "param"),
            ('{"op": "message.send", "params": []}', "params"),
            ('{"op": "message.send", "dry_run": "yes"}', "dry_run"),
            ('{"op": "message.send", "limit": "5"}', "limit"),
        ],
    )
    def test_a_malformed_line_is_a_usage_record_and_the_batch_goes_on(self, fake, raw, field):
        result = run(["-a", "work", "batch"], raw + "\n" + _lines(SEND))
        first, second = _out(result)
        assert first["ok"] is False and first["exit_code"] == 2
        assert first["error"]["code"] == "USAGE"
        if field:
            assert first["error"].get("field") == field
        assert second["ok"] is True
        assert result.exit_code == 2

    def test_an_unknown_operation_names_itself(self, fake):
        result = run(["-a", "work", "batch"], _lines({"op": "message.shout", "id": 1}))
        (record,) = _out(result)
        assert record["id"] == 1
        assert record["error"]["code"] == "USAGE"
        assert "message.shout" in record["error"]["message"]

    def test_an_unknown_parameter_lists_the_known_ones(self, fake):
        line = {"op": "message.send", "params": {"chat": "@alice", "txt": "hi"}}
        (record,) = _out(run(["-a", "work", "batch"], _lines(line)))
        assert record["error"]["field"] == "txt"
        assert "text" in record["error"]["message"]
        assert fake.calls == []

    def test_a_bad_peer_is_the_same_usage_error_as_on_the_command_line(self, fake):
        line = {"op": "message.send", "params": {"chat": "", "text": "hi"}}
        (record,) = _out(run(["-a", "work", "batch"], _lines(line)))
        assert record["exit_code"] == 2
        assert fake.calls == []

    def test_a_secret_is_never_accepted_inline(self, fake):
        line = {"op": "account.delete", "params": {"password": "hunter2"}, "yes": True}
        (record,) = _out(run(["-a", "work", "batch"], _lines(line)))
        assert record["error"]["field"] == "password"
        assert "password_env" in record["error"]["message"]
        assert "hunter2" not in json.dumps(record)

    def test_dash_paths_are_refused_because_stdin_and_stdout_are_taken(self, fake):
        line = {"op": "account.export", "params": {"out": "-"}}
        (record,) = _out(run(["-a", "work", "batch"], _lines(line)))
        assert record["error"]["code"] == "USAGE"
        assert record["error"]["field"] == "out"

    def test_a_live_stream_cannot_run_inside_a_batch(self, fake):
        (record,) = _out(run(["-a", "work", "batch"], _lines({"op": "events.watch"})))
        assert record["error"]["code"] == "USAGE"
        assert "streams" in record["error"]["message"]


class TestErrorPolicy:
    def test_failures_continue_by_default_and_the_first_failure_sets_the_exit(self, fake):
        fake.answers["message.send"] = [
            None,
            NotFoundError("no such chat"),
            RateLimitError("slow down", wait_seconds=30),
            None,
        ]
        result = run(["-a", "work", "batch"], _lines(SEND, SEND, SEND, SEND))
        records = _out(result)
        assert [r["ok"] for r in records] == [True, False, False, True]
        assert records[1]["exit_code"] == 5
        assert records[2]["exit_code"] == 7
        assert records[2]["error"]["wait_seconds"] == 30
        assert result.exit_code == 5
        assert "4 line(s), 2 ok, 2 failed; exit 5" in result.stderr

    def test_stop_on_error_stops_at_the_first_failure(self, fake):
        fake.answers["message.send"] = [None, NotFoundError("gone"), None]
        result = run(["-a", "work", "batch", "--stop-on-error"], _lines(SEND, SEND, SEND))
        assert len(_out(result)) == 2
        assert len(fake.calls) == 2
        assert result.exit_code == 5
        assert "--stop-on-error" in result.stderr

    @pytest.mark.parametrize("flag", [SpamFlagError("PEER_FLOOD"), AccountFrozenError("frozen")])
    def test_a_spam_flag_always_aborts(self, fake, flag):
        fake.answers["message.send"] = [None, flag, None]
        result = run(["-a", "work", "batch"], _lines(SEND, SEND, SEND))
        records = _out(result)
        assert len(records) == 2, "nothing may be sent after a spam flag"
        assert len(fake.calls) == 2
        assert records[1]["aborted"] is True
        assert records[1]["exit_code"] == 9
        assert result.exit_code == 9
        assert "ABORTED" in result.stderr

    def test_the_abort_code_wins_over_an_earlier_failure(self, fake):
        fake.answers["message.send"] = [NotFoundError("gone"), SpamFlagError("PEER_FLOOD")]
        result = run(["-a", "work", "batch"], _lines(SEND, SEND))
        assert result.exit_code == 9

    def test_an_unreachable_daemon_aborts(self, fake):
        fake.answers["message.send"] = [DaemonNotRunningError("no socket"), None]
        result = run(["-a", "work", "batch"], _lines(SEND, SEND))
        assert len(_out(result)) == 1
        assert result.exit_code == 11

    def test_indeterminate_and_empty_carry_the_single_commands_exit_code(self, fake):
        def indeterminate(spec, request, state):
            return {"ok": True, "op": spec.id, "result": {}, "meta": {"indeterminate": True}}

        fake.answers["user.dialog-status"] = indeterminate
        fake.answers["chat.member.get"] = {}
        stdin = _lines(
            {"op": "chat.member.get", "params": {"chat": "@group1", "user": "@alice"}},
            {"op": "user.dialog-status", "params": {"user": "@alice"}},
            SEND,
        )
        result = run(["-a", "work", "batch"], stdin)
        records = _out(result)
        assert [r["exit_code"] for r in records] == [3, 13, 0]
        assert all(r["ok"] for r in records)
        assert result.exit_code == 3, "first non-zero success, since nothing failed"

    def test_a_failure_outranks_a_non_zero_success(self, fake):
        fake.answers["chat.member.get"] = {}
        fake.answers["message.send"] = NotFoundError("gone")
        stdin = _lines(
            {"op": "chat.member.get", "params": {"chat": "@group1", "user": "@alice"}}, SEND
        )
        assert run(["-a", "work", "batch"], stdin).exit_code == 5


class TestGuards:
    def test_enable_commands_is_enforced_per_line_by_canonical_id(self, fake):
        stdin = _lines(SEND, {"op": "chat.list"}, {"op": "send", "params": SEND["params"]})
        result = run(["--enable-commands", "message.send", "-a", "work", "batch"], stdin)
        records = _out(result)
        assert [r["ok"] for r in records] == [True, False, True]
        assert records[1]["exit_code"] == 6
        assert records[1]["error"]["code"] == "PERMISSION_DENIED"
        assert fake.ops == ["message.send", "message.send"]
        assert result.exit_code == 6

    def test_batch_level_enable_commands_works_as_well(self, fake):
        result = run(["-a", "work", "batch", "--enable-commands", "chat"], _lines(SEND))
        assert _out(result)[0]["error"]["code"] == "PERMISSION_DENIED"

    def test_a_destructive_line_without_yes_is_refused_and_never_sent(self, fake):
        line = {"op": "message.delete", "params": {"chat": "@alice", "msg_id": [1]}}
        result = run(["-a", "work", "batch"], _lines(line))
        (record,) = _out(result)
        assert record["exit_code"] == 2
        assert record["error"]["field"] == "yes"
        assert fake.calls == []

    def test_yes_on_the_batch_or_on_the_line_confirms(self, fake):
        line = {"op": "message.delete", "params": {"chat": "@alice", "msg_id": [1]}}
        assert run(["-a", "work", "batch", "--yes"], _lines(line)).exit_code == 0
        assert run(["-a", "work", "batch"], _lines({**line, "yes": True})).exit_code == 0
        assert fake.ops == ["message.delete", "message.delete"]

    def test_dry_run_short_circuits_mutating_lines(self, fake):
        result = run(["-a", "work", "batch", "--dry-run"], _lines(SEND))
        (record,) = _out(result)
        assert record["result"]["dry_run"] is True
        assert record["result"]["would"] == "message.send"
        assert fake.calls == []

    def test_a_per_line_dry_run_and_a_destructive_op_needs_no_yes(self, fake):
        line = {
            "op": "message.delete",
            "params": {"chat": "@alice", "msg_id": [1]},
            "dry_run": True,
        }
        result = run(["-a", "work", "batch"], _lines(line, SEND))
        assert [r["ok"] for r in _out(result)] == [True, True]
        assert fake.ops == ["message.send"]

    def test_the_lines_account_beats_the_batch_default(self, fake):
        run(["-a", "work", "batch"], _lines({**SEND, "account": "other"}, SEND))
        assert [call[2].account for call in fake.calls] == ["other", "work"]

    def test_flood_wait_max_reaches_the_daemon_per_line(self, fake):
        stdin = _lines(SEND, {**SEND, "flood_wait_max": 5})
        run(["-a", "work", "batch", "--flood-wait-max", "60"], stdin)
        assert [call[2].flood_wait_max for call in fake.calls] == [60, 5]

    def test_pagination_keys_reach_the_state(self, fake):
        run(["-a", "work", "batch"], _lines({"op": "chat.list", "limit": 3, "cursor": "c"}))
        state = fake.calls[0][2]
        assert (state.limit, state.cursor, state.fetch_all) == (3, "c", False)

    def test_require_account_needs_an_explicit_account(self, fake, stub_account, monkeypatch):
        """The active alias would satisfy resolution; the policy wants it named."""
        monkeypatch.setenv("TLGR_REQUIRE_ACCOUNT", "1")
        refused = _out(run(["batch"], _lines(SEND)))[0]
        assert refused["error"]["field"] == "account"
        assert fake.calls == []
        assert _out(run(["batch"], _lines({**SEND, "account": "work"})))[0]["ok"] is True
        assert _out(run(["-a", "work", "batch"], _lines(SEND)))[0]["ok"] is True

    def test_without_require_account_batch_adds_no_refusal_of_its_own(self, fake, stub_account):
        """Account resolution is `run_op`'s, unchanged: batch only adds the policy check."""
        assert _out(run(["batch"], _lines(SEND)))[0]["ok"] is True
        assert fake.ops == ["message.send"]

    def test_a_production_home_is_refused_before_any_input_is_read(
        self, fake, tlgr_home, monkeypatch
    ):
        from tlgr.core.paths import PRODUCTION_MARKER

        monkeypatch.delenv("TLGR_ALLOW_PRODUCTION_HOME", raising=False)
        (tlgr_home / PRODUCTION_MARKER).touch()
        result = run(["-a", "work", "batch"], _lines(SEND))
        assert result.exit_code == 10
        assert json.loads(result.stdout)["error"]["code"] == "CONFIG_ERROR"
        assert fake.calls == []


class TestAgainstTheLiveDaemon:
    """The real dispatcher, the real socket, a fake Telegram behind it."""

    async def test_sends_land_in_order_over_one_client(
        self, live_daemon, world, in_thread, monkeypatch
    ):
        from fake_telethon import make_user

        from tlgr.transport import client as transport

        world.add_user(make_user(4242, username="alice"))
        probes: list[int] = []
        original = transport.DaemonClient.probe_status

        def counting(self):
            probes.append(1)
            return original(self)

        monkeypatch.setattr(transport.DaemonClient, "probe_status", counting)
        monkeypatch.setattr(gen, "_dispatch", transport.make_dispatcher())
        stdin = _lines(
            {"op": "message.send", "params": {"chat": "@alice", "text": "one"}, "id": 1},
            {"op": "message.send", "params": {"chat": "@alice", "text": "two"}, "id": 2},
            {"op": "message.send", "params": {"chat": "@nobody-here", "text": "x"}, "id": 3},
        )
        result = await in_thread(run, ["-a", "work", "batch"], stdin)
        records = _out(result)
        assert [r["id"] for r in records] == [1, 2, 3]
        assert [r["ok"] for r in records[:2]] == [True, True], result.output
        assert [r["result"]["text"] for r in records[:2]] == ["one", "two"]
        assert records[0]["result"]["id"] < records[1]["result"]["id"]
        assert [m.message for m in world.messages[4242][-2:]] == ["one", "two"]
        assert records[2]["ok"] is False
        assert len(probes) == 1, "the daemon is probed once per batch, not once per line"


class TestReaderGone:
    def test_a_closed_stdout_stops_the_batch(self, fake, capsys):
        """Nobody is left to read what happened, so nothing more is sent."""
        import io

        from tlgr.cli.batch import run_batch
        from tlgr.cli.globals import CliState

        class Closed(io.StringIO):
            def write(self, text):
                raise BrokenPipeError

        source = io.BytesIO(_lines(SEND, SEND, SEND).encode())
        code = run_batch(source, CliState(fmt="json", account="work"), out=Closed())
        assert code == 1
        assert len(fake.calls) == 1
        assert "stdout was closed" in capsys.readouterr().err
