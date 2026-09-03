"""The event taxonomy: every `Update*` constructor, named or explained.

This is the table `docs/design/EVENTS.md` documents, `tlgr events list` prints,
`tlgr watch --events` selects from and `tlgr/daemon/events.py` normalises
against. It lives in `core/` because all three of `ops/`, `daemon/` and the
doc generator need it and none of them may import each other (§2.2).

Two rules make the table worth trusting, and both are asserted by
`tests/test_event_taxonomy.py`:

* **every constructor is accounted for.** A constructor is either mapped to a
  tlgr event type or listed in `INTERNAL` with the reason it is not surfaced.
  A constructor that is merely missing would be an update tlgr silently drops
  with nobody able to tell — which is exactly what v1's polling watch did to
  everything that was not a new message.
* **no type is invented.** A name here is a name a consumer can filter on
  forever. An update whose meaning we have not worked out is `INTERNAL`, not
  a type called `unknown`.

The table is written against Telethon 1.44 (layer 227). Constructors Telegram
has added since carry `since_layer=229` and `available=False`: they are listed
so that `tlgr events list` can say "this exists and this build cannot parse
it", which is a far more useful answer than their absence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "ALIASES",
    "CONSTRUCTORS",
    "GROUPS",
    "INTERNAL",
    "TYPES",
    "EventTypeSpec",
    "constructors_for",
    "group_of",
    "resolve_selectors",
    "type_for_constructor",
]

#: The families `--events` accepts as a shorthand, and `events list --group`
#: filters by. Derived from the first segment of a type name, with the two
#: presence types folded in by hand.
GROUPS: tuple[str, ...] = (
    "message",
    "read",
    "presence",
    "peer",
    "member",
    "dialog",
    "story",
    "collection",
    "call",
    "bot",
    "stars",
    "secret",
    "account",
    "sync",
)


@dataclass(frozen=True, slots=True)
class EventTypeSpec:
    """One tlgr event type: what it means and where it comes from."""

    type: str
    group: str
    summary: str
    #: field name → a short type description. Documented rather than a JSON
    #: Schema because most payloads are the update's own fields, made
    #: JSON-safe; `events get --json-schema` renders this into one.
    payload: dict[str, str] = field(default_factory=dict)
    #: Which sequence box orders it: pts, qts, seq, channel_pts, version or
    #: none. A consumer that wants gap-free delivery needs to know.
    box: str = "none"
    bot_only: bool = False
    #: 0 when Telethon 1.44 can parse every source constructor.
    since_layer: int = 0
    telethon: str = "raw"
    #: Set on the handful of types no constructor produces on its own: they
    #: are derived by tlgr (a service message inside `updateNewMessage`) or
    #: synthesised by the daemon (health, a revoked session from a push).
    derived: str = ""


def _t(
    type_: str,
    group: str,
    summary: str,
    *,
    payload: dict[str, str] | None = None,
    box: str = "none",
    bot_only: bool = False,
    since_layer: int = 0,
    telethon: str = "raw",
    derived: str = "",
) -> EventTypeSpec:
    return EventTypeSpec(
        type=type_,
        group=group,
        summary=summary,
        payload=payload or {},
        box=box,
        bot_only=bot_only,
        since_layer=since_layer,
        telethon=telethon,
        derived=derived,
    )


#: The payload every generically-normalised event carries: the update's own
#: fields, made JSON-safe (datetimes as RFC-3339, bytes as hex, nested TL
#: objects as `{"_": "ClassName", …}`).
_RAW_PAYLOAD = {
    "_": "str — the source TL constructor name",
    "…": "the update's own fields, JSON-safe",
}

_TYPES: tuple[EventTypeSpec, ...] = (
    # -- message ----------------------------------------------------------
    _t(
        "message_new",
        "message",
        "A message arrived in any chat the account can see",
        payload={"message": "Message"},
        box="pts",
        telethon="high-level: events.NewMessage",
    ),
    _t(
        "message_service",
        "message",
        "A service message: a join, a pin, a title change, a call",
        payload={"message": "Message", "action": "str — the MessageAction name"},
        box="pts",
        telethon="high-level: events.ChatAction (subset)",
        derived="updateNewMessage / updateNewChannelMessage carrying a messageService",
    ),
    _t(
        "message_edited",
        "message",
        "A message was edited",
        payload={"message": "Message — post-edit"},
        box="pts",
        telethon="high-level: events.MessageEdited",
    ),
    _t(
        "message_deleted",
        "message",
        "Messages were deleted",
        payload={"message_ids": "list[int]", "channel_id": "int | null"},
        box="pts",
        telethon="high-level: events.MessageDeleted",
    ),
    _t(
        "message_id_assigned",
        "message",
        "An outgoing message got its server id (random_id reconciliation)",
        payload={"msg_id": "int", "random_id": "int | null"},
        box="pts",
    ),
    _t(
        "message_pinned",
        "message",
        "Messages were pinned or unpinned",
        payload={"message_ids": "list[int]", "pinned": "bool"},
        box="pts",
    ),
    _t(
        "message_views",
        "message",
        "A channel post's view counter moved",
        payload={"msg_id": "int", "views": "int"},
        box="channel_pts",
    ),
    _t(
        "message_forwards",
        "message",
        "A channel post's forward counter moved",
        payload={"msg_id": "int", "forwards": "int"},
        box="channel_pts",
    ),
    _t(
        "message_poll",
        "message",
        "A poll's results changed",
        payload={"poll_id": "int", "poll": "object | null", "results": "object"},
        box="pts",
    ),
    _t(
        "message_poll_vote",
        "message",
        "Somebody voted in a poll you can see the votes of",
        payload={"poll_id": "int", "peer": "object", "options": "list[str]"},
        box="qts",
    ),
    _t(
        "message_reactions",
        "message",
        "Reactions on a message changed",
        payload={"msg_id": "int", "reactions": "object", "top_msg_id": "int | null"},
        box="pts",
    ),
    _t(
        "message_extended_media",
        "message",
        "Paid media on a message was unlocked",
        payload={"msg_id": "int", "extended_media": "list[object]"},
        box="pts",
    ),
    _t(
        "message_transcribed",
        "message",
        "A voice or video note transcription finished",
        payload={
            "msg_id": "int",
            "transcription_id": "int",
            "text": "str",
            "pending": "bool",
        },
    ),
    _t(
        "message_webpage",
        "message",
        "A link preview finished resolving",
        payload={"webpage": "object"},
        box="pts",
    ),
    _t(
        "message_scheduled_new",
        "message",
        "A scheduled message was queued",
        payload={"message": "Message"},
    ),
    _t(
        "message_scheduled_deleted",
        "message",
        "A scheduled message fired or was cancelled",
        payload={"message_ids": "list[int]", "sent": "bool"},
    ),
    _t(
        "message_available_min",
        "message",
        "A channel's history was cleared below a point",
        payload={"available_min_id": "int"},
    ),
    _t(
        "message_emoji_game",
        "message",
        "An emoji game (dice, dart, slot) resolved",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "message_geo_live_viewed",
        "message",
        "Somebody viewed a live location I am sharing",
        payload={"peer": "object", "msg_id": "int"},
    ),
    # -- read -------------------------------------------------------------
    _t(
        "read_inbox",
        "read",
        "My read position moved: messages I have now seen",
        payload={"max_id": "int", "still_unread_count": "int | null", "outbox": "false"},
        box="pts",
        telethon="high-level: events.MessageRead(inbox=True)",
    ),
    _t(
        "read_outbox",
        "read",
        "The other side read my messages",
        payload={"max_id": "int", "outbox": "true"},
        box="pts",
        telethon="high-level: events.MessageRead(inbox=False)",
    ),
    _t(
        "read_contents",
        "read",
        "Media or a mention was marked read (the media_unread flag)",
        payload={"message_ids": "list[int]"},
        box="pts",
    ),
    _t(
        "read_discussion",
        "read",
        "A comment thread's read position moved",
        payload={"msg_id": "int", "read_max_id": "int", "outbox": "bool"},
        box="pts",
    ),
    _t(
        "read_monoforum",
        "read",
        "A direct-messages (monoforum) channel's read position moved",
        payload={"saved_peer_id": "object", "read_max_id": "int", "outbox": "bool"},
        box="pts",
    ),
    # -- presence ---------------------------------------------------------
    _t(
        "user_status",
        "presence",
        "A user came online, or their last-seen changed",
        payload={"user_id": "int", "status": "str", "online": "bool", "was_online": "int | null"},
        telethon="high-level: events.UserUpdate",
    ),
    _t(
        "typing",
        "presence",
        "Somebody is typing, recording or uploading",
        payload={
            "user_id": "int | null",
            "action": "str — the SendMessageAction name",
            "progress": "int | null",
            "top_msg_id": "int | null",
        },
        telethon="high-level: events.UserUpdate",
    ),
    # -- peer -------------------------------------------------------------
    _t(
        "peer_user_changed",
        "peer",
        "A user record was invalidated and should be refetched",
        payload={"user_id": "int"},
    ),
    _t(
        "peer_user_name",
        "peer",
        "A user changed their name or username",
        payload={
            "user_id": "int",
            "first_name": "str",
            "last_name": "str",
            "usernames": "list[str]",
        },
    ),
    _t(
        "peer_user_phone",
        "peer",
        "A contact's phone number changed",
        payload={"user_id": "int", "phone": "str"},
    ),
    _t(
        "peer_user_emoji_status",
        "peer",
        "A user's emoji status changed",
        payload={"user_id": "int", "emoji_status": "object"},
    ),
    _t(
        "peer_chat_changed",
        "peer",
        "A chat or channel record was invalidated (refetch; may mean kicked)",
        payload={"chat_id": "int"},
    ),
    _t(
        "peer_blocked",
        "peer",
        "A peer was blocked or unblocked",
        payload={"peer_id": "object", "blocked": "bool", "blocked_my_stories_from": "bool"},
    ),
    _t(
        "peer_settings",
        "peer",
        "A peer's action-bar settings changed (anti-scam hints included)",
        payload={"peer": "object", "settings": "object"},
    ),
    _t(
        "peer_located",
        "peer",
        "The people/groups-nearby list changed",
        payload={"peers": "list[object]"},
    ),
    _t(
        "peer_wallpaper",
        "peer",
        "A chat wallpaper changed",
        payload={"peer": "object", "wallpaper": "object | null"},
    ),
    _t(
        "peer_history_ttl",
        "peer",
        "A chat's auto-delete timer changed",
        payload={"peer": "object", "ttl_period": "int | null"},
    ),
    _t(
        "peer_notify_settings",
        "peer",
        "Notification settings changed for a peer or a scope",
        payload={"peer": "object", "notify_settings": "object"},
    ),
    # -- member -----------------------------------------------------------
    _t(
        "member_channel",
        "member",
        "A channel or supergroup member or admin changed",
        payload={
            "channel_id": "int",
            "user_id": "int",
            "actor_id": "int | null",
            "prev_participant": "object | null",
            "new_participant": "object | null",
        },
        box="qts",
    ),
    _t(
        "member_chat",
        "member",
        "A basic group's membership or admin list changed",
        payload={
            "chat_id": "int",
            "user_id": "int | null",
            "version": "int | null",
            "participants": "object | null",
        },
        box="version",
    ),
    _t(
        "member_default_rights",
        "member",
        "A group's default permissions changed",
        payload={"peer": "object", "default_banned_rights": "object", "version": "int"},
        box="version",
    ),
    _t(
        "member_join_request",
        "member",
        "A pending join request arrived or was resolved",
        payload={"peer": "object", "requests_pending": "int | null", "recent_requesters": "list"},
    ),
    _t(
        "member_boost",
        "member",
        "A channel boost was applied",
        payload={"peer": "object", "boost": "object"},
        bot_only=True,
    ),
    # -- dialog -----------------------------------------------------------
    _t(
        "dialog_pinned",
        "dialog",
        "A chat was pinned, unpinned or reordered in the list",
        payload={"peer": "object | null", "order": "list[object] | null", "pinned": "bool"},
    ),
    _t(
        "dialog_unread_mark",
        "dialog",
        "A chat was manually marked unread (or the mark was cleared)",
        payload={"peer": "object", "unread": "bool"},
    ),
    _t(
        "dialog_folder",
        "dialog",
        "A chat moved into or out of the Archive",
        payload={"folder_peers": "list[object]"},
        box="pts",
    ),
    _t(
        "dialog_filters",
        "dialog",
        "Chat folders (dialog filters) changed",
        payload={"id": "int | null", "filter": "object | null", "order": "list[int] | null"},
    ),
    _t(
        "dialog_draft",
        "dialog",
        "A cloud draft was set or cleared",
        payload={"peer": "object", "draft": "object", "top_msg_id": "int | null"},
    ),
    _t(
        "dialog_saved_pinned",
        "dialog",
        "A Saved Messages sub-dialog was pinned or reordered",
        payload={"peer": "object | null", "order": "list[object] | null", "pinned": "bool"},
    ),
    _t(
        "dialog_saved_tags",
        "dialog",
        "Saved-message reaction tags changed",
        payload={"saved_peer_id": "object | null"},
    ),
    _t(
        "dialog_forum_pinned",
        "dialog",
        "Forum topics were pinned or reordered",
        payload={"channel_id": "int", "topic_id": "int | null", "order": "list[int] | null"},
    ),
    _t(
        "dialog_forum_view",
        "dialog",
        "A forum's display mode was toggled",
        payload={"channel_id": "int", "enabled": "bool"},
    ),
    _t(
        "dialog_quick_reply",
        "dialog",
        "Business quick-reply shortcuts changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "dialog_monoforum_no_paid",
        "dialog",
        "A direct-messages channel's paid-message exception changed",
        payload={"channel_id": "int", "saved_peer_id": "object", "exception": "bool"},
    ),
    # -- story ------------------------------------------------------------
    _t(
        "story_new",
        "story",
        "A story was posted, edited or deleted",
        payload={"peer": "object", "story": "object"},
    ),
    _t(
        "story_id",
        "story",
        "A story you posted got its server id",
        payload={"id": "int", "random_id": "int"},
    ),
    _t(
        "story_read",
        "story",
        "Stories were marked read",
        payload={"peer": "object", "max_id": "int"},
    ),
    _t(
        "story_reaction",
        "story",
        "A story was reacted to",
        payload={"peer": "object", "story_id": "int", "reaction": "object"},
    ),
    _t(
        "story_stealth",
        "story",
        "Story stealth mode changed",
        payload={"stealth_mode": "object"},
    ),
    # -- collection -------------------------------------------------------
    _t(
        "collection_stickers",
        "collection",
        "Sticker or custom-emoji sets changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "collection_stickers_read",
        "collection",
        "Featured sticker or emoji sets were marked read",
        payload={"message_ids": "list[int]"},
    ),
    _t("collection_gifs", "collection", "Saved GIFs changed", payload={}),
    _t("collection_ringtones", "collection", "Notification sounds changed", payload={}),
    _t("collection_reactions", "collection", "Recent or top reactions changed", payload={}),
    _t("collection_emoji_statuses", "collection", "Recent emoji statuses changed", payload={}),
    _t(
        "collection_themes",
        "collection",
        "A theme changed",
        payload={"theme": "object"},
    ),
    _t(
        "collection_attach_menu",
        "collection",
        "The attachment-menu bot list changed",
        payload={},
    ),
    # -- call -------------------------------------------------------------
    _t(
        "call_phone",
        "call",
        "An incoming or updated 1:1 call (signalling only; tlgr carries no media)",
        payload={"phone_call": "object"},
    ),
    _t(
        "call_signaling",
        "call",
        "Raw call signalling data",
        payload={"phone_call_id": "int", "data": "str — hex"},
    ),
    _t(
        "call_group",
        "call",
        "A group call, video chat or live stream changed",
        payload={"chat_id": "int | null", "call": "object"},
    ),
    _t(
        "call_group_participants",
        "call",
        "Group-call participants changed",
        payload={"call": "object", "participants": "list[object]", "version": "int"},
        box="version",
    ),
    _t(
        "call_group_message",
        "call",
        "A message inside a group call was posted or deleted",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "call_group_encrypted",
        "call",
        "Encrypted group-call key material (conference calls)",
        payload=dict(_RAW_PAYLOAD),
    ),
    # -- bot --------------------------------------------------------------
    _t(
        "bot_callback_query",
        "bot",
        "An inline-keyboard button was pressed",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
        telethon="high-level: events.CallbackQuery",
    ),
    _t(
        "bot_inline_query",
        "bot",
        "An inline query arrived, or a result was chosen",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
        telethon="high-level: events.InlineQuery",
    ),
    _t(
        "bot_precheckout",
        "bot",
        "A pre-checkout query arrived",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_shipping",
        "bot",
        "A shipping query arrived",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_paid_media_purchased",
        "bot",
        "A user bought paid media from this bot",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_stopped",
        "bot",
        "A user started or stopped this bot",
        payload={"user_id": "int", "stopped": "bool", "date": "str"},
        box="qts",
        bot_only=True,
    ),
    _t("bot_commands", "bot", "A bot's command list changed", payload=dict(_RAW_PAYLOAD)),
    _t("bot_menu_button", "bot", "A bot's menu button changed", payload=dict(_RAW_PAYLOAD)),
    _t(
        "bot_webhook",
        "bot",
        "A bot-webhook JSON passthrough arrived",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_business_connection",
        "bot",
        "A business connection was created or changed",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_business_message",
        "bot",
        "A message on a connected business account arrived, changed or went",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_message_reaction",
        "bot",
        "A reaction on a message this bot can see changed",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_guest_chat_query",
        "bot",
        "A guest-mode chat query arrived",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_managed",
        "bot",
        "A bot you manage changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "bot_webview_result",
        "bot",
        "A mini app sent data back",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
    ),
    _t(
        "bot_webview_join_decision",
        "bot",
        "A join-chat decision was made inside a mini app",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "bot_stars_subscription",
        "bot",
        "A Stars subscription to this bot changed",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
        since_layer=229,
    ),
    _t(
        "bot_ephemeral_callback",
        "bot",
        "A callback button on an ephemeral message was pressed",
        payload=dict(_RAW_PAYLOAD),
        bot_only=True,
        since_layer=229,
    ),
    # -- stars ------------------------------------------------------------
    _t(
        "stars_balance",
        "stars",
        "The Telegram Stars balance changed",
        payload={"balance": "object"},
    ),
    _t(
        "stars_revenue",
        "stars",
        "Star revenue or withdrawal status changed",
        payload={"peer": "object", "status": "object"},
    ),
    _t(
        "stars_gift_auction",
        "stars",
        "A star-gift auction or craft changed state",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "stars_paid_reaction_privacy",
        "stars",
        "Paid-reaction privacy changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    # -- secret -----------------------------------------------------------
    _t(
        "secret_chat",
        "secret",
        "A secret chat was requested, accepted or discarded",
        payload={"chat": "object"},
        box="qts",
    ),
    _t(
        "secret_message",
        "secret",
        "Encrypted traffic arrived; tlgr acknowledges it but cannot decrypt it",
        payload={"chat_id": "int", "date": "str", "decrypted": "false"},
        box="qts",
    ),
    _t(
        "secret_read",
        "secret",
        "Secret-chat messages were read or expired",
        payload={"chat_id": "int", "max_date": "str"},
        box="qts",
    ),
    # -- account ----------------------------------------------------------
    _t(
        "account_privacy",
        "account",
        "A privacy rule changed",
        payload={"key": "str", "rules": "list[object]"},
    ),
    _t(
        "account_new_authorization",
        "account",
        "A new login on this account",
        payload={"hash": "int", "device": "str", "location": "str", "unconfirmed": "bool"},
    ),
    _t(
        "account_service_notification",
        "account",
        "An official service notification (from 777000)",
        payload={"type": "str", "message": "str", "popup": "bool", "inbox_date": "str | null"},
    ),
    _t(
        "account_login_token",
        "account",
        "A QR login token was accepted",
        payload={},
    ),
    _t(
        "account_sent_phone_code",
        "account",
        "A login code was delivered in-app",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "account_autosave",
        "account",
        "Media auto-save settings changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "account_browser_settings",
        "account",
        "In-app browser settings or a per-domain exception changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t("account_contacts_reset", "account", "The contact list was wiped", payload={}),
    _t(
        "account_ai_tones",
        "account",
        "The AI compose tone list changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "account_sms_job",
        "account",
        "An SMS-relay job arrived (Telegram's peer-to-peer login SMS programme)",
        payload={"job_id": "str"},
    ),
    _t(
        "account_langpack",
        "account",
        "The language pack changed",
        payload=dict(_RAW_PAYLOAD),
    ),
    _t(
        "account_session_revoked",
        "account",
        "This session was terminated elsewhere (from a push payload)",
        payload={"reason": "str"},
        derived="the SESSION_REVOKE push payload, decoded by `tlgr events decode`",
    ),
    # -- sync -------------------------------------------------------------
    _t(
        "sync_config",
        "sync",
        "The server configuration was invalidated; re-read help.getConfig",
        payload={},
    ),
    _t(
        "sync_dc_options",
        "sync",
        "The data-centre address list changed",
        payload={"dc_options": "list[object]"},
    ),
    _t(
        "sync_pts_changed",
        "sync",
        "The pts sequence was reset; some updates are unrecoverable",
        payload={},
    ),
    _t(
        "sync_channel_too_long",
        "sync",
        "A channel's gap is unrecoverable from its pts; a resync is needed",
        payload={"channel_id": "int", "pts": "int | null"},
    ),
    _t(
        "daemon_health",
        "sync",
        "An account changed state, or the circuit breaker opened",
        payload={"state": "str", "reason": "str", "account": "str"},
        telethon="n/a — synthesised by the daemon",
        derived="the session state machine (ARCHITECTURE §6.2)",
    ),
)

TYPES: dict[str, EventTypeSpec] = {spec.type: spec for spec in _TYPES}


# ---------------------------------------------------------------------------
# Constructor → type
# ---------------------------------------------------------------------------

#: `Update*` constructor name → the tlgr event type it becomes.
CONSTRUCTORS: dict[str, str] = {
    # message
    "UpdateNewMessage": "message_new",
    "UpdateNewChannelMessage": "message_new",
    "UpdateShortMessage": "message_new",
    "UpdateShortChatMessage": "message_new",
    "UpdateQuickReplyMessage": "dialog_quick_reply",
    "UpdateEditMessage": "message_edited",
    "UpdateEditChannelMessage": "message_edited",
    "UpdateDeleteMessages": "message_deleted",
    "UpdateDeleteChannelMessages": "message_deleted",
    "UpdateMessageID": "message_id_assigned",
    "UpdateShortSentMessage": "message_id_assigned",
    "UpdatePinnedMessages": "message_pinned",
    "UpdatePinnedChannelMessages": "message_pinned",
    "UpdateChannelMessageViews": "message_views",
    "UpdateChannelMessageForwards": "message_forwards",
    "UpdateMessagePoll": "message_poll",
    "UpdateMessagePollVote": "message_poll_vote",
    "UpdateMessageReactions": "message_reactions",
    "UpdateMessageExtendedMedia": "message_extended_media",
    "UpdateTranscribedAudio": "message_transcribed",
    "UpdateWebPage": "message_webpage",
    "UpdateChannelWebPage": "message_webpage",
    "UpdateNewScheduledMessage": "message_scheduled_new",
    "UpdateDeleteScheduledMessages": "message_scheduled_deleted",
    "UpdateChannelAvailableMessages": "message_available_min",
    "UpdateEmojiGameInfo": "message_emoji_game",
    "UpdateGeoLiveViewed": "message_geo_live_viewed",
    # read
    "UpdateReadHistoryInbox": "read_inbox",
    "UpdateReadChannelInbox": "read_inbox",
    "UpdateReadHistoryOutbox": "read_outbox",
    "UpdateReadChannelOutbox": "read_outbox",
    "UpdateReadMessagesContents": "read_contents",
    "UpdateChannelReadMessagesContents": "read_contents",
    "UpdateReadChannelDiscussionInbox": "read_discussion",
    "UpdateReadChannelDiscussionOutbox": "read_discussion",
    "UpdateReadMonoForumInbox": "read_monoforum",
    "UpdateReadMonoForumOutbox": "read_monoforum",
    # presence
    "UpdateUserStatus": "user_status",
    "UpdateUserTyping": "typing",
    "UpdateChatUserTyping": "typing",
    "UpdateChannelUserTyping": "typing",
    "UpdateEncryptedChatTyping": "typing",
    # peer
    "UpdateUser": "peer_user_changed",
    "UpdateUserName": "peer_user_name",
    "UpdateUserPhone": "peer_user_phone",
    "UpdateUserEmojiStatus": "peer_user_emoji_status",
    "UpdateChat": "peer_chat_changed",
    "UpdateChannel": "peer_chat_changed",
    "UpdatePeerBlocked": "peer_blocked",
    "UpdatePeerSettings": "peer_settings",
    "UpdatePeerLocated": "peer_located",
    "UpdatePeerWallpaper": "peer_wallpaper",
    "UpdatePeerHistoryTTL": "peer_history_ttl",
    "UpdateNotifySettings": "peer_notify_settings",
    # member
    "UpdateChannelParticipant": "member_channel",
    "UpdateChatParticipant": "member_chat",
    "UpdateChatParticipants": "member_chat",
    "UpdateChatParticipantAdd": "member_chat",
    "UpdateChatParticipantDelete": "member_chat",
    "UpdateChatParticipantAdmin": "member_chat",
    "UpdateChatParticipantRank": "member_chat",
    "UpdateChatDefaultBannedRights": "member_default_rights",
    "UpdatePendingJoinRequests": "member_join_request",
    "UpdateBotChatInviteRequester": "member_join_request",
    "UpdateBotChatBoost": "member_boost",
    # dialog
    "UpdateDialogPinned": "dialog_pinned",
    "UpdatePinnedDialogs": "dialog_pinned",
    "UpdateDialogUnreadMark": "dialog_unread_mark",
    "UpdateFolderPeers": "dialog_folder",
    "UpdateDialogFilter": "dialog_filters",
    "UpdateDialogFilterOrder": "dialog_filters",
    "UpdateDialogFilters": "dialog_filters",
    "UpdateDraftMessage": "dialog_draft",
    "UpdateSavedDialogPinned": "dialog_saved_pinned",
    "UpdatePinnedSavedDialogs": "dialog_saved_pinned",
    "UpdateSavedReactionTags": "dialog_saved_tags",
    "UpdatePinnedForumTopic": "dialog_forum_pinned",
    "UpdatePinnedForumTopics": "dialog_forum_pinned",
    "UpdateChannelViewForumAsMessages": "dialog_forum_view",
    "UpdateQuickReplies": "dialog_quick_reply",
    "UpdateNewQuickReply": "dialog_quick_reply",
    "UpdateDeleteQuickReply": "dialog_quick_reply",
    "UpdateDeleteQuickReplyMessages": "dialog_quick_reply",
    "UpdateMonoForumNoPaidException": "dialog_monoforum_no_paid",
    # story
    "UpdateStory": "story_new",
    "UpdateStoryID": "story_id",
    "UpdateReadStories": "story_read",
    "UpdateNewStoryReaction": "story_reaction",
    "UpdateSentStoryReaction": "story_reaction",
    "UpdateStoriesStealthMode": "story_stealth",
    # collection
    "UpdateStickerSets": "collection_stickers",
    "UpdateStickerSetsOrder": "collection_stickers",
    "UpdateNewStickerSet": "collection_stickers",
    "UpdateRecentStickers": "collection_stickers",
    "UpdateFavedStickers": "collection_stickers",
    "UpdateMoveStickerSetToTop": "collection_stickers",
    "UpdateReadFeaturedStickers": "collection_stickers_read",
    "UpdateReadFeaturedEmojiStickers": "collection_stickers_read",
    "UpdateSavedGifs": "collection_gifs",
    "UpdateSavedRingtones": "collection_ringtones",
    "UpdateRecentReactions": "collection_reactions",
    "UpdateRecentEmojiStatuses": "collection_emoji_statuses",
    "UpdateTheme": "collection_themes",
    "UpdateAttachMenuBots": "collection_attach_menu",
    # call
    "UpdatePhoneCall": "call_phone",
    "UpdatePhoneCallSignalingData": "call_signaling",
    "UpdateGroupCall": "call_group",
    "UpdateGroupCallConnection": "call_group",
    "UpdateGroupCallParticipants": "call_group_participants",
    "UpdateGroupCallMessage": "call_group_message",
    "UpdateDeleteGroupCallMessages": "call_group_message",
    "UpdateGroupCallChainBlocks": "call_group_encrypted",
    "UpdateGroupCallEncryptedMessage": "call_group_encrypted",
    # bot
    "UpdateBotCallbackQuery": "bot_callback_query",
    "UpdateInlineBotCallbackQuery": "bot_callback_query",
    "UpdateBusinessBotCallbackQuery": "bot_callback_query",
    "UpdateBotInlineQuery": "bot_inline_query",
    "UpdateBotInlineSend": "bot_inline_query",
    "UpdateBotPrecheckoutQuery": "bot_precheckout",
    "UpdateBotShippingQuery": "bot_shipping",
    "UpdateBotPurchasedPaidMedia": "bot_paid_media_purchased",
    "UpdateBotStopped": "bot_stopped",
    "UpdateBotCommands": "bot_commands",
    "UpdateBotMenuButton": "bot_menu_button",
    "UpdateBotWebhookJSON": "bot_webhook",
    "UpdateBotWebhookJSONQuery": "bot_webhook",
    "UpdateBotBusinessConnect": "bot_business_connection",
    "UpdateNewBotConnection": "bot_business_connection",
    "UpdateBotNewBusinessMessage": "bot_business_message",
    "UpdateBotEditBusinessMessage": "bot_business_message",
    "UpdateBotDeleteBusinessMessage": "bot_business_message",
    "UpdateBotMessageReaction": "bot_message_reaction",
    "UpdateBotMessageReactions": "bot_message_reaction",
    "UpdateBotGuestChatQuery": "bot_guest_chat_query",
    "UpdateManagedBot": "bot_managed",
    "UpdateWebViewResultSent": "bot_webview_result",
    "UpdateJoinChatWebViewDecision": "bot_webview_join_decision",
    # stars
    "UpdateStarsBalance": "stars_balance",
    "UpdateStarsRevenueStatus": "stars_revenue",
    "UpdateStarGiftAuctionState": "stars_gift_auction",
    "UpdateStarGiftAuctionUserState": "stars_gift_auction",
    "UpdateStarGiftCraftFail": "stars_gift_auction",
    "UpdatePaidReactionPrivacy": "stars_paid_reaction_privacy",
    # secret
    "UpdateEncryption": "secret_chat",
    "UpdateNewEncryptedMessage": "secret_message",
    "UpdateEncryptedMessagesRead": "secret_read",
    # account
    "UpdatePrivacy": "account_privacy",
    "UpdateNewAuthorization": "account_new_authorization",
    "UpdateServiceNotification": "account_service_notification",
    "UpdateLoginToken": "account_login_token",
    "UpdateSentPhoneCode": "account_sent_phone_code",
    "UpdateAutoSaveSettings": "account_autosave",
    "UpdateWebBrowserSettings": "account_browser_settings",
    "UpdateWebBrowserException": "account_browser_settings",
    "UpdateContactsReset": "account_contacts_reset",
    "UpdateAiComposeTones": "account_ai_tones",
    "UpdateSmsJob": "account_sms_job",
    "UpdateLangPack": "account_langpack",
    "UpdateLangPackTooLong": "account_langpack",
    # sync
    "UpdateConfig": "sync_config",
    "UpdateDcOptions": "sync_dc_options",
    "UpdatePtsChanged": "sync_pts_changed",
    "UpdateChannelTooLong": "sync_channel_too_long",
}

#: Constructors that carry no event of their own, and why. Being on this list
#: is a decision; being on neither list is a bug the taxonomy test catches.
INTERNAL: dict[str, str] = {
    "UpdateShort": ("container: carries exactly one Update, which is normalised in its place"),
    "Updates": "container: a batch of updates plus their users/chats arrays",
    "UpdatesCombined": "container: a batch of updates spanning a seq range",
    "UpdatesTooLong": (
        "transport signal: the common box overflowed, handled by the supervisor "
        "with updates.getDifference (see `tlgr sync catch-up`)"
    ),
}

#: Constructors Telegram ships that Telethon 1.44 (layer 227) cannot parse.
#: Listed so `events list` can say "exists, unavailable here" rather than
#: leaving a silent hole; a raw handler sees only an unknown constructor id.
NEWER_THAN_LAYER_227: dict[str, str] = {
    "UpdateNewEphemeralMessage": "message_new",
    "UpdateEditEphemeralMessage": "message_edited",
    "UpdateDeleteEphemeralMessages": "message_deleted",
    "UpdateBotStarsSubscription": "bot_stars_subscription",
    "UpdateBotEphemeralCallbackQuery": "bot_ephemeral_callback",
}

#: Names a consumer may still be using, and what they mean now. v1's `watch`
#: and `jobs.yaml` spelled the message events differently, and the foundation
#: shipped a nine-name starter set; both keep working (§12.4).
ALIASES: dict[str, tuple[str, ...]] = {
    "new_message": ("message_new",),
    "user_joined": ("message_service", "member_chat", "member_channel"),
    "chat_action": ("message_service", "member_chat", "member_channel"),
    "message_read": ("read_inbox", "read_outbox"),
    "reaction_changed": ("message_reactions",),
    "draft_changed": ("dialog_draft",),
    "message_edit": ("message_edited",),
}


def group_of(event_type: str) -> str:
    spec = TYPES.get(event_type)
    return spec.group if spec is not None else ""


def type_for_constructor(name: str) -> str | None:
    """The event type a `Update*` class name maps to, or None when internal."""
    found = CONSTRUCTORS.get(name)
    if found is not None:
        return found
    return NEWER_THAN_LAYER_227.get(name)


def constructors_for(event_type: str) -> tuple[str, ...]:
    """Every `Update*` constructor that produces *event_type*."""
    return tuple(
        sorted(
            name
            for mapping in (CONSTRUCTORS, NEWER_THAN_LAYER_227)
            for name, mapped in mapping.items()
            if mapped == event_type
        )
    )


def resolve_selectors(
    selectors: str | Iterable[str] | None, *, allow_all: bool = True
) -> frozenset[str]:
    """Expand `--events`/`--exclude` values into a set of event type names.

    Accepts type names, group names, the legacy names in `ALIASES`,
    `raw:UpdateFoo` (the type that constructor maps to) and `all`. An unknown
    value is a `USAGE` error rather than an empty selection: silently watching
    nothing is the failure mode that makes a user think the daemon is broken.
    """
    from tlgr.core.errors import UsageError

    if isinstance(selectors, str):
        values = [part.strip() for part in selectors.split(",")]
    else:
        values = [str(part).strip() for part in (selectors or ())]
    wanted: set[str] = set()
    for value in values:
        if not value:
            continue
        lowered = value.lower()
        if lowered in ("all", "*"):
            if not allow_all:
                raise UsageError("'all' is not accepted here", field="events")
            return frozenset(TYPES)
        if lowered.startswith("raw:"):
            constructor = value[4:]
            mapped = type_for_constructor(constructor)
            if mapped is None:
                raise UsageError(
                    f"raw:{constructor} is not an update tlgr names; see `tlgr events list --raw`",
                    field="events",
                )
            wanted.add(mapped)
            continue
        if lowered in TYPES:
            wanted.add(lowered)
            continue
        if lowered in ALIASES:
            wanted.update(ALIASES[lowered])
            continue
        if lowered in GROUPS:
            wanted.update(name for name, spec in TYPES.items() if spec.group == lowered)
            continue
        raise UsageError(
            f"unknown event selector {value!r}; run `tlgr events list` for the vocabulary",
            field="events",
        )
    return frozenset(wanted)
