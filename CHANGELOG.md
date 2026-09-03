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
list, the per-chat settings and the chat folders.

Then the groups-and-channels group: 86 more operations covering members,
admins, invites, join requests, topics, the Manage screen, the admin log,
statistics, revenue and boosts. `tlgr/cli/legacy/chat.py` is deleted along
with the `/chat/create` and `/chat/members` IPC routes and
`ClientWrapper.create_chat` / `list_participants`; both v1 paths keep working
through the registry. Seven commands — `chat community create|list|set|ban`
and `chat welcome list|set|delete` — are registered and refuse with
`NOT_SUPPORTED` (exit 13) because they need MTProto layer 229 and Telethon
1.44 speaks 227.

`auth`, `account` and `passport` follow: 51 more operations covering logging
in, the Devices list, 2-step verification, connected websites, passkeys and
Telegram Passport. `tlgr/cli/legacy/account.py`, `completion.py` and `agent.py`
are deleted, not shadowed.

`media`, `sticker`, `gif` and `emoji` follow — 56 operations where v1 had two
(`media download` and `media upload`, one Telethon call each). The two v1
paths and their `dl`/`up` shortcuts still work; `tlgr/cli/legacy/media.py`,
the `/media/*` IPC routes and the two `ClientWrapper` methods behind them are
deleted rather than shadowed.

The update transport follows: `events`, `watch`, `daemon`, `sync`, `net`,
`proxy`, `config`, `job`, `webhook`, `export` and `agent` — 66 more
operations, 114 event types, and the `updates_sync_network` domain fully
accounted for.

Then `contact`, `user` and `resolve`: 38 operations covering the address book,
one person's profile, both blocklists, the phonebook and the reference
resolver every other group already leans on. `tlgr/cli/legacy/contact.py` and
`tlgr/cli/legacy/user.py` are deleted, along with their eight IPC routes and
the eight `ClientWrapper` methods behind them. The two semantics AGENT.md
freezes are unchanged: `user dialog-status` is still three-valued and still
exits 13 for "could not establish", and `user hide-stories` still reports
`already` and sends nothing when there is nothing to do.

`story` follows — 31 operations covering posting, the feed, viewers, albums,
the story blocklist, stealth mode and live stories, where v1 had exactly one
command (`user hide-stories`). That path still works, `--unhide` and bulk
peers included: `story hide` is now the single implementation of the toggle
and `user hide-stories` is a legacy path on it, so the two spellings cannot
drift apart.

`bot`, `inline`, `webapp` and `payment` follow — 79 operations where v1 had
none. The whole bot surface as a person uses it (profile cards, `/start` with a
hidden deep-link payload, slash commands, every kind of button, Telegram
Login), the whole surface a bot uses (answering queries, publishing commands,
the menu button, inline results, invoices), mini apps, and payments read
end to end. Nothing is deleted, because there was no v1 bot code to delete.

Three deliberate absences in that group are worth naming here.

* **tlgr never spends money.** `payments.sendPaymentForm`, `sendStarsForm`,
  `validateRequestedInfo` and `fulfillStarsSubscription` are not behind a flag
  or an environment variable — they are not on the surface at all.
  `payment form get` returns `payable_here: false` with the reason in the
  payload, and a test asserts the property against the registry rather than
  against a list of commands.
* **`webapp open` prints the mini-app URL and stops.** There is no `--open`:
  the URL carries the user's signed init data and is a credential.
* **A button that discloses something is not pressed without its flag.**
  `--share-phone`, `--share-geo`, `--poll`, `--peers`; without one, `bot press`
  prints what it would have sent and exits 2. A Pay button exits 6.

Five commands (`bot ephemeral send|delete`, `bot welcome list|set|delete`) are
registered and exit 13 `NOT_SUPPORTED`: they need API layer 229, which the
pinned Telethon does not speak. They exist so that "unavailable in this build"
is a different answer from "no such command".

