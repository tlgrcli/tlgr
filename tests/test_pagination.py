"""Tests for cursor pagination encoding/decoding and has_more."""

from __future__ import annotations

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
