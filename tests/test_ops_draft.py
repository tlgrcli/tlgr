"""The draft operations: the handover point between an agent and a person.

A draft is the one thing an agent can leave in a chat that a human still has
to approve, so "did it actually get saved, with the reply target intact" is
worth asserting against a world that changes rather than against a request.
"""

from __future__ import annotations

from typing import Any

import pytest

from tlgr.core.errors import EXIT_USAGE, classify

ALICE = 4242
BOB = 4343


@pytest.fixture
def peers(world):
    from fake_telethon import make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_user(make_user(BOB, username="bobby", first="Bob"))
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


class TestSet:
    async def test_a_draft_is_saved_server_side(self, live_daemon, client, in_thread, peers):
        draft = await result(
            client, in_thread, "draft.set", {"chat": "@alice", "text": "will confirm"}
        )
        assert draft["chat_id"] == ALICE
        assert draft["text"] == "will confirm"
        assert peers.drafts[ALICE].message == "will confirm"

    async def test_nothing_is_sent(self, live_daemon, client, in_thread, peers):
        """The whole point: a draft is not a message."""
        await result(client, in_thread, "draft.set", {"chat": "@alice", "text": "careful"})
        assert peers.history(ALICE) == []
        assert peers.called("SendMessageRequest") == []

    async def test_the_reply_target_survives(self, live_daemon, client, in_thread, peers):
        await result(
            client,
            in_thread,
            "draft.set",
            {"chat": "@alice", "text": "about that", "reply_to": 77},
        )
        request = peers.called("SaveDraftRequest")[0]
        assert request.reply_to.reply_to_msg_id == 77

    async def test_markdown_becomes_entities(self, live_daemon, client, in_thread, peers):
        await result(
            client,
            in_thread,
            "draft.set",
            {"chat": "@alice", "text": "**yes**", "parse": "md"},
        )
        request = peers.called("SaveDraftRequest")[0]
        assert request.message == "yes"
        assert [type(e).__name__ for e in request.entities] == ["MessageEntityBold"]

    async def test_a_rich_body_is_refused(self, live_daemon, client, in_thread, peers, tmp_path):
        body = tmp_path / "b.md"
        body.write_text("# x")
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "draft.set",
                {"chat": "@alice", "rich_markdown": str(body)},
            )
        assert classify(caught.value).code == "NOT_SUPPORTED"


class TestList:
    async def test_only_non_empty_drafts_are_listed(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "draft.set", {"chat": "@alice", "text": "one"})
        await result(client, in_thread, "draft.set", {"chat": "@bobby", "text": "two"})
        page = await call(client, in_thread, "draft.list")
        assert {item["text"] for item in page["result"]} == {"one", "two"}

    async def test_a_single_chat_can_be_asked_for(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "draft.set", {"chat": "@alice", "text": "one"})
        page = await call(client, in_thread, "draft.list", {"chat": "@alice"})
        assert [item["chat_id"] for item in page["result"]] == [ALICE]

    async def test_chat_ids_are_marked(self, live_daemon, client, in_thread, world):
        """COR-10: v1 reported the raw entity id here and the marked one elsewhere."""
        from fake_telethon import make_channel

        world.add_channel(make_channel(9000, title="News", megagroup=True))
        await result(client, in_thread, "draft.set", {"chat": "-1000000009000", "text": "x"})
        page = await call(client, in_thread, "draft.list")
        assert [item["chat_id"] for item in page["result"]] == [-1000000009000]


class TestClear:
    async def test_clearing_one_draft_removes_it(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "draft.set", {"chat": "@alice", "text": "one"})
        cleared = await result(client, in_thread, "draft.clear", {"chat": "@alice"})
        assert cleared["cleared"] is True
        assert ALICE not in peers.drafts

    async def test_clearing_all_takes_every_draft(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "draft.set", {"chat": "@alice", "text": "one"})
        await result(client, in_thread, "draft.set", {"chat": "@bobby", "text": "two"})
        await result(client, in_thread, "draft.clear", {"clear_all": True})
        assert peers.drafts == {}

    async def test_clearing_without_a_target_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "draft.clear", {})
        assert classify(caught.value).exit_code == EXIT_USAGE
