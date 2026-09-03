"""The `emoji` group: custom emoji ids, the picker's lists, and keywords.

`emoji set *` is not here — it is `sticker set *` under an alias, because a
custom-emoji set takes exactly the same calls as a sticker set. What is here
is the part that has no sticker equivalent:

* **turning an id into something printable.** A `MessageEntityCustomEmoji`
  carries only a document id; `alt` is the plain emoji it stands for and
  `free` says whether a non-Premium account may send it. Without this a
  terminal shows an invisible entity over ordinary text.
* **the picker's own lists**, which are server-provided and hash-cached: the
  category chips above the search boxes, and the emoji a non-Premium account
  may use for an avatar or a reply background.
* **keywords**, which are a versioned *local* database rather than a query
  endpoint — fetched once per language and kept current with a difference
  call, which is why the default search path costs no round trip.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.sticker import EmojiGroup, EmojiKeyword, Sticker
from tlgr.ops import _media, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `--kind` → the request that serves that list. Spelled as (module, name) so
#: the module still imports without Telethon.
_LIST_REQUESTS: dict[str, tuple[str, str]] = {
    "groups": ("messages", "GetEmojiGroupsRequest"),
    "sticker-groups": ("messages", "GetEmojiStickerGroupsRequest"),
    "status-groups": ("messages", "GetEmojiStatusGroupsRequest"),
    "profile-photo-groups": ("messages", "GetEmojiProfilePhotoGroupsRequest"),
    "default-profile-photo": ("account", "GetDefaultProfilePhotoEmojisRequest"),
    "default-group-photo": ("account", "GetDefaultGroupPhotoEmojisRequest"),
    "default-background": ("account", "GetDefaultBackgroundEmojisRequest"),
}

_EXAMPLE_EMOJI: dict[str, Any] = {
    "doc_id": 5312836234,
    "emoji": "😀",
    "custom_emoji": True,
    "free": True,
    "set_short_name": "MyEmojiPack",
    "mime": "application/x-tgsticker",
}


def _client(ctx: OpContext) -> Any:
    return _media.client(ctx)


# ---------------------------------------------------------------------------
# emoji get
# ---------------------------------------------------------------------------


class GetReq(Request):
    emoji_id: Annotated[
        list[int],
        arg(
            0, metavar="EMOJI_ID", required=False, variadic=True, help="Custom emoji document ids."
        ),
    ] = []
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Download the documents into here."),
    ] = None
    convert: Annotated[
        str, choice("png", "json", "none", help="Post-process downloads: TGS to Lottie JSON.")
    ] = "none"
    from_message: Annotated[
        str | None,
        opt("--from-message", metavar="CHAT:ID", help="Take every id used in this message."),
    ] = None


async def get(ctx: OpContext, req: GetReq) -> Page[Sticker]:
    """Resolve custom emoji ids to their documents.

    This is what turns the opaque ids in a message's entities into something
    a terminal can print. `--from-message` reads them straight out of the
    entities, so a caller never has to parse them by hand.
    """
    from telethon.tl.functions import messages as fn

    ids = [int(value) for value in req.emoji_id]
    if req.from_message:
        ids.extend(await _ids_in_message(ctx, req.from_message))
    if not ids:
        raise UsageError("give at least one emoji id, or --from-message", field="emoji_id")

    documents = await _client(ctx)(fn.GetCustomEmojiDocumentsRequest(document_id=ids))
    items = [_media.sticker_model(document) for document in documents or []]
    if req.download:
        await _download(ctx, req, documents or [], items)
    return Page(items=items, has_more=False, total=len(items))


async def _ids_in_message(ctx: OpContext, reference: str) -> list[int]:
    """Every `MessageEntityCustomEmoji` document id in a message."""
    peer_text, _, message_id = str(reference).rpartition(":")
    if not peer_text or not message_id.lstrip("-").isdigit():
        raise UsageError("--from-message takes CHAT:ID", field="from_message")
    from tlgr.models.peer import parse_peer_ref

    peer = await _send.resolve(ctx, parse_peer_ref(peer_text))
    message = await _media.fetch_message(ctx, peer, int(message_id))
    out: list[int] = []
    for entity in getattr(message, "entities", None) or []:
        document_id = getattr(entity, "document_id", None)
        if document_id is not None:
            out.append(int(document_id))
    return out


async def _download(
    ctx: OpContext, req: GetReq, documents: list[Any], items: list[Sticker]
) -> None:
    from tlgr.ops.sticker import _convert

    download = getattr(ctx, "download_file", None)
    if download is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot download files")
    directory = Path(os.path.expanduser(str(req.download)))
    directory.mkdir(parents=True, exist_ok=True)
    for index, document in enumerate(documents):
        suffix = {
            "image/webp": ".webp",
            "application/x-tgsticker": ".tgs",
            "video/webm": ".webm",
        }.get(getattr(document, "mime_type", "") or "", ".bin")
        written = await download(
            document,
            directory / f"{getattr(document, 'id', index)}{suffix}",
            size=int(getattr(document, "size", 0) or 0),
            dc_id=int(getattr(document, "dc_id", 0) or 0),
        )
        path = Path(written)
        if req.convert != "none":
            path = _convert(path, req.convert)
        items[index].path = str(path)


SPEC_GET = OperationSpec(
    id="emoji.get",
    request=GetReq,
    response=Page[Sticker],
    impl=get,
    summary="Resolve custom emoji ids to their documents",
    columns=("doc_id", "emoji", "free", "set_short_name"),
    headers=("Doc", "Emoji", "Free", "Set"),
    example={"items": [_EXAMPLE_EMOJI], "has_more": False},
    example_args="emoji get 5312836234",
    covers=("emoji.custom-fetch",),
)


# ---------------------------------------------------------------------------
# emoji list
# ---------------------------------------------------------------------------


class ListReq(Request):
    kind: Annotated[
        str,
        choice(*_LIST_REQUESTS, help="Which server-provided list."),
    ] = "groups"
    resolve: Annotated[
        bool, opt("--resolve", help="Also resolve the returned ids to documents.")
    ] = False


async def list_groups(ctx: OpContext, req: ListReq) -> Page[EmojiGroup]:
    """The picker's own lists.

    The groups are the category chips above the sticker, emoji and GIF search
    boxes: each carries a title, an icon custom-emoji id and an emoticon list
    usable as a `sticker search --emoji` filter. The `default-*` lists are the
    emoji a non-Premium account may use for an avatar, a group photo or a
    reply background, which is what makes those settable without guessing ids.
    """
    module_name, request_name = _LIST_REQUESTS[req.kind]
    if module_name == "messages":
        from telethon.tl.functions import messages as module
    else:
        from telethon.tl.functions import account as module

    result = await _client(ctx)(getattr(module, request_name)(hash=0))
    items: list[EmojiGroup] = []
    for group in getattr(result, "groups", None) or []:
        items.append(
            EmojiGroup(
                kind=req.kind,
                title=str(getattr(group, "title", "") or ""),
                icon_emoji_id=getattr(group, "icon_emoji_id", None),
                emoticons=[str(e) for e in getattr(group, "emoticons", None) or []],
            )
        )
    document_ids = [int(i) for i in getattr(result, "document_id", None) or []]
    if document_ids:
        items.append(EmojiGroup(kind=req.kind, title=req.kind, document_ids=document_ids))
    if req.resolve and document_ids:
        resolved = await get(ctx, GetReq(emoji_id=document_ids))
        items[-1].emoticons = [item.emoji or "" for item in resolved.items]
    return Page(items=items, has_more=False, total=len(items))


SPEC_LIST = OperationSpec(
    id="emoji.list",
    request=ListReq,
    response=Page[EmojiGroup],
    impl=list_groups,
    summary="Server-provided emoji groups and default lists used by the pickers",
    columns=("kind", "title", "icon_emoji_id"),
    headers=("Kind", "Title", "Icon"),
    example={
        "items": [
            {
                "kind": "groups",
                "title": "Smileys",
                "icon_emoji_id": 5312836234,
                "emoticons": ["😀", "😃"],
            }
        ],
        "has_more": False,
    },
    example_args="emoji list --kind groups",
    covers=("emoji.categories", "emoji.default-lists"),
)


# ---------------------------------------------------------------------------
# emoji search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", help="A keyword, or an emoji with --custom.")]
    lang: Annotated[
        list[str], opt("--lang", metavar="CODE", help="Keyword languages to search.")
    ] = []
    custom: Annotated[
        bool, opt("--custom", help="Custom emoji suggestions for the query emoji.")
    ] = False
    sync: Annotated[
        bool, opt("--sync", help="Refresh the cached keyword database before searching.")
    ] = False
    suggest_url: Annotated[
        bool, opt("--suggest-url", help="Print the login URL for suggesting new keywords.")
    ] = False


async def search(ctx: OpContext, req: SearchReq) -> Page[EmojiKeyword]:
    """Find emoji by keyword, or custom emoji matching an emoji.

    Keyword lists are a versioned local database, not a query endpoint: they
    are fetched once per language and kept current with
    `getEmojiKeywordsDifference`. `--custom` is the other half of the
    composer's suggestion popup and *is* a server call.
    """
    from telethon.tl.functions import messages as fn

    limit, _state = _media.window(ctx, "emoji.search", PageKind.LOCAL, 50)
    languages = list(req.lang)
    if not languages:
        result = await _client(ctx)(fn.GetEmojiKeywordsLanguagesRequest(lang_codes=["en"]))
        languages = [str(getattr(entry, "lang_code", "en")) for entry in result or []] or ["en"]

    if req.suggest_url:
        url = await _client(ctx)(fn.GetEmojiURLRequest(lang_code=languages[0]))
        return Page(
            items=[
                EmojiKeyword(
                    emoticon="", keyword=str(getattr(url, "url", "") or ""), lang=languages[0]
                )
            ],
            has_more=False,
        )

    if req.custom:
        result = await _client(ctx)(fn.SearchCustomEmojiRequest(emoticon=req.query, hash=0))
        ids = [int(i) for i in getattr(result, "document_id", None) or []][:limit]
        documents = await get(ctx, GetReq(emoji_id=ids)) if ids else None
        items = [
            EmojiKeyword(
                emoticon=item.emoji or req.query,
                keyword=req.query,
                lang=languages[0],
                doc_id=item.doc_id,
                set_short_name=item.set_short_name,
            )
            for item in (documents.items if documents is not None else [])
        ]
        return Page(items=items, has_more=False, total=len(items))

    items = []
    needle = req.query.strip().lower()
    for language in languages:
        # A difference call when asked to sync, the full list otherwise: both
        # return the same keyword rows, and the search happens here.
        request: Any = (
            fn.GetEmojiKeywordsDifferenceRequest(lang_code=language, from_version=0)
            if req.sync
            else fn.GetEmojiKeywordsRequest(lang_code=language)
        )
        result = await _client(ctx)(request)
        for entry in getattr(result, "keywords", None) or []:
            keyword = str(getattr(entry, "keyword", "") or "")
            if needle and needle not in keyword.lower():
                continue
            for emoticon in getattr(entry, "emoticons", None) or []:
                items.append(EmojiKeyword(emoticon=str(emoticon), keyword=keyword, lang=language))
    return Page(items=items[:limit], has_more=len(items) > limit, total=len(items))


SPEC_SEARCH = OperationSpec(
    id="emoji.search",
    request=SearchReq,
    response=Page[EmojiKeyword],
    impl=search,
    summary="Find emoji by keyword, or custom emoji matching an emoji",
    paginated=PageKind.LOCAL,
    columns=("emoticon", "keyword", "lang"),
    headers=("Emoji", "Keyword", "Lang"),
    example={
        "items": [{"emoticon": "😀", "keyword": "grinning", "lang": "en"}],
        "has_more": False,
    },
    example_args="emoji search grinning",
    covers=("emoji.custom-search-by-emoticon", "emoji.keywords-search"),
)
