"""The `inline` group: `@bot query`, and the two halves of sending a result.

Inline mode looks like a search box and behaves like nothing else in the API.

* **Offsets are the bot's, not Telegram's.** `next_offset` is an opaque string
  the bot invented; feeding it back is the only way to page, and an empty one
  is the end. tlgr passes it through untouched rather than wrapping it in a
  signed cursor that would imply an ordering nobody promised.
* **A result id is only valid with its query id, and only briefly.** They come
  back paired for `cache_time` seconds. `inline send --pick` therefore re-runs
  the query itself instead of accepting a pair from an earlier command, which
  is the difference between a command that works and one that fails whenever
  the user paused to think.
* **A silent bot is not an error.** `BOT_RESPONSE_TIMEOUT` means the bot is
  offline. That is an empty page (exit 3), not a failure — an agent that reads
  it as a failure retries something that will never answer.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.inline import (
    InlineEdited,
    InlineResult,
    InlineSent,
    PreparedMessage,
    PreparedSaved,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _bots, _send
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `botInlineMessage*` → the `send_message` kind reported on a result.
_MESSAGE_KINDS = {
    "BotInlineMessageText": "text",
    "BotInlineMessageMediaAuto": "media_auto",
    "BotInlineMessageMediaGeo": "geo",
    "BotInlineMessageMediaVenue": "venue",
    "BotInlineMessageMediaContact": "contact",
    "BotInlineMessageMediaInvoice": "invoice",
    "BotInlineMessageMediaWebPage": "webpage",
    "BotInlineMessageRichMessage": "rich",
    "BotInlineMessageGame": "game",
}

_PEER_TYPES = {
    "pm": "InlineQueryPeerTypePM",
    "bot": "InlineQueryPeerTypeBotPM",
    "group": "InlineQueryPeerTypeChat",
    "megagroup": "InlineQueryPeerTypeMegagroup",
    "channel": "InlineQueryPeerTypeBroadcast",
    "broadcast": "InlineQueryPeerTypeBroadcast",
    "same_bot": "InlineQueryPeerTypeSameBotPM",
}

_EXAMPLE_RESULT: dict[str, Any] = {
    "n": 0,
    "id": "BQADAgAD",
    "type": "gif",
    "title": "cat",
    "query_id": "987654321",
}


def _result_model(entry: Any, index: int, query_id: int, results: Any = None) -> InlineResult:
    """One `botInlineResult`/`botInlineMediaResult` as one row.

    The two constructors differ in where the bytes live — a `WebDocument` the
    client must fetch, or a `Photo`/`Document` Telegram already holds — and
    `content` is what says which, so a caller that needs to know still can.
    """
    send = getattr(entry, "send_message", None)
    document = getattr(entry, "document", None)
    photo = getattr(entry, "photo", None)
    thumb = getattr(entry, "thumb", None)
    # The constructor, not the payload: a media result with neither a photo
    # nor a document is still a media result, and saying "url" would send a
    # caller looking for a URL that is not there.
    media = type(entry).__name__ == "BotInlineMediaResult"
    return InlineResult(
        n=index,
        id=str(getattr(entry, "id", "") or ""),
        type=str(getattr(entry, "type", "") or ""),
        title=getattr(entry, "title", None),
        description=getattr(entry, "description", None),
        url=getattr(entry, "url", None),
        thumb=getattr(thumb, "url", None),
        content="media" if media else "url",
        send_message=_MESSAGE_KINDS.get(type(send).__name__),
        query_id=str(query_id),
        doc_id=int(getattr(document, "id", 0) or 0) or None,
        photo_id=int(getattr(photo, "id", 0) or 0) or None,
        gallery=bool(getattr(results, "gallery", False)) if results is not None else False,
        cache_time=getattr(results, "cache_time", None) if results is not None else None,
    )


def _switch(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "text": getattr(value, "text", None),
        "start_param": getattr(value, "start_param", None),
        "url": getattr(value, "url", None),
    }


def _timed_out(exc: BaseException) -> bool:
    """`BOT_RESPONSE_TIMEOUT` — the bot is offline, which is an answer."""
    return "BOTRESPONSETIMEOUT" in f"{type(exc).__name__} {exc}".upper().replace("_", "")


async def _query(
    ctx: OpContext,
    bot: Any,
    peer: Any,
    text: str,
    offset: str,
    geo: Any = None,
) -> Any:
    from telethon.tl.functions import messages as fn

    return await client(ctx)(
        fn.GetInlineBotResultsRequest(bot=bot, peer=peer, query=text, offset=offset, geo_point=geo)
    )


def _geo(lat: float | None, lon: float | None, accuracy: int | None) -> Any:
    if lat is None or lon is None:
        return None
    from telethon.tl import types

    return types.InputGeoPoint(lat=lat, long=lon, accuracy_radius=accuracy)


# ---------------------------------------------------------------------------
# inline query
# ---------------------------------------------------------------------------


class QueryReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The inline bot.")]
    query: Annotated[
        str, arg(1, metavar="QUERY", required=False, help="Query text; empty is valid.")
    ] = ""
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Chat the query is made from."),
    ] = None
    offset: Annotated[
        str | None, opt("--offset", metavar="TOKEN", help="Opaque next_offset from a page.")
    ] = None
    lat: Annotated[float | None, opt("--lat", metavar="DEG", help="Latitude for geo bots.")] = None
    lon: Annotated[float | None, opt("--lon", metavar="DEG", help="Longitude for geo bots.")] = None
    accuracy: Annotated[
        int | None, opt("--accuracy", metavar="M", help="Location accuracy radius in metres.")
    ] = None


async def query(ctx: OpContext, req: QueryReq) -> Page[InlineResult]:
    """Query an inline bot and list what it answers with.

    The chat matters: a bot is told which kind of chat the query came from and
    routinely answers differently in a group than in a private chat, so
    `--chat` is not cosmetic.
    """
    from telethon.tl import types

    limit, state = window(ctx, "inline.query", PageKind.RATE, default=50)
    bot = await _bots.input_user(ctx, req.bot)
    peer = await _send.resolve(ctx, req.chat) if req.chat is not None else types.InputPeerSelf()
    offset = req.offset if req.offset is not None else str(state.get("offset", "") or "")

    try:
        results = await _query(
            ctx, bot, peer, req.query, offset, _geo(req.lat, req.lon, req.accuracy)
        )
    except Exception as exc:
        if not _timed_out(exc):
            raise
        ctx.warn("the bot did not answer in time; it is probably offline")
        return Page(items=[], has_more=False, total=0)

    query_id = int(getattr(results, "query_id", 0) or 0)
    entries = list(getattr(results, "results", None) or [])[:limit]
    items = [_result_model(entry, index, query_id, results) for index, entry in enumerate(entries)]
    next_offset = str(getattr(results, "next_offset", "") or "")
    if items:
        items[0].next_offset = next_offset or None
        items[0].switch_pm = _switch(getattr(results, "switch_pm", None))
        items[0].switch_webview = _switch(getattr(results, "switch_webview", None))
    return build_page(
        items,
        op="inline.query",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=None,
    )


SPEC_QUERY = OperationSpec(
    id="inline.query",
    request=QueryReq,
    response=Page[InlineResult],
    impl=query,
    summary="Query an inline bot and list its results",
    description=(
        "Paging offsets are opaque strings the bot invented, not integers: "
        "the `next_offset` on the first row is fed straight back, and an "
        "empty one means the end. A bot that does not answer is an empty page "
        "with a warning, not an error."
    ),
    paginated=PageKind.RATE,
    empty_exit=3,
    columns=("n", "id", "type", "title"),
    headers=("#", "ID", "Type", "Title"),
    example={"items": [_EXAMPLE_RESULT], "has_more": False},
    example_args="inline query @gifbot cat",
    covers=(
        "bots.inline-query",
        "bots.inline-query-paging",
        "bots.inline-query-with-location",
        "bots.inline-result-message-kinds",
        "bots.inline-result-types",
        "bots.inline-switch-webview",
        "bots.switch-inline-button",
    ),
    covers_partial=("bots.inline-switch-pm", "bots.webapp-switch-inline-query"),
    coverage_note=(
        "A `switch_pm` button is completed with `bot start --param` and a "
        "`switch_webview` one with `webapp open --from-switch-webview`."
    ),
)


# ---------------------------------------------------------------------------
# inline search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    kind: Annotated[str, arg(0, metavar="KIND", help="gif, venue or image.")]
    query: Annotated[str, arg(1, metavar="QUERY", required=False, help="Search text.")] = ""
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Chat the search is made from."),
    ] = None
    lat: Annotated[float | None, opt("--lat", metavar="DEG", help="Latitude (venue).")] = None
    lon: Annotated[float | None, opt("--lon", metavar="DEG", help="Longitude (venue).")] = None
    offset: Annotated[str | None, opt("--offset", metavar="TOKEN", help="Opaque next_offset.")] = (
        None
    )


_SEARCH_BOTS = {
    "gif": ("gif_search_username", "gif"),
    "venue": ("venue_search_username", "foursquare"),
    "image": ("img_search_username", "pic"),
}


async def search(ctx: OpContext, req: SearchReq) -> Page[InlineResult]:
    """Search the built-in inline bots for GIFs, venues or images.

    The usernames come from `help.getConfig`, never from a constant here:
    Telegram has moved them before, and a hardcoded one would keep querying an
    account that no longer serves anything.
    """
    from telethon.tl import types
    from telethon.tl.functions import help as help_fn

    if req.kind not in _SEARCH_BOTS:
        raise UsageError("kind must be gif, venue or image", field="kind")
    if req.kind == "venue" and (req.lat is None or req.lon is None):
        raise UsageError("a venue search needs --lat and --lon", field="lat")

    key, fallback = _SEARCH_BOTS[req.kind]
    username = fallback
    try:
        config = await client(ctx)(help_fn.GetConfigRequest())
        username = str(getattr(config, key, "") or fallback)
    except Exception:  # an older server: fall back rather than fail the search
        pass

    limit, state = window(ctx, "inline.search", PageKind.RATE, default=50)
    # Built rather than parsed: Telegram's own service accounts are shorter
    # than the four characters a *user* may register, so the username parser
    # rightly refuses them.
    handle = username.lstrip("@")
    bot = await _bots.input_user(
        ctx, PeerRef(raw=f"@{handle}", kind="username", value=handle), field="kind"
    )
    peer = await _send.resolve(ctx, req.chat) if req.chat is not None else types.InputPeerEmpty()
    offset = req.offset if req.offset is not None else str(state.get("offset", "") or "")

    try:
        results = await _query(ctx, bot, peer, req.query, offset, _geo(req.lat, req.lon, None))
    except Exception as exc:
        if not _timed_out(exc):
            raise
        ctx.warn(f"@{handle} did not answer in time")
        return Page(items=[], has_more=False, total=0)

    query_id = int(getattr(results, "query_id", 0) or 0)
    entries = list(getattr(results, "results", None) or [])[:limit]
    items = [_result_model(entry, index, query_id, results) for index, entry in enumerate(entries)]
    next_offset = str(getattr(results, "next_offset", "") or "")
    return build_page(
        items,
        op="inline.search",
        kind=PageKind.RATE,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
    )


SPEC_SEARCH = OperationSpec(
    id="inline.search",
    request=SearchReq,
    response=Page[InlineResult],
    impl=search,
    summary="Search the built-in inline bots for GIFs, venues or images",
    paginated=PageKind.RATE,
    empty_exit=3,
    columns=("n", "id", "type", "title"),
    headers=("#", "ID", "Type", "Title"),
    example={"items": [_EXAMPLE_RESULT], "has_more": False},
    example_args="inline search gif cat",
    covers=("bots.gif-search-inline", "bots.img-search-inline", "bots.venue-search-inline"),
)


# ---------------------------------------------------------------------------
# inline send
# ---------------------------------------------------------------------------


class SendReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The inline bot.")]
    query: Annotated[
        str, arg(1, metavar="QUERY", required=False, help="Query to re-run for --pick.")
    ] = ""
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="Destination chat.")
    ] = None
    pick: Annotated[
        str | None, opt("--pick", metavar="N|ID", help="Result to send: index or result id.")
    ] = None
    query_id: Annotated[
        str | None, opt("--query-id", metavar="ID", help="query_id from a previous `inline query`.")
    ] = None
    result_id: Annotated[
        str | None, opt("--result-id", metavar="ID", help="Result id belonging to --query-id.")
    ] = None
    hide_via: Annotated[bool, opt("--hide-via", help="Drop the 'via @bot' header.")] = False
    clear_draft: Annotated[bool, opt("--clear-draft", help="Clear the chat draft.")] = False
    background: Annotated[bool, opt("--background", help="Send in the background.")] = False
    quick_reply: Annotated[
        str | None,
        opt("--quick-reply", metavar="SHORTCUT", help="Store it in a Business quick reply."),
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    quote: Annotated[str | None, opt("--quote", help="Quoted fragment of the reply target.")] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Forum topic id.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    schedule: Annotated[
        str | None, opt("--schedule", metavar="TS|online", help="Schedule the send.")
    ] = None
    send_as: Annotated[
        PeerRef | None, opt("--send-as", metavar="PEER", kind="peer", help="Send as this peer.")
    ] = None
    paid_stars: Annotated[
        int | None,
        opt("--paid-stars", metavar="N", help="Agree to pay N Stars for a paid-message peer."),
    ] = None
    business_connection: Annotated[
        str | None,
        opt(
            "--business-connection", metavar="ID", help="Send as a business account (bot session)."
        ),
    ] = None


async def send(ctx: OpContext, req: SendReq) -> InlineSent:
    """Send one chosen inline result into a chat.

    `--pick` re-runs the query in this same command rather than taking a
    `(query_id, result_id)` pair from an earlier one, because that pair
    expires in about a minute: a two-command workflow would fail whenever the
    human in the middle took a moment to choose.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.chat is None:
        raise UsageError("--chat is required", field="chat")
    if req.paid_stars and req.paid_stars < 0:
        raise UsageError("--paid-stars cannot be negative", field="paid_stars")

    target = await _send.resolve(ctx, req.chat)
    query_id, result_id = await _pair(ctx, req, target)

    request = fn.SendInlineBotResultRequest(
        peer=target,
        query_id=query_id,
        id=result_id,
        random_id=_random_id(),
        silent=req.silent or None,
        background=req.background or None,
        clear_draft=req.clear_draft or None,
        hide_via=req.hide_via or None,
        reply_to=await _send.reply_target(
            ctx, reply_to=req.reply_to, quote=req.quote, topic=req.topic
        ),
        schedule_date=_send.schedule_at(req.schedule),
        send_as=await _send.resolve(ctx, req.send_as) if req.send_as is not None else None,
        quick_reply_shortcut=(
            types.InputQuickReplyShortcut(shortcut=req.quick_reply) if req.quick_reply else None
        ),
        allow_paid_stars=req.paid_stars,
    )
    updates = await _invoke_as(ctx, req.business_connection, request)
    message = _send.message_from_updates(updates, chat_id=_send.peer_id_of(target))
    ctx.emit("inline_send", {"chat_id": message.chat_id, "result_id": result_id})
    return InlineSent(
        chat_id=message.chat_id,
        msg_id=message.id,
        result_id=result_id,
        via_bot_id=message.via_bot_id,
        quick_reply=req.quick_reply,
    )


