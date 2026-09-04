# tlgr Agent Reference

Compact reference for LLM agents. Use `--json` for all calls.

## Authentication

**Someone has to read the code; nothing else needs a human.** Logging in is a
sequence of ordinary commands, not one process held open on a prompt — the
pending login lives in the daemon and is mirrored to
`<account>/login-state.json` at 0600, so the two steps can run minutes apart
in different processes.

```
tlgr auth send-code +989123456789 --alias work --api-id 12345 --api-hash-env TLGR_API_HASH
→ {"account": "work", "phone": "989…89", "type": "app", "code_hash": "5f2a…", "timeout": 60}

tlgr auth verify-code 12345 --alias work --password-env TLGR_2FA_PASSWORD
→ {"status": "authorized", "alias": "work", "user_id": 4242, "username": "me"}
```

`type` says where to look: `app` (another logged-in Telegram session — what a
third-party api_id usually gets), `sms`, `sms_word`, `sms_phrase`, `call`,
`missed_call`, `flash_call`, `fragment`, `email`, `setup_email_required`.

Three terminal states, all reportable:

| `status` | Exit | What to do |
|---|---|---|
| `authorized` | 0 | done |
| *(password wanted)* | 4 | `AUTH_PASSWORD_REQUIRED`; add `--password-env TLGR_2FA_PASSWORD` and re-run the same line |
| `signup_required` | 0 | the number has no account — `tlgr auth sign-up --first-name … --accept-tos`, deliberately a separate command |

**Secrets never go in argv.** Every one takes `--x-env`, `--x-stdin` or
`--x-file` and no value flag: `--password-env` (default `TLGR_2FA_PASSWORD`),
`--new-password-env`, `--token-env` (default `TLGR_BOT_TOKEN`),
`--api-hash-env` (default `TLGR_API_HASH`).

Other ways in:

```
tlgr account add --bot --token-env TLGR_BOT_TOKEN --alias helper   # one call; records kind=bot
tlgr auth qr --alias work                                         # streams tg://login tokens until approved
tlgr account import ./work.session --alias work                   # stop the source client first
tlgr auth code list                                               # the code Telegram sent *this* account (chat 777000)
```

`tlgr account add <phone>` is the wrapper: it starts the login and returns the
`auth verify-code` line to run next. `auth code list` is what makes scripted
multi-account onboarding possible — account B reads the code for account A's
new login.

## Account health

`tlgr account check --json` answers the question `daemon status` cannot: a
dropped connection and a revoked auth key look identical from outside.

```
tlgr account check                 # every configured account, one row each
→ [{"alias": "work", "state": "authorized", "user_id": 4242}]
```

