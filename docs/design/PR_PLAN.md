# tlgr v2 — PR plan

Twelve PRs after the foundation lands, following ARCHITECTURE §12.5. The foundation itself is folded into PR-1, which also carries `message` and `draft` so the registry is proven on the busiest group. Each PR is self-contained: ops + models + tests + generated docs + parity delta + deletion of that group's legacy module and v1 routes.

Three PRs are large enough to land as two stacked commits (PR-7, PR-10, PR-12); the split is noted in their rows. No PR is merged or renumbered relative to §12.5.

## PR-1 — `ops: land the operation registry on `message` and `draft``

- **Ops:** 42 (P0, P1, P2, P3)
- **Scope:** `message.*` (39), `draft.*` (3) plus the foundation from ARCHITECTURE §12 (registry, transport, models, dispatcher, renderer).
- **Catalog ids covered:** 196 (P0: 29); contributes to 0 more as `covers_partial`.
- **Depends on:** none — this PR *is* the foundation.
- **New models:** `Message` and everything hanging off it (§3.3), `Peer`, `Page[T]`, `Draft`, `Error`, `Event` envelope, `SendOptions`, `MediaRef`, `ReplyMarkup` (read side).
- **Tests:** registry contract test (every op: request struct ↔ Click params ↔ response example), `test_agentmd_compat.py`, fake-Telethon send/edit/forward/pin/read matrix, `--dry-run` gate on every mutating op, exit-code table.
- **Docs:** generated `COMMANDS.md` slice, `CHANGELOG.md` breaking-change table (§12.4), `AGENT.md` regenerated from the registry.
- **Risks:** the busiest group is also the riskiest place to prove the model; `parse_mode` default flip (md → none) and the `message list` envelope change are user-visible and need the documented config escape hatches.

## PR-2 — `auth: unattended login, sessions, password and passport`

- **Ops:** 52 (P0, P1, P2, P3)
- **Scope:** `auth.*`, `account.*` (incl. `account.session.*`, `account.password.*`, `account.passkey.*`, `account.website.*`), `passport.*`, `completion`.
- **Catalog ids covered:** 108 (P0: 8); contributes to 20 more as `covers_partial`.
- **Depends on:** PR-1 (registry, transport, secret handling).
- **New models:** `Session`, `PasswordState` (SRP), `LoginToken`/QR state, `Authorization`, `PassportForm`, `Account` (local record).
- **Tests:** pre-auth ops against the fake client (needs_auth=False path), SRP vectors, `--password-env/--password-stdin/--password-file` precedence, session-terminate confirmation gate, `account list/switch/rename` on a temp `~/.tlgr`.
- **Docs:** login recipes for agents (QR + env password), security notes (SEC-03).
- **Risks:** account-destroying verbs (`account delete`, `auth reset-account`, `account session terminate --all`) — all destructive, `--yes` off a TTY, `--dry-run` supported; secrets must never reach argv or logs.

## PR-3 — `chat: dialogs, folders and the chat-list surface`

- **Ops:** 47 (P0, P1, P2, P3)
- **Scope:** `chat.*` owned by dialogs (list/get/open/read/pin/mute/archive/clear/delete/leave/typing/unread/catchup/theme/wallpaper/ttl/notify/secret/saved/poster/…), `folder.*`.
- **Catalog ids covered:** 147 (P0: 23); contributes to 7 more as `covers_partial`.
- **Depends on:** PR-1 (Page, cursors), PR-2 (a real account to read dialogs with).
- **New models:** `Dialog`, `Folder`, `ChatSettings`/action bar, `NotifySettings`, `ChatTheme`, `Wallpaper`, dialogs cursor (`PageKind.DIALOGS`).
- **Tests:** folder/type/unread filter matrix, pinned ordering, marked-id correctness (COR-10), `chat catchup` digest shape, archive/unarchive idempotency (`already`).
- **Docs:** dialog filter cheat sheet; the `catchup` vs `sync catch-up` note.
- **Risks:** `chat get`/`chat list` merge two designers' flag sets — the flag surface is wide and needs a curated `--help`; id marking is a documented breaking change.

