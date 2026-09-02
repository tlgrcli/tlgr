# tlgr v2 — production architecture

**Status:** accepted · **Applies to:** `tlgr` 2.x · **Owner:** the foundation PR
**Path in repo:** `docs/design/ARCHITECTURE.md`
**Companion documents:** `docs/design/EVENTS.md` (event taxonomy), `docs/design/STYLE.md` (command-surface style guide), `docs/reference/*.md` (generated), `docs/reference/PARITY.md` (generated).

This is the blueprint the foundation PR implements and every subsequent group PR follows. It is written to be implementable without re-reading the research: the exact structs, the exact wire shapes, the exact file list, the exact acceptance criteria.

Inputs this document consolidates (all read in full):

| Input | What it fixed here |
|---|---|
| production-readiness audit (85 findings, `tlgr_audit.md` / `.json`) | §7 error table, §8 security baseline, §12 foundation scope, Appendix A |
| MTProto/Telegram production notes (25-item daemon checklist) | §6 daemon internals, §3 model fidelity, §10 config |
| Telethon 1.44 capability analysis | §6.3–§6.8 (what to wrap vs. reimplement), §11 fake client |
| Telegram error taxonomy (780 error strings, layer 227) | §7 |
| command-surface style guide `STYLE.md` | §4 CLI mapping, §9 output |
| current code (`tlgr/**`, 7,866 lines; 273 tests green) | §2 migration, §12 |
| feature catalog (1,916 ids, 12 domains) | §1 parity model, §2.3 group plug-in points |

---

## 0. The problem in one paragraph

tlgr v1 expresses every operation **five times** — a Click command, an aiohttp route, an IPC handler, a `ClientWrapper` method, and an `EXAMPLE_RESPONSES` entry — plus two hand-written docs. At 94 commands the drift is already visible (26 of 93 commands have an example; several are stale). At ~500 commands it is ~2,500 lines of pure duplication and permanent drift. Meanwhile the transport is a hand-rolled HTTP client that cannot send a Persian search query, the daemon collapses every error to `IPC_ERROR`/exit 12, the IPC socket is `srwxrwxrwx` with no authentication, and the daemon picks "whichever account is first in a `set`" when `-a` is omitted. v2 replaces everything hand-written with **generation from one source of truth** (the operation registry), replaces the transport with a typed JSON protocol over a 0600 socket, and turns the daemon from a bag of handlers into a supervised, rate-limited, event-producing session host.

---

## 1. Goals, non-goals, and the parity model

### 1.1 Goals

1. **GUI parity.** Everything an official Telegram client can do, a tlgr command can do, measured against the feature catalog (§1.3). Coverage is a number in CI, not an opinion.
2. **One artefact per operation.** Adding an operation means adding one `OperationSpec`. The CLI command, the daemon dispatch entry, the JSON Schema, the reference docs and the contract tests are generated from it. Nothing is written twice.
3. **Production-correct.** Update state survives restarts (`catch_up=True` + supervised reconnect + periodic persistence); one process owns one session file; floods are remembered across restarts; errors are classified, not collapsed; the socket is authenticated.
4. **Equally usable by a human and an agent.** `--json` is a first-class contract with a published JSON Schema, stable exit codes, opaque cursors, and NDJSON streams. Human output is a table with sane formatting.
5. **Honest about uncertainty.** Exit 13 (`INDETERMINATE`) exists because "we could not establish this" must never be reported as "no". That property is preserved and generalised.
6. **Backwards compatible at the CLI surface.** Every v1 command path and JSON shape documented in `AGENT.md` keeps working (§12.4), even while the wire protocol underneath changes completely.

### 1.2 Non-goals

* **Not a TDLib port.** tlgr wraps Telethon 1.44 (layer 227). Layer-229-only features (communities, ephemeral/guest-chat messages, the new keyboard model, Firebase login) are out of scope; the escape hatch for calling them is documented (§6.14) but not used.
* **No WebRTC.** `call`/`vc`/`conference` cover signalling, membership, invite links, recording toggles and metadata — never audio/video media.
* **No bot-only surface.** tlgr is a *user account* client. Bot-only methods (`bot_only`, 31 methods in the error DB) are catalogued as `not-applicable`.
* **No account creation.** `auth.signUp` is never called. Sign-up-required responses are a hard error with a human hint.
* **No official-app impersonation.** Never borrow an official `api_id`/`api_hash`; never spoof `device_model`/`lang_pack` to obtain official-app behaviour (ToS violation, gets accounts banned).
* **Not Windows-native in v2.0.** `fork`, `flock`, `AF_UNIX` and peer-credential checks are POSIX. Windows is a v2.x item (named pipes + a different singleton); the trove classifier is corrected to say so.
* **No plaintext secret handling in argv.** Ever.

### 1.3 Parity model — how coverage is measured

The feature catalog (`analysis/feature_catalog.json`, 1,916 entries) is the ground truth. Each entry has an `id` (`<group>.<slug>`, e.g. `groups-channels-admin.ban-member`), a `domain`, a `priority` (P0–P3) and a `cli.feasibility`.

```
                       total   coverage-required   excluded
full            1,583      ✓
partial           168      ✓                      —
control-only       46      ✓                      —
not-applicable     79                             ✓ (bot-only / server-side / GUI-only)
prohibited         40                             ✓ (ToS, spam, deanonymisation)
                 -----   ------------------      --------
                 1,916    1,797                      119

priority split: P0 178 · P1 382 · P2 620 · P3 736
v1 today:       full 54 · partial 163 · none 1,699   (2.8 % complete)
```

**The contract.** A pruned catalog index ships in the package at `tlgr/data/catalog_index.json` (id, domain, group, name, priority, feasibility — ~250 KB, regenerated by `tools/prune_catalog.py`). Every `OperationSpec` declares `covers: tuple[str, ...]` and, when coverage is partial, `covers_partial: tuple[str, ...]` with a `coverage_note`. Two lints run at import and in CI:

* **L-COV-1** every id in `covers`/`covers_partial` exists in the catalog index (typos are build failures);
* **L-COV-2** every catalog id whose feasibility is `full`, `partial` or `control-only` **and** whose domain has been migrated is covered by at least one op. Domains not yet migrated are listed in `tlgr/data/parity_waivers.toml` with the PR number that will close them, so the gate is meaningful from day one instead of being switched on at the end.

**The report.**

```
$ tlgr agent parity --json
{
  "catalog_version": "2026-09-02",
  "required": 1797, "covered": 1797, "percent": 100.0,
  "by_priority": {"P0": {"required": 178, "covered": 178, "percent": 100.0}, ...},
  "by_domain":   {"messages_core": {"required": 160, "covered": 160, "ops": 71}, ...},
  "partial":     [{"id": "media.stream-download", "op": "media.download", "note": "..."}],
  "uncovered":   [{"id": "...", "priority": "P2", "reason": "waived until PR-9"}],
  "excluded":    {"not-applicable": 79, "prohibited": 40},
  "ops": 503, "commands": 511, "aliases": 38
}

$ tlgr agent parity            # human: one table per domain + a totals line
$ make parity                  # writes docs/reference/PARITY.md, exits 1 on regression
```

CI gate: **P0 coverage may never decrease and must reach 100 % before 2.0.0 final**; total coverage may never decrease; an op covering an unknown id fails the build.

### 1.4 Definition of done for a group PR

A group is "done" when: every non-excluded catalog id in its domain is covered or explicitly waived with a reason; every op has a validated `example`; `docs/reference/<group>.md` regenerates with no diff; the group's legacy `tlgr/cli/<group>.py` module and its IPC routes are deleted; `mypy --strict` passes on the new `ops` module; and `tlgr agent parity` shows the domain at its target percentage.

---

## 2. Module layout

### 2.1 The tree after the foundation PR

```
tlgr/
├── __init__.py                  version string only
├── __main__.py                  python -m tlgr
├── version.py                   VERSION, PROTOCOL_VERSION, CATALOG_VERSION, MIN_DAEMON_PROTOCOL
├── data/
│   ├── catalog_index.json       pruned feature catalog (parity source of truth)
│   └── parity_waivers.toml      domains/ids not yet expected to be covered
│
├── models/                      msgspec Structs — the ONLY place a wire shape is defined
│   ├── __init__.py              re-exports; `from tlgr.models import Message, Page` works
│   ├── base.py                  Struct base config, UNSET, JSON encode/decode helpers, RFC-3339
│   ├── peer.py                  PeerRef, PeerId, PeerKind, UserRef, User, Chat, ChatPhoto, Rights
│   ├── message.py               Message, MessageEntity, MediaSummary, Forward, ReplyHeader,
│   │                            ServiceAction, ReactionSummary, ReplyMarkup, Button
│   ├── dialog.py                Dialog, Draft, Folder, NotifySettings
│   ├── page.py                  Page[T], PageInfo, cursor kinds
│   ├── error.py                 ErrorBody (the wire error), ErrorCode enum
│   ├── event.py                 EventEnvelope, EventType placeholder (see docs/design/EVENTS.md)
│   └── envelope.py              OkEnvelope, ErrEnvelope, Meta
│
├── ops/                         operation definitions — one module per (sub)domain
│   ├── __init__.py              imports every module, builds REGISTRY, runs lints
│   ├── _spec.py                 OperationSpec, OpContext, Surface, helpers (PageKind is
│   │                            re-exported from core/pagination — core sits below ops)
│   ├── _params.py               request-field annotation vocabulary (positional(), secret(), …)
│   ├── message.py               ← FOUNDATION: the whole `message` group (proof of the model)
│   ├── draft.py                 ← FOUNDATION: `draft set|clear|list` (rides the same models)
│   ├── daemon.py                ← FOUNDATION: daemon status/stop/reload/resync/events
│   └── agent.py                 ← FOUNDATION: whoami / exit-codes / parity / schema (local ops)
│
├── registry.py                  REGISTRY mapping + lookup by id/alias + lint entry point
├── schema.py                    JSON Schema generation (msgspec.json.schema_components)
├── docsgen.py                   docs/reference/<group>.md generation
├── parity.py                    catalog ↔ registry coverage report
│
├── cli/
│   ├── __init__.py              build_cli(): generated tree + not-yet-migrated legacy groups
│   ├── gen.py                   OperationSpec → click.Command (params, aliases, help, examples)
│   ├── introspect.py            describes the click tree for `tlgr schema` (handed to
│   │                            tlgr/schema.py, which imports no click)
│   ├── params.py                click ParamTypes: PEER, USER, MSGREF, DURATION, DATETIME, SECRET…
│   ├── globals.py               global flags attached to every command; CliState
│   ├── render.py                JSON / plain / human renderers driven by op.columns
│   ├── confirm.py               one confirm() honouring --yes/--no-input/TTY
│   ├── errors.py                click.UsageError → JSON usage error; TlgrError → exit code
│   └── legacy/                  v1 hand-written command modules, deleted group by group
│       ├── chat.py  contact.py  user.py  profile.py  media.py  account.py
│       ├── config_cmd.py  job.py  completion.py  watch.py
│       └── …                    (moved verbatim from tlgr/cli/*.py, imports rewritten)
│
├── transport/
│   ├── __init__.py
│   ├── client.py                UnixHTTPConnection (stdlib http.client), call_op(), stream_op(),
│   │                            events(), status(), admin(); handshake + daemon auto-start
│   ├── ndjson.py                NDJSON frame reader/writer (bytes, not str)
│   └── autostart.py             race-free daemon spawn: flock probe → spawn → wait for /v1/status
│
├── daemon/
│   ├── __init__.py
│   ├── main.py                  argv, config, umask, singleton, daemonise, run()
│   ├── app.py                   aiohttp Application + middleware chain + route table
│   ├── dispatch.py              /v1/op: decode → policy → account → dry-run → timeout → impl
│   ├── stream.py                NDJSON responses for stream ops and --all walks
│   ├── session.py               AccountSession: client, state machine, supervisor, catch-up
│   ├── sessions.py              SessionManager: alias → AccountSession, per-alias locks
│   ├── events.py                EventBus: normalisation, ring buffer, seq, subscribers
│   ├── webhook.py               webhook subscriber: bounded queue, workers, HMAC, dead letter
│   ├── ratelimit.py             token bucket, persisted flood memory, circuit breaker
│   ├── policy.py                op-id allowlist enforcement (canonicalised)
│   ├── peercred.py              SO_PEERCRED / LOCAL_PEERCRED, token fallback
│   ├── singleton.py             flock on daemon.lock and on each <alias>/session.lock
│   ├── idle.py                  activity accounting + idle-stop decision
│   ├── files.py                 download/upload pipelines, progress events, fileref refresh
│   ├── preauth.py               daemon-hosted login flows (code / password / QR / import)
│   ├── jobs.py                  JobRunner (unchanged API, event-bus fed)
│   ├── lifecycle.py             daemonise, pid/lock files, signals, log setup
│   ├── launchd.py               macOS plist install/uninstall
│   └── systemd.py               Linux user unit install/uninstall
│
├── core/
│   ├── errors.py                ERROR_MAP (the single Telethon-exception table), TlgrError tree
│   ├── peers.py                 entity resolution service (per account)
│   ├── pagination.py            PageKind, Cursor encode/decode/validate, Page building
│   ├── paths.py                 ~/.tlgr layout, alias validation, write_private(), 0600 audit
│   ├── config.py                config.toml → typed Structs; reload; defaults
│   ├── accounts.py              AccountManager (validated aliases, health, active alias)
│   ├── identity.py              honest initConnection identity strings
│   ├── timefmt.py               RFC-3339 in/out, duration parsing (30s/5m/2h/7d/forever)
│   ├── text.py                  parse_mode handling, entity offsets (UTF-16), split_text
│   ├── logging.py               structured rotating logs + redaction filter
│   └── telethon_compat.py       pinned private-API adapters (_save_states_and_entities, …)
│
├── gateway/                     unchanged pipeline, re-pointed at the event bus
│   ├── engine.py                subscribes to EventBus instead of Telethon handlers
│   ├── config.py  event.py
├── filters/  processors/  actions/    unchanged
└── tools/
    ├── prune_catalog.py         feature_catalog.json → tlgr/data/catalog_index.json
    └── gen_docs.py              docsgen entry point used by `make docs`
```

### 2.2 Layer rules (enforced by an import lint)

```
cli/  ──▶ transport/ ──▶ (socket) ──▶ daemon/ ──▶ ops/ ──▶ core/ ──▶ models/
  │                                                  │
  └──────────────▶ registry/models/schema ◀──────────┘
```

* `models/` imports nothing from tlgr. It must import cleanly without Telethon.
* `ops/` may import `models`, `core`, and Telethon. It must **not** import `daemon` or `cli`.
* `cli/` may import `models`, `registry`, `transport`, `core.errors`, `core.timefmt`. It must **not** import Telethon or `daemon` (so `tlgr --help` stays fast and works with no Telethon installed).
* `daemon/` may import everything except `cli`.
* Only `core/errors.py` names Telethon exception classes. Only `ops/` and `daemon/` call Telethon.

The lint is a 20-line test (`tests/test_layering.py`) walking `ast` imports; it is cheap and it is the thing that keeps the tree honest at 500 ops.

### 2.3 Where each future command group plugs in

One `ops/` module per CLI noun (or noun family). The registry does not care about files; the files exist so that a group PR touches one place.

| Catalog domain | ids (P0) | `tlgr/ops/` modules | CLI nouns | PR |
|---|---|---|---|---|
| messages_core | 171 (28) | `message.py`, `draft.py`, `search.py` | `message`, `draft`, `search` | PR-1 (foundation) |
| auth_sessions_security | 99 (8) | `auth.py`, `account.py`, `session.py`, `security.py`, `password.py` | `auth`, `account`, `session`, `security` | PR-2 |
| dialogs_chats | 150 (25) | `chat.py`, `folder.py`, `dialog.py` | `chat`, `folder` | PR-3 |
| updates_sync_network | 200 (21) | `events.py`, `sync.py`, `proxy.py`, `daemonops.py`, `export.py`, `stats.py`, `boost.py` | `events`, `watch`, `daemon`, `proxy`, `export`, `stats`, `boost` | PR-4 |
| contacts_users | 124 (17) | `contact.py`, `user.py`, `block.py` | `contact`, `user` | PR-5 |
| media_files | 150 (14) | `media.py`, `sticker.py`, `gif.py`, `emoji.py` | `media`, `sticker`, `gif`, `emoji` | PR-6 |
| groups_channels_admin | 164 (14) | `chat_member.py`, `chat_admin.py`, `chat_invite.py`, `chat_topic.py`, `chat_permission.py`, `adminlog.py` | `chat member/admin/invite/topic/permission`, `chat admin-log` | PR-7 |
| stories | 124 (14) | `story.py` | `story` | PR-8 |
| polls_reactions_content | 193 (10) | `poll.py`, `reaction.py`, `todo.py`, `location.py`, `link.py` | `poll`, `reaction`, `todo`, `location`, `link` | PR-9 |
| bots_inline_payments | 197 (13) | `bot.py`, `inline.py`, `webapp.py`, `payment.py` | `bot`, `inline`, `webapp`, `payment` | PR-10 |
| calls_voicechats | 148 (10) | `call.py`, `vc.py`, `conference.py` | `call`, `vc`, `conference` | PR-11 |
| profile_settings_privacy | 196 (4) | `profile.py`, `privacy.py`, `notify.py`, `settings.py`, `business.py`, `premium.py`, `gift.py`, `stars.py` | `profile`, `privacy`, `notify`, `settings`, `business`, `premium`, `gift`, `stars` | PR-12 |

Adding a group is: create `tlgr/ops/<name>.py`, define specs, import it in `ops/__init__.py`, delete `tlgr/cli/legacy/<name>.py` and its routes, run `make docs parity`. No other file changes.

### 2.4 What gets deleted (and when)

| Deleted | Replaced by | When |
|---|---|---|
| `tlgr/ipc_client.py` (hand-rolled HTTP, `_decode_chunked`) | `tlgr/transport/client.py` | foundation |
| `tlgr/daemon/ipc.py` route table + 37 handlers | `daemon/app.py` + `daemon/dispatch.py` (generic) | one group at a time; file deleted at PR-12 |
| `tlgr/cli/schema.py::EXAMPLE_RESPONSES` | `OperationSpec.example` | foundation (message), rest per group |
| `tlgr/cli/message.py`, `draft.py` | `ops/message.py`, `ops/draft.py` | foundation |
| `tlgr/cli/watch.py` (polling loop, COR-09) | `GET /v1/events` + `ops/events.py` | foundation ships the endpoint; `watch` re-points in PR-4 |
| `tlgr/core/client.py::ClientWrapper` (1,242 lines, mixed concerns) | `daemon/session.py` + per-op impls + `core/peers.py` | shrinks per group; deleted at PR-12 |
| `tlgr/core/config.py` dead job engine (`load_jobs`, `JobConfig`, `DestinationConfig`, …) | `gateway/config.py` (already the live one) | foundation |
| `tqdm` dependency | progress events over NDJSON | foundation |
| hand-written command tables in `README.md`/`AGENT.md` | `docs/reference/*.md` (generated), prose kept by hand | per group |

