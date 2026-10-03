# Job actions: one execution model

Status: implemented (feat/job-actions). Reference docs: `tlgr/gateway/README.md`
and `tlgr/actions/README.md`. This note records the choices the spec left
open, and why.

## Shape

A gateway job no longer runs actions in its bus handler. The handler
evaluates filters, rolls `percent`, calls the action's `plan()` (processors,
emoji pick, nothing awaited) and queues `PendingItem`s on the account's
`ActionScheduler` (`gateway/scheduler.py`). The scheduler owns delays,
pacing, quiet hours, expiry, takeover, retries and persistence, and calls the
action's `execute()`, which runs operations through `dispatch.execute` via
`DaemonOpRunner` (`gateway/executor.py`). One scheduler per account, shared by
every job on it; one worker and one `Pacer` per action kind.

Every built-in action, `forward` and `reply` included, goes through this
path. A plain function registered with `@register_action` still runs at once,
outside the scheduler, so third-party actions written against the old
interface keep working.

## Decisions

### Pacing and jitter

* `every` is a floor. The gap is drawn from `[every, 1.5 x every]`, a mean of
  1.25x and about +-20% around it. A window centred on `every` (the spec's
  "about +-25%") would put reactions 3 s apart under a 4 s rule; a floor
  keeps the promise "never closer than `every`".
* `per_hour` is a rolling hour of consumed slots. An absent `per_hour` in
  `pacing:` keeps the default cap (300 for react), `per_hour: null` lifts it.
* Pacer state (the rolling hour, the slow-down) is not persisted; a restart
  starts the hour afresh. The server-side flood memory (`flood.json`) still
  survives, which is the part that matters.
* Due times are wall-clock, sleeps are monotonic, so a worker never sleeps
  more than 60 s before looking at the clock again; a delay that spans a Mac
  sleep fires at most a minute late.
* A request cancelled by Telethon (it cancels everything in flight when the
  client disconnects) is a transient failure of that action, never the end of
  the worker; a worker that ends anyway is restarted.
* Dry-run items skip the pacer and presence: they never touch Telegram, so
  they should not slow the real actions on the same account.

### Failures

Classification goes through `core.errors.classify`, the same table the CLI
uses.

* `RATE_LIMITED` (FLOOD_WAIT, slow mode): a job op sleeps a wait of up to
  10 s inside the request (`flood_wait_max=10`); a longer one comes back and
  the item is rescheduled at `now + wait + 1..5 s`. The kind's pacer is held
  past the wait and its spacing doubles (up to 8x), recovering after ten
  minutes without another flood. An item gives up after 10 floods (an error).
  Floods are counted apart from transient failures, so one does not use up
  the other's retries.
* Retryable (`RETRYABLE`: network, timeouts, server errors, disconnected):
  three retries after about 5 s, 30 s and 2 min (each x1-1.5). An action
  that never expires (forward, read) then keeps retrying every ten minutes,
  up to 12 attempts in all (about 1.5 h), so a relay rides out an account
  that is reconnecting instead of dropping posts.
* Everything else is permanent and counted under `errors` with `last_error`
  (`USAGE: REACTION_INVALID: ...`): REACTION_INVALID, MESSAGE_ID_INVALID,
  CHAT_WRITE_FORBIDDEN, policy denials, PEER_FLOOD and frozen accounts. The
  last two are deliberately not retried: the account has been told to stop.

### Persistence

* A JSON file per account, `accounts/<alias>/pending.json`, written with
  `write_private` (temp file, chmod 0600, fsync, rename). Chosen over SQLite
  because the queue is small, it matches `flood.json` beside it, and it can be
  read with `jq` when diagnosing. Writes are debounced (1 s) and forced at
  shutdown.
* Bounded at 10 000 items per account; past that a new item is refused and
  counted as an error, logged once.
* Items carry everything needed to run without the Telethon event: chat,
  message, peer kind, forum topic, `grouped_id`, presence, quiet hours,
  takeover mode, dry run, and the payload (emoji, processed text, forward
  destination, view mode).
* The file also keeps the keys of the last 5 000 finished items. A key is
  `job|index|event|chat|msg|extra` (the edit time for an edit event, the
  destination for a forward), so an update replayed by catch-up after a crash
  (the update state is saved once a minute) is not acted on twice. This also
  protects forwards, which could duplicate before.
* Resume happens once per scheduler, when its account's jobs are created at
  boot. Items whose job no longer exists (or is disabled) are dropped and
  logged. An item that was running when the daemon died runs again
  (at-least-once); a clean shutdown stops starting new actions, lets a
  running one finish within the drain, then saves.
* `job remove` drops the job's pending items and counters; `job disable`
  drops its pending items (counted as superseded). A job updated by
  `job reload` keeps its queue.

### Expiry

