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
