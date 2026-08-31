"""`account info` / `account sync` silently acted on the WRONG account.

Both take the alias as an optional positional and fell straight back to
`mgr.get_active()`, never reading `ctx.obj["account"]` where the global
`-a/--account` flag lands. So `tlgr -a Pouri256 account sync` synced the
active account (`Mr`) instead — and `sync` *writes*, so a stored profile
record was overwritten under an alias the caller never named.

The sharp edge: with `require_account = true` the guard was *satisfied* (an
`-a` was present) and the command ignored it anyway. A rail that passes while
the command does the wrong thing is worse than no rail.

Also covers: `account add` left the Telethon session db world-readable (644)
while `account import` chmod'd it to 600. A session file is a full account
credential.
"""

from __future__ import annotations

import json
import stat

import pytest

from tlgr.cli.account import _resolve_alias, _secure_session_files
from tlgr.core.accounts import AccountManager


@pytest.fixture
def mgr(tmp_path):
    m = AccountManager(tmp_path)
    m.add_account("Mr")          # first added becomes active
    m.add_account("Pouri256")
    return m


class _Ctx:
    """Stand-in for click.Context — only ctx.obj is consulted."""

    def __init__(self, account=""):
        self.obj = {"account": account}


class TestAliasResolution:
    def test_global_flag_is_honored(self, mgr):
        # The regression: -a must win over the active account.
        assert mgr.get_active() == "Mr"
        assert _resolve_alias(_Ctx("Pouri256"), None, mgr) == "Pouri256"

    def test_positional_beats_global_flag(self, mgr):
        assert _resolve_alias(_Ctx("Pouri256"), "Pouri16", mgr) == "Pouri16"

    def test_falls_back_to_active_when_neither_given(self, mgr):
        assert _resolve_alias(_Ctx(""), None, mgr) == "Mr"

    def test_empty_flag_does_not_mask_active(self, mgr):
        # -a defaults to "" (not None); "" must not be returned as an alias.
        assert _resolve_alias(_Ctx(""), None, mgr) == "Mr"

    def test_no_accounts_yields_none(self, tmp_path):
        empty = AccountManager(tmp_path / "empty")
        assert _resolve_alias(_Ctx(""), None, empty) is None

    def test_missing_ctx_obj_does_not_crash(self, mgr):
        ctx = _Ctx()
        ctx.obj = None
        assert _resolve_alias(ctx, None, mgr) == "Mr"


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

        _secure_session_files(session_path)

        for name in ("session.session", "session.session-journal"):
            mode = stat.S_IMODE((acct_dir / name).stat().st_mode)
            assert mode == 0o600, f"{name} is {oct(mode)}, expected 0o600"

    def test_is_a_noop_when_nothing_exists(self, tmp_path):
        _secure_session_files(tmp_path / "acct" / "session")  # must not raise
