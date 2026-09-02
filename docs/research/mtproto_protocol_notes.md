# MTProto / Telegram API — production-engineering reference for the tlgr daemon

Scope: what the protocol requires, what the installed Telethon (1.44.0, layer 227,
`/Users/p4/Projects/tlgr/.venv/lib/python3.12/site-packages/telethon`, hereafter `telethon/`)
actually implements, what the official clients do differently (tdesktop layer 229 at
`refs/tdesktop/Telegram/SourceFiles`, Telegram-Android), and what tlgr's daemon
(`/Users/p4/Projects/tlgr/tlgr/core/client.py`, `/Users/p4/Projects/tlgr/tlgr/daemon/server.py`)
must change. Doc citations are relative to
`<scratchpad>/docs/`.

Inventory note: `analysis/mtproto_methods.json` is the layer-225 doc mirror (757 methods);
`analysis/telethon_raw_functions.json` lists only *namespaced* requests (802) and omits the
top-level helpers (`InvokeWithLayerRequest`, `InitConnectionRequest`, `PingDelayDisconnectRequest`,
`GetFutureSaltsRequest`, `DestroySessionRequest`, `RpcDropAnswerRequest`, `DestroyAuthKeyRequest`,
`InvokeWithTakeoutRequest`, `InvokeWithMessagesRangeRequest`, `InvokeAfterMsg(s)Request`,
`InvokeWithBusinessConnectionRequest`) which nevertheless exist in
`telethon/tl/functions/__init__.py`. Every method named below was checked against
`mtproto_methods.json` and/or tdesktop `mtproto/scheme/api.tl` (`// LAYER 229`); the four
layer-228/229 additions I mention (`messages.getPersonalChannelHistory`, `stats.getPollStats`,
`aicompose.createTone`, `messages.setBotGuestChatResult`) are in api.tl and in Telethon 227's
inventory but not in the layer-225 doc mirror. Nothing in this document is
"newer-than-telethon-227" except where explicitly flagged.

---

## 1. Protocol summary (MTProto 2.0)

### 1.1 Authorization key (DH handshake) — `docs/mtproto/mtproto__auth_key.md`, `mtproto__samples-auth_key.md`
- Plain-text messages (`auth_key_id = 0`, `msg_id`, `length`, body).
  1. `req_pq_multi(nonce)` → `resPQ(nonce, server_nonce, pq, server_public_key_fingerprints)`.
  2. Client factors `pq` (p<q), builds `p_q_inner_data_dc(pq,p,q,nonce,server_nonce,new_nonce,dc)`
     — `dc` = DC id, `+10000` for test DCs, negative for media DCs. Temp-key variant
     `p_q_inner_data_temp_dc(..., expires_in)` is the PFS path.
  3. `RSA_PAD`: data padded to 192 B, byte-reversed, `SHA256(temp_key+data_with_padding)`
     appended, AES-256-IGE with zero IV under a random 32-B `temp_key`, `temp_key XOR SHA256(ct)`
     prefixed → 256 B, must be < RSA modulus (else retry), then textbook RSA.
     `req_DH_params(nonce, server_nonce, p, q, fingerprint, encrypted_data)`.
     Errors: `-404` (restart), `-444` (test/prod DC id mismatch).
  4. `server_DH_params_ok.encrypted_answer` = AES-IGE(`SHA1(answer)+answer+pad`) with
     `tmp_aes_key = SHA1(new_nonce+server_nonce) + SHA1(server_nonce+new_nonce)[:12]`,
     `tmp_aes_iv = SHA1(server_nonce+new_nonce)[12:20] + SHA1(new_nonce+new_nonce) + new_nonce[:4]`
     (Telethon `helpers.generate_key_data_from_nonce`, `telethon/helpers.py:271`).
  5. Client must verify SHA1 prefix, nonces, that `dh_prime` is a safe 2048-bit prime and `g`
     a quadratic residue (rules per g in `mtproto__security_guidelines.md`), and
     `1 < g, g_a, g_b < dh_prime-1`, ideally `2^(2048-64) <= g_a,g_b <= dh_prime-2^(2048-64)`.
  6. `set_client_DH_params(nonce, server_nonce, AES-IGE(SHA1(data)+client_DH_inner_data(retry_id, g_b)+pad))`
     → `dh_gen_ok|retry|fail` with `new_nonce_hash{1,2,3} = SHA1(new_nonce + byte(1|2|3) + auth_key_aux_hash)[4:20]`.
  - `auth_key = g_a^b mod p` (256 B). `auth_key_id` = low 64 bits of SHA1(auth_key);
    `auth_key_aux_hash` = high 64 bits. Initial `server_salt = new_nonce[:8] XOR server_nonce[:8]`.
    `time_offset = server_time - local_time`.
  - Server remembers a handshake response for 10 min; resend identical query if lost.
- **Telethon** (`telethon/network/authenticator.py`): implements all of the above with the
  range checks (lines 143-157) and nonce/hash checks, but **does not verify that `dh_prime` is prime**
  (relies on the fixed well-known prime), always sends `retry_id=0` (TODO at line 163), and
  tries built-in RSA keys then "old" keys (`crypto/rsa.py`). `AuthKey` (`crypto/authkey.py`)
  derives `key_id`/`aux_hash`. Handshake retries: `connection_retries` × `retry_delay`
  (`mtprotosender.py:238-270`).
- Key is generated once per (session file, DC); `_switch_dc` wipes it (`telegrambaseclient.py:770-784`).

### 1.2 Sessions, msg_id, seq_no, containers, acks — `mtproto__description.md`, `mtproto__service_messages.md`, `mtproto__service_messages_about_messages.md`
- **Session** = random 64-bit `session_id` chosen by the client; `(auth_key_id, session_id)` is the
  unit of msg_id/seq/salt/ack state on the server. Server may forget sessions at will and answers
  with `new_session_created(first_msg_id, unique_id, server_salt)` — must be acked, and it means
  there may be a gap in the update stream (official clients call `updates.getDifference` on it:
  tdesktop `api/api_updates.cpp` `Updates::mtpNewSessionCreated`).
  Telethon: `MTProtoState.reset()` picks a **new session_id on every (re)connect**
  (`mtprotostate.py:74-84`, called from `_reconnect` at `mtprotosender.py:375`), and
  `_handle_new_session_created` only stores the salt (`mtprotosender.py:826-835`) — no getDifference.
- **msg_id**: `(unixtime << 32) | (ns << 2)`; client ids ≡ 0 mod 4, server responses ≡ 1, other
  server messages ≡ 3; strictly increasing per session; rejected if >300 s old or >30 s in the
  future (`bad_msg_notification` 16/17/20). Container msg_id > inner msg_ids.
  Telethon `_get_new_msg_id` (`mtprotostate.py:239-252`), `update_time_offset` on codes 16/17 and on
  a first-received bad notification (`mtprotostate.py:206-211`, `mtprotosender.py:778-783`).
- **seq_no**: content-related messages get `2*n+1` and increment `n`; non-content (`msgs_ack`,
  `msg_container`, `gzip_packed`, `msg_copy`) get `2*n`. Codes 32/33 (seqno too low/high) —
  Telethon bumps `_sequence` ±64/−16 instead of a fresh session (`mtprotosender.py:784-790`);
  codes 34/35 = wrong content-relatedness, 48 = bad salt (see `bad_server_salt`), 64 = bad container.
- **Containers**: `msg_container` ≤ 1024 messages (doc), ≤ 8192 ids per `msgs_ack` /
  `msgs_state_req` / `msg_resend_req`. Telethon `MessagePacker` batches everything queued into one
  container with hard caps **100 messages / 1,044,448 bytes**
  (`telethon/tl/core/messagecontainer.py`, `extensions/messagepacker.py`); a single oversize request
  fails with `ValueError('Request payload is too big')`. tdesktop cuts containers at 16 KiB
  (`kCutContainerOnSize`, `mtproto/session_private.cpp:68`). `gzip_packed` is applied to any
  content-related request when it shrinks (`mtprotostate.write_data_as_message`).
- **Acknowledgements**: every content-related server message must be acked with `msgs_ack`
  (batched; stand-alone if >16 pending or idle). Telethon adds every received `msg_id` to
  `_pending_ack` and flushes a `MsgsAck` at the top of the next send-loop iteration
  (`mtprotosender.py:460-464`, `_process_message` line 574); tdesktop waits ≤10 s (`kAckSendWaiting`).
  Server acks client RPCs implicitly via `rpc_result`; `auth.logOut` was historically answered only
  by an ack, which Telethon special-cases (`_handle_ack`, lines 837-859).
- Other service messages Telethon handles: `pong`, `future_salts`, `msg_detailed_info` /
  `msg_new_detailed_info` (just acks the answer id), `msgs_state_req`/`msg_resend_req`
  (answers `msgs_state_info` with all-`0x01` = "unknown"), `msgs_all_info` (ignored),
  `destroy_session_*`, `destroy_auth_key_*` (→ disconnect with `AuthKeyNotFound`).
  `rpc_drop_answer` and `http_wait` exist as requests but are unused.

### 1.3 Server salts — `mtproto__description.md`, `docs/api/api__optimisation.md`
- 64-bit salt in the encrypted header; rotated by the server (docs say every 30 min in one page,
  1 h in another; old salt accepted ~30 min more). Wrong salt → `bad_server_salt(new_server_salt)`
  (code 48): update and resend. `get_future_salts(num≤64)` returns overlapping future salts so
  a client that connects less than hourly can send immediately.
