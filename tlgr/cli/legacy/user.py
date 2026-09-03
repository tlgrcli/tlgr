"""User info commands."""

from __future__ import annotations

import click

from tlgr.cli.legacy._common import resolve_account
from tlgr.core.errors import EXIT_INDETERMINATE
from tlgr.core.output import emit
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
    result = ipc_request("GET", "/user/get", params={"user": user, "account": acct})
    emit(
        ctx.obj,
        result,
        columns=["id", "first_name", "username", "bio", "is_bot", "status", "stories_hidden"],
    )


@user_group.command("hide-stories")
@click.argument("user")
@click.option(
    "--unhide", is_flag=True, default=False, help="Put them back in the main stories bar instead."
)
@click.option("--account", "-a", default=None)
@click.pass_context
def user_hide_stories(ctx: click.Context, user: str, unhide: bool, account: str | None) -> None:
    """Archive USER's stories for this account ("Hide Stories").

    The same thing as the "Hide Stories" item in Telegram's own story
    context menu: they drop out of the main stories bar into the collapsed
    Hidden list. Per-account and purely local — the other side is never
    told, and nothing about the chat, the contact or their access changes.

    Idempotent: `already: true` means the flag was already set and no RPC
    was sent, so bulk passes are cheap to repeat. `tlgr user get` reports
    the current value as `stories_hidden`.
    """
    acct = resolve_account(ctx, account)
    result = ipc_request(
        "POST",
        "/user/stories-hidden",
        body={"user": user, "hidden": not unhide, "account": acct},
    )
    emit(ctx.obj, result, columns=["user_id", "username", "hidden", "already"])


@user_group.command("dialog-status")
@click.argument("user")
@click.option(
    "--max-dialogs",
    type=int,
    default=5000,
    help="Cap on the fallback dialog-list scan. Hitting it is reported "
    "as indeterminate, never as 'no dialog'.",
)
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
        "/user/dialog-status",
        params={"user": user, "account": acct, "max_dialogs": max_dialogs},
        timeout=600,
    )
    emit(
        ctx.obj,
        result,
        columns=["id", "username", "resolved", "has_dialog", "message_count", "source"],
    )
    if not result.get("resolved"):
        ctx.exit(EXIT_INDETERMINATE)