---

## 3. Core data models

### 3.1 Conventions

* **msgspec Structs, everywhere on the wire.** `models/base.py` defines the shared base:

```python
# tlgr/models/base.py
from __future__ import annotations          # required: we target Python >= 3.10 and use `X | None`
from typing import TypeVar, Union
import msgspec

class Model(msgspec.Struct, kw_only=True, omit_defaults=True, forbid_unknown_fields=False):
    """Response/domain model. Unknown fields are tolerated on decode (forward compat)."""

class Request(msgspec.Struct, kw_only=True, omit_defaults=True, forbid_unknown_fields=True):
    """Operation request. Unknown fields are a USAGE error — a newer CLI must not
    silently lose a field against an older daemon; the handshake catches that first."""

UNSET = msgspec.UNSET          # "field not supplied" — distinct from an explicit null
_T = TypeVar("_T")
Unset = Union[_T, msgspec.UnsetType]        # Unset[str]; PEP 695 syntax needs 3.12, we need 3.10
```

Every model module starts with `from __future__ import annotations`; msgspec resolves the
string annotations with `typing.get_type_hints` at first use, so `X | None`, `list[T]` and
forward references all work on Python 3.10.

  `omit_defaults=True` keeps JSON small and makes "absent" meaningful. `UNSET` gives `set`/`edit` operations a genuine tri-state — v1 could not express that. The tri-state field form is

```python
bio: Annotated[Unset[str | None], opt("--bio", "--clear-bio")] = UNSET
#   omitted    -> UNSET  -> "leave alone"   (not serialised at all)
#   --bio "x"  -> "x"    -> "set to x"
#   --clear-bio-> None   -> "clear"
```

  (verified: `Unset[str]` alone rejects an explicit `null`; the `| None` is what makes "clear" expressible.)

* **Identifiers.** Every peer id on the wire is the **marked** id from `utils.get_peer_id` (`123` user, `-55` basic group, `-1001234567890` channel). `raw_id` and `kind` are always emitted next to it so a consumer never has to parse the sign. This closes COR-10, where `chat get` returned `123` and `chat list` returned `-1000000000123` for the same channel.
* **Time.** RFC-3339 UTC with a `Z` suffix in JSON (`2026-09-02T12:00:00Z`), plus a `*_unix` integer for every timestamp an agent is likely to compare. Human output renders local time. One helper (`core/timefmt.fmt_dt` / `parse_dt`) — no `str(datetime)` anywhere. Closes COR-35.
* **Text.** `text` is the raw message text; formatting lives in `entities` (offsets are UTF-16 code units, as Telegram defines them). tlgr never round-trips through Telethon's lossy `unparse`.
* **Optionality.** A field that is *absent* means "not applicable / not requested"; a field that is `null` means "known to be empty". Lists default to `[]`, never `null`.
* **No secrets in models.** `access_hash`, `auth_key`, `file_reference` bytes and bot tokens never appear in any model, log, or error.

### 3.2 Peers, users, chats

```python
# tlgr/models/peer.py
from typing import Annotated, Literal
import msgspec
from msgspec import Meta
from tlgr.models.base import Model

PeerKind = Literal["user", "bot", "saved", "group", "supergroup", "channel", "unknown"]

class PeerRef(Model):
    """How a peer is *addressed* on input. Parsed by core/peers.py, never guessed."""
    raw: str                       # exactly what the user typed: "@x", "-100…", "+98…", "t.me/x", "me"
    kind: Literal["id", "username", "phone", "link", "invite", "self", "saved"] 
    value: str | int               # normalised: int id, lowercase username without @, E.164 phone, hash

class Peer(Model):
    """How a peer is *identified* on output. Present on every object that has a peer."""
    id: int                        # marked id (utils.get_peer_id)
    raw_id: int                    # unmarked id
    kind: PeerKind
    title: str                     # display name: "First Last" or chat title; never None
    username: str | None = None
    usernames: list[str] = []      # collectible/extra usernames (channel.usernames)
    is_self: bool = False

class User(Model):
    id: int
    raw_id: int
    kind: Literal["user", "bot"] = "user"
    first_name: str | None = None
    last_name: str | None = None
    title: str = ""                # convenience: joined display name
    username: str | None = None
    usernames: list[str] = []
    phone: str | None = None
    is_self: bool = False
    is_contact: bool = False
    is_mutual_contact: bool = False
    is_deleted: bool = False
    is_bot: bool = False
    is_verified: bool = False
    is_scam: bool = False
    is_fake: bool = False
    is_premium: bool = False
    is_support: bool = False
    is_blocked: bool | None = None          # only from users.getFullUser
    restricted: bool = False
    restriction_reason: list[str] = []
    lang_code: str | None = None
    status: str | None = None               # "online" | "recently" | "last_week" | "last_month" | "offline" | "hidden"
    status_expires: str | None = None       # RFC-3339 when status == "online"
    last_seen: str | None = None            # RFC-3339 when known exactly
    stories_hidden: bool = False
    emoji_status_id: int | None = None
    photo: "Photo | None" = None
    # --- full-profile fields (only when the op fetched users.getFullUser) ---
    bio: str | None = None
    birthday: str | None = None             # "YYYY-MM-DD" or "--MM-DD"
    common_chats_count: int | None = None
    personal_channel_id: int | None = None
    business_hours: dict | None = None
    min: bool = False                       # access hash usable only via *FromMessage

class Photo(Model):
    id: int
    has_video: bool = False
    stripped_thumb_b64: str | None = None   # inline JPEG preview, base64; no download needed
    dc_id: int | None = None

class Rights(Model):
    """Flattened ChatAdminRights / ChatBannedRights. True == allowed, everywhere,
    including for banned rights (Telegram stores them inverted; we normalise once)."""
    change_info: bool | None = None
    post_messages: bool | None = None
    edit_messages: bool | None = None
    delete_messages: bool | None = None
    ban_users: bool | None = None
    invite_users: bool | None = None
    pin_messages: bool | None = None
    add_admins: bool | None = None
    manage_call: bool | None = None
    manage_topics: bool | None = None
    post_stories: bool | None = None
    edit_stories: bool | None = None
    delete_stories: bool | None = None
    manage_direct_messages: bool | None = None
    anonymous: bool | None = None
    other: bool | None = None
    # banned-only, granular (send_photos/videos/… from ChatBannedRights)
    send_messages: bool | None = None
    send_media: bool | None = None
    send_photos: bool | None = None
    send_videos: bool | None = None
    send_audios: bool | None = None
    send_voices: bool | None = None
    send_roundvideos: bool | None = None
    send_docs: bool | None = None
    send_stickers: bool | None = None
    send_gifs: bool | None = None
    send_games: bool | None = None
    send_inline: bool | None = None
    send_polls: bool | None = None
    send_plain: bool | None = None
    embed_links: bool | None = None
    until: str | None = None                # RFC-3339 or null == forever

class Chat(Model):
    id: int
    raw_id: int
    kind: PeerKind                           # group | supergroup | channel | user | bot | saved
    title: str
    username: str | None = None
    usernames: list[str] = []
    is_creator: bool = False
    is_admin: bool = False
    is_broadcast: bool = False
    is_forum: bool = False
    is_gigagroup: bool = False
    is_verified: bool = False
    is_scam: bool = False
    is_fake: bool = False
    is_restricted: bool = False
    noforwards: bool = False
    join_to_send: bool | None = None
    join_request: bool | None = None
    signatures: bool | None = None
    slowmode_seconds: int | None = None
    slowmode_next_send: str | None = None
    participants_count: int | None = None
    online_count: int | None = None
    photo: Photo | None = None
    date: str | None = None                  # creation date, RFC-3339
    left: bool = False
    # --- full-chat fields (only when the op fetched channels.getFullChannel) ---
    about: str | None = None
    pinned_message_id: int | None = None
    linked_chat_id: int | None = None
    migrated_from_chat_id: int | None = None
    available_reactions: list[str] | None = None
    default_rights: Rights | None = None
    my_rights: Rights | None = None
    ttl_period: int | None = None
    stats_dc: int | None = None
    can_view_participants: bool | None = None
    hidden_prehistory: bool | None = None
    antispam: bool | None = None
```

### 3.3 Message and everything hanging off it

```python
# tlgr/models/message.py
from typing import Literal
from tlgr.models.base import Model
from tlgr.models.peer import Peer, Photo

MediaKind = Literal[
    "photo", "video", "gif", "audio", "voice", "video_note", "sticker", "file",
    "contact", "geo", "geo_live", "venue", "poll", "dice", "game", "invoice",
    "webpage", "story", "giveaway", "paid", "todo", "unsupported",
]

class MediaSummary(Model):
    """What the media IS, from the attributes the message already carries.
    Nothing here downloads a byte. This is v1's `media_details()` promoted to a model:
    `media_type` alone says 'MessageMediaDocument' for a thumbs-up sticker, a voice
    note, a GIF and a PDF alike, and a caption-less one of those IS the message."""
    kind: MediaKind
    tl_type: str                              # "MessageMediaDocument" — the raw label, kept for debugging
    mime_type: str | None = None
    file_name: str | None = None
    size: int | None = None
    duration: int | None = None               # seconds, audio/video/voice/video_note
    width: int | None = None
    height: int | None = None
    alt: str | None = None                    # a sticker's emoji — which IS its content
    sticker_set: str | None = None
    performer: str | None = None
    title: str | None = None
    waveform: bool = False
    is_animated: bool = False
    supports_streaming: bool = False
    spoiler: bool = False
    ttl_seconds: int | None = None
    round: bool = False
    stripped_thumb_b64: str | None = None
    thumbs: list[str] = []                    # available size types: ["s","m","x","y"]
    dc_id: int | None = None
    downloadable: bool = True
    # non-file media, flattened rather than nested one-of:
    latitude: float | None = None
    longitude: float | None = None
    venue_title: str | None = None
    contact_phone: str | None = None
    contact_name: str | None = None
    dice_emoji: str | None = None
    dice_value: int | None = None
    webpage_url: str | None = None
    webpage_title: str | None = None
    story_peer_id: int | None = None
    story_id: int | None = None
    paid_stars: int | None = None

class MessageEntity(Model):
    type: str            # "bold" | "italic" | "code" | "pre" | "text_url" | "spoiler" |
                         # "custom_emoji" | "blockquote" | "mention_name" | …  (snake_case of the TL name)
    offset: int          # UTF-16 code units
    length: int
    url: str | None = None
    user_id: int | None = None
    language: str | None = None
    document_id: int | None = None
    collapsed: bool | None = None

class Button(Model):
    text: str
    type: str                                  # "callback" | "url" | "switch_inline" | "web_view" | …
    data_b64: str | None = None                # callback data, base64 (bytes never raw in JSON)
    url: str | None = None
    query: str | None = None
    user_id: int | None = None
    requires_password: bool = False

class ReplyMarkup(Model):
    kind: Literal["inline", "keyboard", "hide", "force_reply"]
    rows: list[list[Button]] = []
    resize: bool | None = None
    single_use: bool | None = None
    selective: bool | None = None
    persistent: bool | None = None
    placeholder: str | None = None

class ReactionSummary(Model):
    """Compact reaction state including whether WE already reacted.
    `mine` matters: Telegram answers a duplicate reaction with MESSAGE_NOT_MODIFIED,
    so without it the only way to learn a reaction is already there is to send one
    and read the failure. Derived from ReactionCount.chosen_order."""
    counts: dict[str, int] = {}                # "❤" -> 2 ; custom emoji as "custom:<document_id>"
    mine: list[str] = []
    total: int = 0
    can_see_list: bool | None = None
    as_tags: bool = False
    recent: list[dict] = []                    # [{"peer_id": …, "reaction": "👍", "date": "…"}] when asked
    paid_stars: int | None = None

class Forward(Model):
    from_id: int | None = None                 # marked peer id of the original sender/channel
    from_name: str | None = None               # when the sender hides their account
    date: str | None = None
    channel_post_id: int | None = None
    post_author: str | None = None
    saved_from_peer_id: int | None = None
    saved_from_msg_id: int | None = None
    imported: bool = False

class ReplyHeader(Model):
    message_id: int | None = None
    peer_id: int | None = None                 # set when replying across chats
    top_message_id: int | None = None          # forum topic / comment thread root
    forum_topic: bool = False
    quote_text: str | None = None
    quote_entities: list[MessageEntity] = []
    quote_offset: int | None = None
    story_peer_id: int | None = None
    story_id: int | None = None
    todo_item_id: int | None = None

class ServiceAction(Model):
    """A service message is not 'a message with empty text'. It is an event."""
    type: str                                  # "chat_add_user" | "pin_message" | "channel_migrate_from" | …
    tl_type: str                               # "MessageActionChatAddUser"
    user_ids: list[int] = []
    title: str | None = None
    photo: Photo | None = None
    duration: int | None = None
    call_id: int | None = None
    ttl_seconds: int | None = None
    boosts: int | None = None
    stars: int | None = None
    payload: dict = {}                         # remaining action-specific fields, JSON-safe

class Message(Model):
    id: int
    chat_id: int                               # marked
    date: str                                  # RFC-3339 UTC
    date_unix: int
    text: str = ""                             # "" for media-only and service messages
    out: bool = False
    kind: Literal["message", "service"] = "message"
    # --- who ---
    sender_id: int | None = None               # None for anonymous / channel posts without from_id
    sender: Peer | None = None                 # present when the op asked for it (--sender)
    post_author: str | None = None
    via_bot_id: int | None = None
    from_rank: str | None = None               # admin rank shown next to the name
    send_as_id: int | None = None
    # --- what ---
    entities: list[MessageEntity] = []
    media: MediaSummary | None = None
    reply_markup: ReplyMarkup | None = None
    reactions: ReactionSummary | None = None   # ALWAYS present when the message has any
    forward: Forward | None = None
    reply_to: ReplyHeader | None = None
    action: ServiceAction | None = None        # set iff kind == "service"
    grouped_id: int | None = None              # album key
    # --- state ---
    edit_date: str | None = None
    pinned: bool = False
    silent: bool = False
    noforwards: bool = False
    mentioned: bool = False
    media_unread: bool = False
    scheduled: bool = False
    ttl_period: int | None = None
    effect_id: int | None = None
    views: int | None = None
    forwards: int | None = None
    replies_count: int | None = None
    edit_hide: bool = False
    restriction_reason: list[str] = []
    link: str | None = None                    # t.me permalink when the chat is public
```

**Why this shape.** Everything an agent asks about a message without a second RPC is a field: is it a sticker or a voice note (`media.kind`), did we already react (`reactions.mine`), is it a service event (`kind`/`action`), did it come from a topic (`reply_to.top_message_id`), can it be forwarded (`noforwards`). v1 exposed most of these as `media_*` prefixed loose keys assembled ad hoc in three places; here it is one struct produced by one function (`ops/_serialize.py::message_to_model`).

### 3.4 Dialog, Draft, Folder

```python
# tlgr/models/dialog.py
class NotifySettings(Model):
    muted: bool = False
    mute_until: str | None = None              # RFC-3339; null == not muted or forever
    mute_until_unix: int | None = None
    silent: bool | None = None
    show_previews: bool | None = None
    sound: str | None = None                   # "default" | "none" | "<ringtone id>"
    stories_muted: bool | None = None

class Dialog(Model):
    chat: Peer                                 # the peer itself (id/kind/title/username)
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    unread_mark: bool = False                  # manual "mark as unread"
    read_inbox_max_id: int = 0
    read_outbox_max_id: int = 0                # highest OUR message the other side has read
    top_message_id: int | None = None
    pinned: bool = False
    folder_id: int = 0                         # 0 main, 1 archive
    archived: bool = False
    notify: NotifySettings | None = None
    draft: "Draft | None" = None
    ttl_period: int | None = None
    view_forum_as_messages: bool | None = None
    last_message: Message | None = None        # trimmed: text capped, media/reactions/service kept

class Draft(Model):
    chat_id: int
    chat: Peer | None = None
    text: str = ""
    entities: list[MessageEntity] = []
    reply_to_msg_id: int | None = None
    top_msg_id: int | None = None
    no_webpage: bool = False
    effect_id: int | None = None
    date: str | None = None
    empty: bool = False

class Folder(Model):
    id: int
    title: str
    emoticon: str | None = None
    include_peers: list[int] = []
    exclude_peers: list[int] = []
    pinned_peers: list[int] = []
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False
    is_chatlist: bool = False
    has_my_invites: bool = False
```

### 3.5 Page, cursors

```python
# tlgr/models/page.py
from typing import Generic, TypeVar
T = TypeVar("T")

class Page(Model, Generic[T]):
    items: list[T]
    has_more: bool = False
    next_cursor: str | None = None
    total: int | None = None                   # server count when it gives one, else null
```

Cursor kinds and the state each carries (`core/pagination.py`):

| Kind | Used by | State |
|---|---|---|
| `HISTORY` | `messages.getHistory`, `getReplies`, `getScheduledHistory`, `getSavedHistory` | `offset_id`, `offset_date`, `add_offset` |
| `SEARCH` | `messages.search` | `offset_id`, `add_offset`, `filter`, `min_date`, `max_date` |
| `RATE` | `messages.searchGlobal` | `offset_rate`, `offset_peer`, `offset_id` |
| `DIALOGS` | `messages.getDialogs`, `getSavedDialogs` | `offset_date`, `offset_id`, `offset_peer`, `folder_id` |
| `PARTICIPANTS` | `channels.getParticipants`, blocked, photos, importers | `offset`, `filter`, plus `next_offset` for string-offset endpoints |
| `LOCAL` | server-side slice of a materialised list (contacts, drafts, folders) | `offset`, `snapshot_hash` |

Wire form: `base64url(msgspec.json.encode(payload)) + "." + base64url(hmac_sha256(key, payload)[:16])`

```json
{"v":1,"op":"message.list","kind":"HISTORY","acct":"a1b2c3d4","st":{"offset_id":1042},"exp":1788350000}
```

Server-side validation rejects: wrong `v`; `op` different from the op being called; `acct` fingerprint (first 8 hex of `sha256(alias)`) different from the resolved account; expired `exp` (default 1 h for `LOCAL`, 24 h otherwise); bad HMAC. The key lives in `~/.tlgr/cursor.key` (0600, generated once). The HMAC is integrity, not secrecy — it exists so a truncated or hand-edited cursor produces `USAGE: invalid cursor` instead of silently paging from message 0.

