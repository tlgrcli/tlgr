"""Call shapes: 1:1 calls, video chats, livestreams and conferences.

One thing is true of every model in here and is stated on the wire rather than
in a footnote: **tlgr is a control plane**. It rings, answers, mutes, invites,
records and reads state; it never carries audio or video, because there is no
tgcalls binding behind it. Every shape that describes something a media engine
would otherwise be doing therefore carries a `media` field, and its value is
always `"none"` — a caller that asks tlgr to "join a call" learns from the
answer, not from the manual, that nobody can hear it.

The second recurring shape is `CallRef`. Telegram addresses a group call by
`(id, access_hash)`, a conference additionally by an invite `slug`, and an
invitation by the service message that carries it; a caller that has any one
of those must be able to feed it back to the next command, so every group-call
response echoes the whole reference rather than a bare id.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "MEDIA_NONE",
    "ActiveCall",
    "Call",
    "CallConfig",
    "CallDebugUpload",
    "CallDeclined",
    "CallEnded",
    "CallEvent",
    "CallIdentity",
    "CallLogDeleted",
    "CallLogEntry",
    "CallRating",
    "CallRef",
    "CallSignal",
    "CallUpgrade",
    "ChainBlock",
    "ConferenceCreated",
    "ConferenceDeclined",
    "ConferenceInfo",
    "ConferenceInvited",
    "ConferenceRelayed",
    "ConferenceRemoved",
    "ConferenceRevoked",
    "GroupCall",
    "GroupCallCreated",
    "GroupCallEnded",
    "GroupCallEvent",
    "GroupCallInvited",
    "GroupCallJoined",
    "GroupCallLeft",
    "GroupCallLink",
    "GroupCallParticipant",
    "GroupCallSettings",
    "GroupCallStarted",
    "InCallMessage",
    "InCallMessagesDeleted",
    "MuteState",
    "ParticipantRemoved",
    "RaisedHand",
    "RtmpInfo",
    "StreamChannel",
    "StreamDownload",
    "VideoState",
    "VolumeState",
]

MEDIA_NONE = "none"
"""The only value `media` ever takes. Kept as a constant so the promise is
made in one place and every op repeats it verbatim."""


class CallRef(Model):
    """How a group call, conference or live story is addressed and echoed.

    `slug` is the shareable half of a conference link and `msg_id` the
    invitation service message; both are alternative ways of naming the same
    call, and a response carries whichever the server told us about.
    """

    id: int = 0
    access_hash: int | None = None
    slug: str | None = None
    msg_id: int | None = None


# ---------------------------------------------------------------------------
# 1:1 calls
# ---------------------------------------------------------------------------


class Call(Model):
    """A 1:1 voice or video call, as far as signalling can see it.

    `state` is the `phoneCall*` constructor reduced to a word — `waiting`,
    `requested`, `accepted`, `active`, `discarded`, `empty` — because the
    constructor name is the only place the ringing state machine is written
    down, and a caller scripting "wait until it is answered" needs it.
    """

    call_id: int
    state: str
    media: str = MEDIA_NONE
    video: bool = False
    peer: Peer | None = None
    access_hash: int | None = None
    admin_id: int | None = None
    participant_id: int | None = None
    out: bool = False
    date: str | None = None
    date_unix: int | None = None
    conference_supported: bool | None = None
    #: The four-emoji key verification, when *this* session ran the DH
    #: exchange. Null otherwise: it cannot be derived from the server's view.
    fingerprint: list[str] | None = None
    connections: int = 0
    protocol: dict[str, Any] | None = None
    need_rating: bool | None = None
    need_debug: bool | None = None
    reason: str | None = None
    duration: int | None = None
    can_call: bool | None = None
    can_video_call: bool | None = None
    private: bool | None = None


class CallEnded(Model):
    call_id: int
    reason: str
    duration: int = 0
    need_rating: bool = False
    need_debug: bool = False


class CallDeclined(Model):
    call_id: int
    reason: str
    reply_message_id: int | None = None


class CallRating(Model):
    call_id: int
    rating: int
    comment: str = ""


class CallDebugUpload(Model):
    call_id: int
    uploaded: list[str] = []


class CallConfig(Model):
    """Everything a client needs before it can ring.

    `dh` is reported only with the validation verdict beside it: the prime
    comes from the server and a client that trusts it blind has no key
    exchange, it has a key the server chose.
    """

    tgcalls_config: dict[str, Any] | None = None
    timeouts: dict[str, int] = {}
    dh: dict[str, Any] | None = None
    call_requests_disabled: bool | None = None
    conference_call_size_limit: int | None = None
    limits: dict[str, int] = {}


class CallLogEntry(Model):
    """One row of the Calls tab, decoded from its service message."""

    msg_id: int
    chat_id: int
    date: str
    date_unix: int
    kind: str = "call"
    direction: str = "in"
    peer: Peer | None = None
    call_id: int | None = None
    video: bool = False
    reason: str | None = None
    duration: int = 0
    missed: bool = False
    active: bool | None = None
    other_participants: list[Peer] = []


class CallLogDeleted(Model):
    deleted: int = 0
    revoked: bool = False
    offset: int = 0


class CallSignal(Model):
    """One tgcalls signalling packet, in either direction, base64-encoded."""

    direction: str
    call_id: int
    data: str
    at: str


class CallEvent(Model):
    """A line of `call watch`.

    `should_ring` is the CLI's verdict, not a server field: a conference
    invitation rings only when the app-config allows call requests and the
    action is neither missed nor already active, and resolving that once here
    is what keeps every notifier script from getting it wrong.
    """

    kind: str
    at: str
    call_id: int | None = None
    peer: Peer | None = None
    video: bool = False
    state: str | None = None
    msg_id: int | None = None
    other_participants: list[Peer] = []
    should_ring: bool | None = None
    data: str | None = None


class CallUpgrade(Model):
    """`call invite`: the 1:1 call became a conference."""

    call_id: int
    conference: CallRef | None = None
    slug: str | None = None
    invite_link: str | None = None
    invited: list[Peer] = []
    migrated: bool = False


# ---------------------------------------------------------------------------
# Group calls: video chats, livestreams, live stories, conferences
# ---------------------------------------------------------------------------


class StreamChannel(Model):
    channel: int
    scale: int = 0
    last_timestamp_ms: int = 0


class GroupCall(Model):
    """A video chat, livestream, live story or conference.

    `kind` is derived, not sent: the server has one `groupCall` constructor
    for all four products and the difference is in the flags (`conference`,
    `rtmp_stream`) and in the peer it hangs off.
    """

    call: CallRef
    kind: str = "video-chat"
    media: str = MEDIA_NONE
    title: str | None = None
    participants_count: int = 0
    join_muted: bool = False
    can_change_join_muted: bool = False
    messages_enabled: bool = False
    can_change_messages_enabled: bool = False
    record_start_date: str | None = None
    record_video_active: bool = False
    rtmp_stream: bool = False
    #: In a big livestream the participant list carries publishers only. A
    #: caller must not read that as an empty audience — `participants_count`
    #: is the real number.
    listeners_hidden: bool = False
    conference: bool = False
    creator: bool = False
    schedule_date: str | None = None
    schedule_start_subscribed: bool = False
    stream_dc_id: int | None = None
    invite_link: str | None = None
    send_paid_messages_stars: int | None = None
    default_send_as: Peer | None = None
    unmuted_video_count: int = 0
    unmuted_video_limit: int = 0
    version: int = 0
    discarded: bool = False
    duration: int | None = None
    chat: Peer | None = None
    limits: dict[str, int] | None = None
    stream_channels: list[StreamChannel] | None = None
    live_edge_ms: int | None = None
    sources_joined: list[int] | None = None
    sources_missing: list[int] | None = None
    donors: dict[str, Any] | None = None


class GroupCallParticipant(Model):
    peer: Peer | None = None
    source: int = 0
    muted: bool = False
    can_self_unmute: bool = False
    muted_by_you: bool = False
    volume: int | None = None
    volume_by_admin: bool = False
    raise_hand: bool = False
    raise_hand_rating: int | None = None
    video: bool = False
    presentation: bool = False
    video_joined: bool = False
    is_self: bool = False
    left: bool = False
    about: str | None = None
    joined_at: str | None = None
    last_active: str | None = None
    paid_stars_total: int | None = None
    #: `joined`, `invited` or `calling` for a conference; null when only the
    #: E2E chain would know, which needs a block parser tlgr does not have.
    state: str | None = None


class GroupCallCreated(Model):
    call: CallRef
    chat_id: int
    kind: str = "video-chat"
    title: str | None = None
    schedule_date: str | None = None
    rtmp_stream: bool = False


class GroupCallStarted(Model):
    call: CallRef
    chat_id: int
    started: bool = True


class GroupCallEnded(Model):
    call: CallRef
    ended: bool = True
    duration: int | None = None


class GroupCallSettings(Model):
    """What `vc set` changed, read back rather than echoed."""

    call: CallRef
    title: str | None = None
    join_muted: bool | None = None
    messages_enabled: bool | None = None
    send_paid_messages_stars: int | None = None
    schedule_start_subscribed: bool | None = None
    record_start_date: str | None = None
    record_video_active: bool | None = None
    changed: list[str] = []


class GroupCallJoined(Model):
    call: CallRef
    media: str = MEDIA_NONE
    source: int = 0
    mode: str = "webrtc"
    join_as: Peer | None = None
    can_self_unmute: bool = False
    params: dict[str, Any] | None = None
    joined: bool = True
    experimental: bool = False


class GroupCallLeft(Model):
    call: CallRef
    source: int = 0
    left: bool = True


class GroupCallInvited(Model):
    invited: list[Peer] = []
    added: list[Peer] = []
    failed: list[dict[str, Any]] = []
    link_sent: list[Peer] = []
    link: str | None = None


class GroupCallLink(Model):
    kind: str = "listener"
    link: str | None = None
    invite_hash: str | None = None
    fallback: bool = False
    revoked: bool = False


class ActiveCall(Model):
    """One row of "chats with a call running right now"."""

    chat: Peer | None = None
    call: CallRef | None = None
    chat_id: int = 0
    title: str | None = None
    participants_count: int = 0
    active: bool = True
    not_empty: bool = False
    schedule_date: str | None = None
    rtmp_stream: bool = False


class CallIdentity(Model):
    """A peer you may appear as: "Display as" / "Comment as"."""

    peer: Peer | None = None
    kind: str = "join-as"
    default: bool = False
    is_self: bool = False


class RtmpInfo(Model):
    """RTMP ingest credentials. The key is a publishing credential.

    It is masked unless the caller asked for it in so many words, because the
    obvious thing to do with a CLI's output is paste it into a bug report.
    """

    url: str = ""
    key: str = ""
    key_shown: bool = False
    key_file: str | None = None
    peer: Peer | None = None
    live_story: bool = False
    revoked: bool = False


class MuteState(Model):
    call: CallRef
    peer: Peer | None = None
    muted: bool = False
    can_self_unmute: bool | None = None
    muted_by_you: bool = False
    media: str = MEDIA_NONE


class RaisedHand(Model):
    call: CallRef
    peer: Peer | None = None
    raise_hand: bool = False
    raise_hand_rating: int | None = None


class VolumeState(Model):
    call: CallRef
    peer: Peer | None = None
    volume: int = 100
    volume_by_admin: bool = False


class VideoState(Model):
    call: CallRef
    media: str = MEDIA_NONE
    video_stopped: bool | None = None
    video_paused: bool | None = None
    presentation: bool | None = None
    presentation_paused: bool | None = None


class ParticipantRemoved(Model):
    chat_id: int
    peer: Peer | None = None
    banned: bool = False
    removed: bool = True


class InCallMessage(Model):
    """A message on the in-call overlay. It has no history: read it live."""

    call: CallRef
    msg_id: int = 0
    from_id: Peer | None = None
    date: str | None = None
    date_unix: int | None = None
    text: str = ""
    paid_message_stars: int | None = None
    ttl: int | None = None


class InCallMessagesDeleted(Model):
    call: CallRef
    deleted: list[int] = []
    participant: Peer | None = None
    reported: bool = False


class GroupCallEvent(Model):
    """A line of `vc watch`."""

    kind: str
    at: str
    call: CallRef | None = None
    version: int | None = None
    participant: GroupCallParticipant | None = None
    participants: list[GroupCallParticipant] = []
    message: InCallMessage | None = None
    connection: dict[str, Any] | None = None
    blocks: list[str] = []
    encrypted: str | None = None
    resync: bool = False


class StreamDownload(Model):
    """What `vc download` wrote to disk."""

    call: CallRef
    out: str = "-"
    bytes: int = 0
    chunks: int = 0
    mode: str = "stream"
    media: str = MEDIA_NONE
    stream_dc_id: int | None = None
    first_time_ms: int | None = None
    last_time_ms: int | None = None


# ---------------------------------------------------------------------------
# Conferences
# ---------------------------------------------------------------------------


class ConferenceCreated(Model):
    call: CallRef
    slug: str | None = None
    invite_link: str | None = None
    creator: bool = True
    joined: bool = False
    media: str = MEDIA_NONE


class ConferenceInfo(Model):
    call: CallRef
    slug: str | None = None
    invite_link: str | None = None
    participants_count: int = 0
    creator: bool = False
    messages_enabled: bool = False
    conference: bool = True
    title: str | None = None
    limits: dict[str, int] | None = None
    qr: str | None = None


class ConferenceInvited(Model):
    invited: list[Peer] = []
    failed: list[dict[str, Any]] = []
    link_sent: list[Peer] = []
    link: str | None = None


class ConferenceDeclined(Model):
    msg_id: int
    declined: bool = True


class ConferenceRemoved(Model):
    call: CallRef
    removed: list[Peer] = []
    only_left: bool = False
    kicked: bool = False


class ConferenceRevoked(Model):
    call: CallRef
    revoked: bool = True


class ConferenceRelayed(Model):
    """`conference send`: tlgr carried opaque bytes it cannot read."""

    call: CallRef
    kind: str = "encrypted-message"
    sent: bool = True


class ChainBlock(Model):
    """One block of the conference's E2E chain, base64, unvalidated."""

    sub_chain_id: int = 0
    height: int = 0
    block: str = ""
    next_offset: int | None = None
