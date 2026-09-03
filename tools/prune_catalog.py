#!/usr/bin/env python3
"""Prune the design catalog into the index that ships inside the package.

`docs/design/parity-catalog.json` is 830 KB of research: MTProto methods, GUI
locations, prose descriptions. None of that is needed at runtime — the parity
report only asks "does this id exist, what priority is it, whose domain is
it, and is it in scope at all". The pruned index is a tenth of the size and
is the file `tlgr agent parity` reads.

    python tools/prune_catalog.py [--check]

`--check` regenerates into memory and exits 1 if the shipped file differs,
which is what `make parity` runs in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "design" / "parity-catalog.json"
TARGET = ROOT / "tlgr" / "data" / "catalog_index.json"

#: Feasibilities that are in scope. `not-applicable` (bot-only, server-side,
#: GUI-only) and `prohibited` (ToS, spam, deanonymisation) are excluded from
#: the denominator rather than waived, because they are not work we intend to
#: do — counting them would make 100 % unreachable by design.
REQUIRED = ("full", "partial", "control-only")


def build(source: Path = SOURCE) -> dict[str, object]:
    raw = json.loads(source.read_text(encoding="utf-8"))
    entries = []
    for entry in raw["entries"]:
        entries.append(
            {
                "id": entry["id"],
                # The id's own prefix is the *catalog group* (`messages-core`),
                # which is not the same thing as the owning domain
                # (`messages_core`) — some ids are delegated across domains.
                "group": entry["id"].split(".", 1)[0],
                "domain": entry["domain"],
                "name": entry["name"],
                "priority": entry["priority"],
                "feasibility": entry["feasibility"],
            }
        )
    entries.sort(key=lambda item: item["id"])
    return {
        "catalog_version": raw["catalog_version"],
        "source": raw["source"],
        "required_feasibility": list(REQUIRED),
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the shipped index is stale.")
    args = parser.parse_args(argv)

    document = build()
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n"

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET} is stale; run: python tools/prune_catalog.py", file=sys.stderr)
            return 1
        print(f"{TARGET} is current ({len(document['entries'])} entries)")  # type: ignore[arg-type]
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET} ({len(document['entries'])} entries)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
