"""Local operations — the ones that need no account and no daemon.

They are the proof that the registry can carry an operation end to end: these
two were hand-written Click commands in v1 and are now generated, with the
same JSON a v1 agent already parses.
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import EXIT_CODE_MAP
from tlgr.models.base import Model, Request
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = ["SPEC_EXIT_CODES", "SPEC_SCHEMA", "ExitCodeEntry", "ExitCodes", "SchemaDoc"]


class ExitCodeEntry(Model):
    code: int
    description: str


class ExitCodes(Model):
    """Deliberately a mapping, not a list: this is the exact JSON v1 printed."""

    exit_codes: dict[str, ExitCodeEntry]


class ExitCodesReq(Request):
    pass


async def exit_codes(ctx: OpContext, req: ExitCodesReq) -> ExitCodes:
    """Return the stable exit-code table."""
    return ExitCodes(
        exit_codes={
            name: ExitCodeEntry(code=int(info["code"]), description=str(info["description"]))
            for name, info in EXIT_CODE_MAP.items()
        }
    )


SPEC_EXIT_CODES = OperationSpec(
    id="agent.exit-codes",
    request=ExitCodesReq,
    response=ExitCodes,
    impl=exit_codes,
    summary="Print the stable exit codes for automation",
    description=(
        "Every tlgr command exits with one of these codes. They are a "
        "compatibility contract: a code never changes meaning."
    ),
    aliases=("exit-codes",),
    legacy_paths=("agent exit-codes",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=5,
    example={
        "exit_codes": {
            "SUCCESS": {"code": 0, "description": "Success"},
            "USAGE": {"code": 2, "description": "Usage or parse error"},
        }
    },
    example_args="agent exit-codes",
    tags=frozenset({"infrastructure", "agent-safe"}),
)


class SchemaDoc(Model):
    """`tlgr schema` output. Free-form on purpose: it *is* a schema document."""

    schema_version: int
    build: str
    ops: dict[str, Any] = {}


class SchemaReq(Request):
    path: Annotated[
        tuple[str, ...],
        arg(
            0,
            metavar="PATH",
            required=False,
            variadic=True,
            help="Limit the document to one command path, e.g. `schema message send`.",
        ),
    ] = ()
    include_hidden: Annotated[
        bool,
        opt("--include-hidden", help="Include hidden commands and flags."),
    ] = False


async def schema(ctx: OpContext, req: SchemaReq) -> dict[str, Any]:
    """Build the machine-readable schema document.

    The Click command tree is handed in through the context rather than
    imported: `ops/` must not import `cli/` (§2.2), and the tree is a CLI
    concern that only the CLI can describe.
    """
    from tlgr.schema import build_schema

    provider = getattr(ctx, "command_tree", None)
    command = provider(req.path, req.include_hidden) if callable(provider) else None
    return build_schema(path=req.path, command=command, include_hidden=req.include_hidden)


SPEC_SCHEMA = OperationSpec(
    id="agent.schema",
    request=SchemaReq,
    response=dict,
    impl=schema,
    summary="Print the machine-readable schema of the CLI",
    description=(
        "One JSON document: the command tree, and for every registered "
        "operation its request and response JSON Schema plus a validated "
        "example. Draft 2020-12."
    ),
    legacy_paths=("schema",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    example={"schema_version": 2, "build": "2.0.0", "ops": {}},
    example_args="schema message",
    tags=frozenset({"infrastructure", "agent-safe", "json-only"}),
)
