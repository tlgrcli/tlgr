"""The `media` operations, end to end through a real daemon.

Every test here goes over a real Unix socket, through the real middleware
chain and the real dispatcher, into the real implementation, against a fake
Telegram whose documents are real `types.Document` objects and whose bytes
`iter_download` really serves. That is the only arrangement in which "did the
download work" means anything: the assertion is that a file with the right
bytes exists, not that a request object looked plausible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
    classify,
)

ALICE = 4242
CHANNEL = 5150
CHANNEL_ID = -1000000000000 - CHANNEL

PHOTO_ID = 9001
VIDEO_ID = 9002
VOICE_ID = 9003


@pytest.fixture
def peers(world):
    from fake_telethon import make_channel, make_user

    world.add_user(make_user(ALICE, username="alice"))
    world.add_channel(make_channel(CHANNEL, title="News"))
    return world


@pytest.fixture
def media(peers, world):
    """A chat with one photo, one video and one voice note in it."""
    from fake_telethon import make_document
    from telethon.tl import types

    world.add_media_message(ALICE, message_id=101, text="a photo")
    world.add_media_message(
        ALICE,
        message_id=102,
        text="a video",
        document=make_document(
            VIDEO_ID,
            mime="video/mp4",
            size=4096,
            attributes=[
                types.DocumentAttributeVideo(duration=42, w=1280, h=720, supports_streaming=True),
                types.DocumentAttributeFilename(file_name="clip.mp4"),
            ],
        ),
    )
    world.add_media_message(
        ALICE,
        message_id=103,
        document=make_document(
            VOICE_ID,
            mime="audio/ogg",
            size=512,
            attributes=[types.DocumentAttributeAudio(duration=7, voice=True)],
        ),
    )
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


def _error(excinfo) -> Any:
    return classify(excinfo.value)


class TestGet:
    async def test_a_video_reports_what_it_is(self, live_daemon, client, in_thread, media):
        info = await result(client, in_thread, "media.get", {"chat": "@alice", "msg_id": 102})
        assert info["kind"] == "video"
        assert info["mime"] == "video/mp4"
        assert info["duration"] == 42
        assert info["width"] == 1280
        assert info["file_name"] == "clip.mp4"
        assert info["supports_streaming"] is True

    async def test_a_voice_note_is_not_an_audio_file(self, live_daemon, client, in_thread, media):
        """The attribute decides, and only after all of them are collected."""
        info = await result(client, in_thread, "media.get", {"chat": "@alice", "msg_id": 103})
        assert info["kind"] == "voice"
        assert info["voice"] is True

    async def test_sizes_include_the_inline_thumbnail(self, live_daemon, client, in_thread, media):
        """A stripped thumbnail costs no request; it is already in the message."""
        info = await result(
            client, in_thread, "media.get", {"chat": "@alice", "msg_id": 101, "sizes": True}
        )
        stripped = [t for t in info["thumbs"] if t.get("bytes_b64")]
        assert stripped, "the inline stripped size was dropped"

    async def test_a_message_without_media_is_not_found(
        self, live_daemon, client, in_thread, peers
    ):
        peers.add_message(ALICE, "just text", message_id=200)
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.get", {"chat": "@alice", "msg_id": 200})
        assert _error(excinfo).exit_code == EXIT_NOT_FOUND

    async def test_the_message_is_refetched_before_the_ids_are_read(
        self, live_daemon, client, in_thread, media
    ):
        """A file_reference expires; every id tlgr prints came from a live fetch."""
        await result(client, in_thread, "media.get", {"chat": "@alice", "msg_id": 102})
        assert media.called("iter_messages"), "media get answered from a cache"


class TestList:
    async def test_the_media_tab_lists_only_media(self, live_daemon, client, in_thread, media):
        media.add_message(ALICE, "text only", message_id=150)
        page = await call(client, in_thread, "media.list", {"chat": "@alice"})
        assert 150 not in [item["msg_id"] for item in page["result"]]

    async def test_ids_only_drops_everything_else(self, live_daemon, client, in_thread, media):
        page = await call(client, in_thread, "media.list", {"chat": "@alice", "ids_only": True})
        assert page["result"]
        assert all(item.get("name") is None for item in page["result"])

    async def test_a_cursor_continues_where_the_page_stopped(
        self, live_daemon, client, in_thread, media
    ):
        first = await call(client, in_thread, "media.list", {"chat": "@alice"}, limit=1)
        assert first["page"]["has_more"] is True
        cursor = first["page"]["next_cursor"]
        assert cursor
        second = await call(
            client, in_thread, "media.list", {"chat": "@alice"}, limit=1, cursor=cursor
        )
        assert second["result"][0]["msg_id"] != first["result"][0]["msg_id"]

    async def test_a_hand_edited_cursor_is_refused(self, live_daemon, client, in_thread, media):
        with pytest.raises(Exception) as excinfo:
            await call(client, in_thread, "media.list", {"chat": "@alice"}, cursor="nonsense")
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_counts_answer_with_the_tabs(self, live_daemon, client, in_thread, media):
        page = await call(client, in_thread, "media.list", {"chat": "@alice", "counts": True})
        assert {item["type"] for item in page["result"]} >= {"photo", "video", "file"}

    async def test_an_unknown_type_is_a_usage_error(self, live_daemon, client, in_thread, media):
        with pytest.raises(Exception) as excinfo:
            await call(client, in_thread, "media.list", {"chat": "@alice", "type": "nope"})
        assert _error(excinfo).exit_code == EXIT_USAGE


class TestDownload:
    async def test_the_bytes_land_on_disk(self, live_daemon, client, in_thread, media, tmp_path):
        page = await call(
            client,
            in_thread,
            "media.download",
            {"chat": "@alice", "msg_id": ["102"], "out_dir": str(tmp_path)},
        )
        item = page["result"]["items"][0]
        written = Path(item["path"])
        assert written.exists()
        assert written.read_bytes() == media.file_bytes[VIDEO_ID]
        assert item["bytes"] == len(media.file_bytes[VIDEO_ID])

    async def test_a_range_fetches_only_that_range(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        page = await call(
            client,
            in_thread,
            "media.download",
            {"chat": "@alice", "msg_id": ["102"], "out_dir": str(tmp_path), "range": "0-63"},
        )
        assert Path(page["result"]["items"][0]["path"]).stat().st_size == 64

    async def test_parallel_readers_reassemble_the_same_file(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        page = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["102"],
                "out_dir": str(tmp_path),
                "connections": 4,
                "part_size": 4,
            },
        )
        assert Path(page["result"]["items"][0]["path"]).read_bytes() == media.file_bytes[VIDEO_ID]
        assert len(media.called("iter_download")) >= 4

    async def test_skip_existing_does_not_fetch_twice(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        first = await call(
            client,
            in_thread,
            "media.download",
            {"chat": "@alice", "msg_id": ["102"], "out_dir": str(tmp_path)},
        )
        target = Path(first["result"]["items"][0]["path"])
        second = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["102"],
                "out": str(target),
                "skip_existing": True,
            },
        )
        assert second["result"]["items"][0]["skipped"] is True

    async def test_a_protected_chat_needs_the_flag(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        peers.add_media_message(ALICE, message_id=110, noforwards=True)
        with pytest.raises(Exception) as excinfo:
            await call(
                client,
                in_thread,
                "media.download",
                {"chat": "@alice", "msg_id": ["110"], "out_dir": str(tmp_path)},
            )
        assert _error(excinfo).exit_code == EXIT_PERMISSION
        allowed = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["110"],
                "out_dir": str(tmp_path),
                "allow_protected": True,
            },
        )
        assert allowed["result"]["items"][0]["bytes"] > 0

    async def test_a_stripped_thumbnail_costs_no_request(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        before = len(media.called("iter_download"))
        page = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["101"],
                "out_dir": str(tmp_path),
                "thumb": "stripped",
            },
        )
        assert Path(page["result"]["items"][0]["path"]).exists()
        assert len(media.called("iter_download")) == before

    async def test_verify_checks_the_server_hashes(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        page = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["102"],
                "out_dir": str(tmp_path),
                "verify": True,
            },
        )
        assert page["result"]["items"][0]["sha256"]

    async def test_read_marks_the_media_consumed(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        await call(
            client,
            in_thread,
            "media.download",
            {"chat": "@alice", "msg_id": ["103"], "out_dir": str(tmp_path), "read": True},
        )
        assert media.called("ReadMessageContentsRequest")

    async def test_a_server_supplied_name_cannot_escape_the_directory(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        from fake_telethon import make_document
        from telethon.tl import types

        peers.add_media_message(
            ALICE,
            message_id=120,
            document=make_document(
                9100,
                mime="application/pdf",
                size=64,
                attributes=[types.DocumentAttributeFilename(file_name="../../escaped.pdf")],
            ),
        )
        page = await call(
            client,
            in_thread,
            "media.download",
            {"chat": "@alice", "msg_id": ["120"], "out_dir": str(tmp_path)},
        )
        written = Path(page["result"]["items"][0]["path"]).resolve()
        assert str(written).startswith(str(tmp_path.resolve()))

    async def test_play_is_refused_rather_than_run_in_the_daemon(
        self, live_daemon, client, in_thread, media
    ):
        with pytest.raises(Exception) as excinfo:
            await call(
                client,
                in_thread,
                "media.download",
                {"chat": "@alice", "msg_id": ["102"], "play": "mpv -"},
            )
        assert _error(excinfo).code == "NOT_SUPPORTED"

    async def test_a_profile_photo_downloads_without_a_message(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        peers.profile_photos[ALICE] = b"avatarbytes"
        page = await call(
            client,
            in_thread,
            "media.download",
            {"profile": "@alice", "out": str(tmp_path / "a.jpg")},
        )
        assert Path(page["result"]["items"][0]["path"]).read_bytes() == b"avatarbytes"

    async def test_a_chat_is_required_without_another_source(
        self, live_daemon, client, in_thread, media
    ):
        with pytest.raises(Exception) as excinfo:
            await call(client, in_thread, "media.download", {"msg_id": ["102"]})
        assert _error(excinfo).exit_code == EXIT_USAGE


class TestUpload:
    async def test_a_photo_lands_in_the_chat(self, live_daemon, client, in_thread, peers, tmp_path):
        source = tmp_path / "cat.jpg"
        source.write_bytes(b"\xff\xd8" + b"x" * 512)
        sent = await result(
            client,
            in_thread,
            "media.upload",
            {"chat": "@alice", "path": [str(source)], "caption": ["the cat"]},
        )
        assert sent["kind"] == "photo"
        assert sent["msg_id"]
        request = peers.called("SendMediaRequest")[0]
        assert type(request.media).__name__ == "InputMediaUploadedPhoto"
        assert request.message == "the cat"

    async def test_a_video_carries_probed_attributes(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"\x00" * 2048)
        await result(
            client,
            in_thread,
            "media.upload",
            {
                "chat": "@alice",
                "path": [str(source)],
                "duration": 42,
                "width": 1280,
                "height": 720,
            },
        )
        request = peers.called("SendMediaRequest")[0]
        video = [
            a for a in request.media.attributes if type(a).__name__ == "DocumentAttributeVideo"
        ]
        assert video and video[0].duration == 42 and video[0].w == 1280

    async def test_a_voice_note_is_not_an_audio_file(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        source = tmp_path / "note.ogg"
        source.write_bytes(b"OggS" + b"\x00" * 128)
        await result(
            client,
            in_thread,
            "media.upload",
            {"chat": "@alice", "path": [str(source)], "send_as_kind": "voice"},
        )
        request = peers.called("SendMediaRequest")[0]
        audio = [
            a for a in request.media.attributes if type(a).__name__ == "DocumentAttributeAudio"
        ]
        assert audio and audio[0].voice is True

    async def test_two_files_become_one_album(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        first = tmp_path / "a.jpg"
        second = tmp_path / "b.jpg"
        for path in (first, second):
            path.write_bytes(b"\xff\xd8" + b"y" * 256)
        sent = await result(
            client,
            in_thread,
            "media.upload",
            {"chat": "@alice", "path": [str(first), str(second)]},
        )
        assert sent["kind"] == "album"
        assert len(sent["msg_ids"]) == 2
        assert len(peers.called("SendMultiMediaRequest")) == 1
        assert len(peers.called("UploadMediaRequest")) == 2

    async def test_eleven_files_are_refused_before_anything_uploads(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        paths = []
        for index in range(11):
            path = tmp_path / f"{index}.jpg"
            path.write_bytes(b"\xff\xd8z")
            paths.append(str(path))
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.upload", {"chat": "@alice", "path": paths})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_no_send_stops_after_the_upload(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        source = tmp_path / "cat.jpg"
        source.write_bytes(b"\xff\xd8" + b"x" * 64)
        sent = await result(
            client,
            in_thread,
            "media.upload",
            {"chat": "@alice", "path": [str(source)], "no_send": True},
        )
        assert peers.called("UploadMediaRequest")
        assert not peers.called("SendMediaRequest")
        assert sent.get("msg_id", 0) == 0

    async def test_a_missing_file_is_a_usage_error(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        with pytest.raises(Exception) as excinfo:
            await result(
                client,
                in_thread,
                "media.upload",
                {"chat": "@alice", "path": [str(tmp_path / "nope.jpg")]},
            )
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_a_dice_send_uploads_nothing(self, live_daemon, client, in_thread, peers):
        await result(client, in_thread, "media.upload", {"chat": "@alice", "dice": "🎲"})
        request = peers.called("SendMediaRequest")[0]
        assert type(request.media).__name__ == "InputMediaDice"

    async def test_dry_run_does_not_send(self, live_daemon, client, in_thread, peers, tmp_path):
        source = tmp_path / "cat.jpg"
        source.write_bytes(b"\xff\xd8x")
        envelope = await call(
            client,
            in_thread,
            "media.upload",
            {"chat": "@alice", "path": [str(source)]},
            dry_run=True,
        )
        assert envelope["result"]["dry_run"] is True
        assert not peers.called("SendMediaRequest")


class TestEdit:
    async def test_a_caption_edit_moves_no_bytes(self, live_daemon, client, in_thread, media):
        edited = await result(
            client,
            in_thread,
            "media.edit",
            {"chat": "@alice", "msg_id": 102, "caption": "new words"},
        )
        assert edited["changed"] == ["caption"]
        request = media.called("EditMessageRequest")[0]
        assert request.media is None
        assert request.message == "new words"

    async def test_a_spoiler_toggle_passes_the_existing_media_back(
        self, live_daemon, client, in_thread, media
    ):
        edited = await result(
            client,
            in_thread,
            "media.edit",
            {"chat": "@alice", "msg_id": 102, "spoiler": True},
        )
        assert edited["changed"] == ["flags"]
        request = media.called("EditMessageRequest")[0]
        assert type(request.media).__name__ == "InputMediaDocument"
        assert request.media.spoiler is True
        assert request.media.id.id == VIDEO_ID

    async def test_an_edit_with_nothing_to_change_is_a_usage_error(
        self, live_daemon, client, in_thread, media
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.edit", {"chat": "@alice", "msg_id": 102})
        assert _error(excinfo).exit_code == EXIT_USAGE


class TestReadAndLimits:
    async def test_read_marks_the_ids(self, live_daemon, client, in_thread, media):
        marked = await result(client, in_thread, "media.read", {"chat": "@alice", "msg_id": [103]})
        assert marked["marked"] == 1
        assert media.called("ReadMessageContentsRequest")[0].id == [103]

    async def test_read_without_ids_is_a_usage_error(self, live_daemon, client, in_thread, media):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.read", {"chat": "@alice", "msg_id": []})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_limits_come_from_the_server(self, live_daemon, client, in_thread, peers):
        limits = await result(client, in_thread, "media.limit.get")
        assert limits["upload_max_fileparts"] == 4000
        assert limits["upload_max_bytes"] == 4000 * 524288
        assert limits["caption_length_limit"] == 1024


class TestExport:
    async def test_dry_run_reports_the_plan_without_fetching(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        planned = await result(
            client,
            in_thread,
            "media.export",
            {"chat": "@alice", "out_dir": str(tmp_path), "background": False},
            dry_run=True,
        )
        assert planned["planned"] == 3
        assert planned.get("downloaded", 0) == 0

    async def test_an_export_writes_a_manifest_and_resumes(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        first = await result(
            client,
            in_thread,
            "media.export",
            {"chat": "@alice", "out_dir": str(tmp_path), "background": False},
        )
        assert first["downloaded"] == 3
        assert Path(first["manifest"]).exists()
        again = await result(
            client,
            in_thread,
            "media.export",
            {"chat": "@alice", "out_dir": str(tmp_path), "background": False},
        )
        assert again.get("downloaded", 0) == 0
        assert again["skipped"] == 3


class TestTransfers:
    async def test_a_background_download_becomes_a_job(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        page = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["102"],
                "out_dir": str(tmp_path),
                "background": True,
            },
        )
        job_id = page["result"]["items"][0]["job_id"]
        assert job_id
        listed = await call(client, in_thread, "media.transfer.list", {"watch": True})
        found = [row for row in listed["result"] if row["job_id"] == job_id]
        assert found and found[0]["state"] in ("done", "running")

    async def test_stopping_an_unknown_job_reports_nothing_cancelled(
        self, live_daemon, client, in_thread, media
    ):
        stopped = await result(client, in_thread, "media.transfer.stop", {"job_id": ["deadbe"]})
        assert stopped.get("cancelled", 0) == 0
        assert stopped["already"] is True

    async def test_stopping_with_no_target_is_a_usage_error(
        self, live_daemon, client, in_thread, media
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.transfer.stop", {"job_id": []})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_a_finished_job_can_be_retried(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        page = await call(
            client,
            in_thread,
            "media.download",
            {
                "chat": "@alice",
                "msg_id": ["102"],
                "out_dir": str(tmp_path),
                "background": True,
            },
        )
        job_id = page["result"]["items"][0]["job_id"]
        await call(client, in_thread, "media.transfer.list", {"watch": True})
        restarted = await result(client, in_thread, "media.transfer.retry", {"job_id": [job_id]})
        assert restarted["restarted"] == 1


class TestWallpaper:
    @pytest.fixture
    def wallpapers(self, world):
        from fake_telethon import make_wallpaper

        world.wallpapers["Ycb0FfC6"] = make_wallpaper("Ycb0FfC6", pattern=True)
        return world

    async def test_the_gallery_lists_what_the_account_has(
        self, live_daemon, client, in_thread, wallpapers
    ):
        page = await call(client, in_thread, "media.wallpaper.list")
        assert [item["slug"] for item in page["result"]] == ["Ycb0FfC6"]
        assert page["result"][0]["kind"] == "pattern"

    async def test_the_share_link_encodes_the_settings(
        self, live_daemon, client, in_thread, wallpapers
    ):
        item = await result(client, in_thread, "media.wallpaper.get", {"wallpaper": "Ycb0FfC6"})
        assert item["link"].startswith("https://t.me/bg/Ycb0FfC6")
        assert "intensity=50" in item["link"]

    async def test_a_t_me_link_is_accepted_where_a_slug_is(
        self, live_daemon, client, in_thread, wallpapers
    ):
        item = await result(
            client,
            in_thread,
            "media.wallpaper.get",
            {"wallpaper": "https://t.me/bg/Ycb0FfC6?mode=blur"},
        )
        assert item["slug"] == "Ycb0FfC6"

    async def test_installing_also_saves(self, live_daemon, client, in_thread, wallpapers):
        installed = await result(
            client, in_thread, "media.wallpaper.set", {"wallpaper": "Ycb0FfC6", "blur": True}
        )
        assert installed["installed"] is True
        assert wallpapers.installed_wallpaper == "Ycb0FfC6"
        assert wallpapers.called("InstallWallPaperRequest")[0].settings.blur is True

    async def test_a_colour_fill_needs_no_file(self, live_daemon, client, in_thread, wallpapers):
        await result(client, in_thread, "media.wallpaper.set", {"colors": ["#1c2b3a", "#0f5a3c"]})
        request = wallpapers.called("InstallWallPaperRequest")[0]
        assert type(request.wallpaper).__name__ == "InputWallPaperNoFile"
        assert request.settings.background_color == 0x1C2B3A

    async def test_set_without_a_target_is_a_usage_error(
        self, live_daemon, client, in_thread, wallpapers
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.wallpaper.set", {})
        assert _error(excinfo).exit_code == EXIT_USAGE

    async def test_remove_unsaves_without_changing_the_default(
        self, live_daemon, client, in_thread, wallpapers
    ):
        wallpapers.saved_wallpapers.append("Ycb0FfC6")
        removed = await result(
            client, in_thread, "media.wallpaper.remove", {"wallpaper": ["Ycb0FfC6"]}
        )
        assert removed["removed"] == 1
        assert "Ycb0FfC6" not in wallpapers.saved_wallpapers

    async def test_uploading_returns_a_usable_slug(
        self, live_daemon, client, in_thread, wallpapers, tmp_path
    ):
        source = tmp_path / "bg.jpg"
        source.write_bytes(b"\xff\xd8" + b"b" * 128)
        uploaded = await result(client, in_thread, "media.wallpaper.upload", {"path": str(source)})
        assert uploaded["slug"] == "uploaded"
        assert uploaded["link"].endswith("/uploaded")

    async def test_a_numeric_wallpaper_id_is_refused_with_a_reason(
        self, live_daemon, client, in_thread, wallpapers
    ):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.wallpaper.get", {"wallpaper": "12345"})
        body = _error(excinfo)
        assert body.exit_code == EXIT_USAGE
        assert "access hash" in body.message


class TestSettings:
    async def test_auto_download_reports_three_presets(self, live_daemon, client, in_thread, peers):
        settings = await result(client, in_thread, "media.auto-download.get")
        assert [p["preset"] for p in settings["presets"]] == ["low", "medium", "high"]

    async def test_writing_one_preset_keeps_the_others(self, live_daemon, client, in_thread, peers):
        await result(
            client,
            in_thread,
            "media.auto-download.set",
            {"preset": "high", "video_max": "50M"},
        )
        request = peers.called("SaveAutoDownloadSettingsRequest")[0]
        assert request.high is True
        assert request.settings.video_size_max == 50 * 1024 * 1024
        # The untouched fields are the ones that were already there.
        assert request.settings.photo_size_max == 1048576

    async def test_auto_save_writes_exactly_one_scope(self, live_daemon, client, in_thread, peers):
        saved = await result(
            client, in_thread, "media.auto-save.set", {"scope": "users", "photos": True}
        )
        assert saved["scope"] == "users"
        request = peers.called("SaveAutoSaveSettingsRequest")[0]
        assert request.users is True
        assert request.chats is None

    async def test_auto_save_get_reports_the_categories(
        self, live_daemon, client, in_thread, peers
    ):
        settings = await result(client, in_thread, "media.auto-save.get")
        assert set(settings) >= {"users", "chats", "broadcasts"}

    async def test_sensitive_reports_why_it_cannot_change(
        self, live_daemon, client, in_thread, peers
    ):
        peers.sensitive_can_change = False
        settings = await result(client, in_thread, "media.sensitive.get")
        assert settings["sensitive_can_change"] is False
        assert "verification" in (settings.get("reason") or "")

    async def test_setting_sensitive_when_forbidden_is_permission_denied(
        self, live_daemon, client, in_thread, peers
    ):
        peers.sensitive_can_change = False
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.sensitive.set", {"state": "on"})
        assert _error(excinfo).exit_code == EXIT_PERMISSION

    async def test_setting_sensitive_to_what_it_already_is_says_already(
        self, live_daemon, client, in_thread, peers
    ):
        saved = await result(client, in_thread, "media.sensitive.set", {"state": "off"})
        assert saved["already"] is True
        assert not peers.called("SetContentSettingsRequest")

    async def test_a_bad_state_word_is_a_usage_error(self, live_daemon, client, in_thread, peers):
        with pytest.raises(Exception) as excinfo:
            await result(client, in_thread, "media.sensitive.set", {"state": "maybe"})
        assert _error(excinfo).exit_code == EXIT_USAGE


class TestStorage:
    """Local operations: no account, no daemon, no network."""

    async def test_usage_counts_only_tlgr_s_own_root(self, tlgr_home, in_thread):
        import asyncio

        from tlgr.ops.media import StorageGetReq, storage_get

        root = tlgr_home / "downloads" / "777"
        root.mkdir(parents=True)
        (root / "a.jpg").write_bytes(b"x" * 10)
        (root / "b.part").write_bytes(b"y" * 5)
        usage = await asyncio.get_running_loop().run_in_executor(
            None, lambda: asyncio.run(storage_get(_LocalCtx(), StorageGetReq(by_type=True)))
        )
        assert usage.files == 2
        assert usage.bytes == 15
        assert usage.partials == 1
        assert usage.by_type["photo"] == 10

    async def test_clearing_removes_only_what_it_names(self, tlgr_home):
        import asyncio

        from tlgr.ops.media import StorageClearReq, storage_clear

        root = tlgr_home / "downloads"
        root.mkdir(parents=True)
        (root / "a.jpg").write_bytes(b"x" * 10)
        (root / "b.mp4").write_bytes(b"y" * 20)
        cleared = await storage_clear(_LocalCtx(), StorageClearReq(type=["photo"]))
        assert cleared.deleted_files == 1
        assert cleared.freed_bytes == 10
        assert (root / "b.mp4").exists()
        _ = asyncio


class _LocalCtx:
    """The `OpContext` a `Surface.LOCAL` operation runs against."""

    account = ""
    dry_run = False
    request_id = "t"

    def warn(self, message: str) -> None: ...

    def emit(self, event_type: str, payload: dict, **kwargs) -> None: ...


class TestAgentMdCompatibility:
    """v1's two media commands, and the JSON an agent read from them."""

    async def test_the_v1_download_path_still_works(
        self, live_daemon, client, in_thread, media, tmp_path
    ):
        page = await call(
            client,
            in_thread,
            "media.download",
            {"chat": "@alice", "msg_id": ["102"], "out_dir": str(tmp_path)},
        )
        item = page["result"]["items"][0]
        assert set(item) >= {"path", "msg_id"}, "v1's documented keys are gone"

    async def test_the_v1_upload_keys_are_accounted_for(
        self, live_daemon, client, in_thread, peers, tmp_path
    ):
        source = tmp_path / "cat.jpg"
        source.write_bytes(b"\xff\xd8x")
        sent = await result(
            client, in_thread, "media.upload", {"chat": "@alice", "path": [str(source)]}
        )
        # v1 answered {"id", "chat_id"}; `id` became `msg_id`, which is the
        # rename CHANGELOG.md publishes.
        assert "chat_id" in sent
        assert "msg_id" in sent

    def test_the_legacy_paths_resolve(self):
        from tlgr.registry import ALIASES

        for name in ("media.download", "dl", "media.upload", "up", "media.send"):
            assert name in ALIASES
