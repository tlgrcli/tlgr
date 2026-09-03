"""Entity resolution, and the distinction exit 13 exists for (§6.6).

The rule under test: a peer the strategies *exhausted* is NOT_FOUND (exit 5);
a peer whose search was truncated, flooded or errored is INDETERMINATE (exit
13). Reporting the second as the first is how an automation concludes "that
user has no dialog with us" when the truth is "we stopped looking".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fake_telethon import FakeTelegramClient, World, make_channel, make_user
from telethon.tl import types

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    IndeterminateError,
    NotFoundError,
    UsageError,
    classify,
)
from tlgr.core.peers import CachedPeer, PeerCache, PeerResolver, channel_id_from_link

# `asyncio_mode = "auto"` in pyproject collects the async tests; marking
# them explicitly would also mark the synchronous ones in this module.


def _resolver(world: World, tmp: Path, **kwargs: Any) -> PeerResolver:
    return PeerResolver(
        client=FakeTelegramClient(world),
        account="work",
        cache=PeerCache(tmp / "peers.json"),
        **kwargs,
    )


class TestLinks:
    def test_a_c_link_is_arithmetic(self):
        assert channel_id_from_link("https://t.me/c/1234/56") == (-1000000001234, 56)

    def test_a_c_link_without_a_message(self):
        assert channel_id_from_link("t.me/c/1234") == (-1000000001234, None)

    def test_an_ordinary_link_is_not_one(self):
        assert channel_id_from_link("https://t.me/durov") is None


class TestSelf:
    async def test_me_and_saved_are_the_self_peer(self, world, tlgr_home):
        resolver = _resolver(world, tlgr_home)
        for reference in ("me", "saved"):
            assert isinstance(await resolver.resolve(reference), types.InputPeerSelf)

    async def test_self_costs_no_network(self, world, tlgr_home):
        resolver = _resolver(world, tlgr_home)
        await resolver.resolve("me")
        assert world.calls == []


class TestUsernames:
    async def test_a_known_username_comes_from_the_cache(self, world, tlgr_home):
        world.add_user(make_user(5, username="alice"))
        resolver = _resolver(world, tlgr_home)
        peer = await resolver.resolve("@alice")
        assert isinstance(peer, types.InputPeerUser)
        assert peer.user_id == 5
        assert world.called("ResolveUsernameRequest") == []

    async def test_an_unknown_username_resolves_over_the_network(self, world, tlgr_home):
        user = make_user(9, username="bobby")

        class Resolved:
            users = [user]
            chats: list = []

        world.raw["ResolveUsernameRequest"] = Resolved()
        resolver = _resolver(world, tlgr_home)
        found = await resolver.resolve("@bobby")
        assert getattr(found, "user_id", None) == 9 or getattr(found, "id", None) == 9
        assert len(world.called("ResolveUsernameRequest")) == 1

    async def test_a_username_that_does_not_exist_is_not_found(self, world, tlgr_home):
        from telethon.errors import UsernameNotOccupiedError

        world.raw["ResolveUsernameRequest"] = lambda r: (_ for _ in ()).throw(
            UsernameNotOccupiedError(types.InputPeerSelf)
        )
        resolver = _resolver(world, tlgr_home)
        with pytest.raises(NotFoundError) as caught:
            await resolver.resolve("@ghost")
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_offline_resolution_is_indeterminate_not_negative(self, world, tlgr_home):
        """ "we did not look" is not "it is not there"."""
        resolver = _resolver(world, tlgr_home)
        with pytest.raises(IndeterminateError) as caught:
            await resolver.resolve("@nobody", allow_network=False)
        assert classify(caught.value).exit_code == EXIT_INDETERMINATE

    async def test_a_resolve_is_paced_by_the_limiter(self, world, tlgr_home):
        """`contacts.resolveUsername` floods at ~50 calls in a short period."""

        class Resolved:
            users = [make_user(9, username="bobby")]
            chats: list = []

        world.raw["ResolveUsernameRequest"] = Resolved()
        acquired: list[str] = []

        class Limiter:
            async def acquire(self, rate_class: str) -> None:
                acquired.append(rate_class)

        resolver = _resolver(world, tlgr_home)
        resolver.limiter = Limiter()
        await resolver.resolve("@bobby")
        assert acquired == ["resolve"]


class TestPhones:
    async def test_a_privacy_refusal_is_indeterminate(self, world, tlgr_home):
        """A number that hides itself has not been shown not to exist."""

        def refuse(request):
            raise RuntimeError("PHONE_PRIVACY_RESTRICTED")

        world.raw["ResolvePhoneRequest"] = refuse
        resolver = _resolver(world, tlgr_home)
        with pytest.raises(IndeterminateError) as caught:
            await resolver.resolve("+989123456789")
        assert "may exist" in str(caught.value)

    async def test_the_number_is_masked_in_the_error(self, world, tlgr_home):
        def refuse(request):
            raise RuntimeError("nope")

        world.raw["ResolvePhoneRequest"] = refuse
        resolver = _resolver(world, tlgr_home)
        with pytest.raises(IndeterminateError) as caught:
            await resolver.resolve("+989123456789")
        assert "9123456789" not in str(caught.value)


class TestDialogScan:
    async def test_a_scan_that_finds_the_peer_caches_it(self, world, tlgr_home):
        channel = make_channel(123, title="Group", megagroup=True)
        world.add_channel(channel)
        resolver = _resolver(world, tlgr_home)
        found = await resolver.resolve(-1000000000123)
        assert getattr(found, "channel_id", None) == 123

    async def test_an_exhausted_scan_is_not_found(self, world, tlgr_home):
        resolver = _resolver(world, tlgr_home, dialog_scan_max=100)
        with pytest.raises(NotFoundError):
            await resolver.resolve(-1000000000999)

    async def test_a_truncated_scan_is_indeterminate(self, world, tlgr_home):
        """The cap is the reason we do not know, and the caller must be told."""
        for index in range(5):
            world.add_channel(make_channel(1000 + index))
        resolver = _resolver(world, tlgr_home, dialog_scan_max=3)
        with pytest.raises(IndeterminateError) as caught:
            await resolver.resolve(-1000000009999)
        assert "dialog_scan_max" in str(caught.value)

    async def test_a_failing_scan_is_indeterminate(self, world, tlgr_home):
        resolver = _resolver(world, tlgr_home)

        def explode(*args, **kwargs):
            raise RuntimeError("the connection dropped mid-scan")

        resolver.client.iter_dialogs = explode
        with pytest.raises(IndeterminateError):
            await resolver.resolve(-1000000000123)


class TestMinEntities:
    async def test_a_user_seen_only_in_a_channel_is_still_addressable(self, world, tlgr_home):
        """Telethon never builds `InputPeerUserFromMessage`; this is why we do."""
        resolver = _resolver(world, tlgr_home)
        resolver.cache.put(CachedPeer(peer_id=-1000000000123, kind="channel", access_hash=99))
        resolver.remember_from_message(user_id=5, chat_id=-1000000000123, message_id=42)

        built = resolver._build(resolver.cache.get_id(5))
        assert isinstance(built, types.InputPeerUserFromMessage)
        assert built.user_id == 5
        assert built.msg_id == 42

    async def test_without_the_container_there_is_nothing_to_build(self, world, tlgr_home):
        resolver = _resolver(world, tlgr_home)
        resolver.remember_from_message(user_id=5, chat_id=-1000000000123, message_id=42)
        assert resolver._build(resolver.cache.get_id(5)) is None


class TestCache:
    def test_it_persists(self, tlgr_home: Path):
        path = tlgr_home / "peers.json"
        first = PeerCache(path)
        import time

        first.put(
            CachedPeer(
                peer_id=5, kind="user", access_hash=7, username="alice", resolved_at=time.time()
            )
        )
        first.save()

        second = PeerCache(path)
        assert second.get_id(5).access_hash == 7
        assert second.get_username("ALICE") is not None

    def test_the_file_is_private(self, tlgr_home: Path):
        path = tlgr_home / "peers.json"
        cache = PeerCache(path)
        cache.put(CachedPeer(peer_id=5, kind="user"))
        cache.save()
        assert path.stat().st_mode & 0o777 == 0o600

    def test_a_stale_username_is_not_reused(self, tlgr_home: Path):
        import time

        cache = PeerCache(tlgr_home / "peers.json")
        cache.put(
            CachedPeer(
                peer_id=5,
                kind="user",
                username="alice",
                resolved_at=time.time() - 48 * 3600,
            )
        )
        assert cache.get_username("alice") is None


class TestBadInput:
    async def test_nonsense_is_a_usage_error(self, world, tlgr_home):
        from tlgr.models.peer import PeerRef

        resolver = _resolver(world, tlgr_home)
        with pytest.raises(UsageError):
            await resolver.resolve(PeerRef(raw="???", kind="link", value="???"))


async def test_the_resolver_is_per_account(tlgr_home, stub_account, daemon):
    """An access hash minted for one account is meaningless to another."""
    session = await daemon.sessions.ensure(stub_account)
    first = session.resolver
    assert session.resolver is first
    assert first.account == stub_account
    assert first.cache.path == daemon.paths.peers_db(stub_account)
