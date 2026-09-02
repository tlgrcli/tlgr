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
- Tooling: `ruff`, `mypy` (strict on the v2 modules, now including the
  transport and the daemon core), a `Makefile`, and a CI matrix over Python
  3.10–3.14 on Linux and macOS.
- **Wire protocol v2.** `POST /v1/op`, `GET /v1/events`, `GET /v1/status` and
  `POST /v1/admin/*` over a 0600 Unix socket, with NDJSON streams, a version
  handshake and a race-free daemon start. The v1 routes are served by the same
  application and the same middleware chain, so every fix below applies to
  commands that have not migrated yet.
- **Supervised account sessions.** A dropped connection is a state, not an
  exception thrown at whoever was making a request: the daemon reconnects with
  capped jittered backoff, runs `catch_up()` after every reconnect and after a
  wall-clock jump, and persists update state every minute instead of only on a
  clean shutdown.
- **An event bus.** `GET /v1/events` delivers normalised events with a
  persisted, monotonic `seq`, replay via `since`, an explicit `gap` frame when
  the replay window has passed, heartbeats every 15 s, and a `lag` frame for a
  consumer that falls behind. See `docs/design/EVENTS.md`.
- **Per-account rate limiting.** Token buckets per operation class, flood-wait
  deadlines that survive a restart, local refusal of a send that slow mode or
  an owed flood wait would reject anyway, and a circuit breaker that stops
  sending on `PEER_FLOOD`/`FROZEN_*` while leaving reads working.
- **Signed webhooks.** `X-Tlgr-Signature: sha256=<hmac>` over the exact body,
  plus `X-Tlgr-Seq` and a per-delivery id.
- `SECURITY.md` documents the threat model and states plainly that the policy
  allowlist is a usability guard, not a sandbox.
- A systemd user unit (`tlgr daemon install --systemd`), and a launchd plist
  that no longer traps the daemon into never restarting.

### Fixed

- `.gitignore`'s blanket `*.yaml` rule was swallowing `.github/workflows`
  siblings, documentation YAML and test fixtures (PKG-04).
- Errors raised anywhere now map to the exit-code table in one place, so an
  unclassified failure can no longer arrive as a plausible-looking wrong exit
  (COR-06). On the v1 IPC surface an unrecognised failure is now `GENERIC`
  (exit 1) rather than `IPC_ERROR` (exit 12), which claimed the daemon
  connection had failed when it had not.
- **A Persian search query arrives.** Request bodies are JSON encoded with
  msgspec and query strings are built with `urlencode`, over `http.client`
  instead of a hand-written request line. Text containing non-ASCII
  characters, spaces, `#`, `&` or `+` reached the daemon corrupted or
  truncated before this (COR-04, COR-31, COR-32).
- **`chat mute <seconds>` actually mutes.** The deadline was built from the
  event loop's *monotonic* clock, so on a freshly started daemon `now + 3600`
  was a moment in 1970 — in the past — and every timed mute silently did
  nothing while reporting success. The effective `mute_until` is now returned
  (COR-01).
- **`--flood-wait-max` does something.** It reached the daemon and was
  ignored; it is now applied per request, on the generated commands and on the
  hand-written v1 routes (COR-15).
- **The daemon never picks an account for you.** v1 used "whichever alias came
  first out of a `set`", so a two-account user could send from the wrong
  identity with no signal. The CLI resolves the account (`-a` → `TLGR_ACCOUNT`
  → `[accounts] default` → active alias) and the daemon answers
  `ACCOUNT_REQUIRED` when it was not given one (COR-02).
- **Two `tlgr` commands cannot start two daemons.** The spawn is serialised
  behind a lock, readiness is an HTTP 200 rather than the socket file
  appearing, a live process's socket and pid file are never removed, and
  `PermissionError` from `kill(pid, 0)` is no longer read as "not running"
  (COR-14).
- **Two coroutines cannot open one session file.** `SessionManager` holds a
  per-alias lock with a double check, and the daemon holds a `flock` on the
  session file for the account's lifetime, so tlgr can no longer race itself
  into `AUTH_KEY_DUPLICATED` (COR-12).
- **A dead connection is reported as dead.** An account whose transport
  dropped is `degraded` and answers `RETRYABLE` (exit 8) with a hint; a
  revoked one is `needs_login` and answers `SESSION_ERROR` (exit 4).
  `Cannot send requests while disconnected` no longer reaches a user, and
  `tlgr daemon status` no longer reports a fully dead daemon as healthy
  (COR-13).
- **A slow webhook no longer makes every account deaf.** Delivery moved off
  the Telethon update loop onto a bounded worker pool; one unreachable
  endpoint could previously hold the loop for ~97 s (ROB-02). A payload that
  cannot be serialised is logged as a bug rather than retried three times and
  dead-lettered as a delivery failure (COR-07).
- **The daemon does not stop in the middle of your request.** Idle accounting
  counts in-flight requests, open event streams, file transfers, running jobs
  and an enabled webhook; `idle_timeout` is forced to 0 under launchd/systemd
  and with a webhook enabled, and shutdown drains in-flight work rather than
  cancelling it (COR-08, COR-11, COR-39).
- **The socket is `srw-------`.** It was `srwxrwxrwx`, with no authentication
  at all. The daemon sets `umask(0o077)` before creating anything, checks peer
  credentials on every connection, audits `~/.tlgr` at start and refuses to
  run on a world-readable session file (SEC-01).
- An account alias is validated before it becomes part of a path, and reading
  an account no longer creates its directory (SEC-02).
- The policy allowlist is enforced in the daemon, by canonical operation id,
  so an alias cannot be used to get past it (SEC-04).
- The access log is off, logs are 0600, rotating and redacted by allow-list —
  including after a rollover — and dead letters are 0600, rotated and capped
  (SEC-05, SEC-06).
- Every file carrying a secret is written through one `write_private()` that
  chmods before it renames, so nothing is briefly world-readable (SEC-07).
- A message that is not bound to a client — every message built from a raw
  `Updates` reply, including the one `message send` returns — reported empty
  text and no sender. Both are now derived.

### Removed

- The dead `jobs.toml` job engine in `core/config.py` (`load_jobs`,
  `save_jobs`, `JobConfig`, `DestinationConfig`, …). It had no callers left;
  jobs are `jobs.yaml`, parsed by `gateway/config.py` (MNT-04).
- The hand-rolled HTTP client in `ipc_client.py`. The module stays as a shim
  over the new transport until the last v1 command migrates.
