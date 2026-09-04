"""The profile, privacy, notification, settings, business, premium, Stars,
gift and giveaway operations.

Same arrangement as the other group suites: a real Unix socket, the real
middleware chain, the real dispatcher, a fake Telegram. The assertions are
about *the world changing* — a privacy vector that was rewritten, a gift that
left one profile and arrived on another, a notification exception that a
later listing finds — because a canned reply cannot tell a working command
from a command that only looks right.

Four things get more attention than the rest, because they are where this
group can do damage or tell a lie:

* **replace-the-world APIs.** `setPrivacy` and `setGlobalPrivacySettings`
  replace their whole payload; there is a test for each that changes one
  thing and asserts the rest survived.
* **the mute clock.** v1 computed `mute_until` from the event loop's clock;
  there is a test that the timestamp is a real, near-future wall-clock one.
* **money.** There is a test, written against the registry rather than
  against a list of commands, that this group added no verb that spends.
* **absent methods.** Every flag that needs a request class Telethon 1.44
  lacks exits 13, not 1.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
)

ALICE = 4242
BOB = 4343
MYBOT = 5000001
CHANNEL = 1600
CHANNEL_ID = -1000000000000 - CHANNEL
GIFT_ID = 5100


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_world(world):
    """A world with a profile, two peers, a channel and the settings state."""
    from fake_telethon import (
        make_channel,
        make_document,
        make_saved_gift,
        make_star_gift,
        make_unique_gift,
        make_user,
    )
    from telethon.tl import types

    world.me.first_name = "Ada"
    world.me.last_name = "Lovelace"
    world.me.username = "adalovelace"
    world.me.phone = "989123456789"
    world.me.premium = True
    world.add_user(make_user(ALICE, username="alice", first="Alice"))
    world.add_user(make_user(BOB, username="bobby", first="Bob"))
    bot = make_user(MYBOT, username="my_helper_bot", first="Helper")
    bot.bot = True
    world.add_user(bot)
    channel = make_channel(CHANNEL, title="Notes")
    channel.username = "ada_notes"
    world.add_channel(channel)
    world.admined_public.append(CHANNEL)

    world.user_full[int(world.me.id)] = {"about": "counting on it"}

    world.peer_colors = [
        types.help.PeerColorOption(color_id=5, hidden=None, channel_min_level=0, group_min_level=0),
        types.help.PeerColorOption(
            color_id=9,
            colors=types.help.PeerColorSet(colors=[0x3FA3E8]),
            dark_colors=types.help.PeerColorSet(colors=[0x1A5C86]),
            channel_min_level=3,
        ),
    ]
    world.profile_colors = list(world.peer_colors)
    world.emoji_statuses = {
        "recent": [types.EmojiStatus(document_id=5301)],
        "default": [types.EmojiStatus(document_id=5302)],
        "collectible": [],
    }
    world.languages = [
        types.LangPackLanguage(
            name="Persian",
            native_name="فارسی",
            lang_code="fa",
            plural_code="fa",
            strings_count=4000,
            translated_count=4000,
            translations_url="https://translations.telegram.org/fa",
            official=True,
        )
    ]
    world.themes = {
        "Nord": types.Theme(id=991, access_hash=1, slug="Nord", title="Nord", installs_count=42)
    }
    world.ringtones = [
        make_document(
            8811,
            mime="audio/ogg",
            attributes=[types.DocumentAttributeFilename(file_name="chime.ogg")],
        )
    ]
    world.collectible = types.fragment.CollectibleInfo(
        purchase_date=datetime.now(timezone.utc),
        currency="USD",
        amount=1000,
        crypto_currency="TON",
        crypto_amount=5,
        url="https://fragment.com/username/adalovelace",
    )

    # -- business ------------------------------------------------------------
    world.quick_replies = {
        3: {
            "shortcut": "hello",
            "messages": [
                types.Message(
                    id=1,
                    peer_id=types.PeerUser(user_id=world.me.id),
                    date=datetime.now(timezone.utc),
                    message="Hi! I will reply shortly.",
                    out=True,
                )
            ],
        }
    }
    world.quick_reply_order = [3]

    # -- gifts and Stars -----------------------------------------------------
    gift = make_star_gift(GIFT_ID, limited=True)
    world.gift_catalog = [gift, make_star_gift(5200, title="Star", sold_out=True)]
    unique = make_unique_gift(slug="PlushPepe-42", gift_id=GIFT_ID, num=42, resell_stars=12000)
    world.unique_gifts = {unique.slug: unique}
    world.saved_gifts = {
        int(world.me.id): [
            make_saved_gift(gift, msg_id=120, from_id=ALICE),
            make_saved_gift(unique, msg_id=121, from_id=ALICE, convert_stars=0),
        ]
    }
    world.resale_gifts = {GIFT_ID: [unique]}
    world.upgrade_preview = {
        GIFT_ID: [
            types.StarGiftAttributeModel(
                name="Golden",
                document=make_document(9001),
                rarity=types.StarGiftAttributeRarity(permille=5),
            )
        ]
    }
    world.star_balance = 250
    world.star_transactions = [
        types.StarsTransaction(
            id="tx1",
            amount=types.StarsAmount(amount=-25, nanos=0),
            date=datetime.now(timezone.utc),
            peer=types.StarsTransactionPeer(peer=types.PeerUser(user_id=MYBOT)),
            title="Sticker pack",
            gift=True,
        ),
        types.StarsTransaction(
            id="tx2",
            amount=types.StarsAmount(amount=100, nanos=0),
            date=datetime.now(timezone.utc),
            peer=types.StarsTransactionPeerFragment(),
            title="Top-up",
        ),
    ]
    world.subscriptions = [
        types.StarsSubscription(
            id="sub1",
            peer=types.PeerChannel(channel_id=CHANNEL),
            until_date=datetime.now(timezone.utc) + timedelta(days=30),
            pricing=types.StarsSubscriptionPricing(period=2592000, amount=100),
            can_refulfill=True,
        )
    ]
    world.premium_gift_options = [
        types.PremiumGiftCodeOption(users=1, months=3, currency="XTR", amount=1000),
        types.PremiumGiftCodeOption(users=10, months=3, currency="XTR", amount=9000),
    ]
    world.gift_codes = {
        "abcdef": types.payments.CheckedGiftCode(
            date=datetime.now(timezone.utc),
            days=90,
            chats=[],
            users=[],
            from_id=types.PeerChannel(channel_id=CHANNEL),
            to_id=ALICE,
            via_giveaway=True,
            used_date=datetime.now(timezone.utc),
        )
    }
    world.prepaid_giveaways = {
        CHANNEL_ID: [
            types.PrepaidGiveaway(id=77, months=3, quantity=10, date=datetime.now(timezone.utc))
        ]
    }
    world.app_config = {
        "giveaway_countries_max": 10,
        "giveaway_add_peers_max": 10,
        "ringtone_size_max": 300 * 1024,
        "caption_length_limit_default": 1024,
        "caption_length_limit_premium": 2048,
        "channel_wallpaper_level_min": 9,
        "premium_purchase_blocked": True,
        "premium_bot_username": "PremiumBot",
    }
    return world


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    """The result, with a paginated one put back together.

    The daemon splits a `Page` across `result` (the items) and `page` (the
    cursor half), which is the wire shape a caller walks; a test reads
    better with the two halves back in one dict.
    """
    envelope = await call(client, in_thread, op, request, **kwargs)
    if "page" in envelope:
        return {"items": envelope["result"], **envelope["page"]}
    return envelope["result"]


async def fails(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    """Run an op that must fail, and hand back the exception."""
    from tlgr.core.errors import TlgrError

    try:
        await call(client, in_thread, op, request, **kwargs)
    except TlgrError as exc:
        return exc
    raise AssertionError(f"{op} was expected to fail")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


class TestProfileGet:
    async def test_the_bio_comes_from_the_full_user_not_from_an_empty_string(
        self, live_daemon, client, in_thread, settings_world
    ):
        """v1's bug: `get_me()` has no bio, so v1 reported `""` for everyone."""
        profile = await result(client, in_thread, "profile.get")
        assert profile["bio"] == "counting on it"
        assert profile["id"] == settings_world.me.id
        assert profile["first_name"] == "Ada"
        assert profile["username"] == "adalovelace"

    async def test_no_full_skips_the_second_round_trip(
        self, live_daemon, client, in_thread, settings_world
    ):
        profile = await result(client, in_thread, "profile.get", {"full": False})
        assert "bio" not in profile
        assert not settings_world.called("GetFullUserRequest")

    async def test_the_username_vector_marks_the_main_handle(
        self, live_daemon, client, in_thread, settings_world
    ):
        from telethon.tl import types

        settings_world.me.usernames = [
            types.Username(username="parked", active=False),
            types.Username(username="adalovelace", active=True, editable=True),
        ]
        profile = await result(client, in_thread, "profile.get")
        rows = {row["username"]: row for row in profile["usernames"]}
        assert rows["adalovelace"].get("main") is True
        assert rows["parked"].get("main") is not True

    async def test_the_v1_documented_path_still_resolves(self):
        from tlgr.registry import ALIASES

        assert ALIASES["profile.get"] == "profile.get"
        assert ALIASES["profile.update"] == "profile.update"
        assert ALIASES["profile.set"] == "profile.update"


