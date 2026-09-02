"""Message commands — send, list, get, delete, search, pin, react."""

from __future__ import annotations

import click

from tlgr.cli.legacy._common import resolve_account
from tlgr.core.output import add_pagination, decode_cursor, emit
from tlgr.ipc_client import ipc_request


def estimate_typing_seconds(text: str) -> float:
    """Roughly how long a person would take to type *text* on a phone."""
    words = len(text.split())
    return max(2.0, min(30.0, words * 0.35 + 1.5))


@click.group("message")
def message_group() -> None:
    """Send, read, search, and manage messages."""


@message_group.command("send")
@click.argument("chat")
@click.argument("text", required=False, default="")
@click.option("--file", "file_path", default=None, help="File to attach.")
@click.option("--caption", default=None, help="Caption for file.")
@click.option("--reply-to", type=int, default=None, help="Reply to message ID.")
@click.option("--silent", is_flag=True, help="Send without notification.")
@click.option(
    "--typing",
    "typing_s",
    type=float,
    default=0.0,
    help="Show 'typing…' for N seconds before sending (max 60).",
)
@click.option(
    "--typing-auto",
    is_flag=True,
    help="Show 'typing…' for a duration estimated from the text length.",
)
@click.option("--account", "-a", default=None)
@click.pass_context
def message_send(
    ctx: click.Context,
    chat: str,
    text: str,
    file_path: str | None,
    caption: str | None,
    reply_to: int | None,
    silent: bool,
    typing_s: float,
    typing_auto: bool,
    account: str | None,
) -> None:
    """Send a message to a chat."""
    acct = resolve_account(ctx, account)
    if typing_auto and not typing_s:
        typing_s = estimate_typing_seconds(text)
    body = {
        "chat": chat,
        "text": text,
        "account": acct,
        "silent": silent,
    }
    if typing_s:
        body["typing_s"] = typing_s
    if file_path:
        body["file"] = file_path
    if caption:
        body["caption"] = caption
    if reply_to:
        body["reply_to"] = reply_to
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "message.send", **body})
        return
    result = ipc_request("POST", "/message/send", body=body)
    emit(ctx.obj, result, columns=["id", "chat_id", "date"])


@message_group.command("list")
@click.argument("chat")
@click.option("--limit", "-n", type=int, default=20)
@click.option("--offset-id", type=int, default=0)
@click.option("--cursor", default=None, help="Pagination cursor from a previous response.")
@click.option("--sender", is_flag=True, help="Include sender info.")
@click.option("--media", is_flag=True, help="Include media metadata.")
@click.option("--reactions", is_flag=True, help="Include reactions.")
@click.option("--entities", is_flag=True, help="Include entities.")
@click.option("--account", "-a", default=None)
@click.pass_context
def message_list(
    ctx: click.Context,
    chat: str,
    limit: int,
    offset_id: int,
    cursor: str | None,
    sender: bool,
    media: bool,
    reactions: bool,
    entities: bool,
    account: str | None,
) -> None:
    """List recent messages from a chat."""
    acct = resolve_account(ctx, account)
    cur = decode_cursor(cursor)
    if cur.get("offset_id"):
        offset_id = cur["offset_id"]
    params = f"chat={chat}&limit={limit}&offset_id={offset_id}&account={acct}"
    if sender:
        params += "&sender=1"
    if media:
        params += "&media=1"
    if reactions:
        params += "&reactions=1"
    if entities:
        params += "&entities=1"
    result = ipc_request("GET", f"/message/list?{params}")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        msgs = result.get("messages", [])
        next_state = {"offset_id": msgs[-1]["id"]} if msgs else {}
        add_pagination(result, msgs, limit, next_state)
        emit(ctx.obj, result)
    else:
        emit(ctx.obj, result.get("messages", []), columns=["id", "date", "text"])


