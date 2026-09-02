"""The parity gate: P0 coverage never regresses, and no id is invented.

The point of a floor stored in the test rather than computed from the
registry is that it cannot be satisfied by the thing it is measuring. A PR
that removes `covers=("messages-core.send-text",)` from `message.send` fails
here; a PR that adds coverage has to *raise* the floor deliberately, which is
a line in the diff a reviewer can see.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import tlgr.ops  # noqa: F401  — importing it populates the registry
from tlgr.parity import catalog, compute, render_table, unknown_ids, waivers
from tlgr.registry import REGISTRY

ROOT = Path(__file__).resolve().parent.parent

#: Every P0 catalog id PR-1 claims. Raised by each group PR, never lowered.
#: ARCHITECTURE §1.3: "P0 coverage may never decrease and must reach 100 %
#: before 2.0.0 final".
P0_FLOOR = 30

#: The floor for total covered ids. Same rule, weaker guarantee.
COVERED_FLOOR = 200

#: The P0 ids PR-1 is responsible for. Named, not counted, so a swap — one
#: dropped and one added — cannot pass the count check silently.
PR1_P0_IDS = frozenset(
    {
        "messages-core.delete-for-everyone",
        "messages-core.delete-for-me",
        "messages-core.edit-text",
        "messages-core.entities-detect-local",
        "messages-core.format-entity-offsets-utf16",
        "messages-core.format-raw-entities-json",
        "messages-core.forward-basic",
        "messages-core.history-list",
        "messages-core.message-get",
        "messages-core.message-link-create",
        "messages-core.pin-message",
        "messages-core.reaction-add-remove",
        "messages-core.read-mark-history",
        "messages-core.search-in-chat-text",
        "messages-core.send-reply",
        "messages-core.send-text",
        "messages-core.unpin-message",
    }
)


@pytest.fixture(scope="module")
def report():
    return compute()


def _covered_ids() -> set[str]:
    covered: set[str] = set()
    for spec in REGISTRY.values():
        covered |= set(spec.covers) | set(spec.covers_partial)
    return covered


class TestCatalogIndex:
    def test_index_is_regenerated_from_the_design_catalog(self):
        """`make parity` fails on a stale index rather than a wrong number."""
        result = subprocess.run(
            [sys.executable, "tools/prune_catalog.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_the_waivers_name_the_catalog_they_were_written_against(self):
        from tlgr.parity import catalog_version

        assert waivers().catalog_version == catalog_version()

    def test_every_waived_id_exists(self):
        known = catalog()
        assert [i for i in waivers().ids if i not in known] == []

    def test_every_waived_domain_exists(self):
        domains = {entry.domain for entry in catalog().values()}
        assert [d for d in waivers().domains if d not in domains] == []


class TestCoverageLints:
    def test_no_op_covers_an_id_the_catalog_does_not_have(self):
        """L-COV-1. A typo that inflates a number is worse than a gap."""
        assert unknown_ids() == []

    def test_a_partial_cover_explains_itself(self):
        for spec in REGISTRY.values():
            if spec.covers_partial:
                assert spec.coverage_note, f"{spec.id} covers partially without saying why"


class TestTheGate:
    def test_p0_coverage_does_not_regress(self, report):
        covered = report.by_priority["P0"]["covered"]
        assert covered >= P0_FLOOR, (
            f"P0 coverage fell from {P0_FLOOR} to {covered}. "
            "P0 may never decrease (ARCHITECTURE §1.3)."
        )

    def test_total_coverage_does_not_regress(self, report):
        assert report.covered >= COVERED_FLOOR

    def test_every_p0_id_this_pr_owns_is_covered(self):
        missing = sorted(PR1_P0_IDS - _covered_ids())
        assert missing == [], f"PR-1 dropped coverage of {missing}"

    def test_every_uncovered_id_is_waived_with_a_pr_number(self, report):
        """No silent gaps: the gate is meaningful from day one, not at the end."""
        unwaived = [u for u in report.uncovered if not u["reason"].startswith("waived")]
        assert unwaived == [], f"{len(unwaived)} ids are uncovered and unwaived: {unwaived[:5]}"

    def test_messages_core_is_fully_accounted_for(self, report):
        """PR-1's own domain: implemented, or waived to a named later PR."""
        stats = report.by_domain["messages_core"]
        assert stats["accounted_percent"] == 100.0
        assert stats["covered"] >= 130


class TestReport:
    def test_the_excluded_set_is_the_documented_one(self, report):
        assert report.excluded == {"not-applicable": 79, "prohibited": 40}
        assert report.required == 1797

    def test_the_human_table_names_every_domain(self, report):
        table = render_table(report)
        for domain in report.by_domain:
            assert domain in table
        assert "TOTAL" in table

    def test_the_op_returns_the_same_numbers(self):
        import asyncio

        from tlgr.ops.agent import ParityReq, parity

        class _Ctx:
            account = ""
            dry_run = False
            request_id = "t"

            def warn(self, message: str) -> None: ...

            def emit(self, event_type: str, payload: dict, **kwargs) -> None: ...

        result = asyncio.run(parity(_Ctx(), ParityReq()))
        assert result["covered"] == compute().covered
        # The default trims the gap list; --uncovered does not.
        assert len(result["uncovered"]) <= 20
        full = asyncio.run(parity(_Ctx(), ParityReq(uncovered=True)))
        assert len(full["uncovered"]) == len(compute().uncovered)

    def test_the_report_is_json_encodable(self, report):
        json.dumps(report.to_dict())
