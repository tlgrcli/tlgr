"""`tlgr batch`: many operations, one process.

A shell loop that calls `tlgr` once per message pays for a Python start-up and
an import of the whole registry on every line. Measured on a real job, that was
about 0.9 s of every 1.1 s call, so a 2,000-message run was bounded by the
interpreter rather than by Telegram. `batch` reads one operation per line,
runs them in order over the same transport every other command uses, and
writes one result per line as soon as it has it.

It is deliberately not a second way in. Every line goes through `run_op`,
the function every generated command calls, so `--enable-commands`, account
resolution, the `--dry-run` short-circuit and the daemon's own policy all
apply exactly as they would to the single command. Parameters are parsed by
the same Click types the flags use. A destructive operation needs `--yes` (or
`"yes": true` on its line) and never prompts, because stdin is the input.

Input, one JSON object per line::

    {"op": "message.send", "params": {"chat": "@alice", "text": "hi"}, "id": "m1"}

Output, one JSON object per non-blank input line, in order::

    {"line": 1, "id": "m1", "ok": true, "op": "message.send", "account": "work",
     "result": {...}, "meta": {...}, "exit_code": 0}
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
from collections.abc import Iterator
from typing import IO, Any

import click
import msgspec

from tlgr.cli import errors as cli_errors
from tlgr.cli.confirm import confirm
from tlgr.cli.globals import CliState, _require_account_enabled, state_from
from tlgr.cli.render import project
from tlgr.core.errors import (
    EXIT_CANCELLED,
    EXIT_DAEMON,
    EXIT_EMPTY,
    EXIT_GENERIC,
    EXIT_INDETERMINATE,
    EXIT_SPAM_FLAGGED,
    EXIT_SUCCESS,
    UsageError,
)

__all__ = ["ABORT_EXIT_CODES", "LINE_KEYS", "batch_command", "run_batch"]

#: Keys a line may carry. Anything else is refused, so a typo such as
#: `"param"` fails loudly instead of sending a request with no parameters.
LINE_KEYS = frozenset(
    {"op", "params", "account", "id", "dry_run", "yes", "limit", "cursor", "all", "flood_wait_max"}
)

#: Failures that end the batch whatever `--stop-on-error` says. A spam flag
#: (PEER_FLOOD, ACCOUNT_FROZEN) means Telegram has flagged the account, and
#: every further send makes it worse; an unreachable daemon fails every
#: remaining line identically. Neither is about the line that hit it.
ABORT_EXIT_CODES = frozenset({EXIT_SPAM_FLAGGED, EXIT_DAEMON})


@dataclasses.dataclass
class _Tally:
    lines: int = 0
    ok: int = 0
    failed: int = 0
    first_failure: int = EXIT_SUCCESS
    first_nonzero: int = EXIT_SUCCESS
    aborted: int | None = None
    stopped: bool = False

    def exit_code(self) -> int:
        if self.aborted is not None:
            return self.aborted
        if self.first_failure:
            return self.first_failure
        return self.first_nonzero


def _write(out: IO[str], record: dict[str, Any]) -> None:
    out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    out.flush()


def _lines(source: IO[bytes]) -> Iterator[tuple[int, bytes]]:
    """Number the input lines, blank ones included, and yield the others.

    Read as bytes and decoded per line: a line is only ever split on `\\n`,
    which never occurs inside a multi-byte UTF-8 sequence.
    """
    for number, raw in enumerate(source, start=1):
        stripped = raw.strip()
        if stripped:
            yield number, stripped


def _parse_line(raw: bytes) -> dict[str, Any]:
    try:
        decoded = msgspec.json.decode(raw)
    except msgspec.DecodeError as exc:
        raise UsageError(f"not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise UsageError("a batch line must be a JSON object")
    return decoded


def _validate(line: dict[str, Any]) -> None:
    unknown = sorted(set(line) - LINE_KEYS)
    if unknown:
        raise UsageError(
            f"unknown key(s) {unknown} (a line may carry: {', '.join(sorted(LINE_KEYS))})",
            field=unknown[0],
        )
    if not isinstance(line.get("op"), str) or not line["op"].strip():
        raise UsageError('every line needs an "op", e.g. "message.send"', field="op")
    if not isinstance(line.get("params", {}), dict):
        raise UsageError('"params" must be an object', field="params")
    if "account" in line and not isinstance(line["account"], str):
        raise UsageError('"account" must be a string', field="account")
    for flag in ("dry_run", "yes", "all"):
        if flag in line and not isinstance(line[flag], bool):
            raise UsageError(f'"{flag}" must be true or false', field=flag)
    for number in ("limit", "flood_wait_max"):
        value = line.get(number)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise UsageError(f'"{number}" must be an integer', field=number)
    if line.get("cursor") is not None and not isinstance(line["cursor"], str):
        raise UsageError('"cursor" must be a string', field="cursor")


def _line_state(base: CliState, line: dict[str, Any]) -> CliState:
    """The single-command state this line would have had on the command line."""
    state = dataclasses.replace(base, warnings=[])
    if line.get("account"):
        state.account = line["account"].strip()
    state.dry_run = base.dry_run or bool(line.get("dry_run"))
    if line.get("flood_wait_max") is not None:
        state.flood_wait_max = line["flood_wait_max"]
    state.limit = line.get("limit")
    state.cursor = line.get("cursor")
    state.fetch_all = bool(line.get("all"))
    return state


def _run_line(line: dict[str, Any], base: CliState) -> dict[str, Any]:
    """Execute one parsed line and return its envelope (raises on failure)."""
    from tlgr.cli.gen import request_from_params, run_op
    from tlgr.registry import get

    spec = get(line["op"])
    if "live-stream" in spec.tags:
        raise UsageError(
            f"{spec.id} streams until it is stopped and cannot run inside a batch", field="op"
        )
    state = _line_state(base, line)
    if spec.needs_account and not state.account and _require_account_enabled():
        raise UsageError(
            'require_account is on: give this line an "account", or pass -a to batch',
            field="account",
        )
    request = request_from_params(spec, line.get("params") or {})
    if spec.destructive and not state.dry_run:
        # Never a prompt: stdin is the batch itself, so there is nobody to ask.
        confirm(
            f"{spec.summary.rstrip('.')}: this cannot be undone.",
            force=state.force or bool(line.get("yes")),
            no_input=True,
            hint=f'pass --yes to batch, or "yes": true on the line, to confirm {spec.id}',
        )
    envelope = run_op(spec, request, state)
    meta = envelope.get("meta") or {}
    if meta.get("indeterminate"):
        envelope["exit_code"] = EXIT_INDETERMINATE
    elif spec.empty_exit == EXIT_EMPTY and not envelope.get("result"):
        envelope["exit_code"] = EXIT_EMPTY
    else:
        envelope["exit_code"] = EXIT_SUCCESS
    return envelope


def _op_name(line: dict[str, Any] | None) -> str | None:
    if not line or not isinstance(line.get("op"), str):
        return None
    from tlgr.registry import ALIASES

    raw = line["op"].strip()
    return str(ALIASES.get(raw.replace(" ", "."), raw))


def run_batch(
    source: IO[bytes],
    state: CliState,
    *,
    stop_on_error: bool = False,
    out: IO[str] | None = None,
) -> int:
    """Run every line of *source*, write one result per line, return the exit code."""
    out = out or sys.stdout
    tally = _Tally()
    try:
        for number, raw in _lines(source):
            tally.lines += 1
            record: dict[str, Any] = {"line": number}
            line: dict[str, Any] | None = None
            try:
                line = _parse_line(raw)
                if "id" in line:
                    record["id"] = line["id"]
                _validate(line)
                envelope = _run_line(line, state)
            except Exception as exc:
                code = cli_errors.exit_status(exc)
                record["ok"] = False
                op = _op_name(line)
                if op:
                    record["op"] = op
                if line and isinstance(line.get("account"), str) and line["account"]:
                    record["account"] = line["account"]
                elif state.account:
                    record["account"] = state.account
                record["error"] = cli_errors.body_for(exc)
                record["exit_code"] = code
                tally.failed += 1
                tally.first_failure = tally.first_failure or code
                if code in ABORT_EXIT_CODES:
                    record["aborted"] = True
                    tally.aborted = code
                _write(out, record)
                if tally.aborted is not None:
                    break
                if stop_on_error:
                    tally.stopped = True
                    break
                continue

            if state.select and "result" in envelope:
                fields = [f.strip() for f in state.select.split(",") if f.strip()]
                envelope["result"] = project(envelope["result"], fields)
            record.update(envelope)
            tally.ok += 1
            tally.first_nonzero = tally.first_nonzero or int(envelope["exit_code"])
            _write(out, record)
    except KeyboardInterrupt:
        _summary(tally, code=EXIT_CANCELLED, note="interrupted")
        return EXIT_CANCELLED
    except BrokenPipeError:
        # Whoever was reading the results has gone. Stop here rather than
        # keep sending with nobody to tell what happened.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        _summary(tally, code=EXIT_GENERIC, note="stdout was closed, stopped")
        return EXIT_GENERIC
    _summary(tally, code=tally.exit_code())
    return tally.exit_code()


def _summary(tally: _Tally, *, code: int, note: str = "") -> None:
    """One line for a person, on stderr; stdout stays one record per input line."""
    text = f"batch: {tally.lines} line(s), {tally.ok} ok, {tally.failed} failed"
    if note:
        text += f"; {note}"
    elif tally.aborted is not None:
        text += f"; ABORTED (exit {tally.aborted}), remaining lines were not read"
    elif tally.stopped:
        text += "; stopped at the first failure (--stop-on-error)"
    print(f"{text}; exit {code}", file=sys.stderr)


@click.command(
    "batch",
    short_help="Run many operations from JSONL in one process.",
    epilog=(
        "Example:\n\n"
        '  printf \'%s\\n\' \'{"op": "message.send", "params": '
        '{"chat": "@alice", "text": "hi"}, "id": "m1"}\' | tlgr -a work batch'
    ),
)
@click.argument("source", required=False, default="-", metavar="[FILE]")
@click.option(
    "--stop-on-error",
    is_flag=True,
    default=False,
    help="Stop at the first line that fails (a spam flag always stops the batch).",
)
@click.option("--account", "-a", default=None, metavar="ALIAS", help="Default account for lines.")
@click.option("--dry-run", "-n", is_flag=True, default=None, help="Dry-run every line.")
@click.option("--yes", "-y", "force", is_flag=True, default=None, help="Confirm destructive lines.")
@click.option(
    "--no-input", is_flag=True, default=None, help="Accepted for symmetry; batch never prompts."
)
@click.option("--json", "use_json", is_flag=True, default=None, help="Accepted; output is JSONL.")
@click.option(
    "--select",
    "select_fields",
    default=None,
    metavar="FIELDS",
    help="Project comma-separated fields out of each result.",
)
@click.option(
    "--enable-commands",
    default=None,
    metavar="IDS",
    help="Comma-separated allowlist of operation ids, checked per line.",
)
@click.option("--flood-wait-max", type=int, default=None, help="Max auto-sleep per rate limit.")
@click.option("--timeout", type=float, default=None, help="Client-side timeout per line.")
@click.option(
    "--no-daemon-restart",
    is_flag=True,
    default=None,
    help="Never restart the daemon automatically.",
)
def batch_command(source: str, stop_on_error: bool, **values: Any) -> None:
    """Run operations from JSONL (stdin or FILE), one result line per input line.

    Each input line is a JSON object:

    \b
      op              operation id or alias (message.send, send, msg.send)
      params          request fields by name, as in `tlgr schema <op>`
      account         account alias for this line (default: -a)
      id              any JSON value, echoed back on the result line
      dry_run, yes    per-line --dry-run / --yes
      limit, cursor, all, flood_wait_max   as the flags of the same name

    Lines run in order. Each result is the operation's --json envelope plus
    `line`, the echoed `id` and `exit_code` (what the single command would
    have exited with). Blank lines are skipped. A spam flag (PEER_FLOOD,
    ACCOUNT_FROZEN) or an unreachable daemon stops the batch. The exit code
    is that abort's, else the first failed line's, else the first non-zero
    success (3 empty, 13 indeterminate), else 0.
    """
    ctx = click.get_current_context()
    try:
        state = state_from(ctx, values)
        state.fmt = "json"
        # Refuse a production home before reading a byte of input, exactly
        # as the transport would on the first daemon call.
        from tlgr.core.paths import TlgrPaths

        TlgrPaths()
        if source == "-":
            code = run_batch(sys.stdin.buffer, state, stop_on_error=stop_on_error)
        else:
            try:
                handle = open(source, "rb")  # noqa: SIM115 - closed below
            except OSError as exc:
                raise UsageError(f"{source}: {exc.strerror or exc}", field="source") from exc
            with handle:
                code = run_batch(handle, state, stop_on_error=stop_on_error)
    except (click.exceptions.Exit, click.Abort):
        raise
    except Exception as exc:
        ctx.exit(cli_errors.handle(exc, use_json=True, op="batch"))
    ctx.exit(code)
