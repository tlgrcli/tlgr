# tlgr Agent Reference

Compact reference for LLM agents. Use `--json` for all calls.

## Authentication

**Authentication is interactive and requires a human.** Run `tlgr account add <phone>` manually, complete 2FA, then hand the CLI to your agent. Agents work with pre-authenticated accounts only.

## Global Flags

| Flag | Env | Purpose |
|------|-----|---------|
| `--json` | `TLGR_JSON=1` | JSON output (always use this) |
| `--results-only` | | Strip envelope, emit primary result only |
| `--select <fields>` | | Comma-separated dot-path field projection |
| `-a, --account <alias>` | `TLGR_ACCOUNT` | Select account |
| `--enable-commands <list>` | `TLGR_ENABLE_COMMANDS` | Sandbox: `message.send,chat.list` |
| | `TLGR_REQUIRE_ACCOUNT` | With `require_account` config: every command must get an explicit `-a` (exit 2 otherwise) |
| `--dry-run, -n` | | Preview destructive ops without executing |
| `--no-input` | | Never prompt (agent mode) |
| `--cursor <token>` | | Pagination cursor (on list commands) |

## Output Modes

Always use `--json`. Errors also go to stdout as JSON with `exit_code != 0`.

Commands generated from the operation registry — the whole `message` group,
`draft`, `agent exit-codes`, `agent parity`, `schema` — wrap their answer in
an envelope:

```json
{"ok": true, "op": "message.send", "account": "main",
 "result": {...}, "page": {...},
 "meta": {"request_id": "...", "elapsed_ms": 42, "already": false, "warnings": []}}
```

```json
{"ok": false, "error": {"error": "...", "code": "RATE_LIMITED", "exit_code": 7, "wait_seconds": 30}}
```

`--results-only` prints the inner value in both cases, which is v1's shape,
and `--select a.b,c` projects fields by dot path. `meta.already: true` marks an
idempotent no-op (the world already looked the way you asked for) — success,
not an error. Commands still under `tlgr/cli/legacy/` print v1's bare object.

`tlgr agent whoami --json` reports `output_schema_version: 2`; branch on that
rather than probing for each changed shape.

## Pagination

List commands return `{items, has_more, next_cursor, total}`. Pass
`--cursor <next_cursor>` to get the next page; stop when `has_more` is
`false`. A cursor is opaque, signed, and bound to the operation, page kind and
account it came from — a tampered or foreign cursor is exit 2, never a silent
restart from the top of the list. `--all` walks every page inside the daemon
and streams the result.

## Exit Codes

| Code | Name | Meaning |
|------|------|---------|
| 0 | SUCCESS | OK |
| 1 | GENERIC | Unknown failure |
| 2 | USAGE | Bad arguments |
| 3 | EMPTY | No results |
| 4 | AUTH | Authentication needed |
| 5 | NOT_FOUND | Chat/entity not found |
| 6 | PERMISSION | Permission denied |
| 7 | RATE_LIMITED | Retry after `wait_seconds` |
| 8 | RETRYABLE | Transient error |
| 10 | CONFIG | Config error |
| 11 | DAEMON | Daemon error |
| 12 | IPC | IPC error |
| 13 | INDETERMINATE | The answer could not be established — treat as unknown, NEVER as a negative |

## Commands

### Messages

Generated from the operation registry, so every response below is the
`result` inside the v2 envelope (see **Output Modes**). Shapes shown are what
`--results-only` prints. `msg` is an alias for `message`, and `tlgr send` is
an alias for `tlgr message send`.

