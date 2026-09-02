# tlgr production-readiness audit

Repo: `/Users/p4/Projects/tlgr` (branch `v2`, HEAD `78a4934`), 7,866 lines under `tlgr/`, 3,167 lines of tests.
Baseline: `.venv/bin/python -m pytest` → **273 passed in 0.29s**. Python 3.12.13, Telethon 1.44.0 (layer 227), aiohttp 3.14.3, Click 8.5.0.

Every file under `tlgr/` and `tests/` was read in full, plus README.md, AGENT.md, CONTRIBUTING.md, pyproject.toml, requirements.txt, `.gitignore`, the three `*.example.*` files, the four package READMEs and `.github/`. Findings marked **[verified]** were reproduced by executing code against the installed Telethon/aiohttp (with the IPC layer stubbed, or against a throw-away aiohttp `UnixSite`); the rest are established by reading the code paths cited. No daemon-routed command was executed against the user's live accounts.

Severity: **S0** blocker · **S1** major · **S2** minor · **S3** nit. IDs match `tlgr_audit.json`.

---

## 0. Executive summary

tlgr is a well-intentioned agent-first Telegram CLI with a genuinely good core (the dialog-status / posters / media-kind work in `core/client.py` is careful and well tested). But the transport, daemon and command layers were built by hand, one endpoint at a time, and they have not been exercised end-to-end by tests. The consequences are concrete:

| ID | Sev | One line |
|---|---|---|
| SEC-01 | S0 | `daemonize()` sets `umask(0)`: the IPC socket is `srwxrwxrwx` on this machine, the IPC has no authentication, so any local process/user can operate every connected Telegram account. |
| COR-01 | S0 | `chat mute` computes `mute_until` from the asyncio monotonic clock → a date in January 1970 → mute is a no-op. |
| COR-02 | S0 | With `-a` omitted the daemon serves whichever account happens to be first in a `set` → nondeterministic across restarts; `account switch` has no effect on daemon-routed commands. |
| COR-07 | S0 | Webhook `new_message`/`message_edited` pushes always fail: `_serialize_event` embeds `Message.to_dict()` (datetime/bytes) and aiohttp's `json=` raises before any I/O → 3 retries, then dead-letter. The webhook feature does not work for message events. |
| COR-04 | S1 | GET query strings are not URL-encoded → any search/chat argument with a space, `#`, `&` or **non-ASCII text (Persian)** yields HTTP 400 from aiohttp. |
| COR-03 | S1 | `chat create --type group` calls `TelegramClient.create_group`, which does not exist → AttributeError → exit 12. |
| COR-05 | S1 | `message search --cursor` is ignored server-side → every page is page 1, `has_more` never turns false. |
| COR-06 | S1 | All errors except FloodWait/PeerFlood/FROZEN — including tlgr's own `ChatNotFoundError` — come back as HTTP 500 `IPC_ERROR`, exit 12. Exit codes 4/5/6 are unreachable through the daemon. |
| COR-08 | S1 | Webhook-only deployments are idle-stopped after 30 min because the idle monitor only counts jobs. |
| SEC-03 | S1 | Any `-a <string>` is turned into a filesystem path and `mkdir`'d by the daemon (`../x` escapes; junk `0777` dirs already exist under `~/.tlgr/accounts/`). |
| SEC-04 | S1 | `tlgr logout` / `account remove` never call `auth.logOut`; the session stays authorized on Telegram's side. |
| MNT-01 | S1 | 37 IPC handlers, 44 hand-registered routes, 40 hand-declared `--account` options, 16 hand-built query strings, a hand-kept example dict and two hand-written docs per operation. This cannot scale to 500 commands. |
| MNT-03 | S1 | Zero tests for `ipc_client`, the IPC routes, daemon lifecycle, webhook, job runner, TOML config, launchd, and all CLI modules except `account`/sandbox. |
| PKG-01 | S1 | No CI, no lint/format/type configuration, no changelog. |

Total findings: 85 (S0: 4, S1: 20, S2: 44, S3: 17) — correctness 46, security 10, robustness 9, UX 9, maintainability 6, packaging/docs 5. Full list with the same IDs in `tlgr_audit.json`.

---

## 1. Architecture as built

```
tlgr <group> <cmd> [args]           click, tlgr/cli/*.py (16 modules, 93 leaf commands incl. `msg` alias + 10 shortcuts)
   │  ctx.obj = {fmt, account, results_only, select, dry_run, ...}   (tlgr/cli/__init__.py:137-163)
   │  resolve_account()  (cli/_common.py)          builds f-string query or JSON body by hand
   ▼
tlgr/ipc_client.py                  hand-rolled HTTP/1.1 over AF_UNIX; ensures daemon (pid file + fork) → recv-until-EOF → parse
   ▼   ~/.tlgr/daemon.sock
tlgr/daemon/ipc.py  IPCServer       aiohttp UnixSite, 44 routes, one coroutine per route, all identical shape:
   │                                 body/query → ensure_client(account) → try: client.X() → _json_response / _handle_exception
   ▼
tlgr/daemon/server.py DaemonServer  _clients: dict[alias, ClientWrapper]; connects accounts at start; webhook handlers on Telethon
   │                                 events; JobRunner(Gateway jobs from jobs.yaml); idle monitor; SIGTERM
   ▼
tlgr/core/client.py ClientWrapper   Telethon TelegramClient + ~45 async methods returning dict[str, Any]
   ▼
Telethon 1.44 (layer 227) ── MTProto
Side paths: tlgr/daemon/webhook.py (POST + retry + dead-letter), tlgr/gateway/* (filters → processors → actions),
            tlgr/cli/watch.py (polls /chat/list + /message/list every 2 s), tlgr/daemon/launchd.py, lifecycle.py (double fork)
Config: ~/.tlgr/{config.toml, accounts.json, accounts/<alias>/{config.json,session.session}, jobs.yaml, webhook.toml, daemon.{pid,sock}}
```

Key properties of the as-built design:

* Every operation is expressed **five times**: the CLI command (params + query builder + columns), the IPC route, the IPC handler, `ClientWrapper` method, and the `EXAMPLE_RESPONSES` entry — plus README.md and AGENT.md prose. Nothing checks they agree.
* The wire protocol is untyped: `dict[str, Any]` in, JSON out; query strings for reads, JSON bodies for writes, chosen per endpoint.
* There is no event stream between daemon and CLI; `watch` polls.
* Errors are collapsed to three codes at the daemon boundary.
* Account selection is resolved twice (CLI: positional/`-a`/`TLGR_ACCOUNT`; daemon: "first connected") with different answers.

---

## 2. Correctness

### COR-01 · S0 · `chat mute` uses the event-loop monotonic clock as a Unix timestamp **[verified]**
`tlgr/core/client.py:764`
```python
mute_until = 2**31 - 1 if duration is None else int(asyncio.get_event_loop().time()) + duration
```
`loop.time()` is `time.monotonic()` (seconds since boot). Measured on this host: `loop.time()=1057057` → `1970-01-13`; a 1-hour mute is sent as a date **20,686 days in the past**. Telegram interprets `mute_until` as "Date until which all notifications shall be switched off" (`docs/constructor/inputPeerNotifySettings.md`), so the chat is never muted while the CLI reports `{"muted": true}`.
**Fix:** `int(time.time()) + duration`; add `chat unmute` (`mute_until=0`) and return the effective `mute_until` as RFC-3339. Add a unit test with a fake client asserting the request's `mute_until` is within `duration±5` of `time.time()`.

### COR-02 · S0 · Account routing with `-a` omitted is arbitrary and ignores `account switch`
`tlgr/daemon/server.py:49-54` (`get_client('')` → `next(iter(self._clients.values()))`), `:256-275` (`accounts_needed: set[str]` iterated to connect → dict insertion order = set order = hash-seed dependent), `tlgr/cli/_common.py:36` (`acct = account or ctx.obj["account"] or ""` — the active account from `accounts.json` is **never** consulted), `tlgr/cli/__init__.py:156`.
Consequences: (a) when jobs reference several accounts, `tlgr message send @x hi` can send from a different account after every daemon restart; (b) `tlgr account switch work` changes nothing for daemon-routed commands until the daemon restarts, while `agent whoami` reports `work` — the runbook lies. `require_account=true` mitigates only if the operator knows to set it.
**Fix:** resolve the alias fully in the CLI (positional → `-a` → `TLGR_ACCOUNT` → `[accounts].default` → `accounts.json.active`) and always send it; the daemon must reject `account: ""` with `ACCOUNT_REQUIRED` (HTTP 400). Never pick "first". Make `accounts_needed` an ordered list.

