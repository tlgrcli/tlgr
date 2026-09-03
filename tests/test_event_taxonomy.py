"""Every `Update*` constructor is named or explained — and stays that way.

The bus's promise is that an update tlgr receives is either delivered under a
type a consumer can filter on, or is on a list with a reason next to it. That
promise is only worth something if it is checked against the *installed*
Telethon rather than against a list somebody typed once: a Telethon upgrade
that adds a constructor must fail here, in the run that upgrades it.
"""

from __future__ import annotations

import json

import pytest

from tlgr.core import eventtypes
from tlgr.core.errors import UsageError
from tlgr.daemon.events import normalise_update, tl_to_builtins

TELETHON_UPDATES = sorted(
    name
    for name in dir(__import__("telethon.tl.types", fromlist=["x"]))
    if name.startswith("Update")
)


class TestCompleteness:
    def test_every_installed_constructor_is_accounted_for(self):
        """Neither mapped nor internal means an update tlgr silently drops."""
        accounted = set(eventtypes.CONSTRUCTORS) | set(eventtypes.INTERNAL)
        missing = sorted(set(TELETHON_UPDATES) - accounted)
        assert missing == [], (
            f"{len(missing)} update constructors have no event type and no "
            f"reason: {missing}. Add them to tlgr/core/eventtypes.py."
        )

    def test_the_table_names_no_constructor_telethon_does_not_have(self):
        """A typo in the table would map a real update onto nothing."""
        extra = sorted(
            (set(eventtypes.CONSTRUCTORS) | set(eventtypes.INTERNAL)) - set(TELETHON_UPDATES)
        )
        assert extra == []

    def test_the_layer_229_list_is_disjoint_from_the_installed_one(self):
        overlap = sorted(set(eventtypes.NEWER_THAN_LAYER_227) & set(TELETHON_UPDATES))
        assert overlap == [], f"{overlap} are parseable here; drop the since_layer note"

    def test_every_mapped_type_is_declared(self):
        declared = set(eventtypes.TYPES)
        mapped = set(eventtypes.CONSTRUCTORS.values()) | set(
            eventtypes.NEWER_THAN_LAYER_227.values()
        )
        assert sorted(mapped - declared) == []

    def test_every_declared_type_has_a_source_or_says_it_is_derived(self):
        for name, spec in eventtypes.TYPES.items():
            assert eventtypes.constructors_for(name) or spec.derived, (
                f"{name} has no source constructor and does not say where it comes from"
            )

    def test_every_internal_constructor_gives_a_reason(self):
        for name, reason in eventtypes.INTERNAL.items():
            assert len(reason) > 20, f"{name} is listed internal without a real reason"


class TestNames:
    @pytest.mark.parametrize("name", sorted(eventtypes.TYPES))
    def test_a_type_name_is_lowercase_snake_case(self, name):
        assert name == name.lower()
        assert " " not in name and "-" not in name

    @pytest.mark.parametrize("name", sorted(eventtypes.TYPES))
    def test_a_type_belongs_to_a_declared_group(self, name):
        assert eventtypes.TYPES[name].group in eventtypes.GROUPS

    @pytest.mark.parametrize("name", sorted(eventtypes.TYPES))
    def test_a_type_documents_itself(self, name):
        assert eventtypes.TYPES[name].summary

    @pytest.mark.parametrize("name", sorted(eventtypes.TYPES))
    def test_a_box_is_one_of_the_five(self, name):
        assert eventtypes.TYPES[name].box in (
            "pts",
            "qts",
            "seq",
            "channel_pts",
            "version",
            "none",
        )


