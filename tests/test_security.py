"""The §8.2 controls, each asserted rather than assumed.

A session file is an auth key: whoever reads it has the account. The controls
here are what stand between it and every other process running as this user —
and every one of them was missing in v1, where the socket was `srwxrwxrwx`
and an alias went into a path unvalidated.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from tlgr.core.errors import UsageError
from tlgr.core.logging import RedactionFilter, setup_logging
from tlgr.core.paths import (
    ALIAS_RE,
    TlgrPaths,
    audit_permissions,
    require_safe_permissions,
    validate_alias,
    write_private,
)


class TestAliasValidation:
    @pytest.mark.parametrize(
        "alias",
        [
            "",
            "..",
            "../etc",
            "/absolute",
            "a/b",
            "x" * 65,
            "with space",
            "quote'",
            "\x00null",
            "..\\windows",
        ],
    )
    def test_a_dangerous_alias_never_becomes_a_path(self, alias: str):
        with pytest.raises(UsageError):
            validate_alias(alias)

    @pytest.mark.parametrize("alias", ["work", "a", "A-1_b", "x" * 64])
    def test_ordinary_aliases_are_accepted(self, alias: str):
        assert validate_alias(alias) == alias

    def test_the_grammar_is_documented_in_one_place(self):
        assert ALIAS_RE.pattern == r"^[A-Za-z0-9_-]{1,64}$"

    def test_every_per_account_path_validates_first(self, tlgr_home: Path):
        paths = TlgrPaths(tlgr_home)
        for method in (
            paths.account_dir,
            paths.session,
            paths.session_lock,
            paths.credentials,
            paths.peers_db,
            paths.flood,
            paths.events_state,
            paths.outbox,
        ):
            with pytest.raises(UsageError):
                method("../escape")


class TestReadsDoNotCreate:
    def test_reading_an_account_directory_does_not_make_one(self, tlgr_home: Path):
        """SEC-02: `tlgr account list` used to materialise a typo's directory."""
        from tlgr.core.accounts import AccountManager

        manager = AccountManager(tlgr_home)
        manager.add_account("work")
        directory = manager.get_account_dir("work")
        assert directory.exists()

        # A read path for an alias that does not exist creates nothing.
        ghost = TlgrPaths(tlgr_home).account_dir("ghost")
        assert not ghost.exists()
        assert manager.get_account("ghost") is None
        assert not ghost.exists()

    def test_the_accounts_directory_property_is_a_read(self, tlgr_home: Path):
        from tlgr.core.accounts import AccountManager

        fresh = tlgr_home / "elsewhere"
        fresh.mkdir()
        manager = AccountManager(fresh)
        _ = manager.accounts_dir
        assert not (fresh / "accounts").exists()


class TestPrivateWrites:
    def test_write_private_is_never_briefly_world_readable(self, tlgr_home: Path):
        target = tlgr_home / "secret.json"
        write_private(target, '{"api_hash": "x"}')
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_it_replaces_atomically(self, tlgr_home: Path):
        target = tlgr_home / "secret.json"
        write_private(target, "first")
        write_private(target, "second")
        assert target.read_text() == "second"
        assert not list(tlgr_home.glob(".secret.json.*"))

    def test_credentials_are_written_privately(self, tlgr_home: Path):
        from tlgr.core.accounts import AccountManager

        manager = AccountManager(tlgr_home)
        manager.add_account("work")
        manager.save_credentials(1, "hash", "work")
        assert stat.S_IMODE(manager.get_credentials_path("work").stat().st_mode) == 0o600

    def test_the_accounts_registry_is_private(self, tlgr_home: Path):
        from tlgr.core.accounts import AccountManager

        AccountManager(tlgr_home).add_account("work")
        assert stat.S_IMODE((tlgr_home / "accounts.json").stat().st_mode) == 0o600


class TestPermissionAudit:
    def test_it_fixes_what_it_can(self, tlgr_home: Path):
        paths = TlgrPaths(tlgr_home)
        paths.ensure_account_dir("work")
        session = paths.session_file("work")
        session.touch(mode=0o644)
        assert audit_permissions(tlgr_home) == []
        assert stat.S_IMODE(session.stat().st_mode) == 0o600

    def test_it_refuses_to_start_on_a_world_readable_session(self, tlgr_home: Path, monkeypatch):
        """§12.3 item 6. Fixing it is better; refusing is the floor."""
        from tlgr.core.errors import ConfigurationError

        paths = TlgrPaths(tlgr_home)
        paths.ensure_account_dir("work")
        session = paths.session_file("work")
        session.touch(mode=0o644)

        def refuse(path, mode):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(os, "chmod", refuse)
        with pytest.raises(ConfigurationError) as caught:
            require_safe_permissions(tlgr_home)
        assert "chmod" in str(caught.value), "the refusal did not say how to fix it"

    def test_a_file_owned_by_someone_else_is_never_chmod_ed(self, tlgr_home: Path, monkeypatch):
        paths = TlgrPaths(tlgr_home)
        paths.ensure_account_dir("work")
        session = paths.session_file("work")
        session.touch(mode=0o600)

        real_lstat = Path.lstat

        class _Stat:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.st_uid = os.getuid() + 1
                self.st_mode = wrapped.st_mode

        def lie(self):
            info = real_lstat(self)
            return _Stat(info) if self == session else info

        monkeypatch.setattr(Path, "lstat", lie)
        problems = audit_permissions(tlgr_home)
        assert any("owned by uid" in p for p in problems)


