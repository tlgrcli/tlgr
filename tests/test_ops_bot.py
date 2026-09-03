"""The bot, inline, mini-app and payment operations.

Same arrangement as the other group suites: a real Unix socket, the real
middleware chain, the real dispatcher, a fake Telegram. The assertions are
about *the world changing* — an allow-list that grew, a gallery that
reordered, a subscription that is now cancelled — and, where the exact TL
request is the whole point (the `@botusername` suffix in a group, the DC an
inline edit is routed to, the consent flag a button will not be pressed
without), about the request the fake recorded.

Three things get more attention than the rest, because they are where this
group can do damage:

* **consent.** Four button kinds disclose something the user owns, and each
  has a test that presses without the flag and asserts nothing was sent.
* **payment.** There is a test that the surface contains no verb that spends
  money, written against the registry rather than against a list of commands.
* **layer gaps.** Every operation registered-and-refused exits 13, not 1.
"""

from __future__ import annotations

from typing import Any

import pytest

from tlgr.core.errors import (
    EXIT_AUTH,
    EXIT_INDETERMINATE,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_USAGE,
)

ALICE = 4242
HELPER = 5000001
GIFBOT = 93372553
GROUP = 5150
GROUP_ID = -1000000000000 - GROUP


@pytest.fixture
def bots(world):
    """A world with one bot I own, one public bot, a user and a group."""
    from fake_telethon import make_channel, make_user

    alice = make_user(ALICE, username="alice")
    world.add_user(alice)

    helper = make_user(HELPER, username="my_helper_bot", first="Helper")
    helper.bot = True
    helper.bot_can_edit = True
    helper.bot_has_main_app = True
    helper.bot_info_version = 3
    helper.bot_active_users = 12
    world.add_user(helper)
    world.bots[HELPER] = {
        "bot": True,
        "about": "I help",
        "description": "A helper bot",
        "commands": [("start", "Start the bot"), ("help", "Show help")],
        "menu_button": None,
    }
    world.admined_bots.append(HELPER)

    gif = make_user(GIFBOT, username="gifbot", first="GIF")
    gif.bot = True
    world.add_user(gif)
    world.bots[GIFBOT] = {"bot": True, "about": "Send GIFs inline"}

    world.add_channel(make_channel(GROUP, title="Team", megagroup=True))
    return world


@pytest.fixture
def bot_session(bots):
    """The account itself is a bot, which is what the bot-only ops require."""
    bots.me.bot = True
    return bots


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    envelope = await call(client, in_thread, op, request, **kwargs)
    return envelope["result"]


