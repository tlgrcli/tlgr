"""The operation registry: one mapping, one lookup, and the lints that guard it.

Every generated artefact (the Click tree, the daemon dispatch table, the JSON
Schema, the reference docs, the contract tests) reads this module. The lints
run at import, so a malformed spec cannot ship: an import-time failure is
noisy in a way a stale doc or a missing example is not.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any

import msgspec

from tlgr.core.errors import EXIT_EMPTY, EXIT_SUCCESS, UsageError
from tlgr.models.page import Page
from tlgr.ops._params import cli_meta
from tlgr.ops._spec import RATE_CLASSES, OperationSpec, Surface

__all__ = [
    "ALIASES",
    "REGISTRY",
    "VERBS",
    "by_group",
    "canonical",
    "get",
    "groups",
    "lint",
    "policy_allows",
    "register",
    "reset",
]

REGISTRY: dict[str, OperationSpec] = {}
ALIASES: dict[str, str] = {}

_ID_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){1,2}$")

#: STYLE §1's verb vocabulary plus the two documented extensions from
#: COMMANDS.md (accepted verbs, and protocol/lifecycle verbs that name a
#: Telegram or daemon operation with no synonym in the list).
VERBS: frozenset[str] = frozenset(
    [
        "list",
        "get",
        "create",
        "send",
        "edit",
        "set",
        "unset",
        "delete",
        "add",
        "remove",
        "pin",
        "unpin",
        "mute",
        "unmute",
        "archive",
        "unarchive",
        "block",
        "unblock",
        "join",
        "leave",
        "start",
        "stop",
        "open",
        "read",
        "search",
        "download",
        "upload",
        "export",
        "import",
        "enable",
        "disable",
        "toggle",
        "approve",
        "deny",
        "revoke",
        "promote",
        "demote",
        "ban",
        "unban",
        "restrict",
        "transfer",
        "forward",
        "react",
        "vote",
        "close",
        "reopen",
        "hide",
        "unhide",
        "watch",
        "terminate-all",
        "read-all",
        "mark-unread",
        # `chat unread` is v1's spelling of markDialogUnread and AGENT.md
        # documents it; "mark-unread" above is the same operation named the
        # STYLE way, and both resolve to the one op.
        "unread",
        "report",
        "clear",
        "check",
        "accept",
        "decline",
        "convert",
        "press",
        "invoke",
        "translate",
        "link",
        "status",
        "test",
        "reorder",
        "post",
        "end",
        "discard",
        "answer",
        "query",
        "catchup",
        "typing",
        "invite",
        "share",
        "preview",
        "save",
        "catalog",
        "send-code",
        "resend-code",
        "verify-code",
        "sign-up",
        "qr",
        "recover",
        "reset-account",
        "reset",
        "logout",
        "terminate",
        "confirm",
        "accept-qr",
        "change",
        "install",
        "uninstall",
        "restart",
        "reconnect",
        "save-state",
        "replay",
        "decode",
        "ping",
        "catch-up",
        "difference",
        "backfill",
        "purge",
        "upgrade",
        "craft",
        "apply",
        "refulfill",
        "authorize",
        "verify",
        "rate",
        "signal",
        "sync",
        "pay",
        "delete-history",
        "share-phone",
        "raise-hand",
        "tos",
        "whoami",
        "capabilities",
        "exit-codes",
        "init",
        "validate",
        "path",
        "keys",
        "compose",
        "summarize",
        "transcribe",
        "can-message",
        "can-post",
        "reply",
        "nearest",
        "dialog-status",
        "hide-stories",
        "rename",
        "info",
        "temp",
        "retry",
        "logs",
        "floods",
        "venue",
        "stealth",
        "schema",
        "parity",
        # `search` is one of COMMANDS.md's verb-first nouns: the tail of
        # `search global` / `search hashtag` names the *scope*, not an
        # action. They are listed here because L1 checks the last segment
        # and has no way to know which nouns are verb-first.
        "global",
        "hashtag",
        # PR-2. Each names a Telegram or tlgr operation with no synonym in the
        # STYLE list: `switch` is not `set` (it changes which account later
        # commands use, not a field on one) and `completion` is a v1 path
        # §12.4 promises stays invocable.
        "switch",
        "completion",
    ]
)

#: Reserved by the global flags and by transport-level pagination (lint L5).
_RESERVED_FIELDS = frozenset({"account", "json", "plain", "cursor", "limit", "all", "dry_run"})

#: A best-effort blocklist for lint L7: an op that says it does not mutate
#: must not be calling one of these. Waived with tags={"mutating-checked"}.
_MUTATING_CALLS = frozenset(
    {
        "send_message",
        "send_file",
        "edit_message",
        "delete_messages",
        "forward_messages",
        "send_read_acknowledge",
        "edit_permissions",
        "edit_admin",
        "kick_participant",
        "delete_dialog",
        "pin_message",
        "unpin_message",
        "log_out",
    }
)


def register(spec: OperationSpec) -> OperationSpec:
    """Add *spec* to the registry. Returns it so a module can assign the result."""
    if spec.id in REGISTRY:
        raise ValueError(f"duplicate operation id {spec.id!r}")
    REGISTRY[spec.id] = spec
    for name in spec.names:
        existing = ALIASES.get(name)
        if existing is not None and existing != spec.id:
            raise ValueError(f"alias {name!r} is claimed by both {existing!r} and {spec.id!r}")
        ALIASES[name] = spec.id
    return spec


def reset() -> None:
    """Empty the registry. For tests that build a registry of their own."""
    REGISTRY.clear()
    ALIASES.clear()


def canonical(name: str) -> str:
    """`msg.send` → `message.send`; raises USAGE for anything unknown.

    Everything that checks policy calls this first, so an allowlist written
    against canonical ids cannot be side-stepped by using an alias (SEC-04).
    """
    key = name.strip().replace(" ", ".")
    resolved = ALIASES.get(key)
    if resolved is None:
        raise UsageError(f"unknown operation {name!r}", field="op")
    return resolved


def get(op_id_or_alias: str) -> OperationSpec:
    return REGISTRY[canonical(op_id_or_alias)]


def by_group(group: str) -> list[OperationSpec]:
    return [spec for spec in REGISTRY.values() if spec.group == group]


def groups() -> list[str]:
    return sorted({spec.group for spec in REGISTRY.values()})


# ---------------------------------------------------------------------------
# Lints
# ---------------------------------------------------------------------------


def _unwrap(annotation: Any) -> Any:
    """Strip Optional/Annotated down to the underlying type."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _unwrap(typing.get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _unwrap(args[0]) if args else annotation
    return annotation