```
tlgr message send <chat> <text> [--file PATH] [--caption TEXT] [--reply-to ID] [--silent]
                                [--parse md|html|none] [--schedule TS] [--topic ID]
                                [--send-as PEER] [--split] [--typing N|--typing-auto]
→ {"id": 123, "chat_id": -100123, "date": "2026-09-03T09:14:07Z",
   "date_unix": 1788340447, "text": "...", "out": true, "kind": "message"}

tlgr message list <chat> [--limit N] [--cursor TOKEN] [--all] [--since TS] [--until TS]
→ {"items": [...], "has_more": true, "next_cursor": "..."}

tlgr message get <chat> <msg_id>
→ {"id": 123, "text": "...", "sender": {...}, "media": {...}, ...}

tlgr message delete <chat> <id1> [id2 ...]
→ {"chat_id": -100123, "deleted": 2, "ids": [123, 124]}

tlgr message search <chat> <query> [--limit N] [--cursor TOKEN] [--from USER] [--media-type T]
→ {"items": [...], "has_more": false}

tlgr message pin <chat> <msg_id>          → {"chat_id": -100123, "msg_id": 123, "pinned": true}
tlgr message unpin <chat> <msg_id>        → {"chat_id": -100123, "msg_id": 123, "pinned": false, "unpinned": 1}
tlgr message react <chat> <msg_id> <emoji>
→ {"chat_id": -100123, "msg_id": 123, "emoji": "👍", "reacted": true}

tlgr message read <chat> [--up-to MSG_ID]
→ {"chat_id": -100123, "read": true, "read_up_to": 123}

tlgr message edit <chat> <msg_id> <text> [--typing SECONDS]
→ {"id": 123, "chat_id": -100123, "edited": true, "edit_date": "2026-09-03T09:20:00Z", "text": "..."}

tlgr message forward <from_chat> <id1> [id2 ...] --to <chat> [--hide-sender]
→ {"items": [{"id": 200, "chat_id": -1001234, "from_chat_id": 777123, "from_msg_id": 123}], "has_more": false}

tlgr message link <chat> <msg_id>         → {"link": "https://t.me/durov/42", "public": true, ...}
tlgr message entity list --parse md <text>
→ {"text": "hello world", "entities": [...], "auto_entities": [...], "length_utf16": 11, "would_split": 1}
```

Two changes from v1 to note before parsing: `date` is RFC-3339 with a
`date_unix` sibling, `message edit` reports `edit_date` rather than `date`,
and `list`/`search`/`forward` return `{items, has_more, next_cursor, total}`
instead of `{messages: [...]}`. `tlgr agent whoami` reports
`output_schema_version: 2` if you need to branch.

`message send` also supports `--typing SECONDS` (show "typing…" before sending, max 60s)
and `--typing-auto` (duration estimated from text length) for human-like sends.
Text over 4096 UTF-16 units is refused unless `--split` is given: a silently
truncated message is worse than an unsent one. Use `message entity list` to see
exactly what a parse mode did, in the UTF-16 offsets Telegram counts in, before
sending.

Beyond the ten verbs above the group also has `preview`, `compose`, `summarize`,
`translate`, `transcribe`, `report`, `thread list`, `view get`,
`read-receipt list`, `scheduled send`, `dice list`, `effect list`, `paid set`,
`fact-check set`, `game *`, `sponsored *`, `suggested *` and `tone *`. Run
`tlgr message --help`, or read the generated `docs/reference/message.md`.
Anything the pinned Telethon cannot express is refused with `NOT_SUPPORTED`
and a reason — never silently ignored.

### Drafts

Drafts are the human-in-the-loop primitive: prepare a reply without sending;
the user sends or discards it from any Telegram client.

```
tlgr draft set <chat> <text> [--reply-to MSG_ID] [--parse md|html|none]
→ {"chat_id": -100123, "text": "...", "date": "2026-09-03T09:14:07Z"}

tlgr draft clear <chat>                   → {"cleared": true, "chat_id": -100123, "count": 1}
tlgr draft clear --all --yes              → {"cleared": true, "count": 0}

tlgr draft list [--limit N] [--cursor TOKEN]
→ {"items": [{"chat_id": -100123, "chat": {...}, "text": "...", "date": "...", "reply_to": ...}],
   "has_more": false}
```

`draft set` returns the saved draft rather than v1's `{"draft": true}`, and
`draft list` returns a page whose `chat_id` is the **marked** id (`-100…` for
channels, negative for groups) with `raw_id` beside it — v1's raw id could not
tell a user from a channel.

### Chats

```
tlgr chat list [--folder main|archive|all|<name|id>] [--type user|bot|group|supergroup|channel|saved]
               [--unread] [--unread-mark] [--with-mentions] [--with-reactions] [--with-drafts]
               [--pinned] [--muted|--unmuted] [--search TEXT] [--sort date|unread|name|pinned]
               [--scope dialogs|admined-public|inactive|left] [--common-with USER]
               [--limit N] [--cursor TOKEN] [--all]
→ {"ok": true, "result": [{"chat": {"id": -100123, "raw_id": 123, "kind": "supergroup",
                                    "title": "...", "username": "..."},
                           "unread_count": 3, "unread_mark": false, "pinned": false,
                           "folder_id": 0, "notify": {"muted": false},
                           "last_message": {"id": ..., "date": "...", "out": false, "text": "...",
                                            "kind": "message"}}],
   "page": {"has_more": true, "next_cursor": "...", "total": null}}

tlgr chats [...]                            # alias for chat list
tlgr inbox [...]                            # alias; add --unread yourself
```