### 3.6 Error

```python
# tlgr/models/error.py
class ErrorBody(Model):
    code: str                # "RATE_LIMITED" — the stable machine name (§7)
    message: str             # human sentence, safe to print, never contains secrets
    exit_code: int           # 0–13; the CLI exits with exactly this
    retryable: bool = False
    wait_seconds: int | None = None      # RATE_LIMITED only
    field: str | None = None             # USAGE only: which request field
    hint: str | None = None              # "Run: tlgr account add <phone>"
    rpc: dict | None = None              # {"code": 400, "message": "CHAT_ADMIN_REQUIRED", "method": "channels.editBanned"}
    account: str | None = None
    request_id: str | None = None
```

### 3.7 Event envelope

```python
# tlgr/models/event.py
class EventEnvelope(Model):
    seq: int                 # per-account, monotonic, persisted; the cursor for --since
    ts: str                  # RFC-3339 UTC, when the daemon normalised it
    account: str
    type: str                # taxonomy in docs/design/EVENTS.md
    payload: dict            # type-specific; message events carry a full Message model
    chat_id: int | None = None      # denormalised for cheap filtering
    sender_id: int | None = None
    self_origin: bool = False       # true when this event echoes an action tlgr itself performed
```

> **Placeholder — event taxonomy.** The `type` vocabulary, the payload struct per type, and the mapping from Telethon events / raw `Update*` constructors to those types are owned by the updates design group and specified in **`docs/design/EVENTS.md`**. This architecture fixes only the envelope, the delivery guarantees (§6.5), and the requirement that every type name be a lowercase `snake_case` noun-verb (`message_new`, `message_edited`, `reaction_changed`, `dialog_unread_changed`, `session_new_authorization`, …). The foundation PR ships `EVENTS.md` with the envelope, the ordering guarantees, and a starter set of nine types (`message_new`, `message_edited`, `message_deleted`, `message_read`, `chat_action`, `user_status`, `reaction_changed`, `draft_changed`, `daemon_health`) so the bus is testable; the full taxonomy lands in PR-4.

### 3.8 JSON examples

`tlgr --json message get -- -1001234567890 1042`

```json
{
  "ok": true,
  "op": "message.get",
  "account": "work",
  "result": {
    "id": 1042,
    "chat_id": -1001234567890,
    "date": "2026-09-02T09:14:07Z",
    "date_unix": 1788340447,
    "text": "ping — did the deploy land?",
    "out": false,
    "kind": "message",
    "sender_id": 777123,
    "sender": {"id": 777123, "raw_id": 777123, "kind": "user", "title": "Sara N", "username": "saran"},
    "entities": [{"type": "code", "offset": 5, "length": 4}],
    "reactions": {"counts": {"👍": 2, "custom:5451234567890": 1}, "mine": ["👍"], "total": 3},
    "reply_to": {"message_id": 1039, "top_message_id": 12, "forum_topic": true},
    "link": "https://t.me/c/1234567890/1042"
  },
  "meta": {"request_id": "01J9Z7…", "elapsed_ms": 84, "flood_wait_slept": 0, "warnings": []}
}
```

`tlgr --json message list @channel -n 2`

```json
{
  "ok": true, "op": "message.list", "account": "work",
  "result": [
    {"id": 88, "chat_id": -1001111, "date": "2026-09-02T08:00:00Z", "date_unix": 1788336000,
     "text": "", "kind": "message", "sender_id": -1001111,
     "media": {"kind": "voice", "tl_type": "MessageMediaDocument", "mime_type": "audio/ogg",
               "duration": 17, "waveform": true, "size": 41233, "dc_id": 4}},
    {"id": 87, "chat_id": -1001111, "date": "2026-09-02T07:58:12Z", "date_unix": 1788335892,
     "text": "", "kind": "service",
     "action": {"type": "chat_add_user", "tl_type": "MessageActionChatAddUser", "user_ids": [777123]}}
  ],
  "page": {"has_more": true, "next_cursor": "eyJ2IjoxLCJvcCI6…​.qKf3", "total": 4120},
  "meta": {"request_id": "01J9Z8…", "elapsed_ms": 212, "flood_wait_slept": 0, "warnings": []}
}
```

Error:

```json
{
  "ok": false, "op": "message.send", "account": "work",
  "error": {
    "code": "RATE_LIMITED", "message": "A wait of 42 seconds is required", "exit_code": 7,
    "retryable": true, "wait_seconds": 42,
    "rpc": {"code": 420, "message": "FLOOD_WAIT_42", "method": "messages.sendMessage"},
    "hint": "Retry after 42s, or raise --flood-wait-max to let the daemon sleep it off.",
    "request_id": "01J9Z9…"
  }
}
```

Event line (NDJSON from `GET /v1/events`):

```json
{"seq":91824,"ts":"2026-09-02T09:14:07Z","account":"work","type":"message_new","chat_id":-1001234567890,"sender_id":777123,"self_origin":false,"payload":{"message":{"id":1042,"...":"…"}}}
```

---

## 4. The operation registry

### 4.1 `OperationSpec`

```python
# tlgr/ops/_spec.py
from __future__ import annotations
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias
import msgspec

class PageKind(str, Enum):
    HISTORY = "HISTORY"; SEARCH = "SEARCH"; RATE = "RATE"
    DIALOGS = "DIALOGS"; PARTICIPANTS = "PARTICIPANTS"; LOCAL = "LOCAL"

class Surface(str, Enum):
    DAEMON = "daemon"      # runs in the daemon against a Telethon client (the default)
    LOCAL  = "local"       # runs in the CLI process: config, schema, exit-codes, parity
    EITHER = "either"      # local fast path + daemon fallback (e.g. `account list`)

Impl: TypeAlias = Callable[["OpContext", msgspec.Struct], Awaitable[Any]]

@dataclass(frozen=True, slots=True)
class OperationSpec:
    # ---- identity ----
    id: str                                   # "message.send" | "chat.member.ban"  (group[.sub].verb)
    request: type[msgspec.Struct]
    response: type                            # a Model, list[Model], Page[Model], or None
    impl: Impl
    summary: str                              # one line, imperative, <= 72 chars
    description: str = ""                     # markdown paragraph(s) for --help and docs
    aliases: tuple[str, ...] = ()             # ("send", "msg.send") — canonicalised before policy
    # ---- behaviour flags ----
    mutating: bool = False                    # gates --dry-run; default-deny in restrictive policies
    destructive: bool = False                 # additionally requires --yes off a TTY
    paginated: PageKind | None = None         # adds --limit/--cursor/--all; response is Page[T]
    stream: bool = False                      # NDJSON response (watch, --all, progress)
    needs_account: bool = True                # False for config/schema/exit-codes
    needs_auth: bool = True                   # False for pre-auth ops (send-code, verify, qr)
    surface: Surface = Surface.DAEMON
    idempotent: bool = False                  # safe for the transport to retry once on a broken socket
    # ---- policy / limits ----
    timeout_s: int = 120                      # daemon-side asyncio.timeout
    rate_class: str = "read"                  # "read" | "send" | "resolve" | "bulk" | "file" (§6.4)
    min_interval_s: float = 0.0               # extra per-op spacing on top of the account bucket
    # ---- presentation ----
    columns: tuple[str, ...] = ()             # default human/plain projection; dot paths allowed
    headers: tuple[str, ...] = ()             # column titles when they differ from the paths
    empty_exit: int = 0                       # 0 or EXIT_EMPTY(3); applied by the dispatcher
    example: dict | None = None               # validated against `response` in tests; used in docs
    example_args: str = ""                    # the command line that produces `example`
    # ---- parity ----
    covers: tuple[str, ...] = ()
    covers_partial: tuple[str, ...] = ()
    coverage_note: str = ""
    # ---- migration ----
    legacy_paths: tuple[str, ...] = ()        # v1 CLI paths this op replaces (kept as aliases)
    since: str = "2.0"
    deprecated: str = ""                      # non-empty ⇒ hidden from --help, warns on stderr
    tags: frozenset[str] = frozenset()        # free-form: {"p0", "agent-safe", "needs-premium"}
```

Notes on the fields that carry weight:

* **`id` is the only name used for policy.** Aliases are canonicalised to it before the allowlist is checked — in the CLI *and* in the daemon. This is SEC-04's fix: today `--enable-commands message` allows `message send` but not `send` or `msg send`.
* **`mutating` is what makes `--dry-run` uniform.** The dispatcher short-circuits before `impl` for every mutating op. v1 honoured `--dry-run` in 9 commands and ignored it in 12 (COR-17); here it cannot be forgotten.
* **`rate_class` + `min_interval_s`** feed the per-account limiter (§6.4) so "sending" and "reading" are paced differently without every impl knowing about it.
* **`empty_exit`** settles COR-36 centrally: lists exit 0 with `count: 0`; point lookups and harvests that found nothing (`chat posters`, `user dialog-status` when unresolvable) declare `empty_exit=EXIT_EMPTY`.
* **`example` + `example_args`** are the schema example, the docs example and a test fixture at once. A test decodes `example` into `response`; a second test asserts `example_args` parses into a valid request.

### 4.2 Request Struct → Click mapping

Fields are declared with `Annotated[..., msgspec.Meta(...)]`; the CLI generator reads them through `msgspec.inspect.type_info()`, so the same metadata drives Click, the JSON Schema and the docs. The vocabulary lives in `ops/_params.py`:

```python
# tlgr/ops/_params.py
def arg(pos: int, *, metavar: str = "", required: bool = True, variadic: bool = False,
        help: str = "") -> msgspec.Meta: ...
def opt(*flags: str, help: str = "", metavar: str = "", envvar: str = "",
        hidden: bool = False, secret: bool = False, count: bool = False) -> msgspec.Meta: ...
def choice(*values: str, help: str = "") -> msgspec.Meta: ...
```

Both return `msgspec.Meta(description=help, extra={"cli": {...}})`.

**Mapping rules (exhaustive):**

| Request field | Click parameter |
|---|---|
| `Annotated[X, arg(0)]` | positional `click.Argument`, order = `pos` |
| `Annotated[X, arg(2, variadic=True)]` on `tuple[int, ...]`/`list[int]` | `nargs=-1` positional |
| everything else | `click.Option`, flag name = `--` + `field_name.replace("_","-")` |
| `opt("-n", "--limit")` | explicit flags override the derived name; first short flag becomes the short option |
| `bool` with default `False` | `is_flag=True` |
| `bool` with default `True` | paired `--x/--no-x` (`secondary_opts`) |
| `bool | None` (tri-state) | paired `--x/--no-x`, default `None` = leave alone |
| `Unset[str]` | option whose absence sends nothing (`set`/`edit` semantics) |
| `int`, `float`, `str` | `click.INT`, `click.FLOAT`, `click.STRING` |
| `Literal["a","b"]` or `choice(...)` | `click.Choice`, shown in `--help` and in the schema `enum` |
| `list[T]` / `tuple[T, ...]` (non-positional) | `multiple=True`, repeatable (`--file a --file b`) |
| `PeerRef` | `PEER` param type (§4.3) |
| `UserRef` | `USER` param type (rejects channel ids with a targeted message) |
| `MsgRef` | `MSGREF`: an int, or a `t.me/c/…/…` link that expands into `(chat, id)` |
| `Duration` | `DURATION`: `30s 5m 2h 7d forever` → seconds or `None` |
| `datetime` | `DATETIME`: RFC-3339, `YYYY-MM-DD`, or a relative `-2h`/`+7d`; always stored UTC |
| `Path` with `arg(...)` / `opt(..., metavar="PATH")` | `click.Path`; `-` means stdin/stdout |
| `SecretStr` (`opt(secret=True)`) | **no value flag.** Generates `--x-env VAR`, `--x-stdin`, `--x-file PATH` only |
| `bytes` | base64 on the wire; `--x-b64` / `--x-file` on the CLI |
| `Meta(ge=, le=, min_length=, pattern=)` | validated by msgspec at both ends; surfaced in `--help` and the schema |
| field named `account` | never declared: the global `-a` supplies it |
| `default=UNSET` | field omitted from the JSON body entirely when not given |

**Help text** comes from `Meta(description=...)`; `--help` for the command comes from `summary` + `description`; the epilog is `example_args` + a trimmed `example`.

**Implementation notes — verified against msgspec 0.21.1** (these are the sharp edges the generator must respect):

* `msgspec.inspect.type_info(Struct).fields[i].type` is a `Metadata` node **only when the field is `Annotated`**; its `.extra` carries our `{"cli": {...}}` dict and `.type` is the real type node. Read constraints (`ge`, `le`, `pattern`, `min_length`) off the **inner** node, not off `Metadata`.
* **Exactly one `Meta` per field.** Stacking `Annotated[int, opt(...), Meta(ge=1)]` works for validation and for the JSON Schema, but `type_info` surfaces only the first `Meta`'s `extra`/`description`, so the second one's `description` is invisible to the generator. `arg()`/`opt()` therefore accept the constraint kwargs and emit a single merged `Meta`; a registry lint rejects a field with two `Meta` annotations.
* Defaults come from `field.default` **or** `field.default_factory` (`[]`/`{}` become a factory). `field.default is msgspec.NODEFAULT and field.required is False` is the signature of an `UNSET` field.
* `msgspec.ValidationError` messages end in `- at $.chat.kind`; the dispatcher parses that suffix into `error.field` (`chat.kind`), which is what makes a `USAGE` error actionable.
* `Page[T]` is a generic Struct: `msgspec.json.decode(body, type=Page[Message])` and `msgspec.json.schema_components([...])` both work, and the schema component is named `Page_Message_`.
* Struct config (`kw_only`, `omit_defaults`, `forbid_unknown_fields`) is inherited by subclasses, so `Model`/`Request` bases are enough.

**Envvars** are attached with Click's `envvar=` (evaluated per invocation, not at import — fixing MNT-05).

**Paginated ops** get `--limit/-n INT`, `--cursor TOKEN`, `--all` injected by the generator; `--since/--until` are injected when the op's `PageKind` supports date offsets (`HISTORY`, `SEARCH`, `DIALOGS`).

### 4.3 Custom Click parameter types

`tlgr/cli/params.py` — each is a `click.ParamType` whose `convert()` produces the model value **and** whose failure is a `USAGE` error with `field` set:

* **`PEER`** — accepts `@username`, `-100…`/`-…`/positive int, `t.me/<name>`, `t.me/c/<id>/<msg>`, `t.me/+<hash>`, `tg://resolve?domain=`, `tg://join?invite=`, `me`, `saved`, `+<phone>`. Produces `PeerRef`. **Parsing only** — no network, no resolution; that happens in the daemon (§6.6).
* **`USER`** — `PEER` minus channel/group forms; a `-100…` id gets "that is a channel id; this argument wants a user".
* **`MSGREF`** — an int, or a message link. When a link is given for a command that also takes `<chat>`, the CLI fills both and errors if they disagree.
* **`DURATION`** — `\d+(s|m|h|d|w)` or `forever`; `0` is legal and means "now/immediately".
* **`DATETIME`** — RFC-3339 (`Z` or offset), `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, or relative (`-90m`, `+3d`). Naive values are interpreted in the **local** zone and converted to UTC (COR-23's lesson), and the resolved UTC value is echoed in `meta.warnings` when it was ambiguous.
* **`SECRET`** — never a value; `--x-env`, `--x-stdin`, `--x-file`. Reading order: file → stdin → env. Never logged, never in `meta`, never in a dry-run echo.
* **`PARSE_MODE`** — `md|html|none`, default from config (`[defaults] parse_mode`, itself defaulting to `none`; see §9 and COR-21).

### 4.4 Global flags (attached to every generated command)

`--json`, `--plain`, `-a/--account`, `--results-only`, `--select`, `--dry-run/-n`, `--yes/-y`, `--no-input`, `--flood-wait-max`, `--timeout`, `-v/--verbose`, `--no-daemon-restart`, `--enable-commands`.

They are attached **to each command**, not only to the root group, so `tlgr chat list --json` works (UX-01: today that is exit 2 "No such option"). They are also accepted on the root group; a shared `CliState` object merges the two, last-wins, with the command-level value taking precedence. `-n` is `--limit` on paginated commands and `--dry-run` everywhere else — resolved per command by the generator, and the ambiguity (COR-45) disappears because the generator refuses to attach `--dry-run/-n` to a paginated command (it gets `--dry-run` only).

### 4.5 Registry API

```python
# tlgr/registry.py
REGISTRY: dict[str, OperationSpec]           # id -> spec
ALIASES:  dict[str, str]                     # alias/legacy path -> id

def get(op_id_or_alias: str) -> OperationSpec: ...
def canonical(name: str) -> str: ...          # "msg.send" -> "message.send"; raises USAGE if unknown
def by_group(group: str) -> list[OperationSpec]: ...
def groups() -> list[str]: ...
def lint() -> list[str]: ...                  # returns problems; called at import, raises if non-empty
```

**Lints (run at import time, so a broken spec cannot ship):**

| # | Lint |
|---|---|
| L1 | ids unique; ids match `^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){1,2}$`; the last segment is in the STYLE.md verb vocabulary |
| L2 | aliases and `legacy_paths` unique across the registry and disjoint from ids |
| L3 | `request`/`response` are msgspec Structs (or `Page[Struct]`/`list[Struct]`/`None`) |
| L4 | positional indices are contiguous from 0; at most one variadic and it is last |
| L5 | no request field is named `account`, `json`, `plain`, `cursor`, `limit`, `all`, `dry_run` (reserved for globals/pagination) |
| L6 | `paginated` ⇒ `response` is `Page[...]`; `stream` ⇒ the impl is an async generator |
| L7 | `mutating=False` ⇒ the impl body contains no call to a known mutating Telethon method (AST check, best-effort, waivable via `tags={"mutating-checked"}`) |
| L8 | `destructive` ⇒ `mutating` |
| L9 | every op has a non-empty `summary`, `example` and `example_args` |
| L10 | every `covers`/`covers_partial` id exists in the catalog index (L-COV-1) |
| L11 | `columns` paths exist in `response` (walked through the Struct types) |
| L12 | `timeout_s` ≤ 900 and ≥ 5; `rate_class` is known |
| L13 | every op declares at least one `covers` id **or** `tags={"infrastructure"}` (daemon/config/schema ops) |
| L14 | no request field carries two `msgspec.Meta` annotations (only the first one's `extra`/`description` reaches the generator — §4.2) |
| L15 | `Unset[...]` fields include `| None` when the op documents a "clear" flag, and are not positional |

### 4.6 Generated artefacts

From `REGISTRY`, deterministically:

1. **The Click tree** — `cli/gen.py::build_click_tree()` produces groups (`message`), sub-groups (`chat member`), commands, aliases, shortcuts, `--help`, and shell completion. One factory; zero per-command modules.
2. **The daemon dispatch table** — `daemon/dispatch.py` looks the op up by id; there is no route per operation.
3. **`tlgr schema`** — JSON Schema for every request and response (`msgspec.json.schema_components`), plus `example`, flags, columns, exit codes, aliases and catalog coverage. Emitted as one document with `$defs` so the models are defined once. The click tree it also carries comes from `cli/introspect.py` and is passed *in*: `tlgr/schema.py` and `ops/` must not import `cli/`.
4. **`docs/reference/<group>.md`** — synopsis, argument table, flag table, response schema summary, example invocation + example JSON, covered catalog ids. `make docs` regenerates; CI fails on a diff (this is PKG-03's fix).
5. **Contract tests** — `tests/test_registry_contract.py` parametrised over `REGISTRY` (§11.3).
6. **`docs/reference/PARITY.md`** — §1.3.
7. **Shell completions** — static bash/zsh/fish scripts written by `make completions` (PKG-05).

### 4.7 Worked example — `message.send`

```python
# tlgr/ops/message.py  (excerpt)
from typing import Annotated
import msgspec
from tlgr.models import Message, PeerRef, Page
from tlgr.models.base import Request, Unset, UNSET
from tlgr.ops._params import arg, opt, choice
from tlgr.ops._spec import OperationSpec, PageKind, Surface
from tlgr.core.errors import EXIT_EMPTY

