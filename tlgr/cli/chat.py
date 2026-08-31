"""Chat management commands."""

from __future__ import annotations

import click

from tlgr.core.output import add_pagination, decode_cursor, emit
from tlgr.cli._common import resolve_account
from tlgr.ipc_client import ipc_request


@click.group("chat")
def chat_group() -> None:
    """List, create, and manage chats."""


@chat_group.command("list")
@click.option("--type", "chat_type", default=None, help="Filter: user, group, channel, bot.")
@click.option("--search", "-s", default=None, help="Filter by name.")
@click.option("--limit", "-n", type=int, default=None)
@click.option("--unread", is_flag=True, help="Only chats with unread messages.")
@click.option("--cursor", default=None, help="Pagination cursor from a previous response.")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_list(
    ctx: click.Context,
    chat_type: str | None,
    search: str | None,
    limit: int | None,
    unread: bool,
    cursor: str | None,
    account: str | None,
) -> None:
    """List all chats/dialogs."""
    acct = resolve_account(ctx, account)
    cur = decode_cursor(cursor)
    effective_limit = limit or 100
    params = f"account={acct}&limit={effective_limit}"
    if cur.get("offset"):
        params += f"&offset={cur['offset']}"
    if chat_type:
        params += f"&type={chat_type}"
    if search:
        params += f"&search={search}"
    if unread:
        params += "&unread=1"
    result = ipc_request("GET", f"/chat/list?{params}")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        chats = result.get("chats", [])
        offset = cur.get("offset", 0)
        next_state = {"offset": offset + len(chats)}
        add_pagination(result, chats, effective_limit, next_state)
        emit(ctx.obj, result)
    else:
        emit(
            ctx.obj,
            result.get("chats", []),
            columns=["id", "name", "type", "username", "unread_count"],
            headers=["ID", "Name", "Type", "Username", "Unread"],
        )


@chat_group.command("open")
@click.argument("chat")
@click.option("--limit", "-n", type=int, default=30, help="Messages to fetch.")
@click.option("--no-read", is_flag=True, help="Peek silently (no read receipt).")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_open(ctx: click.Context, chat: str, limit: int, no_read: bool, account: str | None) -> None:
    """Open a chat like a human: recent history + read receipt.

    Use --no-read (or 'message list') for a silent peek.
    """
    acct = resolve_account(ctx, account)
    result = ipc_request("POST", "/chat/open", body={
        "account": acct, "chat": chat, "limit": limit, "mark_read": not no_read,
    })
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        emit(ctx.obj, result)
    else:
        emit(
            ctx.obj,
            result.get("messages", []),
            columns=["id", "date", "out", "text"],
            headers=["ID", "Date", "Out", "Text"],
        )


@chat_group.command("catchup")
@click.option("--type", "chat_type", default=None, help="Filter: user, group, channel, bot.")
@click.option("--limit-chats", type=int, default=20, help="Max unread chats to include.")
@click.option("--per-chat", type=int, default=10, help="Max messages per chat.")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_catchup(
    ctx: click.Context,
    chat_type: str | None,
    limit_chats: int,
    per_chat: int,
    account: str | None,
) -> None:
    """What did I miss? Every unread chat with its recent messages, one call.

    Read-only: emits no read receipts. Follow up with 'chat open' on the
    chats you decide to engage.
    """
    acct = resolve_account(ctx, account)
    params = f"account={acct}&limit_chats={limit_chats}&per_chat={per_chat}"
    if chat_type:
        params += f"&type={chat_type}"
    result = ipc_request("GET", f"/chat/catchup?{params}")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        emit(ctx.obj, result)
    else:
        emit(
            ctx.obj,
            result.get("chats", []),
            columns=["id", "name", "unread_count"],
            headers=["ID", "Name", "Unread"],
        )


@chat_group.command("members")
@click.argument("chat")
@click.option("--admins", is_flag=True, help="Only admins and the creator.")
@click.option("--search", "-s", default=None, help="Filter by name (server-side).")
@click.option("--limit", "-n", type=int, default=None)
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_members(
    ctx: click.Context,
    chat: str,
    admins: bool,
    search: str | None,
    limit: int | None,
    account: str | None,
) -> None:
    """List members of a group or channel."""
    acct = resolve_account(ctx, account)
    params = f"chat={chat}&account={acct}"
    if admins:
        params += "&admins=1"
    if search:
        params += f"&search={search}"
    if limit:
        params += f"&limit={limit}"
    result = ipc_request("GET", f"/chat/members?{params}")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        emit(ctx.obj, result)
    else:
        emit(
            ctx.obj,
            result.get("members", []),
            columns=["id", "first_name", "last_name", "username", "is_bot"],
            headers=["ID", "First", "Last", "Username", "Bot"],
        )


@chat_group.command("get")
@click.argument("chat")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_get(ctx: click.Context, chat: str, account: str | None) -> None:
    """Get chat info (members, permissions, etc.)."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/chat/get?chat={chat}&account={acct}")
    emit(ctx.obj, result)


@chat_group.command("create")
@click.argument("name")
@click.option("--type", "chat_type", default="group", type=click.Choice(["group", "channel"]))
@click.option("--members", multiple=True, help="Users to add.")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_create(
    ctx: click.Context,
    name: str,
    chat_type: str,
    members: tuple[str, ...],
    account: str | None,
) -> None:
    """Create a new group or channel."""
    acct = resolve_account(ctx, account)
    result = ipc_request("POST", "/chat/create", body={
        "name": name, "type": chat_type, "members": list(members), "account": acct,
    })
    emit(ctx.obj, result)


@chat_group.command("archive")
@click.argument("chat")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_archive(ctx: click.Context, chat: str, account: str | None) -> None:
    """Archive a chat."""
    acct = resolve_account(ctx, account)
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "chat.archive", "chat": chat})
        return
    result = ipc_request("POST", "/chat/archive", body={"chat": chat, "account": acct})
    emit(ctx.obj, result)


@chat_group.command("mute")
@click.argument("chat")
@click.argument("duration", type=int, required=False, default=None)
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_mute(ctx: click.Context, chat: str, duration: int | None, account: str | None) -> None:
    """Mute a chat. Duration in seconds (omit for permanent)."""
    acct = resolve_account(ctx, account)
    result = ipc_request("POST", "/chat/mute", body={"chat": chat, "duration": duration, "account": acct})
    emit(ctx.obj, result)


@chat_group.command("leave")
@click.argument("chat")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_leave(ctx: click.Context, chat: str, account: str | None) -> None:
    """Leave a chat or group."""
    acct = resolve_account(ctx, account)
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "chat.leave", "chat": chat})
        return
    result = ipc_request("POST", "/chat/leave", body={"chat": chat, "account": acct})
    emit(ctx.obj, result)


@chat_group.command("typing")
@click.argument("chat")
@click.option("--duration", type=float, default=5, help="Seconds to show typing (default 5).")
@click.option("--account", "-a", default=None)
@click.pass_context
def chat_typing(ctx: click.Context, chat: str, duration: float, account: str | None) -> None:
    """Send a typing indicator."""
    acct = resolve_account(ctx, account)
    result = ipc_request("POST", "/chat/typing", body={"chat": chat, "duration": duration, "account": acct})
    emit(ctx.obj, result)
