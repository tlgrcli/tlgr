"""tlgr wire models. `from tlgr.models import Message, Page` works."""

from __future__ import annotations

from tlgr.models.base import UNSET, Model, Request, Unset, decode, encode, to_builtins
from tlgr.models.dialog import Dialog, Draft, Folder, NotifySettings
from tlgr.models.envelope import ErrEnvelope, Meta, OkEnvelope, OpRequest
from tlgr.models.error import ErrorBody
from tlgr.models.event import EventEnvelope
from tlgr.models.message import (
    Button,
    Forward,
    MediaKind,
    MediaSummary,
    Message,
    MessageEntity,
    ReactionSummary,
    ReplyHeader,
    ReplyMarkup,
    ServiceAction,
)
from tlgr.models.page import Page, PageInfo
from tlgr.models.peer import (
    Chat,
    Peer,
    PeerKind,
    PeerRef,
    PeerRefKind,
    Photo,
    Rights,
    User,
    UserRef,
    parse_message_link,
    parse_peer_ref,
    parse_user_ref,
)

__all__ = [
    "UNSET",
    "Button",
    "Chat",
    "Dialog",
    "Draft",
    "ErrEnvelope",
    "ErrorBody",
    "EventEnvelope",
    "Folder",
    "Forward",
    "MediaKind",
    "MediaSummary",
    "Message",
    "MessageEntity",
    "Meta",
    "Model",
    "NotifySettings",
    "OkEnvelope",
    "OpRequest",
    "Page",
    "PageInfo",
    "Peer",
    "PeerKind",
    "PeerRef",
    "PeerRefKind",
    "Photo",
    "ReactionSummary",
    "ReplyHeader",
    "ReplyMarkup",
    "Request",
    "Rights",
    "ServiceAction",
    "Unset",
    "User",
    "UserRef",
    "decode",
    "encode",
    "parse_message_link",
    "parse_peer_ref",
    "parse_user_ref",
    "to_builtins",
]
