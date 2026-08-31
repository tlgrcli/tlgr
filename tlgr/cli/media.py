"""Media download/upload commands."""

from __future__ import annotations

import click

from tlgr.core.output import emit
from tlgr.cli._common import resolve_account
from tlgr.ipc_client import ipc_request


@click.group("media")
def media_group() -> None:
    """Download and upload media files."""


@media_group.command("download")
@click.argument("chat")
@click.argument("msg_id", type=int)
@click.option("--out-dir", default=None, help="Output directory (default ~/.tlgr/downloads/).")
@click.option("--account", "-a", default=None)
@click.pass_context
def media_download(
    ctx: click.Context,
    chat: str,
    msg_id: int,
    out_dir: str | None,
    account: str | None,
) -> None:
    """Download media from a message."""
    acct = resolve_account(ctx, account)
    body = {"chat": chat, "msg_id": msg_id, "account": acct}
    if out_dir:
        body["out_dir"] = out_dir
    result = ipc_request("POST", "/media/download", body=body)
    emit(ctx.obj, result, columns=["path", "msg_id"])


@media_group.command("upload")
@click.argument("chat")
@click.argument("path", type=click.Path(exists=True))
@click.option("--caption", default="", help="Caption for the file.")
@click.option("--account", "-a", default=None)
@click.pass_context
def media_upload(
    ctx: click.Context,
    chat: str,
    path: str,
    caption: str,
    account: str | None,
) -> None:
    """Upload a file to a chat."""
    acct = resolve_account(ctx, account)
    result = ipc_request("POST", "/media/upload", body={
        "chat": chat, "path": path, "caption": caption, "account": acct,
    })
    emit(ctx.obj, result, columns=["id", "chat_id"])