- Telethon: `_handle_bad_server_salt` updates `_state.salt` and re-enqueues the failed messages
  (including container-mates and the last 10 acks); salt from `new_session_created` also applied;
  `GetFutureSaltsRequest` result is returned to the caller but **not stored** (TODO,
  `mtprotosender.py:861-874`). Salt is not persisted in the session file (always starts at 0 and is
  corrected by the first `bad_server_salt`/`new_session_created` — one extra round trip per connect).

### 1.4 PFS temporary keys — `docs/api/api__pfs.md`
- Generate a temp key with `p_q_inner_data_temp_dc(expires_in)`, then bind it to the permanent
  key with `auth.bindTempAuthKey(perm_auth_key_id, nonce, expires_at, encrypted_message)` where the
  encrypted binding message is built from `bind_auth_key_inner` encrypted with the **permanent**
  key; unbound temp keys may only call `auth.bindTempAuthKey`, `help.getConfig`, `help.getNearestDc`.
  After every bind the client must repeat `initConnection`. `ENCRYPTED_MESSAGE_INVALID` on bind:
  if the permanent key is >60 s old drop both keys, recreate, rebind, and re-import authorization
  if it was not the main DC key. Temp key may vanish early (server RAM) → transport `-404`.
  With `tmp_sessions > 1` each parallel main session needs its own temp key.
- tdesktop: `kTemporaryExpiresIn = 86400` (+30 s slack), `kKeyOldEnoughForDestroy = 60 s`,
  binder maps `ENCRYPTED_MESSAGE_INVALID` to "definitely destroyed"
  (`mtproto/session_private.cpp:43-45`, `mtproto/details/mtproto_dc_key_binder.cpp:100-125`).
- **Telethon 1.44 does not implement PFS.** The SQLite schema (v8) has a `tmp_auth_key` column and
  `store_tmp_auth_key_on_disk` (`sessions/sqlite.py:33,161,217`) but nothing in `network/` ever
  creates or binds a temp key. tlgr therefore uses the permanent key directly (allowed; PFS is
  "supported", not mandatory for third-party clients).

### 1.5 Transports, framing, padding, obfuscation — `mtproto__transports.md`, `mtproto__mtproto-transports.md`
- TCP ports 80/443/5222 (`dcOption.port`; honour `this_port_only`, `force_try_ipv6`, `tcpo_only`,
  `media_only`, `cdn`, `static`). Framings:
  - **Abridged**: tag `0xef` once; length/4 in 1 byte (<127) or `0x7f`+3 bytes. Quick-ack: set MSB.
  - **Intermediate**: tag `0xeeeeeeee`; 4-byte LE length.
  - **Padded intermediate**: tag `0xdddddddd`; 4-byte length of payload+0..15 random bytes.
    Telethon's `RandomizedIntermediatePacketCodec` pads 0..3 bytes and strips `len % 4` on read
    (`connection/tcpintermediate.py`) — works because MTProto payloads are 4-aligned.
  - **Full**: `len | seqno | payload | crc32`, no tag; per-connection TCP seqno from 0.
    Telethon default is `ConnectionTcpFull` (`telegrambaseclient.py:250`), which also decodes
    the `-429/-404` transport errors sent as 4-byte negative bodies (`connection/tcpfull.py:33-40`).
  - **Obfuscated2** (`ConnectionTcpObfuscated`, `connection/tcpobfuscated.py`): 64 random bytes
    avoiding `0xef`, `HEAD/POST/GET/...`, `0xeeeeeeee`, `0xdddddddd`, and `[4:8]==0`; encrypt key/IV
    = bytes 8..40/40..56, decrypt = same from the byte-reversed buffer; AES-256-CTR continuous;
    protocol tag at 56..60 (Telethon uses Abridged); bytes 56..64 replaced by their encryption.
    **MTProxy** (`connection/tcpmtproxy.py`): key = SHA256(key + 16-byte secret), 2-byte LE dc id at
    60..62 (negative for media, +10000 test), `dd`-secrets force padded-intermediate; Telethon
    marks it experimental. WebSocket transports require obfuscation (Telethon has none; HTTP
    transport exists in `connection/http.py`).
- Quick ACK tokens: request by setting the MSB of the transport length field; token = first 32 bits
  of the same `SHA256(auth_key[88+x:120+x] + plaintext + padding)` used for `msg_key`, with the MSB
  set (`| 0x80000000`); the server echoes it as a standalone 4-byte packet (byte-swapped for
  Abridged). Means "received and accepted for processing", not "executed". Not used by Telethon.
- Transport errors (negative 4-byte packets): `-404` auth key unknown → Telethon raises
  `AuthKeyNotFound` and **disconnects permanently** (`mtprotosender.py:392-396, 549-552`);
  `-429` transport flood → Telethon disconnects with the error and does **not** reconnect
  (`mtprotosender.py:520-524`); `-444` invalid DC.
- **Encryption of a payload** (`mtproto__description.md`): plaintext = `salt|session_id|msg_id|seq_no|len|body|padding`,
  padding 12..1024 random bytes with total ≡ 0 mod 16; `msg_key = SHA256(auth_key[88+x:120+x] + plaintext+padding)[8:24]`;
  `aes_key/aes_iv` from `SHA256(msg_key + auth_key[x:x+36])` and `SHA256(auth_key[40+x:76+x] + msg_key)`;
  `x = 0` client→server, `8` server→client; AES-256-IGE. Telethon `MTProtoState.encrypt_message_data`
  uses the *minimum* padding (`-(len+12) % 16 + 12` = 12..27 bytes, `mtprotostate.py:136`).
  Decrypt checks (`mtprotostate.py:151-230`): key_id match, msg_key match, session_id match,
  server msg_id odd, duplicate/older msg_id (deque of 500), ±300/30 s time window (skipped for
  `bad_server_salt`/`bad_msg_notification`), and raises `SecurityError` after 10 consecutive
  ignored messages; it does **not** check the salt or seq_no (TODO at line 160). Any decode
  problem makes the receive loop reconnect; `TypeNotFoundError` (unknown constructor) is merely
  logged and the message dropped (`mtprotosender.py:537-541`) — see §2.7.
- Reconnect (`mtprotosender.py:354-421`): on IOError in send/recv loop → `_start_reconnect` →
  new session id, resend all `_pending_state` requests (so a request may execute twice if its reply
  was lost; use `random_id` idempotency, §2.4), then `auto_reconnect_callback` →
  `TelegramClient._handle_auto_reconnect` which only does `get_me()` (the catch-up code after
  `return` at `client/updates.py:674` is dead).

### 1.6 Data centers, migration, exported authorizations — `docs/api/api__datacenter.md`, `api__errors.md`
- `help.getConfig` → `dc_options` (ip/port change often), `this_dc`, `tmp_sessions`, limits.
  `help.getNearestDc` for first login. Redirect errors (303): `PHONE_MIGRATE_X` / `NETWORK_MIGRATE_X`
  during `auth.sendCode`, `USER_MIGRATE_X` if the account was moved, `FILE_MIGRATE_X` for
  `upload.getFile`. Auth keys are per DC; use `auth.exportAuthorization(dc_id)` on the home DC and
  `auth.importAuthorization(id, bytes)` on the other DC (wrapped in `initConnection`).
- Parallel sessions: only **one** main MTProto session per auth key unless `tmp_sessions > 1`;
  a second concurrent main session (same session file from two processes, two daemons, CLI +
  daemon) triggers **`AUTH_KEY_DUPLICATED` (406) which invalidates the key — the user must log in
  again.** Media-DC file sessions are exempt.
- Telethon: `_call` handles `PhoneMigrateError`/`NetworkMigrateError`/`UserMigrateError` via
  `_switch_dc` (new DC, new auth key, session file overwritten; `sessions` table holds exactly one
  row — `sqlite.py:203-219`) (`client/users.py:126-136`). Exported senders:
  `_borrow_exported_sender(dc)` creates an `MTProtoSender(None)` (fresh DH handshake — **exported
  auth keys are never persisted**, so every daemon start pays one handshake per foreign DC),
  exports/imports the authorization, and disconnects it after 60 s idle
  (`telegrambaseclient.py:829-911`). `_get_dc` caches `help.getConfig` **forever at class level**
  (`_config`, line 801) and never refreshes on `updateConfig`/`updateDcOptions`.

### 1.7 RPC errors — `docs/api/api__errors.md`, Telethon `errors/rpcbaseerrors.py`, `errors/rpcerrorlist.py`
- Codes: 303 migrate; 400 bad request (incl. `PEER_FLOOD`, `FILE_REFERENCE_*`, `MSG_WAIT_FAILED`,
  `PERSISTENT_TIMESTAMP_*`, `RANDOM_ID_DUPLICATE`, `FROZEN_PARTICIPANT_MISSING`); 401
  (`AUTH_KEY_UNREGISTERED`, `AUTH_KEY_INVALID`, `USER_DEACTIVATED[_BAN]`, `SESSION_REVOKED`,
  `SESSION_EXPIRED`, `AUTH_KEY_PERM_EMPTY`); 403 privacy/permissions; 404; 406 "show
  updateServiceNotification instead" (`AUTH_KEY_DUPLICATED`, `UPDATE_APP_TO_LOGIN`); 420 flood
  (`FLOOD_WAIT_X`, `FLOOD_PREMIUM_WAIT_X`, `SLOWMODE_WAIT_X`, `TAKEOUT_INIT_DELAY_X`,
  `2FA_CONFIRM_WAIT_X`, `FROZEN_METHOD_INVALID`); 500 internal (also `-500 "No workers running"`,
  `-503 Timeout`, `RPC_CALL_FAIL`, `RPC_MCGET_FAIL`, `INTERDC_*`). Errors may carry `%d`.
