# Telegram MTProto error taxonomy (for tlgr exit-code mapping)

Source: `<scratchpad>/docs/api/api__errors.md` (prose) and `api__errors.json.md` (machine-readable RPC error DB, **layer 227**).  
Counts per code in the DB: `-503`=2, `303`=4, `400`=681, `401`=8, `403`=60, `404`=2, `406`=37, `420`=8, `500`=16; 780 distinct error descriptions.

## 0. Anatomy of an RPC error

An `rpc_error` carries two fields:

- **error_code** — an integer, HTTP-status-like (`303`, `400`, `401`, `403`, `404`, `406`, `420`, `500`, plus the pseudo-code `-503` used for client-side/queue timeouts).
- **error_message** — an optional string literal matching `/[A-Z_0-9]+/`, e.g. `AUTH_KEY_UNREGISTERED`. Some carry a trailing integer parameter and are documented with a `%d` placeholder (`FLOOD_WAIT_%d`, `SLOWMODE_WAIT_%d`, `ALLOW_PAYMENT_REQUIRED_%d`, `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_%dMIN`, `FILE_PART_%d_MISSING`, `FILE_REFERENCE_%d_EXPIRED`, `AUTH_RESTART_%d`, …). **tlgr must normalise the numeric suffix out before lookup and expose it as a separate `retry_after`/`param` field.**

The JSON DB also ships four method-classification lists that tlgr should use for pre-flight validation instead of round-tripping to the server:

| List | Size | Meaning |
| --- | --- | --- |
| `user_only` | 656 | Methods only user accounts can call |
| `bot_only` | 31 | Methods only bots can call |
| `business_supported` | 19 | Methods callable by a bot over `invokeWithBusinessConnection` |
| `unauthed_allowed` | 40 | Methods callable before login |

## 1. Error codes → proposed tlgr exit codes

| Code | Name | Meaning | Retryable? | Proposed tlgr exit code |
| --- | --- | --- | --- | --- |
| `303` | SEE_OTHER | DC migration (`*_MIGRATE_%d`). Telethon handles `PHONE_MIGRATE`/`USER_MIGRATE`/`NETWORK_MIGRATE`/`FILE_MIGRATE` transparently; `STATS_MIGRATE_%d` is **not** auto-handled by all wrappers and needs an explicit re-send to `channelFull.stats_dc`. | Yes, transparently | 10 (should never surface) |
| `400` | BAD_REQUEST | Malformed/invalid user-supplied data. | No | 3 |
| `401` | UNAUTHORIZED | Session missing/expired/revoked, or account deleted. | No — re-login | 4 |
| `403` | FORBIDDEN | Privacy/rights violation. | No | 5 |
| `404` | NOT_FOUND | Non-existent object/method. | No | 6 |
| `406` | NOT_ACCEPTABLE | **Never show your own error text.** Wait for an out-of-band `updateServiceNotification` with `popup` set and display that instead. Exception: `AUTH_KEY_DUPLICATED` (session already killed; must re-auth). | No | 7 |
| `420` | FLOOD | Rate limit; message carries the wait in seconds. | Yes after wait | 8 |
| `500` | INTERNAL | Server-side failure. | Yes with backoff | 9 |
| `-503` | TIMEOUT | `Timeout` / `MSG_WAIT_TIMEOUT` — client-side or invokeAfterMsg-queue timeout. | Yes | 9 |
| other | — | Treat as `500`. | Yes with backoff | 9 |

### Recommended special-cased exit codes (more useful than the raw class)

| Exit | Condition | Errors |
| --- | --- | --- |
| 8 | flood wait, `retry_after` on stderr/JSON | `FLOOD_WAIT_%d`, `FLOOD_PREMIUM_WAIT_%d`, `SLOWMODE_WAIT_%d`, `TAKEOUT_INIT_DELAY_%d`, `2FA_CONFIRM_WAIT_%d`, `STORY_SEND_FLOOD_WEEKLY_%d`, `STORY_SEND_FLOOD_MONTHLY_%d`, `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_%dMIN` |
| 11 | file reference expired → refresh via file-reference DB and retry automatically, only surface if refresh also fails | `FILE_REFERENCE_EXPIRED`, `FILE_REFERENCE_INVALID`, `FILE_REFERENCE_%d_EXPIRED`, `FILE_REFERENCE_%d_INVALID`, `FILEREF_UPGRADE_NEEDED` |
| 12 | 2FA password needed | `SESSION_PASSWORD_NEEDED` (401), `PASSWORD_HASH_INVALID`, `PASSWORD_MISSING`, `PASSWORD_TOO_FRESH_%d`, `SESSION_TOO_FRESH_%d` |
| 13 | payment / Stars balance | `BALANCE_TOO_LOW`, `ALLOW_PAYMENT_REQUIRED_%d`, `ALLOW_PAYMENT_REQUIRED`, `STARS_FORM_AMOUNT_MISMATCH`, `FORM_EXPIRED` |
| 14 | Premium subscription required | `PREMIUM_ACCOUNT_REQUIRED`, `PRIVACY_PREMIUM_REQUIRED`, `AICOMPOSE_FLOOD_PREMIUM`, `BOOSTS_REQUIRED` |
| 15 | account frozen (read-only) | `FROZEN_METHOD_INVALID` (420), `FROZEN_PARTICIPANT_MISSING` (400) — on receipt refetch `help.getAppConfig` for `freeze_since_date`/`freeze_until_date`/`freeze_appeal_url` |
| 16 | needs migration/upgrade of the object | `CHANNEL_INVALID` on a basic group → `messages.migrateChat`; `CHAT_ADMIN_REQUIRED` on supergroup-only ops |
| 17 | invokeAfterMsg(s) queue failure → resend the whole chain | `MSG_WAIT_FAILED` (500), `MSG_WAIT_TIMEOUT` (-503) |

## 2. Full listings for every non-400 code

### -503 TIMEOUT (pseudo-code)

| Error | Description |
| --- | --- |
| `MSG_WAIT_TIMEOUT` | Spent too much time waiting for a previous query in the invokeAfterMsg request queue, aborting! |
| `Timeout` | Timeout while fetching data. |

### 303 SEE_OTHER

| Error | Description |
| --- | --- |
| `NETWORK_MIGRATE_%d` | Your IP address is associated to DC %d, please re-send the query to that DC. |
| `PHONE_MIGRATE_%d` | Your phone number is associated to DC %d, please re-send the query to that DC. |
| `STATS_MIGRATE_%d` | Channel statistics for the specified channel are stored on DC %d, please re-send the query to that DC. |
| `USER_MIGRATE_%d` | Your account is associated to DC %d, please re-send the query to that DC. |

### 401 UNAUTHORIZED

| Error | Description |
| --- | --- |
| `AUTH_KEY_INVALID` | The specified auth key is invalid. |
| `AUTH_KEY_PERM_EMPTY` | The method is unavailable for temporary authorization keys, not bound to a permanent authorization key. |
| `AUTH_KEY_UNREGISTERED` | The specified authorization key is not registered in the system (for example, a PFS temporary key has expired). |
| `SESSION_EXPIRED` | The session has expired. |
| `SESSION_PASSWORD_NEEDED` | 2FA is enabled, use a password to login. |
| `SESSION_REVOKED` | The session was revoked by the user. |
| `USER_DEACTIVATED` | The current account was deleted by the user. |
| `USER_DEACTIVATED_BAN` | The current account was deleted and banned by Telegram's antispam system. |

### 403 FORBIDDEN

| Error | Description |
| --- | --- |
| `ACCESS_DENIED` | The account was deactivated, or is a bot/service account. |
| `ALLOW_PAYMENT_REQUIRED_%d` | This peer charges %d Telegram Stars per message, but the `allow_paid_stars` was not set or its value is smaller than %d. |
| `BOT_ACCESS_FORBIDDEN` | The specified method *can* be used over a business connection for some operations, but the specified query attempted an operation that is not allowed over a business connection. |
| `BOT_FORUM_CREATE_FORBIDDEN` | Since the bot's user.bot_forum_can_manage_topics flag is **not** set, the user cannot create or modify bot forum topics. |
| `BOT_GUARD_NOT_SUPPORTED` | This bot is not designated as a "join guard" bot. This method is only available to bots that mediate user joins to chats. . |
| `BOT_VERIFIER_FORBIDDEN` | This bot cannot assign verification icons. |
| `BROADCAST_FORBIDDEN` | Channel poll voters and reactions cannot be fetched to prevent deanonymization. |
| `CHANNEL_PUBLIC_GROUP_NA` | channel/supergroup not available. |
| `CHAT_ACTION_FORBIDDEN` | You cannot execute this action. |
| `CHAT_ADMIN_INVITE_REQUIRED` | You do not have the rights to do this. |
| `CHAT_ADMIN_REQUIRED` | You must be an admin in this chat to do this. |
| `CHAT_FORBIDDEN` | This chat is not available to the current user. |
| `CHAT_GUEST_SEND_FORBIDDEN` | You join the discussion group before commenting, see here » for more info. |
| `CHAT_SEND_AUDIOS_FORBIDDEN` | You can't send audio messages in this chat. |
| `CHAT_SEND_DOCS_FORBIDDEN` | You can't send documents in this chat. |
| `CHAT_SEND_GAME_FORBIDDEN` | You can't send a game to this chat. |
| `CHAT_SEND_GIFS_FORBIDDEN` | You can't send gifs in this chat. |
| `CHAT_SEND_INLINE_FORBIDDEN` | You can't send inline messages in this group. |
| `CHAT_SEND_MEDIA_FORBIDDEN` | You can't send media in this chat. |
| `CHAT_SEND_PHOTOS_FORBIDDEN` | You can't send photos in this chat. |
| `CHAT_SEND_PLAIN_FORBIDDEN` | You can't send non-media (text) messages in this chat. |
| `CHAT_SEND_POLL_FORBIDDEN` | You can't send polls in this chat. |
| `CHAT_SEND_ROUNDVIDEOS_FORBIDDEN` | You can't send round videos to this chat. |
| `CHAT_SEND_STICKERS_FORBIDDEN` | You can't send stickers in this chat. |
| `CHAT_SEND_VIDEOS_FORBIDDEN` | You can't send videos in this chat. |
| `CHAT_SEND_VOICES_FORBIDDEN` | You can't send voice recordings in this chat. |
| `CHAT_SEND_WEBPAGE_FORBIDDEN` | You can't send webpage previews to this chat. |
| `CHAT_TYPE_INVALID` | The specified user type is invalid. |
| `CHAT_WRITE_FORBIDDEN` | You can't write in this chat. |
| `EDIT_BOT_INVITE_FORBIDDEN` | Normal users can't edit invites that were created by bots. |
| `GROUPCALL_ALREADY_STARTED` | The groupcall has already started, you can join directly using phone.joinGroupCall. |
| `GROUPCALL_CHANGE_FORBIDDEN` | You cannot change this group call setting. |
| `GROUPCALL_FORBIDDEN` | The specified group call cannot be used in this context. |
| `INLINE_BOT_REQUIRED` | Only the inline bot can edit message. |
| `MESSAGE_AUTHOR_REQUIRED` | Message author required. |
| `MESSAGE_DELETE_FORBIDDEN` | You can't delete one of the messages you tried to delete, most likely because it is a service message. |
| `NOT_ELIGIBLE` | The current user is not eligible to join the Peer-to-Peer Login Program. |
| `PARTICIPANT_JOIN_MISSING` | Trying to enable a presentation, when the user hasn't joined the Video Chat with phone.joinGroupCall. |
| `PEER_ID_INVALID` | The provided peer id is invalid. |
| `POLL_VOTE_REQUIRED` | Cast a vote in the poll before calling this method. |
| `PREMIUM_ACCOUNT_REQUIRED` | A premium account is required to execute this action. |
| `PRIVACY_PREMIUM_REQUIRED` | You need a Telegram Premium subscription to send a message to this user. |
| `PUBLIC_CHANNEL_MISSING` | You can only export group call invite links for public chats or channels. |
| `RIGHT_FORBIDDEN` | Your admin rights do not allow you to do this. |
| `SENSITIVE_CHANGE_FORBIDDEN` | You can't change your sensitive content settings. |
| `TAKEOUT_REQUIRED` | A takeout session needs to be initialized first, see here » for more info. |
| `USER_BANNED_IN_CHANNEL` | You're banned from sending messages in supergroups/channels. |
| `USER_BOT_INVALID` | User accounts must provide the `bot` method parameter when calling this method. If there is no such method parameter, this method can only be invoked by bot accounts. |
| `USER_CHANNELS_TOO_MUCH` | One of the users you tried to add is already in too many channels/supergroups. |
| `USER_DELETED` | You can't send this secret message because the other participant deleted their account. |
| `USER_DISALLOWED_STARGIFTS` | The recipient user has configured restrictions on which categories of star gifts they're willing to accept (unique, limited, or unlimited): the sender attempted to get a payment form for a gift that falls into a category the recipient has blocked. |
| `USER_INVALID` | Invalid user provided. |
| `USER_IS_BLOCKED` | You were blocked by this user. |
| `USER_NOT_MUTUAL_CONTACT` | The provided user is not a mutual contact. |
| `USER_NOT_PARTICIPANT` | You're not a member of this supergroup/channel. |
| `USER_PERMISSION_DENIED` | The user hasn't granted or has revoked the bot's access to change their emoji status using bots.toggleUserEmojiStatusPermission. |
| `USER_PRIVACY_RESTRICTED` | The user's privacy settings do not allow you to do this. |
| `USER_RESTRICTED` | You're spamreported, you can't create channels or chats. |
| `VOICE_MESSAGES_FORBIDDEN` | This user's privacy settings forbid you from sending voice messages. |
| `YOUR_PRIVACY_RESTRICTED` | You cannot fetch the read date of this message because you have disallowed other users to do so for *your* messages; to fix, allow other users to see *your* exact last online date OR purchase a Telegram Premium subscription. |

