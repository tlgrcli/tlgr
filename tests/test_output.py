"""Tests for output formatting: JSON transforms, results_only, select."""

from __future__ import annotations

import json

from tlgr.core.output import (
    _select_fields,
    _unwrap_primary,
    apply_json_transforms,
    emit,
    output_json,
)


class TestUnwrapPrimary:
    def test_returns_results_key(self):
        data = {"results": [1, 2, 3], "count": 3}
        assert _unwrap_primary(data) == [1, 2, 3]

    def test_single_non_envelope_key(self):
        data = {"messages": [{"id": 1}], "has_more": False}
        assert _unwrap_primary(data) == [{"id": 1}]

    def test_prefers_list_candidate(self):
        data = {"chats": [{"id": 1}], "query": "test"}
        assert _unwrap_primary(data) == [{"id": 1}]

    def test_non_dict_passthrough(self):
        assert _unwrap_primary([1, 2]) == [1, 2]
        assert _unwrap_primary("hello") == "hello"

    def test_returns_dict_if_no_clear_candidate(self):
        data = {"a": 1, "b": 2}
        assert _unwrap_primary(data) == data


class TestSelectFields:
    def test_select_from_dict(self):
        data = {"id": 1, "name": "Alice", "phone": "555"}
        result = _select_fields(data, ["id", "name"])
        assert result == {"id": 1, "name": "Alice"}

    def test_select_from_list(self):
        data = [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]
        result = _select_fields(data, ["name"])
        assert result == [{"name": "Alice"}, {"name": "Bob"}]

    def test_dot_path(self):
        data = {"sender": {"id": 42, "name": "Alice"}, "text": "hello"}
        result = _select_fields(data, ["sender.name", "text"])
        assert result == {"sender.name": "Alice", "text": "hello"}

    def test_missing_field_excluded(self):
        data = {"id": 1}
        result = _select_fields(data, ["id", "missing"])
        assert result == {"id": 1}

    def test_non_dict_passthrough(self):
        assert _select_fields("hello", ["x"]) == "hello"


class TestApplyJsonTransforms:
    def test_results_only(self):
        data = {"messages": [1, 2], "has_more": True}
        result = apply_json_transforms(data, results_only=True)
        assert result == [1, 2]

    def test_select(self):
        data = {"id": 1, "name": "x", "phone": "555"}
        result = apply_json_transforms(data, select="id,name")
        assert result == {"id": 1, "name": "x"}

    def test_both(self):
        data = {"results": [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]}
        result = apply_json_transforms(data, results_only=True, select="id")
        assert result == [{"id": 1}, {"id": 2}]

    def test_no_transforms(self):
        data = {"a": 1}
        assert apply_json_transforms(data) == {"a": 1}


class TestOutputJson:
    def test_writes_json_line(self, capsys):
        output_json({"ok": True})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"ok": True}

    def test_results_only(self, capsys):
        output_json({"messages": [1]}, results_only=True)
        captured = capsys.readouterr()
        assert json.loads(captured.out) == [1]

    def test_select(self, capsys):
        output_json({"id": 1, "name": "x"}, select="id")
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"id": 1}

    def test_flood_wait(self, capsys):
        output_json({"ok": True}, flood_wait=30)
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["flood_wait"] == 30


class TestEmit:
    def test_passes_results_only(self, capsys):
        ctx_obj = {"fmt": "json", "results_only": True, "select": None}
        emit(ctx_obj, {"messages": [{"id": 1}], "has_more": False})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == [{"id": 1}]

    def test_passes_select(self, capsys):
        ctx_obj = {"fmt": "json", "results_only": False, "select": "id"}
        emit(ctx_obj, {"id": 1, "name": "x"})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"id": 1}

    def test_human_format(self, capsys):
        ctx_obj = {"fmt": "human", "results_only": False, "select": None}
        emit(ctx_obj, [{"key": "val"}], columns=["key"])
        captured = capsys.readouterr()
        assert "KEY" in captured.out
        assert "val" in captured.out