One bug fix rides along: `message get --json` now actually prints
`reply_markup`. PR-1 declared the shape and nothing ever populated it, which
made its two keyboard-rendering P0 ids true only on paper — a caller could see
no button, so a caller could press none.

### Breaking

Every change below applies **only to commands generated from the operation
registry** — in this release that is the `message`, `draft`, `chat`,
`folder`, `auth`, `account`, `passport`, `media`, `sticker`, `gif`, `emoji`,
`story`, `events`, `watch`, `daemon`, `sync`, `net`, `proxy`, `config`, `job`,
`webhook`, `export`, `contact`, `user`, `resolve`, `bot`, `inline`,
`webapp` and `payment` groups,
`tlgr completion`, `tlgr status`, `tlgr schema` and the `agent` group. Commands still
hand-written under `tlgr/cli/legacy/` behave exactly as they did in v1 until
their own migration PR, at which point these rules apply to them too.

No documented command path disappears. Every migrated operation declares its
v1 paths, so `tlgr send`, `tlgr msg list`, `tlgr message react` and the rest
still work; `tests/test_agentmd_compat.py` asserts it, and asserts that every
JSON key v1's `AGENT.md` documented is still there — except for the
thirteen changes in the table below, which is the whole list.

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
| 10 | `account add <phone>` | prompted for the api_id/api_hash and the code, and finished the login in one process | starts the login and returns the next command (`auth verify-code`) | a daemon cannot prompt and an agent has no `input()`. `--bot` still finishes in one call; credentials come from `--api-id` and `--api-hash-env`. See "Logging in" in `AGENT.md` |
| 11 | `account remove` | `{"removed": "work"}` — the alias as a string | `{"alias": "work", "removed": true, "server_logout": false}` | `--select alias` yields the old value. `removed` is now the answer to "did it happen", and `account logout` is the command that revokes the authorization server-side |
| 12 | `account switch` / `account rename` | `{"active": "work"}` / `{"old": …, "new": …}` | `{"ok": true, "account": "work", "already": …}` / the same plus `old`/`new` | `old` and `new` are unchanged on `rename`; `--select account` replaces `active` |
| 13 | An empty paginated result | `{"total": 0}` — a different shape from a non-empty page | `[]` under `result` (`{"items": [], …}` with `--results-only`) | this was a bug: `omit_defaults` dropped the empty `items` list, so "no results" and "some results" had different shapes. Nothing to migrate — the shape is now the one the non-empty case always had |

| 8 | `media.download` | `{path, msg_id}` for one file | `Page[Downloaded]`: `{items: [{msg_id, path, bytes, kind, …}], has_more}` | both v1 keys survive on every item; one invocation can now name several ids, an album or `--all`. `--results-only \| jq -r '.items[0].path'` is the one-file case |
| 9 | `media.upload` | `{id, chat_id}` | `{chat_id, msg_id, msg_ids, kind, …}` | rename only: `id` became `msg_id`, beside `msg_ids` for an album. `--select msg_id` reaches it |

Five more shapes changed in the update-transport groups, all of them list or
status envelopes:

| # | Change | v1 | v2 | Migration |
|---|---|---|---|---|
| 8 | `events.watch` | one line per new message, `{event_type, chat_id, data}` | the full envelope: `{seq, ts, account, type, payload, chat_id, sender_id, self_origin}`, plus `meta`/`end`/`heartbeat`/`gap`/`lag` control frames | `--results-only` prints v1's exact line shape and drops the control frames |
| 9 | `daemon.status` | `{running, pid, uptime_seconds, accounts, connections, disconnected, healthy, jobs}` | the same keys plus `ready`, `version`, `protocol`, `layer`, and a per-account state machine under `accounts` | `accounts` is a list of objects rather than of aliases; `connections` and `disconnected` are unchanged, and `--select connections` reaches the old shape |
| 10 | `job.list` | `{jobs: [...]}` | `Page[JobState]` | `--results-only` yields `{items, has_more, next_cursor, total}` |
| 11 | `config.list` | the raw TOML document | `Page[ConfigEntry]`, one row per key with `value`, `default` and `source`; secrets redacted | `--defaults` includes keys still at their default; `config get <key>` is the point lookup |
| 12 | `config.keys` | `{keys: {name: {section, key, description}}}` | `Page[ConfigKey]` with `type`, `default`, `scope`, `requires_restart` and `help` | key names gained a section prefix (`idle_timeout` → `daemon.idle_timeout`); both spellings are accepted by `config get`/`set`/`unset` |