async def _pair(ctx: OpContext, req: SendReq, target: Any) -> tuple[int, str]:
    """The `(query_id, result_id)` pair, freshly minted unless one was given."""
    if req.query_id and req.result_id:
        try:
            return int(req.query_id), req.result_id
        except ValueError as exc:
            raise UsageError("--query-id must be numeric", field="query_id") from exc
    if req.query_id or req.result_id:
        raise UsageError("--query-id and --result-id are only valid together", field="query_id")
    if req.pick is None:
        raise UsageError("give --pick, or --query-id with --result-id", field="pick")

    bot = await _bots.input_user(ctx, req.bot)
    results = await _query(ctx, bot, target, req.query, "")
    entries = list(getattr(results, "results", None) or [])
    query_id = int(getattr(results, "query_id", 0) or 0)
    spec = req.pick.strip()
    if spec.isdigit() and int(spec) < len(entries):
        return query_id, str(getattr(entries[int(spec)], "id", ""))
    for entry in entries:
        if str(getattr(entry, "id", "")) == spec:
            return query_id, spec
    raise NotFoundError(f"the bot returned no result {spec!r} for that query")


def _random_id() -> int:
    from tlgr.ops._common import random_id

    return random_id()


async def _invoke_as(ctx: OpContext, connection_id: str | None, request: Any) -> Any:
    from tlgr.ops.bot import _invoke_as as wrap

    return await wrap(ctx, connection_id, request)


