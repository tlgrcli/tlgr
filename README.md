# tlgr

![GitHub Repo Banner](https://ghrb.waren.build/banner?header=tlgr%F0%9F%A7%AD&subheader=Telegram+in+your+terminal&bg=f3f4f6&color=1f2937&support=true)
<!-- Created with GitHub Repo Banner by Waren Gonzaga: https://ghrb.waren.build -->

Full Telegram account control from the terminal. Agent-friendly, daemon-based, with webhook event push.

```
pip install tlgr
```

> **For agents:** Authentication requires human interaction (phone code, 2FA). Run `tlgr account add` yourself first, then hand the CLI to your agent. See [AGENT.md](AGENT.md) for the full agent reference.

## Quickstart

```bash
tlgr config init                              # create config files
tlgr login +15551234567                       # authenticate (shortcut for account add)
tlgr daemon start                             # start background daemon
tlgr send @username "Hello from tlgr"         # send a message
tlgr chats --limit 20                         # list your chats
tlgr status                                   # check daemon status
```

## CLI

Every Telegram operation available as a single command. Three output modes: human-readable tables (default), JSON (`--json`) for agents, and plain TSV (`--plain`) for piping.

```mermaid
flowchart LR
    USER["You / Agent"] --> CLI["tlgr CLI"]
    CLI -->|"direct"| TG["Telegram API"]
    CLI -->|"IPC socket"| DAEMON["tlgr daemon"]
    DAEMON --> TG
```

### Shortcuts

Common operations are available as top-level commands for quick access:

```bash
tlgr send <chat> <text>                # message send
tlgr login <phone>                     # account add
tlgr logout <alias>                    # account remove
tlgr status                            # daemon status
tlgr chats                             # chat list
tlgr contacts                          # contact list
tlgr dl <chat> <msg_id>               # media download
tlgr up <chat> <path>                 # media upload
```

### Messages

```bash
tlgr message send <chat> <text>        # --file, --caption, --reply-to, --silent, --parse,
                                       # --schedule, --topic, --send-as, --split
tlgr message list <chat>               # --limit, --cursor, --all, --since, --until
tlgr message get <chat> <msg_id>       # full metadata
tlgr message delete <chat> <ids...>
tlgr message search <chat> <query>     # --from, --media-type, --cursor
tlgr message pin <chat> <msg_id>       # and: message unpin
tlgr message react <chat> <id> <emoji>
tlgr message read <chat>               # --up-to
tlgr message edit <chat> <id> <text>   # --typing N
tlgr message forward <from> <ids...> --to <chat>
tlgr message link <chat> <msg_id>      # shareable t.me link
tlgr message entity list <text>        # --parse md: what the formatting actually did
```

`message send` supports `--typing N` / `--typing-auto` to show a realistic
"typing…" indicator before the message lands. Text over 4096 UTF-16 units is
refused unless you pass `--split`.

That is the everyday tenth of the group. It also has `preview`, `compose`,
`summarize`, `translate`, `transcribe`, `report`, `thread list`, `view get`,
`read-receipt list`, `scheduled send`, `paid set`, `fact-check set`,
`dice list`, `effect list`, `game *`, `sponsored *`, `suggested *` and
`tone *` — 43 operations in all, generated from one registry, with generated
reference in [`docs/reference/message.md`](docs/reference/message.md).

`msg` is an alias for `message` (e.g. `tlgr msg send @user "hello"`).

### Drafts

Prepare a reply without sending it — you send (or discard) it later from any
Telegram client. The human-in-the-loop primitive for agents.

```bash
tlgr draft set <chat> <text>           # --reply-to, --parse
tlgr draft clear <chat>                # --all --yes to clear every draft
tlgr draft list                        # all non-empty drafts across chats
```

`draft set` returns the saved draft, and `draft list` reports marked chat ids
(`-100…` for channels) with `raw_id` beside them. See
[`docs/reference/draft.md`](docs/reference/draft.md).

### Chats

```bash
tlgr chat list                         # --type, --search, --limit, --unread
tlgr inbox                             # shortcut: chats with unread messages
tlgr chat members <chat>               # --admins, --search, --limit
tlgr chat posters <chat>               # distinct senders + message counts; --limit, --max-messages
tlgr chat get <chat>
tlgr chat create <name>                # --type group|channel, --members
tlgr chat archive <chat>
tlgr chat mute <chat> [duration]
tlgr chat leave <chat>
```

### Contacts

```bash
tlgr contact list
tlgr contact add <phone> [name]
tlgr contact rename <user>             # --first-name, --last-name (tags non-contacts too)
tlgr contact remove <user>
tlgr contact search <query>
```

### Users

```bash
tlgr user get <user>
tlgr user dialog-status <user>         # does THIS account have prior history with them?
tlgr user hide-stories <user>          # archive their stories for this account (--unhide)
```

`dialog-status` distinguishes "yes", "definitively no", and "cannot tell"
(exit 13) instead of guessing. Never infer "no history" from an entity
resolution error — see AGENT.md for why.

### Media

```bash
tlgr media download <chat> <msg_id>    # --out-dir
tlgr media upload <chat> <path>        # --caption
```

### Profile

```bash
tlgr profile get
tlgr profile update                    # --first-name, --last-name, --bio, --photo
```

### Accounts

```bash
tlgr account add <phone>              # authenticate a new account
tlgr account import <file.session>    # import an existing Telethon session (no re-auth); --alias
tlgr account list                     # (* = default)
tlgr account switch <alias>
tlgr account remove <alias>
tlgr account rename <old> <new>
tlgr account info [alias]
tlgr account sync [alias]             # refresh stored profile from live Telegram
```

`info` and `sync` take the alias positionally, but also honor the global
`-a/--account` flag; with neither, they fall back to the active account.
Session files are stored `0600` — they are full account credentials.

### Daemon

```bash
tlgr daemon start                     # --foreground
tlgr daemon stop                      # drains in-flight work rather than killing it
tlgr daemon status                    # running/ready/healthy/disconnected/version/protocol
tlgr daemon logs                      # --follow
```

#### Protocol v2

The CLI reaches the daemon over `~/.tlgr/daemon.sock`, created `srw-------`
with the connecting peer's uid checked on every request — v1 left it
world-writable with no authentication at all.

- **The account is always explicit.** The CLI resolves it (`-a` →
  `TLGR_ACCOUNT` → `[accounts] default` → active alias) and sends it. The
  daemon never picks one: with two accounts configured, v1 used whichever
  alias came first out of a set, so you could send from the wrong identity
  with no signal.
- **Handshake, and one restart.** A client newer than the running daemon
  restarts it exactly once and says so on stderr; `--no-daemon-restart`
  refuses instead (exit 11). Two `tlgr` commands racing with no daemon
  running produce exactly one daemon.
- **`status` distinguishes alive from working.** `running` is a live process;
  `ready` is a daemon that can serve. An account whose connection dropped is
  `degraded` and its requests answer exit 8 with a hint instead of
  `Cannot send requests while disconnected`; a revoked session is
  `needs_login` and answers exit 4.

### Global Flags

```
--json               JSON to stdout (for scripting and agents)
--plain              Stable TSV for piping
-a, --account TEXT   Account alias to use
--results-only       In JSON mode, strip envelope and emit only the primary result
--select FIELDS      In JSON mode, project comma-separated fields (supports dot paths)
--enable-commands    Comma-separated allowlist of enabled commands (sandboxing)
-n, --dry-run        Preview destructive operations without executing
-y, --force          Skip confirmations
--no-input           Never prompt; fail instead (CI/agent mode)
-v, --verbose        Verbose logging to stderr
--version / --help
```

### Environment Variables

All global flags can be set via environment variables:

| Variable | Equivalent |
|----------|------------|
| `TLGR_JSON=1` | `--json` |
| `TLGR_PLAIN=1` | `--plain` |
| `TLGR_ACCOUNT=alias` | `--account alias` |
| `TLGR_ENABLE_COMMANDS=cmd1,cmd2` | `--enable-commands cmd1,cmd2` |
| `TLGR_AUTO_JSON=1` | Auto-switch to JSON when stdout is piped (non-TTY) |

## Webhook -- Event Push

tlgr pushes Telegram events to an external HTTP endpoint in real time. Designed for agentic interfaces like [OpenClaw](https://github.com/openclaw) where an agent receives events and calls `tlgr` CLI commands to act.

```mermaid
flowchart LR
    TG["Telegram"] --> DAEMON["tlgr daemon"]
    DAEMON -->|"POST /hooks/agent"| AGENT["Your Agent"]
    AGENT -->|"tlgr --json message send ..."| CLI["tlgr CLI"]
    CLI --> TG
```

Configure in `~/.tlgr/webhook.toml`:

```toml
[webhook]
enabled = true
url = "http://127.0.0.1:18789/hooks/agent"
token = "shared-secret"
events = ["new_message", "message_edited", "message_deleted"]

[webhook.retry]
enabled = true
max_attempts = 3
backoff_base = 2

[webhook.filters]
chats = ["@important_channel"]
```

Events arrive as JSON with `Authorization: Bearer <token>`:

```json
{
  "event_type": "new_message",
  "timestamp": "2025-03-06T12:00:00Z",
  "account": "main",
  "data": { "..." }
}
```

## Gateway -- Background Jobs

tlgr also ships with a deterministic, always-on Gateway that runs background jobs on your Telegram account. Define declarative pipelines in `~/.tlgr/jobs.yaml` that automatically react to incoming messages -- auto-reply, auto-forward, filter by chat type, time of day, content, and more.

```mermaid
flowchart LR
    TG["Telegram event"] --> F["Filters"]
    F -->|"passed"| P["Processors"]
    P --> A["Actions"]
    A --> R["reply / forward / ..."]
```

A few examples of what you can do:

```yaml
jobs:
  # Auto-reply to all private messages
  - name: private-bot
    account: main
    filters:
      chat_type: private
    actions:
      - reply: "shut up i'm just a bot!"

  # Forward breaking news to your archive
  - name: news-forward
    account: main
    filters:
      chat_id: "@raw_feed"
      types: [text, photo]
      contains: [breaking]
    actions:
      - forward:
          to: ["@clean_feed", "@archive"]
          processors: [strip_formatting]

  # Night-mode auto-reply
  - name: night-mode
    account: main
    filters:
      chat_type: private
      time_of_day: "23:00-07:00"
    actions:
      - reply: "I'm sleeping. Will reply tomorrow."
```

Filters support full AND / OR / NOT composition, 20+ built-in filter types, 7 text processors, and a registry pattern for adding your own.

For the full Gateway reference -- filters, processors, actions, composition, extensibility -- see **[Gateway documentation](tlgr/gateway/README.md)**.

## Agent / Automation

tlgr is designed to be consumed by LLM agents and automation pipelines.

### Machine-readable schema

```bash
tlgr schema                            # full CLI schema as JSON
tlgr schema message send               # schema for a specific command
```

Agents can discover all commands, flags, positionals, types, and defaults
without parsing `--help`. For commands generated from the operation registry
the schema also carries JSON Schema (draft 2020-12) for the request *and* the
response, plus a generated example — so a call can be validated before it is
made. `tlgr agent whoami --json` reports `output_schema_version: 2`.

### Feature parity

```bash
tlgr agent parity                      # coverage of the pinned Telegram feature catalog
tlgr agent parity --json --uncovered   # every gap, by priority and domain
```

The answer to "can tlgr do X yet" without guessing. Every uncovered id is
either waived to a named later PR or reported as a gap; nothing in the report
is hand-maintained. The same report is generated into
[`docs/reference/PARITY.md`](docs/reference/PARITY.md).

### JSON envelope

Generated commands wrap their answer:

```json
{"ok": true, "op": "message.send", "result": {...}, "meta": {"request_id": "...", "elapsed_ms": 42}}
{"ok": false, "error": {"error": "...", "code": "RATE_LIMITED", "exit_code": 7, "wait_seconds": 30}}
```

`--results-only` prints the inner value in either case, which is v1's shape;
`--select` projects fields by dot path. Paginated operations return
`{items, has_more, next_cursor, total}` with an opaque signed cursor.

### Stable exit codes

```bash
tlgr exit-codes                        # print the exit code table
tlgr --json agent exit-codes           # as JSON
```

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic failure |
| 2 | Usage / parse error |
| 3 | Empty results |
| 4 | Auth required |
| 5 | Not found |
| 6 | Permission denied |
| 7 | Rate limited |
| 8 | Retryable error |
| 10 | Config error |
| 11 | Daemon error |
| 12 | IPC error |
| 13 | Indeterminate (unknown — not a negative) |
| 130 | Interrupted (SIGINT) |

### Command sandboxing

Restrict which commands an agent can run:

```bash
tlgr --enable-commands="message,chat,schema" send @user "hi"    # allowed
tlgr --enable-commands="message,chat,schema" account remove foo  # blocked (exit 2)
```

Or via environment: `TLGR_ENABLE_COMMANDS=message,chat,schema`.

For generated commands the allowlist is matched by canonical operation id and
enforced **inside the daemon**, so an alias cannot be used to get past it
(`--enable-commands message.list` permits `tlgr msg list`), and a blocked
operation exits 6. It is a usability guard, not a sandbox: anything that can
reach the socket can reach the session. See [SECURITY.md](SECURITY.md).

### JSON transforms

```bash
tlgr --json --results-only chat list           # strip pagination/envelope, emit only the chat array
tlgr --json --select "id,name" chat list       # project specific fields
```

### Auto-JSON for pipelines

Set `TLGR_AUTO_JSON=1` and tlgr automatically outputs JSON whenever stdout is piped (non-TTY), without requiring `--json`.

### Error hints

Errors include actionable recovery hints:

```
Error: No session found for account 'main'
  Session expired. Run: tlgr account add <phone>
```

## Configuration

Config files live in `~/.tlgr/`:

| File | Format | Purpose |
|------|--------|---------|
| `config.toml` | TOML | App defaults, daemon, accounts |
| `jobs.yaml` | YAML | Gateway job definitions |
| `webhook.toml` | TOML | Outbound webhook push |

```bash
tlgr config init                       # create defaults
tlgr config validate                   # check syntax + validate filter/action names
tlgr config path                       # print config directory
tlgr config keys                       # list all known config keys
tlgr config list                       # show current values
tlgr config get <key>                  # get a single value
tlgr config set <key> <value>          # set a value
tlgr config unset <key>                # reset to default
```

### config.toml

```toml
[defaults]
output = "human"

[accounts]
default = "main"

[daemon]
auto_start = true
log_level = "info"
```

## Multi-Account

```bash
tlgr account add +15551234567 --alias personal
tlgr account add +15559876543 --alias work
tlgr -a personal message send @friend "Hi"
```

To eliminate wrong-account mistakes in scripts and agents, enable strict
account selection — every command must then carry an explicit `-a <alias>`
(no default-account fallback; violations exit 2):

```bash
tlgr config set require_account true    # or per-invocation: TLGR_REQUIRE_ACCOUNT=1
```

Jobs can reference different accounts:

```yaml
jobs:
  - name: work-forward
    account: work
    # ...
```

## License

See [LICENSE](LICENSE) for license details.