### 404 NOT_FOUND

| Error | Description |
| --- | --- |
| `METHOD_INVALID` | The specified method is invalid. |
| `PEER_ID_INVALID` | The provided peer id is invalid. |

### 406 NOT_ACCEPTABLE

| Error | Description |
| --- | --- |
| `ALLOW_PAYMENT_REQUIRED` | This peer only accepts paid messages »: this error is only emitted for older layers without paid messages support, so the client must be updated in order to use paid messages. . |
| `API_GIFT_RESTRICTED_UPDATE_APP` | Please update the app to access the gift API. |
| `AUTH_KEY_DUPLICATED` | Concurrent usage of the current session from multiple connections was detected, the current session was invalidated by the server for security reasons! |
| `BANNED_RIGHTS_INVALID` | You provided some invalid flags in the banned rights. |
| `BUSINESS_ADDRESS_ACTIVE` | The user is currently advertising a Business Location, the location may only be changed (or removed) using account.updateBusinessLocation ». . |
| `CALL_PROTOCOL_COMPAT_LAYER_INVALID` | The other side of the call does not support any of the VoIP protocols supported by the local client, as specified by the `protocol.layer` and `protocol.library_versions` fields. |
| `CHANNEL_PRIVATE` | You haven't joined this channel/supergroup. |
| `CHANNEL_TOO_LARGE` | Channel is too large to be deleted; this error is issued when trying to delete channels with more than 1000 members (subject to change). |
| `CHAT_FORWARDS_RESTRICTED` | You can't forward messages from a protected chat. |
| `EDIT_MESSAGE_TEMP_RESTRICTED` | Message editing is temporarily forbidden for this user due to regulatory restrictions. |
| `FILEREF_UPGRADE_NEEDED` | The client has to be updated in order to support file references. |
| `FRESH_CHANGE_ADMINS_FORBIDDEN` | You were just elected admin, you can't add or modify other admins yet. |
| `FRESH_CHANGE_PHONE_FORBIDDEN` | You can't change phone number right after logging in, please wait at least 24 hours. |
| `FRESH_RESET_AUTHORISATION_FORBIDDEN` | You can't logout other sessions if less than 24 hours have passed since you logged on the current session. |
| `INVITE_HASH_EXPIRED` | The invite link has expired. |
| `PAYMENT_UNSUPPORTED` | A detailed description of the error will be received separately as described here ». |
| `PEER_ID_INVALID` | The provided peer id is invalid. |
| `PHONE_NUMBER_INVALID` | The phone number is invalid. |
| `PHONE_PASSWORD_FLOOD` | You have tried logging in too many times. |
| `POLL_COUNTRY_RESTRICTED` | Users from the current user's country cannot vote in this country-restricted poll ». |
| `POLL_MEMBER_RESTRICTED` | Only channel subscribers can vote in this poll. |
| `PRECHECKOUT_FAILED` | Precheckout failed, a detailed and localized description for the error will be emitted via an updateServiceNotification as specified here ». |
| `PREMIUM_CURRENTLY_UNAVAILABLE` | You cannot currently purchase a Premium subscription. |
| `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_%dMIN` | Import for this chat is already in progress, wait %d minutes before starting a new one. |
| `PRIVACY_PREMIUM_REQUIRED` | You need a Telegram Premium subscription to send a message to this user. |
| `SEND_CODE_UNAVAILABLE` | Returned when all available options for this type of number were already used (e.g. flash-call, then SMS, then this error might be returned to trigger a second resend). |
| `STARGIFT_EXPORT_IN_PROGRESS` | A gift export is in progress, a detailed and localized description for the error will be emitted via an updateServiceNotification as specified here ». |
| `STARS_FORM_AMOUNT_MISMATCH` | The form amount has changed, please fetch the new form using payments.getPaymentForm and restart the process. |
| `STICKERSET_INVALID` | The provided sticker set is invalid. |
| `STICKERSET_OWNER_ANONYMOUS` | Provided stickerset can't be installed as group stickerset to prevent admin deanonymization. |
| `TOPIC_CLOSED` | This topic was closed, you can't send messages to it anymore. |
| `TOPIC_DELETED` | The specified topic was deleted. |
| `TRANSLATIONS_DISABLED` | Translations are unavailable, a detailed and localized description for the error will be emitted via an updateServiceNotification as specified here ». |
| `UPDATE_APP_TO_LOGIN` | Please update your client to login. |
| `USERPIC_PRIVACY_REQUIRED` | You need to disable privacy settings for your profile picture in order to make your geolocation public. |
| `USERPIC_UPLOAD_REQUIRED` | You must have a profile picture to publish your geolocation. |
| `USER_RESTRICTED` | You're spamreported, you can't create channels or chats. |

### 420 FLOOD

| Error | Description |
| --- | --- |
| `2FA_CONFIRM_WAIT_%d` | Since this account is active and protected by a 2FA password, we will delete it in 1 week for security purposes. You can cancel this process at any time, you'll be able to reset your account in %d seconds. |
| `ADDRESS_INVALID` | The specified geopoint address is invalid. |
| `FLOOD_PREMIUM_WAIT_%d` | Please wait %d seconds before repeating the action, or purchase a Telegram Premium subscription to remove this rate limit. |
| `FLOOD_WAIT_%d` | Please wait %d seconds before repeating the action. |
| `FROZEN_METHOD_INVALID` | The current account is frozen, and thus cannot execute the specified action. |
| `PREMIUM_SUB_ACTIVE_UNTIL_%d` | You already have a premium subscription active until unixtime %d . |
| `SLOWMODE_WAIT_%d` | Slowmode is enabled in this chat: wait %d seconds before sending another message to this chat. |
| `TAKEOUT_INIT_DELAY_%d` | Sorry, for security reasons, you will be able to begin downloading your data in %d seconds. We have notified all your devices about the export request to make sure it's authorized and to give you time to react if it's not. |

### 500 INTERNAL

| Error | Description |
| --- | --- |
| `AICOMPOSE_TIMEOUT` | A timeout occurred while composing the message. |
| `AUTH_KEY_UNSYNCHRONIZED` | Internal error, please repeat the method call. |
| `AUTH_RESTART` | Restart the authorization process. |
| `AUTH_RESTART_%d` | Internal error (debug info %d), please repeat the method call. |
| `CALL_OCCUPY_FAILED` | The call failed because the user is already making another call. |
| `CDN_UPLOAD_TIMEOUT` | A server-side timeout occurred while reuploading the file to the CDN DC. |
| `CHAT_ID_GENERATE_FAILED` | Failure while generating the chat ID. |
| `CHAT_INVALID` | Invalid chat. |
| `MSG_WAIT_FAILED` | A waiting call returned an error. |
| `OAUTH_REQUEST_INVALID` | The specified OAuth request is invalid. |
| `PERSISTENT_TIMESTAMP_OUTDATED` | Channel internal replication issues, try again later (treat this like an RPC_CALL_FAIL). |
| `RANDOM_ID_DUPLICATE` | You provided a random ID that was already used. |
| `SEND_MEDIA_INVALID` | The specified media is invalid. |
| `SIGN_IN_FAILED` | Failure while signing in. |
| `TRANSLATE_REQ_FAILED` | Translation failed, please try again later. |
| `TRANSLATION_TIMEOUT` | A timeout occurred while translating the specified text. |

## 3. 400 BAD_REQUEST — grouped by family

681 distinct `400` errors are documented. Below they are grouped by the first token of the error string; each group is the natural unit for a tlgr error-class mapping.

### `BOT_*` (24)

| Error | Description |
| --- | --- |
| `BOT_ALREADY_DISABLED` | The connected business bot was already disabled for the specified peer. |
| `BOT_APP_BOT_INVALID` | The bot_id passed in the inputBotAppShortName constructor is invalid. |
| `BOT_APP_INVALID` | The specified bot app is invalid. |
| `BOT_APP_SHORTNAME_INVALID` | The specified bot app short name is invalid. |
| `BOT_BUSINESS_MISSING` | The specified bot is not a business bot (the user.`bot_business` flag is not set). |
| `BOT_CHANNELS_NA` | Bots can't edit admin privileges. |
| `BOT_COMMAND_DESCRIPTION_INVALID` | The specified command description is invalid. |
| `BOT_COMMAND_INVALID` | The specified command is invalid. |
| `BOT_CREATE_LIMIT_EXCEEDED` | The current user already owns the maximum allowed number of owned bots, as specified by `bots_create_limit_default` » and `bots_create_limit_premium` »; if the current user doesn't have Telegram Premium, upgrading to Premium will allow them to create more bots. |
| `BOT_DOMAIN_INVALID` | Bot domain invalid. |
| `BOT_FALLBACK_UNSUPPORTED` | The fallback flag can't be set for bots. |
| `BOT_GAMES_DISABLED` | Games can't be sent to channels. |
| `BOT_GROUPS_BLOCKED` | This bot can't be added to groups. |
| `BOT_ID_INVALID` | The specified bot ID is invalid. |
| `BOT_INLINE_DISABLED` | This bot can't be used in inline mode. |
| `BOT_INVALID` | This is not a valid bot. |
| `BOT_INVOICE_INVALID` | The specified invoice is invalid. |
| `BOT_METHOD_INVALID` | The specified method cannot be used by bots. |
| `BOT_NOT_CONNECTED_YET` | No business bot is connected to the currently logged in user. |
| `BOT_ONESIDE_NOT_AVAIL` | Bots can't pin messages in PM just for themselves. |
| `BOT_PAYMENTS_DISABLED` | Please enable bot payments in botfather before calling this method. |
| `BOT_RESPONSE_TIMEOUT` | A timeout occurred while fetching data from the bot. |
| `BOT_SCORE_NOT_MODIFIED` | The score wasn't modified. |
| `BOT_WEBVIEW_DISABLED` | A webview cannot be opened in the specified conditions: emitted for example if `from_bot_menu` or `url` are set and `peer` is not the chat with the bot. |

### `FILE_*` (21)

| Error | Description |
| --- | --- |
| `FILE_CONTENT_TYPE_INVALID` | File content-type is invalid. |
| `FILE_EMTPY` | An empty file was provided. |
| `FILE_ID_INVALID` | The provided file id is invalid. |
| `FILE_MIGRATE_%d` | The file currently being accessed is stored in DC %d, please re-send the query to that DC. |
| `FILE_PARTS_INVALID` | The number of file parts is invalid. |
| `FILE_PART_%d_MISSING` | Part %d of the file is missing from storage. Try repeating the method call to resave the part. |
| `FILE_PART_EMPTY` | The provided file part is empty. |
| `FILE_PART_INVALID` | The file part number is invalid. |
| `FILE_PART_LENGTH_INVALID` | The length of a file part is invalid. |
| `FILE_PART_SIZE_CHANGED` | Provided file part size has changed. |
| `FILE_PART_SIZE_INVALID` | The provided file part size is invalid. |
| `FILE_PART_TOO_BIG` | The uploaded file part is too big. |
| `FILE_PART_TOO_SMALL` | The size of the uploaded file part is too small, please see the documentation for the allowed sizes. |
| `FILE_REFERENCE_%d_EMPTY` | The file reference of the media file at offset %d in the multi_media array is invalid. |
| `FILE_REFERENCE_%d_EXPIRED` | The file reference of the media file at index %d in the passed media array expired, it must be refreshed as specified in the documentation. . |
| `FILE_REFERENCE_%d_INVALID` | The file reference of the media file at index %d in the passed media array is invalid. |
| `FILE_REFERENCE_EMPTY` | An empty file reference was specified. |
| `FILE_REFERENCE_EXPIRED` | File reference expired, it must be refetched as described in the documentation. |
| `FILE_REFERENCE_INVALID` | The specified file reference is invalid. |
| `FILE_TITLE_EMPTY` | An empty file title was specified. |
| `FILE_TOKEN_INVALID` | The master DC did not accept the `file_token` (e.g., the token has expired). Continue downloading the file from the master DC using upload.getFile. |

### `STARGIFT_*` (21)

| Error | Description |
| --- | --- |
| `STARGIFT_ALREADY_CONVERTED` | The specified star gift was already converted to Stars. |
| `STARGIFT_ALREADY_REFUNDED` | The specified star gift was already refunded. |
| `STARGIFT_ALREADY_UPGRADED` | The specified gift was already upgraded to a collectible gift. |
| `STARGIFT_ATTRIBUTE_INVALID` | One of the specified star gift attributes is invalid. |
| `STARGIFT_INVALID` | The passed gift is invalid. |
| `STARGIFT_MESSAGE_INVALID` | The specified inputInvoiceStarGift.message is invalid. |
| `STARGIFT_NOT_FOUND` | The specified gift was not found. |
| `STARGIFT_NOT_OWNER` | You're not the owner of the gift you trying to transfer. |
| `STARGIFT_NOT_UNIQUE` | You can't transfer a non-collectible gift. |
| `STARGIFT_OBJECT_INVALID` | The specified star gift object is invalid. |
| `STARGIFT_OFFER_INVALID` | The specified offer amount is invalid. |
| `STARGIFT_OFFER_NOT_ALLOWED` | You can't send a purchase offer for this gift. |
| `STARGIFT_OWNER_INVALID` | You cannot transfer or sell a gift owned by another user. |
| `STARGIFT_PEER_INVALID` | The specified inputSavedStarGiftChat.peer is invalid. |
| `STARGIFT_RESELL_CURRENCY_NOT_ALLOWED` | You can't buy the gift using the specified currency (i.e. trying to pay in Stars for TON gifts). |
| `STARGIFT_RESELL_TOO_EARLY_%d` | You will be able to resell this gift in %d seconds. |
| `STARGIFT_SLUG_INVALID` | The specified gift slug is invalid. |
| `STARGIFT_TRANSFER_TOO_EARLY_%d` | You cannot transfer this gift yet, wait %d seconds. |
| `STARGIFT_UPGRADE_UNAVAILABLE` | A received gift can only be upgraded to a collectible gift if the messageActionStarGift/savedStarGift.`can_upgrade` flag is set. |
| `STARGIFT_USAGE_LIMITED` | The gift is sold out. |
| `STARGIFT_USER_USAGE_LIMITED` | You've reached the starGift.limited_per_user limit, you can't buy any more gifts of this type. |

