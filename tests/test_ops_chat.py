"""The chat and folder operations, end to end through a real daemon.

Every test goes over a real Unix socket, through the real middleware chain and
the real dispatcher, into the real implementation, against a fake Telegram.
The assertion is usually that the fake's *world changed* — the dialog moved
into folder 1, the folder's `include_peers` grew, `mute_until` is an absolute
timestamp near now + 8h — because "the request object looked plausible" is
what v1's tests asserted and it is how `chat mute 3600` shipped resolving to
1970 (COR-01).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_EMPTY,
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    classify,
)

ALICE = 4242
BOB = 4343
GROUP = 5150
GROUP_ID = -1000000000000 - GROUP


@pytest.fixture
def peers(world):
    """Two users and a supergroup, each with a dialog row."""
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice", first="Alice"))
    world.add_user(make_user(BOB, username="bob", first="Bob"))
    world.add_channel(make_channel(GROUP, title="News", megagroup=True))
    world.add_message(ALICE, "hello there", message_id=101)
    world.add_message(GROUP_ID, "group post", message_id=201)
    world.add_dialog(ALICE, top_message=101, unread_count=3)
    world.add_dialog(GROUP_ID, top_message=201, unread_count=0)
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


# ---------------------------------------------------------------------------
# chat list
# ---------------------------------------------------------------------------


class TestList:
    async def test_the_dialog_list_comes_back_with_its_peers(
        self, live_daemon, client, in_thread, peers
    ):
        items = await result(client, in_thread, "chat.list")
        by_id = {row["chat"]["id"]: row for row in items}
        assert by_id[ALICE]["chat"]["title"] == "Alice"
        assert by_id[ALICE]["unread_count"] == 3
        assert by_id[GROUP_ID]["chat"]["kind"] == "supergroup"

    async def test_ids_are_marked_so_a_channel_cannot_be_read_as_a_user(
        self, live_daemon, client, in_thread, peers
    ):
        """COR-10: one id shape everywhere, sign and all."""
        items = await result(client, in_thread, "chat.list")
        assert GROUP_ID in {row["chat"]["id"] for row in items}
        assert GROUP not in {row["chat"]["id"] for row in items}

    async def test_the_last_message_carries_the_history_shape(
        self, live_daemon, client, in_thread, peers
    ):
        """A dialog preview is a whole `Message`, so a service event is labelled.

        v1's preview carried id/date/out/text only, which made an empty text
        mean three unrelated things at once.
        """
        from telethon.tl import types

        service = peers.add_message(GROUP_ID, "", message_id=202)
        service.action = types.MessageActionChatJoinedByLink(inviter_id=ALICE)
        peers.dialog(GROUP_ID).top_message = 202

        items = await result(client, in_thread, "chat.list")
        rows = {r["chat"]["id"]: r for r in items}
        assert rows[ALICE]["last_message"]["text"] == "hello there"
        assert rows[GROUP_ID]["last_message"]["kind"] == "service"
        assert rows[GROUP_ID]["last_message"]["action"]["type"] == "chat_joined_by_link"

    async def test_unread_filters_include_a_manual_mark(
        self, live_daemon, client, in_thread, peers
    ):
        """A chat flagged by hand has unread_count 0 and still counts."""
        peers.add_dialog(GROUP_ID, top_message=201, unread_count=0, unread_mark=True)
        items = await result(client, in_thread, "chat.list", {"unread": True})
        assert {row["chat"]["id"] for row in items} == {ALICE, GROUP_ID}

    async def test_the_archive_is_a_different_folder(self, live_daemon, client, in_thread, peers):
        peers.dialog(GROUP_ID).folder_id = 1
        main = await result(client, in_thread, "chat.list")
        archived = await result(client, in_thread, "chat.list", {"folder": "archive"})
        assert {r["chat"]["id"] for r in main} == {ALICE}
        assert {r["chat"]["id"] for r in archived} == {GROUP_ID}

    async def test_search_matches_title_and_username(self, live_daemon, client, in_thread, peers):
        items = await result(client, in_thread, "chat.list", {"search": "ali"})
        assert [row["chat"]["id"] for row in items] == [ALICE]

    async def test_a_folder_is_evaluated_client_side(self, live_daemon, client, in_thread, peers):
        from telethon.tl import types

        peers.add_folder(
            2, "Work", include_peers=[types.InputPeerUser(user_id=ALICE, access_hash=0)]
        )
        items = await result(client, in_thread, "chat.list", {"folder": "Work"})
        assert [row["chat"]["id"] for row in items] == [ALICE]
        assert items[0]["folders"] == [2]

    async def test_an_unknown_folder_is_not_found(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.list", {"folder": "Nope"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_the_cursor_walks_forward_and_is_op_bound(
        self, live_daemon, client, in_thread, peers
    ):
        first = await call(client, in_thread, "chat.list", limit=1)
        assert first["page"]["has_more"] is True
        cursor = first["page"]["next_cursor"]
        second = await call(client, in_thread, "chat.list", limit=1, cursor=cursor)
        assert first["result"][0]["chat"]["id"] != second["result"][0]["chat"]["id"]

    async def test_a_cursor_from_another_op_is_refused(self, live_daemon, client, in_thread, peers):
        page = await call(client, in_thread, "chat.list", limit=1)
        with pytest.raises(Exception) as caught:
            await call(
                client, in_thread, "chat.saved.list", cursor=page["page"]["next_cursor"], limit=1
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_scope_inactive_reports_what_to_leave(
        self, live_daemon, client, in_thread, peers
    ):
        items = await result(client, in_thread, "chat.list", {"scope": "inactive"})
        assert items and items[0]["inactive_since"]

    async def test_common_chats_with_a_user(self, live_daemon, client, in_thread, peers):
        items = await result(client, in_thread, "chat.list", {"common_with": "@alice"})
        assert [row["chat"]["id"] for row in items] == [GROUP_ID]

    async def test_the_v1_shortcuts_reach_the_same_operation(self):
        from tlgr.registry import ALIASES

        assert ALIASES["chats"] == "chat.list"
        assert ALIASES["inbox"] == "chat.list"
        assert ALIASES["catchup"] == "chat.catchup"


# ---------------------------------------------------------------------------
# chat open / catchup / read / unread
# ---------------------------------------------------------------------------


class TestOpenAndCatchup:
    async def test_open_fetches_history_and_marks_read(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.open", {"chat": "@alice"})
        assert out["marked_read"] is True
        assert [m["text"] for m in out["messages"]] == ["hello there"]
        assert peers.read_inbox[ALICE] == 101

    async def test_no_read_is_a_silent_peek(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.open", {"chat": "@alice", "no_read": True})
        assert out.get("marked_read", False) is False
        assert ALICE not in peers.read_inbox

    async def test_catchup_reads_nothing(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.catchup")
        assert [c["id"] for c in out["chats"]] == [ALICE]
        assert out["chats"][0]["messages"][0]["text"] == "hello there"
        assert peers.read_inbox == {}, "catchup must never emit a read receipt"

    async def test_catchup_includes_a_hand_marked_chat(self, live_daemon, client, in_thread, peers):
        peers.dialog(GROUP_ID).unread_mark = True
        out = await result(client, in_thread, "chat.catchup")
        assert {c["id"] for c in out["chats"]} == {ALICE, GROUP_ID}

    async def test_read_sends_the_receipt(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.read", {"chat": ["@alice"]})
        assert out["read"] is True
        assert peers.called("send_read_acknowledge")[0]["chat_id"] == ALICE

    async def test_read_reports_per_peer_results_for_a_folder(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(client, in_thread, "chat.read", {"folder": "main"})
        assert {r["chat_id"] for r in out["results"]} == {ALICE, GROUP_ID}
        assert all(r["ok"] for r in out["results"])

    async def test_read_needs_something_to_read(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.read", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_unread_sets_the_manual_flag(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.unread", {"chat": "@alice"})
        assert out == {"chat_id": ALICE, "unread": True}
        assert peers.dialog(ALICE).unread_mark is True

    async def test_unread_clear_removes_it(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.unread", {"chat": "@alice"})
        await result(client, in_thread, "chat.unread", {"chat": "@alice", "clear": True})
        assert peers.dialog(ALICE).unread_mark is False


# ---------------------------------------------------------------------------
# chat get
# ---------------------------------------------------------------------------


class TestGet:
    async def test_get_reports_the_peer_and_the_dialog_row(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(client, in_thread, "chat.get", {"chat": "@alice"})
        assert out["id"] == ALICE
        assert out["type"] == "user"
        assert out["title"] == "Alice"
        assert out["name"] == "Alice", "v1 spelled the title `name`"

    async def test_full_adds_the_action_bar(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.get", {"chat": "@alice", "full": True})
        assert "settings" in out

    async def test_an_unknown_field_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.get", {"chat": "@alice", "field": "nope"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_an_unknown_peer_is_not_found(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.get", {"chat": "@nobody"})
        assert classify(caught.value).exit_code in (EXIT_NOT_FOUND, EXIT_INDETERMINATE)


# ---------------------------------------------------------------------------
# archive / mute / pin
# ---------------------------------------------------------------------------


class TestArchiveMutePin:
    async def test_archive_moves_the_dialog_into_folder_one(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(client, in_thread, "chat.archive", {"chat": ["@alice"]})
        assert out["archived"] is True
        assert out["chat_id"] == ALICE
        assert peers.dialog(ALICE).folder_id == 1

    async def test_undo_moves_it_back(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.archive", {"chat": ["@alice"]})
        await result(client, in_thread, "chat.archive", {"chat": ["@alice"], "undo": True})
        assert peers.dialog(ALICE).folder_id == 0

    async def test_many_peers_cost_one_request(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.archive", {"chat": ["@alice", str(GROUP_ID)]})
        sent = peers.called("EditPeerFoldersRequest")
        assert len(sent) == 1 and len(sent[0].folder_peers) == 2

    async def test_mute_for_a_duration_is_absolute_wall_clock_time(
        self, live_daemon, client, in_thread, peers
    ):
        """COR-01: v1 built this from the event loop's monotonic clock."""
        out = await result(client, in_thread, "chat.mute", {"chat": ["@alice"], "for_": 3600})
        assert abs(out["mute_until_unix"] - (time.time() + 3600)) < 30
        assert peers.dialog(ALICE).mute_until is not None

    async def test_mute_forever_is_the_sentinel(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.mute", {"chat": ["@alice"]})
        assert out["mute_until_unix"] == 2**31 - 1

    async def test_unmute_clears_it(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.mute", {"chat": ["@alice"]})
        out = await result(client, in_thread, "chat.mute", {"chat": ["@alice"], "off": True})
        assert out.get("muted", False) is False
        assert int(peers.dialog(ALICE).mute_until.timestamp()) == 0

    async def test_mute_a_whole_folder(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.mute", {"folder": "main"})
        assert set(out["chat_ids"]) == {ALICE, GROUP_ID}

    async def test_mute_needs_a_target(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.mute", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_pin_and_unpin_move_the_row(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.pin", {"chat": ["@alice"]})
        assert peers.dialog(ALICE).pinned is True
        await result(client, in_thread, "chat.pin", {"chat": ["@alice"], "unpin": True})
        assert peers.dialog(ALICE).pinned is False

    async def test_pin_order_rewrites_the_whole_list(self, live_daemon, client, in_thread, peers):
        peers.dialog(GROUP_ID).pinned = True
        out = await result(client, in_thread, "chat.pin", {"chat": ["@alice"], "order": True})
        assert out["order"] == [ALICE]
        assert peers.dialog(GROUP_ID).pinned is False

    async def test_pin_inside_a_folder_edits_the_filter(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_folder(2, "Work")
        await result(client, in_thread, "chat.pin", {"chat": ["@alice"], "folder": "Work"})
        assert not peers.called("ToggleDialogPinRequest")
        assert len(peers.filters[0].pinned_peers) == 1

    async def test_pinned_listing_is_in_order(self, live_daemon, client, in_thread, peers):
        peers.dialog(ALICE).pinned = True
        items = await result(client, in_thread, "chat.list", {"pinned": True})
        assert [row["chat"]["id"] for row in items] == [ALICE]
        assert items[0]["pinned_order"] == 0


# ---------------------------------------------------------------------------
# leave / delete / clear
# ---------------------------------------------------------------------------


class TestLeaveDeleteClear:
    async def test_leaving_a_supergroup_calls_leave_channel(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(client, in_thread, "chat.leave", {"chat": [str(GROUP_ID)]})
        assert out["left"] is True
        assert peers.called("LeaveChannelRequest")

    async def test_leave_removes_the_peer_from_every_folder(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.add_folder(
            2,
            "Work",
            include_peers=[types.InputPeerChannel(channel_id=GROUP, access_hash=0)],
        )
        await result(
            client,
            in_thread,
            "chat.leave",
            {"chat": [str(GROUP_ID)], "remove_from_folders": True},
        )
        assert peers.filters[0].include_peers == []

    async def test_delete_for_me_wipes_my_copy(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.delete", {"chat": "@alice"})
        assert out.get("scope", "me") == "me"
        assert peers.history(ALICE) == []

    async def test_delete_for_both_asks_the_server_to_revoke(
        self, live_daemon, client, in_thread, peers
    ):
        await result(client, in_thread, "chat.delete", {"chat": "@alice", "for_both": True})
        assert peers.called("DeleteHistoryRequest")[0].revoke is True

    async def test_clear_keeps_the_dialog_and_counts_what_went(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(client, in_thread, "chat.clear", {"chat": "@alice"})
        assert out["cleared"] is True
        assert out["messages_affected"] == 1
        assert peers.called("DeleteHistoryRequest")[0].just_clear is True

    async def test_a_date_range_is_refused_in_a_channel(
        self, live_daemon, client, in_thread, peers
    ):
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "chat.clear",
                {"chat": str(GROUP_ID), "since": "2026-01-01T00:00:00Z"},
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_destructive_ops_honour_dry_run(self, live_daemon, client, in_thread, peers):
        envelope = await call(client, in_thread, "chat.clear", {"chat": "@alice"}, dry_run=True)
        assert envelope["result"]["dry_run"] is True
        assert envelope["result"]["would"] == "chat.clear"
        assert peers.history(ALICE), "a dry run must not touch the world"


# ---------------------------------------------------------------------------
# typing / posters / mentions
# ---------------------------------------------------------------------------


class TestTypingPostersMentions:
    async def test_typing_broadcasts_the_action(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.typing", {"chat": "@alice", "duration": 0})
        assert out.get("action", "typing") == "typing"
        assert out["typing"] is True
        sent = peers.called("SetTypingRequest")[0]
        assert type(sent.action).__name__ == "SendMessageTypingAction"

    async def test_cancel_sends_the_cancel_action(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.typing", {"chat": "@alice", "cancel": True})
        sent = peers.called("SetTypingRequest")[0]
        assert type(sent.action).__name__ == "SendMessageCancelAction"

    async def test_an_unknown_action_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.typing", {"chat": "@alice", "action": "juggling"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_posters_counts_senders(self, live_daemon, client, in_thread, peers):
        for index in range(3):
            peers.add_message(GROUP_ID, f"m{index}", sender_id=ALICE)
        peers.add_message(GROUP_ID, "one", sender_id=BOB)
        out = await result(client, in_thread, "chat.poster.list", {"chat": str(GROUP_ID)})
        counts = {p["user_id"]: p["count"] for p in out["posters"]}
        assert counts[ALICE] == 3
        assert counts[BOB] == 1
        assert out["distinct_posters"] >= 2
        assert out["posters"][0]["id"] == ALICE, "v1 spelled the sender id `id`"

    async def test_posters_reports_a_flood_cut_scan_as_partial(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.errors import FloodWaitError
        from telethon.tl import types

        peers.add_message(GROUP_ID, "one", sender_id=ALICE)
        peers.fail_next("iter_messages", FloodWaitError(types.InputPeerSelf, capture=42))
        out = await result(client, in_thread, "chat.poster.list", {"chat": str(GROUP_ID)})
        assert out["partial"] is True
        assert out["flood_wait"] == 42

    async def test_posters_exits_empty_when_nobody_posted(
        self, live_daemon, client, in_thread, peers
    ):
        from tlgr.registry import get

        assert get("chat.poster.list").empty_exit == EXIT_EMPTY

    async def test_mentions_list_and_read(self, live_daemon, client, in_thread, peers):
        message = peers.add_message(GROUP_ID, "@me look", message_id=250)
        message.mentioned = True
        items = await result(
            client, in_thread, "chat.mention.list", {"chat": str(GROUP_ID), "read": True}
        )
        assert [m["id"] for m in items] == [250]
        assert peers.called("ReadMentionsRequest")


# ---------------------------------------------------------------------------
# per-chat settings
# ---------------------------------------------------------------------------


class TestSettings:
    async def test_notify_get_reports_the_exception_and_the_effective_value(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(client, in_thread, "chat.notify.get", {"chat": "@alice"})
        assert out["chat_id"] == ALICE
        assert out["scope"] == "users"
        assert "effective" in out

    async def test_notify_set_only_sends_the_field_it_changed(
        self, live_daemon, client, in_thread, peers
    ):
        await result(client, in_thread, "chat.notify.set", {"chat": "@alice", "silent": "on"})
        sent = peers.called("UpdateNotifySettingsRequest")[0]
        assert sent.settings.silent is True
        assert sent.settings.mute_until is None, "an untouched field must stay unset"

    async def test_notify_set_default_removes_the_exception(
        self, live_daemon, client, in_thread, peers
    ):
        await result(client, in_thread, "chat.notify.set", {"chat": "@alice", "silent": "default"})
        sent = peers.called("UpdateNotifySettingsRequest")[0]
        assert sent.settings.silent is None

    async def test_ttl_set_and_show(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.ttl.set", {"chat": "@alice", "period": "1d"})
        assert out["ttl_period"] == 86400
        assert peers.dialog(ALICE).ttl_period == 86400
        shown = await result(client, in_thread, "chat.ttl.set", {"chat": "@alice"})
        assert shown["ttl_period"] == 86400

    async def test_ttl_off_clears_it(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "chat.ttl.set", {"chat": "@alice", "period": "off"})
        assert peers.called("SetHistoryTTLRequest")[0].period == 0

    async def test_theme_list_and_set(self, live_daemon, client, in_thread, peers):
        themes = await result(client, in_thread, "chat.theme.list")
        assert themes[0]["emoticon"] == "🌷"
        out = await result(client, in_thread, "chat.theme.set", {"chat": "@alice", "emoji": "🌷"})
        assert out["theme"]["emoticon"] == "🌷"
        assert peers.called("SetChatThemeRequest")[0].theme.emoticon == "🌷"

    async def test_theme_unset_sends_the_empty_constructor(
        self, live_daemon, client, in_thread, peers
    ):
        await result(client, in_thread, "chat.theme.set", {"chat": "@alice", "unset": True})
        sent = peers.called("SetChatThemeRequest")[0]
        assert type(sent.theme).__name__ == "InputChatThemeEmpty"

    async def test_theme_set_needs_something_to_set(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.theme.set", {"chat": "@alice"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_wallpaper_by_slug(self, live_daemon, client, in_thread, peers):
        out = await result(
            client, in_thread, "chat.wallpaper.set", {"chat": "@alice", "slug": "pattern"}
        )
        assert out["chat_id"] == ALICE
        sent = peers.called("SetChatWallPaperRequest")[0]
        assert sent.wallpaper.slug == "pattern"

    async def test_a_bad_colour_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "chat.wallpaper.set", {"chat": "@alice", "color": "puce"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_translate_off_disables_the_bar(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.translate", {"chat": "@alice", "state": "off"})
        assert out["translations_disabled"] is True
        assert peers.called("TogglePeerTranslationsRequest")[0].disabled is True

    async def test_translate_takes_only_on_or_off(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.translate", {"chat": "@alice", "state": "maybe"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_set_sharing_off_is_noforwards(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.set", {"chat": "@alice", "sharing": "off"})
        assert out["noforwards"] is True
        assert peers.called("ToggleNoForwardsRequest")[0].enabled is True

    async def test_set_needs_a_switch(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.set", {"chat": "@alice"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_the_action_bar_reports_the_triage_signals(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.peer_settings[ALICE] = types.PeerSettings(
            report_spam=True, phone_country="DE", registration_month="03.2024"
        )
        out = await result(client, in_thread, "chat.action-bar.get", {"chat": "@alice"})
        assert out["report_spam"] is True
        assert out["phone_country"] == "DE"
        assert out["registration_month"] == "03.2024"

    async def test_hiding_the_bar_clears_it(self, live_daemon, client, in_thread, peers):
        out = await result(
            client, in_thread, "chat.action-bar.get", {"chat": "@alice", "hide": True}
        )
        assert out["hidden"] is True
        assert peers.called("HidePeerSettingsBarRequest")

    async def test_autoarchive_reads_modifies_and_writes_back(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.global_privacy = types.GlobalPrivacySettings(hide_read_marks=True)
        out = await result(client, in_thread, "chat.autoarchive.set", {"auto": "on"})
        assert out["archive_and_mute_new_noncontact_peers"] is True
        sent = peers.called("SetGlobalPrivacySettingsRequest")[0]
        assert sent.settings.hide_read_marks is True, "the other flags must survive"

    async def test_autoarchive_with_no_flags_only_reads(
        self, live_daemon, client, in_thread, peers
    ):
        await result(client, in_thread, "chat.autoarchive.set", {})
        assert not peers.called("SetGlobalPrivacySettingsRequest")


# ---------------------------------------------------------------------------
# badge / promo / saved / report / secret / import
# ---------------------------------------------------------------------------


class TestTheRest:
    async def test_the_badge_counts_chats_and_splits_out_muted_ones(
        self, live_daemon, client, in_thread, peers
    ):
        from datetime import datetime, timedelta, timezone

        peers.add_dialog(BOB, top_message=1, unread_count=5)
        peers.dialog(BOB).mute_until = datetime.now(timezone.utc) + timedelta(hours=1)
        out = await result(client, in_thread, "chat.badge.get")
        assert out["chats"] == 1
        assert out["messages"] == 3
        assert out["muted_chats"] == 1

    async def test_the_badge_can_include_muted_chats(self, live_daemon, client, in_thread, peers):
        from datetime import datetime, timedelta, timezone

        peers.add_dialog(BOB, top_message=1, unread_count=5)
        peers.dialog(BOB).mute_until = datetime.now(timezone.utc) + timedelta(hours=1)
        out = await result(client, in_thread, "chat.badge.get", {"include_muted": True})
        assert out["chats"] == 2
        assert out["messages"] == 8

    async def test_the_badge_can_report_the_limits(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.badge.get", {"limits": True})
        assert out["limits"]["channels_limit_default"] == 500

    async def test_promo_reports_the_suggestions(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.promo.list")
        assert out["pending_suggestions"] == ["VALIDATE_PHONE_NUMBER"]

    async def test_promo_can_dismiss_one(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.promo.list", {"dismiss": "BIRTHDAY_SETUP"})
        assert "BIRTHDAY_SETUP" in out["dismissed_suggestions"]
        assert peers.called("DismissSuggestionRequest")[0].suggestion == "BIRTHDAY_SETUP"

    async def test_saved_sublists_come_back_as_a_page(self, live_daemon, client, in_thread, peers):
        items = await result(client, in_thread, "chat.saved.list")
        assert {row["origin_id"] for row in items} >= {ALICE}

    async def test_report_walks_the_option_tree(self, live_daemon, client, in_thread, peers):
        first = await result(client, in_thread, "chat.report", {"chat": "@alice"})
        assert first.get("ok", False) is False
        assert first["title"] == "What is wrong?"
        option = first["options"][0]["option"]
        second = await result(
            client, in_thread, "chat.report", {"chat": "@alice", "option": option}
        )
        assert second["ok"] is True

    async def test_report_spam_is_the_one_shot_form(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.report", {"chat": "@alice", "spam": True})
        assert out["ok"] is True
        assert peers.called("ReportSpamRequest")

    async def test_a_hand_edited_option_is_a_usage_error(
        self, live_daemon, client, in_thread, peers
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "chat.report", {"chat": "@alice", "option": "not-hex"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    @pytest.mark.parametrize("op", ["chat.secret.list", "chat.secret.start", "chat.secret.send"])
    async def test_secret_chats_refuse_loudly(self, live_daemon, client, in_thread, peers, op):
        """Exit 13: "tlgr cannot do this" is not "the operation failed"."""
        request = {"id": 1} if op == "chat.secret.send" else {}
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, op, request)
        assert classify(caught.value).exit_code == EXIT_INDETERMINATE
        assert "secret" in str(caught.value).lower()

    async def test_discarding_a_secret_chat_works(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.secret.discard", {"id": 12})
        assert out["discarded"] is True
        assert peers.called("DiscardEncryptionRequest")[0].chat_id == 12

    async def test_import_checks_before_it_writes(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        export = tmp_path / "whatsapp.txt"
        export.write_text("[1/1/24] Alice: hi\n", encoding="utf-8")
        out = await result(
            client,
            in_thread,
            "chat.import",
            {"chat": "@alice", "export": str(export), "check": True},
        )
        assert out["state"] == "checked"
        assert peers.called("CheckHistoryImportPeerRequest")
        assert not peers.called("InitHistoryImportRequest")

    async def test_import_refuses_a_missing_file(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "chat.import", {"chat": "@alice", "export": "/nope.txt"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------


class TestFolders:
    async def test_create_picks_the_first_free_id(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "folder.create", {"title": "Work", "groups": True})
        assert out["id"] == 2
        assert out["title"] == "Work"
        assert peers.filters[0].groups is True

    async def test_create_needs_content(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "folder.create", {"title": "Empty"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_create_batches_every_peer_into_one_request(
        self, live_daemon, client, in_thread, peers
    ):
        await result(
            client,
            in_thread,
            "folder.create",
            {"title": "Work", "include": ["@alice", str(GROUP_ID)]},
        )
        sent = peers.called("UpdateDialogFilterRequest")
        assert len(sent) == 1 and len(sent[0].filter.include_peers) == 2

    async def test_list_reports_the_folders_in_order(self, live_daemon, client, in_thread, peers):
        peers.add_folder(2, "Work", groups=True)
        peers.add_folder(3, "Family")
        out = await result(client, in_thread, "folder.list")
        assert [f["title"] for f in out["folders"]] == ["Work", "Family"]

    async def test_list_with_counts_walks_the_dialog_list_once(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.add_folder(
            2, "Work", include_peers=[types.InputPeerUser(user_id=ALICE, access_hash=0)]
        )
        out = await result(client, in_thread, "folder.list", {"with_counts": True})
        assert out["folders"][0]["chats"] == 1
        assert out["folders"][0]["unread_messages"] == 3

    async def test_tags_can_be_toggled_and_honour_dry_run(
        self, live_daemon, client, in_thread, peers
    ):
        await result(client, in_thread, "folder.list", {"tags": "on"})
        assert peers.tags_enabled is True
        peers.tags_enabled = False
        await call(client, in_thread, "folder.list", {"tags": "on"}, dry_run=True)
        assert peers.tags_enabled is False, "a read command still honours --dry-run"

    async def test_edit_rewrites_the_whole_filter(self, live_daemon, client, in_thread, peers):
        peers.add_folder(2, "Work", groups=True)
        out = await result(client, in_thread, "folder.edit", {"folder": "Work", "title": "Clients"})
        assert out["title"] == "Clients"
        assert out["groups"] is True, "an untouched flag must survive the rewrite"

    async def test_a_shared_folder_refuses_type_flags(self, live_daemon, client, in_thread, peers):
        from telethon.tl import types

        peers.filters.append(
            types.DialogFilterChatlist(
                id=4,
                title=types.TextWithEntities(text="Shared", entities=[]),
                pinned_peers=[],
                include_peers=[],
            )
        )
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "folder.edit", {"folder": "Shared", "groups": True})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_add_drops_the_peer_from_the_exclude_list(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.add_folder(
            2, "Work", exclude_peers=[types.InputPeerUser(user_id=ALICE, access_hash=0)]
        )
        out = await result(client, in_thread, "folder.add", {"folder": "Work", "chat": ["@alice"]})
        assert out["include_peers"] == [ALICE]
        assert out.get("exclude_peers", []) == []

    async def test_remove_with_exclude_makes_it_stick(self, live_daemon, client, in_thread, peers):
        from telethon.tl import types

        peers.add_folder(
            2,
            "Work",
            groups=True,
            include_peers=[types.InputPeerUser(user_id=ALICE, access_hash=0)],
        )
        out = await result(
            client,
            in_thread,
            "folder.remove",
            {"folder": "Work", "chat": ["@alice"], "exclude": True},
        )
        assert out.get("include_peers", []) == []
        assert out["exclude_peers"] == [ALICE]

    async def test_delete_removes_a_plain_folder(self, live_daemon, client, in_thread, peers):
        peers.add_folder(2, "Work", groups=True)
        out = await result(client, in_thread, "folder.delete", {"folder": "Work"})
        assert out["deleted"] is True
        assert peers.filters == []

    async def test_leave_chats_is_refused_for_a_folder_you_made(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_folder(2, "Work", groups=True)
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "folder.delete", {"folder": "Work", "leave_chats": "all"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_deleting_a_shared_folder_leaves_through_chatlists(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.filters.append(
            types.DialogFilterChatlist(
                id=4,
                title=types.TextWithEntities(text="Shared", entities=[]),
                pinned_peers=[],
                include_peers=[],
            )
        )
        out = await result(
            client, in_thread, "folder.delete", {"folder": "Shared", "leave_chats": "suggested"}
        )
        assert out["suggested"] == [ALICE]
        assert peers.called("LeaveChatlistRequest")

    async def test_reorder_puts_the_main_list_where_it_is_told(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_folder(2, "Work")
        peers.add_folder(3, "Family")
        out = await result(client, in_thread, "folder.reorder", {"folder": ["Family", "Work"]})
        assert out["order"] == [3, 2]
        assert peers.called("UpdateDialogFiltersOrderRequest")[0].order == [3, 2]

    async def test_reorder_needs_an_order(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "folder.reorder", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_suggested_lists_and_adds(self, live_daemon, client, in_thread, peers):
        items = await result(client, in_thread, "folder.suggested.list")
        assert items[0]["title"] == "Unread"
        added = await result(client, in_thread, "folder.suggested.list", {"add": "Unread"})
        assert added[0]["added_id"] == 2
        assert peers.filters[0].id == 2

    async def test_a_folder_link_previews_without_joining(
        self, live_daemon, client, in_thread, peers
    ):
        out = await result(
            client, in_thread, "folder.join", {"link": "https://t.me/addlist/AbCdEf"}
        )
        assert out["slug"] == "AbCdEf"
        assert out["peers"] == [ALICE]
        assert out.get("joined", []) == []
        assert not peers.called("JoinChatlistInviteRequest")

    async def test_joining_names_the_chats(self, live_daemon, client, in_thread, peers):
        out = await result(
            client, in_thread, "folder.join", {"link": "AbCdEf", "chats": ["@alice"]}
        )
        assert out["joined"] == [ALICE]
        assert peers.called("JoinChatlistInviteRequest")

    async def test_a_share_link_needs_chats(self, live_daemon, client, in_thread, peers):
        peers.add_folder(2, "Work", groups=True)
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "folder.share.set", {"folder": "Work"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_share_link_is_created_from_the_folder(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_folder(2, "Work", groups=True)
        out = await result(
            client, in_thread, "folder.share.set", {"folder": "Work", "chats": ["@alice"]}
        )
        assert out["slug"] == "AbCdEf"
        assert out["url"].endswith("AbCdEf")

    async def test_share_list_is_a_page(self, live_daemon, client, in_thread, peers):
        peers.add_folder(2, "Work", groups=True)
        envelope = await call(client, in_thread, "folder.share.list", {"folder": "Work"})
        assert envelope["page"] == {"has_more": False, "next_cursor": None, "total": 0}

    async def test_share_delete_revokes_the_slug(self, live_daemon, client, in_thread, peers):
        peers.add_folder(2, "Work", groups=True)
        out = await result(
            client,
            in_thread,
            "folder.share.delete",
            {"folder": "Work", "slug": "https://t.me/addlist/AbCdEf"},
        )
        assert out == {"slug": "AbCdEf", "deleted": True}

    async def test_updates_are_only_a_shared_folder_thing(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_folder(2, "Work", groups=True)
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "folder.update.list", {"folder": "Work"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_updates_report_and_join_the_missing_peers(
        self, live_daemon, client, in_thread, peers
    ):
        from telethon.tl import types

        peers.filters.append(
            types.DialogFilterChatlist(
                id=4,
                title=types.TextWithEntities(text="Shared", entities=[]),
                pinned_peers=[],
                include_peers=[],
            )
        )
        out = await result(
            client, in_thread, "folder.update.list", {"folder": "Shared", "join": "all"}
        )
        assert out["missing_peers"] == [ALICE]
        assert out["joined"] == [ALICE]


# ---------------------------------------------------------------------------
# The v1 promises
# ---------------------------------------------------------------------------


class TestLegacyCompatibility:
    @pytest.mark.parametrize(
        "path",
        [
            "chat list",
            "chat open",
            "chat unread",
            "chat catchup",
            "chat get",
            "chat archive",
            "chat mute",
            "chat leave",
            "chat typing",
            "chat posters",
            "chats",
            "inbox",
            "catchup",
        ],
    )
    def test_every_v1_path_still_resolves(self, path):
        from tlgr.registry import canonical

        assert canonical(path).startswith("chat.")

    def test_chat_create_and_members_are_still_invocable(self):
        """They belong to PR-7, so they stay hand-written until then."""
        from tlgr.cli import cli

        chat = cli.commands["chat"]
        assert "create" in chat.commands
        assert "members" in chat.commands

    async def test_archive_still_answers_with_chat_id(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.archive", {"chat": ["@alice"]})
        assert out["archived"] is True and out["chat_id"] == ALICE

    async def test_typing_still_answers_with_typing(self, live_daemon, client, in_thread, peers):
        out = await result(client, in_thread, "chat.typing", {"chat": "@alice", "duration": 0})
        assert out["typing"] is True and out["chat_id"] == ALICE
