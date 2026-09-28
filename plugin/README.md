# tlgr plugin

This plugin teaches Claude how to read and act on a personal Telegram account with [tlgr](https://github.com/tlgrcli/tlgr), a command-line client that logs in as a normal user over MTProto. With it, Claude can catch up on unread chats, search message history, send, reply to, edit and forward messages, manage chats, groups, channels and contacts, and handle media, polls and stories.

The plugin is one skill, `skills/tlgr/SKILL.md`. It contains instructions only: no hooks, no MCP servers, no scripts, and nothing that runs when the plugin is installed.

## What it needs

The `tlgr` command must be installed and logged in on your machine:

```bash
pipx install tlgr-cli
```

Logging in is two commands, `tlgr auth send-code` and `tlgr auth verify-code`. The skill walks Claude through them, and only reading the login code needs you.

## What it runs and sends

When the skill is used, Claude runs `tlgr` commands in your terminal, with the same permission prompts as any other command. `tlgr` talks to Telegram's servers with your own session, which stays on your machine under `~/.tlgr`. The plugin itself sends nothing anywhere. `tlgr` sends events to a webhook only if you configure one with `tlgr webhook set`.

The skill tells Claude to look without leaving a trace by default (no read receipts), to ask before deleting, leaving, blocking or messaging someone new, and never to put a password or other secret on the command line.

## License

MIT, the same as tlgr.
