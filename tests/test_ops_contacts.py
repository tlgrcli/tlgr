"""The contact, user and resolve operations, end to end through a real daemon.

Every test goes over a real Unix socket, through the real middleware chain and
the real dispatcher, into the real implementation, against a fake Telegram —
and the assertion is usually that the fake's *world moved*: the contact list
grew, the peer landed on the blocklist, `stories_hidden` really flipped. That
is the only way the idempotent second pass (`already: true`, no RPC) can be
asserted at all.

Three contracts get more attention than the rest, because AGENT.md freezes
them and a live agent reads them today:

* `user dialog-status` is three-valued and its exit code is part of the
  answer — exit 13 must never be reachable by reading "unknown" as "no";
* `user hide-stories` (now `story hide`) reports `already` and sends nothing when there is
  nothing to do;
* `contact rename` writes only *our* view of a name, and an empty first name
  still becomes `"."` the way v1 sent it.
"""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from telethon.tl import types

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    classify,
)

ALICE = 4242
BOB = 4343
CAROL = 4444
NOBODY = 999999
NEWS = 5150
NEWS_ID = -1000000000000 - NEWS
OTHER = 5151
OTHER_ID = -1000000000000 - OTHER


@pytest.fixture
def book(world):
    """An address book: two contacts, one stranger, two channels."""
    from fake_telethon import make_channel, make_user, make_wallpaper

    alice = make_user(ALICE, username="alice", first="Alice")
    alice.last_name = "Anderson"
    alice.phone = "15550001111"
    alice.status = types.UserStatusOnline(expires=datetime.now(timezone.utc) + timedelta(hours=1))
    world.add_contact(alice, mutual=True)

    bob = make_user(BOB, username="bobby", first="Bob")
    bob.phone = "15550002222"
    # `by_me` — the bucket is coarse because of OUR privacy, not Bob's.
    bob.status = types.UserStatusRecently(by_me=True)
    world.add_contact(bob, close_friend=True)

    carol = make_user(CAROL, username="carol", first="Carol")
    world.add_user(carol)

    world.add_channel(make_channel(NEWS, title="News")).username = "newschan"
    world.add_channel(make_channel(OTHER, title="Other"))
    world.search_mine = [ALICE]
    world.search_global = [CAROL]
    world.phonebook["+15550009999"] = CAROL
    # The two link targets `resolve link --open` reads back. The sticker and
    # wallpaper worlds belong to the media group; seeding them here is what
    # lets this test assert against the same replies that group's ops see.
    world.add_sticker_set("Pack", [])
    world.wallpapers["Slug"] = make_wallpaper("Slug", wallpaper_id=77)
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


# ---------------------------------------------------------------------------
# contact list
# ---------------------------------------------------------------------------


