#!/usr/bin/env python3
"""Check that a tag, the package version and the changelog agree, and print
the changelog section that belongs to it.

A release has three statements of its own version: the git tag,
`tlgr.__version__` (which `pyproject.toml` reads through `dynamic`), and the
`## [x.y.z]` heading in `CHANGELOG.md`. Nothing makes them agree on its own,
and a wheel that says 2.0.0 under a v2.0.1 tag is the kind of mistake nobody
finds until an install is wrong. This is the check, and it runs before the
build rather than after it.

    python tools/release_notes.py v2.0.0 [--output notes.md]

On agreement the changelog body for that version is written to `--output`
(default: stdout) and the exit status is 0. On any disagreement the reason
goes to stderr and the exit status is 1, which fails the release job before
anything is published.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

#: `## [2.0.0] — 2026-09-04`. The separator between the version and the date
#: is whatever the entry chose; only the bracketed version is matched.
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def package_version() -> str:
    """The version the built artefacts will carry."""
    sys.path.insert(0, str(ROOT))
    from tlgr import __version__

    return __version__


def changelog_section(version: str) -> str:
    """The body under `## [version]`, up to the next `## ` heading."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match is None:
            continue
        if match.group("version") == version:
            start = index + 1
            continue
        if start is not None:
            return "\n".join(lines[start:index]).strip() + "\n"
    if start is None:
        raise SystemExit(
            f"CHANGELOG.md has no '## [{version}]' heading. Write the entry before tagging."
        )
    return "\n".join(lines[start:]).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="the git tag being released, e.g. v2.0.0")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the notes here instead of stdout",
    )
    args = parser.parse_args()

    tag_version = args.tag[1:] if args.tag.startswith("v") else args.tag
    installed = package_version()
    if tag_version != installed:
        print(
            f"tag {args.tag} does not match tlgr.__version__ ({installed}). "
            f"Bump one of them; a wheel must not disagree with its tag.",
            file=sys.stderr,
        )
        return 1

    notes = changelog_section(tag_version)
    if args.output is None:
        sys.stdout.write(notes)
    else:
        args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
