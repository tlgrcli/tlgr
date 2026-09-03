# Changelog

All notable changes to tlgr are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project follows
semantic versioning at the CLI surface, which means the JSON shapes and exit
codes documented in `AGENT.md` are the public API.

## [Unreleased] — 2.0.0-dev

The foundation of v2: operations are defined once, as an `OperationSpec`, and
the command, its JSON Schema, its docs and its contract tests are generated
from that one definition. See `docs/design/ARCHITECTURE.md`.

The whole `message` group and `draft` are migrated — 43 operations, generated
from one registry — which is what makes the model worth trusting: it is proven
on the busiest group, not on a toy one. `tlgr/cli/message.py` and
`tlgr/cli/draft.py` are deleted rather than shadowed.

The `chat` and `folder` groups follow: 47 more operations covering the dialog
list, the per-chat settings and the chat folders. `tlgr/cli/legacy/chat.py`
keeps exactly `chat create` and `chat members`, which are member-and-admin
operations and migrate with the groups-and-channels group.

### Breaking

Every change below applies **only to commands generated from the operation
registry** — in this release that is the `message`, `draft`, `chat` and
`folder` groups, `tlgr agent exit-codes`, `tlgr agent parity` and
`tlgr schema`. Commands still hand-written under `tlgr/cli/legacy/` behave
exactly as they did in v1 until their own migration PR, at which point these
rules apply to them too.

No documented command path disappears. Every migrated operation declares its
v1 paths, so `tlgr send`, `tlgr msg list`, `tlgr message react` and the rest
still work; `tests/test_agentmd_compat.py` asserts it, and asserts that every
JSON key v1's `AGENT.md` documented is still there — except for the nine
changes in the table below, which is the whole list.

| # | Change | v1 | v2 | Migration |
|---|---|---|---|---|
| 1 | Timestamps | `"2025-03-06 12:00:00+00:00"` | `"2026-09-02T09:14:07Z"` + a `*_unix` sibling | RFC-3339 parses everywhere `str(datetime)` did not; `[defaults] legacy_dates = true` restores the old spelling for one minor release |
| 2 | `draft.list` (and `chat get`) ids | raw entity id (`123`) | marked id (`-100…123`), with `raw_id` beside it | this was COR-10, a bug: the raw id was ambiguous between a user and a channel. `raw_id` carries the old value |
| 3 | Default `parse_mode` | `md`, which silently ate `_`, `*` and backticks in ordinary text | `none` | `[defaults] parse_mode = "md"` restores it; `--parse md` is explicit (COR-21) |
| 4 | `--results-only` on a scalar result | printed a bare `2` for `message delete` | prints the result object | this was COR-18; `--select deleted` covers the scalar case |
| 5 | Error envelope | `{"error","code","exit_code"}` on stdout | `{"ok":false,"error":{…}}` with the same three keys inside `error` | `--results-only` emits the inner `error` object, byte-for-byte v1's shape |
| 6 | List envelopes: `message.list`, `message.search`, `message.forward`, `draft.list` | `{"messages":[…],"has_more":…}`, `{"forwarded":2,"ids":[…]}`, `{"drafts":[…]}` | `{"ok":true,"result":[…],"page":{…}}` | `--results-only` yields `Page[T]` = `{items, has_more, next_cursor, total}`. `message.forward` returns the forwarded messages, not just a count; `draft.set` returns the saved `Draft` instead of `{"draft": true}`; `draft.list` moved `chat_name`/`chat_username` into a nested `chat` |
| 7 | `message.edit` timestamp | `date` — the moment the message was *sent* | `edit_date` — the moment it was *edited*, which is what the field always held | rename only; `edited`, `id` and `chat_id` are unchanged, and `--select edit_date` reaches it |
| 8 | `chat.list` rows | `{"chats":[{"id","name","type","username","unread_count",…}]}` | `Page[Dialog]`, each row's peer nested under `chat` (`chat.id`, `chat.title`, `chat.kind`, `chat.username`) | `--results-only` yields `{items, has_more, next_cursor, total}`; `--select chat.id,unread_count` reaches the fields. `chats`, `inbox` and `catchup` keep working and now carry the same shape |
| 9 | `chat.poster.list` (`chat posters`) | each poster had `id`, `last_date`, `last_message_id` | `user_id` beside v1's `id`, and `date`/`date_unix`/`last_msg_id` | `posters`, `scanned_messages`, `distinct_posters`, `partial` and `flood_wait` are unchanged, and `id` is still emitted |

`tlgr agent whoami --json` reports `output_schema_version: 2`, so an agent can
branch on the two sets without probing for each change.

The envelope those changes live in: `--json` prints
`{"ok": true, "op": …, "result": …, "page": {…}, "meta": {…}}` for a success
and `{"ok": false, "error": {…}}` — on **stdout**, with a one-line summary on
stderr — for a failure. *Migration:* `--results-only` prints the inner value
verbatim in both cases, which is v1's shape, and `--select` reaches a field by
dot path.

Two more, outside the documented output shapes:

- **`tlgr schema` reports `schema_version: 2`** and gains per-operation
  `request_schema`/`response_schema` (JSON Schema draft 2020-12) and a shared
  `$defs`. *Migration:* the `command` tree and the `example_response` key
  are unchanged in meaning, and the document is still printed bare when
  `--json` is not given. Examples are now generated from the operations
  themselves rather than a hand-maintained table, so several commands gained
  one and none is stale (COR-33).
