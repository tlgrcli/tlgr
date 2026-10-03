"""`jobs.yaml`: the action syntax, the shared knobs, `pacing:`, and the errors."""

from __future__ import annotations

import pytest
import yaml

from tlgr.gateway.config import JobConfigError, _parse_job, parse_jobs_document
from tlgr.gateway.knobs import ActionKnobs, merge_knobs
from tlgr.gateway.pacer import DEFAULT_PACING, PacingRule

#: The shape of the forward jobs running in production; they must load as before.
PRODUCTION = """\
jobs:
- name: ircfspace-to-vpeon
  account: Neo
  filters:
    chat_id: '@ircfspace'
  actions:
  - forward:
      to:
      - '@VPEON'
- name: wbnet-to-vpeon
  account: Neo
  filters:
    chat_id: '@wbnet'
  actions:
  - forward:
      to:
      - '@VPEON'
      drop_author: true
      processors:
      - type: regex
        pattern: wbnet
        replacement: VPEON
        flags: i
"""

APPROVED = """\
pacing:
  Neo:
    react:   {every: 4s, per_hour: 300}
    read:    {every: 2s}
    view:    {every: 2s}
    forward: {every: 1.5s}
    reply:   {every: 1.5s}
    expire:  {react: 24h, view: 24h, reply: 24h}

jobs:
  - name: dm-ack
    account: Neo
    filters: {chat_type: private, sender_is_contact: true}
    presence: {mode: session, quiet_hours: "01:00-08:00"}
    on_takeover: cancel
    actions:
      - read:  {delay: 10-90s}
      - view:  {delay: 15-120s}
      - react:
          emoji: ["👍", "❤", "🔥"]
          percent: 60
          delay: 30-300s
      - reply:
          text: "Got it, will answer soon"
          filters: {chat_is_new: true}
          typing: true
          delay: 1-3m

  - name: wbnet-to-vpeon
    account: Neo
    filters: {chat_id: "@wbnet"}
    actions:
      - forward: {to: ["@VPEON"], drop_author: true}
"""


class TestProductionJobs:
    def test_the_running_forward_jobs_load_with_their_old_meaning(self):
        loaded = parse_jobs_document(yaml.safe_load(PRODUCTION))
        assert loaded.problems == []
        plain, rewritten = loaded.jobs
        assert plain.actions[0].params == {"to": ["@VPEON"], "drop_author": False}
        assert plain.actions[0].processors is None
        assert rewritten.actions[0].params == {"to": ["@VPEON"], "drop_author": True}
        assert rewritten.actions[0].processors.apply("join WBNET") == "join VPEON"
        # No knobs: act at once, every time, presence untouched.
        assert merge_knobs(rewritten.knobs, rewritten.actions[0].knobs) == ActionKnobs()


class TestApprovedSyntax:
    def test_the_example_the_user_approved_parses(self):
        loaded = parse_jobs_document(yaml.safe_load(APPROVED))
        assert loaded.problems == []
        dm = loaded.jobs[0]
        assert dm.knobs["presence"].mode == "session"
        assert dm.knobs["presence"].quiet_hours.text == "01:00-08:00"
        read, view, react, reply = dm.actions
        assert read.knobs["delay"] == (10.0, 90.0)
        assert react.knobs["percent"] == 60
        assert react.params["choices"] == [["👍", 1.0], ["❤", 1.0], ["🔥", 1.0]]
        assert reply.params == {"text": "Got it, will answer soon", "typing": True}
        assert reply.knobs["delay"] == (60.0, 180.0)
        assert reply.filters is not None

        pacing = loaded.pacing["Neo"]
        assert pacing.rules["react"] == PacingRule(every=4.0, per_hour=300)
        assert pacing.rules["forward"] == PacingRule(every=1.5, per_hour=None)
        assert pacing.expire == {"react": 86400.0, "view": 86400.0, "reply": 86400.0}

    def test_short_forms(self):
        job = _parse_job(
            {
                "name": "short",
                "actions": [{"read": {}}, {"read": True}, {"view": None}, {"reply": "hi"}],
            }
        )
        assert [a.name for a in job.actions] == ["read", "read", "view", "reply"]

    def test_pacing_keeps_the_default_cap_unless_lifted(self):
        loaded = parse_jobs_document(
            {"pacing": {"Neo": {"react": {"every": "6s"}, "view": {"per_hour": None}}}}
        )
        assert loaded.pacing["Neo"].rules["react"] == PacingRule(every=6.0, per_hour=300)
        assert loaded.pacing["Neo"].rules["view"].every == DEFAULT_PACING["view"].every

    def test_expire_never(self):
        loaded = parse_jobs_document({"pacing": {"Neo": {"expire": {"react": "never"}}}})
        assert loaded.pacing["Neo"].expire == {"react": None}


