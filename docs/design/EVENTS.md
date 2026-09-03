# tlgr events

**Status:** final. The envelope, the delivery guarantees and the whole type
vocabulary are settled; new types are additive.
**Applies to:** `tlgr` 2.x, protocol 2
**Companion documents:** `ARCHITECTURE.md` §3.7 (envelope), §6.5 (bus).

An event is something that happened to an account, normalised by the daemon
and delivered to anyone watching: a `GET /v1/events` stream, a webhook, or a
gateway job. This document says what one looks like, what tlgr promises about
delivery, and which types exist today.

---

## 1. The envelope

Every event, of every type, is this shape:

```json
{
  "seq": 91824,
  "ts": "2026-09-02T09:14:07Z",
  "account": "work",
  "type": "message_new",
  "payload": { "…": "type-specific" },
  "chat_id": -1001234567890,
  "sender_id": 777,
  "self_origin": false
}
```

| Field | Meaning |
|---|---|
| `seq` | Per-account, monotonic, **persisted**. The cursor for `--since`. |
| `ts` | RFC-3339 UTC, when the *daemon normalised* it — not when Telegram sent it. The message's own date is in the payload. |
| `account` | The alias the event belongs to. Events are never merged across accounts. |
| `type` | A lowercase `snake_case` noun-verb from §3. |
| `payload` | Type-specific, and always **models** — never a Telethon `to_dict()`. There is no `datetime` and no `bytes` anywhere in it. |
| `chat_id` | Denormalised for cheap filtering. Marked (`-100…` for channels). |
| `sender_id` | Denormalised. `null` when the event has no sender. |
| `self_origin` | `true` when the event echoes something tlgr itself did (§4). |

`payload` being models rather than a raw dict is not a style preference. v1
encoded `to_dict()` with `json.dumps(default=str)`, so a message with media
could fail to serialise *at delivery time* — far from the cause, and counted
as a delivery failure rather than as the bug it was (COR-07).

---

## 2. Delivery guarantees

**Ordering.** Events for one chat are delivered in the order the daemon
normalised them. Events for different chats may interleave: handlers run on a
bounded worker pool whose lane is chosen by `chat_id`, so a message and its
edit cannot be processed out of order while unrelated chats run concurrently.

**At-most-once, with a signal.** A consumer that cannot keep up loses its
*oldest* events and is told how many, rather than being blocked or silently
skipped:

```json
{"type": "lag", "dropped": 214}
```

The bus itself never blocks. That is the ROB-02 fix: with
`sequential_updates=True` a slow consumer would otherwise stall the update
loop for every account, which v1's webhook did for up to 97 seconds.

**Replay, and honesty about its limits.** `GET /v1/events?since=<seq>` replays
from a per-account ring buffer (`[daemon] event_buffer`, default 4,096). If
`since` is older than the buffer, the first frame is:

```json
{"type": "gap", "from": 95000, "requested": 91820, "lost": 3179}
```

and delivery continues. A consumer learns it missed events instead of
receiving the newest 4,096 as though they were the next ones.

**Restart survival.** `seq` is persisted to
`~/.tlgr/accounts/<alias>/events.state`, flushed every 5 s and on shutdown, so
`--since` keeps working across a daemon restart. The *events themselves* are
not persisted: the ring buffer is memory. What recovers history after a
restart is `catch_up=True` plus the supervisor (ARCHITECTURE §6.3), which
replays from Telegram's own update state.

**Heartbeats.** A stream emits `{"type": "heartbeat", "ts": …}` every 15 s so
a quiet chat is distinguishable from a dead connection.

**Frames.** A stream is NDJSON: exactly one `meta` first, exactly one `end`
last, and events, `gap`, `lag` and `heartbeat` frames in between. A stream
that ends without `end` is a failure (`RETRYABLE`, exit 8) — never a short
result reported as a complete one.

---

## 3. The taxonomy

114 types, drawn from every one of the 163 `Update*` constructors Telethon
1.44 (layer 227) can parse, plus the five Telegram has added since. The rule
the table is written to, and `tests/test_event_taxonomy.py` enforces, is that
**every constructor is either mapped to a type or listed in §3.3 with the
reason it carries no event**. A constructor that was merely missing would be
an update tlgr drops with nobody able to tell — which is precisely what v1's
two-second polling `watch` did to everything that was not a new message.

