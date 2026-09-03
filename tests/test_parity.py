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

#: Every P0 catalog id the landed PRs claim. Raised by each group PR, never
#: lowered. ARCHITECTURE §1.3: "P0 coverage may never decrease and must reach
#: 100 % before 2.0.0 final".
P0_FLOOR = 95

#: The floor for total covered ids. Same rule, weaker guarantee.
COVERED_FLOOR = 808

#: Every P0 catalog id PR-1's own operations cover, named rather than
#: counted, so a swap (one dropped, one added) cannot pass a count check
#: silently. `test_the_floor_is_the_whole_truth` keeps the list equal to what
#: the registry actually claims, so raising it is a deliberate line in a diff
#: and never an accident.
#:
#: `messages-core.reaction-add-remove` left this set in PR-9: `message react`
#: became an alias of `reaction add`, so the id is claimed by the group that
#: owns the whole reaction surface. It is still covered — it moved to
#: PR9_P0_IDS below, and the total floor went up, not down.
PR1_P0_IDS = frozenset(
    {
        "bots.inline-keyboard-render",
        "bots.reply-keyboard-render",
        "dialogs.draft-set",
        "messages-core.delete-for-everyone",
        "messages-core.delete-for-me",
        "messages-core.edit-text",
        "messages-core.format-basic-styles",
        "messages-core.format-code-and-pre",
        "messages-core.format-entity-offsets-utf16",
        "messages-core.format-html-parse-mode",
        "messages-core.format-markdown-parse-mode",
        "messages-core.format-mention",
        "messages-core.format-text-link",
        "messages-core.forward-basic",
        "messages-core.forward-hide-sender",
        "messages-core.history-list",
        "messages-core.link-preview-disable",
        "messages-core.message-get",
        "messages-core.message-link-create",
        "messages-core.pin-message",
        "messages-core.read-mark-history",
        "messages-core.saved-messages-send",
        "messages-core.search-filter-media-type",
        "messages-core.search-in-chat-text",
        "messages-core.send-reply",
        "messages-core.send-scheduled",
        "messages-core.send-silent",
        "messages-core.send-text",
        "messages-core.unpin-message",
    }
)

#: Every P0 catalog id PR-11's `call`/`vc`/`conference` operations cover. Same
#: rule as `PR1_P0_IDS`: named rather than counted, so a swap cannot pass.
PR11_P0_IDS = frozenset(
    {
        "calls.decline-call",
        "calls.hangup",
        "calls.history-list",
        "calls.incoming-signalling",
        "groupcall.create-video-chat",
        "groupcall.get",
        "groupcall.leave",
        "groupcall.list-participants",
        "groupcall.mute-self",
    }
)

#: The P0 ids PR-3's own operations cover, named for the same reason: a swap
#: must not pass a count check.
PR3_P0_IDS = frozenset(
    {
        "dialogs.archive",
        "dialogs.chat-full-settings",
        "dialogs.clear-history-both",
        "dialogs.clear-history-self",
        "dialogs.delete-chat-private",
        "dialogs.get-peer-dialog",
        "dialogs.leave-group",
        "dialogs.list-archive",
        "dialogs.list-main",
        "dialogs.mark-read",
        "dialogs.mark-unread",
        "dialogs.mute-for-duration",
        "dialogs.mute-forever",
        "dialogs.open-chat",
        "dialogs.peek-chat",
        "dialogs.pin",
        "dialogs.unarchive",
        "dialogs.unmute",
        "dialogs.unpin",
        "dialogs.unread-counters",
        "dialogs.unread-quick-filter",
        "groups-channels-admin.get-full-info",
        "groups-channels-admin.leave",
    }
)

#: Every P0 catalog id PR-9's `poll`/`reaction`/`todo`/`location`/`search`
#: operations cover. Same rule as PR1_P0_IDS: named, not counted.
PR9_P0_IDS = frozenset(
    {
        "messages-core.reaction-add-remove",
        "messages-core.search-global",
        "poll.create-regular",
        "poll.multiple-choice",
        "poll.public-voters",
        "poll.quiz",
        "poll.stop",
        "poll.vote",
        "reaction.read-summary",
        "reaction.remove",
        "reaction.send-emoji",
        "todo.toggle-completed",
    }
)

#: Every P0 catalog id PR-2's own operations cover. Named rather than counted
#: for the same reason as PR-1's list: a swap (one dropped, one added) must
#: not slip past a count check.
PR2_P0_IDS = frozenset(
    {
        "auth.2fa-login",
        "auth.code-type-app",
        "auth.log-out",
        "auth.multi-account",
        "auth.phone-login-send-code",
        "password.set",
        "sessions.list",
        "sessions.terminate",
    }
)