async def fails(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    """Run an op that must fail, and hand back the exception."""
    from tlgr.core.errors import TlgrError

    try:
        await call(client, in_thread, op, request, **kwargs)
    except TlgrError as exc:
        return exc
    raise AssertionError(f"{op} was expected to fail")


def keyboard(*rows: list[dict[str, Any]]) -> Any:
    """A `ReplyInlineMarkup` from a compact description."""
    from telethon.tl import types

    built = []
    for row in rows:
        buttons = []
        for entry in row:
            kind = entry.get("type", "callback")
            text = entry["text"]
            if kind == "callback":
                buttons.append(
                    types.KeyboardButtonCallback(
                        text=text,
                        data=entry.get("data", b"cb"),
                        requires_password=entry.get("requires_password"),
                    )
                )
            elif kind == "url":
                buttons.append(types.KeyboardButtonUrl(text=text, url=entry["url"]))
            elif kind == "copy":
                buttons.append(types.KeyboardButtonCopy(text=text, copy_text=entry["copy_text"]))
            elif kind == "buy":
                buttons.append(types.KeyboardButtonBuy(text=text))
            elif kind == "request_phone":
                buttons.append(types.KeyboardButtonRequestPhone(text=text))
            elif kind == "request_geo":
                buttons.append(types.KeyboardButtonRequestGeoLocation(text=text))
            elif kind == "request_poll":
                buttons.append(types.KeyboardButtonRequestPoll(text=text))
            elif kind == "request_peer":
                buttons.append(
                    types.KeyboardButtonRequestPeer(
                        text=text,
                        button_id=7,
                        peer_type=types.RequestPeerTypeUser(),
                        max_quantity=1,
                    )
                )
            elif kind == "switch_inline":
                buttons.append(
                    types.KeyboardButtonSwitchInline(text=text, query=entry.get("query", ""))
                )
            elif kind == "webview":
                buttons.append(types.KeyboardButtonWebView(text=text, url=entry["url"]))
            elif kind == "url_auth":
                buttons.append(
                    types.KeyboardButtonUrlAuth(text=text, url=entry["url"], button_id=3)
                )
            elif kind == "game":
                buttons.append(types.KeyboardButtonGame(text=text))
            else:  # plain reply-keyboard text button
                buttons.append(types.KeyboardButton(text=text))
        built.append(types.KeyboardButtonRow(buttons=buttons))
    return types.ReplyInlineMarkup(rows=built)


def with_buttons(world, chat_id: int, markup: Any, message_id: int = 700) -> Any:
    message = world.add_message(chat_id, "pick one", message_id=message_id, sender_id=HELPER)
    message.reply_markup = markup
    message.via_bot_id = HELPER
    return message


# ---------------------------------------------------------------------------
# bot get / list / id
# ---------------------------------------------------------------------------


class TestBotGet:
    async def test_the_card_carries_the_bot_info_and_the_user_flags(
        self, live_daemon, client, in_thread, bots
    ):
        card = await result(client, in_thread, "bot.get", {"bot": "@my_helper_bot"})
        assert card["id"] == HELPER
        assert card["about"] == "I help"
        assert card["description"] == "A helper bot"
        assert [c["command"] for c in card["commands"]] == ["start", "help"]
        assert card["bot_can_edit"] is True
        # bot_info_version is the ONLY invalidation signal for the card.
        assert card["bot_info_version"] == 3

    async def test_help_and_settings_are_reported_from_the_command_list(
        self, live_daemon, client, in_thread, bots
    ):
        card = await result(client, in_thread, "bot.get", {"bot": "@my_helper_bot"})
        assert card["commands"][0]["has_help"] is True
        # The bot declares no /settings, so the GUI must not offer the entry.
        assert all(not c.get("has_settings") for c in card["commands"])

    async def test_refresh_re_resolves_the_username_first(
        self, live_daemon, client, in_thread, bots
    ):
        await result(client, in_thread, "bot.get", {"bot": "@my_helper_bot", "refresh": True})
        assert bots.called("ResolveUsernameRequest")

    async def test_a_language_asks_the_owner_side_for_the_localised_text(
        self, live_daemon, client, in_thread, bots
    ):
        bots.bots[HELPER]["localized"] = {"de": {"about": "Ich helfe", "description": "Hilfe"}}
        card = await result(client, in_thread, "bot.get", {"bot": "@my_helper_bot", "lang": "de"})
        assert card["about"] == "Ich helfe"
        assert bots.called("GetBotInfoRequest")[0].lang_code == "de"

    async def test_access_settings_need_a_bot_you_administer(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(client, in_thread, "bot.get", {"bot": "@gifbot", "access": True})
        assert error.exit_code == EXIT_PERMISSION

    async def test_an_unknown_username_is_not_found(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "bot.get", {"bot": "@nobodyhere"})
        assert error.exit_code == EXIT_NOT_FOUND


class TestBotList:
    async def test_owned_bots_are_the_default(self, live_daemon, client, in_thread, bots):
        page = await call(client, in_thread, "bot.list", {})
        assert [row["id"] for row in page["result"]] == [HELPER]
        assert page["result"][0]["kind"] == "owned"

    async def test_similar_bots_report_the_truncation_a_non_premium_account_gets(
        self, live_daemon, client, in_thread, bots
    ):
        bots.similar_bots = [GIFBOT]
        bots.similar_bots_count = 40
        page = await call(client, in_thread, "bot.list", {"similar_to": "@my_helper_bot"})
        assert page["result"][0]["truncated_count"] == 40

    async def test_popular_apps_page_by_the_servers_string_offset(
        self, live_daemon, client, in_thread, bots
    ):
        bots.popular_apps = [HELPER]
        bots.popular_apps_next = "page2"
        page = await call(client, in_thread, "bot.list", {"popular_apps": True})
        assert page["page"]["has_more"] is True
        cursor = page["page"]["next_cursor"]
        assert cursor
        await call(client, in_thread, "bot.list", {"popular_apps": True}, cursor=cursor)
        assert bots.called("GetPopularAppBotsRequest")[-1].offset == "page2"

    async def test_recent_bots_report_the_feature_being_off_rather_than_an_empty_truth(
        self, live_daemon, client, in_thread, bots
    ):
        bots.top_peers_enabled = False
        envelope = await call(client, in_thread, "bot.list", {"recent": True})
        assert envelope["result"] == []
        assert any("switched off" in w for w in envelope["meta"]["warnings"])

    async def test_recent_bots_come_back_with_their_rating(
        self, live_daemon, client, in_thread, bots
    ):
        bots.top_peer_bots = [GIFBOT]
        page = await call(client, in_thread, "bot.list", {"recent": True})
        assert page["result"][0]["rating"] == 1.0


class TestBotId:
    async def test_a_channel_id_is_the_same_number_in_both_dialects(
        self, live_daemon, client, in_thread, bots
    ):
        ids = await result(client, in_thread, "bot.id.get", {"chat": str(GROUP_ID)})
        assert ids["mtproto_id"] == GROUP_ID
        assert ids["bot_api_id"] == GROUP_ID
        assert ids["kind"] == "channel"

    async def test_resolving_a_username_reports_whether_a_hash_is_cached(
        self, live_daemon, client, in_thread, bots
    ):
        ids = await result(client, in_thread, "bot.id.get", {"chat": "@alice"})
        assert ids["mtproto_id"] == ALICE
        assert ids["kind"] == "user"
        assert ids["has_access_hash"] is True


# ---------------------------------------------------------------------------
# bot start / stop
# ---------------------------------------------------------------------------


class TestBotStart:
    async def test_a_hidden_start_parameter_goes_through_start_bot(
        self, live_daemon, client, in_thread, bots
    ):
        started = await result(
            client, in_thread, "bot.start", {"bot": "@my_helper_bot", "param": "ref123"}
        )
        assert started["bot_id"] == HELPER
        request = bots.called("StartBotRequest")[0]
        assert request.start_param == "ref123"

    async def test_a_referrer_re_resolves_the_username_with_the_referral(
        self, live_daemon, client, in_thread, bots
    ):
        await result(client, in_thread, "bot.start", {"bot": "@my_helper_bot", "referrer": "aff9"})
        assert bots.called("ResolveUsernameRequest")[0].referer == "aff9"

    async def test_restart_unblocks_first(self, live_daemon, client, in_thread, bots):
        bots.bots[HELPER]["blocked"] = True
        started = await result(
            client, in_thread, "bot.start", {"bot": "@my_helper_bot", "restart": True}
        )
        assert started["unblocked"] is True
        assert bots.called("UnblockRequest")

    async def test_starting_in_a_group_grants_the_named_rights(
        self, live_daemon, client, in_thread, bots
    ):
        started = await result(
            client,
            in_thread,
            "bot.start",
            {
                "bot": "@my_helper_bot",
                "chat": str(GROUP_ID),
                "admin": "delete_messages+manage_chat",
                "add": True,
            },
        )
        assert started["admin_rights"] == ["delete_messages", "other"]
        assert bots.called("InviteToChannelRequest")
        rights = bots.called("EditAdminRequest")[0].admin_rights
        assert rights.delete_messages is True
        # manage_chat is the deep-link spelling of `other`.
        assert rights.other is True

    async def test_a_bot_that_refuses_groups_is_a_permission_error(
        self, live_daemon, client, in_thread, bots
    ):
        bots.bots[HELPER]["user_flags"] = {"bot_nochats": True}
        error = await fails(
            client, in_thread, "bot.start", {"bot": "@my_helper_bot", "chat": str(GROUP_ID)}
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_an_unknown_admin_right_is_a_usage_error(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client,
            in_thread,
            "bot.start",
            {"bot": "@my_helper_bot", "chat": str(GROUP_ID), "admin": "rule_the_world"},
        )
        assert error.exit_code == EXIT_USAGE


class TestBotStop:
    async def test_blocking_reports_the_bot(self, live_daemon, client, in_thread, bots):
        stopped = await result(client, in_thread, "bot.stop", {"bot": "@my_helper_bot"})
        assert stopped == {"bot_id": HELPER, "blocked": True}
        assert bots.called("BlockRequest")

    async def test_delete_chat_drives_the_affected_history_loop(
        self, live_daemon, client, in_thread, bots
    ):
        await result(client, in_thread, "bot.stop", {"bot": "@my_helper_bot", "delete_chat": True})
        assert bots.called("DeleteHistoryRequest")

    async def test_report_spams_before_blocking(self, live_daemon, client, in_thread, bots):
        await result(client, in_thread, "bot.stop", {"bot": "@my_helper_bot", "report": True})
        assert [name for name, _ in bots.calls].index("ReportSpamRequest") < [
            name for name, _ in bots.calls
        ].index("BlockRequest")


# ---------------------------------------------------------------------------
# bot command
# ---------------------------------------------------------------------------


class TestBotCommands:
    async def test_a_users_view_reads_the_commands_off_bot_info(
        self, live_daemon, client, in_thread, bots
    ):
        page = await result(client, in_thread, "bot.command.list", {"bot": "@my_helper_bot"})
        assert [row["command"] for row in page["items"]] == ["start", "help"]

    async def test_a_scope_needs_a_bot_session(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "bot.command.list", {"scope": "default"})
        assert error.exit_code == EXIT_AUTH

    async def test_a_bot_reads_its_own_scope_back(
        self, live_daemon, client, in_thread, bot_session
    ):
        await result(client, in_thread, "bot.command.set", {"commands": "start:Start,stop:Stop"})
        page = await result(client, in_thread, "bot.command.list", {"scope": "default"})
        assert [row["command"] for row in page["items"]] == ["start", "stop"]

    async def test_setting_a_peer_scope_without_a_peer_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client, in_thread, "bot.command.set", {"commands": "a:b", "scope": "peer"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_clearing_resets_the_scope(self, live_daemon, client, in_thread, bot_session):
        await result(client, in_thread, "bot.command.set", {"commands": "start:Start"})
        cleared = await result(client, in_thread, "bot.command.set", {"clear": True})
        assert cleared["cleared"] is True
        assert bots_commands_empty(bot_session)

    async def test_a_command_in_a_group_carries_the_bot_username(
        self, live_daemon, client, in_thread, bots
    ):
        sent = await result(
            client,
            in_thread,
            "bot.command.send",
            {"bot": "@my_helper_bot", "command": "start", "chat": str(GROUP_ID)},
        )
        assert sent["text"] == "/start@my_helper_bot"

    async def test_a_command_in_the_private_chat_does_not(
        self, live_daemon, client, in_thread, bots
    ):
        sent = await result(
            client,
            in_thread,
            "bot.command.send",
            {"bot": "@my_helper_bot", "command": "/start", "args": ["deep", "link"]},
        )
        assert sent["text"] == "/start deep link"

    async def test_guest_mode_mentions_the_bot(self, live_daemon, client, in_thread, bots):
        sent = await result(
            client,
            in_thread,
            "bot.command.send",
            {
                "bot": "@my_helper_bot",
                "command": "start",
                "chat": str(GROUP_ID),
                "guest": True,
            },
        )
        assert sent["text"].startswith("@my_helper_bot ")

    async def test_a_business_connection_wraps_the_send_and_routes_it_to_the_connection_dc(
        self, live_daemon, client, in_thread, bot_session
    ):
        await result(
            client,
            in_thread,
            "bot.command.send",
            {
                "bot": "@my_helper_bot",
                "command": "start",
                "business_connection": "conn1",
            },
        )
        assert bot_session.called("InvokeWithBusinessConnectionRequest")
        borrowed = bot_session.called("borrow_exported_sender")
        assert borrowed and borrowed[0]["dc_id"] == bot_session.business_dc


def bots_commands_empty(world) -> bool:
    return not any(world.bot_commands.values())


# ---------------------------------------------------------------------------
# bot menu / permission / access / default rights
# ---------------------------------------------------------------------------


class TestBotMenu:
    async def test_the_default_button_is_normalised_to_commands(
        self, live_daemon, client, in_thread, bots
    ):
        button = await result(client, in_thread, "bot.menu.get", {"bot": "@my_helper_bot"})
        assert button["kind"] == "commands"

    async def test_a_webapp_button_reports_its_url(self, live_daemon, client, in_thread, bots):
        from telethon.tl import types

        bots.bots[HELPER]["menu_button"] = types.BotMenuButton(
            text="Shop", url="https://example.org/shop"
        )
        button = await result(client, in_thread, "bot.menu.get", {"bot": "@my_helper_bot"})
        assert button == {"kind": "webapp", "text": "Shop", "url": "https://example.org/shop"}

    async def test_setting_a_webapp_button_needs_text_and_url(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(client, in_thread, "bot.menu.set", {"webapp": True, "text": "Shop"})
        assert error.exit_code == EXIT_USAGE

    async def test_exactly_one_kind_may_be_chosen(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(client, in_thread, "bot.menu.set", {"commands": True, "default": True})
        assert error.exit_code == EXIT_USAGE

    async def test_setting_the_commands_button_stores_it(
        self, live_daemon, client, in_thread, bot_session
    ):
        button = await result(client, in_thread, "bot.menu.set", {"commands": True})
        assert button["kind"] == "commands"
        assert bot_session.called("SetBotMenuButtonRequest")


class TestBotPermission:
    async def test_reading_both_permissions(self, live_daemon, client, in_thread, bots):
        bots.bots[HELPER]["can_send"] = True
        bots.bots[HELPER]["emoji_status_allowed"] = True
        permission = await result(
            client, in_thread, "bot.permission.get", {"bot": "@my_helper_bot"}
        )
        assert permission["can_send_messages"] is True
        assert permission["emoji_status_allowed"] is True

    async def test_granting_message_permission_is_idempotent(
        self, live_daemon, client, in_thread, bots
    ):
        first = await result(
            client,
            in_thread,
            "bot.permission.set",
            {"bot": "@my_helper_bot", "key": "message", "state": "on"},
        )
        assert first.get("already", False) is False
        envelope = await call(
            client,
            in_thread,
            "bot.permission.set",
            {"bot": "@my_helper_bot", "key": "message", "state": "on"},
        )
        assert envelope["result"]["already"] is True
        assert envelope["meta"]["already"] is True

    async def test_there_is_no_revoke_for_may_message_me(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client,
            in_thread,
            "bot.permission.set",
            {"bot": "@my_helper_bot", "key": "message", "state": "off"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_an_unknown_key_is_a_usage_error(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client,
            in_thread,
            "bot.permission.set",
            {"bot": "@my_helper_bot", "key": "everything", "state": "on"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_the_emoji_status_toggle_reaches_the_server(
        self, live_daemon, client, in_thread, bots
    ):
        await result(
            client,
            in_thread,
            "bot.permission.set",
            {"bot": "@my_helper_bot", "key": "emoji-status", "state": "on"},
        )
        assert bots.called("ToggleUserEmojiStatusPermissionRequest")[0].enabled is True


class TestBotAccess:
    async def test_adding_a_peer_keeps_the_ones_already_there(
        self, live_daemon, client, in_thread, bots
    ):
        bots.bots[HELPER]["allowed"] = [ALICE]
        access = await result(
            client,
            in_thread,
            "bot.access.set",
            {"bot": "@my_helper_bot", "restricted": True, "add": ["@gifbot"]},
        )
        assert sorted(access["allowed_users"]) == sorted([ALICE, GIFBOT])
        read = await result(client, in_thread, "bot.access.get", {"bot": "@my_helper_bot"})
        assert read["restricted"] is True
        assert sorted(read["allowed_users"]) == sorted([ALICE, GIFBOT])

    async def test_removing_a_peer_drops_only_that_one(self, live_daemon, client, in_thread, bots):
        bots.bots[HELPER]["allowed"] = [ALICE, GIFBOT]
        access = await result(
            client,
            in_thread,
            "bot.access.set",
            {"bot": "@my_helper_bot", "remove": ["@alice"]},
        )
        assert access["allowed_users"] == [GIFBOT]

    async def test_restricted_and_open_contradict_each_other(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client,
            in_thread,
            "bot.access.set",
            {"bot": "@my_helper_bot", "restricted": True, "open_to_all": True},
        )
        assert error.exit_code == EXIT_USAGE


class TestBotDefaultRights:
    async def test_the_two_halves_are_set_independently(
        self, live_daemon, client, in_thread, bot_session
    ):
        rights = await result(
            client,
            in_thread,
            "bot.default-rights.set",
            {"group": "delete_messages", "channel": "post_messages"},
        )
        assert rights == {"group_rights": ["delete_messages"], "channel_rights": ["post_messages"]}
        assert bot_session.default_rights["group"].delete_messages is True

    async def test_neither_half_is_a_usage_error(self, live_daemon, client, in_thread, bot_session):
        error = await fails(client, in_thread, "bot.default-rights.set", {})
        assert error.exit_code == EXIT_USAGE

    async def test_a_user_session_is_refused(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client, in_thread, "bot.default-rights.set", {"group": "delete_messages"}
        )
        assert error.exit_code == EXIT_AUTH


# ---------------------------------------------------------------------------
# bot press
# ---------------------------------------------------------------------------


class TestPressAddressing:
    async def test_a_lone_button_needs_no_address(self, live_daemon, client, in_thread, bots):
        with_buttons(bots, ALICE, keyboard([{"text": "Yes"}]))
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["kind"] == "callback"
        assert pressed["message"] == "OK"

    async def test_the_flat_index_is_the_one_message_get_prints(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "A"}], [{"text": "B"}, {"text": "C"}]))
        message = await result(client, in_thread, "message.get", {"chat": "@alice", "msg_id": 700})
        printed = [b["n"] for row in message["reply_markup"]["rows"] for b in row]
        assert printed == [0, 1, 2]
        pressed = await result(
            client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, "button": "2"}
        )
        assert (pressed["row"], pressed["col"], pressed["n"]) == (1, 1, 2)

    async def test_row_and_column_address_the_same_button(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "A"}], [{"text": "B"}, {"text": "C"}]))
        pressed = await result(
            client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, "button": "1,1"}
        )
        assert pressed["n"] == 2

    async def test_text_matches_exactly_then_uniquely(self, live_daemon, client, in_thread, bots):
        with_buttons(bots, ALICE, keyboard([{"text": "Accept"}, {"text": "Accept later"}]))
        exact = await result(
            client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, "button": "Accept"}
        )
        assert exact["n"] == 0
        unique = await result(
            client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, "button": "later"}
        )
        assert unique["n"] == 1

    async def test_an_ambiguous_text_refuses_rather_than_guessing(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "Buy one"}, {"text": "Buy two"}]))
        error = await fails(
            client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, "button": "Buy"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_payload_addresses_a_callback_button(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(
            bots,
            ALICE,
            keyboard([{"text": "A", "data": b"one"}, {"text": "B", "data": b"two"}]),
        )
        pressed = await result(
            client,
            in_thread,
            "bot.press",
            {"chat": "@alice", "msg_id": 700, "data": "str:two"},
        )
        assert pressed["n"] == 1
        assert bots.called("GetBotCallbackAnswerRequest")[0].data == b"two"

    async def test_several_buttons_and_no_address_is_a_usage_error(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "A"}, {"text": "B"}]))
        error = await fails(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert error.exit_code == EXIT_USAGE

    async def test_a_message_without_buttons_is_not_found(
        self, live_daemon, client, in_thread, bots
    ):
        bots.add_message(ALICE, "plain", message_id=701)
        error = await fails(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 701})
        assert error.exit_code == EXIT_NOT_FOUND


