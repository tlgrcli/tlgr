"""Tests for cursor pagination encoding/decoding and has_more."""

from __future__ import annotations

import time

import pytest

from tlgr.core.output import add_pagination, decode_cursor, encode_cursor


class TestCursorEncoding:
    def test_roundtrip(self):
        state = {"offset_id": 12345}
        token = encode_cursor(state)
        assert isinstance(token, str)
        assert len(token) > 0
        decoded = decode_cursor(token)
        assert decoded == state

    def test_roundtrip_complex(self):
        state = {"offset": 50, "offset_id": 999}
        token = encode_cursor(state)
        decoded = decode_cursor(token)
        assert decoded == state

    def test_decode_none(self):
        assert decode_cursor(None) == {}

    def test_decode_empty(self):
        assert decode_cursor("") == {}

    def test_decode_invalid(self):
        assert decode_cursor("not-valid-base64!!!") == {}

    def test_decode_corrupt_json(self):
        import base64

        token = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
        assert decode_cursor(token) == {}


class TestAddPagination:
    def test_has_more_true(self):
        envelope: dict = {"messages": [1, 2, 3]}
        result = add_pagination(envelope, [1, 2, 3], limit=3, cursor_state={"offset_id": 100})
        assert result["has_more"] is True
        assert "next_cursor" in result
        decoded = decode_cursor(result["next_cursor"])
        assert decoded == {"offset_id": 100}

    def test_has_more_false(self):
        envelope: dict = {"messages": [1, 2]}
        result = add_pagination(envelope, [1, 2], limit=5, cursor_state={"offset_id": 100})
        assert result["has_more"] is False
        assert "next_cursor" not in result

    def test_empty_results(self):
        envelope: dict = {"messages": []}
        result = add_pagination(envelope, [], limit=10, cursor_state={})
        assert result["has_more"] is False

    def test_exact_limit(self):
        items = list(range(20))
        envelope: dict = {"items": items}
        result = add_pagination(envelope, items, limit=20, cursor_state={"offset": 20})
        assert result["has_more"] is True

    def test_explicit_has_more_false_suppresses_cursor(self):
        """A caller holding the whole list knows the truth; the heuristic doesn't.

        `contact list` with no --limit sets limit = len(page), so the
        `len(items) >= limit` guess was always True and the cursor never
        stopped — a `while has_more` loop over it could not terminate.
        """
        items = list(range(33))
        envelope: dict = {"contacts": items}
        result = add_pagination(
            envelope, items, limit=33, cursor_state={"offset": 33}, has_more=False
        )
        assert result["has_more"] is False
        assert "next_cursor" not in result

    def test_explicit_has_more_true(self):
        envelope: dict = {"contacts": [1, 2]}
        result = add_pagination(
            envelope, [1, 2], limit=99, cursor_state={"offset": 2}, has_more=True
        )
        assert result["has_more"] is True
        assert decode_cursor(result["next_cursor"]) == {"offset": 2}

    def test_exhausted_page_is_not_more(self):
        """Past the end: zero items, zero limit — the guess said True."""
        envelope: dict = {"contacts": []}
        result = add_pagination(envelope, [], limit=0, cursor_state={"offset": 33}, has_more=False)
        assert result["has_more"] is False
        assert "next_cursor" not in result


# ---------------------------------------------------------------------------
# v2 cursors: versioned, op-bound, signed (core/pagination.py)
# ---------------------------------------------------------------------------

from tlgr.core.errors import UsageError  # noqa: E402
from tlgr.core.pagination import (  # noqa: E402
    PageKind,
    build_page,
    cursor_key,
)
from tlgr.core.pagination import decode_cursor as decode_v2  # noqa: E402
from tlgr.core.pagination import encode_cursor as encode_v2  # noqa: E402

KEY = b"\x01" * 32


