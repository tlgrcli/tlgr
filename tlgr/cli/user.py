"""User info commands."""

from __future__ import annotations

import click

from tlgr.core.output import emit
from tlgr.cli._common import resolve_account
from tlgr.ipc_client import ipc_request


@click.group("user")
def user_group() -> None:
    """Look up Telegram users."""


@user_group.command("get")
@click.argument("user")
@click.option("--account", "-a", default=None)
@click.pass_context
def user_get(ctx: click.Context, user: str, account: str | None) -> None:
    """Get detailed info about a user."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/user/get?user={user}&account={acct}")
    emit(ctx.obj, result, columns=["id", "first_name", "username", "bio", "is_bot", "status"])