class TestProfileUpdate:
    async def test_it_reports_only_the_fields_it_changed(
        self, live_daemon, client, in_thread, settings_world
    ):
        changed = await result(client, in_thread, "profile.update", {"bio": "now with bees"})
        assert changed["changed"] == ["bio"]
        assert changed["bio"] == "now with bees"
        assert settings_world.user_full[int(settings_world.me.id)]["about"] == "now with bees"

    async def test_an_empty_last_name_clears_it_rather_than_being_ignored(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "profile.update", {"last_name": ""})
        assert settings_world.me.last_name == ""

    async def test_a_birthday_reaches_its_own_rpc(
        self, live_daemon, client, in_thread, settings_world
    ):
        changed = await result(client, in_thread, "profile.update", {"birthday": "10-12-1815"})
        assert changed["birthday"] == "10-12-1815"
        sent = settings_world.called("UpdateBirthdayRequest")[0]
        assert (sent.birthday.day, sent.birthday.month, sent.birthday.year) == (10, 12, 1815)

    async def test_a_nonsense_birthday_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "profile.update", {"birthday": "yesterday"})
        assert error.exit_code == EXIT_USAGE

    async def test_changing_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "profile.update", {})
        assert error.exit_code == EXIT_USAGE

    async def test_none_unlinks_the_personal_channel(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "profile.update", {"channel": "none"})
        sent = settings_world.called("UpdatePersonalChannelRequest")[0]
        assert type(sent.channel).__name__ == "InputChannelEmpty"


class TestProfileUsername:
    async def test_check_writes_nothing(self, live_daemon, client, in_thread, settings_world):
        answer = await result(
            client, in_thread, "profile.username.set", {"name": "newname", "check": True}
        )
        assert answer["available"] is True
        assert not settings_world.called("UpdateUsernameRequest")

    async def test_a_fragment_only_name_is_purchasable_not_taken(
        self, live_daemon, client, in_thread, settings_world
    ):
        from telethon.errors import RPCError

        settings_world.fail_next(
            "CheckUsernameRequest", RPCError(None, "USERNAME_PURCHASE_AVAILABLE", 400)
        )
        answer = await result(
            client, in_thread, "profile.username.set", {"name": "adalovelace", "check": True}
        )
        assert answer["available"] is False
        assert answer["purchasable"] is True

    async def test_toggling_needs_the_name(self, live_daemon, client, in_thread, settings_world):
        error = await fails(client, in_thread, "profile.username.set", {"on": True})
        assert error.exit_code == EXIT_USAGE

    async def test_reorder_sends_the_whole_list(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "profile.username.set", {"order": "adalovelace,parked"})
        assert settings_world.called("ReorderUsernamesRequest")[0].order == [
            "adalovelace",
            "parked",
        ]

    async def test_the_listing_answers_from_the_user(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "profile.username.list")
        assert [row["username"] for row in page["items"]] == ["adalovelace"]


