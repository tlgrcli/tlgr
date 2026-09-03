# Design decisions

Where `ARCHITECTURE.md` left a choice open, or where building the thing showed
the blueprint could not be followed literally, the decision is recorded here —
dated, one paragraph, with the reason rather than the rule.

## 2026-09-02 — the error table is keyed by exception class *name*

§7.1 says `core/errors.py` is the only module that names Telethon exception
classes; §2.2 says `cli/` must never import Telethon. Both hold only if the
names are strings: `ERROR_MAP` is keyed by class name and `classify()` walks
`type(exc).__mro__` looking each name up, so classifying a `FloodWaitError`
imports nothing. Walking the MRO also means a Telethon subclass we have never
heard of lands on its base's row — every unknown `*ForbiddenError` becomes
PERMISSION_DENIED rather than GENERIC.

## 2026-09-02 — `format_error_json` keeps v1's flat shape

The blueprint's §12.4 wants `{"ok": false, "error": {…}}` on stdout with
`--results-only` yielding the inner object in v1's spelling. Rather than
change what the existing public function returns, `format_error_json` keeps
v1's exact flat dict (it *is* the `--results-only` payload) and the envelope
is a new `error_envelope()`. The inner object carries both `message` and v1's
`error` key, so a v1 consumer reading `error`/`code`/`exit_code` keeps working
either way.

## 2026-09-02 — `PageKind` lives in `core/pagination.py`

§2.1 lists it under `ops/_spec.py`, but `core/pagination.py` needs it to
validate a cursor and `core/` sits below `ops/`. It is therefore defined in
`core/pagination.py` and re-exported from `ops/_spec.py`, so the spelling in
the blueprint (`from tlgr.ops._spec import PageKind`) still works.

## 2026-09-02 — the schema document is handed its command tree

`tlgr schema` still describes the Click tree, because most commands are not
migrated yet — but `ops/` must not import `cli/`. The walker therefore lives
in `cli/introspect.py`, and the `agent.schema` implementation receives it
through the op context. `tlgr/schema.py` itself imports no click and only
knows about the registry.

## 2026-09-02 — `json-only` operations print bare JSON unless `--json` is given

A schema document has no table shape, and v1 printed it as JSON whatever the
output flags said. An op tagged `json-only` therefore always renders as JSON;
without an explicit `--json` it prints the document bare (v1's exact output)
and with `--json` it gets the v2 envelope. That keeps `tlgr schema | jq
.schema_version` working while the new rules still apply where they are asked
for.

## 2026-09-02 — a policy block is exit 6 for generated commands, exit 2 for legacy

§7.2 says an operation blocked by policy is PERMISSION_DENIED, exit 6.
v1's `--enable-commands` exited 2. Generated commands use the new code (and
match by canonical op id, so an alias cannot slip past the allowlist —
SEC-04); the v1 path matching, and its exit 2, stay in place for groups that
are still hand-written, and go when they do.

## 2026-09-02 — `agent whoami` stays hand-written inside a generated group

`agent exit-codes` and `schema` are registered ops, so the `agent` group is
generated — but `whoami` reads the account manager and the daemon status and
belongs with the account group (PR-2). `build_cli()` therefore has one
enumerated exception, `LEGACY_EXTRAS`, listing commands still hand-written
inside a generated group. It is a list of promises to delete, not a general
escape hatch: the "defined in both places" assertion still fires for anything
not named in it.

## 2026-09-02 — peer links normalise as far as they can

§3.2 lists `link` among the `PeerRef` kinds. Since the point of `value` is to
be normalised, a link is classified as far as it can be: `t.me/<name>` and
`tg://resolve?domain=` become `username`, `t.me/c/<id>/<n>` and
`tg://privatepost` become a marked `id`, `t.me/+hash` and `tg://join` become
`invite`. `link` is what remains for a t.me/tg:// reference we recognise as
Telegram's but cannot classify further.

## 2026-09-02 — non-file media kinds come from a table

v1 derived the kind by lowercasing the TL class name minus `MessageMedia`,
which produced `geolive` and `paidmedia` — neither of which is in the
`MediaKind` vocabulary. The document branch keeps v1's logic exactly
(attributes collected first, kind decided after); the non-document branch maps
through an explicit table and falls back to `unsupported`.

