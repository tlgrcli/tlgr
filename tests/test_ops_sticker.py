"""The `sticker`, `gif` and `emoji` operations, end to end through a daemon.

The distinction these tests exist to hold is the one the API makes and the
GUI hides: a **set** is somebody's collection you install (`messages.*`), a
**pack** is one you created and may edit (`stickers.*`). Uninstalling a set
must not delete a pack, and a pack verb aimed at a set you do not own must
fail rather than half-work.

The other recurring assertion is reference freshness: every `InputDocument`
tlgr sends must carry the `file_reference` that came back from a live fetch,
because a cached one fails with `FILE_REFERENCE_EXPIRED` hours later — and
that is only visible by inspecting the request, never the reply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tlgr.core.errors import EXIT_NOT_FOUND, EXIT_USAGE, classify

ALICE = 4242
CAT_SET = "cats"
EMOJI_SET = "myemoji"
MINE = "my_cats_by_tlgr"


@pytest.fixture
def stickers(world):
    from fake_telethon import make_sticker_document, make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_sticker_set(
        CAT_SET,
        [
            make_sticker_document(1001, "\U0001f431", short_name=CAT_SET),
            make_sticker_document(1002, "\U0001f408", short_name=CAT_SET),
        ],
        set_id=111,
    )
    world.add_sticker_set(
        EMOJI_SET,
        [make_sticker_document(2001, "\U0001f600", custom_emoji=True, short_name=EMOJI_SET)],
        set_id=222,
        emojis=True,
    )
    world.add_sticker_set(
        MINE,
        [make_sticker_document(3001, "\U0001f63a", short_name=MINE)],
        set_id=333,
        creator=True,
    )
    world.add_sticker_set(
        "trending",
        [make_sticker_document(4001, "\U0001f525", short_name="trending")],
        set_id=444,
        installed=False,
        featured=True,
    )
    world.featured_unread = [444]
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


def _error(excinfo) -> Any:
    return classify(excinfo.value)


def _inline_gif(result_id: str, **fields: Any) -> Any:
    """One inline-bot result, with the `send_message` the TL type requires."""
    from telethon.tl import types

    return types.BotInlineResult(
        id=result_id,
        type="gif",
        send_message=types.BotInlineMessageMediaAuto(message=""),
        **fields,
    )


class TestSetGet:
    async def test_a_set_arrives_with_its_stickers_and_emoji_map(
        self, live_daemon, client, in_thread, stickers
    ):
        found = await result(client, in_thread, "sticker.set.get", {"set": CAT_SET})
        assert found["short_name"] == CAT_SET
        assert [s["emoji"] for s in found["stickers"]] == ["\U0001f431", "\U0001f408"]
        assert [s["index"] for s in found["stickers"]] == [0, 1]
        assert found["packs"]["\U0001f431"] == [1001]

    async def test_the_share_link_is_string_formatting(
        self, live_daemon, client, in_thread, stickers
    ):
        found = await result(client, in_thread, "sticker.set.get", {"set": CAT_SET})
        assert found["link"] == f"https://t.me/addstickers/{CAT_SET}"

    async def test_an_emoji_set_links_to_addemoji(self, live_daemon, client, in_thread, stickers):
        found = await result(client, in_thread, "sticker.set.get", {"set": EMOJI_SET})
        assert found["type"] == "emoji"
        assert found["link"] == f"https://t.me/addemoji/{EMOJI_SET}"

    async def test_a_t_me_link_is_accepted_where_a_short_name_is(
        self, live_daemon, client, in_thread, stickers
    ):
        found = await result(
            client,
            in_thread,
            "sticker.set.get",
            {"set": f"https://t.me/addstickers/{CAT_SET}"},
        )
        assert found["short_name"] == CAT_SET

    async def test_an_unknown_set_is_not_found(self, live_daemon, client, in_thread, stickers):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.set.get", {"set": "nosuchset"})
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_a_bare_numeric_id_says_why_it_cannot_work(
        self, live_daemon, client, in_thread, stickers
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.set.get", {"set": "111"})
        body = _error(excinfo)
        assert body.exit_code == EXIT_USAGE
        assert "access hash" in body.message

    async def test_id_and_hash_together_resolve(self, live_daemon, client, in_thread, stickers):
        found = await result(client, in_thread, "sticker.set.get", {"set": "111:555"})
        assert found["short_name"] == CAT_SET

    async def test_downloading_writes_every_sticker(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        found = await result(
            client,
            in_thread,
            "sticker.set.get",
            {"set": CAT_SET, "download": str(tmp_path)},
        )
        paths = [Path(s["path"]) for s in found["stickers"]]
        assert len(paths) == 2
        assert all(path.exists() for path in paths)

    async def test_convert_json_gunzips_a_tgs(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        """TGS is gzipped Lottie, so this conversion needs nothing installed."""
        import gzip

        for doc_id in (1001, 1002):
            payload = gzip.compress(b'{"v":"5.5"}')
            stickers.file_bytes[doc_id] = payload
            stickers.documents[doc_id].size = len(payload)
        found = await result(
            client,
            in_thread,
            "sticker.set.get",
            {"set": CAT_SET, "download": str(tmp_path), "convert": "json"},
        )
        first = Path(found["stickers"][0]["path"])
        assert first.suffix == ".json"
        assert first.read_bytes() == b'{"v":"5.5"}'

    async def test_a_set_is_needed(self, live_daemon, client, in_thread, stickers):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.set.get", {})
        assert _error(excinfo).exit_code == EXIT_USAGE


class TestSetShelves:
    async def test_installed_sets_are_listed_by_library(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "sticker.set.list", {"type": "sticker"})
        names = [item["short_name"] for item in page["result"]]
        assert CAT_SET in names
        assert EMOJI_SET not in names

    async def test_the_emoji_library_is_the_same_command(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "sticker.set.list", {"type": "emoji"})
        assert [item["short_name"] for item in page["result"]] == [EMOJI_SET]

    async def test_featured_reports_the_unread_badge(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "sticker.set.list", {"featured": True})
        assert page["result"][0]["unread"] is True

    async def test_mark_read_clears_the_badge_and_is_opt_in(
        self, live_daemon, client, in_thread, stickers
    ):
        await call(client, in_thread, "sticker.set.list", {"featured": True})
        assert stickers.featured_unread == [444], "listing alone must not mutate"
        await call(client, in_thread, "sticker.set.list", {"featured": True, "mark_read": True})
        assert stickers.featured_unread == []

    async def test_installing_moves_the_set_onto_the_shelf(
        self, live_daemon, client, in_thread, stickers
    ):
        changed = await result(client, in_thread, "sticker.set.add", {"set": ["trending"]})
        assert changed["installed"] == 1
        assert "trending" in stickers.installed_sets

    async def test_uninstalling_is_not_deleting(self, live_daemon, client, in_thread, stickers):
        await result(client, in_thread, "sticker.set.remove", {"set": [CAT_SET]})
        assert CAT_SET not in stickers.installed_sets
        assert CAT_SET in stickers.sticker_sets, "uninstalling deleted the set"

    async def test_archive_then_unarchive_round_trips(
        self, live_daemon, client, in_thread, stickers
    ):
        await result(client, in_thread, "sticker.set.archive", {"set": [CAT_SET]})
        assert CAT_SET in stickers.archived_sets
        archived = await call(client, in_thread, "sticker.set.list", {"archived": True})
        assert [i["short_name"] for i in archived["result"]] == [CAT_SET]
        await result(client, in_thread, "sticker.set.unarchive", {"set": [CAT_SET]})
        assert CAT_SET in stickers.installed_sets

    async def test_reorder_top_writes_the_whole_order(
        self, live_daemon, client, in_thread, stickers
    ):
        """A partial order silently drops the sets left out, so --top rebuilds it."""
        stickers.installed_sets = [CAT_SET, MINE]
        order = await result(client, in_thread, "sticker.set.reorder", {"top": MINE})
        assert order["order"][0] == 333
        assert len(order["order"]) == 2
        assert stickers.called("ReorderStickerSetsRequest")[0].order == order["order"]

    async def test_reorder_needs_something_to_write(self, live_daemon, client, in_thread, stickers):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.set.reorder", {})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_search_finds_a_set_by_name(self, live_daemon, client, in_thread, stickers):
        page = await call(client, in_thread, "sticker.set.search", {"query": "cat"})
        assert CAT_SET in [item["short_name"] for item in page["result"]]

    def test_the_emoji_aliases_reach_the_same_operations(self):
        from tlgr.registry import ALIASES

        assert ALIASES["emoji.set.get"] == "sticker.set.get"
        assert ALIASES["emoji.set.list"] == "sticker.set.list"
        assert ALIASES["emoji.set.remove"] == "sticker.set.remove"


class TestStickerSearch:
    async def test_an_emoji_query_uses_the_suggestion_strip(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "sticker.search", {"emoji": ["\U0001f431"]})
        assert [item["doc_id"] for item in page["result"]] == [1001]
        assert stickers.called("GetStickersRequest")

    async def test_a_text_query_uses_the_search_box(self, live_daemon, client, in_thread, stickers):
        page = await call(client, in_thread, "sticker.search", {"query": "cat"})
        assert page["result"]
        assert stickers.called("SearchStickersRequest")

    async def test_a_special_list_uses_a_reserved_emoticon(
        self, live_daemon, client, in_thread, stickers
    ):
        await call(client, in_thread, "sticker.search", {"special": "greeting"})
        request = stickers.called("GetStickersRequest")[0]
        assert request.emoticon == "\U0001f44b⭐️"


class TestFavesAndRecents:
    async def test_faving_resolves_the_document_from_the_set(
        self, live_daemon, client, in_thread, stickers
    ):
        faved = await result(client, in_thread, "sticker.fave.add", {"sticker": [f"{CAT_SET}/0"]})
        assert faved["doc_ids"] == [1001]
        assert stickers.faved == [1001]
        request = stickers.called("FaveStickerRequest")[0]
        assert request.id.file_reference == b"ref1001", "a stale reference was sent"

    async def test_an_emoji_selects_the_sticker(self, live_daemon, client, in_thread, stickers):
        await result(
            client,
            in_thread,
            "sticker.fave.add",
            {"sticker": [f"{CAT_SET}/\U0001f408"]},
        )
        assert stickers.faved == [1002]

    async def test_an_unknown_emoji_is_not_found(self, live_daemon, client, in_thread, stickers):
        with pytest.raises(Exception) as excinfo:
            await result(
                client, in_thread, "sticker.fave.add", {"sticker": [f"{CAT_SET}/\U0001f984"]}
            )
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_an_index_past_the_end_is_not_found(
        self, live_daemon, client, in_thread, stickers
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.fave.add", {"sticker": [f"{CAT_SET}/9"]})
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_a_bare_document_id_is_refused_with_a_reason(
        self, live_daemon, client, in_thread, stickers
    ):
        """A cached id has a dead file_reference; the set is where a live one is."""
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.fave.add", {"sticker": ["1001"]})
        body = _error(excinfo)
        assert body.exit_code == EXIT_USAGE
        assert "<set>/<index>" in body.message

    async def test_the_eviction_is_reported(self, live_daemon, client, in_thread, stickers):
        stickers.faved_limit = 1
        stickers.faved = [1002]
        faved = await result(client, in_thread, "sticker.fave.add", {"sticker": [f"{CAT_SET}/0"]})
        assert faved["evicted"] == [1002]

    async def test_unfaving_removes_it(self, live_daemon, client, in_thread, stickers):
        stickers.faved = [1001]
        await result(client, in_thread, "sticker.fave.remove", {"sticker": [f"{CAT_SET}/0"]})
        assert stickers.faved == []

    async def test_the_fave_list_reports_what_is_there(
        self, live_daemon, client, in_thread, stickers
    ):
        stickers.faved = [1001]
        page = await call(client, in_thread, "sticker.fave.list")
        assert [item["doc_id"] for item in page["result"]["items"]] == [1001]

    async def test_recents_can_be_forgotten_one_at_a_time(
        self, live_daemon, client, in_thread, stickers
    ):
        stickers.recent_stickers = [1001, 1002]
        await result(client, in_thread, "sticker.recent.remove", {"sticker": [f"{CAT_SET}/0"]})
        assert stickers.recent_stickers == [1002]

    async def test_the_whole_recent_list_can_be_cleared(
        self, live_daemon, client, in_thread, stickers
    ):
        stickers.recent_stickers = [1001, 1002]
        cleared = await result(client, in_thread, "sticker.recent.remove", {"every": True})
        assert cleared["cleared"] is True
        assert stickers.recent_stickers == []


class TestPacks:
    async def test_creating_a_pack_uploads_then_creates(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        source = tmp_path / "cat.webp"
        source.write_bytes(b"RIFF" + b"\x00" * 128)
        created = await result(
            client,
            in_thread,
            "sticker.pack.create",
            {
                "short_name": "fresh_pack",
                "title": "Fresh",
                "add": [f"{source}:\U0001f431"],
            },
        )
        assert created["short_name"] == "fresh_pack"
        assert created["link"] == "https://t.me/addstickers/fresh_pack"
        # uploadMedia before createStickerSet, in that order.
        names = [name for name, _ in stickers.calls]
        assert names.index("UploadMediaRequest") < names.index("CreateStickerSetRequest")

    async def test_a_sticker_without_an_emoji_is_refused(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        source = tmp_path / "cat.webp"
        source.write_bytes(b"RIFF")
        with pytest.raises(Exception) as excinfo:
            await result(
                client,
                in_thread,
                "sticker.pack.create",
                {"short_name": "no_emoji_pack", "add": [f"{source}:"]},
            )
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_a_taken_short_name_is_refused(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        source = tmp_path / "cat.webp"
        source.write_bytes(b"RIFF")
        with pytest.raises(Exception) as excinfo:
            await result(
                client,
                in_thread,
                "sticker.pack.create",
                {"short_name": CAT_SET, "add": [f"{source}:\U0001f431"]},
            )
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_dry_run_does_not_create(self, live_daemon, client, in_thread, stickers):
        checked = await call(
            client,
            in_thread,
            "sticker.pack.create",
            {"short_name": "brand_new_pack", "title": "New"},
            dry_run=True,
        )
        assert checked["result"]["dry_run"] is True
        assert not stickers.called("CreateStickerSetRequest")

    async def test_adding_a_sticker_appends_it(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        source = tmp_path / "cat2.webp"
        source.write_bytes(b"RIFF" + b"\x01" * 64)
        added = await result(
            client,
            in_thread,
            "sticker.pack.add",
            {"pack": MINE, "file": str(source), "emoji": "\U0001f431"},
        )
        assert added["count"] == 2
        assert len(stickers.sticker_sets[MINE]["documents"]) == 2

    async def test_adding_without_an_emoji_is_a_usage_error(
        self, live_daemon, client, in_thread, stickers, tmp_path
    ):
        source = tmp_path / "cat2.webp"
        source.write_bytes(b"RIFF")
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.pack.add", {"pack": MINE, "file": str(source)})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_removing_a_sticker_shortens_the_pack(
        self, live_daemon, client, in_thread, stickers
    ):
        removed = await result(
            client, in_thread, "sticker.pack.remove", {"pack": MINE, "sticker": ["0"]}
        )
        assert removed["removed"] == [3001]
        assert stickers.sticker_sets[MINE]["documents"] == []

    async def test_renaming_a_pack_changes_the_title(
        self, live_daemon, client, in_thread, stickers
    ):
        edited = await result(
            client, in_thread, "sticker.pack.edit", {"pack": MINE, "title": "Renamed"}
        )
        assert edited["changed"] == ["title"]
        assert stickers.sticker_sets[MINE]["set"].title == "Renamed"

    async def test_editing_nothing_is_a_usage_error(self, live_daemon, client, in_thread, stickers):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.pack.edit", {"pack": MINE})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_deleting_a_pack_removes_it_everywhere(
        self, live_daemon, client, in_thread, stickers
    ):
        deleted = await result(client, in_thread, "sticker.pack.delete", {"pack": MINE})
        assert deleted["deleted"] is True
        assert MINE not in stickers.sticker_sets

    async def test_a_pack_verb_on_a_set_you_do_not_own_fails(
        self, live_daemon, client, in_thread, stickers
    ):
        """`stickers.*` answers only to a creator; an unknown set is refused."""
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "sticker.pack.delete", {"pack": "nosuchset"})
        assert _error(excinfo).exit_code in (EXIT_NOT_FOUND, EXIT_USAGE)

    async def test_pack_list_shows_only_what_you_created(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "sticker.pack.list")
        assert [item["short_name"] for item in page["result"]] == [MINE]
        assert page["result"][0]["creator"] is True


class TestGifs:
    @pytest.fixture
    def gifs(self, stickers, world):
        from fake_telethon import make_document, make_user
        from telethon.tl import types

        # `config.gif_search_username` is the server's own three-letter handle.
        world.add_user(make_user(31337, username="gif", first="GIF"))

        animation = make_document(
            6001,
            mime="video/mp4",
            size=256,
            attributes=[
                types.DocumentAttributeAnimated(),
                types.DocumentAttributeVideo(duration=3, w=320, h=240),
            ],
        )
        world.add_media_message(ALICE, message_id=301, document=animation)
        return world

    async def test_saving_from_a_message_uses_a_fresh_reference(
        self, live_daemon, client, in_thread, gifs
    ):
        saved = await result(client, in_thread, "gif.add", {"chat": "@alice", "msg_id": [301]})
        assert saved["doc_ids"] == [6001]
        assert gifs.saved_gifs == [6001]
        assert gifs.called("SaveGifRequest")[0].id.file_reference == b"ref6001"

    async def test_the_list_numbers_what_send_takes(self, live_daemon, client, in_thread, gifs):
        gifs.saved_gifs = [6001]
        page = await call(client, in_thread, "gif.list")
        item = page["result"]["items"][0]
        assert item["index"] == 0
        assert item["duration"] == 3

    async def test_removing_by_index_uses_the_list_just_fetched(
        self, live_daemon, client, in_thread, gifs
    ):
        gifs.saved_gifs = [6001]
        removed = await result(client, in_thread, "gif.remove", {"gif": ["0"]})
        assert removed["removed"] == 1
        assert gifs.saved_gifs == []

    async def test_an_index_that_does_not_exist_is_not_found(
        self, live_daemon, client, in_thread, gifs
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "gif.remove", {"gif": ["7"]})
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_a_message_without_an_animation_is_not_found(
        self, live_daemon, client, in_thread, gifs
    ):
        gifs.add_message(ALICE, "text", message_id=302)
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "gif.add", {"chat": "@alice", "msg_id": [302]})
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_sending_a_saved_gif_is_an_ordinary_send(
        self, live_daemon, client, in_thread, gifs
    ):
        gifs.saved_gifs = [6001]
        sent = await result(client, in_thread, "gif.send", {"chat": "@alice", "gif": "0"})
        assert sent["msg_id"]
        assert sent["doc_id"] == 6001
        assert type(gifs.called("SendMediaRequest")[0].media).__name__ == "InputMediaDocument"

    async def test_sending_a_search_result_goes_through_the_bot(
        self, live_daemon, client, in_thread, gifs
    ):

        gifs.inline_results = [_inline_gif("r1", title="cat", url="https://x/y")]
        sent = await result(client, in_thread, "gif.send", {"chat": "@alice", "search": "cats"})
        assert sent["via_bot"] == "inline"
        assert gifs.called("SendInlineBotResultRequest")

    async def test_hide_via_is_asked_for_explicitly(self, live_daemon, client, in_thread, gifs):

        gifs.inline_results = [_inline_gif("r1", title="cat")]
        await result(
            client,
            in_thread,
            "gif.send",
            {"chat": "@alice", "search": "cats", "hide_via": True},
        )
        assert gifs.called("SendInlineBotResultRequest")[0].hide_via is True

    async def test_picking_past_the_results_is_not_found(
        self, live_daemon, client, in_thread, gifs
    ):
        with pytest.raises(Exception) as excinfo:
            await result(
                client,
                in_thread,
                "gif.send",
                {"chat": "@alice", "search": "cats", "pick": 3},
            )
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_sending_nothing_is_a_usage_error(self, live_daemon, client, in_thread, gifs):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "gif.send", {"chat": "@alice"})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_the_search_bot_comes_from_the_server_config(
        self, live_daemon, client, in_thread, gifs
    ):

        gifs.inline_results = [_inline_gif("r1")]
        page = await call(client, in_thread, "gif.search", {"query": "cats"})
        assert page["result"][0]["query_id"] == 987654321
        assert gifs.called("GetConfigRequest")


class TestEmoji:
    async def test_ids_resolve_to_documents(self, live_daemon, client, in_thread, stickers):
        page = await call(client, in_thread, "emoji.get", {"emoji_id": [2001]})
        item = page["result"]["items"][0]
        assert item["doc_id"] == 2001
        assert item["custom_emoji"] is True
        assert item["emoji"] == "\U0001f600"
        assert item["free"] is True

    async def test_ids_can_come_from_a_message(self, live_daemon, client, in_thread, stickers):
        from telethon.tl import types

        message = stickers.add_message(ALICE, "hi", message_id=401)
        message.entities = [types.MessageEntityCustomEmoji(offset=0, length=2, document_id=2001)]
        page = await call(client, in_thread, "emoji.get", {"from_message": "@alice:401"})
        assert [i["doc_id"] for i in page["result"]["items"]] == [2001]

    async def test_no_ids_at_all_is_a_usage_error(self, live_daemon, client, in_thread, stickers):
        with pytest.raises(Exception) as excinfo:
            await call(client, in_thread, "emoji.get", {})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_the_groups_are_the_picker_chips(self, live_daemon, client, in_thread, stickers):
        page = await call(client, in_thread, "emoji.list", {"kind": "groups"})
        item = page["result"]["items"][0]
        assert item["title"] == "Smileys"
        assert item["emoticons"]

    async def test_the_default_lists_are_document_ids(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "emoji.list", {"kind": "default-profile-photo"})
        assert page["result"]["items"][0]["document_ids"]

    async def test_keyword_search_matches_locally(self, live_daemon, client, in_thread, stickers):
        page = await call(client, in_thread, "emoji.search", {"query": "grin"})
        assert [i["emoticon"] for i in page["result"]] == ["\U0001f600"]

    async def test_custom_search_asks_the_server(self, live_daemon, client, in_thread, stickers):
        page = await call(
            client, in_thread, "emoji.search", {"query": "\U0001f600", "custom": True}
        )
        assert page["result"]
        assert stickers.called("SearchCustomEmojiRequest")

    async def test_the_suggest_url_is_printed_rather_than_opened(
        self, live_daemon, client, in_thread, stickers
    ):
        page = await call(client, in_thread, "emoji.search", {"query": "x", "suggest_url": True})
        assert page["result"][0]["keyword"].startswith("https://")