class TestV2Cursor:
    def test_roundtrip(self):
        token = encode_v2(
            op="message.list",
            kind=PageKind.HISTORY,
            state={"offset_id": 1042},
            account="work",
            key=KEY,
        )
        assert decode_v2(token, op="message.list", account="work", key=KEY) == {"offset_id": 1042}

    def test_is_opaque(self):
        token = encode_v2(op="message.list", kind=PageKind.HISTORY, state={"offset_id": 1}, key=KEY)
        assert "offset_id" not in token
        assert "." in token

    def test_rejects_a_foreign_op(self):
        token = encode_v2(op="message.list", kind=PageKind.HISTORY, state={"a": 1}, key=KEY)
        with pytest.raises(UsageError, match="belongs to"):
            decode_v2(token, op="chat.list", key=KEY)

    def test_rejects_a_foreign_kind(self):
        token = encode_v2(op="message.list", kind=PageKind.HISTORY, state={"a": 1}, key=KEY)
        with pytest.raises(UsageError, match="paginates"):
            decode_v2(token, op="message.list", kind=PageKind.SEARCH, key=KEY)

    def test_rejects_a_foreign_account(self):
        token = encode_v2(
            op="message.list", kind=PageKind.HISTORY, state={"a": 1}, account="work", key=KEY
        )
        with pytest.raises(UsageError, match="different account"):
            decode_v2(token, op="message.list", account="personal", key=KEY)

    def test_rejects_a_tampered_payload(self):
        token = encode_v2(op="message.list", kind=PageKind.HISTORY, state={"a": 1}, key=KEY)
        head, _, sig = token.partition(".")
        tampered = f"{head[:-2]}AA.{sig}"
        with pytest.raises(UsageError, match=r"signature|corrupt"):
            decode_v2(tampered, op="message.list", key=KEY)

    def test_rejects_a_truncated_token(self):
        token = encode_v2(op="message.list", kind=PageKind.HISTORY, state={"a": 1}, key=KEY)
        with pytest.raises(UsageError):
            decode_v2(token[: len(token) // 2], op="message.list", key=KEY)

    def test_rejects_a_v1_cursor(self):
        """v1's bare base64 JSON must fail loudly, not decode to a fresh walk."""
        with pytest.raises(UsageError, match="missing signature"):
            decode_v2(encode_cursor({"offset_id": 5}), op="message.list", key=KEY)

    def test_rejects_a_foreign_key(self):
        token = encode_v2(op="message.list", kind=PageKind.HISTORY, state={"a": 1}, key=KEY)
        with pytest.raises(UsageError, match="signature"):
            decode_v2(token, op="message.list", key=b"\x02" * 32)

    def test_rejects_an_expired_cursor(self):
        token = encode_v2(
            op="message.list", kind=PageKind.HISTORY, state={"a": 1}, key=KEY, ttl=10, now=1000
        )
        with pytest.raises(UsageError, match="expired"):
            decode_v2(token, op="message.list", key=KEY, now=2000)

    def test_local_cursors_expire_sooner(self):
        local = encode_v2(op="draft.list", kind=PageKind.LOCAL, state={"offset": 0}, key=KEY)
        history = encode_v2(op="message.list", kind=PageKind.HISTORY, state={}, key=KEY)
        with pytest.raises(UsageError, match="expired"):
            decode_v2(local, op="draft.list", key=KEY, now=int(time.time()) + 3700)
        assert decode_v2(history, op="message.list", key=KEY, now=int(time.time()) + 3700) == {}

    def test_key_is_created_privately(self, tmp_path):
        key = cursor_key(tmp_path)
        assert len(key) == 32
        assert cursor_key(tmp_path) == key, "the key must be stable once generated"
        assert (tmp_path / "cursor.key").stat().st_mode & 0o077 == 0


class TestBuildPage:
    def test_no_cursor_when_exhausted(self):
        page = build_page([1, 2], op="message.list", kind=PageKind.HISTORY, limit=10, key=KEY)
        assert page.has_more is False
        assert page.next_cursor is None

    def test_cursor_when_more(self):
        page = build_page(
            [1, 2],
            op="message.list",
            kind=PageKind.HISTORY,
            state={"offset_id": 2},
            limit=2,
            key=KEY,
        )
        assert page.has_more is True
        assert page.next_cursor
        assert decode_v2(page.next_cursor, op="message.list", key=KEY) == {"offset_id": 2}

    def test_explicit_has_more_beats_the_guess(self):
        page = build_page(
            [1, 2], op="message.list", kind=PageKind.HISTORY, limit=2, has_more=False, key=KEY
        )
        assert page.has_more is False

    def test_total_is_carried_but_never_invented(self):
        assert build_page([], op="x.list", kind=PageKind.LOCAL, key=KEY).total is None
        assert build_page([], op="x.list", kind=PageKind.LOCAL, total=42, key=KEY).total == 42
