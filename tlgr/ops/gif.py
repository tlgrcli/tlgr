"""The `gif` group: the saved-GIF shelf, and the inline bot behind the search.

Two genuinely different sends hide behind one verb, and the difference is
visible to everyone in the chat:

* a **saved** GIF goes out as `messages.sendMedia(inputMediaDocument)` and
  looks like any other message;
* a **search result** goes out as `messages.sendInlineBotResult` and is
  attributed "via @gif" unless the bot allows `hide_via`.

`gif send --search` re-runs the query in the same command rather than
accepting a `query_id` from an earlier `gif search`, because a query id
expires in about a minute — taking a stale one would be an API that fails for
reasons the caller cannot see.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.sticker import GifResult, GifSaved, GifSent, SavedGif
from tlgr.ops import _media, _send
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The fallback when `help.getConfig` names no GIF bot. Read from the config
#: first: hardcoding it is how a client keeps querying a bot Telegram moved.
DEFAULT_GIF_BOT = "gif"

_EXAMPLE_GIF: dict[str, Any] = {
    "doc_id": 5312836234,
    "index": 0,
    "mime": "video/mp4",
    "size": 184320,
    "duration": 3,
    "width": 320,
    "height": 240,
}


def _client(ctx: OpContext) -> Any:
    return _media.client(ctx)


async def _saved_documents(ctx: OpContext) -> list[Any]:
    """The saved GIFs, freshly fetched so every reference is live."""
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetSavedGifsRequest(hash=0))
    return list(getattr(result, "gifs", None) or [])


async def _gif_bot(ctx: OpContext, override: PeerRef | None) -> Any:
    """The inline bot to query: `config.gif_search_username`, not a constant."""
    if override is not None:
        return await _send.resolve(ctx, override)
    from telethon.tl.functions import help as help_fn

    username = DEFAULT_GIF_BOT
    try:
        config = await _client(ctx)(help_fn.GetConfigRequest())
        username = str(getattr(config, "gif_search_username", "") or DEFAULT_GIF_BOT)
    except Exception:  # an old server: fall back rather than fail the send
        pass
    # Built rather than parsed: Telegram's own service accounts (`gif`, `vid`,
    # `pic`, `wiki`) are shorter than the four characters a *user* may
    # register, so the username parser rightly refuses them.
    from tlgr.models.peer import PeerRef

    handle = username.lstrip("@")
    return await _send.resolve(ctx, PeerRef(raw=f"@{handle}", kind="username", value=handle))


# ---------------------------------------------------------------------------
# gif list
# ---------------------------------------------------------------------------


class ListReq(Request):
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Download every saved GIF into here."),
    ] = None


async def list_gifs(ctx: OpContext, req: ListReq) -> Page[SavedGif]:
    """Your saved GIFs.

    A hash-cached list capped by `saved_gifs_limit`, with the oldest evicted
    server-side. The index printed here is what `gif send <chat> <index>`
    takes, which is why it is resolved against a freshly fetched list.
    """
    documents = await _saved_documents(ctx)
    items = [_media.gif_model(document, index) for index, document in enumerate(documents)]
    if req.download:
        download = getattr(ctx, "download_file", None)
        if download is None:  # pragma: no cover - the daemon always supplies one
            raise UsageError("this context cannot download files")
        directory = Path(os.path.expanduser(req.download))
        directory.mkdir(parents=True, exist_ok=True)
        for index, document in enumerate(documents):
            path = await download(
                document,
                directory / f"gif_{index}.mp4",
                size=int(getattr(document, "size", 0) or 0),
                dc_id=int(getattr(document, "dc_id", 0) or 0),
            )
            items[index].path = str(path)
    return Page(items=items, has_more=False, total=len(items))


SPEC_LIST = OperationSpec(
    id="gif.list",
    request=ListReq,
    response=Page[SavedGif],
    impl=list_gifs,
    summary="Your saved GIFs",
    columns=("index", "doc_id", "duration", "size"),
    headers=("#", "Doc", "Secs", "Bytes"),
    example={"items": [_EXAMPLE_GIF], "has_more": False},
    example_args="gif list",
    covers=("gif.saved-list",),
)


# ---------------------------------------------------------------------------
# gif add / remove
# ---------------------------------------------------------------------------


class AddReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="Chat holding the animation."),
    ] = None
    msg_id: Annotated[
        list[int],
        arg(1, metavar="MSG_ID", required=False, variadic=True, kind="msg_id", help="Message ids."),
    ] = []
    file_id: Annotated[
        str | None, opt("--file-id", metavar="ID", help="Save media already on Telegram.")
    ] = None
    file: Annotated[
        str | None,
        opt("--file", metavar="PATH", kind="path", help="Upload an animation and save it."),
    ] = None


async def add(ctx: OpContext, req: AddReq) -> GifSaved:
    """Save GIFs.

    `saveGif` needs a live `file_reference`, so the message is re-fetched
    immediately before the call. Only animations are accepted — anything else
    is `GIF_CONTENT_TYPE_INVALID`.
    """
    from telethon.tl.functions import messages as fn

    documents = await _gif_documents(ctx, req)
    before = {int(getattr(d, "id", 0)) for d in await _saved_documents(ctx)}
    for document in documents:
        await _client(ctx)(fn.SaveGifRequest(id=_media.input_document(document), unsave=False))
    ids = [int(getattr(document, "id", 0)) for document in documents]
    after = {int(getattr(d, "id", 0)) for d in await _saved_documents(ctx)}
    evicted = sorted(before - after - set(ids))
    if evicted:
        ctx.warn("the saved-GIF limit was reached; the server evicted the oldest entries")
    return GifSaved(
        doc_ids=ids, saved=len(ids), already=bool(ids) and set(ids) <= before, evicted=evicted
    )


async def _gif_documents(ctx: OpContext, req: AddReq) -> list[Any]:
    if req.file_id:
        from telethon import utils

        resolved = utils.resolve_bot_file_id(req.file_id)
        if resolved is None:
            raise UsageError("--file-id: that is not a Telegram file id", field="file_id")
        return [resolved]
    if req.file:
        # Uploading an animation saves it server-side already; sending it to
        # Saved Messages is what makes a document exist to point at.
        from telethon.tl import types
        from telethon.tl.functions import messages as fn

        upload = getattr(ctx, "upload_file", None)
        if upload is None:  # pragma: no cover
            raise UsageError("this context cannot upload files")
        path = Path(os.path.expanduser(req.file))
        if not path.exists():
            raise UsageError(f"--file: {path} does not exist", field="file")
        handle = await upload(path)
        uploaded = await _client(ctx)(
            fn.UploadMediaRequest(
                peer=types.InputPeerSelf(),
                media=types.InputMediaUploadedDocument(
                    file=handle,
                    mime_type="video/mp4",
                    attributes=[types.DocumentAttributeAnimated()],
                ),
            )
        )
        document = _media.document_of(uploaded)
        if document is None:
            raise NotFoundError("the server did not accept that file as an animation")
        return [document]

    if req.chat is None or not req.msg_id:
        raise UsageError("give a chat and message ids, or --file-id/--file", field="msg_id")
    peer = await _send.resolve(ctx, req.chat)
    out: list[Any] = []
    for message_id in req.msg_id:
        message = await _media.fetch_message(ctx, peer, int(message_id))
        document = _media.document_of(getattr(message, "media", None))
        if document is None:
            raise NotFoundError(f"message {message_id} carries no animation")
        out.append(document)
    return out


SPEC_ADD = OperationSpec(
    id="gif.add",
    request=AddReq,
    response=GifSaved,
    impl=add,
    summary="Save a GIF",
    mutating=True,
    idempotent=True,
    columns=("doc_ids", "saved", "evicted"),
    headers=("Docs", "Saved", "Evicted"),
    example={"doc_ids": [5312836234], "saved": 1, "evicted": []},
    example_args="gif add @alice 12345",
    covers_partial=("gif.save-unsave",),
    coverage_note="Removing is `gif remove`; the shelf itself is `gif list`.",
)


class RemoveReq(Request):
    gif: Annotated[
        list[str],
        arg(0, metavar="GIF", variadic=True, help="Document id, or the index `gif list` printed."),
    ] = []


async def remove(ctx: OpContext, req: RemoveReq) -> GifSaved:
    """Remove GIFs from the saved list.

    Indices are resolved against a freshly fetched list, so the reference is
    valid *and* the index means what the user just saw.
    """
    from telethon.tl.functions import messages as fn

    if not req.gif:
        raise UsageError("name at least one GIF", field="gif")
    documents = await _saved_documents(ctx)
    by_id = {int(getattr(document, "id", 0)): document for document in documents}

    picked: list[Any] = []
    for value in req.gif:
        text = str(value).strip()
        if not text.lstrip("-").isdigit():
            raise UsageError(f"{text!r} is not a document id or an index", field="gif")
        number = int(text)
        if number in by_id:
            picked.append(by_id[number])
        elif 0 <= number < len(documents):
            picked.append(documents[number])
        else:
            raise NotFoundError(f"{text} is neither a saved document id nor an index")

    for document in picked:
        await _client(ctx)(fn.SaveGifRequest(id=_media.input_document(document), unsave=True))
    return GifSaved(
        doc_ids=[int(getattr(document, "id", 0)) for document in picked], removed=len(picked)
    )


SPEC_REMOVE = OperationSpec(
    id="gif.remove",
    request=RemoveReq,
    response=GifSaved,
    impl=remove,
    summary="Remove a GIF from the saved list",
    mutating=True,
    destructive=True,
    columns=("doc_ids", "removed"),
    headers=("Docs", "Removed"),
    example={"doc_ids": [5312836234], "removed": 1},
    example_args="gif remove 0",
    covers=("gif.save-unsave",),
)


# ---------------------------------------------------------------------------
# gif search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", help="What to search for.")]
    bot: Annotated[
        PeerRef | None,
        opt("--bot", metavar="USER", kind="user", help="Inline bot to query."),
    ] = None
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Query in this chat's context."),
    ] = None


def _inline_result(entry: Any, query_id: int) -> GifResult:
    document = getattr(entry, "document", None)
    facts = _media.attributes_of(document) if document is not None else {}
    thumb = getattr(entry, "thumb", None)
    return GifResult(
        result_id=str(getattr(entry, "id", "") or ""),
        query_id=query_id,
        type=str(getattr(entry, "type", "gif") or "gif"),
        title=getattr(entry, "title", None),
        url=getattr(entry, "url", None) or getattr(getattr(entry, "content", None), "url", None),
        thumb=getattr(thumb, "url", None),
        duration=int(facts["duration"]) if facts.get("duration") is not None else None,
        width=facts.get("width"),
        height=facts.get("height"),
        doc_id=getattr(document, "id", None),
    )


async def search(ctx: OpContext, req: SearchReq) -> Page[GifResult]:
    """Search GIFs through the inline GIF bot.

    Results paginate on the bot's own opaque `next_offset` string, which the
    cursor carries; there is no numeric offset to invent.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    limit, state = _media.window(ctx, "gif.search", PageKind.LOCAL)
    bot = await _gif_bot(ctx, req.bot)
    peer = await _send.resolve(ctx, req.chat) if req.chat is not None else types.InputPeerEmpty()
    result = await _client(ctx)(
        fn.GetInlineBotResultsRequest(
            bot=bot, peer=peer, query=req.query, offset=str(state.get("offset", ""))
        )
    )
    query_id = int(getattr(result, "query_id", 0) or 0)
    items = [
        _inline_result(entry, query_id)
        for entry in (getattr(result, "results", None) or [])[:limit]
    ]
    next_offset = str(getattr(result, "next_offset", "") or "")
    return build_page(
        items,
        op="gif.search",
        kind=PageKind.LOCAL,
        state={"offset": next_offset} if next_offset else None,
        account=ctx.account,
        has_more=bool(next_offset),
    )


