"""The wire models: decoding, forward compatibility, and peer-ref parsing."""

from __future__ import annotations

import json

import msgspec
import pytest

from tests.data import examples
from tlgr.models import (
    UNSET,
    Dialog,
    ErrEnvelope,
    ErrorBody,
    EventEnvelope,
    Message,
    Model,
    OkEnvelope,
    Page,
    Peer,
    PeerRef,
    Request,
    Unset,
    encode,
    parse_message_link,
    parse_peer_ref,
    parse_user_ref,
)


class TestConventions:
    def test_defaults_are_omitted(self):
        assert json.loads(encode(Peer(id=1, raw_id=1, kind="user"))) == {
            "id": 1,
            "raw_id": 1,
            "kind": "user",
        }

    def test_models_tolerate_unknown_fields(self):
        m = msgspec.json.decode(
            b'{"id":1,"chat_id":-100,"date":"x","date_unix":0,"invented_later":true}',
            type=Message,
        )
        assert m.id == 1

    def test_requests_reject_unknown_fields(self):
        class Req(Request):
            x: int = 0

        with pytest.raises(msgspec.ValidationError):
            msgspec.json.decode(b'{"x":1,"typo":2}', type=Req)

    def test_unset_is_a_third_state(self):
        class Edit(Model):
            bio: Unset[str | None] = UNSET

        assert json.loads(encode(Edit())) == {}
        assert json.loads(encode(Edit(bio=None))) == {"bio": None}
        assert json.loads(encode(Edit(bio="x"))) == {"bio": "x"}

    def test_list_defaults_are_not_shared(self):
        a, b = Peer(id=1, raw_id=1, kind="user"), Peer(id=2, raw_id=2, kind="user")
        a.usernames.append("x")
        assert b.usernames == []


class TestArchitectureExamples:
    """§3.8 must decode into the models it claims to describe."""

    def test_message_get_envelope(self):
        env = msgspec.json.decode(examples.MESSAGE_GET, type=OkEnvelope)
        msg = msgspec.convert(env.result, type=Message)
        assert env.ok and env.op == "message.get"
        assert msg.date_unix == 1788340447
        assert msg.date.endswith("Z")
        assert msg.sender is not None and msg.sender.username == "saran"
        assert msg.reactions is not None and msg.reactions.mine == ["\U0001f44d"]
        assert msg.reply_to is not None and msg.reply_to.forum_topic is True

    def test_message_list_page(self):
        env = msgspec.json.decode(examples.MESSAGE_LIST, type=OkEnvelope)
        items = msgspec.convert(env.result, type=list[Message])
        assert items[0].media is not None and items[0].media.kind == "voice"
        assert items[1].kind == "service"
        assert items[1].action is not None and items[1].action.type == "chat_add_user"
        assert env.page is not None and env.page.total == 4120

    def test_error_envelope(self):
        env = msgspec.json.decode(examples.ERROR, type=ErrEnvelope)
        assert env.ok is False
        assert env.error.code == "RATE_LIMITED"
        assert env.error.exit_code == 7
        assert env.error.wait_seconds == 42

    def test_event_envelope(self):
        ev = msgspec.json.decode(examples.EVENT, type=EventEnvelope)
        assert ev.seq == 91824
        assert ev.type == "message_new"
        assert ev.self_origin is False

    def test_error_body_roundtrips(self):
        body = ErrorBody(code="USAGE", message="bad", exit_code=2, field="chat.kind")
        assert msgspec.json.decode(encode(body), type=ErrorBody) == body


class TestPage:
    def test_generic_page_decodes(self):
        page = msgspec.json.decode(
            b'{"items":[{"id":1,"chat_id":2,"date":"x","date_unix":0}],"has_more":true}',
            type=Page[Message],
        )
        assert page.items[0].id == 1
        assert page.has_more and page.next_cursor is None

    def test_empty_page_is_the_default(self):
        assert json.loads(encode(Page[Message]())) == {}


class TestDialog:
    def test_dialog_carries_a_trimmed_last_message(self):
        d = msgspec.json.decode(
            b'{"chat":{"id":-100,"raw_id":100,"kind":"channel","title":"T"},'
            b'"unread_count":3,'
            b'"last_message":{"id":9,"chat_id":-100,"date":"x","date_unix":0,"text":"hi"}}',
            type=Dialog,
        )
        assert d.chat.kind == "channel"
        assert d.unread_count == 3
        assert d.last_message is not None and d.last_message.text == "hi"


class TestPeerRef:
    @pytest.mark.parametrize(
        ("text", "kind", "value"),
        [
            ("@Alice", "username", "alice"),
            ("alice", "username", "alice"),
            ("me", "self", "me"),
            ("self", "self", "me"),
            ("saved", "saved", "saved"),
            ("777123", "id", 777123),
            ("-55", "id", -55),
            ("-1001234567890", "id", -1001234567890),
            ("+98 912 000 0000", "phone", "+989120000000"),
            ("t.me/alice", "username", "alice"),
            ("https://t.me/alice", "username", "alice"),
            ("https://telegram.me/s/news", "username", "news"),
            ("https://t.me/c/1234567890/1042", "id", -1001234567890),
            ("t.me/+AbCdEfGh", "invite", "AbCdEfGh"),
            ("t.me/joinchat/AbCdEfGh", "invite", "AbCdEfGh"),
            ("tg://resolve?domain=Bob", "username", "bob"),
            ("tg://join?invite=XyZ", "invite", "XyZ"),
        ],
    )
    def test_parses(self, text, kind, value):
        ref = parse_peer_ref(text)
        assert (ref.kind, ref.value) == (kind, value)
        assert ref.raw == text

    @pytest.mark.parametrize("text", ["", "   ", "0", "@ab", "!!!", "https://example.com/x"])
    def test_rejects(self, text):
        with pytest.raises(ValueError):
            parse_peer_ref(text)

    def test_user_ref_rejects_a_channel_id(self):
        with pytest.raises(ValueError, match="wants a user"):
            parse_user_ref("-1001234567890")

    def test_user_ref_accepts_a_user(self):
        assert parse_user_ref("+989120000000").kind == "phone"

    def test_peer_ref_is_a_wire_shape(self):
        ref = parse_peer_ref("@alice")
        assert json.loads(encode(ref)) == {"raw": "@alice", "kind": "username", "value": "alice"}
        assert msgspec.json.decode(encode(ref), type=PeerRef) == ref

    @pytest.mark.parametrize(
        ("text", "value", "msg_id"),
        [
            ("https://t.me/c/1234567890/1042", -1001234567890, 1042),
            ("t.me/alice/55", "alice", 55),
            ("tg://privatepost?channel=1234567890&post=7", -1001234567890, 7),
        ],
    )
    def test_message_links_split(self, text, value, msg_id):
        parsed = parse_message_link(text)
        assert parsed is not None
        ref, got = parsed
        assert (ref.value, got) == (value, msg_id)

    @pytest.mark.parametrize("text", ["@alice", "t.me/alice", "-100123", "https://t.me/+abc"])
    def test_not_a_message_link(self, text):
        assert parse_message_link(text) is None