- Telethon maps by exact name, then regex with capture, then by `abs(code)`
  (`errors/__init__.py::rpc_message_to_error`); base classes `InvalidDCError`, `BadRequestError`,
  `UnauthorizedError`, `ForbiddenError`, `NotFoundError`, `AuthKeyError` (406), `FloodError`,
  `ServerError`, `TimedOutError`. Note `PeerFloodError` subclasses `BadRequestError`,
  `FrozenMethodInvalidError` subclasses `FloodError`, `SlowModeWaitError`/`FloodPremiumWaitError`
  subclass `FloodError` but **not** `FloodWaitError`.

---

## 2. The update system in depth — `docs/api/api__updates.md`, Telethon `_updates/messagebox.py`, `client/updates.py`

### 2.1 Sequences that must be tracked
| Sequence | Carried by | Scope | Persist |
|---|---|---|---|
| `seq` (+`seq_start`) | `updates`, `updatesCombined` | whole account "Updates" stream; `seq_start==0` ⇒ unordered | yes (with `date`) |
| `pts` (+`pts_count`) | `updateNewMessage`, `updateEditMessage`, `updateDeleteMessages`, `updateReadHistory*`, `updateWebPage`, `updatePinnedMessages`, `updateShortMessage/ChatMessage/SentMessage`… | common message box = all private chats + basic groups | yes |
| `qts` | `updateNewEncryptedMessage`, `updateBotNewBusinessMessage`, other bot/business updates (`qts_count` always 1) | secondary box | yes |
| channel `pts` | `updateNewChannelMessage`, `updateEditChannelMessage`, `updateDeleteChannelMessages`, `updateReadChannelInbox`, `updateChannelWebPage`, `updatePinnedChannelMessages`, `updateChannelTooLong(pts?)` | one per channel/supergroup | yes, per channel |
| `version` | `updateChatParticipants`, group-call updates | dedupe/ordering only | no |

Message IDs are a different thing: one shared monotonic id space for private chats + basic
groups (per account), one per channel (same across accounts), separate for scheduled and quick
replies; secret chats use `random_id`.

### 2.2 Apply rules (docs) and Telethon's implementation
- pts/qts: `local + pts_count == pts` → apply; `>` → already applied, drop; `<` → gap.
  On a gap wait ≤0.5 s for reordered updates, then `updates.getDifference` /
  `updates.getChannelDifference`. Telethon: `POSSIBLE_GAP_TIMEOUT = 0.5` s, tdesktop
  `PtsWaiter::kWaitForSkippedTimeout = 1000` ms.
- seq: `local_seq+1 == seq_start` apply; `>` drop; `<` gap → getDifference; after applying set
  `seq` (unless 0) and `date`.
- `updatesTooLong` → getDifference now; `updateChannelTooLong` → getChannelDifference for that
  channel (tdesktop only if `pts` absent or greater than local).
- Telethon `MessageBox.process_updates` (`messagebox.py:405-507`): treats `updatesTooLong`
  (no `date`) as an account gap; checks `seq_start`; **sorts updates by `pts - pts_count`** inside
  one `Updates` so `updateReadChannelInbox(pts=X,count=0)` + `updateNewChannelMessage(pts=X,count=1)`
  apply correctly; applies pts via `apply_pts_info`; keeps out-of-order updates in `possible_gaps`
  and re-tries them after each batch; commits `seq`/`date` only when something was applied and
  no gap is pending. Updates for an entry currently being diffed are dropped (they come back through
  the difference). Updates with no pts are applied immediately. An entry seen for the first time is
  initialised to `pts - (0 if pts_count else 1)` (handles the ReadChannelInbox-first case).
- Entry key: `update.message.peer_id.channel_id`, else `update.channel_id`, else ACCOUNT; `qts` →
  SECRET (`PtsInfo.from_update`, lines 101-116).

### 2.3 getDifference / getChannelDifference
- `updates.getDifference(pts, pts_limit?, pts_total_limit?, date, qts, qts_limit?)` →
  `differenceEmpty(date, seq)` | `difference(new_messages, new_encrypted_messages, other_updates, chats, users, state)` |
  `differenceSlice(..., intermediate_state)` → loop with the intermediate state |
  `differenceTooLong(pts)` (your pts is older than the box keeps, ~5,000,000 events for the common
  box) → reset local pts to the returned one and **re-sync content yourself** (`messages.getMessages`,
  `getHistory`). Recommended `pts_total_limit` 1000-10000 (Telethon passes none; tdesktop passes none).
  Telethon `MessageBox.get_difference()`/`apply_difference()` (`messagebox.py:608-717`): converts
  `new_messages` to `UpdateNewMessage(pts=0)` and `new_encrypted_messages` to
  `UpdateNewEncryptedMessage(qts=0)`, feeds `other_updates` back through `process_updates`
  (that is how `updateChannelTooLong` inside a difference triggers channel diffs), sets the state
  from `state`/`intermediate_state`, and on `differenceTooLong` just stores `pts` (nothing re-synced).
- `updates.getChannelDifference(force?, channel:InputChannel, filter, pts, limit)` →
  `channelDifferenceEmpty(final, pts, timeout?)` | `channelDifference(final, pts, timeout?, new_messages, other_updates, chats, users)` |
  `channelDifferenceTooLong(final, timeout?, dialog, messages, chats, users)` (dialog.pts = new state;
  messages = latest history; your local history for that channel is now unreliable). `limit`:
  users 10-100, bots up to 100000; Telethon uses 100 / 100000 (`USER_CHANNEL_DIFF_LIMIT`),
  tdesktop 100 (`kChannelGetDifferenceLimit`). If not `final`, repeat immediately; if `final` and the
  user keeps the chat open, re-poll after `timeout` (default 1 s) — this "short poll" also enables
  passive updates for public channels you have not joined (max 10 polled channels).
  Telethon (`messagebox.py:723-806`, `client/updates.py:368-478`): **returns nothing for
  `channelDifferenceTooLong`** (only sets pts and users/chats), and **silently ends the diff and
  forgets the channel state if its access_hash is not in the in-memory `EntityCache`**
  (`get_channel_difference` lines 731-739). Errors: `PERSISTENT_TIMESTAMP_OUTDATED|INVALID|EMPTY`,
  `ServerError`, `TimedOutError`, `FloodWaitError` → end diff without changing pts (retry on next
  gap/timeout); `CHANNEL_PRIVATE|CHANNEL_INVALID` → drop the channel's state (banned/left).
  Requires `InputChannel(id, access_hash)`; the `force` flag is set by tdesktop when recovering a
  gap and cleared for short polls.
- Channel pts is seeded from `dialog.pts` in `messages.getDialogs` (Telethon `client/dialogs.py:86-88`
  → `try_set_channel_state`), from `channelFull.pts`, from `updateChannelTooLong.pts`, or from the
  first channel update seen.

### 2.4 When to fetch the difference manually (docs list) and what Telethon does
| Trigger | Docs | Telethon 1.44 | tdesktop |
|---|---|---|---|
| Startup | `getDifference` from persisted state (channels via `updateChannelTooLong` in `other_updates`) | only with `catch_up=True`: loads `update_state` rows and queues `UpdatesTooLong` (`telegrambaseclient.py:583-603`, `client/updates.py:275-278`). With `catch_up=False` it calls `get_me` → `_on_login` → `updates.getState` + `getDifference` **just to seed the box with the *current* state** (`client/auth.py:385-407`), i.e. everything that happened while offline is discarded. | always |
| pts/qts/seq gap | wait 0.5 s then diff | yes (0.5 s) | yes (1 s) |
| `new_session_created` | diff | **no** (salt only) | yes |
| Cannot deserialize an update (newer layer) | treat as 500: reconnect socket, re-`initConnection`, diff | **no** — dropped with a log line (`mtprotosender.py:537-541`); a `TypeNotFoundError` *inside a difference result* disconnects the client with `_updates_error` (`client/updates.py:337-343`) | reconnect+diff |
| Incomplete update (`updateShort*` referencing unknown user/chat) | diff | no check; events get `_entities` only from the `Updates` container | fetches |
| 15 min without updates | diff | yes (`NO_UPDATES_TIMEOUT = 15*60` deadline per entry, `messagebox.py:46`) | pings after 60 s (`kNoUpdatesTimeout`) and diffs after sleep |
| `updatesTooLong` / `updateChannelTooLong` | diff | yes | yes |
| After transport reconnect | (server resends unacked messages of the *same* session) | new session id ⇒ old session's unacked pushes are lost; only `get_me()` is sent; recovery happens on the next pts gap or the 15-min deadline | diff |
| Own request results carrying `Updates` | must be processed for pts | yes: `_store_own_updates` marks them `_self_outgoing` (processed for pts, **not dispatched to handlers**); `messages.AffectedMessages/History` are turned into fake `UpdateDeleteMessages([])` to advance pts (`mtprotosender.py:692-732`) | yes |

