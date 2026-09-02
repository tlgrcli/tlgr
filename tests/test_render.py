"""Output rendering — every rule in ARCHITECTURE §9."""

from __future__ import annotations

import io
import json

import pytest

from tlgr.cli.render import (
    format_cell,
    get_path,
    project,
    render,
    render_human,
    render_json,
    render_plain,
    results_payload,
)

ENVELOPE = {
    "ok": True,
    "op": "message.get",
    "account": "work",
    "result": {"id": 1042, "text": "ping", "sender": {"title": "Sara N"}, "views": None},
    "meta": {"request_id": "01J", "elapsed_ms": 84},
}

PAGE_ENVELOPE = {
    "ok": True,
    "op": "message.list",
    "result": [{"id": 88, "text": "a"}, {"id": 87, "text": "b"}],
    "page": {"has_more": True, "next_cursor": "abc", "total": 4120},
}


def out() -> io.StringIO:
    return io.StringIO()


class TestJson:
    def test_envelope_is_verbatim(self):
        buffer = out()
        render_json(ENVELOPE, stream=buffer)
        assert json.loads(buffer.getvalue()) == ENVELOPE

    def test_results_only_returns_the_result(self):
        buffer = out()
        render_json(ENVELOPE, results_only=True, stream=buffer)
        assert json.loads(buffer.getvalue()) == ENVELOPE["result"]

    def test_results_only_on_a_scalar_result_keeps_the_object(self):
        """COR-18: v1 guessed a 'primary' key and printed a bare 2."""
        envelope = {"ok": True, "op": "message.delete", "result": {"deleted": 2}}
        buffer = out()
        render_json(envelope, results_only=True, stream=buffer)
        assert json.loads(buffer.getvalue()) == {"deleted": 2}

    def test_results_only_on_a_page_is_page_of_t(self):
        buffer = out()
        render_json(PAGE_ENVELOPE, results_only=True, stream=buffer)
        assert json.loads(buffer.getvalue()) == {
            "items": PAGE_ENVELOPE["result"],
            "has_more": True,
            "next_cursor": "abc",
            "total": 4120,
        }

    def test_select_alone_keeps_the_envelope(self):
        """v1's documented example printed {} here."""
        buffer = out()
        render_json(ENVELOPE, select="id,sender.title", stream=buffer)
        rendered = json.loads(buffer.getvalue())
        assert rendered["ok"] is True
        assert rendered["op"] == "message.get"
        assert rendered["result"] == {"id": 1042, "sender.title": "Sara N"}

    def test_select_with_results_only(self):
        buffer = out()
        render_json(ENVELOPE, results_only=True, select="id", stream=buffer)
        assert json.loads(buffer.getvalue()) == {"id": 1042}

    def test_select_recurses_into_lists(self):
        buffer = out()
        render_json(PAGE_ENVELOPE, select="id", stream=buffer)
        assert json.loads(buffer.getvalue())["result"] == [{"id": 88}, {"id": 87}]

    def test_select_omits_missing_paths(self):
        assert project({"a": 1}, ["a", "b.c"]) == {"a": 1}

    def test_unicode_is_not_escaped(self):
        buffer = out()
        render_json({"ok": True, "result": {"text": "سلام"}}, stream=buffer)
        assert "سلام" in buffer.getvalue()

    def test_output_is_newline_terminated(self):
        buffer = out()
        render_json(ENVELOPE, stream=buffer)
        assert buffer.getvalue().endswith("\n")

    def test_error_envelope_results_only_is_the_error_object(self):
        envelope = {"ok": False, "error": {"code": "USAGE", "exit_code": 2}}
        assert results_payload(envelope) == {"code": "USAGE", "exit_code": 2}


class TestCells:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "-"),
            (True, "yes"),
            (False, "no"),
            (42, "42"),
            ("plain", "plain"),
            (["a", "b", "c"], "a, b, c"),
            ([], ""),
            ({"a": 1}, "a=1"),
            ("line\nbreak", "line⏎break"),
        ],
    )
    def test_formatting(self, value, expected):
        assert format_cell(value) == expected

    def test_long_text_is_truncated(self):
        assert format_cell("x" * 100).endswith("…")
        assert len(format_cell("x" * 100)) == 48

    def test_wide_disables_truncation(self):
        assert format_cell("x" * 100, wide=True) == "x" * 100

    def test_timestamps_render_local(self):
        rendered = format_cell("2026-09-02T09:14:07Z")
        assert "T" not in rendered and "Z" not in rendered

    def test_a_non_timestamp_string_is_untouched(self):
        assert format_cell("2026-09-02") == "2026-09-02"