#: Every P0 catalog id PR-6's operations cover. Same rule as PR-1's list: named
#: rather than counted, so a swap cannot pass a count check silently.
PR6_P0_IDS = frozenset(
    {
        "media.big-file-upload",
        "media.download-message-media",
        "media.info",
        "media.send-album",
        "media.send-document",
        "media.send-photo",
        "media.send-photo-uncompressed",
        "media.send-sticker",
        "media.send-video",
        "media.shared-media-list",
        "sticker.set-install-uninstall",
        "sticker.set-list-installed",
        "sticker.set-view",
        "stories.download-media",
    }
)

#: `(group prefixes, the P0 ids those groups claim)` for each landed PR.
P0_OWNERS = (
    (("message.", "draft."), PR1_P0_IDS),
    (("auth.", "account.", "passport."), PR2_P0_IDS),
    (("chat.", "folder."), PR3_P0_IDS),
    (("media.", "sticker.", "gif.", "emoji."), PR6_P0_IDS),
    (("poll.", "reaction.", "todo.", "location.", "search."), PR9_P0_IDS),
    (("call.", "vc.", "conference."), PR11_P0_IDS),
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
        owned: set[str] = set()
        for _, expected in P0_OWNERS:
            owned |= set(expected)
        missing = sorted(owned - _covered_ids())
        assert missing == [], f"a landed PR dropped coverage of {missing}"

    @pytest.mark.parametrize(
        "prefixes,expected", P0_OWNERS, ids=["pr1", "pr2", "pr3", "pr6", "pr9", "pr11"]
    )
    def test_the_floor_is_the_whole_truth(self, prefixes, expected):
        """Each named list is exactly the P0 set its own groups claim.

        A floor that is a subset is a floor with holes in it: an op could drop
        a P0 id nobody wrote down and the gate would stay green. Computing the
        set here and comparing it to the literal above means new coverage has
        to be added to the list on purpose.
        """
        catalogue = catalog()
        actual = {
            cid
            for op_id, spec in REGISTRY.items()
            if op_id.startswith(prefixes)
            for cid in (*spec.covers, *spec.covers_partial)
            if cid in catalogue and catalogue[cid].priority == "P0"
        }
        assert actual == set(expected)

    def test_the_floor_is_the_sum_of_what_the_landed_prs_own(self):
        """The floor is not a number somebody typed: it is those lists, added up."""
        named: set[str] = set()
        for _, expected in P0_OWNERS:
            named |= set(expected)
        assert len(named) == P0_FLOOR

    def test_every_uncovered_id_is_waived_with_a_pr_number(self, report):
        """No silent gaps: the gate is meaningful from day one, not at the end."""
        unwaived = [u for u in report.uncovered if not u["reason"].startswith("waived")]
        assert unwaived == [], f"{len(unwaived)} ids are uncovered and unwaived: {unwaived[:5]}"

    def test_the_auth_domain_is_fully_accounted_for(self, report):
        """PR-2's own domain: implemented, or waived to a named later PR.

        The domain waiver that used to cover all 89 ids is gone. Leaving it
        would have meant PR-2 could land nothing and still read as done.
        """
        stats = report.by_domain["auth_sessions_security"]
        assert stats["accounted_percent"] == 100.0
        assert stats["covered"] >= 85

    def test_calls_voicechats_is_fully_accounted_for(self, report):
        """PR-11's own domain: implemented, or waived to a named other PR.

        The nine waivers left in it are all ids whose *subject* belongs to
        another group — privacy keys, admin rights, the admin log, top peers —
        not calls work nobody did.
        """
        stats = report.by_domain["calls_voicechats"]
        assert stats["accounted_percent"] == 100.0
        assert stats["covered"] >= 124

    def test_messages_core_is_fully_accounted_for(self, report):
        """PR-1's own domain: implemented, or waived to a named later PR."""
        stats = report.by_domain["messages_core"]
        assert stats["accounted_percent"] == 100.0
        assert stats["covered"] >= 130

    def test_dialogs_chats_is_fully_accounted_for(self, report):
        """PR-3's own domain: implemented, or waived to a named later PR.

        The domain-wide waiver is gone, so every id left in it names the group
        that owns it — which is what makes "the chat group is done" checkable
        rather than asserted.
        """
        stats = report.by_domain["dialogs_chats"]
        assert stats["accounted_percent"] == 100.0
        assert stats["covered"] >= 105

    def test_the_dialogs_chats_domain_is_no_longer_waived_wholesale(self):
        assert "dialogs_chats" not in waivers().domains

    def test_media_files_is_fully_accounted_for(self, report):
        """PR-6's own domain. The 22 remaining ids belong to other groups.

        They are catalogued here because they concern a file — a profile
        photo, a chat avatar, a notification sound — and every one of them is
        waived to the PR that owns that command group.
        """
        stats = report.by_domain["media_files"]
        assert stats["accounted_percent"] == 100.0
        assert stats["covered"] >= 118


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
