"""Output rendering (ARCHITECTURE §9), driven entirely by the spec.

Three modes, one rule each:

* `--json` prints the envelope verbatim, so an agent parses one shape;
* `--plain` prints TSV that survives `cut` and `awk`;
* the default prints a table a person can read, with real formatting — v1
  printed `None`, `True` and raw dict reprs into columns (UX-03).

`--results-only` returns `result` verbatim rather than guessing which key was
the "primary" one, which is how v1 came to print a bare `2` for
`message delete` (COR-18).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any

import click

from tlgr.core.timefmt import fmt_local

__all__ = [
    "MAX_COLUMN",
    "format_cell",
    "get_path",
    "project",
    "render",
    "render_human",
    "render_json",
    "render_plain",
    "render_stream",
    "results_payload",
]

MAX_COLUMN = 48

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def use_color(stream: Any = None) -> bool:
    """Colour only on a TTY, and never when `NO_COLOR` is set."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


# ---------------------------------------------------------------------------
# Projection (--select)
# ---------------------------------------------------------------------------


def get_path(obj: Any, path: str) -> tuple[Any, bool]:
    """Walk a dot path into nested dicts and lists. Returns (value, found)."""
    current = obj
    for segment in (s.strip() for s in path.split(".") if s.strip()):
        if isinstance(current, dict):
            if segment not in current:
                return None, False
            current = current[segment]
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None, False
        else:
            return None, False
    return current, True


def project(data: Any, fields: Sequence[str]) -> Any:
    """Keep only *fields*, recursing element-wise into lists.

    Missing paths are omitted rather than emitted as null: `--select` is a
    projection, and inventing keys would make the output lie about what the
    operation returned.
    """
    if isinstance(data, list):
        return [project(item, fields) for item in data]
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for path in fields:
        value, found = get_path(data, path)
        if found:
            out[path] = value
    return out


def _split_fields(select: str | None) -> list[str]:
    return [f.strip() for f in (select or "").split(",") if f.strip()]


