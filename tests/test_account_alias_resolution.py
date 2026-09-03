"""`account info` / `account sync` silently acted on the WRONG account.

Both take the alias as an optional positional and fell straight back to
`mgr.get_active()`, never reading `ctx.obj["account"]` where the global
`-a/--account` flag lands. So `tlgr -a Pouri256 account sync` synced the
active account (`Mr`) instead — and `sync` *writes*, so a stored profile
record was overwritten under an alias the caller never named.

The sharp edge: with `require_account = true` the guard was *satisfied* (an
`-a` was present) and the command ignored it anyway. A rail that passes while
the command does the wrong thing is worse than no rail.

PR-2 deleted `cli/legacy/account.py`, so the same claims are now made against
the code that replaced it: one `resolve_alias` in `ops/_auth.py` that every
account operation calls, and `core.paths.secure_session_files`, which v1
applied to `account import` and forgot for `account add` — leaving a full
account credential world-readable.
"""

from __future__ import annotations

import stat

import pytest

from tlgr.core.accounts import AccountManager
from tlgr.core.errors import AccountRequiredError
from tlgr.core.paths import secure_session_files
from tlgr.ops._auth import resolve_alias


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("TLGR_HOME", str(tmp_path))
    m = AccountManager(tmp_path)
    m.add_account("Mr")  # first added becomes active
    m.add_account("Pouri256")
    return m


class _Ctx:
    """Stand-in for the OpContext — only `.account` and `.paths` are consulted."""

    def __init__(self, account="", paths=None):
        self.account = account
        self.paths = paths


class TestAliasResolution:
    def test_global_flag_is_honored(self, mgr):
        # The regression: -a must win over the active account.
        assert mgr.get_active() == "Mr"
        assert resolve_alias(_Ctx("Pouri256", mgr.paths)) == "Pouri256"

    def test_positional_beats_global_flag(self, mgr):
        assert resolve_alias(_Ctx("Pouri256", mgr.paths), "Pouri16") == "Pouri16"

    def test_falls_back_to_active_when_neither_given(self, mgr):
        assert resolve_alias(_Ctx("", mgr.paths)) == "Mr"

    def test_empty_flag_does_not_mask_active(self, mgr):
        # -a defaults to "" (not None); "" must not be returned as an alias.
        assert resolve_alias(_Ctx("", mgr.paths)) == "Mr"

    def test_no_accounts_is_an_explicit_error(self, tmp_path, monkeypatch):
        """v1 returned None here and every caller then printed its own message."""
        empty = tmp_path / "empty"
        monkeypatch.setenv("TLGR_HOME", str(empty))
        with pytest.raises(AccountRequiredError):
            resolve_alias(_Ctx(""))

    def test_a_traversing_alias_never_reaches_a_path(self, mgr):
        from tlgr.core.errors import UsageError

        with pytest.raises(UsageError):
            resolve_alias(_Ctx("", mgr.paths), "../../etc")


class TestSessionPermissions:
    def test_session_and_siblings_are_owner_only(self, tmp_path):
        acct_dir = tmp_path / "acct"
        acct_dir.mkdir()
        session_path = acct_dir / "session"
        # Telethon appends .session and creates sqlite siblings at runtime.
        for name in ("session.session", "session.session-journal"):
            f = acct_dir / name
            f.write_bytes(b"credential")
            f.chmod(0o644)

        secure_session_files(session_path)

        for name in ("session.session", "session.session-journal"):
            mode = stat.S_IMODE((acct_dir / name).stat().st_mode)
            assert mode == 0o600, f"{name} is {oct(mode)}, expected 0o600"

    def test_is_a_noop_when_nothing_exists(self, tmp_path):
        secure_session_files(tmp_path / "acct" / "session")  # must not raise
