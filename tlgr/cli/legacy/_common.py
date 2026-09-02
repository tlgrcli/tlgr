"""Shared CLI helpers."""

from __future__ import annotations

import os

import click

_require_cached: bool | None = None


def _require_account_enabled() -> bool:
    """True when every command must be given an explicit account.

    Controlled by TLGR_REQUIRE_ACCOUNT (1/true/0/false) or the
    `require_account` config key ([defaults] in config.toml).
    """
    global _require_cached
    if _require_cached is None:
        env = os.environ.get("TLGR_REQUIRE_ACCOUNT", "").strip().lower()
        if env in ("1", "true", "yes", "on"):
            _require_cached = True
        elif env in ("0", "false", "no", "off"):
            _require_cached = False
        else:
            try:
                from tlgr.core.config import load_app_config

                _require_cached = bool(load_app_config().defaults.require_account)
            except Exception:
                _require_cached = False
    return _require_cached


def resolve_account(ctx: click.Context, account: str | None) -> str:
    """Resolve the account for a command, in the CLI, in one order.

    `-a` → the root flag → `TLGR_ACCOUNT` → `[accounts] default` → the active
    alias. v1 stopped after the root flag and let the *daemon* pick "whichever
    alias came first out of a set" when the result was empty, so a two-account
    user could send from the wrong identity with no signal (COR-02). The
    daemon no longer chooses; the choice is made here, where the user's
    configuration is, and an unresolvable account is a usage error rather than
    a silent substitution.
    """
    acct = (account or (ctx.obj or {}).get("account", "") or "").strip()
    if not acct:
        acct = os.environ.get("TLGR_ACCOUNT", "").strip()
    if not acct:
        try:
            from tlgr.core.config import load_app_config

            acct = (load_app_config().default_account or "").strip()
        except Exception:
            acct = ""
    if not acct:
        try:
            from tlgr.core.accounts import AccountManager
            from tlgr.core.paths import default_base

            acct = (AccountManager(default_base()).get_active() or "").strip()
        except Exception:
            acct = ""
    if not acct and _require_account_enabled():
        raise click.UsageError(
            "No account specified and require_account is enabled. "
            "Pass -a <alias> (see: tlgr account list)."
        )
    return acct
