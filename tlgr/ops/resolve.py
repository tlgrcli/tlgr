"""The `resolve` group: references, links and the per-account peer cache.

This is the group whose entire job is to be honest about *how* an answer was
reached, because every other group depends on it being right.

* **A bare numeric id cannot be turned into an access hash.** There is no
  MTProto call that does it for a non-bot account: `users.getUsers` with
  `access_hash=0` answers `UserEmpty` for any non-contact. So an uncached id
  fails with NOT_FOUND or INDETERMINATE rather than being guessed, which is
  the trap `user dialog-status` was built around.
* **`PHONE_NOT_OCCUPIED` is ambiguous.** No account, or an owner who refuses
  lookups by phone — the two are indistinguishable from here, so
  `resolve phone` exits 13 INDETERMINATE, never 5.
* **Resolution never acts.** Joining a chat, starting a bot, installing a
  theme or a sticker set, enabling a proxy, applying a boost, redeeming a
  gift: each is a separate, confirmed command in its own group, and
  `resolve link` names it in `delegated_to` instead of doing it.
* **Access hashes are per login session.** They are never printed and never
  copied between accounts; `access_hash_cached` is the only thing said about
  them.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlsplit

from tlgr.core.errors import IndeterminateError, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.timefmt import fmt_dt
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import Peer, PeerRef, parse_peer_ref
from tlgr.models.resolve import (
    CachedPeerRow,
    ResolvedLink,
    ResolvedPhone,
    ResolvedRef,
    ResolvedUsername,
)
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer
from tlgr.ops._spec import OpContext, OperationSpec
from tlgr.ops.contact import client_of, e164

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: Bot API ids offset channels by this much; the two id spaces differ and a
#: caller moving between them should not have to remember the arithmetic.
_CHANNEL_MARK = -1000000000000

#: `tg://` paths that name a settings screen rather than a peer.
_SETTINGS_SECTIONS = frozenset(
    {
        "settings",
        "privacy",
        "language",
        "themes",
        "devices",
        "folders",
        "chat_folders",
        "stickers",
        "premium",
        "premium_offer",
        "premium_multigift",
        "stars",
        "stars_topup",
        "giftcode",
        "restore_purchases",
        "passport",
        "change_number",
        "auto_delete",
        "edit_profile",
    }
)

#: `t.me/contacts/<section>` — a screen in the contacts UI, not an RPC.
_CONTACT_SECTIONS = frozenset({"new", "search", "sort", "invite", "manage"})

#: kind → the command that would *act* on a link of that kind.
DELEGATES: dict[str, str] = {
    "invite": "chat join",
    "chatlist-invite": "folder join",
    "folder": "folder join",
    "bot-start": "bot start",
    "bot-startgroup": "bot add",
    "bot-startchannel": "bot add",
    "webapp": "webapp open",
    "proxy": "proxy add",
    "boost": "boost apply",
    "giftcode": "gift redeem",
    "unique-gift": "gift get",
    "stars-topup": "stars buy",
    "stickerset": "sticker set install",
    "emojiset": "sticker set install",
    "theme": "settings theme install",
    "wallpaper": "chat wallpaper set",
    "contact-token": "contact add",
    "share-url": "message send",
    "business-chat-link": "message send",
    "message": "message get",
    "private-post": "message get",
    "story": "story get",
    "public-username": "chat get",
    "phone": "user get",
}

_EXAMPLE_REF: dict[str, Any] = {
    "ref": "@alice",
    "kind": "username",
    "id": 777123,
    "marked_id": 777123,
    "type": "user",
    "title": "Alice",
    "username": "alice",
    "source": "resolve_username",
    "resolved": True,
}


def raw_id(marked: int) -> int:
    """The unmarked MTProto id behind a marked one.

    The two id spaces differ only for chats and channels — `-100…` and `-`
    are the marks — and every caller that has reimplemented this arithmetic
    has eventually got a channel id wrong. `resolve peer` emits both.
    """
    if marked < _CHANNEL_MARK:
        return _CHANNEL_MARK - marked
    if marked < 0:
        return -marked
    return marked


def _peer_of(entity: Any) -> Peer:
    return entity_to_peer(entity)


def _kind_matches(kind: str, wanted: str) -> bool:
    return kind in {
        "user": {"user", "saved"},
        "bot": {"bot"},
        "group": {"group", "supergroup"},
        "channel": {"channel"},
    }.get(wanted, {wanted})


# ---------------------------------------------------------------------------
# resolve username
# ---------------------------------------------------------------------------


class UsernameReq(Request):
    username: Annotated[str, arg(0, metavar="USERNAME", help="With or without the @.")]
    referer: Annotated[
        str | None,
        opt("--referer", metavar="USERNAME", help="Attribute the resolution to a referrer."),
    ] = None
    type: Annotated[
        str | None,
        choice("user", "bot", "group", "channel", help="Fail unless the result is of this kind."),
    ] = None


async def username(ctx: OpContext, req: UsernameReq) -> ResolvedUsername:
    """Resolve a public @username to a peer.

    `USERNAME_INVALID` (malformed) and `USERNAME_NOT_OCCUPIED` (free) are
    different answers and get different exit codes — 2 and 5 — because "you
    typed it wrong" and "nobody has it" call for different reactions.

    This hits the network every time and floods at roughly fifty lookups in a
    short window, so the returned access hash is persisted in the per-account
    peer cache on the way out.
    """
    from telethon.tl.functions import contacts as fn

    handle = (req.username or "").strip().lstrip("@")
    if not handle:
        raise UsageError("a username is required", field="username")

    kwargs: dict[str, Any] = {"username": handle}
    if req.referer:
        # Layer 224+ only; an older build simply does not accept the field,
        # and losing the attribution is better than losing the resolution.
        try:
            request = fn.ResolveUsernameRequest(referer=req.referer, **kwargs)
        except TypeError:
            ctx.warn("this Telethon build has no --referer support; resolving without it")
            request = fn.ResolveUsernameRequest(**kwargs)
    else:
        request = fn.ResolveUsernameRequest(**kwargs)

    try:
        result = await client_of(ctx)(request)
    except Exception as exc:
        name = type(exc).__name__
        if name == "UsernameInvalidError":
            raise UsageError(f"@{handle} is not a valid username", field="username") from exc
        if name == "UsernameNotOccupiedError":
            raise NotFoundError(f"nobody holds @{handle}") from exc
        raise

    entities = list(getattr(result, "users", None) or []) + list(
        getattr(result, "chats", None) or []
    )
    if not entities:
        raise NotFoundError(f"nobody holds @{handle}")
    peer = _peer_of(entities[0])
    if req.type and not _kind_matches(peer.kind, req.type):
        raise NotFoundError(f"@{handle} is a {peer.kind}, not a {req.type}")

    # Persist what we paid a round trip for.
    resolver = getattr(ctx, "resolver", None)
    if resolver is not None:
        with contextlib.suppress(Exception):
            resolver._remember(entities[0], username=handle)
    return ResolvedUsername(
        kind=peer.kind,
        peer=peer,
        username=handle,
        access_hash_cached=bool(getattr(entities[0], "access_hash", None)),
    )


SPEC_USERNAME = OperationSpec(
    id="resolve.username",
    request=UsernameReq,
    response=ResolvedUsername,
    impl=username,
    summary="Resolve a public @username to a peer",
    description=(
        "USERNAME_INVALID exits 2 and USERNAME_NOT_OCCUPIED exits 5: a typo "
        "and a free username are different answers. Resolution always hits "
        "the network and floods at roughly fifty lookups in a short period, "
        "so the access hash is cached for a day afterwards."
    ),
    rate_class="resolve",
    columns=("kind", "peer.id", "peer.title", "username"),
    example={
        "kind": "user",
        "username": "alice",
        "peer": {"id": 777123, "raw_id": 777123, "kind": "user", "title": "Alice"},
    },
    example_args="resolve username @alice",
    covers=("contacts-users.search-public-chat",),
)


# ---------------------------------------------------------------------------
# resolve phone
# ---------------------------------------------------------------------------


class PhoneReq(Request):
    phone: Annotated[str, arg(0, metavar="PHONE", help="+countrycode number.")] = ""
    offline: Annotated[bool, opt("--offline", help="Format and validate only; perform no RPC.")] = (
        False
    )
    countries: Annotated[bool, opt("--countries", help="Dump the country/prefix/format table.")] = (
        False
    )
    lang: Annotated[str, opt("--lang", metavar="CODE", help="Language for the table.")] = ""


async def _country_table(ctx: OpContext, lang: str) -> list[dict[str, Any]]:
    from telethon.tl.functions import help as hfn

    result = await client_of(ctx)(hfn.GetCountriesListRequest(lang_code=lang or "", hash=0))
    out: list[dict[str, Any]] = []
    for country in getattr(result, "countries", None) or []:
        for code in getattr(country, "country_codes", None) or []:
            out.append(
                {
                    "iso2": getattr(country, "iso2", None),
                    "name": getattr(country, "default_name", None),
                    "prefix": "+" + str(getattr(code, "country_code", "") or ""),
                    "patterns": list(getattr(code, "patterns", None) or []),
                }
            )
    return out


def _match_country(number: str, table: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Longest prefix wins: +1 and +1204 both exist and only one is right."""
    best: dict[str, Any] | None = None
    for row in table:
        prefix = str(row.get("prefix") or "")
        if (
            len(prefix) > 1
            and number.startswith(prefix)
            and (best is None or len(prefix) > len(str(best.get("prefix") or "")))
        ):
            best = row
    return best


