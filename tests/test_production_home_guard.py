"""A home marked `.production` is refused by development code (see paths.refuse_production_home)."""

from __future__ import annotations

import pytest

from tlgr.core.errors import ConfigurationError
from tlgr.core.paths import PRODUCTION_MARKER, TlgrPaths, refuse_production_home


def test_unmarked_home_is_fine(tmp_path):
    TlgrPaths(tmp_path)
    refuse_production_home(tmp_path)


def test_marked_home_is_refused_with_a_hint(tmp_path):
    (tmp_path / PRODUCTION_MARKER).touch()
    with pytest.raises(ConfigurationError) as excinfo:
        TlgrPaths(tmp_path)
    message = str(excinfo.value)
    assert "TLGR_HOME" in message
    assert "TLGR_ALLOW_PRODUCTION_HOME" in message


def test_explicit_override_allows_the_marked_home(tmp_path, monkeypatch):
    (tmp_path / PRODUCTION_MARKER).touch()
    monkeypatch.setenv("TLGR_ALLOW_PRODUCTION_HOME", "1")
    assert TlgrPaths(tmp_path).base == tmp_path


def test_default_base_honours_the_marker_too(tmp_path, monkeypatch):
    monkeypatch.setenv("TLGR_HOME", str(tmp_path))
    monkeypatch.delenv("TLGR_ALLOW_PRODUCTION_HOME", raising=False)
    (tmp_path / PRODUCTION_MARKER).touch()
    with pytest.raises(ConfigurationError):
        TlgrPaths()