### COR-03 · S1 · `chat create --type group` calls a Telethon method that does not exist **[verified]**
`tlgr/core/client.py:730-731`: `await self.client.create_group(name, users)` — `hasattr(TelegramClient, "create_group") is False` in Telethon 1.44. Group creation raises `AttributeError` → HTTP 500 → exit 12. The `result.id if hasattr(result,"id") else 0` fallback (the "id 0" symptom) is dead code below a crash.
**Fix:** `messages.CreateChatRequest(users=[input_users], title=name)` (`messages.createChat` — present in `mtproto_methods.json` and as `messages.CreateChatRequest` in Telethon 227); take `Updates.chats[0]` and return `utils.get_peer_id(chat)`. Members must be resolved with `get_input_entity` first; report `UserPrivacyRestricted` per member instead of failing the whole call.

### COR-04 · S1 · GET query strings are built by string concatenation with no URL-encoding **[verified]**
Builders: `tlgr/cli/message.py:103,175`, `chat.py:39-47,164-170,289`, `contact.py:25,109`, `user.py:25,88`, `draft.py:67`, `profile.py:23`, `watch.py:38,48`; request line assembled at `tlgr/ipc_client.py:107-109`. Against a real aiohttp `UnixSite`:

| input | result |
|---|---|
| `--search "a b"` | HTTP 400 `Bad status line` |
| `search "issue #12"` | HTTP 400 |
| `search "سلام"` (Persian) | HTTP 400 `Invalid char in url query` |
| `search "fish&chips"` | silently split into two params |
| `search "a+b"` | received as `a b` |

Any non-ASCII search — the primary language of this deployment — cannot be executed. Usernames/refs with `#` or spaces also fail.
**Fix (immediate):** `urllib.parse.urlencode(params, quote_via=urllib.parse.quote)` in one helper; **(structural)** stop using query strings: one `POST /v1/op` with a JSON body for every operation (see §8).

### COR-05 · S1 · `message search --cursor` is ignored by the daemon
CLI encodes `offset_id` into the query (`tlgr/cli/message.py:173-177`), but `_message_search` (`tlgr/daemon/ipc.py:227-243`) never reads it and `search_messages` (`tlgr/core/client.py:565-591`) has no `offset_id` parameter. Every page returns the same first `limit` hits; `add_pagination` sets `has_more=True` whenever `len==limit`, so a `while has_more` loop never terminates. `messages.search` supports `offset_id` (`docs/method/messages.search.md`: "Only return messages starting from the specified message ID").
**Fix:** thread `offset_id` through to `iter_messages(chat, search=query, offset_id=offset_id, limit=limit)`; for `--local` also pass `offset_id`. Add a fake-client test that asserts page 2 starts after page 1's last id.

### COR-06 · S1 · Error mapping collapses everything to `IPC_ERROR` / exit 12 **[verified]**
`tlgr/daemon/ipc.py:47-62` maps only `FloodWaitError`, `PeerFloodError` and RPC errors containing `FROZEN`. Measured:

| raised in daemon | HTTP | code | CLI exit | should be |
|---|---|---|---|---|
| `ChatNotFoundError` (tlgr's own) | 500 | IPC_ERROR | 12 | CHAT_NOT_FOUND / 5 |
| `PermissionError_`, `SessionError`, `TlgrError` | 500 | IPC_ERROR | 12 | 6 / 4 / 1 |
| `ChatAdminRequiredError`, `ChatWriteForbiddenError`, `UserPrivacyRestrictedError`, `UserIsBlockedError` | 500 | IPC_ERROR | 12 | PERMISSION_DENIED / 6 |
| `AuthKeyUnregisteredError`, `SessionRevokedError`, `UserDeactivatedBanError` | 500 | IPC_ERROR | 12 | SESSION_ERROR / 4 |
| `UsernameNotOccupiedError`, `MessageIdInvalidError` | 500 | IPC_ERROR | 12 | NOT_FOUND / 5 |
| `MessageNotModifiedError`, `MessageEmptyError` | 500 | IPC_ERROR | 12 | NOT_MODIFIED (0) / USAGE (2) |
| `asyncio.TimeoutError`, `ConnectionError("Cannot send requests while disconnected")` | 500 | IPC_ERROR | 12 | RETRYABLE / 8 |
| `ensure_client` → `None` (no account configured) | 404 | IPC_ERROR | 12 | CONFIG/AUTH (10/4) |

AGENT.md promises exit 4/5/6; only `account import` (4) and the CLI-side `require_account` (2) can produce them today. The `hint` strings in `errors.py` are also lost (`ipc_client.py:146-152` rebuilds a bare `IPCError`).
**Fix:** a single mapping table (see §8.6) applied in the daemon, transported as `{code, exit_code, http, retryable, wait_seconds?, rpc, hint}` and re-raised by the client as the matching `TlgrError` subclass. Contract-test each row.

### COR-07 · S0 · Webhook message pushes always fail **[verified]**
`tlgr/daemon/server.py:370-374` adds `data["raw"] = msg.to_dict()`; a Telethon `Message.to_dict()` contains `datetime` and (for media) `bytes` (`file_reference`). `WebhookPusher.push` calls `self._session.post(url, json=payload)` (`tlgr/daemon/webhook.py:89-94`); aiohttp's default serializer is `json.dumps`, which raises `TypeError: Object of type datetime is not JSON serializable` **before any network I/O** (reproduced with a live `ClientSession`). The exception is caught by the generic `except`, retried `max_attempts` times with backoff, then written to `dead_letter.jsonl` (that path uses `default=str`, so the dead letter succeeds — which hides the bug). Net effect: `new_message` and `message_edited` never reach the webhook; `message_deleted`/`chat_action`/`message_read` (no `raw`) do.
**Fix:** drop `raw` (or serialize it explicitly: `bytes`→base64, `datetime`→RFC-3339) and post `data=json.dumps(payload, default=_json_default).encode()`; log the serialization failure as a bug, not a delivery failure; add a test that `_serialize_event` output round-trips through `json.dumps` with no `default=`.

### COR-08 · S1 · Idle auto-stop kills webhook-only daemons
`tlgr/daemon/server.py:229-240` stops when there are no *running jobs* and no IPC for `idle_timeout` (default 1800 s). The webhook pusher and any `watch` client are not jobs, so a webhook-only deployment (the README's headline "agentic" setup) silently dies 30 minutes after the last CLI call and no events flow until something invokes the CLI again. Under launchd the exit code 0 also means it is not restarted (see COR-41).
**Fix:** count enabled webhook, open event streams and in-flight requests as activity; set `idle_timeout=0` automatically when webhook is enabled; document.

### COR-09 · S1 · `watch` is a polling loop with a spurious backlog and flood risk
`tlgr/cli/watch.py:29-71`: (a) first iteration has `last_ids` empty → up to 10 historical messages per chat are emitted as `new_message` events; (b) it sends `min_id=` which `/message/list` does not support (`ipc.py:183-201`), so every poll re-fetches 10 messages per chat; (c) default target is the 20 most recent dialogs polled every 2 s → ~10 `messages.getHistory` calls/s → FloodWait within minutes; (d) `except Exception: pass` hides daemon death (infinite silent loop); (e) `--events` other than `new_message` are accepted and ignored; (f) `--chat @user` uses the ref as the dedup key, so the same chat via id and username is tracked twice. The daemon already receives every update via Telethon; none of this is necessary.
**Fix:** daemon-side event bus + `GET /v1/events` NDJSON stream with `since=<seq>` backfill (see §8.4). Interim: seed `last_ids` from the first poll without emitting, honor `min_id` server-side, poll ≥10 s.

### COR-10 · S1 · `chat get` / `draft list` return raw entity ids while `chat list` returns peer ids **[verified]**
`tlgr/core/client.py:316-353`: with `dialog=None` the id is `entity.id`. For `Channel(id=123)` `chat get` returns `123` while `chat list` returns `-1000000000123`; for a legacy `Chat` `55` vs `-55`. `list_drafts` (`:670`) has the same defect. An agent that feeds `chat get`'s id into `message send` addresses a *user* with that id.
**Fix:** always emit `utils.get_peer_id(entity)`; add `peer_type` and `raw_id` fields; test both branches.

### COR-11 · S1 · Idle monitor can shut the daemon down while a long request is running
`_touch_middleware` (`tlgr/daemon/ipc.py:84-87`) stamps `_last_ipc_time` at request **start**; there is no in-flight counter. A `media download` or `chat posters --max-messages 20000` (CLI timeout 600 s) that outlives `idle_timeout` is killed mid-transfer by `_idle_monitor` (`server.py:229-240`), which disconnects all clients.
**Fix:** in-flight counter (increment before handler, decrement in `finally`, touch on completion); refuse shutdown while `> 0`; shutdown drains with a deadline.

### COR-12 · S1 · On-demand connect race in `ensure_client`
`tlgr/daemon/server.py:56-85`: two concurrent requests for a not-yet-connected alias both call `_connect_account`, creating two `TelegramClient`s on the same SQLite session (`database is locked` / `AuthKeyDuplicatedError` risk); the second assignment to `_clients[alias]` orphans the first connection.
**Fix:** `asyncio.Lock` per alias; double-check after acquiring.

### COR-13 · S1 · Dead connections are reported but never healed
After Telethon exhausts `connection_retries=5` the wrapper stays in `_clients`; `ensure_client` returns it (`server.py:58-60`) and every request fails with "Cannot send requests while disconnected" → exit 12 forever. `status()` now says `healthy:false` (`server.py:206-224`), but nothing acts on it.
**Fix:** supervisor task: every 30 s reconnect any wrapper with `is_connected is False` (exponential backoff, cap); `ensure_client` should await reconnect before returning; surface `RETRYABLE` while reconnecting.

### COR-14 · S1 · Daemon auto-start / stale-PID races produce two daemons
`tlgr/ipc_client.py:38-62` and `tlgr/daemon/server.py:390-396,316-321`: (a) two CLIs started concurrently with no daemon both spawn one; the second's `read_pid()` check races the first's `write_pid()` (which happens inside `run()` after import), so both proceed; the second `os.unlink(sock_path)`s the first's socket and overwrites the PID; both hold the same session file. (b) `_auto_start_daemon` waits ≤ ~20 s for the socket, but the socket is bound only **after all accounts are connected** (`run()` line order), so a slow cold start makes the client `unlink` the live daemon's PID file and spawn another. (c) `_daemon_is_running` treats `PermissionError` as "not running" and deletes the PID file. (d) PID reuse: a foreign live PID makes the CLI report "running" and skip auto-start.
**Fix:** `fcntl.flock` on `daemon.lock` held for the daemon's lifetime (both the CLI probe and `main()` try the lock); bind the socket first and expose `ready:false` in `/daemon/status` until accounts connect; never unlink another process's PID/socket.

### COR-15 · S2 · `--flood-wait-max` is a no-op
`tlgr/cli/__init__.py:110,160` stores it in `ctx.obj`; no command or IPC payload carries it; the daemon uses `config.daemon.flood_wait_max` at connect time (`server.py:76,250`). `output_json(flood_wait=...)` is never called with a value, so the documented `flood_wait` field never appears.
**Fix:** send it per request; Telethon's `client(request, flood_sleep_threshold=...)` (`telethon/client/users.py:29`) supports per-call thresholds; report `flood_wait_slept` in the envelope.

### COR-16 · S2 · `--force/-y`, `--no-input`, `--verbose` are dead flags
No module reads `ctx.obj["force"]` or `ctx.obj["no_input"]`. `account remove` uses `click.confirmation_option` (`tlgr/cli/account.py:195`), so `--no-input` neither suppresses nor auto-fails the prompt (it aborts on EOF with exit 1, not 2). `--verbose` enables DEBUG logging but the CLI emits no log records.
**Fix:** central `confirm()` helper honoring `--force/--no-input`; log IPC request/response at DEBUG.

### COR-17 · S2 · `--dry-run` is honored by 9 commands and ignored by 12 mutating ones **[verified]**
With `--json --dry-run`, a real IPC call is made by: `chat open` (sends a read receipt), `chat mute`, `chat create`, `chat typing`, `chat unread`, `message pin`, `message react`, `message read`, `contact add`, `user hide-stories`, `profile update`, `media upload` (also `account remove`, `job remove/enable/disable`). Honored by `message send/delete/edit/forward`, `chat archive/leave`, `contact rename/remove`, `draft set/clear`. The flag is also global-only (`tlgr message pin @x 1 --dry-run` is a usage error).
**Fix:** declare `mutating=True` per operation in the registry and short-circuit in one dispatcher; never in each command.

### COR-18 · S2 · `--results-only` / `--select` semantics are surprising and the documented example returns `{}` **[verified]**
`tlgr/core/output.py:26-41`: `--results-only message delete` prints the bare integer `2`; `--results-only daemon status` prints the `accounts` list (first list-valued key); `--select` applies to the top-level envelope, so README.md:329's `tlgr --json --select "id,name" chat list` prints `{}`. AGENT.md:15 documents `--select` as a field projection without mentioning that `--results-only` is required first.
**Fix:** uniform envelope `{"ok":true,"op":..., "result": <obj|list>, "page": {...}}`; `--select` projects into `result` (recursing into lists); `--results-only` returns `result` verbatim.

### COR-19 · S2 · `message delete` reports `len(msg_ids)` regardless of what was deleted
`tlgr/core/client.py:561-563` reads `pts_count` off the return of `client.delete_messages`, which in Telethon is a **list** of `AffectedMessages` (`telethon/client/messages.py`: `return await self([...DeleteMessagesRequest...])`), so `getattr(list, "pts_count", len(msg_ids))` always falls back.
**Fix:** `sum(r.pts_count for r in result)`; also expose `--for-me` (`revoke=False`).

### COR-20 · S2 · `chat leave` on a private/bot dialog is a silent no-op that reports success
`tlgr/core/client.py:771-779` handles `Channel` and `Chat` only; a `User` entity falls through and returns `{"left": true}`.
**Fix:** raise `USAGE` ("not a group/channel"), or offer `chat delete-history` (`messages.deleteHistory`).

### COR-21 · S2 · Default Markdown parse mode silently alters sent text **[verified]**
`client.send_message`/`edit_message` (`tlgr/core/client.py:385,630`) use Telethon's default `parse_mode='md'`: `"snake_case_var and *star* and `x`"` is sent as `"snake_case_var and *star* and x"` with a code entity — backticks and underscores vanish. Agents sending identifiers, prices (`*`) or code get mangled text; there is no `--parse-mode`/`--raw`.
**Fix:** default `parse_mode=None`; add `--markdown/--html`; expose `--entities` JSON for exact formatting.

### COR-22 · S2 · `chat_id: "@username"` filters never match — the flagship README/example jobs never fire **[verified]**
`tlgr/filters/context.py:57-66` skips `@` refs with a comment "the gateway pre-resolves these"; nothing in `tlgr/gateway/engine.py` resolves anything. `jobs.example.yaml:21` (`chat_id: "@source_channel"`), `README.md:255` (`chat_id: "@raw_feed"`) and `tests/test_config_yaml.py` fixtures all use this form. `config validate` passes them.
**Fix:** resolve usernames to peer ids in `Gateway.setup()` (`contacts.resolveUsername` via `get_input_entity`) and store numeric ids in the node; `config validate` should resolve too (or warn).

### COR-23 · S2 · Temporal filters evaluate in UTC, README "night-mode" example fires at the wrong hours **[verified]**
`tlgr/filters/temporal.py:74-75` formats the message date in UTC; `_parse_date` (:28) assumes naive dates are UTC. A message at 23:30 local (UTC+03:30) is 20:00Z and does **not** match `time_of_day: "23:00-07:00"` (README.md:264, jobs.example.yaml:64).
**Fix:** convert to the daemon's local zone (`datetime.astimezone()`) or a `timezone:` job/config key; document.

### COR-24 · S2 · `user_joined` is actually Telethon `UserUpdate` (typing/online status)
`tlgr/gateway/engine.py:44` and `tlgr/daemon/server.py:131-136` map `user_joined` to `events.UserUpdate`, which fires on presence/typing changes, not joins (those are `ChatAction`). `webhook.example.toml:9-13` additionally lists `user_left` and `reaction`, which exist nowhere (`ALL_EVENT_TYPES`, `gateway/config.py:28-31`).
**Fix:** rename to `user_status`; derive `user_joined`/`user_left` from `ChatAction` (`user_joined`, `user_added`, `user_left`, `user_kicked`); add `reaction` via `events.Raw(UpdateMessageReactions)`.

### COR-25 · S2 · `chat list` always caps at 100 although full enumeration must be one call
`tlgr/cli/chat.py:38` (`effective_limit = limit or 100`) contradicts `ClientWrapper.list_chats`'s own docstring (`client.py:228-238`: paging is O(n²), "enumerate an account ... costs ONE walk"). The CLI offers no way to request `limit=None`; `inbox`/`chats` inherit the cap.
**Fix:** `--all` (limit=None) plus a real server-side dialog cursor (`offset_date/offset_id/offset_peer` of `messages.getDialogs`).

### COR-26 · S2 · `profile get` always returns `bio: ""`
`tlgr/core/client.py:857-866` never calls `users.getFullUser`; the field is hard-coded.

### COR-27 · S2 · `contact search` is a global username search, capped at 50, paginated client-side
`contacts.search` "Returns users found by username substring" (`docs/method/contacts.search.md`) — it is not a search of *my contacts*. `tlgr/core/client.py:847` hard-codes `limit=50`; `tlgr/cli/contact.py:100-125` then slices that capped list, so `--limit 100 --cursor` can never see more than 50 results.
**Fix:** rename to `user search`/`search global`; implement `contact search` as a local filter over `contacts.getContacts`; pass `limit` through.

### COR-28 · S2 · `contact list` re-downloads the whole contact list per page
`tlgr/core/client.py:781-792` (`GetContactsRequest(hash=0)`) + client-side slicing in `contact.py:17-41`. Acceptable for hundreds; wasteful at thousands and inconsistent with the "opaque cursor" contract.
**Fix:** cache with `hash`, or paginate server-side; at minimum document that the cursor is a client-side offset.

### COR-29 · S2 · `account add` leaves half-configured accounts on failure and echoes the API hash
`tlgr/cli/account.py:51-59`: `mgr.add_account(alias)` runs before credentials are validated or login succeeds; a wrong code / `int("abc")` / FloodWait leaves the alias registered ("already exists" on retry, and it may become the *active* account with no session). The API hash is read with `input()` (echoed).
**Fix:** transactional add (register only after `get_me()` succeeds); `getpass` for the hash; map Telethon auth errors (`PhoneNumberInvalidError`, `PhoneCodeInvalidError`, `FloodWaitError`) to codes.

### COR-30 · S2 · IPC handlers have no request validation
`_get_body` swallows malformed JSON (`tlgr/daemon/ipc.py:40-44`) and handlers index `body["chat"]` / `int(q.get("limit", 20))` inside the generic `try`, so a missing field yields HTTP 500 `{"error": "'chat'", "code": "IPC_ERROR"}` and `limit=abc` yields `invalid literal for int()`. Nothing distinguishes a client bug from a Telegram failure.
**Fix:** typed request models validated at the boundary → HTTP 400 `USAGE` with field names.

### COR-31 · S2 · `ipc_request` reports timeouts as "Malformed daemon response" and accepts truncated bodies
`tlgr/ipc_client.py:113-127`: `socket.timeout` breaks the read loop and the partial buffer is parsed; the daemon keeps working. The default 120 s is below what an unpaged `chat list` needs on large accounts (docstring measured 24 s for 708 dialogs). There is no request id to correlate or cancel.
**Fix:** raise `TimeoutError` → `RETRYABLE`; per-operation timeouts in the registry; request ids; daemon-side cancellation on client disconnect.

### COR-32 · S2 · `_decode_chunked` slices characters, not bytes **[verified]**
`tlgr/ipc_client.py:157-178` decodes the body to `str` first, then uses hex chunk sizes (bytes) as character counts; a Persian body `{"t":"سلام"}` decodes to `'{"t":"سلام"}\r\n0\r'`. Latent today (aiohttp sets `Content-Length`), fatal the day a streaming/chunked endpoint is added.
**Fix:** decode chunks on bytes; or drop the hand-rolled HTTP client for `http.client.HTTPConnection` over a Unix socket / a length-prefixed JSON frame protocol.

### COR-33 · S2 · Schema `example_response` coverage and drift **[verified]**
`tlgr schema` has examples for 26 of 93 commands; **`tlgr schema message send` has none** because `_build_node` keys the lookup by `full_path` which is `"send"` when a sub-tree is requested (`tlgr/cli/schema.py:106-132,168`). Existing examples are stale: `message react` lacks `already`; `daemon status` lacks `connections/disconnected/healthy/jobs`; `message get` lacks `out/reply_to/media_*/reactions`; `chat list` rows lack `unread_count/last_message`; `message search` lacks `next_cursor`; `account list` JSON has `active` and no `"* "` prefix; `chat create` for groups can never return the shown shape (COR-03).
**Fix:** examples live on the operation spec and are validated against the result model in tests.

### COR-34 · S2 · `agent whoami` never reports `enabled_commands`
`tlgr/cli/agent.py:65` reads `ctx.obj["enable_commands"]`, which `cli()` never stores (`cli/__init__.py:155-163`).

### COR-35 · S2 · Date/time formats are inconsistent and not ISO-8601
`str(msg.date)` → `"2025-03-06 12:00:00+00:00"` (space separator) in messages, dialogs, drafts (`client.py:285,443,524,676`); `AccountInfo.created_at` is naive local `isoformat()` (`accounts.py:112`); webhook `timestamp` is UTC `isoformat()` (`webhook.py:74`); `mute` takes seconds; filters accept `7d`. Agents must special-case each.
**Fix:** RFC-3339 UTC (`2025-03-06T12:00:00Z`) everywhere plus `*_unix` ints; one `fmt_dt()` helper.

### COR-36 · S2 · Exit code 3 (EMPTY) is emitted by exactly one command
`chat posters` (`tlgr/cli/chat.py:303-304`) exits 3 on empty; `chat list`, `message list/search`, `contact list/search`, `draft list`, `catchup`, `chat members` exit 0. AGENT.md:38 documents 3 as "No results" generally.
**Fix:** decide once (recommended: exit 0 with `count: 0` for lists; keep 3 only for point lookups) and enforce in the dispatcher.

### COR-37 · S2 · `daemon status` / `whoami` conflate "PID alive" with "daemon works"
`tlgr/cli/daemon_cmd.py:147-158` prints `running: true, accounts: "?"` when the socket is dead; `agent whoami.daemon_running` is `read_pid() is not None` (`agent.py:61`). A wedged daemon reads as healthy.
**Fix:** `running` must come from a successful `/daemon/status` (with `ready`, `version`); PID-only state should be `"process_alive": true, "responsive": false`.

### COR-38 · S2 · No version handshake between CLI and daemon
After `pip install -U tlgr` the old daemon keeps running (30 min idle, or forever under launchd) and serves old routes; new commands get HTTP 404 → `IPCError("Daemon error (404): 404: Not Found")` exit 12 (`ipc_client.py:139-152`). `/daemon/status` has no version.
**Fix:** `version` + `protocol` in status; CLI compares on first request and restarts (or refuses with `DAEMON_VERSION_MISMATCH`).

### COR-39 · S2 · launchd and idle-stop/auto-start fight each other
`tlgr/daemon/launchd.py:36-38`: `KeepAlive.SuccessfulExit=false` → an idle-stop (exit 0) is not restarted, and the next CLI call spawns an **unmanaged** daemon outside launchd. Conversely `daemon install` while a manual daemon runs makes launchd's instance exit 1 (`server.py:392-395`) and respawn every `ThrottleInterval` (30 s) forever, spamming the log.
**Fix:** when installed under launchd: disable idle-stop, never auto-start from the CLI (`launchctl kickstart` instead), and make "already running" a clean exit 0.

### COR-40 · S3 · Every daemon log line is written twice
`setup_logging` installs both a `FileHandler(daemon.log)` and a `StreamHandler(stderr)` (`tlgr/daemon/lifecycle.py:69-72`); `daemonize()` then `dup2`s stderr onto the same file (`:55-57`).

### COR-41 · S3 · `_idle_monitor` task is created without holding a reference
`tlgr/daemon/server.py:335` — asyncio documents that such tasks may be garbage-collected mid-run.

### COR-42 · S3 · `TlgrGroup.invoke` reads a param that does not exist
`tlgr/cli/__init__.py:29,37`: `ctx.params.get("json")` — the parameter is `use_json`; the code works only because of the `ctx.obj` fallback.

### COR-43 · S3 · Gateway `_handle` does not guard `evaluate()`
`tlgr/gateway/engine.py:108-124`: content/user/message filters dereference `event.raw.message` (`filters/content.py:14`); for `message_deleted`/`message_read` events this raises `AttributeError`, which escapes to Telethon's dispatcher (logged as "Unhandled exception on handler"), and `_stats["errors"]` is not incremented.

### COR-44 · S3 · Action/event parsing silently drops input
`tlgr/gateway/config.py:56-74` returns after the first key of an action mapping (a second key is ignored); `:84-88` drops unknown `events` names without a warning; `save_gateway_configs` (`:127-153`) writes back only name/account/enabled (destroying filters/actions if ever called).

### COR-45 · S3 · `-n` means `--dry-run` globally and `--limit` per command
`tlgr/cli/__init__.py:114` vs `message.py:77`, `chat.py:21`, `contact.py:18`. `tlgr -n 5 chat list` is a usage error; `tlgr chat list -n 5` is a limit.

### COR-46 · S3 · `daemon start` may report the intermediate fork's PID
`tlgr/cli/daemon_cmd.py:55` falls back to `proc.pid`, which is the first fork that already exited.

---

## 3. Security

### SEC-01 · S0 · `umask(0)` + unauthenticated IPC = any local process controls every account **[verified on disk]**
`tlgr/daemon/lifecycle.py:47` `os.umask(0)`. Observed in `~/.tlgr/`: `srwxrwxrwx daemon.sock`, `-rw-rw-rw- daemon.pid`, account directories `drwxrwxrwx`. The IPC server (`tlgr/daemon/ipc.py`) performs no authentication, no peer-credential check and no CSRF-style token; a connect to the socket is full authority to send/delete messages, download media, edit the profile, leave chats, and to read all history for **every** connected account. Any other UID on the host, any sandboxed/containerised process with access to the home directory, or a malicious `npm`/`pip` post-install script running as the user can do this silently. (With the default umask the socket would be `0755` — still readable/connectable only by the owner, because connect requires write — so the umask is the enabling defect; but relying on the umask alone is fragile.) Files created by the daemon inherit the same problem: `downloads/*` (0666), `dead_letter.jsonl` (0666, full message text), `session.session-journal` (0666).
**Fix:** `os.umask(0o077)` in `daemonize()`; `os.chmod(sock, 0o600)` immediately after `site.start()`; verify the peer UID on every connection (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED`/`getpeereid` on macOS) in the aiohttp middleware and reject mismatches; optionally a per-install random token in `~/.tlgr/ipc.token` (0600) sent as a header. Add a test that asserts socket mode after start.

### SEC-02 · S1 · The daemon turns any `-a` string into a filesystem path and creates it **[verified]**
`DaemonServer._connect_account` (`server.py:69-75`) → `AccountManager.load_credentials(alias)` → `get_credentials_path` → `get_account_dir(alias)` which does `(accounts_dir / alias).mkdir(parents=True, exist_ok=True)` (`tlgr/core/accounts.py:180-187,196-199`) **for any alias, registered or not, with no validation** (`add_account` validates; the read path does not). `load_credentials("../escaped-alias")` created a directory outside `accounts/`; an absolute path (`-a /tmp/x`) would `mkdir` there. Junk directories `Pouri16 468070729/` (mode 0777, from a mistyped `-a`) already exist on this machine. Combined with SEC-01 this is a local-user write primitive.
**Fix:** validate the alias against `accounts.json` and the `^[A-Za-z0-9_-]{1,64}$` rule before any path is built; never `mkdir` on read paths; return `ACCOUNT_NOT_FOUND` (HTTP 404, exit 5).

### SEC-03 · S1 · `tlgr logout` / `account remove` do not log out
`tlgr/cli/account.py:193-203` deletes the local session directory; `ClientWrapper.logout()` (`client.py:190-198`, `auth.logOut`) is never called anywhere. The authorization stays valid on Telegram's side (visible in *Active Sessions*) with no local key to revoke it, and a live daemon holding that client keeps operating (SQLite file handle survives `rmtree`).
**Fix:** `account remove` → ask the daemon to `LogOut` (or connect briefly and log out), then delete; `--keep-session` opt-out; `account sessions` (`account.getAuthorizations`/`resetAuthorization`) to audit and revoke.

### SEC-04 · S1 · `--enable-commands` is not a security boundary and its alias semantics are inconsistent **[verified]**
The allowlist is checked in the CLI process (`tlgr/cli/__init__.py:41-72`) from a flag the agent itself passes; argv `--enable-commands all` overrides `TLGR_ENABLE_COMMANDS` (verified). The daemon enforces nothing. Alias handling: `--enable-commands message` allows `message send` but blocks `send` and `msg send`; `--enable-commands send` allows the shortcut but not `message send`; `--enable-commands message.send` blocks `msg send`. Operators reading README.md:314-323 ("Restrict which commands an agent can run") will believe they have a sandbox.
**Fix:** canonicalize every CLI path (aliases, shortcuts) to the operation id from the registry; enforce the allowlist **in the daemon** per socket/token (e.g. a `~/.tlgr/policies/<name>.toml` bound to a token), with the CLI flag as a convenience only; document that the flag alone is advisory.

### SEC-05 · S2 · aiohttp access log writes every request line (query strings) to `daemon.log`
`web.AppRunner(app)` (`tlgr/daemon/ipc.py:74`) keeps aiohttp's default access logger; `setup_logging` sets the root logger to INFO with a file handler (`lifecycle.py:61-73`), and the default format `%a %t "%r" ...` includes the full request line. Search queries, usernames and chat refs (`/message/search?chat=@x&query=...`) accumulate in a 0644 log with no rotation. DEBUG level additionally logs Telethon request objects (message text).
**Fix:** `AppRunner(app, access_log=None)`; structured request logging with redaction; `RotatingFileHandler`.

### SEC-06 · S2 · Dead-letter file retains full message content indefinitely
`tlgr/daemon/webhook.py:20,115-122` appends the complete payload (text, sender ids, and — for message events — `raw`) to `~/.tlgr/dead_letter.jsonl` with umask-0 permissions; `read_dead_letters`/`purge_dead_letters` exist but no CLI exposes them (the roadmap's `tlgr webhook dead-letter` was never built). Given COR-07, this file currently receives **every** message event.
**Fix:** 0600, size cap + rotation, `tlgr webhook dead-letter list|replay|purge`, store ids not bodies (or encrypt).

### SEC-07 · S2 · `config init` writes secret-bearing files with default permissions
`tlgr/cli/config_cmd.py:70,96` use `write_text` (0644 with umask 022) for `config.toml` and `webhook.toml` (contains `token`); `_save_toml` chmods 600 but `init` bypasses it. `jobs.yaml`/`webhook.toml` on this machine are 0644.
**Fix:** route all writes through one `write_private()`.

### SEC-08 · S2 · Webhook transport has no integrity or transport guarantees
`tlgr/daemon/webhook.py:79-82`: bearer token in a header to whatever `url` says (plain `http://` in the example); no HMAC over the body, no event id/sequence for replay detection, no TLS requirement or warning.
**Fix:** `X-Tlgr-Signature: sha256=HMAC(token, body)`, monotonically increasing `seq`, warn on non-loopback `http://`.

### SEC-09 · S3 · API credentials in environment / echoed input
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` override files (`tlgr/core/accounts.py:213-221`) and are visible in `ps eww`; `account add` echoes the hash (`account.py:57`). Low impact; note for hardening.

### SEC-10 · S3 · Session file permissions are only fixed at add/import time
`_secure_session_files` runs in the CLI (`account.py:28-34`); the daemon (umask 0) recreates `*.session-journal` world-writable at runtime. Subsumed by SEC-01's umask fix.

---

## 4. Robustness and scalability

### ROB-01 · S1 · No streaming; every list is materialised twice and shipped in one body
`/chat/list`, `/chat/catchup`, `/chat/members`, `/contact/list`, `/draft/list` accumulate full Python lists (`tlgr/daemon/ipc.py:359-377,501-516,536-546`), serialise them in one `json.dumps`, and the CLI buffers the whole response (`ipc_client.py:112-123`) under one 120 s timeout. Memory and latency are O(account size); a 10k-dialog account cannot be enumerated.
**Fix:** server-side cursors for every list (see §8.5) and an NDJSON streaming mode for `--all`.

### ROB-02 · S1 · Webhook delivery runs inside Telethon's update handlers with `sequential_updates=True`
`create_client` sets `sequential_updates=True` (`tlgr/core/client.py:120`); the webhook handlers `await self._webhook.push(...)` (`server.py:99-143`), and `push` can spend `30 s × 3 attempts + 1 + 2 s` ≈ 97 s on a slow endpoint (`webhook.py:87-110`). During that time **no other update for that account is processed** — gateway auto-replies, other webhooks, `MessageRead` tracking all stall; with COR-07 every message event takes the full failure path.
**Fix:** handlers only enqueue into a bounded `asyncio.Queue`; a worker with concurrency limit delivers; drop-oldest with a metric when full.

### ROB-03 · S1 · No daemon-side timeouts, cancellation or request ids
A Telethon call that hangs keeps its handler coroutine alive forever; the CLI gives up at 120 s but the work continues and cannot be cancelled; concurrent identical requests (e.g. `watch`) pile up. No request id appears in logs or errors.
**Fix:** `asyncio.timeout(op.timeout_s)` per operation; cancel on client disconnect (aiohttp raises `CancelledError` when the transport closes — propagate it); `X-Request-Id`.

### ROB-04 · S2 · Missed updates are never recovered
`catch_up=False` (Telethon default, verified) and the daemon's idle-stop mean every update that arrives while the daemon is down is lost for gateway jobs, webhooks and `watch`; `updates.getDifference` is never called. The SQLite session does persist `pts/qts/date`, so `catch_up=True` would replay them on reconnect.
**Fix:** `catch_up=True`; on start, log the gap; expose `/daemon/status.update_state`.

### ROB-05 · S2 · Blocking I/O on the event loop
Dead-letter append (`webhook.py:119`), YAML/TOML loads in `reload_jobs` (`server.py:161-163`), `accounts.json` writes (`accounts.py:71-73`), `shutil.rmtree`. Small today, but each stalls all accounts.
**Fix:** `asyncio.to_thread` or aiofiles.

### ROB-06 · S2 · One FloodWait sleep blocks the request for up to `flood_wait_max` (120 s) with no feedback
Telethon sleeps inside `__call__` (`telethon/client/users.py:52`) → the CLI sits at 120 s (its own timeout) with no progress; per-request tuning is impossible (COR-15).
**Fix:** per-op thresholds; surface `flood_wait_slept`; return `RATE_LIMITED` immediately when the wait exceeds the CLI's remaining timeout.

### ROB-07 · S2 · Socket is bound only after all accounts connect
`run()` connects accounts (`server.py:273-275`), starts webhook and jobs, and only then binds the socket (`:316-321`). Cold start with N accounts on a slow link exceeds `_auto_start_daemon`'s ~20 s wait → duplicate spawn (COR-14) and a 20 s hang for every CLI call after an idle-stop.
**Fix:** bind first, connect lazily/concurrently (`asyncio.gather`), report `ready` per account.

### ROB-08 · S2 · No rate limiting or coalescing between IPC and Telegram
Every CLI call is a fresh Telegram request; `watch` alone issues ~10/s. Nothing caches dialogs/entities across calls beyond Telethon's entity cache.
**Fix:** per-account token bucket; short-lived dialog cache with `hash`; dedupe identical in-flight reads.

### ROB-09 · S3 · `chat_posters`/`dialog_status` can run 10 minutes uncancellably
`timeout=600` in the CLI (`chat.py:292`, `user.py:90`) with no daemon-side counterpart; the idle monitor may kill them (COR-11).

---

## 5. Maintainability

### MNT-01 · S1 · Five hand-written copies per operation; boilerplate dominates
Measured: `ensure_client`+`404` preamble ×37, `except Exception as e` ×38, 44 `add_get/add_post` lines (`tlgr/daemon/ipc.py`); `@click.option("--account","-a")` ×40 across `cli/*.py`; 16 f-string query builders; hand-picked `columns=` per command; `EXAMPLE_RESPONSES` dict (`schema.py:76-103`) maintained apart from the commands; README.md and AGENT.md re-describe every command and response shape by hand. Adding one operation today touches 5–7 files; at 500 operations that is ~2,500 lines of pure duplication and permanent drift (COR-33 is the drift already visible at 93).
**Fix:** operation registry (§8.2) — one `OperationSpec` generates the CLI command, IPC dispatch, schema entry, docs and contract tests.

### MNT-02 · S2 · No typed models anywhere
Every boundary is `dict[str, Any]`; request shapes are implicit in each handler's `body.get(...)`; responses are ad-hoc dicts assembled in `ClientWrapper`; `mypy` has never run (no config). Consumers (agents) get no JSON Schema for results.
**Fix:** `msgspec.Struct` (fast, zero-dependency validation, JSON Schema export) or pydantic v2 for `Peer`, `User`, `Chat`, `Message`, `Dialog`, `Draft`, `Page[T]`, plus per-op request/response Structs; mypy `--strict` on `tlgr/core` and `tlgr/models` in CI.

### MNT-03 · S1 · Test coverage stops at the client wrapper
273 tests cover `core/client.py` serialisation (with fakes), filters/processors/gateway/actions, `core/output.py`, `core/errors.py`, `AccountManager` basics, `_handle_exception`, `DaemonServer.status()` and the CLI sandbox. **No test imports** `tlgr/ipc_client.py`, `tlgr/daemon/lifecycle.py`, `launchd.py`, `webhook.py`, `daemon/jobs.py`, `core/config.py` (all TOML loaders), or `cli/{message,chat,contact,user,draft,media,profile,daemon_cmd,config_cmd,job,agent,schema,watch}.py`; no test starts `IPCServer` or runs `DaemonServer.run/ensure_client/reload_jobs/_idle_monitor`. That is exactly where COR-01..14, SEC-01..04 live. `pytest-cov` is not installed; coverage is not measured.
**Fix:** (1) an in-process `IPCServer` fixture on a temp socket with a `FakeClientWrapper` → route contract tests; (2) `ipc_client` tests against that fixture (encoding, timeouts, error transport); (3) `DaemonServer` lifecycle tests with a fake Telethon (lock, readiness, idle with in-flight, reconnect); (4) CLI tests via `CliRunner` with `ipc_request` patched — generated from the registry so every op gets one; (5) `coverage` gate ≥ 80 % in CI.

### MNT-04 · S2 · Dead code and dead configuration
`tlgr/core/config.py:1-7` docstring and `load_jobs/save_jobs/JobConfig/DestinationConfig/TransformInline/JobFilterConfig` (`:60-112,222-265`) belong to the removed `jobs.toml` engine; `BaseJob` is typed against `JobConfig` but receives `_GatewayJobConfig` (`jobs/base.py:22`, `gateway/engine.py:26-36`); `ClientWrapper.logout` unused; `save_gateway_configs` unused; `tqdm` dependency unused (`pyproject.toml:35`); config keys `output`, `drop_author`, `delete_after` are listed by `config keys` and written by `config init` but read by nothing (`config set output json` has no effect; `config_cmd.py:26-28,71`); `config.example.toml` omits `idle_timeout`, `flood_wait_max`, `require_account`.

### MNT-05 · S3 · Environment defaults are frozen at import time
`default=_env_or("TLGR_ACCOUNT", "")` etc. (`tlgr/cli/__init__.py:78-103`) evaluate at decoration; Click's `envvar=` would be evaluated per invocation, appear in `--help`/`schema` (`_build_param` already emits `envvar`), and work under `CliRunner(env=...)`.

### MNT-06 · S3 · Naming and layout inconsistencies
`cli/daemon_cmd.py`, `cli/config_cmd.py` vs `cli/chat.py`; `tlgr/jobs/base.py` vs `tlgr/daemon/jobs.py`; `errors.PermissionError_`; `_ref()` coercion duplicated in spirit by `resolve_chat`; `ClientWrapper` mixes serialisation, business logic and RPC.

---

## 6. Packaging, CI, docs

### PKG-01 · S1 · No CI, lint, formatter or type checker
`.github/` holds only issue/PR templates; there is no `workflows/`. `pyproject.toml` has no `[tool.ruff]`/`[tool.mypy]`/`[tool.black]`; `CONTRIBUTING.md:42-45` and the PR template (`.github/PULL_REQUEST_TEMPLATE.md:26`) instruct contributors to run `py_compile` rather than `pytest`. Nothing verifies the Python 3.10 claim (`requires-python = ">=3.10"`; no 3.11+-only syntax was found — `dataclass(slots=True)`, `X | None` under `from __future__ import annotations`, `tomllib` fallback are all fine — but it is untested).
**Fix:** GitHub Actions matrix (3.10–3.13, macOS + Linux): `ruff check`, `ruff format --check`, `mypy`, `pytest --cov`, `python -m build`; pre-commit config; `.gitignore` must stop ignoring `*.yaml` (see PKG-04) or workflow files need `.yml`.

### PKG-02 · S2 · Release hygiene
Version `2.0.0` hard-coded in `tlgr/__init__.py`; no `CHANGELOG.md`; `requirements.txt` duplicates `pyproject` and pins `tomli` unconditionally (`requirements.txt:4`); `tqdm` unused; no `[project.optional-dependencies]` for lint; no `SECURITY.md` despite CONTRIBUTING mentioning private disclosure.

### PKG-03 · S2 · Documentation drift
README.md:89 `chat get` "(members, permissions, etc.)" — returns 4 fields; README.md:297-312 exit-code table omits 9 (`PEER_FLOOD`/`ACCOUNT_FROZEN`); README.md:329 `--select` example prints `{}` (COR-18); AGENT.md:25 "Errors also go to stdout as JSON" — Click usage errors are plain text (UX-02); AGENT.md:337 `tlgr schema --json` (`schema` ignores `--json`); AGENT.md:21 lists `--cursor` as a global flag; `webhook.example.toml:9-13` lists `user_left`/`reaction` events that do not exist; `tlgr/gateway/README.md:36-43` envelope lacks `event_type`; `README.md:255` and `jobs.example.yaml:21,33` examples cannot fire (COR-22); `config.example.toml` lacks three documented keys; `job add` help says "see docs" with no link.
**Fix:** generate the command reference (README "CLI" section, AGENT.md command blocks, example responses) from the registry; keep only prose by hand; a docs-freshness test.

### PKG-04 · S3 · `.gitignore` blocks YAML and references a dead file
`.gitignore:83-84` ignores `*.yaml` (with `!routes.example.yaml`, which no longer exists); `jobs.example.yaml` is tracked only because it was force-added; any future `.github/workflows/*.yaml` or YAML test fixtures will be silently ignored.

### PKG-05 · S3 · No man pages; completion is eval-only
`tlgr completion <shell>` prints Click's eval line (`tlgr/cli/completion.py`); no `--install`, no generated static scripts, no man pages (`click-man` is a one-liner in CI).

---

## 7. UX consistency

### UX-01 · S2 · Global options are positional-only **[verified]**
`tlgr chat list --json` → `No such option '--json'` exit 2; same for `--dry-run`, `--select`, `--results-only`, `--plain`. Agents (and humans) routinely append flags.
**Fix:** attach global options to every generated command (registry) or implement a `Context`-level pre-parse that lifts known globals from anywhere in argv.

### UX-02 · S2 · Usage errors are never JSON **[verified]**
`tlgr --json message list` prints Click's plain-text usage error; `TlgrGroup.invoke` re-raises `click.ClickException` (`cli/__init__.py:32`). AGENT.md promises JSON errors with `exit_code`.
**Fix:** catch `click.UsageError` and emit `{"error":..., "code":"USAGE", "exit_code":2, "usage": "..."}` in JSON mode.

### UX-03 · S2 · Human/plain tables print Python reprs and disagree on `None`
`output_human` uses `str(row.get(c,""))` → `None`, `['a', 'b']`, `{'id': 1, ...}` appear literally (`tlgr/core/output.py:137`); `--plain` prints `""` for `None` (`:12`). `daemon status` human shows `accounts ['<alias>', ...]`; `chat list` hides `last_message`; `message get` dumps nested dicts.
**Fix:** table renderer that formats `None`→`-`, lists→comma-joined, dicts→flattened `a.b` columns; `--plain` escapes `\r`.

### UX-04 · S2 · Argument style is inconsistent across commands
`chat mute <chat> [duration]` (positional seconds) vs `chat typing --duration`; `contact add <phone> [name]` vs `contact rename --first-name/--last-name`; `message forward <from> <to> <ids>` vs `dl <chat> <id>`; `chat open --no-read` vs "use `message list` for silent"; `--limit/-n` vs `--limit-chats/--per-chat`; `user get`/`chat get`/`profile get` return different shapes for the same entity kind; `contact search` is global user search (COR-27).
**Fix:** style guide enforced by the registry: nouns for groups, verbs for commands, `<target>` first, durations as `30s/5m/2h`, `--limit/--cursor/--all` on every list.

### UX-05 · S2 · `-a` is both a global and a per-command option with different defaults
Global `-a` defaults to `""`, per-command to `None` (`cli/__init__.py:86`, `message.py:36`); per-command silently shadows global; `job`, `daemon`, `config` commands have neither. Shortcuts forward `ctx.obj["account"]` manually (`cli/__init__.py:233,271,...`).
**Fix:** one global `-a` honored everywhere (registry adds it to every generated command bound to the same context key).

### UX-06 · S2 · `account list` puts the active marker inside the alias cell
`"* " + alias` in human mode only (`tlgr/cli/account.py:170`); `--plain` gets no marker and JSON gets `active`. Column set differs per format.

### UX-07 · S3 · Help text and naming
`chat get` help over-promises; `agent` group holds `exit-codes`/`whoami` while `schema` is top-level and `exit-codes` is a hidden alias; `inbox`/`chats`/`catchup`/`dl`/`up` shortcuts are listed alongside groups with no grouping; `message read` vs `chat open` vs `chat unread`; `--typing-auto` on send but not edit.

### UX-08 · S3 · Platform assumptions
`daemon logs` execs `tail` (`daemon_cmd.py:174-176`), `job add` execs `$EDITOR` unvalidated (`job.py:49-50`), `daemon install` is macOS-only, `os.fork` daemonisation — none of it runs on Windows despite `Operating System :: OS Independent` in `pyproject.toml:21`.

### UX-09 · S3 · `message send` with empty text and no file
Sends an empty message → `MessageEmptyError` → exit 12 `IPC_ERROR` (COR-06); should be a usage error (exit 2) before any IPC.

---

## 8. Target architecture for ~500 operations

TDLib exposes 1,022 functions and MTProto layer 227 has 757 methods (`analysis/td_api_functions.json`, `mtproto_methods.json`); tdesktop's feature surface (chats, folders, topics, stories, reactions, polls, scheduled messages, admin rights, privacy, sessions, business features…) maps to roughly 450–550 CLI operations. The as-built pattern (five hand-written artefacts per op) cannot get there. The design below keeps the daemon/CLI split (it is the right call for a Telethon session) and replaces everything hand-written with generation from one source of truth.

### 8.1 Layering
```
tlgr/ops/<domain>.py     OperationSpec definitions + async impl(ctx: OpContext, req: Struct) -> Struct
tlgr/registry.py         REGISTRY = {op.id: op}; lint at import (unique ids, aliases, examples present)
tlgr/models.py           msgspec Structs: Peer, User, Chat, Message, Dialog, Draft, Reaction, Page[T], Error
tlgr/cli/gen.py          build_click_tree(REGISTRY) -> click.Group  (one factory; no per-command modules)
tlgr/transport/          client: length-prefixed JSON frames or HTTP over AF_UNIX via http.client; server: aiohttp
tlgr/daemon/dispatch.py  POST /v1/op  → validate → policy → dry-run gate → timeout → impl → envelope
                         GET  /v1/events (NDJSON stream) · GET /v1/status · POST /v1/admin/*
tlgr/daemon/session.py   AccountSession: Telethon client + reconnect supervisor + per-account rate limiter + event bus
tlgr/errors.py           ERROR_MAP (§8.6) — the only place Telethon exceptions are named
tlgr/schema.py           JSON Schema for every op from the Structs; `tlgr schema` and docs are generated
docs/reference/*.md      generated; README keeps prose only
```

### 8.2 Operation registry
```python
@dataclass(frozen=True)
class Param:
    name: str; type: type; help: str
    positional: bool = False; required: bool = False; multiple: bool = False
    default: Any = None; choices: tuple[str, ...] = (); envvar: str | None = None

@dataclass(frozen=True)
class OperationSpec:
    id: str                          # "message.send"  (group.verb) — the ONLY name used for sandboxing
    aliases: tuple[str, ...] = ()    # ("send", "msg.send")  — CLI sugar, canonicalised before policy checks
    summary: str = ""; description: str = ""
    request: type[msgspec.Struct]    # validated at CLI and daemon; generates click params + JSON Schema
    response: type[msgspec.Struct]
    impl: Callable[[OpContext, msgspec.Struct], Awaitable[msgspec.Struct]]
    mutating: bool = False           # gates --dry-run, default-deny in policies, audit log
    needs_account: bool = True
    paginated: PageKind | None = None  # HISTORY | DIALOGS | PARTICIPANTS | SEARCH | LOCAL
    columns: tuple[str, ...] = ()    # human/plain default projection
    example: dict | None = None      # validated against `response` in tests
    timeout_s: int = 120
    stream: bool = False             # NDJSON response
    empty_exit: int = 0              # 0 or EXIT_EMPTY, decided per op, applied by dispatcher
```
One `OperationSpec` yields: the Click command (params from `request`, global flags attached, aliases registered), the daemon route (generic), `tlgr schema` (JSON Schema + example), `docs/reference/<group>.md`, and a contract test (`test_registry.py` parametrised over `REGISTRY`: CLI parses → request Struct; dry-run never calls `impl` when `mutating`; example validates; sandbox blocks by id incl. aliases; error rows map).

### 8.3 Generic dispatch
CLI: `parse → Request Struct → POST /v1/op {op, account, request, dry_run, flood_wait_max, request_id, client_version}` (never query strings). Daemon middleware chain: peer-uid auth → version check → policy (allowlist by op id) → account resolution (explicit only; `ACCOUNT_REQUIRED` otherwise) → dry-run short-circuit → `asyncio.timeout(op.timeout_s)` → rate limiter → `impl` → envelope `{"ok": true, "op": id, "account": alias, "result": …, "page": {...}?, "meta": {"request_id", "elapsed_ms", "flood_wait_slept"}}` or `{"ok": false, "error": Error}`. `--results-only` returns `result`; `--select` projects into `result` (lists element-wise).

### 8.4 Event streaming
Per account, `AccountSession` fans Telethon updates into an in-memory ring buffer (`seq`, `type`, `account`, `payload`) and an `asyncio.Queue` per subscriber. `GET /v1/events?account=&types=&since=<seq>` returns NDJSON with a 15 s heartbeat; `tlgr watch` is a thin consumer with `--since`/`--follow`; the webhook pusher is another subscriber (bounded queue + worker pool, HMAC-signed, `seq` for replay); gateway jobs subscribe the same way. Open streams count as activity for idle-stop; `catch_up=True` fills the gap after restarts; `seq` persists in the session DB so `--since` survives daemon restarts.

### 8.5 Pagination
`Page[T] = {items: list[T], has_more: bool, next_cursor: str | None, total: int | None}`. Cursor = `base64(json{"v":1,"op":id,"kind":…,"state":…})`, validated server-side (rejecting cursors from another op). Kinds: `HISTORY` (`offset_id`, `offset_date` — `messages.getHistory`), `SEARCH` (`offset_id`, `add_offset` — `messages.search`), `DIALOGS` (`offset_date`, `offset_id`, `offset_peer`, `folder_id` — `messages.getDialogs`), `PARTICIPANTS` (`offset` — `channels.getParticipants`), `LOCAL` (server-side slice of a cached list). Every list op gets `--limit`, `--cursor`, `--all` (daemon walks with the per-account rate limiter and streams NDJSON).

### 8.6 Error mapping table (daemon-side, transported verbatim)
| Telethon / internal | code | exit | HTTP | retryable | extra |
|---|---|---|---|---|---|
| `FloodWaitError`, `SlowModeWaitError` | RATE_LIMITED | 7 | 429 | yes | `wait_seconds` |
| `PeerFloodError` | PEER_FLOOD | 9 | 403 | no | |
| `FrozenMethodInvalidError` / `FROZEN_*` | ACCOUNT_FROZEN | 9 | 403 | no | |
| `AuthKeyUnregisteredError`, `SessionRevokedError`, `SessionExpiredError`, `AuthKeyDuplicatedError`, `UserDeactivatedError`, `UserDeactivatedBanError` | SESSION_ERROR | 4 | 401 | no | hint: re-login |
| `SessionPasswordNeededError`, `PhoneCodeInvalidError`, `PhoneNumberInvalidError`, `PhoneNumberBannedError` | AUTH_ERROR | 4 | 401 | no | |
| `UsernameNotOccupiedError`, `UsernameInvalidError`, `PeerIdInvalidError`, `ChannelInvalidError`, `ChatIdInvalidError`, `UserIdInvalidError`, `MessageIdInvalidError`, `ValueError("Could not find the input entity…")`, `ChatNotFoundError` | NOT_FOUND | 5 | 404 | no | |
| `ChatAdminRequiredError`, `ChatWriteForbiddenError`, `ChannelPrivateError`, `UserPrivacyRestrictedError`, `UserIsBlockedError`, `UserBannedInChannelError`, `UserNotParticipantError`, `MessageDeleteForbiddenError`, `MessageAuthorRequiredError`, `MessageEditTimeExpiredError`, `RightForbiddenError`, `PermissionError_` | PERMISSION_DENIED | 6 | 403 | no | `rpc` |
| `MessageNotModifiedError` | NOT_MODIFIED | 0 | 200 | — | `already: true` in result |
| `MessageEmptyError`, `MessageTooLongError`, `MediaEmptyError`, `MediaInvalidError`, `ContactIdInvalidError`, `UserAlreadyParticipantError`, `UsersTooMuchError`, `BotMethodInvalidError`, msgspec `ValidationError` | USAGE | 2 | 400 | no | `field` |
| `FileReferenceExpiredError`, `ServerError`, `RpcCallFailError`, `TimeoutError`, `ConnectionError`, "Cannot send requests while disconnected" | RETRYABLE | 8 | 503 | yes | `retry_after` |
| unknown `RPCError` | GENERIC | 1 | 500 | no | `rpc: {code, message}` |
| daemon version mismatch | DAEMON_VERSION_MISMATCH | 11 | 409 | no | |
| account not resolvable / not registered | ACCOUNT_REQUIRED / ACCOUNT_NOT_FOUND | 2 / 5 | 400 / 404 | no | |

### 8.7 Security and lifecycle baseline
`umask(0o077)`; socket 0600 + peer-uid check; policies enforced in the daemon by op id; alias validation before any path; `flock`-based single instance; bind-before-connect with `ready`; in-flight counter for idle-stop; reconnect supervisor; `catch_up=True`; version handshake; `auth.logOut` on remove; access log off, rotating structured log with redaction; `dead_letter` 0600 with rotation and a CLI to drain it.

### 8.8 Migration path
1. Land the registry + generic dispatch with the existing 94 commands moved one group at a time (`message` first), keeping old routes for one release behind a `legacy` flag.
2. Move `ClientWrapper` methods into `tlgr/ops/*` unchanged (they already return dicts; wrap in Structs incrementally).
3. Replace `watch` and the webhook with the event bus.
4. Turn on the contract tests and coverage gate; delete `cli/*.py` command modules, `EXAMPLE_RESPONSES`, hand-written reference docs.

---

## Appendix A — verification log
* `mute_until`: `loop.time()=1057057 → 1970-01-13`; wall `1788347457 → 2026-09-02`.
* `json.dumps(Message.to_dict())` → `TypeError: Object of type datetime is not JSON serializable`; `aiohttp.ClientSession.post(json=…)` raises the same before connecting.
* aiohttp `UnixSite` with raw `ipc_request`: space/`#` → 400 Bad status line; Persian → 400 Invalid char in url query; `&` splits; `+` → space.
* `_handle_exception` table above (all → 500 `IPC_ERROR`).
* `hasattr(TelegramClient, "create_group") is False`; `delete_messages` returns a list.
* `_entity_to_dict(Channel(id=123))["id"] == 123` vs `get_peer_id == -1000000000123`.
* `markdown.parse("snake_case_var and *star* and `x`")` strips backticks.
* `_decode_chunked` on a Persian body returns trailing `\r\n0\r`.
* `chat_id` filter with `"@source_channel"` → `False`; `time_of_day "23:00-07:00"` at 23:30 UTC+03:30 → `False`.
* `AccountManager.load_credentials("../escaped-alias")` created `<base>/escaped-alias`.
* `--json --dry-run` made a real IPC call for 12 mutating commands (listed in COR-17).
* `tlgr schema message send` contains no `example_response`; 26/93 commands have one.
* `tlgr chat list --json` → exit 2 "No such option"; `tlgr --json message list` → plain-text usage error.
* `ls -la ~/.tlgr`: `srwxrwxrwx daemon.sock`, `-rw-rw-rw- daemon.pid`, `drwxrwxrwx accounts/<junk alias>/`.
