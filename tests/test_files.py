"""The transfer pipelines stage C will hang the media operations on (§6.7).

The behaviours pinned here are the ones that are invisible until they are
needed at 2 GB and 90 %: a resume that starts on a part boundary, a file
reference refreshed exactly once, a per-DC budget that a single large download
cannot exhaust, and a pre-flight that refuses an impossible upload before it
spends the bandwidth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tlgr.core.errors import UsageError
from tlgr.daemon.files import (
    DEFAULT_PART_SIZE,
    LARGE_TRANSFER_BYTES,
    DownloadPlan,
    TransferSlots,
    UploadPlan,
    download,
    file_reference_index,
    infer_attributes,
    part_size_for,
    upload,
)

pytestmark = pytest.mark.asyncio


class _Client:
    """A client that yields chunks and can fail the first attempt."""

    def __init__(self, data: bytes, *, fail_first: BaseException | None = None) -> None:
        self.data = data
        self.fail_first = fail_first
        self.offsets: list[int] = []
        self.calls: list[Any] = []

    def iter_download(self, location, *, offset=0, request_size=DEFAULT_PART_SIZE, limit=None):
        self.offsets.append(offset)
        failure, self.fail_first = self.fail_first, None
        data = self.data

        class _Iter:
            def __aiter__(self_inner):
                self_inner._pos = offset
                return self_inner

            async def __anext__(self_inner):
                if failure is not None and self_inner._pos == offset:
                    raise failure
                if self_inner._pos >= len(data):
                    raise StopAsyncIteration
                chunk = data[self_inner._pos : self_inner._pos + request_size]
                self_inner._pos += len(chunk)
                return chunk

        return _Iter()

    async def __call__(self, request):
        self.calls.append(request)
        return None


class TestPartSize:
    def test_small_files_use_small_parts(self):
        assert part_size_for(1024) == 128 * 1024

    def test_large_files_use_the_maximum(self):
        assert part_size_for(50 * 1024 * 1024) == DEFAULT_PART_SIZE
        assert part_size_for(2 * 1024**3) == DEFAULT_PART_SIZE

    def test_every_part_size_divides_a_megabyte(self):
        """Telegram rejects anything else with FILE_PART_SIZE_INVALID."""
        for size in (1, 10**6, 10**8, 10**10):
            assert (1024 * 1024) % part_size_for(size) == 0


class TestDownload:
    async def test_it_writes_through_a_part_file_and_renames(self, tmp_path: Path):
        client = _Client(b"x" * 4096)
        plan = DownloadPlan(target=tmp_path / "out.bin", size=4096, part_size=1024)
        result = await download(client, object(), plan)
        assert result.read_bytes() == b"x" * 4096
        assert not plan.part_file.exists(), "the .part file was left behind"

    async def test_it_resumes_on_a_part_boundary(self, tmp_path: Path):
        """Resuming mid-part asks the server for an offset it rejects."""
        client = _Client(b"y" * 4096)
        plan = DownloadPlan(target=tmp_path / "out.bin", size=4096, part_size=1024)
        plan.part_file.write_bytes(b"y" * 1500)  # one and a half parts

        await download(client, object(), plan)
        assert client.offsets == [1024], "the resume offset was not a whole part"
        assert plan.target.stat().st_size == 4096

    async def test_resume_can_be_turned_off(self, tmp_path: Path):
        client = _Client(b"z" * 100)
        plan = DownloadPlan(target=tmp_path / "out.bin", size=100, resume=False)
        plan.part_file.write_bytes(b"stale")
        await download(client, object(), plan)
        assert client.offsets == [0]

    async def test_an_expired_file_reference_is_refreshed_once(self, tmp_path: Path):
        from telethon.errors import FileReferenceExpiredError

        client = _Client(b"a" * 512, fail_first=FileReferenceExpiredError(None))
        refreshed: list[int] = []

        async def refresh() -> object:
            refreshed.append(1)
            return object()

        plan = DownloadPlan(target=tmp_path / "out.bin", size=512)
        await download(client, object(), plan, refresh=refresh)
        assert refreshed == [1]
        assert plan.target.read_bytes() == b"a" * 512

    async def test_a_second_reference_failure_is_not_swallowed(self, tmp_path: Path):
        from telethon.errors import FileReferenceExpiredError

        class _AlwaysFails(_Client):
            def iter_download(self, *args, **kwargs):
                raise FileReferenceExpiredError(None)

        async def refresh() -> object:
            return object()

        plan = DownloadPlan(target=tmp_path / "out.bin", size=512)
        with pytest.raises(FileReferenceExpiredError):
            await download(_AlwaysFails(b""), object(), plan, refresh=refresh)

    async def test_a_short_download_is_an_error_not_a_file(self, tmp_path: Path):
        """A truncated download that renames into place is data loss."""
        from tlgr.core.errors import TlgrError

        client = _Client(b"short")
        plan = DownloadPlan(target=tmp_path / "out.bin", size=99999)
        with pytest.raises(TlgrError):
            await download(client, object(), plan)
        assert not plan.target.exists()

    async def test_progress_is_reported(self, tmp_path: Path):
        seen: list[tuple[int, int]] = []
        client = _Client(b"p" * 2048)
        plan = DownloadPlan(target=tmp_path / "out.bin", size=2048, part_size=512)
        await download(client, object(), plan, progress=lambda d, t: seen.append((d, t)))
        assert seen[-1] == (2048, 2048)

    def test_the_index_of_an_album_item_is_parsed(self):
        """Only the expired item is refreshed, not the whole sendMultiMedia."""
        assert file_reference_index("FILE_REFERENCE_3_EXPIRED") == 3
        assert file_reference_index("FILE_REFERENCE_EXPIRED") is None


class TestTransferSlots:
    async def test_large_and_small_have_separate_budgets(self):
        """One 2 GB download must not starve a chat list's thumbnails."""
        slots = TransferSlots(small=5, large=1)
        big = slots.slot(2, LARGE_TRANSFER_BYTES)
        await big.acquire()
        assert big.locked()

        small = slots.slot(2, 1024)
        assert small is not big
        await small.acquire()
        small.release()
        big.release()

    async def test_the_budget_is_per_dc(self):
        slots = TransferSlots()
        assert slots.slot(1, 1024) is not slots.slot(2, 1024)
        assert slots.slot(1, 1024) is slots.slot(1, 2048)


