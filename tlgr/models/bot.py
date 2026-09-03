"""The bot surface's wire shapes: profile cards, buttons, queries and games.

Two of these shapes carry more weight than the rest.

`Keyboard`/`KeyboardButton` are the **write** side of the reply-markup schema
whose read side is `models.message.ReplyMarkup`. One JSON document therefore
round-trips: `message get --json` prints a keyboard, `bot press --button` can
address a button in it by the `n` that listing printed, and `bot welcome set
--keyboard` can send the same document back. Two schemas for one object is how
a button that can be read stops being a button that can be pressed.

`Pressed` is deliberately one model for every button kind rather than a union.
A caller pressing a button does not know in advance whether the answer is a
toast, a URL, a mini-app session or a list of inline results — that is what
the bot decides — so `kind` names what came back and the rest of the fields
are the ones that kind fills in.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "AttachMenuBot",
    "BotAccess",
    "BotAnswer",
    "BotApiResult",
    "BotCommand",
    "BotCommandSet",
    "BotCreated",
    "BotEdited",
    "BotIds",
    "BotInfo",
    "BotPermission",
    "BotQuery",
    "BotRef",
    "BotStarted",
    "BotStopped",
    "BotToken",
    "BotUsernameCheck",
    "BotUsernames",
    "BotVerification",
    "BotVerified",
    "BotWelcomeMessage",
    "BusinessConnection",
    "CommandSent",
    "DefaultRights",
    "EmojiGame",
    "EphemeralDeleted",
    "EphemeralSent",
    "GameSent",
    "HighScore",
    "Keyboard",
    "KeyboardButton",
    "MenuButton",
    "Pressed",
    "PreviewChange",
    "PreviewMedia",
    "RecentBots",
    "ReportOutcome",
    "ScoreSet",
    "SponsoredRead",
    "StarRefProgram",
    "StreamProgress",
    "ToggledAttachMenu",
    "UrlAuth",
    "WelcomeDeleted",
    "WelcomeSet",
]


# ---------------------------------------------------------------------------
# The reply-markup write side
# ---------------------------------------------------------------------------


class KeyboardButton(Model):
    """One button, in the schema `--keyboard`/`--buttons` files use.

    `type` is the same vocabulary `models.message.Button.type` prints, so a
    button that was read back can be written out again unchanged.
    """

    text: str
    type: str = "text"
    #: callback payload; UTF-8 text, or `hex:…` for bytes that are not text.
    data: str | None = None
    url: str | None = None
    query: str | None = None
    user_id: int | None = None
    requires_password: bool = False
    same_peer: bool = False
    copy_text: str | None = None
    button_id: int | None = None
    fwd_text: str | None = None
    request_write_access: bool = False


class Keyboard(Model):
    """A whole reply markup: rows of buttons plus the keyboard's own flags."""

    kind: str = "inline"
    rows: list[list[KeyboardButton]] = []
    resize: bool = False
    single_use: bool = False
    selective: bool = False
    persistent: bool = False
    placeholder: str | None = None


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class BotCommand(Model):
    """One slash command a bot declares."""

    bot_id: int = 0
    command: str = ""
    description: str = ""
    ephemeral: bool = False
    scope: str | None = None
    lang: str | None = None
    #: The GUI's "Bot Help" / "Bot Settings" entries exist only when the bot
    #: declares `/help` and `/settings`; reporting them as always-true would
    #: put two dead menu items in front of the user.
    has_help: bool = False
    has_settings: bool = False


class MenuButton(Model):
    """The button left of the message input: commands, a mini app, or default."""

    kind: str = "commands"
    text: str | None = None
    url: str | None = None
    user_id: int | None = None


class BotVerification(Model):
    """A third-party verification badge, next to Telegram's own `verified`."""

    verified_by_bot: int | None = None
    description: str | None = None
    icon: int | None = None
    telegram_verified: bool = False


class StarRefProgram(Model):
    """A bot's affiliate (star-ref) program, or my connection to one."""

    bot_id: int = 0
    url: str | None = None
    commission_permille: int = 0
    duration_months: int | None = None
    end_date: str | None = None
    end_date_unix: int | None = None
    participants: int | None = None
    revenue: int | None = None
    date: str | None = None
    date_unix: int | None = None
    revoked: bool = False


class BotAccess(Model):
    """Who may use a managed bot."""

    restricted: bool = False
    allowed_users: list[int] = []
    allowed_chats: list[int] = []