class TestProfilePhoto:
    async def test_the_listing_marks_the_current_avatar(
        self, live_daemon, client, in_thread, settings_world
    ):
        from fake_telethon import make_photo
        from telethon.tl import types

        photo = make_photo(55123)
        settings_world.user_photos[int(settings_world.me.id)] = [photo, make_photo(55122)]
        settings_world.me.photo = types.UserProfilePhoto(photo_id=photo.id, dc_id=2)
        page = await result(client, in_thread, "profile.photo.list")
        assert [row["id"] for row in page["items"]] == [55123, 55122]
        assert page["items"][0]["current"] is True

    async def test_setting_from_a_file_uploads_and_sends_the_raw_request(
        self, live_daemon, client, in_thread, settings_world, tmp_path
    ):
        path = tmp_path / "avatar.jpg"
        path.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
        answer = await result(client, in_thread, "profile.photo.set", {"file": str(path)})
        assert answer["photo_id"]
        sent = settings_world.called("UploadProfilePhotoRequest")[0]
        assert sent.file is not None and sent.video is None

    async def test_an_emoji_avatar_sends_a_markup_and_no_file(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client,
            in_thread,
            "profile.photo.set",
            {"emoji": "5301", "colors": "#FF0000,#00FF00"},
        )
        sent = settings_world.called("UploadProfilePhotoRequest")[0]
        assert sent.file is None
        assert sent.video_emoji_markup.emoji_id == 5301
        assert sent.video_emoji_markup.background_colors == [0xFF0000, 0x00FF00]

    async def test_setting_without_a_source_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "profile.photo.set", {})
        assert error.exit_code == EXIT_USAGE

    async def test_reusing_an_unknown_photo_id_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "profile.photo.set", {"photo_id": "999"})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_deleting_removes_it_from_the_history(
        self, live_daemon, client, in_thread, settings_world
    ):
        from fake_telethon import make_photo

        settings_world.user_photos[int(settings_world.me.id)] = [make_photo(55123)]
        answer = await result(client, in_thread, "profile.photo.delete", {"photo_id": ["55123"]})
        assert answer["deleted"] == 1
        assert settings_world.user_photos[int(settings_world.me.id)] == []

    async def test_deleting_every_photo_when_there_are_none_is_already(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.photo.delete", {"every": True})
        assert answer["already"] is True


class TestProfileMisc:
    async def test_presence_sends_the_inverse_of_online(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.presence.set", {"state": "online"})
        assert answer["online"] is True
        assert settings_world.called("UpdateStatusRequest")[0].offline is False

    async def test_an_unknown_presence_word_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "profile.presence.set", {"state": "away"})
        assert error.exit_code == EXIT_USAGE

    async def test_the_status_lists_carry_the_group_they_came_from(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "profile.status.list", {"recent": True})
        assert [row["group"] for row in page["items"]] == ["recent"]

    async def test_clear_recent_empties_the_list(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "profile.status.list", {"clear_recent": True})
        assert settings_world.recent_statuses_cleared == 1
        assert settings_world.emoji_statuses["recent"] == []

    async def test_a_status_is_set_and_read_back(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.status.set", {"emoji": "5301"})
        assert answer["document_id"] == 5301
        assert settings_world.me.emoji_status.document_id == 5301

    async def test_clearing_the_status_sends_the_empty_constructor(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.status.set", {"clear": True})
        assert answer["cleared"] is True
        sent = settings_world.called("UpdateEmojiStatusRequest")[-1]
        assert type(sent.emoji_status).__name__ == "EmojiStatusEmpty"

    async def test_the_builtin_palettes_are_named_as_such(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "profile.color.list")
        by_id = {row["color_id"]: row for row in page["items"]}
        assert by_id[5]["builtin"] is True
        assert by_id[9].get("builtin") is not True
        assert by_id[9]["colors"] == ["#3FA3E8"]

    async def test_setting_a_palette_sends_the_peer_color(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.color.set", {"color": "5"})
        assert answer["color"] == 5
        assert settings_world.my_color.color == 5

    async def test_a_collectible_palette_is_refused_for_the_profile_colour(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client,
            in_thread,
            "profile.color.set",
            {"color": "collectible:9", "profile": True},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_the_admined_channels_mark_the_one_on_the_profile(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.user_full[int(settings_world.me.id)]["personal_channel_id"] = CHANNEL
        page = await result(client, in_thread, "profile.channel.list")
        assert page["items"][0]["username"] == "ada_notes"
        assert page["items"][0]["current"] is True

    async def test_the_link_is_the_username_form_when_there_is_one(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.link")
        assert answer["link"] == "https://t.me/adalovelace"
        assert answer["resolvable_by_strangers"] is True

    async def test_without_a_username_the_link_only_works_for_people_who_know_me(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.me.username = None
        answer = await result(client, in_thread, "profile.link")
        assert answer["link"].startswith("tg://user?id=")
        assert answer["resolvable_by_strangers"] is False

    async def test_the_collectible_details_come_from_fragment(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "profile.link", {"collectible": True})
        assert answer["collectible"]["currency"] == "USD"
        assert answer["collectible"]["amount"] == 1000

    async def test_the_profile_music_defaults_to_my_own(
        self, live_daemon, client, in_thread, settings_world
    ):
        from fake_telethon import make_document
        from telethon.tl import types

        settings_world.saved_music[int(settings_world.me.id)] = [
            make_document(
                991,
                mime="audio/mpeg",
                attributes=[
                    types.DocumentAttributeAudio(duration=300, title="Nocturne", performer="Chopin")
                ],
            )
        ]
        page = await result(client, in_thread, "profile.music.list")
        assert page["items"][0]["title"] == "Nocturne"


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


class TestPrivacy:
    async def test_a_rule_written_is_a_rule_read_back(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client,
            in_thread,
            "privacy.set",
            {"key": "last-seen", "rule": "contacts", "disallow": "@alice"},
        )
        answer = await result(client, in_thread, "privacy.get", {"key": "last-seen"})
        row = answer["items"][0]
        assert row["base"] == "contacts"
        assert row["deny_users"] == [ALICE]

    async def test_add_allow_keeps_what_was_already_there(
        self, live_daemon, client, in_thread, settings_world
    ):
        """`setPrivacy` replaces the vector; the point of `--add-*` is that a
        script never has to re-state a list it did not mean to touch."""
        await result(
            client,
            in_thread,
            "privacy.set",
            {"key": "phone-number", "rule": "nobody", "allow": "@alice"},
        )
        await result(
            client, in_thread, "privacy.set", {"key": "phone-number", "add_allow": "@bobby"}
        )
        row = (await result(client, in_thread, "privacy.get", {"key": "phone-number"}))["items"][0]
        assert sorted(row["allow_users"]) == sorted([ALICE, BOB])
        assert row["base"] == "nobody"

    async def test_remove_drops_a_peer_from_both_lists(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client,
            in_thread,
            "privacy.set",
            {"key": "forwards", "rule": "contacts", "allow": "@alice", "disallow": "@bobby"},
        )
        await result(client, in_thread, "privacy.set", {"key": "forwards", "remove": "@alice"})
        row = (await result(client, in_thread, "privacy.get", {"key": "forwards"}))["items"][0]
        # An empty list is the default, so `omit_defaults` drops it: absent
        # means "no exceptions", which is exactly what was asked for.
        assert row.get("allow_users", []) == []
        assert row["deny_users"] == [BOB]

    async def test_the_exception_rules_precede_the_base_rule(
        self, live_daemon, client, in_thread, settings_world
    ):
        """The server applies the vector in order, so a broad rule written
        first would decide every case on its own."""
        await result(
            client,
            in_thread,
            "privacy.set",
            {"key": "bio", "rule": "everybody", "disallow": "@alice"},
        )
        sent = settings_world.called("SetPrivacyRequest")[-1]
        names = [type(rule).__name__ for rule in sent.rules]
        assert names.index("InputPrivacyValueDisallowUsers") < names.index(
            "InputPrivacyValueAllowAll"
        )

    async def test_an_unknown_key_lists_the_ones_that_exist(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "privacy.get", {"key": "telepathy"})
        assert error.exit_code == EXIT_USAGE
        assert "last-seen" in str(error)

    async def test_stories_names_the_two_commands_that_own_story_visibility(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "privacy.get", {"key": "stories"})
        assert error.exit_code == EXIT_USAGE
        assert "story blocklist set" in str(error)

    async def test_reading_every_key_answers_one_row_each(
        self, live_daemon, client, in_thread, settings_world
    ):
        from tlgr.ops.privacy import KEYS

        page = await result(client, in_thread, "privacy.get")
        assert len(page["items"]) == len(KEYS)


class TestGlobalPrivacy:
    async def test_one_flag_changes_and_the_others_survive(
        self, live_daemon, client, in_thread, settings_world
    ):
        """`setGlobalPrivacySettings` replaces the whole constructor, so the
        read-modify-write is the only thing standing between one flag and
        silently clearing the rest."""
        await result(client, in_thread, "privacy.global.set", {"hide_read_marks": "on"})
        await result(client, in_thread, "privacy.global.set", {"archive_new_noncontacts": "on"})
        answer = await result(client, in_thread, "privacy.global.get")
        assert answer["hide_read_marks"] is True
        assert answer["archive_and_mute_new_noncontact_peers"] is True

    async def test_the_paid_price_is_carried_through(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "privacy.global.set", {"paid_messages_price": 5})
        assert answer["noncontact_peers_paid_stars"] == 5
        assert "noncontact_peers_paid_stars" in answer["changed"]

    async def test_gift_categories_are_named_not_masked(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "privacy.global.set", {"disallow_gifts": "limited,unique"}
        )
        assert answer["disallowed_gifts"] == ["limited", "unique"]

    async def test_an_unknown_gift_category_lists_the_real_ones(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "privacy.global.set", {"disallow_gifts": "socks"})
        assert error.exit_code == EXIT_USAGE

    async def test_changing_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "privacy.global.set", {})
        assert error.exit_code == EXIT_USAGE


class TestBlocked:
    async def test_blocking_puts_the_peer_on_the_list(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "privacy.blocked.set", {"peer": ["@alice"]})
        assert answer["blocked"] == [ALICE]
        page = await result(client, in_thread, "privacy.blocked.list")
        assert [row["peer"]["id"] for row in page["items"]] == [ALICE]

    async def test_unblocking_reports_the_other_half_of_the_diff(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "privacy.blocked.set", {"peer": ["@alice"]})
        answer = await result(
            client, in_thread, "privacy.blocked.set", {"peer": ["@alice"], "unblock": True}
        )
        assert answer["unblocked"] == [ALICE]
        assert answer["blocked"] == []

    async def test_the_story_list_is_a_separate_one(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client, in_thread, "privacy.blocked.set", {"peer": ["@alice"], "stories": True}
        )
        main = await result(client, in_thread, "privacy.blocked.list")
        stories = await result(client, in_thread, "privacy.blocked.list", {"stories": True})
        assert main["items"] == []
        assert [row["peer"]["id"] for row in stories["items"]] == [ALICE]

    async def test_naming_nobody_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "privacy.blocked.set", {})
        assert error.exit_code == EXIT_USAGE

    async def test_the_paid_message_revenue_reads_back(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.paid_message_revenue = 25
        answer = await result(client, in_thread, "privacy.revenue.get", {"user": "@alice"})
        assert answer["stars_amount"] == 25
        assert answer["user_id"] == ALICE


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


class TestNotify:
    async def test_the_mute_timestamp_is_wall_clock_not_the_event_loop(
        self, live_daemon, client, in_thread, settings_world
    ):
        """v1 computed this from `loop.time()`, whose origin is arbitrary, so
        every "mute for an hour" produced a timestamp in 1970."""
        answer = await result(client, in_thread, "notify.set", {"target": "private", "mute": "2h"})
        sent = settings_world.called("UpdateNotifySettingsRequest")[0]
        assert abs(int(sent.settings.mute_until) - (int(time.time()) + 7200)) < 30
        assert answer["muted"] is True

    async def test_forever_is_the_servers_own_sentinel(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "notify.set", {"target": "groups", "mute": "forever"})
        sent = settings_world.called("UpdateNotifySettingsRequest")[0]
        assert int(sent.settings.mute_until) == 2**31 - 1

    async def test_muting_a_chat_leaves_its_sound_alone(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client, in_thread, "notify.set", {"target": "@alice", "sound": "ringtone:8811"}
        )
        await result(client, in_thread, "notify.set", {"target": "@alice", "mute": "1h"})
        answer = await result(client, in_thread, "notify.get", {"target": "@alice"})
        assert answer["sound"] == "8811"
        assert answer["muted"] is True

    async def test_the_scope_and_the_chat_are_different_targets(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "notify.set", {"target": "private", "mute": "1h"})
        chat = await result(client, in_thread, "notify.get", {"target": "@alice"})
        assert chat.get("muted") is not True

    async def test_contact_joined_is_reported_the_way_a_human_reads_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "notify.set", {"target": "contact-joined", "off": True})
        answer = await result(client, in_thread, "notify.get", {"target": "contact-joined"})
        assert answer["contact_joined"] is False
        assert settings_world.called("SetContactSignUpNotificationRequest")[0].silent is True

    async def test_contact_joined_needs_one_of_the_two_flags(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "notify.set", {"target": "contact-joined"})
        assert error.exit_code == EXIT_USAGE

    async def test_the_reaction_alerts_are_read_modify_written(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client, in_thread, "notify.set", {"target": "reactions", "messages": "contacts"}
        )
        await result(client, in_thread, "notify.set", {"target": "reactions", "stories": "off"})
        answer = await result(client, in_thread, "notify.get", {"target": "reactions"})
        assert answer["messages_from"] == "contacts"
        assert answer["stories_from"] == "off"

    async def test_an_unknown_reaction_audience_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client, in_thread, "notify.set", {"target": "reactions", "messages": "friends"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_changing_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "notify.set", {"target": "private"})
        assert error.exit_code == EXIT_USAGE

    async def test_an_exception_is_listed_and_then_cleared(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "notify.set", {"target": "@alice", "mute": "1h"})
        page = await result(client, in_thread, "notify.exception.list")
        assert [row["chat_id"] for row in page["items"]] == [ALICE]
        assert page["items"][0]["scope"] == "private"

        cleared = await result(client, in_thread, "notify.exception.clear", {"chat": ["@alice"]})
        assert cleared["cleared"] == 1
        assert (await result(client, in_thread, "notify.exception.list"))["items"] == []

    async def test_clearing_nothing_named_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "notify.exception.clear", {})
        assert error.exit_code == EXIT_USAGE

    async def test_reset_drops_every_exception(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "notify.set", {"target": "@alice", "mute": "1h"})
        answer = await result(client, in_thread, "notify.reset")
        assert answer["ok"] is True
        assert settings_world.notify_peers == {}

    async def test_the_ringtone_id_is_what_the_sound_flag_takes(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "notify.ringtone.list")
        assert page["items"][0]["id"] == 8811
        assert page["items"][0]["file_name"] == "chime.ogg"

    async def test_uploading_a_ringtone_saves_it(
        self, live_daemon, client, in_thread, settings_world, tmp_path
    ):
        path = tmp_path / "bell.ogg"
        path.write_bytes(b"OggS" + b"0" * 64)
        answer = await result(client, in_thread, "notify.ringtone.set", {"file": str(path)})
        assert answer["file_name"] == "bell.ogg"
        assert len(settings_world.ringtones) == 2

    async def test_a_ringtone_over_the_server_limit_is_refused_before_upload(
        self, live_daemon, client, in_thread, settings_world, tmp_path
    ):
        settings_world.app_config["ringtone_size_max"] = 16
        path = tmp_path / "big.ogg"
        path.write_bytes(b"0" * 64)
        error = await fails(client, in_thread, "notify.ringtone.set", {"file": str(path)})
        assert error.exit_code == EXIT_USAGE
        assert not settings_world.called("UploadRingtoneRequest")

    async def test_removing_an_unknown_ringtone_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "notify.ringtone.set", {"remove": "999"})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class TestSettings:
    async def test_every_row_says_what_its_setter_accepts(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "settings.get", {"key": "auto-delete"})
        row = answer["items"][0]
        assert row["key"] == "auto-delete"
        assert row["accepts"] == "1d|1w|1m|<duration>|off"

    async def test_a_write_reports_what_it_replaced(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "settings.set", {"key": "auto-delete", "value": ["1w"]}
        )
        assert answer["previous"] == "off"
        assert answer["value"] == "604800s"

    async def test_writing_the_value_that_is_already_there_is_already(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "settings.set", {"key": "auto-delete", "value": ["1w"]})
        answer = await result(
            client, in_thread, "settings.set", {"key": "auto-delete", "value": ["1w"]}
        )
        assert answer["already"] is True

    async def test_an_unknown_key_lists_the_real_ones(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "settings.get", {"key": "telepathy"})
        assert error.exit_code == EXIT_USAGE

    async def test_the_read_only_key_refuses_the_write_with_a_reason(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client, in_thread, "settings.set", {"key": "age-verification", "value": ["on"]}
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_the_browser_exception_lands_in_the_vector_that_means_its_mode(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client,
            in_thread,
            "settings.set",
            {"key": "browser-exception", "value": ["https://example.org/x", "external"]},
        )
        answer = await result(client, in_thread, "settings.get", {"key": "browser-exception"})
        assert answer["items"][0]["value"] == [
            {
                "domain": "example.org",
                "url": "https://example.org/x",
                "title": "example.org",
                "mode": "external",
            }
        ]

    async def test_a_browser_exception_without_a_mode_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client,
            in_thread,
            "settings.set",
            {"key": "browser-exception", "value": ["https://example.org"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_unsetting_the_browser_exception_removes_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client,
            in_thread,
            "settings.set",
            {"key": "browser-exception", "value": ["https://example.org", "in-app"]},
        )
        answer = await result(
            client,
            in_thread,
            "settings.unset",
            {"key": "browser-exception", "value": "https://example.org"},
        )
        assert answer["removed"] == 1
        read = await result(client, in_thread, "settings.get", {"key": "browser-exception"})
        assert read["items"][0]["value"] == []

    async def test_unsetting_an_unknown_key_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "settings.unset", {"key": "colour"})
        assert error.exit_code == EXIT_USAGE

    async def test_reading_every_key_survives_one_of_them_failing(
        self, live_daemon, client, in_thread, settings_world
    ):
        """A single unreadable key must not take the whole screen with it."""
        from telethon.errors import RPCError

        settings_world.fail_next("GetContentSettingsRequest", RPCError(None, "INTERNAL", 500))
        page = await result(client, in_thread, "settings.get")
        assert len(page["items"]) >= 10

    async def test_the_language_list_comes_from_the_server(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "settings.language.list")
        assert page["items"][0]["lang_code"] == "fa"
        assert page["items"][0]["official"] is True

    async def test_an_unknown_language_code_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "settings.language.list", {"code": "xx"})
        assert error.exit_code in (EXIT_NOT_FOUND, EXIT_USAGE)

    async def test_a_theme_is_created_and_then_listed(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "settings.theme.create", {"title": "Dusk"})
        assert answer["title"] == "Dusk"
        page = await result(client, in_thread, "settings.theme.list")
        assert "Dusk" in [row["title"] for row in page["items"]]

    async def test_creating_a_theme_without_a_title_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "settings.theme.create", {})
        assert error.exit_code == EXIT_USAGE

    async def test_installing_a_theme_saves_and_installs_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "settings.theme.install", {"slug": "Nord"})
        assert answer["installed"] is True and answer["saved"] is True
        assert settings_world.installed_theme == "Nord"
        assert settings_world.saved_themes == ["Nord"]

    async def test_removing_a_theme_only_unsaves_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "settings.theme.install", {"slug": "Nord"})
        answer = await result(
            client, in_thread, "settings.theme.install", {"slug": "Nord", "remove": True}
        )
        assert answer["removed"] is True
        assert settings_world.saved_themes == []

    async def test_an_unknown_theme_slug_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "settings.theme.list", {"slug": "Nope"})
        assert error.exit_code in (EXIT_NOT_FOUND, EXIT_USAGE)

    async def test_autosave_writes_the_scope_the_server_names(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "settings.autosave.set", {"scope": "users", "photos": "on"})
        sent = settings_world.called("SaveAutoSaveSettingsRequest")[0]
        assert sent.users is True
        assert sent.settings.photos is True

    async def test_an_unknown_autosave_scope_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "settings.autosave.set", {"scope": "everyone"})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# business
