"""The action registry, and each built-in action's config and planning."""

from __future__ import annotations

import random

import pytest

from tlgr.actions import get_action, get_builtin, list_actions, register_action
from tlgr.actions.base import Action, ActionError, MessageFacts
from tlgr.processors import ProcessorChain


class TestRegistry:
    def test_builtin_actions_registered(self):
        names = list_actions()
        for name in ("reply", "forward", "react", "read", "view"):
            assert name in names
            assert isinstance(get_action(name), Action)

    def test_get_unknown_returns_none(self):
        assert get_action("nonexistent") is None
        assert get_builtin("nonexistent") is None

    def test_custom_action_registration(self):
        @register_action("_test_noop")
        async def noop(event, config, client, chain=None):
            pass

        assert get_action("_test_noop") is not None
        assert get_builtin("_test_noop") is None


def _facts(**kwargs):
    return MessageFacts(chat_id=-1001234, msg_id=5, **{"text": "hello", **kwargs})


RNG = random.Random(0)


class TestForward:
    action = get_builtin("forward")

    def test_short_and_long_forms(self):
        assert self.action.parse("@dest") == {"to": ["@dest"], "drop_author": False}
        assert self.action.parse({"to": ["@a", -1001], "drop_author": True}) == {
            "to": ["@a", "-1001"],
            "drop_author": True,
        }

    @pytest.mark.parametrize(
        "config", [{}, {"to": []}, {"to": "@a", "drop_author": "yes"}, {"to": "@a", "x": 1}]
    )
    def test_invalid(self, config):
        with pytest.raises(ActionError):
            self.action.parse(config)

    def test_a_native_forward_per_destination(self):
        params = self.action.parse({"to": ["@a", "@b"], "drop_author": True})
        payloads = self.action.plan(_facts(), params, None, RNG)
        assert payloads == [
            {"to": "@a", "drop_author": True, "resend": None},
            {"to": "@b", "drop_author": True, "resend": None},
        ]

    def test_processors_turn_it_into_a_re_send(self):
        chain = ProcessorChain().add("add_prefix", {"prefix": "[FWD]"})
        params = self.action.parse({"to": "@a"})
        text = self.action.plan(_facts(), params, chain, RNG)[0]["resend"]
        assert text == {"text": "[FWD]\nhello", "media": False}
        photo = self.action.plan(_facts(media="photo"), params, chain, RNG)[0]["resend"]
        assert photo["media"] is True
        preview = self.action.plan(_facts(media="webpage"), params, chain, RNG)[0]["resend"]
        assert preview["media"] is False

    @pytest.mark.parametrize(
        "facts",
        [
            {"service": True},
            {"view_once": True, "media": "photo"},
            {"text": "", "media": None},
        ],
    )
    def test_what_was_never_forwardable_is_skipped(self, facts):
        assert self.action.plan(_facts(**facts), self.action.parse("@a"), None, RNG) == []


class TestReply:
    action = get_builtin("reply")

    def test_short_and_long_forms(self):
        assert self.action.parse("hi") == {"text": "hi", "typing": True}
        assert self.action.parse({"text": "hi", "typing": False}) == {
            "text": "hi",
            "typing": False,
        }

    @pytest.mark.parametrize("config", ["", {"typing": True}, {"text": "x", "typing": 1}, 5])
    def test_invalid(self, config):
        with pytest.raises(ActionError):
            self.action.parse(config)


class TestReact:
    action = get_builtin("react")

    def test_forms(self):
        assert self.action.parse("👍")["choices"] == [["👍", 1.0]]
        assert self.action.parse(["👍", "❤"])["choices"] == [["👍", 1.0], ["❤", 1.0]]
        assert self.action.parse({"👍": 3, "🔥": 1})["choices"] == [["👍", 3.0], ["🔥", 1.0]]
        long = self.action.parse({"emoji": {"👍": 2}, "big": True})
        assert long == {"choices": [["👍", 2.0]], "big": True}
        assert self.action.parse("custom:5368324170671202286")["choices"][0][0].startswith(
            "custom:"
        )

    @pytest.mark.parametrize(
        "config",
        [[], {}, {"👍": 0}, {"👍": "lots"}, {"emoji": "👍", "size": 3}, "custom:abc", 7],
    )
    def test_invalid(self, config):
        with pytest.raises(ActionError):
            self.action.parse(config)


class TestReadAndView:
    def test_read_forms(self):
        read = get_builtin("read")
        assert read.parse(None) == read.parse(True) == read.parse({})
        assert read.parse({"mentions": True})["mentions"] is True
        with pytest.raises(ActionError):
            read.parse(False)
        with pytest.raises(ActionError):
            read.parse({"history": True})

    def test_view_plans_by_peer_and_media(self):
        view = get_builtin("view")
        params = view.parse({})
        assert view.plan(_facts(peer="channel"), params, None, RNG) == [{"mode": "views"}]
        voice = _facts(peer="user", voice_or_round=True)
        assert view.plan(voice, params, None, RNG) == [{"mode": "contents"}]
        assert view.plan(_facts(peer="user"), params, None, RNG) == []
        once = _facts(peer="user", voice_or_round=True, view_once=True)
        assert view.plan(once, params, None, RNG) == []
        brave = view.parse({"include_view_once": True})
        assert view.plan(once, brave, None, RNG) == [{"mode": "contents"}]
