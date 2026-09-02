"""Draft commands — leave, clear, and list message drafts.

Drafts are the human-in-the-loop primitive: an agent can prepare a reply
in a chat without sending it, and the user sends (or discards) it from
any Telegram client.
"""

from __future__ import annotations

import click

from tlgr.cli.legacy._common import resolve_account
from tlgr.core.output import emit
from tlgr.ipc_client import ipc_request


@click.group("draft")
def draft_group() -> None:
    """Set, clear, and list message drafts."""


@draft_group.command("set")
@click.argument("chat")
@click.argument("text")
@click.option("--reply-to", type=int, default=None, help="Draft as a reply to message ID.")
@click.option("--account", "-a", default=None)
@click.pass_context
def draft_set(
    ctx: click.Context,
    chat: str,
    text: str,
    reply_to: int | None,
    account: str | None,
) -> None:
    """Leave a draft in a chat (does not send anything)."""
    acct = resolve_account(ctx, account)
    body: dict = {"chat": chat, "text": text, "account": acct}
    if reply_to:
        body["reply_to"] = reply_to
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "draft.set", **body})
        return
    result = ipc_request("POST", "/draft/set", body=body)
    emit(ctx.obj, result, columns=["draft", "chat_id", "text"])


@draft_group.command("clear")
@click.argument("chat")
@click.option("--account", "-a", default=None)
@click.pass_context
def draft_clear(ctx: click.Context, chat: str, account: str | None) -> None:
    """Clear the draft in a chat."""
    acct = resolve_account(ctx, account)
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "draft.clear", "chat": chat})
        return
    result = ipc_request("POST", "/draft/clear", body={"chat": chat, "account": acct})
    emit(ctx.obj, result, columns=["cleared", "chat_id"])


@draft_group.command("list")
@click.option("--account", "-a", default=None)
@click.pass_context
def draft_list(ctx: click.Context, account: str | None) -> None:
    """List all non-empty drafts across chats."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/draft/list?account={acct}")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        emit(ctx.obj, result)
    else:
        emit(
            ctx.obj,
            result.get("drafts", []),
            columns=["chat_id", "chat_name", "chat_username", "text"],
            headers=["Chat ID", "Name", "Username", "Draft"],
        )
