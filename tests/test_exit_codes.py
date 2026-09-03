"""Tests for exit codes and error emission."""

from __future__ import annotations

import json

from tlgr.core.errors import (
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_DAEMON,
    EXIT_GENERIC,
    EXIT_IPC,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_RATE_LIMITED,
    AuthenticationError,
    ChatNotFoundError,
    ConfigurationError,
    DaemonError,
    DaemonNotRunningError,
    IPCError,
    PermissionError_,
    RateLimitError,
    SessionError,
    TlgrError,
    emit_error,
    exit_code_for,
    format_error_json,
)


class TestExitCodes:
    def test_base_error(self):
        assert exit_code_for(TlgrError("fail")) == EXIT_GENERIC

    def test_auth_error(self):
        assert exit_code_for(AuthenticationError("bad")) == EXIT_AUTH

    def test_session_error(self):
        assert exit_code_for(SessionError("expired")) == EXIT_AUTH

    def test_config_error(self):
        assert exit_code_for(ConfigurationError("bad config")) == EXIT_CONFIG

    def test_chat_not_found(self):
        assert exit_code_for(ChatNotFoundError("no chat")) == EXIT_NOT_FOUND

    def test_permission_error(self):
        assert exit_code_for(PermissionError_("denied")) == EXIT_PERMISSION

    def test_rate_limit_error(self):
        err = RateLimitError("slow down", wait_seconds=30)
        assert exit_code_for(err) == EXIT_RATE_LIMITED
        assert err.wait_seconds == 30

    def test_daemon_error(self):
        assert exit_code_for(DaemonError("crashed")) == EXIT_DAEMON

    def test_daemon_not_running(self):
        assert exit_code_for(DaemonNotRunningError("not running")) == EXIT_DAEMON

    def test_ipc_error(self):
        assert exit_code_for(IPCError("failed")) == EXIT_IPC

    def test_generic_exception(self):
        assert exit_code_for(ValueError("oops")) == EXIT_GENERIC


class TestFormatErrorJson:
    def test_basic_error(self):
        result = format_error_json(TlgrError("something broke"))
        assert result["error"] == "something broke"
        assert result["code"] == "TLGR_ERROR"
        assert result["exit_code"] == EXIT_GENERIC

    def test_rate_limit_includes_wait(self):
        err = RateLimitError("slow", wait_seconds=42)
        result = format_error_json(err)
        assert result["wait_seconds"] == 42
        assert result["code"] == "RATE_LIMITED"

    def test_unknown_exception(self):
        result = format_error_json(RuntimeError("crash"))
        assert result["code"] == "UNKNOWN_ERROR"


class TestEmitError:
    def test_json_mode(self, capsys):
        emit_error(TlgrError("test error"), use_json=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] == "test error"
        assert "Error: test error" in captured.err

    def test_human_mode(self, capsys):
        emit_error(TlgrError("test error"), use_json=False)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Error: test error" in captured.err

    def test_hint_shown(self, capsys):
        emit_error(AuthenticationError("auth failed"), use_json=False)
        captured = capsys.readouterr()
        assert "Run: tlgr account add" in captured.err