### `USER_*` (21)

| Error | Description |
| --- | --- |
| `USER_ADMIN_INVALID` | You're not an admin. |
| `USER_ALREADY_INVITED` | You have already invited this user. |
| `USER_ALREADY_PARTICIPANT` | The user is already in the group. |
| `USER_BANNED_IN_CHANNEL` | You're banned from sending messages in supergroups/channels. |
| `USER_BLOCKED` | User blocked. |
| `USER_BOT` | Bots can only be admins in channels. |
| `USER_BOT_INVALID` | User accounts must provide the `bot` method parameter when calling this method. If there is no such method parameter, this method can only be invoked by bot accounts. |
| `USER_BOT_REQUIRED` | This method can only be called by a bot. |
| `USER_BOT_TO_BOT_DISABLED` | Bot-to-bot messaging is disabled because one of the two bots hasn't enabled the Bot to Bot setting in @BotFather. |
| `USER_CHANNELS_TOO_MUCH` | One of the users you tried to add is already in too many channels/supergroups. |
| `USER_CREATOR` | For channels.editAdmin: you've tried to edit the admin rights of the owner, but you're not the owner; for channels.leaveChannel: you can't leave this channel, because you're its creator. |
| `USER_GIFT_UNAVAILABLE` | Gifts are not available in the current region (stars_gifts_enabled is equal to false). |
| `USER_ID_INVALID` | The provided user ID is invalid. |
| `USER_INVALID` | Invalid user provided. |
| `USER_IS_BLOCKED` | You were blocked by this user. |
| `USER_IS_BOT` | Bots can't send messages to other bots. |
| `USER_KICKED` | This user was kicked from this supergroup/channel. |
| `USER_NOT_MUTUAL_CONTACT` | The provided user is not a mutual contact. |
| `USER_NOT_PARTICIPANT` | You're not a member of this supergroup/channel. |
| `USER_PUBLIC_MISSING` | Cannot generate a link to stories posted by a peer without a username. |
| `USER_VOLUME_INVALID` | The specified user volume is invalid. |

### `CHAT_*` (19)

| Error | Description |
| --- | --- |
| `CHAT_ABOUT_NOT_MODIFIED` | About text has not changed. |
| `CHAT_ABOUT_TOO_LONG` | Chat about too long. |
| `CHAT_ADMIN_REQUIRED` | You must be an admin in this chat to do this. |
| `CHAT_DISCUSSION_UNALLOWED` | You can't enable forum topics in a discussion group linked to a channel. |
| `CHAT_FORWARDS_RESTRICTED` | You can't forward messages from a protected chat. |
| `CHAT_ID_EMPTY` | The provided chat ID is empty. |
| `CHAT_ID_INVALID` | The provided chat id is invalid. |
| `CHAT_INVALID` | Invalid chat. |
| `CHAT_INVITE_PERMANENT` | You can't set an expiration date on permanent invite links. |
| `CHAT_LINK_EXISTS` | The chat is public, you can't hide the history to new users. |
| `CHAT_MEMBER_ADD_FAILED` | Could not add participants. |
| `CHAT_NOT_MODIFIED` | No changes were made to chat information because the new information you passed is identical to the current information. |
| `CHAT_PUBLIC_REQUIRED` | You can only enable join requests in public groups. |
| `CHAT_RESTRICTED` | You can't send messages in this chat, you were restricted. |
| `CHAT_REVOKE_DATE_UNSUPPORTED` | `min_date` and `max_date` are not available for using with non-user peers. |
| `CHAT_SEND_INLINE_FORBIDDEN` | You can't send inline messages in this group. |
| `CHAT_TITLE_EMPTY` | No chat title provided. |
| `CHAT_TOO_BIG` | This method is not available for groups with more than `chat_read_mark_size_threshold` members, see client configuration ». |
| `CHAT_WRITE_FORBIDDEN` | You can't write in this chat. |

### `INPUT_*` (16)

| Error | Description |
| --- | --- |
| `INPUT_CHATLIST_INVALID` | The specified folder is invalid. |
| `INPUT_CONSTRUCTOR_INVALID` | The specified TL constructor is invalid. |
| `INPUT_FETCH_ERROR` | An error occurred while parsing the provided TL constructor. |
| `INPUT_FETCH_FAIL` | An error occurred while parsing the provided TL constructor. |
| `INPUT_FILE_INVALID` | The specified InputFile is invalid. |
| `INPUT_FILTER_INVALID` | The specified filter is invalid. |
| `INPUT_LAYER_INVALID` | The specified layer is invalid. |
| `INPUT_METHOD_INVALID` | The specified method is invalid. |
| `INPUT_PEERS_EMPTY` | The specified peer array is empty. |
| `INPUT_PURPOSE_INVALID` | The specified payment purpose is invalid. |
| `INPUT_REQUEST_TOO_LONG` | The request payload is too long. |
| `INPUT_STARS_AMOUNT_INVALID` | The specified offer amount in stars is invalid, see here » for the allowed range. |
| `INPUT_STARS_NANOS_INVALID` | The specified offer amount in nanotons is invalid, see here » for the allowed range. |
| `INPUT_TEXT_EMPTY` | The specified text is empty. |
| `INPUT_TEXT_TOO_LONG` | The specified text is too long. |
| `INPUT_USER_DEACTIVATED` | The specified user was deleted. |

### `STICKER_*` (16)

| Error | Description |
| --- | --- |
| `STICKER_DOCUMENT_INVALID` | The specified sticker document is invalid. |
| `STICKER_EMOJI_INVALID` | Sticker emoji invalid. |
| `STICKER_FILE_INVALID` | Sticker file invalid. |
| `STICKER_GIF_DIMENSIONS` | The specified video sticker has invalid dimensions. |
| `STICKER_ID_INVALID` | The provided sticker ID is invalid. |
| `STICKER_INVALID` | The provided sticker is invalid. |
| `STICKER_MIME_INVALID` | The specified sticker MIME type is invalid. |
| `STICKER_PNG_DIMENSIONS` | Sticker png dimensions invalid. |
| `STICKER_PNG_NOPNG` | One of the specified stickers is not a valid PNG file. |
| `STICKER_TGS_NODOC` | You must send the animated sticker as a document. |
| `STICKER_TGS_NOTGS` | Invalid TGS sticker provided. |
| `STICKER_THUMB_PNG_NOPNG` | Incorrect stickerset thumb file provided, PNG / WEBP expected. |
| `STICKER_THUMB_TGS_NOTGS` | Incorrect stickerset TGS thumb file provided. |
| `STICKER_VIDEO_BIG` | The specified video sticker is too big. |
| `STICKER_VIDEO_NODOC` | You must send the video sticker as a document. |
| `STICKER_VIDEO_NOWEBM` | The specified video sticker is not in webm format. |

### `PHONE_*` (13)

| Error | Description |
| --- | --- |
| `PHONE_CODE_EMPTY` | phone_code is missing. |
| `PHONE_CODE_EXPIRED` | The phone code you provided has expired. |
| `PHONE_CODE_HASH_EMPTY` | phone_code_hash is missing. |
| `PHONE_CODE_INVALID` | The provided phone code is invalid. |
| `PHONE_HASH_EXPIRED` | An invalid or expired `phone_code_hash` was provided. |
| `PHONE_NOT_OCCUPIED` | No user is associated to the specified phone number. |
| `PHONE_NUMBER_APP_SIGNUP_FORBIDDEN` | You can't sign up using this app. |
| `PHONE_NUMBER_BANNED` | The provided phone number is banned from telegram. |
| `PHONE_NUMBER_FLOOD` | You asked for the code too many times. |
| `PHONE_NUMBER_INVALID` | The phone number is invalid. |
| `PHONE_NUMBER_OCCUPIED` | The phone number is already in use. |
| `PHONE_NUMBER_UNOCCUPIED` | The phone number is not yet being used. |
| `PHONE_PASSWORD_PROTECTED` | This phone is password protected. |

### `MEDIA_*` (11)

| Error | Description |
| --- | --- |
| `MEDIA_ALREADY_PAID` | You already paid for the specified media. |
| `MEDIA_CAPTION_TOO_LONG` | The caption is too long. |
| `MEDIA_EMPTY` | The provided media object is invalid. |
| `MEDIA_FILE_INVALID` | The specified media file is invalid. |
| `MEDIA_GROUPED_INVALID` | You tried to send media of different types in an album. |
| `MEDIA_INVALID` | Media invalid. |
| `MEDIA_NEW_INVALID` | The new media is invalid. |
| `MEDIA_PREV_INVALID` | Previous media invalid. |
| `MEDIA_TTL_INVALID` | The specified media TTL is invalid. |
| `MEDIA_TYPE_INVALID` | The specified media type cannot be used in stories. |
| `MEDIA_VIDEO_STORY_MISSING` | A non-story video cannot be repubblished as a story (emitted when trying to resend a non-story video as a story using inputDocument). |

### `PHOTO_*` (11)

| Error | Description |
| --- | --- |
| `PHOTO_CONTENT_TYPE_INVALID` | Photo mime-type invalid. |
| `PHOTO_CONTENT_URL_EMPTY` | Photo URL invalid. |
| `PHOTO_CROP_FILE_MISSING` | Photo crop file missing. |
| `PHOTO_CROP_SIZE_SMALL` | Photo is too small. |
| `PHOTO_EXT_INVALID` | The extension of the photo is invalid. |
| `PHOTO_FILE_MISSING` | Profile photo file missing. |
| `PHOTO_ID_INVALID` | Photo ID invalid. |
| `PHOTO_INVALID` | Photo invalid. |
| `PHOTO_INVALID_DIMENSIONS` | The photo dimensions are invalid. |
| `PHOTO_SAVE_FILE_INVALID` | Internal issues, try again later. |
| `PHOTO_THUMB_URL_EMPTY` | Photo thumbnail URL is empty. |

### `BUTTON_*` (10)

| Error | Description |
| --- | --- |
| `BUTTON_COPY_TEXT_INVALID` | The specified keyboardButtonCopy.`copy_text` is invalid. |
| `BUTTON_DATA_INVALID` | The data of one or more of the buttons you provided is invalid. |
| `BUTTON_ID_INVALID` | The specified button ID is invalid. |
| `BUTTON_INVALID` | The specified button is invalid. |
| `BUTTON_POS_INVALID` | The position of one of the keyboard buttons is invalid (i.e. a Game or Pay button not in the first position, and so on...). |
| `BUTTON_TEXT_INVALID` | The specified button text is invalid. |
| `BUTTON_TYPE_INVALID` | The type of one or more of the buttons you provided is invalid. |
| `BUTTON_URL_INVALID` | Button URL invalid. |
| `BUTTON_USER_INVALID` | The `user_id` passed to inputKeyboardButtonUserProfile is invalid! |
| `BUTTON_USER_PRIVACY_RESTRICTED` | The privacy setting of the user specified in a inputKeyboardButtonUserProfile button do not allow creating such a button. |

### `MESSAGE_*` (10)

| Error | Description |
| --- | --- |
| `MESSAGE_EDIT_TIME_EXPIRED` | You can't edit this message anymore, too much time has passed since its creation. |
| `MESSAGE_EMPTY` | The provided message is empty. |
| `MESSAGE_IDS_EMPTY` | No message ids were provided. |
| `MESSAGE_ID_INVALID` | The provided message id is invalid. |
| `MESSAGE_NOT_MODIFIED` | The provided message data is identical to the previous message data, the message wasn't modified. |
| `MESSAGE_NOT_READ_YET` | The specified message wasn't read yet. |
| `MESSAGE_POLL_CLOSED` | Poll closed. |
| `MESSAGE_REQUIRED` | A non-empty list of IDs must be passed to `id`. |
| `MESSAGE_TOO_LONG` | The provided message is too long. |
| `MESSAGE_TOO_OLD` | The message is too old, the requested information is not available. |

### `CONNECTION_*` (9)

| Error | Description |
| --- | --- |
| `CONNECTION_API_ID_INVALID` | The provided API id is invalid. |
| `CONNECTION_APP_VERSION_EMPTY` | App version is empty. |
| `CONNECTION_DEVICE_MODEL_EMPTY` | The specified device model is empty. |
| `CONNECTION_ID_INVALID` | The specified connection ID is invalid. |
| `CONNECTION_LANG_PACK_INVALID` | The specified language pack is empty. |
| `CONNECTION_LAYER_INVALID` | Layer invalid. |
| `CONNECTION_NOT_INITED` | Please initialize the connection using initConnection before making queries. |
| `CONNECTION_SYSTEM_EMPTY` | The specified system version is empty. |
| `CONNECTION_SYSTEM_LANG_CODE_EMPTY` | The specified system language code is empty. |

### `INVITE_*` (9)

