"""Group and channel administration: members, invites, topics, logs, stats.

The shapes here are what the `chat member/admin/invite/topic/permission`,
`chat setting`, `chat stats` and `boost` operations answer with. Three
decisions run through the whole file.

* **A participant is never flattened into a user.** v1's `chat members`
  returned `{id, first_name, username, is_bot}` and threw away the
  `ChannelParticipant*` wrapper, so "who promoted this admin", "when did they
  join", "what may they do" and "are they banned or merely restricted" were
  all unanswerable. `Participant` keeps the wrapper: `status`, `rank`,
  `date`, `inviter_id`, `promoted_by`, `kicked_by` and both rights masks.
* **Rights are always allow-polarity.** Telegram stores banned rights
  inverted (`send_messages=True` means *cannot* send). `Rights` normalises
  once, in `models/peer.py`, so every mask in this file reads the same way
  and `chat permission get` round-trips into `chat permission set --allow`.
* **A graph is emitted verbatim.** `stats.*` answers with Telegram's own
  chart specification; tlgr reports it as the API's JSON and never tries to
  draw it, because a redrawn chart is a chart that can be wrong.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model
from tlgr.models.message import Message
from tlgr.models.peer import Peer, Rights

__all__ = [
    "AdminLogEvent",
    "AdminResult",
    "AffiliateBot",
    "AffiliateResult",
    "AntiSpamReport",
    "Boost",
    "BoostApplied",
    "BoostStatus",
    "ChatEditResult",
    "ChatPhotoResult",
    "ChatStats",
    "CommunityResult",
    "CommunityRow",
    "CreatedChat",
    "DirectBanResult",
    "DirectDialog",
    "DiscussionCandidate",
    "DiscussionResult",
    "Graph",
    "Invite",
    "InviteAdmin",
    "InviteDeleted",
    "InviteInfo",
    "InvitePeek",
    "InviteRevoked",
    "JoinRequest",
    "JoinResult",
    "MemberResult",
    "MembersAdded",
    "MigrateResult",
    "MissingInvitee",
    "Participant",
    "PermissionResult",
    "PermissionView",
    "PublicForward",
    "RequestResult",
    "RevenueSummary",
    "RevenueTransaction",
    "RightInfo",
    "SendAsPeer",
    "SendAsResult",
    "SettingResult",
    "SettingsView",
    "SimilarChat",
    "SponsoredReport",
    "StatValue",
    "SuggestedPostResult",
    "SuggestionResult",
    "Topic",
    "TopicPinResult",
    "TopicReadResult",
    "TopicResult",
    "TransferResult",
    "UsernameCheck",
    "UsernameResult",
    "VerificationResult",
    "WelcomeMessage",
    "WelcomeResult",
]


# ---------------------------------------------------------------------------
# Rights vocabulary
# ---------------------------------------------------------------------------


class RightInfo(Model):
    """One entry of the canonical right vocabulary (`chat permission list`).

    `supported` is False for a flag the installed Telethon layer cannot
    express; naming it anyway is what lets an agent discover that the gap
    exists instead of guessing why a right was silently dropped.
    """

    name: str
    mask: str
    tl_flag: str
    polarity: str
    supported: bool = True
    since_layer: int | None = None
    peer_types: list[str] = []
    grantable: bool | None = None
    summary: str = ""


# ---------------------------------------------------------------------------
# Members and admins
# ---------------------------------------------------------------------------


class Participant(Model):
    """One row of a member listing, with the participant wrapper intact."""

    id: int
    user_id: int = 0
    chat_id: int = 0
    peer: Peer | None = None
    username: str | None = None
    name: str = ""
    is_bot: bool = False
    #: creator | admin | member | self | restricted | banned | left
    status: str = "member"
    rank: str | None = None
    date: str | None = None
    date_unix: int | None = None
    inviter_id: int | None = None
    promoted_by: int | None = None
    kicked_by: int | None = None
    via_request: bool | None = None
    subscription_until_date: str | None = None
    admin_rights: Rights | None = None
    banned_rights: Rights | None = None
    effective_permissions: Rights | None = None
    can_edit: bool | None = None
    #: Only on `--via-link` rows: the invite the member came through.
    via_link: str | None = None
    approved_by: int | None = None
    about: str | None = None


class MissingInvitee(Model):
    """Someone `messages.invitedUsers` refused to add, and why.

    Reported verbatim rather than dropped: "added 3 of 5" with no names is
    not an answer anybody can act on.
    """

    user_id: int
    #: privacy-restricted | premium-would-allow-invite | premium-required-for-pm
    reason: str


class MembersAdded(Model):
    chat_id: int
    added: list[int] = []
    missing: list[MissingInvitee] = []
    invited_by_link: list[int] = []


class MemberResult(Model):
    """The answer to a moderation verb applied to one member."""

    chat_id: int
    user_id: int
    removed: bool | None = None
    banned: bool | None = None
    until: str | None = None
    until_unix: int | None = None
    allow: list[str] = []
    deny: list[str] = []
    purged_messages: int | None = None
    deleted: int | None = None
    reported: bool | None = None
    rank: str | None = None
    free_messages: bool | None = None
    already: bool = False


class AdminResult(Model):
    chat_id: int
    user_id: int
    admin_rights: Rights | None = None
    rank: str | None = None
    dropped: list[str] = []
    already: bool = False


class PermissionView(Model):
    chat_id: int
    allow: list[str] = []
    deny: list[str] = []
    rights: Rights | None = None


class PermissionResult(Model):
    chat_id: int
    allow: list[str] = []
    deny: list[str] = []
    changed: bool = False
    already: bool = False


# ---------------------------------------------------------------------------
# Invites and join requests
# ---------------------------------------------------------------------------


class Invite(Model):
    link: str
    title: str | None = None
    permanent: bool = False
    revoked: bool = False
    request_needed: bool = False
    admin_id: int | None = None
    date: str | None = None
    date_unix: int | None = None
    start_date: str | None = None
    expire_date: str | None = None
    usage_limit: int | None = None
    usage: int | None = None
    requested: int | None = None
    subscription_pricing: dict[str, int] | None = None
    subscription_expired: int | None = None
    replaced_link: str | None = None


class InviteInfo(Invite):
    """`chat invite get`: our own link, or a preview of somebody else's."""

    chat: Peer | None = None
    chat_title: str = ""
    members_count: int | None = None
    already_member: bool | None = None
    peek_expires: str | None = None
    about: str | None = None
    public: bool | None = None
    qr: str | None = None
    png: str | None = None