## PR-4 — `events: the update bus, daemon control, sync, network and export`

- **Ops:** 70 (P0, P1, P2, P3)
- **Scope:** `events.*`, `watch`, `daemon.*`, `sync.*`, `net.*`, `proxy.*`, `webhook.*`, `job.*`, `config.*`, `export.*`, `agent.*`, `schema`, `status`.
- **Catalog ids covered:** 202 (P0: 21); contributes to 156 more as `covers_partial`.
- **Depends on:** PR-1 (transport/NDJSON), PR-3 (dialog state to catch up on).
- **New models:** `Event` taxonomy (`EVENTS.md`), `UpdateState`/pts boxes, `DaemonStatus`, `FloodRecord`, `DeadLetter`, `Proxy`, `Job`, `TakeoutSession`.
- **Tests:** event-type registry test (every declared type constructible from a TL update), replay/ring-buffer, getDifference gap recovery, webhook signing + dead-letter drain, `job` scheduling with a frozen clock.
- **Docs:** `EVENTS.md` (the full taxonomy), daemon operations guide, takeout guide.
- **Risks:** the largest behavioural surface (streams, retries, backfill); `watch` changes from polling to bus-backed — keep the v1 output shape under `--results-only`.

## PR-5 — `contacts: contacts, users, blocking and the resolver`

- **Ops:** 38 (P0, P1, P2, P3)
- **Scope:** `contact.*`, `user.*`, `resolve.*`, `link.*`, `privacy.blocked.*` call sites.
- **Catalog ids covered:** 126 (P0: 20); contributes to 1 more as `covers_partial`.
- **Depends on:** PR-1, PR-3 (dialog cache), PR-4 (entity cache persistence).
- **New models:** `Contact`, `UserFull`, `PeerRef`/resolution result, `BlockList`, `TopPeer`.
- **Tests:** resolver matrix (@username, id, +phone, link, me) incl. the negative-id `--` case, access-hash cache hit/miss, import/sync round-trip with a phonebook file, COR-26/27/28 regressions.
- **Docs:** reference-syntax page (the `chat`/`user` grammar the whole CLI shares).
- **Risks:** phone-number resolution and contact import are privacy-sensitive: `--redact` must apply, and `contact sync` is destructive on the server side.

## PR-6 — `media: file pipelines, stickers, gifs and custom emoji`

- **Ops:** 56 (P0, P1, P2, P3)
- **Scope:** `media.*`, `sticker.*`, `gif.*`, `emoji.*`.
- **Catalog ids covered:** 119 (P0: 14); contributes to 36 more as `covers_partial`.
- **Depends on:** PR-1 (MediaRef), PR-4 (progress streaming), PR-3 (chat targets).
- **New models:** `MediaInfo`, `FileId`, `Transfer` (progress/cancel/retry), `StickerSet`, `StickerPack` (authoring), `Wallpaper`, `AutoDownloadRules`.
- **Tests:** upload/download round-trip with the fake client, resumable/cancelled transfers, `media upload` ↔ `message send --file` contract mirror, sticker set install/archive idempotency.
- **Docs:** file handling guide (`--file -`, `--out`, `--stdout`, albums).
- **Risks:** disk and memory behaviour on large files; the `sticker set` vs `sticker pack` split must be explained once and consistently.

## PR-7 — `chat admin: members, admins, invites, topics, permissions and the admin log`