Every name is lowercase `snake_case`, `<group>_<noun>` or `<noun>_<verb>`, and
a consumer must ignore a `type` it does not know. The machine-readable form is
`tlgr events list --json`; `tlgr events get <type>` prints one row with its
payload schema and an example.

### 3.1 Selecting types

`--events` (on `watch`, `events replay`, `job add`, `webhook set`) accepts, in
any comma-separated combination:

| Form | Means |
|---|---|
| `message_new` | one type |
| `message` | every type in that group (§3.2) |
| `raw:UpdateBotStopped` | the type that constructor maps to |
| `all` | everything |
| `new_message`, `chat_action`, `message_read`, … | v1's names, still accepted (§3.5) |

An unknown selector is a `USAGE` error, never an empty selection: a `watch`
that silently matches nothing is indistinguishable from a broken daemon.

### 3.2 The types, by group

Sources are the TL constructors that produce the type. **Box** is the sequence
the update is ordered by — `pts`, `qts`, `seq`, `channel_pts`, `version` or
`none` — which is what a consumer needs in order to know whether a gap in it
is recoverable (§`sync`) or simply lost.

#### `message`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `message_available_min` | none | `UpdateChannelAvailableMessages` | A channel's history was cleared below a point |
| `message_deleted` | pts | `UpdateDeleteChannelMessages`, `UpdateDeleteEphemeralMessages`, `UpdateDeleteMessages` | Messages were deleted |
| `message_edited` | pts | `UpdateEditChannelMessage`, `UpdateEditEphemeralMessage`, `UpdateEditMessage` | A message was edited |
| `message_emoji_game` | none | `UpdateEmojiGameInfo` | An emoji game (dice, dart, slot) resolved |
| `message_extended_media` | pts | `UpdateMessageExtendedMedia` | Paid media on a message was unlocked |
| `message_forwards` | channel_pts | `UpdateChannelMessageForwards` | A channel post's forward counter moved |
| `message_geo_live_viewed` | none | `UpdateGeoLiveViewed` | Somebody viewed a live location I am sharing |
| `message_id_assigned` | pts | `UpdateMessageID`, `UpdateShortSentMessage` | An outgoing message got its server id (random_id reconciliation) |
| `message_new` | pts | `UpdateNewChannelMessage`, `UpdateNewEphemeralMessage`, `UpdateNewMessage`, `UpdateShortChatMessage`, `UpdateShortMessage` | A message arrived in any chat the account can see |
| `message_pinned` | pts | `UpdatePinnedChannelMessages`, `UpdatePinnedMessages` | Messages were pinned or unpinned |
| `message_poll` | pts | `UpdateMessagePoll` | A poll's results changed |
| `message_poll_vote` | qts | `UpdateMessagePollVote` | Somebody voted in a poll you can see the votes of |
| `message_reactions` | pts | `UpdateMessageReactions` | Reactions on a message changed |
| `message_scheduled_deleted` | none | `UpdateDeleteScheduledMessages` | A scheduled message fired or was cancelled |
| `message_scheduled_new` | none | `UpdateNewScheduledMessage` | A scheduled message was queued |
| `message_service` | pts | _derived: updateNewMessage / updateNewChannelMessage carrying a messageService_ | A service message: a join, a pin, a title change, a call |
| `message_transcribed` | none | `UpdateTranscribedAudio` | A voice or video note transcription finished |
| `message_views` | channel_pts | `UpdateChannelMessageViews` | A channel post's view counter moved |
| `message_webpage` | pts | `UpdateChannelWebPage`, `UpdateWebPage` | A link preview finished resolving |

#### `read`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `read_contents` | pts | `UpdateChannelReadMessagesContents`, `UpdateReadMessagesContents` | Media or a mention was marked read (the media_unread flag) |
| `read_discussion` | pts | `UpdateReadChannelDiscussionInbox`, `UpdateReadChannelDiscussionOutbox` | A comment thread's read position moved |
| `read_inbox` | pts | `UpdateReadChannelInbox`, `UpdateReadHistoryInbox` | My read position moved: messages I have now seen |
| `read_monoforum` | pts | `UpdateReadMonoForumInbox`, `UpdateReadMonoForumOutbox` | A direct-messages (monoforum) channel's read position moved |
| `read_outbox` | pts | `UpdateReadChannelOutbox`, `UpdateReadHistoryOutbox` | The other side read my messages |