class InviteAdmin(Model):
    admin_id: int
    invites_count: int = 0
    revoked_invites_count: int = 0
    link: str = ""


class InviteDeleted(Model):
    chat_id: int
    deleted: int = 0


class InviteRevoked(Model):
    link: str
    revoked: bool = True
    replaced_link: str | None = None


class JoinRequest(Model):
    user_id: int
    username: str | None = None
    name: str = ""
    date: str | None = None
    date_unix: int | None = None
    about: str | None = None
    via_link: str | None = None
    approved_by: int | None = None


class RequestResult(Model):
    chat_id: int
    approved: list[int] = []
    denied: list[int] = []
    failed: list[MissingInvitee] = []
    all: bool = False


class InvitePeek(Model):
    chat: Peer | None = None
    chat_title: str = ""
    peek_expires: str | None = None
    messages: list[Message] = []


class JoinResult(Model):
    chat_id: int
    title: str = ""
    joined: bool = False
    pending_approval: bool = False
    already: bool = False
    #: A layer-229 join that needs a web view tlgr cannot open.
    needs_web_view: str | None = None


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


class Topic(Model):
    id: int
    chat_id: int = 0
    title: str = ""
    icon_emoji_id: int | None = None
    icon_color: int | None = None
    closed: bool = False
    pinned: bool = False
    hidden: bool = False
    my: bool = False
    top_message: int | None = None
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    from_id: int | None = None
    muted: bool | None = None
    link: str | None = None
    date: str | None = None
    date_unix: int | None = None
    deleted: bool = False


class TopicResult(Model):
    chat_id: int
    topic_id: int
    title: str | None = None
    icon_emoji_id: int | None = None
    closed: bool | None = None
    hidden: bool | None = None
    deleted: bool | None = None
    mute_until: str | None = None
    silent: bool | None = None
    previews: bool | None = None
    changed: list[str] = []
    already: bool = False


class TopicPinResult(Model):
    chat_id: int
    pinned: list[int] = []
    unpinned: list[int] = []
    already: bool = False


class TopicReadResult(Model):
    chat_id: int
    topic_id: int
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    max_id: int | None = None
    items: list[Message] = []