# ---------------------------------------------------------------------------


class TestBusiness:
    async def test_the_overview_reads_what_the_setters_wrote(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client,
            in_thread,
            "business.set",
            {"tz": "Europe/London", "open": ["mon-fri 09:00-18:00"]},
        )
        answer = await result(client, in_thread, "business.get")
        assert answer["work_hours"]["timezone_id"] == "Europe/London"
        assert len(answer["work_hours"]["weekly_open"]) == 5

    async def test_opening_hours_are_merged_and_sorted(
        self, live_daemon, client, in_thread, settings_world
    ):
        """Two lines for the same day is how a human writes one interval; the
        server rejects overlaps, so they are merged before they are sent."""
        await result(
            client,
            in_thread,
            "business.set",
            {"tz": "Europe/London", "open": ["mon 09:00-13:00", "mon 12:00-18:00"]},
        )
        sent = settings_world.called("UpdateBusinessWorkHoursRequest")[0]
        opens = sent.business_work_hours.weekly_open
        assert len(opens) == 1
        assert (opens[0].start_minute, opens[0].end_minute) == (9 * 60, 18 * 60)

    async def test_a_range_that_ends_before_it_starts_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client,
            in_thread,
            "business.set",
            {"tz": "Europe/London", "open": ["mon 22:00-02:00"]},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_opening_hours_need_a_timezone(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.set", {"open": ["mon 09:00-18:00"]})
        assert error.exit_code == EXIT_USAGE

    async def test_a_location_needs_an_address(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.set", {"lat": 51.5, "lon": -0.1})
        assert error.exit_code == EXIT_USAGE

    async def test_clearing_the_hours_sends_no_constructor(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "business.set", {"clear_hours": True})
        assert (
            settings_world.called("UpdateBusinessWorkHoursRequest")[0].business_work_hours is None
        )

    async def test_changing_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.set", {})
        assert error.exit_code == EXIT_USAGE

    async def test_the_greeting_names_a_shortcut_and_lands_on_the_world(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client,
            in_thread,
            "business.message.set",
            {"kind": "greeting", "shortcut": "hello", "new_chats": True},
        )
        assert answer["kind"] == "greeting"
        assert answer["shortcut_id"] == 3
        assert settings_world.business["greeting"].shortcut_id == 3

    async def test_an_unknown_shortcut_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client,
            in_thread,
            "business.message.set",
            {"kind": "greeting", "shortcut": "missing"},
        )
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_omitting_the_shortcut_disables_the_message(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "business.message.set", {"kind": "away"})
        assert answer["enabled"] is False
        assert settings_world.business["away"] is None

    async def test_a_custom_away_schedule_needs_both_ends(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client,
            in_thread,
            "business.message.set",
            {"kind": "away", "shortcut": "hello", "schedule": "custom"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_an_unknown_message_kind_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.message.set", {"kind": "farewell"})
        assert error.exit_code == EXIT_USAGE


class TestQuickReplies:
    async def test_the_listing_carries_the_messages_of_one_shortcut(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "business.reply.list", {"shortcut": "hello"})
        assert page["items"][0]["shortcut"] == "hello"
        assert page["items"][0]["messages"][0]["text"].startswith("Hi!")

    async def test_an_unknown_shortcut_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.reply.list", {"shortcut": "missing"})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_adding_to_a_new_shortcut_checks_it_first(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client, in_thread, "business.reply.add", {"shortcut": "bye", "text": "Goodbye"}
        )
        assert settings_world.called("CheckQuickReplyShortcutRequest")[0].shortcut == "bye"
        sent = settings_world.called("SendMessageRequest")[0]
        assert type(sent.quick_reply_shortcut).__name__ == "InputQuickReplyShortcut"

    async def test_adding_to_an_existing_shortcut_uses_its_id(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client, in_thread, "business.reply.add", {"shortcut": "hello", "text": "Again"}
        )
        sent = settings_world.called("SendMessageRequest")[0]
        assert type(sent.quick_reply_shortcut).__name__ == "InputQuickReplyShortcutId"

    async def test_adding_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.reply.add", {"shortcut": "bye"})
        assert error.exit_code == EXIT_USAGE

    async def test_renaming_a_shortcut_changes_the_world(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(
            client, in_thread, "business.reply.edit", {"shortcut": "hello", "rename": "hi"}
        )
        assert settings_world.quick_replies[3]["shortcut"] == "hi"

    async def test_reorder_wants_every_shortcut(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.reply.edit", {"order": "nope"})
        assert error.exit_code == EXIT_USAGE

    async def test_deleting_one_message_keeps_the_shortcut(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "business.reply.delete", {"shortcut": "hello", "msg_id": [1]}
        )
        assert answer["deleted"] == 1
        assert settings_world.quick_replies[3]["messages"] == []
        assert 3 in settings_world.quick_replies

    async def test_deleting_the_shortcut_removes_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "business.reply.delete", {"shortcut": "hello"})
        assert settings_world.quick_replies == {}

    async def test_sending_a_quick_reply_lands_in_the_chat(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "business.reply.send", {"chat": "@alice", "shortcut": "hello"}
        )
        assert answer["chat_id"] == ALICE
        assert settings_world.history(ALICE)


class TestBusinessLinksAndBots:
    async def test_a_link_is_created_and_listed(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "business.link.set", {"text": "Hi there"})
        assert answer["message"] == "Hi there"
        page = await result(client, in_thread, "business.link.list")
        assert page["items"][0]["message"] == "Hi there"

    async def test_deleting_a_link_needs_its_slug(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.link.set", {"delete": True})
        assert error.exit_code == EXIT_USAGE

    async def test_resolving_a_link_answers_its_prefilled_message(
        self, live_daemon, client, in_thread, settings_world
    ):
        created = await result(client, in_thread, "business.link.set", {"text": "Hi there"})
        page = await result(client, in_thread, "business.link.list", {"slug": created["slug"]})
        assert page["items"][0]["message"] == "Hi there"

    async def test_rights_default_to_none_and_are_granted_by_name(
        self, live_daemon, client, in_thread, settings_world
    ):
        """The security-critical assertion of this group: no flag, no right."""
        await result(
            client,
            in_thread,
            "business.bot.set",
            {"bot": "@my_helper_bot", "reply_to": True, "new_chats": True},
        )
        sent = settings_world.called("UpdateConnectedBotRequest")[0]
        granted = (
            {name for name in sent.rights.__struct_fields__ if getattr(sent.rights, name, None)}
            if hasattr(sent.rights, "__struct_fields__")
            else {
                name
                for name in dir(sent.rights)
                if not name.startswith("_") and getattr(sent.rights, name, None) is True
            }
        )
        assert granted == {"reply"}

    async def test_the_connection_is_listed_after_it_is_made(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "business.bot.set", {"bot": "@my_helper_bot", "read": True})
        page = await result(client, in_thread, "business.bot.list")
        assert page["items"][0]["bot_id"] == MYBOT
        assert page["items"][0]["rights"]["read_messages"] is True

    async def test_disconnecting_removes_it(self, live_daemon, client, in_thread, settings_world):
        await result(client, in_thread, "business.bot.set", {"bot": "@my_helper_bot"})
        await result(
            client, in_thread, "business.bot.set", {"bot": "@my_helper_bot", "disconnect": True}
        )
        assert settings_world.connected_bots == []

    async def test_the_bot_side_connection_needs_a_bot_session(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.bot.list", {"connection": "conn1"})
        assert error.exit_code in (4, EXIT_PERMISSION)

    async def test_pausing_and_removing_are_different_chat_states(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "business.bot.toggle", {"chat": "@alice"})
        assert settings_world.bot_chat_state[ALICE] == "paused"
        await result(client, in_thread, "business.bot.toggle", {"chat": "@alice", "remove": True})
        assert settings_world.bot_chat_state[ALICE] == "removed"

    async def test_the_stars_transfer_prices_and_refuses(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client,
            in_thread,
            "business.stars.transfer",
            {"bot": "@my_helper_bot", "amount": 100},
        )
        assert answer["ok"] is False
        assert "never spends money" in answer["reason"]
        assert not settings_world.called("SendStarsFormRequest")

    async def test_the_transfer_needs_an_amount(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "business.stars.transfer", {"bot": "@my_helper_bot"})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# premium, stars, giveaway
# ---------------------------------------------------------------------------


class TestPremium:
    async def test_the_status_prints_the_deep_link_rather_than_an_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "premium.status")
        assert answer["premium"] is True
        assert answer["premium_bot"] == "PremiumBot"
        assert answer["invoice_link"].startswith("https://t.me/PremiumBot")

    async def test_the_limit_table_pairs_default_with_premium(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "premium.feature.list", {"limits": True})
        rows = {row["name"]: row for row in answer["limits"]}
        assert rows["caption_length"]["default"] == 1024
        assert rows["caption_length"]["premium"] == 2048

    async def test_the_boost_level_table_is_assembled_from_app_config(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "premium.feature.list", {"boost_levels": True})
        assert {"key": "channel_wallpaper_level_min", "level": 9} in answer["boost_levels"]

    async def test_my_boost_slots_are_the_default_listing(
        self, live_daemon, client, in_thread, settings_world
    ):
        from telethon.tl import types

        settings_world.my_boosts = [
            types.MyBoost(slot=1, date=datetime.now(timezone.utc), expires=None)
        ]
        page = await result(client, in_thread, "premium.boost.list")
        assert page["items"][0]["slot"] == 1

    async def test_the_gift_options_hide_the_giveaway_ones_by_default(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "premium.gift.list")
        assert [row["users"] for row in page["items"]] == [1]
        every = await result(client, in_thread, "premium.gift.list", {"single": False})
        assert len(every["items"]) == 2

    async def test_gifting_premium_prices_and_refuses(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "premium.gift.send", {"user": "@alice", "months": 3}
        )
        assert answer["ok"] is False
        assert answer["months"] == 3
        assert not settings_world.called("SendStarsFormRequest")

    async def test_gifting_needs_the_length(self, live_daemon, client, in_thread, settings_world):
        error = await fails(client, in_thread, "premium.gift.send", {"user": "@alice"})
        assert error.exit_code == EXIT_USAGE

    async def test_a_gift_code_reports_months_from_the_wires_days(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "premium.giftcode.get", {"slug": "abcdef"})
        assert answer["days"] == 90
        assert answer["months"] == 3
        assert answer["via_giveaway"] is True

    async def test_an_unknown_code_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "premium.giftcode.get", {"slug": "nope"})
        assert error.exit_code in (EXIT_NOT_FOUND, EXIT_USAGE)

    async def test_redeeming_a_code_applies_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.gift_codes["abcdef"].used_date = None
        await result(client, in_thread, "premium.giftcode.get", {"slug": "abcdef", "redeem": True})
        assert settings_world.applied_codes == ["abcdef"]


class TestStars:
    async def test_the_balance_keeps_its_nanos(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.star_nanos = 500000000
        answer = await result(client, in_thread, "stars.balance.get")
        assert answer["stars"] == 250
        assert answer["nanos"] == 500000000

    async def test_the_ton_balance_is_reported_separately(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.ton_balance = 7
        answer = await result(client, in_thread, "stars.balance.get", {"ton": True})
        assert answer["ton"] == 7
        assert answer["currency"] == "TON"

    async def test_the_ledger_signs_outgoing_amounts(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "stars.transaction.list")
        by_id = {row["id"]: row for row in page["items"]}
        assert by_id["tx1"]["stars"] == -25
        assert by_id["tx1"]["kind"] == "gift"
        assert by_id["tx2"]["peer_kind"] == "fragment"

    async def test_out_filters_to_the_spends(self, live_daemon, client, in_thread, settings_world):
        page = await result(client, in_thread, "stars.transaction.list", {"outbound": True})
        assert [row["id"] for row in page["items"]] == ["tx1"]

    async def test_specific_ids_are_fetched_by_id(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "stars.transaction.list", {"id": "tx2"})
        assert [row["id"] for row in page["items"]] == ["tx2"]
        assert settings_world.called("GetStarsTransactionsByIDRequest")

    async def test_the_subscriptions_carry_the_refulfill_flag(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "stars.subscription.list")
        assert page["items"][0]["id"] == "sub1"
        assert page["items"][0]["can_refulfill"] is True

    async def test_refulfill_reports_the_price_and_refuses(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "stars.subscription.refulfill", {"id": "sub1"})
        assert answer["ok"] is False
        assert answer["can_refulfill"] is True
        assert answer["stars"] == 100
        assert not settings_world.called("FulfillStarsSubscriptionRequest")

    async def test_refulfilling_an_unknown_subscription_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "stars.subscription.refulfill", {"id": "nope"})
        assert error.exit_code == EXIT_USAGE

    async def test_the_rating_reports_the_progress_to_the_next_level(
        self, live_daemon, client, in_thread, settings_world
    ):
        from telethon.tl import types

        settings_world.user_full[int(settings_world.me.id)]["stars_rating"] = types.StarsRating(
            level=3, current_level_stars=1000, stars=1200, next_level_stars=2000
        )
        answer = await result(client, in_thread, "stars.rating.get")
        assert (answer["level"], answer["stars"], answer["next_level_stars"]) == (3, 1200, 2000)

    async def test_the_revenue_reports_the_graph_as_a_token(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "stars.revenue.get", {"chat": "ada_notes"})
        assert answer["withdrawal_enabled"] is True
        assert answer["revenue_graph"]["kind"]

    async def test_the_withdrawal_url_is_printed_and_nothing_moves(
        self, live_daemon, client, in_thread, settings_world, monkeypatch
    ):
        monkeypatch.setenv("TLGR_2FA_PASSWORD", "hunter2")
        answer = await result(
            client, in_thread, "stars.url.get", {"chat": "ada_notes", "amount": 1000}
        )
        assert answer["kind"] == "withdrawal"
        assert answer["url"].startswith("https://fragment.com/")

    async def test_the_ads_url_needs_no_password(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "stars.url.get", {"chat": "ada_notes", "ads": True}
        )
        assert answer["kind"] == "ads"


class TestGiveaway:
    async def test_the_personal_and_public_halves_are_one_answer(
        self, live_daemon, client, in_thread, settings_world
    ):
        from telethon.tl import types

        settings_world.giveaway_info = types.payments.GiveawayInfoResults(
            start_date=datetime.now(timezone.utc),
            finish_date=datetime.now(timezone.utc),
            winners_count=10,
            winner=True,
            gift_code_slug="abcdef",
            activated_count=4,
        )
        answer = await result(
            client, in_thread, "giveaway.get", {"chat": "ada_notes", "msg_id": 42}
        )
        assert answer["state"] == "finished"
        assert answer["winner"] is True
        assert answer["gift_code_slug"] == "abcdef"

    async def test_a_country_refusal_is_named(self, live_daemon, client, in_thread, settings_world):
        from telethon.tl import types

        settings_world.giveaway_info = types.payments.GiveawayInfo(
            start_date=datetime.now(timezone.utc), disallowed_country="NL"
        )
        answer = await result(
            client, in_thread, "giveaway.get", {"chat": "ada_notes", "msg_id": 42}
        )
        assert answer["disallowed_reason"] == "disallowed-country"

    async def test_joining_is_boosting(self, live_daemon, client, in_thread, settings_world):
        from telethon.tl import types

        settings_world.my_boosts = [
            types.MyBoost(slot=1, date=datetime.now(timezone.utc), expires=None)
        ]
        answer = await result(client, in_thread, "giveaway.join", {"chat": "ada_notes"})
        assert answer["chat_id"] == CHANNEL_ID
        assert settings_world.called("ApplyBoostRequest")

    async def test_the_prepaid_list_comes_from_the_boost_status(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "giveaway.list", {"chat": "ada_notes"})
        assert page["items"][0]["id"] == 77
        assert page["items"][0]["quantity"] == 10

    async def test_listing_without_a_channel_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "giveaway.list", {})
        assert error.exit_code == EXIT_USAGE

    async def test_launching_a_prepaid_giveaway_spends_nothing(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client,
            in_thread,
            "giveaway.start",
            {"chat": "ada_notes", "prepaid_id": 77, "winners": 10},
        )
        assert answer["prepaid_id"] == 77
        assert settings_world.launched_giveaways == [77]
        assert not settings_world.called("SendStarsFormRequest")

    async def test_too_many_countries_is_refused_before_the_call(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.app_config["giveaway_countries_max"] = 1
        error = await fails(
            client,
            in_thread,
            "giveaway.start",
            {"chat": "ada_notes", "prepaid_id": 77, "countries": "NL,GB"},
        )
        assert error.exit_code == EXIT_USAGE
        assert not settings_world.called("LaunchPrepaidGiveawayRequest")

    async def test_checking_a_code_names_the_winner(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "giveaway.code.check", {"slug": "abcdef"})
        assert answer["to_id"] == ALICE

    async def test_applying_a_used_code_is_already(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "giveaway.code.apply", {"slug": "abcdef"})
        assert answer["already"] is True
        assert settings_world.applied_codes == []

    async def test_applying_a_fresh_code_activates_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.gift_codes["abcdef"].used_date = None
        answer = await result(client, in_thread, "giveaway.code.apply", {"slug": "abcdef"})
        assert answer["applied"] is True
        assert settings_world.applied_codes == ["abcdef"]


# ---------------------------------------------------------------------------
# gift
# ---------------------------------------------------------------------------


class TestGift:
    async def test_the_catalogue_reads_and_filters(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "gift.catalog", {"available": True})
        assert [row["gift_id"] for row in page["items"]] == [GIFT_ID]

    async def test_the_can_send_annotation_refuses_with_the_method_it_needs(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.catalog", {"until": "@alice"})
        assert error.exit_code == EXIT_INDETERMINATE
        assert "canSendStarGift" in str(error)

    async def test_a_gift_reference_round_trips(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "gift.list")
        refs = [row["ref"] for row in page["items"]]
        assert refs == ["msg:120", "msg:121"]
        one = await result(client, in_thread, "gift.get", {"ref": "msg:120"})
        assert one["ref"] == "msg:120"
        assert one["convert_stars"] == 250

    async def test_a_slug_reference_resolves_the_collectible(
        self, live_daemon, client, in_thread, settings_world
    ):
        one = await result(client, in_thread, "gift.get", {"ref": "PlushPepe-42"})
        assert one["kind"] == "collectible"
        assert one["num"] == 42

    async def test_a_t_me_nft_link_is_reduced_to_its_slug(
        self, live_daemon, client, in_thread, settings_world
    ):
        one = await result(
            client, in_thread, "gift.unique.get", {"slug": "https://t.me/nft/PlushPepe-42"}
        )
        assert one["slug"] == "PlushPepe-42"
        assert one["link"] == "https://t.me/nft/PlushPepe-42"

    async def test_an_unknown_reference_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.get", {"ref": "msg:999"})
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_a_malformed_reference_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.get", {"ref": "msg:abc"})
        assert error.exit_code == EXIT_USAGE

    async def test_hiding_a_gift_takes_it_off_the_profile(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "gift.set", {"ref": ["msg:120"], "unsave": True})
        assert answer["displayed"] is False
        page = await result(client, in_thread, "gift.list", {"exclude_unsaved": True})
        assert [row["ref"] for row in page["items"]] == ["msg:121"]

    async def test_pinning_adds_to_the_set_rather_than_replacing_it(
        self, live_daemon, client, in_thread, settings_world
    ):
        await result(client, in_thread, "gift.set", {"ref": ["msg:120"], "pin": True})
        await result(client, in_thread, "gift.set", {"ref": ["msg:121"], "pin": True})
        page = await result(client, in_thread, "gift.list")
        assert [row["ref"] for row in page["items"] if row.get("pinned")] == [
            "msg:120",
            "msg:121",
        ]

    async def test_wearing_needs_a_collectible(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.set", {"ref": ["msg:120"], "wear": True})
        assert error.exit_code == EXIT_USAGE

    async def test_wearing_a_collectible_sets_the_emoji_status(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "gift.set", {"ref": ["msg:121"], "wear": True})
        assert answer["worn"] is True
        assert type(settings_world.me.emoji_status).__name__ == "InputEmojiStatusCollectible"

    async def test_asking_for_no_change_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.set", {"ref": ["msg:120"]})
        assert error.exit_code == EXIT_USAGE

    async def test_converting_credits_stars_and_destroys_the_gift(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "gift.convert", {"ref": "msg:120"})
        assert answer["stars_received"] == 250
        assert answer["balance_after"] == 500
        page = await result(client, in_thread, "gift.list")
        assert [row["ref"] for row in page["items"]] == ["msg:121"]

    async def test_a_prepaid_upgrade_is_performed(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(client, in_thread, "gift.upgrade", {"ref": "msg:120"})
        assert answer["upgraded"] is True
        assert answer["slug"]

    async def test_an_unpaid_upgrade_is_priced_and_refused(
        self, live_daemon, client, in_thread, settings_world
    ):
        row = settings_world.saved_gifts[int(settings_world.me.id)][0]
        row.upgrade_stars = None
        row.can_upgrade = None
        answer = await result(client, in_thread, "gift.upgrade", {"ref": "msg:120"})
        assert answer["upgraded"] is False
        assert "never spends money" in answer["refused_reason"]
        assert not settings_world.called("UpgradeStarGiftRequest")

    async def test_a_free_transfer_moves_the_gift(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "gift.transfer", {"ref": "msg:121", "chat": "@alice"}
        )
        assert answer["transferred"] is True
        assert settings_world.saved_gifts[ALICE]

    async def test_a_paid_transfer_is_priced_and_refused(
        self, live_daemon, client, in_thread, settings_world
    ):
        settings_world.saved_gifts[int(settings_world.me.id)][1].transfer_stars = 50
        answer = await result(
            client, in_thread, "gift.transfer", {"ref": "msg:121", "chat": "@alice"}
        )
        assert answer["transferred"] is False
        assert answer["price_stars"] == 50
        assert not settings_world.called("TransferStarGiftRequest")

    async def test_crafting_burns_every_input(self, live_daemon, client, in_thread, settings_world):
        answer = await result(client, in_thread, "gift.craft", {"ref": ["msg:120", "msg:121"]})
        assert answer["crafted"] is True
        assert answer["burned"] == ["msg:120", "msg:121"]
        page = await result(client, in_thread, "gift.list")
        assert page["items"] == []

    async def test_the_craft_candidates_flag_names_the_method_it_needs(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.craft", {"candidates": GIFT_ID})
        assert error.exit_code == EXIT_INDETERMINATE
        assert "getStarGiftCraftCandidates" in str(error)

    async def test_crafting_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.craft", {})
        assert error.exit_code == EXIT_USAGE

    async def test_the_marketplace_reads_prices(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "gift.resale.list", {"gift_id": GIFT_ID})
        assert page["items"][0]["price_stars"] == 12000

    async def test_a_malformed_attribute_filter_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client, in_thread, "gift.resale.list", {"gift_id": GIFT_ID, "attr": ["colour=red"]}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_listing_my_collectible_sets_a_price(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "gift.resale.set", {"ref": "msg:121", "stars": 9000}
        )
        assert answer["listed"] is True
        sent = settings_world.called("UpdateStarGiftPriceRequest")[0]
        assert sent.resell_amount.amount == 9000

    async def test_listing_without_a_price_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.resale.set", {"ref": "msg:121"})
        assert error.exit_code == EXIT_USAGE

    async def test_declining_an_offer_is_performed(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client,
            in_thread,
            "gift.offer.approve",
            {"chat": "@alice", "msg_id": 512, "deny": True},
        )
        assert answer["state"] == "declined"
        assert settings_world.gift_offers == [512]

    async def test_accepting_an_offer_is_refused_because_it_sells_an_asset(
        self, live_daemon, client, in_thread, settings_world
    ):
        answer = await result(
            client, in_thread, "gift.offer.approve", {"chat": "@alice", "msg_id": 512}
        )
        assert answer["state"] == "refused"
        assert settings_world.gift_offers == []

    async def test_the_upgrade_preview_reports_rarity(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(
            client, in_thread, "gift.variant.list", {"gift_id": GIFT_ID, "preview": True}
        )
        assert page["items"][0]["rarity_permille"] == 5
        assert page["items"][0]["kind"] == "model"

    async def test_the_craft_only_filter_names_the_method_it_needs(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client, in_thread, "gift.variant.list", {"gift_id": GIFT_ID, "craft_only": True}
        )
        assert error.exit_code == EXIT_INDETERMINATE

    async def test_the_value_flag_warns_about_what_is_missing(
        self, live_daemon, client, in_thread, settings_world
    ):
        envelope = await call(
            client, in_thread, "gift.unique.get", {"slug": "PlushPepe-42", "value": True}
        )
        assert envelope["result"]["value_stars"] == 15000
        assert any("getStarGiftValueInfo" in w for w in envelope["meta"].get("warnings", []))


class TestGiftCollections:
    async def test_a_collection_is_created_listed_edited_and_deleted(
        self, live_daemon, client, in_thread, settings_world
    ):
        created = await result(
            client,
            in_thread,
            "gift.collection.create",
            {"chat": "me", "title": "Favourites", "refs": ["msg:120"]},
        )
        assert created["title"] == "Favourites"

        page = await result(client, in_thread, "gift.collection.list")
        assert [row["title"] for row in page["items"]] == ["Favourites"]

        edited = await result(
            client,
            in_thread,
            "gift.collection.edit",
            {"chat": "me", "id": created["id"], "title": "Best"},
        )
        assert edited["title"] == "Best"

        await result(
            client, in_thread, "gift.collection.delete", {"chat": "me", "id": created["id"]}
        )
        assert (await result(client, in_thread, "gift.collection.list"))["items"] == []

    async def test_editing_an_unknown_collection_is_not_found(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client, in_thread, "gift.collection.edit", {"chat": "me", "id": 99, "title": "x"}
        )
        assert error.exit_code in (EXIT_NOT_FOUND, EXIT_USAGE)

    async def test_reordering_needs_the_collection_ids(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(
            client,
            in_thread,
            "gift.collection.edit",
            {"chat": "me", "order_collections": "nope"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_editing_without_an_id_is_a_usage_error(
        self, live_daemon, client, in_thread, settings_world
    ):
        error = await fails(client, in_thread, "gift.collection.edit", {"chat": "me"})
        assert error.exit_code == EXIT_USAGE


class TestGiftAuctions:
    @pytest.fixture
    def auction_world(self, settings_world):
        from telethon.tl import types

        gift = settings_world.unique_gifts["PlushPepe-42"]
        state = types.StarGiftAuctionState(
            version=3,
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(hours=1),
            min_bid_amount=5500,
            bid_levels=[
                types.AuctionBidLevel(pos=1, amount=9000, date=None),
                types.AuctionBidLevel(pos=2, amount=3000, date=None),
            ],
            top_bidders=[],
            next_round_at=0,
            last_gift_num=1,
            gifts_left=1,
            current_round=1,
            total_rounds=1,
            rounds=[],
        )
        user = types.StarGiftAuctionUserState(acquired_count=0, bid_amount=5000)
        settings_world.auctions = [
            types.StarGiftActiveAuctionState(gift=gift, state=state, user_state=user)
        ]
        settings_world.auction_states = [
            types.payments.StarGiftAuctionState(
                gift=gift, state=state, user_state=user, timeout=30, users=[], chats=[]
            )
        ]
        return settings_world

    async def test_the_active_list_flattens_the_three_nested_structures(
        self, live_daemon, client, in_thread, auction_world
    ):
        page = await result(client, in_thread, "gift.auction.list")
        row = page["items"][0]
        assert row["slug"] == "PlushPepe-42"
        assert row["my_bid"] == 5000
        assert row["min_bid"] == 5500
        assert row["state"] == "active"

    async def test_the_won_listing_needs_a_gift_id(
        self, live_daemon, client, in_thread, auction_world
    ):
        error = await fails(client, in_thread, "gift.auction.list", {"won": True})
        assert error.exit_code == EXIT_USAGE

    async def test_the_state_stream_estimates_my_position(
        self, live_daemon, client, in_thread, auction_world
    ):
        frames = await in_thread(
            lambda: list(
                client.op_stream(
                    "gift.auction.get",
                    {"auction": "PlushPepe-42", "with_position": True},
                    account="work",
                )
            )
        )
        rows = [f["data"] for f in frames if f.get("type") == "item"]
        assert rows
        state = rows[0]
        assert state["version"] == 3
        # One bid above mine, so I am second.
        assert state["position"] == 2


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


GROUPS = (
    "profile",
    "privacy",
    "notify",
    "settings",
    "business",
    "premium",
    "stars",
    "gift",
    "giveaway",
)


def _specs():
    import tlgr.ops  # noqa: F401
    from tlgr.registry import REGISTRY

    return [spec for op_id, spec in REGISTRY.items() if op_id.split(".")[0] in GROUPS]


class TestTheSurface:
    def test_the_group_registered_every_operation_the_work_list_names(self):
        assert len(_specs()) == 90

    def test_no_operation_in_this_group_signs_a_payment_form(self):
        """Written against the source rather than against a list of commands,
        so a future addition cannot quietly re-open the door PR-10 closed."""
        import inspect

        forbidden = (
            "SendStarsFormRequest",
            "SendPaymentFormRequest",
            "ValidateRequestedInfoRequest",
            "FulfillStarsSubscriptionRequest",
        )
        for spec in _specs():
            try:
                source = inspect.getsource(spec.impl)
            except (OSError, TypeError):  # pragma: no cover - every impl has source
                continue
            for name in forbidden:
                assert name not in source, f"{spec.id} calls {name}"

    def test_every_destructive_operation_is_also_mutating(self):
        for spec in _specs():
            if spec.destructive:
                assert spec.mutating, spec.id

    def test_the_operations_that_change_what_others_see_say_so(self):
        """`visible-to-others` is what an agent filters on before acting."""
        wanted = {
            "profile.update",
            "profile.photo.set",
            "profile.presence.set",
            "profile.status.set",
            "business.bot.set",
            "gift.transfer",
        }
        tagged = {spec.id for spec in _specs() if "visible-to-others" in spec.tags}
        assert wanted <= tagged

    @pytest.mark.parametrize(
        "op_id",
        sorted(spec.id for spec in _specs() if spec.mutating),
    )
    def test_a_dry_run_never_reaches_a_mutating_implementation(self, op_id, tlgr_home):
        """The short-circuit lives above every implementation (COR-17)."""
        import shlex

        from click.testing import CliRunner

        from tlgr.cli import cli
        from tlgr.registry import get

        spec = get(op_id)
        outcome = CliRunner().invoke(
            cli, [*shlex.split(spec.example_args), "--dry-run", "--yes", "--json"]
        )
        assert outcome.exit_code == 0, outcome.output
        assert '"dry_run": true' in outcome.output


class TestPagination:
    async def test_the_gift_catalogue_pages_with_a_signed_cursor(
        self, live_daemon, client, in_thread, settings_world
    ):
        first = await result(client, in_thread, "gift.catalog", limit=1)
        assert first["has_more"] is True
        assert first["next_cursor"]
        second = await result(
            client, in_thread, "gift.catalog", limit=1, cursor=first["next_cursor"]
        )
        assert [row["gift_id"] for row in second["items"]] == [5200]

    async def test_a_cursor_from_another_operation_is_refused(
        self, live_daemon, client, in_thread, settings_world
    ):
        first = await result(client, in_thread, "gift.catalog", limit=1)
        error = await fails(
            client,
            in_thread,
            "gift.variant.list",
            {"gift_id": GIFT_ID},
            cursor=first["next_cursor"],
        )
        assert error.exit_code == EXIT_USAGE

    async def test_the_variant_listing_pages_too(
        self, live_daemon, client, in_thread, settings_world
    ):
        page = await result(client, in_thread, "gift.variant.list", {"gift_id": GIFT_ID}, limit=1)
        assert len(page["items"]) == 1
        assert page["has_more"] is False