#### `presence`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `typing` | none | `UpdateChannelUserTyping`, `UpdateChatUserTyping`, `UpdateEncryptedChatTyping`, `UpdateUserTyping` | Somebody is typing, recording or uploading |
| `user_status` | none | `UpdateUserStatus` | A user came online, or their last-seen changed |

#### `peer`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `peer_blocked` | none | `UpdatePeerBlocked` | A peer was blocked or unblocked |
| `peer_chat_changed` | none | `UpdateChannel`, `UpdateChat` | A chat or channel record was invalidated (refetch; may mean kicked) |
| `peer_history_ttl` | none | `UpdatePeerHistoryTTL` | A chat's auto-delete timer changed |
| `peer_located` | none | `UpdatePeerLocated` | The people/groups-nearby list changed |
| `peer_notify_settings` | none | `UpdateNotifySettings` | Notification settings changed for a peer or a scope |
| `peer_settings` | none | `UpdatePeerSettings` | A peer's action-bar settings changed (anti-scam hints included) |
| `peer_user_changed` | none | `UpdateUser` | A user record was invalidated and should be refetched |
| `peer_user_emoji_status` | none | `UpdateUserEmojiStatus` | A user's emoji status changed |
| `peer_user_name` | none | `UpdateUserName` | A user changed their name or username |
| `peer_user_phone` | none | `UpdateUserPhone` | A contact's phone number changed |
| `peer_wallpaper` | none | `UpdatePeerWallpaper` | A chat wallpaper changed |

#### `member`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `member_boost` | none | `UpdateBotChatBoost` | A channel boost was applied (bot-only) |
| `member_channel` | qts | `UpdateChannelParticipant` | A channel or supergroup member or admin changed |
| `member_chat` | version | `UpdateChatParticipant`, `UpdateChatParticipantAdd`, `UpdateChatParticipantAdmin`, `UpdateChatParticipantDelete`, `UpdateChatParticipantRank`, `UpdateChatParticipants` | A basic group's membership or admin list changed |
| `member_default_rights` | version | `UpdateChatDefaultBannedRights` | A group's default permissions changed |
| `member_join_request` | none | `UpdateBotChatInviteRequester`, `UpdatePendingJoinRequests` | A pending join request arrived or was resolved |

#### `dialog`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `dialog_draft` | none | `UpdateDraftMessage` | A cloud draft was set or cleared |
| `dialog_filters` | none | `UpdateDialogFilter`, `UpdateDialogFilterOrder`, `UpdateDialogFilters` | Chat folders (dialog filters) changed |
| `dialog_folder` | pts | `UpdateFolderPeers` | A chat moved into or out of the Archive |
| `dialog_forum_pinned` | none | `UpdatePinnedForumTopic`, `UpdatePinnedForumTopics` | Forum topics were pinned or reordered |
| `dialog_forum_view` | none | `UpdateChannelViewForumAsMessages` | A forum's display mode was toggled |
| `dialog_monoforum_no_paid` | none | `UpdateMonoForumNoPaidException` | A direct-messages channel's paid-message exception changed |
| `dialog_pinned` | none | `UpdateDialogPinned`, `UpdatePinnedDialogs` | A chat was pinned, unpinned or reordered in the list |
| `dialog_quick_reply` | none | `UpdateDeleteQuickReply`, `UpdateDeleteQuickReplyMessages`, `UpdateNewQuickReply`, `UpdateQuickReplies`, `UpdateQuickReplyMessage` | Business quick-reply shortcuts changed |
| `dialog_saved_pinned` | none | `UpdatePinnedSavedDialogs`, `UpdateSavedDialogPinned` | A Saved Messages sub-dialog was pinned or reordered |
| `dialog_saved_tags` | none | `UpdateSavedReactionTags` | Saved-message reaction tags changed |
| `dialog_unread_mark` | none | `UpdateDialogUnreadMark` | A chat was manually marked unread (or the mark was cleared) |

#### `story`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `story_id` | none | `UpdateStoryID` | A story you posted got its server id |
| `story_new` | none | `UpdateStory` | A story was posted, edited or deleted |
| `story_reaction` | none | `UpdateNewStoryReaction`, `UpdateSentStoryReaction` | A story was reacted to |
| `story_read` | none | `UpdateReadStories` | Stories were marked read |
| `story_stealth` | none | `UpdateStoriesStealthMode` | Story stealth mode changed |

