"""Output formatters for JSON, plain (TSV), and human-readable modes."""

from __future__ import annotations

import base64
import json
import sys
from typing import Any, Sequence


def _tsv_escape(value: Any) -> str:
    s = str(value) if value is not None else ""
    return s.replace("\t", " ").replace("\n", " ")


# ---------------------------------------------------------------------------
# JSON transforms (--results-only, --select)
# ---------------------------------------------------------------------------

_ENVELOPE_KEYS = frozenset({
    "next_page_token", "nextPageToken", "next_cursor", "has_more",
    "count", "total", "query", "dry_run", "dryRun", "op", "action",
})


def _unwrap_primary(data: Any) -> Any:
    """Strip envelope metadata and return only the primary result."""
    if not isinstance(data, dict):
        return data
    if "results" in data:
        return data["results"]

    candidates = [k for k in data if k not in _ENVELOPE_KEYS]
    if len(candidates) == 1:
        return data[candidates[0]]

    for k in candidates:
        if isinstance(data[k], list):
            return data[k]

    return data


def _get_at_path(obj: Any, path: str) -> tuple[Any, bool]:
    """Traverse dot-delimited path into a nested dict/list."""
    segments = [s.strip() for s in path.split(".") if s.strip()]
    cur = obj
    for seg in segments:
        if isinstance(cur, dict):
            if seg not in cur:
                return None, False
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None, False
        else:
            return None, False
    return cur, True


def _select_fields(data: Any, fields: list[str]) -> Any:
    """Project only selected fields from data."""
    if isinstance(data, list):
        return [_select_from_item(item, fields) for item in data]
    return _select_from_item(data, fields)


def _select_from_item(item: Any, fields: list[str]) -> Any:
    if not isinstance(item, dict):
        return item
    out: dict[str, Any] = {}
    for f in fields:
        val, found = _get_at_path(item, f)
        if found:
            out[f] = val
    return out


def apply_json_transforms(
    data: Any,
    *,
    results_only: bool = False,
    select: str | None = None,
) -> Any:
    """Apply --results-only and --select transforms to JSON data."""
    if results_only:
        data = _unwrap_primary(data)
    if select:
        fields = [f.strip() for f in select.split(",") if f.strip()]
        if fields:
            data = _select_fields(data, fields)
    return data


# ---------------------------------------------------------------------------
# Core output functions
# ---------------------------------------------------------------------------

def output_json(
    data: Any,
    *,
    flood_wait: int | None = None,
    results_only: bool = False,
    select: str | None = None,
) -> None:
    """Write JSON to stdout."""
    if flood_wait:
        if isinstance(data, dict):
            data["flood_wait"] = flood_wait
        else:
            data = {"result": data, "flood_wait": flood_wait}

    data = apply_json_transforms(data, results_only=results_only, select=select)
    json.dump(data, sys.stdout, default=str, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def output_plain(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    """Write TSV to stdout (no colors, stable for piping)."""
    print("\t".join(columns))
    for row in rows:
        print("\t".join(_tsv_escape(row.get(c)) for c in columns))
    sys.stdout.flush()


def output_human(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str],
    *,
    headers: Sequence[str] | None = None,
) -> None:
    """Write space-padded columns to stdout (kubectl / docker style)."""
    display_headers = headers or columns
    cells = [[str(row.get(c, "")) for c in columns] for row in rows]

    widths = [len(h) for h in display_headers]
    for cell_row in cells:
        for i, v in enumerate(cell_row):
            widths[i] = max(widths[i], len(v))

    gap = "   "
    header_line = gap.join(h.upper().ljust(w) for h, w in zip(display_headers, widths))
    print(header_line.rstrip())
    for cell_row in cells:
        line = gap.join(v.ljust(w) for v, w in zip(cell_row, widths))
        print(line.rstrip())
    sys.stdout.flush()


def output_result(
    data: Any,
    *,
    fmt: str = "human",
    columns: Sequence[str] | None = None,
    headers: Sequence[str] | None = None,
    flood_wait: int | None = None,
    results_only: bool = False,
    select: str | None = None,
) -> None:
    """Dispatch to the correct output formatter.

    *fmt* is one of ``"json"``, ``"plain"``, or ``"human"``.
    For ``"json"`` *data* is emitted as-is.
    For ``"plain"`` and ``"human"`` *data* must be a list of dicts
    and *columns* selects which keys to display.
    """
    if fmt == "json":
        output_json(data, flood_wait=flood_wait, results_only=results_only, select=select)
        return

    if not isinstance(data, list):
        data = [data] if isinstance(data, dict) else [{"result": data}]

    cols = columns or (list(data[0].keys()) if data else ["result"])

    if fmt == "plain":
        output_plain(data, cols)
    else:
        output_human(data, cols, headers=headers)


def emit(ctx_obj: dict[str, Any], data: Any, **kwargs: Any) -> None:
    """Convenience wrapper: ``output_result`` with global transforms from *ctx.obj*."""
    kwargs.setdefault("fmt", ctx_obj.get("fmt", "human"))
    kwargs.setdefault("results_only", ctx_obj.get("results_only", False))
    kwargs.setdefault("select", ctx_obj.get("select"))
    output_result(data, **kwargs)


# ---------------------------------------------------------------------------
# Cursor-based pagination helpers
# ---------------------------------------------------------------------------

def encode_cursor(state: dict[str, Any]) -> str:
    """Encode pagination state as an opaque base64 cursor token."""
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(token: str | None) -> dict[str, Any]:
    """Decode a cursor token back to pagination state. Returns {} on invalid input."""
    if not token:
        return {}
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        return json.loads(raw)
    except Exception:
        return {}


def add_pagination(
    envelope: dict[str, Any],
    items: list[Any],
    limit: int,
    cursor_state: dict[str, Any],
    has_more: bool | None = None,
) -> dict[str, Any]:
    """Add ``has_more`` and ``next_cursor`` to a JSON envelope.

    ``len(items) >= limit`` is a heuristic for server-paginated endpoints,
    which cannot know whether more rows exist without asking again. Callers
    that hold the full result set and slice it themselves know the exact
    answer — they pass it as ``has_more`` instead of guessing, so the cursor
    stops being emitted once the list is exhausted.
    """
    if has_more is None:
        has_more = len(items) >= limit
    envelope["has_more"] = has_more
    if has_more:
        envelope["next_cursor"] = encode_cursor(cursor_state)
    return envelope