class BotInfo(Model):
    """A bot's profile card, as `userFull.bot_info` and the user flags carry it."""

    id: int = 0
    username: str | None = None
    usernames: list[str] = []
    first_name: str | None = None
    about: str | None = None
    description: str | None = None
    description_photo: int | None = None
    description_document: int | None = None
    privacy_policy_url: str | None = None
    commands: list[BotCommand] = []
    menu_button: MenuButton | None = None
    app_settings: dict[str, Any] | None = None
    verifier_settings: dict[str, Any] | None = None
    bot_verification: BotVerification | None = None
    bot_info_version: int | None = None
    bot_active_users: int | None = None
    bot_can_edit: bool = False
    bot_has_main_app: bool = False
    bot_nochats: bool = False
    bot_business: bool = False
    bot_attach_menu: bool = False
    bot_inline_geo: bool = False
    inline_placeholder: str | None = None
    bot_group_admin_rights: list[str] = []
    bot_broadcast_admin_rights: list[str] = []
    has_preview_medias: bool = False
    starref_program: StarRefProgram | None = None
    blocked: bool = False
    access: BotAccess | None = None
    lang: str | None = None


class BotRef(Model):
    """A row in any of the four bot listings."""

    id: int = 0
    username: str | None = None
    title: str | None = None
    kind: str = "bot"
    active_users: int | None = None
    #: Non-Premium accounts get a shortened similar-bots list plus a count.
    truncated_count: int | None = None
    rating: float | None = None


class BotIds(Model):
    """The same peer in both id dialects."""

    mtproto_id: int = 0
    bot_api_id: int = 0
    kind: str = "user"
    has_access_hash: bool = False
    username: str | None = None


class BotUsernameCheck(Model):
    username: str = ""
    available: bool = False
    reason: str | None = None


class BotUsernames(Model):
    bot_id: int = 0
    usernames: list[str] = []


class BotCreated(Model):
    bot_id: int = 0
    username: str = ""
    manager: int | None = None
    token_available: bool = False


class BotEdited(Model):
    bot_id: int = 0
    name: str | None = None
    about: str | None = None
    description: str | None = None
    lang: str | None = None
    photo_id: int | None = None


class BotToken(Model):
    """A managed bot's credential. Redacted unless the caller asked to see it."""

    bot_id: int = 0
    token: str | None = None
    revoked: bool = False
    path: str | None = None


class DefaultRights(Model):
    group_rights: list[str] = []
    channel_rights: list[str] = []


class BotPermission(Model):
    bot_id: int = 0
    can_send_messages: bool = False
    emoji_status_allowed: bool = False
    key: str | None = None
    state: str | None = None
    already: bool = False


class BotVerified(Model):
    peer_id: int = 0
    verified: bool = False
    description: str | None = None


class PreviewMedia(Model):
    index: int = 0
    kind: str = "photo"
    date: str | None = None
    date_unix: int | None = None
    lang: str | None = None
    file_id: int | None = None
    size: int | None = None


class PreviewChange(Model):
    index: int | None = None
    kind: str | None = None
    lang: str | None = None
    order: list[int] = []
    deleted: int = 0
    remaining: int = 0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class BotStarted(Model):
    bot_id: int = 0
    chat_id: int = 0
    msg_id: int = 0
    start_param: str | None = None
    admin_rights: list[str] = []
    unblocked: bool = False


class BotStopped(Model):
    bot_id: int = 0
    blocked: bool = False
    history_deleted: int = 0


class CommandSent(Model):
    chat_id: int = 0
    msg_id: int = 0
    text: str = ""
    via_bot: str | None = None


class BotCommandSet(Model):
    scope: str = "default"
    lang: str = ""
    commands: list[BotCommand] = []
    cleared: bool = False


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


class Pressed(Model):
    """What pressing a button produced. `kind` says which fields are filled."""

    kind: str = ""
    row: int | None = None
    col: int | None = None
    n: int | None = None
    text: str | None = None
    message: str | None = None
    alert: bool = False
    url: str | None = None
    native_ui: bool = False
    cache_time: int | None = None
    query_id: str | None = None
    copy_text: str | None = None
    user: dict[str, Any] | None = None
    peers: list[int] = []
    results: list[dict[str, Any]] = []
    sent_message_id: int | None = None
    auth: dict[str, Any] | None = None


