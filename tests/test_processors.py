"""Tests for the processor registry and chain."""

from __future__ import annotations

from tlgr.processors import (
    ProcessorChain,
    create_chain_from_list,
    create_chain_from_spec,
    get_processor,
    list_processors,
    register_processor,
)


class TestRegistry:
    def test_builtin_processors_registered(self):
        names = list_processors()
        assert "replace_mentions" in names
        assert "remove_links" in names
        assert "remove_hashtags" in names
        assert "strip_formatting" in names
        assert "add_prefix" in names
        assert "add_suffix" in names
        assert "regex_replace" in names

    def test_get_unknown_returns_none(self):
        assert get_processor("nonexistent") is None

    def test_custom_processor_registration(self):
        @register_processor("_test_upper")
        def upper(text, config=None):
            return text.upper()

        assert get_processor("_test_upper") is not None
        assert get_processor("_test_upper")("hello", {}) == "HELLO"


class TestBuiltinProcessors:
    def test_replace_mentions(self):
        fn = get_processor("replace_mentions")
        assert fn("hello @user world", {}) == "hello  world"

    def test_remove_links(self):
        fn = get_processor("remove_links")
        assert fn("visit https://example.com now", {}) == "visit  now"

    def test_remove_hashtags(self):
        fn = get_processor("remove_hashtags")
        assert fn("hello #world", {}) == "hello "

    def test_strip_formatting(self):
        fn = get_processor("strip_formatting")
        result = fn("  hello   world  \n\n\n\nend  ", {})
        assert result == "hello world\n\nend"

    def test_add_prefix(self):
        fn = get_processor("add_prefix")
        result = fn("hello", {"prefix": "[NEWS]"})
        assert result == "[NEWS]\nhello"

    def test_add_suffix(self):
        fn = get_processor("add_suffix")
        result = fn("hello", {"suffix": "[END]"})
        assert result == "hello\n[END]"

    def test_regex_replace(self):
        fn = get_processor("regex_replace")
        result = fn("hello WORLD", {"pattern": "world", "replacement": "earth", "flags": "i"})
        assert result == "hello earth"

    def test_regex_replace_no_pattern(self):
        fn = get_processor("regex_replace")
        assert fn("hello", {}) == "hello"


class TestProcessorChain:
    def test_empty_chain(self):
        chain = ProcessorChain()
        assert chain.apply("hello") == "hello"
        assert len(chain) == 0

    def test_single_processor(self):
        chain = ProcessorChain()
        chain.add("strip_formatting")
        assert chain.apply("  hello  ") == "hello"

    def test_chained_processors(self):
        chain = ProcessorChain()
        chain.add("remove_links")
        chain.add("strip_formatting")
        result = chain.apply("visit  https://example.com  now")
        assert "https" not in result
        assert result == "visit now"

    def test_add_inline_regex(self):
        chain = ProcessorChain()
        chain.add_inline("foo", "bar", "i")
        assert chain.apply("Foo baz") == "bar baz"

    def test_fluent_api(self):
        result = (
            ProcessorChain()
            .add("remove_links")
            .add("strip_formatting")
            .apply("  https://x.com  hello  ")
        )
        assert result == "hello"

    def test_unknown_processor_raises(self):
        chain = ProcessorChain()
        import pytest

        with pytest.raises(ValueError, match="Unknown processor"):
            chain.add("nonexistent_proc")


class TestChainFromSpec:
    def test_simple(self):
        chain = create_chain_from_spec("strip_formatting")
        assert len(chain) == 1

    def test_multiple(self):
        chain = create_chain_from_spec("remove_links,strip_formatting")
        assert len(chain) == 2

    def test_with_config(self):
        chain = create_chain_from_spec("add_prefix:prefix=[NEWS]")
        assert len(chain) == 1
        assert chain.apply("hello") == "[NEWS]\nhello"

    def test_empty(self):
        chain = create_chain_from_spec("")
        assert len(chain) == 0


class TestChainFromList:
    def test_string_items(self):
        chain = create_chain_from_list(["strip_formatting", "remove_links"])
        assert len(chain) == 2

    def test_string_with_config(self):
        chain = create_chain_from_list(["add_prefix:prefix=[NEWS]"])
        assert chain.apply("hello") == "[NEWS]\nhello"

    def test_dict_items(self):
        chain = create_chain_from_list(
            [
                {"pattern": "foo", "replacement": "bar", "flags": "i"},
            ]
        )
        assert len(chain) == 1
        assert chain.apply("Foo") == "bar"

    def test_mixed(self):
        chain = create_chain_from_list(
            [
                "strip_formatting",
                {"pattern": "hello", "replacement": "world"},
            ]
        )
        assert len(chain) == 2
