"""The `story` operations, end to end through a real daemon.

Same arrangement as the other group suites: a real Unix socket, the real
middleware chain, the real dispatcher, a fake Telegram. Three properties are
worth more here than anywhere else and most of the file is about them.

* **Reading is not being seen.** `story read` must send `readStories` and
  nothing else; only `--register-view` may reach `incrementStoryViews`. That
  is a privacy boundary, and it is only visible by inspecting the requests.
* **The audience vector is ordered.** `[base, allow…, disallow…]` is what the
  server evaluates, so "contacts, except Bob" is asserted on the request tlgr
  built, not on the reply.
* **A placeholder is not a story.** A feed hands back `storyItemSkipped`, and
  a caller has to be able to tell that from a caption-less story.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
)

ALICE = 4242
BOB = 9001
CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL
ME = 777


@pytest.fixture
def stories(world):
    """Alice has three stories; two of them are on her profile page."""
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_user(make_user(BOB, username="bobby"))
    world.add_channel(make_channel(CHANNEL, title="News"))
    world.add_user(make_user(7777, username="foursquare", first="Venues"))

    world.add_story(ALICE, story_id=41, caption="yesterday", pinned=True, out=False)
    world.add_story(ALICE, story_id=42, caption="morning", out=False)
    world.add_story(ALICE, story_id=43, caption="evening", pinned=True, out=False)
    world.add_story(ME, story_id=7, caption="mine", archived=True)
    world.add_story_viewer(ALICE, 42, BOB, reaction="🔥")
    world.add_story_viewer(ALICE, 42, ME)
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


async def paged(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    """A paginated op's items and pagination, as one dict.

    The daemon puts a page's items in `result` and its cursor in `page`; every
    assertion below wants both, and unpacking them at each call site is how a
    cursor assertion ends up testing the wrong half.
    """
    envelope = await call(client, in_thread, op, request, **kwargs)
    return {"items": envelope["result"], **envelope["page"]}


async def fails(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    from tlgr.core.errors import TlgrError

    try:
        await call(client, in_thread, op, request, **kwargs)
    except TlgrError as exc:
        return exc
    raise AssertionError(f"{op} was expected to fail")


def photo(tmp_path, name: str = "story.jpg") -> str:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8" + b"x" * 128)
    return str(path)


# ---------------------------------------------------------------------------
# story list / get
# ---------------------------------------------------------------------------


class TestList:
    async def test_the_active_stories_come_back_with_their_captions(
        self, live_daemon, client, in_thread, stories
    ):
        page = await paged(client, in_thread, "story.list", {"chat": "@alice"})
        assert [item["id"] for item in page["items"]] == [41, 42, 43]
        assert page["items"][1]["caption"] == "morning"

    async def test_the_profile_page_is_a_different_rpc(
        self, live_daemon, client, in_thread, stories
    ):
        page = await paged(client, in_thread, "story.list", {"chat": "@alice", "profile": True})
        assert [item["id"] for item in page["items"]] == [41, 43]
        assert stories.called("GetPinnedStoriesRequest")
        assert not stories.called("GetStoriesArchiveRequest")

    async def test_the_archive_is_its_own_rpc(self, live_daemon, client, in_thread, stories):
        page = await paged(client, in_thread, "story.list", {"chat": "me", "archive": True})
        assert [item["id"] for item in page["items"]] == [7]
        assert stories.called("GetStoriesArchiveRequest")

    async def test_an_album_pages_on_an_integer_offset(
        self, live_daemon, client, in_thread, stories
    ):
        stories.add_album(ALICE, 3, "Trips", [41, 42, 43])
        page = await paged(client, in_thread, "story.list", {"chat": "@alice", "album": 3}, limit=2)
        assert [item["id"] for item in page["items"]] == [41, 42]
        request = stories.called("GetAlbumStoriesRequest")[0]
        assert (request.offset, request.limit) == (0, 2)

    async def test_the_cursor_continues_the_album_walk(
        self, live_daemon, client, in_thread, stories
    ):
        stories.add_album(ALICE, 3, "Trips", [41, 42, 43])
        first = await paged(
            client, in_thread, "story.list", {"chat": "@alice", "album": 3}, limit=2
        )
        assert first["has_more"] is True
        second = await paged(
            client,
            in_thread,
            "story.list",
            {"chat": "@alice", "album": 3},
            limit=2,
            cursor=first["next_cursor"],
        )
        assert [item["id"] for item in second["items"]] == [43]

    async def test_a_skipped_placeholder_is_hydrated(self, live_daemon, client, in_thread, stories):
        """A feed placeholder has no caption; hydrating it fetches the real item."""
        from telethon.tl import types

        stories.peer_stories[ALICE][42] = types.StoryItemSkipped(id=42, date=None, expire_date=None)
        stories.raw["GetPeerStoriesRequest"] = lambda request: types.stories.PeerStories(
            stories=types.PeerStories(
                peer=types.PeerUser(user_id=ALICE),
                stories=[types.StoryItemSkipped(id=99, date=None, expire_date=None)],
                max_read_id=0,
            ),
            chats=[],
            users=[],
        )
        stories.peer_stories[ALICE][99] = stories.add_story(ALICE, story_id=99, caption="real")
        page = await paged(client, in_thread, "story.list", {"chat": "@alice"})
        assert page["items"][0]["caption"] == "real"
        assert stories.called("GetStoriesByIDRequest")

    async def test_no_hydrate_reports_the_placeholder_as_a_placeholder(
        self, live_daemon, client, in_thread, stories
    ):
        from telethon.tl import types

        stories.raw["GetPeerStoriesRequest"] = lambda request: types.stories.PeerStories(
            stories=types.PeerStories(
                peer=types.PeerUser(user_id=ALICE),
                stories=[types.StoryItemSkipped(id=99, date=None, expire_date=None)],
                max_read_id=0,
            ),
            chats=[],
            users=[],
        )
        page = await paged(client, in_thread, "story.list", {"chat": "@alice", "hydrate": False})
        assert page["items"][0]["skipped"] is True
        assert "caption" not in page["items"][0]

    async def test_listing_never_registers_a_view(self, live_daemon, client, in_thread, stories):
        await paged(client, in_thread, "story.list", {"chat": "@alice"})
        assert not stories.called("IncrementStoryViewsRequest")
        assert not stories.called("ReadStoriesRequest")


class TestGet:
    async def test_a_story_comes_back_whole(self, live_daemon, client, in_thread, stories):
        page = await result(client, in_thread, "story.get", {"chat": "@alice", "id": ["42"]})
        assert page["items"][0]["caption"] == "morning"
        assert page["items"][0]["peer_id"] == ALICE

    async def test_a_story_link_replaces_the_chat_and_id_pair(
        self, live_daemon, client, in_thread, stories
    ):
        page = await result(client, in_thread, "story.get", {"chat": "t.me/alice/s/42"})
        assert page["items"][0]["id"] == 42

    async def test_link_only_exports_the_deep_link(self, live_daemon, client, in_thread, stories):
        page = await result(
            client, in_thread, "story.get", {"chat": "@alice", "id": ["42"], "link": True}
        )
        assert page["items"][0]["link"] == "https://t.me/alice/s/42"
        assert not stories.called("GetStoriesByIDRequest")

    async def test_the_album_link_is_built_from_the_username(
        self, live_daemon, client, in_thread, stories
    ):
        page = await result(client, in_thread, "story.get", {"chat": "@alice", "album_link": 3})
        assert page["items"][0]["link"] == "https://t.me/alice/a/3"

    async def test_views_are_a_second_call(self, live_daemon, client, in_thread, stories):
        page = await result(
            client, in_thread, "story.get", {"chat": "@alice", "id": ["42"], "views": True}
        )
        assert page["items"][0]["views"]["views_count"] == 2
        assert stories.called("GetStoriesViewsRequest")

    async def test_areas_out_writes_json_that_areas_reads_back(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        from telethon.tl import types

        stories.peer_stories[ALICE][42].media_areas = [
            types.MediaAreaUrl(
                coordinates=types.MediaAreaCoordinates(
                    x=50.0, y=20.0, w=30.0, h=10.0, rotation=0.0
                ),
                url="https://example.com",
            )
        ]
        target = tmp_path / "areas.json"
        await result(
            client,
            in_thread,
            "story.get",
            {"chat": "@alice", "id": ["42"], "areas_out": str(target)},
        )
        written = json.loads(target.read_text())
        assert written[0]["type"] == "url"
        assert written[0]["url"] == "https://example.com"

    async def test_a_missing_story_is_not_found(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.get", {"chat": "@alice", "id": ["999"]})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_no_id_and_no_link_is_a_usage_error(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(client, in_thread, "story.get", {"chat": "@alice"})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# story post / edit / delete
# ---------------------------------------------------------------------------


class TestPost:
    async def test_a_story_is_posted_and_findable(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        page = await result(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)], "caption": "hello"},
        )
        posted = page["items"][0]
        assert posted["id"] > 0
        assert stories.peer_stories[ME][posted["id"]].caption == "hello"

    async def test_two_files_post_two_stories_and_re_check_between_them(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        page = await result(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path, "a.jpg"), photo(tmp_path, "b.jpg")]},
        )
        assert len(page["items"]) == 2
        assert len(stories.called("CanSendStoryRequest")) == 2

    async def test_the_privacy_vector_is_base_then_allow_then_disallow(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(
            client,
            in_thread,
            "story.post",
            {
                "file": [photo(tmp_path)],
                "privacy": "contacts",
                "allow": ["@alice"],
                "exclude": ["@bobby"],
            },
        )
        request = stories.called("SendStoryRequest")[0]
        assert [type(rule).__name__ for rule in request.privacy_rules] == [
            "InputPrivacyValueAllowContacts",
            "InputPrivacyValueAllowUsers",
            "InputPrivacyValueDisallowUsers",
        ]

    async def test_close_friends_is_its_own_base_rule(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)], "privacy": "close-friends"},
        )
        request = stories.called("SendStoryRequest")[0]
        assert type(request.privacy_rules[0]).__name__ == "InputPrivacyValueAllowCloseFriends"

    async def test_selected_with_no_allow_list_is_refused(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        error = await fails(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)], "privacy": "selected"},
        )
        assert error.exit_code == EXIT_USAGE
        assert not stories.called("SendStoryRequest")

    async def test_the_period_becomes_seconds(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(client, in_thread, "story.post", {"file": [photo(tmp_path)], "period": "48h"})
        assert stories.called("SendStoryRequest")[0].period == 172800

    async def test_a_url_area_is_built_from_the_flag(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(
            client,
            in_thread,
            "story.post",
            {
                "file": [photo(tmp_path)],
                "area_url": ["https://example.com@50,20,30,10,15"],
            },
        )
        area = stories.called("SendStoryRequest")[0].media_areas[0]
        assert type(area).__name__ == "MediaAreaUrl"
        assert area.url == "https://example.com"
        assert (area.coordinates.x, area.coordinates.rotation) == (50.0, 15.0)

    async def test_a_malformed_area_is_a_usage_error(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        error = await fails(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)], "area_url": ["https://example.com"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_venue_area_runs_the_inline_query(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        from telethon.tl import types

        stories.venues = [
            types.BotInlineResult(
                id="v1",
                type="venue",
                send_message=types.BotInlineMessageMediaVenue(
                    geo=types.GeoPoint(long=13.4, lat=52.5, access_hash=0),
                    title="Gate",
                    address="Platz",
                    provider="foursquare",
                    venue_id="4ac",
                    venue_type="landmark",
                ),
            )
        ]
        await result(
            client,
            in_thread,
            "story.post",
            {
                "file": [photo(tmp_path)],
                "area_venue": ["gate@50,50,40,10"],
                "area_venue_near": "52.5,13.4",
            },
        )
        area = stories.called("SendStoryRequest")[0].media_areas[0]
        assert type(area).__name__ == "MediaAreaVenue"
        assert area.venue_id == "4ac"

    async def test_a_repost_carries_the_origin(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)], "repost": "@alice:42", "modified": True},
        )
        request = stories.called("SendStoryRequest")[0]
        assert request.fwd_from_story == 42
        assert request.fwd_modified is True

    async def test_a_mention_excluded_by_the_rules_is_warned_about(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        envelope = await call(
            client,
            in_thread,
            "story.post",
            {
                "file": [photo(tmp_path)],
                "caption": "hi @bobby",
                "privacy": "contacts",
                "exclude": ["@bobby"],
            },
        )
        assert any("excluded" in warning for warning in envelope["meta"]["warnings"])

    async def test_no_check_skips_the_preflight(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(client, in_thread, "story.post", {"file": [photo(tmp_path)], "no_check": True})
        assert not stories.called("CanSendStoryRequest")

    async def test_a_premium_gate_is_a_permission_error(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        from telethon.errors import PremiumAccountRequiredError

        stories.fail_next("CanSendStoryRequest", PremiumAccountRequiredError(request=None))
        error = await fails(client, in_thread, "story.post", {"file": [photo(tmp_path)]})
        assert error.exit_code == EXIT_PERMISSION
        assert not stories.called("SendStoryRequest")

    async def test_dry_run_posts_nothing(self, live_daemon, client, in_thread, stories, tmp_path):
        envelope = await call(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)]},
            dry_run=True,
        )
        assert envelope["result"]["dry_run"] is True
        assert not stories.called("SendStoryRequest")

    async def test_no_file_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.post", {"file": []})
        assert error.exit_code == EXIT_USAGE


class TestEdit:
    async def test_a_caption_edit_lands_on_the_stored_story(
        self, live_daemon, client, in_thread, stories
    ):
        await result(
            client, in_thread, "story.edit", {"chat": "me", "id": 7, "caption": "reworded"}
        )
        assert stories.peer_stories[ME][7].caption == "reworded"

    async def test_only_what_was_passed_is_sent(self, live_daemon, client, in_thread, stories):
        await result(
            client, in_thread, "story.edit", {"chat": "me", "id": 7, "caption": "reworded"}
        )
        request = stories.called("EditStoryRequest")[0]
        assert request.media is None
        assert request.privacy_rules is None

    async def test_an_empty_edit_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.edit", {"chat": "me", "id": 7})
        assert error.exit_code == EXIT_USAGE

    async def test_a_cover_change_on_a_photo_story_is_refused(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(
            client, in_thread, "story.edit", {"chat": "me", "id": 7, "cover_ts": 1.5}
        )
        assert error.exit_code == EXIT_USAGE


class TestDelete:
    async def test_the_story_really_goes(self, live_daemon, client, in_thread, stories):
        deleted = await result(client, in_thread, "story.delete", {"chat": "me", "id": ["7"]})
        assert deleted["deleted_ids"] == [7]
        assert 7 not in stories.peer_stories[ME]

    async def test_a_range_expands(self, live_daemon, client, in_thread, stories):
        await result(client, in_thread, "story.delete", {"chat": "@alice", "id": ["41-43"]})
        assert stories.called("DeleteStoriesRequest")[0].id == [41, 42, 43]

    async def test_dry_run_deletes_nothing(self, live_daemon, client, in_thread, stories):
        envelope = await call(
            client, in_thread, "story.delete", {"chat": "me", "id": ["7"]}, dry_run=True
        )
        assert envelope["result"]["dry_run"] is True
        assert 7 in stories.peer_stories[ME]


# ---------------------------------------------------------------------------
# story read / react / reply / share
# ---------------------------------------------------------------------------


class TestRead:
    async def test_reading_clears_the_ring_without_registering_a_view(
        self, live_daemon, client, in_thread, stories
    ):
        read = await result(client, in_thread, "story.read", {"chat": "@alice"})
        assert read["max_id"] == 43
        assert stories.called("ReadStoriesRequest")
        assert not stories.called("IncrementStoryViewsRequest")

    async def test_register_view_is_the_opt_in(self, live_daemon, client, in_thread, stories):
        read = await result(
            client,
            in_thread,
            "story.read",
            {"chat": "@alice", "id": ["42"], "register_view": True},
        )
        assert read["viewed_ids"] == [42]
        assert stories.called("IncrementStoryViewsRequest")[0].id == [42]

    async def test_reading_twice_is_already(self, live_daemon, client, in_thread, stories):
        await result(client, in_thread, "story.read", {"chat": "@alice"})
        envelope = await call(client, in_thread, "story.read", {"chat": "@alice"})
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True

    async def test_a_peer_with_no_stories_is_not_found(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(client, in_thread, "story.read", {"chat": "@bobby"})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_the_view_alias_is_the_same_operation(self, live_daemon, client, in_thread):
        from tlgr.registry import canonical

        assert canonical("story view") == "story.read"


class TestReact:
    async def test_a_reaction_lands_on_the_story(self, live_daemon, client, in_thread, stories):
        reacted = await result(
            client, in_thread, "story.react", {"chat": "@alice", "id": 42, "emoji": "🔥"}
        )
        assert reacted["reaction"] == "🔥"
        request = stories.called("SendReactionRequest")[0]
        assert request.story_id == 42
        assert request.reaction.emoticon == "🔥"

    async def test_remove_sends_reaction_empty(self, live_daemon, client, in_thread, stories):
        removed = await result(
            client, in_thread, "story.react", {"chat": "@alice", "id": 42, "remove": True}
        )
        assert removed["removed"] is True
        assert type(stories.called("SendReactionRequest")[0].reaction).__name__ == "ReactionEmpty"

    async def test_a_custom_emoji_is_spelled_the_reaction_way(
        self, live_daemon, client, in_thread, stories
    ):
        reacted = await result(
            client,
            in_thread,
            "story.react",
            {"chat": "@alice", "id": 42, "custom_emoji": 555},
        )
        assert reacted["reaction"] == "custom:555"

    async def test_as_message_sends_an_ordinary_story_reply(
        self, live_daemon, client, in_thread, stories
    ):
        reacted = await result(
            client,
            in_thread,
            "story.react",
            {"chat": "@alice", "id": 42, "emoji": "🔥", "as_message": True},
        )
        assert reacted["msg_id"] > 0
        request = stories.called("SendMessageRequest")[0]
        assert type(request.reply_to).__name__ == "InputReplyToStory"

    async def test_nothing_to_react_with_is_a_usage_error(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(client, in_thread, "story.react", {"chat": "@alice", "id": 42})
        assert error.exit_code == EXIT_USAGE


class TestReply:
    async def test_a_text_reply_carries_input_reply_to_story(
        self, live_daemon, client, in_thread, stories
    ):
        reply = await result(
            client,
            in_thread,
            "story.reply",
            {"chat": "@alice", "id": 42, "text": "nice one"},
        )
        assert reply["reply_to_story"] == 42
        request = stories.called("SendMessageRequest")[0]
        assert (type(request.reply_to).__name__, request.reply_to.story_id) == (
            "InputReplyToStory",
            42,
        )

    async def test_a_file_reply_goes_through_send_media(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        await result(
            client,
            in_thread,
            "story.reply",
            {"chat": "@alice", "id": 42, "file": [photo(tmp_path)]},
        )
        assert type(stories.called("SendMediaRequest")[0].reply_to).__name__ == (
            "InputReplyToStory"
        )

    async def test_an_empty_reply_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.reply", {"chat": "@alice", "id": 42})
        assert error.exit_code == EXIT_USAGE


class TestShare:
    async def test_a_share_sends_a_story_card(self, live_daemon, client, in_thread, stories):
        shared = await result(
            client,
            in_thread,
            "story.share",
            {"chat": "@alice", "id": 42, "until": ["@bobby"]},
        )
        assert shared["story_id"] == 42
        request = stories.called("SendMediaRequest")[0]
        assert type(request.media).__name__ == "InputMediaStory"
        assert not stories.called("ForwardMessagesRequest")

    async def test_a_protected_story_is_refused_with_a_way_out(
        self, live_daemon, client, in_thread, stories
    ):
        stories.peer_stories[ALICE][42].noforwards = True
        error = await fails(
            client,
            in_thread,
            "story.share",
            {"chat": "@alice", "id": 42, "until": ["@bobby"]},
        )
        assert error.exit_code == EXIT_PERMISSION
        assert "--link" in str(error)

    async def test_no_destination_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.share", {"chat": "@alice", "id": 42})
        assert error.exit_code == EXIT_USAGE

    async def test_the_forward_alias_resolves(self, live_daemon, client, in_thread):
        from tlgr.registry import canonical

        assert canonical("story forward") == "story.share"


# ---------------------------------------------------------------------------
# story pin / unpin / hide / unhide
# ---------------------------------------------------------------------------


class TestPin:
    async def test_pinning_puts_the_story_on_the_profile_page(
        self, live_daemon, client, in_thread, stories
    ):
        pinned = await result(client, in_thread, "story.pin", {"chat": "me", "id": ["7"]})
        assert pinned["pinned"] is True
        assert 7 in stories.story_pinned[ME]

    async def test_pinning_twice_is_already(self, live_daemon, client, in_thread, stories):
        await result(client, in_thread, "story.pin", {"chat": "me", "id": ["7"]})
        envelope = await call(client, in_thread, "story.pin", {"chat": "me", "id": ["7"]})
        assert envelope["meta"]["already"] is True

    async def test_top_replaces_the_whole_set(self, live_daemon, client, in_thread, stories):
        pinned = await result(
            client, in_thread, "story.pin", {"chat": "@alice", "id": ["43"], "top": True}
        )
        assert pinned["pinned_to_top"] == [43]
        assert stories.story_pinned_top[ALICE] == [43]

    async def test_top_with_no_ids_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.pin", {"chat": "@alice", "top": True})
        assert error.exit_code == EXIT_USAGE

    async def test_unpin_takes_it_off_the_page(self, live_daemon, client, in_thread, stories):
        await result(client, in_thread, "story.unpin", {"chat": "@alice", "id": ["41"]})
        assert 41 not in stories.story_pinned[ALICE]

    async def test_unpin_top_with_no_ids_clears_the_row(
        self, live_daemon, client, in_thread, stories
    ):
        stories.story_pinned_top[ALICE] = [41, 43]
        await result(client, in_thread, "story.unpin", {"chat": "@alice", "top": True})
        assert stories.story_pinned_top[ALICE] == []


class TestHide:
    async def test_hiding_a_peer_flips_the_flag(self, live_daemon, client, in_thread, stories):
        hidden = await result(client, in_thread, "story.hide", {"chat": "@alice"})
        assert hidden == {
            "user_id": ALICE,
            "username": "alice",
            "peer_id": ALICE,
            "hidden": True,
        }, "the v1 keys, and nothing invented beside them"
        assert stories.called("TogglePeerStoriesHiddenRequest")[0].hidden is True

    async def test_hiding_twice_sends_no_request(self, live_daemon, client, in_thread, stories):
        await result(client, in_thread, "story.hide", {"chat": "@alice"})
        envelope = await call(client, in_thread, "story.hide", {"chat": "@alice"})
        assert envelope["result"]["already"] is True
        assert len(stories.called("TogglePeerStoriesHiddenRequest")) == 1

    async def test_unhide_is_the_same_toggle_the_other_way(
        self, live_daemon, client, in_thread, stories
    ):
        await result(client, in_thread, "story.hide", {"chat": "@alice"})
        await result(client, in_thread, "story.unhide", {"chat": "@alice"})
        assert stories.called("TogglePeerStoriesHiddenRequest")[1].hidden is False

    async def test_the_v1_unhide_flag_still_works(self, live_daemon, client, in_thread, stories):
        """`user hide-stories --unhide` was v1's spelling of `story unhide`."""
        await result(client, in_thread, "story.hide", {"chat": "@alice"})
        unhidden = await result(client, in_thread, "story.hide", {"chat": "@alice", "unhide": True})
        assert unhidden.get("hidden", False) is False
        assert unhidden.get("already", False) is False

    async def test_all_collapses_the_whole_bar(self, live_daemon, client, in_thread, stories):
        hidden = await result(client, in_thread, "story.hide", {"every": True})
        assert hidden["all"] is True
        assert stories.all_stories_hidden is True

    async def test_no_peer_and_no_all_is_a_usage_error(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(client, in_thread, "story.hide", {})
        assert error.exit_code == EXIT_USAGE


class TestLegacyUserHideStories:
    """AGENT.md's `tlgr user hide-stories` keeps working (§12.4)."""

    def test_the_v1_path_resolves_to_the_story_operation(self):
        from tlgr.registry import canonical

        assert canonical("user hide-stories") == "story.hide"

    def test_the_v1_path_is_still_invocable(self):
        from tlgr.cli import cli

        command = cli.commands["user"].commands["hide-stories"]
        flags = {opt for param in command.params for opt in param.opts}
        assert "--unhide" in flags

    async def test_the_v1_keys_survive(self, live_daemon, client, in_thread, stories):
        hidden = await result(client, in_thread, "user.hide-stories", {"chat": "@alice"})
        assert set(hidden) >= {"user_id", "username", "hidden"}


# ---------------------------------------------------------------------------
# story feed
# ---------------------------------------------------------------------------


class TestFeed:
    async def test_the_bar_lists_peers_with_unread_counts(
        self, live_daemon, client, in_thread, stories
    ):
        page = await paged(client, in_thread, "story.feed.list", {})
        alice = next(row for row in page["items"] if row["peer_id"] == ALICE)
        assert alice["unread_count"] == 3
        assert alice["has_unread"] is True

    async def test_the_cursor_carries_the_state_and_the_next_flag(
        self, live_daemon, client, in_thread, stories
    ):
        stories.story_feed_has_more = True
        page = await paged(client, in_thread, "story.feed.list", {})
        assert page["has_more"] is True
        await paged(client, in_thread, "story.feed.list", {}, cursor=page["next_cursor"])
        second = stories.called("GetAllStoriesRequest")[1]
        assert (second.state, second.next) == ("feed-state-1", True)

    async def test_the_hidden_feed_is_a_separate_flag(
        self, live_daemon, client, in_thread, stories
    ):
        stories.stories_hidden_peers.add(ALICE)
        main = await paged(client, in_thread, "story.feed.list", {})
        assert [row["peer_id"] for row in main["items"]] == [ME]
        hidden = await paged(client, in_thread, "story.feed.list", {"hidden": True})
        assert [row["peer_id"] for row in hidden["items"]] == [ALICE]

    async def test_not_modified_reports_already(self, live_daemon, client, in_thread, stories):
        stories.story_feed_not_modified = "feed-state-1"
        envelope = await call(client, in_thread, "story.feed.list", {"refresh": True})
        assert envelope["result"] == []
        assert envelope["meta"]["already"] is True

    async def test_unread_only_drops_the_read_peers(self, live_daemon, client, in_thread, stories):
        stories.story_read[ALICE] = 43
        page = await paged(client, in_thread, "story.feed.list", {"unread_only": True})
        assert [row["peer_id"] for row in page["items"]] == [ME]

    async def test_peers_uses_the_compact_summary(self, live_daemon, client, in_thread, stories):
        page = await paged(client, in_thread, "story.feed.list", {"peers": ["@alice"]})
        assert page["items"][0]["max_id"] == 43
        assert stories.called("GetPeerMaxIDsRequest")
        assert not stories.called("GetAllStoriesRequest")

    async def test_read_state_is_the_login_bootstrap(self, live_daemon, client, in_thread, stories):
        stories.story_read[ALICE] = 41
        page = await paged(client, in_thread, "story.feed.list", {"read_state": True})
        assert page["items"][0]["max_read_id"] == 41
        assert stories.called("GetAllReadPeerStoriesRequest")


# ---------------------------------------------------------------------------
# story album
# ---------------------------------------------------------------------------


class TestAlbum:
    async def test_creating_an_album_stores_it(self, live_daemon, client, in_thread, stories):
        album = await result(
            client,
            in_thread,
            "story.album.create",
            {"chat": "me", "title": "Trips", "story": [7]},
        )
        assert album["title"] == "Trips"
        assert stories.story_albums[ME][album["id"]]["stories"] == [7]

    async def test_a_too_long_title_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(
            client,
            in_thread,
            "story.album.create",
            {"chat": "me", "title": "a much too long title", "story": [7]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_an_album_with_no_stories_is_a_usage_error(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(
            client, in_thread, "story.album.create", {"chat": "me", "title": "Trips"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_one_rpc_backs_all_four_edits(self, live_daemon, client, in_thread, stories):
        stories.add_album(ME, 1, "Trips", [7])
        await result(
            client,
            in_thread,
            "story.album.edit",
            {"chat": "me", "album_id": 1, "title": "Trips 2026", "add": [8], "remove": [7]},
        )
        entry = stories.story_albums[ME][1]
        assert entry["title"] == "Trips 2026"
        assert entry["stories"] == [8]
        assert len(stories.called("UpdateAlbumRequest")) == 1

    async def test_an_edit_that_changes_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, stories
    ):
        stories.add_album(ME, 1, "Trips", [7])
        error = await fails(client, in_thread, "story.album.edit", {"chat": "me", "album_id": 1})
        assert error.exit_code == EXIT_USAGE

    async def test_listing_albums(self, live_daemon, client, in_thread, stories):
        stories.add_album(ME, 1, "Trips", [7])
        stories.add_album(ME, 2, "Food", [7])
        page = await paged(client, in_thread, "story.album.list", {"chat": "me"})
        assert [album["title"] for album in page["items"]] == ["Trips", "Food"]

    async def test_a_matching_hash_reports_already(self, live_daemon, client, in_thread, stories):
        stories.add_album(ME, 1, "Trips", [7])
        envelope = await call(
            client, in_thread, "story.album.list", {"chat": "me", "hash": stories.story_albums_hash}
        )
        assert envelope["result"] == []
        assert envelope["meta"]["already"] is True

    async def test_deleting_an_album_keeps_the_stories(
        self, live_daemon, client, in_thread, stories
    ):
        stories.add_album(ME, 1, "Trips", [7])
        await result(client, in_thread, "story.album.delete", {"chat": "me", "album_id": 1})
        assert 1 not in stories.story_albums[ME]
        assert 7 in stories.peer_stories[ME]

    async def test_reordering_is_a_full_replace(self, live_daemon, client, in_thread, stories):
        stories.add_album(ME, 1, "Trips", [7])
        stories.add_album(ME, 2, "Food", [7])
        order = await result(
            client, in_thread, "story.album.reorder", {"chat": "me", "album_id": [2, 1]}
        )
        assert order["order"] == [2, 1]
        assert stories.story_album_order[ME] == [2, 1]

    async def test_reorder_with_no_ids_is_a_usage_error(
        self, live_daemon, client, in_thread, stories
    ):
        error = await fails(client, in_thread, "story.album.reorder", {"chat": "me"})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# story viewer / blocklist
# ---------------------------------------------------------------------------


class TestViewers:
    async def test_the_viewers_come_back_with_their_reactions(
        self, live_daemon, client, in_thread, stories
    ):
        page = await paged(client, in_thread, "story.viewer.list", {"chat": "@alice", "id": 42})
        assert [row["user_id"] for row in page["items"]] == [BOB, ME]
        assert page["items"][0]["reaction"] == "🔥"

    async def test_the_source_rpc_is_reported(self, live_daemon, client, in_thread, stories):
        envelope = await call(client, in_thread, "story.viewer.list", {"chat": "@alice", "id": 42})
        assert any("getStoryViewsList" in w for w in envelope["meta"]["warnings"])

    async def test_a_channel_story_uses_the_reactions_rpc(
        self, live_daemon, client, in_thread, stories
    ):
        stories.add_story(CHANNEL_ID, story_id=5, caption="channel")
        stories.add_story_viewer(CHANNEL_ID, 5, BOB, reaction="👍")
        envelope = await call(
            client, in_thread, "story.viewer.list", {"chat": str(CHANNEL_ID), "id": 5}
        )
        assert stories.called("GetStoryReactionsListRequest")
        assert not stories.called("GetStoryViewsListRequest")
        assert any("getStoryReactionsList" in w for w in envelope["meta"]["warnings"])

    async def test_the_search_reaches_the_server(self, live_daemon, client, in_thread, stories):
        await paged(
            client, in_thread, "story.viewer.list", {"chat": "@alice", "id": 42, "q": "bobby"}
        )
        assert stories.called("GetStoryViewsListRequest")[0].q == "bobby"

    async def test_the_cursor_carries_the_opaque_offset(
        self, live_daemon, client, in_thread, stories
    ):
        page = await paged(
            client, in_thread, "story.viewer.list", {"chat": "@alice", "id": 42}, limit=1
        )
        assert page["has_more"] is True
        await paged(
            client,
            in_thread,
            "story.viewer.list",
            {"chat": "@alice", "id": 42},
            limit=1,
            cursor=page["next_cursor"],
        )
        assert stories.called("GetStoryViewsListRequest")[1].offset == "page2"

    async def test_csv_is_the_export_the_gui_has_no_button_for(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        target = tmp_path / "viewers.csv"
        await paged(
            client,
            in_thread,
            "story.viewer.list",
            {"chat": "@alice", "id": 42, "csv_out": str(target)},
        )
        rows = target.read_text().splitlines()
        assert rows[0] == "id,username,name,date,reaction,blocked,kind"
        assert rows[1].startswith(f"{BOB},")

    async def test_hide_from_adds_the_viewer_to_the_blocklist(
        self, live_daemon, client, in_thread, stories
    ):
        await paged(
            client,
            in_thread,
            "story.viewer.list",
            {"chat": "@alice", "id": 42, "hide_from": ["@bobby"]},
        )
        assert stories.called("BlockRequest")[0].my_stories_from is True


class TestBlocklist:
    async def test_adding_uses_the_story_only_flag(self, live_daemon, client, in_thread, stories):
        changed = await result(client, in_thread, "story.blocklist.set", {"user": ["@bobby"]})
        assert changed["added"] == [BOB]
        assert stories.called("BlockRequest")[0].my_stories_from is True

    async def test_removing_is_the_inverse(self, live_daemon, client, in_thread, stories):
        stories.story_blocklist = [BOB]
        changed = await result(
            client, in_thread, "story.blocklist.set", {"user": ["@bobby"], "remove": True}
        )
        assert changed["removed"] == [BOB]
        assert stories.story_blocklist == []

    async def test_removing_somebody_absent_is_already(
        self, live_daemon, client, in_thread, stories
    ):
        envelope = await call(
            client, in_thread, "story.blocklist.set", {"user": ["@bobby"], "remove": True}
        )
        assert envelope["result"]["already"] is True

    async def test_replace_overwrites_in_one_rpc(self, live_daemon, client, in_thread, stories):
        stories.story_blocklist = [ALICE]
        changed = await result(
            client, in_thread, "story.blocklist.set", {"user": ["@bobby"], "replace": True}
        )
        assert changed["total"] == 1
        assert stories.story_blocklist == [BOB]
        assert not stories.called("BlockRequest")

    async def test_the_list_is_its_own_blocklist(self, live_daemon, client, in_thread, stories):
        stories.story_blocklist = [BOB]
        page = await paged(client, in_thread, "story.blocklist.list", {})
        assert page["items"][0]["user_id"] == BOB
        assert stories.called("GetBlockedRequest")[0].my_stories_from is True

    async def test_no_user_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.blocklist.set", {"user": []})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# story can-post / stealth / search / report / stats
# ---------------------------------------------------------------------------


class TestCanPost:
    async def test_the_preflight_reports_the_free_slots(
        self, live_daemon, client, in_thread, stories
    ):
        check = await result(client, in_thread, "story.can-post", {})
        assert check["can_post"] is True
        assert check["count_remains"] == 3

    async def test_the_limits_come_from_the_app_config(
        self, live_daemon, client, in_thread, stories
    ):
        stories.app_config = {
            "story_expiring_limit_default": 3,
            "story_expiring_limit_premium": 100,
            "stories_albums_limit": 12,
        }
        check = await result(client, in_thread, "story.can-post", {})
        assert check["limits"]["expiring_limit"] == 3
        assert check["limits"]["albums_limit"] == 12
        assert "expiring_limit" in check["limits"]["premium_unlocks"]

    async def test_a_refusal_is_named_rather_than_raw(
        self, live_daemon, client, in_thread, stories
    ):
        from telethon.errors import RPCError

        stories.fail_next(
            "CanSendStoryRequest",
            RPCError(request=None, message="STORY_SEND_FLOOD_WEEKLY_86400", code=420),
        )
        check = await result(client, in_thread, "story.can-post", {})
        assert check.get("can_post", False) is False
        assert check["reason"] == "STORY_SEND_FLOOD_WEEKLY"
        assert check["retry_after"] == 86400

    async def test_chats_lists_where_i_may_post(self, live_daemon, client, in_thread, stories):
        from fake_telethon import make_channel

        stories.chats_to_send = [make_channel(CHANNEL, title="News")]
        check = await result(client, in_thread, "story.can-post", {"chats": True})
        assert check["chats"][0]["title"] == "News"


class TestStealth:
    async def test_status_only_reads(self, live_daemon, client, in_thread, stories):
        mode = await result(client, in_thread, "story.stealth.set", {"status": True})
        assert mode.get("active", False) is False
        assert not stories.called("ActivateStealthModeRequest")

    async def test_activating_sets_both_windows(self, live_daemon, client, in_thread, stories):
        mode = await result(client, in_thread, "story.stealth.set", {"past": True, "future": True})
        assert (mode["past"], mode["future"]) == (True, True)
        assert mode["active"] is True
        request = stories.called("ActivateStealthModeRequest")[0]
        assert (request.past, request.future) == (True, True)

    async def test_a_flood_wait_is_reported_as_the_cooldown(
        self, live_daemon, client, in_thread, stories
    ):
        stories.flood("ActivateStealthModeRequest", 42)
        error = await fails(client, in_thread, "story.stealth.set", {"past": True})
        assert error.exit_code == EXIT_PERMISSION
        assert "42" in str(error)


class TestSearch:
    async def test_a_hashtag_search_returns_public_stories(
        self, live_daemon, client, in_thread, stories
    ):
        from fake_telethon import make_story
        from telethon.tl import types

        stories.public_stories = [
            types.FoundStory(
                peer=types.PeerUser(user_id=ALICE), story=make_story(60, caption="#berlin")
            )
        ]
        page = await paged(client, in_thread, "story.search", {"hashtag": "berlin"})
        assert page["items"][0]["caption"] == "#berlin"
        assert stories.called("SearchPostsRequest")[0].hashtag == "berlin"

    async def test_a_venue_search_builds_the_area(self, live_daemon, client, in_thread, stories):
        await paged(client, in_thread, "story.search", {"venue": "foursquare:4ac"})
        area = stories.called("SearchPostsRequest")[0].area
        assert (type(area).__name__, area.venue_id) == ("MediaAreaVenue", "4ac")

    async def test_a_geo_search_needs_an_address(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.search", {"geo": "52.5,13.4"})
        assert error.exit_code == EXIT_USAGE

    async def test_a_geo_search_with_an_address_works(
        self, live_daemon, client, in_thread, stories
    ):
        await paged(client, in_thread, "story.search", {"geo": "52.5,13.4", "address": "DE,Berlin"})
        area = stories.called("SearchPostsRequest")[0].area
        assert area.address.country_iso2 == "DE"

    async def test_two_criteria_are_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(
            client, in_thread, "story.search", {"hashtag": "berlin", "venue": "f:1"}
        )
        assert error.exit_code == EXIT_USAGE


class TestReport:
    async def test_the_first_step_returns_the_menu(self, live_daemon, client, in_thread, stories):
        report = await result(client, in_thread, "story.report", {"chat": "@alice", "id": ["42"]})
        assert report["result"] == "choose_option"
        assert report["options"][0]["text"] == "Spam"

    async def test_the_second_step_reports(self, live_daemon, client, in_thread, stories):
        report = await result(
            client,
            in_thread,
            "story.report",
            {"chat": "@alice", "id": ["42"], "option": "AQ=="},
        )
        assert report["reported"] is True

    async def test_no_id_is_a_usage_error(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.report", {"chat": "@alice", "id": []})
        assert error.exit_code == EXIT_USAGE


class TestStats:
    async def test_an_async_graph_is_resolved_before_it_is_reported(
        self, live_daemon, client, in_thread, stories
    ):
        stats = await result(client, in_thread, "story.stats.get", {"chat": "me", "id": 7})
        assert stats["views_graph"] == {"columns": []}
        assert stats["reactions_by_emotion_graph"] == {"columns": ["reactions"]}
        assert stories.called("LoadAsyncGraphRequest")

    async def test_forwards_lists_the_public_reposts(self, live_daemon, client, in_thread, stories):
        stats = await result(
            client, in_thread, "story.stats.get", {"chat": "me", "id": 7, "forwards": True}
        )
        assert stats["forwards"][0]["kind"] == "story"


# ---------------------------------------------------------------------------
# story live / export / watch
# ---------------------------------------------------------------------------


class TestLive:
    async def test_no_live_story_is_not_found(self, live_daemon, client, in_thread, stories):
        error = await fails(client, in_thread, "story.live.get", {"chat": "@alice"})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_a_live_story_is_reported_with_a_layer_warning(
        self, live_daemon, client, in_thread, stories
    ):
        from telethon.tl import types

        stories.peer_stories[ALICE][44] = types.StoryItemSkipped(
            id=44, date=None, expire_date=None, live=True
        )
        envelope = await call(client, in_thread, "story.live.get", {"chat": "@alice"})
        assert envelope["result"]["story_id"] == 44
        assert any("group call" in w for w in envelope["meta"]["warnings"])

    async def test_starting_without_rtmp_warns_about_the_silence(
        self, live_daemon, client, in_thread, stories
    ):
        envelope = await call(client, in_thread, "story.live.start", {})
        assert envelope["result"]["story_id"] > 0
        assert any("media engine" in w for w in envelope["meta"]["warnings"])

    async def test_rtmp_prints_the_ingest_url(self, live_daemon, client, in_thread, stories):
        live = await result(client, in_thread, "story.live.start", {"rtmp": True})
        assert live["rtmp_url"] == "rtmps://dc.tg/s/"
        assert live["rtmp_key"] == "secret-key"
        assert stories.called("GetGroupCallStreamRtmpUrlRequest")[0].live_story is True

    async def test_dry_run_starts_nothing(self, live_daemon, client, in_thread, stories):
        envelope = await call(client, in_thread, "story.live.start", {}, dry_run=True)
        assert envelope["result"]["dry_run"] is True
        assert not stories.called("StartLiveRequest")


class TestExport:
    async def test_the_archive_is_written_to_disk(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        export = await result(
            client,
            in_thread,
            "story.export",
            {"chat": "me", "out": str(tmp_path), "with_media": False, "jsonl": True},
        )
        assert export["count"] == 1
        written = (tmp_path / f"stories-{ME}.jsonl").read_text().strip().splitlines()
        assert json.loads(written[0])["id"] == 7

    async def test_max_stories_caps_the_walk(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        stories.add_story(ME, story_id=8, caption="two", archived=True)
        export = await result(
            client,
            in_thread,
            "story.export",
            {"chat": "me", "out": str(tmp_path), "with_media": False, "max_stories": 1},
        )
        assert export["count"] == 1


class TestWatch:
    async def test_a_raw_story_update_reaches_the_stream(self, live_daemon, world):
        """`story watch` reads the same bus `watch --events story` reads."""
        import asyncio

        from telethon.tl import types

        from tlgr.daemon.events import normalise_story

        event_type, payload, chat_id = normalise_story(
            types.UpdateStory(
                peer=types.PeerUser(user_id=ALICE), story=types.StoryItemDeleted(id=42)
            )
        )
        assert (event_type, payload["kind"], chat_id) == ("story_new", "story.new", ALICE)

        subscriber = live_daemon.bus.subscribe("work", types=(event_type,))
        try:
            live_daemon.bus.emit("work", event_type, payload, chat_id=chat_id)
            envelope = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
        finally:
            live_daemon.bus.unsubscribe(subscriber)
        assert envelope.payload["story_id"] == 42

    def test_every_story_update_class_is_named(self):
        from telethon.tl import types

        from tlgr.daemon.events import normalise_story

        kinds = {
            normalise_story(update)[1]["kind"]
            for update in (
                types.UpdateStory(peer=types.PeerUser(user_id=1), story=None),
                types.UpdateStoryID(id=1, random_id=2),
                types.UpdateReadStories(peer=types.PeerUser(user_id=1), max_id=3),
                types.UpdateNewStoryReaction(
                    story_id=1, peer=types.PeerUser(user_id=1), reaction=None
                ),
                types.UpdateSentStoryReaction(
                    peer=types.PeerUser(user_id=1), story_id=1, reaction=None
                ),
                types.UpdateStoriesStealthMode(stealth_mode=types.StoriesStealthMode()),
            )
        }
        assert kinds == {
            "story.new",
            "story.id-assigned",
            "story.read",
            "story.reaction-received",
            "story.reaction-sent",
            "story.stealth",
        }

    def test_an_ordinary_update_is_not_a_story_event(self):
        from telethon.tl import types

        from tlgr.daemon.events import normalise_story

        assert normalise_story(types.UpdateNewMessage(message=None, pts=1, pts_count=1)) is None


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


class TestGroupShape:
    def test_every_story_op_is_registered(self):
        from tlgr.registry import by_group

        assert len(by_group("story")) == 31

    def test_the_privacy_preset_comes_from_the_config(self):
        """A preset nobody defined is a usage error, not a silent 'everyone'."""
        import asyncio

        from tlgr.core.errors import UsageError
        from tlgr.ops._story import privacy_rules

        class _Ctx:
            account = "work"
            dry_run = False
            request_id = "t"
            config = None

            def warn(self, message: str) -> None: ...

            def emit(self, event_type: str, payload: dict, **kwargs: Any) -> None: ...

        with pytest.raises(UsageError):
            asyncio.run(privacy_rules(_Ctx(), base="everyone", preset="friends"))

    def test_an_unknown_media_area_type_is_refused(self):
        from tlgr.core.errors import UsageError
        from tlgr.ops._story import areas_from_json

        with pytest.raises(UsageError):
            areas_from_json('[{"type": "not-a-thing"}]')

    def test_a_missing_areas_file_is_a_usage_error(self):
        from tlgr.core.errors import UsageError
        from tlgr.ops._story import areas_from_json

        with pytest.raises(UsageError):
            areas_from_json("/nonexistent/areas.json")

    async def test_an_unknown_venue_bot_is_indeterminate(
        self, live_daemon, client, in_thread, stories, tmp_path
    ):
        stories.venue_search_username = ""
        error = await fails(
            client,
            in_thread,
            "story.post",
            {"file": [photo(tmp_path)], "area_venue": ["gate@50,50,40,10"]},
        )
        assert error.exit_code == EXIT_INDETERMINATE