async def phone(ctx: OpContext, req: PhoneReq) -> ResolvedPhone:
    """Resolve a phone number to a user, without adding a contact.

    `PHONE_NOT_OCCUPIED` is genuinely ambiguous — the number may have no
    account, or its owner may hide themselves behind
    `inputPrivacyKeyAddedByPhone` — so this exits 13 INDETERMINATE and never
    "not found". Unlike `contact add`, nothing is saved to the address book.

    Telegram asks for at most one of these every three seconds, which is why
    `--offline` exists: format and validate locally first.
    """
    from telethon.tl.functions import contacts as fn

    number = e164(req.phone)
    table: list[dict[str, Any]] = []
    if req.countries and not number:
        table = await _country_table(ctx, req.lang)
        return ResolvedPhone(phone="", e164="", resolved=False, countries=table)
    if not number:
        raise UsageError("a phone number is required", field="phone")

    out = ResolvedPhone(phone=req.phone, e164=number)
    if req.offline or req.countries:
        table = await _country_table(ctx, req.lang)
        match = _match_country(number, table)
        if match is not None:
            out.country = str(match.get("name") or "")
            out.prefix = str(match.get("prefix") or "")
            patterns = list(match.get("patterns") or [])
            out.pattern = str(patterns[0]) if patterns else None
    if req.countries:
        out.countries = table
    if req.offline:
        out.reason = "offline: the number was formatted and validated, not looked up"
        return out

    try:
        result = await client_of(ctx)(fn.ResolvePhoneRequest(phone=number.lstrip("+")))
    except Exception as exc:
        name = type(exc).__name__
        if name == "PhoneNumberInvalidError":
            raise UsageError(f"{number} is not a valid phone number", field="phone") from exc
        # Everything else — including PHONE_NOT_OCCUPIED — is "we could not
        # establish it", and a caller must not read it as "no account".
        out.reason = (
            f"{type(exc).__name__}: the number may have no Telegram account, OR its "
            "owner may refuse lookups by phone. These are not distinguishable."
        )
        raise IndeterminateError(out.reason) from exc

    entities = list(getattr(result, "users", None) or []) + list(
        getattr(result, "chats", None) or []
    )
    if not entities:
        raise IndeterminateError(
            "the server answered with no peer: no account, or a privacy refusal"
        )
    out.peer = _peer_of(entities[0])
    out.resolved = True
    return out