Three more changed in the contact and user groups:

| # | Change | v1 | v2 | Migration |
|---|---|---|---|---|
| 10 | `contact.list`, `contact.search` | `{"contacts":[…],"has_more":…}` | `Page[Contact]` | `--results-only` yields `{items, has_more, next_cursor, total}`; every v1 row key (`id`, `name`, `username`, `phone`) is still there, and `phone` is now normalised to E.164 |
| 11 | `contact.add` | `{"added": true, "user_id": 123}` | the same two keys plus `imported`, `retry`, `popular_importers` and `reason` | additive. `reason` is filled when the import came back empty, because "no such account" and "the owner hides their number" are indistinguishable and v1 reported the first |
| 12 | `user.get` | `{"id","first_name","username","bio","is_bot","status","stories_hidden",…}` | the same keys, plus everything `users.getFullUser` carries | additive; `--select` reaches any of it. `--field` is gone: the global `--select bio --results-only` does the same thing on every command |

Two more in the groups-and-channels group:

| # | Change | v1 | v2 | Migration |
|---|---|---|---|---|
| 10 | `chat.member.list` (`chat members`) | `{"members":[{"id","first_name","last_name","username","is_bot"}]}` | `Page[Participant]`, each row keeping its `ChannelParticipant*` wrapper: `status`, `rank`, `date`, `inviter_id`, `promoted_by`, `kicked_by`, `admin_rights`, `banned_rights` | `--results-only` yields `{items, has_more, next_cursor, total}`; `id`, `username` and `is_bot` are unchanged, and `first_name`/`last_name` are joined into `name` (`--select name` reaches it). The dropped wrapper was why v1 could list members but not say whether one was banned or merely restricted |
| 11 | `chat.create` | `{"id","name","type"}` with `--type group\|channel` | `{"id","type","title","username","invite_link","added","missing"}` with `--type group\|supergroup\|channel\|forum` | `name` became `title` (`--select title`), and `--type group` still means the legacy basic group. `missing` names every seed member the server refused, instead of dropping them |

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

- **The `story` group.** 31 operations covering the whole story surface:
  posting (with the audience vector, media areas, albums, reposts and a
  soundtrack), the stories bar, a peer's active/profile/archive/album grids,
  reading, reacting, replying, sharing, pinning, hiding, viewers, the
  story-only blocklist, stealth mode, hashtag and location search, statistics,
  a bulk export and live stories. Four of Telegram's shapes drive the design
  and are worth knowing before scripting against them:
  - **Reading a story and being seen watching it are different calls.**
    `story read` sends `stories.readStories`, which clears *your* unread ring
    and tells the poster nothing; `--register-view` is what calls
    `stories.incrementStoryViews` and puts you in their viewer list. It is
    opt-in because an agent that silently appears there is a privacy bug.
  - **The audience is an ordered vector**, `[base rule, allows…, disallows…]`,
    which is the only way `--privacy contacts --exclude @bob` can mean
    "contacts, except Bob". `--privacy selected` with no `--allow` is refused
    rather than posted to nobody, and channel stories ignore the vector.
  - **One id has three TL shapes.** A feed row can be a `storyItemSkipped`
    placeholder and a gone story a `storyItemDeleted`; both come back with
    `skipped: true` / `deleted: true` rather than as a story with no caption.
    `story list` hydrates placeholders by default.
  - **The feed pages on an opaque state, not an offset.**
    `stories.getAllStories` returns a `state` that the next call sends back
    with `next`; `--cursor` carries both, and `--refresh` re-sends the stored
    state and reports `already: true` when nothing changed.