SPEC_SEND = OperationSpec(
    id="inline.send",
    request=SendReq,
    response=InlineSent,
    impl=send,
    summary="Send a chosen inline result to a chat",
    tags=frozenset({"visible-to-others"}),
    description=(
        "`--paid-stars` agrees to a per-message Star fee. Naming the number "
        "is the consent: `--yes` is a CLI-level gate an operation never sees, "
        "so a flag that spends money spells out how much."
    ),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "result_id"),
    headers=("Chat", "Message", "Result"),
    example={"chat_id": 4242, "msg_id": 12, "result_id": "BQADAgAD"},
    example_args="inline send @gifbot cat --chat @alice --pick 0",
    covers=(
        "bots.inline-result-into-quick-reply",
        "bots.send-inline-result",
        "bots.webapp-switch-inline-query",
    ),
    covers_partial=("bots.gif-search-inline", "bots.venue-search-inline"),
    coverage_note="Running the built-in searches themselves is `inline search`.",
)


# ---------------------------------------------------------------------------
# inline edit
# ---------------------------------------------------------------------------


class EditReq(Request):
    inline_msg_id: Annotated[
        str, arg(0, metavar="INLINE_MSG_ID", help="Inline message id as dc:id:access_hash.")
    ]
    text: Annotated[str | None, opt("--text", help="New text.")] = None
    media: Annotated[str | None, opt("--media", metavar="PATH", kind="path", help="New media.")] = (
        None
    )
    buttons: Annotated[
        str | None, opt("--buttons", metavar="PATH", kind="path", help="New keyboard, as JSON.")
    ] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Text formatting.")] = None
    no_preview: Annotated[bool, opt("--no-preview", help="Disable the link preview.")] = False