SPEC_PHONE = OperationSpec(
    id="resolve.phone",
    request=PhoneReq,
    response=ResolvedPhone,
    impl=phone,
    summary="Resolve a phone number to a user without adding a contact",
    description=(
        "PHONE_NOT_OCCUPIED exits 13, never 5: no account and a privacy "
        "refusal are indistinguishable from here. The server asks for at "
        "most one lookup every three seconds, so --offline formats and "
        "validates against help.getCountriesList without an RPC."
    ),
    rate_class="resolve",
    min_interval_s=3.0,
    columns=("e164", "country", "resolved"),
    example={"phone": "+15550001111", "e164": "+15550001111", "resolved": False},
    example_args="resolve phone +15550001111 --offline",
    covers=("contacts-users.phone-number-info", "contacts-users.resolve-phone"),
)


# ---------------------------------------------------------------------------
# resolve peer
# ---------------------------------------------------------------------------


class PeerReq(Request):
    ref: Annotated[
        list[str],
        arg(0, metavar="REF", variadic=True, help="@username, id, marked id, +phone, link, me."),
    ] = []
    from_chat: Annotated[
        PeerRef | None,
        opt("--from-chat", metavar="CHAT", kind="peer", help="Context chat for a `min` peer."),
    ] = None
    from_message: Annotated[
        int | None,
        opt("--from-message", metavar="ID", kind="msg_id", help="Message id in --from-chat."),
    ] = None
    ids: Annotated[
        str | None,
        choice("mtproto", "botapi", help="Also emit the id in the other id space."),
    ] = None
    cache_only: Annotated[bool, opt("--cache-only", help="Never hit the network.")] = False


async def _describe(ctx: OpContext, target: Any) -> tuple[str, str]:
    """`(type, title)` for a resolved peer, from whatever is already known."""
    kind = {
        "InputPeerUser": "user",
        "InputPeerUserFromMessage": "user",
        "InputPeerChat": "group",
        "InputPeerChannel": "channel",
        "InputPeerChannelFromMessage": "channel",
        "InputPeerSelf": "saved",
    }.get(type(target).__name__, "unknown")
    title = ""
    with contextlib.suppress(Exception):
        entity = await client_of(ctx).get_entity(target)
        peer = _peer_of(entity)
        kind, title = peer.kind, peer.title
    return kind, title


