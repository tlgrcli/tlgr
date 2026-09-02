# Changelog

All notable changes to tlgr are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project follows
semantic versioning at the CLI surface, which means the JSON shapes and exit
codes documented in `AGENT.md` are the public API.

## [Unreleased]

The foundation of v2: operations are defined once, as an `OperationSpec`, and
the command, its JSON Schema, its docs and its contract tests are generated
from that one definition. See `docs/design/ARCHITECTURE.md`.

### Breaking

Every change below applies **only to commands generated from the operation
registry** — in this release that is `tlgr agent exit-codes` and
`tlgr schema`. Commands still hand-written under `tlgr/cli/legacy/` behave
exactly as they did in v1 until their own migration PR, at which point these
rules apply to them too.

- **Response envelope.** `--json` now prints
  `{"ok": true, "op": …, "result": …, "meta": {…}}` instead of a bare result
  object. *Migration:* `--results-only` prints the result verbatim, which is
  v1's shape. Paginated operations print `Page[T]`
  (`{items, has_more, next_cursor, total}`) under `--results-only`.
- **Error envelope.** A failure prints
  `{"ok": false, "error": {…}}` to **stdout** in JSON mode, and a one-line
  summary to stderr. *Migration:* the inner error object still carries v1's
  `error`, `code` and `exit_code` keys, and `--results-only` prints exactly
  that object.
- **Timestamps.** Dates are RFC-3339 UTC (`2026-09-02T09:14:07Z`) with a
  `*_unix` integer sibling, instead of v1's `str(datetime)`
  (`2025-03-06 12:00:00+00:00`). *Migration:* `[defaults] legacy_dates = true`
  restores the old spelling for one minor release.
- **`--results-only` no longer guesses.** v1 picked whichever key looked
  "primary" and could print a bare `2` for `message delete` (COR-18); it now
  returns `result` verbatim. *Migration:* use `--select` to reach a scalar.
- **`tlgr schema` reports `schema_version: 2`** and gains per-operation
  `request_schema`/`response_schema` (JSON Schema draft 2020-12) and a shared
  `$defs`. *Migration:* the `command` tree and the `example_response` key
  are unchanged in meaning, and the document is still printed bare when
  `--json` is not given. Examples are now generated from the operations
  themselves rather than a hand-maintained table, so several commands gained
  one and none is stale (COR-33).
- **A policy-blocked operation exits 6** (`PERMISSION_DENIED`) rather than 2,
  and the allowlist is matched by canonical operation id, so
  `--enable-commands agent.exit-codes` also permits the `exit-codes` alias
  (SEC-04). Hand-written groups keep v1's exit 2 for now.
- **Default parse mode is `none`.** v1 defaulted to markdown, which silently
  ate `_`, `*` and backticks in ordinary text (COR-21). *Migration:*
  `[defaults] parse_mode = "md"`, or pass `--parse md`.

### Added

- Global flags work anywhere on the line: `tlgr agent exit-codes --json` and
  `tlgr --json agent exit-codes` are both accepted. v1 rejected the first with
  exit 2 and "No such option" (UX-01).
- Click usage errors are reported as JSON in JSON mode, with `usage` and the
  offending `field` (UX-02).
- Human tables format their values: `-` for absent, `yes`/`no` for booleans,
  joined lists, local time by recency, dot paths as columns, `--columns`,
  `--wide`, `--no-header`, and `NO_COLOR` honoured (UX-03).
- Opaque cursors are versioned, bound to an operation, page kind and account,
  given an expiry and signed. A tampered or foreign cursor is a USAGE error
  instead of a silent restart from the beginning of the list.
- `||spoiler||` and `<tg-spoiler>` produce a real spoiler entity. Telethon
  1.44 drops both silently.
- `msgspec` models for every wire shape, importable without Telethon.
- Secrets are read from `--x-env`, `--x-stdin` or `--x-file`; a secret can no
  longer be passed as a bare argument.
- Tooling: `ruff`, `mypy` (strict on the v2 modules), a `Makefile`, and a CI
  matrix over Python 3.10–3.14 on Linux and macOS.

### Fixed

- `.gitignore`'s blanket `*.yaml` rule was swallowing `.github/workflows`
  siblings, documentation YAML and test fixtures (PKG-04).
- Errors raised anywhere now map to the exit-code table in one place, so an
  unclassified failure can no longer arrive as a plausible-looking wrong exit
  (COR-06).
