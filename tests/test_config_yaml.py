"""Tests for YAML configuration loading."""

from __future__ import annotations

from tlgr.filters.compose import Op
from tlgr.gateway.config import (
    _parse_action,
    _parse_job,
    load_gateway_configs,
)


class TestParseAction:
    def test_reply_string(self):
        ac = _parse_action({"reply": "hello!"})
        assert ac.name == "reply"
        assert ac.config == "hello!"

    def test_forward_dict(self):
        ac = _parse_action({"forward": {"to": ["@dest"], "drop_author": True}})
        assert ac.name == "forward"
        assert ac.config["to"] == ["@dest"]
        assert ac.config["drop_author"] is True

    def test_action_with_filters(self):
        ac = _parse_action(
            {
                "forward": {
                    "to": ["@dest"],
                    "filters": {"chat_type": "private"},
                }
            }
        )
        assert ac.filters is not None
        assert ac.filters.filter_name == "chat_type"

    def test_action_with_processors(self):
        ac = _parse_action(
            {
                "forward": {
                    "to": ["@dest"],
                    "processors": ["strip_formatting"],
                }
            }
        )
        assert ac.processors is not None
        assert len(ac.processors) == 1


class TestParseJob:
    def test_minimal(self):
        cfg = _parse_job(
            {
                "name": "test",
                "account": "main",
                "actions": [{"reply": "hi"}],
            }
        )
        assert cfg.name == "test"
        assert cfg.account == "main"
        assert cfg.enabled is True
        assert len(cfg.actions) == 1

    def test_with_filters(self):
        cfg = _parse_job(
            {
                "name": "test",
                "filters": {"chat_type": "private", "contains": ["hello"]},
                "actions": [{"reply": "hi"}],
            }
        )
        assert cfg.filters is not None
        assert cfg.filters.op is Op.AND

    def test_disabled(self):
        cfg = _parse_job(
            {
                "name": "disabled",
                "enabled": False,
                "actions": [{"reply": "hi"}],
            }
        )
        assert cfg.enabled is False

    def test_multiple_actions(self):
        cfg = _parse_job(
            {
                "name": "multi",
                "actions": [
                    {"reply": "hello!"},
                    {"forward": {"to": ["@dest"]}},
                ],
            }
        )
        assert len(cfg.actions) == 2
        assert cfg.actions[0].name == "reply"
        assert cfg.actions[1].name == "forward"

    def test_job_level_processors(self):
        cfg = _parse_job(
            {
                "name": "proc",
                "processors": ["strip_formatting"],
                "actions": [{"reply": "hi"}],
            }
        )
        assert cfg.processors is not None
        assert len(cfg.processors) == 1


class TestLoadGatewayConfigs:
    def test_load_from_yaml(self, tmp_path):
        yaml_content = """
jobs:
  - name: test-reply
    account: main
    filters:
      chat_type: private
    actions:
      - reply: "hello!"

  - name: test-forward
    account: main
    filters:
      chat_id: "@source"
    actions:
      - forward:
          to: ["@dest"]
"""
        (tmp_path / "jobs.yaml").write_text(yaml_content)
        configs = load_gateway_configs(tmp_path)
        assert len(configs) == 2
        assert configs[0].name == "test-reply"
        assert configs[0].actions[0].name == "reply"
        assert configs[0].actions[0].config == "hello!"
        assert configs[1].name == "test-forward"

    def test_load_missing_file(self, tmp_path):
        configs = load_gateway_configs(tmp_path)
        assert configs == []

    def test_load_empty_file(self, tmp_path):
        (tmp_path / "jobs.yaml").write_text("")
        configs = load_gateway_configs(tmp_path)
        assert configs == []

    def test_complex_filters(self, tmp_path):
        yaml_content = """
jobs:
  - name: complex
    account: main
    filters:
      chat_type: private
      any_of:
        - contains: [hello]
        - from_users: [42]
      none_of:
        - contains: [spam]
    actions:
      - reply: "matched!"
"""
        (tmp_path / "jobs.yaml").write_text(yaml_content)
        configs = load_gateway_configs(tmp_path)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.filters is not None
        assert cfg.filters.op is Op.AND

    def test_inline_regex_processors(self, tmp_path):
        yaml_content = """
jobs:
  - name: regex-job
    account: main
    actions:
      - forward:
          to: ["@dest"]
          processors:
            - strip_formatting
            - type: regex
              pattern: "sponsor"
              replacement: ""
              flags: i
"""
        (tmp_path / "jobs.yaml").write_text(yaml_content)
        configs = load_gateway_configs(tmp_path)
        assert len(configs) == 1
        ac = configs[0].actions[0]
        assert ac.processors is not None
        assert len(ac.processors) == 2