- **`tlgr user hide-stories` is now `tlgr story hide`.** The v1 path, its
  `--unhide` flag and its four keys (`user_id`, `username`, `hidden`,
  `already`) are unchanged; `story unhide` is the canonical inverse and
  `--all` collapses the whole stories bar.
- **`story viewer list --csv PATH` and `story export`** are the two things the
  official clients have no button for: the viewer list as a file, and every
  story with its media on disk.
- **The content groups: `poll`, `reaction`, `todo`, `location` and `search`.**
  43 operations covering polls and quizzes, the whole reaction surface,
  checklists, places and live locations, and search outside a single chat.
  Three of Telegram's shapes drive most of the design and are worth knowing
  before scripting against them:
  - **A poll answer is opaque bytes, not an index.** `poll.answers[i].option`
    is assigned by the server, and `--shuffle` means the order a client shows
    is not the order the server stores. Every command that names an answer
    resolves the caller's index against a fresh copy of the poll, and hands
    the identifier back as `options[].option_b64`.
  - **`sendReaction` carries the whole desired set.** `reaction add` reads the
    reactions this account already has and resends them with the new one
    appended, so adding a second reaction no longer removes the first;
    `--replace` asks for the old behaviour explicitly.
  - **Global search pages on a triple.** `(offset_rate, offset_peer,
    offset_id)` goes into `--cursor` whole; a message id alone restarts the
    walk at the top of the first chat.
- **`tlgr message react` and `tlgr msg react` are now `reaction add`.** The
  operation moved group; the paths, and the `reacted`/`msg_id`/`emoji` keys,
  did not. `tlgr message unreact` is a new alias of `reaction remove`.
- **`tlgr message send --poll JSON` works**, and takes the same fields as
  `poll create` because it calls the same builder.
- **Star spending is never implicit.** `reaction pay` requires `--stars N`
  with no default, and `search post` does free price discovery (`--quota`)
  before refusing to fall through to a paid search without `--pay-stars N`.
- **`location preview`** renders a map thumbnail from the webfile data centre,
  and **`poll stats get`** follows the `STATS_MIGRATE` redirect to the stats
  DC — both through a borrowed sender, as Telethon's own `get_stats` does.
- **The `events`, `watch`, `daemon`, `sync`, `net`, `proxy`, `config`, `job`,
  `webhook`, `export` and `agent` groups, generated from the registry.** 66
  operations. `tlgr/cli/legacy/watch.py`, `daemon_cmd.py`, `job.py`,
  `config_cmd.py` and `agent.py` are deleted, along with the v1 `/daemon/*`
  and `/job/*` IPC routes.
- **The complete event taxonomy.** 114 types covering every one of the 163
  `Update*` constructors Telethon 1.44 can parse, plus the five Telegram has
  added since; four containers are listed internal with a reason, and a test
  checks the table against the installed Telethon so an upgrade that adds a
  constructor fails in the run that upgrades it. `tlgr events list` prints it,
  `tlgr events get <type>` prints one row with its payload and sequence box,
  and `docs/design/EVENTS.md` is the prose form.
- **`tlgr watch`, push-driven.** The daemon holds one `events.Raw` handler per
  account and a watcher is a bounded queue on the bus. Select by type, group,
  `raw:Constructor` or `all`; `--since <seq>` replays the ring buffer with a
  `gap` frame when it cannot reach that far back; `--exclude`, `--chat`,
  `--sender`, `--topic`, `--account all`, heartbeats, `lag` frames and
  `--print-cursor`.
- **`tlgr events replay`, `events decode`.** Replay a buffered range without
  following it, or decode a raw TL update — or an encrypted push payload,
  which `events decode --push` decrypts and classifies (`SESSION_REVOKE`
  means this session was terminated).
