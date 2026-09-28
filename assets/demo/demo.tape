# Renders tlgr-demo.gif and tlgr-demo.mp4 without touching any account:
# every command here runs against a throwaway TLGR_HOME.
#   cd assets/demo && vhs demo.tape
# live.tape is the same idea against a real account.

Output "tlgr-demo.gif"
Output "tlgr-demo.mp4"

Set Shell "zsh"
Set FontSize 17
Set Width 1280
Set Height 720
Set Padding 28
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set WindowBar Colorful

Hide
Type "setopt interactivecomments; export TLGR_HOME=$(mktemp -d) PS1='%F{cyan}$%f ' PROMPT='%F{cyan}$%f ' && clear"
Enter
Sleep 500ms
Show

Type "# tlgr: your Telegram account, from the terminal, for scripts and AI agents"
Sleep 1.2s
Enter
Sleep 400ms

Type "tlgr events list --group message | head -8"
Sleep 400ms
Enter
Sleep 3s

Type "tlgr message entity list --parse md '**Deploy** done, see [logs](https://ci.example)' --json --results-only | jq -c"
Sleep 400ms
Enter
Sleep 3.5s

Type "tlgr agent capabilities --section policy"
Sleep 400ms
Enter
Sleep 3.5s

Type "tlgr agent exit-codes | head -12"
Sleep 400ms
Enter
Sleep 4s