class TestUpload:
    async def test_a_file_that_cannot_fit_is_refused_before_a_byte_is_sent(self, tmp_path: Path):
        source = tmp_path / "big.bin"
        source.write_bytes(b"0" * 4096)
        plan = UploadPlan(source=source, part_size=1)
        client = _Client(b"")
        with pytest.raises(UsageError) as caught:
            await upload(client, plan, max_parts_allowed=10)
        assert "too large" in str(caught.value)
        assert client.calls == [], "parts were sent for a file that cannot fit"

    async def test_a_missing_file_is_a_usage_error(self, tmp_path: Path):
        plan = UploadPlan(source=tmp_path / "nope.bin")
        with pytest.raises(UsageError):
            await upload(_Client(b""), plan)

    async def test_a_small_file_uploads_with_a_checksum(self, tmp_path: Path):
        from telethon.tl.functions.upload import SaveFilePartRequest
        from telethon.tl.types import InputFile

        source = tmp_path / "small.bin"
        source.write_bytes(b"s" * 3000)
        client = _Client(b"")
        result = await upload(client, UploadPlan(source=source, part_size=1024))
        assert isinstance(result, InputFile)
        assert result.md5_checksum
        assert all(isinstance(r, SaveFilePartRequest) for r in client.calls)
        assert len(client.calls) == 3

    async def test_a_big_file_uses_the_big_part_request(self, tmp_path: Path):
        from telethon.tl.functions.upload import SaveBigFilePartRequest
        from telethon.tl.types import InputFileBig

        source = tmp_path / "big.bin"
        source.write_bytes(b"b" * (11 * 1024 * 1024))
        client = _Client(b"")
        result = await upload(client, UploadPlan(source=source))
        assert isinstance(result, InputFileBig)
        assert all(isinstance(r, SaveBigFilePartRequest) for r in client.calls)
        assert result.parts == len(client.calls)

    async def test_parts_are_numbered_from_zero_and_complete(self, tmp_path: Path):
        source = tmp_path / "f.bin"
        source.write_bytes(b"c" * 5000)
        client = _Client(b"")
        await upload(client, UploadPlan(source=source, part_size=1024))
        assert sorted(r.file_part for r in client.calls) == [0, 1, 2, 3, 4]

    async def test_parts_go_out_concurrently(self, tmp_path: Path):
        """Telethon uploads strictly sequentially; that is the whole point."""
        source = tmp_path / "f.bin"
        source.write_bytes(b"d" * 8192)

        in_flight = 0
        peak = 0

        class _Slow(_Client):
            async def __call__(self, request):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
                self.calls.append(request)
                return None

        await upload(_Slow(b""), UploadPlan(source=source, part_size=1024, parts_in_flight=4))
        assert peak > 1, "the upload was sequential"


class TestAttributes:
    def test_a_file_nobody_can_read_produces_a_warning(self, tmp_path: Path):
        """A video sent with duration=0, 1x1 renders as a black rectangle."""
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"not really a video")
        facts, warnings = infer_attributes(source)
        if not facts:
            assert warnings, "a file with no readable metadata was sent silently"
            assert "--duration" in warnings[0]

    def test_an_unknown_extension_needs_no_attributes(self, tmp_path: Path):
        source = tmp_path / "notes.txt"
        source.write_bytes(b"hello")
        assert infer_attributes(source) == ({}, [])
