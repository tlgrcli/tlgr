"""What is left of v1's `chat` group.

Every dialog-level command — `list`, `open`, `catchup`, `read`, `unread`,
`get`, `archive`, `mute`, `leave`, `typing`, `posters` and the `chats` /
`inbox` / `catchup` shortcuts — is generated from the registry now (PR-3),
and the v1 spellings survive as `legacy_paths` on those specs.

`create` and `members` are member-and-admin operations, so they migrate with
the groups/channels group (PR-7). Until then they stay here and are attached
into the *generated* `chat` group by `build_cli()`, which is the one
sanctioned overlap: enumerated in LEGACY_EXTRAS rather than a group defined
in two places.
"""

from __future__ import annotations

import click

from tlgr.cli.legacy._common import resolve_account
from tlgr.core.output import emit
from tlgr.ipc_client import ipc_request


@click.command("members")
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
    params: dict[str, object] = {"chat": chat, "account": acct}
    if admins:
        params["admins"] = 1
    if search:
        params["search"] = search
    if limit:
        params["limit"] = limit
    result = ipc_request("GET", "/chat/members", params=params)
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


@click.command("create")
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
    result = ipc_request(
        "POST",
        "/chat/create",
        body={
            "name": name,
            "type": chat_type,
            "members": list(members),
            "account": acct,
        },
    )
    emit(ctx.obj, result)