@pytest.mark.asyncio
class TestSocket:
    async def test_the_socket_is_0600(self, live_daemon):
        mode = stat.S_IMODE(live_daemon.paths.socket.stat().st_mode)
        assert mode == 0o600, f"the socket is {mode:o}; v1 shipped srwxrwxrwx"

    async def test_the_state_file_is_private(self, live_daemon):
        assert stat.S_IMODE(live_daemon.paths.state.stat().st_mode) == 0o600

    async def test_a_peer_from_another_uid_is_refused_and_logged(
        self, live_daemon, client, in_thread, monkeypatch, caplog
    ):
        from tlgr.daemon import app as app_module
        from tlgr.daemon.peercred import Peer

        monkeypatch.setattr(app_module, "peer_of", lambda sock: Peer(uid=424242, pid=99))
        client._ready = True
        with caplog.at_level(logging.WARNING):
            with pytest.raises(Exception):
                await in_thread(client.request, "GET", "/v1/status")
        assert any("refused a connection" in record.message for record in caplog.records)

    async def test_our_own_uid_is_accepted(self, live_daemon, client, in_thread):
        assert (await in_thread(client.status))["ok"] is True


@pytest.mark.asyncio
class TestToken:
    async def test_a_required_token_is_enforced(self, live_daemon, tlgr_home, in_thread):
        from tlgr.transport.client import DaemonClient

        write_private(live_daemon.paths.token, "s3cret")
        live_daemon._token = None
        live_daemon.config.security.require_token = True

        wrong = DaemonClient(tlgr_home, auto_start=False, token="nope")
        wrong._ready = True
        with pytest.raises(Exception) as caught:
            await in_thread(wrong.request, "GET", "/v1/status")
        assert "X-Tlgr-Token" in str(caught.value)

        right = DaemonClient(tlgr_home, auto_start=False, token="s3cret")
        right._ready = True
        assert (await in_thread(right.request, "GET", "/v1/status"))["ok"] is True

    async def test_the_token_file_is_read_from_disk(self, live_daemon, tlgr_home):
        from tlgr.transport.client import DaemonClient

        write_private(TlgrPaths(tlgr_home).token, "from-disk\n")
        assert DaemonClient(tlgr_home).token() == "from-disk"


class TestRedaction:
    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord("t", logging.INFO, __file__, 1, message, (), None)

    @pytest.mark.parametrize(
        "message",
        [
            "auth_key=deadbeefcafe",
            "api_hash: 0123456789abcdef",
            "access_hash=-9218273",
            "token=abc123",
            "password: hunter2",
            "Authorization: Bearer eyJhbGci.payload.sig",
            "calling +98 912 345 6789 now",
        ],
    )
    def test_secrets_do_not_reach_the_log(self, message: str):
        record = self._record(message)
        RedactionFilter().filter(record)
        rendered = record.getMessage()
        assert "deadbeef" not in rendered
        assert "0123456789abcdef" not in rendered
        assert "hunter2" not in rendered
        assert "eyJhbGci" not in rendered
        assert "9123456789" not in rendered.replace(" ", "")

    def test_redaction_is_not_disabled_by_verbosity(self, tlgr_home: Path):
        """`--verbose` raises verbosity, never redaction (§8.2)."""
        setup_logging(TlgrPaths(tlgr_home).log_file, level="debug")
        logging.getLogger("tlgr.test").debug("token=supersecret")
        for handler in logging.getLogger().handlers:
            handler.flush()
        content = TlgrPaths(tlgr_home).log_file.read_text()
        assert "supersecret" not in content

    def test_the_log_file_is_private_and_rotates(self, tlgr_home: Path):
        paths = TlgrPaths(tlgr_home)
        setup_logging(paths.log_file, max_bytes=512, backups=2)
        for index in range(200):
            logging.getLogger("tlgr.test").info("line %d with some padding text", index)
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert stat.S_IMODE(paths.log_file.stat().st_mode) == 0o600
        assert list(paths.logs.glob("daemon.log.*")), "the log never rotated"

    def test_only_allow_listed_extras_reach_the_log(self, tlgr_home: Path):
        import json

        paths = TlgrPaths(tlgr_home)
        setup_logging(paths.log_file)
        logging.getLogger("tlgr.test").info(
            "hello", extra={"account": "work", "message_text": "a private message"}
        )
        for handler in logging.getLogger().handlers:
            handler.flush()
        line = json.loads(paths.log_file.read_text().strip().splitlines()[-1])
        assert line["account"] == "work"
        assert "message_text" not in line

    def teardown_method(self):
        logging.getLogger().handlers.clear()


@pytest.mark.asyncio
async def test_the_access_log_is_off(live_daemon):
    """SEC-05: an access log records every request path, forever."""
    assert logging.getLogger("aiohttp.access").disabled is True
