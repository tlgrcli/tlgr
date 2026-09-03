"""`config.toml` decodes into types, and a typo is an error (§10.2).

v1 read every key with `raw.get("x", default)`, so a misspelled key was
silently the default and a wrongly-typed one became a `TypeError` three
modules away from the file that caused it. The tests below pin the two things
that changed: a wrong type names the key, and every §10.2 section exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tlgr.core.config import (
    AppConfig,
    WebhookConfig,
    load_app_config,
    load_webhook_config,
    save_webhook_config,
)
from tlgr.core.errors import ConfigurationError


def _write(home: Path, body: str) -> None:
    (home / "config.toml").write_text(body)


class TestDefaults:
    def test_an_absent_file_is_the_defaults(self, tlgr_home: Path):
        config = load_app_config(tlgr_home)
        assert isinstance(config, AppConfig)
        assert config.daemon.auto_start is True
        assert config.daemon.idle_timeout == 1800
        assert config.defaults.parse_mode == "none"
        assert config.security.peer_uid_check is True

    def test_every_section_of_10_2_exists(self, tlgr_home: Path):
        config = load_app_config(tlgr_home)
        for section in (
            "accounts",
            "defaults",
            "daemon",
            "identity",
            "presence",
            "network",
            "flood",
            "limits",
            "security",
            "policy",
            "logging",
            "media",
        ):
            assert hasattr(config, section), f"[{section}] is missing from AppConfig"

    def test_the_rate_classes_have_shipped_defaults(self, tlgr_home: Path):
        config = load_app_config(tlgr_home)
        assert config.rate_for("read").rate == 10.0
        # Deliberately slow: contacts.resolveUsername floods at ~50 calls.
        assert config.rate_for("resolve").rate == 0.5
        assert config.rate_for("send").new_peers_per_day == 30
        # An unknown class falls back rather than crashing a request.
        assert config.rate_for("nonsense").rate == 10.0


class TestParsing:
    def test_values_are_read(self, tlgr_home: Path):
        _write(
            tlgr_home,
            """
            [accounts]
            default = "work"

            [daemon]
            idle_timeout = 0
            preconnect = ["work", "personal"]

            [presence]
            mode = "online"

            [rate.send]
            rate = 0.2
            burst = 1
            """,
        )
        config = load_app_config(tlgr_home)
        assert config.default_account == "work"
        assert config.daemon.idle_timeout == 0
        assert config.daemon.preconnect == ["work", "personal"]
        assert config.presence.mode == "online"
        assert config.rate_for("send").rate == 0.2
        # A partially overridden [rate] table keeps the other classes.
        assert config.rate_for("read").rate == 10.0

    def test_a_wrong_type_names_the_key(self, tlgr_home: Path):
        _write(tlgr_home, '[daemon]\nidle_timeout = "soon"\n')
        with pytest.raises(ConfigurationError) as caught:
            load_app_config(tlgr_home)
        assert "idle_timeout" in str(caught.value)

    def test_invalid_toml_names_the_file(self, tlgr_home: Path):
        _write(tlgr_home, "[daemon\n")
        with pytest.raises(ConfigurationError) as caught:
            load_app_config(tlgr_home)
        assert "config.toml" in str(caught.value)

    def test_an_unknown_key_is_tolerated(self, tlgr_home: Path):
        """A config written by a newer tlgr must not stop an older one."""
        _write(tlgr_home, "[daemon]\nfrom_the_future = true\n")
        assert load_app_config(tlgr_home).daemon.auto_start is True

    def test_the_environment_wins_over_the_file(self, tlgr_home: Path, monkeypatch):
        _write(tlgr_home, '[accounts]\ndefault = "work"\n')
        monkeypatch.setenv("TLGR_ACCOUNT", "personal")
        assert load_app_config(tlgr_home).default_account == "personal"


class TestWebhookConfig:
    def test_it_round_trips(self, tlgr_home: Path):
        original = WebhookConfig(
            enabled=True,
            url="https://example.test/hook",
            secret="k",
            events=["message_new"],
        )
        save_webhook_config(original, tlgr_home)
        loaded = load_webhook_config(tlgr_home)
        assert loaded.enabled is True
        assert loaded.url == "https://example.test/hook"
        assert loaded.events == ["message_new"]

    def test_the_file_is_private(self, tlgr_home: Path):
        """It holds a token: SEC-07 applies to it more than to anything else."""
        save_webhook_config(WebhookConfig(enabled=True, url="x", token="t"), tlgr_home)
        assert (tlgr_home / "webhook.toml").stat().st_mode & 0o777 == 0o600

    def test_the_signing_key_falls_back_to_the_token(self):
        """An existing config keeps signing without being edited."""
        assert WebhookConfig(token="t").signing_key == "t"
        assert WebhookConfig(token="t", secret="s").signing_key == "s"

    def test_v1_filters_still_parse(self, tlgr_home: Path):
        (tlgr_home / "webhook.toml").write_text(
            """
            [webhook]
            enabled = true
            url = "https://example.test/hook"

            [webhook.filters]
            chats = ["@one", "@two"]
            chat_type = "private"
            """
        )
        config = load_webhook_config(tlgr_home)
        assert config.filters.chats == ["@one", "@two"]
        assert config.filters.raw == {"chat_type": "private"}


class TestDeadCodeIsGone:
    def test_the_jobs_toml_engine_is_deleted(self):
        """MNT-04: it had no callers and a second job format is a trap."""
        from tlgr.core import config

        for name in ("load_jobs", "save_jobs", "JobConfig", "DestinationConfig"):
            assert not hasattr(config, name), f"{name} survived the deletion"

    def test_jobs_are_yaml_and_parsed_by_the_gateway(self, tlgr_home: Path):
        from tlgr.gateway.config import load_gateway_configs

        assert load_gateway_configs(tlgr_home) == []


class TestPaths:
    def test_the_layout_matches_10_1(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        assert paths.config.name == "config.toml"
        assert paths.socket.name == "daemon.sock"
        assert paths.state.name == "daemon.state"
        assert paths.session("work").parent.name == "work"
        assert paths.flood("work").name == "flood.json"
        assert paths.events_state("work").name == "events.state"

    def test_the_home_can_be_moved_with_an_environment_variable(self, monkeypatch, tmp_path):
        from tlgr.core.paths import default_base

        monkeypatch.setenv("TLGR_HOME", str(tmp_path / "elsewhere"))
        assert default_base() == tmp_path / "elsewhere"

    def test_directories_are_created_0700(self, tlgr_home: Path):
        from tlgr.core.paths import TlgrPaths

        paths = TlgrPaths(tlgr_home)
        assert paths.ensure_logs().stat().st_mode & 0o777 == 0o700
        assert paths.ensure_account_dir("work").stat().st_mode & 0o777 == 0o700


class TestIdleTimeout:
    def test_a_webhook_forces_it_off(self):
        from tlgr.daemon.idle import effective_idle_timeout

        assert effective_idle_timeout(1800, webhook_enabled=True) == 0

    def test_a_supervisor_forces_it_off(self):
        """COR-39: a clean idle exit under launchd is never restarted."""
        from tlgr.daemon.idle import effective_idle_timeout

        assert effective_idle_timeout(1800, webhook_enabled=False, managed_by="launchd") == 0
        assert effective_idle_timeout(1800, webhook_enabled=False, managed_by="systemd") == 0

    def test_otherwise_the_configured_value_stands(self):
        from tlgr.daemon.idle import effective_idle_timeout

        assert effective_idle_timeout(900, webhook_enabled=False) == 900


class TestActivity:
    def test_an_in_flight_request_is_never_idle(self):
        """COR-11: v1 killed a ten-minute scan at minute thirty."""
        from tlgr.daemon.idle import ActivityTracker

        tracker = ActivityTracker()
        tracker.begin_request()
        tracker.last_activity -= 100000
        assert tracker.may_stop(1) is False
        tracker.end_request()

    def test_an_open_stream_or_transfer_counts(self):
        from tlgr.daemon.idle import ActivityTracker

        tracker = ActivityTracker()
        tracker.begin_stream()
        assert tracker.busy is True
        tracker.end_stream()
        tracker.begin_transfer()
        assert tracker.busy is True
        tracker.end_transfer()
        assert tracker.busy is False

    def test_the_counters_never_go_negative(self):
        from tlgr.daemon.idle import ActivityTracker

        tracker = ActivityTracker()
        tracker.end_request()
        tracker.end_stream()
        assert tracker.in_flight == 0
        assert tracker.event_streams == 0


class TestProxy:
    def test_a_socks_url_becomes_a_proxy_dict(self):
        from tlgr.daemon.sessions import _proxy_tuple

        proxy = _proxy_tuple("socks5://user:pass@host:1080")
        assert proxy["proxy_type"] == "socks5"
        assert proxy["addr"] == "host"
        assert proxy["port"] == 1080
        assert proxy["username"] == "user"

    def test_an_mtproxy_needs_its_secret(self):
        from tlgr.daemon.sessions import _proxy_tuple

        assert _proxy_tuple("mtproxy://host:443#deadbeef") == ("host", 443, "deadbeef")
        with pytest.raises(ConfigurationError):
            _proxy_tuple("mtproxy://host:443")

    def test_nonsense_is_a_config_error(self):
        from tlgr.daemon.sessions import _proxy_tuple

        assert _proxy_tuple("") is None
        with pytest.raises(ConfigurationError):
            _proxy_tuple("not-a-url")
        with pytest.raises(ConfigurationError):
            _proxy_tuple("gopher://host:70")


class TestIdentity:
    def test_it_is_stable_across_calls(self, tlgr_home: Path):
        """A Devices entry that changes every reboot is worse than useless."""
        from tlgr.core.identity import load_identity

        first = load_identity(tlgr_home)
        second = load_identity(tlgr_home)
        assert first == second
        assert (tlgr_home / "identity.json").stat().st_mode & 0o777 == 0o600

    def test_config_overrides_the_derived_values(self, tlgr_home: Path):
        from tlgr.core.identity import load_identity

        identity = load_identity(tlgr_home, device_model="Test Rig")
        assert identity.device_model == "Test Rig"

    def test_it_never_claims_to_be_an_official_client(self, tlgr_home: Path):
        from tlgr.core.identity import load_identity

        identity = load_identity(tlgr_home)
        assert identity.app_version.startswith("tlgr ")
        assert "Telegram" not in identity.device_model
