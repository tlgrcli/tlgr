# Upgrading from tlgr 1.x to 2.0.0

This is the cutover for the case the release was actually written against: a
`pipx`-installed tlgr with a **running v1 daemon** holding live accounts, and
an agent scripting against its JSON.

Two facts shape everything below.

* **The daemon owns the session files.** Two processes with the same
  `~/.tlgr/accounts/<alias>/session.session` is how you get `database is
  locked` and, worse, `AUTH_KEY_DUPLICATED` — which Telegram answers by
  revoking the authorization. The v1 daemon must be **stopped**, not merely
  told to reload, before the new one starts.
* **No documented command path disappears.** Every v1 path is a
  `legacy_paths` alias on the operation that replaced it, and
  `tests/test_agentmd_compat.py` walks the whole list. Scripts keep working;
  what changes is the *shape* of six answers, listed under
  [Output changes](#output-changes-an-agent-must-adapt-to).

Budget ten minutes. Nothing here is reversible-by-accident, and
[Rollback](#rollback) is one command plus a restart.

---

## 1. Stop the v1 daemon

Run this from the **v1 install**, before you upgrade anything:

```bash
tlgr daemon stop
```

If a service manager will restart it, stop that first — otherwise the daemon
comes back under the old code between your two commands:

```bash
# macOS (launchd)
launchctl bootout gui/$(id -u)/dev.tlgr.daemon 2>/dev/null || true

# Linux (systemd --user)
systemctl --user stop tlgr.service
systemctl --user disable tlgr.service
```

Also stop anything of your own that restarts it — a cron entry, a watchdog
script, a supervisor. `tlgr daemon uninstall` removes the unit tlgr itself
installed; it does not know about yours.

### Verify it is actually gone

Two checks, and both must be clean. The first says no process is running:

```bash
pgrep -fl 'tlgr.daemon' || echo "no daemon process"
```

`tlgr.daemon.server` and `tlgr.daemon.main` are the same daemon under two
module names — a v1 plist names the first — so match on `tlgr.daemon` rather
than on either one.

The second says nothing still holds a session file, which is the check that
matters:

```bash
# macOS / Linux with lsof
lsof ~/.tlgr/accounts/*/session.session 2>/dev/null || echo "no open handles"

# Linux alternative
fuser -v ~/.tlgr/accounts/*/session.session 2>&1 | grep -v 'not found' || true
```

If either shows something, find it and stop it before going on. A stale
socket with no process behind it is harmless — `~/.tlgr/daemon.sock` is
removed on the next start — but an *open file handle* is not.

---

## 2. Upgrade the install

```bash
pipx upgrade tlgr        # or: pipx install --force tlgr
tlgr --version           # expect 2.0.0
```

`pip install -U tlgr` works the same way if that is how it was installed.

---

## 3. Clear the production marker

2.0.0 refuses to operate on a home carrying a `.production` marker unless it
is told the caller is the deployment. That guard exists because a development
checkout resolving to the same `~/.tlgr` as the installed tlgr is a live
deploy by accident: on 2026-09-03 exactly that bound the production socket
and held the production session files.

Pick **one**:

```bash
# (a) the home is no longer a marked deployment
rm ~/.tlgr/.production

# (b) keep the marker, and tell the deployed tlgr it is the one that may use it
export TLGR_ALLOW_PRODUCTION_HOME=1
```

Choose (b) if you develop against this machine. Set the variable **only** in
the environment of the installed tlgr — the service unit, or the shell that
runs the agent — never in your interactive shell, or the guard protects
nothing. `tlgr daemon install` writes the unit for you and is the easiest way
to get it into the right place.

---

## 4. First start

```bash
tlgr daemon start
tlgr daemon status --json
```

The first start reconnects every configured account and runs a catch-up. It
takes longer than a normal start; give it thirty seconds before concluding
anything.

---

## 5. Verification checklist

Run all five. The first two are about the daemon, the last three about the
accounts.

```bash
tlgr daemon status --json
# → ready: true, and one row per account under `accounts` with state "online"

tlgr agent whoami --json
# → output_schema_version: 2, the protocol, and the accounts it can see

for a in $(tlgr account list --results-only --select alias | tr -d '"[],'); do
  echo "== $a"
  tlgr -a "$a" chat list --limit 1 --json    # proves the session is usable
done

tlgr job list --json          # every job that was running should be here
tlgr agent parity --json | jq '.percent, .by_priority.P0.percent'
# → 99.5 and 100.0
```

`daemon status` reporting an account is **not** the same as the account
working — that was COR-37, and it is why the per-account `state` exists.
`chat list --limit 1` is the cheap call that proves it.

---

## 6. Configuration

Your `config.toml` is read unchanged; every key v1 had still works. Three
things are worth setting deliberately.

### `[defaults] parse_mode` — the one changed default

v1 defaulted to `md`, which silently ate `_`, `*` and backticks in ordinary
text (COR-21). 2.0.0 defaults to `none`.

```toml
[defaults]
parse_mode = "md"    # restore v1's behaviour if your scripts rely on it
```

Prefer passing `--parse md` on the sends that want markdown. It is explicit,
and it does not change what every other command does.

### `[defaults] legacy_dates` — a one-release bridge

2.0.0 emits RFC-3339 (`2026-09-02T09:14:07Z`) with a `*_unix` sibling on every
timestamp; v1 emitted `str(datetime)` (`2025-03-06 12:00:00+00:00`).

```toml
[defaults]
legacy_dates = true   # v1's spelling, for one minor release
```

Use it to buy time, not to stay. It goes in 2.1.

### New sections and their defaults

None of these need setting — the defaults are the shipped behaviour — but
they are where the new knobs live:

| Section | Keys | Default worth knowing |
|---|---|---|
| `[defaults]` | `output`, `require_account`, `parse_mode`, `legacy_dates`, `confirm_destructive`, `timezone` | `confirm_destructive = true`; off a TTY a destructive op needs `--yes` regardless |
| `[daemon]` | `idle_timeout`, `preconnect`, `event_buffer`, `event_workers`, `resync_depth`, `state_save_interval`, `drain_seconds` | `idle_timeout = 1800`; the daemon exits when idle and auto-starts on the next command |
| `[presence]` | `mode` | `off` — tlgr reports **no** presence unless asked. Always-online advertises a machine; reading while "offline" is the classic bot tell. `tlgr profile presence set` is the explicit command |
| `[flood]` / `[rate]` | `sleep_threshold`, `max_wait`, `persist`, per-class `rate`/`burst`/`new_peers_per_day` | flood waits are persisted across restarts, so a restart no longer forgets a wait |
| `[security]` | `require_token`, `peer_uid_check`, `warn_insecure_webhook` | `peer_uid_check = true`; the socket checks the connecting peer's uid on every request |
| `[policy]` | `allow`, `deny` | empty. Matched by **canonical operation id**, so `message.list` also permits the `msg list` alias (SEC-04) |
| `[identity]` | `device_model`, `system_version`, `lang_code`, `system_lang_code` | `lang_code` decides the language of *server-side* strings; `tlgr settings set language <code>` writes it |

Config keys gained a section prefix (`idle_timeout` → `daemon.idle_timeout`).
Both spellings are accepted by `config get`/`set`/`unset`.

```bash
tlgr config validate      # says what it does not understand, and where
tlgr config keys --json   # every key with its type, default and scope
```

---

## Output changes an agent must adapt to

Six shapes changed. Everything else v1's `AGENT.md` documented is unchanged,
and `tests/test_agentmd_compat.py` asserts it key by key. The full table is in
`CHANGELOG.md`; these are the ones a script hits.

**`--results-only` prints the inner value in every case, which is v1's
shape.** If you add nothing else to your scripts, add that flag.

1. **The envelope.** `--json` prints
   `{"ok": true, "op": …, "result": …, "page": {…}, "meta": {…}}`, and a
   failure prints `{"ok": false, "error": {…}}` **on stdout** with a one-line
   summary on stderr. v1's flat `{"error","code","exit_code"}` is now the
   `error` object inside it.
   *Adapt:* `--results-only`, or read `.result` / `.error`.

2. **Lists are pages.** `{"messages": […]}`, `{"drafts": […]}`,
   `{"chats": […]}`, `{"contacts": […]}`, `{"jobs": […]}` and the rest are
   `{"items": […], "has_more": …, "next_cursor": …, "total": …}`.
   *Adapt:* `--results-only | jq '.items'`. An empty page is `[]`, not `{}` —
   that was a bug, and `for row in result` used to iterate dict keys.

3. **Timestamps are RFC-3339, with a `*_unix` sibling.**
   *Adapt:* parse the ISO string, or read `date_unix`. `legacy_dates = true`
   buys one release.

4. **Ids are marked.** `draft list` and `chat get` return `-100…123` for a
   channel rather than the raw `123`, with `raw_id` beside it. The raw id was
   ambiguous between a user and a channel (COR-10).
   *Adapt:* `--select raw_id` for the old value.

5. **A dialog names its peer under `chat`.** `chat list` rows moved `id`,
   `name`, `type` and `username` into a nested `chat` object
   (`chat.id`, `chat.title`, `chat.kind`, `chat.username`).
   *Adapt:* `--select chat.id,unread_count`.

6. **A policy-blocked command exits 6, not 2**, and `--enable-commands` is
   matched by canonical operation id.
   *Adapt:* treat 6 as PERMISSION_DENIED. `tlgr agent exit-codes --json` is
   the full table.

`tlgr agent whoami --json` reports `output_schema_version: 2` — branch on that
rather than probing for each change.

Two behaviours that were *wrong* in v1 now answer differently, and no flag
restores them because the old answers were not correct:

* `profile get` reports the real bio. v1 never fetched `users.getFullUser`
  and reported `""` for every account.
* `chat mute --for 8h` writes an absolute wall-clock timestamp. v1 computed
  it from the asyncio event loop's clock, whose origin is arbitrary, so the
  mute landed in 1970 and did nothing.

---

## Rollback

The session files, `config.toml`, `jobs.yaml` and the account registry are
compatible in both directions — 2.0.0 reads and writes what 1.x did. Rolling
back is reinstalling and restarting:

```bash
tlgr daemon stop                      # from 2.0.0
pgrep -fl 'tlgr.daemon' || echo ok    # confirm it is down
pipx install --force 'tlgr<2'         # or: pip install 'tlgr<2'
tlgr daemon start
tlgr daemon status
```

Two things do not roll back on their own:

* **`~/.tlgr/.production`**, if you removed it in step 3. Recreate it with
  `touch ~/.tlgr/.production` — 1.x ignores the file, and you will want it
  back when you upgrade again.
* **The service unit**, if you disabled it. Re-enable it
  (`systemctl --user enable --now tlgr.service`, or
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.tlgr.daemon.plist`).

Anything written by a 2.0.0 command — a message sent, a gift converted, a
privacy rule rewritten — is server-side and is not undone by downgrading.