class TestSelectors:
    def test_a_group_expands_to_its_types(self):
        selected = eventtypes.resolve_selectors("read")
        assert "read_inbox" in selected and "read_outbox" in selected
        assert "message_new" not in selected

    def test_all_is_everything(self):
        assert eventtypes.resolve_selectors("all") == frozenset(eventtypes.TYPES)

    def test_a_v1_name_still_selects(self):
        """§12.4: `--events new_message` was v1's spelling and keeps working."""
        assert eventtypes.resolve_selectors("new_message") == frozenset({"message_new"})
        assert eventtypes.resolve_selectors("message_read") == frozenset(
            {"read_inbox", "read_outbox"}
        )

    def test_a_raw_constructor_selects_its_type(self):
        assert eventtypes.resolve_selectors("raw:UpdateBotStopped") == frozenset({"bot_stopped"})

    def test_an_unknown_selector_is_a_usage_error_not_an_empty_watch(self):
        """Watching nothing looks exactly like a broken daemon."""
        with pytest.raises(UsageError):
            eventtypes.resolve_selectors("messages")

    def test_selectors_combine(self):
        selected = eventtypes.resolve_selectors("message_new,presence")
        assert {"message_new", "user_status", "typing"} <= selected


class TestNormalisation:
    def test_a_container_is_not_an_event(self):
        from telethon.tl import types

        update = types.UpdatesTooLong()
        assert normalise_update("work", update) is None

    def test_a_raw_update_becomes_its_type(self):
        from telethon.tl import types

        update = types.UpdateDeleteChannelMessages(
            channel_id=5150, messages=[4, 5], pts=2, pts_count=2
        )
        kind, payload, chat_id, _sender = normalise_update("work", update)
        assert kind == "message_deleted"
        assert payload["message_ids"] == [4, 5]
        assert chat_id == -1000000005150

    def test_a_read_inbox_and_a_read_outbox_are_different_types(self):
        from telethon.tl import types

        inbox = types.UpdateReadHistoryInbox(
            peer=types.PeerUser(4242), max_id=9, still_unread_count=3, pts=1, pts_count=1
        )
        outbox = types.UpdateReadHistoryOutbox(
            peer=types.PeerUser(4242), max_id=9, pts=1, pts_count=1
        )
        assert normalise_update("work", inbox)[0] == "read_inbox"
        assert normalise_update("work", outbox)[0] == "read_outbox"
        assert normalise_update("work", inbox)[1]["still_unread_count"] == 3

    def test_a_service_message_is_its_own_type(self):
        from telethon.tl import types

        service = types.MessageService(
            id=11,
            peer_id=types.PeerChat(77),
            date=None,
            action=types.MessageActionChatJoinedByLink(inviter_id=4242),
        )
        update = types.UpdateNewMessage(message=service, pts=1, pts_count=1)
        kind, payload, _chat, _sender = normalise_update("work", update)
        assert kind == "message_service"
        assert payload["action"] == "MessageActionChatJoinedByLink"

    def test_a_generic_update_carries_its_own_fields_json_safe(self):
        from telethon.tl import types

        update = types.UpdateChannelMessageViews(channel_id=5150, id=8, views=1200)
        kind, payload, chat_id, _sender = normalise_update("work", update)
        assert kind == "message_views"
        assert payload["views"] == 1200
        assert chat_id == -1000000005150
        json.dumps(payload)

    @pytest.mark.parametrize("name", sorted(eventtypes.CONSTRUCTORS))
    def test_no_payload_carries_a_datetime_or_bytes(self, name):
        """COR-07: a datetime in a payload is a crash at delivery time."""
        import inspect

        from telethon.tl import types

        klass = getattr(types, name)
        update = klass.__new__(klass)
        # Every field present and empty: the shape a half-populated update
        # from an older layer arrives in, and the one that used to crash the
        # serialiser at delivery time rather than here.
        for field in inspect.signature(klass.__init__).parameters:
            if field != "self":
                setattr(update, field, None)
        normalised = normalise_update("work", update)
        assert normalised is not None
        json.dumps(normalised[1])


class TestToBuiltins:
    def test_a_datetime_becomes_rfc_3339(self):
        from datetime import datetime, timezone

        assert tl_to_builtins(datetime(2026, 9, 3, 9, 14, 7, tzinfo=timezone.utc)) == (
            "2026-09-03T09:14:07Z"
        )

    def test_bytes_become_hex(self):
        assert tl_to_builtins(b"\x00\xff") == "00ff"

    def test_a_tl_object_keeps_its_constructor_name(self):
        from telethon.tl import types

        out = tl_to_builtins(types.PeerChannel(5150))
        assert out["_"] == "PeerChannel"
        assert out["channel_id"] == 5150
