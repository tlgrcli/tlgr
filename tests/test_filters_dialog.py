"""`sender_is_contact` and `chat_is_new`, on events built from real TL updates."""

from __future__ import annotations

import pytest
from fake_telethon import FakeTelegramClient, World, make_user
from telethon.tl import types

from tlgr.filters import dialog
from tlgr.filters.compose import evaluate, evaluate_async, parse_filter_config
from tlgr.gateway.engine import _builders
from tlgr.gateway.event import Event
from tlgr.gateway.tlevents import build_event

ALICE = 4242


@pytest.fixture(autouse=True)
def _fresh_caches():
    dialog.clear_caches()
    yield
    dialog.clear_caches()


def _private(message_id: int, *, sender=None) -> types.UpdateNewMessage:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerUser(user_id=ALICE),
        from_id=types.PeerUser(user_id=ALICE),
        date=None,
        message="hello",
    )
    update = types.UpdateNewMessage(message=message, pts=1, pts_count=1)
    if sender is not None:
        update._entities = {ALICE: sender}
    return update


async def _event(client, update) -> Event:
    built = await build_event(_builders(["new_message"]), update, client)
    assert built is not None
    return Event(source="telegram", raw=built[0], account="work")


class TestSenderIsContact:
    async def test_a_contact_matches(self):
        client = FakeTelegramClient(World())
        alice = make_user(ALICE)
        alice.contact = True
        event = await _event(client, _private(10, sender=alice))
        node = parse_filter_config({"sender_is_contact": True})
        assert (await evaluate_async(node, event))[0] is True

    async def test_a_stranger_does_not(self):
        client = FakeTelegramClient(World())
        event = await _event(client, _private(10, sender=make_user(ALICE)))
        ok, reason = await evaluate_async(parse_filter_config({"sender_is_contact": True}), event)
        assert ok is False
        assert "sender_is_contact=False" in reason
        ok, _ = await evaluate_async(parse_filter_config({"sender_is_contact": False}), event)
        assert ok is True

    async def test_the_answer_is_cached_for_an_update_without_the_entity(self):
        client = FakeTelegramClient(World())
        alice = make_user(ALICE)
        alice.contact = True
        await evaluate_async(
            parse_filter_config({"sender_is_contact": True}),
            await _event(client, _private(10, sender=alice)),
        )
        bare = await _event(client, _private(11))
        ok, _ = await evaluate_async(parse_filter_config({"sender_is_contact": True}), bare)
        assert ok is True


class TestChatIsNew:
    async def test_the_first_message_in_a_dialog_is_new(self):
        world = World()
        world.add_user(make_user(ALICE))
        client = FakeTelegramClient(world)
        world.add_message(ALICE, "hello", message_id=500, sender_id=ALICE)
        event = await _event(client, _private(500))
        assert (await evaluate_async(parse_filter_config({"chat_is_new": True}), event))[0]

    async def test_a_dialog_with_history_is_not_new(self):
        world = World()
        world.add_user(make_user(ALICE))
        client = FakeTelegramClient(world)
        world.add_message(ALICE, "earlier", message_id=400, sender_id=ALICE)
        world.add_message(ALICE, "hello", message_id=500, sender_id=ALICE)
        event = await _event(client, _private(500))
        ok, reason = await evaluate_async(parse_filter_config({"chat_is_new": True}), event)
        assert ok is False
        assert "chat_is_new=False" in reason

    async def test_a_busy_dialog_costs_one_probe(self):
        world = World()
        world.add_user(make_user(ALICE))
        client = FakeTelegramClient(world)
        probes = []
        original = client.get_messages

        async def counting(*args, **kwargs):
            probes.append(kwargs)
            return await original(*args, **kwargs)

        client.get_messages = counting
        world.add_message(ALICE, "first", message_id=500, sender_id=ALICE)
        node = parse_filter_config({"chat_is_new": True})
        assert (await evaluate_async(node, await _event(client, _private(500))))[0] is True
        for message_id in (501, 502, 503):
            world.add_message(ALICE, "more", message_id=message_id, sender_id=ALICE)
            ok, _ = await evaluate_async(node, await _event(client, _private(message_id)))
            assert ok is False
        assert len(probes) == 1


class TestSyncEvaluation:
    async def test_an_async_filter_rejects_with_a_reason_instead_of_leaking(self):
        client = FakeTelegramClient(World())
        event = await _event(client, _private(10, sender=make_user(ALICE)))
        ok, reason = evaluate(parse_filter_config({"chat_is_new": True}), event)
        assert ok is False
        assert "needs the job engine" in reason
