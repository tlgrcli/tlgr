# tlgr v2 command-surface style guide (binding for all design agents)

tlgr is "Telegram in your terminal": every feature of the official GUI clients, as a CLI that is equally usable by humans and by LLM agents (`--json`). The command surface must read like one product designed by one person, even though eight agents design it in parallel. Follow this guide exactly; the consolidation pass rejects deviations.

## 1. Shape

```
tlgr [global flags] <noun> [<sub-noun>] <verb> [<target>] [<args>] [--flags]
```

- **Nouns are singular** (`message`, `chat`, `contact`, `story`, `poll`), lowercase, no abbreviations except the sanctioned aliases below.
- **Two levels by default, three when a noun owns a sub-resource** the GUI shows as its own tab/screen: `chat member ban`, `chat admin promote`, `chat invite create`, `chat topic create`, `chat permission set`, `account session terminate`, `account password set`, `story viewer list`. Never four.
- **Verbs** come from this fixed vocabulary, in this order of preference: `list, get, create, send, edit, set, unset, delete, add, remove, pin, unpin, mute, unmute, archive, unarchive, block, unblock, join, leave, start, stop, open, read, search, download, upload, export, import, enable, disable, toggle, approve, deny, revoke, promote, demote, ban, unban, restrict, transfer, forward, react, vote, close, reopen, hide, unhide, watch`. Compound verbs are hyphenated only when unavoidable (`terminate-all`, `read-all`, `mark-unread`). Prefer a flag over a new verb (`chat clear --for-both`, not `chat clear-both`).
- **`get` returns one object; `list` returns a page**; `search` is a `list` with a query. `set` writes a value; `edit` opens/changes an existing object with several fields.
- **Existing commands keep their paths** (`message send/list/get/delete/search/edit/forward/pin/react/read`, `chat list/open/catchup/unread/members/posters/get/create/archive/mute/leave/typing`, `contact list/add/rename/remove/search`, `draft set/clear/list`, `user get/dialog-status/hide-stories`, `profile get/update`, `media download/upload`, `account add/import/list/switch/remove/rename/info/sync`, `daemon *`, `job *`, `config *`, `schema`, `agent whoami/exit-codes`, `watch`, and the shortcuts `send login logout status chats inbox catchup contacts dl up`). Extend them with flags; if a rename is truly better, keep the old path as an alias and say so.
- **Top-level nouns** (closed list; propose additions only with justification in `notes`): `account, auth, security, privacy, notify, settings, profile, contact, user, chat, folder, message, draft, media, sticker, gif, emoji, story, poll, reaction, todo, location, gift, premium, stars, business, bot, inline, webapp, payment, call, vc (video chat / group call), conference, boost, stats, admin-log (under chat), search, resolve, link, session (alias of account session), export, import, proxy, config, daemon, job, agent, schema, watch, events, help-center`. A domain agent uses only the nouns that belong to it; the mapping is given in the task.

## 2. Targets and references

- `<chat>` accepts: `@username`, numeric peer id (`-100…` for channels/supergroups, negative ids after `--`), `t.me/...`/`tg://` links, `me`/`saved` (Saved Messages), `+phone` only where a user is meant.
- `<user>` accepts `@username`, id, `+phone`, `me`.
- `<msg-id>` is the message id inside `<chat>`; message links are also accepted wherever a `<chat> <msg-id>` pair is expected (the CLI splits them).
- Topics: `--topic <id>` on any message command; `chat topic ...` for management.
- Multiple ids: variadic positional (`message delete <chat> <id>...`).

## 3. Flags (global conventions)

- Global flags work anywhere on the line: `--json`, `--plain`, `-a/--account`, `--results-only`, `--select`, `--dry-run/-n`, `--yes/-y`, `--no-input`, `--flood-wait-max`, `-v`.
- Every `list`/`search`: `--limit/-n N`, `--cursor TOKEN`, `--all` (walks pages inside the daemon), `--since <datetime|relative>`, `--until`, plus domain filters. JSON output is `Page[T]`: `{"items": [...], "has_more": bool, "next_cursor": str|null, "total": int|null}`.
- Durations: `30s`, `5m`, `2h`, `7d`, `forever`. Timestamps: ISO-8601 in and out (UTC `Z` in JSON; local in human tables).
- Booleans: paired flags `--silent/--no-silent`, or `on|off` positional for `set` verbs (`privacy set last-seen contacts`).
- Secrets: never as bare argv. `--password-env VAR` (default `TLGR_2FA_PASSWORD`), `--password-stdin`, `--password-file PATH`; same for bot tokens (`--token-env`).
- Files: `--file PATH` (repeatable for albums), `-` for stdin; downloads take `--out PATH|DIR`, `--stdout`.
- Destructive/irreversible (`delete`, `terminate`, `leave`, `ban`, `revoke`, account-level changes): require `--yes` when not on a TTY; support `--dry-run`.
- Formatting text: `--parse md|html|none` (default `md`), `--entities JSON` for explicit entities.
- Send-time options shared by every "send something" command: `--reply-to ID [--quote TEXT]`, `--topic ID`, `--silent`, `--schedule <ts|online>`, `--send-as <peer>`, `--effect ID`, `--no-preview`, `--spoiler`, `--ttl S`, `--noforwards` (`--protect`), `--typing[-auto]`, `--paid-stars N`.

## 4. Output

- JSON: one object; lists as `Page[T]`; mutations return the changed object or `{"ok": true, ...ids}`; idempotent no-ops return `"already": true`.
- Human: table for lists (choose 3–6 default columns), key/value for single objects.
- Exit codes are the existing table (0 ok, 1 generic, 2 usage, 3 empty, 4 auth, 5 not found, 6 permission, 7 rate limited, 8 retryable, 9 spam-flagged, 10 config, 11 daemon, 12 ipc, 13 indeterminate). `list` with zero items exits 0 unless the op declares `empty_exit=3`.

## 5. What to produce per command (the schema the consolidator expects)

```json
{
  "path": "chat member ban",
  "aliases": ["chat ban"],
  "summary": "Ban a member from a group or channel",
  "args": [{"name": "chat", "type": "chat", "required": true}, {"name": "user", "type": "user", "required": true}],
  "flags": [{"name": "--until", "type": "duration|datetime", "help": "...", "default": "forever"}, {"name": "--delete-history", "type": "bool"}],
  "mutating": true,
  "destructive": true,
  "paginated": false,
  "stream": false,
  "output": {"shape": "object", "fields": ["chat_id", "user_id", "until"]},
  "impl": {"telethon": "high-level: client.edit_permissions(chat, user, view_messages=False, until_date=...)", "raw": ["channels.EditBannedRequest"], "rights": "ban_users admin right", "notes": "basic groups: messages.deleteChatUser; layer_gap: false"},
  "covers": ["groups-channels-admin.ban-member", "groups-channels-admin.ban-with-duration"],
  "priority": "P0",
  "existing": "new | extends:<old path> | keeps:<old path>",
  "daemon": true
}
```

- `covers` must list every catalog id the command satisfies (fully or partially — say which in `notes`). Every catalog id in your slice whose `cli.feasibility` is `full`, `partial` or `control-only` must appear in at least one command's `covers`. Ids with `not-applicable` or `prohibited` go into a separate `uncovered` list with a one-line reason.
- Prefer fewer, richer commands over many thin ones: a GUI dialog with six toggles is one `set` command with six flags, not six commands. Target roughly 30–80 commands per group.
- `daemon: false` only for commands that must run without a connected account (login flows, config, local session files).
