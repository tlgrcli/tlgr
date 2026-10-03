"""`job queue list/cancel`, the per-action counters in `job list`/`job get`, and
the validation `job add` and `job reload --validate-only` apply."""

from __future__ import annotations

from typing import Any

import pytest
from fake_telethon import make_user
from job_helpers import ALICE, BOB, daemon_job_env, private_update, until

from tlgr.core.errors import EXIT_USAGE, classify

JOBS = """\
jobs:
  - name: dm-ack
    account: work
    filters: {chat_type: private}
    actions:
      - read: {delay: 1h}
      - react: {emoji: "👍", delay: 2h}
  - name: archive
    account: work
    actions:
      - forward: {to: ["@alice"], delay: 1h}
"""


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


@pytest.fixture
async def queued(live_daemon, world, tlgr_home):
    world.add_user(make_user(ALICE, username="alice"))
    world.add_user(make_user(BOB, username="bobby"))
    env = await daemon_job_env(live_daemon)
    (tlgr_home / "jobs.yaml").write_text(JOBS)
    await live_daemon.reload_jobs()
    client = live_daemon.sessions.get("work").client
    await client.feed(private_update(10, user=ALICE))
    await client.feed(private_update(11, user=BOB))
    await until(lambda: len(env.scheduler.items) == 6)
    return env


class TestQueueList:
    async def test_every_pending_action_is_listed_soonest_first(self, queued, client, in_thread):
        rows = await result(client, in_thread, "job.queue.list")
        assert len(rows) == 6
        assert [row["state"] for row in rows] == ["waiting"] * 6
        assert {row["action"] for row in rows} == {"read", "react", "forward"}
        etas = [row["eta_s"] for row in rows]
        assert etas == sorted(etas)
        react = next(row for row in rows if row["action"] == "react")
        assert react["detail"] == "👍"
        assert react["due_at"].endswith("Z")

    async def test_filters(self, queued, client, in_thread):
        rows = await result(client, in_thread, "job.queue.list", {"job": "archive"})
        assert {row["action"] for row in rows} == {"forward"}
        rows = await result(client, in_thread, "job.queue.list", {"chat": "@bobby"})
        assert {row["chat_id"] for row in rows} == {BOB}
        rows = await result(client, in_thread, "job.queue.list", {"chat": str(ALICE)})
        assert len(rows) == 3


class TestQueueCancel:
    async def test_by_id(self, queued, client, in_thread):
        rows = await result(client, in_thread, "job.queue.list")
        body = await result(client, in_thread, "job.queue.cancel", {"ids": [rows[0]["id"]]})
        assert body == {"cancelled": 1, "ids": [rows[0]["id"]]}
        assert len(queued.scheduler.items) == 5

    async def test_by_chat_and_job(self, queued, client, in_thread):
        body = await result(
            client, in_thread, "job.queue.cancel", {"chat": "@alice", "job": "dm-ack"}
        )
        assert body["cancelled"] == 2
        remaining = {(item.job, item.chat_id) for item in queued.scheduler.items.values()}
        assert ("dm-ack", ALICE) not in remaining

    async def test_all(self, queued, client, in_thread):
        body = await result(client, in_thread, "job.queue.cancel", {"every": True})
        assert body["cancelled"] == 6
        assert queued.scheduler.items == {}

    async def test_a_selector_is_required(self, queued, client, in_thread):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "job.queue.cancel", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_the_cli_wants_yes_off_a_terminal(self, queued, in_thread):
        from click.testing import CliRunner

        from tlgr.cli import cli

        outcome = await in_thread(CliRunner().invoke, cli, ["job", "queue", "cancel", "--all"])
        assert outcome.exit_code == EXIT_USAGE
        assert len(queued.scheduler.items) == 6
        outcome = await in_thread(
            CliRunner().invoke, cli, ["job", "queue", "cancel", "--all", "--yes"]
        )
        assert outcome.exit_code == 0, outcome.output
        assert queued.scheduler.items == {}

    async def test_bare_job_queue_lists(self, queued, in_thread):
        from click.testing import CliRunner

        from tlgr.cli import cli

        outcome = await in_thread(CliRunner().invoke, cli, ["job", "queue"])
        assert outcome.exit_code == 0, outcome.output
        assert "dm-ack" in outcome.output
        assert "archive" in outcome.output


class TestCounters:
    async def test_job_list_and_get_report_per_action_counters(self, queued, client, in_thread):
        queued.scheduler.cancel(chat_id=BOB, job="dm-ack")
        rows = await result(client, in_thread, "job.list")
        ack = next(row for row in rows if row["name"] == "dm-ack")
        assert ack["running"] is True
        assert ack["matched"] == 2
        assert ack["pending"] == 2
        assert ack["superseded"] == 2
        by_action = {c["action"]: c for c in ack["action_counters"]}
        assert by_action["read"]["pending"] == 1
        assert by_action["react"]["superseded"] == 1
        for key in ("done", "skipped", "expired", "errors"):
            assert by_action["read"][key] == 0

        state = await result(client, in_thread, "job.get", {"name": "dm-ack"})
        assert state["pending"] == 2
        assert [c["action"] for c in state["action_counters"]] == ["read", "react"]

    async def test_daemon_status_carries_the_queue(self, queued, client, in_thread):
        status = await in_thread(client.status)
        assert status["actions"]["work"]["pending"] == 6
        assert status["actions"]["work"]["by_action"]["react"] == 2


class TestValidation:
    async def test_job_add_refuses_bad_knobs(self, live_daemon, client, in_thread):
        bad = {
            "name": "dm",
            "action": ["react:emoji=👍,percent=150"],
        }
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "job.add", bad)
        error = classify(caught.value)
        assert error.exit_code == EXIT_USAGE
        assert "percent" in error.message

    async def test_job_add_accepts_the_new_actions(self, live_daemon, client, in_thread, tlgr_home):
        body = {
            "name": "dm",
            "action": ["read:delay=10-90s", "react:emoji=👍", "view"],
            "knob": ["presence=session", "on_takeover=cancel_read"],
        }
        await call(client, in_thread, "job.add", body)
        import yaml

        saved = yaml.safe_load((tlgr_home / "jobs.yaml").read_text())["jobs"][0]
        assert saved["actions"] == [
            {"read": {"delay": "10-90s"}},
            {"react": {"emoji": "👍"}},
            {"view": {}},
        ]
        assert saved["presence"] == "session"

    async def test_reload_validate_only_reports_every_problem(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        (tlgr_home / "jobs.yaml").write_text(
            "pacing:\n  work:\n    react: {every: soon}\n"
            "jobs:\n"
            "  - name: a\n    actions: [{react: {emoji: '👍', delay: 'forever'}}]\n"
            "  - name: b\n    presence: loud\n    actions: [{read: {}}]\n"
        )
        body = await result(client, in_thread, "job.reload", {"validate_only": True})
        errors = " | ".join(body["errors"])
        assert "pacing.work.react.every" in errors
        assert "job 'a', action 1: delay" in errors
        assert "job 'b': presence: unknown mode 'loud'" in errors
