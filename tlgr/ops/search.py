"""The `search` group: finding messages outside one chat.

Global search does not paginate on a message id. `messages.searchGlobal` walks
a **triple** — `(offset_rate, offset_peer, offset_id)` — where `offset_rate`
comes back as `messagesSlice.next_rate` and is meaningless anywhere else.
v1-style "remember the last id" paging silently restarts the walk at the top
of the first chat, so the whole triple goes into the cursor and comes back out
of it.

`search post` spends Stars once the free quota is gone. It never does so
implicitly: `--quota` reports the price for free, and paying takes `--pay-stars`
plus the ordinary destructive confirmation.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from tlgr.core.errors import NotSupportedError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import parse_dt
from tlgr.models.base import Request
from tlgr.models.message import Message
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import message_to_model
from tlgr.ops._spec import OpContext, OperationSpec

#: `--type` names the same `inputMessagesFilter*` values `message search`
#: takes: one media vocabulary across the surface, not two that drift.
from tlgr.ops.message import FILTERS

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_HIT: dict[str, Any] = {
    "id": 12345,
    "chat_id": -1001234567890,
    "date": "2026-09-03T09:14:07Z",
    "date_unix": 1788340447,
    "text": "the release is out",
}

#: Where the local hashtag history lives, per account.
_HASHTAG_FILE = "hashtags.json"
_HASHTAG_MAX = 50


def _filter(name: str | None, *, missed: bool = False) -> Any:
    """`--type photo` → `InputMessagesFilterPhotos()`; empty means everything."""
    from telethon.tl import types

    if not name:
        return types.InputMessagesFilterEmpty()
    class_name = FILTERS.get(name)
    if class_name is None:
        raise UsageError(
            f"--type {name!r} is not a media filter; expected one of {', '.join(FILTERS)}",
            field="type",
        )
    if class_name == "InputMessagesFilterPhoneCalls":
        return types.InputMessagesFilterPhoneCalls(missed=missed or None)
    return getattr(types, class_name)()


async def _empty_peer(ctx: OpContext, state: dict[str, Any]) -> Any:
    """The `offset_peer` half of the search triple, resolved from the cursor."""
    from telethon.tl import types

    marked = state.get("offset_peer")
    if not marked:
        return types.InputPeerEmpty()
    try:
        return await client(ctx).get_input_entity(int(marked))
    except (ValueError, TypeError):
        return types.InputPeerEmpty()


def _hits(result: Any, ctx: OpContext) -> list[Message]:
    """`messages.Messages*` → models, with the chat each hit came from filled in."""
    from tlgr.ops._serialize import entity_to_peer

    chats = {
        int(getattr(entity, "id", 0)): entity
        for entity in (
            *(getattr(result, "chats", None) or []),
            *(getattr(result, "users", None) or []),
        )
    }
    out: list[Message] = []
    for raw in getattr(result, "messages", None) or []:
        model = message_to_model(raw)
        peer = getattr(raw, "peer_id", None)
        for attribute in ("channel_id", "chat_id", "user_id"):
            value = getattr(peer, attribute, None)
            entity = chats.get(int(value)) if value is not None else None
            if entity is not None:
                model.chat = entity_to_peer(entity)
                break
        out.append(model)
    return out


def _next_state(result: Any, items: list[Message]) -> dict[str, Any]:
    """The triple to resume from: rate, peer and id of the last hit."""
    last = items[-1] if items else None
    return {
        "offset_rate": int(getattr(result, "next_rate", 0) or 0),
        "offset_peer": last.chat_id if last is not None else 0,
        "offset_id": last.id if last is not None else 0,
    }


# ---------------------------------------------------------------------------
# search global
# ---------------------------------------------------------------------------


class GlobalReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", required=False, help="What to look for.")] = ""
    type: Annotated[
        str | None, opt("--type", metavar="FILTER", help="Media filter, as `message search`.")
    ] = None
    only: Annotated[
        str | None, choice("user", "group", "channel", help="Scope to one kind of chat.")
    ] = None
    community: Annotated[
        PeerRef | None,
        opt("--community", metavar="CHAT", kind="peer", help="Scope to one community (layer 229)."),
    ] = None
    folder: Annotated[int | None, opt("--folder", metavar="ID", help="Scope to a chat folder.")] = (
        None
    )
    archived: Annotated[bool, opt("--archived", help="Search the archive folder.")] = False
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Only after this time.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Only before this time.")
    ] = None
    missed: Annotated[bool, opt("--missed", help="With --type call: only missed calls.")] = False
    sent_media: Annotated[
        bool, opt("--sent-media", help="Search my own recently sent documents instead.")
    ] = False


async def search_global(ctx: OpContext, req: GlobalReq) -> Page[Message]:
    """Search messages across every chat this account is in.

    Global search never reaches chats you have left or secret chats; that is
    the server's rule, not tlgr's, and it is why an empty answer here is not
    proof a message does not exist.
    """
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "search.global", PageKind.RATE)
    if req.community is not None:
        raise NotSupportedError(
            "--community is a layer-229 flag on messages.searchGlobal and the pinned "
            "Telethon speaks 227; search inside the community's chats instead"
        )

    if req.sent_media:
        # A different method with no pagination at all: it answers with the
        # most recent N and nothing else, so the page is honest about that.
        result = await client(ctx)(
            fn.SearchSentMediaRequest(q=req.query, filter=_filter(req.type or "file"), limit=limit)
        )
        items = _hits(result, ctx)
        return build_page(items, op="search.global", kind=PageKind.RATE, has_more=False)

    folder = req.folder
    if req.archived and folder is None:
        folder = 1

    result = await client(ctx)(
        fn.SearchGlobalRequest(
            q=req.query,
            filter=_filter(req.type, missed=req.missed),
            min_date=parse_dt(req.since),
            max_date=parse_dt(req.until),
            offset_rate=int(state.get("offset_rate") or 0),
            offset_peer=await _empty_peer(ctx, state),
            offset_id=int(state.get("offset_id") or 0),
            limit=limit,
            broadcasts_only=(req.only == "channel") or None,
            groups_only=(req.only == "group") or None,
            users_only=(req.only == "user") or None,
            folder_id=folder,
        )
    )
    items = _hits(result, ctx)
    return build_page(
        items,
        op="search.global",
        kind=PageKind.RATE,
        state=_next_state(result, items),
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_GLOBAL = OperationSpec(
    id="search.global",
    request=GlobalReq,
    response=Page[Message],
    impl=search_global,
    summary="Search messages across every chat",
    description=(
        "Pagination is the `(offset_rate, offset_peer, offset_id)` triple, "
        "carried whole in `--cursor`; a message id alone would restart the "
        "walk at the top. Global search never covers chats you have left or "
        "secret chats, so an empty answer is not proof of absence."
    ),
    aliases=("search.message",),
    paginated=PageKind.RATE,
    columns=("id", "chat_id", "date", "text"),
    example={"items": [_EXAMPLE_HIT], "has_more": False},
    example_args="search global 'release notes'",
    covers=(
        "messages-core.search-calls-log",
        "messages-core.search-global",
        "messages-core.search-global-media-tabs",
        "messages-core.search-global-scope",
        "messages-core.search-sent-media",
    ),
    coverage_note=(
        "`--community` (layer 229) is refused with NOT_SUPPORTED; every other "
        "scope — users / groups / broadcasts / folder — works on layer 227."
    ),
)


# ---------------------------------------------------------------------------
# search hashtag
# ---------------------------------------------------------------------------


def _hashtag_path(ctx: OpContext) -> Any:
    from pathlib import Path

    paths = getattr(ctx, "paths", None)
    if paths is None:
        return None
    return Path(paths.ensure_account_dir(ctx.account)) / _HASHTAG_FILE


def _recent_hashtags(ctx: OpContext) -> list[str]:
    """The local hashtag history.

    There is no `messages.getRecentHashtags` on this layer — the official
    clients keep the list themselves — so tlgr keeps it in the account's own
    directory rather than pretending the server remembers.
    """
    path = _hashtag_path(ctx)
    if path is None or not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _remember_hashtag(ctx: OpContext, tag: str) -> None:
    from tlgr.core.paths import write_private

    path = _hashtag_path(ctx)
    if path is None or not tag:
        return
    history = [item for item in _recent_hashtags(ctx) if item != tag]
    history.insert(0, tag)
    write_private(path, json.dumps(history[:_HASHTAG_MAX]))


def _forget_hashtag(ctx: OpContext, tag: str) -> None:
    from tlgr.core.paths import write_private

    path = _hashtag_path(ctx)
    if path is None:
        return
    history = [] if tag == "*" else [item for item in _recent_hashtags(ctx) if item != tag]
    write_private(path, json.dumps(history))


class HashtagReq(Request):
    tag: Annotated[str, arg(0, metavar="TAG", required=False, help="The hashtag or cashtag.")] = ""
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="Search this chat only.")
    ] = None
    public: Annotated[
        bool, opt("--public", help="Global public-post search (channels.searchPosts).")
    ] = False
    stories: Annotated[bool, opt("--stories", help="Search public stories instead.")] = False
    recent: Annotated[
        bool, opt("--recent", help="List recently searched hashtags instead of searching.")
    ] = False
    forget: Annotated[
        str | None, opt("--forget", metavar="TAG|*", help="Remove a recent hashtag, or all.")
    ] = None


async def search_hashtag(ctx: OpContext, req: HashtagReq) -> Page[Message]:
    """Search a hashtag in a chat, or across public channel posts.

    In-chat hashtag search is `messages.search` with `q="#tag"`; the public
    form is `channels.searchPosts`, which pages on the same rate triple as
    global search. The recently-searched list is local state — this layer has
    no `getRecentHashtags` — kept in the account's own directory.
    """
    from telethon.tl import types
    from telethon.tl.functions import channels as ch
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "search.hashtag", PageKind.RATE)

    if req.forget is not None:
        _forget_hashtag(ctx, req.forget)
    if req.recent:
        # A local list of strings, reported as message stubs would be a lie;
        # the tags come back as warnings-free `text` rows.
        rows = [
            Message(id=index, chat_id=0, date="", date_unix=0, text=tag)
            for index, tag in enumerate(_recent_hashtags(ctx), start=1)
        ]
        return build_page(rows, op="search.hashtag", kind=PageKind.RATE, has_more=False)

    if req.stories:
        raise NotSupportedError(
            "hashtag search over public stories answers with stories, not messages; "
            "it belongs to the `story` group and lands with it"
        )
    tag = req.tag.strip()
    if not tag:
        raise UsageError("a hashtag is required", field="tag")
    if not tag.startswith(("#", "$")):
        tag = f"#{tag}"
    _remember_hashtag(ctx, tag)

    if req.chat is not None:
        peer = await _send.resolve(ctx, req.chat)
        chat_id = _send.peer_id_of(peer)
        result = await client(ctx)(
            fn.SearchRequest(
                peer=peer,
                q=tag,
                filter=types.InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_id=int(state.get("offset_id") or 0),
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        items = [
            message_to_model(raw, chat_id=chat_id)
            for raw in (getattr(result, "messages", None) or [])
        ]
        return build_page(
            items,
            op="search.hashtag",
            kind=PageKind.RATE,
            state={"offset_id": items[-1].id if items else 0},
            account=ctx.account,
            limit=limit,
            total=getattr(result, "count", None),
        )

    result = await client(ctx)(
        ch.SearchPostsRequest(
            offset_rate=int(state.get("offset_rate") or 0),
            offset_peer=await _empty_peer(ctx, state),
            offset_id=int(state.get("offset_id") or 0),
            limit=limit,
            hashtag=tag.lstrip("#$"),
        )
    )
    items = _hits(result, ctx)
    return build_page(
        items,
        op="search.hashtag",
        kind=PageKind.RATE,
        state=_next_state(result, items),
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_HASHTAG = OperationSpec(
    id="search.hashtag",
    request=HashtagReq,
    response=Page[Message],
    impl=search_hashtag,
    summary="Search a hashtag or cashtag, in a chat or across public posts",
    description=(
        "Without `--chat` this is the global public-post search. The "
        "recently-searched list is tlgr's own state: this layer has no "
        "`getRecentHashtags`, and the official clients keep it locally too."
    ),
    paginated=PageKind.RATE,
    tags=frozenset({"mutating-checked"}),
    columns=("id", "chat_id", "date", "text"),
    example={"items": [_EXAMPLE_HIT], "has_more": False},
    example_args="search hashtag telegram",
    covers=(
        "contacts-users.search-hashtag-history",
        "messages-core.search-hashtag-in-chat",
        "messages-core.search-hashtag-public-posts",
        "messages-core.search-recent-hashtags",
    ),
    coverage_note=(
        "The two recent-hashtag ids have no MTProto method; they are tlgr's "
        "own per-account state, which is what the official clients do too. "
        "Story hashtag search is refused here and belongs to the story group."
    ),
)


# ---------------------------------------------------------------------------
# search post
# ---------------------------------------------------------------------------


class PostReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", help="What to look for.")]
    quota: Annotated[
        bool, opt("--quota", help="Report the free searches left and the Star price.")
    ] = False
    pay_stars: Annotated[
        int | None,
        opt("--pay-stars", metavar="N", help="Agree to spend N Stars on this search."),
    ] = None


async def search_post(ctx: OpContext, req: PostReq) -> Page[Message]:
    """Full-text search across all public channel posts.

    Free while the quota lasts, then priced in Stars. Price discovery is free
    and happens first; spending is never implicit, which is why `--pay-stars`
    exists and why the command confirms like any other destructive one.
    """
    from telethon.tl.functions import channels as ch

    limit, state = window(ctx, "search.post", PageKind.RATE)
    flood = await client(ctx)(ch.CheckSearchPostsFloodRequest(query=req.query))
    remaining = int(getattr(flood, "remains", 0) or 0)
    price = int(getattr(flood, "stars_amount", 0) or 0)
    free = bool(getattr(flood, "query_is_free", False)) or remaining > 0

    if req.quota:
        page: Page[Message] = Page(items=[], has_more=False)
        ctx.warn(f"{remaining} free searches left; beyond that this query costs {price} Stars")
        return page

    if not free and req.pay_stars is None:
        raise UsageError(
            f"the free quota is exhausted and this search costs {price} Stars; "
            f"pass --pay-stars {price} to agree to spend them",
            field="pay-stars",
        )
    if req.pay_stars is not None and price and req.pay_stars < price:
        raise UsageError(
            f"--pay-stars {req.pay_stars} is below the {price} Stars this search costs",
            field="pay-stars",
        )

    result = await client(ctx)(
        ch.SearchPostsRequest(
            offset_rate=int(state.get("offset_rate") or 0),
            offset_peer=await _empty_peer(ctx, state),
            offset_id=int(state.get("offset_id") or 0),
            limit=limit,
            query=req.query,
            allow_paid_stars=req.pay_stars if not free else None,
        )
    )
    items = _hits(result, ctx)
    return build_page(
        items,
        op="search.post",
        kind=PageKind.RATE,
        state=_next_state(result, items),
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_POST = OperationSpec(
    id="search.post",
    request=PostReq,
    response=Page[Message],
    impl=search_post,
    summary="Full-text search across all public channel posts",
    description=(
        "Free while the quota lasts. `--quota` reports what is left and what "
        "the query would cost, without spending anything; paying needs an "
        "explicit `--pay-stars N`, because spending Stars is a human decision."
    ),
    paginated=PageKind.RATE,
    mutating=True,
    destructive=True,
    rate_class="resolve",
    columns=("id", "chat_id", "date", "text"),
    example={"items": [_EXAMPLE_HIT], "has_more": False},
    example_args="search post 'release notes'",
    covers=(
        "contacts-users.search-public-posts",
        "messages-core.search-public-posts-fulltext",
    ),
)