| Error | Description |
| --- | --- |
| `INVITE_FORBIDDEN_WITH_JOINAS` | If the user has anonymously joined a group call as a channel, they can't invite other users to the group call because that would cause deanonymization, because the invite would be sent using the original user ID, not the anonymized channel ID. |
| `INVITE_HASH_EMPTY` | The invite hash is empty. |
| `INVITE_HASH_EXPIRED` | The invite link has expired. |
| `INVITE_HASH_INVALID` | The invite hash is invalid. |
| `INVITE_REQUEST_SENT` | You have successfully requested to join this chat or channel. |
| `INVITE_REVOKED_MISSING` | The specified invite link was already revoked or is invalid. |
| `INVITE_SLUG_EMPTY` | The specified invite slug is empty. |
| `INVITE_SLUG_EXPIRED` | The specified chat folder link has expired. |
| `INVITE_SLUG_INVALID` | The specified invitation slug is invalid. |

### `REPLY_*` (9)

| Error | Description |
| --- | --- |
| `REPLY_MARKUP_BUY_EMPTY` | Reply markup for buy button empty. |
| `REPLY_MARKUP_GAME_EMPTY` | A game message is being edited, but the newly provided keyboard doesn't have a keyboardButtonGame button. |
| `REPLY_MARKUP_INVALID` | The provided reply markup is invalid. |
| `REPLY_MARKUP_TOO_LONG` | The specified reply_markup is too long. |
| `REPLY_MESSAGES_TOO_MUCH` | Each shortcut can contain a maximum of appConfig.`quick_reply_messages_limit` messages, the limit was reached. |
| `REPLY_MESSAGE_ID_INVALID` | The specified reply-to message ID is invalid. |
| `REPLY_TO_INVALID` | The specified `reply_to` field is invalid. |
| `REPLY_TO_MONOFORUM_PEER_INVALID` | The specified inputReplyToMonoForum.monoforum_peer_id is invalid. |
| `REPLY_TO_USER_INVALID` | The replied-to user is invalid. |

### `CHANNEL_*` (8)

| Error | Description |
| --- | --- |
| `CHANNEL_FORUM_MISSING` | This supergroup is not a forum. |
| `CHANNEL_ID_INVALID` | The specified supergroup ID is invalid. |
| `CHANNEL_INVALID` | The provided channel is invalid. |
| `CHANNEL_MONOFORUM_UNSUPPORTED` | Monoforums do not support this feature. |
| `CHANNEL_PARICIPANT_MISSING` | The current user is not in the channel. |
| `CHANNEL_PRIVATE` | You haven't joined this channel/supergroup. |
| `CHANNEL_TOO_BIG` | This channel has too many participants (>1000) to be deleted. |
| `CHANNEL_TOO_LARGE` | Channel is too large to be deleted; this error is issued when trying to delete channels with more than 1000 members (subject to change). |

### `EMAIL_*` (8)

| Error | Description |
| --- | --- |
| `EMAIL_HASH_EXPIRED` | Email hash expired. |
| `EMAIL_INSTALL_MISSING` | Attempting to send a code to the recovery email, but no email is configured. |
| `EMAIL_INVALID` | The specified email is invalid. |
| `EMAIL_NOT_ALLOWED` | The specified email cannot be used to complete the operation. |
| `EMAIL_NOT_SETUP` | In order to change the login email with emailVerifyPurposeLoginChange, an existing login email must already be set using emailVerifyPurposeLoginSetup. |
| `EMAIL_UNCONFIRMED` | Email unconfirmed. |
| `EMAIL_UNCONFIRMED_%d` | The provided email isn't confirmed, %d is the length of the verification code that was just sent to the email: use account.verifyEmail to enter the received verification code and enable the recovery email. |
| `EMAIL_VERIFY_EXPIRED` | The verification email has expired. |

### `BUSINESS_*` (7)

| Error | Description |
| --- | --- |
| `BUSINESS_CONNECTION_INVALID` | The `connection_id` passed to the wrapping invokeWithBusinessConnection call is invalid. |
| `BUSINESS_CONNECTION_NOT_ALLOWED` | This method was invoked over a business connection using invokeWithBusinessConnection, but either (1) we're a user, and users cannot invoke methods over a business connection; (2) we're a bot, but business mode was disabled in @botfather or (3); we're a bot, but this method cannot be invoked over a business connection. |
| `BUSINESS_PEER_INVALID` | Messages can't be set to the specified peer through the current business connection. |
| `BUSINESS_PEER_USAGE_MISSING` | You cannot send a message to a user through a business connection if the user hasn't recently contacted us. |
| `BUSINESS_RECIPIENTS_EMPTY` | You didn't set any flag in inputBusinessBotRecipients, thus the bot cannot work with *any* peer. |
| `BUSINESS_WORK_HOURS_EMPTY` | No work hours were specified. |
| `BUSINESS_WORK_HOURS_PERIOD_INVALID` | The specified work hours are invalid, see here » for the exact requirements. |

### `CALL_*` (7)

| Error | Description |
| --- | --- |
| `CALL_ALREADY_ACCEPTED` | The call was already accepted. |
| `CALL_ALREADY_DECLINED` | The call was already declined. |
| `CALL_NOT_ACTIVE` | The specified call is not active. |
| `CALL_OCCUPY_FAILED` | The call failed because the user is already making another call. |
| `CALL_PEER_INVALID` | The provided call peer object is invalid. |
| `CALL_PROTOCOL_FLAGS_INVALID` | Call protocol flags invalid. |
| `CALL_PROTOCOL_LAYER_INVALID` | The specified protocol layer version range is invalid. |

### `PASSWORD_*` (7)

| Error | Description |
| --- | --- |
| `PASSWORD_EMPTY` | The provided password is empty. |
| `PASSWORD_HASH_INVALID` | The provided password hash is invalid. |
| `PASSWORD_MISSING` | You must enable 2FA before executing this operation. |
| `PASSWORD_RECOVERY_EXPIRED` | The recovery code has expired. |
| `PASSWORD_RECOVERY_NA` | No email was set, can't recover password via email. |
| `PASSWORD_REQUIRED` | A 2FA password must be configured to use Telegram Passport. |
| `PASSWORD_TOO_FRESH_%d` | The password was modified less than 24 hours ago, try again in %d seconds. |

### `STORY_*` (7)

| Error | Description |
| --- | --- |
| `STORY_ID_EMPTY` | You specified no story IDs. |
| `STORY_ID_INVALID` | The specified story ID is invalid. |
| `STORY_LIVE_ALREADY_%d` | This peer already has an active live story, and its ID is equal to %d. |
| `STORY_NOT_MODIFIED` | The new story information you passed is equal to the previous story information, thus it wasn't modified. |
| `STORY_PERIOD_INVALID` | The specified story period is invalid for this account. |
| `STORY_SEND_FLOOD_MONTHLY_%d` | You've hit the monthly story limit as specified by the `stories_sent_monthly_limit_*` client configuration parameters: wait %d seconds before posting a new story. |
| `STORY_SEND_FLOOD_WEEKLY_%d` | You've hit the weekly story limit as specified by the `stories_sent_weekly_limit_*` client configuration parameters: wait for %d seconds before posting a new story. |

### `THEME_*` (7)

| Error | Description |
| --- | --- |
| `THEME_FILE_INVALID` | Invalid theme file provided. |
| `THEME_FORMAT_INVALID` | Invalid theme format provided. |
| `THEME_INVALID` | Invalid theme provided. |
| `THEME_MIME_INVALID` | The theme's MIME type is invalid. |
| `THEME_PARAMS_INVALID` | The specified `theme_params` field is invalid. |
| `THEME_SLUG_INVALID` | The specified theme slug is invalid. |
| `THEME_TITLE_INVALID` | The specified theme title is invalid. |

### `TOPIC_*` (7)

| Error | Description |
| --- | --- |
| `TOPIC_CLOSED` | This topic was closed, you can't send messages to it anymore. |
| `TOPIC_CLOSE_SEPARATELY` | The `close` flag cannot be provided together with any of the other flags. |
| `TOPIC_DELETED` | The specified topic was deleted. |
| `TOPIC_HIDE_SEPARATELY` | The `hide` flag cannot be provided together with any of the other flags. |
| `TOPIC_ID_INVALID` | The specified topic ID is invalid. |
| `TOPIC_NOT_MODIFIED` | The updated topic info is equal to the current topic info, nothing was changed. |
| `TOPIC_TITLE_EMPTY` | The specified topic title is empty. |

### `AUTH_*` (6)

| Error | Description |
| --- | --- |
| `AUTH_BYTES_INVALID` | The provided authorization is invalid. |
| `AUTH_TOKEN_ALREADY_ACCEPTED` | The specified auth token was already accepted. |
| `AUTH_TOKEN_EXCEPTION` | An error occurred while importing the auth token. |
| `AUTH_TOKEN_EXPIRED` | The authorization token has expired. |
| `AUTH_TOKEN_INVALID` | The specified auth token is invalid. |
| `AUTH_TOKEN_INVALIDX` | The specified auth token is invalid. |

### `GROUPCALL_*` (6)

| Error | Description |
| --- | --- |
| `GROUPCALL_ALREADY_DISCARDED` | The group call was already discarded. |
| `GROUPCALL_FORBIDDEN` | The specified group call cannot be used in this context. |
| `GROUPCALL_INVALID` | The specified group call is invalid. |
| `GROUPCALL_JOIN_MISSING` | You haven't joined this group call. |
| `GROUPCALL_NOT_MODIFIED` | Group call settings weren't modified. |
| `GROUPCALL_SSRC_DUPLICATE_MUCH` | The app needs to retry joining the group call with a new SSRC value. |

### `USERNAME_*` (6)

| Error | Description |
| --- | --- |
| `USERNAME_INVALID` | The provided username is not valid. |
| `USERNAME_NOT_MODIFIED` | The username was not modified. |
| `USERNAME_NOT_OCCUPIED` | The provided username is not occupied. |
| `USERNAME_OCCUPIED` | The provided username is already occupied. |
| `USERNAME_PURCHASE_AVAILABLE` | The specified username can be purchased on https://fragment.com. |
| `USERNAME_SUFFIX_MISSING` | The required `bot` suffix is missing from the passed username. |

### `VIDEO_*` (6)

| Error | Description |
| --- | --- |
| `VIDEO_CONTENT_TYPE_INVALID` | The video's content type is invalid. |
| `VIDEO_DURATION_INVALID` | The duration of the specified video is invalid. |
| `VIDEO_FILE_INVALID` | The specified video file is invalid. |
| `VIDEO_PAUSE_FORBIDDEN` | You cannot pause the video stream. |
| `VIDEO_STOP_FORBIDDEN` | You cannot stop the video stream. |
| `VIDEO_TITLE_EMPTY` | The specified video title is empty. |

### `CONTACT_*` (5)

| Error | Description |
| --- | --- |
| `CONTACT_ADD_MISSING` | Contact to add is missing. |
| `CONTACT_ID_INVALID` | The provided contact ID is invalid. |
| `CONTACT_MISSING` | The specified user is not a contact. |
| `CONTACT_NAME_EMPTY` | Contact name empty. |
| `CONTACT_REQ_MISSING` | Missing contact request. |

### `ENTITY_*` (5)

| Error | Description |
| --- | --- |
| `ENTITY_BOUNDS_INVALID` | A specified entity offset or length is invalid, see here » for info on how to properly compute the entity offset/length. |
| `ENTITY_DATE_FORMAT_INVALID` | One of the passed messageEntityFormattedDate objects has an invalid format (i.e. an invalid combination of the format flags). |
| `ENTITY_DATE_INVALID` | One of the passed messageEntityFormattedDate objects has an invalid date: the allowed value ranges from `0` to the current date plus 1098 days (`time()+1098*86400`). |
| `ENTITY_DATE_TOO_LONG` | The maximum text span that can be covered by a date entity is 31 UTF-16 code units if any of the date formatting flags is set, or 127 UTF-16 code units without. . |
| `ENTITY_MENTION_USER_INVALID` | You mentioned an invalid user. |

### `IMPORT_*` (5)

| Error | Description |
| --- | --- |
| `IMPORT_FILE_INVALID` | The specified chat export file is invalid. |
| `IMPORT_FORMAT_DATE_INVALID` | The date specified in the import file is invalid. |
| `IMPORT_FORMAT_UNRECOGNIZED` | The specified chat export file was exported from an unsupported chat app. |
| `IMPORT_ID_INVALID` | The specified import ID is invalid. |
| `IMPORT_TOKEN_INVALID` | The specified token is invalid. |

### `MSG_*` (5)

| Error | Description |
| --- | --- |
| `MSG_ID_INVALID` | Invalid message ID provided. |
| `MSG_TOO_OLD` | `chat_read_mark_expire_period` seconds have passed since the message was sent, read receipts were deleted. |
| `MSG_VOICE_MISSING` | The specified message is not a voice message. |
| `MSG_VOICE_TOO_LONG` | The specified voice message is too long to be transcribed. |
| `MSG_WAIT_FAILED` | A waiting call returned an error. |

### `PEER_*` (5)

| Error | Description |
| --- | --- |
| `PEER_FLOOD` | The current account is spamreported, you cannot execute this action, check @spambot for more info. |
| `PEER_HISTORY_EMPTY` | You can't pin an empty chat with a user. |
| `PEER_ID_INVALID` | The provided peer id is invalid. |
| `PEER_ID_NOT_SUPPORTED` | The provided peer ID is not supported. |
| `PEER_TYPES_INVALID` | The passed keyboardButtonSwitchInline.`peer_types` field is invalid. |

### `POLL_*` (5)

