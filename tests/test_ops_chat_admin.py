"""The group- and channel-administration surface, end to end through a daemon.

Every test goes over a real Unix socket, through the real middleware chain and
the real dispatcher, into the real implementation, against a fake Telegram
that holds *state*. The assertion is almost always that the world changed —
the member moved into the banned set, the mask that came back is complete,
the topic is really closed, the cursor really walks — because "the request
object looked plausible" is exactly the class of test that let v1 ship
`chat mute 3600` resolving to 1970 (COR-01).

Three properties get more attention than the rest, because they are the ones
a wrong implementation gets wrong quietly:

* **mask completeness** — `channels.editBanned` replaces the whole mask, so a
  restrict that forgets to re-send an unrelated flag silently hands a right
  back;
* **polarity** — `ChatBannedRights` is inverted, and a single missing `not`
  turns "may not send media" into "may send media";
* **the layer-229 refusals** — seven commands must exit 13 with an
  explanation rather than 1 with a traceback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
    classify,
)

ALICE = 4242
BOB = 4343
CAROL = 4444
GROUP = 5150
GROUP_ID = -1000000000000 - GROUP
CHANNEL = 6100
CHANNEL_ID = -1000000000000 - CHANNEL
BASIC = 320
BASIC_ID = -BASIC


def make_basic_chat(chat_id: int, title: str = "Basic") -> Any:
    from telethon.tl import types

    return types.Chat(
        id=chat_id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=datetime.now(timezone.utc),
        version=1,
    )


@pytest.fixture
def admin_world(world):
    """A supergroup, a broadcast channel, a basic group, and three people."""
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice", first="Alice"))
    world.add_user(make_user(BOB, username="bobby", first="Bob"))
    world.add_user(make_user(CAROL, username="carol", first="Carol"))
    world.add_channel(make_channel(GROUP, title="News chat", megagroup=True))
    world.add_channel(make_channel(CHANNEL, title="News", megagroup=False))
    world.chats[BASIC] = make_basic_chat(BASIC, "Old crew")

    world.add_member(GROUP_ID, world.me.id, status="creator")
    world.add_member(GROUP_ID, ALICE, status="admin", rank="moderator")
    world.add_member(GROUP_ID, BOB)
    world.add_member(BASIC_ID, ALICE, status="admin")
    world.add_member(BASIC_ID, BOB)
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


async def fails(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> int:
    with pytest.raises(Exception) as caught:
        await call(client, in_thread, op, request, **kwargs)
    return classify(caught.value).exit_code


# ---------------------------------------------------------------------------
# chat member list / get
# ---------------------------------------------------------------------------


class TestMemberList:
    async def test_a_member_keeps_its_participant_wrapper(
        self, live_daemon, client, in_thread, admin_world
    ):
        """v1 returned a bare user; status, rank and promoter went in the bin."""
        rows = await result(client, in_thread, "chat.member.list", {"chat": str(GROUP_ID)})
        by_id = {row["id"]: row for row in rows}
        assert by_id[ALICE]["status"] == "admin"
        assert by_id[ALICE]["rank"] == "moderator"
        assert by_id[ALICE]["promoted_by"] == admin_world.me.id
        assert by_id[BOB].get("status", "member") == "member"

    async def test_the_admins_filter_asks_the_server_for_admins(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(
            client, in_thread, "chat.member.list", {"chat": str(GROUP_ID), "filter": "admins"}
        )
        assert {row["id"] for row in rows} == {admin_world.me.id, ALICE}
        sent = admin_world.called("GetParticipantsRequest")[-1]
        assert type(sent.filter).__name__ == "ChannelParticipantsAdmins"

    async def test_the_search_goes_to_the_server_not_to_the_page(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(
            client, in_thread, "chat.member.list", {"chat": str(GROUP_ID), "search": "ali"}
        )
        assert [row["id"] for row in rows] == [ALICE]
        sent = admin_world.called("GetParticipantsRequest")[-1]
        assert type(sent.filter).__name__ == "ChannelParticipantsSearch"

    async def test_the_cursor_walks_forward_and_is_op_bound(
        self, live_daemon, client, in_thread, admin_world
    ):
        first = await call(client, in_thread, "chat.member.list", {"chat": str(GROUP_ID)}, limit=1)
        assert first["page"]["has_more"] is True
        cursor = first["page"]["next_cursor"]
        second = await call(
            client, in_thread, "chat.member.list", {"chat": str(GROUP_ID)}, limit=1, cursor=cursor
        )
        assert first["result"][0]["id"] != second["result"][0]["id"]
        assert (
            await fails(
                client, in_thread, "chat.admin.list", {"chat": str(GROUP_ID)}, cursor=cursor
            )
            == EXIT_USAGE
        )

    async def test_a_basic_group_is_sliced_client_side(
        self, live_daemon, client, in_thread, admin_world
    ):
        """chatFull hands over every participant at once; the offset is ours."""
        rows = await result(client, in_thread, "chat.member.list", {"chat": str(BASIC_ID)})
        assert {row["id"] for row in rows} == {ALICE, BOB}

    async def test_via_link_asks_the_importers_endpoint(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_importer(GROUP_ID, CAROL)
        rows = await result(
            client,
            in_thread,
            "chat.member.list",
            {"chat": str(GROUP_ID), "via_link": "https://t.me/+abc"},
        )
        assert [row["id"] for row in rows] == [CAROL]
        assert rows[0]["via_link"] == "https://t.me/+abc"

    async def test_the_v1_path_still_works(self, live_daemon, client, in_thread, admin_world):
        """AGENT.md documents `chat members`; §12.4 says it never disappears."""
        rows = await result(client, in_thread, "chat.members", {"chat": str(GROUP_ID)})
        assert {row["id"] for row in rows} == {admin_world.me.id, ALICE, BOB}


class TestMemberGet:
    async def test_it_reports_effective_permissions_not_just_the_mask(
        self, live_daemon, client, in_thread, admin_world
    ):
        from tlgr.ops import _rights

        entity = admin_world.chats[GROUP]
        entity.default_banned_rights = _rights.build_banned_rights(
            ["view-messages", "send-messages"]
        )
        row = await result(
            client, in_thread, "chat.member.get", {"chat": str(GROUP_ID), "user": str(BOB)}
        )
        effective = row["effective_permissions"]
        assert effective["send_messages"] is True
        assert effective["send_media"] is False

    async def test_a_stranger_is_not_found(self, live_daemon, client, in_thread, admin_world):
        code = await fails(
            client, in_thread, "chat.member.get", {"chat": str(GROUP_ID), "user": str(CAROL)}
        )
        assert code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# chat member add / remove / ban / unban / restrict
# ---------------------------------------------------------------------------


class TestMemberWrite:
    async def test_adding_a_member_moves_the_world(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.member.add", {"chat": str(GROUP_ID), "user": [str(CAROL)]}
        )
        assert out["added"] == [CAROL]
        assert CAROL in admin_world.members[GROUP_ID]

    async def test_a_refused_invitee_is_named_not_counted(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        def refuse(request):
            return types.messages.InvitedUsers(
                updates=types.Updates(updates=[], users=[], chats=[], date=None, seq=0),
                missing_invitees=[
                    types.MissingInvitee(user_id=CAROL, premium_required_for_pm=True)
                ],
            )

        admin_world.raw["InviteToChannelRequest"] = refuse
        out = await result(
            client, in_thread, "chat.member.add", {"chat": str(GROUP_ID), "user": [str(CAROL)]}
        )
        assert out["missing"] == [{"user_id": CAROL, "reason": "premium-required-for-pm"}]
        assert out.get("added", []) == []

    async def test_a_kick_bans_then_unbans_so_they_may_return(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(
            client, in_thread, "chat.member.remove", {"chat": str(GROUP_ID), "user": [str(BOB)]}
        )
        sent = admin_world.called("EditBannedRequest")
        assert len(sent) == 2
        assert sent[0].banned_rights.view_messages is True
        assert sent[1].banned_rights.view_messages is False
        assert admin_world.banned.get(GROUP_ID, {}) == {}

    async def test_a_ban_leaves_the_mask_in_place(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.member.ban", {"chat": str(GROUP_ID), "user": [str(BOB)]}
        )
        assert out[0]["banned"] is True
        assert BOB in admin_world.banned[GROUP_ID]
        rows = await result(
            client, in_thread, "chat.member.list", {"chat": str(GROUP_ID), "filter": "kicked"}
        )
        assert [row["id"] for row in rows] == [BOB]

    async def test_a_timed_ban_writes_an_absolute_timestamp(
        self, live_daemon, client, in_thread, admin_world
    ):
        """COR-01 again: `--until 7d` must not resolve to 1970."""
        import time

        await result(
            client,
            in_thread,
            "chat.member.ban",
            {"chat": str(GROUP_ID), "user": [str(BOB)], "until": "7d"},
        )
        until = admin_world.called("EditBannedRequest")[-1].banned_rights.until_date
        stamp = until if isinstance(until, int) else int(until.timestamp())
        assert abs(stamp - (time.time() + 7 * 86400)) < 5

    async def test_a_short_ban_is_rounded_to_forever_like_the_server_does(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.member.ban",
            {"chat": str(GROUP_ID), "user": [str(BOB)], "until": "10s"},
        )
        assert "until" not in out[0]

    async def test_ban_with_purge_drains_the_history_loop(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(
            client,
            in_thread,
            "chat.member.ban",
            {"chat": str(GROUP_ID), "user": [str(BOB)], "purge": True, "report": True},
        )
        assert admin_world.called("DeleteParticipantHistoryRequest")
        assert admin_world.called("ReportSpamRequest")

    async def test_unban_sends_an_all_clear_mask(self, live_daemon, client, in_thread, admin_world):
        await result(
            client, in_thread, "chat.member.ban", {"chat": str(GROUP_ID), "user": [str(BOB)]}
        )
        await result(
            client, in_thread, "chat.member.unban", {"chat": str(GROUP_ID), "user": [str(BOB)]}
        )
        sent = admin_world.called("EditBannedRequest")[-1]
        assert sent.banned_rights.view_messages is False
        assert sent.banned_rights.send_messages is False
        assert admin_world.banned.get(GROUP_ID, {}) == {}


class TestMemberRestrict:
    async def test_the_mask_that_goes_back_is_complete(
        self, live_daemon, client, in_thread, admin_world
    ):
        """`editBanned` replaces the mask; an omitted flag is a cleared flag."""
        out = await result(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "deny": "send-media"},
        )
        sent = admin_world.called("EditBannedRequest")[-1]
        rights = sent.banned_rights
        assert rights.send_media is True, "the denied right must be prohibited"
        assert rights.send_messages is False, "an untouched right must stay allowed"
        assert rights.view_messages is False
        assert "send-media" in out["deny"]
        assert "send-messages" in out["allow"]

    async def test_a_second_restrict_patches_rather_than_resets(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "deny": "send-media"},
        )
        await result(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "deny": "send-polls"},
        )
        rights = admin_world.called("EditBannedRequest")[-1].banned_rights
        assert rights.send_media is True and rights.send_polls is True

    async def test_none_is_read_only_and_all_gives_it_back(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "none": True},
        )
        assert out["allow"] == ["view-messages"]
        out = await result(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "everything": True},
        )
        assert "send-messages" in out["allow"]

    async def test_an_unknown_right_is_refused_not_ignored(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "deny": "send-telepathy"},
        )
        assert code == EXIT_USAGE

    async def test_a_layer_229_right_exits_thirteen(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(GROUP_ID), "user": str(BOB), "deny": "manage-linked-peers"},
        )
        assert code == EXIT_INDETERMINATE

    async def test_a_basic_group_has_no_per_member_mask(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.member.restrict",
            {"chat": str(BASIC_ID), "user": str(BOB), "deny": "send-media"},
        )
        assert code == EXIT_USAGE


class TestMemberOdds:
    async def test_edit_sets_a_rank(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client,
            in_thread,
            "chat.member.edit",
            {"chat": str(GROUP_ID), "user": str(BOB), "rank": "helper"},
        )
        assert out["rank"] == "helper"
        assert admin_world.members[GROUP_ID][BOB].rank == "helper"

    async def test_edit_with_nothing_to_change_is_a_usage_error(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client, in_thread, "chat.member.edit", {"chat": str(GROUP_ID), "user": str(BOB)}
        )
        assert code == EXIT_USAGE

    async def test_delete_history_drains_and_reports_a_count(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.member.delete-history",
            {"chat": str(GROUP_ID), "user": str(BOB)},
        )
        assert out["deleted"] >= 0
        assert admin_world.called("DeleteParticipantHistoryRequest")

    async def test_delete_history_needs_a_supergroup(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.member.delete-history",
            {"chat": str(BASIC_ID), "user": str(BOB)},
        )
        assert code == EXIT_USAGE

    async def test_report_sends_the_message_ids(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client,
            in_thread,
            "chat.member.report",
            {"chat": str(GROUP_ID), "user": str(BOB), "messages": [918]},
        )
        assert out["reported"] is True
        assert admin_world.called("ReportSpamRequest")[-1].id == [918]

    async def test_a_purge_is_gated_by_dry_run(self, live_daemon, client, in_thread, admin_world):
        """COR-17: the short-circuit is above the implementation, always."""
        envelope = await call(
            client,
            in_thread,
            "chat.member.delete-history",
            {"chat": str(GROUP_ID), "user": str(BOB)},
            dry_run=True,
        )
        assert envelope["result"]["dry_run"] is True
        assert not admin_world.called("DeleteParticipantHistoryRequest")


# ---------------------------------------------------------------------------
# chat admin
# ---------------------------------------------------------------------------


class TestAdmins:
    async def test_the_creator_is_reported_as_creator(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.admin.list", {"chat": str(GROUP_ID)})
        by_id = {row["id"]: row for row in rows}
        assert by_id[admin_world.me.id]["status"] == "creator"
        assert by_id[ALICE]["admin_rights"]["ban_users"] is True

    async def test_no_rights_drops_the_mask(self, live_daemon, client, in_thread, admin_world):
        rows = await result(
            client, in_thread, "chat.admin.list", {"chat": str(GROUP_ID), "rights": False}
        )
        assert all("admin_rights" not in row for row in rows)

    async def test_the_antispam_bot_is_appended_the_way_the_gui_does(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        admin_world.settings_of(GROUP_ID)["antispam"] = True
        admin_world.raw["GetAppConfigRequest"] = types.help.AppConfig(
            hash=0,
            config=types.JsonObject(
                value=[
                    types.JsonObjectValue(
                        key="telegram_antispam_user_id", value=types.JsonNumber(value=5434988373)
                    )
                ]
            ),
        )
        rows = await result(client, in_thread, "chat.admin.list", {"chat": str(GROUP_ID)})
        assert any(row.get("name") == "Telegram Anti-Spam" for row in rows)

    async def test_promote_sets_the_mask_absolutely(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.admin.promote",
            {"chat": str(GROUP_ID), "user": str(BOB), "rights": "ban-users,delete-messages"},
        )
        rights = out["admin_rights"]
        assert rights["ban_users"] is True and rights["delete_messages"] is True
        assert rights["add_admins"] is False

    async def test_grant_patches_the_mask_it_read_first(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.admin.promote",
            {"chat": str(GROUP_ID), "user": str(ALICE), "grant": "pin-messages"},
        )
        rights = out["admin_rights"]
        assert rights["ban_users"] is True, "the right Alice already had must survive"
        assert rights["pin_messages"] is True

    async def test_revoke_takes_one_right_away(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client,
            in_thread,
            "chat.admin.promote",
            {"chat": str(GROUP_ID), "user": str(ALICE), "revoke": "ban-users"},
        )
        assert out["admin_rights"]["ban_users"] is False

    async def test_a_rank_reaches_the_request(self, live_daemon, client, in_thread, admin_world):
        await result(
            client,
            in_thread,
            "chat.admin.promote",
            {"chat": str(GROUP_ID), "user": str(BOB), "grant": "pin-messages", "rank": "mod"},
        )
        assert admin_world.called("EditAdminRequest")[-1].rank == "mod"

    async def test_a_basic_group_reports_the_rights_it_dropped(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.admin.promote",
            {"chat": str(BASIC_ID), "user": str(BOB), "rights": "ban-users"},
        )
        assert out["dropped"] == ["ban-users"]
        assert admin_world.called("EditChatAdminRequest")[-1].is_admin is True

    async def test_demote_sends_an_empty_mask(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client, in_thread, "chat.admin.demote", {"chat": str(GROUP_ID), "user": str(ALICE)}
        )
        assert all(value is False for value in out["admin_rights"].values())
        rows = await result(client, in_thread, "chat.admin.list", {"chat": str(GROUP_ID)})
        assert ALICE not in {row["id"] for row in rows}


# ---------------------------------------------------------------------------
# chat permission
# ---------------------------------------------------------------------------


class TestPermissions:
    async def test_get_prints_the_polarity_set_accepts(
        self, live_daemon, client, in_thread, admin_world
    ):
        from tlgr.ops import _rights

        admin_world.chats[GROUP].default_banned_rights = _rights.build_banned_rights(
            ["view-messages", "send-messages"]
        )
        out = await result(client, in_thread, "chat.permission.get", {"chat": str(GROUP_ID)})
        assert "send-messages" in out["allow"]
        assert "send-media" in out["deny"]

    async def test_set_round_trips_through_get(self, live_daemon, client, in_thread, admin_world):
        await result(
            client,
            in_thread,
            "chat.permission.set",
            {"chat": str(GROUP_ID), "deny": "send-media,send-stickers"},
        )
        out = await result(client, in_thread, "chat.permission.get", {"chat": str(GROUP_ID)})
        assert "send-media" in out["deny"] and "send-stickers" in out["deny"]
        assert "send-messages" in out["allow"]

    async def test_view_messages_is_never_taken_away_here(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.permission.set", {"chat": str(GROUP_ID), "none": True}
        )
        assert out["allow"] == ["view-messages"]

    async def test_an_unchanged_mask_reports_already(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(
            client, in_thread, "chat.permission.set", {"chat": str(GROUP_ID), "deny": "send-media"}
        )
        envelope = await call(
            client, in_thread, "chat.permission.set", {"chat": str(GROUP_ID), "deny": "send-media"}
        )
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True

    async def test_set_with_nothing_named_is_a_usage_error(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert (
            await fails(client, in_thread, "chat.permission.set", {"chat": str(GROUP_ID)})
            == EXIT_USAGE
        )

    async def test_the_vocabulary_marks_the_layer_gap(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.permission.list", {"mask": "admin"})
        by_name = {row["name"]: row for row in rows}
        assert by_name["ban-users"]["tl_flag"] == "ban_users"
        assert by_name["manage-linked-peers"]["supported"] is False
        assert by_name["manage-linked-peers"]["since_layer"] == 229

    async def test_the_member_vocabulary_is_the_one_restrict_accepts(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.permission.list", {"mask": "member"})
        names = {row["name"] for row in rows}
        assert "send-rounds" in names and "view-messages" in names
        assert all(row["mask"] == "member" for row in rows)


# ---------------------------------------------------------------------------
# chat admin-log
# ---------------------------------------------------------------------------


class TestAdminLog:
    def _events(self, world):
        from telethon.tl import types

        world.add_admin_log(
            GROUP_ID,
            91,
            types.ChannelAdminLogEventActionChangeTitle(prev_value="Old", new_value="New"),
        )
        world.add_admin_log(
            GROUP_ID,
            92,
            types.ChannelAdminLogEventActionToggleSlowMode(prev_value=0, new_value=30),
        )

    async def test_an_action_is_a_slug_and_keeps_its_tl_name(
        self, live_daemon, client, in_thread, admin_world
    ):
        self._events(admin_world)
        rows = await result(client, in_thread, "chat.admin-log.list", {"chat": str(GROUP_ID)})
        newest = rows[0]
        assert newest["action"] == "toggle-slow-mode"
        assert newest["raw_type"] == "ChannelAdminLogEventActionToggleSlowMode"
        assert newest["prev"] == 0 and newest["new"] == 30

    async def test_the_filter_names_are_the_apis_not_telethons(
        self, live_daemon, client, in_thread, admin_world
    ):
        self._events(admin_world)
        await result(
            client, in_thread, "chat.admin-log.list", {"chat": str(GROUP_ID), "filter": "ban,kick"}
        )
        sent = admin_world.called("GetAdminLogRequest")[-1]
        assert sent.events_filter.ban is True and sent.events_filter.kick is True
        assert sent.events_filter.promote is None

    async def test_an_unknown_filter_class_is_a_usage_error(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.admin-log.list",
            {"chat": str(GROUP_ID), "filter": "everything-please"},
        )
        assert code == EXIT_USAGE

    async def test_the_cursor_is_the_max_id_and_walks_backwards(
        self, live_daemon, client, in_thread, admin_world
    ):
        self._events(admin_world)
        first = await call(
            client, in_thread, "chat.admin-log.list", {"chat": str(GROUP_ID)}, limit=1
        )
        assert first["result"][0]["id"] == 92
        second = await call(
            client,
            in_thread,
            "chat.admin-log.list",
            {"chat": str(GROUP_ID)},
            limit=1,
            cursor=first["page"]["next_cursor"],
        )
        assert second["result"][0]["id"] == 91

    async def test_the_antispam_false_positive_report(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.admin-log.report", {"chat": str(GROUP_ID), "msg_id": 918}
        )
        assert out == {"chat_id": GROUP_ID, "msg_id": 918}, "reported=True is the default"


# ---------------------------------------------------------------------------
# chat transfer
# ---------------------------------------------------------------------------


class TestTransfer:
    async def test_without_a_password_it_refuses_before_the_network(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client, in_thread, "chat.transfer", {"chat": str(GROUP_ID), "user": str(ALICE)}
        )
        assert code == EXIT_USAGE
        assert not admin_world.called("EditChatCreatorRequest")

    async def test_dry_run_never_reaches_the_password_check(
        self, live_daemon, client, in_thread, admin_world
    ):
        envelope = await call(
            client,
            in_thread,
            "chat.transfer",
            {"chat": str(GROUP_ID), "user": str(ALICE)},
            dry_run=True,
        )
        assert envelope["result"]["would"] == "chat.transfer"


# ---------------------------------------------------------------------------
# chat invite
# ---------------------------------------------------------------------------


class TestInvites:
    async def test_creating_a_link_carries_its_limits(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.invite.create",
            {"chat": str(GROUP_ID), "title": "Launch", "usage_limit": 25, "expires": "7d"},
        )
        assert out["title"] == "Launch" and out["usage_limit"] == 25
        sent = admin_world.called("ExportChatInviteRequest")[-1]
        assert sent.expire_date is not None

    async def test_a_limit_and_an_approval_queue_are_mutually_exclusive(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.invite.create",
            {"chat": str(GROUP_ID), "usage_limit": 5, "request_approval": True},
        )
        assert code == EXIT_USAGE

    async def test_a_paid_link_is_billed_per_thirty_days(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(
            client,
            in_thread,
            "chat.invite.create",
            {"chat": str(GROUP_ID), "subscription_stars": 50},
        )
        pricing = admin_world.called("ExportChatInviteRequest")[-1].subscription_pricing
        assert pricing.period == 30 * 86400 and pricing.amount == 50

    async def test_listing_separates_active_from_revoked(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_invite(GROUP_ID, "https://t.me/+live")
        admin_world.add_invite(GROUP_ID, "https://t.me/+dead", revoked=True)
        active = await result(client, in_thread, "chat.invite.list", {"chat": str(GROUP_ID)})
        revoked = await result(
            client, in_thread, "chat.invite.list", {"chat": str(GROUP_ID), "revoked": True}
        )
        assert [row["link"] for row in active] == ["https://t.me/+live"]
        assert [row["link"] for row in revoked] == ["https://t.me/+dead"]

    async def test_by_admin_counts_instead_of_linking(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_invite(GROUP_ID, "https://t.me/+one")
        admin_world.add_invite(GROUP_ID, "https://t.me/+two", revoked=True)
        rows = await result(
            client, in_thread, "chat.invite.list", {"chat": str(GROUP_ID), "by_admin": True}
        )
        assert rows[0]["invites_count"] == 1 and rows[0]["revoked_invites_count"] == 1

    async def test_editing_a_link_changes_it(self, live_daemon, client, in_thread, admin_world):
        admin_world.add_invite(GROUP_ID, "https://t.me/+one", title="Old")
        out = await result(
            client,
            in_thread,
            "chat.invite.edit",
            {"chat": str(GROUP_ID), "link": "https://t.me/+one", "title": "New"},
        )
        assert out["title"] == "New"

    async def test_a_replaced_link_is_reported_beside_the_new_one(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        old = types.ChatInviteExported(
            link="https://t.me/+old", admin_id=admin_world.me.id, date=None
        )
        new = types.ChatInviteExported(
            link="https://t.me/+new", admin_id=admin_world.me.id, date=None
        )
        admin_world.raw["EditExportedChatInviteRequest"] = (
            types.messages.ExportedChatInviteReplaced(invite=old, new_invite=new, users=[])
        )
        out = await result(
            client,
            in_thread,
            "chat.invite.revoke",
            {"chat": str(GROUP_ID), "link": "https://t.me/+old"},
        )
        assert out["link"] == "https://t.me/+new"
        assert out["replaced_link"] == "https://t.me/+old"

    async def test_deleting_needs_a_link_or_the_revoked_flag(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert (
            await fails(client, in_thread, "chat.invite.delete", {"chat": str(GROUP_ID)})
            == EXIT_USAGE
        )

    async def test_deleting_every_revoked_link_purges_them(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_invite(GROUP_ID, "https://t.me/+live")
        admin_world.add_invite(GROUP_ID, "https://t.me/+dead", revoked=True)
        await result(
            client, in_thread, "chat.invite.delete", {"chat": str(GROUP_ID), "revoked": True}
        )
        assert [i.link for i in admin_world.invites[GROUP_ID]] == ["https://t.me/+live"]

    async def test_getting_a_link_you_hold_previews_it_without_rights(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.invite.get", {"target": "https://t.me/+AbCdEf"})
        assert out["chat_title"] == "Shared group"
        assert out["already_member"] is False
        assert admin_world.called("CheckChatInviteRequest")[-1].hash == "AbCdEf"

    async def test_a_link_you_already_joined_says_so(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        admin_world.invite_previews["AbCdEf"] = types.ChatInviteAlready(
            chat=admin_world.chats[GROUP]
        )
        out = await result(client, in_thread, "chat.invite.get", {"target": "https://t.me/+AbCdEf"})
        assert out["already_member"] is True

    async def test_the_qr_warns_instead_of_pretending(
        self, live_daemon, client, in_thread, admin_world
    ):
        envelope = await call(
            client,
            in_thread,
            "chat.invite.get",
            {"target": "https://t.me/+AbCdEf", "qr": True},
        )
        warnings = envelope["meta"]["warnings"]
        assert any("QR" in warning for warning in warnings)

    async def test_a_peek_reads_without_joining(self, live_daemon, client, in_thread, admin_world):
        from telethon.tl import types

        admin_world.add_message(GROUP_ID, "secret", message_id=77)
        admin_world.invite_previews["Peek"] = types.ChatInvitePeek(
            chat=admin_world.chats[GROUP], expires=datetime.now(timezone.utc)
        )
        out = await result(client, in_thread, "chat.invite.open", {"link": "https://t.me/+Peek"})
        assert out["peek_expires"]
        assert [m["text"] for m in out["messages"]] == ["secret"]

    async def test_a_link_with_no_peek_on_offer_exits_six(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(client, in_thread, "chat.invite.open", {"link": "https://t.me/+NoPeek"})
        assert code == EXIT_PERMISSION


class TestJoin:
    async def test_joining_a_public_chat_by_username(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.join", {"target": str(GROUP_ID)})
        assert out["joined"] is True and out["chat_id"] == GROUP_ID
        assert admin_world.called("JoinChannelRequest")

    async def test_already_a_participant_is_success_not_failure(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.errors import UserAlreadyParticipantError

        admin_world.fail_next("ImportChatInviteRequest", UserAlreadyParticipantError(request=None))
        envelope = await call(client, in_thread, "chat.join", {"target": "https://t.me/+abc"})
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True

    async def test_a_queued_join_reports_pending_approval(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.errors import InviteRequestSentError

        admin_world.fail_next("ImportChatInviteRequest", InviteRequestSentError(request=None))
        out = await result(client, in_thread, "chat.join", {"target": "https://t.me/+abc"})
        assert out["pending_approval"] is True and out.get("joined", False) is False


# ---------------------------------------------------------------------------
# chat request
# ---------------------------------------------------------------------------


class TestJoinRequests:
    async def test_listing_shows_only_pending_requests(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_importer(GROUP_ID, CAROL, requested=True, about="let me in")
        admin_world.add_importer(GROUP_ID, BOB)
        rows = await result(client, in_thread, "chat.request.list", {"chat": str(GROUP_ID)})
        assert [row["user_id"] for row in rows] == [CAROL]
        assert rows[0]["about"] == "let me in"

    async def test_approving_moves_them_into_the_chat(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_importer(GROUP_ID, CAROL, requested=True)
        out = await result(
            client,
            in_thread,
            "chat.request.approve",
            {"chat": str(GROUP_ID), "user": [str(CAROL)]},
        )
        assert out["approved"] == [CAROL]
        assert CAROL in admin_world.members[GROUP_ID]

    async def test_denying_leaves_them_out(self, live_daemon, client, in_thread, admin_world):
        admin_world.add_importer(GROUP_ID, CAROL, requested=True)
        out = await result(
            client, in_thread, "chat.request.deny", {"chat": str(GROUP_ID), "user": [str(CAROL)]}
        )
        assert out["denied"] == [CAROL]
        assert CAROL not in admin_world.members.get(GROUP_ID, {})

    async def test_approve_all_answers_the_whole_queue(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_importer(GROUP_ID, CAROL, requested=True)
        out = await result(
            client, in_thread, "chat.request.approve", {"chat": str(GROUP_ID), "everyone": True}
        )
        assert out["all"] is True
        assert admin_world.called("HideAllChatJoinRequestsRequest")

    async def test_answering_needs_a_target(self, live_daemon, client, in_thread, admin_world):
        assert (
            await fails(client, in_thread, "chat.request.approve", {"chat": str(GROUP_ID)})
            == EXIT_USAGE
        )

    async def test_the_list_answer_flags_honour_dry_run_themselves(
        self, live_daemon, client, in_thread, admin_world
    ):
        """The op is a read, so `--dry-run` must keep listing (DECISIONS)."""
        admin_world.add_importer(GROUP_ID, CAROL, requested=True)
        envelope = await call(
            client,
            in_thread,
            "chat.request.list",
            {"chat": str(GROUP_ID), "approve": [str(CAROL)]},
            dry_run=True,
        )
        assert [row["user_id"] for row in envelope["result"]] == [CAROL]
        assert any("dry-run" in w for w in envelope["meta"]["warnings"])
        assert not admin_world.called("HideChatJoinRequestRequest")

    async def test_the_list_answer_flags_do_fire_without_dry_run(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.add_importer(GROUP_ID, CAROL, requested=True)
        rows = await result(
            client,
            in_thread,
            "chat.request.list",
            {"chat": str(GROUP_ID), "approve": [str(CAROL)]},
        )
        assert rows == []
        assert CAROL in admin_world.members[GROUP_ID]


# ---------------------------------------------------------------------------
# chat topic
# ---------------------------------------------------------------------------


@pytest.fixture
def forum(admin_world):
    admin_world.chats[GROUP].forum = True
    admin_world.add_topic(GROUP_ID, 1, "General")
    admin_world.add_topic(GROUP_ID, 314, "Releases", unread_count=2)
    return admin_world


class TestTopics:
    async def test_listing_reports_the_counters(self, live_daemon, client, in_thread, forum):
        rows = await result(client, in_thread, "chat.topic.list", {"chat": str(GROUP_ID)})
        by_id = {row["id"]: row for row in rows}
        assert by_id[314]["title"] == "Releases"
        assert by_id[314]["unread_count"] == 2
        assert 1 in by_id, "General is always present"

    async def test_the_search_reaches_the_request(self, live_daemon, client, in_thread, forum):
        rows = await result(
            client, in_thread, "chat.topic.list", {"chat": str(GROUP_ID), "search": "rele"}
        )
        assert [row["id"] for row in rows] == [314]

    async def test_a_client_side_filter_does_not_move_the_cursor(
        self, live_daemon, client, in_thread, forum
    ):
        """The cursor is built from the server's last row, not the survivor."""
        forum.topics[GROUP_ID][314].closed = True
        envelope = await call(
            client, in_thread, "chat.topic.list", {"chat": str(GROUP_ID), "closed": True}
        )
        assert [row["id"] for row in envelope["result"]] == [314]

    async def test_a_chat_that_is_not_a_forum_says_so(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert (
            await fails(client, in_thread, "chat.topic.list", {"chat": str(BASIC_ID)}) == EXIT_USAGE
        )

    async def test_get_reports_a_deleted_topic_rather_than_dropping_it(
        self, live_daemon, client, in_thread, forum
    ):
        rows = await result(
            client, in_thread, "chat.topic.get", {"chat": str(GROUP_ID), "topic": [314, 999]}
        )
        by_id = {row["id"]: row for row in rows}
        assert by_id[999]["deleted"] is True
        assert "deleted" not in by_id[314]

    async def test_creating_a_topic_returns_the_id_that_topic_flags_take(
        self, live_daemon, client, in_thread, forum
    ):
        out = await result(
            client, in_thread, "chat.topic.create", {"chat": str(GROUP_ID), "title": "Design"}
        )
        assert out["topic_id"] in forum.topics[GROUP_ID]
        assert forum.topics[GROUP_ID][out["topic_id"]].title == "Design"

    async def test_close_and_reopen_move_the_topic(self, live_daemon, client, in_thread, forum):
        await result(client, in_thread, "chat.topic.close", {"chat": str(GROUP_ID), "topic": 314})
        assert forum.topics[GROUP_ID][314].closed is True
        await result(client, in_thread, "chat.topic.reopen", {"chat": str(GROUP_ID), "topic": 314})
        assert forum.topics[GROUP_ID][314].closed is False

    async def test_edit_renames_and_reports_what_changed(
        self, live_daemon, client, in_thread, forum
    ):
        out = await result(
            client,
            in_thread,
            "chat.topic.edit",
            {"chat": str(GROUP_ID), "topic": 314, "title": "Release notes"},
        )
        assert out["changed"] == ["title"]
        assert forum.topics[GROUP_ID][314].title == "Release notes"

    async def test_edit_with_nothing_to_change_is_a_usage_error(
        self, live_daemon, client, in_thread, forum
    ):
        assert (
            await fails(client, in_thread, "chat.topic.edit", {"chat": str(GROUP_ID), "topic": 314})
            == EXIT_USAGE
        )

    async def test_hide_and_unhide_always_target_general(
        self, live_daemon, client, in_thread, forum
    ):
        out = await result(client, in_thread, "chat.topic.hide", {"chat": str(GROUP_ID)})
        assert out["topic_id"] == 1
        assert forum.topics[GROUP_ID][1].hidden is True
        await result(client, in_thread, "chat.topic.unhide", {"chat": str(GROUP_ID)})
        assert forum.topics[GROUP_ID][1].hidden is False

    async def test_general_cannot_be_deleted(self, live_daemon, client, in_thread, forum):
        assert (
            await fails(client, in_thread, "chat.topic.delete", {"chat": str(GROUP_ID), "topic": 1})
            == EXIT_USAGE
        )

    async def test_delete_drains_and_removes(self, live_daemon, client, in_thread, forum):
        out = await result(
            client, in_thread, "chat.topic.delete", {"chat": str(GROUP_ID), "topic": 314}
        )
        assert out["deleted"] is True
        assert 314 not in forum.topics[GROUP_ID]

    async def test_pin_and_unpin(self, live_daemon, client, in_thread, forum):
        await result(client, in_thread, "chat.topic.pin", {"chat": str(GROUP_ID), "topic": [314]})
        assert forum.topics[GROUP_ID][314].pinned is True
        await result(client, in_thread, "chat.topic.unpin", {"chat": str(GROUP_ID), "topic": [314]})
        assert not forum.topics[GROUP_ID][314].pinned

    async def test_reorder_sends_the_whole_order(self, live_daemon, client, in_thread, forum):
        await result(
            client,
            in_thread,
            "chat.topic.pin",
            {"chat": str(GROUP_ID), "topic": [314, 1], "reorder": True},
        )
        sent = forum.called("ReorderPinnedForumTopicsRequest")[-1]
        assert sent.order == [314, 1]

    async def test_unpin_all_clears_the_order(self, live_daemon, client, in_thread, forum):
        await result(
            client, in_thread, "chat.topic.unpin", {"chat": str(GROUP_ID), "everything": True}
        )
        sent = forum.called("ReorderPinnedForumTopicsRequest")[-1]
        assert sent.order == [] and sent.force is True

    async def test_muting_writes_an_absolute_timestamp(self, live_daemon, client, in_thread, forum):
        """COR-01 for topics: `8h` is now + 8h, not 1970."""
        import time

        await result(
            client,
            in_thread,
            "chat.topic.mute",
            {"chat": str(GROUP_ID), "topic": 314, "duration": "8h"},
        )
        sent = forum.called("UpdateNotifySettingsRequest")[-1]
        assert type(sent.peer).__name__ == "InputNotifyForumTopic"
        assert sent.peer.top_msg_id == 314
        until = sent.settings.mute_until
        stamp = until if isinstance(until, int) else int(until.timestamp())
        assert abs(stamp - (time.time() + 8 * 3600)) < 5

    async def test_unmute_clears_it(self, live_daemon, client, in_thread, forum):
        await result(client, in_thread, "chat.topic.unmute", {"chat": str(GROUP_ID), "topic": 314})
        sent = forum.called("UpdateNotifySettingsRequest")[-1]
        assert sent.settings.mute_until is None

    async def test_reading_a_topic_omits_top_msg_id_for_general(
        self, live_daemon, client, in_thread, forum
    ):
        await result(
            client,
            in_thread,
            "chat.topic.read",
            {"chat": str(GROUP_ID), "topic": 1, "mentions": True},
        )
        sent = forum.called("ReadMentionsRequest")[-1]
        assert sent.top_msg_id is None

    async def test_reading_a_normal_topic_sends_top_msg_id(
        self, live_daemon, client, in_thread, forum
    ):
        await result(
            client,
            in_thread,
            "chat.topic.read",
            {"chat": str(GROUP_ID), "topic": 314, "reactions": True},
        )
        assert forum.called("ReadReactionsRequest")[-1].top_msg_id == 314

    async def test_list_only_reads_nothing(self, live_daemon, client, in_thread, forum):
        await result(
            client,
            in_thread,
            "chat.topic.read",
            {"chat": str(GROUP_ID), "topic": 314, "list_only": True},
        )
        assert not forum.called("ReadDiscussionRequest")
        assert forum.called("GetUnreadMentionsRequest")


# ---------------------------------------------------------------------------
# chat create / edit / convert / setting / username / photo / send-as
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_a_supergroup_is_what_the_gui_creates(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.create", {"title": "Release team"})
        assert out["type"] == "supergroup"
        sent = admin_world.called("CreateChannelRequest")[-1]
        assert sent.megagroup is True and sent.broadcast is None

    async def test_a_forum_asks_for_a_forum(self, live_daemon, client, in_thread, admin_world):
        await result(client, in_thread, "chat.create", {"title": "Hub", "type": "forum"})
        assert admin_world.called("CreateChannelRequest")[-1].forum is True

    async def test_a_basic_group_takes_the_other_request(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(
            client,
            in_thread,
            "chat.create",
            {"title": "Old crew", "type": "group", "members": [str(ALICE)]},
        )
        assert admin_world.called("CreateChatRequest")[-1].title == "Old crew"

    async def test_a_basic_group_cannot_have_a_username(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.create",
            {"title": "Old crew", "type": "group", "username": "oldcrew"},
        )
        assert code == EXIT_USAGE

    async def test_the_v1_path_still_creates(self, live_daemon, client, in_thread, admin_world):
        """AGENT.md documents `chat create`; it must keep working."""
        out = await result(client, in_thread, "chat.create", {"title": "Legacy"})
        assert out["title"] == "Legacy"


class TestEditAndConvert:
    async def test_editing_a_title_and_an_about_reports_both(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.edit",
            {"chat": str(GROUP_ID), "title": "Renamed", "about": "Ship it"},
        )
        assert out["changed"] == ["title", "about"]
        assert admin_world.called("EditTitleRequest")[-1].title == "Renamed"
        assert admin_world.called("EditChatAboutRequest")[-1].about == "Ship it"

    async def test_a_basic_group_edits_through_the_other_request(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(client, in_thread, "chat.edit", {"chat": str(BASIC_ID), "title": "Renamed"})
        assert admin_world.called("EditChatTitleRequest")[-1].title == "Renamed"

    async def test_a_colour_is_refused_on_a_basic_group(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(client, in_thread, "chat.edit", {"chat": str(BASIC_ID), "color": "5"})
        assert code == EXIT_USAGE

    async def test_nothing_to_change_is_a_usage_error(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert await fails(client, in_thread, "chat.edit", {"chat": str(GROUP_ID)}) == EXIT_USAGE

    async def test_converting_a_basic_group_reports_both_ids(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.convert", {"chat": str(BASIC_ID), "target": "supergroup"}
        )
        assert out["old_chat_id"] == BASIC_ID
        assert out["type"] == "supergroup"

    async def test_converting_a_supergroup_to_a_supergroup_is_refused(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client, in_thread, "chat.convert", {"chat": str(GROUP_ID), "target": "supergroup"}
        )
        assert code == EXIT_USAGE

    async def test_an_unknown_target_is_a_usage_error(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client, in_thread, "chat.convert", {"chat": str(GROUP_ID), "target": "megachat"}
        )
        assert code == EXIT_USAGE


class TestSettings:
    async def test_the_keys_round_trip_from_get_into_set(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.settings_of(GROUP_ID)["slowmode_seconds"] = 30
        out = await result(client, in_thread, "chat.setting.get", {"chat": str(GROUP_ID)})
        assert out["slow_mode"] == 30
        assert "slow_mode" in out["available"]

    async def test_a_toggle_already_in_that_state_is_not_sent(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.settings_of(GROUP_ID)["slowmode_seconds"] = 30
        envelope = await call(
            client, in_thread, "chat.setting.set", {"chat": str(GROUP_ID), "slow_mode": "30s"}
        )
        assert envelope["result"]["already"] == ["slow_mode"]
        assert not admin_world.called("ToggleSlowModeRequest")

    async def test_slow_mode_is_rounded_onto_the_server_ladder(
        self, live_daemon, client, in_thread, admin_world
    ):
        envelope = await call(
            client, in_thread, "chat.setting.set", {"chat": str(GROUP_ID), "slow_mode": "45s"}
        )
        assert admin_world.called("ToggleSlowModeRequest")[-1].seconds == 60
        assert any("rounded" in w for w in envelope["meta"]["warnings"])

    async def test_a_failed_key_does_not_hide_the_ones_that_worked(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.errors import ChatAdminRequiredError

        admin_world.fail_next(
            "ToggleParticipantsHiddenRequest", ChatAdminRequiredError(request=None)
        )
        out = await result(
            client,
            in_thread,
            "chat.setting.set",
            {"chat": str(GROUP_ID), "slow_mode": "30s", "hidden_members": "on"},
        )
        assert out["changed"] == ["slow_mode"]
        assert "hidden_members" in out["failed"]

    async def test_on_off_is_validated(self, live_daemon, client, in_thread, admin_world):
        code = await fails(
            client, in_thread, "chat.setting.set", {"chat": str(GROUP_ID), "antispam": "maybe"}
        )
        assert code == EXIT_USAGE

    async def test_a_basic_group_refuses_the_supergroup_keys(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client, in_thread, "chat.setting.set", {"chat": str(BASIC_ID), "slow_mode": "30s"}
        )
        assert code == EXIT_USAGE

    async def test_nothing_to_change_is_a_usage_error(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert (
            await fails(client, in_thread, "chat.setting.set", {"chat": str(GROUP_ID)})
            == EXIT_USAGE
        )

    async def test_the_settings_alias_still_reads(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.settings", {"chat": str(GROUP_ID)})
        assert out["chat_id"] == GROUP_ID


class TestUsernames:
    async def test_an_available_name_says_so(self, live_daemon, client, in_thread, admin_world):
        out = await result(client, in_thread, "chat.username.get", {"username": "mynews"})
        assert out == {"username": "mynews", "status": "available", "available": True}

    async def test_an_occupied_name_says_so(self, live_daemon, client, in_thread, admin_world):
        admin_world.taken_usernames.add("mynews")
        out = await result(client, in_thread, "chat.username.get", {"username": "mynews"})
        assert out["status"] == "occupied" and out.get("available", False) is False

    async def test_setting_a_username_updates_the_entity(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.username.set", {"chat": str(GROUP_ID), "username": "mynews"}
        )
        assert out["link"] == "https://t.me/mynews"
        assert admin_world.chats[GROUP].username == "mynews"

    async def test_exactly_one_of_username_and_order(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert (
            await fails(client, in_thread, "chat.username.set", {"chat": str(GROUP_ID)})
            == EXIT_USAGE
        )

    async def test_a_basic_group_needs_upgrade_to_be_explicit(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(
            client,
            in_thread,
            "chat.username.set",
            {"chat": str(BASIC_ID), "username": "oldcrew"},
        )
        assert code == EXIT_USAGE

    async def test_toggling_one_username(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client,
            in_thread,
            "chat.username.toggle",
            {"chat": str(GROUP_ID), "username": "mynews", "state": "off"},
        )
        assert out.get("usernames", []) == []
        assert admin_world.called("ToggleUsernameRequest")[-1].active is False

    async def test_going_private_prints_the_invite_link(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.username.unset", {"chat": str(GROUP_ID)})
        assert out["invite_link"].startswith("https://t.me/+")


class TestPhotoAndSendAs:
    async def test_exactly_one_photo_source(self, live_daemon, client, in_thread, admin_world):
        assert (
            await fails(client, in_thread, "chat.photo.set", {"chat": str(GROUP_ID)}) == EXIT_USAGE
        )

    async def test_deleting_the_photo_sends_the_empty_one(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.photo.delete", {"chat": str(GROUP_ID)})
        assert out.get("ok", True) is True
        sent = admin_world.called("EditPhotoRequest")[-1]
        assert type(sent.photo).__name__ == "InputChatPhotoEmpty"

    async def test_send_as_lists_and_sets(self, live_daemon, client, in_thread, admin_world):
        rows = await result(client, in_thread, "chat.send-as.list", {"chat": str(GROUP_ID)})
        assert rows[0]["id"] == admin_world.me.id
        out = await result(
            client, in_thread, "chat.send-as.set", {"chat": str(GROUP_ID), "peer": str(CHANNEL_ID)}
        )
        assert out["send_as"] == CHANNEL_ID


class TestDiscussion:
    async def test_candidates_flag_a_basic_group_for_migration(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.discussion.list", {})
        needs = {row["id"]: row.get("needs_migration", False) for row in rows}
        assert needs[BASIC_ID] is True
        assert needs[GROUP_ID] is False

    async def test_linking_unhides_the_prehistory_when_asked(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.discussion.set",
            {
                "channel": str(CHANNEL_ID),
                "group": str(GROUP_ID),
                "unhide_prehistory": True,
            },
        )
        assert out["linked_chat_id"] == GROUP_ID
        assert admin_world.called("TogglePreHistoryHiddenRequest")[-1].enabled is False

    async def test_unlinking_sends_the_empty_channel(
        self, live_daemon, client, in_thread, admin_world
    ):
        await result(client, in_thread, "chat.discussion.unset", {"channel": str(CHANNEL_ID)})
        sent = admin_world.called("SetDiscussionGroupRequest")[-1]
        assert type(sent.group).__name__ == "InputChannelEmpty"


# ---------------------------------------------------------------------------
# chat similar / sponsored / suggestion / verification / affiliate
# ---------------------------------------------------------------------------


class TestExtras:
    async def test_similar_channels_report_the_truncation(
        self, live_daemon, client, in_thread, admin_world
    ):
        envelope = await call(
            client, in_thread, "chat.similar.list", {"chat": str(CHANNEL_ID)}, limit=1
        )
        assert envelope["page"]["total"] >= 1

    async def test_sponsored_messages_are_never_marked_viewed(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.sponsored.list", {"chat": str(CHANNEL_ID)})
        assert rows[0]["random_id"] == "AQID"
        assert rows[0].get("viewed", False) is False
        assert not admin_world.called("ViewSponsoredMessageRequest")

    async def test_reporting_a_sponsored_message_walks_the_menu(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.sponsored.report",
            {"chat": str(CHANNEL_ID), "random_id": "AQID"},
        )
        assert out["result"] == "choose-option"
        assert out["options"][0]["text"] == "Spam"
        picked = await result(
            client,
            in_thread,
            "chat.sponsored.report",
            {"chat": str(CHANNEL_ID), "random_id": "AQID", "option": out["options"][0]["option"]},
        )
        assert picked["result"] == "reported"

    async def test_dismissing_a_suggestion_reports_what_is_left(
        self, live_daemon, client, in_thread, admin_world
    ):
        admin_world.settings_of(CHANNEL_ID)["pending_suggestions"] = [
            "CONVERT_GIGAGROUP",
            "VALIDATE_PASSWORD",
        ]
        out = await result(
            client,
            in_thread,
            "chat.suggestion.delete",
            {"chat": str(CHANNEL_ID), "key": "CONVERT_GIGAGROUP"},
        )
        assert out["pending_suggestions"] == ["VALIDATE_PASSWORD"]

    async def test_verification_is_a_bots_badge(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client,
            in_thread,
            "chat.verification.set",
            {"chat": str(GROUP_ID), "bot": str(BOB)},
        )
        assert out["enabled"] is True and out["bot_id"] == BOB

    async def test_suggested_post_approval_and_rejection(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client,
            in_thread,
            "chat.suggested-post.approve",
            {"channel": str(CHANNEL_ID), "msg_id": 918},
        )
        assert out["approved"] is True
        out = await result(
            client,
            in_thread,
            "chat.suggested-post.deny",
            {"channel": str(CHANNEL_ID), "msg_id": 918, "comment": "off topic"},
        )
        assert out["rejected"] is True
        assert admin_world.called("ToggleSuggestedPostApprovalRequest")[-1].reject is True

    async def test_affiliate_bots_come_back_with_their_commission(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.affiliate.list", {"chat": str(CHANNEL_ID)})
        assert rows[0]["bot_id"] == 8800 and rows[0]["commission_permille"] == 200

    async def test_connecting_an_affiliate_bot(self, live_daemon, client, in_thread, admin_world):
        out = await result(
            client, in_thread, "chat.affiliate.set", {"chat": str(CHANNEL_ID), "bot": str(BOB)}
        )
        assert out["commission_permille"] == 200
        assert admin_world.called("ConnectStarRefBotRequest")

    async def test_a_channel_without_direct_messages_is_not_found(
        self, live_daemon, client, in_thread, admin_world
    ):
        code = await fails(client, in_thread, "chat.direct.list", {"channel": str(CHANNEL_ID)})
        assert code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# The layer-229 refusals
# ---------------------------------------------------------------------------


class TestLayerGaps:
    @pytest.mark.parametrize(
        ("op", "payload"),
        [
            ("chat.community.create", {"title": "Hub"}),
            ("chat.community.list", {}),
            ("chat.community.set", {"community": "@myhub", "chat": "@mygroup"}),
            ("chat.community.ban", {"community": "@myhub", "user": "@alice"}),
            ("chat.welcome.list", {"chat": "@mygroup"}),
            ("chat.welcome.set", {"chat": "@mygroup", "text": "hi"}),
            ("chat.welcome.delete", {"chat": "@mygroup", "everything": True}),
        ],
    )
    async def test_it_refuses_with_an_explanation_not_a_traceback(
        self, live_daemon, client, in_thread, admin_world, op, payload
    ):
        with pytest.raises(Exception) as caught:
            await call(client, in_thread, op, payload)
        body = classify(caught.value)
        assert body.exit_code == EXIT_INDETERMINATE
        assert "layer 229" in body.message

    async def test_the_rights_vocabulary_names_them_rather_than_hiding_them(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.permission.list", {})
        gaps = {row["name"] for row in rows if not row.get("supported", True)}
        assert gaps == {"manage-linked-peers", "manage-welcome-messages"}


# ---------------------------------------------------------------------------
# chat stats / revenue / boost
# ---------------------------------------------------------------------------


class TestStats:
    async def test_broadcast_stats_carry_growth_and_graphs(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.stats.get", {"chat": str(CHANNEL_ID)})
        assert out["type"] == "broadcast"
        assert out["followers"]["current"] == 120.0
        assert out["followers"]["growth"] == 20.0
        names = {graph["name"] for graph in out["graphs"]}
        assert "growth_graph" in names

    async def test_an_async_graph_stays_a_token_until_asked(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "chat.stats.get", {"chat": str(CHANNEL_ID)})
        growth = next(g for g in out["graphs"] if g["name"] == "growth_graph")
        assert growth["token"] == "growth-token" and "json" not in growth

    async def test_load_graphs_resolves_every_token(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(
            client, in_thread, "chat.stats.get", {"chat": str(CHANNEL_ID), "load_graphs": True}
        )
        growth = next(g for g in out["graphs"] if g["name"] == "growth_graph")
        assert growth["json"] == {"columns": ["x"]}
        assert admin_world.called("LoadAsyncGraphRequest")

    async def test_out_writes_the_specs_to_files(
        self, live_daemon, client, in_thread, admin_world, tmp_path
    ):
        out = await result(
            client,
            in_thread,
            "chat.stats.get",
            {"chat": str(CHANNEL_ID), "out": str(tmp_path / "graphs")},
        )
        written = [g for g in out["graphs"] if g.get("path")]
        assert written and (tmp_path / "graphs").is_dir()

    async def test_a_basic_group_has_no_statistics(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert (
            await fails(client, in_thread, "chat.stats.get", {"chat": str(BASIC_ID)}) == EXIT_USAGE
        )

    async def test_public_forwards_need_a_target(self, live_daemon, client, in_thread, admin_world):
        assert (
            await fails(client, in_thread, "chat.stats.list", {"chat": str(CHANNEL_ID)})
            == EXIT_USAGE
        )

    async def test_public_forwards_come_back_as_rows(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(
            client, in_thread, "chat.stats.list", {"chat": str(CHANNEL_ID), "message": 918}
        )
        assert rows[0]["msg_id"] == 12

    async def test_the_stats_alias_still_resolves(
        self, live_daemon, client, in_thread, admin_world
    ):
        out = await result(client, in_thread, "stats.get", {"chat": str(CHANNEL_ID)})
        assert out["type"] == "broadcast"


class TestRevenue:
    async def test_revenue_is_read_only_and_says_so(
        self, live_daemon, client, in_thread, admin_world
    ):
        envelope = await call(client, in_thread, "chat.revenue.get", {"chat": str(CHANNEL_ID)})
        assert envelope["result"]["overall_revenue"] == 900
        assert any("official client" in w for w in envelope["meta"]["warnings"])

    async def test_transactions_come_back_as_rows(
        self, live_daemon, client, in_thread, admin_world
    ):
        rows = await result(client, in_thread, "chat.revenue.list", {"chat": str(CHANNEL_ID)})
        assert rows[0]["amount"] == 50 and rows[0]["title"] == "Subscription"


class TestBoosts:
    async def test_status_carries_the_boost_url(self, live_daemon, client, in_thread, admin_world):
        out = await result(client, in_thread, "boost.get", {"chat": str(CHANNEL_ID)})
        assert out["boost_url"].startswith("https://t.me/boost")

    async def test_features_map_a_level_onto_a_tlgr_flag(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        admin_world.raw["GetAppConfigRequest"] = types.help.AppConfig(
            hash=0,
            config=types.JsonObject(
                value=[
                    types.JsonObjectValue(
                        key="channel_autotranslation_level_min", value=types.JsonNumber(value=3)
                    )
                ]
            ),
        )
        out = await result(client, in_thread, "boost.get", {"features": True})
        assert out["features"] == [
            {
                "key": "channel_autotranslation_level_min",
                "level": 3,
                "unlocks": "chat setting set --autotranslate",
            }
        ]

    async def test_boost_get_needs_a_chat_or_features(
        self, live_daemon, client, in_thread, admin_world
    ):
        assert await fails(client, in_thread, "boost.get", {}) == EXIT_USAGE

    async def test_my_slots_report_their_cooldown(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        admin_world.my_boosts = [
            types.MyBoost(
                slot=1, date=datetime.now(timezone.utc), expires=datetime.now(timezone.utc)
            )
        ]
        rows = await result(client, in_thread, "boost.list", {"mine": True})
        assert rows[0]["slot"] == 1

    async def test_boosting_spends_the_free_slots(
        self, live_daemon, client, in_thread, admin_world
    ):
        from telethon.tl import types

        admin_world.my_boosts = [
            types.MyBoost(
                slot=7, date=datetime.now(timezone.utc), expires=datetime.now(timezone.utc)
            )
        ]
        out = await result(client, in_thread, "boost.add", {"chat": str(CHANNEL_ID)})
        assert out["slots"] == [7]
        assert admin_world.called("ApplyBoostRequest")[-1].slots == [7]

    async def test_no_free_slot_is_not_found(self, live_daemon, client, in_thread, admin_world):
        admin_world.my_boosts = []
        code = await fails(client, in_thread, "boost.add", {"chat": str(CHANNEL_ID)})
        assert code == EXIT_NOT_FOUND

    async def test_the_boost_apply_alias_resolves(
        self, live_daemon, client, in_thread, admin_world
    ):
        from tlgr.registry import ALIASES

        assert ALIASES["boost.apply"] == "boost.add"
        assert ALIASES["premium.boost.apply"] == "boost.add"

    async def test_listing_a_chats_boosters(self, live_daemon, client, in_thread, admin_world):
        from telethon.tl import types

        admin_world.boosts[CHANNEL_ID] = [
            types.Boost(
                id="b1",
                date=datetime.now(timezone.utc),
                expires=datetime.now(timezone.utc),
                user_id=ALICE,
            )
        ]
        rows = await result(client, in_thread, "boost.list", {"chat": str(CHANNEL_ID)})
        assert rows[0]["user_id"] == ALICE