async def peer(ctx: OpContext, req: PeerReq) -> Page[ResolvedRef]:
    """Resolve any peer reference to a normalised peer object.

    Order: cache → `resolveUsername`/`resolvePhone` → `getPeerDialogs` →
    dialog-list scan → `contacts.search`, cheapest first, and every step
    exists because the one before it cannot answer. A bare numeric id that
    nothing has cached fails — there is no call that mints an access hash for
    it — rather than being guessed at.

    `--from-chat/--from-message` builds `inputPeerUserFromMessage` for a
    `min` peer, which Telethon never builds and which is what makes a
    stranger seen in a channel actionable.
    """
    from telethon import utils
    from telethon.tl import types

    if not req.ref:
        raise UsageError("give at least one reference to resolve", field="ref")

    rows: list[ResolvedRef] = []
    for raw in req.ref:
        row = ResolvedRef(ref=raw)
        try:
            parsed = parse_peer_ref(raw)
        except ValueError as exc:
            row.reason = str(exc)
            rows.append(row)
            continue
        row.kind = parsed.kind

        if req.from_chat is not None and req.from_message is not None and parsed.kind == "id":
            container = await _send.resolve(ctx, req.from_chat)
            target: Any = types.InputPeerUserFromMessage(
                peer=container, msg_id=int(req.from_message), user_id=abs(int(parsed.value))
            )
            row.source = "from_message"
            row.min = True
        else:
            resolver = getattr(ctx, "resolver", None)
            if resolver is None:  # pragma: no cover - the daemon always supplies one
                raise UsageError("no peer resolver is available in this context")
            try:
                target = await resolver.resolve(parsed, allow_network=not req.cache_only)
            except Exception as exc:
                row.reason = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                if len(req.ref) == 1:
                    raise
                continue
            row.source = "cache" if req.cache_only else _source_for(parsed.kind)

        with contextlib.suppress(TypeError, ValueError):
            row.marked_id = int(utils.get_peer_id(target))
        # `id` is the raw MTProto id, `marked_id` the signed form every tlgr
        # response uses (COR-10). Both are always present so nobody has to
        # redo the sign arithmetic; --ids adds the Bot API spelling, which is
        # the marked one.
        row.id = raw_id(row.marked_id) if row.marked_id is not None else None
        row.access_hash_cached = bool(int(getattr(target, "access_hash", 0) or 0))
        row.type, row.title = await _describe(ctx, target)
        row.username = str(parsed.value) if parsed.kind == "username" else None
        if req.ids is not None:
            row.botapi_id = row.marked_id if req.ids == "botapi" else row.id
        row.resolved = row.marked_id is not None
        rows.append(row)

    return Page(items=rows, has_more=False, total=len(rows))


def _source_for(kind: str) -> str:
    return {
        "username": "resolve_username",
        "phone": "resolve_phone",
        "id": "cache_or_dialogs",
        "invite": "check_chat_invite",
        "self": "self",
        "saved": "self",
        "link": "link",
    }.get(kind, kind)


SPEC_PEER = OperationSpec(
    id="resolve.peer",
    request=PeerReq,
    response=Page[ResolvedRef],
    impl=peer,
    summary="Resolve any peer reference to a normalised peer object",
    description=(
        "There is NO method that turns a bare id into an access hash, so an "
        "uncached numeric id fails (exit 5 or 13) instead of guessing — that "
        "is the trap `user dialog-status` was built around. Access hashes "
        "are per account and never printed."
    ),
    rate_class="resolve",
    columns=("ref", "id", "type", "title", "source"),
    headers=("Ref", "Id", "Kind", "Title", "How"),
    example={"items": [_EXAMPLE_REF], "has_more": False},
    example_args="resolve peer @alice",
    covers=("contacts-users.peer-id-conversion", "dialogs.resolve-peer"),
)


# ---------------------------------------------------------------------------
# resolve link
# ---------------------------------------------------------------------------


class LinkReq(Request):
    url: Annotated[str, arg(0, metavar="URL", help="Any t.me / tg:// link, or a bare slug.")]
    no_network: Annotated[
        bool, opt("--no-network", help="Classify from the URL only; resolve nothing.")
    ] = False
    open: Annotated[
        bool, opt("--open", help="Perform the follow-up read for the classified kind.")
    ] = False
    draft: Annotated[
        PeerRef | None,
        opt("--draft", metavar="CHAT", kind="peer", help="Save the carried text as a draft here."),
    ] = None


def _split(url: str) -> tuple[str, list[str], dict[str, str]]:
    """`(scheme, path segments, query)` for a t.me or tg:// reference."""
    text = (url or "").strip()
    if text.lower().startswith("tg://"):
        rest = text[5:]
        head, _, query = rest.partition("?")
        return "tg", [s for s in head.split("/") if s], dict(parse_qsl(query))
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parts = urlsplit(text)
    host = (parts.netloc or "").lower()
    if host not in ("t.me", "telegram.me", "telegram.dog", "www.t.me"):
        return "", [], {}
    return "tme", [s for s in parts.path.split("/") if s], dict(parse_qsl(parts.query))


