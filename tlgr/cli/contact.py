"""Contact management commands."""

from __future__ import annotations

import click

from tlgr.core.output import add_pagination, decode_cursor, emit
from tlgr.cli._common import resolve_account
from tlgr.ipc_client import ipc_request


@click.group("contact")
def contact_group() -> None:
    """Manage contacts."""


@contact_group.command("list")
@click.option("--limit", "-n", type=int, default=None, help="Max contacts to return.")
@click.option("--cursor", default=None, help="Pagination cursor from a previous response.")
@click.option("--account", "-a", default=None)
@click.pass_context
def contact_list(ctx: click.Context, limit: int | None, cursor: str | None, account: str | None) -> None:
    """List all contacts."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/contact/list?account={acct}")
    contacts = result.get("contacts", [])
    cur = decode_cursor(cursor)
    offset = cur.get("offset", 0)
    if offset:
        contacts = contacts[offset:]
    effective_limit = limit or len(contacts)
    page = contacts[:effective_limit]
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        next_state = {"offset": offset + len(page)}
        out = {"contacts": page}
        add_pagination(out, page, effective_limit, next_state,
                       has_more=len(contacts) > len(page))
        emit(ctx.obj, out)
    else:
        emit(ctx.obj, page, columns=["id", "name", "username", "phone"])


@contact_group.command("add")
@click.argument("phone")
@click.argument("name", required=False, default="")
@click.option("--account", "-a", default=None)
@click.pass_context
def contact_add(ctx: click.Context, phone: str, name: str, account: str | None) -> None:
    """Add a contact by phone number."""
    acct = resolve_account(ctx, account)
    result = ipc_request("POST", "/contact/add", body={"phone": phone, "name": name, "account": acct})
    emit(ctx.obj, result)


@contact_group.command("rename")
@click.argument("user")
@click.option("--first-name", default=None, help="New first name (omit to keep current).")
@click.option("--last-name", default=None, help="New last name (omit to keep current).")
@click.option("--account", "-a", default=None)
@click.pass_context
def contact_rename(
    ctx: click.Context,
    user: str,
    first_name: str | None,
    last_name: str | None,
    account: str | None,
) -> None:
    """Save a user as a contact under a custom name (also works on non-contacts).

    Useful for tagging users, e.g. appending a state marker to the last name.
    """
    acct = resolve_account(ctx, account)
    body: dict = {"user": user, "account": acct}
    if first_name is not None:
        body["first_name"] = first_name
    if last_name is not None:
        body["last_name"] = last_name
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "contact.rename", **body})
        return
    result = ipc_request("POST", "/contact/rename", body=body)
    emit(ctx.obj, result, columns=["saved", "user_id", "first_name", "last_name"])


@contact_group.command("remove")
@click.argument("user")
@click.option("--account", "-a", default=None)
@click.pass_context
def contact_remove(ctx: click.Context, user: str, account: str | None) -> None:
    """Remove a contact."""
    acct = resolve_account(ctx, account)
    if ctx.obj.get("dry_run"):
        emit(ctx.obj, {"dry_run": True, "op": "contact.remove", "user": user})
        return
    result = ipc_request("POST", "/contact/remove", body={"user": user, "account": acct})
    emit(ctx.obj, result)


@contact_group.command("search")
@click.argument("query")
@click.option("--limit", "-n", type=int, default=None, help="Max results.")
@click.option("--cursor", default=None, help="Pagination cursor from a previous response.")
@click.option("--account", "-a", default=None)
@click.pass_context
def contact_search(ctx: click.Context, query: str, limit: int | None, cursor: str | None, account: str | None) -> None:
    """Search contacts."""
    acct = resolve_account(ctx, account)
    result = ipc_request("GET", f"/contact/search?query={query}&account={acct}")
    contacts = result.get("contacts", [])
    cur = decode_cursor(cursor)
    offset = cur.get("offset", 0)
    if offset:
        contacts = contacts[offset:]
    effective_limit = limit or len(contacts)
    page = contacts[:effective_limit]
    fmt = ctx.obj.get("fmt", "human")
    if fmt == "json":
        next_state = {"offset": offset + len(page)}
        out = {"contacts": page}
        add_pagination(out, page, effective_limit, next_state,
                       has_more=len(contacts) > len(page))
        emit(ctx.obj, out)
    else:
        emit(ctx.obj, page, columns=["id", "name", "username"])