async def edit(ctx: OpContext, req: EditReq) -> InlineEdited:
    """Edit a message that was sent through inline mode.

    The request has to reach the DC named in the inline message id. Sending it
    to the home DC fails with an error that says nothing about data centres,
    which is why the id carries one at all.
    """
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "editing an inline message")
    identifier = _bots.inline_message_id(req.inline_msg_id, field="inline_msg_id")
    text, entities = _send.body(req.text, parse=req.parse) if req.text is not None else ("", [])
    await _bots.on_dc(
        ctx,
        int(getattr(identifier, "dc_id", 0) or 0),
        fn.EditInlineBotMessageRequest(
            id=identifier,
            message=text if req.text is not None else None,
            entities=_send.tl_entities(entities) if req.text is not None else None,
            media=await _send.input_media(ctx, req.media) if req.media else None,
            reply_markup=_bots.keyboard_tl(req.buttons, field="buttons"),
            no_webpage=req.no_preview or None,
        ),
    )
    return InlineEdited(inline_msg_id=req.inline_msg_id, edited=True)


SPEC_EDIT = OperationSpec(
    id="inline.edit",
    request=EditReq,
    response=InlineEdited,
    impl=edit,
    summary="Edit a message sent through inline mode",
    mutating=True,
    columns=("inline_msg_id", "edited"),
    headers=("Inline ID", "Edited"),
    example={"inline_msg_id": "2:123:456", "edited": True},
    example_args="inline edit 2:123:456 --text Updated",
    covers=("bots.edit-inline-message",),
)


