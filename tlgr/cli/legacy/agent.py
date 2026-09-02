"""What is left of v1's `agent` group.

`agent exit-codes` and `schema` are generated from the registry now; only
`whoami` is still hand-written, and it moves when the account group migrates
(PR-2). It is attached into the generated `agent` group by build_cli().
"""

from __future__ import annotations

import click

from tlgr.core.output import emit


@click.group("agent")
def agent_group() -> None:
    """Agent-friendly helpers (schema, exit codes)."""


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