`state` is `authorized`, `revoked` (exit 4 territory — log in again),
`banned` / `deactivated` (write to recover@telegram.org), `frozen` (carries
Telegram's own `appeal_url`; the appeal is a web form) or `offline` — which is
*not* a statement about the account, only about reaching it.

Security housekeeping an agent can run on a schedule:

```
tlgr account session list --unconfirmed     # a new login nobody has confirmed yet
→ [{"hash": "9021045", "device_model": "Unknown", "ip": "203.0.113.7",
    "deny_deadline": "2026-09-10T09:14:07Z", "unconfirmed": true}]

tlgr account session terminate 9021045 --deny --yes   # "it wasn't me"
tlgr account session terminate --all-others --yes
tlgr account website list                   # a different list from Devices
tlgr account logout work --yes              # revokes it server-side; `account remove` does not
```

`deny_deadline` is `date_created + authorization_autoconfirm_period`: after it
Telegram confirms the login for you, so a check that runs less often than that
window will never see an unconfirmed session at all.

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
tlgr message react <chat> <msg_id> <emoji>   # alias of `reaction add`; see Reactions
→ {"chat_id": -100123, "msg_id": 123, "emoji": "👍", "reacted": true, "mine": ["👍"]}

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

### Polls

An answer is addressed by **index** on the command line and by an opaque
server-assigned identifier on the wire. tlgr resolves the index against a
fresh copy of the poll on every call, so `--shuffle` cannot make index 1 mean
two different answers; the identifier comes back as `option_b64` if you would
rather hold it yourself.

```
tlgr poll create <chat> <question> <option>... [--quiz --correct N] [--multiple]
                 [--public-voters] [--duration 2h] [--hide-results] [--allow-adding-options]
→ {"type": "poll", "can_vote": true, "chat_id": -100123, "msg_id": 123, "poll_id": 506...,
   "question": "Lunch?", "total_voters": 0,
   "options": [{"index": 0, "text": "Pizza", "option_b64": "AA"}, ...]}

tlgr poll get <chat> <msg_id> [--follow --follow-for 5m]
→ {..., "can_vote": false, "restriction": "already-voted", "my_votes": [1]}

tlgr poll vote <chat> <msg_id> <index>...  → the poll, with the new tally
tlgr poll vote <chat> <msg_id> --retract   → the poll, with my votes gone
tlgr poll close <chat> <msg_id> --yes      → {"closed": true, ...}

tlgr poll voter list <chat> <msg_id> [--option N]   # public polls only
→ {"items": [{"user_id": 4242, "option": 0, "date": "..."}], "has_more": false}

tlgr poll option add <chat> <msg_id> <text>     # open-answer polls
tlgr poll option remove <chat> <msg_id> <index> --yes
tlgr poll unread list <chat> [--read-all]
tlgr poll stats get <chat> <msg_id>             # channel admins
```

`can_vote` and `restriction` are computed for you — `closed`,
`already-voted`, `subscribers-only`, `country-restricted` — so you do not have
to learn the answer by sending a vote and reading the error.

### Reactions

`messages.sendReaction` carries the **whole** set of reactions this account
has on a message, not a delta. `reaction add` therefore reads what is already
there and resends it with the new one appended; `mine` in the response is the
set after the call. Use `--replace` to send exactly what you named.

```
tlgr reaction add <chat> <msg_id> <emoji>... [--custom DOC_ID] [--big] [--replace]
→ {"chat_id": -100123, "msg_id": 123, "emoji": "👍", "reacted": true, "mine": ["👍"],
   "reactions": {"counts": {"👍": 4}, "mine": ["👍"], "total": 4}}

tlgr reaction remove <chat> <msg_id> [<emoji>] [--every]
tlgr reaction list <chat> <msg_id>... [--top-senders]
tlgr reaction user list <chat> <msg_id> [--emoji E]   # needs can_see_list
tlgr reaction unread list <chat> [--read-all]
tlgr reaction catalog [--top] [--recent] [--forget]
tlgr reaction chat get|set <chat> [--every|--none|--some 👍,❤] [--max-unique N] [--paid on|off]
tlgr reaction default get|set [<emoji>]
tlgr reaction tag list|set                            # Saved Messages tags (Premium)
tlgr reaction purge <chat> <user> --msg ID|--every --yes
tlgr reaction report <chat> <msg_id> <user> [--ban]
tlgr reaction pay <chat> <msg_id> --stars N --yes     # spends Stars
```

A custom (Premium) emoji is spelled `custom:<document_id>` everywhere — read
one out of `mine` and hand it straight back to `reaction remove`. Reacting
twice, or removing what is not there, is `already: true`, not an error.

`reaction pay` spends real Stars: `--stars` has no default, the channel is
validated before anything is spent, and a failed payment is never retried.

### Checklists

A task id is assigned once and never renumbered — completions are keyed by it,
so removing task 1 leaves tasks 2 and 3 as 2 and 3.

```
tlgr todo create <chat> <title> <task>... [--others-can-add] [--others-can-complete]
→ {"chat_id": -100123, "msg_id": 123, "title": "Release checklist",
   "tasks": [{"id": 1, "title": "tag the commit"}, ...], "done_count": 0}

tlgr todo get <chat> <msg_id>
tlgr todo toggle <chat> <msg_id> --done 1,2 --undone 3     # one request, both directions
tlgr todo add <chat> <msg_id> <task>...                    # ids continue from the highest
tlgr todo edit <chat> <msg_id> [--title T] [--remove-task ID] [--rename-task ID=TEXT]
```

Creating a checklist needs Telegram Premium. A tick that is already there is
`already: true`.

### Locations

```
tlgr location send <chat> <lat> <lon> [--accuracy M]
tlgr location venue send <chat> <lat> <lon> --title T [--address A] [--venue-id ID]
tlgr location search <lat> <lon> [<query>]        # venue provider inline bot

tlgr location live start <chat> <lat> <lon> [--period 1h] [--heading DEG] [--proximity M]
→ {"id": 123, "chat_id": -100123, "period": 3600, "expires_at": "2026-09-03T10:14:07Z"}
tlgr location live edit <chat> <msg_id> [<lat> <lon>] [--heading DEG]
tlgr location live stop <chat> <msg_id> | --every
tlgr location live list <chat> [--mine]

tlgr location nearby list <lat> <lon> [--publish 1h --yes] [--unpublish]
tlgr location preview <chat> <msg_id> [--out map.png] [--zoom 15] [--size 512x512]
```

Nothing moves a live location for you: the server stores one position and a
period, so `expires_at` is reported rather than a duration and you re-issue
`location live edit` yourself. Stopping deliberately sends an empty point —
a stopped share should not publish where you were when you stopped it.
Looking at who is nearby never publishes your own position; `--publish` does,
and says so.

A negative longitude looks like an option to the parser: put `--` before the
coordinates, as in `tlgr location send @alice -- 51.5074 -0.1278`.

### Search

```
tlgr search global [<query>] [--type photo|video|link|...] [--only user|group|channel]
                   [--folder ID] [--archived] [--since TS] [--until TS] [--limit N] [--cursor T]
→ {"items": [{"id": 123, "chat_id": -100123, "chat": {...}, "text": "..."}], "has_more": true}

tlgr search hashtag <tag> [--chat CHAT]        # in one chat, or all public posts
tlgr search hashtag --recent [--forget TAG|*]  # tlgr's own local history
tlgr search post <query> [--quota] [--pay-stars N --yes]
```

Global search pages on `(offset_rate, offset_peer, offset_id)`; `--cursor`
carries all three, so never rebuild it from a message id. Global search only
covers chats you are in and never secret chats, so an empty answer is not
proof a message does not exist.

`search post` is free while a daily quota lasts. `--quota` reports what is
left and what the query would cost without spending anything; beyond the free
quota the search refuses to run until you pass `--pay-stars N`.

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

### Groups and channels

Members, admins, invites, topics and the Manage screen. Three shapes matter
before you script any of it.

* **A member is a participant, not a user.** Every row keeps its wrapper —
  `status` (creator/admin/member/self/restricted/banned/left), `rank`, `date`,
  `inviter_id`, `promoted_by`, `kicked_by`, `admin_rights`, `banned_rights` —
  so "is this person banned or merely restricted" is answerable.
* **Rights are always allow-polarity.** Telegram stores banned rights inverted
  (`send_messages=true` means *cannot* send); tlgr normalises once, so `true`
  means allowed everywhere, including in `banned_rights`.
* **Both masks are replaced, never patched, server-side.** Every writing
  command reads the current mask first and sends a complete one, which is why
  `chat member restrict --deny send-media` does not hand back the four other
  restrictions somebody set last week.

```
tlgr chat member list <chat> [--filter recent|admins|bots|contacts|kicked|banned|restricted|mentions]
                             [--search Q] [--via-link LINK] [--topic ID] [--limit N] [--all]
→ page of {"id", "user_id", "username", "name", "status", "rank", "date",
           "inviter_id", "promoted_by", "kicked_by", "admin_rights", "banned_rights"}
# `chat members` is v1's spelling and still works. --filter kicked is people
# who were removed; --filter restricted is people still in the chat with a
# mask on them. A chat with participants_hidden answers exit 6, not an empty
# page — "nobody is in this group" would be a lie.

tlgr chat member get <chat> <user>        # + effective_permissions: defaults patched with their mask
tlgr chat member add <chat> <user>...     # missing[] carries {user_id, reason} verbatim
tlgr chat member remove <chat> <user>...  # a kick: they may rejoin
tlgr chat member ban <chat> <user>... [--until 7d] [--purge] [--messages ID] [--report] --yes
tlgr chat member unban <chat> <user>...
tlgr chat member restrict <chat> <user> [--deny R,R] [--allow R,R] [--none|--all|--clear]
                                        [--replace] [--until 7d] [--purge]
tlgr chat member edit <chat> <user> [--rank TITLE] [--free-messages on|off] [--refund]
tlgr chat member delete-history <chat> <user> --yes
tlgr chat member report <chat> <user> [--messages ID] --yes

tlgr chat admin list <chat> [--no-rights]
tlgr chat admin promote <chat> <user> [--rights R,R|--grant R,R|--revoke R,R|--all|--none]
                                      [--except R,R] [--rank TITLE] [--anonymous]
tlgr chat admin demote <chat> <user> --yes
tlgr chat permission list [--mask admin|member|all] [--chat CHAT]   # the canonical names
tlgr chat permission get <chat>   → {"allow": [...], "deny": [...], "rights": {...}}
tlgr chat permission set <chat> [--allow R,R] [--deny R,R] [--all|--none] [--replace]
tlgr chat transfer <chat> <user> --password-stdin --yes             # 2FA, irreversible
tlgr chat admin-log list <chat> [--filter join,ban,kick,…] [--admin USER] [--search Q]
→ page of {"id", "date", "user_id", "action", "raw_type", "prev", "new"}
tlgr chat admin-log report <chat> <msg_id>                          # anti-spam false positive
```

`chat permission list` is the single source of truth for right names.
`manage-linked-peers` and `manage-welcome-messages` are layer-229 flags
Telethon 1.44 cannot express: they are listed with `"supported": false` and
asking for one exits 13 rather than granting less than you asked for.

Invites, join requests and topics:

```
tlgr chat invite create <chat> [--title T] [--expires 7d] [--limit N|--request-approval]
                               [--subscription-stars N] [--replace-primary]
tlgr chat invite list <chat> [--admin USER] [--revoked] [--by-admin]
tlgr chat invite get <chat> <link> | chat invite get <t.me/+hash> [--qr] [--png PATH]
tlgr chat invite edit <chat> <link> [--title T] [--expires …] [--limit N] [--request-approval on|off]
tlgr chat invite revoke <chat> <link> --yes      # the permanent link is replaced, both are reported
tlgr chat invite delete <chat> [<link>|--revoked] --yes
tlgr chat invite open <t.me/+hash>               # read a peek without joining
tlgr chat join <@name|t.me/+hash>
→ {"chat_id", "title", "joined", "pending_approval", "already"}
# All three outcomes exit 0: joined, already a member, and request-sent.

tlgr chat request list <chat> [--link L] [--search Q] [--approve USER] [--decline USER]
                             [--approve-all] [--decline-all]
tlgr chat request approve|deny <chat> [<user>...] [--all] [--link L] --yes

tlgr chat topic list <chat> [--search Q] [--closed] [--hidden] [--pinned]
tlgr chat topic get <chat> <topic>...            # a deleted topic comes back as {"id", "deleted": true}
tlgr chat topic create <chat> <title> [--icon-emoji ID] [--icon-color RGB]
tlgr chat topic edit <chat> <topic> [--title T] [--icon-emoji ID|--no-icon] [--closed on|off]
tlgr chat topic close|reopen <chat> <topic>
tlgr chat topic hide|unhide <chat>               # General (id 1) only
tlgr chat topic pin <chat> <topic>... [--reorder] | chat topic unpin <chat> [<topic>...|--all]
tlgr chat topic mute <chat> <topic> [8h] | chat topic unmute <chat> <topic>
tlgr chat topic delete <chat> <topic> --yes
tlgr chat topic read <chat> <topic> [--max-id N] [--mentions] [--reactions] [--list]
```

A topic id **is** the id of its creation service message, so it is exactly what
`message send --topic` and `message list --topic` take. General is id 1, always
exists, cannot be deleted, and is the only topic that may be hidden.

The Manage screen, and the numbers behind it:

```
tlgr chat create <title> [--type group|supergroup|channel|forum] [--about T] [--members USER]
                         [--photo PATH] [--username NAME] [--ttl 1d] [--geo lat,lon] [--tabs]
tlgr chat edit <chat> [--title T] [--about T] [--geo lat,lon|off] [--color ID] [--emoji-status ID]
                      [--main-tab posts|gifts|media|…] [--palettes]
tlgr chat convert <chat> supergroup|gigagroup --yes           # one-way, both ids reported
tlgr chat setting get <chat>                                  # every toggle, keyed as its flag
tlgr chat setting set <chat> [--slow-mode 30s] [--prehistory visible|hidden] [--forum on|off]
                             [--antispam on|off] [--hidden-members on|off] [--signatures on|off]
                             [--reactions all|none|👍,❤️] [--sticker-set NAME] [--ads on|off] …
→ {"chat_id", "changed": [...], "already": [...], "failed": {"key": "why"}}
tlgr chat username get <name> [--chat CHAT] | chat username set <chat> <name> [--order LIST]
tlgr chat username toggle <chat> <name> on|off | chat username unset <chat> [--all]
tlgr chat photo set <chat> <file>|--video PATH|--emoji-markup ID | chat photo delete <chat>
tlgr chat send-as list <chat> | chat send-as set <chat> <peer>
tlgr chat discussion list | chat discussion set <channel> <group> [--unhide-prehistory]
                          | chat discussion unset <channel>
tlgr chat stats get <chat> [--message ID|--story ID|--poll ID] [--load-graphs] [--out DIR]
tlgr chat stats list <chat> --message ID|--story ID            # public reposts
tlgr chat revenue get <chat> [--ton] [--since USER] | chat revenue list <chat> [--in|--out]
tlgr boost get [<chat>] [--features] | boost list [<chat>] [--mine|--user U|--gifts]
tlgr boost add <chat> [--slots N]
```

`chat setting set` is a batch: a toggle already in the state you asked for is
reported in `already` and never sent, and a refusal lands in `failed` per key
instead of hiding the changes that did land. Statistics are routed to
`channelFull.stats_dc` automatically, and graphs are emitted as Telegram's own
chart spec — tlgr never redraws them. Revenue is read-only: withdrawing money
needs your 2FA password and belongs in an official client.

Seven commands are registered and refuse with `NOT_SUPPORTED` (exit 13):
`chat community create|list|set|ban` and `chat welcome list|set|delete` need
MTProto layer 229 and Telethon 1.44 speaks 227, so there is no request to
send. The command shapes are settled and will start working with the layer
uplift.

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
tlgr contact list [--limit N] [--cursor TOKEN] [--with-status] [--with-stories]
                  [--sort name|first-name|last-name|last-seen|added] [--mutual-only]
                  [--close-friends-only] [--ids-only] [--export vcard|csv|json --out PATH]
→ Page[Contact]: {"items": [{"id", "name", "username", "phone", "mutual", ...}],
                  "has_more": false, "next_cursor": null, "total": 2}

tlgr contact add <user|+phone> [name] [--first-name T] [--last-name T] [--note T]
                 [--share-phone] [--from-message <chat>:<id>]
→ {"added": true, "user_id": 123, "imported": [123], "retry": [], "reason": null}
# By USER it is contacts.addContact; by +PHONE it is contacts.importContacts.
# An empty `imported` with an empty `retry` is AMBIGUOUS: the number may have
# no Telegram account, OR its owner may refuse lookups by phone. `reason` says
# so. Do not report it as "no such user". `retry` entries are not failures --
# the server is asking for them again later.

tlgr contact rename <user> [--first-name TEXT] [--last-name TEXT]
→ {"saved": true, "user_id": 123, "first_name": "...", "last_name": "..."}
# Works on non-contacts too (saves them as a contact). Omitted parts keep the
# current profile name. Useful for tagging users with state markers. An empty
# first name is sent as "." because the server rejects an empty one.

tlgr contact remove <user>... [--phone NUMBER]
→ {"removed": true, "user_ids": [123], "phones": []}
# Deleting by phone reaches numbers with no Telegram account and is
# irreversible server-side.

tlgr contact search <query> [--mine-only] [--global-only] [--recent] [--type KIND]
→ Page[FoundPeer]: each row carries `source`: mine | global | recent | sponsored.
# Adverts are OFF unless --with-sponsored. `--recent` is tlgr-local state.

tlgr contact note set <user> [text] [--clear]        # private; read back by `user get --full`
tlgr contact status list [--online-only]             # last-seen for every contact, one call
tlgr contact birthday list [--window DAYS]
tlgr contact close-friends list|set <user>...        # --add/--remove are read-modify-write
tlgr contact blocked list [--stories]                # the two blocklists are independent
tlgr contact blocked set <peer>...                   # REPLACES the list; the reply is the diff
tlgr contact top list|set [--category NAME]          # frequent contacts
tlgr contact import <file.vcf|file.csv> [--batch-size N]
tlgr contact sync <file> [--apply] [--delete-missing]  # prints the diff unless --apply
tlgr contact saved list [--invite-text]              # every number ever uploaded
tlgr contact share <user> --to <chat>
tlgr contact share-phone <user>                      # irreversible disclosure
```

`contact status list` reports `by_me` on the coarse buckets (`recently`,
`last_week`, `last_month`). It means **our own** last-seen privacy caused the
coarseness — never report it as the peer hiding from us.

### Users

```
tlgr user get <user> [--full] [--translate-bio LANG]
                     [--from-chat CHAT --from-message ID]
→ {"id": ..., "first_name": ..., "username": ..., "bio": ..., "is_bot": false,
   "status": "online", "stories_hidden": false, ...}
# Never prints an access hash: `access_hash_cached` says whether one is held.
# A bare numeric id resolves only from this account's peer cache -- there is no
# MTProto call that mints an access hash for one. For a `min` user (seen only
# inside a channel message) pass --from-chat/--from-message.
# No photo plus an empty status is a SIGNAL, not a verdict: this never claims
# "they blocked you". Use the global --select to pull out one field.

tlgr user block <user> [--stories] [--report-spam] [--delete-history]
→ {"peer_id": ..., "blocked": true, "stories_only": false, "deleted": false}
tlgr user unblock <user> [--stories]
→ {"peer_id": ..., "blocked": false, "already": false}

tlgr user can-message <user>...
→ Page[ContactRequirement]: {"user_id", "result": free|premium|paid, "stars_amount"}
# Pairs with dialog-status for cold-outreach gating: this answers "am I
# allowed to", dialog-status answers "have I already".

tlgr user chat list <user> [--leave-all]     # groups and channels you share
tlgr user link <user> [--profile] [--text T] # `me --token` mints an expiring link
tlgr user photo list|set <user>
tlgr user music list <user>
tlgr user personal-channel get <user>
tlgr user birthday set <user> <date>         # sends a visible message

tlgr user dialog-status <user> [--max-dialogs N]
→ {"ref": ..., "id": ..., "username": ..., "resolved": true, "has_dialog": true,
   "message_count": 12, "source": "peer_dialogs", "reason": null}

tlgr user hide-stories <user>... [--unhide] [--all]   # v1's spelling of `story hide`
→ {"user_id": ..., "username": ..., "hidden": true, "already": false}
# More than one peer fills `peers`; a single peer answers with exactly the
# four keys above.
```

### Resolving a reference

```
tlgr resolve peer <ref>...  [--from-chat CHAT --from-message ID] [--ids botapi]
→ Page[ResolvedRef]: {"ref", "id" (raw), "marked_id", "type", "title",
                      "source", "resolved", "access_hash_cached"}
# `source` says HOW it was answered. An uncached bare numeric id FAILS
# (exit 5 or 13) rather than being guessed at -- there is no MTProto call that
# turns an id into an access hash for a non-bot account.

tlgr resolve username <name> [--type user|bot|group|channel]
# USERNAME_INVALID exits 2 (a typo); USERNAME_NOT_OCCUPIED exits 5 (free).

tlgr resolve phone <+number> [--offline] [--countries]
→ {"phone", "e164", "country", "resolved", "peer", "reason"}
# PHONE_NOT_OCCUPIED exits 13, NEVER 5: no account and a privacy refusal are
# indistinguishable. --offline formats and validates without an RPC.

tlgr resolve link <url> [--no-network] [--open] [--draft CHAT]
→ {"kind", "raw_url", "username", "msg_id", ..., "delegated_to"}
# Classifies any t.me / tg:// link into one of ~30 kinds and NEVER acts:
# `delegated_to` names the command that would (chat join, bot start, gift
# redeem, proxy add...). `t.me/+X` is a PHONE when X parses as a number and an
# invite hash otherwise.

tlgr resolve cache get [--type KIND] [--stale 7d] [--refresh PEER] [--purge]
# The per-account peer database. Access hashes are never printed, only
# `access_hash_cached`; they are per login session and worthless elsewhere.
```

`hide-stories` is now `tlgr story hide <peer>` (and `--unhide` is
`tlgr story unhide <peer>`). The old path, the old flag and the four keys are
unchanged — see **Stories** below for what it does and why it is free to
repeat. `tlgr user get` still reports the current value as `stories_hidden`,
so the state can be audited without writing.

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

### Media, stickers, GIFs and custom emoji

Both v1 spellings still work (`tlgr dl`, `tlgr up`), and both answer with more
than they used to — the two shape changes are in the CHANGELOG's table.

```
tlgr media get <chat> <msg_id>            # what it IS, without fetching a byte
→ {"chat_id": …, "msg_id": …, "kind": "video", "mime": "video/mp4",
   "size": …, "duration": 42, "width": 1280, "doc_id": …, "file_id": "…"}

tlgr media download <chat> <msg_id>...    # --out/--out-dir, --thumb, --range,
→ {"items": [{"msg_id": 123, "path": "/path/to/file", "bytes": …,
              "kind": "video"}], "has_more": false}
                                          # --resume, --connections, --verify,
                                          # --all, --album, --read, --background

tlgr media upload <chat> <path>...        # --as, --caption, --spoiler, --ttl,
→ {"chat_id": …, "msg_id": 200, "msg_ids": [200], "kind": "photo"}
                                          # --thumb, --album, --no-send, --dedupe

tlgr media list <chat> --type photo       # the shared-media tabs, paginated
tlgr media search <query> --type file     # across every chat
tlgr media limit get                      # the server's own upload limits
tlgr media transfer list                  # the Downloads panel

tlgr sticker set get <set>                # every sticker, its emoji and index
tlgr sticker set add|remove <set>...      # install / uninstall (not delete)
tlgr sticker pack create <name> --add f:😀 # a pack you own; `pack` verbs need that
tlgr gif list | gif send <chat> <index>
tlgr emoji get <id>...                    # a custom emoji id → what it stands for
```

Two rules worth knowing before scripting against these:

- **`set` vs `pack`.** `sticker set remove` uninstalls somebody's set;
  `sticker pack delete` destroys one you created, for everyone. Different
  commands, different blast radius.
- **A sticker is named `<set>/<index>` or `<set>/<emoji>`, never by a bare
  document id.** A cached id carries a dead `file_reference`; naming the set
  lets tlgr fetch a live one in the same call.
### Calls, video chats and conferences

**tlgr carries no audio or video.** It speaks the signalling half of calls: it
can ring, answer, hang up, rate, mute, moderate, invite, record and observe,
and nobody can hear it. Every response that could be mistaken for
participation says so — `"media": "none"` is on the wire, not in a footnote.

```
tlgr call start <user> [--video] [--check] [--wait] [--auto-discard 60s]
→ {"call_id": 4815162342, "state": "waiting", "media": "none", "video": false, "out": true}
   --check rings nothing and reports {"state": "checked", "can_call": true, ...}

tlgr call accept <call> [--ack-only] [--no-ack]  → {"call_id": ..., "state": "accepted", "media": "none"}
tlgr call decline <call> [--reason missed|busy] [--reply TEXT]
                                                 → {"call_id": ..., "reason": "missed"}
tlgr call end <call> [--reason hangup|busy|...] [--duration N]
                                                 → {"call_id": ..., "reason": "hangup", "need_rating": true}
tlgr call get <call>                             → the live state, and the key-verification indices
tlgr call rate <call> <1-5> [--problem echo]     → {"call_id": ..., "rating": 4, "comment": "#echo"}
tlgr call log list [--missed] [--with USER] [--limit N] [--cursor TOKEN]
→ {"items": [{"msg_id": 900, "chat_id": 4242, "kind": "call", "direction": "in",
              "duration": 42, "date": "2026-09-03T09:14:07Z"}], "has_more": false}
tlgr call watch                                  → NDJSON: one record per ring or state change
```

A call is addressed by its id. The daemon remembers the calls it has seen —
Telegram hands out a call's `access_hash` once, in an update — so a bare id
works; the `id:access_hash` form works without that memory and teaches it.

```
tlgr vc create <chat> [--title T] [--schedule WHEN] [--rtmp] --yes
tlgr vc get <chat|id:hash|link|msg:ID> [--limits] [--stream-channels]
tlgr vc list                                     → chats with a call running now
tlgr vc set <call> [--title T] [--join-muted on|off] [--record start|stop]
tlgr vc participant list <call> [--raised-hands] [--limit N] [--cursor TOKEN]
tlgr vc mute|unmute <call> [PEER] [--for-me]     → {"muted": true, "media": "none"}
tlgr vc invite <chat> <user>...                  → {"invited": [...], "failed": [...]}
tlgr vc link <chat> [--speaker] [--revoke]       → the listener or speaker link
tlgr vc rtmp get <chat> [--show-key|--key-file PATH] [--revoke]
tlgr vc send <call> <text> [--stars N --confirm-stars]
tlgr vc download <call> --out PATH [--duration 30s]
tlgr vc watch <call>                             → NDJSON: participants, mutes, in-call chat
```

`vc download` is the one thing a headless CLI does better than the GUI: it
cannot play a livestream and it can record one. It needs a join first
(`vc join --listen-only`, experimental: it synthesizes a listener payload to
obtain presence and still carries no audio).

The in-call chat has **no history and no fetch method**. `vc watch --messages`
is the only way to read it.

The RTMP stream key is a publishing credential: it is masked unless you pass
`--show-key` or `--key-file`.

```
tlgr conference create                           → {"slug": "AbCdEf", "invite_link": "https://t.me/call/AbCdEf"}
tlgr conference get <link|slug|id:hash|msg:ID> [--qr]
tlgr conference invite <call> <user>...          → rings them; falls back to the link
tlgr conference decline <msg_id>                 → refuse an invitation, or cancel your own
tlgr conference revoke <call> --yes              → invalidate the link
tlgr conference chain list <call> [--tip]        → the E2E chain, base64, unvalidated
```

Conferences are end-to-end encrypted. Creating and reading a call link, ringing
people and revoking it need no crypto and are complete. **Joining**, **removing
a participant** and **sending inside** need a signed `e2e.chain` block built on
the current tip; tlgr has no block builder, accepts one from an external
implementation (`--block`, `--public-key`) and otherwise exits 2 naming exactly
what is missing rather than sending a request that will fail.

### Stories

```
tlgr story feed list                      # the stories bar; --hidden, --unread-only
→ {"items": [{"peer_id": …, "max_read_id": 41, "unread_count": 1,
              "has_unread": true}], "has_more": false}

tlgr story list <chat>                    # active; --profile, --archive, --album ID
→ {"items": [{"id": 42, "date": "…Z", "expire_date": "…Z", "caption": "…",
              "media": {…}, "pinned": false}], "has_more": false}

tlgr story get <chat> <id>...             # --views, --link, --areas-out, --translate
tlgr story post <file>...                 # --caption, --privacy, --allow, --exclude,
                                          # --period, --pin, --album, --area-*
tlgr story edit <chat> <id>               # --caption, --file, --cover-ts, --privacy
tlgr story delete <chat> <id>...          # irreversible; needs --yes off a TTY

tlgr story read <chat> [<id>...]          # clears YOUR unread ring
→ {"peer": …, "max_id": 43, "ids": [42, 43], "ok": true}
tlgr story read <chat> <id> --register-view   # …and appear in their viewer list

tlgr story react <chat> <id> 🔥           # --remove, --custom-emoji, --as-message
tlgr story reply <chat> <id> "text"       # a private message carrying the story
tlgr story share <chat> <id> --until <chat>   # sends a story card, not a copy

tlgr story pin|unpin <chat> <id>...       # the profile page; --top for the top row
tlgr story hide|unhide <chat>...          # the stories bar; --all for the whole bar
tlgr story viewer list <chat> <id>        # --contacts, --q, --csv PATH, --hide-from
tlgr story blocklist list|set <user>...   # "Hide my stories from"; --remove, --replace
tlgr story album create|edit|list|delete|reorder <chat> …
tlgr story can-post                       # free slots, limits, Premium gates, --chats
tlgr story stealth set --past --future    # Premium; --status only reads
tlgr story search --hashtag berlin        # public stories only
tlgr story stats get <chat> <id>          # graphs; --forwards for public reposts
tlgr story export <chat> --out DIR        # the bulk export the GUI has no button for
tlgr story live start --rtmp              # prints the ingest URL and key
tlgr story watch                          # story.new / read / reaction / stealth
```

Four rules matter more than the flags:

- **Reading is not being seen.** `story read` sends `stories.readStories`,
  which clears the ring on *your* side and tells the poster nothing. Appearing
  in their viewer list is `--register-view`, and it is opt-in on purpose.
- **The audience is a base rule plus exceptions**, applied in that order:
  `--privacy contacts --exclude @bob` is "contacts, except Bob". `--privacy
  selected` with no `--allow` is refused, because it would post to nobody.
  Channel stories ignore the vector entirely.
- **A placeholder is not a story.** A feed row can come back as
  `{"id": 99, "skipped": true}` and a gone story as `{"id": 99,
  "deleted": true}`. `story list` hydrates placeholders by default;
  `--no-hydrate` gives you the raw shape.
- **Re-run `story can-post` immediately before posting.** The weekly and
  monthly quotas move under you, and a refusal comes back as a named
  `reason` (`STORY_SEND_FLOOD_WEEKLY`, `BOOSTS_REQUIRED`, …) with the
  seconds or boosts still missing.

`story hide` is Telegram's own "Hide Stories" menu item: the peer leaves the
main stories bar for the collapsed Hidden list. Per-account and purely local —
**the other side is never notified**, the chat, the contact entry and their
access to you are untouched — so it is safe to apply in bulk to everyone an
outreach campaign has contacted. Idempotent: it reads the fresh
`stories_hidden` flag first and returns `already: true` without an RPC when
there is nothing to do, so repeating a pass over hundreds of peers is nearly
free.

### Agent Helpers

```
tlgr agent whoami
→ {"output_schema_version": 2, "account": "main", "user_id": 123,
   "daemon_running": true, "daemon_healthy": true, "layer": 227, ...}

tlgr agent capabilities [--section protocol|policy|gates|events|limits]
→ {"layer": 227, "event_types": 114, "unsupported_constructors": [...],
   "prohibited": [{"action": "...", "reason": "..."}],
   "premium_gated": [...], "bot_only": [...], "admin_only": [...]}

tlgr agent exit-codes [--errors] [--search TEXT]
→ {"exit_codes": {...}, "errors": [{"name": "FloodWaitError", "code":
   "RATE_LIMITED", "exit": 7, "retryable": true, "extra": "wait_seconds"}]}

tlgr agent parity [--uncovered] [--domain NAME]
→ {"catalog_version": "...", "required": 1797, "covered": 1010, "percent": 56.2,
   "by_priority": {...}, "by_domain": {...}, "uncovered": [...], "waivers": 787}

tlgr schema [commands|events|config|errors|exit-codes|all|<command path>...]
→ {"schema_version": 2, "build": "2.0.0", "ops": {...}}

tlgr status [--check]
→ {"account": "main", "connected": true, "daemon_healthy": true,
   "behind_seconds": 3, "problems": []}
```

`agent capabilities` is the one to read before planning. It separates three
different answers that all look like "no": what this **build** cannot do (a
constructor newer than the pinned Telethon layer), what this **account** may
not reach (premium, bot-only, admin-only), and what tlgr **will not** do —
fake a read receipt, suppress typing status, misrepresent online status, pass
a device-integrity attestation, or execute a payment — each with its reason.
Only the first is a gap somebody might close.

`agent parity` reports coverage of the pinned Telegram feature catalog: what
tlgr can do today, per priority and per domain, with every gap either waived
to a named later PR or listed. Nothing in it is hand-maintained.

`status --check` exits non-zero when anything is wrong, and is the cheapest
thing for a monitor to run: a frozen account, an open send circuit breaker, an
outstanding flood deadline and a daemon that is up but not ready are the
states in which every *other* command starts failing.

### Daemon

```
tlgr daemon start [--foreground] [--catch-up/--no-catch-up] [--wait 30s]
tlgr daemon stop [--grace 10s]
tlgr daemon restart
tlgr daemon status [--check]
→ {"running": true, "ready": true, "healthy": true, "pid": 12345,
   "uptime_seconds": 3600, "version": "2.0.0", "protocol": 2, "layer": 227,
   "accounts": [{"alias": "main", "state": "online", "pts": 91824,
                 "behind_seconds": 0, "reconnects": 0}],
   "connections": {"main": true}, "disconnected": []}

tlgr daemon reconnect [--reset-proxy] [--no-catch-up]
tlgr daemon save-state
tlgr daemon logs [--follow] [--lines 50] [--level warning] [--grep TEXT]
tlgr daemon flood list [--include-expired]
tlgr daemon flood clear --every
tlgr daemon dead-letter list | send | delete
tlgr daemon install [--supervisor auto|launchd|systemd] | uninstall
```

`running` means a process is alive; `ready` means it can serve; `healthy`
means the accounts are actually working. A daemon that is up but cannot reach
Telegram reports `healthy: false` and names the accounts in `disconnected` —
check `healthy`, not `running`.

`daemon flood list` is the persistent store of rate-limit deadlines. Telethon
remembers a `FLOOD_WAIT` in memory and forgets it on exit; tlgr writes it per
`(account, method, peer)`, so a fresh process does not immediately re-trip a
wait — which is how a short wait becomes a long one.

`daemon dead-letter *` is the store of events no consumer could be given. A
re-drive reuses the original delivery id, so a receiver keyed on
`Idempotency-Key` sees a duplicate rather than a new event.

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

A tlgr home containing a `.production` marker file is refused by the daemon
and by `daemon start`/`restart`/`install` unless `TLGR_ALLOW_PRODUCTION_HOME=1`
is set. Two processes on one home share session files, and Telegram treats a
second client on one auth key as a compromised session and revokes it.

### Events and sync

```
tlgr events list [--group message|read|presence|...] [--available] [--raw]
→ Page[EventType] — 114 types, every Update* constructor accounted for

tlgr events get message_new [--json-schema]
→ {"type": "message_new", "group": "message", "box": "pts",
   "sources": ["UpdateNewMessage", ...], "payload": {...}, "example": {...}}

tlgr events replay --since 91820 [--events TYPES] [--chat CHAT]
tlgr events decode [FILE|-] [--push] [--key-env TLGR_PUSH_KEY]

tlgr sync status [--channels] [--refresh]
→ {"pts": 91824, "qts": 12, "seq": 4410, "behind_seconds": 3,
   "channels": [{"chat_id": -100…, "pts": 42, "access_hash_known": true}]}

tlgr sync catch-up            # updates.getDifference — replay what was missed
tlgr sync difference [--chat CHAT] [--follow 30]   # diagnostics, read-only
tlgr sync reset               # give up on the gap and re-baseline
tlgr sync backfill CHAT --from-id 91800 --to-id 91900
```

`sync catch-up` **replays** a gap; `sync reset` **gives up on** one —
everything before the new baseline is marked seen and is not recoverable. They
are not interchangeable, and neither is `chat catchup`, which is the unread
digest a human reads.

`sync status --channels` reports `access_hash_known`. A channel without an
access hash in the session is *skipped* by catch-up — Telethon will not call
`getChannelDifference` without one — so it looks idle rather than broken.

### Network and proxies

```
tlgr net status [--no-ping]
→ {"connected": true, "phase": "online", "dc_id": 4, "transport": "...",
   "ping_ms": 41.2, "layer": 227, "time_offset_seconds": 0}

tlgr net ping [--probes 3] [--via nearest-dc|get-state]
tlgr net dc list [--ipv6] [--media-only] [--cdn] [--test]
tlgr net dc nearest
tlgr net usage get

tlgr proxy add 'tg://proxy?server=…&port=…&secret=…' [--set]
tlgr proxy list | set <id|none|system> | remove <id> | test [--every] | link <id>
```

`time_offset_seconds` is worth reading when requests fail for no visible
reason: MTProto derives `msg_id` from the local clock, and the server drops
anything outside its window without an error the client can see. A drift over
30 seconds is reported as a warning.

Proxy credentials live in `~/.tlgr/proxies.json` (mode 0600) and are never
printed by `proxy list`; `proxy link` is the one command that emits them and
says so.

### Streaming

```
tlgr watch [--events TYPES] [--exclude TYPES] [--chat CHAT] [--sender USER]
           [--since SEQ] [--no-follow] [--account all] [--print-cursor]
→ newline-delimited JSON to stdout, one frame per line
```

Push-driven from the daemon's event bus — nothing is polled. `--events`
accepts an event type, a group name (`message`, `read`, `presence`, `peer`,
`member`, `dialog`, `story`, `collection`, `call`, `bot`, `stars`, `secret`,
`account`, `sync`), a `raw:UpdateFoo` constructor name, `all`, or v1's names
(`new_message`, `chat_action`, `message_read`, …). An unknown selector is a
usage error, never an empty watch. Run `tlgr events list` for the vocabulary.

Each event frame is the envelope:

```json
{"seq": 91824, "ts": "2026-09-03T09:14:07Z", "account": "main",
 "type": "message_new", "payload": {...}, "chat_id": -1001234567890,
 "sender_id": 4242, "self_origin": false}
```

Control frames share the stream and are distinguishable by `type`: `meta`
first, `end` last, and `heartbeat`, `gap` and `lag` in between. A `gap` frame
means the replay window has passed and events were lost — a number, not
silence. `--results-only` prints v1's line shape
(`{event_type, chat_id, data}`) and drops the control frames.

`--since <seq>` replays the daemon's ring buffer first; `seq` is per account,
monotonic and persisted, so it survives a daemon restart.

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