**The row's peer is nested under `chat`.** v1 spelled it flat
(`id`/`name`/`type`/`username`); those keys moved into the same `Peer` object
every other v2 response embeds, and `--select chat.id,unread_count` reaches
them. `last_message` is a whole `Message`, so a service event
(`"kind": "service"`) and a caption-less sticker (`"media": {...}`) are
distinguishable from a genuinely blank message. Never emits a read receipt.

```
tlgr catchup [--type user] [--limit-chats N] [--per-chat N]
tlgr chat catchup [...]
→ {"chats": [{"id": ..., "name": ..., "unread_count": 3, "unread_mark": false,
              "messages": [{...}]}]}
# "What did I miss?" — every unread chat with its recent messages, one call.
# READ-ONLY: emits no read receipts. Start every session/wake with this.
# A chat carrying only the manual unread mark is included even at count 0.

tlgr chat open <chat> [--limit N] [--no-read] [--topic ID]
→ {"chat_id": ..., "marked_read": true, "messages": [...]}
# Open a chat the way a human would: fetch recent history AND emit a read
# receipt (visible to the other side — that's the point; it humanizes you).
#   - loud (visible):  chat open            → history + read receipt
#   - silent (peek):   chat open --no-read, or message list
# A read receipt also clears the unread badge in the ACCOUNT OWNER's own
# client. On a chat the owner is handling by hand that badge is their only
# reminder that they owe a reply — peek with --no-read there. If you cleared
# one by accident, see 'chat unread'.

tlgr chat read [<chat>...] [--up-to ID] [--mentions] [--reactions] [--polls]
               [--folder FOLDER] [--from-file PATH] [--topic ID]
→ {"read": true, "results": [{"chat_id": ..., "ok": true}]}
# The receipt without the history. Irreversible for the other side.

tlgr chat unread <chat> [--clear]
→ {"unread": true, "chat_id": ...}
# Mark a chat unread again — the undo for an accidental read receipt.
# Restores the badge the owner sees; does NOT un-send the read receipt the
# other side already got (that is irreversible), and sets Telegram's manual
# unread FLAG rather than a numeric count. Chats flagged this way come back
# with "unread_mark": true and DO appear in chat list --unread / inbox /
# catchup, even though their unread_count is 0.

tlgr chat get <chat> [--full] [--field NAME]
→ {"id": ..., "type": "user", "title": "...", "name": "...", "username": "...",
   "unread": 3, "notify_settings": {...}, "ttl_period": null, "settings": {...}}
# --full adds getFullUser/getFullChat/getFullChannel: about, participant
# counts, admin rights, linked chat, invite link, antispam, and the rest.
# `name` is v1's spelling of `title` and is still emitted.

tlgr chat posters <chat> [--limit N] [--max-messages N] [--since TS] [--min-messages N]
→ {"posters": [{"user_id": ..., "id": ..., "username": ..., "name": ..., "count": 44,
                "is_bot": false, "is_deleted": false,
                "last_msg_id": ..., "date": "...", "date_unix": ...}],
   "scanned_messages": 2400, "distinct_posters": 137}
# Distinct senders in a chat's recent history, by count. Pagination is handled
# INTERNALLY — do not hand-roll the walk. --max-messages bounds the scan
# (default 2000, hard cap 20000). Exits 3 (EMPTY) when nobody has posted.
# On a FloodWait mid-scan it backs off and returns the partial harvest with
# "partial": true and "flood_wait": N.
# Filter on is_bot / is_deleted before contacting anyone. Senders are not
# always users: anonymous admins and a linked channel post under a negative
# channel id, so filter to positive ids when harvesting people.

tlgr chat mention list <chat> [--kind mention|reaction|poll-vote] [--read] [--topic ID]
→ the three unread queues next to unread_count, as a page of messages.
```