# ---------------------------------------------------------------------------
# The admin log
# ---------------------------------------------------------------------------


class AdminLogEvent(Model):
    """One `channelAdminLogEvent`, normalised but never lossy.

    Telegram has 50-odd `channelAdminLogEventAction*` constructors. They are
    reduced to `{action, prev, new}` so a script can switch on one string,
    and `raw_type` keeps the TL name so nothing that was in the reply is
    unavailable.
    """

    id: int
    date: str | None = None
    date_unix: int | None = None
    user_id: int = 0
    action: str = ""
    raw_type: str = ""
    prev: Any = None
    new: Any = None
    chat_id: int = 0


class AntiSpamReport(Model):
    chat_id: int
    msg_id: int
    reported: bool = True


# ---------------------------------------------------------------------------
# Creation, editing, settings
# ---------------------------------------------------------------------------


class CreatedChat(Model):
    id: int
    type: str = ""
    title: str = ""
    username: str | None = None
    invite_link: str | None = None
    added: list[int] = []
    missing: list[MissingInvitee] = []


class ChatEditResult(Model):
    id: int
    changed: list[str] = []
    already: bool = False
    palettes: list[dict[str, Any]] = []


class MigrateResult(Model):
    old_chat_id: int
    chat_id: int
    type: str = ""


class TransferResult(Model):
    chat_id: int
    new_owner_id: int


class SettingsView(Model):
    """Every administrable toggle, keyed the way `chat setting set` spells it."""

    chat_id: int = 0
    slow_mode: int | None = None
    prehistory: str | None = None
    join_to_send: bool | None = None
    join_request: bool | None = None
    guard_bot: int | None = None
    noforwards: bool | None = None
    antispam: bool | None = None
    hidden_members: bool | None = None
    signatures: bool | None = None
    signature_profiles: bool | None = None
    forum: bool | None = None
    forum_tabs: str | None = None
    view_as: str | None = None
    autotranslate: bool | None = None
    ads: bool | None = None
    reactions: str | None = None
    reactions_list: list[str] = []
    reactions_limit: int | None = None
    paid_reactions: bool | None = None
    sticker_set: str | None = None
    emoji_set: str | None = None
    paid_messages_stars: int | None = None
    direct_messages: bool | None = None
    gift_notifications: bool | None = None
    #: key → may I change it here
    available: dict[str, bool] = {}
    #: key → the capability flag or boost level that blocks it
    gated_by: dict[str, str] = {}


class SettingResult(Model):
    chat_id: int
    changed: list[str] = []
    already: list[str] = []
    failed: dict[str, str] = {}


class UsernameCheck(Model):
    username: str
    #: available | occupied | invalid | purchasable
    status: str = ""
    available: bool = False
    collectible: dict[str, Any] | None = None


class UsernameResult(Model):
    chat_id: int
    username: str | None = None
    link: str | None = None
    usernames: list[str] = []
    invite_link: str | None = None
    already: bool = False


class ChatPhotoResult(Model):
    chat_id: int
    photo_id: int | None = None
    ok: bool = True


class SendAsPeer(Model):
    id: int
    type: str = ""
    title: str = ""
    premium_required: bool = False
    default: bool = False


class SendAsResult(Model):
    chat_id: int
    send_as: int


class DiscussionCandidate(Model):
    id: int
    title: str = ""
    type: str = ""
    needs_migration: bool = False
    prehistory_hidden: bool | None = None


class DiscussionResult(Model):
    channel_id: int
    linked_chat_id: int | None = None
    already: bool = False


class SimilarChat(Model):
    id: int
    title: str = ""
    username: str | None = None
    participants_count: int | None = None


class SponsoredReport(Model):
    #: reported | choose-option | premium-required
    result: str = ""
    title: str = ""
    options: list[dict[str, str]] = []


class SuggestionResult(Model):
    chat_id: int
    key: str = ""
    pending_suggestions: list[str] = []
    already: bool = False


class SuggestedPostResult(Model):
    channel_id: int
    msg_id: int
    approved: bool | None = None
    rejected: bool | None = None
    schedule_date: str | None = None


class VerificationResult(Model):
    chat_id: int
    bot_id: int | None = None
    enabled: bool = False


class AffiliateBot(Model):
    bot_id: int
    url: str = ""
    commission_permille: int = 0
    duration_months: int | None = None
    participants: int | None = None
    revenue: int | None = None
    date: str | None = None
    date_unix: int | None = None


