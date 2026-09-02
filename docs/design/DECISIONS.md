# Design decisions

Where `ARCHITECTURE.md` left a choice open, or where building the thing showed
the blueprint could not be followed literally, the decision is recorded here —
dated, one paragraph, with the reason rather than the rule.

## 2026-09-02 — the error table is keyed by exception class *name*

§7.1 says `core/errors.py` is the only module that names Telethon exception
classes; §2.2 says `cli/` must never import Telethon. Both hold only if the
names are strings: `ERROR_MAP` is keyed by class name and `classify()` walks
`type(exc).__mro__` looking each name up, so classifying a `FloodWaitError`
imports nothing. Walking the MRO also means a Telethon subclass we have never
heard of lands on its base's row — every unknown `*ForbiddenError` becomes
PERMISSION_DENIED rather than GENERIC.

## 2026-09-02 — `format_error_json` keeps v1's flat shape

The blueprint's §12.4 wants `{"ok": false, "error": {…}}` on stdout with
`--results-only` yielding the inner object in v1's spelling. Rather than
change what the existing public function returns, `format_error_json` keeps
v1's exact flat dict (it *is* the `--results-only` payload) and the envelope
is a new `error_envelope()`. The inner object carries both `message` and v1's
`error` key, so a v1 consumer reading `error`/`code`/`exit_code` keeps working
either way.

## 2026-09-02 — `PageKind` lives in `core/pagination.py`

§2.1 lists it under `ops/_spec.py`, but `core/pagination.py` needs it to
validate a cursor and `core/` sits below `ops/`. It is therefore defined in
`core/pagination.py` and re-exported from `ops/_spec.py`, so the spelling in
the blueprint (`from tlgr.ops._spec import PageKind`) still works.

## 2026-09-02 — the schema document is handed its command tree

`tlgr schema` still describes the Click tree, because most commands are not
migrated yet — but `ops/` must not import `cli/`. The walker therefore lives
in `cli/introspect.py`, and the `agent.schema` implementation receives it
through the op context. `tlgr/schema.py` itself imports no click and only
knows about the registry.

## 2026-09-02 — `json-only` operations print bare JSON unless `--json` is given

A schema document has no table shape, and v1 printed it as JSON whatever the
output flags said. An op tagged `json-only` therefore always renders as JSON;
without an explicit `--json` it prints the document bare (v1's exact output)
and with `--json` it gets the v2 envelope. That keeps `tlgr schema | jq
.schema_version` working while the new rules still apply where they are asked
for.

## 2026-09-02 — a policy block is exit 6 for generated commands, exit 2 for legacy

§7.2 says an operation blocked by policy is PERMISSION_DENIED, exit 6.
v1's `--enable-commands` exited 2. Generated commands use the new code (and
match by canonical op id, so an alias cannot slip past the allowlist —
SEC-04); the v1 path matching, and its exit 2, stay in place for groups that
are still hand-written, and go when they do.

## 2026-09-02 — `agent whoami` stays hand-written inside a generated group

`agent exit-codes` and `schema` are registered ops, so the `agent` group is
generated — but `whoami` reads the account manager and the daemon status and
belongs with the account group (PR-2). `build_cli()` therefore has one
enumerated exception, `LEGACY_EXTRAS`, listing commands still hand-written
inside a generated group. It is a list of promises to delete, not a general
escape hatch: the "defined in both places" assertion still fires for anything
not named in it.

## 2026-09-02 — peer links normalise as far as they can

§3.2 lists `link` among the `PeerRef` kinds. Since the point of `value` is to
be normalised, a link is classified as far as it can be: `t.me/<name>` and
`tg://resolve?domain=` become `username`, `t.me/c/<id>/<n>` and
`tg://privatepost` become a marked `id`, `t.me/+hash` and `tg://join` become
`invite`. `link` is what remains for a t.me/tg:// reference we recognise as
Telegram's but cannot classify further.

## 2026-09-02 — non-file media kinds come from a table

v1 derived the kind by lowercasing the TL class name minus `MessageMedia`,
which produced `geolive` and `paidmedia` — neither of which is in the
`MediaKind` vocabulary. The document branch keeps v1's logic exactly
(attributes collected first, kind decided after); the non-document branch maps
through an explicit table and falls back to `unsupported`.

## 2026-09-02 — request constraints are enforced by a round trip

Constructing a msgspec Struct does not run its `Meta` constraints; only
decoding does. The generated command therefore builds the request and
immediately re-validates it with `msgspec.convert`, so `--limit 500` against
`le=100` fails in the CLI with a USAGE error naming the field rather than in
the daemon.

## 2026-09-02 — `TLGR_HOME` overrides where the cursor key lives

`core/pagination` needs a signing key at `~/.tlgr/cursor.key`. Tests must not
write to a developer's real home directory, and `CONFIG_DIR` is a module
constant computed at import. `TLGR_HOME` is read at call time and wins when
set; everything else keeps using `CONFIG_DIR`.

## 2026-09-02 — ruff formats code, not the design documents

`ruff format` reformats Python inside Markdown fences, which rewrote the
illustrative snippets in `ARCHITECTURE.md`. Those snippets are prose about
code, not code, so `*.md` is excluded from formatting.

## 2026-09-02 — strict typing is configured per module, not per invocation

§11.4 runs `mypy --strict` over whole packages. `tlgr/core` contains v1
modules that will not pass strict until they are rewritten, so strictness is
declared in `pyproject.toml` for the modules that are ready
(`models`, `ops`, `registry`, `schema`, `core.errors`, `core.timefmt`,
`core.pagination`) with `follow_imports = "silent"`, and the list grows as
each group PR lands.
