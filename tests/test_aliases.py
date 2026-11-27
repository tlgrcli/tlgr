"""Remembered contacts: a saved wording resolves, anything else asks the user.

The rule under test is the one that keeps a message from reaching the wrong
person: only an *exact* (normalized) saved wording on *this* account resolves.
A lookalike alias, a contact whose display name matches, and a public
username that spells the name are suggestions in a PEER_NEEDS_USER error,
never a recipient, and nothing is sent.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
from fake_telethon import FakeTelegramClient, World, make_user
from telethon.tl import types

from tlgr.core.aliases import AliasStore, check_wording, new_record, similar_keys
from tlgr.core.errors import (
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    ConfigurationError,
    PeerNeedsUserError,
    UsageError,
    classify,
)
from tlgr.core.peers import PeerCache, PeerResolver
from tlgr.models.peer import normalize_name, parse_peer_ref, parse_user_ref

SARA = 4242
STRANGER = 4343
MOM = 4444


def _store(tmp: Path, account: str = "work") -> AliasStore:
    return AliasStore(tmp / account / "aliases.json")


def _resolver(world: World, tmp: Path, account: str = "work") -> PeerResolver:
    return PeerResolver(
        client=FakeTelegramClient(world),
        account=account,
        cache=PeerCache(tmp / account / "peers.json"),
        aliases=_store(tmp, account),
    )


def _save(store: AliasStore, wording: str, peer_id: int, *, name: str = "", access_hash: int = 0):
    store.put(new_record(wording, peer_id=peer_id, kind="user", name=name, access_hash=access_hash))


@pytest.fixture
def people(world: World) -> World:
    sara = make_user(SARA, username="sara_a", first="Sara")
    sara.last_name = "Ahmadi"
    world.add_contact(sara)
    # The public @sara is somebody else entirely: the wrong-person send.
    world.add_user(make_user(STRANGER, username="sara", first="Stranger"))
    world.add_contact(make_user(MOM, first="مامان"))
    return world


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Sara", "sara"),
            ("SARA  AHMADI", "sara ahmadi"),
            ("  sara\tahmadi ", "sara ahmadi"),
            ("Ｓａｒａ", "sara"),  # fullwidth, folded by NFKC
            ("Straße", "STRASSE"),  # casefold, not lower
            ("علی‌کار", "علیکار"),  # ZWNJ
            ("علی‍کار", "علیکار"),  # ZWJ
            ("علي", "علی"),  # Arabic ye
            ("على", "علی"),  # alef maksura
            ("كاوه", "کاوه"),  # Arabic kaf
            ("مُحَمَّد", "محمد"),  # harakat
            ("محـــمد", "محمد"),  # tatweel
            ("‏مامان", "مامان"),  # a pasted direction mark
        ],
    )
    def test_spellings_of_one_wording_share_a_key(self, a, b):
        assert normalize_name(a) == normalize_name(b)

    @pytest.mark.parametrize(("a", "b"), [("sara", "saras"), ("ali", "alireza"), ("مامان", "ماما")])
    def test_no_stemming(self, a, b):
        assert normalize_name(a) != normalize_name(b)


# ---------------------------------------------------------------------------
# parsing: the name kind, and what it must not change
# ---------------------------------------------------------------------------


class TestParsing:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("مامان", "مامان"),
            ("علي كار", "علی کار"),
            ("Sara Ahmadi", "sara ahmadi"),
            ("ali", "ali"),  # too short to be a username
            ("mom.work", "mom.work"),  # not a username form
            ("name:sara", "sara"),
            ("NAME: Sara ", "sara"),
        ],
    )
    def test_free_text_is_a_name(self, text, value):
        ref = parse_peer_ref(text)
        assert (ref.kind, ref.value) == ("name", value)

    def test_a_bare_username_still_means_the_username(self):
        """Changing this would silently retarget every existing script."""
        ref = parse_peer_ref("sara")
        assert (ref.kind, ref.value) == ("username", "sara")

    @pytest.mark.parametrize(
        "text", ["!!!", "@ab", "+abc", "https://example.com/x", "name:", "name:!!"]
    )
    def test_malformed_handles_are_still_parse_errors(self, text):
        with pytest.raises(ValueError):
            parse_peer_ref(text)

    def test_a_user_argument_accepts_a_name(self):
        assert parse_user_ref("مامان").kind == "name"


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class TestStore:
    def test_the_file_is_owner_only_and_round_trips(self, tmp_path):
        store = _store(tmp_path)
        _save(store, "Sara", SARA, name="Sara Ahmadi", access_hash=99)
        mode = stat.S_IMODE(store.path.stat().st_mode)  # type: ignore[union-attr]
        assert mode == 0o600
        record = store.get("sara")
        assert record is not None
        assert (record.alias, record.peer_id, record.access_hash) == ("Sara", SARA, 99)

    def test_put_returns_what_it_replaced(self, tmp_path):
        store = _store(tmp_path)
        _save(store, "Sara", SARA)
        previous = store.put(new_record("sara", peer_id=STRANGER, kind="user"))
        assert previous is not None and previous.peer_id == SARA

    def test_a_corrupt_file_is_never_overwritten(self, tmp_path):
        store = _store(tmp_path)
        store.path.parent.mkdir(parents=True)  # type: ignore[union-attr]
        store.path.write_text("{not json")  # type: ignore[union-attr]
        with pytest.raises(ConfigurationError):
            _save(store, "Sara", SARA)
        assert store.path.read_text() == "{not json"  # type: ignore[union-attr]
        assert store.get_quietly("sara") is None

    @pytest.mark.parametrize("wording", ["@sara", "12345", "+15550001111", "me", "saved", "t.me/x"])
    def test_an_alias_can_never_shadow_a_real_reference(self, wording):
        with pytest.raises(UsageError):
            check_wording(wording)

    def test_lookalikes_never_include_the_exact_key(self):
        assert similar_keys("sara", ["sara", "sarah", "sara work", "bob"]) == ["sara work", "sarah"]


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------


class TestResolve:
    async def test_an_exact_alias_resolves_from_the_store_without_network(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        _save(resolver.aliases, "Sara", SARA, access_hash=people.users[SARA].access_hash or 0)
        peer = await resolver.resolve("name:sara")
        assert isinstance(peer, types.InputPeerUser)
        assert peer.user_id == SARA
        assert people.calls == []

    async def test_persian_spelling_variants_resolve_to_the_same_alias(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        _save(resolver.aliases, "علی‌کار", MOM)
        for typed in ("علیکار", "علي‌كار", "علـــی‌کار"):
            peer = await resolver.resolve(typed)
            assert peer.user_id == MOM

    async def test_an_unknown_name_sends_nothing_and_asks_the_user(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        _save(resolver.aliases, "Sara work", SARA, name="Sara Ahmadi")
        with pytest.raises(PeerNeedsUserError) as caught:
            await resolver.resolve("name:sara")
        error = caught.value
        assert classify(error).exit_code == EXIT_USAGE
        details = error.details or {}
        assert details["reason"] == "unknown"
        assert details["nothing_sent"] is True
        assert "contact alias set sara <PEER>" in details["save_with"]
        sources = {(c["source"], c["peer_id"]) for c in details["candidates"]}
        # The lookalike alias and the contact named Sara are suggestions...
        assert ("alias", SARA) in sources
        assert ("contact", SARA) in sources
        # ...and the public @sara is not even suggested.
        assert all(c["peer_id"] != STRANGER for c in details["candidates"])
        assert "contact alias set" in error.hint
        assert people.called("ResolveUsernameRequest") == []

    async def test_a_unique_exact_contact_name_is_still_only_a_candidate(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        with pytest.raises(PeerNeedsUserError) as caught:
            await resolver.resolve("مامان")
        candidates = (caught.value.details or {})["candidates"]
        assert candidates[0]["match"] == "exact_name"
        assert candidates[0]["peer_id"] == MOM

    async def test_a_stale_alias_asks_again(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        _save(resolver.aliases, "Old friend", 777777)
        with pytest.raises(PeerNeedsUserError) as caught:
            await resolver.resolve("old friend")
        details = caught.value.details or {}
        assert details["reason"] == "stale"
        assert details["stored"]["peer_id"] == 777777
        assert "--replace" in details["save_with"]

    async def test_a_bare_word_that_is_also_an_alias_is_refused(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        _save(resolver.aliases, "sara", SARA)
        with pytest.raises(PeerNeedsUserError) as caught:
            await resolver.resolve("sara")
        assert (caught.value.details or {})["reason"] == "ambiguous"
        assert "name:sara" in caught.value.hint
        assert people.called("ResolveUsernameRequest") == []
        # The two unambiguous spellings each still work.
        assert (await resolver.resolve("name:sara")).user_id == SARA
        assert (await resolver.resolve("@sara")).user_id == STRANGER

    async def test_a_bare_word_without_an_alias_is_the_username_as_before(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        assert (await resolver.resolve("sara")).user_id == STRANGER

    async def test_aliases_are_per_account(self, people, tlgr_home):
        _save(_store(tlgr_home, "work"), "Sara", SARA)
        other = _resolver(people, tlgr_home, account="home")
        with pytest.raises(PeerNeedsUserError):
            await other.resolve("name:sara")

    async def test_an_unreadable_store_refuses_rather_than_guessing(self, people, tlgr_home):
        resolver = _resolver(people, tlgr_home)
        assert resolver.aliases.path is not None
        resolver.aliases.path.parent.mkdir(parents=True, exist_ok=True)
        resolver.aliases.path.write_text("[]")
        with pytest.raises(ConfigurationError):
            await resolver.resolve("name:sara")
        # A bare username is not held hostage by a broken alias file.
        assert (await resolver.resolve("sara")).user_id == STRANGER


async def test_the_session_resolver_keeps_aliases_next_to_the_peer_cache(
    tlgr_home, stub_account, daemon
):
    session = await daemon.sessions.ensure(stub_account)
    assert session.resolver.aliases.path == daemon.paths.aliases(stub_account)
    assert daemon.paths.aliases(stub_account).parent == daemon.paths.peers_db(stub_account).parent


# ---------------------------------------------------------------------------
# end to end through the daemon
# ---------------------------------------------------------------------------


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


class TestThroughTheDaemon:
    async def test_an_unknown_name_sends_nothing(self, live_daemon, client, in_thread, people):
        with pytest.raises(PeerNeedsUserError) as caught:
            await result(client, in_thread, "message.send", {"chat": "Sara Ahmadi", "text": "hi"})
        assert people.called("SendMessageRequest") == []
        details = caught.value.details or {}
        assert details["reason"] == "unknown"
        assert details["account"] == "work"
        assert any(c["peer_id"] == SARA for c in details["candidates"])

    async def test_a_gateway_job_resolves_an_alias(self, live_daemon, people):
        _save(_store(live_daemon.paths.accounts), "Sara", SARA)
        session = await live_daemon.sessions.ensure("work")
        assert await session.job_client.resolve_chat("name:sara") == SARA

    async def test_dialog_status_asks_rather_than_answering_unknown(
        self, live_daemon, client, in_thread, people
    ):
        """An unsaved name must not become a three-valued 'unknown' that hides the ask."""
        with pytest.raises(PeerNeedsUserError):
            await result(client, in_thread, "user.dialog-status", {"user": "Sara Ahmadi"})


class TestOps:
    async def test_set_then_send_by_name(self, live_daemon, client, in_thread, people):
        saved = await result(
            client, in_thread, "contact.alias.set", {"alias": "Sara", "peer": "@sara_a"}
        )
        assert (saved["peer_id"], saved["name"], saved["key"]) == (SARA, "Sara Ahmadi", "sara")
        sent = await result(
            client, in_thread, "message.send", {"chat": "name:Sara", "text": "on my way"}
        )
        assert sent["chat_id"] == SARA
        assert [m.message for m in people.history(STRANGER)] == []

    async def test_a_persian_alias_sends_through_any_spelling(
        self, live_daemon, client, in_thread, people
    ):
        await result(client, in_thread, "contact.alias.set", {"alias": "مامان", "peer": MOM})
        sent = await result(client, in_thread, "message.send", {"chat": "‏مامان", "text": "سلام"})
        assert sent["chat_id"] == MOM

    async def test_repointing_needs_replace(self, live_daemon, client, in_thread, people):
        await result(client, in_thread, "contact.alias.set", {"alias": "Sara", "peer": SARA})
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "contact.alias.set", {"alias": "sara", "peer": STRANGER}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE
        moved = await result(
            client,
            in_thread,
            "contact.alias.set",
            {"alias": "sara", "peer": STRANGER, "replace": True},
        )
        assert moved["peer_id"] == STRANGER
        assert moved["replaced"]["peer_id"] == SARA

    async def test_setting_the_same_alias_twice_is_already(
        self, live_daemon, client, in_thread, people
    ):
        await result(client, in_thread, "contact.alias.set", {"alias": "Sara", "peer": SARA})
        again = await call(client, in_thread, "contact.alias.set", {"alias": "Sara", "peer": SARA})
        assert again["result"]["already"] is True

    async def test_the_peer_must_be_exact(self, live_daemon, client, in_thread, people):
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "contact.alias.set", {"alias": "boss", "peer": "Sara Ahmadi"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert classify(caught.value).field == "peer"

    async def test_an_existing_alias_can_be_the_peer(self, live_daemon, client, in_thread, people):
        await result(client, in_thread, "contact.alias.set", {"alias": "Sara", "peer": SARA})
        second = await result(
            client, in_thread, "contact.alias.set", {"alias": "سارا", "peer": "name:sara"}
        )
        assert second["peer_id"] == SARA

    async def test_list_groups_aliases_per_person(self, live_daemon, client, in_thread, people):
        for alias in ("Sara", "سارا"):
            await result(client, in_thread, "contact.alias.set", {"alias": alias, "peer": SARA})
        await result(client, in_thread, "contact.alias.set", {"alias": "مامان", "peer": MOM})
        rows = await result(client, in_thread, "contact.alias.list")
        by_peer = {row["peer_id"]: row for row in rows}
        assert sorted(by_peer[SARA]["aliases"]) == sorted(["Sara", "سارا"])
        assert by_peer[MOM]["aliases"] == ["مامان"]

    async def test_remove_is_exact(self, live_daemon, client, in_thread, people):
        await result(client, in_thread, "contact.alias.set", {"alias": "Sara work", "peer": SARA})
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.alias.remove", {"alias": "Sara"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND
        gone = await result(client, in_thread, "contact.alias.remove", {"alias": "SARA  WORK"})
        assert (gone["removed"], gone["peer_id"]) == (True, SARA)
        assert await result(client, in_thread, "contact.alias.list") == []