Acting on one chat:

```
tlgr chat archive <chat>... [--undo] [--dismiss-bar]  → {"archived": true, "chat_id": ..., "chat_ids": [...]}
tlgr chat mute <chat>... [--for 8h|--until TS|--forever|--off] [--stories] [--folder F]
                                                      → {"muted": true, "chat_id": ..., "mute_until": "..."}
tlgr chat pin <chat>... [--unpin] [--folder F] [--order]
tlgr chat leave <chat>... [--delete-history] [--remove-from-folders] [--yes]
tlgr chat delete <chat> [--for-both|--for-everyone] [--yes]
tlgr chat clear <chat> [--for-both] [--since TS] [--until TS] [--yes]
tlgr chat typing <chat> [--action typing|record-audio|upload-photo|…] [--duration N]
tlgr chat ttl set <chat> [1d|1w|off]                  # omit the period to read it
tlgr chat notify get|set <chat> [--silent on|off|default] [--preview …] [--sound …]
tlgr chat theme list | chat theme set <chat> --emoji 🌷 [--unset]
tlgr chat wallpaper set <chat> [--slug S|--file PATH|--color '#rrggbb'] [--for-both] [--unset]
tlgr chat translate <chat> on|off
tlgr chat set <chat> [--sharing on|off] [--view-as topics|messages] [--send-as PEER]
tlgr chat action-bar get <chat> [--hide]              # the anti-spam bar, incl. registration_month
tlgr chat badge get [--folder F] [--include-muted] [--limits]
tlgr chat report <chat> [--spam|--reason …|--option HEX] [--comment TEXT] [--yes]
tlgr chat saved list [--in CHAT] [--pinned]           # Saved-Messages sublists / monoforum topics
tlgr chat import <chat> <export.txt> [--check] [--yes]
tlgr chat autoarchive set [--auto on|off] [--keep-unmuted on|off]
tlgr chat promo list [--dismiss KEY] [--hide-promo]
```

`chat mute --for 8h` writes an **absolute** timestamp: v1 computed it from the
event loop's clock, so every timed mute resolved to 1970 and did nothing.
`chat secret list|start|send` are registered and refuse with `NOT_SUPPORTED`
(exit 13) — tlgr has no end-to-end layer; `chat secret discard` works.

`chat create` and `chat members` are unchanged from v1 and migrate with the
groups-and-channels group.

### Folders

A chat folder is a *filter*, not a container: Telegram stores one
`dialogFilter` per folder and every client applies it to its own dialog list.
Editing one is a read-modify-write of the whole filter, which tlgr does in a
single call.

```
tlgr folder list [--with-counts] [--tags on|off]
→ {"tags_enabled": false,
   "folders": [{"id": 2, "title": "Work", "emoticon": "💼", "groups": true,
                "include_peers": [...], "exclude_peers": [...], "pinned_peers": [...],
                "is_chatlist": false, "chats": 12, "unread_chats": 3}]}

tlgr folder create <title> [--emoji E] [--groups] [--channels] [--bots] [--contacts]
                           [--exclude-muted] [--exclude-read] [--exclude-archived]
                           [--include CHAT]... [--exclude CHAT]... [--pin CHAT]...
tlgr folder edit <folder> [--title T] [--groups/--no-groups] [--add CHAT] [--remove CHAT] …
tlgr folder add <folder> <chat>... [--pin]
tlgr folder remove <folder> <chat>... [--exclude]
tlgr folder delete <folder> [--leave-chats none|suggested|all|<chat>,…] [--yes]
tlgr folder reorder <folder>...            # 'main' positions the All-chats tab
tlgr folder suggested list [--add TITLE]
tlgr folder join <t.me/addlist/SLUG> [--chats CHAT]... [--all-chats]   # previews unless told to join
tlgr folder share list|set|delete <folder> [--slug S] [--chats CHAT]... [--all-eligible]
tlgr folder update list <folder> [--join all] [--dismiss]
```

`--folder <name|id>` works on `chat list`, `chat read`, `chat mute`,
`chat pin` and `chat badge get` too; it is matched by folder id or by title.
Removing a chat from a folder does not stick if the folder's type flags still
match it — that is what `folder remove --exclude` is for.

### Contacts

