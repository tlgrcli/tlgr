"""Machine-readable CLI schema for agent/tool discovery."""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from tlgr import __version__


def _param_type_name(param: click.Parameter) -> str:
    """Return a stable type name for a Click parameter."""
    t = param.type
    if isinstance(t, click.Choice):
        return "choice"
    if isinstance(t, click.IntRange):
        return "int"
    if isinstance(t, click.FloatRange):
        return "float"
    name = getattr(t, "name", type(t).__name__)
    return str(name).lower()


def _build_param(param: click.Parameter) -> dict[str, Any]:
    """Serialize a Click parameter to a schema dict."""
    entry: dict[str, Any] = {"name": param.name or ""}

    if isinstance(param, click.Option):
        entry["type"] = "option"
        opts = list(param.opts) + list(param.secondary_opts)
        entry["flags"] = sorted(opts)
        entry["is_flag"] = getattr(param, "is_flag", False)
    elif isinstance(param, click.Argument):
        entry["type"] = "argument"
        entry["required"] = param.required
        if param.nargs != 1:
            entry["nargs"] = param.nargs
    else:
        entry["type"] = "parameter"

    entry["param_type"] = _param_type_name(param)

    if isinstance(param.type, click.Choice):
        entry["choices"] = list(param.type.choices)

    is_flag = getattr(param, "is_flag", False)
    default = param.default
    if (
        default is not None
        and default != ()
        and not is_flag
        and not (hasattr(default, "__class__") and "Sentinel" in type(default).__name__)
    ):
        entry["default"] = default

    help_text = getattr(param, "help", None)
    if help_text:
        entry["help"] = help_text

    if getattr(param, "hidden", False):
        entry["hidden"] = True

    if getattr(param, "envvar", None):
        envvar = param.envvar
        if isinstance(envvar, str):
            entry["envvar"] = [envvar]
        elif envvar:
            entry["envvar"] = list(envvar)

    return entry


EXAMPLE_RESPONSES: dict[str, Any] = {
    "tlgr message send": {"id": 12345, "chat_id": -100123, "date": "2025-03-06 12:00:00+00:00"},
    "tlgr message list": {
        "messages": [{"id": 100, "date": "2025-03-06 12:00:00+00:00", "text": "Hello"}],
        "has_more": True,
        "next_cursor": "eyJvZmZzZXRfaWQiOjEwMH0",
    },
    "tlgr message get": {
        "id": 100,
        "date": "2025-03-06 12:00:00+00:00",
        "text": "Hello",
        "sender": {"id": 123, "name": "Alice", "username": "alice"},
    },
    "tlgr message delete": {"deleted": 2},
    "tlgr message search": {
        "messages": [{"id": 50, "date": "2025-03-05", "text": "match"}],
        "has_more": False,
    },
    "tlgr message pin": {"pinned": True, "msg_id": 100},
    "tlgr message react": {"reacted": True, "msg_id": 100, "emoji": "👍"},
    "tlgr chat list": {
        "chats": [{"id": -100123, "name": "My Group", "type": "supergroup", "username": "mygroup"}],
        "has_more": True,
        "next_cursor": "eyJvZmZzZXQiOjF9",
    },
    "tlgr chat get": {
        "id": -100123,
        "name": "My Group",
        "type": "supergroup",
        "username": "mygroup",
    },
    "tlgr chat create": {"id": -100456, "name": "New Group", "type": "group"},
    "tlgr chat archive": {"archived": True, "chat_id": -100123},
    "tlgr chat mute": {"muted": True, "chat_id": -100123},
    "tlgr chat leave": {"left": True, "chat_id": -100123},
    "tlgr contact list": {
        "contacts": [{"id": 123, "name": "Alice", "username": "alice", "phone": "+1555123"}],
        "has_more": False,
    },
    "tlgr contact add": {"added": True, "user_id": 123},
    "tlgr contact remove": {"removed": True},
    "tlgr contact search": {
        "contacts": [{"id": 123, "name": "Alice", "username": "alice"}],
        "has_more": False,
    },
    "tlgr profile get": {
        "id": 123,
        "first_name": "Me",
        "last_name": "",
        "username": "me",
        "phone": "+1555000",
    },
    "tlgr profile update": {"updated": True},
    "tlgr media download": {"path": "/home/user/.tlgr/downloads/photo.jpg", "msg_id": 100},
    "tlgr media upload": {"id": 200, "chat_id": -100123},
    "tlgr daemon status": {
        "running": True,
        "pid": 12345,
        "uptime_seconds": 3600,
        "accounts": ["main"],
    },
    "tlgr job list": {
        "jobs": [{"name": "my-job", "type": "gateway", "enabled": True, "running": True}]
    },
    "tlgr account list": [{"alias": "* main", "user_id": 123, "name": "Me", "phone": "+1555000"}],
    "tlgr account info": {
        "alias": "main",
        "user_id": 123,
        "username": "me",
        "first_name": "Me",
        "phone": "+1555000",
    },
    "tlgr agent whoami": {
        "account": "main",
        "user_id": 123,
        "username": "me",
        "daemon_running": True,
    },
}


