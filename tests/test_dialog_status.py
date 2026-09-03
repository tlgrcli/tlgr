"""`user dialog-status` must never turn "I can't tell" into "no dialog".

Regression for the 2026-08-31 cold-contact incident. The guard that stops a
second account from re-greeting someone probed with `message list` and read
Telethon's "Could not find the input entity" as PROOF of no history. It is
not: `get_input_entity` for a bare numeric id only consults the local entity
cache, and its network fallback (`users.GetUsers` with access_hash=0) returns
`UserEmpty` for any non-contact. During that run id 1863814631 was refused as
unresolvable on Pouri2048 and, minutes later, the identical probe on the same
account returned a real outgoing message from 2026-08-21.

So the unresolvable case must come back as `resolved: false`, and the only
thing that licenses `has_dialog: false` is an *exhausted* server-side dialog
list.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetHistoryRequest, GetPeerDialogsRequest
from telethon.tl.types import InputPeerUser, User

from tlgr.core.client import ClientWrapper
from tlgr.core.errors import EXIT_CODE_MAP, EXIT_INDETERMINATE


class _TotalList(list):
    """Mirrors Telethon's TotalList: a list that carries a server-side total."""

    def __init__(self, items, total):
        super().__init__(items)
        self.total = total


def _dialog(user: User, top_message: int = 0):
    return SimpleNamespace(id=user.id, entity=user, top_message=top_message)


class _FakeTelethon:
    """A Telethon stand-in whose entity cache can be cold on purpose."""

    def __init__(
        self, *, dialogs=None, cached_ids=(), totals=None, peer_dialog_top=None, dialogs_raise=None
    ):
        self._dialogs = dialogs or []
        self._cached = set(cached_ids)
        self._totals = totals or {}
        self._peer_dialog_top = peer_dialog_top or {}
        self._dialogs_raise = dialogs_raise
        self.dialogs_iterated = 0

    async def get_input_entity(self, ref):
        if isinstance(ref, User):
            return InputPeerUser(ref.id, access_hash=ref.access_hash or 0)
        if isinstance(ref, InputPeerUser):
            return ref
        if isinstance(ref, int) and ref in self._cached:
            return InputPeerUser(ref, access_hash=123)
        # The exact failure the old guard mis-read as "no history".
        raise ValueError(
            f"Could not find the input entity for PeerUser(user_id={ref}). "
            "Please read https://docs.telethon.dev/en/stable/concepts/entities.html"
        )

    async def iter_dialogs(self):
        idx = 0
        while True:
            if self._dialogs_raise is not None and idx == self._dialogs_raise[0]:
                raise self._dialogs_raise[1]
            if idx >= len(self._dialogs):
                return
            self.dialogs_iterated += 1
            yield self._dialogs[idx]
            idx += 1

    async def __call__(self, request):
        assert isinstance(request, GetPeerDialogsRequest)
        peer = request.peers[0].peer
        uid = peer.user_id
        top = self._peer_dialog_top.get(uid, 0)
        return SimpleNamespace(dialogs=[SimpleNamespace(top_message=top)])

    async def get_messages(self, peer, limit=1, **kw):
        uid = getattr(peer, "user_id", getattr(peer, "id", None))
        total = self._totals.get(uid, 0)
        return _TotalList([SimpleNamespace(id=1)] if total else [], total)


def _wrap(fake) -> ClientWrapper:
    w = ClientWrapper(Path("/nonexistent"), 1, "x")
    w._client = fake
    return w


def _u(uid, username=None):
    return User(id=uid, first_name=f"u{uid}", username=username, access_hash=99)


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------


def test_unresolvable_id_is_never_reported_as_no_dialog():
    """THE bug. A cold cache must produce "unknown", not a green light."""
    fake = _FakeTelethon(
        dialogs=[], cached_ids=(), dialogs_raise=(0, FloodWaitError(GetHistoryRequest, capture=30))
    )
    r = asyncio.run(_wrap(fake).dialog_status(1863814631))
    assert r["resolved"] is False
    assert r["has_dialog"] is None  # NOT False
    assert r["source"] == "unknown"
    assert r["message_count"] is None
    assert "did not complete" in r["reason"]


def test_cold_cache_still_finds_a_real_dialog_via_the_server_scan():
    """The 2026-08-31 case: unresolvable id, but the account HAS history."""
    target = _u(1863814631, "E_Gurl")
    fake = _FakeTelethon(
        dialogs=[_dialog(_u(1)), _dialog(target), _dialog(_u(3))],
        cached_ids=(),  # entity cache is cold
        totals={1863814631: 12},
        peer_dialog_top={1863814631: 44},
    )
    r = asyncio.run(_wrap(fake).dialog_status(1863814631))
    assert r["resolved"] is True
    assert r["has_dialog"] is True
    assert r["message_count"] == 12
    assert r["source"] == "dialog_scan"
    assert r["username"] == "E_Gurl"


