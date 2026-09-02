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
    """Resolve the account for a command; enforce require_account if enabled."""
    acct = account or (ctx.obj or {}).get("account", "") or ""
    if not acct and _require_account_enabled():
        raise click.UsageError(
            "No account specified and require_account is enabled. "
            "Pass -a <alias> (see: tlgr account list)."
        )
    return acct
