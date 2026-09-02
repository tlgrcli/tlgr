"""Parse modes, spoilers and entity JSON."""

from __future__ import annotations

import pytest

from tlgr.core.errors import UsageError
from tlgr.core.text import (
    entities_from_json,
    entities_to_json,
    parse_text,
    utf16_len,
)
from tlgr.models.message import MessageEntity


class TestParseMode:
    def test_none_leaves_text_alone(self):
        assert parse_text("**not bold** and _not_ italic", "none") == (
            "**not bold** and _not_ italic",
            [],
        )

    def test_markdown(self):
        text, entities = parse_text("**bold** and `code`", "md")
        assert text == "bold and code"
        assert [(e.type, e.offset, e.length) for e in entities] == [
            ("bold", 0, 4),
            ("code", 9, 4),
        ]

    def test_html(self):
        text, entities = parse_text("<b>bold</b> and <code>code</code>", "html")
        assert text == "bold and code"
        assert {e.type for e in entities} == {"bold", "code"}

    def test_text_url_carries_its_url(self):
        _, entities = parse_text("[here](https://example.com)", "md")
        assert entities[0].type == "text_url"
        assert entities[0].url == "https://example.com"

    def test_unknown_mode_is_usage(self):
        with pytest.raises(UsageError, match="parse mode"):
            parse_text("x", "rst")


class TestSpoilers:
    def test_markdown_spoiler(self):
        """Telethon leaves `||x||` as literal text; tlgr must not."""
        text, entities = parse_text("a ||sec|| b", "md")
        assert text == "a sec b"
        assert [(e.type, e.offset, e.length) for e in entities] == [("spoiler", 2, 3)]

    def test_html_spoiler(self):
        text, entities = parse_text("a <tg-spoiler>sec</tg-spoiler> b", "html")
        assert text == "a sec b"
        assert [(e.type, e.offset, e.length) for e in entities] == [("spoiler", 2, 3)]

    def test_spoiler_shifts_the_other_entities(self):
        text, entities = parse_text("||sec|| **bold**", "md")
        assert text == "sec bold"
        assert sorted((e.type, e.offset, e.length) for e in entities) == [
            ("bold", 4, 4),
            ("spoiler", 0, 3),
        ]

    def test_unmatched_marker_is_left_as_typed(self):
        text, entities = parse_text("cost is 5 || 6", "md")
        assert text == "cost is 5 || 6"
        assert entities == []

    def test_no_spoiler_in_plain_mode(self):
        assert parse_text("a ||sec|| b", "none") == ("a ||sec|| b", [])


class TestUtf16:
    def test_emoji_is_two_units(self):
        assert utf16_len("👍") == 2
        assert utf16_len("a👍") == 3

    def test_offsets_are_utf16_not_characters(self):
        _, entities = parse_text("😀 **b**", "md")
        assert entities[0].offset == 3, "one emoji (2 units) plus a space"

    def test_persian_is_one_unit_per_character(self):
        text, entities = parse_text("**سلام** دنیا", "md")
        assert text == "سلام دنیا"
        assert (entities[0].offset, entities[0].length) == (0, 4)


class TestEntityJson:
    def test_roundtrip(self):
        entities = [MessageEntity(type="bold", offset=0, length=4)]
        assert entities_from_json(entities_to_json(entities)) == entities

    def test_rejects_non_json(self):
        with pytest.raises(UsageError, match="valid JSON"):
            entities_from_json("{not json")

    def test_rejects_a_bare_object(self):
        with pytest.raises(UsageError, match="array"):
            entities_from_json('{"type":"bold"}')

    def test_rejects_a_malformed_entity(self):
        with pytest.raises(UsageError, match="entities"):
            entities_from_json('[{"type":"bold","offset":"zero","length":4}]')