- **`tlgr sync status | catch-up | difference | reset | backfill`.** The
  update transport, made inspectable: pts/qts/seq, the per-channel table with
  `access_hash_known` (a channel without one is skipped by catch-up and looks
  idle rather than broken), an explicitly-run `getDifference` that does *not*
  advance the stored pts unless asked, a re-baseline for a corrupted state,
  and id-range backfill for a box that overflowed.
- **`tlgr net status | ping | dc list | dc nearest | usage get`.** Which DC,
  which transport, which proxy, and how far this host's clock has drifted — a
  drift over 30 s is warned about, because MTProto derives `msg_id` from local
  time and the server drops anything outside its window with no error.
- **`tlgr proxy add | list | set | remove | test | link`.** SOCKS5, HTTP and
  MTProxy, `tg://proxy` and `t.me/proxy` links both parsed, credentials in a
  0600 store and never in argv. `proxy test` probes through a throwaway
  in-memory session so it cannot become the account's update connection.
- **`tlgr config keys | list | get | set | unset | validate`** over a
  machine-readable catalogue of 34 documented keys with types, defaults and
  restart requirements, and **`config server get`, `config app get`,
  `config info get`, `config country list`, `config promo get`** for
  Telegram's own configuration. `config app get --frozen` surfaces the freeze
  fields that turn a bare `FROZEN_METHOD_INVALID` into an appeal link. The
  server's suggestion list stayed where the account group put it, as
  `account suggestion list`; PR-4 added `--chat` to it for the per-chat
  nudges.
- **`tlgr daemon status | reconnect | save-state | flood list | flood clear |
  dead-letter list | send | delete`.** `running`, `ready` and `healthy` are
  three answers to three questions; the flood store Telethon forgets on exit
  is listable and clearable; the dead-letter file is drainable.
- **`tlgr job add` without an editor.** Flags, `--from-file -`, or `--edit`
  for the v1 behaviour, and **`job test`**, which names every filter node and
  why it passed or rejected — the missing piece when a rule silently never
  fires.
- **`tlgr export start | status | end | message download | account download`.**
  Takeout as the mode it is: the session wraps every later call in
  `invokeWithTakeout`, and `TAKEOUT_INIT_DELAY` is reported as
  `RATE_LIMITED` with `retry_after` rather than slept through.
- **`tlgr agent capabilities`** separates what this build *cannot* do (the
  layer gap) from what this account may not reach (premium, bot, admin) from
  what tlgr *will not* do (fake a read receipt, suppress typing status,
  misrepresent presence, pass an integrity attestation, execute a payment) —
  with the reason for each.
- **`tlgr schema events | config | errors | exit-codes | all`**, and
  `tlgr agent exit-codes --errors`, which prints the RPC-error taxonomy with
  the field a regex error captures (`FLOOD_WAIT_42` is a wait of 42 seconds,
  not a distinct error).
- **A production-home guard.** A tlgr home carrying a `.production` marker is
  refused unless `TLGR_ALLOW_PRODUCTION_HOME=1`: two processes on one home
  share session files, and Telegram revokes an auth key it sees two clients
  on, so a development build pointed at a live home breaks it rather than
  degrading it.

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
- **The `auth`, `account` and `passport` groups, generated from the registry.**
  51 operations. Logging in is now a *sequence of commands* rather than one
  held-open process: `auth send-code` writes the pending login (phone,
  `phone_code_hash`, code type) to `<account>/login-state.json` at 0600 and
  `auth verify-code` finishes it from another process, so an unattended login
  works. `auth qr` streams QR tokens and re-exports each one as it expires;
  `auth recover`, `auth resend-code`, `auth tos`, `auth login-email set`,
  `auth code list` (read the login code Telegram delivered to chat 777000 —
  which is what makes scripted multi-account onboarding possible) and
  `auth reset-account` complete the flow.