SPEC_SEARCH = OperationSpec(
    id="gif.search",
    request=SearchReq,
    response=Page[GifResult],
    impl=search,
    summary="Search GIFs through the inline GIF bot",
    paginated=PageKind.LOCAL,
    columns=("result_id", "type", "title", "duration"),
    headers=("Result", "Type", "Title", "Secs"),
    example={
        "items": [{"result_id": "1234", "query_id": 987654321, "type": "gif", "title": "cat"}],
        "has_more": True,
    },
    example_args="gif search cats",
    covers=("gif.search",),
)


# ---------------------------------------------------------------------------
# gif send
# ---------------------------------------------------------------------------


class SendReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to send it.")]
    gif: Annotated[
        str | None,
        arg(1, metavar="GIF", required=False, help="Document id, or a saved-list index."),
    ] = None
    search: Annotated[
        str | None, opt("--search", metavar="QUERY", help="Send from a fresh inline search.")
    ] = None
    pick: Annotated[
        int, opt("--pick", metavar="N", help="Which search result to send (1-based).", ge=1)
    ] = 1
    bot: Annotated[
        PeerRef | None, opt("--bot", metavar="USER", kind="user", help="Inline bot for --search.")
    ] = None
    hide_via: Annotated[
        bool, opt("--hide-via", help="Hide the 'via @gif' attribution where allowed.")
    ] = False
    caption: Annotated[str | None, opt("--caption", metavar="TEXT", help="Caption text.")] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Forum topic id.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification sound.")] = False
    schedule: Annotated[
        str | None, opt("--schedule", metavar="TS", kind="datetime", help="Send later.")
    ] = None
    save: Annotated[bool, opt("--save", help="Also add the sent GIF to the saved list.")] = False


