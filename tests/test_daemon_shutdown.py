"""A stop finishes in seconds, whatever is still connected, and the process exits.

Production, 2026-11-21: `tlgr daemon restart` failed with "the daemon did not
stop within 10s". The daemon spent twenty more seconds draining after the
restart client had given up, and then never exited: every account reported
stopped and the process sat in `S` until launchd killed it. Two things held
it, and both are reproduced here with the fake client:

* the drain waited for requests that never end on their own (an open
  `events.watch` stream, a job action stuck on Telegram), for the config's
  `drain_seconds` rather than the `drain_s` the stopping client asked for;
* the IPC server's cleanup waited aiohttp's default 60 seconds for a stream
  handler that did not notice the bus had closed its subscription.

Every test bounds the stop with `STOP_BUDGET_S`. Before the fix each of them
ran into that bound (a 30 second drain plus a 60 second server cleanup).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import aiohttp
from fake_telethon import fake_client_factory, make_user
from job_helpers import ALICE, private_update

#: How long a stop may take with something still connected. The restart
#: client waits 10 seconds; this leaves the daemon well inside that.
STOP_BUDGET_S = 5.0


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


def _hang_reads(world) -> asyncio.Event:
    """Make every `ReadHistoryRequest` wait until the returned event is set.

    This is a Telegram call that does not come back: the shape of an action
    stuck behind a slow DC or an updates difference, in flight when the stop
    arrives.
    """
    release = asyncio.Event()

    async def hang(request):
        await release.wait()
        return None

    world.raw["ReadHistoryRequest"] = hang
    return release


def _session(daemon) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(daemon.paths.socket)))


async def _start(daemon) -> asyncio.Task:
    runner = asyncio.create_task(daemon.run())
    await _until(lambda: daemon.ready)
    return runner


async def _stopped_within(runner: asyncio.Task, budget: float = STOP_BUDGET_S) -> float:
    started = time.monotonic()
    try:
        await asyncio.wait_for(runner, timeout=budget)
    except (TimeoutError, asyncio.TimeoutError):
        raise AssertionError(f"the daemon was still stopping after {budget}s") from None
    return time.monotonic() - started


class TestTheDrain:
    async def test_admin_stop_drains_for_the_time_the_caller_asked_for(
        self, tlgr_home: Path, stub_account: str, world
    ):
        """`daemon stop --grace 10` waits 10s; the daemon used to drain for 30."""
        from tlgr.daemon.app import Daemon

        world.add_user(make_user(ALICE, username="alice"))
        release = _hang_reads(world)
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        assert daemon.config.daemon.drain_seconds > STOP_BUDGET_S
        runner = await _start(daemon)
        try:
            async with _session(daemon) as http:
                stuck = asyncio.create_task(
                    http.post(
                        "http://tlgr/v1/op",
                        json={
                            "op": "message.read",
                            "account": stub_account,
                            "request": {"chat": str(ALICE)},
                        },
                    )
                )
                await _until(lambda: daemon.activity.in_flight >= 1)
                async with http.post("http://tlgr/v1/admin/stop", json={"drain_s": 0.5}) as reply:
                    assert (await reply.json())["stopping"] is True
                elapsed = await _stopped_within(runner)
                # The request that never finished was cancelled, not left
                # holding the socket, and its caller is told to retry rather
                # than seeing the connection drop.
                response = await asyncio.wait_for(stuck, timeout=STOP_BUDGET_S)
                body = await response.json()
                assert body["ok"] is False
                assert body["error"]["code"] == "RETRYABLE", body
            assert elapsed < STOP_BUDGET_S
            assert not daemon.paths.socket.exists()
        finally:
            release.set()
            if not runner.done():
                runner.cancel()

    async def test_a_walk_cut_off_by_the_drain_ends_with_a_retryable_frame(
        self, tlgr_home: Path, stub_account: str, world
    ):
        """A `--all` stream the drain gives up on says so in its own framing."""
        from tlgr.daemon.app import Daemon

        release = asyncio.Event()

        async def hang(request):
            await release.wait()

        world.raw["GetDialogsRequest"] = hang
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = await _start(daemon)
        try:
            async with _session(daemon) as http:
                walk = await http.post(
                    "http://tlgr/v1/op",
                    json={"op": "chat.list", "account": stub_account, "all": True},
                )
                assert walk.status == 200
                await _until(lambda: daemon.activity.in_flight >= 1)
                async with http.post("http://tlgr/v1/admin/stop", json={"drain_s": 0.2}):
                    pass
                elapsed = await _stopped_within(runner)
                lines = [json.loads(line) for line in (await walk.text()).splitlines() if line]
            assert elapsed < STOP_BUDGET_S
            assert lines[-1]["type"] == "end", lines
            assert lines[-1]["ok"] is False
            assert lines[-1]["error"]["code"] == "RETRYABLE"
        finally:
            release.set()
            if not runner.done():
                runner.cancel()

    async def test_a_job_action_stuck_on_telegram_does_not_hold_the_stop(
        self, tlgr_home: Path, stub_account: str, world
    ):
        """The gateway scheduler's in-flight action is cut short and kept for next time."""
        from tlgr.daemon.app import Daemon

        world.add_user(make_user(ALICE, username="alice"))
        release = _hang_reads(world)
        (tlgr_home / "jobs.yaml").write_text(
            textwrap.dedent(
                f"""\
                jobs:
                  - name: autoread
                    account: {stub_account}
                    filters: {{chat_type: private}}
                    actions:
                      - read: {{}}
                """
            )
        )
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = await _start(daemon)
        try:
            await _until(lambda: any(job.get("running") for job in daemon.list_jobs()))
            scheduler = daemon.schedulers[stub_account]
            session = daemon.sessions.get(stub_account)
            await session.client.feed(private_update(10))
            await _until(lambda: bool(scheduler._running))
            assert daemon.activity.in_flight >= 1

            # The signal path: no drain_s, so the config's 30s default applies.
            daemon.request_shutdown()
            elapsed = await _stopped_within(runner)
            assert elapsed < STOP_BUDGET_S
        finally:
            release.set()
            if not runner.done():
                runner.cancel()

        # Cut short, not lost: the action resumes at the next start.
        saved = json.loads((tlgr_home / "accounts" / stub_account / "pending.json").read_text())
        assert [item["msg_id"] for item in saved["items"]] == [10]


