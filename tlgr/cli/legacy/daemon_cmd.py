"""Daemon lifecycle commands.

Still a v1 module — it migrates to the registry in PR-4 — but three things
about it were wrong and are fixed here because they are what an operator uses
when something is broken:

* `start` waited for the **socket file** to appear. The socket exists from
  `bind()`, before any account has connected and before the daemon can serve
  anything, so "started" meant "a process got as far as binding" (ROB-07).
  It now waits for an HTTP 200 from `/v1/status`.
* `status` reported `running: true` for any live pid. It now merges the v2
  status, so `ready`, `version` and `protocol` are visible and a daemon that
  is alive but unable to work is distinguishable from a healthy one
  (COR-37, COR-38).
* `install` was macOS-only. Linux gets a systemd user unit.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time

import click

from tlgr.core.config import CONFIG_DIR, get_logs_dir, get_pid_path
from tlgr.core.output import emit
from tlgr.daemon.lifecycle import read_pid, stop_daemon


def _wait_ready(timeout: float = 30.0) -> dict | None:
    """Poll `/v1/status` until the daemon answers, or give up.

    Readiness is a reply, not a file: see the module docstring.
    """
    from tlgr.transport.autostart import wait_ready
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(CONFIG_DIR, auto_start=False)
    return wait_ready(client.probe_status, timeout=timeout)


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "tlgr.daemon.main", "--base", str(CONFIG_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@click.group("daemon")
def daemon_group() -> None:
    """Manage the tlgr daemon."""


@daemon_group.command("start")
@click.option("--foreground", is_flag=True, help="Run in foreground (don't fork).")
@click.pass_context
def daemon_start(ctx: click.Context, foreground: bool) -> None:
    """Start the daemon (forks to background by default)."""
    existing = read_pid()
    if existing:
        click.echo(f"Daemon already running (pid={existing})", err=True)
        sys.exit(1)

    if foreground:
        from tlgr.daemon.main import main as daemon_main

        sys.exit(daemon_main(["--base", str(CONFIG_DIR), "--foreground"]))

    proc = _spawn()
    status = _wait_ready()
    if status is None:
        click.echo("Daemon did not become ready within 30 seconds", err=True)
        sys.exit(11)
    emit(
        ctx.obj,
        {
            "started": True,
            "pid": status.get("daemon", {}).get("pid") or read_pid() or proc.pid,
            "ready": status.get("daemon", {}).get("ready", False),
        },
    )


@daemon_group.command("stop")
@click.pass_context
def daemon_stop(ctx: click.Context) -> None:
    """Stop the daemon."""
    if stop_daemon():
        for _ in range(20):
            time.sleep(0.25)
            if not get_pid_path().exists():
                break
        emit(ctx.obj, {"stopped": True})
    else:
        click.echo("Daemon is not running", err=True)
        sys.exit(1)


@daemon_group.command("restart")
@click.pass_context
def daemon_restart(ctx: click.Context) -> None:
    """Restart the daemon."""
    if read_pid():
        stop_daemon()
        for _ in range(20):
            time.sleep(0.25)
            if not get_pid_path().exists():
                break

    proc = _spawn()
    status = _wait_ready()
    if status is None:
        click.echo("Daemon did not become ready within 30 seconds", err=True)
        sys.exit(11)
    emit(
        ctx.obj,
        {
            "restarted": True,
            "pid": status.get("daemon", {}).get("pid") or read_pid() or proc.pid,
        },
    )


def _supervisor(choice: str) -> str:
    """Which supervisor to use. `auto` follows the platform."""
    if choice != "auto":
        return choice
    return "launchd" if platform.system() == "Darwin" else "systemd"


@daemon_group.command("install")
@click.option("--force", is_flag=True, help="Reinstall even if already installed.")
@click.option(
    "--supervisor",
    type=click.Choice(["auto", "launchd", "systemd"]),
    default="auto",
    help="Which service manager to install into.",
)
@click.pass_context
def daemon_install(ctx: click.Context, force: bool, supervisor: str) -> None:
    """Install as a user service (auto-start on login, restart on crash).

    macOS gets a LaunchAgent, Linux a systemd **user** unit — user, because
    the daemon holds session files under $HOME and must run as their owner.
    Both force `idle_timeout` to 0: under a supervisor, a clean idle exit is
    either a respawn loop or a daemon that never comes back (COR-39).
    """
    kind = _supervisor(supervisor)
    if kind == "launchd":
        from tlgr.daemon.launchd import install, is_installed

        if is_installed() and not force:
            click.echo("Service already installed. Use --force to reinstall.", err=True)
            sys.exit(1)
        path = install(CONFIG_DIR, get_logs_dir())
    else:
        from tlgr.daemon.systemd import install as install_unit
        from tlgr.daemon.systemd import is_installed

        if is_installed() and not force:
            click.echo("Service already installed. Use --force to reinstall.", err=True)
            sys.exit(1)
        path = install_unit(CONFIG_DIR)
    emit(ctx.obj, {"installed": True, "supervisor": kind, "path": str(path)})


@daemon_group.command("uninstall")
@click.pass_context
def daemon_uninstall(ctx: click.Context) -> None:
    """Remove the user service (stop auto-start on login)."""
    from tlgr.daemon import launchd, systemd

    removed = launchd.uninstall() if platform.system() == "Darwin" else False
    removed = systemd.uninstall() or removed
    if removed:
        emit(ctx.obj, {"uninstalled": True})
    else:
        click.echo("Service is not installed.", err=True)
        sys.exit(1)


@daemon_group.command("status")
@click.pass_context
def daemon_status(ctx: click.Context) -> None:
    """Show daemon status."""
    pid = read_pid()
    if not pid:
        emit(ctx.obj, {"running": False, "ready": False}, columns=["running", "ready"])
        return
    try:
        from tlgr.ipc_client import ipc_request

        result = ipc_request("GET", "/daemon/status")
        # `running` has always meant "a process is alive". `ready` is the
        # question people were actually asking (COR-37): a daemon that is
        # alive but cannot reach Telegram is not a working daemon.
        v2 = _wait_ready(timeout=2.0) or {}
        daemon = v2.get("daemon", {})
        result.setdefault("ready", daemon.get("ready", False))
        result.setdefault("version", daemon.get("version"))
        result.setdefault("protocol", daemon.get("protocol"))
        result.setdefault("managed_by", daemon.get("managed_by"))
        emit(
            ctx.obj,
            result,
            columns=[
                "running",
                "ready",
                "pid",
                "uptime_seconds",
                "accounts",
                "healthy",
                "disconnected",
            ],
        )
    except Exception:
        emit(
            ctx.obj,
            {"running": True, "ready": False, "pid": pid, "uptime_seconds": "?", "accounts": "?"},
        )


@daemon_group.command("logs")
@click.option("--follow", "-f", is_flag=True, help="Follow log output.")
@click.option("--lines", "-n", type=int, default=50, help="Number of lines to show.")
def daemon_logs(follow: bool, lines: int) -> None:
    """View daemon logs."""
    log_file = get_logs_dir() / "daemon.log"
    if not log_file.exists():
        click.echo("No log file found", err=True)
        sys.exit(1)

    if follow:
        os.execlp("tail", "tail", "-f", "-n", str(lines), str(log_file))
    else:
        os.execlp("tail", "tail", "-n", str(lines), str(log_file))