React, view and reply expire 24 h after the event arrived; read and forward
never. `pacing.<alias>.expire` overrides per kind with a duration, or
`never`/`null`/`0`.

### react

* Custom emoji are supported as `custom:<document id>`, the spelling
  `reaction add` already uses.
* Album target: the message carrying the caption, else the first one. That is
  where Telegram Desktop attaches an album's reactions
  (`HistoryView::GroupedMedia::itemForText()`: the caption item, else the
  first part) and what Telegram for Android uses
  (`MessageObject.GroupedMessages.findPrimaryMessageObject()`). If the
  caption message arrives after the first one while the reaction is still
  pending, the pending reaction is moved to it.
* An album is one `percent` roll, not one per photo. Album siblings count as
  `skipped`.
* React implies read through `ensure_read`: paced on the read queue, read up
  to the reacted message, and any pending read for that chat at or below it
  finishes as done (coalesced). If the chat is already known read that far
  (by tlgr or by you elsewhere) no read is sent. If the read fails the
  reaction does not go out.

### reply

* One reply per album as well (to the caption message), the same rule as
  react. Replying once per photo was never useful in a DM, which is the
  main use case.
* Typing is a two-step item: `chat.typing` is started in the background and
  the item is re-queued for when typing ends, then `message.send` goes out.
  The reply queue is never blocked for the typing time.
* Text is sent as markdown, which is what `event.reply()` did with the
  client's default parse mode.

### forward

* One pending item per destination, so one refusing destination does not
  stop the others and each is retried or counted on its own.
* With processors: a photo or document is re-sent with `media.upload
  --from-message` and the processed caption; a link-preview post is re-sent
  as text (Telegram regenerates the preview), where the old code failed on
  it; other media with text is re-sent as text, without text it is skipped.
  Text is taken as `message.text` (markdown under the client's parse mode)
  and sent with `--parse md`, so formatting survives as it did.
* Albums are still forwarded message by message, as before; grouping them
  would need a hold window and changes what the destination sees.

### read

Reading time is 250 words a minute plus 3 s for media, capped at 60 s, added
to the `delay` draw.

### view

A broadcast channel post (the `post` flag, or a broadcast chat entity) is a
view. In private chats and groups only voice and round notes are something
to view; view-once media needs `include_view_once: true`.

### Presence

* The rule for two jobs with different modes on one account: the most online
  request wins while its actions run. `leave` never sends anything and never
  ends an online stretch another job started; offline is sent only once no
  `blip`/`session` action is running and, for `session`, none is due within
  5 s. Linger after the last action: 5 s.
* `session` goes online when its first action executes, not ahead of it.
* If the account-level `[presence] mode` is not `off`, the daemon already
  manages the status and job presence is silent.
* Quiet hours use `[defaults] timezone` (an IANA name) when set, else the
  daemon's local time. Held items are released at the end of the window,
  spread over five minutes, then go through the pacer.

### Manual takeover

* A read is tlgr's own when its `max_id` is at or below the highest id tlgr
  asked to read in that chat; the mark is set before the RPC, because the
  echo can arrive before the answer.
* An outgoing message is tlgr's own when its id is one tlgr recorded sending,
  or when tlgr sent in that chat less than 10 s earlier.
* A read that arrives within 10 s of tlgr sending in that chat is also tlgr's
  own: sending marks the chat read on the server.
* Takeover drops items in that chat with a message id up to the read or sent
  id, by each item's own `on_takeover`; forwards are never dropped. A read in
  a forum topic only drops items of that topic (forum ids span the chat).
* `job queue cancel` counts what it drops as `superseded` too.

### Filters

* `sender_is_contact` trusts the `contact` flag on the sender entity the
  update carried; without one it fetches the sender and caches the answer for
  10 minutes.
* `chat_is_new` probes the history once per chat (`get_messages(limit=1,
  offset_id=msg)`) and caches the first message id (or "not new"), in an LRU
  of 5 000 chats per process.
* Both are coroutines, so filter evaluation gained `evaluate_async`; the
  synchronous `evaluate` (webhook filters) rejects them with a reason.

### Validation and the CLI

* Validation is strict per job: unknown keys, bad durations, percent outside
  0-100, unknown presence or takeover modes, unknown actions, processors on
  an action that cannot use them. At load a broken job is skipped and logged
  while the others run; on `job reload` a job whose edit broke it keeps
  running in its last good form.
* `tlgr job queue` lists and `tlgr job queue cancel` cancels. The registry
  forbids an op id that is also a group, so the list op is `job.queue.list`
  tagged `group-default`, and its CLI group runs it when called bare. Options
  go on the leaf (`tlgr job queue list --job dm-ack`).
* `job.queue.cancel` is destructive as a whole, so every variant needs
  `--yes` off a terminal, like `job remove`.
* `job.get` moved from the local surface to the daemon so it can report live
  counters.