async def send(ctx: OpContext, req: SendReq) -> GifSent:
    """Send a saved GIF, or a fresh search result.

    A search result goes out through `sendInlineBotResult` and carries the
    bot's attribution; a saved GIF is an ordinary `sendMedia`. The two are
    not interchangeable and the response says which happened.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    reply = await _send.reply_target(ctx, reply_to=req.reply_to, topic=req.topic)

    if req.search:
        bot = await _gif_bot(ctx, req.bot)
        found = await _client(ctx)(
            fn.GetInlineBotResultsRequest(bot=bot, peer=peer, query=req.search, offset="")
        )
        results = list(getattr(found, "results", None) or [])
        if len(results) < req.pick:
            raise NotFoundError(f"the search returned {len(results)} results")
        chosen = results[req.pick - 1]
        await _client(ctx)(
            fn.SendInlineBotResultRequest(
                peer=peer,
                query_id=int(getattr(found, "query_id", 0) or 0),
                id=str(getattr(chosen, "id", "")),
                random_id=_random_id(),
                silent=req.silent or None,
                hide_via=req.hide_via or None,
                reply_to=reply,
                schedule_date=_send.schedule_at(req.schedule),
            )
        )
        ctx.emit("media.sent", {"chat_id": chat_id, "kind": "gif", "via_bot": True})
        return GifSent(
            chat_id=chat_id,
            doc_id=getattr(getattr(chosen, "document", None), "id", None),
            via_bot=None if req.hide_via else "inline",
        )

    if req.gif is None:
        raise UsageError("name a saved GIF by index or id, or use --search", field="gif")
    documents = await _saved_documents(ctx)
    by_id = {int(getattr(document, "id", 0)): document for document in documents}
    number = int(req.gif) if str(req.gif).lstrip("-").isdigit() else -1
    document = by_id.get(number) or (documents[number] if 0 <= number < len(documents) else None)
    if document is None:
        raise NotFoundError(f"{req.gif} is neither a saved document id nor an index")

    result = await _client(ctx)(
        fn.SendMediaRequest(
            peer=peer,
            media=types.InputMediaDocument(id=_media.input_document(document)),
            message=req.caption or "",
            random_id=_random_id(),
            silent=req.silent or None,
            reply_to=reply,
            schedule_date=_send.schedule_at(req.schedule),
        )
    )
    message = _send.message_from_updates(result, chat_id=chat_id, sent_text=req.caption or "")
    if req.save:
        await _client(ctx)(fn.SaveGifRequest(id=_media.input_document(document), unsave=False))
    ctx.emit("media.sent", {"chat_id": chat_id, "msg_id": message.id, "kind": "gif"})
    return GifSent(
        chat_id=chat_id,
        msg_id=message.id,
        doc_id=int(getattr(document, "id", 0)),
        saved=req.save,
    )


def _random_id() -> int:
    return int.from_bytes(os.urandom(8), "big", signed=True)


SPEC_SEND = OperationSpec(
    id="gif.send",
    request=SendReq,
    response=GifSent,
    impl=send,
    summary="Send a saved GIF or a GIF search result",
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "doc_id", "via_bot"),
    headers=("Chat", "ID", "Doc", "Via"),
    example={"chat_id": 777123, "msg_id": 12348, "doc_id": 5312836234},
    example_args="gif send @alice 0",
    covers=("gif.send-saved",),
    covers_partial=("gif.search",),
    coverage_note="`gif search` owns the listing; --search re-runs it because a query id expires.",
    tags=frozenset({"visible-to-others"}),
)