def _response_item_type(response: Any) -> Any:
    """The Struct a response's columns are projected out of."""
    if response is None:
        return None
    origin = typing.get_origin(response)
    if origin is not None and typing.get_args(response):
        # Page[Message] and list[Message] both project out of Message.
        return _response_item_type(typing.get_args(response)[0])
    return response


def _column_exists(item_type: Any, path: str) -> bool:
    """Walk a dot path through Struct annotations."""
    current = item_type
    for segment in path.split("."):
        if not (isinstance(current, type) and issubclass(current, msgspec.Struct)):
            return False
        hints = typing.get_type_hints(current, include_extras=False)
        if segment not in hints:
            return False
        current = _unwrap(hints[segment])
        inner = typing.get_origin(current)
        if inner in (list, tuple, set):
            args = typing.get_args(current)
            current = args[0] if args else Any
    return True


def _field_metas(request: type[msgspec.Struct]) -> list[tuple[str, Any]]:
    """(name, type node) for every request field, in declaration order."""
    info = msgspec.inspect.type_info(request)
    fields = getattr(info, "fields", ())
    return [(f.name, f.type) for f in fields]


def _double_meta_fields(request: type[msgspec.Struct]) -> list[str]:
    """Fields carrying two `msgspec.Meta` annotations (lint L14).

    Only the first Meta's `extra`/`description` reaches the generator, so the
    second one's help text would silently vanish (§4.2).
    """
    bad: list[str] = []
    for name, annotation in typing.get_type_hints(request, include_extras=True).items():
        if typing.get_origin(annotation) is typing.Annotated:
            metas = [a for a in typing.get_args(annotation)[1:] if isinstance(a, msgspec.Meta)]
            if len(metas) > 1:
                bad.append(name)
    return bad