```
tlgr contact list [--limit N] [--cursor TOKEN]
→ {"contacts": [{"id": ..., "name": ..., "username": ..., "phone": ...}], "has_more": false}

tlgr contact add <phone> [name]
→ {"added": true, "user_id": 123}

tlgr contact rename <user> [--first-name TEXT] [--last-name TEXT]
→ {"saved": true, "user_id": 123, "first_name": "...", "last_name": "..."}
# Works on non-contacts too (saves them as a contact). Omitted parts keep the
# current profile name. Useful for tagging users with state markers.

tlgr contact remove <user>
→ {"removed": true}

tlgr contact search <query> [--limit N] [--cursor TOKEN]
→ {"contacts": [...], "has_more": false}
```

### Users

```
tlgr user get <user>
→ {"id": ..., "first_name": ..., "username": ..., "bio": ..., "is_bot": false, ...}

tlgr user dialog-status <user> [--max-dialogs N]
→ {"ref": ..., "id": ..., "username": ..., "resolved": true, "has_dialog": true,
   "message_count": 12, "source": "peer_dialogs", "reason": null}

tlgr user hide-stories <user> [--unhide]
→ {"user_id": ..., "username": ..., "hidden": true, "already": false}
```

`hide-stories` is Telegram's own "Hide Stories" menu item: the peer leaves the
main stories bar for the collapsed Hidden list. Per-account and purely local —
**the other side is never notified**, the chat, the contact entry and their
access to you are untouched — so it is safe to apply in bulk to everyone an
outreach campaign has contacted, which is what keeps a working account's story
bar readable. Idempotent: it reads the fresh `stories_hidden` flag first and
returns `already: true` without an RPC when there is nothing to do, so
repeating a pass over hundreds of peers is nearly free. `tlgr user get` reports
the same flag as `stories_hidden`, so the state can be audited without writing.

`dialog-status` is the ONLY correct way to ask "does this account have prior
history with this person?". Three outcomes, never conflated:

| `resolved` | `has_dialog` | exit | meaning |
|---|---|---|---|
| `true` | `true` | 0 | a dialog exists; `message_count` is the server's exact total |
| `true` | `false` | 0 | definitively none — the account's complete dialog list was enumerated and they were not in it |
| `false` | `null` | **13** | could NOT be established; `reason` says why |

**Do not infer a negative from an error.** `message list` on a bare numeric id
raises "Could not find the input entity" whenever the local entity cache is
cold — including for people the account demonstrably HAS messaged. That error
is indistinguishable from a genuinely unknown peer, so any guard that reads it
as "no history" will eventually let a duplicate cold message through. That is
the bug this command exists to remove; exit 13 must be treated as a refusal.

`source` says how the answer was reached:
- `peer_dialogs` — an input peer was available, so the server was asked directly
  (`messages.GetPeerDialogs` + an exact message total). Cheap.
- `dialog_scan` — the peer was unresolvable, so the account's complete dialog
  list was enumerated server-side. Finding the id is a positive; **exhausting**
  the list is the only thing that licenses a negative. `scanned_dialogs` reports
  how far it got.
- `unknown` — cap hit, FloodWait, or RPC failure. `resolved` is `false`.

Note: there is no MTProto call that resolves a bare user id to an access hash
for a non-bot account (`users.GetUsers` with `access_hash=0` returns
`UserEmpty` for non-contacts). The dialog list, not entity resolution, is what
makes the answer authoritative. It reports on the dialog list: a conversation
the account itself deleted is gone server-side too and correctly reads as no
dialog.

### Profile

```
tlgr profile get
→ {"id": ..., "first_name": ..., "last_name": ..., "username": ..., "phone": ...}

tlgr profile update [--first-name TEXT] [--last-name TEXT] [--bio TEXT] [--photo PATH]
→ {"updated": true}
```

### Media

```
tlgr media download <chat> <msg_id> [--out-dir PATH]
→ {"path": "/path/to/file", "msg_id": 123}

tlgr media upload <chat> <path> [--caption TEXT]
→ {"id": 200, "chat_id": -100123}
```

### Agent Helpers

```
tlgr agent whoami
→ {"account": "main", "user_id": 123, "username": "me", "daemon_running": true, ...}

tlgr agent exit-codes
→ {"exit_codes": {...}}

tlgr agent parity [--uncovered] [--domain NAME]
→ {"catalog_version": "...", "required": 1797, "covered": 200, "percent": 11.1,
   "by_priority": {...}, "by_domain": {...}, "uncovered": [...], "waivers": 1597}

tlgr schema [command_path...]
→ {"schema_version": 2, "build": "2.0.0", "command": {...}}
```

