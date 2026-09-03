"""Shared plumbing for the three call groups: `call`, `vc` and `conference`.

Three things live here because all three groups need them and none of them
owns them.

* **How a call is addressed.** A 1:1 call is `(id, access_hash)`; a group call
  is that, or a conference `slug`, or the invitation service message, or just
  "the video chat in this chat". One resolver understands all of it, so `vc
  get @team`, `vc get 123:456` and `vc get t.me/call/AbCd` are the same
  command rather than three.
* **What tlgr remembers.** Telegram hands out a call's `access_hash` exactly
  once, in the update that created the call. A CLI whose process ends between
  "ring" and "hang up" would otherwise be unable to hang up, so the daemon
  keeps the live calls it has seen and `call end 12345` works with the bare
  id. The store is per account and in memory: it describes a connection, not
  a fact worth persisting.
* **The honest boundary.** tlgr speaks the signalling half of MTProto and has
  no media engine. `MEDIA_NONE` is stamped on every answer that would
  otherwise be mistaken for "you are now in a call".
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.models.call import CallRef
from tlgr.models.peer import parse_peer_ref
from tlgr.ops._spec import OpContext

__all__ = [
    "CallHandle",
    "LiveCall",
    "app_config",
    "call_ref_of",
    "discard_reason",
    "forget_call",
    "input_phone_call",
    "live_calls",
    "protocol",
    "remember_call",
    "resolve_call",
    "state_name",
]

#: The frozen legacy constant set every official client still sends. These are
#: not tlgr's capabilities — tlgr has no media engine at all — they are what
#: the peer's client needs to see to consider the call negotiable.
PROTOCOL_MIN_LAYER = 65
PROTOCOL_MAX_LAYER = 92
LIBRARY_VERSIONS = ("2.4.4", "2.7.7", "5.0.0")

#: `phoneCall*` constructor → the word tlgr reports. The constructor name is
#: the only place the ringing state machine is written down.
CALL_STATES = {
    "PhoneCallEmpty": "empty",
    "PhoneCallWaiting": "waiting",
    "PhoneCallRequested": "requested",
    "PhoneCallAccepted": "accepted",
    "PhoneCall": "active",
    "PhoneCallDiscarded": "discarded",
}

DISCARD_REASONS = ("missed", "busy", "hangup", "disconnect")

_REASON_CLASSES = {
    "missed": "PhoneCallDiscardReasonMissed",
    "busy": "PhoneCallDiscardReasonBusy",
    "hangup": "PhoneCallDiscardReasonHangup",
    "disconnect": "PhoneCallDiscardReasonDisconnect",
}

#: The app-config keys the call surface reads. Kept as one list so `call
#: config get` and `vc get --limits` cannot disagree about what a limit is
#: called.
APP_CONFIG_KEYS = (
    "call_requests_disabled",
    "conference_call_size_limit",
    "group_call_message_length_limit",
    "group_call_message_ttl",
    "groupcall_video_participants_max",
    "stars_groupcall_message_amount_max",
    "stars_groupcall_message_limits",
)


# ---------------------------------------------------------------------------
# The live-call store
# ---------------------------------------------------------------------------


@dataclass
class LiveCall:
    """A 1:1 call this daemon has seen, and the secrets it holds for it.

    `a`, `g_a` and `p` exist only when *this* session ran the key exchange;
    they never leave the process and are what makes an emoji fingerprint
    derivable at all. Nothing here is written to disk.
    """

    id: int
    access_hash: int
    state: str = "waiting"
    video: bool = False
    out: bool = False
    peer_id: int | None = None
    admin_id: int | None = None
    participant_id: int | None = None
    conference_supported: bool | None = None
    connections: int = 0
    need_rating: bool | None = None
    need_debug: bool | None = None
    reason: str | None = None
    duration: int | None = None
    a: int | None = None
    p: int | None = None
    g: int | None = None
    g_a: bytes | None = None
    key: bytes | None = None
    discarded: bool = False


@dataclass
class _Store:
    calls: dict[int, LiveCall] = field(default_factory=dict)


_STORES: dict[str, _Store] = {}


def live_calls(account: str) -> dict[int, LiveCall]:
    """Every call this daemon currently knows about for *account*."""
    return _STORES.setdefault(account, _Store()).calls


def remember_call(account: str, call: LiveCall) -> LiveCall:
    """Record (or merge into) what we know about a call."""
    known = live_calls(account).get(call.id)
    if known is None:
        live_calls(account)[call.id] = call
        return call
    for name, value in vars(call).items():
        if value not in (None, 0, False, "") or getattr(known, name, None) is None:
            setattr(known, name, value)
    return known


def forget_call(account: str, call_id: int) -> None:
    live_calls(account).pop(call_id, None)


def reset_calls() -> None:
    """Drop every remembered call. For tests, and for a daemon restart."""
    _STORES.clear()


# ---------------------------------------------------------------------------
# 1:1 call references
# ---------------------------------------------------------------------------


def parse_call_id(text: str) -> tuple[int, int | None]:
    """`"12345"` or `"12345:678"` → `(id, access_hash | None)`."""
    raw = str(text or "").strip()
    if not raw:
        raise UsageError("a call id is required", field="call")
    head, _, tail = raw.partition(":")
    try:
        call_id = int(head)
    except ValueError as exc:
        raise UsageError(f"{raw!r} is not a call id", field="call") from exc
    if not tail:
        return call_id, None
    try:
        return call_id, int(tail)
    except ValueError as exc:
        raise UsageError(f"{raw!r} is not an id:access_hash pair", field="call") from exc


def input_phone_call(ctx: OpContext, text: str) -> tuple[Any, LiveCall | None]:
    """The `InputPhoneCall` for a call reference, plus what we remember of it.

    An id on its own is resolved through the store, which is why `call end
    12345` works: the access hash arrived in an update, not from the user.
    """
    from telethon.tl import types

    call_id, access_hash = parse_call_id(text)
    known = live_calls(ctx.account).get(call_id)
    if access_hash is None:
        if known is None:
            raise NotFoundError(
                f"call {call_id} is not one this daemon has seen; "
                "give it as id:access_hash, or run `tlgr call watch` to pick it up"
            )
        access_hash = known.access_hash
    elif known is None:
        # A caller who typed the access hash knows something the daemon does
        # not; remembering it is what makes the *next* command work with the
        # bare id, which is the whole point of the store.
        known = remember_call(
            ctx.account, LiveCall(id=call_id, access_hash=access_hash, state="unknown")
        )
    return types.InputPhoneCall(id=call_id, access_hash=access_hash), known


def protocol() -> Any:
    """The `phoneCallProtocol` every client sends, tlgr included."""
    from telethon.tl import types

    return types.PhoneCallProtocol(
        min_layer=PROTOCOL_MIN_LAYER,
        max_layer=PROTOCOL_MAX_LAYER,
        library_versions=list(LIBRARY_VERSIONS),
        udp_p2p=True,
        udp_reflector=True,
    )


def protocol_dict() -> dict[str, Any]:
    return {
        "min_layer": PROTOCOL_MIN_LAYER,
        "max_layer": PROTOCOL_MAX_LAYER,
        "udp_p2p": True,
        "udp_reflector": True,
        "library_versions": list(LIBRARY_VERSIONS),
    }


def discard_reason(name: str) -> Any:
    """`"hangup"` → `PhoneCallDiscardReasonHangup()`."""
    from telethon.tl import types

    class_name = _REASON_CLASSES.get(str(name or "").lower())
    if class_name is None:
        raise UsageError(
            f"--reason {name!r} is not a discard reason; expected one of "
            + ", ".join(DISCARD_REASONS),
            field="reason",
        )
    return getattr(types, class_name)()


def reason_name(reason: Any) -> str | None:
    """A `phoneCallDiscardReason*` back to its word."""
    if reason is None:
        return None
    name = type(reason).__name__
    for word, class_name in _REASON_CLASSES.items():
        if class_name == name:
            return word
    if name == "PhoneCallDiscardReasonMigrateConferenceCall":
        return "migrate-conference"
    return name


def state_name(call: Any) -> str:
    return CALL_STATES.get(type(call).__name__, "unknown")


# ---------------------------------------------------------------------------
# Group call references
# ---------------------------------------------------------------------------


@dataclass
class CallHandle:
    """A resolved group call: what to send, and what to echo back."""

    input: Any
    ref: CallRef
    chat: Any = None
    call: Any = None


def call_ref_of(call: Any, *, slug: str | None = None, msg_id: int | None = None) -> CallRef:
    """A `groupCall`/`inputGroupCall` as the reference every response carries."""
    return CallRef(
        id=int(getattr(call, "id", 0) or 0),
        access_hash=getattr(call, "access_hash", None),
        slug=slug,
        msg_id=msg_id,
    )


def _slug_from_link(text: str) -> str | None:
    """`t.me/call/AbCd` or `tg://call?slug=AbCd` → `AbCd`."""
    lowered = text.lower()
    if "t.me/call/" in lowered:
        return text.split("/call/", 1)[1].split("?", 1)[0].strip("/")
    if lowered.startswith("tg://call") and "slug=" in lowered:
        return text.split("slug=", 1)[1].split("&", 1)[0]
    if lowered.startswith("slug:"):
        return text.split(":", 1)[1]
    return None


async def _chat_call(ctx: OpContext, ref: str) -> CallHandle:
    """The active (or scheduled) group call of a chat."""
    from telethon.tl.functions import channels, messages

    from tlgr.ops import _send

    peer = await _send.resolve(ctx, parse_peer_ref(ref))
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")

    name = type(peer).__name__
    if name == "InputPeerChannel":
        from telethon import utils

        full = await client(channels.GetFullChannelRequest(utils.get_input_channel(peer)))
    elif name == "InputPeerChat":
        full = await client(messages.GetFullChatRequest(chat_id=peer.chat_id))
    else:
        raise UsageError(
            "a 1:1 chat has no video chat; give a group, a channel or a call link",
            field="call",
        )
    call = getattr(getattr(full, "full_chat", None), "call", None)
    if call is None:
        raise NotFoundError("that chat has no video chat running or scheduled")
    return CallHandle(input=call, ref=call_ref_of(call), chat=peer)


async def resolve_call(ctx: OpContext, ref: str) -> CallHandle:
    """Any way a human names a group call → the `InputGroupCall` to send.

    Accepted: `id:access_hash`, `msg:<id>` for an invitation service message,
    a `t.me/call/<slug>` or `tg://call?slug=` link, `slug:<slug>`, or a chat
    (its video chat). A bare word is tried as a chat first and as a slug
    second, because usernames and conference slugs share a shape and the chat
    is overwhelmingly the common case.
    """
    from telethon.tl import types

    raw = str(ref or "").strip()
    if not raw:
        raise UsageError("a call reference is required", field="call")

    if raw.lower().startswith("msg:"):
        try:
            msg_id = int(raw.split(":", 1)[1])
        except ValueError as exc:
            raise UsageError(f"{raw!r} is not msg:<id>", field="call") from exc
        return CallHandle(
            input=types.InputGroupCallInviteMessage(msg_id=msg_id),
            ref=CallRef(msg_id=msg_id),
        )

    slug = _slug_from_link(raw)
    if slug:
        return CallHandle(input=types.InputGroupCallSlug(slug=slug), ref=CallRef(slug=slug))

    head, _, tail = raw.partition(":")
    if tail and head.lstrip("-").isdigit() and tail.lstrip("-").isdigit():
        call = types.InputGroupCall(id=int(head), access_hash=int(tail))
        return CallHandle(input=call, ref=call_ref_of(call))

    try:
        return await _chat_call(ctx, raw)
    except (NotFoundError, UsageError, ValueError):
        if raw.startswith("@") or raw.lstrip("-").isdigit() or "/" in raw:
            raise
    return CallHandle(input=types.InputGroupCallSlug(slug=raw), ref=CallRef(slug=raw))


async def concrete_call(ctx: OpContext, handle: CallHandle) -> CallHandle:
    """Turn a slug/invitation handle into the `(id, access_hash)` pair.

    Several `phone.*` methods only accept the concrete constructor, and a slug
    is one `getGroupCall` away from it — asking here is cheaper than every
    caller getting the distinction right.
    """
    from telethon.tl import types
    from telethon.tl.functions import phone

    if isinstance(handle.input, types.InputGroupCall):
        return handle
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover
        raise UsageError("this operation needs a connected account")
    result = await client(phone.GetGroupCallRequest(call=handle.input, limit=0))
    call = getattr(result, "call", None)
    if call is None or getattr(call, "access_hash", None) is None:
        raise NotFoundError("that call link does not resolve to a call any more")
    return CallHandle(
        input=types.InputGroupCall(id=call.id, access_hash=call.access_hash),
        ref=call_ref_of(call, slug=handle.ref.slug, msg_id=handle.ref.msg_id),
        chat=handle.chat,
        call=call,
    )


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------


def _json_value(node: Any) -> Any:
    """A `JSONValue` tree as plain Python."""
    name = type(node).__name__
    if name == "JsonNull":
        return None
    if name in ("JsonBool", "JsonNumber", "JsonString"):
        return node.value
    if name == "JsonArray":
        return [_json_value(item) for item in node.value]
    if name == "JsonObject":
        return {item.key: _json_value(item.value) for item in node.value}
    return node


async def app_config(ctx: OpContext) -> dict[str, Any]:
    """The call-related keys of `help.getAppConfig`, as plain values.

    Failures are swallowed into an empty dict on purpose: a missing limit must
    degrade the answer, never fail the command that only wanted to mention it.
    """
    from telethon.tl.functions import help as help_fn

    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover
        return {}
    try:
        result = await client(help_fn.GetAppConfigRequest(hash=0))
    except Exception:  # pragma: no cover - a diagnostic must not fail the op
        return {}
    config = _json_value(getattr(result, "config", None))
    if not isinstance(config, dict):
        return {}
    return {key: config[key] for key in APP_CONFIG_KEYS if key in config}


# ---------------------------------------------------------------------------
# Diffie-Hellman
# ---------------------------------------------------------------------------


def _is_probable_prime(n: int, rounds: int = 16) -> bool:
    """Miller-Rabin. Enough rounds that a composite slipping through is not
    the reason a key exchange failed."""
    if n < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


@lru_cache(maxsize=8)
def validate_dh(p_bytes: bytes, g: int) -> dict[str, Any]:
    """Check the server's DH parameters instead of trusting them.

    A client that accepts whatever prime the server sends has not performed a
    key exchange; it has accepted a key the server chose. The checks are the
    documented ones: 2048 bits, `p` and `(p-1)/2` prime, and the residue
    condition for the generator.

    Cached: the parameters change roughly never, and two Miller-Rabin runs
    over a 2048-bit number cost about half a second — worth paying once per
    prime rather than once per call.
    """
    p = int.from_bytes(p_bytes, "big")
    checks: dict[str, Any] = {
        "bits": p.bit_length(),
        "size_ok": p.bit_length() == 2048,
        "generator": g,
    }
    residue = {
        2: p % 8 == 7,
        3: p % 3 == 2,
        4: True,
        5: p % 5 in (1, 4),
        6: p % 24 in (19, 23),
        7: p % 7 in (3, 5, 6),
    }
    checks["generator_ok"] = residue.get(g, False)
    checks["prime"] = _is_probable_prime(p)
    checks["safe_prime"] = checks["prime"] and _is_probable_prime((p - 1) // 2)
    checks["ok"] = bool(checks["size_ok"] and checks["generator_ok"] and checks["safe_prime"])
    return checks


def dh_verdict(p_bytes: bytes, g: int) -> dict[str, Any]:
    """`validate_dh` as a fresh dict, so a caller may annotate it safely."""
    return dict(validate_dh(p_bytes, g))


def key_fingerprint(key: bytes) -> int:
    """The 64-bit fingerprint both sides compare: the tail of SHA1(key)."""
    return int.from_bytes(hashlib.sha1(key).digest()[-8:], "little", signed=True)


def emoji_indices(key: bytes, g_a: bytes) -> list[int]:
    """The four indices into Telegram's 333-emoji verification table.

    tlgr reports the indices, not the emoji: the table is a fixed list every
    client ships, tlgr does not bundle it, and printing four *guessed* emoji
    for a security check would be worse than printing none.
    """
    digest = hashlib.sha256(key + g_a).digest()
    return [int.from_bytes(digest[8 + i * 8 : 16 + i * 8], "big") % 333 for i in range(4)]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str, *, field: str = "data") -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise UsageError(f"--{field} is not valid base64", field=field) from exc