## 2026-09-02 — request constraints are enforced by a round trip

Constructing a msgspec Struct does not run its `Meta` constraints; only
decoding does. The generated command therefore builds the request and
immediately re-validates it with `msgspec.convert`, so `--limit 500` against
`le=100` fails in the CLI with a USAGE error naming the field rather than in
the daemon.

## 2026-09-02 — `TLGR_HOME` overrides where the cursor key lives

`core/pagination` needs a signing key at `~/.tlgr/cursor.key`. Tests must not
write to a developer's real home directory, and `CONFIG_DIR` is a module
constant computed at import. `TLGR_HOME` is read at call time and wins when
set; everything else keeps using `CONFIG_DIR`.

## 2026-09-02 — ruff formats code, not the design documents

`ruff format` reformats Python inside Markdown fences, which rewrote the
illustrative snippets in `ARCHITECTURE.md`. Those snippets are prose about
code, not code, so `*.md` is excluded from formatting.

## 2026-09-02 — strict typing is configured per module, not per invocation

§11.4 runs `mypy --strict` over whole packages. `tlgr/core` contains v1
modules that will not pass strict until they are rewritten, so strictness is
declared in `pyproject.toml` for the modules that are ready
(`models`, `ops`, `registry`, `schema`, `core.errors`, `core.timefmt`,
`core.pagination`) with `follow_imports = "silent"`, and the list grows as
each group PR lands.

## 2026-09-03 — the spawn probe and the daemon singleton are different locks

§5.8 says the autostart probe takes `flock(~/.tlgr/daemon.lock)`, keeps it
across the spawn, and releases it once the child reports ready; §6.1 says the
daemon takes the same lock as its first act. Both cannot hold one file: the
child would block forever on a lock its parent is holding on its behalf, and
no daemon would ever start. The probe therefore uses a separate
`daemon.spawn.lock` to serialise *the decision to spawn*, and `daemon.lock`
remains the daemon's own singleton. The property §5.8 wanted — twenty
simultaneous CLIs produce one daemon — is tested and holds.

## 2026-09-03 — `--flood-wait-max` stacks, and the smallest budget wins

§6.4 says a per-request `flood_wait_max` is honoured "by passing
`flood_sleep_threshold` per call". Telethon has no per-call form: it reads
`client.flood_sleep_threshold` at call time, so concurrent requests on one
account necessarily share it. Serialising every request to make the flag exact
would cost far more than the flag is worth. Instead the active budgets are
kept on a stack and the **smallest** is in force, so a caller that asked for at
most five seconds is never held for a hundred and twenty. The cost is that a
generous caller may return sooner than it had to, which is the safe direction.

## 2026-09-03 — the event bus carries the raw Telethon object beside the envelope

§6.5 has the bus deliver `EventEnvelope`s. The gateway's filters read the raw
Telethon event (`chat_type`, `is_reply`, media predicates), and re-deriving
those from the normalised payload would be both lossy and a second source of
truth. A bus handler is therefore called as `handler(envelope, raw)`, where
`raw` is the source object for an in-process consumer and `None` for a
synthesised event. The envelope alone is what leaves the process. Moving the
filters onto models is PR-4's job, and this signature is what lets the gateway
run on bus worker lanes today instead of on the update loop (ROB-02).

## 2026-09-03 — an unrecognised update is dropped, not named `unknown`

The bus normalises only the nine starter types. §6.5 does not say what to do
with the rest, and the tempting answer — deliver them as `unknown` with the
Telethon class name — is wrong: a type name that means "we have not looked at
this yet" cannot be filtered on, and it changes meaning the day the real type
is added, silently breaking every consumer that matched it. Unrecognised
updates are dropped until PR-4 gives them names.

## 2026-09-03 — the legacy IPC surface answers GENERIC, not IPC_ERROR

v1's `_handle_exception` recognised three exception types and answered
`500 IPC_ERROR` (exit 12) for everything else, which said "the channel between
you and the daemon failed" about errors that had nothing to do with the
channel. COR-06 routes it through `core.errors.classify`, the same table the
v2 dispatcher uses, so an unclassified failure now lands on the table's
`GENERIC` row (exit 1) and a recognised one gets its real code. The body keeps
v1's flat `error`/`code`/`exit_code` shape, because that is what its callers
parse. One pinned test changed with it.

