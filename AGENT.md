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

Always use `--json`. Responses are JSON objects. Errors also go to stdout as JSON with `exit_code != 0`.

## Pagination

List commands return `has_more` (bool) and `next_cursor` (opaque string). Pass `--cursor <next_cursor>` to get the next page. Stop when `has_more` is `false`.

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

## Commands

### Messages

```
tlgr message send <chat> <text> [--file PATH] [--caption TEXT] [--reply-to ID] [--silent]
→ {"id": 123, "chat_id": -100123, "date": "..."}

tlgr message list <chat> [--limit N] [--cursor TOKEN] [--sender] [--media]
→ {"messages": [...], "has_more": true, "next_cursor": "..."}

tlgr message get <chat> <msg_id>
→ {"id": 123, "text": "...", "sender": {...}, "media": {...}, ...}

tlgr message delete <chat> <id1> [id2 ...]
→ {"deleted": 2}

tlgr message search <chat> <query> [--limit N] [--cursor TOKEN] [--local] [--regex PATTERN]
→ {"messages": [...], "has_more": false}

tlgr message pin <chat> <msg_id>
→ {"pinned": true, "msg_id": 123}

tlgr message react <chat> <msg_id> <emoji>
→ {"reacted": true, "msg_id": 123, "emoji": "👍"}

tlgr message read <chat> [--up-to MSG_ID]
→ {"read": true, "chat_id": -100123}

tlgr message edit <chat> <msg_id> <text> [--typing SECONDS]
→ {"edited": true, "id": 123, "chat_id": -100123, "date": "..."}

tlgr message forward <from_chat> <to_chat> <id1> [id2 ...]
→ {"forwarded": 2, "ids": [200, 201]}
```

`message send` also supports `--typing SECONDS` (show "typing…" before sending, max 60s)
and `--typing-auto` (duration estimated from text length) for human-like sends.

### Drafts

Drafts are the human-in-the-loop primitive: prepare a reply without sending;
the user sends or discards it from any Telegram client.

```
tlgr draft set <chat> <text> [--reply-to MSG_ID]
→ {"draft": true, "chat_id": -100123, "text": "..."}

tlgr draft clear <chat>
→ {"cleared": true, "chat_id": -100123}

tlgr draft list
→ {"drafts": [{"chat_id": ..., "chat_name": ..., "chat_username": ..., "text": ..., "date": ..., "reply_to": ...}]}
```

### Chats

```
tlgr chat list [--type user|group|channel] [--search TEXT] [--unread] [--limit N] [--cursor TOKEN]
→ {"chats": [{"id": ..., "name": ..., "type": ..., "username": ...,
              "unread_count": 3, "last_message": {"id": ..., "date": ..., "out": false, "text": "..."}}],
   "has_more": true, "next_cursor": "..."}

tlgr inbox [--type user] [--limit N]        # shortcut for chat list --unread

tlgr catchup [--type user] [--limit-chats N] [--per-chat N]   # shortcut for chat catchup
tlgr chat catchup [--type ...] [--limit-chats N] [--per-chat N]
→ {"chats": [{...chat info..., "unread_count": 3,
              "messages": [{"id": ..., "date": ..., "out": ..., "reply_to": ..., "text": ...,
                            "sender": {"id": ..., "name": ..., "username": ...}}]}]}
# "What did I miss?" — every unread chat with its recent messages in one call.
# READ-ONLY: emits no read receipts. Start every session/wake with this.

tlgr chat open <chat> [--limit N] [--no-read]
→ {"chat_id": ..., "marked_read": true, "messages": [...same shape as catchup...]}
# Open a chat the way a human would: fetch recent history AND emit a read
# receipt (visible to the other side — that's the point; it humanizes you).
# Choose your reading mode deliberately:
#   - loud (visible):  chat open            → history + read receipt
#   - silent (peek):   chat open --no-read, or message list
# Read before you act: pull real history instead of trusting summaries.

tlgr chat members <chat> [--admins] [--search TEXT] [--limit N]
→ {"members": [{"id": ..., "first_name": ..., "last_name": ..., "username": ...,
                "is_bot": false, "is_deleted": false, "is_contact": false, "is_self": false}]}

tlgr chat get <chat>
→ {"id": ..., "name": ..., "type": ..., "username": ...}

tlgr chat create <name> [--type group|channel] [--members USER1 USER2]
→ {"id": ..., "name": ..., "type": ...}

tlgr chat archive <chat>
→ {"archived": true, "chat_id": ...}

tlgr chat mute <chat> [duration_seconds]
→ {"muted": true, "chat_id": ...}

tlgr chat leave <chat>
→ {"left": true, "chat_id": ...}

tlgr chat typing <chat> [--duration SECONDS]
→ {"typing": true, "chat_id": ...}
```

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
```

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

tlgr schema [command_path...]
→ {"schema_version": 1, "build": "2.0.0", "command": {...}}
```

### Daemon

```
tlgr daemon start [--foreground]
tlgr daemon stop
tlgr daemon status
→ {"running": true, "pid": 12345, "uptime_seconds": 3600, "accounts": ["main"]}
```

### Streaming

```
tlgr watch [--chat CHAT1 --chat CHAT2]
→ newline-delimited JSON to stdout, one event per line
```

## Error Response Shape

```json
{"error": "message text", "code": "RATE_LIMITED", "exit_code": 7, "wait_seconds": 30}
```

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

## Self-Discovery

Run `tlgr schema --json` for the full machine-readable CLI schema with parameter types, defaults, and example responses.