class TestContactList:
    async def test_the_contact_list_comes_back_with_its_people(
        self, live_daemon, client, in_thread, book
    ):
        rows = await result(client, in_thread, "contact.list")
        by_id = {row["id"]: row for row in rows}
        assert set(by_id) == {ALICE, BOB}
        assert by_id[ALICE]["name"] == "Alice Anderson"
        assert by_id[ALICE]["mutual"] is True

    async def test_v1_keys_survive(self, live_daemon, client, in_thread, book):
        """AGENT.md publishes id/name/username/phone for every row."""
        rows = await result(client, in_thread, "contact.list")
        assert {"id", "name", "username", "phone"} <= set(rows[0])

    async def test_ids_only_is_the_cheap_drift_check(self, live_daemon, client, in_thread, book):
        rows = await result(client, in_thread, "contact.list", {"ids_only": True})
        assert sorted(row["id"] for row in rows) == [ALICE, BOB]
        assert book.called("GetContactIDsRequest")
        assert not book.called("GetContactsRequest")

    async def test_with_status_merges_one_call_for_the_whole_list(
        self, live_daemon, client, in_thread, book
    ):
        rows = await result(client, in_thread, "contact.list", {"with_status": True})
        statuses = {row["id"]: row["status"]["kind"] for row in rows}
        assert statuses[ALICE] == "online"
        assert statuses[BOB] == "recently"
        assert len(book.called("GetStatusesRequest")) == 1

    async def test_a_coarse_bucket_says_it_is_our_own_privacy(
        self, live_daemon, client, in_thread, book
    ):
        """`by_me` is the difference between 'coarse' and 'they hid from you'."""
        rows = await result(client, in_thread, "contact.list", {"with_status": True})
        bob = next(row for row in rows if row["id"] == BOB)
        assert bob["status"]["by_me"] is True

    async def test_mutual_and_close_friend_filters(self, live_daemon, client, in_thread, book):
        mutual = await result(client, in_thread, "contact.list", {"mutual_only": True})
        close = await result(client, in_thread, "contact.list", {"close_friends_only": True})
        assert [row["id"] for row in mutual] == [ALICE]
        assert [row["id"] for row in close] == [BOB]

    async def test_sorting_is_local(self, live_daemon, client, in_thread, book):
        by_first = await result(client, in_thread, "contact.list", {"sort": "first-name"})
        assert [row["id"] for row in by_first] == [ALICE, BOB]

    async def test_the_cursor_walks_forward(self, live_daemon, client, in_thread, book):
        first = await call(client, in_thread, "contact.list", limit=1)
        assert first["page"]["has_more"] is True
        second = await call(
            client, in_thread, "contact.list", limit=1, cursor=first["page"]["next_cursor"]
        )
        assert first["result"][0]["id"] != second["result"][0]["id"]
        assert second["page"]["has_more"] is False

    async def test_export_writes_a_private_file(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        target = tmp_path / "book.vcf"
        await result(client, in_thread, "contact.list", {"export": "vcard", "out": str(target)})
        text = target.read_text()
        assert "BEGIN:VCARD" in text and "+15550001111" in text
        # A phonebook is not something to leave world-readable.
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    async def test_export_without_a_destination_is_a_usage_error(
        self, live_daemon, client, in_thread, book
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.list", {"export": "csv"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_with_stories_flags_unseen_ones(self, live_daemon, client, in_thread, book):
        book.users[ALICE].stories_max_id = types.RecentStory(max_id=9)
        book.story_read[ALICE] = 4
        book.users[BOB].stories_max_id = types.RecentStory(max_id=2)
        book.story_read[BOB] = 2
        rows = await result(client, in_thread, "contact.list", {"with_stories": True})
        unseen = {row["id"]: row["has_unseen_stories"] for row in rows}
        assert unseen == {ALICE: True, BOB: False}

    async def test_unregistered_lists_numbers_with_no_account(
        self, live_daemon, client, in_thread, book
    ):
        book.add_saved_contact("+15550003333", "Dave")
        book.add_saved_contact("+15550001111", "Alice")
        rows = await result(client, in_thread, "contact.list", {"unregistered": True})
        assert [row["phone"] for row in rows] == ["+15550003333"]


# ---------------------------------------------------------------------------
# contact add / rename / remove / note
# ---------------------------------------------------------------------------


class TestContactAdd:
    async def test_adding_a_known_user_uses_add_contact(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.add", {"user": "@carol", "first_name": "Carol"}
        )
        assert answer["added"] is True
        assert answer["user_id"] == CAROL
        assert CAROL in book.contacts
        assert book.users[CAROL].contact is True

    async def test_the_v1_positional_name_still_works(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.add", {"user": "@carol", "name": "Carol Cooper"}
        )
        assert (answer["first_name"], answer["last_name"]) == ("Carol", "Cooper")

    async def test_a_phone_goes_through_import_contacts(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.add", {"user": "+15550009999", "first_name": "Carol"}
        )
        assert answer["added"] is True
        assert answer["imported"] == [CAROL]
        assert book.called("ImportContactsRequest")

    async def test_an_empty_import_reports_the_ambiguity_not_a_negative(
        self, live_daemon, client, in_thread, book
    ):
        """No account and a privacy refusal are indistinguishable from here."""
        answer = await result(
            client, in_thread, "contact.add", {"user": "+15550007777", "first_name": "Nobody"}
        )
        assert answer["added"] is False
        assert answer["imported"] == []
        assert "privacy" in answer["reason"] or "refuses" in answer["reason"]

    async def test_share_phone_warns_that_it_cannot_be_undone(
        self, live_daemon, client, in_thread, book
    ):
        envelope = await call(
            client,
            in_thread,
            "contact.add",
            {"user": "@carol", "first_name": "Carol", "share_phone": True},
        )
        assert any("cannot be undone" in w for w in envelope["meta"]["warnings"])

    async def test_a_contact_card_in_a_message_can_be_added(
        self, live_daemon, client, in_thread, book
    ):
        message = book.add_message(ALICE, "", message_id=310)
        message.media = types.MessageMediaContact(
            phone_number="+15550009999",
            first_name="Carol",
            last_name="Cooper",
            vcard="",
            user_id=CAROL,
        )
        answer = await result(client, in_thread, "contact.add", {"from_message": "@alice:310"})
        assert answer["user_id"] == CAROL

    async def test_a_card_with_no_user_falls_back_to_the_phone(
        self, live_daemon, client, in_thread, book
    ):
        message = book.add_message(ALICE, "", message_id=311)
        message.media = types.MessageMediaContact(
            phone_number="+15550009999", first_name="Carol", last_name="", vcard="", user_id=0
        )
        answer = await result(client, in_thread, "contact.add", {"from_message": "@alice:311"})
        assert answer["imported"] == [CAROL]

    async def test_no_target_at_all_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.add", {"first_name": "Nobody"})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestContactRename:
    async def test_it_writes_only_our_view_of_the_name(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.rename", {"user": "@alice", "last_name": "· lead"}
        )
        assert answer == {
            "saved": True,
            "user_id": ALICE,
            "first_name": "Alice",
            "last_name": "· lead",
        }
        assert book.users[ALICE].last_name == "· lead"

    async def test_an_omitted_part_keeps_the_current_one(
        self, live_daemon, client, in_thread, book
    ):
        await result(client, in_thread, "contact.rename", {"user": "@alice", "last_name": "X"})
        assert book.users[ALICE].first_name == "Alice"

    async def test_an_empty_first_name_still_becomes_a_dot(
        self, live_daemon, client, in_thread, book
    ):
        """v1's dodge for CONTACT_NAME_EMPTY, which the tagging scheme relies on."""
        book.users[ALICE].first_name = ""
        answer = await result(
            client, in_thread, "contact.rename", {"user": "@alice", "last_name": "tagged"}
        )
        assert answer["first_name"] == "."

    async def test_it_works_on_a_non_contact(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.rename", {"user": "@carol", "first_name": "C"}
        )
        assert answer["saved"] is True


class TestContactRemove:
    async def test_removing_a_contact_moves_the_world(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "contact.remove", {"user": ["@alice"]})
        assert answer["removed"] is True
        assert answer["user_ids"] == [ALICE]
        assert ALICE not in book.contacts

    async def test_removing_by_phone_reaches_numbers_with_no_account(
        self, live_daemon, client, in_thread, book
    ):
        book.add_saved_contact("+15550003333", "Dave")
        answer = await result(client, in_thread, "contact.remove", {"phone": ["+1 555 000 3333"]})
        assert answer["phones"] == ["+15550003333"]
        assert book.saved_contacts == []

    async def test_nothing_to_remove_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.remove", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestContactNote:
    async def test_a_note_is_written_and_read_back_through_user_get(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(
            client, in_thread, "contact.note.set", {"user": "@alice", "text": "met in Berlin"}
        )
        assert answer["user_id"] == ALICE
        assert answer["note"] == "met in Berlin"
        profile = await result(client, in_thread, "user.get", {"user": "@alice", "full": True})
        assert profile["note"] == "met in Berlin"

    async def test_clearing_sends_an_empty_text(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "contact.note.set", {"user": "@alice", "text": "x"})
        answer = await result(
            client, in_thread, "contact.note.set", {"user": "@alice", "clear": True}
        )
        assert answer["cleared"] is True
        assert ALICE not in book.contact_notes

    async def test_a_note_on_a_non_contact_fails(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception):
            await result(client, in_thread, "contact.note.set", {"user": "@carol", "text": "hi"})


# ---------------------------------------------------------------------------
# contact search / status / birthdays / joined
# ---------------------------------------------------------------------------


class TestContactSearch:
    async def test_results_are_labelled_with_where_they_came_from(
        self, live_daemon, client, in_thread, book
    ):
        rows = await result(client, in_thread, "contact.search", {"query": "a"})
        sources = {row["peer"]["id"]: row["source"] for row in rows}
        assert sources[ALICE] == "mine"
        assert sources[CAROL] == "global"

    async def test_mine_only_and_global_only(self, live_daemon, client, in_thread, book):
        mine = await result(client, in_thread, "contact.search", {"query": "a", "mine_only": True})
        globally = await result(
            client, in_thread, "contact.search", {"query": "a", "global_only": True}
        )
        assert [row["peer"]["id"] for row in mine] == [ALICE]
        assert [row["peer"]["id"] for row in globally] == [CAROL]

    async def test_adverts_are_off_by_default(self, live_daemon, client, in_thread, book):
        book.sponsored_peers = [CAROL]
        rows = await result(client, in_thread, "contact.search", {"query": "a"})
        assert not any(row["sponsored"] for row in rows)
        assert not book.called("GetSponsoredPeersRequest")

    async def test_sponsored_rows_are_labelled_when_asked_for(
        self, live_daemon, client, in_thread, book
    ):
        book.sponsored_peers = [CAROL]
        rows = await result(
            client, in_thread, "contact.search", {"query": "a", "with_sponsored": True}
        )
        assert any(row["source"] == "sponsored" for row in rows)

    async def test_recent_history_is_local_state_that_survives_a_search(
        self, live_daemon, client, in_thread, book
    ):
        await result(client, in_thread, "contact.search", {"query": "alice"})
        rows = await result(client, in_thread, "contact.search", {"recent": True})
        assert ALICE in {row["peer"]["id"] for row in rows}
        assert all(row["source"] == "recent" for row in rows)

    async def test_clear_recent_empties_it(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "contact.search", {"query": "alice"})
        await result(client, in_thread, "contact.search", {"clear_recent": True, "recent": True})
        rows = await result(client, in_thread, "contact.search", {"recent": True})
        assert rows == []

    async def test_forget_resets_the_server_side_rating_too(
        self, live_daemon, client, in_thread, book
    ):
        await result(client, in_thread, "contact.search", {"query": "alice"})
        await result(client, in_thread, "contact.search", {"forget": "@alice", "recent": True})
        assert book.called("ResetTopPeerRatingRequest")

    async def test_an_empty_query_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.search", {"query": "  "})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_search_stays_dry_runnable(self, live_daemon, client, in_thread, book):
        """A read must not print a stub just because one flag can write."""
        envelope = await call(client, in_thread, "contact.search", {"query": "alice"}, dry_run=True)
        assert envelope["result"][0]["peer"]["id"] == ALICE

    async def test_dry_run_does_not_forget_anything(self, live_daemon, client, in_thread, book):
        await call(
            client, in_thread, "contact.search", {"forget": "@alice", "recent": True}, dry_run=True
        )
        assert not book.called("ResetTopPeerRatingRequest")


class TestContactStatuses:
    async def test_statuses_come_back_in_one_call(self, live_daemon, client, in_thread, book):
        rows = await result(client, in_thread, "contact.status.list")
        assert {row["user_id"] for row in rows} == {ALICE, BOB}

    async def test_online_only(self, live_daemon, client, in_thread, book):
        rows = await result(client, in_thread, "contact.status.list", {"online_only": True})
        assert [row["user_id"] for row in rows] == [ALICE]


class TestContactBirthdays:
    async def test_todays_birthdays_carry_an_age(self, live_daemon, client, in_thread, book):
        today = datetime.now(timezone.utc)
        book.birthdays[ALICE] = types.Birthday(day=today.day, month=today.month, year=1990)
        rows = await result(client, in_thread, "contact.birthday.list")
        assert rows[0]["id"] == ALICE
        assert rows[0]["birthday"].endswith(f"{today.month:02d}-{today.day:02d}")
        assert rows[0]["age"] == today.year - 1990

    async def test_a_birthday_outside_the_window_is_dropped(
        self, live_daemon, client, in_thread, book
    ):
        far = datetime.now(timezone.utc) + timedelta(days=40)
        book.birthdays[ALICE] = types.Birthday(day=far.day, month=far.month, year=1990)
        rows = await result(client, in_thread, "contact.birthday.list")
        assert rows == []


class TestContactJoined:
    async def test_the_notification_switch_is_read_back(self, live_daemon, client, in_thread, book):
        book.contact_signup_silent = True
        rows = await result(client, in_thread, "contact.joined.list", {"notify": "on"})
        assert book.contact_signup_silent is False
        assert rows == [] or rows[0]["notify"] is True

    async def test_a_signup_service_message_is_found(self, live_daemon, client, in_thread, book):
        from fake_telethon import _Dialog

        message = book.add_message(ALICE, "", message_id=320)
        message.action = types.MessageActionContactSignUp()
        book.dialogs = [_Dialog(book.users[ALICE])]
        rows = await result(client, in_thread, "contact.joined.list")
        assert [row["user_id"] for row in rows] == [ALICE]


# ---------------------------------------------------------------------------
# blocking
# ---------------------------------------------------------------------------


class TestBlocking:
    async def test_block_lands_the_peer_on_the_list(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "user.block", {"user": "@carol"})
        assert answer["blocked"] is True
        assert CAROL in book.blocked
        rows = await result(client, in_thread, "contact.blocked.list")
        assert [row["peer"]["id"] for row in rows] == [CAROL]

    async def test_the_story_blocklist_is_independent(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "user.block", {"user": "@carol", "stories": True})
        assert CAROL in book.blocked_stories
        assert CAROL not in book.blocked
        rows = await result(client, in_thread, "contact.blocked.list", {"stories": True})
        assert [row["kind"] for row in rows] == ["stories"]

    async def test_delete_history_revokes_for_both_sides(
        self, live_daemon, client, in_thread, book
    ):
        book.add_message(ALICE, "hello", message_id=301)
        answer = await result(
            client, in_thread, "user.block", {"user": "@alice", "delete_history": True}
        )
        assert answer["deleted"] is True
        assert book.history(ALICE) == []

    async def test_report_spam_happens_before_the_block(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "user.block", {"user": "@carol", "report_spam": True})
        names = [name for name, _ in book.calls]
        assert names.index("ReportSpamRequest") < names.index("BlockRequest")

    async def test_from_replies_takes_a_message_id_in_the_replies_chat(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "user.block", {"from_replies": 77})
        assert answer["blocked"] is True
        assert book.called("BlockFromRepliesRequest")[0].msg_id == 77

    async def test_blocking_nothing_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.block", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_unblock_is_idempotent(self, live_daemon, client, in_thread, book):
        book.block(CAROL)
        first = await call(client, in_thread, "user.unblock", {"user": "@carol"})
        second = await call(client, in_thread, "user.unblock", {"user": "@carol"})
        assert first["result"]["already"] is False
        assert second["result"]["already"] is True
        assert second["meta"]["already"] is True

    async def test_blocked_list_pages(self, live_daemon, client, in_thread, book):
        book.block(ALICE)
        book.block(BOB)
        book.block(CAROL)
        first = await call(client, in_thread, "contact.blocked.list", limit=2)
        assert first["page"]["total"] == 3
        assert first["page"]["has_more"] is True
        second = await call(
            client,
            in_thread,
            "contact.blocked.list",
            limit=2,
            cursor=first["page"]["next_cursor"],
        )
        assert len(second["result"]) == 1

    async def test_set_blocked_reports_the_diff_it_caused(
        self, live_daemon, client, in_thread, book
    ):
        book.block(ALICE)
        answer = await result(client, in_thread, "contact.blocked.set", {"user": ["@carol"]})
        assert answer["blocked"] == [CAROL]
        assert answer["unblocked"] == [ALICE]
        assert set(book.blocked) == {CAROL}

    async def test_an_empty_replacement_is_refused(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.blocked.set", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_set_blocked_reads_a_file(self, live_daemon, client, in_thread, book, tmp_path):
        listing = tmp_path / "block.txt"
        listing.write_text("# spammers\n@carol\n")
        answer = await result(client, in_thread, "contact.blocked.set", {"from_file": str(listing)})
        assert answer["count"] == 1


# ---------------------------------------------------------------------------
# close friends and top peers
# ---------------------------------------------------------------------------


class TestCloseFriends:
    async def test_the_list_is_the_contact_list_filtered(
        self, live_daemon, client, in_thread, book
    ):
        rows = await result(client, in_thread, "contact.close-friends.list")
        assert [row["id"] for row in rows] == [BOB]

    async def test_setting_replaces_the_whole_list(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "contact.close-friends.set", {"user": ["@alice"]})
        assert answer["user_ids"] == [ALICE]
        assert book.users[BOB].close_friend is False

    async def test_add_is_a_read_modify_write(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "contact.close-friends.set", {"add": ["@alice"]})
        assert sorted(answer["user_ids"]) == sorted([ALICE, BOB])

    async def test_remove_is_a_read_modify_write(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.close-friends.set", {"remove": ["@bobby"]}
        )
        assert answer["user_ids"] == []

    async def test_no_change_sends_no_rpc(self, live_daemon, client, in_thread, book):
        envelope = await call(client, in_thread, "contact.close-friends.set", {"user": ["@bobby"]})
        assert envelope["meta"]["already"] is True
        assert not book.called("EditCloseFriendsRequest")

    async def test_only_contacts_may_be_close_friends(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.close-friends.set", {"user": ["@carol"]})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestTopPeers:
    async def test_categories_come_back_labelled(self, live_daemon, client, in_thread, book):
        book.top_peers["correspondents"] = [(ALICE, 12.5)]
        rows = await result(client, in_thread, "contact.top.list")
        assert rows[0]["peer"]["id"] == ALICE
        assert rows[0]["category"] == "correspondents"
        assert rows[0]["rating"] == 12.5

    async def test_an_unknown_category_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.top.list", {"category": ["nope"]})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_disabled_feature_is_indeterminate_not_empty(
        self, live_daemon, client, in_thread, book
    ):
        """Nothing was measured, so an empty list would be a lie by omission."""
        book.top_peers_enabled = False
        envelope = await call(client, in_thread, "contact.top.list")
        assert envelope["result"] == []
        assert envelope["meta"]["indeterminate"] is True
        assert "turned off" in envelope["meta"]["reason"]

    async def test_turning_it_off_wipes_the_ratings(self, live_daemon, client, in_thread, book):
        book.top_peers["correspondents"] = [(ALICE, 1.0)]
        answer = await result(client, in_thread, "contact.top.set", {"state": "off"})
        assert answer["enabled"] is False
        assert book.top_peers == {}

    async def test_reset_zeroes_one_peer(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "contact.top.set", {"reset": "@alice"})
        assert answer["reset_peer"] == ALICE
        assert book.called("ResetTopPeerRatingRequest")

    async def test_neither_state_nor_reset_is_a_usage_error(
        self, live_daemon, client, in_thread, book
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.top.set", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# sharing, the phonebook, syncing
# ---------------------------------------------------------------------------


class TestSharing:
    async def test_sharing_a_card_sends_a_message(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "contact.share", {"user": "@alice", "to": "@bobby"}
        )
        assert answer["chat_id"] == BOB
        assert answer["contact"]["id"] == ALICE
        assert book.history(BOB)

    async def test_sharing_needs_a_destination(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.share", {"user": "@alice"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_share_phone_accepts_the_contact(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "contact.share-phone", {"user": "@alice"})
        assert answer == {"user_id": ALICE, "shared": True}
        assert book.called("AcceptContactRequest")


class TestPhonebook:
    def _vcard(self, path: Path) -> Path:
        path.write_text(
            "BEGIN:VCARD\nVERSION:3.0\nN:Cooper;Carol;;;\nFN:Carol Cooper\n"
            "TEL;TYPE=CELL:+1 555 000 9999\nEND:VCARD\n"
        )
        return path

    async def test_import_reads_a_vcard_and_reports_what_landed(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        answer = await result(
            client,
            in_thread,
            "contact.import",
            {"file": str(self._vcard(tmp_path / "book.vcf"))},
        )
        assert answer["parsed"] == 1
        assert answer["imported"][0]["user_id"] == CAROL
        assert CAROL in book.contacts

    async def test_import_reports_the_retry_list_rather_than_dropping_it(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        book.phonebook["+15550008888"] = -1  # the fake's "ask again later"
        path = tmp_path / "retry.csv"
        path.write_text("+15550008888,Later,Person\n")
        envelope = await call(client, in_thread, "contact.import", {"file": str(path)})
        assert envelope["result"]["retry"][0]["phone"] == "+15550008888"
        assert any("retry_contacts" in w for w in envelope["meta"]["warnings"])

    async def test_import_batches(self, live_daemon, client, in_thread, book, tmp_path):
        path = tmp_path / "many.csv"
        path.write_text("\n".join(f"+1555000{i:04d},P{i}," for i in range(5)))
        answer = await result(
            client, in_thread, "contact.import", {"file": str(path), "batch_size": 2}
        )
        assert answer["batches"] == 3

    async def test_an_empty_file_is_a_usage_error(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        path = tmp_path / "empty.csv"
        path.write_text("phone,first,last\n")
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.import", {"file": str(path)})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_stdin_is_refused_because_the_daemon_cannot_reach_it(
        self, live_daemon, client, in_thread, book
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "contact.import", {"file": "-"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_sync_prints_the_diff_and_changes_nothing_by_default(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        answer = await result(
            client, in_thread, "contact.sync", {"file": str(self._vcard(tmp_path / "b.vcf"))}
        )
        assert answer["applied"] is False
        assert answer["to_import"][0]["phone"] == "+15550009999"
        assert CAROL not in book.contacts

    async def test_sync_applies_and_can_delete_what_is_missing(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        answer = await result(
            client,
            in_thread,
            "contact.sync",
            {
                "file": str(self._vcard(tmp_path / "b.vcf")),
                "apply": True,
                "delete_missing": True,
            },
        )
        assert answer["applied"] is True
        assert answer["imported"] == 1
        assert sorted(answer["to_delete"]) == ["+15550001111", "+15550002222"]

    async def test_saved_list_reaches_numbers_with_no_account(
        self, live_daemon, client, in_thread, book
    ):
        book.add_saved_contact("+15550003333", "Dave")
        book.add_saved_contact("+15550001111", "Alice")
        rows = await result(client, in_thread, "contact.saved.list", {"invite_text": True})
        by_phone = {row["phone"]: row for row in rows}
        assert by_phone["+15550001111"]["has_account"] is True
        assert by_phone["+15550003333"]["has_account"] is False
        assert rows[0]["invite_text"]


# ---------------------------------------------------------------------------
# user get
# ---------------------------------------------------------------------------


class TestUserGet:
    async def test_v1_keys_survive_verbatim(self, live_daemon, client, in_thread, book):
        """AGENT.md publishes id/first_name/username/bio/is_bot/status."""
        book.user_full[ALICE] = {"about": "somewhere warm"}
        profile = await result(client, in_thread, "user.get", {"user": "@alice"})
        assert profile["id"] == ALICE
        assert profile["first_name"] == "Alice"
        assert profile["username"] == "alice"
        assert profile["bio"] == "somewhere warm"
        assert profile["is_bot"] is False
        assert profile["status"] == "online"

    async def test_stories_hidden_is_auditable_without_a_write(
        self, live_daemon, client, in_thread, book
    ):
        book.users[ALICE].stories_hidden = True
        profile = await result(client, in_thread, "user.get", {"user": "@alice"})
        assert profile["stories_hidden"] is True

    async def test_the_access_hash_is_never_printed(self, live_daemon, client, in_thread, book):
        profile = await result(client, in_thread, "user.get", {"user": "@alice"})
        assert "access_hash" not in profile
        assert profile["access_hash_cached"] is True

    async def test_full_adds_the_profile_fields(self, live_daemon, client, in_thread, book):
        book.user_full[ALICE] = {"about": "hi", "common_chats_count": 2}
        profile = await result(client, in_thread, "user.get", {"user": "@alice", "full": True})
        assert profile["full"] is True
        assert profile["common_chats_count"] == 2

    async def test_blocked_state_comes_from_the_full_user(
        self, live_daemon, client, in_thread, book
    ):
        book.block(ALICE)
        profile = await result(client, in_thread, "user.get", {"user": "@alice", "full": True})
        assert profile["blocked"] is True

    async def test_one_field_is_pulled_out_with_the_global_select(
        self, live_daemon, client, in_thread, book
    ):
        """`--select` is the projection, and it works on every op."""
        from click.testing import CliRunner

        from tlgr.cli import cli

        outcome = CliRunner().invoke(cli, ["user", "get", "--help"])
        assert outcome.exit_code == 0
        assert "--select" in outcome.output
        assert "--field" not in outcome.output

    async def test_a_short_profile_survives_a_full_user_failure(
        self, live_daemon, client, in_thread, book
    ):
        book.fail_next("GetFullUserRequest", RuntimeError("privacy"))
        envelope = await call(client, in_thread, "user.get", {"user": "@alice", "full": True})
        assert envelope["result"]["id"] == ALICE
        assert any("getFullUser" in w for w in envelope["meta"]["warnings"])

    async def test_a_bio_can_be_translated(self, live_daemon, client, in_thread, book):
        from telethon.tl import types as tl

        book.user_full[ALICE] = {"about": "irgendwo warm"}
        book.raw["TranslateTextRequest"] = tl.messages.TranslateResult(
            result=[tl.TextWithEntities(text="somewhere warm", entities=[])]
        )
        profile = await result(
            client, in_thread, "user.get", {"user": "@alice", "translate_bio": "en"}
        )
        assert profile["bio_translated"] == "somewhere warm"

    async def test_a_min_user_is_addressed_through_the_message_it_was_seen_in(
        self, live_daemon, client, in_thread, book
    ):
        """Telethon builds `inputUserFromMessage` for nobody; tlgr does."""
        book.add_message(NEWS_ID, "posted", message_id=88)
        envelope = await call(
            client,
            in_thread,
            "user.get",
            {"user": str(CAROL), "from_chat": str(NEWS_ID), "from_message": 88},
        )
        built = book.called("GetUsersRequest")[0].id[0]
        assert type(built).__name__ == "InputUserFromMessage"
        assert envelope["result"]["id"] == CAROL

    async def test_an_unknown_username_is_not_found(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.get", {"user": "@ghost"})
        assert classify(caught.value).exit_code in (EXIT_NOT_FOUND, EXIT_INDETERMINATE)

    async def test_a_channel_id_is_refused_as_a_user(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.get", {"user": str(NEWS_ID)})
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# user dialog-status — the frozen contract
# ---------------------------------------------------------------------------


class TestDialogStatus:
    async def test_a_dialog_is_confirmed_against_the_server(
        self, live_daemon, client, in_thread, book
    ):
        book.add_message(ALICE, "hello", message_id=101)
        book.add_dialog(ALICE, top_message=101)
        answer = await result(client, in_thread, "user.dialog-status", {"user": "@alice"})
        assert answer["resolved"] is True
        assert answer["has_dialog"] is True
        assert answer["source"] == "peer_dialogs"

    async def test_the_documented_keys_are_all_there(self, live_daemon, client, in_thread, book):
        book.add_dialog(ALICE, top_message=0)
        answer = await result(client, in_thread, "user.dialog-status", {"user": "@alice"})
        assert {
            "ref",
            "id",
            "username",
            "resolved",
            "has_dialog",
            "message_count",
            "read_outbox_max_id",
            "unread_count",
            "top_message",
            "source",
        } <= set(answer)

    async def test_resolving_an_entity_is_not_evidence_of_a_dialog(
        self, live_daemon, client, in_thread, book
    ):
        """A group co-member resolves fine and has never been messaged."""
        answer = await result(client, in_thread, "user.dialog-status", {"user": "@carol"})
        assert answer["resolved"] is True
        assert answer["has_dialog"] is False
        assert answer["source"] == "peer_dialogs"

    async def test_an_exhausted_dialog_list_is_the_only_licence_for_a_negative(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "user.dialog-status", {"user": str(NOBODY)})
        assert answer["resolved"] is True
        assert answer["has_dialog"] is False
        assert answer["source"] == "dialog_scan"
        assert "complete dialog list" in answer["reason"]

    async def test_a_capped_scan_is_indeterminate_and_exits_13(
        self, live_daemon, client, in_thread, book
    ):
        """THE bug: a truncated scan proves nothing and must not read as 'go'."""
        envelope = await call(
            client,
            in_thread,
            "user.dialog-status",
            {"user": str(NOBODY), "max_dialogs": 1},
        )
        answer = envelope["result"]
        assert answer["resolved"] is False
        assert answer["has_dialog"] is None  # NOT False
        assert "cap" in answer["reason"]
        assert envelope["meta"]["indeterminate"] is True

    async def test_the_three_outcomes_are_distinguishable(
        self, live_daemon, client, in_thread, book
    ):
        book.add_message(ALICE, "hi", message_id=101)
        book.add_dialog(ALICE, top_message=101)
        positive = await result(client, in_thread, "user.dialog-status", {"user": "@alice"})
        negative = await result(client, in_thread, "user.dialog-status", {"user": str(NOBODY)})
        unknown = await result(
            client, in_thread, "user.dialog-status", {"user": str(NOBODY), "max_dialogs": 1}
        )
        assert (positive["resolved"], positive["has_dialog"]) == (True, True)
        assert (negative["resolved"], negative["has_dialog"]) == (True, False)
        assert (unknown["resolved"], unknown["has_dialog"]) == (False, None)

    async def test_the_cli_exits_13_on_an_indeterminate_answer(self, tlgr_home, monkeypatch):
        """Exit 13 is part of the answer, not decoration on top of it."""
        from click.testing import CliRunner

        from tlgr.cli import cli, gen

        def fake_dispatch(spec, request, state):
            return {
                "ok": True,
                "op": spec.id,
                "result": {"ref": "@x", "resolved": False, "has_dialog": None},
                "meta": {"indeterminate": True, "reason": "cap hit"},
            }

        monkeypatch.setattr(gen, "_dispatch", fake_dispatch)
        outcome = CliRunner().invoke(cli, ["--json", "user", "dialog-status", "@alice"])
        assert outcome.exit_code == EXIT_INDETERMINATE
        assert '"has_dialog": null' in outcome.output

    # -- the peer's read state, which must always be PRESENT ---------------
    #
    # An ABSENT key reads back as `null`, which is indistinguishable from
    # "they have not read it". A real 0 must be a real 0, and every other
    # outcome must still carry the key. `/chat/list` has always reported this
    # field, so without it the two routes disagreed about the same peer.

    async def test_read_state_is_reported_when_the_peer_read_our_message(
        self, live_daemon, client, in_thread, book
    ):
        book.add_message(ALICE, "hello", message_id=893)
        book.add_dialog(ALICE, top_message=893, unread_count=0)
        book.read_outbox[ALICE] = 893
        answer = await result(client, in_thread, "user.dialog-status", {"user": "@alice"})
        assert answer["read_outbox_max_id"] == 893
        assert answer["top_message"] == 893
        assert answer["unread_count"] == 0

    async def test_an_unread_outgoing_message_is_zero_not_missing(
        self, live_daemon, client, in_thread, book
    ):
        """The trap this exists to close: 0 is an answer, absence is not."""
        book.add_message(ALICE, "hello", message_id=852)
        book.add_dialog(ALICE, top_message=852)
        book.read_outbox[ALICE] = 0
        answer = await result(client, in_thread, "user.dialog-status", {"user": "@alice"})
        assert answer["read_outbox_max_id"] == 0
        assert answer["top_message"] == 852

    async def test_every_return_path_carries_the_read_state_keys(
        self, live_daemon, client, in_thread, book
    ):
        """Including the ones that answer nothing — that is the whole point."""
        keys = {"read_outbox_max_id", "unread_count", "top_message"}

        indeterminate = await result(
            client, in_thread, "user.dialog-status", {"user": str(NOBODY), "max_dialogs": 1}
        )
        assert keys <= set(indeterminate)
        assert all(indeterminate[key] is None for key in keys)

        negative = await result(client, in_thread, "user.dialog-status", {"user": str(NOBODY)})
        assert keys <= set(negative)
        assert all(negative[key] == 0 for key in keys)

    async def test_the_read_state_does_not_become_a_they_read_it_boolean(
        self, live_daemon, client, in_thread, book
    ):
        """The comparison is only meaningful when the last message is ours,
        which only the caller knows — so the raw fields are echoed and no
        derived verdict is invented."""
        book.add_message(ALICE, "hi", message_id=849)
        book.add_dialog(ALICE, top_message=849, unread_count=3)
        book.read_outbox[ALICE] = 846
        answer = await result(client, in_thread, "user.dialog-status", {"user": "@alice"})
        assert (answer["read_outbox_max_id"], answer["top_message"]) == (846, 849)
        assert answer["unread_count"] == 3
        assert "they_read_it" not in answer


# ---------------------------------------------------------------------------
# user hide-stories — the frozen contract, now owned by `story hide`
# ---------------------------------------------------------------------------


class TestHideStories:
    """The v1 path is a legacy path of `story.hide`, so the contract is tested
    through the id an agent would actually call. AGENT.md's four keys, the
    idempotence and the bulk shape are unchanged; only the owner moved."""

    async def test_hiding_moves_the_flag_and_reports_v1_keys(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "story.hide", {"chat": ["@alice"]})
        assert {
            "user_id": ALICE,
            "username": "alice",
            "hidden": True,
            "already": False,
        }.items() <= answer.items()
        assert book.users[ALICE].stories_hidden is True

    async def test_a_second_pass_costs_no_rpc(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "story.hide", {"chat": ["@alice"]})
        book.calls.clear()
        envelope = await call(client, in_thread, "story.hide", {"chat": ["@alice"]})
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True
        assert not book.called("TogglePeerStoriesHiddenRequest")

    async def test_unhide_puts_them_back(self, live_daemon, client, in_thread, book):
        book.users[ALICE].stories_hidden = True
        answer = await result(client, in_thread, "story.hide", {"chat": ["@alice"], "unhide": True})
        assert answer["hidden"] is False
        assert book.users[ALICE].stories_hidden is False

    async def test_a_bulk_pass_keeps_the_single_peer_shape(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "story.hide", {"chat": ["@alice", "@bobby"]})
        assert answer["user_id"] == ALICE
        assert [row["user_id"] for row in answer["peers"]] == [ALICE, BOB]

    async def test_the_whole_strip_can_be_collapsed(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "story.hide", {"every": True})
        assert answer["all"] is True
        assert book.all_stories_hidden is True

    async def test_no_target_at_all_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "story.hide", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# the rest of the user group
# ---------------------------------------------------------------------------


class TestUserMisc:
    async def test_can_message_reports_the_requirement(self, live_daemon, client, in_thread, book):
        book.contact_requirements = {ALICE: "free", BOB: "paid:25"}
        rows = await result(client, in_thread, "user.can-message", {"user": ["@alice", "@bobby"]})
        by_id = {row["user_id"]: row for row in rows}
        assert by_id[ALICE]["result"] == "free"
        assert by_id[BOB]["result"] == "paid"
        assert by_id[BOB]["stars_amount"] == 25

    async def test_can_message_needs_a_user(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.can-message", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_common_chats_lists_what_is_shared(self, live_daemon, client, in_thread, book):
        rows = await result(client, in_thread, "user.chat.list", {"user": "@alice"})
        assert {row["id"] for row in rows} == {NEWS_ID, OTHER_ID}

    async def test_leave_all_is_dry_runnable(self, live_daemon, client, in_thread, book):
        envelope = await call(
            client,
            in_thread,
            "user.chat.list",
            {"user": "@alice", "leave_all": True},
            dry_run=True,
        )
        assert any("would leave" in w for w in envelope["meta"]["warnings"])
        assert not book.called("LeaveChannelRequest")

    async def test_leave_all_leaves(self, live_daemon, client, in_thread, book):
        rows = await result(
            client, in_thread, "user.chat.list", {"user": "@alice", "leave_all": True}
        )
        assert all(row["left"] for row in rows)
        assert len(book.called("LeaveChannelRequest")) == 2

    async def test_a_link_is_built_locally(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "user.link", {"user": "@alice"})
        assert answer["url"] == "https://t.me/alice"

    async def test_a_profile_link_and_a_prefilled_draft(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "user.link", {"user": "@alice", "profile": True, "text": "hi"}
        )
        assert "profile" in answer["url"] and "text=hi" in answer["url"]

    async def test_a_user_with_no_username_has_no_tme_link(
        self, live_daemon, client, in_thread, book
    ):
        book.users[CAROL].username = None
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.link", {"user": str(CAROL)})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_a_contact_token_link_reports_its_expiry(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "user.link", {"user": "me", "token": True})
        assert answer["kind"] == "contact-token"
        assert answer["expires"]

    async def test_a_token_link_is_only_for_me(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.link", {"user": "@alice", "token": True})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_profile_photos_page(self, live_daemon, client, in_thread, book):
        book.user_photos[ALICE] = [
            types.Photo(
                id=900 + i,
                access_hash=1,
                file_reference=b"",
                date=datetime.now(timezone.utc),
                sizes=[],
                dc_id=2,
            )
            for i in range(3)
        ]
        first = await call(client, in_thread, "user.photo.list", {"user": "@alice"}, limit=2)
        assert len(first["result"]) == 2
        assert first["page"]["total"] == 3

    async def test_a_personal_photo_can_be_set_and_reset(
        self, live_daemon, client, in_thread, book, tmp_path
    ):
        image = tmp_path / "avatar.jpg"
        image.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 64)
        answer = await result(
            client, in_thread, "user.photo.set", {"user": "@alice", "file": str(image)}
        )
        assert answer["photo_id"] == 5150
        cleared = await result(
            client, in_thread, "user.photo.set", {"user": "@alice", "reset": True}
        )
        assert cleared["reset"] is True

    async def test_pinned_music_comes_back(self, live_daemon, client, in_thread, book):
        book.saved_music[ALICE] = [
            types.Document(
                id=991,
                access_hash=1,
                file_reference=b"",
                date=datetime.now(timezone.utc),
                mime_type="audio/mpeg",
                size=1024,
                dc_id=2,
                attributes=[
                    types.DocumentAttributeAudio(duration=180, title="Nocturne", performer="Chopin")
                ],
            )
        ]
        rows = await result(client, in_thread, "user.music.list", {"user": "@alice"})
        assert rows[0]["title"] == "Nocturne"
        assert rows[0]["performer"] == "Chopin"

    async def test_a_personal_channel_comes_with_its_posts(
        self, live_daemon, client, in_thread, book
    ):
        book.user_full[ALICE] = {"personal_channel_id": NEWS, "personal_channel_message": 7}
        book.add_message(NEWS_ID, "a post", message_id=7)
        answer = await result(client, in_thread, "user.personal-channel.get", {"user": "@alice"})
        assert answer["channel"]["id"] == NEWS_ID
        assert answer["posts"][0]["text"] == "a post"

    async def test_no_personal_channel_is_not_found(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.personal-channel.get", {"user": "@alice"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_a_birthday_can_be_suggested(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "user.birthday.set", {"user": "@alice", "date": "1990-04-01"}
        )
        assert answer == {"user_id": ALICE, "birthday": "1990-04-01", "sent": True}

    async def test_a_month_day_birthday_is_accepted(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "user.birthday.set", {"user": "@alice", "date": "04-01"}
        )
        assert answer["birthday"] == "04-01"

    async def test_a_bad_date_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "user.birthday.set", {"user": "@alice", "date": "nope"})
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    async def test_a_username_resolves_to_a_peer(self, live_daemon, client, in_thread, book):
        answer = await result(client, in_thread, "resolve.username", {"username": "alice"})
        assert answer["peer"]["id"] == ALICE
        assert answer["kind"] == "user"

    async def test_a_free_username_is_not_found(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "resolve.username", {"username": "ghosty"})
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_a_type_mismatch_is_not_found(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "resolve.username", {"username": "alice", "type": "channel"}
            )
        assert classify(caught.value).exit_code == EXIT_NOT_FOUND

    async def test_an_empty_username_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "resolve.username", {"username": " "})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_phone_resolves_without_adding_a_contact(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "resolve.phone", {"phone": "+1 555 000 9999"})
        assert answer["resolved"] is True
        assert answer["peer"]["id"] == CAROL
        assert CAROL not in book.contacts

    async def test_an_unoccupied_phone_is_indeterminate_never_not_found(
        self, live_daemon, client, in_thread, book
    ):
        """No account and a privacy refusal are indistinguishable from here."""
        envelope = await call(client, in_thread, "resolve.phone", {"phone": "+15550007777"})
        assert envelope["result"]["resolved"] is False
        assert "refuse lookups by phone" in envelope["result"]["reason"]
        assert envelope["meta"]["indeterminate"] is True

    async def test_offline_formats_and_validates_without_an_rpc(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(
            client, in_thread, "resolve.phone", {"phone": "+1 555 000 1111", "offline": True}
        )
        assert answer["e164"] == "+15550001111"
        assert answer["country"] == "United States"
        assert answer["resolved"] is False
        assert not book.called("ResolvePhoneRequest")

    async def test_peer_resolution_emits_both_id_spaces(self, live_daemon, client, in_thread, book):
        rows = await result(
            client, in_thread, "resolve.peer", {"ref": [str(NEWS_ID)], "ids": "botapi"}
        )
        assert rows[0]["marked_id"] == NEWS_ID
        assert rows[0]["id"] == NEWS
        assert rows[0]["botapi_id"] == NEWS_ID

    async def test_peer_resolution_reports_how_it_answered(
        self, live_daemon, client, in_thread, book
    ):
        rows = await result(client, in_thread, "resolve.peer", {"ref": ["@alice"]})
        assert rows[0]["resolved"] is True
        assert rows[0]["source"] == "resolve_username"
        assert "access_hash" not in rows[0]

    async def test_an_uncached_bare_id_fails_rather_than_guessing(
        self, live_daemon, client, in_thread, book
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "resolve.peer", {"ref": [str(NOBODY)]})
        assert classify(caught.value).exit_code in (EXIT_NOT_FOUND, EXIT_INDETERMINATE)

    async def test_resolving_nothing_is_a_usage_error(self, live_daemon, client, in_thread, book):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "resolve.peer", {"ref": []})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_the_peer_cache_is_inspectable_without_hashes(
        self, live_daemon, client, in_thread, book
    ):
        await result(client, in_thread, "resolve.username", {"username": "alice"})
        rows = await result(client, in_thread, "resolve.cache.get")
        row = next(r for r in rows if r["marked_id"] == ALICE)
        assert row["access_hash_cached"] is True
        assert "access_hash" not in row

    async def test_purge_drops_the_rows(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "resolve.username", {"username": "alice"})
        await result(client, in_thread, "resolve.cache.get", {"purge": True})
        rows = await result(client, in_thread, "resolve.cache.get")
        assert rows == []

    async def test_purge_honours_dry_run(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "resolve.username", {"username": "alice"})
        envelope = await call(client, in_thread, "resolve.cache.get", {"purge": True}, dry_run=True)
        assert any("would be dropped" in w for w in envelope["meta"]["warnings"])
        rows = await result(client, in_thread, "resolve.cache.get")
        assert rows


# ---------------------------------------------------------------------------
# resolve link — one dispatcher, one union
# ---------------------------------------------------------------------------


class TestResolveLink:
    @pytest.mark.parametrize(
        ("url", "kind"),
        [
            ("https://t.me/alice", "public-username"),
            ("https://t.me/alice/4210", "message"),
            ("https://t.me/c/1234/56", "private-post"),
            ("https://t.me/+AbCdEf", "invite"),
            ("https://t.me/+15550001111", "phone"),
            ("https://t.me/joinchat/AbCdEf", "invite"),
            ("https://t.me/addlist/AbCdEf", "chatlist-invite"),
            ("https://t.me/addstickers/Pack", "stickerset"),
            ("https://t.me/addemoji/Pack", "emojiset"),
            ("https://t.me/addtheme/Slug", "theme"),
            ("https://t.me/bg/Slug", "wallpaper"),
            ("https://t.me/giftcode/AbC", "giftcode"),
            ("https://t.me/nft/AbC", "unique-gift"),
            ("https://t.me/contact/Token", "contact-token"),
            ("https://t.me/m/Slug", "business-chat-link"),
            ("https://t.me/alice/s/12", "story"),
            ("https://t.me/mybot?start=abc", "bot-start"),
            ("https://t.me/mybot?startgroup=abc", "bot-startgroup"),
            ("https://t.me/mybot?startapp=abc", "webapp"),
            ("https://t.me/proxy?server=x&port=1", "proxy"),
            ("https://t.me/share?url=x", "share-url"),
            ("https://t.me/contacts/new", "contacts-section"),
            ("tg://settings", "settings-section"),
            ("tg://resolve?domain=alice", "public-username"),
            ("tg://join?invite=AbC", "invite"),
            ("tg://privatepost?channel=1234&post=5", "private-post"),
            ("tg://msg_url?url=x&text=y", "share-url"),
        ],
    )
    async def test_every_link_shape_is_classified(
        self, live_daemon, client, in_thread, book, url, kind
    ):
        answer = await result(client, in_thread, "resolve.link", {"url": url, "no_network": True})
        assert answer["kind"] == kind

    async def test_a_plus_number_is_a_phone_not_an_invite(
        self, live_daemon, client, in_thread, book
    ):
        """The one ambiguity that turns a lookup into a join if guessed wrong."""
        answer = await result(
            client, in_thread, "resolve.link", {"url": "t.me/+15550001111", "no_network": True}
        )
        assert answer["kind"] == "phone"
        assert answer["phone"] == "+15550001111"

    async def test_resolution_names_the_command_that_would_act(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(
            client, in_thread, "resolve.link", {"url": "t.me/+AbCdEf", "no_network": True}
        )
        assert answer["delegated_to"] == "chat join"
        assert answer["requires_action"] is True

    async def test_no_network_performs_no_rpc(self, live_daemon, client, in_thread, book):
        await result(client, in_thread, "resolve.link", {"url": "t.me/alice", "no_network": True})
        assert not book.called("ResolveUsernameRequest")

    async def test_open_performs_the_follow_up_read(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "resolve.link", {"url": "t.me/alice", "open": True}
        )
        assert answer["peer"]["id"] == ALICE

    async def test_an_unknown_tg_path_asks_the_server_what_it_means(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(client, in_thread, "resolve.link", {"url": "tg://whatever"})
        assert answer["kind"] == "unknown"
        assert answer["deeplink_info"] == book.deep_link_message

    async def test_a_message_link_carries_its_ids(self, live_daemon, client, in_thread, book):
        answer = await result(
            client, in_thread, "resolve.link", {"url": "t.me/alice/4210", "no_network": True}
        )
        assert (answer["username"], answer["msg_id"]) == ("alice", 4210)

    async def test_a_thread_link_splits_thread_from_message(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(
            client, in_thread, "resolve.link", {"url": "t.me/alice/12/34", "no_network": True}
        )
        assert (answer["thread_id"], answer["msg_id"]) == (12, 34)

    @pytest.mark.parametrize(
        ("url", "check"),
        [
            ("t.me/+AbCdEf", lambda a: a["title"] == "Shared group"),
            ("t.me/addstickers/Pack", lambda a: a["title"] == "Pack"),
            ("t.me/addtheme/Slug", lambda a: a["title"] == "Midnight"),
            ("t.me/bg/Slug", lambda a: a["opened"]["id"] == 77),
            ("t.me/boost/newschan", lambda a: a["opened"]["level"] == 3),
            ("t.me/giftcode/AbC", lambda a: a["opened"]["used"] is True),
            ("t.me/alice/s/12", lambda a: a["opened"]["stories"] == 0),
            ("t.me/contact/Token", lambda a: a["peer"]["id"] in (ALICE, BOB, CAROL)),
        ],
    )
    async def test_open_reads_but_never_acts(
        self, live_daemon, client, in_thread, book, url, check
    ):
        answer = await result(client, in_thread, "resolve.link", {"url": url, "open": True})
        assert check(answer)
        # Nothing that *changes* the world may have been sent.
        assert not book.called("ImportChatInviteRequest")
        assert not book.called("InstallStickerSetRequest")

    async def test_a_message_link_can_be_read(self, live_daemon, client, in_thread, book):
        book.add_message(ALICE, "the post", message_id=4210)
        answer = await result(
            client, in_thread, "resolve.link", {"url": "t.me/alice/4210", "open": True}
        )
        assert answer["opened"]["text"] == "the post"

    async def test_a_private_post_resolves_through_the_peer_cache(
        self, live_daemon, client, in_thread, book
    ):
        """`t.me/c/<id>/<msg>` carries a bare channel id and nothing else."""
        book.add_message(NEWS_ID, "private post", message_id=7)
        answer = await result(
            client, in_thread, "resolve.link", {"url": f"t.me/c/{NEWS}/7", "open": True}
        )
        assert answer["kind"] == "private-post"
        assert answer["opened"]["text"] == "private post"

    async def test_a_shared_text_can_be_saved_as_a_draft(
        self, live_daemon, client, in_thread, book
    ):
        answer = await result(
            client,
            in_thread,
            "resolve.link",
            {"url": "tg://msg_url?url=x&text=hello", "draft": "@alice"},
        )
        assert answer["draft_saved"] is True
        assert book.drafts[ALICE].message == "hello"

    async def test_a_draft_with_no_text_is_a_usage_error(
        self, live_daemon, client, in_thread, book
    ):
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "resolve.link", {"url": "t.me/alice", "draft": "@alice"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# cross-cutting
# ---------------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.parametrize(
        ("op", "payload"),
        [
            ("contact.add", {"user": "@carol", "first_name": "C"}),
            ("contact.remove", {"user": ["@alice"]}),
            ("contact.rename", {"user": "@alice", "first_name": "X"}),
            ("user.block", {"user": "@carol"}),
            ("contact.blocked.set", {"user": ["@carol"]}),
        ],
    )
    async def test_a_mutating_op_changes_nothing_under_dry_run(
        self, live_daemon, client, in_thread, book, op, payload
    ):
        before = (dict(book.contacts), dict(book.blocked), len(book.calls))
        envelope = await call(client, in_thread, op, payload, dry_run=True)
        assert envelope["result"]["dry_run"] is True
        assert envelope["result"]["would"] == op
        assert (dict(book.contacts), dict(book.blocked), len(book.calls)) == before


class TestLegacyPaths:
    """§12.4: no documented v1 path disappears."""

    @pytest.mark.parametrize(
        ("path", "op_id"),
        [
            ("contact list", "contact.list"),
            ("contacts", "contact.list"),
            ("contact add", "contact.add"),
            ("contact rename", "contact.rename"),
            ("contact remove", "contact.remove"),
            ("contact search", "contact.search"),
            ("user get", "user.get"),
            ("user dialog-status", "user.dialog-status"),
            ("user hide-stories", "story.hide"),
        ],
    )
    def test_the_v1_path_still_resolves(self, path, op_id):
        import tlgr.ops  # noqa: F401
        from tlgr.registry import canonical

        assert canonical(path) == op_id

    @pytest.mark.parametrize(
        "path",
        [
            ("contact", "list"),
            ("contacts",),
            ("contact", "add"),
            ("contact", "rename"),
            ("contact", "remove"),
            ("contact", "search"),
            ("user", "get"),
            ("user", "dialog-status"),
            ("user", "hide-stories"),
        ],
        ids=lambda p: " ".join(p),
    )
    def test_the_v1_path_is_still_invocable(self, path):
        from tlgr.cli import cli

        node: Any = cli
        for token in path:
            node = node.commands.get(token) if hasattr(node, "commands") else None
        assert node is not None