class TestErrors:
    @pytest.mark.parametrize(
        ("job", "fragment"),
        [
            ({"name": "x", "actions": [{"read": {"delay": "soonish"}}]}, "delay"),
            ({"name": "x", "actions": [{"react": {"emoji": "👍", "percent": 101}}]}, "percent"),
            ({"name": "x", "presence": "always", "actions": [{"read": {}}]}, "presence"),
            ({"name": "x", "on_takeover": "panic", "actions": [{"read": {}}]}, "on_takeover"),
            ({"name": "x", "actions": [{"read": {"histroy": True}}]}, "unknown key"),
            ({"name": "x", "colour": "red", "actions": [{"read": {}}]}, "unknown key"),
            ({"name": "x", "actions": [{"teleport": {}}]}, "unknown action"),
            ({"name": "x", "actions": [{"react": {"👍": 1, "delay": "5s"}}]}, "long form"),
            ({"name": "x", "actions": [{"read": {"processors": ["x"]}}]}, "processors"),
            ({"name": "x", "actions": [{"forward": {}}]}, "to"),
            ({"actions": [{"read": {}}]}, "name"),
        ],
    )
    def test_each_problem_is_named(self, job, fragment):
        with pytest.raises(JobConfigError) as caught:
            _parse_job(job)
        assert fragment in str(caught.value)

    def test_problems_carry_the_job_and_the_action_position(self):
        with pytest.raises(JobConfigError) as caught:
            _parse_job({"name": "dm", "actions": [{"read": {}}, {"react": {"emoji": ""}}]})
        assert "job 'dm', action 2" in str(caught.value)

    def test_a_broken_job_does_not_take_the_others_with_it(self):
        loaded = parse_jobs_document(
            {
                "jobs": [
                    {"name": "good", "actions": [{"forward": {"to": "@a"}}]},
                    {"name": "bad", "actions": [{"read": {"delay": "x"}}]},
                ]
            }
        )
        assert [job.name for job in loaded.jobs] == ["good"]
        assert loaded.rejected == ["bad"]
        assert len(loaded.problems) == 1

    @pytest.mark.parametrize(
        "pacing",
        [
            {"Neo": {"react": {"every": "x"}}},
            {"Neo": {"react": {"per_hour": 0}}},
            {"Neo": {"tickle": {"every": "1s"}}},
            {"Neo": {"react": {"every": "1s", "burst": 3}}},
            {"Neo": {"expire": {"read": "soon"}}},
            {"Neo": {"expire": {"poke": "1h"}}},
            ["Neo"],
        ],
    )
    def test_bad_pacing_is_reported(self, pacing):
        assert parse_jobs_document({"pacing": pacing, "jobs": []}).problems

    def test_a_duplicate_name_is_reported(self):
        loaded = parse_jobs_document({"jobs": [{"name": "a", "actions": [{"read": {}}]}] * 2})
        assert [job.name for job in loaded.jobs] == ["a"]
        assert "used twice" in loaded.problems[0]
