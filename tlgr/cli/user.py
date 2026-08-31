"""User info commands."""

from __future__ import annotations

import click

from tlgr.core.errors import EXIT_INDETERMINATE
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


@user_group.command("dialog-status")
@click.argument("user")
@click.option("--max-dialogs", type=int, default=5000,
              help="Cap on the fallback dialog-list scan. Hitting it is reported "
                   "as indeterminate, never as 'no dialog'.")
@click.option("--account", "-a", default=None)
@click.pass_context
def user_dialog_status(
    ctx: click.Context, user: str, max_dialogs: int, account: str | None
) -> None:
    """Does this account have a dialog with USER? (authoritative, or honest.)

    Answers one of three things, and they are never conflated:

      resolved=true,  has_dialog=true   -> prior conversation exists
      resolved=true,  has_dialog=false  -> definitively none (exit 0)
      resolved=false, has_dialog=null   -> could not be established (exit 13)

    Exit 13 means UNKNOWN. Callers gating a cold first message must treat it
    as a refusal, not as a green light -- that conflation is exactly the bug
    this command exists to remove.
    """
    acct = resolve_account(ctx, account)
    result = ipc_request(
        "GET",
        f"/user/dialog-status?user={user}&account={acct}&max_dialogs={max_dialogs}",
        timeout=600,
    )
    emit(
        ctx.obj,
        result,
        columns=["id", "username", "resolved", "has_dialog", "message_count", "source"],
    )
    if not result.get("resolved"):
        ctx.exit(EXIT_INDETERMINATE)
