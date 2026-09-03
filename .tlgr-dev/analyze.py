import json
import sys

sys.path.insert(0, "/Users/p4/Projects/tlgr/.claude/worktrees/agent-a1a314cd397effee5")

from tlgr.parity import catalog  # noqa: E402

SRC = (
    "/private/tmp/claude-501/-Users-p4-Projects-tlgr/"
    "5fd65c79-1a6c-42cc-90da-18a07ec24b43/scratchpad/impl/pr7_commands.json"
)
d = json.load(open(SRC))
cov = set()
for c in d["commands"]:
    cov |= set(c["covers"]) | set(c["covers_partial"])

cat = catalog()
unknown = sorted(i for i in cov if i not in cat)
print("claimed ids:", len(cov), "unknown:", unknown)

dom = {i: e for i, e in cat.items() if e.domain == "groups_channels_admin" and e.required}
print("domain required:", len(dom))
missing = sorted(i for i in dom if i not in cov)
print("domain ids NOT claimed by PR7:", len(missing))
for i in missing:
    print("   ", cat[i].priority, i, "-", cat[i].name)

p0 = sorted(i for i in cov if i in cat and cat[i].priority == "P0")
print("P0 ids claimed:", len(p0))
for i in p0:
    print("   ", i, "|", cat[i].domain)

# which claimed ids belong to other domains
other = sorted({cat[i].domain for i in cov if i in cat} - {"groups_channels_admin"})
print("other domains touched:", other)