class UrlAuth(Model):
    """A seamless-login request, inspected or completed."""

    result: str = ""
    bot: str | None = None
    domain: str | None = None
    verified_app_name: str | None = None
    is_app: bool = False
    browser: str | None = None
    platform: str | None = None
    ip: str | None = None
    region: str | None = None
    request_write_access: bool = False
    request_phone_number: bool = False
    match_codes: bool = False
    match_codes_first: bool = False
    user_id_hint: int | None = None
    url: str | None = None
    code_valid: bool | None = None
    write_allowed: bool = False
    phone_shared: bool = False
    declined: bool = False


# ---------------------------------------------------------------------------
# Bot-side plumbing
# ---------------------------------------------------------------------------


class BotQuery(Model):
    """One pending query out of the daemon's bot-update buffer."""

    query_id: str = ""
    kind: str = ""
    user_id: int | None = None
    peer_id: int | None = None
    msg_id: int | None = None
    inline_msg_id: str | None = None
    data: str | None = None
    query: str | None = None
    payload: str | None = None
    answered: bool = False
    expires_at: str | None = None
    message: dict[str, Any] | None = None


class BotAnswer(Model):
    query_id: str = ""
    kind: str = ""
    answered: bool = False


class BotApiResult(Model):
    """An opaque `DataJSON` reply, passed through verbatim."""

    method: str = ""
    result: Any = None


class BusinessConnection(Model):
    connection_id: str = ""
    user_id: int | None = None
    dc_id: int | None = None
    date: str | None = None
    date_unix: int | None = None
    rights: list[str] = []
    disabled: bool = False
    result: Any = None


class StreamProgress(Model):
    chat_id: int = 0
    draft_id: int = 0
    chunks_sent: int = 0
    stopped: bool = False


# ---------------------------------------------------------------------------
# Attachment menu, ads, games
# ---------------------------------------------------------------------------


class AttachMenuBot(Model):
    bot_id: int = 0
    username: str | None = None
    short_name: str | None = None
    peer_types: list[str] = []
    inactive: bool = False
    request_write_access: bool = False
    show_in_attach_menu: bool = False
    show_in_side_menu: bool = False
    side_menu_disclaimer_needed: bool = False


class ToggledAttachMenu(Model):
    bot_id: int = 0
    installed: bool = False
    write_allowed: bool = False


class RecentBots(Model):
    enabled: bool = True
    kind: str = "pm"
    forgotten: list[int] = []


class SponsoredRead(Model):
    random_id: str = ""
    viewed: bool = False
    clicked: bool = False


class ReportOutcome(Model):
    """One step of the report option tree, or its end."""

    result: str = ""
    title: str | None = None
    options: list[dict[str, str]] = []
    reported: bool = False


class EmojiGame(Model):
    """`messages.getEmojiGameInfo`, as the method actually answers.

    The parameters the work list called "stakes and payouts" arrive as one
    opaque `params` vector plus the caller's own streak; reporting invented
    field names over them would be a schema nobody could check against the
    server. `ton_enabled` is false whenever the server says the game is
    unavailable — and staking TON is a financial action tlgr does not perform
    either way.
    """

    emoticon: str = ""
    available: bool = False
    game_hash: str | None = None
    prev_stake: int | None = None
    current_streak: int | None = None
    params: list[int] = []
    plays_left: int | None = None
    ton_enabled: bool = False


class GameSent(Model):
    chat_id: int = 0
    msg_id: int = 0
    game_id: int | None = None
    short_name: str = ""


class HighScore(Model):
    position: int = 0
    user_id: int = 0
    score: int = 0


class ScoreSet(Model):
    user_id: int = 0
    score: int = 0
    position: int | None = None


# ---------------------------------------------------------------------------
# Layer-229 shapes tlgr models but cannot yet call
# ---------------------------------------------------------------------------


class EphemeralSent(Model):
    chat_id: int = 0
    ephemeral_id: int = 0
    receiver_id: int | None = None
    anchor: bool = False


class EphemeralDeleted(Model):
    chat_id: int = 0
    deleted: int = 0
    dismissed: bool = False


class BotWelcomeMessage(Model):
    id: int = 0
    text: str = ""
    entities: list[dict[str, Any]] = []
    reply_markup: Keyboard | None = None
    media: dict[str, Any] | None = None


class WelcomeSet(Model):
    chat_id: int = 0
    id: int = 0
    text: str = ""


class WelcomeDeleted(Model):
    chat_id: int = 0
    deleted: int = 0
