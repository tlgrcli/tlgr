"""Job management commands."""

from __future__ import annotations

import os

import click

from tlgr.core.config import CONFIG_DIR
from tlgr.core.output import emit
from tlgr.ipc_client import ipc_request


@click.group("job")
def job_group() -> None:
    """Manage background jobs."""


@job_group.command("list")
@click.pass_context
def job_list(ctx: click.Context) -> None:
    """List configured jobs and their status."""
    result = ipc_request("GET", "/job/list")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        emit(ctx.obj, result)
    else:
        emit(ctx.obj, result.get("jobs", []), columns=["name", "type", "enabled", "running"])


@job_group.command("add")
def job_add() -> None:
    """Open jobs.yaml in $EDITOR to add a job."""
    jobs_path = CONFIG_DIR / "jobs.yaml"
    if not jobs_path.exists():
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(
            "# Gateway jobs configuration — see docs for full reference.\n"
            "#\n"
            "# jobs:\n"
            "#   - name: my-job\n"
            "#     account: main\n"
            "#     filters:\n"
            "#       chat_type: private\n"
            "#     actions:\n"
            '#       - reply: "hello!"\n'
        )
    editor = os.environ.get("EDITOR", "vi")
    os.execlp(editor, editor, str(jobs_path))


@job_group.command("remove")
@click.argument("name")
@click.pass_context
def job_remove(ctx: click.Context, name: str) -> None:
    """Remove a job by name."""
    result = ipc_request("POST", "/job/remove", body={"name": name})
    emit(ctx.obj, result)


@job_group.command("enable")
@click.argument("name")
@click.pass_context
def job_enable(ctx: click.Context, name: str) -> None:
    """Enable a disabled job."""
    result = ipc_request("POST", "/job/enable", body={"name": name})
    emit(ctx.obj, result)


@job_group.command("disable")
@click.argument("name")
@click.pass_context
def job_disable(ctx: click.Context, name: str) -> None:
    """Disable a job without removing it."""
    result = ipc_request("POST", "/job/disable", body={"name": name})
    emit(ctx.obj, result)


@job_group.command("reload")
@click.pass_context
def job_reload(ctx: click.Context) -> None:
    """Hot-reload jobs from jobs.yaml without restarting the daemon."""
    result = ipc_request("POST", "/job/reload")
    emit(ctx.obj, result)