| Error | Description |
| --- | --- |
| `POLL_ANSWERS_INVALID` | Invalid poll answers were provided. |
| `POLL_ANSWER_INVALID` | One of the poll answers is not acceptable. |
| `POLL_OPTION_DUPLICATE` | Duplicate poll options provided. |
| `POLL_OPTION_INVALID` | Invalid poll option provided. |
| `POLL_QUESTION_INVALID` | One of the poll questions is not acceptable. |

### `QUIZ_*` (5)

| Error | Description |
| --- | --- |
| `QUIZ_ANSWER_MISSING` | You can forward a quiz while hiding the original author only after choosing an option in the quiz. |
| `QUIZ_CORRECT_ANSWERS_EMPTY` | No correct quiz answer was specified. |
| `QUIZ_CORRECT_ANSWERS_TOO_MUCH` | You specified too many correct answers in a quiz, quizzes can only have one right answer! |
| `QUIZ_CORRECT_ANSWER_INVALID` | An invalid value was provided to the correct_answers field. |
| `QUIZ_MULTIPLE_INVALID` | Quizzes can't have the multiple_choice flag set! |

### `RANDOM_*` (5)

| Error | Description |
| --- | --- |
| `RANDOM_ID_DUPLICATE` | You provided a random ID that was already used. |
| `RANDOM_ID_EMPTY` | Random ID empty. |
| `RANDOM_ID_EXPIRED` | The specified `random_id` was expired (most likely it didn't follow the required `uint64_t random_id = (time() << 32) \| ((uint64_t)random_uint32_t())` format, or the specified time is too far in the past). |
| `RANDOM_ID_INVALID` | A provided random ID is invalid. |
| `RANDOM_LENGTH_INVALID` | Random length invalid. |

### `SCHEDULE_*` (5)

| Error | Description |
| --- | --- |
| `SCHEDULE_BOT_NOT_ALLOWED` | Bots cannot schedule messages. |
| `SCHEDULE_DATE_INVALID` | Invalid schedule date provided. |
| `SCHEDULE_DATE_TOO_LATE` | You can't schedule a message this far in the future. |
| `SCHEDULE_STATUS_PRIVATE` | Can't schedule until user is online, if the user's last seen timestamp is hidden by their privacy settings. |
| `SCHEDULE_TOO_MUCH` | There are too many scheduled messages. |

### `STARREF_*` (5)

| Error | Description |
| --- | --- |
| `STARREF_AWAITING_END` | The previous referral program was terminated less than 24 hours ago: further changes can be made after the date specified in userFull.starref_program.end_date. |
| `STARREF_EXPIRED` | The specified referral link is invalid. |
| `STARREF_HASH_REVOKED` | The specified affiliate link was already revoked. |
| `STARREF_PERMILLE_INVALID` | The specified commission_permille is invalid: the minimum and maximum values for this parameter are contained in the starref_min_commission_permille and starref_max_commission_permille client configuration parameters. |
| `STARREF_PERMILLE_TOO_LOW` | The specified commission_permille is too low: the minimum and maximum values for this parameter are contained in the starref_min_commission_permille and starref_max_commission_permille client configuration parameters. |

### `WEBDOCUMENT_*` (5)

| Error | Description |
| --- | --- |
| `WEBDOCUMENT_INVALID` | Invalid webdocument URL provided. |
| `WEBDOCUMENT_MIME_INVALID` | Invalid webdocument mime type provided. |
| `WEBDOCUMENT_SIZE_TOO_BIG` | Webdocument is too big! |
| `WEBDOCUMENT_URL_EMPTY` | The passed web document URL is empty. |
| `WEBDOCUMENT_URL_INVALID` | The specified webdocument URL is invalid. |

### `ADMIN_*` (4)

| Error | Description |
| --- | --- |
| `ADMIN_ID_INVALID` | The specified admin ID is invalid. |
| `ADMIN_RANK_EMOJI_NOT_ALLOWED` | An admin rank cannot contain emojis. |
| `ADMIN_RANK_INVALID` | The specified admin rank is invalid. |
| `ADMIN_RIGHTS_EMPTY` | The chatAdminRights constructor passed in keyboardButtonRequestPeer.peer_type.user_admin_rights has no rights set (i.e. flags is 0). |

### `DATA_*` (4)

| Error | Description |
| --- | --- |
| `DATA_HASH_SIZE_INVALID` | The size of the specified secureValueErrorData.data_hash is invalid. |
| `DATA_INVALID` | Encrypted data invalid. |
| `DATA_JSON_INVALID` | The provided JSON data is invalid. |
| `DATA_TOO_LONG` | Data too long. |

### `ENCRYPTION_*` (4)

| Error | Description |
| --- | --- |
| `ENCRYPTION_ALREADY_ACCEPTED` | Secret chat already accepted. |
| `ENCRYPTION_ALREADY_DECLINED` | The secret chat was already declined. |
| `ENCRYPTION_DECLINED` | The secret chat was declined. |
| `ENCRYPTION_ID_INVALID` | The provided secret chat ID is invalid. |

### `EXTENDED_*` (4)

| Error | Description |
| --- | --- |
| `EXTENDED_MEDIA_AMOUNT_INVALID` | The specified `stars_amount` of the passed inputMediaPaidMedia is invalid. |
| `EXTENDED_MEDIA_EMPTY` | The specified extended media is empty. |
| `EXTENDED_MEDIA_INVALID` | The specified paid media is invalid. |
| `EXTENDED_MEDIA_PEER_INVALID` | Paid media is not allowed for the target peer. |

### `FILTER_*` (4)

| Error | Description |
| --- | --- |
| `FILTER_ID_INVALID` | The specified filter ID is invalid. |
| `FILTER_INCLUDE_EMPTY` | The include_peers vector of the filter is empty. |
| `FILTER_NOT_SUPPORTED` | The specified filter cannot be used in this context. |
| `FILTER_TITLE_EMPTY` | The title field of the filter is empty. |

### `FORM_*` (4)

| Error | Description |
| --- | --- |
| `FORM_EXPIRED` | The form was generated more than 10 minutes ago and has expired, please re-generate it using payments.getPaymentForm and pass the new `form_id`. |
| `FORM_ID_EMPTY` | The specified form ID is empty. |
| `FORM_SUBMIT_DUPLICATE` | The same payment form was already submitted. . |
| `FORM_UNSUPPORTED` | Please update your client. |

### `GIFT_*` (4)

| Error | Description |
| --- | --- |
| `GIFT_MONTHS_INVALID` | The value passed in invoice.inputInvoicePremiumGiftStars.months is invalid. |
| `GIFT_SLUG_EXPIRED` | The specified gift slug has expired. |
| `GIFT_SLUG_INVALID` | The specified slug is invalid. |
| `GIFT_STARS_INVALID` | The specified amount of stars is invalid. |

### `MEGAGROUP_*` (4)

| Error | Description |
| --- | --- |
| `MEGAGROUP_GEO_REQUIRED` | This method can only be invoked on a geogroup. |
| `MEGAGROUP_ID_INVALID` | Invalid supergroup ID. |
| `MEGAGROUP_PREHISTORY_HIDDEN` | Group with hidden history for new members can't be set as discussion groups. |
| `MEGAGROUP_REQUIRED` | You can only use this method on a supergroup. |

### `PACK_*` (4)

| Error | Description |
| --- | --- |
| `PACK_SHORT_NAME_INVALID` | Short pack name invalid. |
| `PACK_SHORT_NAME_OCCUPIED` | A stickerpack with this name already exists. |
| `PACK_TITLE_INVALID` | The stickerpack title is invalid. |
| `PACK_TYPE_INVALID` | The masks and emojis flags are mutually exclusive. |

### `RESULT_*` (4)

| Error | Description |
| --- | --- |
| `RESULT_ID_DUPLICATE` | You provided a duplicate result ID. |
| `RESULT_ID_EMPTY` | Result ID empty. |
| `RESULT_ID_INVALID` | One of the specified result IDs is invalid. |
| `RESULT_TYPE_INVALID` | Result type invalid. |

### `SEND_*` (4)

| Error | Description |
| --- | --- |
| `SEND_AS_PEER_INVALID` | You can't send messages as the specified peer. |
| `SEND_MESSAGE_GAME_INVALID` | An inputBotInlineMessageGame can only be contained in an inputBotInlineResultGame, not in an inputBotInlineResult/inputBotInlineResultPhoto/etc. |
| `SEND_MESSAGE_MEDIA_INVALID` | Invalid media provided. |
| `SEND_MESSAGE_TYPE_INVALID` | The message type is invalid. |

### `TODO_*` (4)

| Error | Description |
| --- | --- |
| `TODO_ITEMS_EMPTY` | A checklist was specified, but no checklist items were passed. |
| `TODO_ITEMS_TOO_MUCH` | You specified too many todo list items. |
| `TODO_ITEM_DUPLICATE` | Duplicate checklist items detected. |
| `TODO_NOT_MODIFIED` | No todo items were specified, so no changes were made to the todo list. |

### `WALLPAPER_*` (4)

| Error | Description |
| --- | --- |
| `WALLPAPER_FILE_INVALID` | The specified wallpaper file is invalid. |
| `WALLPAPER_INVALID` | The specified wallpaper is invalid. |
| `WALLPAPER_MIME_INVALID` | The specified wallpaper MIME type is invalid. |
| `WALLPAPER_NOT_FOUND` | The specified wallpaper could not be found. |

### `WEBPAGE_*` (4)

| Error | Description |
| --- | --- |
| `WEBPAGE_CURL_FAILED` | Failure while fetching the webpage with cURL. |
| `WEBPAGE_MEDIA_EMPTY` | Webpage media empty. |
| `WEBPAGE_NOT_FOUND` | A preview for the specified webpage `url` could not be generated. |
| `WEBPAGE_URL_INVALID` | The specified webpage `url` is invalid. |

### `AICOMPOSE_*` (3)

| Error | Description |
| --- | --- |
| `AICOMPOSE_FLOOD_PREMIUM` | You've reached the daily limit of AI text transformations, upgrade to Telegram Premium to get **50x** times more AI text transformations per day! |
| `AICOMPOSE_TONE_INVALID` | The specified tone is invalid. |
| `AICOMPOSE_TONE_TITLE_INVALID` | The specified tone title is invalid. |

### `BROADCAST_*` (3)

| Error | Description |
| --- | --- |
| `BROADCAST_ID_INVALID` | Broadcast ID invalid. |
| `BROADCAST_PUBLIC_VOTERS_FORBIDDEN` | You can't forward polls with public voters. |
| `BROADCAST_REQUIRED` | This method can only be called on a channel, please use stats.getMegagroupStats for supergroups. |

### `CHANNELS_*` (3)

| Error | Description |
| --- | --- |
| `CHANNELS_ADMIN_LOCATED_TOO_MUCH` | The user has reached the limit of public geogroups. |
| `CHANNELS_ADMIN_PUBLIC_TOO_MUCH` | You're admin of too many public channels, make some channels private to change the username of this channel. |
| `CHANNELS_TOO_MUCH` | You have joined too many channels/supergroups. |

### `CHARGE_*` (3)

| Error | Description |
| --- | --- |
| `CHARGE_ALREADY_REFUNDED` | The transaction was already refunded. |
| `CHARGE_ID_EMPTY` | The specified charge_id is empty. |
| `CHARGE_ID_INVALID` | The specified charge_id is invalid. |

### `CODE_*` (3)

| Error | Description |
| --- | --- |
| `CODE_EMPTY` | The provided code is empty. |
| `CODE_HASH_INVALID` | Code hash invalid. |
| `CODE_INVALID` | Code invalid. |

### `EMOJI_*` (3)

| Error | Description |
| --- | --- |
| `EMOJI_INVALID` | The specified theme emoji is valid. |
| `EMOJI_MARKUP_INVALID` | The specified `video_emoji_markup` was invalid. |
| `EMOJI_NOT_MODIFIED` | The theme wasn't changed. |

### `EMOTICON_*` (3)

| Error | Description |
| --- | --- |
| `EMOTICON_EMPTY` | The emoji is empty. |
| `EMOTICON_INVALID` | The specified emoji is invalid. |
| `EMOTICON_STICKERPACK_MISSING` | inputStickerSetDice.emoji cannot be empty. |

### `GRAPH_*` (3)

| Error | Description |
| --- | --- |
| `GRAPH_EXPIRED_RELOAD` | This graph has expired, please obtain a new graph token. |
| `GRAPH_INVALID_RELOAD` | Invalid graph token provided, please reload the stats and provide the updated token. |
| `GRAPH_OUTDATED_RELOAD` | The graph is outdated, please get a new async token using stats.getBroadcastStats. |

### `LANG_*` (3)

| Error | Description |
| --- | --- |
| `LANG_CODE_INVALID` | The specified language code is invalid. |
| `LANG_CODE_NOT_SUPPORTED` | The specified language code is not supported. |
| `LANG_PACK_INVALID` | The provided language pack is invalid. |

### `MAX_*` (3)

| Error | Description |
| --- | --- |
| `MAX_DATE_INVALID` | The specified maximum date is invalid. |
| `MAX_ID_INVALID` | The provided max ID is invalid. |
| `MAX_QTS_INVALID` | The specified max_qts is invalid. |

### `NEW_*` (3)

| Error | Description |
| --- | --- |
| `NEW_SALT_INVALID` | The new salt is invalid. |
| `NEW_SETTINGS_EMPTY` | No password is set on the current account, and no new password was specified in `new_settings`. |
| `NEW_SETTINGS_INVALID` | The new password settings are invalid. |

### `PARTICIPANT_*` (3)

| Error | Description |
| --- | --- |
| `PARTICIPANT_ID_INVALID` | The specified participant ID is invalid. |
| `PARTICIPANT_JOIN_MISSING` | Trying to enable a presentation, when the user hasn't joined the Video Chat with phone.joinGroupCall. |
| `PARTICIPANT_VERSION_OUTDATED` | The other participant does not use an up to date telegram client with support for calls. |

### `PAYMENT_*` (3)

| Error | Description |
| --- | --- |
| `PAYMENT_CREDENTIALS_INVALID` | The specified payment credentials are invalid. |
| `PAYMENT_PROVIDER_INVALID` | The specified payment provider is invalid. |
| `PAYMENT_REQUIRED` | Payment is required for this action, see here » for more info. |

### `PINNED_*` (3)

| Error | Description |
| --- | --- |
| `PINNED_DIALOGS_TOO_MUCH` | Too many pinned dialogs. |
| `PINNED_TOO_MUCH` | There are too many pinned topics, unpin some first. |
| `PINNED_TOPIC_NOT_MODIFIED` | The specified topic is already pinned. |

### `PRIVACY_*` (3)

| Error | Description |
| --- | --- |
| `PRIVACY_KEY_INVALID` | The privacy key is invalid. |
| `PRIVACY_TOO_LONG` | Too many privacy rules were specified, the current limit is 1000. |
| `PRIVACY_VALUE_INVALID` | The specified privacy rule combination is invalid. |

### `PUBLIC_*` (3)

| Error | Description |
| --- | --- |
| `PUBLIC_BROADCAST_EXPECTED` | `channel` only accepts a channel, but a supergroup was passed. |
| `PUBLIC_KEY_INVALID` | The specified e2e public key is invalid. |
| `PUBLIC_KEY_REQUIRED` | A public key is required. |

### `QUERY_*` (3)

| Error | Description |
| --- | --- |
| `QUERY_ID_EMPTY` | The query ID is empty. |
| `QUERY_ID_INVALID` | The query ID is invalid. |
| `QUERY_TOO_SHORT` | The query string is too short. |

### `SRP_*` (3)

| Error | Description |
| --- | --- |
| `SRP_A_INVALID` | The specified inputCheckPasswordSRP.A value is invalid. |
| `SRP_ID_INVALID` | Invalid SRP ID provided. |
| `SRP_PASSWORD_CHANGED` | Password has changed. |

### `STARS_*` (3)

| Error | Description |
| --- | --- |
| `STARS_AMOUNT_INVALID` | The specified amount in stars is invalid. |
| `STARS_INVOICE_INVALID` | The specified Telegram Star invoice is invalid. |
| `STARS_PAYMENT_REQUIRED` | To import this chat invite link, you must first pay for the associated Telegram Star subscription ». |

### `START_*` (3)

| Error | Description |
| --- | --- |
| `START_PARAM_EMPTY` | The start parameter is empty. |
| `START_PARAM_INVALID` | Start parameter invalid. |
| `START_PARAM_TOO_LONG` | Start parameter is too long. |

### `SUBSCRIPTION_*` (3)

| Error | Description |
| --- | --- |
| `SUBSCRIPTION_EXPORT_MISSING` | You cannot send a bot subscription invoice directly, you may only create invoice links using payments.exportInvoice. |
| `SUBSCRIPTION_ID_INVALID` | The specified subscription_id is invalid. |
| `SUBSCRIPTION_PERIOD_INVALID` | The specified subscription_pricing.period is invalid. |

### `TOKEN_*` (3)

| Error | Description |
| --- | --- |
| `TOKEN_EMPTY` | The specified token is empty. |
| `TOKEN_INVALID` | The provided token is invalid. |
| `TOKEN_TYPE_INVALID` | The specified token type is invalid. |

### `TTL_*` (3)

| Error | Description |
| --- | --- |
| `TTL_DAYS_INVALID` | The provided TTL is invalid. |
| `TTL_MEDIA_INVALID` | Invalid media Time To Live was provided. |
| `TTL_PERIOD_INVALID` | The specified TTL period is invalid. |

### `WEBPUSH_*` (3)

| Error | Description |
| --- | --- |
| `WEBPUSH_AUTH_INVALID` | The specified web push authentication secret is invalid. |
| `WEBPUSH_KEY_INVALID` | The specified web push elliptic curve Diffie-Hellman public key is invalid. |
| `WEBPUSH_TOKEN_INVALID` | The specified web push token is invalid. |

### `ACCESS_*` (2)

| Error | Description |
| --- | --- |
| `ACCESS_TOKEN_EXPIRED` | Access token expired. |
| `ACCESS_TOKEN_INVALID` | Access token invalid. |

### `API_*` (2)

| Error | Description |
| --- | --- |
| `API_ID_INVALID` | API ID invalid. |
| `API_ID_PUBLISHED_FLOOD` | This API id was published somewhere, you can't use it now. |

### `AUDIO_*` (2)

| Error | Description |
| --- | --- |
| `AUDIO_CONTENT_URL_EMPTY` | The remote URL specified in the content field is empty. |
| `AUDIO_TITLE_EMPTY` | An empty audio title was provided. |

### `BIRTHDAY_*` (2)

| Error | Description |
| --- | --- |
| `BIRTHDAY_ALREADY` | The target user already has a birthday set. |
| `BIRTHDAY_INVALID` | An invalid age was specified, must be between 0 and 150 years. |

### `BOOST_*` (2)

| Error | Description |
| --- | --- |
| `BOOST_NOT_MODIFIED` | You're already boosting the specified channel. |
| `BOOST_PEER_INVALID` | The specified `boost_peer` is invalid. |

### `BOOSTS_*` (2)

| Error | Description |
| --- | --- |
| `BOOSTS_EMPTY` | No boost slots were specified. |
| `BOOSTS_REQUIRED` | The specified channel must first be boosted by its users in order to perform this action. |

### `CHATLINK_*` (2)

| Error | Description |
| --- | --- |
| `CHATLINK_SLUG_EMPTY` | The specified slug is empty. |
| `CHATLINK_SLUG_EXPIRED` | The specified business chat link has expired. |

### `COLLECTIBLE_*` (2)

| Error | Description |
| --- | --- |
| `COLLECTIBLE_INVALID` | The specified collectible is invalid. |
| `COLLECTIBLE_NOT_FOUND` | The specified collectible could not be found. |

### `EFFECT_*` (2)

| Error | Description |
| --- | --- |
| `EFFECT_CHAT_INVALID` | Message effects can only be used in private 1-on-1 chats, but the caller tried to send a message with an effect to a group or channel. |
| `EFFECT_ID_INVALID` | The specified effect ID is invalid. |

### `FOLDER_*` (2)

| Error | Description |
| --- | --- |
| `FOLDER_ID_EMPTY` | An empty folder ID was specified. |
| `FOLDER_ID_INVALID` | Invalid folder ID. |

### `FROM_*` (2)

| Error | Description |
| --- | --- |
| `FROM_MESSAGE_BOT_DISABLED` | Bots can't use fromMessage min constructors. |
| `FROM_PEER_INVALID` | The specified from_id is invalid. |

### `GIF_*` (2)

| Error | Description |
| --- | --- |
| `GIF_CONTENT_TYPE_INVALID` | GIF content-type invalid. |
| `GIF_ID_INVALID` | The provided GIF ID is invalid. |

### `HASH_*` (2)

| Error | Description |
| --- | --- |
| `HASH_INVALID` | The provided hash is invalid. |
| `HASH_SIZE_INVALID` | The size of the specified secureValueError.hash is invalid. |

### `ID_*` (2)

| Error | Description |
| --- | --- |
| `ID_EXPIRED` | The passed prepared inline message ID has expired. |
| `ID_INVALID` | The passed ID is invalid. |

### `INVOICE_*` (2)

| Error | Description |
| --- | --- |
| `INVOICE_INVALID` | The specified invoice is invalid. |
| `INVOICE_PAYLOAD_INVALID` | The specified invoice payload is invalid. |

### `LIMIT_*` (2)

| Error | Description |
| --- | --- |
| `LIMIT_INVALID` | The provided limit is invalid. |
| `LIMIT_PER_POST_INVALID` | The specified reactions_limit value is invalid. |

### `MANAGER_*` (2)

| Error | Description |
| --- | --- |
| `MANAGER_INVALID` | The specified manager bot is invalid. |
| `MANAGER_PERMISSION_MISSING` | The specified manager bot does not have the user.`bot_can_manage_bots` flag set. |

### `NOT_*` (2)

| Error | Description |
| --- | --- |
| `NOT_ELIGIBLE` | The current user is not eligible to join the Peer-to-Peer Login Program. |
| `NOT_JOINED` | The current user hasn't joined the Peer-to-Peer Login Program. |

### `OFFSET_*` (2)

| Error | Description |
| --- | --- |
| `OFFSET_INVALID` | The provided offset is invalid. |
| `OFFSET_PEER_ID_INVALID` | The provided offset peer is invalid. |

### `PERSISTENT_*` (2)

| Error | Description |
| --- | --- |
| `PERSISTENT_TIMESTAMP_EMPTY` | Persistent timestamp empty. |
| `PERSISTENT_TIMESTAMP_INVALID` | Persistent timestamp invalid. |

### `PREMIUM_*` (2)

| Error | Description |
| --- | --- |
| `PREMIUM_ACCOUNT_REQUIRED` | A premium account is required to execute this action. |
| `PREMIUM_PURPOSE_INVALID` | The specified InputStorePaymentPurpose is invalid. |

### `QUICK_*` (2)

| Error | Description |
| --- | --- |
| `QUICK_REPLIES_BOT_NOT_ALLOWED` | Quick replies cannot be used by bots. |
| `QUICK_REPLIES_TOO_MUCH` | A maximum of appConfig.`quick_replies_limit` shortcuts may be created, the limit was reached. |

### `REACTION_*` (2)

| Error | Description |
| --- | --- |
| `REACTION_EMPTY` | Empty reaction provided. |
| `REACTION_INVALID` | The specified reaction is invalid. |

### `REACTIONS_*` (2)

| Error | Description |
| --- | --- |
| `REACTIONS_COUNT_INVALID` | The specified number of reactions is invalid. |
| `REACTIONS_TOO_MANY` | The message already has exactly `reactions_uniq_max` reaction emojis, you can't react with a new emoji, see the docs for more info ». |

### `REQUEST_*` (2)

| Error | Description |
| --- | --- |
| `REQUEST_MSG_EXPIRED` | The request specified in request_msg_id has already expired. |
| `REQUEST_TOKEN_INVALID` | The master DC did not accept the `request_token` from the CDN DC. Continue downloading the file from the master DC using upload.getFile. |

### `RESELL_*` (2)

| Error | Description |
| --- | --- |
| `RESELL_STARS_TOO_FEW` | The offered price is too low. |
| `RESELL_STARS_TOO_MUCH` | The offered price is too high. |

### `RINGTONE_*` (2)

| Error | Description |
| --- | --- |
| `RINGTONE_INVALID` | The specified ringtone is invalid. |
| `RINGTONE_MIME_INVALID` | The MIME type for the ringtone is invalid. |

### `SEARCH_*` (2)

| Error | Description |
| --- | --- |
| `SEARCH_QUERY_EMPTY` | The search query is empty. |
| `SEARCH_WITH_LINK_NOT_SUPPORTED` | You cannot provide a search query and an invite link at the same time. |

### `SHORT_*` (2)

| Error | Description |
| --- | --- |
| `SHORT_NAME_INVALID` | The specified short name is invalid. |
| `SHORT_NAME_OCCUPIED` | The specified short name is already in use. |

### `STICKERS_*` (2)

| Error | Description |
| --- | --- |
| `STICKERS_EMPTY` | No sticker provided. |
| `STICKERS_TOO_MUCH` | There are too many stickers in this stickerpack, you can't add any more. |

### `STICKERSET_*` (2)

| Error | Description |
| --- | --- |
| `STICKERSET_INVALID` | The provided sticker set is invalid. |
| `STICKERSET_NOT_MODIFIED` | The passed stickerset information is equal to the current information. |

### `STORIES_*` (2)

| Error | Description |
| --- | --- |
| `STORIES_NEVER_CREATED` | This peer hasn't ever posted any stories. |
| `STORIES_TOO_MUCH` | You have hit the maximum active stories limit as specified by the `story_expiring_limit_*` client configuration parameters: you should buy a Premium subscription, delete an active story, or wait for the oldest story to expire. |

### `SUGGESTED_*` (2)

| Error | Description |
| --- | --- |
| `SUGGESTED_POST_AMOUNT_INVALID` | The specified price for the suggested post is invalid. |
| `SUGGESTED_POST_PEER_INVALID` | You cannot send suggested posts to non-monoforum peers. |

### `SWITCH_*` (2)

| Error | Description |
| --- | --- |
| `SWITCH_PM_TEXT_EMPTY` | The switch_pm.text field was empty. |
| `SWITCH_WEBVIEW_URL_INVALID` | The URL specified in switch_webview.url is invalid! |

### `TAKEOUT_*` (2)

| Error | Description |
| --- | --- |
| `TAKEOUT_INVALID` | The specified takeout ID is invalid. |
| `TAKEOUT_REQUIRED` | A takeout session needs to be initialized first, see here » for more info. |

### `TEMP_*` (2)

| Error | Description |
| --- | --- |
| `TEMP_AUTH_KEY_ALREADY_BOUND` | The passed temporary key is already bound to another **perm_auth_key_id**. |
| `TEMP_AUTH_KEY_EMPTY` | No temporary auth key provided. |

### `TMP_*` (2)

| Error | Description |
| --- | --- |
| `TMP_PASSWORD_DISABLED` | The temporary password is disabled. |
| `TMP_PASSWORD_INVALID` | The passed tmp_password is invalid. |

### `TO_*` (2)

| Error | Description |
| --- | --- |
| `TO_ID_INVALID` | The specified `to_id` of the passed inputInvoiceStarGiftResale or inputInvoiceStarGiftTransfer is invalid. |
| `TO_LANG_INVALID` | The specified destination language is invalid. |

### `URL_*` (2)

| Error | Description |
| --- | --- |
| `URL_EXPIRED` | The specified OAuth request has expired. |
| `URL_INVALID` | Invalid URL provided. |

### `USERS_*` (2)

| Error | Description |
| --- | --- |
| `USERS_TOO_FEW` | Not enough users (to create a chat, for example). |
| `USERS_TOO_MUCH` | The maximum number of users has been exceeded (to create a chat, for example). |

### `ABOUT_*` (1)

| Error | Description |
| --- | --- |
| `ABOUT_TOO_LONG` | About string too long. |

### `AD_*` (1)

| Error | Description |
| --- | --- |
| `AD_EXPIRED` | The ad has expired (too old or not found). |

### `ADDRESS_*` (1)

| Error | Description |
| --- | --- |
| `ADDRESS_INVALID` | The specified geopoint address is invalid. |

### `ADMINS_*` (1)

| Error | Description |
| --- | --- |
| `ADMINS_TOO_MUCH` | There are too many admins. |

### `AI_*` (1)

| Error | Description |
| --- | --- |
| `AI_COMPOSE_TASK_MISSING` | No AI task was specified. The caller must provide at least one of: proofread, translate (with a target language), tone, or emojify. |

### `ALBUM_*` (1)

| Error | Description |
| --- | --- |
| `ALBUM_PHOTOS_TOO_MANY` | You have uploaded too many profile photos, delete some before retrying. |

### `ARTICLE_*` (1)

| Error | Description |
| --- | --- |
| `ARTICLE_TITLE_EMPTY` | The title of the article is empty. |

### `AUTOARCHIVE_*` (1)

| Error | Description |
| --- | --- |
| `AUTOARCHIVE_NOT_AVAILABLE` | The autoarchive setting is not available at this time: please check the value of the autoarchive_setting_available field in client config » before calling this method. |

### `BALANCE_*` (1)

| Error | Description |
| --- | --- |
| `BALANCE_TOO_LOW` | The transaction cannot be completed because the current Telegram Stars balance is too low. |

### `BANK_*` (1)

| Error | Description |
| --- | --- |
| `BANK_CARD_NUMBER_INVALID` | The specified card number is invalid. |

### `BANNED_*` (1)

| Error | Description |
| --- | --- |
| `BANNED_RIGHTS_INVALID` | You provided some invalid flags in the banned rights. |

### `BOTS_*` (1)

| Error | Description |
| --- | --- |
| `BOTS_TOO_MUCH` | There are too many bots in this chat/channel. |

### `CDN_*` (1)

| Error | Description |
| --- | --- |
| `CDN_METHOD_INVALID` | You can't call this method in a CDN DC. |

### `CHATLINKS_*` (1)

| Error | Description |
| --- | --- |
| `CHATLINKS_TOO_MUCH` | Too many business chat links were created, please delete some older links. |

### `CHATLIST_*` (1)

| Error | Description |
| --- | --- |
| `CHATLIST_EXCLUDE_INVALID` | The specified `exclude_peers` are invalid. |

### `CHATLISTS_*` (1)

| Error | Description |
| --- | --- |
| `CHATLISTS_TOO_MUCH` | You have created too many folder links, hitting the `chatlist_invites_limit_default`/`chatlist_invites_limit_premium` limits ». |

### `COLLECTION_*` (1)

| Error | Description |
| --- | --- |
| `COLLECTION_ID_INVALID` | The specified collection ID is invalid. |

### `COLOR_*` (1)

| Error | Description |
| --- | --- |
| `COLOR_INVALID` | The specified color palette ID was invalid. |

### `CREATE_*` (1)

| Error | Description |
| --- | --- |
| `CREATE_CALL_FAILED` | An error occurred while creating the call. |

### `CREDENTIAL_*` (1)

| Error | Description |
| --- | --- |
| `CREDENTIAL_INVALID` | The specified credential is invalid. |

### `CURRENCY_*` (1)

| Error | Description |
| --- | --- |
| `CURRENCY_TOTAL_AMOUNT_INVALID` | The total amount of all prices is invalid. |

### `CUSTOM_*` (1)

| Error | Description |
| --- | --- |
| `CUSTOM_REACTIONS_TOO_MANY` | Too many custom reactions were specified. |

### `DATE_*` (1)

| Error | Description |
| --- | --- |
| `DATE_EMPTY` | Date empty. |

### `DC_*` (1)

| Error | Description |
| --- | --- |
| `DC_ID_INVALID` | The provided DC ID is invalid. |

### `DH_*` (1)

| Error | Description |
| --- | --- |
| `DH_G_A_INVALID` | g_a invalid. |

### `DOCUMENT_*` (1)

| Error | Description |
| --- | --- |
| `DOCUMENT_INVALID` | The specified document is invalid. |

### `ENCRYPTED_*` (1)

| Error | Description |
| --- | --- |
| `ENCRYPTED_MESSAGE_INVALID` | Encrypted message invalid. |

### `ENTITIES_*` (1)

| Error | Description |
| --- | --- |
| `ENTITIES_TOO_LONG` | You provided too many styled message entities. |

### `ERROR_*` (1)

| Error | Description |
| --- | --- |
| `ERROR_TEXT_EMPTY` | The provided error message is empty. |

### `EXPIRE_*` (1)

| Error | Description |
| --- | --- |
| `EXPIRE_DATE_INVALID` | The specified expiration date is invalid. |

### `EXPIRES_*` (1)

| Error | Description |
| --- | --- |
| `EXPIRES_AT_INVALID` | The specified `expires_at` timestamp is invalid. |

### `EXPORT_*` (1)

| Error | Description |
| --- | --- |
| `EXPORT_CARD_INVALID` | Provided card is invalid. |

### `EXTERNAL_*` (1)

| Error | Description |
| --- | --- |
| `EXTERNAL_URL_INVALID` | External URL invalid. |

### `FIRSTNAME_*` (1)

| Error | Description |
| --- | --- |
| `FIRSTNAME_INVALID` | The first name is invalid. |

### `FORUM_*` (1)

| Error | Description |
| --- | --- |
| `FORUM_ENABLED` | You can't execute the specified action because the group is a forum, disable forum functionality to continue. |

### `FRESH_*` (1)

| Error | Description |
| --- | --- |
| `FRESH_CHANGE_ADMINS_FORBIDDEN` | You were just elected admin, you can't add or modify other admins yet. |

### `FROZEN_*` (1)

| Error | Description |
| --- | --- |
| `FROZEN_PARTICIPANT_MISSING` | The current account is frozen, and cannot access the specified peer. |

### `GAME_*` (1)

| Error | Description |
| --- | --- |
| `GAME_BOT_INVALID` | Bots can't send another bot's game. |

### `GENERAL_*` (1)

| Error | Description |
| --- | --- |
| `GENERAL_MODIFY_ICON_FORBIDDEN` | You can't modify the icon of the "General" topic. |

### `GEO_*` (1)

| Error | Description |
| --- | --- |
| `GEO_POINT_INVALID` | Invalid geoposition provided. |

### `GROUPED_*` (1)

| Error | Description |
| --- | --- |
| `GROUPED_MEDIA_INVALID` | Invalid grouped media. |

### `HASHTAG_*` (1)

| Error | Description |
| --- | --- |
| `HASHTAG_INVALID` | The specified hashtag is invalid. |

### `HIDE_*` (1)

| Error | Description |
| --- | --- |
| `HIDE_REQUESTER_MISSING` | The join request was missing or was already handled. |

### `IMAGE_*` (1)

| Error | Description |
| --- | --- |
| `IMAGE_PROCESS_FAILED` | Failure while processing image. |

### `INLINE_*` (1)

| Error | Description |
| --- | --- |
| `INLINE_RESULT_EXPIRED` | The inline query expired. |

### `INVITES_*` (1)

| Error | Description |
| --- | --- |
| `INVITES_TOO_MUCH` | The maximum number of per-folder invites specified by the `chatlist_invites_limit_default`/`chatlist_invites_limit_premium` client configuration parameters » was reached. |

### `JOIN_*` (1)

| Error | Description |
| --- | --- |
| `JOIN_AS_PEER_INVALID` | The specified peer cannot be used to join a group call. |

### `LANGUAGE_*` (1)

| Error | Description |
| --- | --- |
| `LANGUAGE_INVALID` | The specified lang_code is invalid. |

### `LASTNAME_*` (1)

| Error | Description |
| --- | --- |
| `LASTNAME_INVALID` | The last name is invalid. |

### `LINK_*` (1)

| Error | Description |
| --- | --- |
| `LINK_NOT_MODIFIED` | Discussion link not modified. |

### `LOCATION_*` (1)

| Error | Description |
| --- | --- |
| `LOCATION_INVALID` | The provided location is invalid. |

### `MD5_*` (1)

| Error | Description |
| --- | --- |
| `MD5_CHECKSUM_INVALID` | The MD5 checksums do not match. |

### `METHOD_*` (1)

| Error | Description |
| --- | --- |
| `METHOD_INVALID` | The specified method is invalid. |

### `MIN_*` (1)

| Error | Description |
| --- | --- |
| `MIN_DATE_INVALID` | The specified minimum date is invalid. |

### `MONTH_*` (1)

| Error | Description |
| --- | --- |
| `MONTH_INVALID` | The number of months specified in inputInvoicePremiumGiftStars.months is invalid. |

### `MULTI_*` (1)

| Error | Description |
| --- | --- |
| `MULTI_MEDIA_TOO_LONG` | Too many media files for album. |

### `NAME_*` (1)

| Error | Description |
| --- | --- |
| `NAME_INVALID` | The specified bot name is invalid. |

### `NEED_*` (1)

| Error | Description |
| --- | --- |
| `NEED_ACTION_MISSING` | The caller didn't specify a valid action (either save or suggest) for the contact profile photo upload. |

### `NEXT_*` (1)

| Error | Description |
| --- | --- |
| `NEXT_OFFSET_INVALID` | The specified offset is longer than 64 bytes. |

### `NO_*` (1)

| Error | Description |
| --- | --- |
| `NO_PAYMENT_NEEDED` | The upgrade/transfer of the specified gift was already paid for or is free. |

### `NOGENERAL_*` (1)

| Error | Description |
| --- | --- |
| `NOGENERAL_HIDE_FORBIDDEN` | Only the "General" topic with `id=1` can be hidden. |

### `OPTION_*` (1)

| Error | Description |
| --- | --- |
| `OPTION_INVALID` | Invalid option selected. |

### `OPTIONS_*` (1)

| Error | Description |
| --- | --- |
| `OPTIONS_TOO_MUCH` | Too many options provided. |

### `ORDER_*` (1)

| Error | Description |
| --- | --- |
| `ORDER_INVALID` | The specified username order is invalid. |

### `PARENT_*` (1)

| Error | Description |
| --- | --- |
| `PARENT_PEER_INVALID` | The specified `parent_peer` is invalid. |

### `PARTICIPANTS_*` (1)

| Error | Description |
| --- | --- |
| `PARTICIPANTS_TOO_FEW` | Not enough participants. |

### `PASSKEY_*` (1)

| Error | Description |
| --- | --- |
| `PASSKEY_ORIGIN_MISMATCH` | Third-party clients currently don't support passkeys even when changing the origin. |

### `PEERS_*` (1)

| Error | Description |
| --- | --- |
| `PEERS_LIST_EMPTY` | The specified list of peers is empty. |

### `PIN_*` (1)

| Error | Description |
| --- | --- |
| `PIN_RESTRICTED` | You can't pin messages. |

### `PRICING_*` (1)

| Error | Description |
| --- | --- |
| `PRICING_CHAT_INVALID` | The pricing for the subscription is invalid, the maximum price is specified in the `stars_subscription_amount_max` config key ». |

### `PURPOSE_*` (1)

| Error | Description |
| --- | --- |
| `PURPOSE_INVALID` | The specified payment purpose is invalid. |

### `QUOTE_*` (1)

| Error | Description |
| --- | --- |
| `QUOTE_TEXT_INVALID` | The specified `reply_to`.`quote_text` field is invalid. |

### `RAISE_*` (1)

| Error | Description |
| --- | --- |
| `RAISE_HAND_FORBIDDEN` | You cannot raise your hand. |

### `RANGES_*` (1)

| Error | Description |
| --- | --- |
| `RANGES_INVALID` | Invalid range provided. |

### `RECEIPT_*` (1)

| Error | Description |
| --- | --- |
| `RECEIPT_EMPTY` | The specified receipt is empty. |

### `RESET_*` (1)

| Error | Description |
| --- | --- |
| `RESET_REQUEST_MISSING` | No password reset is in progress. |

### `RESULTS_*` (1)

| Error | Description |
| --- | --- |
| `RESULTS_TOO_MUCH` | Too many results were provided. |

### `REVOTE_*` (1)

| Error | Description |
| --- | --- |
| `REVOTE_NOT_ALLOWED` | You cannot change your vote. |

### `RIGHTS_*` (1)

| Error | Description |
| --- | --- |
| `RIGHTS_NOT_MODIFIED` | The new admin rights are equal to the old rights, no change was made. |

### `RSA_*` (1)

| Error | Description |
| --- | --- |
| `RSA_DECRYPT_FAILED` | Internal RSA decryption failed. |

### `SAVED_*` (1)

| Error | Description |
| --- | --- |
| `SAVED_ID_EMPTY` | The passed inputSavedStarGiftChat.saved_id is empty. |

### `SCORE_*` (1)

| Error | Description |
| --- | --- |
| `SCORE_INVALID` | The specified game score is invalid. |

### `SECONDS_*` (1)

| Error | Description |
| --- | --- |
| `SECONDS_INVALID` | Invalid duration provided. |

### `SECURE_*` (1)

| Error | Description |
| --- | --- |
| `SECURE_SECRET_REQUIRED` | A secure secret is required. |

### `SELF_*` (1)

| Error | Description |
| --- | --- |
| `SELF_DELETE_RESTRICTED` | Business bots can't delete messages just for the user, `revoke` **must** be set. |

### `SESSION_*` (1)

| Error | Description |
| --- | --- |
| `SESSION_TOO_FRESH_%d` | This session was created less than 24 hours ago, try again in %d seconds. |

### `SETTINGS_*` (1)

| Error | Description |
| --- | --- |
| `SETTINGS_INVALID` | Invalid settings were provided. |

### `SHA256_*` (1)

| Error | Description |
| --- | --- |
| `SHA256_HASH_INVALID` | The provided SHA256 hash is invalid. |

### `SHORTCUT_*` (1)

| Error | Description |
| --- | --- |
| `SHORTCUT_INVALID` | The specified shortcut is invalid. |

### `SLOTS_*` (1)

| Error | Description |
| --- | --- |
| `SLOTS_EMPTY` | The specified slot list is empty. |

### `SLOWMODE_*` (1)

| Error | Description |
| --- | --- |
| `SLOWMODE_MULTI_MSGS_DISABLED` | Slowmode is enabled, you cannot forward multiple messages to this group. |

### `SLUG_*` (1)

| Error | Description |
| --- | --- |
| `SLUG_INVALID` | The specified invoice slug is invalid. |

### `SMS_*` (1)

| Error | Description |
| --- | --- |
| `SMS_CODE_CREATE_FAILED` | An error occurred while creating the SMS code. |

### `SMSJOB_*` (1)

| Error | Description |
| --- | --- |
| `SMSJOB_ID_INVALID` | The specified job ID is invalid. |

### `STICKERPACK_*` (1)

| Error | Description |
| --- | --- |
| `STICKERPACK_STICKERS_TOO_MUCH` | There are too many stickers in this stickerpack, you can't add any more. |

### `TASK_*` (1)

| Error | Description |
| --- | --- |
| `TASK_ALREADY_EXISTS` | An email reset was already requested. |

### `TERMS_*` (1)

| Error | Description |
| --- | --- |
| `TERMS_URL_INVALID` | The specified invoice.`terms_url` is invalid. |

### `TEXTDRAFT_*` (1)

| Error | Description |
| --- | --- |
| `TEXTDRAFT_PEER_INVALID` | sendMessageTextDraftAction can only be used in private 1-on-1 chats. |

### `TIMEZONE_*` (1)

| Error | Description |
| --- | --- |
| `TIMEZONE_INVALID` | The specified timezone does not exist. |

### `TITLE_*` (1)

| Error | Description |
| --- | --- |
| `TITLE_INVALID` | The specified stickerpack title is invalid. |

### `TOPICS_*` (1)

| Error | Description |
| --- | --- |
| `TOPICS_EMPTY` | You specified no topic IDs. |

### `TRANSACTION_*` (1)

| Error | Description |
| --- | --- |
| `TRANSACTION_ID_INVALID` | The specified transaction ID is invalid. |

### `TRANSCRIPTION_*` (1)

| Error | Description |
| --- | --- |
| `TRANSCRIPTION_FAILED` | Audio transcription failed. |

### `TRANSLATE_*` (1)

| Error | Description |
| --- | --- |
| `TRANSLATE_REQ_QUOTA_EXCEEDED` | Translation is currently unavailable due to a temporary server-side lack of resources. |

### `TYPES_*` (1)

| Error | Description |
| --- | --- |
| `TYPES_EMPTY` | No top peer type was provided. |

### `UNSUPPORTED_*` (1)

| Error | Description |
| --- | --- |
| `UNSUPPORTED` | `require_payment` cannot be *set* by users, only by monoforums: users must instead use the inputPrivacyKeyNoPaidMessages privacy setting to remove a previously added exemption. |

### `UNTIL_*` (1)

| Error | Description |
| --- | --- |
| `UNTIL_DATE_INVALID` | Invalid until date provided. |

### `USAGE_*` (1)

| Error | Description |
| --- | --- |
| `USAGE_LIMIT_INVALID` | The specified usage limit is invalid. |

### `USERNAMES_*` (1)

| Error | Description |
| --- | --- |
| `USERNAMES_ACTIVE_TOO_MUCH` | The maximum number of active usernames was reached. |

### `USERPIC_*` (1)

| Error | Description |
| --- | --- |
| `USERPIC_UPLOAD_REQUIRED` | You must have a profile picture to publish your geolocation. |

### `VENUE_*` (1)

| Error | Description |
| --- | --- |
| `VENUE_ID_INVALID` | The specified venue ID is invalid. |

### `VOICE_*` (1)

| Error | Description |
| --- | --- |
| `VOICE_MESSAGES_FORBIDDEN` | This user's privacy settings forbid you from sending voice messages. |

### `WC_*` (1)

| Error | Description |
| --- | --- |
| `WC_CONVERT_URL_INVALID` | WC convert URL invalid. |

### `WEBAPP_*` (1)

| Error | Description |
| --- | --- |
| `WEBAPP_REQ_ID_INVALID` | The specified webapp_req_id is invalid. |

### `WEBAUTH_*` (1)

| Error | Description |
| --- | --- |
| `WEBAUTH_TOKEN_EXPIRED` | The specified auth token has expired. |

### `YOU_*` (1)

| Error | Description |
| --- | --- |
| `YOU_BLOCKED_USER` | You blocked this user. |

## 4. Errors documented outside the JSON DB (prose pages)

These strings appear in the conceptual docs and must also be handled, even where the JSON DB does not attach them to a method:

- **`API_ID_PUBLISHED_FLOOD`** — Using the sample api_id shipped with open-source Telegram code. Obtain your own api_id.
- **`AUTH_KEY_DUPLICATED`** — 406. Two parallel connections on one non-media DC session. Session already invalidated server-side: regenerate the auth key and log in again. Only up to `config.tmp_sessions` parallel main sessions are allowed; media-DC file sessions are always exempt.
- **`ENCRYPTED_MESSAGE_INVALID`** — Returned by `auth.bindTempAuthKey`. If the permanent key is >60s old, drop both keys, recreate and rebind; otherwise just retry the bind.
- **`MSG_WAIT_TIMEOUT / MSG_WAIT_FAILED`** — invokeAfterMsg / invokeAfterMsgs chain failure — resend the chained queries as described in `api/invoking`.
- **`RANDOM_ID_DUPLICATE`** — 500. A `random_id` was reused. Note that for most send methods the server instead silently returns the previously generated result; `messages.requestEncryption` always errors.
- **`SCHEDULE_STATUS_PRIVATE`** — Scheduling with the magic `0x7FFFFFFE` 'when online' timestamp against a user whose last-seen is hidden.
- **`USER_BOT_TO_BOT_DISABLED`** — Bot-to-bot private messaging requires both bots to enable Bot-to-Bot Communication Mode.
- **`BUSINESS_CONNECTION_INVALID / BUSINESS_CONNECTION_NOT_ALLOWED / BOT_ACCESS_FORBIDDEN`** — Business-connection method errors (see `api/bots/connected-business-bots`).
- **`CONF_WRITE_CHAIN_INVALID*`** — E2E conference blockchain race: refetch the latest block with `phone.getGroupCallChainBlocks` and rebuild the join/removal block.
- **`GROUPCALL_JOIN_MISSING / GROUPCALL_FORBIDDEN`** — Stream-mode chunk download lost the join: rejoin with `phone.joinGroupCall`.
- **`TIME_TOO_BIG`** — Group-call stream chunk not yet available: sleep 100 ms (ignore the FLOOD_WAIT value) and retry the same chunk.
- **`TAKEOUT_FILE_EMPTY`** — `inputTakeoutFileLocation` has no extra data to export — treat the file as empty, not as an error.
- **`LOCATION_INVALID / VERSION_INVALID / LOCATION_NOT_AVAILABLE`** — During takeout downloads: the file is gone, skip it.
- **`STORY_LIVE_ALREADY_%d`** — A live story is already active for this peer; the ID is in the suffix.
- **`URL_EXPIRED`** — `messages.requestUrlAuth`/`messages.acceptUrlAuth` OAuth link expired.
- **`AD_EXPIRED / PREMIUM_ACCOUNT_REQUIRED`** — `messages.reportSponsoredMessage` outcomes.
- **`INVITE_REQUEST_SENT`** — `messages.importChatInvite` on a join-request link — this is a *success* signal, not a failure.
- **`CHAT_FORWARDS_RESTRICTED`** — Content protection is on for the source chat; also a signal to refresh the peer DB entry.
- **`CHAT_GUEST_SEND_FORBIDDEN`** — Discussion group requires joining first; also a signal to refresh the peer DB entry.
- **`USER_NOT_PARTICIPANT`** — On `channels.leaveChannel`: also a signal to refresh the peer DB entry.
- **`USERNAME_NOT_MODIFIED`** — Treated as success by the username-toggle/reorder flows.
- **`CHAT_NOT_MODIFIED`** — On `messages.setChatAvailableReactions`, the only error that does NOT require invalidating the full-info cache.
- **`SEND_AS_PEER_INVALID`** — Refresh full info for the destination to re-read the allowed `send_as` peers.
- **`BOT_CREATE_LIMIT_EXCEEDED / MANAGER_PERMISSION_MISSING / USERNAME_OCCUPIED`** — `bots.createBot` failures.
- **`STARREF_AWAITING_END / STARREF_EXPIRED`** — Affiliate-program lifecycle errors.
- **`TODO_ITEM_DUPLICATE`** — `messages.appendTodoList` with an already-used item ID.
- **`CHATLINKS_TOO_MUCH`** — business chat-link limit hit.
- **`QUICK_REPLIES_TOO_MUCH / REPLY_MESSAGES_TOO_MUCH`** — quick-reply shortcut limits.
- **`BIRTHDAY_INVALID / BIRTHDAY_ALREADY`** — birthday set/suggest failures.
- **`PASSKEY_CREDENTIAL_NOT_FOUND`** — `auth.finishPasskeyLogin` — the passkey was removed server-side.
- **`BOOST_NOT_MODIFIED`** — `premium.applyBoost` with the same slots.
- **`PHONE_NOT_OCCUPIED`** — `contacts.resolvePhone`: no Telegram account, or privacy blocks the lookup.
- **`BUSINESS_ADDRESS_ACTIVE`** — `contacts.getLocated` while a Business Location is set — change it via `account.updateBusinessLocation`.
- **`BOT_RESPONSE_TIMEOUT`** — Inline/callback query bot timeout — render nothing, do not surface as an error.
- **`FILE_PARTS_INVALID / FILE_PART_INVALID / FILE_PART_TOO_BIG / FILE_PART_EMPTY / FILE_PART_SIZE_INVALID / FILE_PART_SIZE_CHANGED / FILE_PART_%d_MISSING / MD5_CHECKSUM_INVALID`** — Upload-part errors — see `api/files`.
- **`OFFSET_INVALID / LIMIT_INVALID / FILE_ID_INVALID`** — Download-parameter errors — offset/limit alignment rules in `api/files`.

## 5. Implementation notes for tlgr

1. **Normalise `%d`-parameterised errors.** Build the lookup key by replacing the trailing (or embedded) integer with `%d`; `FLOOD_WAIT_37` → `FLOOD_WAIT_%d`, param `37`. Also handle the infix forms `FILE_PART_%d_MISSING`, `FILE_REFERENCE_%d_EXPIRED`, `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_%dMIN`, `AUTH_RESTART_%d`, `ALLOW_PAYMENT_REQUIRED_%d`, `STORY_LIVE_ALREADY_%d`.
2. **Ship the JSON DB.** Vendor `errors.json` (it is versioned by `layer`) and emit its localized `descriptions` string, not the raw error type, in human output; emit both in `--json` output.
3. **Never print a 406 message.** Suppress it and surface the follow-up `updateServiceNotification` popup text instead. If tlgr is not running an update loop for the command, do a short (~2 s) wait for the notification before falling back.
4. **Do local checks first.** The docs explicitly ask clients to prevent illegal operations locally from cached peer/rights info, and to still handle the server error because of unavoidable races (stale cache, update in flight, new server-side check in a later layer).
5. **Some errors are cache-invalidation signals**, not just failures: `CHAT_FORWARDS_RESTRICTED`, `CHAT_GUEST_SEND_FORBIDDEN`, `USER_NOT_PARTICIPANT`, `SEND_AS_PEER_INVALID`, `CHANNEL_PRIVATE`, `CHANNEL_PUBLIC_GROUP_NA`, `USERNAME_NOT_MODIFIED`. Refresh the peer / full-info DB entry when they arrive.
6. **`FROZEN_METHOD_INVALID` (420) and `FROZEN_PARTICIPANT_MISSING` (400)** must not be retried like flood waits — they mean the account is read-only until an appeal succeeds.
7. **`STATS_MIGRATE_%d`** — statistics methods (`stats.*`) must be sent to `channelFull.stats_dc`, not the home DC. Telethon does not route this automatically for you in every version; tlgr should resolve `stats_dc` up front.
