"""Incoming updates reach the daemon's event bus.

Until 2.0.1 none did: the daemon attached its update handler straight after
`AccountSession.start()`, which only schedules the supervisor, so it always
found `session.client is None` and returned. Jobs, webhooks and `watch` saw
the daemon's own sends and nothing else, and every job stopped with
`matched=0 skipped=0`.
"""

from __future__ import annotations

import asyncio

from telethon.tl import types

from tlgr.daemon.session import SessionState


async def _online(daemon, alias: str):
    session = await daemon.sessions.ensure(alias)
    deadline = asyncio.get_running_loop().time() + 2.0
    while session.state != SessionState.ONLINE:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{alias} never came online: {session.state}")
        await asyncio.sleep(0.01)
    return session


def _incoming(message_id: int = 7) -> types.UpdateNewChannelMessage:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(channel_id=1234),
        date=None,
        message="hello",
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)


class TestIncomingUpdates:
    async def test_an_incoming_message_is_numbered_on_the_bus(self, daemon, stub_account):
        session = await _online(daemon, stub_account)
        before = daemon.bus.latest_seq(stub_account)

        await session.client.feed(_incoming())

        assert daemon.bus.latest_seq(stub_account) == before + 1
        [envelope], _gap = daemon.bus.replay(stub_account, before)
        assert envelope.type == "message_new"
        assert envelope.chat_id == -1000000001234

    async def test_the_handler_is_attached_once_across_reconnects(self, daemon, stub_account):
        """The client outlives a reconnect, so a second handler would double every event."""
        session = await _online(daemon, stub_account)
        handlers = len(session.client._handlers)

        await session.client.disconnect()
        deadline = asyncio.get_running_loop().time() + 5.0
        while session.reconnects == 0 or session.state != SessionState.ONLINE:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the session never reconnected")
            await asyncio.sleep(0.01)

        assert len(session.client._handlers) == handlers
