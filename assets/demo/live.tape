# Records tlgr against a real account. Nothing personal is printed:
# catchup is reduced to two counts, and the only messages shown are the
# one this script sends to your own Saved Messages.
#
# Run from this folder, choosing the account in the environment so the
# alias never appears on screen:
#   TLGR_ALLOW_PRODUCTION_HOME=1 TLGR_ACCOUNT=<alias> vhs live.tape
# Check every frame before publishing (open tlgr-live.mp4 and scrub).

Output "tlgr-live.gif"
Output "tlgr-live.mp4"

Set Shell "zsh"
Set FontSize 17
Set Width 1280
Set Height 720
Set Padding 28
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set WindowBar Colorful

Hide
Type "setopt interactivecomments; PS1='%F{cyan}$%f '; PROMPT='%F{cyan}$%f '; clear"
Enter
Sleep 500ms
Show

Type "# tlgr: your Telegram account, from the terminal, for scripts and AI agents"
Sleep 1.2s
Enter
Sleep 400ms

Type "# what did I miss? (read-only: no read receipts are sent)"
Enter
Type "tlgr catchup --results-only | jq '{unread_chats: (.chats | length), unread_messages: ([.chats[].unread_count] | add)}'"
Sleep 400ms
Enter
Sleep 4s

Type "tlgr message send me 'Hello from tlgr' --results-only | jq -c '{id, text, out, date}'"
Sleep 400ms
Enter
Sleep 4s

Type "tlgr message list me --limit 1 --results-only | jq -c '.items[] | {id, text, out}'"
Sleep 400ms
Enter
Sleep 4s

Type "# an agent can be limited to an allowlist, enforced by the daemon"
Enter
Type "tlgr --enable-commands message.list message send me 'blocked'; echo \"exit code: $?\""
Sleep 400ms
Enter
Sleep 4.5s