def results_payload(envelope: dict[str, Any]) -> Any:
    """What `--results-only` prints.

    For a paginated op that is the `Page[T]` object, so STYLE's contract
    (`{items, has_more, next_cursor, total}`) holds; otherwise it is `result`
    verbatim.
    """
    page = envelope.get("page")
    if page is not None:
        return {"items": envelope.get("result") or [], **page}
    if "error" in envelope:
        return envelope["error"]
    return envelope.get("result")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def render_json(
    envelope: dict[str, Any],
    *,
    results_only: bool = False,
    select: str | None = None,
    stream: Any = None,
) -> None:
    """Print the envelope, or the projection of it the flags ask for."""
    fields = _split_fields(select)
    if results_only:
        payload: Any = results_payload(envelope)
        if fields:
            payload = project(payload, fields)
    elif fields:
        # `--select` alone keeps the envelope and projects only `result`;
        # v1 projected the envelope and printed `{}`.
        payload = dict(envelope)
        if "result" in payload:
            payload["result"] = project(payload["result"], fields)
    else:
        payload = envelope

    out = stream or sys.stdout
    json.dump(payload, out, default=str, ensure_ascii=False)
    out.write("\n")
    out.flush()


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def format_cell(value: Any, *, wide: bool = False, width: int = MAX_COLUMN) -> str:
    """One value, formatted for a human table (the §9 table)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        text = fmt_local(value) if _RFC3339.match(value) else value
    elif isinstance(value, dict):
        # A dict in a column means the caller asked for the object rather than
        # a dot path into it; summarise instead of printing a repr.
        text = ", ".join(f"{k}={format_cell(v, wide=True)}" for k, v in value.items())
    elif isinstance(value, Iterable) and not isinstance(value, bytes):
        text = ", ".join(format_cell(item, wide=True) for item in value)
    else:
        text = str(value)

    text = text.replace("\n", "⏎").replace("\r", "")
    if not wide and len(text) > width:
        text = text[: width - 1] + "…"
    return text


def _rows(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return [row if isinstance(row, dict) else {"value": row} for row in data]
    if isinstance(data, dict):
        return [data]
    return [{"value": data}]


def _default_columns(rows: Sequence[dict[str, Any]]) -> list[str]:
    return list(rows[0].keys()) if rows else ["value"]


def resolve_columns(
    data: Any,
    *,
    spec_columns: Sequence[str] = (),
    override: str | None = None,
) -> list[str]:
    if override:
        return [c.strip() for c in override.split(",") if c.strip()]
    if spec_columns:
        return list(spec_columns)
    return _default_columns(_rows(data))


# ---------------------------------------------------------------------------
# Plain (TSV)
# ---------------------------------------------------------------------------


def _tsv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    return text.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def render_plain(
    data: Any,
    columns: Sequence[str],
    *,
    no_header: bool = False,
    stream: Any = None,
) -> None:
    out = stream or sys.stdout
    if not no_header:
        out.write("\t".join(columns) + "\n")
    for row in _rows(data):
        out.write("\t".join(_tsv(get_path(row, c)[0]) for c in columns) + "\n")
    out.flush()


# ---------------------------------------------------------------------------
# Human
# ---------------------------------------------------------------------------


def render_human(
    data: Any,
    columns: Sequence[str],
    *,
    headers: Sequence[str] = (),
    wide: bool = False,
    no_header: bool = False,
    stream: Any = None,
) -> None:
    """Table for lists, key/value for a single object."""
    out = stream or sys.stdout
    rows = _rows(data)

    if isinstance(data, dict) and not columns:
        _render_object(data, wide=wide, stream=out)
        return

    if not rows:
        return

    titles = list(headers) if headers else [c.upper() for c in columns]
    cells = [[format_cell(get_path(row, c)[0], wide=wide) for c in columns] for row in rows]
    widths = [len(t) for t in titles]
    for row_cells in cells:
        for index, value in enumerate(row_cells):
            widths[index] = max(widths[index], len(value))

    gap = "   "
    if not no_header:
        header = gap.join(t.ljust(w) for t, w in zip(titles, widths, strict=True)).rstrip()
        out.write((click.style(header, bold=True) if use_color(out) else header) + "\n")
    for row_cells in cells:
        out.write(
            gap.join(v.ljust(w) for v, w in zip(row_cells, widths, strict=True)).rstrip() + "\n"
        )
    out.flush()


def _render_object(data: dict[str, Any], *, wide: bool, stream: Any) -> None:
    """A single object as a key/value block, with maps of records as tables.

    A mapping whose values are all objects (the exit-code table, a per-account
    health map) is data with a natural key column; printing it as one long
    `k=v` cell would be technically correct and useless.
    """
    scalars = {k: v for k, v in data.items() if not _is_record_map(v)}
    if scalars:
        width = max(len(k) for k in scalars)
        for key, value in scalars.items():
            stream.write(f"{key.ljust(width)}  {format_cell(value, wide=wide)}\n")

    for key, value in data.items():
        if not _is_record_map(value):
            continue
        inner_columns = sorted({k for record in value.values() for k in record})
        rows = [{"name": name, **record} for name, record in value.items()]
        if scalars:
            stream.write("\n")
        stream.write(f"{key}:\n")
        render_human(rows, ["name", *inner_columns], wide=wide, stream=stream)
    stream.flush()


def _is_record_map(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(item, dict) for item in value.values())
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render(
    envelope: dict[str, Any],
    *,
    fmt: str = "human",
    results_only: bool = False,
    select: str | None = None,
    spec_columns: Sequence[str] = (),
    headers: Sequence[str] = (),
    columns: str | None = None,
    wide: bool = False,
    no_header: bool = False,
    stream: Any = None,
) -> None:
    """Render one response envelope in the mode the flags ask for."""
    if fmt == "json":
        render_json(envelope, results_only=results_only, select=select, stream=stream)
        return

    data = results_payload(envelope)
    if isinstance(data, dict) and "items" in data and envelope.get("page") is not None:
        data = data["items"]
    fields = _split_fields(select)
    if fields:
        data = project(data, fields)
        spec_columns = fields

    resolved = resolve_columns(data, spec_columns=spec_columns, override=columns)
    if fmt == "plain":
        render_plain(data, resolved, no_header=no_header, stream=stream)
        return

    if isinstance(data, dict) and not columns and not fields:
        render_human(data, (), wide=wide, stream=stream)
        return
    render_human(
        data,
        resolved,
        headers=headers if len(headers) == len(resolved) else (),
        wide=wide,
        no_header=no_header,
        stream=stream,
    )


#: Frames that describe the *stream* rather than something that happened. A
#: consumer filtering on the event type has to be able to tell them apart,
#: which is why they are named here rather than recognised by their absence.
CONTROL_FRAMES = frozenset(
    {"meta", "end", "heartbeat", "lag", "gap", "watching", "cursor", "page", "item"}
)


def render_stream(
    frames: Iterable[dict[str, Any]],
    *,
    fmt: str = "human",
    results_only: bool = False,
    select: str | None = None,
    stream: Any = None,
) -> int:
    """Print an NDJSON stream as it arrives, and return the exit code.

    Flushed per frame, deliberately: a `watch` piped into `jq` that only
    appears once a 4 KB buffer fills is indistinguishable from a daemon that
    is not delivering anything.

    `--results-only` restores v1's line shape — `{"event_type", "chat_id",
    "data"}` — and drops the control frames, because that is what a script
    written against v1's `tlgr watch` already parses (§12.4).
    """
    out = stream or sys.stdout
    fields = _split_fields(select)
    exit_code = 0
    for frame in frames:
        kind = str(frame.get("type", ""))
        if kind == "end" and not frame.get("ok", True):
            from tlgr.core.errors import EXIT_RETRYABLE

            body = frame.get("error") or {}
            exit_code = int(body.get("exit_code", EXIT_RETRYABLE))
            click.echo(f"Error: {body.get('message') or 'the stream ended'}", err=True)
            continue
        if results_only:
            if kind in CONTROL_FRAMES:
                continue
            frame = {
                "event_type": kind,
                "chat_id": frame.get("chat_id"),
                "data": frame.get("payload", {}),
                "seq": frame.get("seq"),
                "account": frame.get("account"),
            }
        if fields:
            frame = project(frame, fields)
        _write_frame(frame, out)
    return exit_code


def _write_frame(frame: Any, out: Any) -> None:
    out.write(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
    with contextlib.suppress(AttributeError, ValueError, OSError):  # a closed pipe
        out.flush()
