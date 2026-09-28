# Privacy policy

Effective 28 September 2026.

tlgr is open-source software that you run on your own computer. It is not a hosted service. The tlgr project has no servers, no accounts, and no database, so it never receives, sees or stores your data. This policy explains what the software does with your data on your machine and where it sends it. It covers the `tlgr` command and daemon, the `tlgr-cli` package on PyPI, and the tlgr plugin and Agent Skill in [`plugin/`](plugin/).

## What tlgr collects

Nothing, for the tlgr project. tlgr contains no telemetry, analytics, crash reporting or update checks, and it never contacts a server run by the project.

## What tlgr stores on your computer

Everything tlgr keeps is under its home directory, `~/.tlgr` by default (or `$TLGR_HOME`). Files that hold secrets are created readable by your user only.

- **Session files** for each logged-in account (`accounts/<alias>/session.session`). A session file is full access to that Telegram account; see [SECURITY.md](SECURITY.md).
- **Your Telegram API credentials** (`api_id`, `api_hash`) and account settings.
- **A peer cache** (`accounts/<alias>/peers.json`): the ids, usernames and names of chats and people your account has seen, so tlgr can address them again.
- **Operational state:** sync positions, rate-limit deadlines, the daemon's socket, lock and pid files, and your `config.toml`, `jobs.yaml` and `webhook.toml`.
- **Logs** (`logs/`): rotating files of daemon activity. Message text, phone numbers and tokens are kept out of them by an allow-list filter.
- **Undelivered webhook events** (`dead_letter.jsonl`): if you set up a webhook and an event cannot be delivered, the event, which can include message text, is kept here so you can resend or delete it.
- **Files you download** with tlgr, where you tell it to save them.

To delete all of it, stop the daemon and remove the tlgr home directory. Log out first (`tlgr account logout <alias>`) if you also want Telegram to end the session on its side.

## Where tlgr sends data

tlgr only makes network connections for things you ask it to do:

- **Telegram.** tlgr connects to Telegram's servers to act on your account: reading and sending messages, managing chats, uploading and downloading media, and so on. Telegram's [privacy policy](https://telegram.org/privacy) governs what Telegram does with that data. When you log in, tlgr tells Telegram a device name, system version, app version and language, which show up in your account's list of active sessions.
- **Your webhook, if you set one.** With `tlgr webhook set`, the daemon sends Telegram events for the accounts and event types you choose, which can include message text, to the URL you configured. Nothing is sent unless you configure and enable a webhook.
- **A proxy, if you set one.** If you configure an MTProto or SOCKS proxy, tlgr's traffic to Telegram goes through it.
- **A mini app's file, if you ask for it.** One command downloads a file that a Telegram mini app offers. tlgr fetches it over HTTPS from the address the mini app gave.

## Using tlgr with an AI assistant

The tlgr plugin and Agent Skill contain instructions only: no code, no hooks, no MCP servers, and nothing that runs when you install them. When an AI assistant such as Claude uses tlgr on your behalf, it runs `tlgr` commands on your computer, and the output of those commands, which can include your messages, contacts and chat names, becomes part of your conversation with that assistant. How that conversation is handled is set by your AI provider's privacy policy and your settings with them, not by tlgr. You can limit what an assistant is able to do with `--enable-commands` (see [AGENT.md](AGENT.md)).

## Age

tlgr is a developer tool and is not intended for anyone under 18.

## Changes

Changes to this policy are made in this file, so the repository's history shows every version and when it changed.

## Contact

Questions about this policy go to [GitHub issues](https://github.com/tlgrcli/tlgr/issues). To report a security problem privately, see [SECURITY.md](SECURITY.md).