#### `collection`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `collection_attach_menu` | none | `UpdateAttachMenuBots` | The attachment-menu bot list changed |
| `collection_emoji_statuses` | none | `UpdateRecentEmojiStatuses` | Recent emoji statuses changed |
| `collection_gifs` | none | `UpdateSavedGifs` | Saved GIFs changed |
| `collection_reactions` | none | `UpdateRecentReactions` | Recent or top reactions changed |
| `collection_ringtones` | none | `UpdateSavedRingtones` | Notification sounds changed |
| `collection_stickers` | none | `UpdateFavedStickers`, `UpdateMoveStickerSetToTop`, `UpdateNewStickerSet`, `UpdateRecentStickers`, `UpdateStickerSets`, `UpdateStickerSetsOrder` | Sticker or custom-emoji sets changed |
| `collection_stickers_read` | none | `UpdateReadFeaturedEmojiStickers`, `UpdateReadFeaturedStickers` | Featured sticker or emoji sets were marked read |
| `collection_themes` | none | `UpdateTheme` | A theme changed |

#### `call`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `call_group` | none | `UpdateGroupCall`, `UpdateGroupCallConnection` | A group call, video chat or live stream changed |
| `call_group_encrypted` | none | `UpdateGroupCallChainBlocks`, `UpdateGroupCallEncryptedMessage` | Encrypted group-call key material (conference calls) |
| `call_group_message` | none | `UpdateDeleteGroupCallMessages`, `UpdateGroupCallMessage` | A message inside a group call was posted or deleted |
| `call_group_participants` | version | `UpdateGroupCallParticipants` | Group-call participants changed |
| `call_phone` | none | `UpdatePhoneCall` | An incoming or updated 1:1 call (signalling only; tlgr carries no media) |
| `call_signaling` | none | `UpdatePhoneCallSignalingData` | Raw call signalling data |

#### `bot`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `bot_business_connection` | none | `UpdateBotBusinessConnect`, `UpdateNewBotConnection` | A business connection was created or changed (bot-only) |
| `bot_business_message` | none | `UpdateBotDeleteBusinessMessage`, `UpdateBotEditBusinessMessage`, `UpdateBotNewBusinessMessage` | A message on a connected business account arrived, changed or went (bot-only) |
| `bot_callback_query` | none | `UpdateBotCallbackQuery`, `UpdateBusinessBotCallbackQuery`, `UpdateInlineBotCallbackQuery` | An inline-keyboard button was pressed (bot-only) |
| `bot_commands` | none | `UpdateBotCommands` | A bot's command list changed |
| `bot_ephemeral_callback` | none | `UpdateBotEphemeralCallbackQuery` | A callback button on an ephemeral message was pressed (bot-only, layer 229) |
| `bot_guest_chat_query` | none | `UpdateBotGuestChatQuery` | A guest-mode chat query arrived (bot-only) |
| `bot_inline_query` | none | `UpdateBotInlineQuery`, `UpdateBotInlineSend` | An inline query arrived, or a result was chosen (bot-only) |
| `bot_managed` | none | `UpdateManagedBot` | A bot you manage changed |
| `bot_menu_button` | none | `UpdateBotMenuButton` | A bot's menu button changed |
| `bot_message_reaction` | none | `UpdateBotMessageReaction`, `UpdateBotMessageReactions` | A reaction on a message this bot can see changed (bot-only) |
| `bot_paid_media_purchased` | none | `UpdateBotPurchasedPaidMedia` | A user bought paid media from this bot (bot-only) |
| `bot_precheckout` | none | `UpdateBotPrecheckoutQuery` | A pre-checkout query arrived (bot-only) |
| `bot_shipping` | none | `UpdateBotShippingQuery` | A shipping query arrived (bot-only) |
| `bot_stars_subscription` | none | `UpdateBotStarsSubscription` | A Stars subscription to this bot changed (bot-only, layer 229) |
| `bot_stopped` | qts | `UpdateBotStopped` | A user started or stopped this bot (bot-only) |
| `bot_webhook` | none | `UpdateBotWebhookJSON`, `UpdateBotWebhookJSONQuery` | A bot-webhook JSON passthrough arrived (bot-only) |
| `bot_webview_join_decision` | none | `UpdateJoinChatWebViewDecision` | A join-chat decision was made inside a mini app |
| `bot_webview_result` | none | `UpdateWebViewResultSent` | A mini app sent data back (bot-only) |