def classify(url: str) -> ResolvedLink:
    """Classify a link from its shape alone. No network, no side effects.

    One function rather than twenty commands because the human pasting a
    link does not know which of the twenty kinds it is — that is the
    question. `unknown` is a real answer and keeps the raw path.
    """
    out = ResolvedLink(raw_url=url)
    scheme, segments, query = _split(url)
    out.scheme = scheme
    if not scheme:
        return out

    if scheme == "tg":
        verb = (segments[0] if segments else "").lower()
        return _classify_tg(out, verb, query)

    if not segments:
        return out

    head = segments[0]
    lowered = head.lower()

    if lowered == "contact" and len(segments) > 1:
        out.kind = "contact-token"
        out.contact_token = segments[1]
    elif lowered == "addlist" and len(segments) > 1:
        out.kind = "chatlist-invite"
        out.chatlist_slug = segments[1]
    elif lowered == "list" and len(segments) > 1:
        out.kind = "folder"
        out.chatlist_slug = segments[1]
    elif lowered in ("addstickers", "addemoji") and len(segments) > 1:
        out.kind = "emojiset" if lowered == "addemoji" else "stickerset"
        out.stickerset = segments[1]
    elif lowered == "addtheme" and len(segments) > 1:
        out.kind = "theme"
        out.theme = segments[1]
    elif lowered == "bg" and len(segments) > 1:
        out.kind = "wallpaper"
        out.wallpaper = segments[1]
    elif lowered == "proxy" or lowered == "socks":
        out.kind = "proxy"
        out.proxy = dict(query)
    elif lowered == "share" and query:
        out.kind = "share-url"
        out.share = dict(query)
    elif lowered == "giftcode" and len(segments) > 1:
        out.kind = "giftcode"
        out.gift = segments[1]
    elif lowered == "nft" and len(segments) > 1:
        out.kind = "unique-gift"
        out.gift = segments[1]
    elif lowered == "boost":
        out.kind = "boost"
        out.boost = True
        out.username = query.get("c") or (segments[1] if len(segments) > 1 else None)
    elif lowered == "m" and len(segments) > 1:
        out.kind = "business-chat-link"
        out.start_param = segments[1]
    elif lowered == "invoice" and len(segments) > 1:
        out.kind = "invoice"
        out.start_param = segments[1]
    elif lowered == "login" and len(segments) > 1:
        out.kind = "login-code"
        out.start_param = segments[1]
    elif lowered == "contacts" and len(segments) > 1 and segments[1].lower() in _CONTACT_SECTIONS:
        out.kind = "contacts-section"
        out.section = segments[1].lower()
    elif head.startswith("+") or lowered == "joinchat":
        value = head[1:] if head.startswith("+") else (segments[1] if len(segments) > 1 else "")
        # `t.me/+15550001111` is a PHONE when it parses as a number; only
        # otherwise is it an invite hash. Guessing the wrong one turns a
        # contact lookup into a join.
        if value.isdigit():
            out.kind = "phone"
            out.phone = "+" + value
        elif value:
            out.kind = "invite"
            out.invite_hash = value
    elif lowered == "c" and len(segments) > 2 and segments[1].isdigit():
        out.kind = "private-post"
        out.msg_id = int(segments[2]) if segments[2].isdigit() else None
        out.username = None
        out.thread_id = int(segments[3]) if len(segments) > 3 and segments[3].isdigit() else None
    elif lowered == "s" and len(segments) > 1:
        out.kind = "public-username"
        out.username = segments[1].lower()
    else:
        out.username = head.lower()
        if len(segments) > 1 and segments[1].isdigit():
            out.kind = "message"
            out.msg_id = int(segments[1])
            if len(segments) > 2 and segments[2].isdigit():
                out.thread_id, out.msg_id = out.msg_id, int(segments[2])
        elif len(segments) > 1 and segments[1].lower() == "s" and len(segments) > 2:
            out.kind = "story"
            out.story_id = int(segments[2]) if segments[2].isdigit() else None
        elif "start" in query:
            out.kind = "bot-start"
            out.bot = head.lower()
            out.start_param = query["start"]
        elif "startgroup" in query:
            out.kind = "bot-startgroup"
            out.bot = head.lower()
            out.start_param = query["startgroup"]
        elif "startchannel" in query:
            out.kind = "bot-startchannel"
            out.bot = head.lower()
            out.start_param = query["startchannel"]
        elif "startapp" in query or "appname" in query:
            out.kind = "webapp"
            out.bot = head.lower()
            out.start_param = query.get("startapp") or query.get("appname")
        else:
            out.kind = "public-username"

    if "comment" in query and query["comment"].isdigit():
        out.comment_id = int(query["comment"])
    if "thread" in query and query["thread"].isdigit():
        out.thread_id = int(query["thread"])
    if "single" in query and out.kind == "message":
        out.start_target = "single"
    return out