`updateMessageID(id, random_id)` is delivered as a normal update (also via getDifference) and lets
a client reconcile a `messages.sendMessage` whose `rpc_result` was lost; the server also dedupes
`random_id` forever (`RANDOM_ID_DUPLICATE` while in flight). Telethon maps `random_id` → message
in `_get_response_message` (`client/messageparse.py:113-230`) but only for the immediate result.

### 2.5 Persistence and what Telethon guarantees
- Persist: account `pts, qts, date, seq`; per-channel `pts`; the entity table (id → access_hash,
  username, phone, name) because channel diffs need `InputChannel(access_hash)`. Telethon stores
  these in `update_state(id, pts, qts, date, seq)` (`id=0` = account) and `entities` in the
  SQLite session; saved every 60 s by `_keepalive_loop` and on `disconnect()`
  (`client/updates.py:552-554`, `telegrambaseclient.py:702-722, 754`). A crash loses ≤60 s of pts
  progress (harmless: replays are deduped by the `local+count > pts` rule, but handlers will see
  those events again).
- With **`catch_up=True`** Telethon guarantees: on `connect()` the persisted state is loaded, a
  `getDifference` loop runs before normal dispatch, and updates missed while offline are delivered
  through the same event handlers — for the common box and for every channel whose pts *and*
  access_hash were persisted (warning "No access_hash in cache for channel …, will not catch up"
  otherwise). `client.catch_up()` can be called at any time (it enqueues `UpdatesTooLong`).
- With **`sequential_updates=True`** (tlgr already sets it): updates are dispatched one at a time in
  the order the MessageBox released them (`updates_to_dispatch` deque, `client/updates.py:283-293`);
  a slow handler delays everything; without it each update becomes an asyncio task (unordered).
  Ordering is per message box; there is no ordering guarantee across boxes/accounts.
- `entity_cache_limit` (default 5000) flushes the in-memory hash map to SQLite, keeping only
  self + channels present in the MessageBox (`client/updates.py:295-310`).

### 2.6 MessageBox limitations / pitfalls (Telethon 1.44)
1. `differenceTooLong` and `channelDifferenceTooLong` deliver **no content** to handlers; the
   daemon must notice (there is no callback — watch for `updates.DifferenceTooLong` via a `Raw`
   handler? No: results of getDifference are consumed internally) and resync by re-reading recent
   history. Practical detection: compare `dialog.unread_count`/`top_message` from `iter_dialogs`
   against what was dispatched, or subscribe to Telethon's `messagebox` logger.
2. Channel gap recovery silently dies when the access hash is not in memory (§2.3).
3. Exceptions during `getDifference`: `UnauthorizedError`/`AuthKeyError` → `_updates_error` set,
   client disconnected (`client/updates.py:328-336`) — the daemon must observe `client.disconnected`.
   `FloodWaitError`/`ServerError`/`TimedOutError` → diff abandoned, nothing scheduled until the next
   gap or the 15-min deadline (tdesktop retries with exponential back-off 1→64 s,
   `failDifferenceStartTimerFor`).
4. Updates that the server sends **through another session** (`tmp_sessions>1`, or a second
   process on the same key) never reach this session; each update is delivered to exactly one
   session.
