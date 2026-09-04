"""The plumbing the bot, inline, mini-app and payment groups all need.

Four things live here rather than in one of the four modules, because all four
reach for them and a second copy is how they start to disagree:

* **the keyboard schema**, read *and* write. One vocabulary of button `type`
  names serves `message get --json`, `bot press --button <text>` and
  `--keyboard FILE`, so a button that can be read back can be pressed and
  re-sent.
* **the bot-session gate.** Half of this surface is bot-only, and Telegram
  answers a user session with a bare `403 BOT_METHOD_INVALID`. Asking the
  session what it is first turns that into exit 4 with a sentence saying how
  to add a bot account.
* **DC routing.** An inline message id names the DC it lives on, and sending
  `editInlineBotMessage` to the home DC fails with an error that says nothing
  about DCs. One helper borrows the exported sender for every caller.
* **the report option tree.** `messages.report`, `messages.reportSponsoredMessage`
  and the mini-app report all walk the same
  chooseOption → addComment → reported state machine.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

from tlgr.core.errors import AuthenticationError, NotSupportedError, UsageError
from tlgr.models.bot import Keyboard, KeyboardButton, ReportOutcome
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._common import client
from tlgr.ops._spec import OpContext

__all__ = [
    "ADMIN_RIGHTS",
    "BUTTON_TYPES",
    "LAYER_229",
    "admin_rights",
    "bot_peer",
    "client",
    "command_scope",
    "data_json",
    "inline_message_id",
    "input_user",
    "keyboard_model",
    "keyboard_tl",
    "load_json",
    "on_dc",
    "option_bytes",
    "payload_bytes",
    "peer_ref",
    "report_outcome",
    "require_bot_session",
    "rights_keywords",
    "unsupported",
]

#: The one sentence every layer-gap refusal ends with. Written once so that
#: `tlgr agent capabilities` and the docs cannot describe the gap differently.
LAYER_229 = (
    "it is a layer-229 method and the pinned Telethon speaks layer 227; "
    "tlgr refuses rather than guessing at a constructor id"
)


def unsupported(feature: str, reason: str = LAYER_229) -> Any:
    """Refuse a feature this build genuinely cannot perform (exit 13)."""
    raise NotSupportedError(f"{feature} is not supported: {reason}")


# ---------------------------------------------------------------------------
# Peers and sessions
# ---------------------------------------------------------------------------


def peer_ref(value: str) -> PeerRef:
    """A `@username`/id string as a `PeerRef`, for a bot named in config."""
    from tlgr.models.peer import parse_peer_ref

    return parse_peer_ref(value)


async def bot_peer(ctx: OpContext, ref: PeerRef | str | None) -> Any:
    """The `InputPeer` of a bot, through the account's own resolver (§6.6)."""
    if isinstance(ref, str):
        ref = peer_ref(ref)
    return await _send.resolve(ctx, ref)