- **A policy-blocked operation exits 6** (`PERMISSION_DENIED`) rather than 2,
  and the allowlist is matched by canonical operation id, so
  `--enable-commands message.list` also permits the `msg list` alias (SEC-04).
  Hand-written groups keep v1's exit 2 for now.

### Added

- **The `message` group and `draft`, generated from the registry.** 43
  operations, 77 command paths, 30 aliases: alongside v1's ten `message`
  verbs and three `draft` verbs, the group gains `unpin`, `link`,
  `entity list`, `preview`, `compose`, `summarize`, `translate`,
  `transcribe`, `report`, `thread list`, `view get`, `read-receipt list`,
  `scheduled send`, `dice list`, `effect list`, `game *`, `paid set`,
  `fact-check set`, `sponsored *`, `suggested *`, `thread disable` and
  `tone *`. Everything a pinned Telethon (layer 227) cannot express is
  refused with `NOT_SUPPORTED` and a reason, never silently ignored.
- **The `chat` and `folder` groups, generated from the registry.** 47
  operations: v1's `chat list/open/catchup/unread/get/archive/mute/leave/
  typing/posters` keep their paths and gain `read`, `pin`, `clear`, `delete`,
  `set`, `notify get|set`, `ttl set`, `theme list|set`, `wallpaper set`,
  `translate`, `mention list`, `badge get`, `action-bar get`,
  `autoarchive set`, `promo list`, `saved list`, `report`, `import` and
  `secret *`; `folder` is new in full (`list`, `create`, `edit`, `add`,
  `remove`, `delete`, `reorder`, `join`, `share *`, `suggested list`,
  `update list`). `chat archive` gained `--undo`, `chat mute` gained
  durations that actually work (COR-01), and every list is a signed page.
  The four secret-chat commands that need end-to-end keys are registered and
  refuse with `NOT_SUPPORTED` (exit 13) rather than pretending.
- **`tlgr agent parity`** — coverage of the pinned feature catalog by
  priority and domain, with every uncovered id either waived to a named PR or
  reported as a gap. `--uncovered` prints the full list; `docs/reference/PARITY.md`
  is the same report, generated. Neither number is hand-maintained.
- **Generated reference docs.** `docs/reference/message.md`, `draft.md`,
  `chat.md`, `folder.md`, `agent.md` and `PARITY.md` come out of the registry via `make docs` /
  `make parity`; `tests/test_docs_fresh.py` fails the build on a stale page,
  so a flag cannot ship undocumented.
- `tlgr agent whoami` reports `output_schema_version: 2` (§12.4), so an agent
  can tell v1 output from v2 without probing for each changed shape.
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
- **`message search --cursor` returns the next page.** v1 restarted from the
  most recent match on every call, so a walk over a long result set looped
  forever. Cursors are opaque, signed and bound to the operation, page kind
  and account (COR-05).
- **`draft list` reports a marked chat id.** v1 printed the raw entity id, so
  a draft in a channel and a draft in a user chat could carry the same number;
  `raw_id` keeps the old value beside it (COR-10, for `draft` — `chat` follows
  in PR-3).
- **Flags that parsed and did nothing now do something.** `--dry-run` is
  enforced by the dispatcher for every mutating operation rather than by each
  command remembering to check it, and the confirmation prompt is derived from
  `destructive` on the spec (COR-16, COR-17).
- **A request body that does not fit its operation is a usage error naming the
  field**, decoded once by msgspec instead of by whichever `ctx.params` lookup
  ran first (COR-30). A peer reference on the wire is parsed by the same
  parser the CLI uses, so `/v1/op` and `tlgr` cannot disagree about what a
  `-100…` id means.
- **`agent whoami` answers without a daemon** and reports what it actually
  knows rather than a partly-filled shape (COR-34).
- Every timestamp in the new models goes through `core/timefmt`, so there is
  one format and one parser rather than a `str(datetime)` per call site
  (COR-35).
- An operation that legitimately finds nothing exits 3 (`EMPTY`) because its
  spec says so, not because a command remembered to check the length
  (COR-36).
- `GET /v1/status` reports `ready`, `version` and `protocol`, and the client
  performs a version handshake and restarts a stale daemon exactly once
  (COR-37, COR-38).
- One logging handler is installed instead of one per call, background task
  references are held so a task cannot be garbage-collected mid-flight, and
  the `ctx.params` bug is gone with the hand-written tree (COR-40, COR-41,
  COR-42).
- Webhook deliveries are signed (`X-Tlgr-Signature`), carry a monotonic
  `X-Tlgr-Seq` and a per-delivery id, and their dead-letter file is 0600 and
  rotated (SEC-08).

### Removed

- The dead `jobs.toml` job engine in `core/config.py` (`load_jobs`,
  `save_jobs`, `JobConfig`, `DestinationConfig`, …). It had no callers left;
  jobs are `jobs.yaml`, parsed by `gateway/config.py` (MNT-04).
- The `tqdm` dependency. It was pulled in for a progress bar that no command
  drew; a CLI whose output is meant to be parsed should not print one to
  stderr by default (MNT-04).
- `tlgr/cli/message.py` and `tlgr/cli/draft.py`, and their `EXAMPLE_RESPONSES`
  entries. The generated group replaces them outright — §12.4 forbids a group
  being defined in both places, and a start-up assertion enforces it.
- The hand-rolled HTTP client in `ipc_client.py`. The module stays as a shim
  over the new transport until the last v1 command migrates.
