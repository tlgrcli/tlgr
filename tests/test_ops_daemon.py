"""The `daemon`, `sync`, `net`, `config` and `job` operations, end to end.

Everything here goes over a real Unix socket, through the real middleware and
dispatcher, into the real implementation, against a fake Telegram. The
assertions are mostly about *what was sent* — the exact TL request the fake
recorded — because for this group the interesting failures are requests that
were never made (a catch-up that skipped a channel) or made wrongly (a
difference probe that advanced the stored pts).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tlgr.core.errors import EXIT_NOT_FOUND, EXIT_USAGE, classify

CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL


@pytest.fixture
def peers(world):
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(4242, username="alice"))
    world.add_channel(make_channel(CHANNEL, title="News"))
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


def local(op_id: str, request: dict[str, Any] | None = None, **state: Any) -> Any:
    """Run a `Surface.LOCAL` operation the way the CLI does — synchronously."""
    import msgspec

    from tlgr.cli.gen import LocalContext
    from tlgr.registry import get

    spec = get(op_id)
    context = LocalContext(account=state.pop("account", ""))
    for key, value in state.items():
        setattr(context, key, value)
    payload = msgspec.convert(request or {}, type=spec.request, strict=False)
    return asyncio.run(spec.impl(context, payload))


def _human(*args: str) -> dict[str, str]:
    """Run a command through the real CLI and read back its key/value table.

    Through click rather than the renderer directly: the bug being pinned was
    a model that serialised to `{}`, which no assertion on the *object* can
    see and which printed an empty screen.
    """
    from click.testing import CliRunner

    from tlgr.cli import cli

    outcome = CliRunner().invoke(cli, list(args))
    assert outcome.exit_code == 0, outcome.output
    assert not outcome.output.lstrip().startswith("{"), "human mode printed an envelope"
    rows: dict[str, str] = {}
    for line in outcome.output.splitlines():
        key, _, value = line.partition("  ")
        if key:
            rows[key.strip()] = value.strip()
    return rows


async def alocal(in_thread, op_id: str, request: dict[str, Any] | None = None, **state: Any):
    """`local`, off the event loop.

    A local operation runs in the *client* process and talks to the daemon
    over a blocking socket. Calling it inline from an async test would block
    the very loop the daemon is serving on — a deadlock, not a slow test — so
    it goes to an executor exactly as the CLI's own process does.
    """
    return await in_thread(local, op_id, request, **state)


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


class TestDaemonStatus:
    async def test_it_separates_running_ready_and_healthy(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        """COR-37: v1 had only `running`, so a deaf daemon looked fine."""
        status = await alocal(in_thread, "daemon.status", {}, account="work")
        assert status.running is True
        assert status.ready is True
        assert {row.alias for row in status.accounts} == {"work"}

    def test_a_stopped_daemon_is_reported_not_guessed(self, tlgr_home, stub_account):
        status = local("daemon.status")
        assert status.running is False
        assert status.ready is False
        assert status.healthy is False

    def test_a_stopped_daemon_serialises_the_false_answers(self, tlgr_home, stub_account):
        """`omit_defaults` dropped every false and every zero, so the whole
        result encoded to `{}` — the one shape this operation must never
        return, because "no" is the answer it exists to give."""
        import msgspec

        payload = msgspec.to_builtins(local("daemon.status"))
        assert payload["running"] is False
        assert payload["ready"] is False
        assert payload["healthy"] is False
        assert payload["pid"] is None
        assert payload["version"] is None
        assert payload["socket"].endswith("daemon.sock")

    def test_a_stopped_daemon_prints_a_table_not_an_envelope(self, tlgr_home, stub_account):
        """Human mode is the key/value table every other op renders; v1
        printed `RUNNING false` and the empty result printed nothing."""
        rendered = _human("daemon", "status")
        assert rendered["running"] == "no"
        assert rendered["ready"] == "no"
        assert rendered["healthy"] == "no"
        assert rendered["pid"] == "-"

    def test_the_status_shortcut_answers_the_same_way(self, tlgr_home, stub_account):
        """`tlgr status` is a different operation asking the same question."""
        rendered = _human("status")
        assert rendered["daemon_running"] == "no"
        assert rendered["connected"] == "no"
        assert "the daemon is not running" in rendered["problems"]

    def test_check_turns_an_unhealthy_daemon_into_an_exit_code(self, tlgr_home, stub_account):
        from tlgr.core.errors import DaemonNotRunningError

        with pytest.raises(DaemonNotRunningError):
            local("daemon.status", {"check": True})


class TestDaemonFloods:
    async def test_a_remembered_deadline_is_listed(self, live_daemon, client, in_thread):
        """The store Telethon forgets on exit: v1 re-hit every wait on restart."""
        live_daemon.sessions.limiter("work").note_flood("SendMessageRequest", 41, peer=4242)
        items = await result(client, in_thread, "daemon.flood.list")
        assert items and items[0]["method"] == "SendMessageRequest"
        assert items[0]["wait_seconds"] > 0
        assert items[0]["kind"] == "flood_wait"

    async def test_clearing_needs_to_be_told_what_to_clear(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "daemon.flood.clear", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_clearing_everything_forgets_the_deadlines(self, live_daemon, client, in_thread):
        limiter = live_daemon.sessions.limiter("work")
        limiter.note_flood("SendMessageRequest", 41)
        limiter.trip("peer flood")
        cleared = await result(client, in_thread, "daemon.flood.clear", {"everything": True})
        assert cleared["cleared"] == 1
        assert limiter.breaker.open is False
        assert limiter.flood.entries() == []


class TestDeadLetters:
    async def test_an_empty_store_exits_empty(self, live_daemon, client, in_thread):
        items = await result(client, in_thread, "daemon.dead-letter.list")
        assert items == []

    async def test_entries_are_listed_and_deletable(self, live_daemon, client, in_thread):
        live_daemon.webhook.write_dead_letters(
            [
                {
                    "ts": "2026-09-03T09:00:00Z",
                    "first_failed_at": "2026-09-03T08:00:00Z",
                    "reason": "HTTP 502",
                    "source": "webhook",
                    "attempts": 3,
                    "delivery_id": "abc",
                    "seq": 12,
                    "event": "message_new",
                    "account": "work",
                    "body": "{}",
                }
            ]
        )
        items = await result(client, in_thread, "daemon.dead-letter.list")
        assert [row["id"] for row in items] == ["abc"]
        assert items[0]["attempts"] == 3

        deleted = await result(client, in_thread, "daemon.dead-letter.delete", {"id": ["abc"]})
        assert deleted["deleted"] == 1
        assert deleted.get("remaining", 0) == 0

    async def test_deleting_needs_a_selector(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "daemon.dead-letter.delete", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestSaveState:
    async def test_it_flushes_the_session(self, live_daemon, client, in_thread, world):
        before = world.saves
        saved = await result(client, in_thread, "daemon.save-state")
        assert saved["accounts"][0]["alias"] == "work"
        assert saved["accounts"][0]["pts"] == 91824
        assert world.saves > before


class TestReconnect:
    async def test_it_reconnects_and_catches_up(self, live_daemon, client, in_thread, world):
        before = world.catch_ups
        report = await result(client, in_thread, "daemon.reconnect")
        row = report["accounts"][0]
        assert row["alias"] == "work"
        assert row["reconnected"] is True
        assert row["caught_up"] is True
        assert world.catch_ups > before

    async def test_no_catch_up_skips_the_difference(self, live_daemon, client, in_thread, world):
        before = world.catch_ups
        report = await result(client, in_thread, "daemon.reconnect", {"catch_up": False})
        assert report["accounts"][0].get("caught_up", False) is False
        assert world.catch_ups == before


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


class TestSyncStatus:
    async def test_it_reports_the_cursors(self, live_daemon, client, in_thread):
        status = await result(client, in_thread, "sync.status")
        assert status["pts"] == 91824
        assert status["qts"] == 12
        assert status["seq"] == 4410

    async def test_refresh_reports_the_server_delta(self, live_daemon, client, in_thread, world):
        world.server_ahead = 40
        status = await result(client, in_thread, "sync.status", {"refresh": True})
        assert status["server_pts"] == 91864
        assert status["behind_pts"] == 40

    async def test_a_channel_without_an_access_hash_is_flagged(
        self, live_daemon, client, in_thread, world
    ):
        """Catch-up skips such a channel silently; it must not look idle."""
        world.update_state[CHANNEL] = (42, 0, 0)
        envelope = await call(client, in_thread, "sync.status", {"channels": True})
        rows = envelope["result"]["channels"]
        assert any(row["chat_id"] == CHANNEL_ID for row in rows)
        assert any("access hash" in warning for warning in envelope["meta"]["warnings"])


class TestSyncCatchUp:
    async def test_it_forces_a_difference(self, live_daemon, client, in_thread, world):
        before = world.catch_ups
        report = await result(client, in_thread, "sync.catch-up")
        assert report["account"] == "work"
        assert world.catch_ups > before


class TestSyncDifference:
    async def test_a_probe_does_not_advance_the_stored_pts(
        self, live_daemon, client, in_thread, world
    ):
        """The safety property: a diagnostic cannot create the gap it looks for."""
        before = dict(world.update_state)
        report = await result(client, in_thread, "sync.difference")
        assert report["kind"] == "common"
        assert report["final"] is True
        assert report["dry_run"] is True
        assert world.update_state == before
        assert world.called("GetDifferenceRequest")

    async def test_a_channel_difference_uses_the_channel_request(
        self, live_daemon, client, in_thread, peers
    ):
        report = await result(client, in_thread, "sync.difference", {"chat": str(CHANNEL_ID)})
        assert report["kind"] == "channel"
        request = peers.called("GetChannelDifferenceRequest")[0]
        assert request.force is True
        assert 1 <= request.limit <= 100

    async def test_a_private_chat_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "sync.difference", {"chat": "@alice"})
        error = classify(caught.value)
        assert error.exit_code == EXIT_USAGE


class TestSyncReset:
    async def test_it_re_baselines_from_the_server(self, live_daemon, client, in_thread, world):
        world.server_ahead = 100
        report = await result(client, in_thread, "sync.reset")
        assert report["reset"] is True
        assert report["pts_before"] == 91824
        assert report["pts_after"] == 91924
        assert world.update_state[0][0] == 91924


class TestSyncBackfill:
    async def test_it_fetches_by_explicit_id(self, live_daemon, client, in_thread, peers):
        for index in range(5):
            peers.add_message(CHANNEL_ID, f"post {index}", message_id=100 + index)

        def read() -> list[dict[str, Any]]:
            frames = []
            for frame in client.op_stream(
                "sync.backfill",
                {"chat": str(CHANNEL_ID), "from_id": 100, "to_id": 104},
                account="work",
            ):
                frames.append(frame)
                if frame.get("type") == "end":
                    break
            return frames

        client._ready = True
        frames = await in_thread(read)
        items = [f["data"] for f in frames if f.get("type") == "item"]
        assert [item["id"] for item in items] == [100, 101, 102, 103, 104]

    async def test_a_range_is_required(self, live_daemon, client, in_thread, peers):
        def read() -> list[dict[str, Any]]:
            return list(
                client.op_stream("sync.backfill", {"chat": str(CHANNEL_ID)}, account="work")
            )

        client._ready = True
        frames = await in_thread(read)
        assert frames[-1]["error"]["exit_code"] == EXIT_USAGE


# ---------------------------------------------------------------------------
# net
# ---------------------------------------------------------------------------


class TestNet:
    async def test_dc_list_marks_the_current_one(self, live_daemon, client, in_thread):
        items = await result(client, in_thread, "net.dc.list")
        assert [row["id"] for row in items] == [4, 4, 2]
        # `current` is False by default and `Model` omits defaults, so the
        # media-only DC simply has no key rather than a false one.
        assert [row.get("current", False) for row in items] == [True, True, False]

    async def test_the_ipv6_filter_narrows_it(self, live_daemon, client, in_thread):
        items = await result(client, in_thread, "net.dc.list", {"ipv6": True})
        assert len(items) == 1 and items[0]["ipv6"] is True

    async def test_test_dcs_need_no_connection(self, live_daemon, client, in_thread):
        items = await result(client, in_thread, "net.dc.list", {"test": True})
        assert {row["id"] for row in items} == {1, 2, 3}

    async def test_resolve_says_it_is_not_implemented(self, live_daemon, client, in_thread):
        """A --resolve that quietly did nothing would be worse than one that says so."""
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "net.dc.list", {"resolve": True})
        assert classify(caught.value).code == "NOT_SUPPORTED"

    async def test_nearest_dc(self, live_daemon, client, in_thread):
        report = await result(client, in_thread, "net.dc.nearest")
        assert report["country"] == "GB"
        assert report["nearest_dc"] == 4

    async def test_ping_reports_every_probe(self, live_daemon, client, in_thread, world):
        report = await result(client, in_thread, "net.ping", {"probes": 2})
        assert report["probes"] == 2
        assert report["loss"] == 0.0
        assert len(world.called("GetNearestDcRequest")) == 2

    async def test_status_reports_the_connection(self, live_daemon, client, in_thread):
        report = await result(client, in_thread, "net.status", {"ping": False})
        assert report["connected"] is True
        assert report["dc_id"] == 4
        assert report["layer"] > 0
        assert report["state"]["pts"] == 91824


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfigKeys:
    def test_the_catalogue_is_machine_readable(self, tlgr_home):
        page = local("config.keys", {}, limit=200)
        keys = {row.key for row in page.items}
        assert {"presence.mode", "daemon.idle_timeout", "flood.sleep_threshold"} <= keys

    def test_a_section_filter_narrows_it(self, tlgr_home):
        page = local("config.keys", {"section": "presence"}, limit=200)
        assert [row.key for row in page.items] == ["presence.mode"]


class TestConfigSet:
    def test_a_value_round_trips(self, tlgr_home):
        written = local("config.set", {"key": "daemon.idle_timeout", "value": "0"})
        assert written.updated is True
        assert written.value == 0
        read = local("config.get", {"key": "daemon.idle_timeout"})
        assert read.value == 0
        assert read.source == "file"

    def test_setting_it_twice_reports_already(self, tlgr_home):
        local("config.set", {"key": "daemon.idle_timeout", "value": "0"})
        again = local("config.set", {"key": "daemon.idle_timeout", "value": "0"})
        assert again.already is True

    def test_a_wrong_type_is_a_usage_error_not_a_silent_default(self, tlgr_home):
        """The whole reason the catalogue exists (v1 swallowed this)."""
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError) as caught:
            local("config.set", {"key": "daemon.idle_timeout", "value": "soon"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    def test_an_unknown_key_is_not_found(self, tlgr_home):
        from tlgr.core.errors import NotFoundError

        with pytest.raises(NotFoundError) as caught:
            local("config.set", {"key": "daemon.nonsense", "value": "1"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    def test_a_v1_key_name_still_works(self, tlgr_home):
        """§12.4: `tlgr config set idle_timeout 0` is a documented spelling."""
        written = local("config.set", {"key": "idle_timeout", "value": "0"})
        assert written.key == "daemon.idle_timeout"

    def test_a_choice_is_enforced(self, tlgr_home):
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError):
            local("config.set", {"key": "presence.mode", "value": "ghost"})

    def test_unset_reverts_to_the_default(self, tlgr_home):
        local("config.set", {"key": "daemon.idle_timeout", "value": "0"})
        removed = local("config.unset", {"key": "daemon.idle_timeout"})
        assert removed.removed is True
        assert local("config.get", {"key": "daemon.idle_timeout"}).value == 1800

    def test_a_secret_is_redacted_in_a_listing(self, tlgr_home):
        local("config.set", {"key": "net.proxy", "value": "socks5://user:pw@10.0.0.5:1080"})
        rows = {row.key: row.value for row in local("config.list", {}, limit=200).items}
        assert rows["net.proxy"] == "<redacted>"


class TestConfigValidate:
    def test_a_clean_tree_validates(self, tlgr_home):
        local("config.init", {})
        report = local("config.validate", {})
        assert report.ok is True

    def test_an_unknown_key_is_a_warning_and_strict_makes_it_an_error(self, tlgr_home):
        (tlgr_home / "config.toml").write_text("[daemon]\nnonsense = 1\n")
        report = local("config.validate", {})
        assert report.ok is True
        assert any(issue.key == "daemon.nonsense" for issue in report.warnings)
        strict = local("config.validate", {"strict": True})
        assert strict.ok is False

    def test_an_unknown_event_name_in_the_webhook_is_an_error(self, tlgr_home):
        (tlgr_home / "webhook.toml").write_text(
            '[webhook]\nenabled = false\nevents = ["message_exploded"]\n'
        )
        report = local("config.validate", {"file": "webhook"})
        assert report.ok is False
        assert "unknown event type" in report.errors[0].message


class TestConfigServer:
    async def test_it_reads_the_server_limits(self, live_daemon, client, in_thread):
        report = await result(client, in_thread, "config.server.get")
        assert report["message_length_max"] == 4096
        assert report["edit_time_limit"] == 172800

    async def test_dc_options_are_opt_in(self, live_daemon, client, in_thread):
        without = await result(client, in_thread, "config.server.get")
        assert "dc_options" not in without
        with_them = await result(client, in_thread, "config.server.get", {"dc_options": True})
        assert len(with_them["dc_options"]) == 3


class TestConfigApp:
    async def test_the_json_object_is_flattened(self, live_daemon, client, in_thread):
        report = await result(client, in_thread, "config.app.get")
        assert report["values"]["reactions_user_max_default"] == 1

    async def test_frozen_narrows_to_the_freeze_fields(self, live_daemon, client, in_thread):
        report = await result(client, in_thread, "config.app.get", {"frozen": True})
        assert set(report["values"]) == {"freeze_appeal_url"}
        assert report["freeze_appeal_url"] == "https://t.me/spambot"


class TestConfigCountries:
    async def test_a_phone_is_classified(self, live_daemon, client, in_thread):
        items = await result(client, in_thread, "config.country.list", {"phone": "+447700900000"})
        assert [row["iso2"] for row in items] == ["GB"]
        assert items[0]["matched_prefix"] == "447"
        assert items[0]["flag_emoji"] == "🇬🇧"

    async def test_an_unclaimed_prefix_is_not_found(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "config.country.list", {"phone": "+999123"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------


class TestJobs:
    def test_a_job_is_added_from_flags(self, tlgr_home):
        added = local(
            "job.add",
            {"name": "archive", "action": ["forward:to=@archive"], "events": "new_message"},
        )
        assert added.name == "archive"
        state = local("job.get", {"name": "archive"})
        assert state.actions == [{"forward": {"to": "@archive"}}]

    def test_a_job_with_no_actions_is_refused(self, tlgr_home):
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError):
            local("job.add", {"name": "empty"})

    def test_an_unknown_event_is_refused_at_write_time(self, tlgr_home):
        """v1 dropped the name at load time, so the job never fired and never said why."""
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError):
            local("job.add", {"name": "x", "action": ["reply:hi"], "events": "message_exploded"})

    def test_adding_the_same_name_twice_is_refused(self, tlgr_home):
        from tlgr.core.errors import UsageError

        local("job.add", {"name": "archive", "action": ["reply:hi"]})
        with pytest.raises(UsageError):
            local("job.add", {"name": "archive", "action": ["reply:hi"]})

    def test_an_unknown_job_is_not_found(self, tlgr_home):
        from tlgr.core.errors import NotFoundError

        with pytest.raises(NotFoundError) as caught:
            local("job.get", {"name": "ghost"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    def test_explain_names_an_unregistered_filter(self, tlgr_home):
        local(
            "job.add",
            {"name": "archive", "action": ["reply:hi"], "filter": ["nonsense=1"]},
        )
        state = local("job.get", {"name": "archive", "explain": True})
        assert "UNKNOWN" in state.filters["nonsense"]["resolves_to"]

    async def test_enable_and_disable_round_trip(self, live_daemon, client, in_thread, tlgr_home):
        await alocal(in_thread, "job.add", {"name": "archive", "action": ["reply:hi"]})
        disabled = await result(client, in_thread, "job.disable", {"name": "archive"})
        assert disabled["enabled"] is False
        again = await result(client, in_thread, "job.disable", {"name": "archive"})
        assert again["already"] is True
        enabled = await result(client, in_thread, "job.enable", {"name": "archive"})
        assert enabled["enabled"] is True

    async def test_removing_a_job_takes_it_out_of_the_file(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        await alocal(in_thread, "job.add", {"name": "archive", "action": ["reply:hi"]})
        removed = await result(client, in_thread, "job.remove", {"name": "archive"})
        assert removed["removed"] is True
        items = await result(client, in_thread, "job.list")
        assert items == []

    async def test_adding_one_job_keeps_the_others(self, live_daemon, client, in_thread, tlgr_home):
        """A rewrite that lost unmodelled filters would silently delete rules."""
        await alocal(
            in_thread,
            "job.add",
            {"name": "first", "action": ["reply:hi"], "filter": ["chat_type=private"]},
        )
        await alocal(in_thread, "job.add", {"name": "second", "action": ["reply:yo"]})
        assert {row["name"] for row in await result(client, in_thread, "job.list")} == {
            "first",
            "second",
        }
        first = await alocal(in_thread, "job.get", {"name": "first"})
        assert first.filters == {"chat_type": "private"}


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExport:
    async def test_a_session_wraps_every_later_call(self, live_daemon, client, in_thread, world):
        """A call that forgets invokeWithTakeout gets a smaller export, not an error."""
        session = await result(client, in_thread, "export.start", {"messages": True, "files": True})
        assert session["takeout_id"] == 1234567890
        assert session["scope"] == ["messages", "files"]

        status = await result(client, in_thread, "export.status")
        assert status["active"] is True
        assert "GetSplitRangesRequest" in world.takeout_calls

        finished = await result(client, in_thread, "export.end")
        assert finished["finished"] is True
        assert "FinishTakeoutSessionRequest" in world.takeout_calls

    async def test_a_scope_is_required(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, "export.start", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_starting_twice_reports_already(self, live_daemon, client, in_thread):
        await result(client, in_thread, "export.start", {"messages": True})
        again = await call(client, in_thread, "export.start", {"messages": True})
        assert again["result"]["already"] is True
        assert again["meta"]["already"] is True

    async def test_status_without_a_session_is_inactive(self, live_daemon, client, in_thread):
        status = await result(client, in_thread, "export.status")
        assert status["active"] is False

    async def test_ending_without_a_session_reports_already(self, live_daemon, client, in_thread):
        envelope = await call(client, in_thread, "export.end")
        assert envelope["meta"]["already"] is True


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------


class TestWebhook:
    def test_secrets_are_redacted_by_default(self, tlgr_home):
        local("webhook.set", {"url": "https://example.invalid/h", "secret": "s3cret"})
        settings = local("webhook.get", {})
        assert settings.secret == "<redacted>"
        revealed = local("webhook.get", {"show_secret": True})
        assert revealed.secret == "s3cret"

    def test_an_unknown_event_is_refused(self, tlgr_home):
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError):
            local("webhook.set", {"events": "message_exploded"})

    def test_a_group_selector_expands(self, tlgr_home):
        settings = local("webhook.set", {"events": "read"})
        assert set(settings.events) == {
            "read_inbox",
            "read_outbox",
            "read_contents",
            "read_discussion",
            "read_monoforum",
        }

    def test_enabling_without_a_url_is_a_usage_error(self, tlgr_home):
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError):
            local("webhook.set", {"enabled": True})


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


class TestAgent:
    def test_whoami_always_carries_the_schema_version(self, tlgr_home, stub_account):
        info = local("agent.whoami", {}, account="work")
        assert info.output_schema_version == 2
        assert info.layer > 0
        assert info.daemon_running is False

    def test_capabilities_separates_cannot_from_will_not(self, tlgr_home):
        report = local("agent.capabilities", {})
        assert report.event_types > 100
        assert report.unsupported_constructors
        assert any("read receipt" in row["action"] for row in report.prohibited)

    def test_a_section_narrows_the_report(self, tlgr_home):
        report = local("agent.capabilities", {"section": "policy"})
        assert report.prohibited
        assert report.premium_gated == []

    def test_the_error_table_names_the_captured_field(self, tlgr_home):
        table = local("agent.exit-codes", {"errors": True, "search": "flood"})
        rows = {row.name: row for row in table.errors}
        assert rows["FloodWaitError"].extra == "wait_seconds"
        assert rows["FloodWaitError"].exit == 7
        assert rows["FloodWaitError"].retryable is True

    def test_the_exit_code_table_is_unchanged_without_errors(self, tlgr_home):
        table = local("agent.exit-codes", {})
        assert table.errors == []
        assert table.exit_codes["USAGE"].code == 2

    def test_schema_events_prints_the_taxonomy(self, tlgr_home):
        document = local("agent.schema", {"path": ["events"]})
        types = {row["type"] for row in document["events"]}
        assert {"message_new", "read_inbox"} <= types

    def test_schema_still_narrows_by_command_path(self, tlgr_home):
        document = local("agent.schema", {"path": ["sync"]})
        assert all(op.startswith("sync.") for op in document["ops"])