# ---------------------------------------------------------------------------
# inline prepared
# ---------------------------------------------------------------------------


class PreparedGetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The mini app's bot.")]
    id: Annotated[str, arg(1, metavar="ID", help="Prepared message id from the app.")]


def _peer_type_names(values: Any) -> list[str]:
    names = {tl: name for name, tl in _PEER_TYPES.items()}
    return [names.get(type(v).__name__, type(v).__name__) for v in (values or [])]


async def prepared_get(ctx: OpContext, req: PreparedGetReq) -> PreparedMessage:
    """Inspect a prepared inline message shared from a mini app.

    `peer_types` is not advisory: it restricts which chats the picker may
    offer, and `inline prepared send` refuses a chat outside it rather than
    letting the server reject the send after the fact.
    """
    from telethon.tl.functions import messages as fn

    result = await client(ctx)(
        fn.GetPreparedInlineMessageRequest(bot=await _bots.input_user(ctx, req.bot), id=req.id)
    )
    query_id = int(getattr(result, "query_id", 0) or 0)
    entry = getattr(result, "result", None)
    return PreparedMessage(
        query_id=str(query_id),
        result=_result_model(entry, 0, query_id) if entry is not None else None,
        peer_types=_peer_type_names(getattr(result, "peer_types", None)),
        cache_time=getattr(result, "cache_time", None),
    )


SPEC_PREPARED_GET = OperationSpec(
    id="inline.prepared.get",
    request=PreparedGetReq,
    response=PreparedMessage,
    impl=prepared_get,
    summary="Inspect a prepared inline message from a mini app",
    columns=("query_id", "peer_types", "cache_time"),
    headers=("Query", "Chat types", "Cache"),
    example={"query_id": "987654321", "peer_types": ["pm"]},
    example_args="inline prepared get @my_helper_bot abc123",
    covers_partial=("bots.prepared-inline-message-send",),
    coverage_note="Sending it is `inline prepared send`.",
)


class PreparedSaveReq(Request):
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Who will be able to share it."),
    ] = None
    result: Annotated[
        str | None, opt("--result", metavar="PATH", kind="path", help="JSON inline result.")
    ] = None
    peer_types: Annotated[
        list[str],
        opt("--peer-types", metavar="KIND", help="Chat types the picker may offer (repeatable)."),
    ] = []