def _classify_tg(out: ResolvedLink, verb: str, query: dict[str, str]) -> ResolvedLink:
    if verb == "resolve":
        out.username = (query.get("domain") or "").lower() or None
        out.phone = ("+" + query["phone"]) if query.get("phone") else None
        if query.get("post", "").isdigit():
            out.kind = "message"
            out.msg_id = int(query["post"])
        elif "start" in query:
            out.kind = "bot-start"
            out.bot = out.username
            out.start_param = query["start"]
        elif "startapp" in query:
            out.kind = "webapp"
            out.bot = out.username
            out.start_param = query["startapp"]
        elif out.phone:
            out.kind = "phone"
        else:
            out.kind = "public-username"
    elif verb == "join":
        out.kind = "invite"
        out.invite_hash = query.get("invite")
    elif verb == "privatepost":
        out.kind = "private-post"
        out.msg_id = int(query["post"]) if query.get("post", "").isdigit() else None
    elif verb in ("addstickers", "addemoji"):
        out.kind = "emojiset" if verb == "addemoji" else "stickerset"
        out.stickerset = query.get("set")
    elif verb == "addtheme":
        out.kind = "theme"
        out.theme = query.get("slug")
    elif verb in ("bg", "wallpaper"):
        out.kind = "wallpaper"
        out.wallpaper = query.get("slug") or query.get("color")
    elif verb in ("proxy", "socks"):
        out.kind = "proxy"
        out.proxy = dict(query)
    elif verb == "msg_url":
        out.kind = "share-url"
        out.share = dict(query)
    elif verb == "boost":
        out.kind = "boost"
        out.boost = True
        out.username = (query.get("domain") or "").lower() or None
    elif verb == "giftcode":
        out.kind = "giftcode"
        out.gift = query.get("slug")
    elif verb == "nft":
        out.kind = "unique-gift"
        out.gift = query.get("slug")
    elif verb in ("stars_topup", "premium_offer"):
        out.kind = "stars-topup" if verb == "stars_topup" else "premium-offer"
        out.stars = int(query["balance"]) if query.get("balance", "").isdigit() else None
    elif verb == "confirmphone":
        out.kind = "confirm-phone"
        out.phone = ("+" + query["phone"]) if query.get("phone") else None
    elif verb == "login":
        out.kind = "login-code"
        out.start_param = query.get("code")
    elif verb == "message":
        out.kind = "business-chat-link"
        out.start_param = query.get("slug")
    elif verb == "invoice":
        out.kind = "invoice"
        out.start_param = query.get("slug")
    elif verb in _SETTINGS_SECTIONS:
        out.kind = "settings-section"
        out.section = verb
    elif verb == "contacts":
        out.kind = "contacts-section"
        out.section = (query.get("section") or "new").lower()
    return out


async def _open(ctx: OpContext, out: ResolvedLink) -> None:
    """The follow-up *read* for a classified link. Never an action."""
    from telethon.tl import types as tl
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import contacts as cfn
    from telethon.tl.functions import messages as mfn
    from telethon.tl.functions import payments as pfn
    from telethon.tl.functions import premium as prfn
    from telethon.tl.functions import stories as sfn

    client = client_of(ctx)
    kind = out.kind
    if kind in ("public-username", "bot-start", "bot-startgroup", "bot-startchannel", "webapp"):
        found = await client(cfn.ResolveUsernameRequest(out.username or out.bot or ""))
        entities = list(getattr(found, "users", None) or []) + list(
            getattr(found, "chats", None) or []
        )
        if entities:
            out.peer = _peer_of(entities[0])
    elif kind == "phone" and out.phone:
        found = await client(cfn.ResolvePhoneRequest(phone=out.phone.lstrip("+")))
        entities = list(getattr(found, "users", None) or [])
        if entities:
            out.peer = _peer_of(entities[0])
    elif kind == "invite" and out.invite_hash:
        preview = await client(mfn.CheckChatInviteRequest(hash=out.invite_hash))
        out.title = str(getattr(preview, "title", "") or "")
        chat = getattr(preview, "chat", None)
        out.peer = _peer_of(chat) if chat is not None else None
        out.opened = {"already_member": type(preview).__name__ == "ChatInviteAlready"}
    elif kind in ("chatlist-invite", "folder") and out.chatlist_slug:
        from telethon.tl.functions import chatlists as clfn

        preview = await client(clfn.CheckChatlistInviteRequest(slug=out.chatlist_slug))
        title = getattr(preview, "title", None)
        out.title = str(getattr(title, "text", title) or "")
    elif kind == "contact-token" and out.contact_token:
        imported = await client(cfn.ImportContactTokenRequest(token=out.contact_token))
        out.peer = _peer_of(imported) if imported is not None else None
    elif kind == "business-chat-link" and out.start_param:
        resolved = await client(afn.ResolveBusinessChatLinkRequest(slug=out.start_param))
        message = getattr(resolved, "message", None)
        out.opened = {"text": message}
    elif kind in ("message", "private-post") and out.msg_id:
        from tlgr.core.peers import channel_id_from_link

        if out.username:
            reference: Any = "@" + out.username
        else:
            found_link = channel_id_from_link(out.raw_url)
            if found_link is None:
                raise NotFoundError("that private-post link carries no channel id")
            # A bare channel id needs an access hash this account already
            # holds; there is no call that mints one, so this fails loudly.
            reference = str(found_link[0])
        target = await _send.resolve(ctx, reference)
        found = await client.get_messages(target, ids=[out.msg_id])
        text = next((getattr(m, "message", "") for m in found or [] if m is not None), "")
        out.peer = out.peer or Peer(
            id=_send.peer_id_of(target), raw_id=abs(_send.peer_id_of(target)), kind="unknown"
        )
        out.opened = {"text": text}
    elif kind == "story" and out.story_id and out.username:
        target = await _send.resolve(ctx, "@" + out.username)
        found = await client(sfn.GetStoriesByIDRequest(peer=target, id=[out.story_id]))
        out.opened = {"stories": len(list(getattr(found, "stories", None) or []))}
    elif kind == "boost" and out.username:
        target = await _send.resolve(ctx, "@" + out.username)
        status = await client(prfn.GetBoostsStatusRequest(peer=target))
        out.opened = {
            "level": getattr(status, "level", None),
            "boosts": getattr(status, "boosts", None),
        }
    elif kind == "giftcode" and out.gift:
        info = await client(pfn.CheckGiftCodeRequest(slug=out.gift))
        out.opened = {"used": bool(getattr(info, "used_date", None))}
    elif kind == "unique-gift" and out.gift:
        info = await client(pfn.GetUniqueStarGiftRequest(slug=out.gift))
        out.opened = {"title": getattr(getattr(info, "gift", None), "title", None)}
    elif kind in ("stickerset", "emojiset") and out.stickerset:
        info = await client(
            mfn.GetStickerSetRequest(
                stickerset=tl.InputStickerSetShortName(short_name=out.stickerset), hash=0
            )
        )
        out.title = str(getattr(getattr(info, "set", None), "title", "") or "")
    elif kind == "theme" and out.theme:
        info = await client(
            afn.GetThemeRequest(format="android", theme=tl.InputThemeSlug(slug=out.theme))
        )
        out.title = str(getattr(info, "title", "") or "")
    elif kind == "wallpaper" and out.wallpaper:
        info = await client(
            afn.GetWallPaperRequest(wallpaper=tl.InputWallPaperSlug(slug=out.wallpaper))
        )
        out.opened = {"id": getattr(info, "id", None)}