- **`account session *`** — the Devices list, with the two deadlines nobody
  can derive by hand: `deny_deadline` (when an unconfirmed login stops being
  deniable because Telegram auto-confirms it) and
  `sensitive_actions_eligible_at` (what `SESSION_TOO_FRESH_X` counts down to).
  Terminate one, several, or all others; confirm or deny a new login; approve
  another device's QR login; set per-session call/secret-chat permissions and
  the account-wide inactive-session TTL.
- **`account password *`** — 2-step verification: status, set, change,
  remove, the 7-day reset, and a temporary payment password. The SRP loop is
  written once (`inputCheckPasswordEmpty`, then refetch on `SRP_ID_INVALID`,
  because an `srp_id` is single-use) and every sensitive operation reuses it.
  `password change` **refuses** when the account holds Telegram Passport
  documents unless `--keep-passport` acknowledges the loss, because the
  secure secret is encrypted under the password and Telethon's helper drops
  it silently.
- **`account website *`, `account passkey *`** — the connected-websites list
  is a *different* list from Devices and the one people forget; passkeys are
  auditable but never creatable from here, because the server only accepts
  the relying-party id `telegram.org`.
- **`account logout`** — v1 had no way to revoke an authorization: `account
  remove` deleted the local files and left the session alive in every other
  client's Devices list forever. Logout revokes it, keeps the returned
  `future_auth_token` (0600, capped at 20) for a code-less re-login, and
  keeps the alias; `account remove --logout` does both.
- **`account check`** — the distinction `daemon status` cannot make:
  `authorized` / `revoked` / `banned` / `deactivated` / `frozen` / `offline`,
  one row per account, reported rather than raised so one dead account cannot
  hide the health of the others. `frozen` carries Telegram's own appeal URL.
- **`account export` / `account import`** — a StringSession or a session file,
  in and out. The export is a bearer credential: it goes to a 0600 file unless
  `--stdout` says printing it is intended, and the reply warns that one auth
  key on two live connections earns `AUTH_KEY_DUPLICATED`.
- **`passport *`** — list the stored documents, read what a service is asking
  for, delete documents, verify a phone or email. `passport authorize` is
  registered and refuses with `NOT_SUPPORTED`: it needs a secure-value crypto
  stack (AES-256-CBC plus a SHA-512 KDF over the cloud password) that Telethon
  does not provide, and shipping a half-correct implementation of a format
  carrying identity documents is worse than not offering it.
- **The `media`, `sticker`, `gif` and `emoji` groups.** 56 operations where v1
  had two. `media download` gains ranged and resumable reads, parallel
  connections, server-hash verification, thumbnails (including the stripped and
  vector ones, which cost no request at all), profile photos, stories, web
  files, map previews, albums, `--all` and background transfers; `media upload`
  gains albums, every media kind, spoilers, self-destruct timers, video covers,
  paid media, `--no-send` and a pre-flight against the server's own limits.
  `media get` answers "what is this" without fetching a byte, `media list` and
  `media search` are the shared-media tabs, `media export` archives a chat with
  a resumable ledger, and `media transfer list/stop/retry` is the Downloads
  panel. `sticker`/`emoji` cover sets (install, archive, reorder, search) and
  packs you own (create, add, replace, reorder, delete); `gif` covers the saved
  shelf and the inline search bot.
- **A transfer is a thing with a lifetime.** `--background` hands a download or
  upload to the daemon and returns a job id; the transfer keeps its `.part`
  file when cancelled, and a retry re-fetches the source first because a queued
  transfer is holding an expired `file_reference`.
- **The `call`, `vc` and `conference` groups.** 45 operations covering 1:1
  calls, video chats, livestreams, RTMP, live stories and conference call
  links: ring, answer, decline, hang up, rate, the Calls tab with its service
  messages decoded, incoming-call streaming, video-chat creation and
  scheduling, recording, muting and moderation, invite and speaker links, RTMP
  credentials, the in-call chat, participants, and call links. **tlgr carries
  no audio or video** — there is no tgcalls binding behind any of it — so
  every shape that could be mistaken for participation reports `media:
  "none"`, and the operations that would need a real media engine
  (`vc join --params-json`, `vc video set --screen --on`, `call signal`) take a
  payload rather than inventing one. The one thing this does better than a GUI
  is `vc download`: it cannot play a livestream and it can record one.
  Conference joining, participant removal and in-call encryption need a signed
  `e2e.chain` block, which tlgr accepts from an external implementation
  (`--block`, `--public-key`) and refuses to fake — the refusal is a usage
  error naming the missing piece, not a failed RPC.
