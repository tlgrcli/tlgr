"""Telethon → model, and the promise that v1's classification is preserved.

`media_details` is the function these summaries were ported from; the parity
test below is the one that matters, because "the same logic, typed" is a claim
that decays silently unless something checks it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tlgr.core.client import media_details
from tlgr.ops._serialize import (
    entity_to_peer,
    marked_id,
    media_summary,
    message_entities,
    message_to_model,
    reactions_summary,
    service_action,
    tl_snake,
)


class MessageMediaPhoto(SimpleNamespace):
    pass


class MessageMediaDocument(SimpleNamespace):
    pass


class MessageMediaGeoLive(SimpleNamespace):
    pass


class MessageMediaPaidMedia(SimpleNamespace):
    pass


class DocumentAttributeSticker(SimpleNamespace):
    pass


class DocumentAttributeAudio(SimpleNamespace):
    pass


class DocumentAttributeVideo(SimpleNamespace):
    pass


class DocumentAttributeAnimated(SimpleNamespace):
    pass


class DocumentAttributeFilename(SimpleNamespace):
    pass


def doc(*attrs, mime=None, **kwargs):
    return MessageMediaDocument(
        document=SimpleNamespace(mime_type=mime, attributes=list(attrs), **kwargs)
    )


CASES = {
    "photo": MessageMediaPhoto(photo=SimpleNamespace(id=1, sizes=[])),
    "sticker": doc(DocumentAttributeSticker(alt="👍")),
    "voice": doc(DocumentAttributeAudio(voice=True, duration=17)),
    "audio": doc(DocumentAttributeAudio(voice=False, duration=200)),
    "video_note": doc(DocumentAttributeVideo(round_message=True, duration=8)),
    "video": doc(DocumentAttributeVideo(round_message=False, duration=90)),
    "gif": doc(DocumentAttributeVideo(round_message=False), DocumentAttributeAnimated()),
    "file": doc(DocumentAttributeFilename(file_name="a.pdf"), mime="application/pdf"),
}


class TestMediaParity:
    @pytest.mark.parametrize(("expected", "media"), sorted(CASES.items()))
    def test_kind_matches_v1(self, expected, media):
        assert media_details(media)["kind"] == expected
        summary = media_summary(media)
        assert summary is not None and summary.kind == expected

    def test_gif_by_mime_alone(self):
        """A GIF carries Video AND Animated; 'first attribute wins' loses it."""
        media = doc(DocumentAttributeVideo(round_message=False), mime="image/gif")
        assert media_summary(media).kind == "gif"

    def test_video_sticker_is_a_sticker(self):
        media = doc(DocumentAttributeVideo(round_message=False), DocumentAttributeSticker(alt="🙂"))
        assert media_summary(media).kind == "sticker"

    def test_sticker_alt_is_its_content(self):
        assert media_summary(CASES["sticker"]).alt == "👍"

    def test_file_name_and_mime_survive(self):
        summary = media_summary(CASES["file"])
        assert summary.file_name == "a.pdf"
        assert summary.mime_type == "application/pdf"

    def test_tl_type_is_kept_for_debugging(self):
        assert media_summary(CASES["file"]).tl_type == "MessageMediaDocument"

    def test_none_is_none(self):
        assert media_summary(None) is None

    def test_non_document_media_maps_to_a_known_kind(self):
        """v1 lowercased the class suffix, which produced 'geolive'."""
        assert media_summary(MessageMediaGeoLive(geo=SimpleNamespace(lat=1.0, long=2.0))).kind == (
            "geo_live"
        )
        assert media_summary(MessageMediaPaidMedia()).kind == "paid"

    def test_geo_is_flattened(self):
        summary = media_summary(MessageMediaGeoLive(geo=SimpleNamespace(lat=1.5, long=2.5)))
        assert (summary.latitude, summary.longitude) == (1.5, 2.5)


class TestReactions:
    def _msg(self, *results):
        return SimpleNamespace(reactions=SimpleNamespace(results=list(results)))

    def test_none_when_absent(self):
        assert reactions_summary(SimpleNamespace(reactions=None)) is None

    def test_counts_and_mine(self):
        summary = reactions_summary(
            self._msg(
                SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=2, chosen_order=0),
                SimpleNamespace(reaction=SimpleNamespace(emoticon="❤"), count=1, chosen_order=None),
            )
        )
        assert summary.counts == {"👍": 2, "❤": 1}
        assert summary.mine == ["👍"]
        assert summary.total == 3

    def test_custom_reactions_are_named_not_dropped(self):
        summary = reactions_summary(
            self._msg(
                SimpleNamespace(
                    reaction=SimpleNamespace(document_id=545, emoticon=None),
                    count=1,
                    chosen_order=None,
                )
            )
        )
        assert summary.counts == {"custom:545": 1}


class TestPeers:
    def test_user(self):
        user = type("User", (SimpleNamespace,), {})(
            id=777123, first_name="Sara", last_name="N", username="saran", bot=False
        )
        peer = entity_to_peer(user)
        assert (peer.id, peer.raw_id, peer.kind, peer.title) == (777123, 777123, "user", "Sara N")

    def test_channel_id_is_marked(self):
        """COR-10: one id shape, not a raw id here and a marked id there."""
        channel = type("Channel", (SimpleNamespace,), {})(
            id=1234567890, title="News", megagroup=False, username="news"
        )
        peer = entity_to_peer(channel)
        assert peer.id == -1001234567890
        assert peer.raw_id == 1234567890
        assert peer.kind == "channel"

    def test_supergroup_is_distinct_from_channel(self):
        chan = type("Channel", (SimpleNamespace,), {})(id=5, title="G", megagroup=True)
        assert entity_to_peer(chan).kind == "supergroup"

    def test_self_is_saved_messages(self):
        user = type("User", (SimpleNamespace,), {})(id=1, is_self=True, bot=False)
        assert entity_to_peer(user).kind == "saved"

    @pytest.mark.parametrize(
        ("raw", "kind", "expected"),
        [(5, "user", 5), (5, "group", -5), (5, "channel", -1000000000005), (-7, "user", -7)],
    )
    def test_marked_id(self, raw, kind, expected):
        assert marked_id(raw, kind) == expected


class TestMessage:
    def test_service_messages_are_events(self):
        action = type("MessageActionChatAddUser", (SimpleNamespace,), {})(users=[777])
        message = message_to_model(SimpleNamespace(id=1, action=action, date=None), chat_id=-100)
        assert message.kind == "service"
        assert message.action.type == "chat_add_user"
        assert message.action.tl_type == "MessageActionChatAddUser"
        assert message.action.user_ids == [777]

    def test_plain_message(self):
        message = message_to_model(
            SimpleNamespace(id=9, text="hi", out=True, date=None, action=None), chat_id=-100
        )
        assert (message.kind, message.text, message.out) == ("message", "hi", True)

    def test_entities_keep_utf16_offsets(self):
        entity = type("MessageEntityTextUrl", (SimpleNamespace,), {})(
            offset=3, length=4, url="https://x"
        )
        entities = message_entities(SimpleNamespace(entities=[entity]))
        assert entities[0].type == "text_url"
        assert (entities[0].offset, entities[0].length) == (3, 4)
        assert entities[0].url == "https://x"

    def test_service_action_of_none_is_none(self):
        assert service_action(None) is None

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("MessageActionChatAddUser", "message_action_chat_add_user"),
            ("ChatAddUser", "chat_add_user"),
            ("MessageEntityBold", "message_entity_bold"),
        ],
    )
    def test_tl_snake(self, name, expected):
        assert tl_snake(name) == expected

    def test_prefix_is_stripped(self):
        assert tl_snake("MessageEntityBold", "MessageEntity") == "bold"