class SendReq(Request):
    chat:  Annotated[PeerRef, arg(0, metavar="CHAT", help="Target chat, user, or link.")]
    text:  Annotated[str, arg(1, metavar="TEXT", required=False,
                              help="Message text. Use '-' to read from stdin.")] = ""
    file:  Annotated[list[str], opt("--file", metavar="PATH",
                                    help="Attach a file. Repeat for an album (max 10).")] = []
    caption: Annotated[str | None, opt("--caption", help="Caption for the attached file.")] = None
    reply_to: Annotated[int | None, opt("--reply-to", metavar="ID")] = None
    quote:  Annotated[str | None, opt("--quote", help="Quote this part of the replied message.")] = None
    topic:  Annotated[int | None, opt("--topic", metavar="ID", help="Forum topic id.")] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    schedule: Annotated[str | None, opt("--schedule", metavar="TS|online")] = None
    send_as: Annotated[PeerRef | None, opt("--send-as", metavar="PEER")] = None
    parse:  Annotated[str, choice("md", "html", "none", help="Text formatting.")] = "none"
    entities: Annotated[str | None, opt("--entities", metavar="JSON")] = None
    no_preview: Annotated[bool, opt("--no-preview")] = False
    spoiler: Annotated[bool, opt("--spoiler", help="Send media as a spoiler.")] = False
    ttl:    Annotated[int | None, opt("--ttl", metavar="SECONDS")] = None
    noforwards: Annotated[bool, opt("--noforwards", "--protect")] = False
    effect: Annotated[int | None, opt("--effect", metavar="ID")] = None
    paid_stars: Annotated[int | None, opt("--paid-stars", metavar="N")] = None
    typing: Annotated[float, opt("--typing", metavar="SECONDS")] = 0.0
    typing_auto: Annotated[bool, opt("--typing-auto")] = False
    clear_draft: Annotated[bool, opt("--clear-draft/--keep-draft")] = True

async def send(ctx: OpContext, req: SendReq) -> Message: ...

SPEC_SEND = OperationSpec(
    id="message.send",
    aliases=("send", "msg.send"),
    legacy_paths=("message send", "send"),
    summary="Send a message to a chat",
    description="Sends text, a file, or an album. …",
    request=SendReq, response=Message, impl=send,
    mutating=True, rate_class="send", timeout_s=180,
    columns=("id", "chat_id", "date", "text"),
    example_args='message send @alice "on my way"',
    example={"id": 12345, "chat_id": 777123, "date": "2026-09-02T09:14:07Z",
             "date_unix": 1788340447, "text": "on my way", "out": True, "kind": "message"},
    covers=("messages-core.send-text", "messages-core.send-reply", "messages-core.send-silent",
            "messages-core.send-scheduled", "messages-core.send-as-peer", "messages-core.send-album",
            "messages-core.send-with-entities", "messages-core.send-spoiler-media",
            "messages-core.send-ttl", "messages-core.send-noforwards", "messages-core.send-effect",
            "messages-core.send-paid"),
    covers_partial=("messages-core.send-to-topic",),
    coverage_note="Topic sends work; topic *creation* is chat topic create (PR-7).",
)
```

Generated command:

```
Usage: tlgr message send [OPTIONS] CHAT [TEXT]

  Send a message to a chat.
  …
Options:
  --file PATH               Attach a file. Repeat for an album (max 10).  [repeatable]
  --caption TEXT            Caption for the attached file.
  --reply-to ID
  --quote TEXT              Quote this part of the replied message.
  …
  --parse [md|html|none]    Text formatting.  [default: none]
  -a, --account ALIAS       Account alias.  [env: TLGR_ACCOUNT]
  --json / --plain          Output format.
  -n, --dry-run             Show what would happen without doing it.
  …
Example:
  tlgr message send @alice "on my way"
  → {"id": 12345, "chat_id": 777123, "date": "2026-09-02T09:14:07Z", …}
```

---

## 5. Wire protocol (v2)

### 5.1 Transport

* **Socket:** `~/.tlgr/daemon.sock`, `AF_UNIX`, `SOCK_STREAM`, mode **0600**, owned by the invoking uid.
* **Client:** stdlib `http.client.HTTPConnection` subclassed to connect to the path. No hand-rolled request lines, no hand-rolled chunk decoding (COR-04, COR-31, COR-32 all die here).

```python
# tlgr/transport/client.py  (the whole connection story)
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path
    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s
```

* **Server:** aiohttp `UnixSite`, `AppRunner(app, access_log=None)` (SEC-05), `chmod(0o600)` on the socket immediately after `site.start()`.
* **Encoding:** UTF-8 JSON bodies for everything. Query strings are used only by `GET /v1/events` and `GET /v1/status`, and are built with `urllib.parse.urlencode`.
* **Headers on every request:** `X-Tlgr-Client: tlgr/2.0.0`, `X-Tlgr-Protocol: 2`, `X-Request-Id: <ULID>`, `Content-Type: application/json`, optional `X-Tlgr-Token`.

> **Verified.** This exact recipe was exercised against a live aiohttp `UnixSite`: a POST body containing `سلام #12 a+b` and `@fish&chips` round-trips byte-identically; `GET /v1/status?q=سلام%20%26%20%231` arrives intact via `urlencode`; NDJSON frames read cleanly with `HTTPResponse.readline()` over chunked transfer-encoding; the socket is `srw-------`. Every one of COR-04, COR-31 and COR-32 is a direct consequence of not doing this.

### 5.2 `POST /v1/op`

Request body:

```json
{
  "op": "message.send",
  "account": "work",
  "request": {"chat": {"raw": "@alice", "kind": "username", "value": "alice"}, "text": "on my way"},
  "dry_run": false,
  "flood_wait_max": 120,
  "request_id": "01J9Z7QW0000000000000000",
  "client_version": "2.0.0",
  "protocol": 2,
  "stream": false,
  "limit": null,
  "cursor": null,
  "all": false
}
```

* `op` may be an alias; the daemon canonicalises before policy.
* `account` is **mandatory** for `needs_account` ops. Empty/absent ⇒ `ACCOUNT_REQUIRED` (400, exit 2). The daemon never chooses (COR-02).
* `request` is decoded with `msgspec.json.decode(..., type=spec.request)`; a `ValidationError` becomes `USAGE` (400, exit 2) with `field` taken from the msgspec error path.
* `limit`/`cursor`/`all` are transport-level, not part of the request struct, so pagination is uniform.

Success:

```json
{
  "ok": true,
  "op": "message.send",
  "account": "work",
  "result": { … the response model … },
  "page": {"has_more": true, "next_cursor": "…", "total": 4120},
  "meta": {
    "request_id": "01J9Z7QW0000000000000000",
    "elapsed_ms": 84,
    "flood_wait_slept": 0,
    "warnings": ["--schedule interpreted as 2026-09-03T06:00:00Z (local +03:30)"],
    "already": false,
    "daemon_version": "2.0.0",
    "protocol": 2
  }
}
```

* `page` is present iff the op is paginated. `result` is then the bare `items` list — one shape for JSON consumers, matching STYLE.md's `Page[T]` when `--results-only` is not used (see §9 for exactly what each mode prints).
* `meta.already: true` is how `NOT_MODIFIED` is reported: `ok: true`, exit 0, no error (fixed decision 8).
* `meta.warnings` are non-fatal advisories: ambiguous datetimes, truncated results, `min` entities used, partial harvests.

Failure: HTTP status from the table in §7, body:

```json
{"ok": false, "op": "message.send", "account": "work", "error": { … ErrorBody … }}
```

### 5.3 Streaming (NDJSON)

Used for `stream=true` ops (`watch`, `media download --progress`) and for `all=true` walks. `Content-Type: application/x-ndjson`, `Transfer-Encoding: chunked`, one JSON object per line, flushed per line.

```
{"type":"meta","op":"message.list","account":"work","request_id":"01J…","protocol":2}
{"type":"item","seq":1,"data":{ … Message … }}
{"type":"item","seq":2,"data":{ … }}
{"type":"progress","done":512000,"total":4194304,"rate_bps":1048576}
{"type":"page","has_more":true,"next_cursor":"…","fetched":100,"elapsed_ms":812}
{"type":"heartbeat","ts":"2026-09-02T09:14:22Z"}
{"type":"end","ok":true,"count":4120,"elapsed_ms":41230,"flood_wait_slept":6}
{"type":"end","ok":false,"error":{ … ErrorBody … }}
```

Rules: exactly one `meta` first and exactly one `end` last; the CLI exits with `end.error.exit_code` (or 0); a stream that dies without `end` is `RETRYABLE` (exit 8) — never a silent success. `--all` walks **inside the daemon** through the account rate limiter, so a 10k-dialog enumeration is one request with backpressure instead of a client loop hammering the socket (ROB-01).

### 5.4 `GET /v1/events`

```
GET /v1/events?account=work&types=message_new,reaction_changed&since=91820&timeout=300&chats=-1001,-1002
```

* NDJSON, one `EventEnvelope` per line, `{"type":"heartbeat","ts":…}` every **15 s**.
* `since` replays from the per-account ring buffer (default 4,096 events, config `[daemon] event_buffer`); if `since` is older than the buffer the first line is `{"type":"gap","from":<oldest>,"requested":<since>,"lost":<n>}` and delivery continues — a consumer learns it missed events instead of silently skipping them.
* `seq` is persisted (`~/.tlgr/accounts/<alias>/events.state`, fsynced every 5 s and on shutdown) so `--since` survives daemon restarts.
* `timeout` bounds the connection (default 3600, max 86400); the server closes with `{"type":"end","reason":"timeout"}` and the client reconnects with the last `seq`.
* Multiple subscribers are independent; each has a bounded queue (1,024). A subscriber that cannot keep up gets `{"type":"lag","dropped":N}` and the oldest events are dropped for **that subscriber only** — never for the bus, never blocking the update loop (ROB-02).
* Open streams count as activity for idle-stop (COR-08).

### 5.5 `GET /v1/status`

```json
{
  "ok": true,
  "daemon": {"version": "2.0.0", "protocol": 2, "pid": 8123, "uptime_s": 4210,
             "ready": true, "started_at": "2026-09-02T08:00:00Z",
             "managed_by": "launchd", "idle_timeout_s": 0, "socket": "/Users/p/.tlgr/daemon.sock"},
  "accounts": [
    {"alias": "work", "state": "online", "user_id": 777, "username": "me",
     "connected_since": "2026-09-02T08:00:04Z", "last_update": "2026-09-02T09:14:07Z",
     "reconnects": 0, "catch_up_pending": false, "event_seq": 91824,
     "flood_until": null, "circuit": "closed", "in_flight": 0,
     "pts": 1204551, "qts": 0, "channels_tracked": 214, "resync_needed": []},
    {"alias": "personal", "state": "needs_login", "reason": "SESSION_REVOKED",
     "since": "2026-09-01T22:10:00Z"}
  ],
  "jobs": [{"name": "feed", "type": "gateway", "enabled": true, "running": true, "account": "work"}],
  "webhook": {"enabled": true, "queued": 0, "delivered": 8123, "failed": 2, "dead_letters": 0},
  "activity": {"in_flight": 0, "event_streams": 1, "transfers": 0, "last_request": "2026-09-02T09:14:07Z"}
}
```

`ready` is false between socket bind and the first successful account connect, so `tlgr daemon status` distinguishes "process alive" from "daemon works" (COR-37). `GET /v1/status` never requires an account and is exempt from the policy allowlist.

### 5.6 `POST /v1/admin/*`

| Endpoint | Body | Effect |
|---|---|---|
| `/v1/admin/stop` | `{"drain_s": 30}` | graceful shutdown (§6.12); returns before exiting |
| `/v1/admin/reload` | `{"what": ["config","jobs","webhook","policy"]}` | re-read from disk, apply, return a diff |
| `/v1/admin/resync` | `{"account":"work","scope":"all"\|"channel","channel_id":-100…}` | force `catch_up()` / channel difference / dialog rescan |
| `/v1/admin/logout` | `{"account":"work","destroy_key":true}` | `auth.logOut` + optional `DestroyAuthKey`, then release the session (SEC-03) |

All admin endpoints require the same peer-uid/token authentication and are additionally gated by the policy op ids `daemon.stop`, `daemon.reload`, `daemon.resync`, `account.logout`.

### 5.7 Version handshake

The transport caches the daemon's `(version, protocol)` in `~/.tlgr/daemon.state` (written by the daemon at bind, read by the CLI). On every call the CLI sends `X-Tlgr-Protocol`. Outcomes:

| Situation | Behaviour |
|---|---|
| protocols equal | proceed |
| daemon protocol < client protocol, or `/v1/op` returns 404 (a v1 daemon) | print `daemon is running an older protocol (1 < 2); restarting it` to stderr, `POST /v1/admin/stop` (or SIGTERM + wait), auto-start, retry once |
| daemon protocol > client protocol | error `DAEMON_VERSION_MISMATCH` (exit 11) with "upgrade the CLI or run `tlgr daemon stop`" — never silently kill a newer daemon |
| `--no-daemon-restart` | never restart; `DAEMON_VERSION_MISMATCH` (exit 11) |
| daemon not running | auto-start when `[daemon] auto_start` (default true), else `DAEMON_NOT_RUNNING` (exit 11) |

Restart is attempted at most once per CLI invocation, and never for a daemon managed by launchd/systemd (there the CLI asks the supervisor to restart and says so).

### 5.8 Auto-start, race-free (COR-14)

```
1. try to flock ~/.tlgr/daemon.lock  (LOCK_EX|LOCK_NB)
     acquired  → no daemon is running. Keep the lock, spawn the daemon, RELEASE the lock
                 only after the child reports readiness or the spawn fails.
     busy      → a daemon exists (or is starting). Skip to 3.
2. spawn: subprocess.Popen([sys.executable, "-m", "tlgr.daemon.main", "--base", BASE],
          start_new_session=True, stdout/stderr=DEVNULL)
3. poll GET /v1/status every 100 ms (capped backoff to 500 ms) for up to `[daemon] start_timeout`
   (default 30 s). Success requires HTTP 200 — not merely the socket file existing.
4. on timeout: read the last 20 lines of daemon.log into the error message; exit 11.
```

The daemon itself takes the same `flock` for its whole lifetime inside `main()` **before** any other work, so two daemons cannot coexist even if two CLIs spawn simultaneously. Nothing ever unlinks another process's socket or pid file; a stale socket is removed only while holding the lock. `PermissionError` on the pid file is never treated as "not running" (COR-14c).

---

## 6. Daemon internals

### 6.1 Process lifecycle

```
main()
 ├─ parse argv (--base, --foreground, --log-level)
 ├─ os.umask(0o077)                                   ← SEC-01, before anything creates a file
 ├─ load config.toml
 ├─ acquire flock(~/.tlgr/daemon.lock)                ← single instance; exit 0 with a clear
 │                                                      message if busy (COR-39)
 ├─ setup_logging()   (rotating file only; stderr only with --foreground)
 ├─ daemonize() unless --foreground  (double fork, setsid, close stdin, dup2 stdout/stderr → log)
 ├─ write pid; re-verify the lock is still ours after the fork
 ├─ audit permissions: ~/.tlgr 0700; accounts/**/session* 0600; fix or refuse to start
 ├─ bind the socket FIRST, chmod 0600, publish ready:false          ← ROB-07
 ├─ install signal handlers (SIGTERM/SIGINT → graceful, SIGHUP → reload)
 ├─ start EventBus, RateLimiterStore, WebhookSubscriber, JobRunner (idle)
 ├─ connect accounts concurrently (asyncio.gather) — failures do not block readiness
 ├─ publish ready:true; write ~/.tlgr/daemon.state {version, protocol, pid, socket}
 └─ await shutdown
```

Accounts connected at start = the union of: accounts referenced by enabled jobs, `[accounts] default`, the active alias, and every alias listed in `[daemon] preconnect`. Everything else connects on demand (§6.3). The connect list is an **ordered list**, never a `set` (COR-02).

### 6.2 `AccountSession` state machine

```
                 ┌──────────────┐
   create ──────▶│   starting   │  session file flock taken; client constructed
                 └──────┬───────┘
                        │ connect() ok, authorized
                        ▼
     ┌────────────▶┌──────────┐ ◀───── catch_up done
     │             │  online  │
     │             └────┬─────┘
     │   transport drop │      │ auth error (401/406 family)
     │                  ▼      ▼
     │            ┌──────────┐ ┌──────────────┐
     └────────────│ degraded │ │ needs_login  │ terminal until the operator re-auths
        backoff   └────┬─────┘ └──────────────┘
        reconnect      │ PEER_FLOOD / FROZEN_*
                       ▼
                 ┌──────────┐
                 │  frozen  │  circuit breaker open; reads allowed, sends refused
                 └──────────┘
                       │ operator reset / freeze_until_date passed
                       ▼   → degraded → online

   any state ── stop() ──▶ stopping ──▶ stopped   (state saved, flock released)
```