async def prepared_save(ctx: OpContext, req: PreparedSaveReq) -> PreparedSaved:
    """Save a prepared inline message for a user to share later."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    await _bots.require_bot_session(ctx, "saving a prepared inline message")
    if req.user is None or not req.result:
        raise UsageError("--user and --result are both required", field="user")

    from tlgr.ops.bot import _inline_results

    peer_types = []
    for name in req.peer_types:
        klass = _PEER_TYPES.get(name)
        if klass is None:
            raise UsageError(
                f"--peer-types: {name!r} is not a chat type ({', '.join(sorted(_PEER_TYPES))})",
                field="peer_types",
            )
        peer_types.append(getattr(types, klass)())

    result = await client(ctx)(
        fn.SavePreparedInlineMessageRequest(
            result=_inline_results(req.result)[0],
            user_id=await _bots.input_user(ctx, req.user, field="user"),
            peer_types=peer_types or None,
        )
    )
    from tlgr.core.timefmt import fmt_dt

    return PreparedSaved(
        id=str(getattr(result, "id", "") or ""),
        expires_at=fmt_dt(getattr(result, "expire_date", None)),
    )


SPEC_PREPARED_SAVE = OperationSpec(
    id="inline.prepared.save",
    request=PreparedSaveReq,
    response=PreparedSaved,
    impl=prepared_save,
    summary="Save a prepared inline message for a user",
    mutating=True,
    columns=("id", "expires_at"),
    headers=("ID", "Expires"),
    example={"id": "abc123"},
    example_args="inline prepared save --user @alice --result ./result.json",
    covers=("bots.prepared-inline-message-save",),
)


class PreparedSendReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The mini app's bot.")]
    id: Annotated[str, arg(1, metavar="ID", help="Prepared message id from the app.")]
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="Destination chat.")
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    hide_via: Annotated[bool, opt("--hide-via", help="Drop the 'via @bot' header.")] = False


_PEER_KINDS = {
    "InputPeerUser": {"pm", "bot", "same_bot"},
    "InputPeerChat": {"group"},
    "InputPeerChannel": {"channel", "broadcast", "megagroup", "group"},
    "InputPeerSelf": {"pm", "same_bot"},
}


async def prepared_send(ctx: OpContext, req: PreparedSendReq) -> InlineSent:
    """Send a prepared inline message a mini app handed over.

    The app said which chat types it allows; a destination outside them is a
    usage error here rather than a server rejection, because the app's
    restriction is the thing the user agreed to when they tapped share.
    """
    from telethon.tl.functions import messages as fn

    if req.chat is None:
        raise UsageError("--chat is required", field="chat")
    prepared = await prepared_get(ctx, PreparedGetReq(bot=req.bot, id=req.id))
    target = await _send.resolve(ctx, req.chat)
    allowed = set(prepared.peer_types)
    if allowed:
        kinds = _PEER_KINDS.get(type(target).__name__, set())
        if not (kinds & allowed):
            raise UsageError(
                f"this prepared message may only go to {', '.join(sorted(allowed))}",
                field="chat",
            )

    updates = await client(ctx)(
        fn.SendInlineBotResultRequest(
            peer=target,
            query_id=int(prepared.query_id or 0),
            id=prepared.result.id if prepared.result is not None else "",
            random_id=_random_id(),
            silent=req.silent or None,
            hide_via=req.hide_via or None,
            reply_to=await _send.reply_target(ctx, reply_to=req.reply_to),
        )
    )
    message = _send.message_from_updates(updates, chat_id=_send.peer_id_of(target))
    return InlineSent(
        chat_id=message.chat_id,
        msg_id=message.id,
        result_id=prepared.result.id if prepared.result is not None else "",
        via_bot_id=message.via_bot_id,
    )


SPEC_PREPARED_SEND = OperationSpec(
    id="inline.prepared.send",
    request=PreparedSendReq,
    response=InlineSent,
    impl=prepared_send,
    summary="Send a prepared inline message shared from a mini app",
    tags=frozenset({"visible-to-others"}),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "result_id"),
    headers=("Chat", "Message", "Result"),
    example={"chat_id": 4242, "msg_id": 12, "result_id": "BQADAgAD"},
    example_args="inline prepared send @my_helper_bot abc123 --chat @alice",
    covers=("bots.prepared-inline-message-send",),
)