- **Ops:** 86 (P0, P1, P2, P3)
- **Scope:** `chat.member.*`, `chat.admin.*`, `chat.admin-log.*`, `chat.invite.*`, `chat.topic.*`, `chat.permission.*`, `chat.request.*`, `chat.welcome.*`, `chat.username.*`, `chat.photo.*`, `chat.setting.*`, `chat.stats.*`, `chat.community.*`, `chat.direct.*`, `chat.sponsored.*`, `chat.revenue.*`, `boost.*`, plus `chat create/edit/join/convert/transfer/delete/import`.
- **Catalog ids covered:** 154 (P0: 12); contributes to 48 more as `covers_partial`.
- **Depends on:** PR-3 (chat model, participants cursor), PR-5 (user refs).
- **New models:** `Rights` (admin + banned, the shared rights vocabulary), `Participant`, `Invite`, `Topic`, `AdminLogEvent`, `Boost`, `ChatStats`.
- **Tests:** rights round-trip (promote → read back → demote), banned-until durations, participants pagination, admin-log filters, topic lifecycle, basic-group → supergroup conversion.
- **Docs:** rights vocabulary table (referenced by `bot default-rights set` and `business bot set`).
- **Risks:** largest single PR (86 ops) — land it as two stacked commits (members/admins/permissions, then invites/topics/settings); many ops are destructive and permission-dependent, so the 6/permission exit path needs real coverage.

## PR-8 — `story: stories, albums, viewers and stealth`

- **Ops:** 31 (P0, P1, P2, P3)
- **Scope:** `story.*` (post/edit/delete/list/feed/get/read/react/reply/share/pin/hide/album/viewer/blocklist/stealth/live/stats/search/export/can-post/watch).
- **Catalog ids covered:** 106 (P0: 13); contributes to 5 more as `covers_partial`.
- **Depends on:** PR-6 (media), PR-5 (close-friends list lives on `contact`), PR-4 (story events).
- **New models:** `Story`, `StoryAlbum`, `StoryViewer`, `StoryPrivacy` (reuses the privacy rule vocabulary), `StealthMode`.
- **Tests:** posting with each privacy rule, viewer pagination, read vs registered-view (`story read --register-view`), expiry/pin to profile, feed ordering.
- **Docs:** stories guide incl. the `user hide-stories` compatibility path.
- **Risks:** story privacy is easy to get wrong; the `story view`→`story read` fold must not silently change who sees you as a viewer.

## PR-9 — `content: polls, reactions, todos, locations and search`

- **Ops:** 43 (P0, P1, P2, P3)
- **Scope:** `poll.*`, `reaction.*`, `todo.*`, `location.*`, `search.*`, `message.link` call sites.
- **Catalog ids covered:** 109 (P0: 12); contributes to 0 more as `covers_partial`.
- **Depends on:** PR-1 (Message), PR-3 (chat refs), PR-6 (media for venue/location previews).
- **New models:** `Poll`, `PollResults`, `Reaction`/`ReactionCount`, `Todo`/`TodoItem`, `GeoPoint`, `Venue`, `LiveLocation`.
- **Tests:** vote/retract/close, quiz answers, paid (Star) reactions gating, todo toggle idempotency, live-location TTL, global/hashtag/post search cursors.
- **Docs:** reactions and tags page; poll/todo models.
- **Risks:** paid reactions spend Stars — mutating, `--dry-run` honoured, and the price is echoed before the spend.

## PR-10 — `bots: bots, inline mode, mini apps and payments`

- **Ops:** 79 (P0, P1, P2, P3)
- **Scope:** `bot.*`, `inline.*`, `webapp.*`, `payment.*`.
- **Catalog ids covered:** 159 (P0: 11); contributes to 31 more as `covers_partial`.
- **Depends on:** PR-1 (ReplyMarkup), PR-5 (bot resolution), PR-6 (inline media results).
- **New models:** `BotInfo`, `BotCommand`, `ReplyMarkup` (write side + the documented JSON schema), `InlineResult`, `WebAppSession`, `Invoice`, `PaymentForm`, `Receipt`, `Subscription`.
- **Tests:** conversation-style helpers (`bot command send` → answer), callback press with an expected update, inline query pagination, invoice → form → receipt flow against the fake client, url-auth accept/decline.
- **Docs:** `reply_markup` JSON schema (authored here, rendered by `message get`), bot/agent recipes.
- **Risks:** payment ops touch money: every one is mutating, never auto-confirms, and card/credential entry stays out of the CLI (forms are opened, not filled).