async def input_user(ctx: OpContext, ref: PeerRef | str | None, *, field: str = "bot") -> Any:
    """The `InputUser` a `bots.*` request wants.

    `utils.get_input_user` is arithmetic on a peer we already resolved; going
    back to the network would hide the real problem when the ref names a chat.
    """
    from telethon import utils

    peer = await bot_peer(ctx, ref)
    try:
        return utils.get_input_user(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{field} must name a user or a bot", field=field) from exc


async def require_bot_session(ctx: OpContext, what: str) -> Any:
    """Refuse a bot-only operation on a user session, with exit 4.

    Telegram answers `BOT_METHOD_INVALID`, which reads like a bug in the
    request rather than like "this account is a person".
    """
    account = await client(ctx).get_me()
    if not bool(getattr(account, "bot", False)):
        raise AuthenticationError(
            f"{what} needs a bot session; add one with "
            "`tlgr account add --bot-token <token>` and pass it with -a"
        )
    return account


# ---------------------------------------------------------------------------
# Admin rights
# ---------------------------------------------------------------------------

#: The keyword vocabulary `--admin`/`--group`/`--channel` accept, in the
#: spelling the groups-and-channels group uses.
ADMIN_RIGHTS: tuple[str, ...] = (
    "change_info",
    "post_messages",
    "edit_messages",
    "delete_messages",
    "ban_users",
    "invite_users",
    "pin_messages",
    "add_admins",
    "anonymous",
    "manage_call",
    "other",
    "manage_topics",
    "post_stories",
    "edit_stories",
    "delete_stories",
    "manage_direct_messages",
)

#: The names the t.me deep links use, mapped onto the TL field they set.
_RIGHT_ALIASES = {
    "manage_chat": "other",
    "restrict_members": "ban_users",
    "promote_members": "add_admins",
    "manage_video_chats": "manage_call",
}


def admin_rights(text: str | None, *, field: str = "admin") -> Any:
    """`change_info+invite_users` as a `ChatAdminRights`, or None."""
    if not text:
        return None
    from telethon.tl import types

    flags: dict[str, bool] = {}
    for raw in str(text).replace(",", "+").split("+"):
        name = raw.strip().lower()
        if not name:
            continue
        name = _RIGHT_ALIASES.get(name, name)
        if name not in ADMIN_RIGHTS:
            raise UsageError(
                f"--{field}: {raw!r} is not an admin right; choose from {', '.join(ADMIN_RIGHTS)}",
                field=field,
            )
        flags[name] = True
    return types.ChatAdminRights(**flags)


def rights_keywords(rights: Any) -> list[str]:
    """The keywords a `ChatAdminRights` has set, in the documented order."""
    if rights is None:
        return []
    return [name for name in ADMIN_RIGHTS if bool(getattr(rights, name, False))]


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def load_json(value: str | None, *, field: str) -> Any:
    """Inline JSON, `@path`, or a bare path — whichever the caller typed.

    A JSON document large enough to be worth writing is large enough to be
    worth keeping in a file, and a document small enough to type belongs on
    the command line; supporting only one of the two is what makes a caller
    write `--params "$(cat f.json)"`.
    """
    if value is None:
        return None
    text = value.strip()
    if text.startswith("@"):
        text = _read(text[1:], field=field)
    elif not text.startswith(("{", "[", '"')) and not text.lstrip("-").isdigit():
        text = _read(text, field=field)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--{field}: {exc}", field=field) from exc


def _read(path: str, *, field: str) -> str:
    handle = Path(os.path.expanduser(path))
    try:
        return handle.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"--{field}: {exc.strerror or exc}", field=field) from exc


def data_json(value: str | None, *, field: str) -> Any:
    """A `DataJSON` built from whatever `load_json` accepts."""
    from telethon.tl import types

    payload = load_json(value, field=field)
    if payload is None:
        return None
    return types.DataJSON(data=json.dumps(payload, separators=(",", ":")))


def payload_bytes(value: str | None, *, field: str) -> bytes | None:
    """Callback payload bytes from `hex:…`, `str:…`, `@file`, or bare hex/text.

    Callback data is *bytes*, and most of it is not text; a flag that only
    accepted text would make half the buttons on Telegram unpressable, and one
    that only accepted hex would make the other half unreadable.
    """
    if value is None:
        return None
    text = str(value)
    if text.startswith("hex:"):
        return _unhex(text[4:], field=field)
    if text.startswith("str:"):
        return text[4:].encode()
    if text.startswith("b64:"):
        return _unb64(text[4:], field=field)
    if text.startswith("@"):
        try:
            return Path(os.path.expanduser(text[1:])).read_bytes()
        except OSError as exc:
            raise UsageError(f"--{field}: {exc.strerror or exc}", field=field) from exc
    stripped = text.strip()
    if stripped and len(stripped) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        return _unhex(stripped, field=field)
    return text.encode()