def test_exhausted_dialog_list_is_the_only_licence_for_a_negative():
    fake = _FakeTelethon(dialogs=[_dialog(_u(1)), _dialog(_u(2))], cached_ids=())
    r = asyncio.run(_wrap(fake).dialog_status(777))
    assert r["resolved"] is True
    assert r["has_dialog"] is False
    assert r["message_count"] == 0
    assert r["source"] == "dialog_scan"
    assert r["scanned_dialogs"] == 2
    assert "complete dialog list" in r["reason"]


def test_scan_cap_is_indeterminate_not_a_negative():
    """A truncated scan proves nothing; it must not read as 'clear to send'."""
    fake = _FakeTelethon(dialogs=[_dialog(_u(i)) for i in range(1, 51)], cached_ids=())
    r = asyncio.run(_wrap(fake).dialog_status(9999, max_dialogs=10))
    assert r["resolved"] is False
    assert r["has_dialog"] is None
    assert r["scanned_dialogs"] == 10
    assert "cap" in r["reason"]


def test_flood_wait_mid_scan_is_indeterminate():
    fake = _FakeTelethon(
        dialogs=[_dialog(_u(1)), _dialog(_u(2)), _dialog(_u(3))],
        cached_ids=(),
        dialogs_raise=(2, FloodWaitError(GetHistoryRequest, capture=17)),
    )
    r = asyncio.run(_wrap(fake).dialog_status(3))
    assert r["resolved"] is False
    assert r["has_dialog"] is None


# --------------------------------------------------------------------------
# The cheap path
# --------------------------------------------------------------------------


def test_cached_peer_is_confirmed_against_the_server_not_the_cache():
    """Resolving an entity is not evidence of a dialog — a group co-member
    resolves fine and has never been messaged. Only the server's answer counts."""
    fake = _FakeTelethon(cached_ids=(555,), totals={555: 0}, peer_dialog_top={555: 0})
    r = asyncio.run(_wrap(fake).dialog_status(555))
    assert r["resolved"] is True
    assert r["has_dialog"] is False
    assert r["source"] == "peer_dialogs"
    assert fake.dialogs_iterated == 0  # no scan needed


def test_cached_peer_with_history_reports_the_server_message_total():
    fake = _FakeTelethon(cached_ids=(555,), totals={555: 12}, peer_dialog_top={555: 88})
    r = asyncio.run(_wrap(fake).dialog_status(555))
    assert (r["resolved"], r["has_dialog"], r["message_count"]) == (True, True, 12)
    assert r["source"] == "peer_dialogs"


def test_top_message_alone_establishes_a_dialog():
    """A dialog whose history the peer wiped still counts as prior contact."""
    fake = _FakeTelethon(cached_ids=(555,), totals={555: 0}, peer_dialog_top={555: 5})
    r = asyncio.run(_wrap(fake).dialog_status(555))
    assert r["has_dialog"] is True


def test_string_numeric_ref_behaves_like_the_int():
    fake = _FakeTelethon(
        dialogs=[_dialog(_u(42))], cached_ids=(), totals={42: 4}, peer_dialog_top={42: 9}
    )
    r = asyncio.run(_wrap(fake).dialog_status("42"))
    assert r["resolved"] is True and r["has_dialog"] is True


def test_username_ref_is_matched_during_the_scan():
    fake = _FakeTelethon(
        dialogs=[_dialog(_u(1)), _dialog(_u(2, "someone"))],
        cached_ids=(),
        totals={2: 3},
        peer_dialog_top={2: 9},
    )
    r = asyncio.run(_wrap(fake).dialog_status("@someone"))
    assert r["resolved"] is True and r["has_dialog"] is True and r["id"] == 2


def test_indeterminate_has_its_own_stable_exit_code():
    assert EXIT_CODE_MAP["INDETERMINATE"]["code"] == EXIT_INDETERMINATE == 13
    assert EXIT_INDETERMINATE not in (0, 3)  # not success, not "empty results"


@pytest.mark.parametrize("outcome", ["positive", "negative", "indeterminate"])
def test_the_three_outcomes_are_distinguishable(outcome):
    if outcome == "positive":
        fake = _FakeTelethon(cached_ids=(5,), totals={5: 2}, peer_dialog_top={5: 7})
    elif outcome == "negative":
        fake = _FakeTelethon(dialogs=[_dialog(_u(1))], cached_ids=())
    else:
        fake = _FakeTelethon(dialogs=[], cached_ids=(), dialogs_raise=(0, RuntimeError("rpc died")))
    r = asyncio.run(_wrap(fake).dialog_status(5))
    seen = (r["resolved"], r["has_dialog"])
    assert (
        seen
        == {
            "positive": (True, True),
            "negative": (True, False),
            "indeterminate": (False, None),
        }[outcome]
    )


def test_scan_hit_wins_even_if_the_follow_up_count_is_zero():
    """Presence in the dialog list IS the dialog. A zero message total (both
    sides wiped history) must not downgrade that to "never spoke"."""
    fake = _FakeTelethon(
        dialogs=[_dialog(_u(8))], cached_ids=(), totals={8: 0}, peer_dialog_top={8: 0}
    )
    r = asyncio.run(_wrap(fake).dialog_status(8))
    assert r["resolved"] is True
    assert r["has_dialog"] is True
    assert r["source"] == "dialog_scan"