5. `qts`: for user accounts `qts` is only tracked once a non-zero value has been seen; bots may have
   it reset to 0 during difference (issue #3873 handling in `set_state(reset=False)`).
6. `seq`: if the persisted `seq` is ever ahead of the server (e.g. state written after a bad diff),
   all subsequent `updates` containers are dropped as "already handled" until a `getDifference`
   resets it — the 15-min deadline eventually does.
7. Own requests' `Updates` are not dispatched (`_self_outgoing`), so a message the daemon sends never
   produces `events.NewMessage` — intended, but jobs that "watch a chat" must add their own sends.
8. Very long offline (days): common box size ≈5,000,000 events, channel ≈100,000; beyond that
   `*TooLong`. Secret-chat/`qts` queue is deleted after 7 days or once acknowledged via
   `messages.receivedQueue` / `getDifference(qts)`. Also, `update_state.date` is used by
   `getDifference`; a very old `date` is fine (server keys on pts).
9. `MessageBox` never sends `pts_total_limit`; a huge difference arrives in slices anyway, but each
   slice may be large; handlers see a burst of events (rate-limit outbound actions).
10. When the account is not logged in, Telethon's update loop disconnects on the first auth error
    only if it was "once logged in"; a fresh session keeps looping (fine for QR login, which needs
    `updateLoginToken`).

### 2.7 tlgr today (from `tlgr/core/client.py:105-121`, `daemon/server.py`)
- `create_client` sets `sequential_updates=True`, `flood_sleep_threshold=120`, `request_retries=5`,
  `connection_retries=5`, `retry_delay=1`, `auto_reconnect=True` — **no `catch_up=True`**, no
  identity strings, no `raise_last_call_error`. Consequence: every daemon restart discards
  everything received while it was down; jobs (`gateway/engine.py` builds `events.NewMessage` etc.)
  miss those messages; `tlgr watch` polls the IPC every 2 s instead of consuming events.
- `DaemonServer.status()` reports `client.is_connected` but nothing consumes `client.disconnected`
  or `_updates_error`, so an `AUTH_KEY_UNREGISTERED`/`SESSION_REVOKED` account stays "held" and every
  request fails with "Cannot send requests while disconnected".

---

## 3. Files — `docs/api/api__files.md`, `api__file-references.md`, `api__datacenter.md`, Telethon `client/uploads.py`, `client/downloads.py`

### 3.1 Upload
- Random 64-bit `file_id`; `part_size % 1024 == 0` and `524288 % part_size == 0` (512 KB
  recommended); parts numbered 0..N-1; `N ≤ upload_max_fileparts_default (4000 ⇒ 2 GB)` /
  `_premium (8000 ⇒ 4 GB)` from `help.getAppConfig`. `upload.saveFilePart(file_id, part, bytes)` for
  files ≤10 MB (send `inputFile(id, parts, name, md5)`), `upload.saveBigFilePart(file_id, part,
  total_parts, bytes)` for >10 MB (`inputFileBig`); streamed uploads use `total_parts = -1` until the
  last part. Parts live "minutes to hours" on the server; the resulting `InputFile` handle is usable
  for < 1 day. Docs recommend ≥2 requests in flight and multiple TCP connections. Errors:
  `FILE_PARTS_INVALID`, `FILE_PART_INVALID`, `FILE_PART_TOO_BIG`, `FILE_PART_EMPTY`,
  `FILE_PART_SIZE_INVALID`, `FILE_PART_SIZE_CHANGED`, `FILE_PART_X_MISSING` (re-save that part and
  retry the final method), `MD5_CHECKSUM_INVALID`, `FLOOD_PREMIUM_WAIT_X` (retry after X s; only after
  tens of GB). Uploads go to the **home DC** (a part is only available where it was saved).
- Telethon `upload_file` (`client/uploads.py:577-761`): part size 128/256/512 KB by file size
  (`utils.get_appropriated_part_size`), **strictly sequential** (one request in flight), MD5 only for
  small files, `progress_callback(sent, total)`, returns `InputFileBig`/`InputSizedFile`; each part
  request goes through `_call` (so 500s/flood are retried per part). `send_file` → `_file_to_media`
  (PIL resize for photos, hachoir attributes) → `messages.sendMedia`; albums via
  `messages.uploadMedia` per item + `messages.sendMultiMedia` (≤10). The session's `sent_files`
  (md5,size → id,hash) cache exists but `allow_cache` is a no-op.
- Re-sending existing media: `inputMediaPhoto(inputPhoto(id, access_hash, file_reference))` /
  `inputMediaDocument`; `messages.getDocumentByHash(sha256,size,mime)` for GIF/document dedupe;
  `messages.editMessage` can replace media; only `spoiler/ttl_seconds/query/video_cover/video_timestamp`
  can change without re-upload.

### 3.2 Download
- `upload.getFile(precise?, cdn_supported?, location, offset, limit)`; without `precise`: offset and
  limit multiples of 4 KB, `1 MB % limit == 0`, and the range must stay inside one 1 MB block; with
  `precise`: 1 KB granularity, limit ≤ 1 MB. Locations: `inputDocumentFileLocation(id, access_hash,
  file_reference, thumb_size)`, `inputPhotoFileLocation(...)`, `inputPeerPhotoFileLocation(peer,
  photo_id, big)`, `inputStickerSetThumb`, `inputEncryptedFileLocation`, `inputSecureFileLocation`,
  `inputTakeoutFileLocation`, `inputGroupCallStream`. Download from the object's `dc_id`
  (`FILE_MIGRATE_X` otherwise) using a **separate media session**. Parallelism guidance:
  `small_queue_max_active_operations_count = 5` (<20 MB) / `large_... = 2` (≥20 MB) per DC.
  `upload.getFileHashes(location, offset)` gives SHA-256 per part for integrity (not used by
  Telethon). Web files: `upload.getWebFile` on `config.webfile_dc_id`. `FLOOD_PREMIUM_WAIT_X` → retry.
  PhotoSize types `s/m/x/y/w` (bounded), `a/b/c/d` (cropped), `i` stripped, `j` SVG path; VideoSize
  `p/u/v/f`.
- Telethon `download_file`/`iter_download` (`client/downloads.py:453-773`): request size clamped to
  4 KB..512 KB; `_DirectDownloadIter` when offset/stride are 4 KB-aligned else `_GenericDownloadIter`;
  borrows an exported sender for foreign `dc_id`, switches on `FileMigrateError`, retries one
  `TimedOutError` after 1 s; `progress_callback(received, total)`; **resumable** via `iter_download(file,
  offset=N)` (write to disk sequentially, restart from the byte count you already have); decrypts
  secret-chat files with AES-IGE when `key/iv` are given.
- **CDN** (`upload.fileCdnRedirect(dc_id, file_token, encryption_key, encryption_iv, file_hashes)`):
  fetch with `upload.getCdnFile(file_token, offset, limit)` from the CDN DC (own auth key generated
  with the CDN RSA keys from `help.getCdnConfig`), decrypt with AES-256-CTR where the IV's last 4
  bytes are `offset/16` (big-endian), verify each `fileHash` (fetch more with
  `upload.getCdnFileHashes`), and on `upload.cdnFileReuploadNeeded` call `upload.reuploadCdnFile` on
  the master DC. Telethon has a correct `crypto/cdndecrypter.py` (CTR + hash check) but it is
  **unused**; the live path (`_DirectDownloadIter._init` + `_download_file` on `_CdnRedirect`) reuses
  the *main DC auth key* for the CDN client (`_get_cdn_client`, `telegrambaseclient.py:913-939`),
  decrypts with `AES.decrypt_ige` instead of CTR (`downloads.py:568-569`) and never verifies hashes;
  bots get a `ValueError`. Treat CDN downloads as unverified in Telethon 1.44; in practice CDN
  redirects are rare for user accounts (large public-channel media).

### 3.3 File references and refresh strategy — `api__file-references.md`
- `file_reference` (bytes in `photo`/`document`) expires; using a stale one gives
  `FILE_REFERENCE_EXPIRED` / `FILE_REFERENCE_INVALID` (or `FILE_REFERENCE_%d_EXPIRED` for the n-th
  item of `sendMultiMedia`/paid media). Refresh = re-fetch the **source** where the media was seen:
  message → `messages.getMessages` / `channels.getMessages` (or `messages.getQuickReplyMessages`),
  scheduled → `messages.getScheduledMessages`, story → `stories.getStoriesByID`, web page, user
  photo → `photos.getUserPhotos`, sticker set → `messages.getStickerSet`, `users.getFullUser`,
  `channels.getFullChannel`, `messages.getFullChat`, saved GIFs, wallpapers, themes, etc. The docs
  publish a machine-readable map (`api__file-references__map-schema.md`) of incoming traversers
  (where to harvest `id → file_reference` and `id → source`) and outgoing traversers (which
  request fields to swap). Keep `HashMap<FileId, bytes>` + `HashMap<FileId, Vector<FileSource>>`.
- Telethon refreshes only during a **document** download when it knows `(input_chat, msg_id)`
  (`downloads.py:118-139`, re-fetches the message and swaps `file_reference`); no refresh for
  photos, thumbnails, profile photos, or when *sending* existing media. Bot-API `file_id` strings
  carry no reference (`_CacheType` sets `b''`).
- Daemon strategy: always download from a freshly fetched `Message` (tlgr's `download_media` already
  calls `get_messages(ids=)` first — good); keep `(peer, msg_id)` as the source of every media the
  daemon stores or re-sends; on any `FileReference*Error` (incl. `FilerefUpgradeNeededError`)
  re-fetch the source, swap `file_reference`, retry once; for `sendMultiMedia` parse the index.

---

## 4. Entities — `docs/api/api__min.md`, `api__offsets.md`, Telethon `client/users.py`, `sessions/*.py`, `_updates/entitycache.py`

- `InputPeerUser(id, access_hash)` / `InputPeerChannel(id, access_hash)` / `InputPeerChat(id)` /
  `InputPeerSelf`; `access_hash` is **per account** (cannot be shared between tlgr accounts) and is
  obtained only by *encountering* the full `user`/`channel` constructor (dialogs, messages, search,
  participants, resolve). There is **no method that turns a bare id into an access hash.**
- **min constructors**: `user.min`/`channel.min` carry a restricted hash usable only for files and
  for `inputPeerUserFromMessage(peer, msg_id, user_id)` / `inputPeerChannelFromMessage` /
  `inputUserFromMessage` / `inputChannelFromMessage` — store the (chat, msg_id) context where the
  min entity was seen (message sender, forward header, `messageEntityMentionName`). Telethon skips
  min entities in both caches (`sessions/memory.py:108-119`, `_updates/entitycache.py:39,49`) and
  only allows them for profile-photo downloads (`utils.get_input_peer(check_hash=False)`); it has no
  helper to build `*FromMessage` — do it by hand.
- Telethon resolution order, `get_input_entity(peer)` (`client/users.py:354-480`): (1) already an
  `Input*` → return; (2) int / `Peer` → in-memory `EntityCache`; (3) `'me'/'self'`; (4) SQLite
  `entities` table by marked id, username (lower-cased; duplicates → oldest evicted), phone, or exact
  display name; (5) strings → network: phone → `contacts.getContacts` scan (users only, not bots),
  `t.me/joinchat` → `messages.checkChatInvite` (needs membership), username →
  `contacts.resolveUsername` (~50 per short period before `FLOOD_WAIT`); (6) bare `PeerUser` →
  `users.getUsers([InputUser(id, access_hash=0)])` which works only for **bots** (users who wrote to
  the bot) or for **contacts**, else `userEmpty` → `ValueError`; bare `PeerChannel` →
  `channels.getChannels([InputChannel(id, 0)])` → `CHANNEL_INVALID`; `PeerChat` → `InputPeerChat`.
  `get_entity(x)` = `get_input_entity` then `users.getUsers`(≤200)/`messages.getChats`/
  `channels.getChannels` (network every time; avoid in hot paths — `get_input_entity` is free).
- Correct strategies for a daemon (all methods exist in Telethon):
  1. Warm the cache with `iter_dialogs()` (`messages.getDialogs`; each page returns full users/chats
     with hashes and seeds channel pts). tlgr's `dialog_status` scan is exactly this fallback.
  2. `messages.getPeerDialogs([InputDialogPeer(peer)])` for peers already resolvable — returns
     dialog + top message + entities.
  3. `contacts.resolveUsername`, `contacts.resolvePhone(phone)` (works for non-contacts only if
     their privacy allows), `contacts.search(q, limit)` (global username/name search),
     `messages.searchGlobal` (messages from unknown peers, gives min entities + context).
  4. Membership lists: `channels.getParticipants` (full users, hashed pagination),
     `messages.getFullChat`, `channels.getLeftChannels`, `messages.getCommonChats`.
  5. From messages: `messages.getMessages`/`channels.getMessages` return `users`/`chats` vectors;
     senders in groups are often min → use `InputPeerUserFromMessage`.
  6. Self: `users.getUsers([InputUserSelf])` (Telethon `get_me`, cached in `EntityCache.self_id`).
- **Hash-based caching** (`api__offsets.md`): many list methods take `hash:long` computed over the
  cached ids with `h = h ^ (h >> 21); h = h ^ (h << 35); h = h ^ (h >> 4); h = h + id` (unsigned
  64-bit, strings via first 8 bytes of MD5) and return `*NotModified`; Telethon passes `hash=0`
  everywhere (e.g. `iter_dialogs`, `GetContactsRequest(0)`, `GetAppConfig(0)`), so it always
  re-downloads — fine for correctness, costly for big accounts. tlgr's `list_contacts` also passes 0.
- Telethon's SQLite entity table stores `(id marked, hash, username, phone, name, date)`; writes
  are batched every 60 s; `entity_cache_limit` governs the in-memory map. Marked ids (`-100…`) come
  from `utils.get_peer_id`; `utils.resolve_id` inverts.

---

## 5. Flood / rate limits and retry policy — `api__errors.md`, `api__auth.md`, Telethon `client/users.py:32-140`

- Families: `FLOOD_WAIT_X` (per method + arguments; e.g. `contacts.resolveUsername`,
  `messages.sendMessage` to many peers, `auth.sendCode` 5/day/number, `updates.getDifference` spam),
  `FLOOD_PREMIUM_WAIT_X` (file transfer throttling for non-Premium), `SLOWMODE_WAIT_X` (per chat;
  `channelFull.slowmode_seconds`/`slowmode_next_send_date`), `FLOOD_TEST_PHONE_WAIT_X`,
  `TAKEOUT_INIT_DELAY_X`, `2FA_CONFIRM_WAIT_X`, `PHONE_NUMBER_FLOOD`, `API_ID_PUBLISHED_FLOOD`,
  transport `-429`. **Account-level flags with no wait**: `PEER_FLOOD` (400 "Too many requests" —
  spam limits on messaging non-contacts; only lifted by @SpamBot/time), `FROZEN_METHOD_INVALID`
  (420) / `FROZEN_PARTICIPANT_MISSING` (400) — account frozen for ToS violations; call
  `help.getAppConfig` and read `freeze_since_date`, `freeze_until_date`, `freeze_appeal_url`.
  `USER_PRIVACY_RESTRICTED`, `CHAT_WRITE_FORBIDDEN`, `USER_BANNED_IN_CHANNEL`, `USER_IS_BLOCKED` are
  permissions, not floods. `MSG_WAIT_FAILED`/`MSG_WAIT_TIMEOUT` concern `invokeAfterMsg` chains.
- Telethon `_call` retry policy (`request_retries`, tlgr sets 5):
  - `ServerError` (500/-500), `RpcCallFailError`, `RpcMcgetFailError`, `InterdcCallErrorError`,
    `InterdcCallRichErrorError`, `TimedOutError` → sleep 2 s, retry.
  - `FloodWaitError`, `FloodPremiumWaitError`, `SlowModeWaitError`, `FloodTestPhoneWaitError` →
    remember `time()+seconds` per **request constructor** (not per peer; slow-mode excluded) in
    `_flood_waited_requests`; sleep and retry if `seconds <= flood_sleep_threshold` (tlgr 120 s,
    capped at 1 day) else raise; before sending, a remembered wait ≤3 s is ignored, ≤threshold is
    slept "early", otherwise `FloodWaitError` is raised without a network call. `FLOOD_WAIT_0` → 1 s.
  - Migrate errors → `_switch_dc` (raise `PhoneMigrate`/`NetworkMigrate` if already authorized).
  - After exhausting retries: `ValueError('Request was unsuccessful N time(s)')` unless
    `raise_last_call_error=True` (tlgr should set it so `FloodWaitError`/`ServerError` surface).
  - Everything else (400/401/403/404/406/420-frozen) propagates immediately.
- Anything the daemon should add on top (Telethon's flood memory is in-process and per request
  type): a per-account outbound token bucket (official clients pace sends; a safe default is ≤1
  message/s and ≤20-30 new-peer messages/day for unwarmed accounts), per-chat slow-mode awareness,
  persistent FLOOD_WAIT deadlines keyed `(account, method, peer)` so restarts do not re-hit them,
  exponential back-off with jitter for 500-class errors beyond Telethon's 5×2 s, and a **circuit
  breaker** on `PEER_FLOOD`/`FROZEN_*`/`USER_DEACTIVATED` that pauses all jobs for that account.
- tlgr IPC mapping today (`daemon/ipc.py:47-63`): `FloodWaitError` → 429 `RATE_LIMITED` +
  `wait_seconds`; `PeerFloodError` → 403 `PEER_FLOOD`; any RPC error containing "FROZEN" → 403
  `ACCOUNT_FROZEN`; everything else → 500. Missing: `SlowModeWaitError`/`FloodPremiumWaitError`/
  `TakeoutInitDelayError` (they are `FloodError` but not `FloodWaitError`), auth errors (§6.5) →
  `SESSION_ERROR` (exit 4), permission errors → `PERMISSION_DENIED`, `ChannelPrivate`/`PeerIdInvalid`
  /`UsernameNotOccupied`/`ValueError("Could not find the input entity")` → `CHAT_NOT_FOUND`,
  `ServerError`/`TimedOutError`/`ConnectionError` → `RETRYABLE`.

---

## 6. Auth flows — `api__auth.md`, `api__srp.md`, `api__qr-login.md`, Telethon `client/auth.py`, `password.py`, `tl/custom/qrlogin.py`

### 6.1 Phone-code login
1. `auth.sendCode(phone, api_id, api_hash, codeSettings)` → `auth.sentCode(type, phone_code_hash,
   next_type?, timeout?)` | `auth.sentCodeSuccess(authorization)` (future auth token matched) |
   `auth.sentCodePaymentRequired` (official apps only). Redirects `PHONE_MIGRATE_X`/`NETWORK_MIGRATE_X`
   → Telethon `_switch_dc`. `AUTH_RESTART` → resend (Telethon retries 3×).
2. Code types: `sentCodeTypeApp` (code delivered to other logged-in sessions — the normal path for
   third-party apps), `Sms`, `SmsWord`, `SmsPhrase`, `Call`, `FlashCall(pattern)`, `MissedCall(prefix)`,
   `EmailCode(email_pattern, length, reset_available_period, reset_pending_date)`,
   `SetUpEmailRequired(apple/google_signin_allowed)`, `FragmentSms(url)`, `FirebaseSms` (official
   mobile apps only). **Third-party apps cannot get SMS/voice codes in general** — only Telegram-app
   codes, Fragment, email, future auth tokens, QR and passkeys; `UPDATE_APP_TO_LOGIN` (406) means
   this api_id is not allowed to log in this way — fall back to QR. `auth.resendCode(phone,
   phone_code_hash, reason?)` after `timeout` s switches to `next_type`; `auth.cancelCode`.
3. `auth.signIn(phone, phone_code_hash, phone_code | email_verification)` → `auth.authorization(user,
   setup_password_required?, otherwise_relogin_days?, tmp_sessions?, future_auth_token?)` |
   `auth.authorizationSignUpRequired(terms_of_service)` → `auth.signUp(phone, hash, first, last)` after
   `help.acceptTermsOfService` (tlgr should refuse sign-up; Telethon raises
   `PhoneNumberUnoccupiedError` and keeps `_tos`). Errors `PHONE_CODE_INVALID`, `PHONE_CODE_EXPIRED`
   (Telethon drops the cached hash), `PHONE_NUMBER_BANNED`, `PHONE_NUMBER_INVALID`.
4. Email login: on `sentCodeTypeSetUpEmailRequired` call `account.sendVerifyEmailCode(purpose=
   emailVerifyPurposeLoginSetup(phone, phone_code_hash), email)` then `account.verifyEmail(purpose,
   emailVerificationCode(code))` → `account.emailVerifiedLogin(sent_code)`; `auth.resetLoginEmail`
   if inaccessible; Google/Apple tokens alternative. Telethon has no helper — raw requests.
5. Future auth tokens: `auth.logOut` and `auth.authorization` may return `future_auth_token`; keep
   ≤20 and pass them in `codeSettings.logout_tokens` so re-login skips the code (Telethon ignores
   them).
6. Telethon helpers: `send_code_request` (`SendCodeRequest` with empty `CodeSettings()`, resends via
   `ResendCodeRequest`), `sign_in(phone, code | password | bot_token)`, `sign_up`, `_on_login`
   (sets `EntityCache.self_user`, `updates.getState` + `getDifference`, loads the MessageBox),
   `start()` interactive wrapper. tlgr `ClientWrapper.login` uses `send_code_request` + `sign_in`
   with `input()`/`getpass()` prompts (`core/client.py:166-188`) — no `next_type`/resend, no email
   flow, no QR.

### 6.2 2FA (SRP-6a) — `api__srp.md`
- `account.getPassword` → `account.password(srp_B, srp_id, current_algo=
  passwordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow(salt1, salt2, g, p), secure_random)`.
  `x = SH(pbkdf2(sha512, SH(SH(pw,salt1),salt2), salt1, 100000), salt2)`, `v = g^x`,
  `k = H(p|g)`, `u = H(g_a|g_b)`, `t = (g_b - k*v) mod p`, `S = t^(a + u*x)`, `K = H(S)`,
  `M1 = H(H(p) xor H(g) | H(salt1) | H(salt2) | g_a | g_b | K)` → `inputCheckPasswordSRP(srp_id, A=g_a,
  M1)` → `auth.checkPassword` (`PASSWORD_HASH_INVALID` if wrong). Telethon `password.compute_check`
  does all of it including the safe-prime and `is_good_large`/`is_good_mod_exp_first` checks
  (`telethon/password.py:8-193`); `sign_in(password=...)` wraps it. Setting/changing: `account.
  updatePasswordSettings(password, new_settings)` with `new_password_hash = v` over a fresh 32-byte
  salt suffix (Telethon `edit_2fa`), recovery email `account.confirmPasswordEmail`; recovery
  `auth.requestPasswordRecovery` → `auth.checkRecoveryPassword` → `auth.recoverPassword`; reset
  `account.resetPassword` (7-day wait) / `account.declinePasswordReset`. Other methods needing a
  password return `PASSWORD_MISSING`, `PASSWORD_TOO_FRESH_X`, `SESSION_TOO_FRESH_X`; call first with
  `inputCheckPasswordEmpty` and react to the error.

### 6.3 QR login — `api__qr-login.md`
- `auth.exportLoginToken(api_id, api_hash, except_ids)` → `auth.loginToken(expires≈30 s, token)`;
  show `tg://login?token=<base64url>`; a logged-in app calls `auth.acceptLoginToken`; this client
  receives `updateLoginToken` (one of the few updates delivered before login — so **updates must be
  enabled** on that connection, i.e. not `receive_updates=False`), then calls `exportLoginToken`
  again → `auth.loginTokenSuccess(authorization)` or `auth.loginTokenMigrateTo(dc_id, token)` →
  `auth.importLoginToken(token)` on that DC. 2FA afterwards → `SESSION_PASSWORD_NEEDED` → §6.2.
  Telethon: `qr = await client.qr_login(); qr.url; await qr.wait(timeout)`; `recreate()` on expiry;
  handles `LoginTokenMigrateTo` via `_switch_dc` (`tl/custom/qrlogin.py`). This is the login method
  that always works for third-party api_ids.

### 6.4 Sessions / authorizations
- `account.getAuthorizations` → `account.authorizations(authorization_ttl_days, [authorization(hash,
  current, official_app, password_pending, encrypted_requests_disabled, call_requests_disabled,
  unconfirmed, device_model, platform, system_version, api_id, app_name, app_version, date_created,
  date_active, ip, country, region)])`; `account.resetAuthorization(hash)` (fails with
  `FRESH_RESET_AUTHORISATION_FORBIDDEN` for the first 24 h of a new session), `auth.resetAuthorizations`
  (all others), `account.changeAuthorizationSettings(hash, confirmed?, encrypted_requests_disabled?,
  call_requests_disabled?)`, `account.setAuthorizationTTL(days)`, web logins
  `account.getWebAuthorizations` / `resetWebAuthorization(s)`. `updateNewAuthorization(unconfirmed,
  hash, date, device, location)` arrives on other sessions when this one logs in; unconfirmed ones
  auto-confirm after `authorization_autoconfirm_period` (604800 s). Login codes must be invalidated
  with `account.invalidateSignInCodes` if the user forwards/screenshots a 777000 message.
- Logout: `auth.logOut` → `auth.loggedOut(future_auth_token?)`; official clients then send
  `destroy_auth_key` (tdesktop `mtp_instance.cpp:1715-1765`, "should be called whenever a permanent
  auth key isn't needed anymore"). Telethon `log_out()` sends `LogOutRequest`, clears self/authorized,
  disconnects and deletes the session file; a `DestroyAuthKeyRequest` can be sent before
  disconnecting (Telethon handles the `destroy_auth_key_*` answers).

### 6.5 Fatal auth errors and their meaning for a daemon
| Error | Meaning | Action |
|---|---|---|
| `AUTH_KEY_UNREGISTERED` (401) | key not bound to any user: logged out elsewhere, session revoked, or never logged in (also returned when calling authed methods before login) | mark account `needs_login`; keep the file only if never logged in |
| `SESSION_REVOKED`, `SESSION_EXPIRED`, `USER_DEACTIVATED`, `USER_DEACTIVATED_BAN` (401) | authorization terminated / account deleted or banned | stop jobs, mark `needs_login` (or `banned`), delete session |
| `AUTH_KEY_DUPLICATED` (406, `AuthKeyError`) | same auth key used from two TCP connections/IPs; server invalidated the key | stop **both** users of the session; re-login; find the second process |
| transport `-404` / `AuthKeyNotFound` | DC forgot the key (temp key expiry, key destroyed, or DC switch) | for a permanent key: treat like `AUTH_KEY_UNREGISTERED` after one retry |
| `AUTH_KEY_PERM_EMPTY` (401) | temp key not bound (PFS only) | n/a for Telethon |
| `UPDATE_APP_TO_LOGIN` (406) | this api_id may not use this login path | use QR login |
| `FROZEN_METHOD_INVALID` (420) | account frozen | read `help.getAppConfig` freeze fields, pause |
Telethon surfaces the 401 family as `UnauthorizedError` subclasses from any request; inside the
update loop they set `client._updates_error` and disconnect (`client/updates.py:328-336, 373-388`);
`AuthKeyNotFound` resolves `client.disconnected` with that exception.

---

## 7. `initConnection` identity — `docs/api/api__invoking.md`, tdesktop `mtproto/session_private.cpp:553-712`, Android `jni/tgnet/ConnectionsManager.cpp:3125-3175`

- `initConnection#c1cd5ea9 {X} flags api_id device_model system_version app_version system_lang_code
  lang_pack lang_code proxy? params? query` wrapped in `invokeWithLayer(LAYER, …)`; must be the
  first call on each new connection (and after each temp-key bind; and whenever a parameter
  changes); the layer and the strings are stored server-side with the auth key and shown in
  Active Sessions (`authorization.device_model/platform/system_version/app_name/app_version`).
  `invokeWithoutUpdates` wraps it when the connection should not receive updates (file sessions).
  Telethon sends `InvokeWithLayerRequest(227, InitConnectionRequest(query=help.getConfig))` on every
  `connect()` (`telegrambaseclient.py:605-611`) and `InitConnection(query=auth.importAuthorization)`
  for exported senders.
- Official values: tdesktop `device_model` = machine model from the OS (CDN sessions send "n/a"),
  `system_version` = OS name/version, `app_version` = `AppVersionStr + " x64" + [" Flatpak"|" Snap"|
  " Mac App Store"|" Microsoft Store"]`, `lang_pack = "tdesktop"`, `lang_code` = cloud language,
  `system_lang_code` = OS locale, `params = {"tz_offset": <seconds rounded to 900>}`. Android:
  `Build.MANUFACTURER + Build.MODEL`, `versionName (versionCode)[ beta]`, `lang_pack = "android"`,
  params `device_token`, `data` (cert fingerprint), `tz_offset`. TDLib mirrors these as
  `setTdlibParameters(device_model, system_version, application_version, system_language_code)`.
- "Official app" behaviours are keyed on the **api_id/api_hash** (and, for langpacks, on
  `lang_pack`): SMS/Firebase/paid auth, `official_app` flag, `payments.assign*Transaction`,
  `auth.reportMissingCode`, langpack access. They cannot be obtained by copying strings, and using an
  official app's api_id/api_hash from a third-party client is a ToS violation that gets accounts
  banned — do not do it. Telethon defaults (`telegrambaseclient.py:358-378`): `device_model =
  'PC 64bit'` (from `platform.uname().machine`), `system_version = '24.6.0'`-style kernel release,
  `app_version = '1.44.0'`, `lang_code = system_lang_code = 'en'`, `lang_pack = ''`.
- **What tlgr should declare** (via `TelegramClient(device_model=..., system_version=...,
  app_version=..., lang_code=..., system_lang_code=...)`): stable, honest, human-recognisable
  strings so the user can identify the session in Settings → Devices, e.g. `device_model =
  f"{hostname} ({platform.machine()})"` or the hardware model (macOS `hw.model`),
  `system_version = f"macOS {platform.mac_ver()[0]}"` / `f"{distro} {release}"`,
  `app_version = f"tlgr {tlgr.__version__} (Telethon {telethon.__version__})"`,
  `lang_code`/`system_lang_code` from the user's locale (`en` fallback), `lang_pack = ''`. Keep them
  constant across restarts (changing them re-registers nothing but makes the sessions list churn).
  Optional: set `params={"tz_offset": …}` by constructing `InitConnectionRequest` manually
  (Telethon's `_init_request` is a plain attribute that can be replaced before `connect()`).

---

## 8. Takeout sessions — `docs/api/api__takeout.md`, Telethon `client/account.py`

- `account.initTakeoutSession(contacts?, message_users?, message_chats?, message_megagroups?,
  message_channels?, files?, file_max_size?)` → `account.takeout(id)`; the user must confirm the
  export from another device — the first call returns `TAKEOUT_INIT_DELAY_X` (420) until the
  confirmation window passes; then wrap **every** request (including `upload.getFile`) in
  `invokeWithTakeout(takeout_id, query)`; history/dialog/getMessages calls must additionally be
  wrapped in `invokeWithMessagesRange(range, query)` iterating the ranges from
  `messages.getSplitRanges`; finish with `account.finishTakeoutSession(success)`. Errors
  `TAKEOUT_REQUIRED` (method must be inside a takeout for this account/size), `TAKEOUT_INVALID`
  (id expired/finished). Extra data via `upload.getFile(inputTakeoutFileLocation)` (JSON) —
  `TAKEOUT_FILE_EMPTY` if nothing. Takeout relaxes history flood limits and is the only sanctioned
  way to dump full histories (dialogs via `messages.getDialogs`, history via `messages.getHistory`
  or `messages.search(from_id=inputPeerSelf)` for own messages, `CHANNEL_PRIVATE` fallback to
  search, `channels.getLeftChannels`, `stories.getStoriesArchive`, `photos.getUserPhotos`,
  `users.getSavedMusic`, `contacts.getSaved`, `contacts.getTopPeers`, `account.getAuthorizations`,
  `account.getWebAuthorizations`, `messages.getReplies` for topics, `messages.getCustomEmojiDocuments`).
- Telethon: `async with client.takeout(contacts=…, users=…, chats=…, megagroups=…, channels=…,
  files=…, max_file_size=…) as t:` — `t(request)` wraps in `InvokeWithTakeoutRequest`; the id is
  persisted in `session.takeout_id` so an interrupted export can be resumed; `client.end_takeout(
  success)`; `TakeoutInitDelayError.seconds` tells how long to wait; `invokeWithMessagesRange` is
  **not** used by Telethon (TODO) — wrap manually with `InvokeWithMessagesRangeRequest`.

---

## 9. Checklist — what tlgr's daemon must implement/change to be production-correct

1. **Persist and replay update state**: `create_client(...)` must pass `catch_up=True` (Telethon then
   loads `update_state`, runs `getDifference` before dispatch, and delivers offline messages to
   `gateway/engine.py` handlers). Keep `sequential_updates=True`. Warm the channel hash cache before
   the first difference by running `async for _ in client.iter_dialogs(): pass` once per connect
   (seeds `dialog.pts` and access hashes) — do it *before* registering time-sensitive handlers or
   accept that the first `catch_up` may skip channels whose hash is unknown.
2. **Catch up after every reconnect**: subclass `TelegramClient` (or monkey-patch
   `_handle_auto_reconnect`) to `await self.catch_up()` in addition to `get_me()`; Telethon's stock
   callback does not, and reconnects create a new MTProto session whose lost pushes are only
   recovered on the next pts gap or 15 min later. Also call `client.catch_up()` when the daemon has
   been suspended (wall-clock jump > 60 s) — tdesktop's `kNoUpdatesAfterSleepTimeout` behaviour.
3. **Supervise each account's connection**: per-account task awaiting `client.disconnected`; on
   exception or `client._updates_error` classify: `AuthKeyUnregisteredError`, `SessionRevokedError`,
   `SessionExpiredError`, `UserDeactivated(Ban)Error`, `AuthKeyDuplicatedError`, `AuthKeyNotFound` →
   set account state `needs_login` (persist in `AccountManager`), stop its jobs, report in `status()`
   (`healthy:false`, reason) and make IPC return `SESSION_ERROR` (exit 4) for that account; otherwise
   reconnect with capped exponential back-off (Telethon gives up after `connection_retries=5` × 1 s).
   `ConnectionError('Cannot send requests while disconnected')` must never be the user-visible error.
4. **Single owner per session file**: take an exclusive `fcntl.flock` on `<session>.lock` in the
   daemon and in `tlgr account add/import`; refuse to open a session that is locked (explain that
   two connections cause `AUTH_KEY_DUPLICATED`, which invalidates the login). Route logins through
   the daemon or stop it first; `account import` must not "verify by connecting" while the daemon
   holds the same key.
5. **Declare an honest identity** (§7): `device_model`, `system_version`, `app_version`,
   `lang_code`, `system_lang_code` in `create_client`; keep them stable; never use official api_ids.
6. **Surface real errors**: `raise_last_call_error=True`; extend `daemon/ipc.py::_handle_exception`
   to map `SlowModeWaitError`/`FloodPremiumWaitError`/`TakeoutInitDelayError` → `RATE_LIMITED` with
   `wait_seconds`, the 401/406 family → `SESSION_ERROR`, `ForbiddenError`/`ChatWriteForbiddenError`/
   `UserBannedInChannelError`/`UserPrivacyRestrictedError`/`ChatAdminRequiredError` →
   `PERMISSION_DENIED`, `ChannelPrivateError`/`PeerIdInvalidError`/`UsernameNotOccupiedError`/
   `ValueError("Could not find the input entity")` → `CHAT_NOT_FOUND`, `ServerError`/`TimedOutError`/
   `OSError` → `RETRYABLE`, `FrozenMethodInvalidError`/`FrozenParticipantMissingError` →
   `ACCOUNT_FROZEN` (then fetch `help.getAppConfig` freeze fields into the status).
7. **Outbound rate limiting & flood memory**: per-account send queue with a token bucket, per-chat
   slow-mode (`channelFull.slowmode_next_send_date`), persist `FLOOD_WAIT` deadlines keyed
   `(account, request type, peer)` to disk, refuse sends while a deadline is active (return
   `wait_seconds`), and trip a circuit breaker on `PEER_FLOOD`/`FROZEN_*` that pauses jobs until an
   operator resets it. Telethon's in-memory `_flood_waited_requests` is lost on restart.
8. **Idempotent sends**: generate and persist `random_id` per outgoing job message before calling
   `messages.sendMessage/sendMedia/forwardMessages` (pass `random_id=` on the raw requests; Telethon's
   high-level `send_message` auto-generates one) so a retry after a lost `rpc_result` is deduplicated
   by the server; reconcile via the returned `updateMessageID`.
9. **Resync on `*TooLong`**: log-watch Telethon's `messagebox` logger (or wrap `MessageBox.
   apply_difference`/`apply_channel_difference`) to detect `updates.DifferenceTooLong` and
   `updates.ChannelDifferenceTooLong`; mark the account/channel `resync_needed` and re-scan the last
   N messages of affected dialogs (`iter_dialogs` → `iter_messages(min_id=last_seen)`) so jobs do not
   silently miss history.
10. **Failed differences must retry**: after `FloodWait`/`ServerError` during `getDifference`
    Telethon idles until the next gap or 15 min; schedule `client.catch_up()` with back-off
    (2 s → 64 s) like tdesktop's `getDifferenceAfterFail`.
11. **Entity resolution service** (§4): resolve `int | @username | +phone | t.me link` through
    `get_input_entity` (cache-only) → `contacts.resolveUsername`/`contacts.resolvePhone` →
    `iter_dialogs` scan (already in `dialog_status`) → `contacts.search`; never call `get_entity` on
    ids in hot paths; support `InputPeerUserFromMessage` for group posters (`chat posters` →
    `user info`) by remembering `(chat, msg_id)` per sender; keep marked ids consistent
    (`utils.get_peer_id`). Access hashes are per account — never share caches across accounts.
12. **Config freshness**: Telethon caches `help.getConfig` forever; register a `events.Raw` handler
    for `UpdateConfig`/`UpdateDcOptions` that clears `TelegramClient._config`, and refresh
    `help.getConfig`/`help.getAppConfig(hash)` at least daily; read `upload_max_fileparts_*`,
    caption/message length limits, `small/large_queue_max_active_operations_count`, freeze fields,
    `authorization_autoconfirm_period` from it instead of hard-coding.
13. **Download pipeline**: `iter_download(location, request_size=512*1024, offset=resume_offset)`
    into a temp file with fsync'd progress, at most 2 large (≥20 MB) + 5 small concurrent transfers
    per DC, progress events over IPC (`received,total`), verify final size, refresh file references
    by re-fetching the message on any `FileReference*Error` (photos and thumbnails too — Telethon only
    does documents), and keep exported senders warm (`_DISCONNECT_EXPORTED_AFTER=60 s` is fine).
    Treat CDN redirects as best-effort until Telethon's CDN path is fixed (§3.2).
14. **Upload pipeline**: for files >10 MB implement a pipelined uploader — 512 KB parts,
    3-4 `upload.saveBigFilePart` requests in flight (`asyncio.gather` per window, order irrelevant),
    md5 for ≤10 MB, retry individual parts on `FILE_PART_X_MISSING`, reuse `InputFileBig` handles
    (<1 day) for retries of the final `sendMedia`, `messages.uploadMedia` + `sendMultiMedia` for
    albums, respect `upload_max_fileparts_*`, honour `FLOOD_PREMIUM_WAIT_X`.
15. **Event streaming instead of polling**: replace `cli/watch.py`'s 2-second `chat list`/`message
    list` polling with a daemon-side fan-out — keep a per-account ring buffer of normalised events
    (from the same `events.NewMessage/MessageEdited/MessageDeleted/ChatAction/UserUpdate/MessageRead`
    handlers used for the webhook) with a monotonic cursor, and add a long-poll IPC endpoint
    (`GET /events?after=<cursor>&timeout=30`) so `watch` is push-driven and sees edits/deletes; note
    the daemon's own sends do not produce events (`_self_outgoing`).
16. **Login flows in the CLI/daemon**: add `tlgr account login --qr` (Telethon `qr_login()`, needs
    updates enabled and the connection alive while waiting; handle `SessionPasswordNeededError`),
    handle `next_type`/`timeout` with `auth.resendCode`, `PHONE_CODE_EXPIRED`, `UPDATE_APP_TO_LOGIN` →
    suggest QR, the login-email setup flow (`account.sendVerifyEmailCode`/`verifyEmail`), and never
    call `auth.signUp`. Store `future_auth_token`s (≤20) for password-less re-login (optional).
17. **Logout & session hygiene**: `account remove` should `auth.logOut` (+ optional
    `DestroyAuthKeyRequest`) so the entry disappears from Active Sessions, then delete the file; add
    `account sessions list|revoke <hash>|revoke-others|confirm <hash>|ttl <days>` on
    `account.getAuthorizations`, `account.resetAuthorization`, `auth.resetAuthorizations`,
    `account.changeAuthorizationSettings(confirmed=True)`, `account.setAuthorizationTTL`; watch
    `UpdateNewAuthorization(unconfirmed=True)` and emit a security event.
18. **Presence policy**: Telethon never calls `account.updateStatus`, so the account stays "offline"
    while the daemon runs; if a job should look online, call `account.updateStatus(offline=False)`
    periodically (config `online_update_period_ms`) and `offline=True` on shutdown — make it explicit
    config, default off.
19. **State save cadence**: Telethon saves pts/entities every 60 s and on `disconnect()`; ensure
    every shutdown path (SIGTERM, idle monitor, crash handler) awaits `client.disconnect()`; consider
    a shorter cadence by calling `client._save_states_and_entities()` after each job batch.
20. **Takeout for bulk export** (§8): implement `tlgr export`/`history --all` inside
    `client.takeout(...)` with `InvokeWithMessagesRangeRequest` over `messages.getSplitRanges`,
    handle `TakeoutInitDelayError` by asking the user to confirm on another device and retrying after
    `seconds`, and always `end_takeout(success)`.
21. **Transport/network options**: expose `proxy` (SOCKS5/HTTP via python-socks, MTProxy via
    `ConnectionTcpMTProxyRandomizedIntermediate`), `use_ipv6`, and connection class in config;
    handle transport `-429` (Telethon disconnects without reconnect → item 3 reconnect path with a
    longer back-off) and `-404` (item 3).
22. **Hash-based list caching** where volume matters: pass the documented `hash` to
    `contacts.getContacts`, `messages.getDialogs` (Telethon's `iter_dialogs` cannot), `help.getAppConfig`,
    `messages.getStickers`-style calls to get `*NotModified` on repeated CLI invocations.
23. **Test-DC mode** for CI: allow `session.set_dc(2, '149.154.167.40', 80)` + `99966XYYYY` numbers
    (code = X repeated 5 times) behind a `--test-dc` flag; never mix test/prod ids (`-444`).
24. **Clock sanity**: log Telethon's `time_offset` corrections; if `abs(offset) > 30 s` warn the
    user (msg_id window), since repeated `bad_msg_notification` 16/17 stalls sends.
25. **Security**: sessions are credentials — keep `chmod 600` (done), exclude from backups/logs,
    add `account export --string` via `StringSession.save()` only with explicit consent, and never
    print `auth_key`/`access_hash` values in debug output.