class AffiliateResult(Model):
    chat_id: int
    bot_id: int | None = None
    url: str = ""
    commission_permille: int = 0
    revoked: bool = False


class DirectDialog(Model):
    saved_peer_id: int
    name: str = ""
    top_message: int | None = None
    unread_count: int = 0
    unread_reactions_count: int = 0
    date: str | None = None
    date_unix: int | None = None
    nopaid_messages_exception: bool | None = None


class DirectBanResult(Model):
    channel_id: int
    monoforum_id: int | None = None
    user_id: int = 0
    banned: bool = False


class CommunityRow(Model):
    """A layer-229 community. Reserved until Telethon speaks the layer."""

    id: int
    title: str = ""
    type: str = ""
    visible: bool | None = None
    collapsed: bool | None = None
    requested_by: int | None = None
    date: str | None = None
    date_unix: int | None = None
    chats: list[int] = []


class CommunityResult(Model):
    community_id: int = 0
    chat_id: int | None = None
    user_id: int | None = None
    title: str = ""
    state: str = ""
    banned: bool | None = None
    linked_peers: list[int] = []


class WelcomeMessage(Model):
    id: int
    text: str = ""
    entities: list[dict[str, Any]] = []
    media: str | None = None
    date: str | None = None
    date_unix: int | None = None


class WelcomeResult(Model):
    chat_id: int
    id: int | None = None
    text: str = ""
    deleted: int = 0


# ---------------------------------------------------------------------------
# Statistics, revenue and boosts
# ---------------------------------------------------------------------------


class StatValue(Model):
    current: float = 0.0
    previous: float = 0.0
    growth: float = 0.0


class Graph(Model):
    """One chart. Either resolved JSON, an async token, or an error."""

    name: str
    token: str | None = None
    zoom_token: str | None = None
    json: Any = None
    error: str | None = None
    path: str | None = None


class ChatStats(Model):
    chat_id: int = 0
    #: broadcast | megagroup | message | story | poll
    type: str = ""
    period: dict[str, str] = {}
    followers: StatValue | None = None
    views_per_post: StatValue | None = None
    shares_per_post: StatValue | None = None
    reactions_per_post: StatValue | None = None
    enabled_notifications: StatValue | None = None
    members: StatValue | None = None
    messages: StatValue | None = None
    viewers: StatValue | None = None
    posters: StatValue | None = None
    views: int | None = None
    forwards: int | None = None
    reactions: int | None = None
    graphs: list[Graph] = []
    recent_posts: list[dict[str, Any]] = []


class PublicForward(Model):
    chat_id: int = 0
    chat_title: str = ""
    msg_id: int | None = None
    story_id: int | None = None
    views: int | None = None
    date: str | None = None
    date_unix: int | None = None


class RevenueSummary(Model):
    chat_id: int
    currency: str = "stars"
    current_balance: int = 0
    available_balance: int = 0
    overall_revenue: int = 0
    withdrawal_enabled: bool = False
    next_withdrawal_at: str | None = None
    usd_rate: float | None = None
    graphs: list[Graph] = []
    from_user_revenue: int | None = None


class RevenueTransaction(Model):
    id: str
    date: str | None = None
    date_unix: int | None = None
    amount: int = 0
    currency: str = "stars"
    peer: str = ""
    title: str = ""
    refund: bool = False
    pending: bool = False
    failed: bool = False
    subscription_period: int | None = None


class Boost(Model):
    id: str = ""
    slot: int | None = None
    user_id: int | None = None
    chat_id: int | None = None
    gift: bool = False
    giveaway: bool = False
    unclaimed: bool = False
    multiplier: int | None = None
    stars: int | None = None
    date: str | None = None
    date_unix: int | None = None
    expires: str | None = None
    cooldown_until_date: str | None = None


class BoostStatus(Model):
    chat_id: int = 0
    level: int = 0
    boosts: int = 0
    current_level_boosts: int = 0
    next_level_boosts: int | None = None
    premium_audience: dict[str, float] | None = None
    boost_url: str = ""
    my_boost: bool = False
    boosts_applied: int | None = None
    prepaid_giveaways: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []


class BoostApplied(Model):
    chat_id: int
    level: int = 0
    boosts: int = 0
    my_boost: bool = True
    already: bool = False
    peer: str = ""
    slots: list[int] = []
    cooldown_until_date: str | None = None