class TestPressKinds:
    async def test_a_url_button_is_printed_never_opened(self, live_daemon, client, in_thread, bots):
        with_buttons(
            bots, ALICE, keyboard([{"text": "Site", "type": "url", "url": "https://x.example"}])
        )
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed == {
            "kind": "url",
            "row": 0,
            "col": 0,
            "n": 0,
            "text": "Site",
            "url": "https://x.example",
        }

    async def test_a_copy_button_prints_its_text(self, live_daemon, client, in_thread, bots):
        with_buttons(
            bots, ALICE, keyboard([{"text": "Copy", "type": "copy", "copy_text": "ABC-123"}])
        )
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["copy_text"] == "ABC-123"

    async def test_a_reply_keyboard_text_button_sends_its_own_text(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "Menu", "type": "text"}]))
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["kind"] == "text"
        assert bots.called("SendMessageRequest")[0].message == "Menu"

    async def test_a_game_button_asks_for_the_game_url(self, live_daemon, client, in_thread, bots):
        from telethon.tl import types

        bots.callback_answer = types.messages.BotCallbackAnswer(
            cache_time=0, url="https://game.example/play", has_url=True
        )
        with_buttons(bots, ALICE, keyboard([{"text": "Play", "type": "game"}]))
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["url"] == "https://game.example/play"
        assert bots.called("GetBotCallbackAnswerRequest")[0].game is True

    async def test_a_buy_button_is_refused(self, live_daemon, client, in_thread, bots):
        with_buttons(bots, ALICE, keyboard([{"text": "Pay", "type": "buy"}]))
        error = await fails(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert error.exit_code == EXIT_PERMISSION

    async def test_a_switch_inline_button_runs_the_real_inline_query(
        self, live_daemon, client, in_thread, bots
    ):
        """Telethon's own MessageButton.click sends startBot here, which is wrong."""
        with_buttons(
            bots,
            ALICE,
            keyboard([{"text": "Search", "type": "switch_inline", "query": "cats"}]),
        )
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["kind"] == "switch_inline"
        assert bots.called("GetInlineBotResultsRequest")[0].query == "cats"
        assert not bots.called("StartBotRequest")

    async def test_a_webview_button_returns_the_signed_url_and_its_session(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(
            bots,
            ALICE,
            keyboard([{"text": "Open", "type": "webview", "url": "https://app.example"}]),
        )
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["url"] == bots.webapp_url
        assert pressed["query_id"] == str(bots.webapp_query_id)

    async def test_a_url_auth_button_is_inspected_never_accepted(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(
            bots,
            ALICE,
            keyboard([{"text": "Login", "type": "url_auth", "url": "https://x.example"}]),
        )
        pressed = await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert pressed["auth"]["result"] == "request"
        assert not bots.called("AcceptUrlAuthRequest")

    async def test_a_bot_that_does_not_answer_is_not_an_error(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.errors import BotResponseTimeoutError

        with_buttons(bots, ALICE, keyboard([{"text": "A"}]))
        bots.fail_next("GetBotCallbackAnswerRequest", BotResponseTimeoutError(request=None))
        envelope = await call(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert envelope["result"]["kind"] == "callback"
        assert "message" not in envelope["result"]
        assert any("offline" in w for w in envelope["meta"]["warnings"])

    async def test_a_password_guarded_button_needs_the_password(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "Transfer", "requires_password": True}]))
        error = await fails(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert error.exit_code == EXIT_USAGE

    async def test_a_password_guarded_button_sends_an_srp_check(
        self, live_daemon, client, in_thread, bots
    ):
        bots.auth.password = "hunter2"
        with_buttons(bots, ALICE, keyboard([{"text": "Transfer", "requires_password": True}]))
        await result(
            client,
            in_thread,
            "bot.press",
            {"chat": "@alice", "msg_id": 700, "password": "hunter2"},
        )
        assert bots.called("GetBotCallbackAnswerRequest")[0].password is not None

    async def test_the_two_layer_229_button_kinds_exit_13(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "A"}]))
        for field in ("rich_button", "ephemeral"):
            error = await fails(
                client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, field: 1}
            )
            assert error.exit_code == EXIT_INDETERMINATE
            assert error.code == "NOT_SUPPORTED"


