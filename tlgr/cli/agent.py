"""Agent-friendly helper commands."""

from __future__ import annotations

import json
import sys

import click

from tlgr.core.errors import EXIT_CODE_MAP
from tlgr.core.output import emit


@click.group("agent")
def agent_group() -> None:
    """Agent-friendly helpers (schema, exit codes)."""


@agent_group.command("exit-codes")
@click.pass_context
def agent_exit_codes(ctx: click.Context) -> None:
    """Print stable exit codes for automation."""
    fmt = ctx.obj.get("fmt", "human") if ctx.obj else "human"

    if fmt == "json":
        json.dump({"exit_codes": EXIT_CODE_MAP}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    rows = sorted(
        ((info["code"], name, info["description"]) for name, info in EXIT_CODE_MAP.items()),
        key=lambda r: r[0],
    )
    seen: set[int] = set()
    click.echo(f"{'CODE':<6} {'NAME':<22} DESCRIPTION")
    for code, name, desc in rows:
        marker = "" if code not in seen else " (alias)"
        seen.add(code)
        click.echo(f"{code:<6} {name:<22} {desc}{marker}")


@agent_group.command("whoami")
@click.pass_context
def agent_whoami(ctx: click.Context) -> None:
    """Return current account info, daemon status, and environment for agents."""
    from tlgr.core.accounts import AccountManager
    from tlgr.core.config import CONFIG_DIR
    from tlgr.daemon.lifecycle import read_pid

    obj = ctx.obj or {}
    mgr = AccountManager(CONFIG_DIR)
    active_alias = obj.get("account") or mgr.get_active()
    acct = mgr.get_account(active_alias) if active_alias else None

    info: dict = {
        "account": active_alias or "",
        "user_id": acct.user_id if acct else None,
        "username": acct.username if acct else None,
        "phone": acct.phone if acct else None,
        "daemon_running": read_pid() is not None,
        "config_dir": str(CONFIG_DIR),
    }

    enabled = obj.get("enable_commands", "")
    if enabled:
        info["enabled_commands"] = [c.strip() for c in enabled.split(",") if c.strip()]

    # Try to get job list from daemon
    if info["daemon_running"]:
        try:
            from tlgr.ipc_client import ipc_request
            status = ipc_request("GET", "/daemon/status")
            info["daemon_uptime"] = status.get("uptime_seconds")
            # `accounts` is every client the daemon holds, connected or not — this
            # field used to report it as `accounts_connected`, so a daemon whose
            # clients had all given up reconnecting still listed them all here.
            conns = status.get("connections")
            if conns is None:  # daemon predates the connections field
                info["accounts_connected"] = status.get("accounts", [])
            else:
                info["accounts_connected"] = sorted(a for a, ok in conns.items() if ok)
                info["accounts_disconnected"] = status.get("disconnected", [])
                info["daemon_healthy"] = status.get("healthy")
            jobs = ipc_request("GET", "/job/list")
            info["active_jobs"] = [j["name"] for j in jobs.get("jobs", []) if j.get("running")]
        except Exception:
            pass

    emit(obj, info)
