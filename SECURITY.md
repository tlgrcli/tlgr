# Security

tlgr holds Telegram **session files**. A session file contains an `auth_key`,
which is full access to the account: reading one is equivalent to logging in
as you, with no code, no password and no notification you will notice. The
IPC socket is equivalent to the session files, because anything that can talk
to the daemon can ask it to do anything the account can do.

Everything below follows from that.

## Reporting a vulnerability

Open a private security advisory on the repository, or email the maintainer.
Please include the tlgr version (`tlgr --version`), the platform, and what an
attacker would gain. We aim to acknowledge within a week.

Do not open a public issue for anything that discloses session material.

## Threat model

**Assets.** Session files (`~/.tlgr/accounts/*/session.session`), API
credentials (`api_id`/`api_hash`), the IPC socket, message content, the
contact graph, webhook tokens and signing secrets, and 2FA passwords in
transit.

**In scope.** Other local uids. Unprivileged processes running as *you* — a
malicious `npm`/`pip` post-install script, a sandboxed tool with `$HOME`
access, an agent given a shell. Accidental disclosure through logs, dead
letters, `ps` output and backups. A compromised webhook endpoint. Two tlgr
processes racing one session file.

**Out of scope.** An attacker who already has your account with a debugger
attached, physical access, Telegram itself, and MTProto cryptography (that is
Telethon's).

## Controls

| Control | What it does |
|---|---|
| `umask(0o077)` before anything | Set as the daemon's first statement, before a single file exists. Fixing modes afterwards leaves a window. |
| Socket mode 0600 | `chmod` immediately after `site.start()`, asserted by a test. v1 shipped `srwxrwxrwx`. |
| Peer-uid check | Every connection's peer credentials are read (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS/BSD) and a mismatch is refused with a WARN naming the peer pid. Defence in depth over the 0600 socket, and an audit trail the file mode cannot give you. |
| Token fallback | `~/.tlgr/ipc.token` (0600), compared with `hmac.compare_digest`, required when the platform gives no peer credentials or when `[security] require_token = true`. |
| Alias validation | `^[A-Za-z0-9_-]{1,64}$`, in one function, called before an alias becomes part of a path. Read paths never create directories. |
| Single owner per session | An exclusive `flock` on `<alias>/session.lock`, held by the daemon for the account's lifetime. The CLI never opens a session file; login runs in the daemon. |
| Single daemon | An exclusive `flock` on `daemon.lock` held for the process lifetime; the spawn probe is serialised behind a separate lock so twenty simultaneous CLIs produce one daemon. |
| Private writes | One `write_private()` used by every writer: write to a temp file, chmod, atomic rename. Nothing is ever briefly world-readable. |
| Permission audit at start | `~/.tlgr` 0700, `accounts/**/session*` and every secret 0600 — fixed where possible, and the daemon **refuses to start** where it is not, printing the `chmod` to run. |
| No secrets in argv | Passwords and tokens arrive through `--x-env` / `--x-stdin` / `--x-file` only. The `SECRET` parameter type refuses to generate a value-taking flag at all, so `ps` never shows one. |
| Access log off | `AppRunner(access_log=None)`. An access log is a permanent record of every chat you touched. |
| Redacted, rotating logs | JSON lines in a 0600 rotating file (8 MB × 5), chmod-ed again after every rollover. Message text, phone numbers, tokens, `access_hash`, `auth_key` and `file_reference` are scrubbed. `--verbose` raises verbosity, never redaction. |
| Dead letters | 0600, rotated, capped at 16 MB × 4. Bodies are shown only with `--full`. |
| Webhook integrity | `X-Tlgr-Signature: sha256=<hmac of the exact body>` plus a monotonic `seq` and a per-delivery id. A plain `http://` URL to a non-loopback host logs a warning at start. |
| Logout on removal | `account remove` calls `auth.logOut` before deleting anything; `--keep-session` opts out. Deleting a session file without logging out leaves a live authorisation on the account. |

## What the policy allowlist is, and is not

`--enable-commands` / `[policy] allow` restricts which operations a caller may
invoke, matched by **canonical operation id** so an alias cannot slip past it,
and enforced in the daemon as well as the CLI.

**It is not a sandbox.** Anything that can open the socket can also read the
session file directly and use it without tlgr at all. The allowlist is a
usability guard with teeth: it stops an agent given
`--enable-commands message.list,message.send` from deleting a chat by mistake,
and the daemon enforces that even if the agent forges its own IPC call —
*provided the operator binds the policy to a token rather than passing a flag
the agent itself controls*.

If your threat model includes a hostile process running as your user, the
answer is not a longer allowlist. It is a different user account, or a
different machine.

## Operating tlgr safely

- Keep `~/.tlgr` on a local disk, not a synced folder. Dropbox, iCloud Drive
  and `rsync` backups all copy the auth key, usually to somewhere with weaker
  permissions than 0600.
- Prefer `~/.tlgr/accounts/<alias>/config.json` over `TELEGRAM_API_ID` /
  `TELEGRAM_API_HASH` environment variables: environment variables are visible
  in `ps` on some platforms and are inherited by every child process.
- Use your own `api_id`/`api_hash` from <https://my.telegram.org>. Never
  borrow an official client's credentials, and never spoof `device_model` to
  obtain official-app behaviour: it violates the ToS and gets accounts banned.
- Use `https://` for webhooks, and verify `X-Tlgr-Signature` over the raw body
  before trusting a delivery.
- `account export --string` prints full account access. It requires `--yes`
  and says so.
- Review `~/.tlgr/logs/daemon.log` if you share the machine. It is redacted,
  but it still records which accounts and chats were active when.

## Known limitations

- **POSIX only.** `flock`, `AF_UNIX` and peer credentials are POSIX. Windows
  support needs named pipes and a different singleton, and is a v2.x item.
- **A per-request `--flood-wait-max` is shared between concurrent requests.**
  Telethon reads its sleep threshold off the client at call time, so tlgr
  stacks the active budgets and applies the smallest. A generous caller may
  therefore return sooner than it had to. That is the safe direction, and
  serialising every request to make the flag exact would cost more than it
  buys.
- **The daemon trusts its own configuration file.** `config.toml` is
  0600 and read as data, but a caller who can write it can set a proxy, so
  treat write access to `~/.tlgr` as equivalent to account access.