## PR-11 — `call: 1:1 calls, video chats and conferences`

- **Ops:** 45 (P0, P1, P2, P3)
- **Scope:** `call.*`, `vc.*`, `conference.*`.
- **Catalog ids covered:** 128 (P0: 9); contributes to 2 more as `covers_partial`.
- **Depends on:** PR-4 (call events on the bus), PR-5 (user refs), PR-7 (the manage-video-chats admin right).
- **New models:** `Call`, `CallLog`, `GroupCall`, `GroupCallParticipant`, `RtmpCredentials`, `ConferenceInvite`.
- **Tests:** signalling and metadata only (no media path): accept/decline/end state machine, participant mute/volume/raise-hand, rtmp key rotation, call-log pagination.
- **Docs:** explicit non-goal note: tlgr does signalling and metadata, not audio/video transport.
- **Risks:** tgcalls media is out of scope; commands must fail with a clear message rather than pretend to carry a call.

## PR-12 — `profile: profile, privacy, notifications, settings, business, premium, gifts and stars — plus the v1 cleanup`

- **Ops:** 90 (P0, P1, P2, P3)
- **Scope:** `profile.*`, `privacy.*`, `notify.*`, `settings.*`, `business.*`, `premium.*`, `gift.*`, `giveaway.*`, `stars.*`.
- **Catalog ids covered:** 249 (P0: 6); contributes to 55 more as `covers_partial`.
- **Depends on:** every earlier PR (this is the tail); PR-2 for account state, PR-5 for privacy rule targets.
- **New models:** `PrivacyRule`/`PrivacyKey`, `NotifySettings` (global scopes), `Settings`, `BusinessProfile`/`BusinessHours`, `PremiumStatus`, `Gift`/`UniqueGift`, `Giveaway`, `StarTransaction`.
- **Tests:** privacy key × rule matrix, notification scope defaults vs exceptions, business hours/location round-trip, gift lifecycle (send → upgrade → transfer), Stars balance/transaction paging.
- **Docs:** privacy and notification reference; the final parity report with no waivers.
- **Risks:** biggest tail (90 ops) — land as two stacked commits (profile/privacy/notify/settings, then business/premium/gift/giveaway/stars); the same commit deletes `ClientWrapper`, `daemon/ipc.py`, `ipc_client.py` and the v1 routes, so the compatibility test suite must be green first.

## Totals

| PR | title | ops | P0 ops | catalog ids | P0 ids |
|---|---|---|---|---|---|
| 1 | ops | 42 | 12 | 196 | 29 |
| 2 | auth | 52 | 11 | 108 | 8 |
| 3 | chat | 47 | 12 | 147 | 23 |
| 4 | events | 70 | 12 | 202 | 21 |
| 5 | contacts | 38 | 14 | 126 | 20 |
| 6 | media | 56 | 8 | 119 | 14 |
| 7 | chat admin | 86 | 8 | 154 | 12 |
| 8 | story | 31 | 9 | 106 | 13 |
| 9 | content | 43 | 8 | 109 | 12 |
| 10 | bots | 79 | 8 | 159 | 11 |
| 11 | call | 45 | 10 | 128 | 9 |
| 12 | profile | 90 | 4 | 249 | 6 |
| **all** | | **679** | **116** | **1803** | **178** |

Rolling gates are unchanged: coverage floor 85 % after PR-4, 90 % after PR-8, P0 parity 100 % and P1 ≥ 90 % before 2.0.0 final. `_consolidate/verify.py` is the gate's first check — it fails the build if a catalog id loses its command, if a v1 path stops being invocable, or if two commands claim the same id as fully covered.
