# Security

This plugin is instructions only: one skill (`skills/tlgr/SKILL.md`) and no hooks, MCP servers or scripts. It runs nothing on its own. What it asks Claude to run is the `tlgr` command, and `tlgr` holds Telegram session files that give full access to the account.

## Reporting a vulnerability

Report problems in the plugin or in tlgr the same way: open a [private security advisory](https://github.com/tlgrcli/tlgr/security/advisories/new) on the repository. Please do not open a public issue for anything that discloses session material. The full threat model is in the repository's [SECURITY.md](https://github.com/tlgrcli/tlgr/blob/main/SECURITY.md).

## Supported versions

Only the latest version of the plugin and the latest tlgr release on PyPI (`tlgr-cli`) receive fixes.