def option_bytes(value: str | None, *, field: str = "option") -> bytes:
    """Report-option bytes, as `bot ad list`/a previous step printed them."""
    if not value:
        return b""
    return payload_bytes(value, field=field) or b""


def _unhex(text: str, *, field: str) -> bytes:
    try:
        return binascii.unhexlify(text.strip())
    except (binascii.Error, ValueError) as exc:
        raise UsageError(f"--{field}: {text!r} is not hexadecimal", field=field) from exc


def _unb64(text: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise UsageError(f"--{field}: {text!r} is not base64", field=field) from exc


def key_text(raw: bytes | None) -> str:
    """Opaque bytes as something a shell can round-trip: text, else base64."""
    if not raw:
        return ""
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode()


# ---------------------------------------------------------------------------
# Keyboards — the schema, both ways
# ---------------------------------------------------------------------------

#: TL class suffix → the `type` name the JSON schema uses.
BUTTON_TYPES: dict[str, str] = {
    "KeyboardButton": "text",
    "KeyboardButtonCallback": "callback",
    "KeyboardButtonUrl": "url",
    "KeyboardButtonUrlAuth": "url_auth",
    "InputKeyboardButtonUrlAuth": "url_auth",
    "KeyboardButtonSwitchInline": "switch_inline",
    "KeyboardButtonWebView": "webview",
    "KeyboardButtonSimpleWebView": "simple_webview",
    "KeyboardButtonGame": "game",
    "KeyboardButtonBuy": "buy",
    "KeyboardButtonRequestPhone": "request_phone",
    "KeyboardButtonRequestGeoLocation": "request_geo",
    "KeyboardButtonRequestPoll": "request_poll",
    "KeyboardButtonRequestPeer": "request_peer",
    "KeyboardButtonUserProfile": "user_profile",
    "InputKeyboardButtonUserProfile": "user_profile",
    "KeyboardButtonCopy": "copy",
}

_MARKUP_KINDS = {
    "ReplyInlineMarkup": "inline",
    "ReplyKeyboardMarkup": "keyboard",
    "ReplyKeyboardHide": "hide",
    "ReplyKeyboardForceReply": "force_reply",
}


def button_model(button: Any) -> KeyboardButton:
    """One TL button as the schema row `--keyboard` would write."""
    kind = BUTTON_TYPES.get(type(button).__name__, "unsupported")
    data = getattr(button, "data", None)
    return KeyboardButton(
        text=str(getattr(button, "text", "") or ""),
        type=kind,
        data=key_text(data) if data else None,
        url=getattr(button, "url", None),
        query=getattr(button, "query", None),
        user_id=_user_id(getattr(button, "user_id", None)),
        requires_password=bool(getattr(button, "requires_password", False)),
        same_peer=bool(getattr(button, "same_peer", False)),
        copy_text=getattr(button, "copy_text", None),
        button_id=getattr(button, "button_id", None),
        fwd_text=getattr(button, "fwd_text", None),
        request_write_access=bool(getattr(button, "request_write_access", False)),
    )


def _user_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(getattr(value, "user_id", 0) or 0) or None


def keyboard_model(markup: Any) -> Keyboard | None:
    """A TL reply markup as the `Keyboard` schema, or None."""
    if markup is None:
        return None
    kind = _MARKUP_KINDS.get(type(markup).__name__)
    if kind is None:
        return None
    rows = [
        [button_model(button) for button in (getattr(row, "buttons", None) or [])]
        for row in (getattr(markup, "rows", None) or [])
    ]
    return Keyboard(
        kind=kind,
        rows=rows,
        resize=bool(getattr(markup, "resize", False)),
        single_use=bool(getattr(markup, "single_use", False)),
        selective=bool(getattr(markup, "selective", False)),
        persistent=bool(getattr(markup, "persistent", False)),
        placeholder=getattr(markup, "placeholder", None),
    )


def keyboard_tl(spec: Any, *, field: str = "keyboard") -> Any:
    """The `Keyboard` schema as a TL reply markup.

    Only the button kinds a *client* can legitimately author are built. A
    `url_auth` button carries an `InputUser` the caller has not resolved and a
    `buy` button starts a payment, so both are refused here rather than half
    built.
    """
    from telethon.tl import types

    if spec is None:
        return None
    if isinstance(spec, str):
        spec = load_json(spec, field=field)
    if not isinstance(spec, dict):
        raise UsageError(f"--{field}: expected a keyboard object", field=field)

    kind = str(spec.get("kind") or "inline")
    rows: list[Any] = []
    for row in spec.get("rows") or []:
        buttons = [_button_tl(entry, field=field) for entry in row]
        rows.append(types.KeyboardButtonRow(buttons=buttons))
    if kind == "inline":
        return types.ReplyInlineMarkup(rows=rows)
    if kind == "keyboard":
        return types.ReplyKeyboardMarkup(
            rows=rows,
            resize=bool(spec.get("resize")) or None,
            single_use=bool(spec.get("single_use")) or None,
            selective=bool(spec.get("selective")) or None,
            persistent=bool(spec.get("persistent")) or None,
            placeholder=spec.get("placeholder"),
        )
    if kind == "hide":
        return types.ReplyKeyboardHide(selective=bool(spec.get("selective")) or None)
    if kind == "force_reply":
        return types.ReplyKeyboardForceReply(
            single_use=bool(spec.get("single_use")) or None,
            selective=bool(spec.get("selective")) or None,
            placeholder=spec.get("placeholder"),
        )
    raise UsageError(f"--{field}: kind must be inline, keyboard, hide or force_reply", field=field)


def _button_tl(entry: Any, *, field: str) -> Any:
    from telethon.tl import types

    if not isinstance(entry, dict):
        raise UsageError(f"--{field}: every button must be an object", field=field)
    text = str(entry.get("text") or "")
    kind = str(entry.get("type") or "text")
    if kind == "text":
        return types.KeyboardButton(text=text)
    if kind == "callback":
        return types.KeyboardButtonCallback(
            text=text,
            data=payload_bytes(entry.get("data"), field=field) or b"",
            requires_password=bool(entry.get("requires_password")) or None,
        )
    if kind == "url":
        return types.KeyboardButtonUrl(text=text, url=str(entry.get("url") or ""))
    if kind == "switch_inline":
        return types.KeyboardButtonSwitchInline(
            text=text,
            query=str(entry.get("query") or ""),
            same_peer=bool(entry.get("same_peer")) or None,
        )
    if kind == "webview":
        return types.KeyboardButtonWebView(text=text, url=str(entry.get("url") or ""))
    if kind == "simple_webview":
        return types.KeyboardButtonSimpleWebView(text=text, url=str(entry.get("url") or ""))
    if kind == "copy":
        return types.KeyboardButtonCopy(text=text, copy_text=str(entry.get("copy_text") or ""))
    if kind == "game":
        return types.KeyboardButtonGame(text=text)
    if kind == "request_phone":
        return types.KeyboardButtonRequestPhone(text=text)
    if kind == "request_geo":
        return types.KeyboardButtonRequestGeoLocation(text=text)
    if kind == "request_poll":
        return types.KeyboardButtonRequestPoll(text=text, quiz=entry.get("quiz"))
    raise UsageError(
        f"--{field}: {kind!r} is not a button kind tlgr can author "
        "(text, callback, url, switch_inline, webview, simple_webview, copy, game, "
        "request_phone, request_geo, request_poll)",
        field=field,
    )


# ---------------------------------------------------------------------------
# Inline message ids and DC routing
# ---------------------------------------------------------------------------


def inline_message_id(text: str, *, field: str = "inline_id") -> Any:
    """`dc:id:hash` (or `dc:owner:id:hash`) as an `InputBotInlineMessageID*`.

    The id names the DC the message lives on, and every request that takes one
    must be sent *there*; see `on_dc`.
    """
    from telethon.tl import types

    parts = [p for p in str(text).replace("-", ":").split(":") if p != ""]
    try:
        numbers = [int(p) for p in parts]
    except ValueError as exc:
        raise UsageError(
            f"--{field}: expected 'dc:id:access_hash' (or 'dc:owner:id:access_hash')",
            field=field,
        ) from exc
    if len(numbers) == 3:
        return types.InputBotInlineMessageID(
            dc_id=numbers[0], id=numbers[1], access_hash=numbers[2]
        )
    if len(numbers) == 4:
        return types.InputBotInlineMessageID64(
            dc_id=numbers[0], owner_id=numbers[1], id=numbers[2], access_hash=numbers[3]
        )
    raise UsageError(
        f"--{field}: expected 'dc:id:access_hash' (or 'dc:owner:id:access_hash')", field=field
    )


def inline_id_text(value: Any) -> str:
    """The round-trip spelling of an `InputBotInlineMessageID*`."""
    owner = getattr(value, "owner_id", None)
    parts = [getattr(value, "dc_id", 0)]
    if owner is not None:
        parts.append(owner)
    parts += [getattr(value, "id", 0), getattr(value, "access_hash", 0)]
    return ":".join(str(int(p or 0)) for p in parts)


async def on_dc(ctx: OpContext, dc_id: int, request: Any) -> Any:
    """Send *request* to *dc_id* through an exported sender.

    Inline message ids and web files live on a DC that is not necessarily the
    home one, and sending there anyway fails with an error that says nothing
    about data centres.
    """
    handle = client(ctx)
    if not dc_id:
        return await handle(request)
    sender = await handle._borrow_exported_sender(dc_id)
    try:
        return await sender.send(request)
    finally:
        await handle._return_exported_sender(sender)


# ---------------------------------------------------------------------------
# Bot command scopes
# ---------------------------------------------------------------------------

_SCOPES = {
    "default": "BotCommandScopeDefault",
    "users": "BotCommandScopeUsers",
    "chats": "BotCommandScopeChats",
    "chat-admins": "BotCommandScopeChatAdmins",
    "peer": "BotCommandScopePeer",
    "peer-admins": "BotCommandScopePeerAdmins",
    "peer-user": "BotCommandScopePeerUser",
}


async def command_scope(
    ctx: OpContext, name: str, chat: PeerRef | None, user: PeerRef | None
) -> Any:
    """One of the seven `botCommandScope*` constructors."""
    from telethon.tl import types

    klass_name = _SCOPES.get(name)
    if klass_name is None:
        raise UsageError(f"--scope: {name!r} is not a command scope", field="scope")
    klass = getattr(types, klass_name)
    if name in ("peer", "peer-admins", "peer-user"):
        if chat is None:
            raise UsageError(f"--scope {name} needs --peer", field="peer")
        peer = await _send.resolve(ctx, chat)
        if name == "peer-user":
            if user is None:
                raise UsageError("--scope peer-user needs --user", field="user")
            return klass(peer=peer, user_id=await input_user(ctx, user, field="user"))
        return klass(peer=peer)
    return klass()


# ---------------------------------------------------------------------------
# The report option tree
# ---------------------------------------------------------------------------


def report_outcome(result: Any) -> ReportOutcome:
    """One step of `reportResultChooseOption → addComment → reported`."""
    name = type(result).__name__
    if name == "ReportResultChooseOption":
        return ReportOutcome(
            result="choose_option",
            title=str(getattr(result, "title", "") or ""),
            options=[
                {
                    "text": str(getattr(option, "text", "") or ""),
                    "option": key_text(getattr(option, "option", b"")),
                }
                for option in (getattr(result, "options", None) or [])
            ],
        )
    if name == "ReportResultAddComment":
        return ReportOutcome(
            result="add_comment",
            options=[{"option": key_text(getattr(result, "option", b""))}],
            title="optional" if getattr(result, "optional", False) else "required",
        )
    return ReportOutcome(result="reported", reported=True)
