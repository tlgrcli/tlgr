"""Profile management commands."""

from __future__ import annotations

import click

from tlgr.cli._common import resolve_account
from tlgr.core.output import emit
from tlgr.ipc_client import ipc_request


@click.group("profile")
def profile_group() -> None:
    """View and update your Telegram profile."""


@profile_group.command("get")
@click.option("--account", "-a", default=None)
@click.pass_context
def profile_get(ctx: click.Context, account: str | None) -> None:
    """Show your current profile."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/profile/get?account={acct}")
    emit(ctx.obj, result, columns=["id", "first_name", "last_name", "username", "phone"])


@profile_group.command("update")
@click.option("--first-name", default=None)
@click.option("--last-name", default=None)
@click.option("--bio", default=None)
@click.option("--photo", default=None, type=click.Path(exists=True), help="Path to profile photo.")
@click.option("--account", "-a", default=None)
@click.pass_context
def profile_update(
    ctx: click.Context,
    first_name: str | None,
    last_name: str | None,
    bio: str | None,
    photo: str | None,
    account: str | None,
) -> None:
    """Update your profile."""
    acct = resolve_account(ctx, account)
    body = {"account": acct}
    if first_name is not None:
        body["first_name"] = first_name
    if last_name is not None:
        body["last_name"] = last_name
    if bio is not None:
        body["bio"] = bio
    if photo is not None:
        body["photo"] = photo
    result = ipc_request("POST", "/profile/update", body=body)
    emit(ctx.obj, result)