async def link(ctx: OpContext, req: LinkReq) -> ResolvedLink:
    """Normalise any t.me / tg:// link into a typed object.

    Classification is local and always happens; `--open` adds the read that
    matches the kind. Nothing here ever *acts*: joining, starting a bot,
    installing a theme, enabling a proxy, applying a boost and redeeming a
    gift are separate confirmed verbs, and `delegated_to` names the one this
    link would need.
    """
    from telethon.tl.functions import help as hfn

    out = classify(req.url)
    out.delegated_to = DELEGATES.get(out.kind)
    out.requires_action = out.kind in DELEGATES and out.kind not in (
        "public-username",
        "message",
        "private-post",
        "phone",
    )

    if out.kind == "unknown" and out.scheme == "tg" and not req.no_network:
        # Telegram adds deep links faster than any client learns them;
        # `help.getDeepLinkInfo` is the server telling us what it means. The
        # query is deliberately not sent — it can carry a token.
        path = req.url.split("://", 1)[-1].split("?", 1)[0]
        info = await client_of(ctx)(hfn.GetDeepLinkInfoRequest(path=path))
        message = getattr(info, "message", None)
        if message:
            out.deeplink_info = str(message)

    if req.no_network:
        return out
    if req.open:
        try:
            await _open(ctx, out)
        except Exception as exc:
            ctx.warn(f"--open could not read this link: {type(exc).__name__}: {exc}")

    if req.draft is not None:
        text = (out.share or {}).get("text") or (out.opened or {}).get("text")
        if not text:
            raise UsageError("this link carries no text to save as a draft", field="draft")
        if getattr(ctx, "dry_run", False):
            ctx.warn("--dry-run: the carried text would be saved as a draft")
        else:
            from telethon.tl.functions import messages as mfn

            target = await _send.resolve(ctx, req.draft)
            await client_of(ctx)(mfn.SaveDraftRequest(peer=target, message=str(text)))
            out.draft_saved = True
    return out


SPEC_LINK = OperationSpec(
    id="resolve.link",
    request=LinkReq,
    response=ResolvedLink,
    impl=link,
    summary="Normalise any t.me / tg:// link into a typed JSON object",
    description=(
        "One dispatcher and one discriminated union. `t.me/+X` is a PHONE "
        "when X parses as a number and an invite hash otherwise. "
        "`t.me/c/<id>/<msg>` carries a bare channel id, so the access hash "
        "must come from this account's peer cache — it fails loudly rather "
        "than guessing. Resolution NEVER acts: `delegated_to` names the "
        "command that would."
    ),
    rate_class="resolve",
    tags=frozenset({"mutating-checked"}),
    columns=("kind", "username", "msg_id", "delegated_to"),
    example={
        "kind": "message",
        "raw_url": "https://t.me/alice/4210",
        "scheme": "tme",
        "username": "alice",
        "msg_id": 4210,
        "delegated_to": "message get",
    },
    example_args="resolve link https://t.me/alice/4210",
    covers=(
        "contact.share-token",
        "contacts-users.contacts-deeplink-sections",
        "contacts-users.resolve-account-maintenance-links",
        "contacts-users.resolve-boost-link",
        "contacts-users.resolve-bot-start-link",
        "contacts-users.resolve-business-chat-link",
        "contacts-users.resolve-deeplink",
        "contacts-users.resolve-gift-link",
        "contacts-users.resolve-invite-link",
        "contacts-users.resolve-message-link",
        "contacts-users.resolve-proxy-link",
        "contacts-users.resolve-share-url-link",
        "contacts-users.resolve-stickerset-link",
        "contacts-users.resolve-story-link",
        "contacts-users.resolve-theme-wallpaper-link",
        "contacts-users.resolve-unknown-deeplink",
        "dialogs.business-link-resolve",
    ),
)


# ---------------------------------------------------------------------------
# resolve cache get
# ---------------------------------------------------------------------------