class TestPaths:
    def test_dot_path(self):
        assert get_path({"a": {"b": 1}}, "a.b") == (1, True)

    def test_missing_path(self):
        assert get_path({"a": {}}, "a.b") == (None, False)

    def test_list_index(self):
        assert get_path({"a": [10, 20]}, "a.1") == (20, True)


class TestPlain:
    def test_tsv_with_header(self):
        buffer = out()
        render_plain(PAGE_ENVELOPE["result"], ["id", "text"], stream=buffer)
        assert buffer.getvalue() == "id\ttext\n88\ta\n87\tb\n"

    def test_no_header(self):
        buffer = out()
        render_plain(PAGE_ENVELOPE["result"], ["id"], no_header=True, stream=buffer)
        assert buffer.getvalue() == "88\n87\n"

    def test_none_is_an_empty_field(self):
        buffer = out()
        render_plain([{"a": None}], ["a"], no_header=True, stream=buffer)
        assert buffer.getvalue() == "\n"

    def test_tabs_and_newlines_are_escaped(self):
        buffer = out()
        render_plain([{"a": "x\ty\nz"}], ["a"], no_header=True, stream=buffer)
        assert buffer.getvalue() == "x y z\n"

    def test_dot_paths_work(self):
        buffer = out()
        render_plain([{"s": {"t": "S"}}], ["s.t"], no_header=True, stream=buffer)
        assert buffer.getvalue() == "S\n"


class TestHuman:
    def test_table_for_lists(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render_human(PAGE_ENVELOPE["result"], ["id", "text"], stream=buffer)
        lines = buffer.getvalue().splitlines()
        assert lines[0].split() == ["ID", "TEXT"]
        assert lines[1].split() == ["88", "a"]

    def test_custom_headers(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render_human([{"id": 1}], ["id"], headers=["Message"], stream=buffer)
        assert buffer.getvalue().splitlines()[0] == "Message"

    def test_no_header(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render_human([{"id": 1}], ["id"], no_header=True, stream=buffer)
        assert buffer.getvalue() == "1\n"

    def test_nested_objects_use_dot_paths(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render_human([{"sender": {"title": "Sara N"}}], ["sender.title"], stream=buffer)
        assert "Sara N" in buffer.getvalue()
        assert "{" not in buffer.getvalue()

    def test_key_value_block_for_a_single_object(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render_human({"id": 1042, "views": None}, (), stream=buffer)
        assert buffer.getvalue().splitlines() == ["id     1042", "views  -"]

    def test_map_of_records_becomes_a_table(self, monkeypatch):
        """A mapping whose values are objects has a natural key column."""
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render_human({"codes": {"OK": {"code": 0}}}, (), stream=buffer)
        text = buffer.getvalue()
        assert "codes:" in text
        assert "NAME" in text and "CODE" in text
        assert "OK" in text

    def test_colour_is_off_without_a_tty(self):
        buffer = out()
        render_human([{"id": 1}], ["id"], stream=buffer)
        assert "\x1b[" not in buffer.getvalue()

    def test_no_color_env_is_honoured(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")

        class Tty(io.StringIO):
            def isatty(self):
                return True

        buffer = Tty()
        render_human([{"id": 1}], ["id"], stream=buffer)
        assert "\x1b[" not in buffer.getvalue()


class TestRenderEntryPoint:
    def test_json_mode(self):
        buffer = out()
        render(ENVELOPE, fmt="json", stream=buffer)
        assert json.loads(buffer.getvalue())["ok"] is True

    def test_human_uses_the_spec_columns(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render(PAGE_ENVELOPE, fmt="human", spec_columns=("id",), stream=buffer)
        assert buffer.getvalue().splitlines()[0] == "ID"

    def test_columns_override_the_spec(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render(PAGE_ENVELOPE, fmt="human", spec_columns=("id",), columns="text", stream=buffer)
        assert buffer.getvalue().splitlines()[0] == "TEXT"

    def test_a_page_renders_its_items(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render(PAGE_ENVELOPE, fmt="plain", spec_columns=("id",), stream=buffer)
        assert buffer.getvalue() == "id\n88\n87\n"

    def test_select_drives_the_columns(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        buffer = out()
        render(PAGE_ENVELOPE, fmt="plain", select="id", stream=buffer)
        assert buffer.getvalue() == "id\n88\n87\n"

    def test_wide_is_passed_through(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        long_text = "y" * 100
        buffer = out()
        render(
            {"ok": True, "result": [{"t": long_text}]},
            fmt="human",
            spec_columns=("t",),
            wide=True,
            stream=buffer,
        )
        assert long_text in buffer.getvalue()