def _build_node(cmd: click.BaseCommand, name: str = "", path: str = "") -> dict[str, Any]:
    """Recursively build a schema node for a command."""
    full_path = f"{path} {name}".strip() if path else name

    node: dict[str, Any] = {
        "name": name or cmd.name or "",
        "path": full_path,
    }

    if isinstance(cmd, click.MultiCommand):
        node["type"] = "group"
    else:
        node["type"] = "command"

    if cmd.help:
        node["help"] = cmd.help.split("\n")[0].strip()

    if getattr(cmd, "hidden", False):
        node["hidden"] = True

    params = getattr(cmd, "params", [])
    if params:
        node["params"] = [_build_param(p) for p in params if p.name != "help"]

    example = EXAMPLE_RESPONSES.get(full_path)
    if example is not None:
        node["example_response"] = example

    if isinstance(cmd, click.MultiCommand):
        sub_names = cmd.list_commands(click.Context(cmd, info_name=name))
        subcommands = []
        for sub_name in sorted(sub_names):
            sub_cmd = cmd.get_command(click.Context(cmd, info_name=name), sub_name)
            if sub_cmd is not None:
                subcommands.append(_build_node(sub_cmd, sub_name, full_path))
        if subcommands:
            node["subcommands"] = subcommands

    return node


@click.command("schema")
@click.argument("command_path", nargs=-1)
@click.option("--include-hidden", is_flag=True, help="Include hidden commands and flags.")
def schema_command(command_path: tuple[str, ...], include_hidden: bool) -> None:
    """Print machine-readable JSON schema of the CLI (for agents)."""
    from tlgr.cli import cli as root_cli

    node = root_cli
    walked_path = "tlgr"

    for token in command_path:
        if not isinstance(node, click.MultiCommand):
            click.echo(f"Error: {walked_path!r} is not a command group", err=True)
            sys.exit(2)
        sub = node.get_command(click.Context(node, info_name=walked_path.split()[-1]), token)
        if sub is None:
            click.echo(f"Error: unknown command {token!r} under {walked_path!r}", err=True)
            sys.exit(2)
        walked_path = f"{walked_path} {token}"
        node = sub

    schema = _build_node(node, name=walked_path.split()[-1], path="")
    schema["path"] = walked_path

    if not include_hidden:
        _strip_hidden(schema)

    doc = {
        "schema_version": 1,
        "build": __version__,
        "command": schema,
    }

    json.dump(doc, sys.stdout, indent=2, default=str, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _strip_hidden(node: dict[str, Any]) -> None:
    """Recursively remove hidden commands and params."""
    if "params" in node:
        node["params"] = [p for p in node["params"] if not p.get("hidden")]
    if "subcommands" in node:
        node["subcommands"] = [s for s in node["subcommands"] if not s.get("hidden")]
        for sub in node["subcommands"]:
            _strip_hidden(sub)
