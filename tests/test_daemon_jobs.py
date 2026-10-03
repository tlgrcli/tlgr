"""The daemon starts the jobs in `jobs.yaml` when it boots.

Until 2.0.1 it did not: `reload_jobs()` ran only on `tlgr job reload`, so a
restarted daemon (launchd, an upgrade, a crash) ran no jobs at all and said
nothing about it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fake_telethon import fake_client_factory

JOBS = """\
jobs:
  - name: archive
    account: {account}
    actions:
      - forward:
          to: ["@archive"]
  - name: parked
    account: {account}
    enabled: false
    actions:
      - reply: "parked"
"""


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


class TestJobsAtBoot:
    async def test_enabled_jobs_are_running_once_the_daemon_is_up(
        self, tlgr_home: Path, stub_account: str, world
    ):
        from tlgr.daemon.app import Daemon

        (tlgr_home / "jobs.yaml").write_text(JOBS.format(account=stub_account))
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = asyncio.create_task(daemon.run())
        try:
            await _until(lambda: any(job.get("running") for job in daemon.list_jobs()))
            jobs = {job["name"]: job for job in daemon.list_jobs()}
            assert jobs["archive"]["running"] is True
            assert "parked" not in jobs
        finally:
            daemon.request_shutdown()
            await asyncio.wait_for(runner, timeout=10)

    async def test_a_broken_jobs_file_does_not_stop_the_daemon(
        self, tlgr_home: Path, stub_account: str, world
    ):
        from tlgr.daemon.app import Daemon

        (tlgr_home / "jobs.yaml").write_text("jobs: [unclosed")
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = asyncio.create_task(daemon.run())
        try:
            await _until(lambda: daemon.ready)
            await _until(lambda: daemon._jobs_task is not None and daemon._jobs_task.done())
            assert daemon.list_jobs() == []
        finally:
            daemon.request_shutdown()
            await asyncio.wait_for(runner, timeout=10)


PACED = """\
pacing:
  {account}:
    read: {{every: 3s}}
jobs:
  - name: dm-read
    account: {account}
    filters: {{chat_type: private}}
    actions:
      - read: {{delay: 1h}}
"""


class TestPendingActionsAcrossRestarts:
    async def test_a_pending_action_is_saved_at_shutdown_and_resumed_at_boot(
        self, tlgr_home: Path, stub_account: str, world
    ):
        import json

        from fake_telethon import make_user
        from job_helpers import ALICE, private_update

        from tlgr.daemon.app import Daemon

        world.add_user(make_user(ALICE, username="alice"))
        (tlgr_home / "jobs.yaml").write_text(PACED.format(account=stub_account))

        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = asyncio.create_task(daemon.run())
        try:
            await _until(lambda: any(job.get("running") for job in daemon.list_jobs()))
            scheduler = daemon.schedulers[stub_account]
            assert scheduler.pacers["read"].rule.every == 3.0
            session = daemon.sessions.get(stub_account)
            await session.client.feed(private_update(10))
            await _until(lambda: len(scheduler.items) == 1)
            due = next(iter(scheduler.items.values())).due_at
        finally:
            daemon.request_shutdown()
            await asyncio.wait_for(runner, timeout=10)

        saved = tlgr_home / "accounts" / stub_account / "pending.json"
        assert saved.stat().st_mode & 0o777 == 0o600
        body = json.loads(saved.read_text())
        assert [item["msg_id"] for item in body["items"]] == [10]

        again = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = asyncio.create_task(again.run())
        try:
            await _until(lambda: stub_account in again.schedulers)
            scheduler = again.schedulers[stub_account]
            await _until(lambda: len(scheduler.items) == 1)
            item = next(iter(scheduler.items.values()))
            assert (item.msg_id, item.due_at) == (10, due)
            status = again.v1_status()
            assert status["actions"][stub_account]["pending"] == 1
            assert status["jobs"][0]["actions"][0]["pending"] == 1
        finally:
            again.request_shutdown()
            await asyncio.wait_for(runner, timeout=10)

    async def test_a_broken_edit_keeps_the_running_job(
        self, tlgr_home: Path, stub_account: str, world
    ):
        from tlgr.daemon.app import Daemon

        (tlgr_home / "jobs.yaml").write_text(JOBS.format(account=stub_account))
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = asyncio.create_task(daemon.run())
        try:
            await _until(lambda: any(job.get("running") for job in daemon.list_jobs()))
            (tlgr_home / "jobs.yaml").write_text(
                JOBS.format(account=stub_account).replace("to: [", "too: [")
            )
            result = await daemon.reload_jobs()
            assert result["removed"] == []
            assert any("unknown key" in problem for problem in result["problems"])
            assert {job["name"] for job in daemon.list_jobs()} == {"archive"}
        finally:
            daemon.request_shutdown()
            await asyncio.wait_for(runner, timeout=10)