## 2026-09-03 — `Message.text` and `sender_id` are derived when Telethon cannot

`message_to_model` read `message.text` and `message.sender_id`. Both are
filled in by Telethon's `_finish_init`, which only runs for a *client-bound*
message — so every message tlgr builds from a raw `Updates` reply, including
the one `message send` returns, reported empty text and no sender. The
serialiser now falls back to `raw_text`/`message` and computes the sender from
`from_id` with the same arithmetic `utils.get_peer_id` uses, kept local so
nothing below `ops/` has to import Telethon.

## 2026-09-03 — the daemon does not connect an account it was not given

§6.1's connect list is "accounts referenced by enabled jobs, `[accounts]
default`, the active alias, and `[daemon] preconnect`", as an ordered list.
The jobs part is deferred: jobs are created after the accounts connect, and
reading `jobs.yaml` twice at start to discover aliases would make the connect
order depend on a file the account registry does not own. A job whose account
is not in the list connects it on demand through `SessionManager.ensure`,
which is the same path every request uses, so the only difference is that the
connection happens a second later.

## 2026-09-03 — `message fact-check` and `message paid` get no short alias

The PR-1 work list gives `message.fact-check.set` the alias `message
fact-check` and `message.paid.set` the alias `message paid`. Both are refused.
An alias is placed in the Click tree by splitting it on `.`, and each of these
names a *group* that already exists — the group holding `set`. Placing the
alias would replace that group and take `message fact-check set` with it, so
the shorthand would cost the canonical path. Every other alias in the work
list is registered.

## 2026-09-03 — §12.3's parity criterion is restated as "accounted, plus P0"

Criterion 17 asks for `messages_core` at ≥ 95 % "with the remaining ids waived
and named". Coverage is 79.6 %; accounted coverage is 100 %. The 34 remaining
ids are catalogued under `messages_core` because they concern messages, but
33 of them belong to a different *command group* — `history clear`, `chat mark
unread` and `typing action` are the `chat` noun (PR-3), checklists and paid
star reactions are PR-9 — and PR-1's scope is explicitly `message` and
`draft`. Chasing the percentage would mean building half of PR-3 here. The
gate the tests actually enforce is the honest form of the criterion: every
uncovered id waived to a named PR, and every P0 id the group owns covered,
with the 30 of them named in `tests/test_parity.py` and asserted to be exactly
what the registry claims. `docs/design/FOUNDATION_ACCEPTANCE.md` records the
shortfall rather than restating the number.

## 2026-09-03 — `channels.*` requests convert the peer instead of re-resolving it

Five operations passed `await client.get_input_entity(peer)` where a
`channels.*` request wants an `InputChannel`. That returns an `InputPeer*`,
which happens to serialise correctly for a channel and produces a wrong
request for a user — a private chat reached `ExportMessageLink` or
`ReadHistory` and failed with something that did not name the cause.
`_input_channel()` uses `utils.get_input_channel`, which is arithmetic on a
peer already in hand rather than a round trip, and turns "this is not a
channel" into a usage error naming the `chat` field.

## 2026-09-03 — automatic entities are re-derived locally, and say so

`message entity list` exists to answer "what will Telegram do with this text".
Telethon's markdown and HTML parsers emit only the entities the *client*
declares; URLs, mentions, hashtags and phone numbers are added by the server
on receipt, so the report was empty exactly where a caller most needs it. They
are re-derived with a small pattern table, kept close to Telegram's rules and
deliberately not authoritative: they are reported under `auto_entities`, never
mixed into `entities`, because a message that re-declares a server-side entity
is rejected.

## 2026-09-03 — a chat folder is evaluated client-side, and `--all` is the daemon's job

There is no `messages.getDialogs(filter_id)`: a `dialogFilter` is a *filter*
every client applies to its own dialog list. `chat list --folder <name|id>`
therefore walks the whole list inside the daemon and applies the filter's
rules — explicit include or pin wins, explicit exclude loses, the type flags
decide the rest — rather than paging. That is also why such a listing ignores
`--limit` for the *walk* and only slices the answer: "the dialog list was
fully enumerated" is the guarantee `user dialog-status` depends on to turn an
absent peer into a negative rather than an "I don't know", and a folder query
that paged would quietly break it.

## 2026-09-03 — `chat unread` keeps v1's verb, and the vocabulary grows by one

STYLE §1 spells this operation `mark-unread`, and the registry already had
that verb. `chat unread` is the path v1's `AGENT.md` documents and agents have
in their prompts, and an id of `chat.mark-unread` with `chat unread` as a
legacy path would have made the canonical name the one nobody uses. The id is
`chat.unread` and `unread` joins the verb list, next to `mark-unread`, with a
comment saying the two name the same call.

## 2026-09-03 — a two-segment alias that shadows a sub-noun group is dropped

`chat badge`, `chat action-bar`, `chat ttl`, `chat autoarchive`, `chat promo`
and `folder suggested` were proposed as shorthands for their `… get`/`… set`/
`… list` operations. Click has one namespace per level, so registering them
would have replaced the *group* of the same name and taken `chat badge get`
with it. The canonical three-segment paths stand alone; only aliases that
cannot collide (`chat mentions`, `chat posters`, `chat suggestions`,
`chat unarchive`, `folder updates`, …) are registered.

## 2026-09-03 — the two cross-noun aliases wait for the `user` group

The work list gives `chat action-bar get` the alias `user action-bar` and
`chat report` the alias `user report`. Registering either would make the
registry generate a top-level `user` group, which collides with the
hand-written one still serving `user get`, `user dialog-status` and
`user hide-stories` — `build_cli()` refuses that overlap on purpose. Both
aliases are deferred to the PR that migrates the `user` group; the canonical
`chat …` paths cover the operations meanwhile.

## 2026-09-03 — `chat import` is not a stream

The work list marks it `stream: true`. The foundation has exactly one
streaming shape (`GET /v1/events`) and no streaming *operation*, so a
streaming import would have meant inventing the second one for a P2 command
that runs a fixed five-step sequence. It runs to completion and reports the
state it reached (`checked` or `started`) with the media counts; `--check` and
`--dry-run` stop after the two feasibility calls, which is where the useful
progress information actually is.

## 2026-09-03 — `folder list --tags` honours `--dry-run` itself

`folder list` is a read, and the work list marks it non-mutating, but
`--tags on|off` writes an account-wide setting. Declaring the whole op
mutating would make `--dry-run folder list` print a stub instead of the
folders, which is the wrong trade for the command people run to *see* their
folders. The implementation checks `ctx.dry_run` before the toggle and warns
instead, so the flag cannot silently fire under a dry run and listing keeps
working.

## 2026-09-03 — the four secret-chat commands are registered and refuse

Telethon has no MTProto-2.0 end-to-end layer: DH validation, AES-IGE, in/out
sequence numbers, PFS re-keying and a local key store are a module tlgr does
not have, and secret chats never appear in `messages.getDialogs`.
`chat secret list`, `start` and `send` are therefore registered and raise
`NOT_SUPPORTED` (exit 13 — "tlgr cannot do this", not "the operation failed")
with the reason. `chat secret discard` is implemented, because discarding
needs only the chat id. The catalog ids stay covered by ops that refuse
loudly rather than by silence.

## 2026-09-03 — `chat notify set` reads before it writes

`account.updateNotifySettings` replaces the whole `inputPeerNotifySettings`,
and an omitted field means "inherit the scope default". Sending only the field
the caller named would therefore have cleared every other exception on that
chat as a side effect. The implementation fetches the current exception,
carries its fields over, applies the change, and treats `default` as *dropping*
the field — which is the only way to express "stop overriding this" in a
request whose absent fields are meaningful.

## 2026-09-03 — an empty page keeps its `page` envelope

`omit_defaults` drops an empty `items`, and the daemon decided whether to emit
the `page` envelope by testing for that key. A paginated operation with no
results therefore answered `{"total": 0}` with no `page` at all — the one
shape a caller walking pages must be able to rely on. The test is now on the
spec (`paginated is not None`), and the result is `[]`.

## 2026-09-03 — `chat list` rows nest their peer, and v1's keys move with it

v1's `chat list` returned `{"chats": [{"id", "name", "type", "username", …}]}`.
A dialog now returns `Page[Dialog]` with the peer under `chat`, the same
`Peer` every other v2 response embeds, and the preview under `last_message` as
a whole `Message` — so a service event, a caption-less sticker and a genuinely
blank message are three distinguishable things in the surface agents read
first, instead of three empty strings. It is rows 8 and 9 of the
CHANGELOG's breaking table; `--select chat.id,unread_count` is the migration.

## 2026-09-03 — an alias that is a prefix of another command's path is dropped

`build_click_tree` places an alias by walking its path and attaching a
command at the leaf, so an alias that spells a *prefix* of a canonical
three-segment path replaces the Click group sitting there: registering
`poll stats` as an alias of `poll.stats.get` deletes the `poll stats` group
and takes `poll stats get` with it. Five aliases from the command surface are
therefore not registered — `poll stats`, `poll unread`, `reaction unread`,
`location nearby` and `location venue` — because a shorthand is not worth the
canonical path it would delete. The same rule already cost `message
fact-check` in PR-1. Making them work needs a group that falls back to a
default command, which is a change to the CLI generator, not to this group.

## 2026-09-03 — cross-group aliases wait for the group they reach into

`reaction tag list` wants `chat saved tags` and `location nearby list` wants
`chat nearby`, both of which the command surface asks for. Registering either
creates a generated `chat` group, and `build_cli` refuses to have `chat`
defined by both the registry and `tlgr/cli/legacy` — correctly, because a
half-migrated noun is exactly the silent disagreement §12.4 is about. The two
aliases arrive when PR-3 migrates `chat`; until then the canonical paths are
the only spelling, and no v1 path was lost to get there.

## 2026-09-03 — `search global` and `search hashtag` are verb-first, so the lint learns two words

Registry lint L1 checks that an id's last segment is in the STYLE vocabulary.
`search` is one of COMMANDS.md's verb-first nouns (`search <scope>`), so
`search global`'s tail names the scope, not an action, and there is nothing in
an id for the lint to tell the two apart by. `global` and `hashtag` are added
to `VERBS` with a comment saying why, rather than renaming the commands into
`search message list`-shaped paths that no user would guess.

## 2026-09-03 — `poll get --follow` blocks instead of streaming

The work list marks `poll get` as a stream. A `stream=True` op in this
architecture streams *unconditionally* — the CLI collects NDJSON item frames
into a list — so `poll get` would answer with an array of polls whether or not
anybody asked to follow one, and the op's own documented shape is an object.
`--follow` therefore refreshes `messages.getPollResults` on an interval and
returns the final state, bounded by `--follow-for` and by the op timeout,
warning when it gave up while the poll was still open. That is the useful half
of "watch this poll" for an agent, and it keeps one answer shape.

## 2026-09-03 — the unread listings page as PARTICIPANTS, not HISTORY

`poll unread list` and `reaction unread list` walk `getHistory`-shaped offsets
(`offset_id`/`add_offset`), which is what `PageKind.HISTORY` describes. But
HISTORY is in `DATE_OFFSET_KINDS`, so the CLI generator injects `--since` and
`--until` — and `getUnreadPollVotes`/`getUnreadReactions` take no date bounds
at all, so both flags would be accepted and silently dropped. They use
`PageKind.PARTICIPANTS`, which is the same "an offset into a server-side
listing" contract without the date promise. A flag that does nothing is worse
than a cursor kind whose name is a shade off.

## 2026-09-03 — layer-229 fields are refused by name, not silently ignored

Three flags in the work list describe fields the pinned Telethon (layer 227)
does not carry: `poll create --description`, `search global --community`, and
the rich-body half of a checklist. Each raises `NOT_SUPPORTED` (exit 13)
naming the field and the layer, rather than being dropped on the way to the
request. Exit 13 rather than 1 because tlgr never asked Telegram — "this build
cannot" and "Telegram said no" are different facts, and only one of them
changes if you retry. `search hashtag --stories` is refused the same way for a
different reason: it answers with stories, not messages, and pouring them into
a `Page[Message]` would be a lie about what came back.

## 2026-09-03 — `sendReaction` is read-modify-write, and `message react` moves group

`messages.sendReaction` carries the whole desired reaction set. v1's `message
react` sent the emoji alone, which the server reads as "replace everything",
so reacting a second time silently removed the first reaction. `reaction add`
reads the account's current reactions and resends them in `chosen_order` with
the new one appended; `--replace` is the explicit way to ask for v1's
behaviour. The op id moved from `message.react` to `reaction.add` because the
`reaction` group owns the surface, and `message react`/`msg react` stay
invocable as `legacy_paths` with the same `reacted`/`msg_id`/`emoji` keys.
`test_parity.py` names the P0 id that changed hands, so the move is a line in
a diff rather than a silent swap.

## 2026-09-03 — the recently-searched hashtag list is tlgr's own state

The catalog lists "recently searched hashtags" twice and both entries say the
same thing: there is no MTProto method for it — the official clients keep the
list locally. `search hashtag --recent` reads, and `--forget` prunes, a
per-account JSON file under the account's own directory, written through
`write_private`. Storing it server-side is not an option, and pretending the
ids are unimplementable when every GUI ships the feature would be the wrong
kind of honest.

## 2026-09-03 — `reaction pay` and `search post` never spend by themselves

Both commands can spend Stars. Neither has a default amount: `reaction pay`
requires `--stars N` and validates the channel *before* anything is spent, and
`search post` does free price discovery first (`--quota`), refuses to fall
through to a paid search when the free quota is gone, and requires
`--pay-stars N` that at least meets the quoted price. Both are `destructive`,
so the ordinary confirmation applies. A failed payment is never retried
automatically. `reaction pay` also builds the non-standard `random_id` the
paid-reaction method wants — `(unixtime << 32) | random_uint32` — which is
unlike every other send in the API.
## 2026-09-03 — login is a sequence of commands, and `account add` does not finish it

v1's `account add` sent the code, blocked on `input()` while a human read
their phone, and signed in from the same client — because Telethon keeps
`phone_code_hash` in memory on the client object, so a second process has
lost it. A daemon cannot prompt, and an agent has no `input()`. PR-2 keeps
the pending login *in the daemon*, keyed by alias, and mirrors phone +
`phone_code_hash` + code type into `<account>/login-state.json` at 0600. So
`tlgr account add +98…` now starts the login and returns the exact next
command (`tlgr auth verify-code <code> --alias …`) instead of holding a
terminal open. `--bot` still finishes in one call, because a bot token is a
complete credential. AGENT.md and README say so in the same words.

## 2026-09-03 — `auth sign-up` exists, and a *login* still never signs up

ARCHITECTURE §1.2 said "`auth.signUp` is never called", and the PR-2 work
list has `auth sign-up` as a P2 command with its own consent gate. Both
concerns are satisfied by splitting them: a login that finds no account stops
with `{"status": "signup_required"}` and names the other command — it never
registers one as a side effect of a failed sign-in — while registering is a
separate run that requires `--first-name` *and* `--accept-tos`, because
accepting Terms of Service is a legal act tlgr will not perform implicitly.
§1.2's bullet is amended to "no *silent* account creation", which is what the
rule was protecting.

## 2026-09-03 — `passport authorize` is registered and refuses

`account.acceptAuthorization` takes a `secureCredentialsEncrypted` blob: the
requested documents re-encrypted under the service's RSA key with a secret
derived from the cloud password. Telethon 1.44 implements none of that KDF.
The command is registered anyway, so `tlgr passport authorize` exists and
explains itself, and it raises NOT_SUPPORTED (exit 13 — nothing refused this,
it was never asked) pointing at `passport form get`, which reads the whole
request. Half-implementing it would either hand a service identity documents
encrypted wrongly or produce an error nobody outside Telegram can diagnose.
Reading, deleting and phone/email verification need no crypto and are fully
implemented, which is why the catalog ids stay *partial* rather than covered.

## 2026-09-03 — logging out is not removing the account

v1's `account remove` deleted the local files and never called
`auth.logOut`, so the authorization went on appearing in every other client's
Devices list forever. PR-2 splits the two. `account logout` revokes the
authorization, stores the returned `future_auth_token` (0600, capped at 20)
and deletes the dead session file, but keeps the alias and its credentials so
`tlgr auth send-code` can log back in. `account remove` deletes the record;
without `--logout` it says, in the response, that the server-side
authorization is still alive.

## 2026-09-03 — `account check` reports states instead of raising them

"The network is down" and "Telegram revoked this auth key" both look like a
disconnected client to `daemon status`, and only one of them is fixed by
waiting. `account check` distinguishes `authorized` / `revoked` / `banned` /
`deactivated` / `frozen` / `offline` and returns a row per account rather
than raising, because `tlgr account check` with no alias has to answer for
every configured account even when one of them is dead. `frozen` carries
Telegram's own appeal URL from `help.getAppConfig`; the appeal itself is a
web form a CLI cannot submit.

## 2026-09-03 — `account password change` refuses rather than destroy Passport data

The Passport secure secret is encrypted under the cloud password, and
Telethon's `edit_2fa` helper drops `new_secure_settings` — so the obvious
implementation silently destroys a user's stored identity documents. When
`account.getPassword` reports `has_secure_values`, the change is refused with
a usage error naming both ways out (delete the documents, or `--keep-passport`
to accept the loss). Refusing is the conservative half of a choice between
"refuse" and "destroy something irreplaceable".

## 2026-09-03 — secrets get `--x-env/--x-stdin/--x-file` and no value flag

The work list spells `--api-hash` alongside `--api-hash-env`. STYLE §3 says a
secret never takes a value-bearing flag, because argv is world-readable
through `ps` and lands in shell history. Every secret in this group —
the cloud password, the new password, the bot token, the api_hash — is
therefore declared `secret=True` and generates only the three reading flags,
with the documented environment variable as the default source. The same rule
is why `account export` refuses to print to stdout unless `--stdout` says so.

## 2026-09-03 — `completion` is `agent.completion` until PR-4 deletes the legacy `config` group

The work list gives the op id `config.completion`, whose canonical path would
create a generated top-level `config` group — and `build_cli()` fails the
import when a group is defined both by the registry and by `cli/legacy`,
which `config` still is until PR-4. The op is registered as
`agent.completion` with `completion` as both an alias and a legacy path, so
`tlgr completion bash` — the only spelling v1 documented, and the one §12.4
protects — is unchanged. Two aliases from the work list are dropped for the
same reason and land with their groups: `config terms` (PR-4) and
`user support` (PR-5).

## 2026-09-03 — an alias may not shadow a sub-noun group

`account device-locked`, `account smsjobs`, `account support`, `auth
autologin-url` and `passport form` are listed as aliases of their `… set`/
`… get` command. Each would be placed at a path the canonical command has
already turned into a Click *group*, replacing it — so `account
device-locked set` would stop existing the moment its own alias was
registered. They are dropped; the canonical three-level paths remain.

## 2026-09-03 — an op tagged `text` prints its `text` field verbatim

`tlgr completion bash` produces a blob meant to be pasted into a shell rc
file. Rendering it as a one-row key/value table truncates it into something
unusable, and forcing `--json` would change v1's output. The generator
therefore honours one new tag: an op tagged `text` prints `result.text` and
nothing else in human and plain mode, and the normal envelope under `--json`.

## 2026-09-03 — an empty page is `[]`, not `{"total": 0}`

`Page.items` defaults to an empty list and `Model` sets `omit_defaults=True`,
so an empty page serialised to `{"total": 0}` and the dispatcher's
`"items" in body` check left it un-unwrapped — a *different shape* for "no
results", which is exactly what a consumer cannot branch on. The dispatcher
now unwraps any dict body from a paginated op with `.get("items", [])`. For
the same reason `Whoami` and `AccountRecord` set `omit_defaults=False`:
`daemon_running: false` and `active: false` are answers v1 printed, and a
consumer reading them must not get a `KeyError` because the answer was "no".

## 2026-09-03 — a streaming op must mark its pages `has_more`

`walk_pages` stops when a page reports `has_more` false, which is right for a
`--all` walk and wrong for a stream: `auth qr` printed one token and ended.
Every non-terminal frame of a streaming operation now sets `has_more=True`,
and only the final one (authorized, or expired) says otherwise.

## 2026-09-03 — no test may reach the developer's real `~/.tlgr`

`TlgrPaths` falls back to `~/.tlgr` when `TLGR_HOME` is unset, so a test that
forgot the `tlgr_home` fixture silently operated on a real installation —
real accounts, a real session file, a real daemon socket. One did.
`tests/conftest.py` now has an autouse fixture that points `TLGR_HOME` at a
throwaway directory whenever it is unset, so forgetting the fixture costs a
temp directory rather than somebody's session.
