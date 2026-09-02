# tlgr events

**Status:** partial — the envelope and the delivery guarantees are final; the
type vocabulary below is the **starter set** the foundation ships. The full
taxonomy is owned by the updates group and lands in PR-4.
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

## 3. The starter taxonomy

Nine types, chosen because they are what a watcher, a webhook and a gateway
rule need on day one. Every name is a lowercase `snake_case` noun-verb, and
new types are additive: a consumer must ignore a `type` it does not know.

| Type | When | Payload |
|---|---|---|
| `message_new` | a message arrives, in any chat the account can see | the full `Message` model |
| `message_edited` | a message is edited | the full `Message` model, post-edit |
| `message_deleted` | messages are deleted | `{"message_ids": [int, …]}` |
| `message_read` | read receipts move | `{"max_id": int, "outbox": bool}` |
| `chat_action` | a member joins, leaves, is promoted, the title changes… | `{"action": str, "user_id": int?, "user_ids": [int]}` |
| `user_status` | a user's online status changes | `{"user_id": int, "status": str, "online": bool}` |
| `reaction_changed` | reactions on a message change | *(reserved — emitted from PR-4)* |
| `draft_changed` | a draft is set or cleared elsewhere | *(reserved — emitted from PR-4)* |
| `daemon_health` | an account changes state, or the breaker opens | `{"state": str, "reason": str}` |

`chat_action.action` is currently the Telethon action class name
(`MessageActionChatAddUser`). PR-4 replaces it with a `snake_case` vocabulary;
consumers should treat it as an opaque string until then.

**What is deliberately absent.** tlgr does not invent a type name for an
update it has no taxonomy entry for. An unrecognised `Update*` is dropped
rather than delivered as `unknown`, because a type name that means "we did not
look" is worse than silence: it cannot be filtered on, and it will change
meaning the moment the real type is added.

---

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

## 6. Adding a type (for PR-4 and later)

1. Add the name to `EVENT_TYPES` in `tlgr/daemon/events.py`. Lowercase
   `snake_case`, noun then verb.
2. Map the Telethon event or raw `Update*` to it in `normalise()`. The
   function must stay pure and Telethon-free: it matches on the qualified
   class name so the bus can be unit-tested with a fake event.
3. Build the payload from **models**. If a model does not exist for the shape,
   add one to `tlgr/models/`; do not reach for `to_dict()`.
4. Add a row to the table in §3, and a test that the payload round-trips
   through `msgspec.json.encode` — which is the check that no `datetime` or
   `bytes` slipped in.
