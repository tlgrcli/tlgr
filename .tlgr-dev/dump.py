import json

SRC = (
    "/private/tmp/claude-501/-Users-p4-Projects-tlgr/"
    "5fd65c79-1a6c-42cc-90da-18a07ec24b43/scratchpad/impl/pr7_commands.json"
)
DEST = "/Users/p4/Projects/tlgr/.claude/worktrees/agent-a1a314cd397effee5/.tlgr-dev/pr7_cmds.txt"

d = json.load(open(SRC))
out = []
for c in d["commands"]:
    c2 = {k: v for k, v in c.items() if k != "_catalog"}
    out.append(json.dumps(c2, indent=1))
open(DEST, "w").write("\n=====\n".join(out))
print("ok", sum(len(o) for o in out))