class TestOpenStreams:
    async def test_an_open_event_stream_ends_cleanly_and_the_daemon_stops(
        self, tlgr_home: Path, stub_account: str, world
    ):
        """`tlgr watch` held the drain for 30s and the server cleanup for 60s more."""
        from tlgr.daemon.app import Daemon

        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        runner = await _start(daemon)
        frames: list[dict] = []
        try:
            async with _session(daemon) as http:
                response = await http.get(
                    "http://tlgr/v1/events",
                    params={"account": stub_account, "heartbeat": "1"},
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
                )
                assert response.status == 200

                async def read_all() -> None:
                    async for line in response.content:
                        if line.strip():
                            frames.append(json.loads(line))

                reader = asyncio.create_task(read_all())
                await _until(lambda: any(f.get("type") == "watching" for f in frames))

                daemon.request_shutdown()
                elapsed = await _stopped_within(runner)
                await asyncio.wait_for(reader, timeout=STOP_BUDGET_S)
            assert elapsed < STOP_BUDGET_S
            # Ended with the stream's own `end` frame, so a client can tell a
            # stop from a dropped connection.
            assert frames[-1]["type"] == "end", frames
            assert frames[-1].get("ok") is True
            assert frames[-1].get("reason") == "shutdown"  # ARCHITECTURE §6.11 step 2
            assert daemon.activity.event_streams == 0
        finally:
            if not runner.done():
                runner.cancel()


class TestSignals:
    async def test_a_second_signal_stops_without_waiting_for_the_teardown(
        self, tlgr_home: Path, stub_account: str, world
    ):
        """An operator who signals twice gets an exit, not another 60s wait."""
        from tlgr.daemon import main as daemon_main
        from tlgr.daemon.app import Daemon

        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        hung = asyncio.Event()

        async def a_teardown_that_never_ends(*, drain=None) -> None:
            hung.set()
            await asyncio.Event().wait()

        daemon.shutdown = a_teardown_that_never_ends  # type: ignore[method-assign]
        runner = asyncio.create_task(daemon_main.run(daemon))
        try:
            await _until(lambda: daemon.ready)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(hung.wait(), timeout=STOP_BUDGET_S)
            os.kill(os.getpid(), signal.SIGTERM)
            await _stopped_within(runner, budget=2.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)


_WATCHER = """\
import sys
from tlgr.transport.client import DaemonClient

client = DaemonClient(sys.argv[1], auto_start=False)
for frame in client.stream("GET", "/v1/events", params={"account": "all", "heartbeat": 1}):
    print(frame.get("type"), flush=True)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_real_daemon_with_a_watcher_attached_stops_with_one_command(tlgr_home: Path):
    """The whole restart path in real processes: one `daemon stop`, then no process.

    Before the fix this is the production failure exactly: `daemon stop`
    reports "did not stop within 10s" and the process outlives it.
    """
    from tlgr.core.process import read_pid
    from tlgr.transport.client import DaemonClient

    status = DaemonClient(tlgr_home, auto_start=True).status()
    pid = int(status["daemon"]["pid"])
    assert read_pid(tlgr_home) == pid  # ours: started under this temp home
    env = {**os.environ, "TLGR_HOME": str(tlgr_home)}
    watcher = subprocess.Popen(
        [sys.executable, "-c", _WATCHER, str(tlgr_home)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert watcher.stdout is not None
        for _ in range(3):
            if watcher.stdout.readline().strip() == "watching":
                break
        else:
            raise AssertionError("the watcher never started streaming")

        started = time.monotonic()
        stop = subprocess.run(
            [sys.executable, "-m", "tlgr", "--json", "daemon", "stop"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        elapsed = time.monotonic() - started
        assert stop.returncode == 0, stop.stdout + stop.stderr
        assert elapsed < STOP_BUDGET_S, f"daemon stop took {elapsed:.1f}s"
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.05)
        assert not _alive(pid), "the daemon process outlived `daemon stop`"
        # The watcher saw a clean end, not a reset, and exited on its own.
        out, err = watcher.communicate(timeout=STOP_BUDGET_S)
        assert watcher.returncode == 0, err
        assert out.split()[-1:] == ["end"], out
    finally:
        if watcher.poll() is None:
            watcher.kill()
            watcher.wait()
        if _alive(pid) and read_pid(tlgr_home) == pid:
            os.kill(pid, signal.SIGKILL)
