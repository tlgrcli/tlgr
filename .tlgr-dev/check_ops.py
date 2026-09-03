import json
import sys

sys.path.insert(0, "/Users/p4/Projects/tlgr/.claude/worktrees/agent-a1a314cd397effee5")

import tlgr.ops  # noqa: F401,E402
from tlgr.registry import ALIASES, REGISTRY  # noqa: E402

SRC = (
    "/private/tmp/claude-501/-Users-p4-Projects-tlgr/"
    "5fd65c79-1a6c-42cc-90da-18a07ec24b43/scratchpad/impl/pr7_commands.json"
)
work = json.load(open(SRC))["commands"]
wanted = {c["path"].replace(" ", "."): c for c in work}

missing = sorted(p for p in wanted if p not in REGISTRY)
print("commands in the work list:", len(wanted))
print("MISSING op ids:", missing)

extra_aliases = []
for path, c in wanted.items():
    if path not in REGISTRY:
        continue
    spec = REGISTRY[path]
    for alias in c["aliases"]:
        key = alias.replace(" ", ".")
        if ALIASES.get(key) != spec.id:
            extra_aliases.append((path, alias, ALIASES.get(key)))
    for legacy in c["legacy_paths"]:
        key = legacy.replace(" ", ".")
        if ALIASES.get(key) != spec.id:
            extra_aliases.append((path, "legacy:" + legacy, ALIASES.get(key)))
print("alias/legacy gaps:", extra_aliases)

# Coverage comparison
claimed = set()
for c in work:
    claimed |= set(c["covers"]) | set(c["covers_partial"])
have = set()
for path in wanted:
    if path in REGISTRY:
        spec = REGISTRY[path]
        have |= set(spec.covers) | set(spec.covers_partial)
print("catalog ids the work list claims:", len(claimed))
print("not claimed by the implementation:", sorted(claimed - have))
print("claimed extra:", sorted(have - claimed))