- **`tlgr agent parity`** — coverage of the pinned feature catalog by
  priority and domain, with every uncovered id either waived to a named PR or
  reported as a gap. `--uncovered` prints the full list; `docs/reference/PARITY.md`
  is the same report, generated. Neither number is hand-maintained.
- **Generated reference docs.** `docs/reference/message.md`, `draft.md`,
  `chat.md`, `folder.md`, `auth.md`, `account.md`, `passport.md`, `media.md`,
  `sticker.md`, `gif.md`, `emoji.md`, `agent.md` and `PARITY.md` come out of
  the registry via `make docs` / `make parity`;
  `tests/test_docs_fresh.py` fails the build on a stale page, so a flag cannot
  ship undocumented.
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

- **A paginated operation with no rows answers `[]`, not `{}`.** `Page.items`
  defaults to an empty list and models omit defaults, so an empty page encoded
  to `{}` and the envelope left `result` as an empty *object* — `for row in
  result` then iterated dict keys and reported nothing wrong. Every paginated
  operation now answers with a list. No correct consumer could have depended
  on the old shape, so this is a fix rather than a breaking change.
- **`from tlgr.models import DraftCleared` raised ImportError.** The name was
  in `__all__` but never imported.

- `tlgr account add` left the Telethon session database world-readable (0644
  under a default umask) while `account import` chmod-ed it to 0600. A session
  file is a complete account credential; both paths now secure the database
  and every sqlite sibling Telethon creates.
- `account info` and `account sync` ignored the global `-a/--account` and
  acted on the *active* account instead — and `sync` writes, so a stored
  profile record was overwritten under an alias the caller never named. Every
  account operation now resolves the alias in one place: positional, then
  `-a`, then the active account.
- A test that forgot the `tlgr_home` fixture operated on the developer's real
  `~/.tlgr`. `tests/conftest.py` now points `TLGR_HOME` at a throwaway
  directory whenever it is unset.
- **`tlgr watch` no longer polls.** v1 asked the daemon for `chat list` every
  two seconds and then `message list` per chat — thirty round trips a minute
  whether or not anything happened — and could only ever report new messages.
  An edit, a deletion, a read receipt, a reaction and every service message
  were invisible.
- **`tlgr daemon status` distinguishes alive from working.** v1 reported the
  clients the daemon *held*, so a client whose connection had died was still
  listed and the daemon still called itself healthy (COR-13, COR-37).
- **A config key with a typo is an error, not a default.** v1 read the file
  with `raw.get(key, default)` at every call site, so a misspelled key or a
  wrong type silently did nothing.
- **An unknown event name is refused where it is written.** `jobs.yaml` and
  `webhook.toml` dropped a name they did not recognise, so a typo produced a
  job or a webhook that never fired and never said why.
- **An empty paginated result is `[]`.** It was the page object itself, which
  reads as one row of metadata.
- **`sync difference` matched the wrong TL class names**, so every reply
  looked like a slice and the probe looped.

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

- **`message.react` as an operation id.** It is `reaction.add`, and both v1
  paths still resolve to it. `models.ReactResult` went with it; the reaction
  group answers with `models.reaction.ReactionResult`, which adds `mine` — the
  full set of reactions this account holds after the call, which is what the
  next `sendReaction` has to resend.

- **The second implementation of the "Hide Stories" toggle.** `story hide`
  owns it, and `tlgr user hide-stories` is declared as its legacy path rather
  than kept as an operation of its own — one alias, one implementation.
  `tlgr user get` still reports `stories_hidden`.

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
