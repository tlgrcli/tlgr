"""The `events` group and `watch`: discovering, replaying and following events.

Four of these five operations run without touching Telegram at all. That is
the point: an agent should be able to ask *what can arrive* (`events list`),
*what one looks like* (`events get`), and *what did arrive* (`events replay`)
before it commits to holding a stream open (`watch`).

`watch` replaces v1's poller. v1 asked the daemon for `chat list` every two
seconds, then `message list` per chat, and emitted only new messages — so an
edit, a deletion, a read receipt, a reaction and every service message were
invisible, and twenty chats cost thirty HTTP round trips a minute whether or
not anything happened. Here the daemon holds one `events.Raw` handler per
account and a watcher is a bounded queue on the bus: nothing is polled, and
everything in the taxonomy is selectable.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import sys
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from tlgr.core import eventtypes
from tlgr.core.errors import (
    EXIT_EMPTY,
    IndeterminateError,
    NotFoundError,
    NotSupportedError,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request, to_builtins
from tlgr.models.event import DecodedEvent, EventEnvelope, EventType, EventTypeDetail
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_ENVELOPE: dict[str, Any] = {
    "seq": 91824,
    "ts": "2026-09-03T09:14:07Z",
    "account": "work",
    "type": "message_new",
    "payload": {"id": 12345, "chat_id": 777123, "text": "on my way"},
    "chat_id": 777123,
    "sender_id": 4242,
}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _row(name: str, spec: eventtypes.EventTypeSpec, sources: tuple[str, ...]) -> EventType:
    return EventType(
        type=name,
        group=spec.group,
        summary=spec.summary,
        sources=list(sources),
        telethon=spec.telethon,
        box=spec.box,
        bot_only=spec.bot_only,
        since_layer=spec.since_layer,
        available=spec.since_layer == 0,
        derived=spec.derived,
    )


def _window(ctx: OpContext, op: str, default: int = 200) -> tuple[int, dict[str, Any]]:
    from tlgr.core.pagination import decode_cursor

    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=PageKind.LOCAL, account=ctx.account)
    return min(limit, 5000), state


async def _chat_ids(ctx: OpContext, refs: tuple[PeerRef, ...]) -> list[int]:
    """`--chat` → marked ids, resolving through the account when it is connected.

    A watcher does not need a Telegram client, so the resolver may be absent;
    a numeric id still works, and a username without a connected account is a
    usage error naming the fix rather than a filter that silently matches
    nothing.
    """
    out: list[int] = []
    for ref in refs:
        if ref.kind == "id":
            out.append(int(ref.value))
            continue
        if getattr(ctx, "resolver", None) is None:
            raise UsageError(
                f"{ref.raw!r} needs a connected account to resolve; "
                "pass the numeric chat id, or start the daemon first",
                field="chat",
            )
        out.append(_send.peer_id_of(await _send.resolve(ctx, ref)))
    return out


def _bus(ctx: OpContext) -> Any:
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise UsageError("this operation needs the daemon's event bus")
    return bus


def _accounts(ctx: OpContext) -> list[str]:
    """The accounts an `--account all` operation spans, or the one given."""
    if ctx.account and ctx.account != "all":
        return [ctx.account]
    daemon = getattr(ctx, "daemon", None)
    sessions = getattr(daemon, "sessions", None)
    return list(getattr(sessions, "aliases", []) or [])


# ---------------------------------------------------------------------------
# events list
# ---------------------------------------------------------------------------


class EventListReq(Request):
    group: Annotated[
        str | None,
        choice(*eventtypes.GROUPS, help="Only this family."),
    ] = None
    raw: Annotated[
        bool,
        opt("--raw", help="One row per raw TL update constructor instead of per type."),
    ] = False
    available: Annotated[
        bool,
        opt(
            "--available",
            help="Only types this build can actually receive (hides bot-only and layer-229).",
        ),
    ] = False
    search: Annotated[
        str | None,
        opt("--search", metavar="TEXT", help="Substring match on type, constructor or summary."),
    ] = None


async def event_list(ctx: OpContext, req: EventListReq) -> Page[EventType]:
    """The subscribable surface, machine-readable.

    An agent that has to learn the vocabulary from prose will get it wrong;
    this is the same table `docs/design/EVENTS.md` prints and `watch --events`
    accepts, so there is exactly one source of truth for it.
    """
    rows: list[EventType] = []
    for name, spec in sorted(eventtypes.TYPES.items()):
        if req.group and spec.group != req.group:
            continue
        if req.available and (spec.bot_only or spec.since_layer):
            continue
        sources = eventtypes.constructors_for(name)
        if req.raw:
            for source in sources:
                if req.available and source in eventtypes.NEWER_THAN_LAYER_227:
                    continue
                row = _row(name, spec, (source,))
                row.available = source not in eventtypes.NEWER_THAN_LAYER_227
                rows.append(row)
        else:
            rows.append(_row(name, spec, sources))

    if req.search:
        needle = req.search.lower()
        rows = [
            row
            for row in rows
            if needle in row.type
            or needle in row.summary.lower()
            or any(needle in source.lower() for source in row.sources)
        ]

    limit, state = _window(ctx, "events.list")
    offset = int(state.get("offset", 0))
    window = rows[offset : offset + limit]
    return build_page(
        window,
        op="events.list",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window)},
        account=ctx.account,
        has_more=offset + len(window) < len(rows),
        total=len(rows),
    )


SPEC_EVENT_LIST = OperationSpec(
    id="events.list",
    request=EventListReq,
    response=Page[EventType],
    impl=event_list,
    summary="List the event types tlgr can emit, with their source constructors",
    description=(
        "114 types covering every `Update*` constructor Telethon can parse, "
        "plus the ones Telegram has added since. `--raw` lists the "
        "constructors instead. These names are the only values `watch "
        "--events`, `job add --events` and `webhook set --events` accept."
    ),
    # No `schema events` alias: placing one would turn the top-level `schema`
    # command into a group and take v1's bare `tlgr schema` with it. The same
    # taxonomy is reachable as `tlgr schema events`, which is `agent.schema`'s
    # own positional (DECISIONS, 2026-09-03).
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    paginated=PageKind.LOCAL,
    columns=("type", "group", "box", "summary"),
    example={
        "items": [
            {
                "type": "message_new",
                "group": "message",
                "summary": "A message arrived in any chat the account can see",
                "sources": ["UpdateNewMessage"],
                "box": "pts",
            }
        ],
        "has_more": False,
        "total": 114,
    },
    example_args="events list --group message",
    covers=(
        "updates.event-ai-compose-tones",
        "updates.event-autosave-settings",
        "updates.event-bot-callback-query",
        "updates.event-bot-ephemeral-callback",
        "updates.event-bot-inline-query",
        "updates.event-bot-message-reactions",
        "updates.event-bot-stars-subscription",
        "updates.event-bot-webhook-json",
        "updates.event-channel-forwards",
        "updates.event-channel-views",
        "updates.event-chat-participants",
        "updates.event-config-changed",
        "updates.event-dc-options",
        "updates.event-dialog-filters",
        "updates.event-dialog-unread-mark",
        "updates.event-emoji-game-info",
        "updates.event-ephemeral-messages",
        "updates.event-folder-peers",
        "updates.event-group-call",
        "updates.event-join-chat-webview-decision",
        "updates.event-login-token",
        "updates.event-message-deleted",
        "updates.event-new-authorization",
        "updates.event-new-message",
        "updates.event-peer-blocked",
        "updates.event-peer-wallpaper",
        "updates.event-pinned-messages",
        "updates.event-pts-changed",
        "updates.event-read-contents",
        "updates.event-read-monoforum",
        "updates.event-saved-dialogs",
        "updates.event-scheduled-deleted",
        "updates.event-service-message",
        "updates.event-stars-balance",
        "updates.event-stories-stealth",
        "updates.event-story-reaction",
        "updates.event-typing",
        "updates.event-user-phone",
        "updates.event-view-forum-as-messages",
        "updates.event-webview-result-sent",
    ),
    covers_partial=(
        "updates.event-attach-menu-bots",
        "updates.event-bot-business",
        "updates.event-bot-commands",
        "updates.event-bot-guest-chat-query",
        "updates.event-bot-menu-button",
        "updates.event-bot-payments",
        "updates.event-bot-stopped",
        "updates.event-channel-available-messages",
        "updates.event-channel-participant",
        "updates.event-chat-boost",
        "updates.event-chat-refetch",
        "updates.event-contacts-reset",
        "updates.event-default-banned-rights",
        "updates.event-dialog-pinned",
        "updates.event-draft",
        "updates.event-encrypted-chats",
        "updates.event-extended-media",
        "updates.event-geo-live-viewed",
        "updates.event-history-ttl",
        "updates.event-join-requests",
        "updates.event-managed-bot",
        "updates.event-message-edited",
        "updates.event-message-id-map",
        "updates.event-new-bot-connection",
        "updates.event-new-channel-message",
        "updates.event-notify-settings",
        "updates.event-paid-reaction-privacy",
        "updates.event-peer-located",
        "updates.event-peer-settings",
        "updates.event-phone-call",
        "updates.event-pinned-forum-topics",
        "updates.event-poll",
        "updates.event-privacy",
        "updates.event-quick-replies",
        "updates.event-reactions",
        "updates.event-read-discussion",
        "updates.event-read-inbox",
        "updates.event-read-outbox",
        "updates.event-recent-reactions",
        "updates.event-report-message-delivery",
        "updates.event-saved-gifs",
        "updates.event-saved-ringtones",
        "updates.event-scheduled-new",
        "updates.event-sent-phone-code",
        "updates.event-service-notification",
        "updates.event-star-gift-auction",
        "updates.event-stars-revenue",
        "updates.event-stickers-changed",
        "updates.event-story-id",
        "updates.event-story-new",
        "updates.event-story-read",
        "updates.event-transcription",
        "updates.event-user-emoji-status",
        "updates.event-user-name",
        "updates.event-user-refetch",
        "updates.event-user-status",
        "updates.event-web-browser-settings",
        "updates.event-webpage",
        "updates.stream-event-types",
        "updates.stream-raw-passthrough",
    ),
    coverage_note=(
        "the catalogue half: the type exists and is selectable. Receiving one "
        "is `watch`, which owns those ids fully."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# events get
# ---------------------------------------------------------------------------


class EventGetReq(Request):
    type: Annotated[
        str,
        arg(0, metavar="TYPE", help="An event type name, or a `raw:Constructor`."),
    ]
    example: Annotated[
        bool, opt("--example/--no-example", help="Include a synthetic example envelope.")
    ] = True
    json_schema: Annotated[
        bool, opt("--json-schema", help="Emit the payload as a JSON Schema object.")
    ] = False


def _json_schema(payload: dict[str, str]) -> dict[str, Any]:
    """The payload table as draft 2020-12.

    Deliberately loose: most payloads are the update's own fields made
    JSON-safe, and a schema that claimed to be exhaustive about them would be
    a promise the taxonomy does not make.
    """
    properties: dict[str, Any] = {}
    for name, described in payload.items():
        if name in ("…", "_"):
            continue
        base = described.split("—")[0].strip()
        kind = {
            "int": "integer",
            "str": "string",
            "bool": "boolean",
            "object": "object",
            "true": "boolean",
            "false": "boolean",
        }.get(base.replace(" | null", "").strip(), "string")
        if base.startswith("list["):
            kind = "array"
        properties[name] = {"type": kind, "description": described}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


async def event_get(ctx: OpContext, req: EventGetReq) -> EventTypeDetail:
    """One event type in full: sources, box, payload, and an example."""
    name = req.type.strip().lower()
    if name.startswith("raw:"):
        mapped = eventtypes.type_for_constructor(req.type[4:])
        if mapped is None:
            raise NotFoundError(f"no event type is produced by {req.type[4:]!r}")
        name = mapped
    for legacy, expansion in eventtypes.ALIASES.items():
        if name == legacy and len(expansion) == 1:
            name = expansion[0]
    spec = eventtypes.TYPES.get(name)
    if spec is None:
        raise NotFoundError(f"unknown event type {req.type!r}; run `tlgr events list`")

    row = _row(name, spec, eventtypes.constructors_for(name))
    detail = EventTypeDetail(
        type=row.type,
        group=row.group,
        summary=row.summary,
        sources=row.sources,
        telethon=row.telethon,
        box=row.box,
        bot_only=row.bot_only,
        since_layer=row.since_layer,
        available=row.available,
        derived=row.derived,
        payload=dict(spec.payload),
        filters=["account", "chat", "sender", "type", "self_origin"],
    )
    if req.json_schema:
        detail.json_schema = _json_schema(spec.payload)
    if req.example:
        detail.example = EventEnvelope(
            seq=1,
            ts="2026-09-03T09:14:07Z",
            account=ctx.account or "work",
            type=name,
            payload={key: None for key in spec.payload if key not in ("…",)},
            chat_id=-1001234567890,
        )
    return detail


SPEC_EVENT_GET = OperationSpec(
    id="events.get",
    request=EventGetReq,
    response=EventTypeDetail,
    impl=event_get,
    summary="Show one event type: payload, source constructors, sequence box, example",
    description=(
        "`box` is the field to read first: it says which sequence orders the "
        "event, and therefore whether a gap in it is recoverable with `sync "
        "difference` or simply lost."
    ),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    example={
        "type": "message_new",
        "group": "message",
        "summary": "A message arrived in any chat the account can see",
        "sources": ["UpdateNewMessage"],
        "box": "pts",
        "payload": {"message": "Message"},
    },
    example_args="events get message_new",
    covers=(
        "updates.event-attach-menu-bots",
        "updates.event-bot-business",
        "updates.event-bot-commands",
        "updates.event-bot-guest-chat-query",
        "updates.event-bot-menu-button",
        "updates.event-bot-payments",
        "updates.event-bot-stopped",
        "updates.event-channel-available-messages",
        "updates.event-channel-participant",
        "updates.event-chat-boost",
        "updates.event-chat-refetch",
        "updates.event-contacts-reset",
        "updates.event-default-banned-rights",
        "updates.event-dialog-pinned",
        "updates.event-draft",
        "updates.event-encrypted-chats",
        "updates.event-extended-media",
        "updates.event-geo-live-viewed",
        "updates.event-history-ttl",
        "updates.event-join-requests",
        "updates.event-managed-bot",
        "updates.event-message-edited",
        "updates.event-new-bot-connection",
        "updates.event-notify-settings",
        "updates.event-peer-located",
        "updates.event-phone-call",
        "updates.event-poll",
        "updates.event-quick-replies",
        "updates.event-read-discussion",
        "updates.event-read-outbox",
        "updates.event-saved-gifs",
        "updates.event-scheduled-new",
        "updates.event-service-notification",
        "updates.event-stars-revenue",
        "updates.event-story-id",
        "updates.event-story-read",
        "updates.event-user-emoji-status",
        "updates.event-user-refetch",
        "updates.event-web-browser-settings",
    ),
    covers_partial=("updates.stream-event-types", "updates.sync-min-constructors"),
    coverage_note=(
        "documents the type and its payload; receiving one is `watch`, and "
        "min-constructor hydration happens on the bus."
    ),
    empty_exit=EXIT_EMPTY,
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# events decode
# ---------------------------------------------------------------------------

#: `loc_key` prefixes Telegram uses in a push payload → tlgr event types. The
#: two that matter are not messages: `DC_UPDATE` and `SESSION_REVOKE` are
#: security events, and the second one means this session has been killed.
_PUSH_KEYS: dict[str, str] = {
    "DC_UPDATE": "sync_dc_options",
    "SESSION_REVOKE": "account_session_revoked",
    "AUTH_REGION": "account_new_authorization",
    "AUTH_UNKNOWN": "account_new_authorization",
    "MESSAGE": "message_new",
    "CHAT_MESSAGE": "message_new",
    "CHANNEL_MESSAGE": "message_new",
    "ENCRYPTED_MESSAGE": "secret_message",
    "PHONE_CALL": "call_phone",
    "READ_HISTORY": "read_inbox",
    "MESSAGE_DELETED": "message_deleted",
    "REACT": "message_reactions",
    "GEO_LIVE_PENDING": "message_geo_live_viewed",
    "STORY": "story_new",
}


def _push_event_type(loc_key: str) -> str:
    key = (loc_key or "").upper()
    for prefix, mapped in _PUSH_KEYS.items():
        if key == prefix or key.startswith(f"{prefix}_"):
            return mapped
    return "account_service_notification"


def _read_input(source: str | None) -> str:
    if source in (None, "", "-"):
        if sys.stdin is None or sys.stdin.isatty():
            raise UsageError("no input was given and stdin is a terminal", field="input")
        return sys.stdin.read()
    try:
        with open(str(source), encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise UsageError(f"{source}: {exc.strerror or exc}", field="input") from exc


def _decrypt_push(blob: bytes, auth_key: bytes) -> dict[str, Any]:
    """MTProto 2.0 decryption of an encrypted push payload.

    Telegram encrypts a push notification with the *push* auth key, using the
    same key derivation as a message: `msg_key` first, then AES-256-IGE. The
    direction byte is not documented for push, so both are tried and the one
    whose recomputed `msg_key` matches is the right one — which also means a
    wrong key produces "could not be decrypted" rather than plausible rubbish.
    """
    import hashlib

    from telethon.crypto import AES

    if len(auth_key) != 256:
        raise UsageError(f"a push auth key is 256 bytes; this one is {len(auth_key)}", field="key")
    if len(blob) < 16 or (len(blob) - 16) % 16:
        raise UsageError("the push payload is not a multiple of the AES block size", field="input")
    msg_key, body = blob[:16], blob[16:]

    for offset in (0, 8):
        sha256_a = hashlib.sha256(msg_key + auth_key[offset : offset + 36]).digest()
        sha256_b = hashlib.sha256(auth_key[offset + 40 : offset + 76] + msg_key).digest()
        key = sha256_a[:8] + sha256_b[8:24] + sha256_a[24:32]
        iv = sha256_b[:8] + sha256_a[8:24] + sha256_b[24:32]
        plain = AES.decrypt_ige(body, key, iv)
        computed = hashlib.sha256(auth_key[88 + offset : 88 + offset + 32] + plain).digest()[8:24]
        if computed != msg_key:
            continue
        length = int.from_bytes(plain[:4], "little")
        payload = plain[4 : 4 + length]
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IndeterminateError(
                "the payload decrypted but is not the JSON push body; "
                "the key may be for a different session"
            ) from exc
        if not isinstance(decoded, dict):
            raise IndeterminateError("the decrypted push payload is not an object")
        return decoded

    raise IndeterminateError(
        "the push payload could not be decrypted with this key — it belongs to "
        "another session, or the payload is truncated"
    )


class EventDecodeReq(Request):
    input: Annotated[
        str | None,
        arg(
            0,
            metavar="INPUT",
            required=False,
            help="A file, or '-' for stdin: a JSON TL object, or a base64 push payload.",
        ),
    ] = None
    push: Annotated[
        bool, opt("--push", help="The input is a Telegram push-notification payload.")
    ] = False
    key: Annotated[
        str | None,
        opt(
            "--key",
            secret=True,
            envvar="TLGR_PUSH_KEY",
            help="The base64 push auth key used to decrypt the payload.",
        ),
    ] = None
    raw: Annotated[
        bool, opt("--raw", help="Print the decoded TL object instead of the tlgr envelope.")
    ] = False


async def event_decode(ctx: OpContext, req: EventDecodeReq) -> DecodedEvent:
    """Turn a raw update or a push payload into the envelope tlgr would emit.

    Offline and account-free on purpose. tlgr does not register for push —
    the daemon holds a socket, so it has no need of one — but a phone-relay
    setup does, and `DC_UPDATE`/`SESSION_REVOKE` are security events somebody
    has to be able to read.
    """
    text = _read_input(req.input)

    if req.push:
        try:
            blob = base64.b64decode(text.strip(), validate=False)
        except (binascii.Error, ValueError) as exc:
            raise UsageError("the push payload is not base64", field="input") from exc
        try:
            body = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The CLI has already read the secret out of the environment, a
            # file or stdin; it never travels as argv (STYLE §3).
            if not req.key:
                raise UsageError(
                    "this push payload is encrypted; supply the push auth key with "
                    "--key-env, --key-file or --key-stdin",
                    field="key",
                ) from None
            body = _decrypt_push(blob, base64.b64decode(req.key))
        if not isinstance(body, dict):
            raise UsageError("the push payload is not a JSON object", field="input")
        inner = body.get("data")
        data: dict[str, Any] = inner if isinstance(inner, dict) else body
        loc_key = str(data.get("loc_key", ""))
        chat = data.get("chat_id") or data.get("channel_id") or data.get("from_id")
        chat_id = (
            int(chat) if isinstance(chat, (int, str)) and str(chat).lstrip("-").isdigit() else None
        )
        return DecodedEvent(
            event=_push_event_type(loc_key),
            account=ctx.account,
            chat_id=chat_id,
            sender_id=None,
            data=dict(data),
            raw=dict(body) if req.raw else None,
            push=True,
        )

    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"the input is not JSON: {exc}", field="input") from exc
    if not isinstance(loaded, dict):
        raise UsageError("a TL update is a JSON object", field="input")

    constructor = str(loaded.get("_") or loaded.get("constructor") or "")
    if not constructor:
        raise UsageError(
            "the object has no `_` naming its TL constructor; "
            "`tlgr watch --with-raw` emits that form",
            field="input",
        )
    event_type = eventtypes.type_for_constructor(constructor)
    if event_type is None:
        reason = eventtypes.INTERNAL.get(constructor)
        if reason:
            raise NotSupportedError(f"{constructor} carries no event: {reason}")
        raise NotFoundError(
            f"{constructor} is not an update tlgr knows; run `tlgr events list --raw`"
        )
    chat = loaded.get("chat_id")
    return DecodedEvent(
        event=event_type,
        account=ctx.account,
        chat_id=int(chat) if isinstance(chat, int) else None,
        data={key: value for key, value in loaded.items() if key != "_"},
        raw=dict(loaded) if req.raw else None,
    )


SPEC_EVENT_DECODE = OperationSpec(
    id="events.decode",
    request=EventDecodeReq,
    response=DecodedEvent,
    impl=event_decode,
    summary="Decode a raw TL update or an encrypted push payload into an event",
    description=(
        "Offline; no account needed. tlgr does not register for push "
        "notifications — the daemon holds a socket — but a phone-relay setup "
        "does, and `DC_UPDATE` and `SESSION_REVOKE` are security events: the "
        "second one means this session has been terminated."
    ),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    example={
        "event": "message_new",
        "data": {"pts": 4213, "pts_count": 1},
    },
    example_args="events decode - --push",
    covers=("updates.push-payload-decrypt",),
    covers_partial=("updates.stream-raw-passthrough",),
    coverage_note="decodes one update offline; the live passthrough is `watch --raw`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# The shared selection used by watch and replay
# ---------------------------------------------------------------------------


class _Selection:
    """The filter a watcher or a replay applies, resolved once."""

    __slots__ = ("chats", "senders", "topic", "types")

    def __init__(
        self,
        types: frozenset[str],
        chats: list[int],
        senders: list[int],
        topic: int | None,
    ) -> None:
        self.types = types
        self.chats = set(chats)
        self.senders = set(senders)
        self.topic = topic

    def wants(self, event: EventEnvelope) -> bool:
        if self.types and event.type not in self.types:
            return False
        if self.chats and (event.chat_id is None or event.chat_id not in self.chats):
            return False
        if self.senders and (event.sender_id is None or event.sender_id not in self.senders):
            return False
        return not (self.topic is not None and event.payload.get("top_msg_id") != self.topic)


async def _selection(
    ctx: OpContext,
    *,
    events: str,
    exclude: str | None,
    chats: tuple[PeerRef, ...],
    senders: tuple[PeerRef, ...],
    topic: int | None,
) -> _Selection:
    wanted = eventtypes.resolve_selectors(events)
    if exclude:
        wanted = wanted - eventtypes.resolve_selectors(exclude, allow_all=False)
    if not wanted:
        raise UsageError(
            "--events and --exclude together select nothing; a watch that "
            "matches nothing is indistinguishable from a broken daemon",
            field="events",
        )
    return _Selection(
        wanted,
        await _chat_ids(ctx, chats),
        await _chat_ids(ctx, senders),
        topic,
    )


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


class WatchReq(Request):
    events: Annotated[
        str,
        opt(
            "--events",
            metavar="TYPES",
            help=(
                "Types, groups, `raw:Constructor` names or `all`, "
                "comma-separated. See `tlgr events list`."
            ),
        ),
    ] = "new_message"
    exclude: Annotated[
        str | None,
        opt("--exclude", metavar="TYPES", help="Subtract these after --events is applied."),
    ] = None
    chat: Annotated[
        list[PeerRef],
        opt("--chat", metavar="CHAT", kind="peer", help="Only events about this chat."),
    ] = []
    sender: Annotated[
        list[PeerRef],
        opt("--sender", metavar="USER", kind="user", help="Only events from this user."),
    ] = []
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", help="Only events inside this forum topic.")
    ] = None
    since: Annotated[
        int | None,
        opt("--since", metavar="SEQ", help="Replay from this seq (exclusive) before following."),
    ] = None
    follow: Annotated[
        bool, opt("--follow/--no-follow", help="Keep streaming after the replay is drained.")
    ] = True
    max_events: Annotated[
        int | None,
        opt("--max-events", metavar="N", help="Stop after this many events."),
    ] = None
    raw: Annotated[
        bool, opt("--raw", help="Emit only the raw TL update instead of the envelope.")
    ] = False
    with_raw: Annotated[
        bool, opt("--with-raw", help="Include the raw TL update beside the payload.")
    ] = False
    heartbeat: Annotated[
        int,
        opt("--heartbeat", metavar="SECONDS", ge=0, help="Idle keepalive; 0 disables."),
    ] = 15
    on_lag: Annotated[
        str,
        choice(
            "drop",
            "block",
            "fail",
            help=(
                "Falling behind: drop the oldest and report it, take a much "
                "larger queue, or stop with exit 13."
            ),
        ),
    ] = "drop"
    follow_for: Annotated[
        int,
        opt("--follow-for", metavar="SECONDS", ge=1, le=86400, help="Close the stream after this."),
    ] = 3600
    print_cursor: Annotated[
        bool, opt("--print-cursor", help="Emit a final frame carrying the resume seq.")
    ] = False


async def watch(ctx: OpContext, req: WatchReq) -> AsyncIterator[dict[str, Any]]:
    """Follow the bus, as NDJSON frames.

    Push, never polling: the daemon already holds the update socket, so a
    watcher is a bounded queue on the bus rather than v1's two-second
    `chat list` + `message list` loop, which cost thirty round trips a minute
    and could only ever report new messages.

    `--account all` multiplexes every connected account; each frame carries
    its own `account`, and `seq` is per account because update state is.
    """
    bus = _bus(ctx)
    accounts = _accounts(ctx)
    selection = await _selection(
        ctx,
        events=req.events,
        exclude=req.exclude,
        chats=tuple(req.chat),
        senders=tuple(req.sender),
        topic=req.topic,
    )
    watching = accounts or [ctx.account]

    yield {
        "type": "watching",
        "accounts": watching,
        "events": sorted(selection.types),
        "chats": sorted(selection.chats),
        "latest_seq": {alias: bus.latest_seq(alias) for alias in watching},
    }

    subscribers = [
        bus.subscribe(
            alias,
            types=selection.types,
            maxsize=8192 if req.on_lag == "block" else 2048,
            want_raw=req.raw or req.with_raw,
        )
        for alias in watching
    ]
    delivered = 0
    last_seq: dict[str, int] = {}
    reason = "closed"
    try:
        for alias in watching:
            if req.since is None:
                continue
            replayed, gap = bus.replay(alias, req.since)
            if gap is not None:
                yield {**gap, "account": alias}
            for event in replayed:
                if not selection.wants(event):
                    continue
                delivered += 1
                last_seq[alias] = event.seq
                yield _frame(event, req)
                if req.max_events and delivered >= req.max_events:
                    reason = "limit"
                    break
            if reason == "limit":
                break

        if req.follow and reason != "limit":
            deadline = time.monotonic() + req.follow_for
            heartbeat = float(req.heartbeat) if req.heartbeat else None
            while reason == "closed":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                wait = min(heartbeat, remaining) if heartbeat else remaining
                pending = [asyncio.ensure_future(sub.queue.get()) for sub in subscribers]
                done, _ = await asyncio.wait(
                    pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    if task not in done:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                if not done:
                    if heartbeat:
                        yield {"type": "heartbeat", "ts": _now()}
                    continue
                for task in done:
                    event = task.result()
                    lag = _take_lag(subscribers, event.account)
                    if lag:
                        if req.on_lag == "fail":
                            raise IndeterminateError(
                                f"this watcher fell behind and lost {lag} events; "
                                "resume from the last seq you saw with --since"
                            )
                        yield {"type": "lag", "dropped": lag, "account": event.account}
                    if not selection.wants(event):
                        continue
                    delivered += 1
                    last_seq[event.account] = event.seq
                    yield _frame(event, req)
                    if req.max_events and delivered >= req.max_events:
                        reason = "limit"
                        break
    finally:
        for subscriber in subscribers:
            bus.unsubscribe(subscriber)

    # Outside the `finally`, deliberately: yielding while an async generator
    # is being closed raises, and the resume cursor is worth having only when
    # the stream ended on its own terms.
    if req.print_cursor:
        yield {"type": "cursor", "latest_seq": last_seq, "reason": reason}


def _take_lag(subscribers: list[Any], account: str) -> int:
    for subscriber in subscribers:
        if subscriber.account == account:
            return int(subscriber.take_lag())
    return 0


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _frame(event: EventEnvelope, req: WatchReq) -> dict[str, Any]:
    if req.raw:
        return {"type": event.type, "seq": event.seq, "account": event.account, "raw": event.raw}
    frame = to_builtins(event)
    if not isinstance(frame, dict):  # pragma: no cover - EventEnvelope is a Struct
        return {"type": event.type}
    if not req.with_raw:
        frame.pop("raw", None)
    return frame


SPEC_WATCH = OperationSpec(
    id="events.watch",
    request=WatchReq,
    response=None,
    impl=watch,
    summary="Stream live events from the daemon as newline-delimited JSON",
    description=(
        "Push-driven from the daemon's event bus, not polled: v1 asked for "
        "`chat list` and then `message list` every two seconds and could only "
        "report new messages. Every type in `tlgr events list` is selectable, "
        "`--since <seq>` replays the ring buffer first (with a `gap` frame "
        "when it cannot reach that far back), and a watcher that falls behind "
        "gets a `lag` frame rather than silence."
    ),
    aliases=("events.tail",),
    legacy_paths=("watch",),
    stream=True,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=900,
    example={"type": "message_new", "seq": 91824},
    example_args="watch --events message_new,read_inbox --chat @alice",
    covers=(
        "bots.bot-side-update-stream",
        "bots.bot-subscription-update",
        "bots.ephemeral-message-view",
        "contacts-users.user-status-watch",
        "dialogs.typing-watch",
        "dialogs.watch-dialog-events",
        "giveaway.prize-stars",
        "location.proximity-alert-event",
        "location.viewed-receipt",
        "messages-core.message-watch-events",
        "updates.event-message-id-map",
        "updates.event-new-channel-message",
        "updates.event-paid-reaction-privacy",
        "updates.event-peer-settings",
        "updates.event-pinned-forum-topics",
        "updates.event-privacy",
        "updates.event-reactions",
        "updates.event-read-inbox",
        "updates.event-recent-reactions",
        "updates.event-saved-ringtones",
        "updates.event-sent-phone-code",
        "updates.event-star-gift-auction",
        "updates.event-stickers-changed",
        "updates.event-story-new",
        "updates.event-transcription",
        "updates.event-user-name",
        "updates.event-user-status",
        "updates.event-webpage",
        "updates.stream-event-types",
        "updates.stream-raw-passthrough",
        "updates.stream-watch-ndjson",
        "updates.sync-min-constructors",
    ),
    covers_partial=(
        "updates.event-ai-compose-tones",
        "updates.event-attach-menu-bots",
        "updates.event-autosave-settings",
        "updates.event-bot-business",
        "updates.event-bot-callback-query",
        "updates.event-bot-commands",
        "updates.event-bot-ephemeral-callback",
        "updates.event-bot-guest-chat-query",
        "updates.event-bot-inline-query",
        "updates.event-bot-menu-button",
        "updates.event-bot-message-reactions",
        "updates.event-bot-payments",
        "updates.event-bot-stars-subscription",
        "updates.event-bot-stopped",
        "updates.event-bot-webhook-json",
        "updates.event-channel-available-messages",
        "updates.event-channel-forwards",
        "updates.event-channel-participant",
        "updates.event-channel-views",
        "updates.event-chat-boost",
        "updates.event-chat-participants",
        "updates.event-chat-refetch",
        "updates.event-config-changed",
        "updates.event-contacts-reset",
        "updates.event-dc-options",
        "updates.event-default-banned-rights",
        "updates.event-dialog-filters",
        "updates.event-dialog-pinned",
        "updates.event-dialog-unread-mark",
        "updates.event-draft",
        "updates.event-emoji-game-info",
        "updates.event-encrypted-chats",
        "updates.event-ephemeral-messages",
        "updates.event-extended-media",
        "updates.event-folder-peers",
        "updates.event-geo-live-viewed",
        "updates.event-group-call",
        "updates.event-history-ttl",
        "updates.event-join-chat-webview-decision",
        "updates.event-join-requests",
        "updates.event-login-token",
        "updates.event-managed-bot",
        "updates.event-message-deleted",
        "updates.event-message-edited",
        "updates.event-new-authorization",
        "updates.event-new-bot-connection",
        "updates.event-new-message",
        "updates.event-notify-settings",
        "updates.event-peer-blocked",
        "updates.event-peer-located",
        "updates.event-peer-wallpaper",
        "updates.event-phone-call",
        "updates.event-pinned-messages",
        "updates.event-poll",
        "updates.event-pts-changed",
        "updates.event-quick-replies",
        "updates.event-read-contents",
        "updates.event-read-discussion",
        "updates.event-read-monoforum",
        "updates.event-read-outbox",
        "updates.event-report-message-delivery",
        "updates.event-saved-dialogs",
        "updates.event-saved-gifs",
        "updates.event-scheduled-deleted",
        "updates.event-scheduled-new",
        "updates.event-service-message",
        "updates.event-service-notification",
        "updates.event-stars-balance",
        "updates.event-stars-revenue",
        "updates.event-stories-stealth",
        "updates.event-story-id",
        "updates.event-story-reaction",
        "updates.event-story-read",
        "updates.event-typing",
        "updates.event-user-emoji-status",
        "updates.event-user-phone",
        "updates.event-user-refetch",
        "updates.event-view-forum-as-messages",
        "updates.event-web-browser-settings",
        "updates.event-webview-result-sent",
        "updates.stream-daemon-multi-account",
        "updates.stream-event-filtering",
        "updates.stream-resume-cursor",
        "updates.sync-channel-short-poll",
        "updates.sync-difference-too-long",
        "updates.sync-dispatch-ordering",
        "updates.sync-duplicate-suppression",
        "updates.sync-peer-cache-from-updates",
        "updates.sync-too-long",
        "updates.sync-updating-indicator",
    ),
    coverage_note=(
        "delivers every type; the catalogue half (what exists, what it means) "
        "is `events list`/`events get`, and gap recovery is the `sync` group."
    ),
    tags=frozenset({"agent-safe", "frames", "live-stream"}),
)


# ---------------------------------------------------------------------------
# events replay
# ---------------------------------------------------------------------------


class EventReplayReq(Request):
    since: Annotated[
        int | None,
        opt("--since", metavar="SEQ", help="First seq (exclusive). Default: the whole buffer."),
    ] = None
    until: Annotated[
        int | None, opt("--until", metavar="SEQ", help="Stop at this seq (inclusive).")
    ] = None
    events: Annotated[str, opt("--events", metavar="TYPES", help="Filter the replay.")] = "all"
    exclude: Annotated[
        str | None, opt("--exclude", metavar="TYPES", help="Subtract these types.")
    ] = None
    chat: Annotated[
        list[PeerRef], opt("--chat", metavar="CHAT", kind="peer", help="Only this chat.")
    ] = []
    webhook: Annotated[
        bool,
        opt("--webhook", help="Re-deliver the range to the configured webhook, not to stdout."),
    ] = False
    difference: Annotated[
        bool,
        opt(
            "--difference",
            help="Rebuild a range older than the buffer with updates.getDifference.",
        ),
    ] = False


async def event_replay(ctx: OpContext, req: EventReplayReq) -> AsyncIterator[Page[EventEnvelope]]:
    """Read the ring buffer without following it.

    The honest failure is the point. Asking for events after 91,820 when the
    buffer starts at 95,000 does not return the newest page as though it were
    the next one; it is INDETERMINATE with the oldest seq it does hold, so a
    consumer knows it has a hole rather than believing it caught up.
    """
    bus = _bus(ctx)
    selection = await _selection(
        ctx,
        events=req.events,
        exclude=req.exclude,
        chats=tuple(req.chat),
        senders=(),
        topic=None,
    )
    limit, state = _window(ctx, "events.replay", default=1000)
    offset = int(state.get("offset", 0))

    collected: list[EventEnvelope] = []
    for alias in _accounts(ctx) or [ctx.account]:
        events, gap = bus.replay(alias, req.since if req.since is not None else 0)
        if gap is not None and not req.difference:
            raise IndeterminateError(
                f"seq {req.since} is older than the buffer, which starts at "
                f"{gap['from']}; {gap['lost']} events are not recoverable from "
                "memory. Re-run with --difference to rebuild from Telegram, or "
                "start from that seq."
            )
        if gap is not None:
            ctx.warn(
                f"{gap['lost']} events before seq {gap['from']} were rebuilt from "
                "updates.getDifference and may be incomplete"
            )
            await _difference_backfill(ctx, alias)
        collected.extend(
            event
            for event in events
            if selection.wants(event) and (req.until is None or event.seq <= req.until)
        )

    collected.sort(key=lambda event: (event.account, event.seq))
    if req.webhook:
        pushed = _push_to_webhook(ctx, collected)
        ctx.warn(f"{pushed} events were re-queued for the webhook instead of printed")
        collected = []

    window = collected[offset : offset + limit]
    yield build_page(
        window,
        op="events.replay",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window)},
        account=ctx.account,
        has_more=offset + len(window) < len(collected),
        total=len(collected),
    )


async def _difference_backfill(ctx: OpContext, alias: str) -> None:
    """Ask the session to catch up so the gap is at least *narrowed*."""
    daemon = getattr(ctx, "daemon", None)
    sessions = getattr(daemon, "sessions", None)
    session = sessions.get(alias) if sessions is not None else None
    if session is not None:
        await session.catch_up()


def _push_to_webhook(ctx: OpContext, events: list[EventEnvelope]) -> int:
    daemon = getattr(ctx, "daemon", None)
    webhook = getattr(daemon, "webhook", None)
    if webhook is None:
        raise UsageError("no webhook is configured; run `tlgr webhook set --url …`")
    for event in events:
        webhook.enqueue(event)
    return len(events)


SPEC_EVENT_REPLAY = OperationSpec(
    id="events.replay",
    request=EventReplayReq,
    response=Page[EventEnvelope],
    impl=event_replay,
    summary="Replay buffered events from the daemon's ring buffer without following",
    description=(
        "Exit 3 when the range is inside the buffer and empty; exit 13, with "
        "the oldest seq the daemon still holds, when `--since` predates it. "
        "Returning the newest page instead would be a silent lie about having "
        "caught up."
    ),
    stream=True,
    paginated=PageKind.LOCAL,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=120,
    columns=("seq", "ts", "type", "chat_id"),
    empty_exit=EXIT_EMPTY,
    example={
        "items": [_EXAMPLE_ENVELOPE],
        "has_more": False,
        "total": 1,
    },
    example_args="events replay --since 91820 --events message_new",
    covers=("updates.stream-resume-cursor",),
    covers_partial=("updates.stream-watch-ndjson", "updates.sync-duplicate-suppression"),
    coverage_note=(
        "replays a range; following it live is `watch`, and de-duplication is "
        "the consumer's job through the envelope's stable seq."
    ),
    tags=frozenset({"agent-safe"}),
)