| State | `/v1/status` | Behaviour of `/v1/op` for that account |
|---|---|---|
| `starting` | `starting` | queued up to 10 s, then `RETRYABLE` (503, exit 8) |
| `online` | `online` | normal |
| `degraded` | `degraded` + `reconnect_in_s` | reads queued up to `min(op.timeout_s, 15 s)`; if still down, `RETRYABLE` (exit 8) with a hint. Never `ConnectionError("Cannot send requests while disconnected")` (checklist item 3) |
| `needs_login` | `needs_login` + `reason` | `SESSION_ERROR` (401, exit 4) immediately, with the re-login command in `hint` |
| `frozen` | `frozen` + `freeze_until` + `appeal_url` | mutating ops in `rate_class="send"` → `ACCOUNT_FROZEN`/`PEER_FLOOD` (403, exit 9); reads proceed |
| `stopping`/`stopped` | — | `RETRYABLE` |

The state is persisted into `AccountManager` (`~/.tlgr/accounts.json`, per alias: `state`, `reason`, `since`) so `tlgr account list` and `agent whoami` tell the truth even when the daemon is down.

**Client construction** (`core/identity.py` + `session.py`):

```python
TelegramClient(
    session=str(session_path), api_id=…, api_hash=…,
    catch_up=True,                      # replay what we missed while down       (checklist 1)
    sequential_updates=True,            # ordered dispatch into the bus
    raise_last_call_error=True,         # real RPC errors, not ValueError('unsuccessful 5 times')
    entity_cache_limit=int(cfg.limits.entity_cache),        # default 20_000
    connection_retries=None,            # our supervisor owns backoff            (checklist 3)
    retry_delay=1, request_retries=cfg.limits.request_retries,
    auto_reconnect=True, timeout=cfg.network.connect_timeout,
    flood_sleep_threshold=cfg.flood.sleep_threshold,
    device_model=identity.device_model,     # "MacBookPro18,3 (arm64)" or "<hostname> (x86_64)"
    system_version=identity.system_version, # "macOS 15.6" / "Debian 12 (6.1.0)"
    app_version=f"tlgr {VERSION} (Telethon {telethon.__version__})",
    lang_code=identity.lang_code, system_lang_code=identity.system_lang_code,
    proxy=cfg.network.proxy_tuple, use_ipv6=cfg.network.ipv6,
    connection=cfg.network.connection_class,
)
```

Identity strings are computed once, cached in `~/.tlgr/identity.json`, and **kept stable across restarts** so the entry in Settings → Devices does not churn. Never an official app's `api_id`.

**Session ownership.** Before constructing the client, `AccountSession` takes an exclusive `flock` on `~/.tlgr/accounts/<alias>/session.lock` and holds it for its lifetime. `tlgr account add/import` no longer opens session files at all — pre-auth runs in the daemon (§6.8) — so `AUTH_KEY_DUPLICATED` from tlgr racing itself becomes impossible. If the lock is busy, the op fails with `CONFIG_ERROR` (exit 10) naming the holding pid.

### 6.3 Supervisor and catch-up

One task per account:

```python
while not stopping:
    try:
        await client.connect()
        if not await client.is_user_authorized(): -> needs_login; return
        me = await client.get_me()
        await warm_entity_cache()          # one iter_dialogs pass: seeds access hashes AND
                                           # channel pts, without which catch_up skips channels
        await client.catch_up()            # checklist 1 & 2
        state = online; backoff.reset()
        await client.disconnected          # resolves on drop or fatal error
    except Exception as e:
        cls = classify_auth(e)             # core/errors.py
        if cls is FATAL_AUTH: state = needs_login(reason=e.__class__.__name__); persist; stop jobs; return
        state = degraded
        await asyncio.sleep(backoff.next()) # 1,2,4,8,16,32,60,60… ±20 % jitter, cap 60 s
```

Additional triggers for `catch_up()`:

* after **every** successful reconnect (Telethon's `_handle_auto_reconnect` only calls `get_me()`) — implemented by wrapping the client's reconnect callback in `core/telethon_compat.py`;
* on a **wall-clock jump** > 60 s (laptop sleep): a 30 s ticker comparing `time.time()` deltas to `time.monotonic()` deltas;
* on a `DifferenceTooLong` / `ChannelDifferenceTooLong` observation (below);
* every 15 min of complete silence, as a belt-and-braces backstop, and after `getDifference` failures with 2→64 s backoff (checklist 10);
* on `POST /v1/admin/resync`.

**`*TooLong` detection (checklist 9).** Telethon consumes differences internally and delivers nothing to handlers. `core/telethon_compat.py` installs a thin wrapper around `MessageBox.apply_difference` / `apply_channel_difference` (version-pinned, guarded by `hasattr` with a logged fallback to watching the `telethon._updates.messagebox` logger). On `differenceTooLong` the account is marked `resync_needed`; on `channelDifferenceTooLong` that channel id is. A resync task then re-reads the last N (config `[daemon] resync_depth`, default 50) messages of the affected dialogs and emits normal `message_new` events with `meta.resynced: true`, so gateway jobs and webhooks do not silently miss history. `resync_needed` is reported in `/v1/status`.

**Periodic persistence (checklist 19).** Every 60 s and on every shutdown path: `await client._save_states_and_entities(); client.session.save()` through `telethon_compat` (pinned private API, wrapped in try/except with a one-time warning if the attribute is gone). Plus a `session.save()` after each `--all` walk and after each job batch.

**Config freshness (checklist 12).** A `Raw(types=[UpdateConfig, UpdateDcOptions])` handler clears Telethon's cached `_config` and re-fetches `help.getConfig`; `help.getAppConfig(hash=…)` is refreshed at start and every 12 h. `upload_max_fileparts_*`, caption/message length limits, `small/large_queue_max_active_operations_count`, `freeze_*` and `authorization_autoconfirm_period` are read from it instead of being hard-coded, and surfaced in `/v1/status.accounts[].app_config`.

### 6.4 Rate limiter, flood memory, circuit breaker

`daemon/ratelimit.py`, one instance per account:

* **Token bucket per `rate_class`** — `read` (10/s burst 20), `resolve` (0.5/s burst 5 — `contacts.resolveUsername` floods at ~50 per short period), `send` (1/s burst 3, plus a configurable per-new-peer daily cap), `bulk` (2/s), `file` (governed by transfer slots instead, §6.7). Defaults are config keys; the values above are the shipped defaults for an unwarmed account.
* **Per-chat slow mode** — `channelFull.slowmode_next_send_date` is cached per chat; a send that would violate it is refused with `RATE_LIMITED` and `wait_seconds` **without** a network round trip.
* **Persisted flood memory** — every `FloodWaitError` / `SlowModeWaitError` / `FloodPremiumWaitError` / `TakeoutInitDelayError` writes `{account, method, peer_id, until_unix, seconds}` to `~/.tlgr/accounts/<alias>/flood.json` (0600, atomic replace). Loaded at start. A request whose `(method, peer)` deadline has not passed fails immediately with the remaining `wait_seconds` — Telethon's in-process `_flood_waited_requests` is lost on restart, so v1 re-hit every wait after a bounce.
* **Sleep policy** — a wait ≤ `min(request.flood_wait_max, remaining op timeout)` is slept inside the daemon and reported as `meta.flood_wait_slept`; anything longer returns `RATE_LIMITED` immediately. Per-request `flood_wait_max` is honoured by passing `flood_sleep_threshold` per call (COR-15, ROB-06).
* **Circuit breaker** — `PeerFloodError`, `FrozenMethodInvalidError`/`FROZEN_*`, or three consecutive `USER_PRIVACY_RESTRICTED`-class refusals within 60 s on new peers open the breaker: state `frozen`, all `rate_class="send"` ops refused with exit 9, jobs for that account paused, a `daemon_health` event emitted, and `help.getAppConfig` polled for `freeze_since_date`/`freeze_until_date`/`freeze_appeal_url`. Reset is manual (`tlgr account unfreeze --yes`) or automatic once `freeze_until_date` passes.
* **Idempotent sends (checklist 8)** — every outgoing message gets a `random_id` generated and journalled to `~/.tlgr/accounts/<alias>/outbox.jsonl` *before* the RPC, and cleared on a confirmed result. A retry after a lost `rpc_result` reuses the same `random_id`, so the server dedupes; `updateMessageID` reconciles the real id. `RANDOM_ID_DUPLICATE` is therefore a success, not an error.

### 6.5 Event bus

`daemon/events.py`:

```
Telethon handlers (NewMessage, MessageEdited, MessageDeleted, MessageRead, ChatAction,
UserUpdate, Album) + events.Raw(<everything else>)
        │  normalise → EventEnvelope  (models, not to_dict(); no datetime, no bytes)
        ▼
   assign seq (per account, monotonic, persisted)
        ▼
   ring buffer (per account, N=4096)  ──▶ subscribers:
        ├─ IPC event streams  (bounded queue 1024, lag reporting)
        ├─ webhook pusher     (bounded queue 2048, worker pool, HMAC, dead letter)
        └─ gateway jobs       (bounded queue 512 per job, per-chat ordering)
```

**Handlers never block the update loop.** With `sequential_updates=True` a slow subscriber would stall every account (ROB-02: v1's webhook could hold the loop for ~97 s). The Telethon handler does exactly three things: normalise, assign `seq`, `put_nowait` into each subscriber's queue (dropping the oldest and counting on overflow). All real work happens in a bounded worker pool. **Per-chat order is preserved** by hashing `chat_id` to a fixed worker lane (`workers = config [daemon] event_workers`, default 8); events for one chat always run on one lane, in order, while different chats run concurrently.

**Own actions are echoed.** Telethon marks results of our own requests `_self_outgoing` and does not dispatch them, so a message the daemon sends never fires `NewMessage`. Every mutating op therefore feeds its returned `Updates` through the same normaliser with `self_origin: true`. That is what makes `tlgr watch` show the account's own sends, and what lets a gateway job react to them.

**Webhook subscriber** (COR-07, SEC-06, SEC-08): payload is `{"event": EventEnvelope, "delivery_id": ULID}` encoded with `msgspec.json.encode` (never `to_dict()`); posted as `data=<bytes>` with `Content-Type: application/json`, `X-Tlgr-Delivery: <ulid>`, `X-Tlgr-Seq: <seq>`, `X-Tlgr-Signature: sha256=<hex hmac of the exact body>`. Retries with jittered exponential backoff; exhausted deliveries go to `~/.tlgr/dead_letter.jsonl` at **0600** with size-based rotation (`dead_letter.jsonl.1`, keep 3, cap 16 MB) and a `webhook dead-letter list|replay|purge` CLI. A serialisation failure is logged as a **bug** at ERROR with the type name — never counted as a delivery failure.

### 6.6 Entity resolution service

`core/peers.py`, one resolver per account (access hashes are per account — never share a cache across accounts).

```python
async def resolve(ref: PeerRef, *, allow_network=True, want: Literal["peer","user","channel"]="peer") -> InputPeer
```

Strategy order (from the MTProto notes §4, cheapest first):

1. **`me`/`saved`** → `InputPeerSelf`.
2. **In-memory `EntityCache`** then the session's SQLite `entities` table via `client.get_input_entity` (free, no network).
3. **`@username`** → `contacts.resolveUsername` (rate class `resolve`; result cached with a 24 h TTL in `~/.tlgr/accounts/<alias>/peers.db`).
4. **`+phone`** → `contacts.resolvePhone` (works for non-contacts only if their privacy allows), falling back to a scan of `contacts.getContacts`.
5. **`t.me/+hash` / `tg://join`** → `messages.checkChatInvite` (read-only: reports the chat without joining; joining is `chat join`).
6. **`t.me/c/<id>/<msg>`** → marked channel id + message id.
7. **Bare int** → `messages.getPeerDialogs([InputDialogPeer])` when a hash is cached; otherwise a **dialog scan** (`iter_dialogs`, the mechanism `user dialog-status` already uses) bounded by `[limits] dialog_scan_max` (default 5,000). Exhausting the list is the only thing that licenses a negative answer.
8. **`min` entities** — when a user was only ever seen inside a channel message, the resolver remembers `(chat_id, msg_id, user_id)` in `peers.db` and builds `InputPeerUserFromMessage` / `InputUserFromMessage` / `InputChannelFromMessage` by hand (Telethon never does). This is what makes `chat posters` → `user get` work for non-contacts.

Failure is **never** ambiguous: a peer that could not be resolved because the strategy ran out of options is `NOT_FOUND` (exit 5); a peer that could not be resolved because a scan was truncated, flooded or errored is `INDETERMINATE` (exit 13) with `reason`. That distinction is the whole point of v1's `dialog-status` and it is now enforced by the resolver for every command.

All ids leaving the daemon go through `utils.get_peer_id` (COR-10). All ids entering it are validated: a positive id used where a channel is required gets a targeted `USAGE` message rather than a Telethon `ValueError`.

### 6.7 File pipelines

**Download** (`daemon/files.py`, checklist 13):

* Always fetch the **message** first (`messages.getMessages`) so `file_reference` is fresh, and keep `(chat_id, msg_id)` as the media's source for the whole transfer.
* `iter_download(location, request_size=512*1024, offset=resume_offset)` into `<target>.part`, `fsync` every 8 MB, atomic rename at the end, verify the final size.
* **Resume**: `--resume` (default on) reads the existing `.part` size and starts there. **Ranges**: `--range START-END`.
* **Per-DC concurrency**: at most 2 large (≥20 MB) + 5 small transfers per DC, matching `small/large_queue_max_active_operations_count` from `help.getAppConfig`; a semaphore per `(account, dc_id)`.
* **File-reference refresh**: any `FileReference*Error` / `FilerefUpgradeNeededError` re-fetches the source message, swaps the reference and retries once — for photos, thumbnails and profile photos too (Telethon only does documents). For `sendMultiMedia` the `FILE_REFERENCE_%d_EXPIRED` index is parsed and only that item is refreshed.
* **Progress** is emitted as NDJSON `progress` frames and as `file_progress` bus events (so a webhook consumer sees them too), throttled to 1/s or 1 MB.
* CDN redirects are treated as best-effort and flagged in `meta.warnings` (Telethon 1.44's CDN path reuses the main auth key and skips hash verification).

**Upload** (checklist 14):

* Parts of 512 KB (128/256 KB for small files, matching `utils.get_appropriated_part_size`), **3–4 `upload.saveBigFilePart` requests in flight** via a sliding `asyncio.gather` window (Telethon uploads strictly sequentially), MD5 for files ≤10 MB, per-part retry on `FILE_PART_X_MISSING`, `InputFileBig` handles reused for a retried `sendMedia` (valid <1 day).
* Pre-flight against `upload_max_fileparts_default/_premium`: a file that cannot fit is a `USAGE` error before a byte is sent.
* **Attributes**: `DocumentAttributeVideo/Audio` are built from `hachoir`/`pillow` when the `[media]` extra is installed, from `ffprobe` when it is on `PATH`, and otherwise from explicit flags (`--duration`, `--width`, `--height`). Without any of those, a video is sent with `duration=0, w=1, h=1` — which renders as a 1×1 "video" — so the daemon emits a `meta.warnings` entry telling the user to install the extra. Photos are resized to ≤2560 px only when Pillow is present; otherwise oversized/PNG-alpha images are sent as documents with a warning rather than failing with `PHOTO_INVALID_DIMENSIONS`.
* **Albums**: `messages.uploadMedia` per item then `messages.sendMultiMedia` (≤10), one `random_id` per item, all journalled.
* `FLOOD_PREMIUM_WAIT_X` is honoured like any flood wait.

### 6.8 Pre-auth flows in the daemon

Because the daemon owns every session file (fixed decision 1), login runs there too. `ops/auth.py` (PR-2) uses these daemon services, but the plumbing lands in the foundation:

| Op | Flow |
|---|---|
| `auth.send-code` | daemon creates a *pending* `AccountSession` in `starting`, takes the session flock, `auth.sendCode`, returns `{phone_code_hash, type, next_type, timeout}`. The hash is held **server-side** keyed by alias (Telethon keeps `_phone_code_hash` in memory, so a two-process login would lose it). |
| `auth.verify-code` | `auth.signIn`; `SessionPasswordNeededError` → `{"needs_password": true, "hint": …}` with exit 4 and a distinct code `AUTH_PASSWORD_REQUIRED` |
| `auth.password` | SRP via `client.sign_in(password=…)`; the password arrives via `--password-env/--password-stdin/--password-file` only |
| `auth.qr` | `client.qr_login()`; streams `{"type":"qr","url":"tg://login?token=…","expires":…}` frames, re-creating the token on expiry, until success or timeout. This is the login method that always works for third-party `api_id`s (`UPDATE_APP_TO_LOGIN` on the code path suggests it automatically). |
| `auth.resend-code` | `auth.resendCode` honouring `next_type`/`timeout` |
| `account.import` | writes a session file from a StringSession or an existing `.session`, verifies by connecting **inside the daemon**, never from the CLI |
| `account.remove` | `auth.logOut` (+ optional `DestroyAuthKey`), release the flock, then delete — `--keep-session` opts out (SEC-03) |

A pending login is bounded (10 min) and counts as daemon activity. `auth.signUp` is never called: `PhoneNumberUnoccupiedError` becomes a `USAGE` error saying tlgr does not create accounts.

### 6.9 Config reload

`SIGHUP` or `POST /v1/admin/reload`. Reloadable without a restart: `[defaults]`, `[limits]`, `[flood]`, `[logging]`, `[webhook]`, `[policy]`, jobs (`jobs.yaml`), `[presence]`. **Not** reloadable: `[network] proxy/ipv6/connection` and identity strings (they require reconnecting, so the reload reports `requires_restart: ["network.proxy"]` and leaves the old value in force). Reload is atomic: parse into new Structs, validate, swap; a parse error leaves the running config untouched and returns `CONFIG_ERROR`. All file reads happen in `asyncio.to_thread` (ROB-05).

### 6.10 Idle-stop

Activity = any of: in-flight `/v1/op` requests > 0, an open `/v1/events` stream, an active file transfer, a running job, an enabled webhook, a pending login. `[daemon] idle_timeout` (default 1800 s) counts only when **all** of them are zero and no request has completed within the window. `idle_timeout = 0` disables it, and it is forced to 0 when the webhook is enabled or when running under launchd/systemd (COR-08, COR-39). The in-flight counter is incremented before the handler and decremented in `finally`, and the monitor refuses to stop while it is non-zero (COR-11) — a 10-minute `chat posters` scan can no longer be killed mid-flight.

### 6.11 Shutdown

```
SIGTERM / /v1/admin/stop / idle
 1. flip ready:false; stop accepting new /v1/op (503 RETRYABLE with a "daemon is shutting down" hint)
 2. close event streams with {"type":"end","reason":"shutdown"}
 3. wait for in-flight requests, drain_s (default 30) deadline; cancel the rest with CancelledError
 4. stop jobs; flush the webhook queue with a 10 s deadline, dead-letter the remainder
 5. per account: presence offline if enabled → _save_states_and_entities() → session.save()
                 → client.disconnect() → release the session flock
 6. persist event seq, flood memory, account states
 7. remove the socket and pid file (only ours), release daemon.lock, exit 0
```

`SIGKILL` loses at most 60 s of pts progress and the in-flight requests; `catch_up=True` recovers the updates on the next start.

### 6.12 launchd / systemd

* **launchd** (`daemon/launchd.py`): `~/Library/LaunchAgents/com.tlgr.daemon.plist`, `KeepAlive.SuccessfulExit=false` **plus** forced `idle_timeout=0`, so an idle exit cannot happen and the "not restarted after a clean exit" trap (COR-39) is unreachable. When installed, the CLI never auto-starts the daemon; it runs `launchctl kickstart -k gui/<uid>/com.tlgr.daemon` and says so. A second manual daemon exits **0** with "already running under launchd" instead of 1-and-respawn-forever.
* **systemd** (`daemon/systemd.py`, new): `~/.config/systemd/user/tlgr.service`, `Type=notify` is not used (no sd_notify dependency); `Type=simple` with `ExecStart=… --foreground`, `Restart=on-failure`, `RestartSec=5`. `tlgr daemon install --systemd|--launchd|--auto`.
* Both write `managed_by` into `~/.tlgr/daemon.state` so `/v1/status` and the CLI know not to fork their own.

### 6.13 Presence policy

Telethon never calls `account.updateStatus`, so a tlgr account reads as permanently offline. `[presence] mode = "off" | "online" | "mirror"` (default **`off`**): `online` pings `account.updateStatus(offline=False)` every `online_update_period_ms` from `help.getAppConfig`, and sends `offline=True` on shutdown; `mirror` does it only while a job or an interactive command is active. Off by default because appearing online is a visible, account-affecting behaviour the operator must opt into.

### 6.14 Layer escape hatch

`tlgr/core/custom_tl.py` holds the recipe for calling methods newer than Telethon's layer 227 (subclass `TLRequest`, `CONSTRUCTOR_ID`, `SUBCLASS_OF_ID = zlib.crc32(b'<ResultType>')`, `_bytes()`, `from_reader`, register result types in `alltlobjects`, wrap in `InvokeWithLayerRequest`). It ships **empty except for the helper base classes and a doctest**, because nothing on the P0/P1 path needs layer 229 (communities and ephemeral messages are brand-new server features; Firebase login is Android-only). Any op that needs it must set `tags={"custom-tl"}` and carry a test that round-trips the serialisation.

---

## 7. Errors end-to-end

### 7.1 Flow

```
Telethon exception / msgspec ValidationError / tlgr exception
        │  (raised inside impl, inside asyncio.timeout, inside the dispatcher)
        ▼
core/errors.py::classify(exc)  ── the ONLY place Telethon exception classes are named
        │  (as *strings*: keying ERROR_MAP by class name is what lets cli/ stay
        │   Telethon-free while this table still classifies every Telethon error)
        │  → ErrorBody{code, message, exit_code, retryable, wait_seconds?, field?, rpc?, hint?}
        ▼
daemon: log (structured, redacted, with request_id) → HTTP status from the table → JSON envelope
        ▼
transport: envelope → raise the matching TlgrError subclass, carrying every field
        ▼
cli: --json → print the error object to stdout; always → one human line + hint on stderr
        ▼
sys.exit(error.exit_code)
```

The same `classify()` is used by the legacy `daemon/ipc.py::_handle_exception` during migration, so unmigrated groups get correct exit codes on day one (COR-06).

### 7.2 The mapping table

| Telethon / internal | code | exit | HTTP | retryable | extra |
|---|---|---|---|---|---|
| `FloodWaitError`, `SlowModeWaitError`, `FloodPremiumWaitError`, `FloodTestPhoneWaitError`, `TakeoutInitDelayError`, `2FA_CONFIRM_WAIT_X`, `PreviousChatImportActiveWaitXminError` | `RATE_LIMITED` | 7 | 429 | yes | `wait_seconds` |
| `PeerFloodError` | `PEER_FLOOD` | 9 | 403 | no | breaker opens |
| `FrozenMethodInvalidError`, any RPC message matching `^FROZEN_` | `ACCOUNT_FROZEN` | 9 | 403 | no | `freeze_until`, `appeal_url` |
| `AuthKeyUnregisteredError`, `AuthKeyInvalidError`, `AuthKeyPermEmptyError`, `SessionRevokedError`, `SessionExpiredError`, `AuthKeyDuplicatedError`, `UserDeactivatedError`, `UserDeactivatedBanError`, `AuthKeyNotFound` | `SESSION_ERROR` | 4 | 401 | no | account → `needs_login`; `hint` = re-login command |
| `SessionPasswordNeededError` | `AUTH_PASSWORD_REQUIRED` | 4 | 401 | no | `hint` = `--password-env` |
| `PhoneCodeInvalidError`, `PhoneCodeExpiredError`, `PhoneNumberInvalidError`, `PhoneNumberBannedError`, `PhoneNumberUnoccupiedError`, `PasswordHashInvalidError`, `UpdateAppToLoginError` | `AUTH_ERROR` | 4 | 401 | no | `UPDATE_APP_TO_LOGIN` hints QR login |
| `UsernameNotOccupiedError`, `UsernameInvalidError`, `PeerIdInvalidError`, `ChannelInvalidError`, `ChatIdInvalidError`, `UserIdInvalidError`, `MessageIdInvalidError`, `MsgIdInvalidError`, `InviteHashExpiredError`, `InviteHashInvalidError`, `StickersetInvalidError`, `ValueError("Could not find the input entity…")`, tlgr `ChatNotFoundError` | `NOT_FOUND` | 5 | 404 | no | `rpc` |
| `ChatAdminRequiredError`, `ChatWriteForbiddenError`, `ChatSend*ForbiddenError` (all 14), `ChannelPrivateError`, `UserPrivacyRestrictedError`, `UserIsBlockedError`, `UserBannedInChannelError`, `UserNotParticipantError`, `MessageDeleteForbiddenError`, `MessageAuthorRequiredError`, `MessageEditTimeExpiredError`, `RightForbiddenError`, `ChatForwardsRestrictedError`, `TopicClosedError`, `BroadcastForbiddenError`, `ForbiddenError` (403 base), tlgr `PermissionError_` | `PERMISSION_DENIED` | 6 | 403 | no | `rpc` |
| `PremiumAccountRequiredError`, `PrivacyPremiumRequiredError`, `BoostsRequiredError` | `PERMISSION_DENIED` | 6 | 403 | no | `hint` = "requires Telegram Premium" |
| `BalanceTooLowError`, `AllowPaymentRequiredError`, `StarsFormAmountMismatchError`, `FormExpiredError` | `PERMISSION_DENIED` | 6 | 402→403 | no | `hint` names the Stars amount |
| `MessageNotModifiedError` | *(no error)* | 0 | 200 | — | `ok:true`, `meta.already=true` |
| `MessageEmptyError`, `MessageTooLongError`, `MediaEmptyError`, `MediaInvalidError`, `PhotoInvalidDimensionsError`, `ContactIdInvalidError`, `UserAlreadyParticipantError`, `UsersTooMuchError`, `BotMethodInvalidError`, `BannedRightsInvalidError`, `ScheduleDateInvalidError`, most other `400 BAD_REQUEST`, **`msgspec.ValidationError`**, `click.UsageError` | `USAGE` | 2 | 400 | no | `field` |
| `FileReferenceExpiredError`, `FileReferenceInvalidError`, `FilerefUpgradeNeededError` | *(internal retry)* → `RETRYABLE` if the refresh also fails | 8 | 503 | yes | |
| `ServerError`, `RpcCallFailError`, `RpcMcgetFailError`, `InterdcCallErrorError`, `TimedOutError`, `PersistentTimestampOutdatedError`, `asyncio.TimeoutError`, `ConnectionError`, `OSError`, `"Cannot send requests while disconnected"` | `RETRYABLE` | 8 | 503 | yes | `retry_after` |
| unknown `RPCError` | `GENERIC` | 1 | 500 | no | `rpc:{code,message,method}` |
| account not resolvable | `ACCOUNT_REQUIRED` | 2 | 400 | no | |
| account alias not registered / invalid | `ACCOUNT_NOT_FOUND` | 5 | 404 | no | |
| daemon/CLI protocol mismatch | `DAEMON_VERSION_MISMATCH` | 11 | 409 | no | |
| daemon not running / not ready / failed to start | `DAEMON_NOT_RUNNING` / `DAEMON_ERROR` | 11 | — | no | last log lines in `hint` |
| socket/transport failure, malformed envelope, truncated stream | `IPC_ERROR` | 12 | — | yes | |
| op blocked by policy | `PERMISSION_DENIED` | 6 | 403 | no | `hint` names the op id to allow |
| config parse/validation failure | `CONFIG_ERROR` | 10 | 400 | no | file + key |
| answer could not be established (truncated scan, flood mid-harvest, RPC failure during a negative proof) | `INDETERMINATE` | 13 | 200 | maybe | `reason`; **never** reported as a negative |
| SIGINT | `CANCELLED` | 130 | — | — | |

Numeric suffixes are stripped before lookup and re-exposed as parameters (`FLOOD_WAIT_42` → `wait_seconds: 42`; `FILE_PART_7_MISSING` → `which: 7`; `PHONE_MIGRATE_2` → handled internally). `406 NOT_ACCEPTABLE` errors other than `AUTH_KEY_DUPLICATED` carry a `hint` telling the caller that the real message arrives out of band via `updateServiceNotification` and that tlgr surfaces it as a `service_notification` event.

### 7.3 Exit codes (unchanged from v1 — this is a compatibility contract)

| Code | Name | Meaning |
|---|---|---|
| 0 | SUCCESS | ok (including idempotent no-ops with `already: true`) |
| 1 | GENERIC | unclassified failure |
| 2 | USAGE | bad arguments, validation error, missing account |
| 3 | EMPTY | no results, only where the op declares `empty_exit=3` |
| 4 | AUTH | authentication / session error |
| 5 | NOT_FOUND | chat, user, message or account not found |
| 6 | PERMISSION | rights, privacy, policy, premium |
| 7 | RATE_LIMITED | retry after `wait_seconds` |
| 8 | RETRYABLE | transient; retry with backoff |
| 9 | SPAM_FLAGGED | `PEER_FLOOD` / `ACCOUNT_FROZEN` — **stop sending** |
| 10 | CONFIG | configuration error |
| 11 | DAEMON | daemon not running / mismatched / failed |
| 12 | IPC | transport failure |
| 13 | INDETERMINATE | could not be established — treat as unknown, never as a negative |
| 130 | CANCELLED | SIGINT |

`tlgr agent exit-codes --json` is generated from this table, and a test asserts the table, `AGENT.md` and `docs/reference/` agree.

---

## 8. Security model

### 8.1 Assets and threat model

**Assets:** session files (`auth_key` = full account access), API credentials, message content, contact graph, the IPC socket (equivalent to the session files), webhook tokens, 2FA passwords.

**In scope:** other local uids; unprivileged processes running as the same user (a malicious `npm`/`pip` post-install script, a sandboxed tool with `$HOME` access); accidental disclosure through logs, dead letters, `ps`, backups; a compromised webhook endpoint; two tlgr processes racing one session.

**Out of scope:** a fully compromised user account with a debugger, physical access, Telegram itself, and MTProto cryptography (Telethon's).

### 8.2 Controls

| Control | Implementation | Fixes |
|---|---|---|
| `umask(0o077)` before daemonising | `daemon/main.py`, first statements | SEC-01 |
| Socket mode 0600 | `os.chmod(sock, 0o600)` immediately after `site.start()`; asserted by a test | SEC-01 |
| Peer-uid check | middleware reads `request.transport.get_extra_info("socket")`; Linux `SO_PEERCRED` (`struct 3i` → pid, uid, gid), macOS `SOL_LOCAL(0)/LOCAL_PEERCRED(1)` → `struct xucred`, uid at bytes 4–8 (**verified on Darwin 25.6**); mismatch → 403 and a WARN log with the peer pid | SEC-01 |
| Token fallback | `~/.tlgr/ipc.token` (32 random bytes, 0600) sent as `X-Tlgr-Token`, compared with `hmac.compare_digest`. Required when the platform gives no peer credentials, or when `[security] require_token = true` | SEC-01 |
| Alias validation | `^[A-Za-z0-9_-]{1,64}$` in **one** function, called before any path is built, in `AccountManager`, the daemon and the CLI; read paths never `mkdir` | SEC-02 |
| Policy in the daemon | `--enable-commands`/`[policy] allow`/`~/.tlgr/policies/<name>.toml` bound to a token; enforced by canonical op id after alias canonicalisation, in the CLI *and* the daemon; `deny` beats `allow`; `mutating`/`destructive` can be denied wholesale (`allow = ["*:read"]`) | SEC-04 |
| Logout on removal | `account.remove` → `auth.logOut` (+ optional `DestroyAuthKey`) before deleting; `--keep-session` opts out; `account session list/revoke/revoke-others/confirm/ttl` land in PR-2 | SEC-03 |
| Secrets never in argv | `--password-env` (default `TLGR_2FA_PASSWORD`), `--password-stdin`, `--password-file`; same for `--token-env`. The `SECRET` param type refuses to generate a value-taking flag at all | STYLE §3 |
| Access log off | `web.AppRunner(app, access_log=None)` | SEC-05 |
| Structured logs + redaction | `core/logging.py`: JSON lines, `RotatingFileHandler` (8 MB × 5), 0600. A `RedactionFilter` drops message text, phone numbers, tokens, `access_hash`, `auth_key`, `file_reference`, cursors and password material — allow-list of loggable fields, not a blocklist of patterns. `--verbose` raises verbosity, never redaction | SEC-05, SEC-06 |
| Dead letters | 0600, rotated, capped; `webhook dead-letter list --ids-only` by default (bodies only with `--full`) | SEC-06 |
| Private writes | one `write_private(path, data, mode=0o600)` used by every writer (`config init` included) | SEC-07 |
| Webhook integrity | HMAC-SHA256 over the exact body, monotonic `seq`, `delivery_id`; a non-loopback `http://` URL logs a WARN at start and puts a warning in `/v1/status` | SEC-08 |
| Session file hygiene | 0600 enforced at add/import **and** re-checked at every daemon start (including `*.session-journal`); a world-readable session refuses to start with a fix hint | SEC-01, SEC-10 |
| Single owner per session | `flock` on `<alias>/session.lock`, held by the daemon; the CLI never opens a session file | Telethon §4, `AUTH_KEY_DUPLICATED` |
| Single daemon | `flock` on `daemon.lock` held for the process lifetime | COR-14 |
| API credentials | read from `config.json` (0600) or env; `account add` reads the hash with `getpass` (no echo); env credentials are noted as visible in `ps` in the docs | SEC-09 |
| No string-session export by default | `account export --string` requires `--yes` and prints a warning that the string is full account access | checklist 25 |

### 8.3 Notes

* The policy allowlist is a **usability guard with teeth**, not a sandbox against a hostile local process: anything that can connect to the socket can also read the session file directly. It is documented that way. What it does buy: an agent given `--enable-commands message.list,message.send` cannot delete a chat by mistake, and the daemon enforces that even if the agent forges its own IPC call — provided the operator binds the policy to a token rather than passing a flag the agent controls.
* The peer-uid check is defence in depth over the 0600 socket, both because umasks get changed and because it produces an auditable log line when something else tries.
* Redaction is allow-list based because a blocklist over free-form message text is not a control.

---

## 9. Output and rendering

`cli/render.py`, driven entirely by the spec.

**JSON (`--json`)** — the envelope from §5.2 verbatim, `ensure_ascii=False`, UTF-8, one object, newline-terminated. For paginated ops `result` is the item list and `page` carries the pagination fields; for `--results-only` on a paginated op the output is the `Page[T]` object (`{"items": …, "has_more": …, "next_cursor": …, "total": …}`) so STYLE.md's contract holds. For stream ops, NDJSON passes through unchanged.

**`--results-only`** returns `result` verbatim (or the `Page[T]` above) — no envelope keys, no heuristics. This kills COR-18: v1 guessed the "primary" key and printed a bare `2` for `message delete`.

**`--select a,b.c`** projects *into* `result`, recursing element-wise into lists, preserving key order, silently omitting missing paths. `--select` works with or without `--results-only`; when used alone, the envelope is preserved and only `result` is projected (v1's README example printed `{}`).

**Plain (`--plain`)** — TSV, header row of `op.columns`, `\t`/`\n`/`\r` escaped to spaces, `None` → empty string. Stable for `cut`/`awk`.

**Human (default)** — space-padded table for lists, key/value block for single objects, with real formatting rules (UX-03):

| Value | Rendered |
|---|---|
| `None` / absent | `-` |
| `True` / `False` | `yes` / `no` |
| list of scalars | `a, b, c` (truncated to the column width with `…`) |
| nested object | the dot path is the column (`sender.title`), so nothing prints a `dict` repr |
| RFC-3339 timestamp | local time, `today 14:02` / `Mon 09:14` / `2026-09-02 09:14` by recency |
| long text | truncated to the column width with `…`, newlines → `⏎` |
| marked ids | as-is; a `--wide` flag disables all truncation |

Columns come from `op.columns` (3–6 by default); `--columns a,b,c` overrides; `--no-header` for scripts. Colour is used only on a TTY and honours `NO_COLOR`.

**Errors** — in JSON mode the error object goes to **stdout** (so an agent parsing stdout always gets JSON) *and* a one-line human summary goes to stderr. In human mode only stderr. Click usage errors are formatted the same way in JSON mode (`{"ok":false,"error":{"code":"USAGE","exit_code":2,"message":…,"usage":…}}`), fixing UX-02.

**Confirmations** — `cli/confirm.py`: destructive ops prompt on a TTY unless `--yes`; off a TTY they require `--yes` and otherwise fail with `USAGE` (exit 2). `--no-input` never prompts and never blocks (COR-16).

---

## 10. Configuration and file layout

### 10.1 `~/.tlgr/` (mode 0700)

```
~/.tlgr/
├── config.toml                 0600  main configuration
├── accounts.json               0600  alias registry: active alias, per-account identity + health
├── jobs.yaml                   0600  gateway jobs (unchanged format)
├── webhook.toml                0600  webhook config (token!)
├── policies/<name>.toml        0600  named policies bound to tokens
├── ipc.token                   0600  IPC auth token (when enabled)
├── cursor.key                  0600  cursor HMAC key
├── identity.json               0600  stable initConnection identity strings
├── daemon.lock                 0600  flock target (single instance)
├── daemon.pid                  0600
├── daemon.sock                 0600  srw-------
├── daemon.state                0600  {version, protocol, pid, socket, managed_by, started_at}
├── dead_letter.jsonl[.1..3]    0600  rotated, capped
├── logs/daemon.log[.1..5]      0600  rotated structured logs
├── downloads/                  0700  default download target
├── accounts/<alias>/           0700
│   ├── config.json             0600  api_id / api_hash
│   ├── session.session         0600  Telethon SQLite session
│   ├── session.lock            0600  flock target (single owner)
│   ├── peers.db                0600  resolver cache: username/phone TTL, min-entity contexts
│   ├── flood.json              0600  persisted flood deadlines
│   ├── events.state            0600  last emitted event seq
│   └── outbox.jsonl            0600  in-flight random_ids for idempotent sends
└── cache/                      0700  app config, sticker sets, hash-based list caches
```

A start-up audit fixes `0700`/`0600` where it can and refuses to start where it cannot (with the exact `chmod` to run).

### 10.2 `config.toml`

```toml
[accounts]
default = "work"                  # used when -a and TLGR_ACCOUNT are absent

[defaults]
output = "human"                  # human | json | plain   (TLGR_JSON / --json still win)
parse_mode = "none"               # none | md | html  — 'none' is the safe default (COR-21)
require_account = false           # true ⇒ every command must be given an explicit account
timezone = ""                     # IANA name for human timestamps and time-of-day filters; "" = system
confirm_destructive = true

[daemon]
auto_start = true
start_timeout = 30
idle_timeout = 1800               # 0 disables; forced to 0 with webhook enabled or under launchd/systemd
drain_seconds = 30
preconnect = []                   # aliases to connect at start, in this order
event_buffer = 4096               # per-account ring buffer
event_workers = 8                 # bounded pool; per-chat order preserved
resync_depth = 50                 # messages re-read per dialog after *TooLong
state_save_interval = 60
log_level = "info"

[identity]                        # honest initConnection strings; stable across restarts
device_model = ""                 # "" = derive from hostname + machine
system_version = ""               # "" = derive from the OS
lang_code = ""                    # "" = derive from the locale, fallback "en"
system_lang_code = ""
tz_offset = true                  # send params={"tz_offset": …} like official clients

[presence]
mode = "off"                      # off | online | mirror

[network]
proxy = ""                        # "socks5://user:pass@host:1080" | "http://…" | "mtproxy://host:port#secret"
ipv6 = false
connect_timeout = 10
connection = "tcp_full"           # tcp_full | tcp_abridged | tcp_intermediate | tcp_obfuscated | mtproxy

[flood]
sleep_threshold = 120             # seconds tlgr will sleep off inside a request
max_wait = 600                    # above this, always return RATE_LIMITED immediately
persist = true                    # remember deadlines across restarts

[rate.read]        rate = 10.0  burst = 20
[rate.resolve]     rate = 0.5   burst = 5
[rate.send]        rate = 1.0   burst = 3   new_peers_per_day = 30
[rate.bulk]        rate = 2.0   burst = 4

[limits]
entity_cache = 20000
request_retries = 5
dialog_scan_max = 5000
download_concurrency_small = 5    # per DC, files < 20 MB
download_concurrency_large = 2    # per DC, files >= 20 MB
upload_parts_in_flight = 4
max_album = 10

[security]
require_token = false             # force X-Tlgr-Token even when peer credentials work
peer_uid_check = true
warn_insecure_webhook = true

[policy]
allow = ["*"]                     # canonical op ids, "group.*" wildcards, or "*"
deny  = []

[logging]
redact = true                     # never false in a release build; a warning is logged if set false
max_bytes = 8388608
backups = 5

[media]
download_dir = "~/.tlgr/downloads"
ffprobe = "auto"                  # auto | off | /path/to/ffprobe
```

Precedence for every value: **CLI flag → environment (`TLGR_*`) → `config.toml` → built-in default**. `tlgr config get/set/list/validate/path` operate on this file, and `tlgr config validate` resolves job chat references (COR-22) and reports which keys require a daemon restart.

---

## 11. Testing and CI

### 11.1 Fake Telethon client (`tests/fake_telethon.py`)

The single most valuable artefact in the test suite: it lets every op impl be unit-tested with no network and no session file.

```python
class FakeTelegramClient:
    """In-memory stand-in for telethon.TelegramClient.

    Backed by a small world model, not by recorded fixtures, so the same fake
    serves every group as the surface grows.
    """
    def __init__(self, world: World) -> None: ...
    # ---- world ----------------------------------------------------------
    # World holds: users{id: FakeUser}, chats{id: FakeChat}, dialogs[…],
    # messages{chat_id: [FakeMessage…]}, drafts{}, contacts[], participants{},
    # reactions{}, files{path: bytes}, and a `raw` request table.
    # ---- behaviour knobs -------------------------------------------------
    #   world.fail_next(RequestType, exc)        one-shot exception injection
    #   world.flood(RequestType, seconds)        make a request raise FloodWaitError
    #   world.latency(ms)                        exercise timeouts and cancellation
    #   world.disconnect_after(n_requests)       exercise the supervisor
    #   world.calls                              ordered list of (request_type, kwargs)
    # ---- API surface -----------------------------------------------------
    async def __call__(self, request, ordered=False): ...     # dispatch through world.raw
    def iter_messages(...); async def get_messages(...); async def send_message(...)
    async def send_file(...); async def edit_message(...); async def delete_messages(...)
    def iter_dialogs(...); def iter_participants(...); async def get_input_entity(...)
    async def get_entity(...); async def send_read_acknowledge(...); def action(...)
    async def download_media(...); def iter_download(...); async def upload_file(...)
    def on(self, event); def add_event_handler(...); async def catch_up(...)
    async def connect(); async def disconnect(); def is_connected(); disconnected: Future
```

Rules: it returns **real Telethon type objects** (`types.Message`, `types.User`, `types.Channel`, `types.Updates`) built by small builders (`tests/factories.py`), so serialisers are tested against the real shapes; it records every request so tests can assert *what was sent* (e.g. "`mute_until` is within ±5 s of `time.time() + 3600`" — the COR-01 regression test); and it can raise any Telethon error class on demand, which is how the error table is tested row by row.

### 11.2 Test layers

| Layer | Files | What it proves |
|---|---|---|
| Models | `test_models.py` | encode/decode round-trip, RFC-3339, unknown-field tolerance, `UNSET` semantics |
| Serialisers | `test_serialize_message.py`, `_dialog.py`, `_peer.py` | real Telethon objects → models: media kinds, service actions, reactions incl. `mine`, forwards, reply headers, marked ids for every entity type |
| Op impls | `test_ops_<group>.py` | each impl against the fake client; assertions on the requests issued, not just the output |
| Registry contract | `test_registry_contract.py` | §11.3 |
| CLI mapping | `test_cli_mapping.py` | `CliRunner` parse → expected request struct, for every op |
| Transport | `test_transport.py` | real `UnixSite` on a temp socket: unicode/space/`#`/`&` payloads, NDJSON framing, chunked bodies, timeouts → `RETRYABLE`, truncated stream → error, handshake mismatch → restart |
| Dispatch | `test_dispatch.py` | policy, account required, dry-run, timeout, cancellation on client disconnect, envelope shape |
| Errors | `test_errors_map.py` | parametrised over the §7.2 table: raise → HTTP status, code, exit code, retryable, extras |
| Daemon lifecycle | `test_daemon_lifecycle.py` | flock singleton, concurrent auto-start spawns exactly one, bind-before-connect (`ready:false`), idle-stop with in-flight, drain on shutdown, stale pid/socket |
| Sessions | `test_account_session.py` | state machine transitions, fatal-auth classification, backoff, catch-up after reconnect, wall-clock jump, state persistence |
| Events | `test_events.py` | seq monotonic + persisted, ring buffer replay, `gap` frame, per-chat ordering under load, slow subscriber lags without blocking the bus, self-origin echo |
| Rate limiting | `test_ratelimit.py` | bucket pacing, persisted flood deadlines survive a restart, breaker opens on `PEER_FLOOD` and refuses sends but not reads |
| Security | `test_security.py` | socket mode 0600 after start, umask, alias rejection (`../x`, absolute, 65 chars), read paths never mkdir, policy denies by canonical id including aliases, redaction filter |
| Pagination | `test_pagination.py` | each cursor kind round-trips; a cursor from another op/account is rejected; `--all` walks and terminates; page 2 starts after page 1's last id (COR-05) |
| Files | `test_files.py` | resume, ranges, fileref refresh on `FileReferenceExpiredError`, per-DC concurrency caps, progress frames |
| Render | `test_render.py` | `--results-only`, `--select` (incl. into lists), human formatting of `None`/lists/dicts, plain escaping |
| Parity | `test_parity.py` | every `covers` id exists; P0 coverage does not regress; waivers are well-formed |
| Docs | `test_docs_fresh.py` | `make docs` produces no diff; `AGENT.md` exit-code table matches `core/errors.py` |
| v1 compatibility | `test_agentmd_compat.py` | every key documented in v1's `AGENT.md` still exists with the same type; every v1 command path is still invocable |
| Layering | `test_layering.py` | §2.2 import rules |
| Live (opt-in) | `test_live_*.py` | `TLGR_LIVE_TESTS=1` against Telegram **test DCs** (`session.set_dc(2, '149.154.167.40', 80)`, `99966XYYYY` numbers, code = X×5). Never run in CI by default; never mixes test and production `api_id`s |

Existing v1 tests are kept: the 273 green tests cover `media_details`, dialog status, posters, filters, processors, gateway and output, and they are the regression net for the serialiser rewrite. Tests whose subject moves are re-pointed, not deleted.

### 11.3 The contract test (the reason the registry pays for itself)

```python
@pytest.mark.parametrize("spec", REGISTRY.values(), ids=lambda s: s.id)
class TestOperationContract:
    def test_example_validates(self, spec):        # example decodes into spec.response
    def test_example_args_parse(self, spec):       # example_args → a valid spec.request
    def test_cli_command_exists(self, spec):       # the generated tree exposes it + every alias
    def test_globals_attached(self, spec):         # --json/--account/--dry-run accepted after the args
    def test_dry_run_skips_impl(self, spec):       # mutating ⇒ impl is never called with --dry-run
    def test_destructive_needs_yes(self, spec):    # destructive ⇒ non-TTY without --yes exits 2
    def test_policy_blocks_by_id(self, spec):      # allowlist without the id ⇒ exit 6, via alias too
    def test_schema_generates(self, spec):         # JSON Schema for request+response, no $ref dangling
    def test_docs_section(self, spec):             # docsgen renders it
    def test_columns_resolve(self, spec):          # every column path exists in the response model
    def test_covers_known(self, spec):             # catalog ids exist
    def test_paginated_shape(self, spec):          # paginated ⇒ Page[T] + --limit/--cursor/--all
    def test_timeout_sane(self, spec)
```

One new op therefore arrives with 13 tests for free, and the failure modes that produced COR-17, COR-33, SEC-04 and UX-01 become impossible to reintroduce.

### 11.4 CI

`.github/workflows/ci.yml` — matrix `{ubuntu-latest, macos-latest} × {3.10, 3.11, 3.12, 3.13, 3.14}`:

```
ruff check .            ruff format --check .
mypy --strict tlgr/models tlgr/ops tlgr/core tlgr/registry.py tlgr/schema.py
pytest -q --cov=tlgr --cov-report=term-missing --cov-fail-under=80
python -m tlgr.tools.gen_docs --check           # docs are fresh
python -m tlgr agent parity --check-p0          # P0 coverage did not regress
python -m build && twine check dist/*
```

`--cov-fail-under` starts at 80 and ratchets by 1 per group PR to a target of 90. A separate scheduled job runs the live suite against test DCs. `.gitignore` stops ignoring `*.yaml` (PKG-04) so workflow files and YAML fixtures are tracked. `pyproject.toml` gains `[tool.ruff]`, `[tool.mypy]`, `[tool.coverage]`, `[project.optional-dependencies] {dev, fast, proxy, media}` and pins `telethon==1.44.*`.

---

## 12. The foundation PR

**Title:** `foundation: operation registry, typed IPC v2, supervised daemon sessions`
**Proof of the model:** the whole `message` group (plus `draft`) migrated to the registry.
**Non-goal:** migrating any other group. Everything else keeps working through the legacy path.

### 12.1 Files delivered

**New — models (9)**
`tlgr/models/__init__.py`, `base.py`, `peer.py`, `message.py`, `dialog.py`, `page.py`, `error.py`, `event.py`, `envelope.py`

**New — registry & ops (12)**
`tlgr/registry.py`, `tlgr/schema.py`, `tlgr/docsgen.py`, `tlgr/parity.py`,
`tlgr/ops/__init__.py`, `tlgr/ops/_spec.py`, `tlgr/ops/_params.py`, `tlgr/ops/_serialize.py`,
`tlgr/ops/message.py`, `tlgr/ops/draft.py`, `tlgr/ops/daemon.py`, `tlgr/ops/agent.py`

**New — CLI (6 + the `legacy/` package move)**
`tlgr/cli/gen.py`, `params.py`, `globals.py`, `render.py`, `confirm.py`, `errors.py`; `tlgr/cli/legacy/` (v1 modules moved verbatim, imports rewritten, `message.py`/`draft.py` deleted)

**New — transport (4)**
`tlgr/transport/__init__.py`, `client.py`, `ndjson.py`, `autostart.py`

**New — daemon (15)**
`tlgr/daemon/main.py`, `app.py`, `dispatch.py`, `stream.py`, `session.py`, `sessions.py`, `events.py`, `ratelimit.py`, `policy.py`, `peercred.py`, `singleton.py`, `idle.py`, `files.py`, `preauth.py`, `systemd.py`

**New — core (9)**
`tlgr/core/paths.py`, `pagination.py`, `peers.py`, `identity.py`, `timefmt.py`, `text.py`, `logging.py`, `telethon_compat.py`, `custom_tl.py`

**New — data & tools (4)**
`tlgr/data/catalog_index.json`, `tlgr/data/parity_waivers.toml`, `tools/prune_catalog.py`, `tools/gen_docs.py`

**Rewritten**
`tlgr/core/errors.py` (ERROR_MAP + classify + the full TlgrError tree), `tlgr/core/config.py` (typed Structs, dead job engine deleted), `tlgr/core/accounts.py` (alias validation, health persistence, ordered lists), `tlgr/daemon/webhook.py` (bus subscriber, HMAC, msgspec encoding, rotation), `tlgr/daemon/lifecycle.py` (umask, locks, rotating logs), `tlgr/daemon/launchd.py` (idle-stop interaction), `tlgr/cli/__init__.py` (build_cli merging generated + legacy), `tlgr/gateway/engine.py` (bus subscriber), `pyproject.toml`, `.gitignore`

**Deleted**
`tlgr/ipc_client.py` → replaced by a 20-line shim re-exporting `transport.legacy_request` until the last group migrates; `tlgr/cli/message.py`, `tlgr/cli/draft.py`; `EXAMPLE_RESPONSES` entries for `message`/`draft`; `core/config.py` dead job engine; the `tqdm` dependency

**Docs**
`docs/design/ARCHITECTURE.md` (this file), `docs/design/EVENTS.md` (envelope + starter taxonomy), `docs/design/STYLE.md` (moved in from the design workspace), `docs/reference/message.md` + `draft.md` + `daemon.md` + `agent.md` (generated), `docs/reference/PARITY.md` (generated), `CHANGELOG.md`, `SECURITY.md`, `Makefile` (`docs`, `parity`, `completions`, `lint`, `test`), `README.md`/`AGENT.md` updated to point at generated reference and the new protocol

**Tests (22)**
`tests/fake_telethon.py`, `tests/factories.py`, `tests/conftest.py` (temp-base, fake-client, live-daemon fixtures), `test_models.py`, `test_serialize_message.py`, `test_ops_message.py`, `test_registry_contract.py`, `test_cli_mapping.py`, `test_transport.py`, `test_dispatch.py`, `test_errors_map.py`, `test_daemon_lifecycle.py`, `test_account_session.py`, `test_events.py`, `test_ratelimit.py`, `test_security.py`, `test_pagination.py`, `test_render.py`, `test_parity.py`, `test_layering.py`, `test_docs_fresh.py`, `test_agentmd_compat.py`

**CI (1)**
`.github/workflows/ci.yml`

**Total: 108 files** — 59 new modules/data files, 10 rewritten, 3 deleted, 13 docs, 22 tests, 1 workflow.

### 12.2 Audit items closed by the foundation

| ID | Sev | Fixed by |
|---|---|---|
| SEC-01 | S0 | `umask(0o077)` + socket 0600 + peer-uid check + token fallback + permission audit at start (`daemon/main.py`, `peercred.py`, `core/paths.py`); asserted by `test_security.py` |
| COR-02 | S0 | account resolved fully in the CLI (positional → `-a` → `TLGR_ACCOUNT` → `[accounts] default` → active alias); `get_client("")` deleted; daemon returns `ACCOUNT_REQUIRED`; connect list ordered |
| COR-04 | S1 | `http.client` + JSON bodies; `urlencode` for the two remaining GETs; the legacy shim encodes too, so unmigrated groups are fixed as well. Test: Persian, spaces, `#`, `&`, `+` |
| COR-06 | S1 | `core/errors.py::classify` + the §7.2 table, applied in the new dispatcher *and* the legacy `_handle_exception`; every row contract-tested |
| COR-07 | S0 | webhook is a bus subscriber; payload built from models and encoded with `msgspec.json.encode`; a serialisation failure is a logged bug, not a delivery retry; dead letter 0600 + rotation |
| COR-12 | S1 | `SessionManager` with a per-alias `asyncio.Lock` + double-check, and a session-file `flock` |
| COR-13 | S1 | `AccountSession` supervisor: reconnect with capped jittered backoff, `catch_up()` after every reconnect, fatal-auth → `needs_login`, `RETRYABLE` while degraded |
| COR-14 | S1 | `flock` singleton in both the probe and `main()`; bind-before-connect with `ready:false`; readiness polled via `/v1/status` (not the socket file); never unlink another process's socket/pid; `PermissionError` ≠ "not running" |
| COR-01 | S0 | one-line fix in the legacy `chat mute` (`int(time.time()) + duration`), `chat unmute`, effective `mute_until` returned as RFC-3339, plus the fake-client regression test. (The `chat` group itself migrates in PR-3.) |
| COR-11 | S1 | in-flight counter + idle refusal + drain on shutdown |
| COR-08 | S1 | activity accounting includes webhook, event streams and transfers; `idle_timeout` forced to 0 with webhook enabled |
| COR-15 | S1 | `flood_wait_max` sent per request, applied as a per-call `flood_sleep_threshold`, reported as `meta.flood_wait_slept` |
| COR-17 | S2 | `mutating` + dispatcher short-circuit (uniform for every migrated op; contract-tested) |
| COR-18 | S2 | uniform envelope + `--results-only`/`--select` semantics in `render.py` |
| COR-30/31/32 | S2 | typed request decoding (`USAGE` + `field`), real timeouts (`RETRYABLE`), stdlib HTTP (no hand-rolled chunk decoding) |
| COR-33 | S2 | `example` on the spec, validated in tests, emitted by `tlgr schema` |
| COR-35 | S2 | `core/timefmt` everywhere in the new models |
| COR-37/38 | S2 | `/v1/status` with `ready`+`version`+`protocol`; handshake with restart |
| COR-40/41/42 | S3 | single log handler; task references held; `ctx.params` bug gone with the generated tree |
| SEC-02 | S1 | one alias validator called before any path; read paths never `mkdir` |
| SEC-04 | S1 | policy enforced in the daemon by canonical op id (aliases canonicalised first) |
| SEC-05/06/07/08 | S2 | access log off, rotating redacted logs, `write_private()`, HMAC + `seq` + rotation for webhooks |
| ROB-01/02/03 | S1 | NDJSON streaming and daemon-side `--all`; bounded worker pool off the update loop; per-op timeouts, request ids, cancellation on disconnect |
| ROB-04/05 | S2 | `catch_up=True` + supervisor; blocking I/O moved to `asyncio.to_thread` |
| MNT-01/02 | S1 | the registry and typed models |
| MNT-03 | S1 | the test layers in §11.2 |
| MNT-04 | S2 | dead config/job engine deleted; `tqdm` dropped |
| PKG-01/02/04 | S1/S2/S3 | CI, ruff, mypy, coverage gate, `CHANGELOG.md`, `SECURITY.md`, `.gitignore` fix |
| UX-01/02 | S2 | globals on every generated command; JSON usage errors |
| UX-03 | S2 | the human renderer |

Deferred to the group PRs (with the S1 ones called out): COR-03 (`chat create`, PR-3), COR-05 (`message search --cursor` — **fixed in the foundation**, since `message` migrates), COR-09 (`watch` re-points, PR-4; the endpoint ships now), COR-10 (`chat get`/`draft list` ids — `draft` is fixed now, `chat` in PR-3), SEC-03 (logout, PR-2), COR-19..29 (per group).

### 12.3 Acceptance criteria

1. `pytest -q` is green, ≥ 80 % coverage, and the 273 pre-existing tests still pass (re-pointed where their subject moved).
2. `mypy --strict tlgr/models tlgr/ops tlgr/core tlgr/registry.py tlgr/schema.py` is clean; `ruff check` and `ruff format --check` are clean.
3. `tlgr message send|list|get|delete|search|edit|forward|pin|read|react` and `tlgr draft set|clear|list` are generated from the registry, and their `--json` output is byte-compatible with the shapes documented in `AGENT.md` **except** for documented additions (`date` is now RFC-3339 with a `date_unix` sibling; `media_*` keys are additionally available under `media`). A compatibility test asserts every documented key still exists.
4. `tlgr chat list --json`, `tlgr message list --json @x`, `tlgr --json message list @x` all work (globals anywhere).
5. `tlgr --json message search @x "سلام"` succeeds against a live daemon fixture (COR-04 regression).
6. `ls -l ~/.tlgr/daemon.sock` shows `srw-------`; a connection from another uid is refused with a log line; the permission audit refuses to start on a 0644 session file.
7. Two `tlgr` processes started simultaneously with no daemon result in exactly one daemon (stress test: 20 concurrent spawns).
8. Killing the network for 60 s puts the account in `degraded`, requests return exit 8 with a hint (never `Cannot send requests while disconnected`), and on recovery the account returns to `online` and `catch_up()` runs.
9. A revoked session moves the account to `needs_login` within one request, `/v1/status` says why, and `message send` returns exit 4.
10. `tlgr message list --all @big-channel --json` streams NDJSON, terminates, and issues requests at the configured `read` rate.
11. `GET /v1/events` delivers a `message_new` within 2 s of a message arriving in the fake world, heartbeats every 15 s, replays from `since`, and reports a `gap` when `since` is too old.
12. A message sent through `/v1/op` produces a `self_origin: true` event.
13. The webhook delivers a real `message_new` payload (with `datetime` and media) to a local test server, with a valid HMAC signature, and dead-letters with 0600 on failure.
14. Every row of the §7.2 error table is reproduced end-to-end (raise in the fake client → CLI exit code).
15. `tlgr schema --json` validates as JSON Schema draft 2020-12 and contains request+response schemas and an example for every registered op, `message send` included (COR-33).
16. `make docs` and `make parity` produce no diff; `docs/reference/message.md` exists and is generated.
17. `tlgr agent parity --json` reports `messages_core` at ≥ 95 % with the remaining ids waived and named.
18. `tlgr --enable-commands message.list message send @x hi` exits 6 from the **daemon** (verified by calling `/v1/op` directly with the policy set), and so does the alias form `tlgr --enable-commands message.list send @x hi`.
19. Upgrading the daemon protocol under a running old daemon triggers exactly one restart with a message on stderr, and `--no-daemon-restart` yields exit 11.
20. `tlgr daemon stop` drains a 30 s in-flight request instead of killing it, and the account state files are written.

### 12.4 Compatibility rules during migration

* `build_cli()` composes the generated tree with `tlgr/cli/legacy/*`; a start-up assertion fails the build if a group is defined in both places.
* `tlgr/ipc_client.py` remains as a shim (`legacy_request()`) over the new transport, so unmigrated commands immediately gain proper encoding, timeouts and error mapping. It is deleted in PR-12.
* The daemon serves `/v1/*` **and** the v1 routes until PR-12; both go through the same middleware chain (auth, policy, account, error mapping), so the security and correctness fixes are global from day one.
* Every migrated op declares `legacy_paths`, which become aliases, so no documented command path ever disappears. A test asserts that the set of invocable paths is a superset of v1's 94.

**The complete list of deliberate output changes** (everything else is additive; `test_agentmd_compat.py` asserts every key documented in v1's `AGENT.md` still exists with the same type):

| Change | v1 | v2 | Mitigation |
|---|---|---|---|
| Timestamps | `"2025-03-06 12:00:00+00:00"` | `"2026-09-02T09:14:07Z"` + `date_unix` | RFC-3339 parses everywhere `str(datetime)` did not; `[defaults] legacy_dates = true` restores the old form for one minor release |
| `chat get` / `draft list` ids | raw entity id (`123`) | marked id (`-100…123`) + `raw_id` | this was COR-10, a bug; `raw_id` carries the old value |
| Default `parse_mode` | `md` (silently ate `_`, `*`, backticks) | `none` | `[defaults] parse_mode = "md"` restores it; `--parse md` is explicit |
| `--results-only` on a scalar result | printed a bare `2` | prints the result object | this was COR-18; `--select` covers the scalar case |
| Error envelope | `{"error","code","exit_code"}` on stdout | `{"ok":false,"error":{…}}` with the same three keys inside `error` | `--results-only` emits the inner `error` object, matching v1 exactly |
| `message list` / `search` envelope | `{"messages":[…],"has_more":…}` | `{"ok":true,"result":[…],"page":{…}}` | `--results-only` yields `Page[T]`; `messages` is kept as a deprecated alias key for one minor release |

Every one of these is listed in `CHANGELOG.md` under "Breaking" with the migration line, and `tlgr agent whoami --json` reports `output_schema_version: 2` so an agent can branch.

### 12.5 PR sequence after the foundation

Each PR is self-contained: ops + models + tests + generated docs + parity delta + deletion of the group's legacy module and routes.

| PR | Scope | Catalog domain (P0) | Why here |
|---|---|---|---|
| **1** | Foundation + `message`, `draft` | messages_core (28) | the model must be proven on the busiest group |
| **2** | `auth`, `account`, `session`, `security`, `password` | auth_sessions_security (8) | unblocks unattended login (QR, `--password-env`), closes SEC-03, makes every later PR testable against a real account |
| **3** | `chat`, `folder`, dialogs | dialogs_chats (25) | second-largest P0 block; closes COR-01/03/10/20/25 properly |
| **4** | `events`, `watch`, `daemon`, `sync`, `proxy`, `export` + full `EVENTS.md` taxonomy + gateway on the bus | updates_sync_network (21) | turns the foundation's bus into a first-class surface; retires the polling `watch` |
| **5** | `contact`, `user`, blocking | contacts_users (17) | closes COR-26/27/28; the resolver's real workout |
| **6** | `media`, `sticker`, `gif`, `emoji` | media_files (14) | the file pipelines get their ops |
| **7** | `chat member/admin/invite/topic/permission`, `chat admin-log` | groups_channels_admin (14) | needs the `Rights` model and the participants cursor from PR-3 |
| **8** | `story` | stories (14) | self-contained; depends on media |
| **9** | `poll`, `reaction`, `todo`, `location`, `link` | polls_reactions_content (10) | reactions already modelled; poll/todo need new models |
| **10** | `bot`, `inline`, `webapp`, `payment` | bots_inline_payments (13) | largest P2/P3 tail; needs `Conversation`-style helpers |
| **11** | `call`, `vc`, `conference` | calls_voicechats (10) | signalling and metadata only |
| **12** | `profile`, `privacy`, `notify`, `settings`, `business`, `premium`, `gift`, `stars` + **final cleanup** | profile_settings_privacy (4) | biggest P3 tail; last PR deletes `ClientWrapper`, `daemon/ipc.py`, `ipc_client.py` and the v1 routes, and flips the parity gate to "no waivers" |

Rolling gates: after PR-4 the coverage floor rises to 85 %; after PR-8 to 90 %; P0 parity must be 100 % before 2.0.0 final, P1 ≥ 90 %.

---

## 13. Open questions and risks

| # | Question / risk | Recommendation |
|---|---|---|
| 1 | **Telethon private APIs.** `_save_states_and_entities`, `_borrow_exported_sender`, `_message_box`, `_handle_auto_reconnect`, `MessageBox.apply_difference` are private and could change. | Pin `telethon==1.44.*` (a `~=` minor pin, not `>=`), isolate every private access in `core/telethon_compat.py` behind a feature probe, and add `test_telethon_compat.py` that asserts each symbol exists. Degrade loudly (one WARN per symbol, a `/v1/status` warning) rather than crashing when one disappears. |
| 2 | **`*TooLong` detection is a wrapper around internals.** | Ship both detectors: the `MessageBox` wrapper *and* a `logging.Handler` attached to `telethon._updates.messagebox` matching the known message. If neither is available, fall back to a periodic dialog-consistency check (`top_message`/`unread_count` vs. what we dispatched). Do not silently pretend catch-up is complete. |
| 3 | **Exit-code taxonomy divergence.** The error-taxonomy research proposes richer codes (file-reference, payment, premium, frozen, migration = 11–17); tlgr's documented table is 0–13 with different meanings. | Keep the documented 0–13 table (it is a compatibility contract, and fixed decision 8 says so). Express the finer distinctions in `error.code` + `error.hint`, which are free-form and machine-readable. Revisit only at 3.0 with a `--exit-code-scheme` flag if agents ask for it. |
| 4 | **`msgspec` has no ARM/musl wheel for some Python versions.** | It ships wheels for CPython 3.9–3.13 on the platforms in the CI matrix; 3.14 may lag. Gate 3.14 in CI as `continue-on-error` until wheels exist, and keep the dependency at `msgspec>=0.18,<1.0`. There is no pure-Python fallback and none is wanted — a second validation library would defeat the purpose. |
| 5 | **`--all` inside the daemon can run for tens of minutes** and holds a connection. | Cap it: `[limits] all_max_items` (default 100,000) and `all_max_seconds` (default 1800); emit a final `end` frame with `truncated: true` and a resumable `next_cursor` rather than running forever. Count it as activity so idle-stop cannot kill it. |
| 6 | **Event ring buffer sizing.** 4,096 events is minutes on a busy account; a consumer that disconnects for an hour gets a `gap`. | Keep the in-memory ring for latency, and add an optional on-disk spool (`[daemon] event_spool_days`, default 0 = off) writing NDJSON per account per day. Recommend webhooks (push, at-least-once, dead-lettered) for consumers that cannot afford gaps. |
| 7 | **Two tlgr installs, one `~/.tlgr`** (e.g. a venv and a pipx install). | The `daemon.lock` + protocol handshake already prevents two daemons and mismatched clients. Additionally write `daemon.state.executable`; when it differs from `sys.executable`, warn once. Do not try to be clever beyond that. |
| 8 | **Policy is not a hard sandbox** (anything that can reach the socket can read the session file). | Document it plainly in `SECURITY.md` and in `--enable-commands --help`. Offer the stronger form — `~/.tlgr/policies/<name>.toml` bound to a token the agent gets but cannot rewrite — and recommend a separate OS user for genuinely untrusted agents. |
| 9 | **Presence and read receipts are socially visible.** An agent that reads a chat clears the *owner's* unread badge. | Keep `presence.mode = off` by default, keep `chat open --no-read` and `message list` as the silent paths, and keep `chat unread` as the undo. Mark every op that emits a visible signal with `tags={"visible-to-others"}` and print it in `--help` and the reference docs. |
| 10 | **Media metadata without `hachoir`/`ffprobe`** produces 1×1 videos. | Ship `[media]` as an extra (pillow + hachoir), probe `ffprobe` on `PATH`, accept explicit `--duration/--width/--height`, and emit a `meta.warnings` entry naming the fix when none is available. Never fail the send. |
| 11 | **Layer 227 vs 229.** tdesktop is already at 229; communities, ephemeral messages and the new keyboard model will land upstream. | Stay on Telethon 227. Keep `core/custom_tl.py` as a documented, tested escape hatch. Re-run the layer diff each Telethon release; treat a layer bump as its own PR with a full contract-test run, because changed constructor ids (`user`, `channel`, `keyboardButton`, `replyInlineMarkup`) break parsing. |
| 12 | **500 ops × 13 contract tests = ~6,500 parametrised tests.** | They are microseconds each (no I/O), but keep them fast by construction: no network, no session files, module-scoped registry fixture. Split the suite into `-m contract` and `-m unit` so contributors can run one group quickly. Budget: the full suite under 60 s. |
| 13 | **Catalog drift.** The catalog is a research artefact; Telegram ships features weekly. | Version it (`catalog_version` in the index and in the parity report), regenerate it per release, and treat "new uncovered ids appeared" as a release-note item rather than a build failure — the P0 gate is on the pinned version. |
| 14 | **Human table rendering for 500 heterogeneous ops.** | `columns` is per op and lint-checked, so the default is always sensible; `--columns` and `--wide` cover the rest. Resist adding a template language. |
| 15 | **Migration window risk:** two dispatch paths coexist for 11 PRs. | Both go through the same middleware, and `test_legacy_parity.py` asserts the invocable command set never shrinks. Set a hard rule: no new hand-written command may be added to `cli/legacy/` after PR-1 — new work goes in the registry or it does not go in. |

---

## Appendix A — audit finding → resolution index

| Finding | Where it is answered |
|---|---|
| COR-01 mute epoch | §12.2 (quick fix), §3.4 `NotifySettings`, PR-3 |
| COR-02 account routing | §5.2, §6.1, §12.2 |
| COR-03 `create_group` | PR-3 (`messages.CreateChatRequest`) |
| COR-04 query encoding | §5.1, §12.2 |
| COR-05 search cursor | §3.5, §6 pagination, foundation (message) |
| COR-06 error mapping | §7 |
| COR-07 webhook JSON | §6.5, §12.2 |
| COR-08 idle vs webhook | §6.10 |
| COR-09 polling watch | §5.4, §6.5, PR-4 |
| COR-10 marked ids | §3.1, §3.2, §6.6 |
| COR-11 idle vs in-flight | §6.10 |
| COR-12 connect race | §6.2, §6.1 |
| COR-13 dead connections | §6.2, §6.3 |
| COR-14 daemon races | §5.8, §6.1 |
| COR-15 `--flood-wait-max` | §5.2, §6.4 |
| COR-16 dead flags | §9 (`confirm.py`), §4.4 |
| COR-17 `--dry-run` | §4.1, §11.3 |
| COR-18 `--results-only`/`--select` | §9 |
| COR-19..29 | per-group PRs; models in §3 make the shapes uniform |
| COR-30 request validation | §5.2 |
| COR-31/32 transport | §5.1, §5.3 |
| COR-33 schema examples | §4.1, §4.6, §11.3 |
| COR-34 whoami | `ops/agent.py`, §12.1 |
| COR-35 timestamps | §3.1 |
| COR-36 exit 3 | §4.1 `empty_exit` |
| COR-37/38 status & handshake | §5.5, §5.7 |
| COR-39 launchd | §6.12 |
| COR-40..46 | §6.1 logging, §4 generated tree, §4.4 flags |
| SEC-01..10 | §8.2 |
| ROB-01..09 | §5.3, §6.4, §6.5, §6.7, §6.9 |
| MNT-01..06 | §4, §3, §11, §2 |
| PKG-01..05 | §11.4, §4.6 |
| UX-01..09 | §4.4, §9, §7 |

## Appendix B — MTProto checklist coverage

| Checklist item (protocol notes §9) | Section |
|---|---|
| 1 `catch_up=True` + warm channel hashes | §6.2, §6.3 |
| 2 catch up after reconnect / clock jump | §6.3 |
| 3 per-account supervisor + fatal-auth classification | §6.2, §6.3, §7.2 |
| 4 single owner per session file | §6.2, §8.2 |
| 5 honest identity | §6.2, §10.2 `[identity]` |
| 6 surface real errors (`raise_last_call_error`) | §6.2, §7.2 |
| 7 outbound rate limiting + persisted flood memory + breaker | §6.4 |
| 8 idempotent sends (`random_id` journal) | §6.4 |
| 9 resync on `*TooLong` | §6.3, risk 2 |
| 10 retry failed differences with backoff | §6.3 |
| 11 entity resolution service | §6.6 |
| 12 config freshness (`UpdateConfig`, `getAppConfig`) | §6.3 |
| 13 download pipeline | §6.7 |
| 14 upload pipeline | §6.7 |
| 15 event streaming instead of polling | §5.4, §6.5 |
| 16 login flows (QR, resend, email, never sign-up) | §6.8, PR-2 |
| 17 logout & session hygiene | §6.8, §8.2, PR-2 |
| 18 presence policy | §6.13 |
| 19 state save cadence | §6.3, §6.11 |
| 20 takeout for bulk export | PR-4 (`export`) |
| 21 transport/network options | §10.2 `[network]` |
| 22 hash-based list caching | §6.6, `~/.tlgr/cache/` |
| 23 test-DC mode | §11.2 (live tests) |
| 24 clock sanity | §6.3 (`time_offset` warning in `/v1/status`) |
| 25 session security | §8.2 |