`agent parity` reports coverage of the pinned Telegram feature catalog: what
tlgr can do today, per priority and per domain, with every gap either waived
to a named later PR or listed. Use it to find out whether a capability exists
before writing a workaround. Nothing in it is hand-maintained.

### Daemon

```
tlgr daemon start [--foreground]
tlgr daemon stop
tlgr daemon status
→ {"running": true, "ready": true, "pid": 12345, "uptime_seconds": 3600,
   "accounts": ["main"], "connections": {"main": true}, "disconnected": [],
   "healthy": true, "version": "2.0.0", "protocol": 2}
```

`running` means a process is alive; `ready` means it can actually serve. A
daemon that is up but cannot reach Telegram reports `healthy: false` and names
the accounts in `disconnected` — check `ready`, not `running`.

**Protocol v2.** The CLI talks to the daemon over `~/.tlgr/daemon.sock`, mode
`srw-------`, with the peer's uid checked on every connection. Four things
follow that are worth knowing as a caller:

- **The account is always explicit.** The CLI resolves it (`-a` →
  `TLGR_ACCOUNT` → `[defaults]` → active alias) and sends it; the daemon never
  picks one for you and answers `ACCOUNT_REQUIRED` (exit 2) when it was not
  given one. v1 used whichever alias came first out of a set, so a two-account
  user could send from the wrong identity with no signal.
- **Version handshake with one restart.** A client newer than the running
  daemon restarts it exactly once and says so on stderr. `--no-daemon-restart`
  refuses instead, with exit 11.
- **The daemon starts itself, once.** Concurrent `tlgr` invocations with no
  daemon running produce exactly one daemon; readiness is an HTTP 200 from
  `/v1/status`, not the socket file appearing.
- **A dropped connection is a state, not an exception.** The account goes
  `degraded` and requests answer `RETRYABLE` (exit 8) with a hint; a revoked
  session goes `needs_login` and answers exit 4. `tlgr daemon stop` drains
  in-flight work instead of cancelling it.

### Streaming

```
tlgr watch [--chat CHAT1 --chat CHAT2]
→ newline-delimited JSON to stdout, one event per line
```

## Error Response Shape

```json
{"error": "message text", "code": "RATE_LIMITED", "exit_code": 7, "wait_seconds": 30}
```

Generated commands nest exactly that object under `{"ok": false, "error": …}`
and add a `hint` when there is a useful next step; `--results-only` prints the
inner object, which is the shape above. Legacy commands print it bare. Either
way it goes to **stdout**, and the exit code is the contract.

## Chat Resolution

Chat arguments accept:
- Numeric ID: `12345`, `-100123456` — pass negative IDs after a `--` separator,
  e.g. `tlgr message list --limit 5 -- -100123456`
- Username: `@username`

Display names and phone numbers are NOT accepted for chat arguments.

## Rate Limiting

Telegram rate limits are surfaced as exit code 7 with `wait_seconds` in the JSON error. The agent should back off accordingly.

## Sandboxing

Use `--enable-commands` to restrict what the agent can do:
- `--enable-commands message.send,message.list,chat.list` — only these commands
- `--enable-commands message` — all message subcommands
- `--enable-commands '*'` — everything (default)

For generated commands the allowlist is matched by **canonical operation id**
and enforced inside the daemon, so an alias cannot be used to get past it:
`--enable-commands message.list` permits `tlgr msg list` too, and a blocked
operation exits 6. It is a usability guard, not a sandbox — anything that can
reach the socket can reach the session. See `SECURITY.md`.

## Self-Discovery

Run `tlgr schema --json` for the full machine-readable CLI schema with
parameter types, defaults, and example responses. For generated commands it
carries a JSON Schema (draft 2020-12) for both the request and the response of
every operation, plus a generated example — so you can validate a call before
making it. `tlgr agent parity --json` answers the other question: whether a
capability exists at all yet.

Generated per-command reference lives in `docs/reference/` (`message.md`,
`draft.md`, `agent.md`, `PARITY.md`). It comes out of the same definitions, so
it cannot describe a flag the CLI does not have.