#### `stars`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `stars_balance` | none | `UpdateStarsBalance` | The Telegram Stars balance changed |
| `stars_gift_auction` | none | `UpdateStarGiftAuctionState`, `UpdateStarGiftAuctionUserState`, `UpdateStarGiftCraftFail` | A star-gift auction or craft changed state |
| `stars_paid_reaction_privacy` | none | `UpdatePaidReactionPrivacy` | Paid-reaction privacy changed |
| `stars_revenue` | none | `UpdateStarsRevenueStatus` | Star revenue or withdrawal status changed |

#### `secret`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `secret_chat` | qts | `UpdateEncryption` | A secret chat was requested, accepted or discarded |
| `secret_message` | qts | `UpdateNewEncryptedMessage` | Encrypted traffic arrived; tlgr acknowledges it but cannot decrypt it |
| `secret_read` | qts | `UpdateEncryptedMessagesRead` | Secret-chat messages were read or expired |

#### `account`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `account_ai_tones` | none | `UpdateAiComposeTones` | The AI compose tone list changed |
| `account_autosave` | none | `UpdateAutoSaveSettings` | Media auto-save settings changed |
| `account_browser_settings` | none | `UpdateWebBrowserException`, `UpdateWebBrowserSettings` | In-app browser settings or a per-domain exception changed |
| `account_contacts_reset` | none | `UpdateContactsReset` | The contact list was wiped |
| `account_langpack` | none | `UpdateLangPack`, `UpdateLangPackTooLong` | The language pack changed |
| `account_login_token` | none | `UpdateLoginToken` | A QR login token was accepted |
| `account_new_authorization` | none | `UpdateNewAuthorization` | A new login on this account |
| `account_privacy` | none | `UpdatePrivacy` | A privacy rule changed |
| `account_sent_phone_code` | none | `UpdateSentPhoneCode` | A login code was delivered in-app |
| `account_service_notification` | none | `UpdateServiceNotification` | An official service notification (from 777000) |
| `account_session_revoked` | none | _derived: the SESSION_REVOKE push payload, decoded by `tlgr events decode`_ | This session was terminated elsewhere (from a push payload) |
| `account_sms_job` | none | `UpdateSmsJob` | An SMS-relay job arrived (Telegram's peer-to-peer login SMS programme) |

#### `sync`

| Type | Box | Source constructors | Meaning |
|---|---|---|---|
| `daemon_health` | none | _derived: the session state machine (ARCHITECTURE §6.2)_ | An account changed state, or the circuit breaker opened |
| `sync_channel_too_long` | none | `UpdateChannelTooLong` | A channel's gap is unrecoverable from its pts; a resync is needed |
| `sync_config` | none | `UpdateConfig` | The server configuration was invalidated; re-read help.getConfig |
| `sync_dc_options` | none | `UpdateDcOptions` | The data-centre address list changed |
| `sync_pts_changed` | none | `UpdatePtsChanged` | The pts sequence was reset; some updates are unrecoverable |

### 3.3 Constructors that carry no event

These four are containers or transport signals: they have no payload of their
own, and the thing inside them is normalised in their place.

| Constructor | Why |
|---|---|
| `UpdateShort` | container: carries exactly one Update, which is normalised in its place |
| `Updates` | container: a batch of updates plus their users/chats arrays |
| `UpdatesCombined` | container: a batch of updates spanning a seq range |
| `UpdatesTooLong` | transport signal: the common box overflowed, handled by the supervisor with updates.getDifference (see `tlgr sync catch-up`) |

### 3.4 Constructors newer than Telethon 1.44 (layer 227)

Telegram ships these; this build cannot parse them, so a raw handler
sees only an unknown constructor id. They are listed — rather than
omitted — so `tlgr events list` can answer "exists, unavailable here".

| Constructor | Would be | Status |
|---|---|---|
| `UpdateBotEphemeralCallbackQuery` | `bot_ephemeral_callback` | unparseable in this build; listed by `events list` |
| `UpdateBotStarsSubscription` | `bot_stars_subscription` | unparseable in this build; listed by `events list` |
| `UpdateDeleteEphemeralMessages` | `message_deleted` | unparseable in this build; listed by `events list` |
| `UpdateEditEphemeralMessage` | `message_edited` | unparseable in this build; listed by `events list` |
| `UpdateNewEphemeralMessage` | `message_new` | unparseable in this build; listed by `events list` |

### 3.5 Compatibility names

v1's `watch --events` and `jobs.yaml` spelled these differently, and the
foundation shipped a nine-name starter set. Both keep working (§12.4); each
expands to one or more v2 types.

| Legacy name | Expands to |
|---|---|
| `new_message` | `message_new` |
| `message_edit` | `message_edited` |
| `message_read` | `read_inbox`, `read_outbox` |
| `chat_action`, `user_joined` | `message_service`, `member_chat`, `member_channel` |
| `reaction_changed` | `message_reactions` |
| `draft_changed` | `dialog_draft` |

### 3.6 Payloads

Fourteen types carry a **modelled** payload: `message_new`, `message_service`,
`message_edited` and `message_scheduled_new` carry the full `Message` model;
`message_deleted`, `message_pinned`, `message_id_assigned`, `read_inbox`,
`read_outbox`, `typing`, `user_status`, `message_reactions`, `dialog_draft`
and `sync_channel_too_long` carry a small, named shape. `tlgr events get`
prints it.

Every other type carries **the update's own fields, made JSON-safe**: a
`datetime` becomes RFC-3339, `bytes` become hex, and a nested TL object
becomes `{"_": "ClassName", …}` so a consumer can still branch on the
constructor. That conversion is `tl_to_builtins` and it is the COR-07 fix:
v1 encoded a raw `to_dict()` with `json.dumps(default=str)`, so a message with
media could fail to serialise *at delivery time*, far from the cause, and be
counted as a delivery failure rather than as the bug it was.

**What is deliberately absent.** tlgr does not invent a type name for an
update it has no taxonomy entry for, and there is no `unknown` type. A name
that means "we did not look" cannot be filtered on and changes meaning the day
the real one is added.


## 4. Self-origin events

Telethon marks the result of our own requests and does not dispatch it, so a
message the daemon sends never fires `NewMessage`. Every mutating operation
therefore feeds its returned `Updates` through the same normaliser with
`self_origin: true`.

This is what makes `tlgr watch` show the account's own sends, and it is why a
gateway rule must check `self_origin` before acting on a message: a rule that
replies to every message, without that check, replies to its own reply.

---

## 5. Consuming events

**Stream (NDJSON over the socket):**

```
GET /v1/events?account=work&types=message_new,message_read&since=91820&chats=-1001,-1002&timeout=300
```

`types` and `chats` are optional filters; `since` replays; `timeout` bounds
the connection (default 3600, max 86400), after which the server closes with
`{"type": "end", "reason": "timeout"}` and the client reconnects with the last
`seq` it saw.

**Webhook.** The same envelope, POSTed as
`{"event": <envelope>, "delivery_id": "<ulid>"}` with:

| Header | Value |
|---|---|
| `X-Tlgr-Signature` | `sha256=<hmac-sha256 of the exact body>` |
| `X-Tlgr-Delivery` | unique per delivery attempt |
| `X-Tlgr-Seq` | the event's `seq` |
| `X-Tlgr-Event` | the event's `type` |
| `X-Tlgr-Account` | the account alias |

Verify the signature over the **raw bytes you received**. Re-encoding the JSON
first will make an honest signature fail.

**Gateway jobs** subscribe to the bus in-process; see `tlgr/gateway/README.md`.

---

## 6. Adding a type

1. Add an `EventTypeSpec` to `_TYPES` in `tlgr/core/eventtypes.py`: lowercase
   `snake_case`, a group from `GROUPS`, a one-line summary, the sequence box,
   and the payload fields. The table is in `core/` because `ops/`, `daemon/`
   and the doc generator all read it and none may import each other.
2. Point its `Update*` constructor(s) at it in `CONSTRUCTORS`. A constructor
   that carries no event goes in `INTERNAL` **with a reason** —
   `tests/test_event_taxonomy.py` checks the installed Telethon against both
   lists and fails on a constructor that is in neither, so a Telethon upgrade
   that adds one fails in the run that upgrades it.
3. If the payload deserves more than the update's own fields, add a branch to
   `normalise_update()` in `tlgr/daemon/events.py` and build it from
   **models** — never `to_dict()`. Everything else is handled by
   `tl_to_builtins`, which is what guarantees no `datetime` and no `bytes`
   reach a consumer.
4. Regenerate §3.2 and `docs/reference/events.md`, and add a test that the
   payload survives `msgspec.json.encode`.

The daemon subscribes with a single `events.Raw()` handler, deliberately.
Telethon's high-level builders (`NewMessage`, `ChatAction`, …) drop service
messages, topic ids and every action kind Telethon does not model, so a stream
built on them can only ever show a subset of what the GUI shows.