@message_group.command("get")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.option("--account", "-a", default=None)
@click.pass_context
def message_get(ctx: click.Context, chat: str, msg_id: int, account: str | None) -> None:
    """Get a single message with full metadata."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/message/get?chat={chat}&msg_id={msg_id}&account={acct}")
    emit(ctx.obj, result)


@message_group.command("delete")
@click.argument("chat")
@click.argument("msg_ids", nargs=-1, type=int, required=True)
@click.option("--account", "-a", default=None)
@click.pass_context
def message_delete(
    ctx: click.Context, chat: str, msg_ids: tuple[int, ...], account: str | None
) -> None:
    """Delete messages from a chat."""
    acct = resolve_account(ctx, account)
    if ctx.obj.get("dry_run"):
        emit(
            ctx.obj,
            {"dry_run": True, "op": "message.delete", "chat": chat, "msg_ids": list(msg_ids)},
        )
        return
    result = ipc_request(
        "POST",
        "/message/delete",
        body={
            "chat": chat,
            "msg_ids": list(msg_ids),
            "account": acct,
        },
    )
    emit(ctx.obj, result, columns=["deleted"])


@message_group.command("search")
@click.argument("chat")
@click.argument("query")
@click.option("--local", is_flag=True, help="Client-side regex search.")
@click.option("--regex", default=None, help="Regex pattern (with --local).")
@click.option("--limit", "-n", type=int, default=20)
@click.option("--cursor", default=None, help="Pagination cursor from a previous response.")
@click.option("--account", "-a", default=None)
@click.pass_context
def message_search(
    ctx: click.Context,
    chat: str,
    query: str,
    local: bool,
    regex: str | None,
    limit: int,
    cursor: str | None,
    account: str | None,
) -> None:
    """Search messages in a chat."""
    acct = resolve_account(ctx, account)
    cur = decode_cursor(cursor)
    offset_id = cur.get("offset_id", 0)
    params = f"chat={chat}&query={query}&limit={limit}&account={acct}"
    if offset_id:
        params += f"&offset_id={offset_id}"
    if local:
        params += "&local=1"
    if regex:
        params += f"&regex={regex}"
    result = ipc_request("GET", f"/message/search?{params}")
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        msgs = result.get("messages", [])
        next_state = {"offset_id": msgs[-1]["id"]} if msgs else {}
        add_pagination(result, msgs, limit, next_state)
        emit(ctx.obj, result)
    else:
        emit(ctx.obj, result.get("messages", []), columns=["id", "date", "text"])


@message_group.command("edit")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.argument("text")
@click.option(
    "--typing",
    "typing_s",
    type=float,
    default=0.0,
    help="Show 'typing…' for N seconds before editing (max 60).",
)
@click.option("--account", "-a", default=None)
@click.pass_context
def message_edit(
    ctx: click.Context,
    chat: str,
    msg_id: int,
    text: str,
    typing_s: float,
    account: str | None,
) -> None:
    """Edit an outgoing message."""
    acct = resolve_account(ctx, account)
    body: dict = {"chat": chat, "msg_id": msg_id, "text": text, "account": acct}
    if typing_s:
        body["typing_s"] = typing_s
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "message.edit", **body})
        return
    result = ipc_request("POST", "/message/edit", body=body)
    emit(ctx.obj, result, columns=["edited", "id", "chat_id", "date"])


@message_group.command("forward")
@click.argument("from_chat")
@click.argument("to_chat")
@click.argument("msg_ids", nargs=-1, type=int, required=True)
@click.option("--account", "-a", default=None)
@click.pass_context
def message_forward(
    ctx: click.Context,
    from_chat: str,
    to_chat: str,
    msg_ids: tuple[int, ...],
    account: str | None,
) -> None:
    """Forward messages from one chat to another."""
    acct = resolve_account(ctx, account)
    body = {"from_chat": from_chat, "to_chat": to_chat, "msg_ids": list(msg_ids), "account": acct}
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "message.forward", **body})
        return
    result = ipc_request("POST", "/message/forward", body=body)
    emit(ctx.obj, result, columns=["forwarded", "ids"])


@message_group.command("pin")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.option("--account", "-a", default=None)
@click.pass_context
def message_pin(ctx: click.Context, chat: str, msg_id: int, account: str | None) -> None:
    """Pin a message in a chat."""
    acct = resolve_account(ctx, account)
    result = ipc_request(
        "POST", "/message/pin", body={"chat": chat, "msg_id": msg_id, "account": acct}
    )
    emit(ctx.obj, result)


@message_group.command("read")
@click.argument("chat")
@click.option("--up-to", type=int, default=None, help="Read up to this message ID.")
@click.option("--account", "-a", default=None)
@click.pass_context
def message_read(ctx: click.Context, chat: str, up_to: int | None, account: str | None) -> None:
    """Mark messages as read."""
    acct = resolve_account(ctx, account)
    body: dict = {"chat": chat, "account": acct}
    if up_to:
        body["up_to"] = up_to
    result = ipc_request("POST", "/message/read", body=body)
    emit(ctx.obj, result)


@message_group.command("react")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.argument("emoji")
@click.option("--account", "-a", default=None)
@click.pass_context
def message_react(
    ctx: click.Context, chat: str, msg_id: int, emoji: str, account: str | None
) -> None:
    """React to a message with an emoji."""
    acct = resolve_account(ctx, account)
    result = ipc_request(
        "POST",
        "/message/react",
        body={"chat": chat, "msg_id": msg_id, "emoji": emoji, "account": acct},
    )
    emit(ctx.obj, result)
