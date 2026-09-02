"""Describing the Click tree for `tlgr schema`.

The walker itself is v1's, kept because the schema document still has to
describe every not-yet-migrated command; what changed is where the examples
come from. v1 hand-maintained `EXAMPLE_RESPONSES` and covered 26 of 93
commands (COR-33); now an example is the spec's, validated by a test.

This lives in `cli/` because only the CLI knows what the CLI looks like;
`tlgr/schema.py` sits below it and is handed the result.
"""

from __future__ import annotations

from typing import Any

import click

__all__ = ["describe"]


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


def _registry_example(full_path: str) -> Any:
    """The example for a command path, from the spec that owns it.

    Not every command has one yet: an unmigrated v1 group has no spec, and
    saying nothing is better than shipping the stale literal v1 kept here.
    """
    from tlgr.registry import ALIASES, REGISTRY

    op_id = ALIASES.get(full_path.removeprefix("tlgr ").replace(" ", "."))
    spec = REGISTRY.get(op_id) if op_id else None
    if spec is None or spec.example is None:
        return None
    import msgspec

    return msgspec.to_builtins(spec.example)


def _build_node(cmd: click.BaseCommand, name: str = "", path: str = "") -> dict[str, Any]:
    """Recursively build a schema node for a command."""
    full_path = f"{path} {name}".strip() if path else name

    node: dict[str, Any] = {
        "name": name or cmd.name or "",
        "path": full_path,
    }

    if isinstance(cmd, click.Group):
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

    example = _registry_example(full_path)
    if example is not None:
        node["example_response"] = example

    if isinstance(cmd, click.Group):
        sub_names = cmd.list_commands(click.Context(cmd, info_name=name))
        subcommands = []
        for sub_name in sorted(sub_names):
            sub_cmd = cmd.get_command(click.Context(cmd, info_name=name), sub_name)
            if sub_cmd is not None:
                subcommands.append(_build_node(sub_cmd, sub_name, full_path))
        if subcommands:
            node["subcommands"] = subcommands

    return node


def describe(path: tuple[str, ...] = (), *, include_hidden: bool = False) -> dict[str, Any] | None:
    """The command tree under *path*, or None when the path does not exist."""
    from tlgr.cli import cli as root_cli

    node: Any = root_cli
    walked = "tlgr"
    for token in path:
        if not isinstance(node, click.Group):
            return None
        sub = node.get_command(click.Context(node, info_name=walked.split()[-1]), token)
        if sub is None:
            return None
        walked = f"{walked} {token}"
        node = sub

    tree = _build_node(node, name=walked.split()[-1], path="")
    tree["path"] = walked
    if not include_hidden:
        _strip_hidden(tree)
    return tree


def _strip_hidden(node: dict[str, Any]) -> None:
    """Recursively remove hidden commands and params."""
    if "params" in node:
        node["params"] = [p for p in node["params"] if not p.get("hidden")]
    if "subcommands" in node:
        node["subcommands"] = [s for s in node["subcommands"] if not s.get("hidden")]
        for sub in node["subcommands"]:
            _strip_hidden(sub)