def _lint_spec(spec: OperationSpec, problems: list[str]) -> None:
    def bad(message: str) -> None:
        problems.append(f"{spec.id}: {message}")

    # L1 — id shape and verb vocabulary.
    if not _ID_RE.match(spec.id):
        bad("id must be two or three lowercase dotted segments")
    elif spec.verb not in VERBS:
        bad(f"verb {spec.verb!r} is not in the STYLE.md vocabulary")

    # L3 — request/response types.
    if not (isinstance(spec.request, type) and issubclass(spec.request, msgspec.Struct)):
        bad("request must be a msgspec Struct")
        return
    item = _response_item_type(spec.response)
    is_struct = isinstance(item, type) and issubclass(item, msgspec.Struct)
    is_dict = item is dict or typing.get_origin(item) is dict
    if item is not None and not is_struct and not is_dict:
        bad("response must be a Struct, list[Struct], Page[Struct], dict or None")

    # L4 — positional indices contiguous from 0, at most one variadic, last.
    positions: list[int] = []
    variadic_at: int | None = None
    for name, annotation in _field_metas(spec.request):
        cli = cli_meta(annotation)
        if cli.get("role") != "arg":
            continue
        position = int(cli.get("pos", 0))
        positions.append(position)
        if cli.get("variadic"):
            if variadic_at is not None:
                bad("more than one variadic positional")
            variadic_at = position
        # L15 — an UNSET tri-state cannot be positional; there is no way to
        # spell "not supplied" in a positional slot.
        if "Unset" in str(typing.get_type_hints(spec.request, include_extras=True).get(name, "")):
            bad(f"field {name!r} is an Unset tri-state and cannot be positional")
    if positions and sorted(positions) != list(range(len(positions))):
        bad(f"positional indices must be contiguous from 0, got {sorted(positions)}")
    if variadic_at is not None and positions and variadic_at != max(positions):
        bad("the variadic positional must be last")

    # L5 — reserved field names.
    reserved = _RESERVED_FIELDS.intersection(name for name, _ in _field_metas(spec.request))
    if reserved:
        bad(f"request fields {sorted(reserved)} are reserved for global flags/pagination")

    # L14 — one Meta per field.
    for name in _double_meta_fields(spec.request):
        bad(f"field {name!r} carries two msgspec.Meta annotations; merge them")

    # L6 — pagination and streaming shapes.
    if spec.paginated is not None and typing.get_origin(spec.response) is not Page:
        bad("paginated ops must declare response=Page[...]")
    if spec.stream and not inspect.isasyncgenfunction(spec.impl):
        bad("stream ops must be implemented as an async generator")

    # L7 — a non-mutating op should not be calling a mutating method.
    if not spec.mutating and "mutating-checked" not in spec.tags:
        try:
            source = inspect.getsource(spec.impl)
        except (OSError, TypeError):
            source = ""
        for call in _MUTATING_CALLS:
            if f".{call}(" in source:
                bad(f"declares mutating=False but calls {call}()")

    # L8 — destructive implies mutating.
    if spec.destructive and not spec.mutating:
        bad("destructive ops must also be mutating")

    # L9 — every op documents itself.
    if not spec.summary:
        bad("summary is empty")
    if spec.example is None:
        bad("example is missing")
    if not spec.example_args:
        bad("example_args is missing")

    # L11 — declared columns exist on the response.
    if spec.columns and item is not None:
        for column in spec.columns:
            if not _column_exists(item, column):
                bad(f"column {column!r} does not exist on the response type")
    if spec.headers and len(spec.headers) != len(spec.columns):
        bad("headers must line up one-to-one with columns")

    # L12 — sane limits.
    if not 5 <= spec.timeout_s <= 900:
        bad(f"timeout_s {spec.timeout_s} is outside 5..900")
    if spec.rate_class not in RATE_CLASSES:
        bad(f"unknown rate_class {spec.rate_class!r}")
    if spec.empty_exit not in (EXIT_SUCCESS, EXIT_EMPTY):
        bad("empty_exit must be 0 or 3")

    # L13 — every op is either catalogued or explicitly infrastructure.
    if not spec.covers and not spec.covers_partial and "infrastructure" not in spec.tags:
        bad("declares no catalog coverage and is not tagged infrastructure")

    if spec.surface is Surface.LOCAL and spec.needs_account:
        bad("local ops must set needs_account=False")


def lint() -> list[str]:
    """Return every problem in the registry; empty means the registry is sound."""
    problems: list[str] = []
    seen_names: dict[str, str] = {}
    for spec in REGISTRY.values():
        _lint_spec(spec, problems)
        # L2 — aliases and legacy paths are unique and disjoint from ids.
        for name in spec.names[1:]:
            if name in REGISTRY:
                problems.append(f"{spec.id}: alias {name!r} collides with an operation id")
            owner = seen_names.get(name)
            if owner is not None:
                problems.append(f"{spec.id}: alias {name!r} is also claimed by {owner!r}")
            seen_names[name] = spec.id
    return problems


def policy_allows(allowlist: str, op_id: str) -> bool:
    """Is *op_id* permitted by an `--enable-commands` / `[policy] allow` list?

    Entries are canonicalised before comparison, so an allowlist written
    against ids cannot be side-stepped by invoking an alias, and a bare group
    name (`message`) allows every operation in it. This is SEC-04's fix, and
    it lives in the registry because the CLI and the daemon must reach the
    same verdict from the same data.
    """
    entries = {e.strip().lower() for e in allowlist.replace(" ", ",").split(",") if e.strip()}
    if not entries or "*" in entries or "all" in entries:
        return True
    canonical_id = ALIASES.get(op_id, op_id).lower()
    for entry in entries:
        resolved = ALIASES.get(entry.replace(" ", "."), entry)
        if resolved == canonical_id or canonical_id.startswith(f"{resolved}."):
            return True
    return False


def lint_or_raise() -> None:
    """Called at the end of `ops/__init__.py`; a broken spec fails the import."""
    problems = lint()
    if problems:
        raise RuntimeError("operation registry lint failed:\n  " + "\n  ".join(problems))