class CacheGetReq(Request):
    type: Annotated[
        str | None, choice("user", "bot", "group", "channel", help="Only entries of this kind.")
    ] = None
    refresh: Annotated[
        list[PeerRef],
        opt("--refresh", metavar="PEER", kind="peer", help="Re-fetch these peers."),
    ] = []
    purge: Annotated[
        bool, opt("--purge", help="Drop cached entries (never the session auth key).")
    ] = False
    stale: Annotated[
        str | None,
        opt("--stale", metavar="DURATION", kind="duration", help="Only entries older than this."),
    ] = None


async def cache_get(ctx: OpContext, req: CacheGetReq) -> Page[CachedPeerRow]:
    """Inspect, refresh or purge this account's peer database.

    The cache is what makes a bare numeric id addressable at all, and it is
    per account: an access hash minted for one login is meaningless to
    another, which is why this never prints one and why `--purge` is scoped
    to the resolver's own store and never touches the session.

    `min_context` is the `(chat, message)` where a `min` user was seen.
    Telethon records none, so tlgr keeps it; without it a stranger who posted
    in a channel cannot be addressed at all.
    """
    from tlgr.core.timefmt import parse_duration

    resolver = getattr(ctx, "resolver", None)
    if resolver is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("no peer resolver is available in this context")
    cache = resolver.cache

    refreshed: set[int] = set()
    if req.refresh:
        if getattr(ctx, "dry_run", False):
            ctx.warn(f"--dry-run: {len(req.refresh)} peers would be re-fetched")
        else:
            for ref in req.refresh:
                target = await resolver.resolve(ref)
                with contextlib.suppress(Exception):
                    entity = await client_of(ctx).get_entity(target)
                    resolver._remember(entity)
                    refreshed.add(_send.peer_id_of(target))

    cutoff = 0.0
    if req.stale:
        seconds = parse_duration(req.stale)
        if seconds is None:
            raise UsageError(f"--stale: cannot read {req.stale!r} as a duration", field="stale")
        cutoff = time.time() - float(seconds)

    rows: list[CachedPeerRow] = []
    for entry in list(cache.by_id.values()):
        if req.type and not _kind_matches(entry.kind, req.type):
            continue
        if cutoff and entry.resolved_at > cutoff:
            continue
        seen = float(entry.resolved_at or 0.0)
        rows.append(
            CachedPeerRow(
                id=abs(int(entry.peer_id)),
                marked_id=int(entry.peer_id),
                type=entry.kind,
                username=entry.username or None,
                access_hash_cached=bool(entry.access_hash),
                min=not entry.access_hash and bool(entry.from_message),
                min_context=(
                    f"{entry.from_chat}:{entry.from_message}" if entry.from_message else None
                ),
                seen_at=fmt_dt(datetime.fromtimestamp(seen, tz=timezone.utc)) if seen else None,
                seen_at_unix=int(seen) if seen else None,
                refreshed=int(entry.peer_id) in refreshed,
            )
        )
    rows.sort(key=lambda row: (-(row.seen_at_unix or 0), row.marked_id))

    purged = 0
    if req.purge:
        if getattr(ctx, "dry_run", False):
            ctx.warn(f"--dry-run: {len(rows)} cache entries would be dropped")
        else:
            for row in rows:
                entry = cache.by_id.pop(row.marked_id, None)
                if entry is not None:
                    purged += 1
                    if entry.username:
                        cache.by_username.pop(entry.username.lower(), None)
                row.purged = True
            cache._dirty = True
            cache.save()
            ctx.emit("peer_cache_purge", {"count": purged})

    limit = int(getattr(ctx, "limit", None) or 200)
    token = getattr(ctx, "cursor", None)
    offset = 0
    if token:
        offset = int(
            decode_cursor(
                token, op="resolve.cache.get", kind=PageKind.LOCAL, account=ctx.account
            ).get("offset", 0)
            or 0
        )
    window = rows[offset : offset + limit]
    return build_page(
        window,
        op="resolve.cache.get",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window)},
        account=ctx.account,
        has_more=offset + len(window) < len(rows),
        total=len(rows),
    )


SPEC_CACHE_GET = OperationSpec(
    id="resolve.cache.get",
    request=CacheGetReq,
    response=Page[CachedPeerRow],
    impl=cache_get,
    summary="Inspect, refresh or purge the local peer database",
    description=(
        "Access-hash priority is full > min > from-message > none. Telethon "
        "skips `min` entities in both of its caches, so tlgr records the "
        "(peer, msg_id) context itself — that is what makes a `chat posters` "
        "follow-up possible. Hashes are per login session: never printed, "
        "never copied between accounts. `--purge` drops cache rows only; the "
        "session and its auth key are untouched."
    ),
    paginated=PageKind.LOCAL,
    rate_class="local",
    tags=frozenset({"mutating-checked"}),
    columns=("marked_id", "type", "username", "access_hash_cached", "seen_at"),
    headers=("Id", "Kind", "Username", "Hash", "Seen"),
    example={
        "items": [{"id": 777123, "marked_id": 777123, "type": "user", "access_hash_cached": True}],
        "has_more": False,
    },
    example_args="resolve cache get",
    covers=("contacts-users.peer-cache",),
)
