"""Feature parity, computed from the registry rather than asserted in prose.

tlgr v2's goal is stated as a number — every feature of the official Telegram
clients, reachable from the CLI — so the number has to be checkable. It is:
the catalog index says what exists, every `OperationSpec` declares the ids it
`covers`, and this module subtracts one from the other.

Three rules keep it honest.

* **The denominator is fixed.** Ids whose feasibility is `not-applicable`
  (bot-only, server-side, GUI-only) or `prohibited` (ToS, spam,
  deanonymisation) are excluded once, here, and never again. Anything else
  counts, so coverage cannot be improved by re-labelling work as out of scope.
* **A waiver names a permanent reason, not a later PR.** Until PR-12 a
  waiver was a promise with a PR number on it; every one of those promises
  has been kept, so `parity_waivers.toml` now holds only ids this build
  genuinely cannot cover, each with a `kind` (`layer-gap`, `absent-method`,
  `prohibited`, `not-applicable`) and the method that is missing. A waived id
  is still in the denominator; it is reported as uncovered-with-a-reason,
  never subtracted.
* **An unknown id is a build failure.** An op that covers an id the catalog
  has never heard of is a typo, and a typo that inflates a coverage number is
  worse than a gap.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI leg
    import tomli as tomllib

__all__ = [
    "CatalogEntry",
    "ParityReport",
    "Waiver",
    "Waivers",
    "catalog",
    "compute",
    "render_table",
    "unknown_ids",
    "waivers",
]

DATA = Path(__file__).resolve().parent / "data"
CATALOG_PATH = DATA / "catalog_index.json"
WAIVERS_PATH = DATA / "parity_waivers.toml"

PRIORITIES = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    group: str
    domain: str
    name: str
    priority: str
    feasibility: str

    @property
    def required(self) -> bool:
        return self.feasibility in ("full", "partial", "control-only")


#: The only reasons a waiver may give. Anything else is a backlog entry
#: wearing a waiver's clothes, and `tests/test_parity.py` refuses it.
KINDS = ("layer-gap", "absent-method", "prohibited", "not-applicable")


@dataclass(frozen=True, slots=True)
class Waiver:
    """One id that cannot be covered, and why."""

    kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class Waivers:
    """What is knowingly uncovered, by id.

    `domains` survives as a mapping rather than being deleted, so that a
    domain waiver reappearing in the file is something the gate can *see* and
    refuse — "no blanket waivers" is then a rule the file cannot break rather
    than a habit somebody has to remember.
    """

    catalog_version: str = ""
    final_pr: int = 0
    domains: dict[str, tuple[int, str]] = field(default_factory=dict)
    ids: dict[str, Waiver] = field(default_factory=dict)

    def reason_for(self, entry: CatalogEntry) -> str:
        found = self.ids.get(entry.id)
        if found is not None:
            return f"waived ({found.kind}): {found.reason}"
        legacy = self.domains.get(entry.domain)
        if legacy is not None:  # pragma: no cover - the file carries none
            return f"waived (domain): {legacy[1]}"
        return ""


_catalog_cache: dict[str, CatalogEntry] | None = None
_waivers_cache: Waivers | None = None


def catalog(path: Path | None = None) -> dict[str, CatalogEntry]:
    """The pruned catalog index, keyed by id. Read once per process.

    Loaded lazily and never at import: this is 325 KB of JSON and `tlgr
    --help` must not pay for it.
    """
    global _catalog_cache
    if path is None and _catalog_cache is not None:
        return _catalog_cache
    raw = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    entries = {
        item["id"]: CatalogEntry(
            id=item["id"],
            group=item["group"],
            domain=item["domain"],
            name=item["name"],
            priority=item["priority"],
            feasibility=item["feasibility"],
        )
        for item in raw["entries"]
    }
    if path is None:
        _catalog_cache = entries
    return entries


def catalog_version(path: Path | None = None) -> str:
    raw = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    return str(raw.get("catalog_version", ""))


def waivers(path: Path | None = None) -> Waivers:
    global _waivers_cache
    if path is None and _waivers_cache is not None:
        return _waivers_cache
    raw = tomllib.loads((path or WAIVERS_PATH).read_text(encoding="utf-8"))
    meta = raw.get("meta", {})
    parsed = Waivers(
        catalog_version=str(meta.get("catalog_version", "")),
        final_pr=int(meta.get("final_pr", 0)),
        domains={
            str(item["name"]): (int(item["pr"]), str(item.get("reason", "")))
            for item in raw.get("domain", [])
        },
        ids={
            str(item["id"]): Waiver(
                kind=str(item.get("kind", "")), reason=str(item.get("reason", ""))
            )
            for item in raw.get("id", [])
        },
    )
    if path is None:
        _waivers_cache = parsed
    return parsed


@dataclass
class ParityReport:
    catalog_version: str = ""
    required: int = 0
    covered: int = 0
    percent: float = 0.0
    #: covered + waived. The number the "definition of done for a group PR"
    #: is actually about: every id in the domain is either implemented or
    #: has a named owner and a PR number. Reporting only `percent` would make
    #: a group whose remaining ids belong to *other* groups look unfinished.
    accounted: int = 0
    accounted_percent: float = 0.0
    by_priority: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_domain: dict[str, dict[str, Any]] = field(default_factory=dict)
    partial: list[dict[str, str]] = field(default_factory=list)
    uncovered: list[dict[str, str]] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    unknown: list[dict[str, str]] = field(default_factory=list)
    ops: int = 0
    commands: int = 0
    aliases: int = 0
    waivers: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "required": self.required,
            "covered": self.covered,
            "percent": self.percent,
            "accounted": self.accounted,
            "accounted_percent": self.accounted_percent,
            "by_priority": self.by_priority,
            "by_domain": self.by_domain,
            "partial": self.partial,
            "uncovered": self.uncovered,
            "excluded": self.excluded,
            "unknown": self.unknown,
            "ops": self.ops,
            "commands": self.commands,
            "aliases": self.aliases,
            "waivers": self.waivers,
        }


def _coverage(specs: Any) -> tuple[dict[str, str], dict[str, str]]:
    """`(id → op that fully covers it, id → op that partially covers it)`."""
    full: dict[str, str] = {}
    partial: dict[str, str] = {}
    for spec in specs:
        for entry_id in spec.covers:
            full.setdefault(entry_id, spec.id)
        for entry_id in spec.covers_partial:
            partial.setdefault(entry_id, spec.id)
    return full, partial


def unknown_ids(registry: Any = None) -> list[dict[str, str]]:
    """Ids an op claims to cover that the catalog does not contain (L-COV-1).

    Not run at import: it would put a 325 KB JSON read in front of every
    `tlgr --help`. It runs in `tests/test_parity.py` and in `make parity`,
    which is where a typo is actually caught.
    """
    from tlgr.registry import REGISTRY

    known = catalog()
    out: list[dict[str, str]] = []
    for spec in (registry or REGISTRY).values():
        for entry_id in (*spec.covers, *spec.covers_partial):
            if entry_id not in known:
                out.append({"op": spec.id, "id": entry_id})
    return sorted(out, key=lambda item: (item["op"], item["id"]))


def compute(registry: Any = None) -> ParityReport:
    """The whole parity picture, from the registry as it stands right now."""
    from tlgr.registry import REGISTRY

    specs = list((registry or REGISTRY).values())
    entries = catalog()
    rules = waivers()
    full, partial = _coverage(specs)
    covered_ids = set(full) | set(partial)

    report = ParityReport(
        catalog_version=catalog_version(),
        ops=len(specs),
        commands=len({name for spec in specs for name in spec.names}),
        aliases=sum(len(spec.aliases) for spec in specs),
    )

    priority_stats: dict[str, dict[str, Any]] = {
        p: {"required": 0, "covered": 0, "percent": 0.0, "waived": 0, "accounted_percent": 0.0}
        for p in PRIORITIES
    }
    domain_stats: dict[str, dict[str, Any]] = {}
    excluded: dict[str, int] = {}

    for entry in entries.values():
        if not entry.required:
            excluded[entry.feasibility] = excluded.get(entry.feasibility, 0) + 1
            continue
        report.required += 1
        stats = domain_stats.setdefault(
            entry.domain,
            {
                "required": 0,
                "covered": 0,
                "percent": 0.0,
                "waived": 0,
                "accounted_percent": 0.0,
                "ops": 0,
            },
        )
        stats["required"] += 1
        priority_stats[entry.priority]["required"] += 1

        if entry.id in covered_ids:
            report.covered += 1
            stats["covered"] += 1
            priority_stats[entry.priority]["covered"] += 1
            if entry.id in partial and entry.id not in full:
                report.partial.append(
                    {
                        "id": entry.id,
                        "op": partial[entry.id],
                        "note": _note_for(specs, partial[entry.id]),
                    }
                )
            continue

        reason = rules.reason_for(entry)
        if reason:
            report.waivers += 1
            stats["waived"] += 1
            priority_stats[entry.priority]["waived"] += 1
        report.uncovered.append(
            {
                "id": entry.id,
                "priority": entry.priority,
                "domain": entry.domain,
                "name": entry.name,
                "reason": reason or "not covered and not waived",
            }
        )

    for domain, stats in domain_stats.items():
        stats["ops"] = sum(
            1
            for spec in specs
            if any(
                entries[i].domain == domain
                for i in (*spec.covers, *spec.covers_partial)
                if i in entries
            )
        )
        stats["percent"] = _percent(stats["covered"], stats["required"])
        stats["accounted_percent"] = _percent(stats["covered"] + stats["waived"], stats["required"])
    for stats in priority_stats.values():
        stats["percent"] = _percent(stats["covered"], stats["required"])
        stats["accounted_percent"] = _percent(stats["covered"] + stats["waived"], stats["required"])

    report.accounted = report.covered + report.waivers
    report.accounted_percent = _percent(report.accounted, report.required)
    report.percent = _percent(report.covered, report.required)
    report.by_priority = {p: priority_stats[p] for p in PRIORITIES if p in priority_stats}
    report.by_domain = dict(sorted(domain_stats.items()))
    report.excluded = dict(sorted(excluded.items()))
    report.unknown = unknown_ids(registry)
    report.uncovered.sort(key=lambda item: (item["priority"], item["id"]))
    report.partial.sort(key=lambda item: item["id"])
    return report


def _note_for(specs: list[Any], op_id: str) -> str:
    for spec in specs:
        if spec.id == op_id:
            return str(spec.coverage_note)
    return ""


def _percent(covered: int, required: int) -> float:
    return round(100.0 * covered / required, 1) if required else 100.0


def render_table(report: ParityReport) -> str:
    """The human view: one row per domain, then priorities, then the totals."""
    lines = [
        f"catalog {report.catalog_version} — {report.ops} operations, "
        f"{report.commands} invocable paths",
        "",
        f"{'domain':<28} {'covered':>8} {'req':>6} {'%':>7} {'acct%':>7}  ops",
    ]
    for domain, stats in report.by_domain.items():
        lines.append(
            f"{domain:<28} {stats['covered']:>8} {stats['required']:>6} "
            f"{stats['percent']:>6.1f}% {stats['accounted_percent']:>6.1f}%  {stats['ops']}"
        )
    lines += ["", f"{'priority':<28} {'covered':>8} {'req':>6} {'%':>7} {'acct%':>7}"]
    for priority, stats in report.by_priority.items():
        lines.append(
            f"{priority:<28} {stats['covered']:>8} {stats['required']:>6} "
            f"{stats['percent']:>6.1f}% {stats['accounted_percent']:>6.1f}%"
        )
    excluded = ", ".join(f"{k} {v}" for k, v in report.excluded.items())
    lines += [
        "",
        f"{'TOTAL':<28} {report.covered:>8} {report.required:>6} "
        f"{report.percent:>6.1f}% {report.accounted_percent:>6.1f}%",
        f"excluded: {excluded}",
        f"uncovered: {len(report.uncovered)} ({report.waivers} waived with a reason)",
    ]
    if report.unknown:
        lines.append(
            f"UNKNOWN IDS: {len(report.unknown)} — an op covers an id the catalog does not have"
        )
    return "\n".join(lines)