class TestPressConsent:
    """A button that discloses something is not pressed without its flag."""

    @pytest.mark.parametrize(
        "kind,flag,value",
        [
            ("request_phone", "share_phone", True),
            ("request_geo", "share_geo", "1.0,2.0"),
            ("request_poll", "poll", "Lunch?:Pizza,Sushi"),
            ("request_peer", "peers", ["@alice"]),
        ],
    )
    async def test_without_the_flag_nothing_is_sent(
        self, live_daemon, client, in_thread, bots, kind, flag, value
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "Share", "type": kind}]))
        error = await fails(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700})
        assert error.exit_code == EXIT_USAGE
        assert not bots.called("SendMediaRequest")
        assert not bots.called("SendBotRequestedPeerRequest")

    @pytest.mark.parametrize(
        "kind,flag,value",
        [
            ("request_phone", "share_phone", True),
            ("request_geo", "share_geo", "1.0,2.0"),
            ("request_poll", "poll", "Lunch?:Pizza,Sushi"),
        ],
    )
    async def test_with_the_flag_the_media_goes_out(
        self, live_daemon, client, in_thread, bots, kind, flag, value
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "Share", "type": kind}]))
        await result(client, in_thread, "bot.press", {"chat": "@alice", "msg_id": 700, flag: value})
        assert bots.called("SendMediaRequest")

    async def test_sharing_a_peer_sends_the_requested_peer_answer(
        self, live_daemon, client, in_thread, bots
    ):
        with_buttons(bots, ALICE, keyboard([{"text": "Pick", "type": "request_peer"}]))
        pressed = await result(
            client,
            in_thread,
            "bot.press",
            {"chat": "@alice", "msg_id": 700, "peers": ["@alice"]},
        )
        assert pressed["peers"] == [ALICE]
        assert bots.called("SendBotRequestedPeerRequest")[0].button_id == 7

    async def test_a_quiz_poll_needs_the_correct_answer(self, live_daemon, client, in_thread, bots):
        with_buttons(bots, ALICE, keyboard([{"text": "Poll", "type": "request_poll"}]))
        error = await fails(
            client,
            in_thread,
            "bot.press",
            {"chat": "@alice", "msg_id": 700, "poll": "Q?:a,b", "quiz": True},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_mini_app_peer_request_is_answered_by_id(
        self, live_daemon, client, in_thread, bots
    ):
        pressed = await result(
            client,
            in_thread,
            "bot.press",
            {"chat": "@my_helper_bot", "webapp_req": "req1", "peers": ["@alice"]},
        )
        assert pressed["peers"] == [ALICE]
        assert bots.called("SendBotRequestedPeerRequest")[0].webapp_req_id == "req1"


# ---------------------------------------------------------------------------
# bot url-auth
# ---------------------------------------------------------------------------


class TestUrlAuth:
    async def test_inspecting_prints_the_domain_and_grants_nothing(
        self, live_daemon, client, in_thread, bots
    ):
        auth = await result(
            client, in_thread, "bot.url-auth.get", {"target": "https://x.example/login"}
        )
        assert auth["result"] == "request"
        assert auth["domain"] == "example.org"
        assert not bots.called("AcceptUrlAuthRequest")

    async def test_a_button_needs_both_coordinates(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client, in_thread, "bot.url-auth.get", {"target": "@my_helper_bot", "msg_id": 700}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_accepting_defaults_both_consent_flags_off(
        self, live_daemon, client, in_thread, bots
    ):
        auth = await result(
            client, in_thread, "bot.url-auth.accept", {"target": "https://x.example/login"}
        )
        assert auth["result"] == "accepted"
        assert auth["url"].startswith("https://example.org/login")
        request = bots.called("AcceptUrlAuthRequest")[0]
        assert request.write_allowed is None
        assert request.share_phone_number is None

    async def test_consent_flags_are_passed_through_when_given(
        self, live_daemon, client, in_thread, bots
    ):
        await result(
            client,
            in_thread,
            "bot.url-auth.accept",
            {"target": "https://x.example/login", "write_allowed": True, "share_phone": True},
        )
        request = bots.called("AcceptUrlAuthRequest")[0]
        assert request.write_allowed is True
        assert request.share_phone_number is True

    async def test_a_match_code_is_mandatory_when_the_request_shows_one(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.tl import types

        bots.url_auth = types.UrlAuthResultRequest(
            bot=bots.me, domain="example.org", match_codes=True
        )
        error = await fails(
            client, in_thread, "bot.url-auth.accept", {"target": "https://x.example/login"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_wrong_match_code_stops_the_login(self, live_daemon, client, in_thread, bots):
        from telethon.tl import types

        bots.url_auth = types.UrlAuthResultRequest(
            bot=bots.me, domain="example.org", match_codes=True, match_codes_first=True
        )
        error = await fails(
            client,
            in_thread,
            "bot.url-auth.accept",
            {"target": "https://x.example/login", "match_code": "dog"},
        )
        assert error.exit_code == EXIT_PERMISSION
        assert not bots.called("AcceptUrlAuthRequest")

    async def test_declining_says_so(self, live_daemon, client, in_thread, bots):
        declined = await result(
            client, in_thread, "bot.url-auth.decline", {"url": "tg://oauth?domain=x"}
        )
        assert declined == {"result": "declined", "declined": True, "url": "tg://oauth?domain=x"}

    async def test_the_folded_in_v1_paths_all_reach_this_op(self):
        from tlgr.registry import canonical

        for name in ("link auth", "auth url-login", "bot login-url get"):
            assert canonical(name) == "bot.url-auth.get"


# ---------------------------------------------------------------------------
# bot answer / query / api / connection / stream
# ---------------------------------------------------------------------------


class TestBotAnswer:
    async def test_a_user_session_cannot_answer(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "bot.answer", {"kind": "callback", "query_id": "1"})
        assert error.exit_code == EXIT_AUTH

    async def test_a_callback_answer_carries_its_text(
        self, live_daemon, client, in_thread, bot_session
    ):
        answered = await result(
            client,
            in_thread,
            "bot.answer",
            {"kind": "callback", "query_id": "17", "text": "Saved", "alert": True},
        )
        assert answered == {"query_id": "17", "kind": "callback", "answered": True}
        request = bot_session.called("SetBotCallbackAnswerRequest")[0]
        assert (request.message, request.alert) == ("Saved", True)

    async def test_a_flag_from_another_kind_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client,
            in_thread,
            "bot.answer",
            {"kind": "callback", "query_id": "17", "next_offset": "p2"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_precheckout_answer_is_allowed_because_it_is_not_a_payment(
        self, live_daemon, client, in_thread, bot_session
    ):
        await result(
            client,
            in_thread,
            "bot.answer",
            {"kind": "precheckout", "query_id": "17", "ok": True},
        )
        assert bot_session.called("SetBotPrecheckoutResultsRequest")[0].success is True

    async def test_an_inline_answer_reads_its_results_from_a_file(
        self, live_daemon, client, in_thread, bot_session, tmp_path
    ):
        path = tmp_path / "results.json"
        path.write_text('[{"id":"r1","type":"article","message":{"text":"hello"}}]')
        await result(
            client,
            in_thread,
            "bot.answer",
            {
                "kind": "inline",
                "query_id": "17",
                "results": str(path),
                "gallery": True,
                "switch_pm": "Log in:start",
            },
        )
        request = bot_session.called("SetInlineBotResultsRequest")[0]
        assert request.results[0].id == "r1"
        assert request.gallery is True
        assert request.switch_pm.start_param == "start"

    async def test_an_unknown_kind_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(client, in_thread, "bot.answer", {"kind": "telepathy", "query_id": "1"})
        assert error.exit_code == EXIT_USAGE


class TestBotQueryAndApi:
    async def test_the_query_list_says_when_nobody_is_buffering(
        self, live_daemon, client, in_thread, bot_session
    ):
        envelope = await call(client, in_thread, "bot.query.list", {})
        assert envelope["result"] == []
        assert any("bot-updates" in w for w in envelope["meta"]["warnings"])

    async def test_the_query_list_needs_a_bot_session(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "bot.query.list", {})
        assert error.exit_code == EXIT_AUTH

    async def test_an_arbitrary_bot_api_method_passes_its_json_through(
        self, live_daemon, client, in_thread, bot_session
    ):
        bot_session.custom_response = '{"ok":true,"result":{"id":7}}'
        answer = await result(
            client, in_thread, "bot.api.send", {"method": "getMe", "params": "{}"}
        )
        assert answer["result"]["result"]["id"] == 7
        assert bot_session.called("SendCustomRequestRequest")[0].custom_method == "getMe"

    async def test_a_business_connection_reports_its_dc_and_rights(
        self, live_daemon, client, in_thread, bot_session
    ):
        connection = await result(
            client, in_thread, "bot.connection.get", {"connection_id": "conn1"}
        )
        assert connection["dc_id"] == bot_session.business_dc
        assert connection["rights"] == ["reply"]

    async def test_wrapping_an_arbitrary_command_is_refused_with_a_pointer(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client,
            in_thread,
            "bot.connection.invoke",
            {"connection_id": "conn1", "command": ["message", "send"]},
        )
        assert error.exit_code == EXIT_INDETERMINATE
        assert "--business-connection" in str(error)


class TestBotStream:
    async def test_each_chunk_is_one_typing_action_keyed_by_the_draft(
        self, live_daemon, client, in_thread, bot_session, tmp_path
    ):
        path = tmp_path / "chunks.txt"
        path.write_text("one\ntwo\nthree\n")
        progress = await result(
            client,
            in_thread,
            "bot.stream.send",
            {"chat": "@alice", "draft_id": 99, "file": str(path)},
        )
        assert progress["chunks_sent"] == 3
        actions = [c.action for _, c in bot_session.calls if _ == "SetTypingRequest"]
        assert [a.text.text for a in actions] == ["one", "two", "three"]
        assert {a.random_id for a in actions} == {99}

    async def test_the_layer_229_stop_flags_exit_13(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client,
            in_thread,
            "bot.stream.send",
            {"chat": "@alice", "draft_id": 99, "stop": True},
        )
        assert error.exit_code == EXIT_INDETERMINATE

    async def test_a_stream_without_a_draft_id_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(client, in_thread, "bot.stream.send", {"chat": "@alice", "text": "hi"})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# bot create / edit / username / token
# ---------------------------------------------------------------------------


class TestBotLifecycle:
    async def test_the_username_is_checked_before_the_quota_is_spent(
        self, live_daemon, client, in_thread, bots
    ):
        bots.taken_usernames.add("taken_bot")
        error = await fails(client, in_thread, "bot.create", {"name": "X", "username": "taken_bot"})
        assert error.exit_code == EXIT_USAGE
        assert not bots.called("CreateBotRequest")

    async def test_check_only_never_creates(self, live_daemon, client, in_thread, bots):
        created = await result(
            client,
            in_thread,
            "bot.create",
            {"name": "X", "username": "free_bot", "check_only": True},
        )
        assert created["username"] == "free_bot"
        assert not bots.called("CreateBotRequest")

    async def test_creating_a_bot_returns_its_id(self, live_daemon, client, in_thread, bots):
        created = await result(
            client, in_thread, "bot.create", {"name": "Helper 2", "username": "helper2_bot"}
        )
        assert created["bot_id"] > 0
        assert created["token_available"] is True

    async def test_editing_targets_the_bot_and_not_the_calling_account(
        self, live_daemon, client, in_thread, bots
    ):
        await result(
            client,
            in_thread,
            "bot.edit",
            {"bot": "@my_helper_bot", "name": "Helper", "lang": "en"},
        )
        request = bots.called("SetBotInfoRequest")[0]
        assert request.bot is not None
        assert request.lang_code == "en"

    async def test_a_photo_is_uploaded_against_the_bot(
        self, live_daemon, client, in_thread, bots, tmp_path
    ):
        path = tmp_path / "avatar.jpg"
        path.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
        edited = await result(
            client, in_thread, "bot.edit", {"bot": "@my_helper_bot", "photo": str(path)}
        )
        assert edited["bot_id"] == HELPER
        assert bots.called("UploadProfilePhotoRequest")[0].bot is not None

    async def test_username_check_reports_availability(self, live_daemon, client, in_thread, bots):
        bots.taken_usernames.add("busy_bot")
        assert (await result(client, in_thread, "bot.username.check", {"username": "busy_bot"}))[
            "available"
        ] is False
        assert (await result(client, in_thread, "bot.username.check", {"username": "quiet_bot"}))[
            "available"
        ] is True

    async def test_toggling_and_reordering_usernames_reads_back(
        self, live_daemon, client, in_thread, bots
    ):
        await result(
            client,
            in_thread,
            "bot.username.set",
            {"bot": "@my_helper_bot", "enable": ["alt_bot", "second_bot"]},
        )
        names = await result(
            client,
            in_thread,
            "bot.username.set",
            {"bot": "@my_helper_bot", "order": "second_bot,alt_bot"},
        )
        assert names["usernames"][:2] == ["my_helper_bot", "second_bot"] or names["usernames"] == [
            "my_helper_bot",
            "second_bot",
            "alt_bot",
        ]

    async def test_the_token_is_redacted_unless_asked_for(
        self, live_daemon, client, in_thread, bots
    ):
        envelope = await call(client, in_thread, "bot.token.export", {"bot": "@my_helper_bot"})
        assert "token" not in envelope["result"]
        assert any("redacted" in w for w in envelope["meta"]["warnings"])

    async def test_show_prints_it_and_out_writes_it_privately(
        self, live_daemon, client, in_thread, bots, tmp_path
    ):
        import stat

        target = tmp_path / "token"
        exported = await result(
            client,
            in_thread,
            "bot.token.export",
            {"bot": "@my_helper_bot", "show": True, "out": str(target)},
        )
        assert exported["token"].endswith("TESTTOKEN")
        assert target.read_text().endswith("TESTTOKEN")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    async def test_revoking_issues_a_new_token(self, live_daemon, client, in_thread, bots):
        exported = await result(
            client,
            in_thread,
            "bot.token.export",
            {"bot": "@my_helper_bot", "revoke": True, "show": True},
        )
        assert exported["revoked"] is True
        assert "REVOKED-AND-NEW" in exported["token"]


# ---------------------------------------------------------------------------
# bot preview / affiliate / verification / attach / recent
# ---------------------------------------------------------------------------


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "shot.jpg"
    path.write_bytes(b"\xff\xd8\xff" + b"1" * 64)
    return str(path)


class TestBotPreviews:
    async def test_adding_then_listing_shows_the_gallery(
        self, live_daemon, client, in_thread, bots, picture
    ):
        await result(
            client, in_thread, "bot.preview.add", {"bot": "@my_helper_bot", "file": picture}
        )
        page = await result(client, in_thread, "bot.preview.list", {"bot": "@my_helper_bot"})
        assert len(page["items"]) == 1

    async def test_reordering_moves_the_gallery(
        self, live_daemon, client, in_thread, bots, picture
    ):
        for _ in range(2):
            await result(
                client, in_thread, "bot.preview.add", {"bot": "@my_helper_bot", "file": picture}
            )
        before = await result(client, in_thread, "bot.preview.list", {"bot": "@my_helper_bot"})
        first = before["items"][0]["file_id"]
        await result(
            client, in_thread, "bot.preview.edit", {"bot": "@my_helper_bot", "order": "1,0"}
        )
        after = await result(client, in_thread, "bot.preview.list", {"bot": "@my_helper_bot"})
        assert after["items"][1]["file_id"] == first

    async def test_an_order_that_is_not_a_permutation_is_refused(
        self, live_daemon, client, in_thread, bots, picture
    ):
        await result(
            client, in_thread, "bot.preview.add", {"bot": "@my_helper_bot", "file": picture}
        )
        error = await fails(
            client, in_thread, "bot.preview.edit", {"bot": "@my_helper_bot", "order": "0,1"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_index_and_order_are_mutually_exclusive(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client,
            in_thread,
            "bot.preview.edit",
            {"bot": "@my_helper_bot", "index": 0, "order": "0"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_deleting_an_absent_position_is_not_found(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client, in_thread, "bot.preview.delete", {"bot": "@my_helper_bot", "index": [3]}
        )
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_deleting_removes_it_from_the_gallery(
        self, live_daemon, client, in_thread, bots, picture
    ):
        await result(
            client, in_thread, "bot.preview.add", {"bot": "@my_helper_bot", "file": picture}
        )
        deleted = await result(
            client, in_thread, "bot.preview.delete", {"bot": "@my_helper_bot", "index": [0]}
        )
        assert deleted["deleted"] == 1
        page = await result(client, in_thread, "bot.preview.list", {"bot": "@my_helper_bot"})
        assert page.get("items", []) == []

    async def test_replacing_one_swaps_it(self, live_daemon, client, in_thread, bots, picture):
        await result(
            client, in_thread, "bot.preview.add", {"bot": "@my_helper_bot", "file": picture}
        )
        changed = await result(
            client,
            in_thread,
            "bot.preview.edit",
            {"bot": "@my_helper_bot", "index": 0, "file": picture},
        )
        assert changed["index"] == 0
        assert bots.called("EditPreviewMediaRequest")

    async def test_the_owner_view_asks_for_the_per_language_set(
        self, live_daemon, client, in_thread, bots
    ):
        await result(
            client, in_thread, "bot.preview.list", {"bot": "@my_helper_bot", "owner": True}
        )
        assert bots.called("GetPreviewInfoRequest")


class TestBotAffiliate:
    async def test_a_commission_outside_the_servers_bounds_is_refused(
        self, live_daemon, client, in_thread, bots
    ):
        bots.app_config["starref_max_commission_permille"] = 300
        error = await fails(
            client,
            in_thread,
            "bot.affiliate.set",
            {"bot": "@my_helper_bot", "commission_permille": 900},
        )
        assert error.exit_code == EXIT_USAGE
        assert not bots.called("UpdateStarRefProgramRequest")

    async def test_the_feature_can_be_switched_off_server_side(
        self, live_daemon, client, in_thread, bots
    ):
        bots.app_config["starref_program_allowed"] = False
        error = await fails(
            client,
            in_thread,
            "bot.affiliate.set",
            {"bot": "@my_helper_bot", "commission_permille": 200},
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_setting_a_program_reads_back(self, live_daemon, client, in_thread, bots):
        program = await result(
            client,
            in_thread,
            "bot.affiliate.set",
            {"bot": "@my_helper_bot", "commission_permille": 200, "duration_months": 6},
        )
        assert program["commission_permille"] == 200
        assert program["duration_months"] == 6

    async def test_unsetting_sends_a_zero_commission(self, live_daemon, client, in_thread, bots):
        await result(client, in_thread, "bot.affiliate.unset", {"bot": "@my_helper_bot"})
        assert bots.called("UpdateStarRefProgramRequest")[0].commission_permille == 0

    async def test_joining_returns_my_referral_link(self, live_daemon, client, in_thread, bots):
        joined = await result(client, in_thread, "bot.affiliate.join", {"bot": "@my_helper_bot"})
        assert joined["url"].startswith("https://t.me/")
        assert joined["bot_id"] == HELPER

    async def test_joining_can_be_switched_off_server_side(
        self, live_daemon, client, in_thread, bots
    ):
        bots.app_config["starref_connect_allowed"] = False
        error = await fails(client, in_thread, "bot.affiliate.join", {"bot": "@my_helper_bot"})
        assert error.exit_code == EXIT_PERMISSION

    async def test_connected_programs_are_listed(self, live_daemon, client, in_thread, bots):
        await result(client, in_thread, "bot.affiliate.join", {"bot": "@my_helper_bot"})
        page = await call(client, in_thread, "bot.affiliate.list", {})
        assert page["result"][0]["bot_id"] == HELPER

    async def test_suggested_programs_page_by_a_string_offset(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.tl import types

        bots.suggested_refs = [types.StarRefProgram(bot_id=HELPER, commission_permille=150)]
        bots.suggested_refs_next = "page2"
        page = await call(client, in_thread, "bot.affiliate.list", {"suggested": True})
        assert page["result"][0]["commission_permille"] == 150
        assert page["page"]["has_more"] is True

    async def test_revoking_a_dead_link_is_already_done(self, live_daemon, client, in_thread, bots):
        from telethon.errors import RPCError

        class StarrefExpiredError(RPCError):
            def __init__(self) -> None:
                super().__init__(request=None, message="STARREF_EXPIRED", code=400)

        bots.fail_next("EditConnectedStarRefBotRequest", StarrefExpiredError())
        envelope = await call(
            client, in_thread, "bot.affiliate.revoke", {"link": "https://t.me/x?start=_tgr_a"}
        )
        assert envelope["result"]["revoked"] is True
        assert envelope["meta"]["already"] is True


class TestBotVerification:
    async def test_both_badges_are_reported(self, live_daemon, client, in_thread, bots):
        from telethon.tl import types

        bots.bots[ALICE] = {
            "bot": False,
            "verification": types.BotVerification(
                bot_id=HELPER, icon=1, description="Verified merchant"
            ),
        }
        bots.users[ALICE].verified = True
        badge = await result(client, in_thread, "bot.verification.get", {"chat": "@alice"})
        assert badge["verified_by_bot"] == HELPER
        assert badge["telegram_verified"] is True

    async def test_setting_needs_a_verifier_bot(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "bot.verification.set", {"chat": "@alice"})
        assert error.exit_code == EXIT_USAGE

    async def test_removing_sends_enabled_none(self, live_daemon, client, in_thread, bots):
        verified = await result(
            client,
            in_thread,
            "bot.verification.set",
            {"chat": "@alice", "bot": "@my_helper_bot", "remove": True},
        )
        assert verified["verified"] is False
        assert bots.called("SetCustomVerificationRequest")[0].enabled is None


class TestBotAttachMenu:
    async def test_installing_shows_up_in_the_listing(self, live_daemon, client, in_thread, bots):
        toggled = await result(
            client, in_thread, "bot.attach.toggle", {"bot": "@my_helper_bot", "state": "on"}
        )
        assert toggled["installed"] is True
        page = await result(client, in_thread, "bot.attach.list", {})
        assert [row["bot_id"] for row in page["items"]] == [HELPER]
        assert page["items"][0]["username"] == "my_helper_bot"

    async def test_write_access_is_never_implicit(self, live_daemon, client, in_thread, bots):
        await result(
            client, in_thread, "bot.attach.toggle", {"bot": "@my_helper_bot", "state": "on"}
        )
        assert bots.called("ToggleBotInAttachMenuRequest")[0].write_allowed is None

    async def test_a_disclaimer_bot_needs_accept_tos(self, live_daemon, client, in_thread, bots):
        bots.attach_menu[HELPER] = {"disclaimer": True}
        error = await fails(
            client, in_thread, "bot.attach.toggle", {"bot": "@my_helper_bot", "state": "on"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_bad_state_is_a_usage_error(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client, in_thread, "bot.attach.toggle", {"bot": "@my_helper_bot", "state": "maybe"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_one_bots_entry_can_be_inspected(self, live_daemon, client, in_thread, bots):
        page = await result(client, in_thread, "bot.attach.list", {"bot": "@my_helper_bot"})
        assert page["items"][0]["bot_id"] == HELPER


class TestBotRecent:
    async def test_turning_the_feature_off(self, live_daemon, client, in_thread, bots):
        recent = await result(client, in_thread, "bot.recent.set", {"state": "off"})
        assert recent["enabled"] is False
        assert bots.top_peers_enabled is False

    async def test_forgetting_one_bot(self, live_daemon, client, in_thread, bots):
        recent = await result(client, in_thread, "bot.recent.set", {"forget": "@my_helper_bot"})
        assert recent["forgotten"] == [HELPER]

    async def test_doing_nothing_is_a_usage_error(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "bot.recent.set", {})
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# bot report / ad / game / score
# ---------------------------------------------------------------------------


class TestBotReportAndAds:
    async def test_the_first_report_step_returns_the_option_tree(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.tl import types

        bots.raw["ReportRequest"] = types.ReportResultChooseOption(
            title="What is wrong?",
            options=[types.MessageReportOption(text="Spam", option=b"spam")],
        )
        outcome = await result(client, in_thread, "bot.report", {"bot": "@my_helper_bot"})
        assert outcome["result"] == "choose_option"
        assert outcome["options"][0] == {"text": "Spam", "option": "spam"}

    async def test_an_ephemeral_report_exits_13(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client, in_thread, "bot.report", {"bot": "@my_helper_bot", "ephemeral": 3}
        )
        assert error.exit_code == EXIT_INDETERMINATE

    async def test_ads_are_listed_without_reporting_an_impression(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.tl import types

        bots.raw["GetSponsoredMessagesRequest"] = types.messages.SponsoredMessages(
            messages=[
                types.SponsoredMessage(
                    random_id=b"ad1",
                    title="Sponsor",
                    message="An ad",
                    button_text="Open",
                    url="https://x.example",
                    can_report=True,
                )
            ],
            chats=[],
            users=[],
        )
        page = await result(client, in_thread, "bot.ad.list", {"bot": "@my_helper_bot"})
        assert page["items"][0]["message"] == "An ad"
        assert not bots.called("ViewSponsoredMessageRequest")

    async def test_reading_an_ad_records_the_view_and_optionally_the_click(
        self, live_daemon, client, in_thread, bots
    ):
        read = await result(
            client, in_thread, "bot.ad.read", {"random_id": "str:ad1", "click": True}
        )
        assert read["viewed"] is True and read["clicked"] is True
        assert bots.called("ViewSponsoredMessageRequest")[0].random_id == b"ad1"
        assert bots.called("ClickSponsoredMessageRequest")

    async def test_reporting_an_ad_walks_the_same_tree(self, live_daemon, client, in_thread, bots):
        outcome = await result(client, in_thread, "bot.ad.report", {"random_id": "str:ad1"})
        assert outcome["result"] == "reported"


class TestBotGames:
    async def test_an_unavailable_emoji_game_says_so(self, live_daemon, client, in_thread, bots):
        game = await result(client, in_thread, "bot.game.get", {"emoji": "🎲"})
        assert game == {"emoticon": "🎲"}

    async def test_a_live_emoji_game_reports_its_parameters(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.tl import types

        bots.emoji_game = types.messages.EmojiGameDiceInfo(
            game_hash="h", prev_stake=5, current_streak=2, params=[1, 2, 3]
        )
        game = await result(client, in_thread, "bot.game.get", {})
        assert game["available"] is True
        assert game["params"] == [1, 2, 3]

    async def test_sending_a_game_needs_a_bot_session(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client,
            in_thread,
            "bot.game.send",
            {"bot": "@my_helper_bot", "short_name": "tetris", "chat": "@alice"},
        )
        assert error.exit_code == EXIT_AUTH

    async def test_a_game_goes_out_as_input_media_game(
        self, live_daemon, client, in_thread, bot_session
    ):
        sent = await result(
            client,
            in_thread,
            "bot.game.send",
            {"bot": "@my_helper_bot", "short_name": "tetris", "chat": "@alice"},
        )
        assert sent["short_name"] == "tetris"
        media = bot_session.called("SendMediaRequest")[0].media
        assert type(media).__name__ == "InputMediaGame"
        assert media.id.short_name == "tetris"

    async def test_a_game_without_a_chat_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client, in_thread, "bot.game.send", {"bot": "@my_helper_bot", "short_name": "tetris"}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_high_scores_come_back_in_order(self, live_daemon, client, in_thread, bots):
        from telethon.tl import types

        bots.high_scores = [types.HighScore(pos=1, user_id=ALICE, score=900)]
        page = await result(client, in_thread, "bot.score.list", {"chat": "@alice", "msg_id": 12})
        assert page["items"] == [{"position": 1, "user_id": ALICE, "score": 900}]

    async def test_an_inline_score_table_is_fetched_from_the_messages_own_dc(
        self, live_daemon, client, in_thread, bots
    ):
        await result(client, in_thread, "bot.score.list", {"inline_id": "3:99:77"})
        borrowed = bots.called("borrow_exported_sender")
        assert borrowed and borrowed[0]["dc_id"] == 3

    async def test_a_malformed_inline_id_is_a_usage_error(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(client, in_thread, "bot.score.list", {"inline_id": "not-an-id"})
        assert error.exit_code == EXIT_USAGE

    async def test_setting_a_score_needs_a_player(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client, in_thread, "bot.score.set", {"chat": "@alice", "msg_id": 12, "score": 10}
        )
        assert error.exit_code == EXIT_USAGE

    async def test_allow_lower_maps_to_the_servers_force_flag(
        self, live_daemon, client, in_thread, bot_session
    ):
        await result(
            client,
            in_thread,
            "bot.score.set",
            {
                "chat": "@alice",
                "msg_id": 12,
                "user": "@alice",
                "score": 10,
                "allow_lower": True,
            },
        )
        assert bot_session.called("SetGameScoreRequest")[0].force is True


# ---------------------------------------------------------------------------
# The layer-229 surface
# ---------------------------------------------------------------------------


class TestLayerGaps:
    """Registered and refused. 'Unavailable' is not 'no such command'."""

    @pytest.mark.parametrize(
        "op,request_body",
        [
            ("bot.ephemeral.send", {"chat": "@alice", "text": "hi"}),
            ("bot.ephemeral.delete", {"chat": "@alice", "id": [1]}),
            ("bot.welcome.list", {"chat": "@alice"}),
            ("bot.welcome.set", {"chat": "@alice", "text": "Welcome"}),
            ("bot.welcome.delete", {"chat": "@alice", "id": [1]}),
        ],
    )
    async def test_each_exits_13_with_not_supported(
        self, live_daemon, client, in_thread, bots, op, request_body
    ):
        error = await fails(client, in_thread, op, request_body)
        assert error.exit_code == EXIT_INDETERMINATE
        assert error.code == "NOT_SUPPORTED"

    def test_they_are_registered_so_capabilities_can_name_them(self):
        from tlgr.registry import REGISTRY

        for op in (
            "bot.ephemeral.send",
            "bot.ephemeral.delete",
            "bot.welcome.list",
            "bot.welcome.set",
            "bot.welcome.delete",
        ):
            assert op in REGISTRY


# ---------------------------------------------------------------------------
# inline
# ---------------------------------------------------------------------------


@pytest.fixture
def inline(bots):
    """Two results the fake's inline bot answers with."""
    from telethon.tl import types

    bots.inline_results = [
        types.BotInlineResult(
            id="r1",
            type="article",
            send_message=types.BotInlineMessageText(message="first"),
            title="First",
            url="https://x.example/1",
        ),
        types.BotInlineMediaResult(
            id="r2",
            type="gif",
            send_message=types.BotInlineMessageMediaAuto(message=""),
            title="Second",
        ),
    ]
    return bots


class TestInlineQuery:
    async def test_results_carry_their_index_query_id_and_shape(
        self, live_daemon, client, in_thread, inline
    ):
        page = await call(client, in_thread, "inline.query", {"bot": "@gifbot", "query": "cat"})
        rows = page["result"]
        assert [row["n"] for row in rows] == [0, 1]
        assert rows[0]["content"] == "url"
        assert rows[1]["content"] == "media"
        assert rows[0]["send_message"] == "text"
        assert rows[0]["query_id"] == "987654321"

    async def test_the_chat_is_passed_to_the_bot(self, live_daemon, client, in_thread, inline):
        await call(
            client,
            in_thread,
            "inline.query",
            {"bot": "@gifbot", "query": "cat", "chat": str(GROUP_ID)},
        )
        request = inline.called("GetInlineBotResultsRequest")[0]
        assert type(request.peer).__name__ == "InputPeerChannel"

    async def test_the_bots_own_offset_is_fed_straight_back(
        self, live_daemon, client, in_thread, inline
    ):
        from telethon.tl import types

        inline.raw["GetInlineBotResultsRequest"] = types.messages.BotResults(
            query_id=1,
            results=list(inline.inline_results),
            cache_time=300,
            users=[],
            next_offset="opaque-42",
        )
        page = await call(client, in_thread, "inline.query", {"bot": "@gifbot"})
        assert page["result"][0]["next_offset"] == "opaque-42"
        assert page["page"]["has_more"] is True
        await call(
            client,
            in_thread,
            "inline.query",
            {"bot": "@gifbot"},
            cursor=page["page"]["next_cursor"],
        )
        assert inline.called("GetInlineBotResultsRequest")[-1].offset == "opaque-42"

    async def test_a_geo_query_attaches_the_point(self, live_daemon, client, in_thread, inline):
        await call(
            client,
            in_thread,
            "inline.query",
            {"bot": "@gifbot", "lat": 51.5, "lon": -0.1, "accuracy": 40},
        )
        point = inline.called("GetInlineBotResultsRequest")[-1].geo_point
        assert (point.lat, point.long, point.accuracy_radius) == (51.5, -0.1, 40)

    async def test_a_silent_bot_is_an_empty_page_and_not_a_failure(
        self, live_daemon, client, in_thread, inline
    ):
        from telethon.errors import BotResponseTimeoutError

        inline.fail_next("GetInlineBotResultsRequest", BotResponseTimeoutError(request=None))
        envelope = await call(client, in_thread, "inline.query", {"bot": "@gifbot"})
        assert envelope["result"] == []
        assert any("offline" in w for w in envelope["meta"]["warnings"])


class TestInlineSearch:
    async def test_the_bot_username_comes_from_the_server_config(
        self, live_daemon, client, in_thread, inline
    ):
        from fake_telethon import make_user

        inline.gif_search_username = "housegif"
        gifbot = make_user(70001, username="housegif", first="GIF")
        gifbot.bot = True
        inline.add_user(gifbot)
        page = await call(client, in_thread, "inline.search", {"kind": "gif", "query": "cat"})
        assert page["result"][0]["id"] == "r1"
        assert inline.called("GetConfigRequest")

    async def test_a_venue_search_needs_coordinates(self, live_daemon, client, in_thread, inline):
        error = await fails(client, in_thread, "inline.search", {"kind": "venue"})
        assert error.exit_code == EXIT_USAGE

    async def test_an_unknown_kind_is_a_usage_error(self, live_daemon, client, in_thread, inline):
        error = await fails(client, in_thread, "inline.search", {"kind": "sounds"})
        assert error.exit_code == EXIT_USAGE


class TestInlineSend:
    async def test_pick_re_runs_the_query_so_the_pair_is_fresh(
        self, live_daemon, client, in_thread, inline
    ):
        sent = await result(
            client,
            in_thread,
            "inline.send",
            {"bot": "@gifbot", "query": "cat", "chat": "@alice", "pick": "1"},
        )
        assert sent["result_id"] == "r2"
        assert inline.called("GetInlineBotResultsRequest")
        request = inline.called("SendInlineBotResultRequest")[0]
        assert request.query_id == 987654321

    async def test_a_result_id_may_be_picked_by_name(self, live_daemon, client, in_thread, inline):
        sent = await result(
            client,
            in_thread,
            "inline.send",
            {"bot": "@gifbot", "chat": "@alice", "pick": "r1"},
        )
        assert sent["result_id"] == "r1"

    async def test_an_unknown_pick_is_not_found(self, live_daemon, client, in_thread, inline):
        error = await fails(
            client,
            in_thread,
            "inline.send",
            {"bot": "@gifbot", "chat": "@alice", "pick": "nope"},
        )
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_a_query_id_without_a_result_id_is_a_usage_error(
        self, live_daemon, client, in_thread, inline
    ):
        error = await fails(
            client,
            in_thread,
            "inline.send",
            {"bot": "@gifbot", "chat": "@alice", "query_id": "1"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_a_supplied_pair_is_used_verbatim(self, live_daemon, client, in_thread, inline):
        await result(
            client,
            in_thread,
            "inline.send",
            {"bot": "@gifbot", "chat": "@alice", "query_id": "555", "result_id": "r9"},
        )
        request = inline.called("SendInlineBotResultRequest")[0]
        assert (request.query_id, request.id) == (555, "r9")
        assert not inline.called("GetInlineBotResultsRequest")

    async def test_paid_stars_are_passed_through_as_the_agreed_amount(
        self, live_daemon, client, in_thread, inline
    ):
        await result(
            client,
            in_thread,
            "inline.send",
            {
                "bot": "@gifbot",
                "chat": "@alice",
                "query_id": "555",
                "result_id": "r9",
                "paid_stars": 5,
            },
        )
        assert inline.called("SendInlineBotResultRequest")[0].allow_paid_stars == 5

    async def test_a_quick_reply_shortcut_is_carried(self, live_daemon, client, in_thread, inline):
        sent = await result(
            client,
            in_thread,
            "inline.send",
            {
                "bot": "@gifbot",
                "chat": "@alice",
                "query_id": "555",
                "result_id": "r9",
                "quick_reply": "hello",
            },
        )
        assert sent["quick_reply"] == "hello"
        assert inline.called("SendInlineBotResultRequest")[0].quick_reply_shortcut is not None


class TestInlineEditAndPrepared:
    async def test_an_edit_is_routed_to_the_messages_own_dc(
        self, live_daemon, client, in_thread, bot_session
    ):
        edited = await result(
            client,
            in_thread,
            "inline.edit",
            {"inline_msg_id": "4:12:34", "text": "Updated"},
        )
        assert edited == {"inline_msg_id": "4:12:34", "edited": True}
        borrowed = bot_session.called("borrow_exported_sender")
        assert borrowed and borrowed[0]["dc_id"] == 4

    async def test_an_edit_needs_a_bot_session(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client, in_thread, "inline.edit", {"inline_msg_id": "4:12:34", "text": "x"}
        )
        assert error.exit_code == EXIT_AUTH

    async def test_a_prepared_message_reports_the_chat_types_it_allows(
        self, live_daemon, client, in_thread, inline
    ):
        from telethon.tl import types

        inline.prepared_peer_types = [types.InlineQueryPeerTypePM()]
        prepared = await result(
            client, in_thread, "inline.prepared.get", {"bot": "@my_helper_bot", "id": "p1"}
        )
        assert prepared["peer_types"] == ["pm"]
        assert prepared["result"]["id"] == "r1"

    async def test_sending_outside_those_chat_types_is_refused(
        self, live_daemon, client, in_thread, inline
    ):
        from telethon.tl import types

        inline.prepared_peer_types = [types.InlineQueryPeerTypeBroadcast()]
        error = await fails(
            client,
            in_thread,
            "inline.prepared.send",
            {"bot": "@my_helper_bot", "id": "p1", "chat": "@alice"},
        )
        assert error.exit_code == EXIT_USAGE
        assert not inline.called("SendInlineBotResultRequest")

    async def test_sending_inside_them_works(self, live_daemon, client, in_thread, inline):
        from telethon.tl import types

        inline.prepared_peer_types = [types.InlineQueryPeerTypePM()]
        sent = await result(
            client,
            in_thread,
            "inline.prepared.send",
            {"bot": "@my_helper_bot", "id": "p1", "chat": "@alice"},
        )
        assert sent["result_id"] == "r1"

    async def test_saving_a_prepared_message_needs_a_bot_session(
        self, live_daemon, client, in_thread, bots, tmp_path
    ):
        path = tmp_path / "r.json"
        path.write_text('{"id":"r1","type":"article","message":{"text":"hi"}}')
        error = await fails(
            client,
            in_thread,
            "inline.prepared.save",
            {"user": "@alice", "result": str(path)},
        )
        assert error.exit_code == EXIT_AUTH

    async def test_saving_returns_an_id(
        self, live_daemon, client, in_thread, bot_session, tmp_path
    ):
        path = tmp_path / "r.json"
        path.write_text('{"id":"r1","type":"article","message":{"text":"hi"}}')
        saved = await result(
            client,
            in_thread,
            "inline.prepared.save",
            {"user": "@alice", "result": str(path), "peer_types": ["pm", "group"]},
        )
        assert saved["id"] == "prep1"
        request = bot_session.called("SavePreparedInlineMessageRequest")[0]
        assert len(request.peer_types) == 2

    async def test_an_unknown_peer_type_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session, tmp_path
    ):
        path = tmp_path / "r.json"
        path.write_text('{"id":"r1","type":"article","message":{"text":"hi"}}')
        error = await fails(
            client,
            in_thread,
            "inline.prepared.save",
            {"user": "@alice", "result": str(path), "peer_types": ["telepathy"]},
        )
        assert error.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# webapp
# ---------------------------------------------------------------------------


@pytest.fixture
def mini_app(bots):
    from telethon.tl import types

    bots.bot_apps["shop"] = types.messages.BotApp(
        app=types.BotApp(
            id=1,
            access_hash=2,
            short_name="shop",
            title="Shop",
            description="Buy things",
            photo=types.PhotoEmpty(id=0),
            hash=0,
        ),
        request_write_access=True,
        has_settings=True,
    )
    bots.bots[HELPER]["app_settings"] = types.BotAppSettings(
        placeholder_path=b"12345", background_color=0xFFFFFF
    )
    return bots


class TestWebAppGet:
    async def test_the_manifest_reports_the_placeholder_length_not_its_bytes(
        self, live_daemon, client, in_thread, mini_app
    ):
        info = await result(
            client, in_thread, "webapp.get", {"bot": "@my_helper_bot", "short_name": "shop"}
        )
        assert info["title"] == "Shop"
        assert info["placeholder_path"] == 5
        assert info["link"] == "https://t.me/my_helper_bot/shop"

    async def test_an_unknown_app_is_not_found(self, live_daemon, client, in_thread, mini_app):
        error = await fails(
            client, in_thread, "webapp.get", {"bot": "@my_helper_bot", "short_name": "nope"}
        )
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_a_pending_peer_request_is_shown(self, live_daemon, client, in_thread, mini_app):
        info = await result(
            client,
            in_thread,
            "webapp.get",
            {"bot": "@my_helper_bot", "button_request": "req1"},
        )
        assert info["button_request"]["button_id"] == 7


class TestWebAppOpen:
    async def test_the_url_comes_back_with_the_session_it_needs_kept_alive(
        self, live_daemon, client, in_thread, bots
    ):
        envelope = await call(
            client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "main": True}
        )
        session = envelope["result"]
        assert session["kind"] == "main"
        assert session["url"] == bots.webapp_url
        assert session["needs_prolong"] is True
        assert session["prolong_every"] == 60
        assert any("credential" in w for w in envelope["meta"]["warnings"])

    async def test_a_direct_link_app_has_no_session_to_keep(
        self, live_daemon, client, in_thread, mini_app
    ):
        session = await result(
            client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "app": "shop"}
        )
        assert session["kind"] == "direct-link"
        assert "query_id" not in session

    async def test_an_inactive_app_is_confirmed_before_opening(
        self, live_daemon, client, in_thread, mini_app
    ):
        from telethon.tl import types

        mini_app.bot_apps["shop"] = types.messages.BotApp(
            app=mini_app.bot_apps["shop"].app, inactive=True
        )
        error = await fails(
            client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "app": "shop"}
        )
        assert error.exit_code == EXIT_PERMISSION
        session = await result(
            client,
            in_thread,
            "webapp.open",
            {"bot": "@my_helper_bot", "app": "shop", "open_inactive": True},
        )
        assert session["url"]

    async def test_write_access_is_a_separate_deliberate_call(
        self, live_daemon, client, in_thread, bots
    ):
        await result(client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "main": True})
        assert not bots.called("AllowSendMessageRequest")
        await result(
            client,
            in_thread,
            "webapp.open",
            {"bot": "@my_helper_bot", "main": True, "allow_write": True},
        )
        assert bots.called("AllowSendMessageRequest")

    async def test_a_simple_view_uses_the_simple_request(
        self, live_daemon, client, in_thread, bots
    ):
        session = await result(
            client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "side_menu": True}
        )
        assert session["kind"] == "side-menu"
        assert bots.called("RequestSimpleWebViewRequest")[0].from_side_menu is True

    async def test_a_menu_button_app_needs_the_bot_to_have_one(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "menu": True}
        )
        assert error.exit_code == EXIT_NOT_FOUND

    async def test_the_chat_join_view_exits_13(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client,
            in_thread,
            "webapp.open",
            {"bot": "@my_helper_bot", "join_query_id": "j1"},
        )
        assert error.exit_code == EXIT_INDETERMINATE

    async def test_the_theme_defaults_to_something_renderable(
        self, live_daemon, client, in_thread, bots
    ):
        await result(client, in_thread, "webapp.open", {"bot": "@my_helper_bot", "main": True})
        assert "bg_color" in bots.called("RequestMainWebViewRequest")[0].theme_params.data


class TestWebAppRest:
    async def test_watching_prolongs_and_ends_when_the_session_dies(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.errors import RPCError

        class QueryIdInvalidError(RPCError):
            def __init__(self) -> None:
                super().__init__(request=None, message="QUERY_ID_INVALID", code=400)

        bots.fail_next("ProlongWebViewRequest", QueryIdInvalidError())
        frames = await in_thread(
            lambda: list(
                client.op_stream(
                    "webapp.watch",
                    {"bot": "@my_helper_bot", "query_id": "987654321"},
                    account="work",
                )
            )
        )
        rows = [f["data"] for f in frames if f["type"] == "item"]
        assert rows and rows[-1]["alive"] is False
        assert frames[-1]["type"] == "end"

    async def test_watching_needs_a_numeric_query_id(self, live_daemon, client, in_thread, bots):
        frames = await in_thread(
            lambda: list(
                client.op_stream(
                    "webapp.watch",
                    {"bot": "@my_helper_bot", "query_id": "abc"},
                    account="work",
                )
            )
        )
        end = frames[-1]
        assert end["type"] == "end" and end["ok"] is False
        assert end["error"]["code"] == "USAGE"

    async def test_sending_data_is_capped_at_four_kilobytes(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(
            client,
            in_thread,
            "webapp.send",
            {"bot": "@my_helper_bot", "button_text": "Order", "data": "x" * 5000},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_sending_data_reaches_the_bot(self, live_daemon, client, in_thread, bots):
        sent = await result(
            client,
            in_thread,
            "webapp.send",
            {"bot": "@my_helper_bot", "button_text": "Order", "data": '{"n":1}'},
        )
        assert sent == {"bot_id": HELPER, "sent": True}
        assert bots.called("SendWebViewDataRequest")[0].button_text == "Order"

    async def test_a_custom_method_passes_its_json_through(
        self, live_daemon, client, in_thread, bots
    ):
        bots.custom_response = '{"orders":[1,2]}'
        answer = await result(
            client,
            in_thread,
            "webapp.invoke",
            {"bot": "@my_helper_bot", "method": "getOrders", "params": '{"page":1}'},
        )
        assert answer["result"] == {"orders": [1, 2]}

    async def test_a_download_is_checked_and_not_fetched(
        self, live_daemon, client, in_thread, bots
    ):
        checked = await result(
            client,
            in_thread,
            "webapp.download",
            {
                "bot": "@my_helper_bot",
                "file_name": "invoice.pdf",
                "url": "https://x.example/i.pdf",
            },
        )
        assert checked["allowed"] is True
        assert "downloaded" not in checked
        assert "path" not in checked

    async def test_a_download_the_server_refuses_is_never_fetched(
        self, live_daemon, client, in_thread, bots
    ):
        bots.download_allowed = False
        error = await fails(
            client,
            in_thread,
            "webapp.download",
            {
                "bot": "@my_helper_bot",
                "file_name": "x.bin",
                "url": "https://x.example/x.bin",
                "fetch": True,
            },
        )
        assert error.exit_code == EXIT_PERMISSION

    async def test_only_https_is_fetched(self, live_daemon, client, in_thread, bots):
        error = await fails(
            client,
            in_thread,
            "webapp.download",
            {
                "bot": "@my_helper_bot",
                "file_name": "x.bin",
                "url": "http://x.example/x.bin",
                "fetch": True,
            },
        )
        assert error.exit_code == EXIT_PERMISSION


# ---------------------------------------------------------------------------
# payment
# ---------------------------------------------------------------------------


class TestPaymentForm:
    async def test_a_form_is_readable_and_says_it_cannot_be_paid_here(
        self, live_daemon, client, in_thread, bots
    ):
        form = await result(client, in_thread, "payment.form.get", {"slug": "tshirt"})
        assert form["title"] == "T-shirt"
        assert form["currency"] == "USD"
        assert form["total_amount"] == 1999
        assert form["payable_here"] is False
        assert "never spends money" in form["reason"]

    async def test_exactly_one_invoice_kind_may_be_named(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(client, in_thread, "payment.form.get", {"slug": "a", "stars": 100})
        assert error.exit_code == EXIT_USAGE
        assert (await fails(client, in_thread, "payment.form.get", {})).exit_code == EXIT_USAGE

    async def test_a_message_invoice_wants_chat_and_id(self, live_daemon, client, in_thread, bots):
        await result(client, in_thread, "payment.form.get", {"message": "@alice:12"})
        invoice = bots.called("GetPaymentFormRequest")[0].invoice
        assert type(invoice).__name__ == "InputInvoiceMessage"
        assert invoice.msg_id == 12

    async def test_a_malformed_message_reference_is_a_usage_error(
        self, live_daemon, client, in_thread, bots
    ):
        error = await fails(client, in_thread, "payment.form.get", {"message": "@alice"})
        assert error.exit_code == EXIT_USAGE

    async def test_a_stars_topup_form_is_readable(self, live_daemon, client, in_thread, bots):
        await result(client, in_thread, "payment.form.get", {"stars": 100})
        invoice = bots.called("GetPaymentFormRequest")[0].invoice
        assert type(invoice).__name__ == "InputInvoiceStars"


class TestPaymentRest:
    async def test_a_receipt_is_read_only(self, live_daemon, client, in_thread, bots):
        receipt = await result(
            client, in_thread, "payment.receipt.get", {"chat": "@alice", "msg_id": 42}
        )
        assert receipt["total_amount"] == 1999
        assert receipt["credentials_title"] == "Visa •1234"

    async def test_saved_info_never_carries_a_card_number(
        self, live_daemon, client, in_thread, bots
    ):
        from telethon.tl import types

        bots.saved_order_info = types.PaymentRequestedInfo(name="Alice", email="a@example.org")
        info = await result(client, in_thread, "payment.info.get", {})
        assert info["name"] == "Alice"
        assert info["has_saved_credentials"] is True
        assert "number" not in str(info)

    async def test_clearing_through_the_read_command(self, live_daemon, client, in_thread, bots):
        info = await result(
            client, in_thread, "payment.info.get", {"clear": True, "credentials": True}
        )
        assert info["cleared"] is True
        assert bots.saved_credentials is False

    async def test_deleting_needs_something_to_delete(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "payment.info.delete", {})
        assert error.exit_code == EXIT_USAGE

    async def test_deleting_cards_and_info_separately(self, live_daemon, client, in_thread, bots):
        cleared = await result(client, in_thread, "payment.info.delete", {"credentials": True})
        assert cleared == {"credentials_cleared": True, "info_cleared": False}

    async def test_a_bin_lookup_names_the_issuer(self, live_daemon, client, in_thread, bots):
        card = await result(client, in_thread, "payment.card.get", {"number": "4111 11"})
        assert card["title"] == "Example Bank"
        assert bots.called("GetBankCardDataRequest")[0].number == "411111"

    async def test_exporting_an_invoice_link(self, live_daemon, client, in_thread, bot_session):
        link = await result(
            client,
            in_thread,
            "payment.invoice.export",
            {
                "title": "Shirt",
                "description": "A shirt",
                "currency": "USD",
                "prices": "Shirt:1999",
                "payload": "order-1",
            },
        )
        assert link == {"url": "https://t.me/$abc123", "slug": "$abc123"}

    async def test_an_invoice_needs_all_its_required_parts(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(client, in_thread, "payment.invoice.export", {"title": "Shirt"})
        assert error.exit_code == EXIT_USAGE

    async def test_a_malformed_price_list_is_a_usage_error(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client,
            in_thread,
            "payment.invoice.export",
            {
                "title": "Shirt",
                "description": "A shirt",
                "currency": "USD",
                "prices": "Shirt",
                "payload": "order-1",
            },
        )
        assert error.exit_code == EXIT_USAGE

    async def test_an_invoice_only_goes_to_a_private_chat(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client,
            in_thread,
            "payment.invoice.send",
            {
                "user": str(GROUP_ID),
                "title": "Shirt",
                "description": "A shirt",
                "currency": "USD",
                "prices": "Shirt:1999",
                "payload": "order-1",
            },
        )
        assert error.exit_code == EXIT_USAGE

    async def test_sending_an_invoice_builds_input_media_invoice(
        self, live_daemon, client, in_thread, bot_session
    ):
        sent = await result(
            client,
            in_thread,
            "payment.invoice.send",
            {
                "user": "@alice",
                "title": "Shirt",
                "description": "A shirt",
                "currency": "USD",
                "prices": "Shirt:1999",
                "payload": "order-1",
            },
        )
        assert sent["total_amount"] == 1999
        media = bot_session.called("SendMediaRequest")[0].media
        assert type(media).__name__ == "InputMediaInvoice"
        assert media.payload == b"order-1"


class TestSubscriptions:
    @pytest.fixture
    def subscribed(self, bots):
        from telethon.tl import types

        bots.subscriptions = [
            types.StarsSubscription(
                id="sub1",
                peer=types.PeerUser(user_id=HELPER),
                until_date=None,
                pricing=types.StarsSubscriptionPricing(period=2592000, amount=50),
                can_refulfill=True,
                missing_balance=True,
            )
        ]
        return bots

    async def test_a_lapsed_subscription_says_the_server_would_refulfil_it(
        self, live_daemon, client, in_thread, subscribed
    ):
        page = await call(client, in_thread, "payment.subscription.list", {})
        row = page["result"][0]
        assert row["can_refulfill"] is True
        assert row["missing_balance"] is True
        assert row["pricing"] == {"period": 2592000, "amount": 50}

    async def test_cancelling_flips_the_flag(self, live_daemon, client, in_thread, subscribed):
        changed = await result(
            client,
            in_thread,
            "payment.subscription.set",
            {"subscription_id": "sub1", "auto_renew": "off"},
        )
        assert changed["cancelled"] is True
        assert subscribed.subscriptions[0].canceled is True

    async def test_resuming_sends_canceled_false(self, live_daemon, client, in_thread, subscribed):
        await result(
            client,
            in_thread,
            "payment.subscription.set",
            {"subscription_id": "sub1", "auto_renew": "on"},
        )
        assert subscribed.called("ChangeStarsSubscriptionRequest")[0].canceled is None
        assert subscribed.subscriptions[0].canceled is False

    async def test_the_bot_side_needs_both_halves(
        self, live_daemon, client, in_thread, bot_session
    ):
        error = await fails(
            client,
            in_thread,
            "payment.subscription.set",
            {"auto_renew": "off", "user": "@alice"},
        )
        assert error.exit_code == EXIT_USAGE

    async def test_neither_side_is_a_usage_error(self, live_daemon, client, in_thread, bots):
        error = await fails(client, in_thread, "payment.subscription.set", {"auto_renew": "off"})
        assert error.exit_code == EXIT_USAGE


class TestThePaymentPolicy:
    def test_no_operation_can_spend_money(self):
        """Written against the registry, not against a list of commands.

        A future PR that adds `payments.sendPaymentForm` behind any flag fails
        here, which is the point: the policy is a property of the surface, not
        a promise in a docstring.
        """
        import inspect

        from tlgr.registry import REGISTRY

        forbidden = (
            "SendPaymentFormRequest",
            "SendStarsFormRequest",
            "ValidateRequestedInfoRequest",
            "FulfillStarsSubscriptionRequest",
            "AssignAppStoreTransactionRequest",
            "AssignPlayMarketTransactionRequest",
        )
        for spec in REGISTRY.values():
            try:
                source = inspect.getsource(spec.impl)
            except (OSError, TypeError):  # pragma: no cover - every impl has source
                continue
            for name in forbidden:
                assert name not in source, f"{spec.id} calls {name}"
