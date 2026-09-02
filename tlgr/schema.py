"""JSON Schema (draft 2020-12) for every registered operation.

v1 built this by walking the Click tree and hand-maintaining an
`EXAMPLE_RESPONSES` dict, which covered 26 of 93 commands and went stale
(COR-33). Here the request and response schemas come from the same Structs the
daemon decodes with, and the example is the one the contract test validates,
so "documented" and "true" are the same artefact.
"""

from __future__ import annotations

import typing
from typing import Any

import msgspec

from tlgr import __version__
from tlgr.ops._params import cli_meta
from tlgr.ops._spec import OperationSpec
from tlgr.registry import REGISTRY

__all__ = ["SCHEMA_VERSION", "build_schema", "op_schema", "schema_components"]

#: Bumped from 1: the document now carries per-op request/response schemas and
#: a `$defs` section, and `example_response` is generated rather than curated.
SCHEMA_VERSION = 2

_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _response_types(spec: OperationSpec) -> list[Any]:
    return [spec.response] if spec.response is not None else []


def schema_components(specs: list[OperationSpec]) -> tuple[dict[str, Any], dict[str, Any]]:
    """`({op id: {"request": …, "response": …}}, $defs)` for *specs*.

    One `schema_components` call for the whole registry, so a model shared by
    forty operations is defined once and referenced forty times.
    """
    types: list[Any] = []
    slots: list[tuple[str, str]] = []
    for spec in specs:
        types.append(spec.request)
        slots.append((spec.id, "request"))
        for response in _response_types(spec):
            types.append(response)
            slots.append((spec.id, "response"))

    if not types:
        return {}, {}
    schemas, defs = msgspec.json.schema_components(types, ref_template="#/$defs/{name}")
    out: dict[str, dict[str, Any]] = {}
    for (op_id, slot), schema in zip(slots, schemas, strict=True):
        out.setdefault(op_id, {})[slot] = schema
    return out, defs


def _params(spec: OperationSpec) -> list[dict[str, Any]]:
    """The CLI shape of each request field, straight off its annotation."""
    info = msgspec.inspect.type_info(spec.request)
    out: list[dict[str, Any]] = []
    for field in getattr(info, "fields", ()):
        cli = cli_meta(field.type)
        entry: dict[str, Any] = {
            "name": field.name,
            "type": "argument" if cli.get("role") == "arg" else "option",
            "required": field.required,
        }
        if cli.get("role") == "arg":
            entry["position"] = cli.get("pos", 0)
            if cli.get("variadic"):
                entry["variadic"] = True
        else:
            entry["flags"] = cli.get("flags") or [f"--{field.name.replace('_', '-')}"]
        for key in ("metavar", "envvar", "hidden", "secret", "kind", "choices"):
            if cli.get(key):
                entry[key] = cli[key]
        description = getattr(field.type, "extra_json_schema", {}) or {}
        if description.get("description"):
            entry["help"] = description["description"]
        if field.default is not msgspec.NODEFAULT:
            entry["default"] = field.default
        out.append(entry)
    return out


def op_schema(spec: OperationSpec, shapes: dict[str, Any]) -> dict[str, Any]:
    """One operation as the schema document describes it."""
    entry: dict[str, Any] = {
        "id": spec.id,
        "path": spec.cli_path,
        "summary": spec.summary,
        "surface": spec.surface.value,
        "mutating": spec.mutating,
        "destructive": spec.destructive,
        "stream": spec.stream,
        "needs_account": spec.needs_account,
        "empty_exit": spec.empty_exit,
        "params": _params(spec),
        "request_schema": shapes.get("request", {}),
    }
    if spec.description:
        entry["description"] = spec.description
    if spec.aliases:
        entry["aliases"] = list(spec.aliases)
    if spec.legacy_paths:
        entry["legacy_paths"] = list(spec.legacy_paths)
    if spec.paginated is not None:
        entry["paginated"] = spec.paginated.value
    if spec.columns:
        entry["columns"] = list(spec.columns)
    if "response" in shapes:
        entry["response_schema"] = shapes["response"]
    if spec.example is not None:
        # v1 called this `example_response`; keeping the key means an agent
        # written against schema_version 1 still finds the example.
        entry["example_response"] = msgspec.to_builtins(spec.example)
    if spec.example_args:
        entry["example_args"] = spec.example_args
    if spec.covers:
        entry["covers"] = list(spec.covers)
    if spec.deprecated:
        entry["deprecated"] = spec.deprecated
    return entry


def build_schema(
    *,
    path: tuple[str, ...] = (),
    command: dict[str, Any] | None = None,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """The whole `tlgr schema` document.

    *command* is the Click command tree, passed in rather than imported: this
    module sits below `cli/` and must not reach up into it (§2.2).
    """
    prefix = ".".join(path)
    specs = [
        spec
        for spec in REGISTRY.values()
        if not prefix or spec.id == prefix or spec.id.startswith(f"{prefix}.")
    ]
    if not include_hidden:
        specs = [spec for spec in specs if not spec.deprecated]
    specs.sort(key=lambda s: s.id)

    shapes, defs = schema_components(specs)
    document: dict[str, Any] = {
        "$schema": _DIALECT,
        "schema_version": SCHEMA_VERSION,
        "build": __version__,
        "ops": {spec.id: op_schema(spec, shapes.get(spec.id, {})) for spec in specs},
    }
    if defs:
        document["$defs"] = defs
    if command is not None:
        document["command"] = command
    return document


def response_type_name(spec: OperationSpec) -> str:
    """A human label for the response shape, used by the docs generator."""
    response = spec.response
    if response is None:
        return "none"
    origin = typing.get_origin(response)
    args = typing.get_args(response)
    if origin is not None and args:
        inner = getattr(args[0], "__name__", str(args[0]))
        return f"{getattr(origin, '__name__', str(origin))}[{inner}]"
    return getattr(response, "__name__", str(response))
