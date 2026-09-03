"""The `sticker` group — and, through its aliases, the `emoji set` one.

One distinction runs through the whole module and it is the API's, not a
naming preference:

* a **set** is somebody's collection you install, archive, reorder or
  uninstall. Every verb here is in the `messages.*` namespace and works on
  any set in the world.
* a **pack** is a set *you created*. Every verb is in the `stickers.*`
  namespace and the server answers `STICKERSET_INVALID` to anyone else.

Uninstalling a set is therefore not deleting a pack, and `sticker set remove`
and `sticker pack delete` are deliberately different commands with different
blast radii.

Custom emoji sets take exactly the same calls — only the `emojis` flag on the
archive/reorder requests differs — which is why `emoji set list` is an alias
of `sticker set list --type emoji` rather than a second implementation.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import gzip
import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, NotSupportedError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.media import MediaFile
from tlgr.models.page import Page
from tlgr.models.sticker import (
    FaveResult,
    PackCreated,
    PackDeleted,
    PackEdited,
    PackStickerAdded,
    PackStickerRemoved,
    RecentResult,
    Sticker,
    StickerSet,
    StickerSetOrder,
    StickerSetsChanged,
)
from tlgr.ops import _media, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The reserved emoticons behind the server's two special sticker lists: the
#: greeting sticker an empty chat offers, and the Premium promo strip.
SPECIAL_EMOTICONS = {"greeting": "👋⭐️", "premium": "📂⭐️"}

_EXAMPLE_SET: dict[str, Any] = {
    "id": 1258816259751983,
    "short_name": "AnimatedEmojies",
    "title": "Animated Emoji",
    "count": 120,
    "type": "sticker",
    "installed": True,
    "link": "https://t.me/addstickers/AnimatedEmojies",
}

_EXAMPLE_STICKER: dict[str, Any] = {
    "doc_id": 5312836234,
    "emoji": "😀",
    "index": 0,
    "set_short_name": "AnimatedEmojies",
    "mime": "application/x-tgsticker",
}


def _client(ctx: OpContext) -> Any:
    return _media.client(ctx)


def _refs(values: list[str], field: str = "set") -> list[Any]:
    if not values:
        raise UsageError("name at least one sticker set", field=field)
    return [_media.sticker_set_ref(value, field=field) for value in values]


def _names(values: list[str]) -> list[str]:
    """The short names, as the caller spelled them, for the response."""
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        for marker in ("addstickers/", "addemoji/"):
            if marker in text.lower():
                text = text[text.lower().index(marker) + len(marker) :].split("?")[0]
        out.append(text)
    return out


def _archived_names(result: Any) -> list[str]:
    """`stickerSetInstallResultArchive` names the sets it pushed aside.

    Reporting them is the whole point: an install that silently archived four
    other sets to stay under the limit looks like a bug from the outside.
    """
    sets = getattr(result, "sets", None) or []
    out: list[str] = []
    for covered in sets:
        inner = getattr(covered, "set", covered)
        name = getattr(inner, "short_name", None)
        if name:
            out.append(str(name))
    return out


# ---------------------------------------------------------------------------
# sticker set get
# ---------------------------------------------------------------------------


class SetGetReq(Request):
    set: Annotated[
        str | None,
        arg(0, metavar="SET", required=False, help="Short name, t.me link, or <id>:<hash>."),
    ] = None
    from_message: Annotated[
        str | None,
        opt("--from-message", metavar="CHAT:ID", help="Take the set from a message's sticker."),
    ] = None
    system: Annotated[
        str | None,
        opt(
            "--system",
            metavar="NAME",
            help="A system set: dice:EMOJI, animated-emoji, topic-icons, default-statuses…",
        ),
    ] = None
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Download every sticker into here."),
    ] = None
    thumb: Annotated[bool, opt("--thumb", help="Also download the set thumbnail.")] = False
    effects: Annotated[bool, opt("--effects", help="Also download premium effect videos.")] = False
    convert: Annotated[
        str, choice("png", "json", "none", help="Post-process downloads: TGS to Lottie JSON.")
    ] = "none"
    link: Annotated[bool, opt("--link", help="Print only the shareable set link.")] = False


def _set_detail(result: Any) -> StickerSet:
    """A `messages.stickerSet` with its documents, emoji map and keywords."""
    model = _media.sticker_set_model(getattr(result, "set", result))
    documents = list(getattr(result, "documents", None) or [])
    keywords: dict[str, list[str]] = {}
    for entry in getattr(result, "keywords", None) or []:
        keywords[str(getattr(entry, "document_id", ""))] = list(
            getattr(entry, "keywords", None) or []
        )
    model.stickers = [
        _media.sticker_model(
            document,
            index=index,
            set_short_name=model.short_name,
            set_title=model.title,
            keywords=keywords.get(str(getattr(document, "id", "")), []),
        )
        for index, document in enumerate(documents)
    ]
    model.packs = {
        str(getattr(pack, "emoticon", "")): [int(i) for i in getattr(pack, "documents", None) or []]
        for pack in getattr(result, "packs", None) or []
    }
    model.keywords = keywords
    return model


async def set_get(ctx: OpContext, req: SetGetReq) -> StickerSet:
    """One sticker set with all its stickers, the emoji map and the keywords.

    `--system` reaches the sets a client holds without installing: `dice:🎲`
    is how a received `messageMediaDice` value renders (`documents[value]`),
    and the animated-emoji sets are the assets behind an emoji-only message.
    """
    reference = await _set_reference(ctx, req)
    result = await _media.fetch_set(ctx, reference)
    model = _set_detail(result)
    if req.download:
        await _download_set(ctx, req, result, model)
    return model


async def _set_reference(ctx: OpContext, req: SetGetReq) -> Any:
    if req.from_message:
        peer_text, _, message_id = str(req.from_message).rpartition(":")
        if not peer_text or not message_id.lstrip("-").isdigit():
            raise UsageError("--from-message takes CHAT:ID", field="from_message")
        from tlgr.models.peer import parse_peer_ref

        peer = await _send.resolve(ctx, parse_peer_ref(peer_text))
        message = await _media.fetch_message(ctx, peer, int(message_id))
        document = _media.document_of(getattr(message, "media", None))
        stickerset = _media.attributes_of(document).get("stickerset")
        if stickerset is None:
            raise NotFoundError(f"message {message_id} carries no sticker")
        return stickerset
    if req.system:
        return _media.sticker_set_ref(req.system, field="system")
    if req.set:
        return _media.sticker_set_ref(req.set, field="set")
    raise UsageError("name a set, or use --system/--from-message", field="set")


async def _download_set(ctx: OpContext, req: SetGetReq, result: Any, model: StickerSet) -> None:
    """Fetch every sticker, and convert the ones that can be converted here."""
    directory = Path(os.path.expanduser(req.download or "."))
    directory.mkdir(parents=True, exist_ok=True)
    download = getattr(ctx, "download_file", None)
    if download is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot download files")

    for index, document in enumerate(getattr(result, "documents", None) or []):
        suffix = {
            "image/webp": ".webp",
            "application/x-tgsticker": ".tgs",
            "video/webm": ".webm",
        }.get(getattr(document, "mime_type", "") or "", ".bin")
        target = directory / f"{model.short_name or model.id}_{index}{suffix}"
        written = await download(
            document,
            target,
            size=int(getattr(document, "size", 0) or 0),
            dc_id=int(getattr(document, "dc_id", 0) or 0),
        )
        path = Path(written)
        if req.convert != "none":
            path = _convert(path, req.convert)
        model.stickers[index].path = str(path)


def _convert(path: Path, into: str) -> Path:
    """TGS is gzipped Lottie, so `--convert json` is a gunzip and nothing more.

    `--convert png` is not: rasterising WebP needs Pillow, and pretending to
    do it without one would write a file that is not a PNG.
    """
    if into == "json":
        if path.suffix != ".tgs":
            return path
        target = path.with_suffix(".json")
        target.write_bytes(gzip.decompress(path.read_bytes()))
        return target
    try:
        from PIL import Image
    except ImportError as exc:
        raise NotSupportedError(
            "--convert png needs Pillow (install the [media] extra); the stickers were "
            "downloaded in their original format"
        ) from exc
    target = path.with_suffix(".png")
    with Image.open(path) as image:
        image.save(target, "PNG")
    return target


SPEC_SET_GET = OperationSpec(
    id="sticker.set.get",
    request=SetGetReq,
    response=StickerSet,
    impl=set_get,
    summary="One sticker set with all its stickers, emoji map and keywords",
    description=(
        "Each sticker row carries the document id, its emoji, its keywords and "
        "its index, which is what `sticker fave add` and `message send "
        "--sticker <set>/<index>` consume. The share link is string "
        "formatting, not a request: t.me/addstickers/<name>, or "
        "t.me/addemoji/<name> for an emoji set."
    ),
    aliases=("emoji.set.get",),
    columns=("short_name", "title", "count", "type"),
    headers=("Short name", "Title", "Count", "Type"),
    empty_exit=EXIT_EMPTY,
    example={**_EXAMPLE_SET, "stickers": [_EXAMPLE_STICKER]},
    example_args="sticker set get AnimatedEmojies",
    covers=(
        "media.animated-emoji",
        "sticker.dice-and-system-sets",
        "sticker.download-assets",
        "sticker.set-view",
        "sticker.share-link",
    ),
    covers_partial=("emoji.custom-sets",),
    coverage_note="Custom-emoji sets take the same calls; `emoji set get` is this command.",
)


# ---------------------------------------------------------------------------
# sticker set list
# ---------------------------------------------------------------------------


class SetListReq(Request):
    type: Annotated[str, choice("sticker", "mask", "emoji", help="Which library.")] = "sticker"
    archived: Annotated[bool, opt("--archived", help="The archived shelf.")] = False
    featured: Annotated[bool, opt("--featured", help="The trending shelf.")] = False
    old_featured: Annotated[
        bool, opt("--old-featured", help="The long tail behind the trending shelf.")
    ] = False
    mark_read: Annotated[
        bool, opt("--mark-read", help="With --featured: clear the unread badge.")
    ] = False
    unread_only: Annotated[
        bool, opt("--unread-only", help="With --featured: only sets still badged.")
    ] = False


async def set_list(ctx: OpContext, req: SetListReq) -> Page[StickerSet]:
    """Installed, archived or featured sets.

    The installed and featured lists are hash-cached rather than offset
    paginated — the server answers `*NotModified` when nothing changed — so
    `--limit`/`--cursor` only mean something for the two that genuinely walk:
    `--archived` (offset_id) and `--old-featured` (offset).
    """
    from telethon.tl.functions import messages as fn

    limit, state = _media.window(ctx, "sticker.set.list", PageKind.LOCAL)
    if req.archived:
        result = await _client(ctx)(
            fn.GetArchivedStickersRequest(
                offset_id=int(state.get("offset_id", 0)),
                limit=limit,
                masks=req.type == "mask" or None,
                emojis=req.type == "emoji" or None,
            )
        )
        items = [_media.covered_set(covered) for covered in getattr(result, "sets", None) or []]
        for item in items:
            item.archived = True
        next_state = {"offset_id": items[-1].id} if items else {}
        return build_page(
            items,
            op="sticker.set.list",
            kind=PageKind.LOCAL,
            state=next_state,
            account=ctx.account,
            limit=limit,
        )

    if req.old_featured:
        result = await _client(ctx)(
            fn.GetOldFeaturedStickersRequest(
                offset=int(state.get("offset", 0)), limit=limit, hash=0
            )
        )
        items = [_media.covered_set(covered) for covered in getattr(result, "sets", None) or []]
        next_state = {"offset": int(state.get("offset", 0)) + len(items)}
        return build_page(
            items,
            op="sticker.set.list",
            kind=PageKind.LOCAL,
            state=next_state,
            account=ctx.account,
            limit=limit,
        )

    if req.featured:
        request: Any = (
            fn.GetFeaturedEmojiStickersRequest(hash=0)
            if req.type == "emoji"
            else fn.GetFeaturedStickersRequest(hash=0)
        )
        result = await _client(ctx)(request)
        unread = {int(i) for i in getattr(result, "unread", None) or []}
        items = [_media.covered_set(covered) for covered in getattr(result, "sets", None) or []]
        for item in items:
            item.unread = item.id in unread
        if req.unread_only:
            items = [item for item in items if item.unread]
        if req.mark_read and unread:
            await _client(ctx)(fn.ReadFeaturedStickersRequest(id=sorted(unread)))
        return Page(items=items[:limit], has_more=False, total=len(items))

    installed: Any = {
        "sticker": fn.GetAllStickersRequest(hash=0),
        "mask": fn.GetMaskStickersRequest(hash=0),
        "emoji": fn.GetEmojiStickersRequest(hash=0),
    }[req.type]
    result = await _client(ctx)(installed)
    items = [_media.sticker_set_model(entry) for entry in getattr(result, "sets", None) or []]
    return Page(items=items[:limit], has_more=False, total=len(items))


SPEC_SET_LIST = OperationSpec(
    id="sticker.set.list",
    request=SetListReq,
    response=Page[StickerSet],
    impl=set_list,
    summary="Installed, archived or featured sticker, mask or emoji sets",
    aliases=("emoji.set.list",),
    paginated=PageKind.LOCAL,
    columns=("short_name", "title", "count", "type"),
    headers=("Short name", "Title", "Count", "Type"),
    example={"items": [_EXAMPLE_SET], "has_more": False},
    example_args="sticker set list --type emoji",
    covers=("sticker.set-featured", "sticker.set-list-installed"),
    covers_partial=("emoji.custom-sets", "sticker.set-archive"),
    coverage_note=(
        "Archiving is `sticker set archive`; this lists the shelf. `--mark-read` is the "
        "only mutation the listing performs and it is opt-in."
    ),
    tags=frozenset({"mutating-checked"}),
)


# ---------------------------------------------------------------------------
# sticker set install / uninstall / archive
# ---------------------------------------------------------------------------


class SetAddReq(Request):
    set: Annotated[
        list[str], arg(0, metavar="SET", variadic=True, help="Short names, links or <id>:<hash>.")
    ] = []
    archived: Annotated[
        bool, opt("--archived", help="Install straight into the archived shelf.")
    ] = False


async def set_add(ctx: OpContext, req: SetAddReq) -> StickerSetsChanged:
    """Install sets.

    The reply can be `stickerSetInstallResultArchive`, meaning the install
    pushed *other* sets into the archive to stay under the installed-sets
    limit. Those names are reported rather than swallowed.
    """
    from telethon.tl.functions import messages as fn

    names = _names(req.set)
    changed = StickerSetsChanged(short_names=names)
    for reference in _refs(req.set):
        result = await _client(ctx)(
            fn.InstallStickerSetRequest(stickerset=reference, archived=req.archived)
        )
        changed.installed += 1
        changed.archived_sets.extend(_archived_names(result))
    if changed.archived_sets:
        ctx.warn(
            "the installed-sets limit was reached; these were archived to make room: "
            + ", ".join(changed.archived_sets)
        )
    return changed


SPEC_SET_ADD = OperationSpec(
    id="sticker.set.add",
    request=SetAddReq,
    response=StickerSetsChanged,
    impl=set_add,
    summary="Install a sticker, mask or emoji set",
    aliases=("emoji.set.add",),
    mutating=True,
    idempotent=True,
    columns=("short_names", "installed", "archived_sets"),
    headers=("Sets", "Installed", "Archived to fit"),
    example={"short_names": ["AnimatedEmojies"], "installed": 1, "archived_sets": []},
    example_args="sticker set add AnimatedEmojies",
    covers_partial=("emoji.custom-sets", "sticker.set-install-uninstall"),
    coverage_note="Uninstalling is `sticker set remove`; deleting an owned pack is `sticker pack delete`.",
)


class SetRemoveReq(Request):
    set: Annotated[list[str], arg(0, metavar="SET", variadic=True, help="Sets to uninstall.")] = []


async def set_remove(ctx: OpContext, req: SetRemoveReq) -> StickerSetsChanged:
    """Uninstall sets.

    Uninstalling is not deleting: a pack you own still exists afterwards
    (that is `sticker pack delete`), and any set can be reinstalled from its
    link.
    """
    from telethon.tl.functions import messages as fn

    names = _names(req.set)
    for reference in _refs(req.set):
        await _client(ctx)(fn.UninstallStickerSetRequest(stickerset=reference))
    return StickerSetsChanged(short_names=names, removed=len(names))


SPEC_SET_REMOVE = OperationSpec(
    id="sticker.set.remove",
    request=SetRemoveReq,
    response=StickerSetsChanged,
    impl=set_remove,
    summary="Uninstall a sticker, mask or emoji set",
    aliases=("emoji.set.remove",),
    mutating=True,
    destructive=True,
    columns=("short_names", "removed"),
    headers=("Sets", "Removed"),
    example={"short_names": ["AnimatedEmojies"], "removed": 1},
    example_args="sticker set remove AnimatedEmojies",
    covers=("sticker.set-install-uninstall",),
    covers_partial=("emoji.custom-sets",),
    coverage_note="Emoji sets uninstall through the same call; `emoji set remove` is this command.",
)


class SetArchiveReq(Request):
    set: Annotated[list[str], arg(0, metavar="SET", variadic=True, help="Sets to archive.")] = []


async def set_archive(ctx: OpContext, req: SetArchiveReq) -> StickerSetsChanged:
    """Move installed sets to the archived shelf.

    Archiving keeps a set usable — it stays in `sticker set list --archived`
    and its stickers still send — it only leaves the panel.
    """
    from telethon.tl.functions import messages as fn

    names = _names(req.set)
    await _client(ctx)(fn.ToggleStickerSetsRequest(stickersets=_refs(req.set), archive=True))
    return StickerSetsChanged(short_names=names, archived=len(names))


SPEC_SET_ARCHIVE = OperationSpec(
    id="sticker.set.archive",
    request=SetArchiveReq,
    response=StickerSetsChanged,
    impl=set_archive,
    summary="Move installed sets to the archived shelf",
    aliases=("emoji.set.archive",),
    mutating=True,
    idempotent=True,
    columns=("short_names", "archived"),
    headers=("Sets", "Archived"),
    example={"short_names": ["AnimatedEmojies"], "archived": 1},
    example_args="sticker set archive AnimatedEmojies",
    covers_partial=("sticker.set-archive",),
    coverage_note="Restoring is `sticker set unarchive`; the shelf is `sticker set list --archived`.",
)


class SetUnarchiveReq(Request):
    set: Annotated[list[str], arg(0, metavar="SET", variadic=True, help="Sets to restore.")] = []


async def set_unarchive(ctx: OpContext, req: SetUnarchiveReq) -> StickerSetsChanged:
    """Restore archived sets to the panel.

    This can itself push other sets out when the installed limit is reached,
    so anything the server archived as a side effect is reported.
    """
    from telethon.tl.functions import messages as fn

    names = _names(req.set)
    result = await _client(ctx)(
        fn.ToggleStickerSetsRequest(stickersets=_refs(req.set), unarchive=True)
    )
    changed = StickerSetsChanged(short_names=names, unarchived=len(names))
    changed.archived_sets = _archived_names(result)
    if changed.archived_sets:
        ctx.warn(
            "restoring these reached the installed-sets limit; archived instead: "
            + ", ".join(changed.archived_sets)
        )
    return changed


SPEC_SET_UNARCHIVE = OperationSpec(
    id="sticker.set.unarchive",
    request=SetUnarchiveReq,
    response=StickerSetsChanged,
    impl=set_unarchive,
    summary="Restore archived sets to the panel",
    aliases=("emoji.set.unarchive",),
    mutating=True,
    idempotent=True,
    columns=("short_names", "unarchived"),
    headers=("Sets", "Unarchived"),
    example={"short_names": ["AnimatedEmojies"], "unarchived": 1},
    example_args="sticker set unarchive AnimatedEmojies",
    covers=("sticker.set-archive",),
)


class SetReorderReq(Request):
    set: Annotated[
        list[str],
        arg(0, metavar="SET", required=False, variadic=True, help="Full desired order."),
    ] = []
    type: Annotated[
        str, choice("sticker", "mask", "emoji", help="Which library's order to write.")
    ] = "sticker"
    top: Annotated[
        str | None, opt("--top", metavar="SET", help="Move just this set to the front.")
    ] = None


async def set_reorder(ctx: OpContext, req: SetReorderReq) -> StickerSetOrder:
    """Write the order of the installed sets.

    The API writes the **full** order for one library, and a partial vector
    silently drops the sets left out of it. `--top` is therefore
    read-modify-write: fetch the current order, move one id to the front,
    send the whole thing back.
    """
    from telethon.tl.functions import messages as fn

    current = await set_list(ctx, SetListReq(type=req.type))
    known = {item.short_name.lower(): item.id for item in current.items}
    known.update({str(item.id): item.id for item in current.items})

    def resolve(value: str) -> int:
        found = known.get(_names([value])[0].lower()) or known.get(str(value))
        if found is None:
            raise NotFoundError(f"{value!r} is not an installed {req.type} set")
        return found

    if req.top:
        head = resolve(req.top)
        order = [head] + [item.id for item in current.items if item.id != head]
    elif req.set:
        order = [resolve(value) for value in req.set]
    else:
        raise UsageError("give the full order, or --top <set>", field="set")

    await _client(ctx)(
        fn.ReorderStickerSetsRequest(
            order=order, masks=req.type == "mask" or None, emojis=req.type == "emoji" or None
        )
    )
    return StickerSetOrder(type=req.type, order=order, ok=True)


SPEC_SET_REORDER = OperationSpec(
    id="sticker.set.reorder",
    request=SetReorderReq,
    response=StickerSetOrder,
    impl=set_reorder,
    summary="Set the order of the installed sets",
    aliases=("emoji.set.reorder",),
    mutating=True,
    columns=("type", "order"),
    headers=("Library", "Order"),
    example={"type": "sticker", "order": [1258816259751983, 1258816259751984], "ok": True},
    example_args="sticker set reorder --top AnimatedEmojies",
    covers=("sticker.set-reorder",),
)


class SetSearchReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", help="Text to find.")]
    type: Annotated[str, choice("sticker", "emoji", help="Which catalogue.")] = "sticker"
    exclude_featured: Annotated[
        bool, opt("--exclude-featured", help="Skip sets already on the trending shelf.")
    ] = False


async def set_search(ctx: OpContext, req: SetSearchReq) -> Page[StickerSet]:
    """Search public sets by name.

    Results are set *previews* — metadata plus a couple of cover stickers,
    not the document list. `sticker set get` is what fetches the contents.
    """
    from telethon.tl.functions import messages as fn

    limit, _state = _media.window(ctx, "sticker.set.search", PageKind.LOCAL)
    request: Any = (
        fn.SearchEmojiStickerSetsRequest(
            q=req.query, hash=0, exclude_featured=req.exclude_featured or None
        )
        if req.type == "emoji"
        else fn.SearchStickerSetsRequest(
            q=req.query, hash=0, exclude_featured=req.exclude_featured or None
        )
    )
    result = await _client(ctx)(request)
    items = [_media.covered_set(covered) for covered in getattr(result, "sets", None) or []]
    return Page(items=items[:limit], has_more=False, total=len(items))


SPEC_SET_SEARCH = OperationSpec(
    id="sticker.set.search",
    request=SetSearchReq,
    response=Page[StickerSet],
    impl=set_search,
    summary="Search public sticker or emoji sets by name",
    aliases=("emoji.set.search",),
    paginated=PageKind.LOCAL,
    columns=("short_name", "title", "count", "official"),
    headers=("Short name", "Title", "Count", "Official"),
    example={"items": [_EXAMPLE_SET], "has_more": False},
    example_args="sticker set search cats",
    covers=("emoji.custom-sets", "sticker.set-search"),
)


# ---------------------------------------------------------------------------
# sticker search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    query: Annotated[
        str, arg(0, metavar="QUERY", required=False, help="Free text, or an emoji.")
    ] = ""
    emoji: Annotated[
        list[str], opt("--emoji", metavar="EMOJI", help="Emoji to match (repeatable).")
    ] = []
    custom: Annotated[
        bool, opt("--custom", help="Search custom emoji documents instead of stickers.")
    ] = False
    lang: Annotated[list[str], opt("--lang", metavar="CODE", help="Language codes.")] = []
    source: Annotated[str, choice("server", "installed", "all", help="Where to look.")] = "all"
    special: Annotated[
        str | None, choice("greeting", "premium", help="Fetch a server special list.")
    ] = None


async def search(ctx: OpContext, req: SearchReq) -> Page[Sticker]:
    """Search individual stickers by words or emoji.

    Two surfaces behind one command: `searchStickers` is the global search
    box, and `getStickers(emoticon)` is the suggestion strip that appears
    when you type an emoji. The app config's
    `stickers_emoji_suggest_only_api` forbids answering emoji suggestions
    purely from local sets, so the server is asked first and the installed
    sets are the fallback.
    """
    from telethon.tl.functions import messages as fn

    limit, state = _media.window(ctx, "sticker.search", PageKind.LOCAL)

    if req.special:
        result = await _client(ctx)(
            fn.GetStickersRequest(emoticon=SPECIAL_EMOTICONS[req.special], hash=0)
        )
        items = [_media.sticker_model(doc) for doc in getattr(result, "stickers", None) or []]
        return Page(items=items[:limit], has_more=False, total=len(items))

    if req.emoji and not req.query:
        items = []
        for emoticon in req.emoji:
            result = await _client(ctx)(fn.GetStickersRequest(emoticon=emoticon, hash=0))
            items.extend(
                _media.sticker_model(doc) for doc in getattr(result, "stickers", None) or []
            )
        if not items and req.source in ("installed", "all"):
            items = await _installed_matches(ctx, req.emoji)
        return Page(items=items[:limit], has_more=False, total=len(items))

    if req.source == "installed":
        return Page(items=await _installed_matches(ctx, req.emoji), has_more=False)

    offset = int(state.get("offset", 0))
    result = await _client(ctx)(
        fn.SearchStickersRequest(
            q=req.query,
            emoticon="".join(req.emoji),
            lang_code=list(req.lang) or [],
            offset=offset,
            limit=limit,
            hash=0,
            emojis=req.custom or None,
        )
    )
    items = [_media.sticker_model(doc) for doc in getattr(result, "stickers", None) or []]
    return build_page(
        items,
        op="sticker.search",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(items)},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


async def _installed_matches(ctx: OpContext, emojis: list[str]) -> list[Sticker]:
    """The local half of the suggestion strip: installed sets, walked here."""
    installed = await set_list(ctx, SetListReq())
    out: list[Sticker] = []
    for entry in installed.items[:20]:
        if not entry.short_name:
            continue
        detail = _set_detail(await _media.fetch_set(ctx, entry.short_name))
        for sticker in detail.stickers:
            if not emojis or sticker.emoji in emojis:
                out.append(sticker)
    return out


SPEC_SEARCH = OperationSpec(
    id="sticker.search",
    request=SearchReq,
    response=Page[Sticker],
    impl=search,
    summary="Search individual stickers by words or emoji",
    paginated=PageKind.LOCAL,
    columns=("doc_id", "emoji", "set_short_name", "mime"),
    headers=("Doc", "Emoji", "Set", "MIME"),
    example={"items": [_EXAMPLE_STICKER], "has_more": False},
    example_args="sticker search 'happy cat'",
    covers=("sticker.search", "sticker.special-lists", "sticker.suggestion-settings"),
    covers_partial=("emoji.custom-search-by-emoticon",),
    coverage_note="`emoji search --custom` is the custom-emoji half of the same suggestion popup.",
)


# ---------------------------------------------------------------------------
# sticker fave
# ---------------------------------------------------------------------------


class FaveListReq(Request):
    pass


async def fave_list(ctx: OpContext, req: FaveListReq) -> Page[Sticker]:
    """Favourite stickers.

    The reply also carries the emoji→sticker packs, which is what lets a
    caller resolve `fave:<emoji>` without a second round trip.
    """
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetFavedStickersRequest(hash=0))
    items = [
        _media.sticker_model(document, index=index)
        for index, document in enumerate(getattr(result, "stickers", None) or [])
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_FAVE_LIST = OperationSpec(
    id="sticker.fave.list",
    request=FaveListReq,
    response=Page[Sticker],
    impl=fave_list,
    summary="Favourite stickers",
    columns=("doc_id", "emoji", "set_short_name"),
    headers=("Doc", "Emoji", "Set"),
    example={"items": [_EXAMPLE_STICKER], "has_more": False},
    example_args="sticker fave list",
    covers_partial=("sticker.favorites",),
    coverage_note="Adding and removing are `sticker fave add` and `sticker fave remove`.",
)


class FaveAddReq(Request):
    sticker: Annotated[
        list[str],
        arg(
            0,
            metavar="STICKER",
            required=False,
            variadic=True,
            help="<set>/<index> or <set>/<emoji>.",
        ),
    ] = []
    from_message: Annotated[
        str | None,
        opt("--from-message", metavar="CHAT:ID", help="Take the sticker from this message."),
    ] = None


async def _from_message(ctx: OpContext, reference: str | None) -> tuple[Any, int] | None:
    if not reference:
        return None
    peer_text, _, message_id = str(reference).rpartition(":")
    if not peer_text or not message_id.lstrip("-").isdigit():
        raise UsageError("--from-message takes CHAT:ID", field="from_message")
    from tlgr.models.peer import parse_peer_ref

    return await _send.resolve(ctx, parse_peer_ref(peer_text)), int(message_id)


async def fave_add(ctx: OpContext, req: FaveAddReq) -> FaveResult:
    """Add stickers to favourites.

    Every `InputDocument` is resolved from the set (or the message) here and
    now: a document id copied out of an old listing carries a dead
    `file_reference` and the call fails with `FILE_REFERENCE_EXPIRED`.
    """
    from telethon.tl.functions import messages as fn

    documents = await _media.resolve_stickers(
        ctx, list(req.sticker), from_message=await _from_message(ctx, req.from_message)
    )
    before = {item.doc_id for item in (await fave_list(ctx, FaveListReq())).items}
    for document in documents:
        await _client(ctx)(fn.FaveStickerRequest(id=_media.input_document(document), unfave=False))
    ids = [int(getattr(document, "id", 0)) for document in documents]
    after = {item.doc_id for item in (await fave_list(ctx, FaveListReq())).items}
    evicted = sorted(before - after - set(ids))
    if evicted:
        ctx.warn(
            "the favourites limit was reached; the server evicted the oldest entries: "
            + ", ".join(str(i) for i in evicted)
        )
    return FaveResult(
        doc_ids=ids,
        faved=len(ids),
        already=bool(ids) and set(ids) <= before,
        evicted=evicted,
    )


SPEC_FAVE_ADD = OperationSpec(
    id="sticker.fave.add",
    request=FaveAddReq,
    response=FaveResult,
    impl=fave_add,
    summary="Add a sticker to favourites",
    mutating=True,
    idempotent=True,
    columns=("doc_ids", "faved", "evicted"),
    headers=("Docs", "Faved", "Evicted"),
    example={"doc_ids": [5312836234], "faved": 1, "evicted": []},
    example_args="sticker fave add AnimatedEmojies/0",
    covers_partial=("sticker.favorites",),
    coverage_note="Listing is `sticker fave list`; removing is `sticker fave remove`.",
)


class FaveRemoveReq(Request):
    sticker: Annotated[
        list[str], arg(0, metavar="STICKER", variadic=True, help="<set>/<index> or <set>/<emoji>.")
    ] = []


async def fave_remove(ctx: OpContext, req: FaveRemoveReq) -> FaveResult:
    """Remove stickers from favourites, with the same reference-freshness rule."""
    from telethon.tl.functions import messages as fn

    documents = await _media.resolve_stickers(ctx, list(req.sticker))
    for document in documents:
        await _client(ctx)(fn.FaveStickerRequest(id=_media.input_document(document), unfave=True))
    ids = [int(getattr(document, "id", 0)) for document in documents]
    return FaveResult(doc_ids=ids, unfaved=len(ids))


SPEC_FAVE_REMOVE = OperationSpec(
    id="sticker.fave.remove",
    request=FaveRemoveReq,
    response=FaveResult,
    impl=fave_remove,
    summary="Remove a sticker from favourites",
    mutating=True,
    idempotent=True,
    columns=("doc_ids", "unfaved"),
    headers=("Docs", "Unfaved"),
    example={"doc_ids": [5312836234], "unfaved": 1},
    example_args="sticker fave remove AnimatedEmojies/0",
    covers=("sticker.favorites",),
)


# ---------------------------------------------------------------------------
# sticker recent
# ---------------------------------------------------------------------------


class RecentListReq(Request):
    masks: Annotated[bool, opt("--masks", help="The recently used masks list instead.")] = False


async def recent_list(ctx: OpContext, req: RecentListReq) -> Page[Sticker]:
    """Recently used stickers — a hash-cached list the server appends to."""
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetRecentStickersRequest(hash=0, attached=req.masks or None))
    items = [
        _media.sticker_model(document, index=index)
        for index, document in enumerate(getattr(result, "stickers", None) or [])
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_RECENT_LIST = OperationSpec(
    id="sticker.recent.list",
    request=RecentListReq,
    response=Page[Sticker],
    impl=recent_list,
    summary="Recently used stickers",
    columns=("doc_id", "emoji", "set_short_name"),
    headers=("Doc", "Emoji", "Set"),
    example={"items": [_EXAMPLE_STICKER], "has_more": False},
    example_args="sticker recent list",
    covers_partial=("sticker.recent",),
    coverage_note="Forgetting one, or clearing the list, is `sticker recent remove`.",
)


class RecentRemoveReq(Request):
    sticker: Annotated[
        list[str],
        arg(
            0,
            metavar="STICKER",
            required=False,
            variadic=True,
            help="<set>/<index> or <set>/<emoji>.",
        ),
    ] = []
    masks: Annotated[bool, opt("--masks", help="Operate on the recent masks list.")] = False
    every: Annotated[bool, opt("--all", help="Clear the whole list.")] = False


async def recent_remove(ctx: OpContext, req: RecentRemoveReq) -> RecentResult:
    """Forget one recent sticker, or clear the list."""
    from telethon.tl.functions import messages as fn

    if req.every:
        await _client(ctx)(fn.ClearRecentStickersRequest(attached=req.masks or None))
        return RecentResult(cleared=True)

    documents = await _media.resolve_stickers(ctx, list(req.sticker))
    for document in documents:
        await _client(ctx)(
            fn.SaveRecentStickerRequest(
                id=_media.input_document(document), unsave=True, attached=req.masks or None
            )
        )
    ids = [int(getattr(document, "id", 0)) for document in documents]
    return RecentResult(doc_ids=ids, removed=len(ids))


SPEC_RECENT_REMOVE = OperationSpec(
    id="sticker.recent.remove",
    request=RecentRemoveReq,
    response=RecentResult,
    impl=recent_remove,
    summary="Forget one recent sticker, or clear the list",
    mutating=True,
    destructive=True,
    columns=("doc_ids", "removed", "cleared"),
    headers=("Docs", "Removed", "Cleared"),
    example={"doc_ids": [5312836234], "removed": 1, "cleared": False},
    example_args="sticker recent remove AnimatedEmojies/0",
    covers=("sticker.recent",),
)


# ---------------------------------------------------------------------------
# sticker pack (the sets you created)
# ---------------------------------------------------------------------------


def _mask_coords(value: str | None) -> Any:
    """`eyes:0.5,0.5,1.2` → `MaskCoords(n, x, y, zoom)`."""
    if not value:
        return None
    from telethon.tl import types

    places = {"forehead": 0, "eyes": 1, "mouth": 2, "chin": 3}
    where, _, numbers = value.partition(":")
    if where not in places:
        raise UsageError(
            f"--mask: expected one of {', '.join(places)}, optionally with :x,y,zoom",
            field="mask",
        )
    parts = [part for part in numbers.split(",") if part]
    try:
        x, y, zoom = (
            (float(parts[0]), float(parts[1]), float(parts[2])) if parts else (0.5, 0.5, 1.0)
        )
    except (IndexError, ValueError) as exc:
        raise UsageError("--mask coordinates are x,y,zoom", field="mask") from exc
    return types.MaskCoords(n=places[where], x=x, y=y, zoom=zoom)


async def _sticker_document(ctx: OpContext, path: Path, emoji: str) -> Any:
    """Upload one sticker file and return the `InputDocument` it became."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    if not path.exists():
        raise UsageError(f"{path} does not exist", field="file")
    handle = await upload(path)
    mime = {
        ".webp": "image/webp",
        ".png": "image/png",
        ".tgs": "application/x-tgsticker",
        ".webm": "video/webm",
    }.get(path.suffix.lower(), "image/webp")
    result = await _client(ctx)(
        fn.UploadMediaRequest(
            peer=types.InputPeerSelf(),
            media=types.InputMediaUploadedDocument(
                file=handle,
                mime_type=mime,
                attributes=[
                    types.DocumentAttributeSticker(
                        alt=emoji, stickerset=types.InputStickerSetEmpty()
                    )
                ],
            ),
        )
    )
    document = _media.document_of(result)
    if document is None:
        raise NotFoundError(f"the server did not accept {path.name} as a sticker")
    return _media.input_document(document)


class PackCreateReq(Request):
    short_name: Annotated[
        str | None,
        arg(0, metavar="SHORT_NAME", required=False, help="URL name; omit to have one suggested."),
    ] = None
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="Display title.")] = None
    type: Annotated[str, choice("sticker", "mask", "emoji", help="Pack kind.")] = "sticker"
    add: Annotated[
        list[str],
        opt("--add", metavar="FILE:EMOJI[:kw,kw]", help="A sticker to seed the pack with."),
    ] = []
    manifest: Annotated[
        str | None,
        opt("--manifest", metavar="PATH", kind="path", help="JSON manifest of files and emoji."),
    ] = None
    convert: Annotated[
        bool, opt("--convert/--no-convert", help="Convert inputs to the required format first.")
    ] = True
    thumb: Annotated[
        str | None, opt("--thumb", metavar="PATH", kind="path", help="Pack thumbnail.")
    ] = None
    text_color: Annotated[
        bool, opt("--text-color", help="Emoji packs: render in the message text colour.")
    ] = False
    software: Annotated[str, opt("--software", metavar="NAME", help="Creating-software tag.")] = (
        "tlgr"
    )


def _seed_items(req: PackCreateReq) -> list[tuple[Path, str, list[str]]]:
    """`--add FILE:EMOJI[:kw,kw]` and `--manifest` as one ordered list."""
    import json

    out: list[tuple[Path, str, list[str]]] = []
    for entry in req.add:
        parts = str(entry).split(":")
        if len(parts) < 2:
            raise UsageError("--add takes FILE:EMOJI[:kw,kw]", field="add")
        keywords = parts[2].split(",") if len(parts) > 2 and parts[2] else []
        out.append((Path(os.path.expanduser(parts[0])), parts[1], keywords))
    if req.manifest:
        raw = json.loads(Path(os.path.expanduser(req.manifest)).read_text(encoding="utf-8"))
        rows = raw.get("stickers", raw) if isinstance(raw, dict) else raw
        for row in rows:
            out.append(
                (
                    Path(os.path.expanduser(str(row["file"]))),
                    str(row.get("emoji") or ""),
                    list(row.get("keywords") or []),
                )
            )
    for path, emoji, _ in out:
        if not emoji:
            raise UsageError(
                f"{path.name}: every sticker must carry at least one emoji", field="add"
            )
    return out


async def pack_create(ctx: OpContext, req: PackCreateReq) -> PackCreated:
    """Create your own sticker, mask or custom-emoji pack.

    Four steps and the order matters: suggest or check the short name, upload
    each file with `messages.uploadMedia` (bound to `me`) to get an
    `InputDocument`, then one `createStickerSet` carrying every item.
    `--manifest` is the third-party import path — a folder of images plus an
    emoji map becomes exactly the same call, which is why `tg://importStickers`
    needs no command of its own.
    """
    from telethon.tl import types
    from telethon.tl.functions import stickers as fn

    short_name = req.short_name
    if not short_name:
        if not req.title:
            raise UsageError(
                "give a short name, or --title so one can be suggested", field="short_name"
            )
        suggestion = await _client(ctx)(fn.SuggestShortNameRequest(title=req.title))
        short_name = str(getattr(suggestion, "short_name", "") or "")
    available = bool(await _client(ctx)(fn.CheckShortNameRequest(short_name=short_name)))
    if getattr(ctx, "dry_run", False):
        return PackCreated(short_name=short_name, title=req.title or "", available=available)
    if not available:
        raise UsageError(f"the short name {short_name!r} is taken", field="short_name")

    items = _seed_items(req)
    if not items:
        raise UsageError("a new pack needs at least one --add or a --manifest", field="add")
    if req.convert:
        ctx.warn(
            "tlgr uploads the bytes as given: converting to 512px WebP / TGS / VP9 WebM "
            "needs Pillow or ffmpeg, and the server rejects a wrong format by name"
        )

    stickers = [
        types.InputStickerSetItem(
            document=await _sticker_document(ctx, path, emoji),
            emoji=emoji,
            keywords=",".join(keywords) or None,
        )
        for path, emoji, keywords in items
    ]
    thumb = None
    if req.thumb:
        thumb = await _sticker_document(ctx, Path(os.path.expanduser(req.thumb)), items[0][1])

    result = await _client(ctx)(
        fn.CreateStickerSetRequest(
            user_id=types.InputUserSelf(),
            title=req.title or short_name,
            short_name=short_name,
            stickers=stickers,
            masks=req.type == "mask" or None,
            emojis=req.type == "emoji" or None,
            text_color=req.text_color or None,
            thumb=thumb,
            software=req.software or None,
        )
    )
    detail = _set_detail(result)
    return PackCreated(
        id=detail.id,
        short_name=detail.short_name or short_name,
        title=detail.title,
        type=req.type,
        count=detail.count or len(stickers),
        link=_media.set_link(detail.short_name or short_name, req.type),
        stickers=detail.stickers,
        available=True,
    )


SPEC_PACK_CREATE = OperationSpec(
    id="sticker.pack.create",
    request=PackCreateReq,
    response=PackCreated,
    impl=pack_create,
    summary="Create your own sticker, mask or custom-emoji pack",
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("short_name", "title", "count", "link"),
    headers=("Short name", "Title", "Count", "Link"),
    example={
        "id": 1258816259751990,
        "short_name": "my_cats_by_tlgr",
        "title": "My cats",
        "type": "sticker",
        "count": 2,
        "link": "https://t.me/addstickers/my_cats_by_tlgr",
    },
    example_args="sticker pack create my_cats_by_tlgr --title 'My cats' --add cat.webp:🐱",
    covers=("sticker.import-third-party", "sticker.set-create"),
    covers_partial=("sticker.create-from-image",),
    coverage_note=(
        "Converting an arbitrary image to the required format needs Pillow/ffmpeg; tlgr "
        "uploads the bytes as given and says so rather than writing a file that is not a sticker."
    ),
)


class PackAddReq(Request):
    pack: Annotated[str, arg(0, metavar="PACK", help="A pack you created.")]
    file: Annotated[
        str | None, arg(1, metavar="FILE", required=False, kind="path", help="Sticker file.")
    ] = None
    emoji: Annotated[
        str | None, opt("--emoji", metavar="EMOJI", help="Emoji for the new sticker.")
    ] = None
    keywords: Annotated[list[str], opt("--keywords", metavar="WORD", help="Search keywords.")] = []
    mask: Annotated[
        str | None, opt("--mask", metavar="PLACE[:x,y,zoom]", help="Mask placement.")
    ] = None
    position: Annotated[
        int | None, opt("--position", metavar="N", help="Insert at this 0-based position.")
    ] = None
    replace: Annotated[
        str | None, opt("--replace", metavar="STICKER", help="Replace this sticker instead.")
    ] = None
    file_id: Annotated[
        str | None, opt("--file-id", metavar="ID", help="Use media already on Telegram.")
    ] = None


async def pack_add(ctx: OpContext, req: PackAddReq) -> PackStickerAdded:
    """Add or replace a sticker in a pack you created.

    `addStickerToSet` always appends, so `--position` is a second call; and
    `--replace` keeps the pack's ordering, which is what the GUI's "replace
    sticker" does.
    """
    from telethon.tl import types
    from telethon.tl.functions import stickers as fn

    if not req.emoji:
        raise UsageError("--emoji is required: every sticker carries at least one", field="emoji")
    if req.file_id:
        from telethon import utils

        resolved = utils.resolve_bot_file_id(req.file_id)
        if resolved is None:
            raise UsageError("--file-id: that is not a Telegram file id", field="file_id")
        document = _media.input_document(resolved)
    elif req.file:
        document = await _sticker_document(ctx, Path(os.path.expanduser(req.file)), req.emoji)
    else:
        raise UsageError("give a file, or --file-id", field="file")

    item = types.InputStickerSetItem(
        document=document,
        emoji=req.emoji,
        keywords=",".join(req.keywords) or None,
        mask_coords=_mask_coords(req.mask),
    )
    replaced: int | None = None
    if req.replace:
        old = (await _media.resolve_stickers(ctx, [req.replace]))[0]
        replaced = int(getattr(old, "id", 0))
        result = await _client(ctx)(
            fn.ReplaceStickerRequest(sticker=_media.input_document(old), new_sticker=item)
        )
    else:
        result = await _client(ctx)(
            fn.AddStickerToSetRequest(
                stickerset=_media.sticker_set_ref(req.pack, field="pack"), sticker=item
            )
        )
    detail = _set_detail(result)
    if req.position is not None:
        await _client(ctx)(fn.ChangeStickerPositionRequest(sticker=document, position=req.position))
    return PackStickerAdded(
        short_name=detail.short_name or _names([req.pack])[0],
        doc_id=int(getattr(document, "id", 0)),
        position=req.position,
        count=detail.count or len(detail.stickers),
        replaced=replaced,
    )


SPEC_PACK_ADD = OperationSpec(
    id="sticker.pack.add",
    request=PackAddReq,
    response=PackStickerAdded,
    impl=pack_add,
    summary="Add or replace a sticker in a pack you created",
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("short_name", "doc_id", "count"),
    headers=("Pack", "Doc", "Count"),
    example={"short_name": "my_cats_by_tlgr", "doc_id": 5312836299, "count": 3},
    example_args="sticker pack add my_cats_by_tlgr cat2.webp --emoji 🐱",
    covers=("sticker.create-from-image",),
    covers_partial=("sticker.set-edit-stickers",),
    coverage_note="Removing one is `sticker pack remove`; reordering is `sticker pack edit`.",
)


class PackRemoveReq(Request):
    pack: Annotated[str, arg(0, metavar="PACK", help="A pack you created.")]
    sticker: Annotated[
        list[str], arg(1, metavar="STICKER", variadic=True, help="Index, emoji or <set>/<index>.")
    ] = []


async def pack_remove(ctx: OpContext, req: PackRemoveReq) -> PackStickerRemoved:
    """Remove stickers from a pack you created.

    Removing the last sticker deletes the set server-side, which is worth
    knowing before you do it.
    """
    from telethon.tl.functions import stickers as fn

    refs = [value if "/" in str(value) else f"{req.pack}/{value}" for value in req.sticker]
    documents = await _media.resolve_stickers(ctx, refs)
    for document in documents:
        await _client(ctx)(fn.RemoveStickerFromSetRequest(sticker=_media.input_document(document)))
    remaining = _set_detail(await _media.fetch_set(ctx, req.pack, field="pack"))
    return PackStickerRemoved(
        short_name=remaining.short_name or _names([req.pack])[0],
        removed=[int(getattr(document, "id", 0)) for document in documents],
        count=remaining.count or len(remaining.stickers),
    )


SPEC_PACK_REMOVE = OperationSpec(
    id="sticker.pack.remove",
    request=PackRemoveReq,
    response=PackStickerRemoved,
    impl=pack_remove,
    summary="Remove a sticker from a pack you created",
    mutating=True,
    destructive=True,
    columns=("short_name", "removed", "count"),
    headers=("Pack", "Removed", "Left"),
    example={"short_name": "my_cats_by_tlgr", "removed": [5312836299], "count": 2},
    example_args="sticker pack remove my_cats_by_tlgr 0",
    covers=("sticker.set-edit-stickers",),
)


class PackEditReq(Request):
    pack: Annotated[str, arg(0, metavar="PACK", help="A pack you created.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New display title.")] = None
    thumb: Annotated[
        str | None, opt("--thumb", metavar="PATH", kind="path", help="New pack thumbnail.")
    ] = None
    thumb_sticker: Annotated[
        str | None, opt("--thumb-sticker", metavar="STICKER", help="Use a pack sticker as thumb.")
    ] = None
    no_thumb: Annotated[bool, opt("--no-thumb", help="Drop the custom thumbnail.")] = False
    sticker: Annotated[
        str | None, opt("--sticker", metavar="STICKER", help="Select one sticker in the pack.")
    ] = None
    emoji: Annotated[
        str | None, opt("--emoji", metavar="EMOJI", help="New emoji for --sticker.")
    ] = None
    keywords: Annotated[
        list[str], opt("--keywords", metavar="WORD", help="New keywords for --sticker.")
    ] = []
    mask: Annotated[
        str | None, opt("--mask", metavar="PLACE[:x,y,zoom]", help="Mask placement for --sticker.")
    ] = None
    position: Annotated[
        int | None, opt("--position", metavar="N", help="Move --sticker to this position.")
    ] = None


async def pack_edit(ctx: OpContext, req: PackEditReq) -> PackEdited:
    """The whole "edit pack" dialog, in one command.

    Without `--sticker` the flags act on the set (rename, thumbnail); with it
    they act on that one document. There is no bulk reorder in the API, so a
    full re-order is N position writes — which is why `--position` moves one
    sticker rather than taking a list.
    """
    from telethon.tl.functions import stickers as fn

    reference = _media.sticker_set_ref(req.pack, field="pack")
    changed: list[str] = []
    edited = PackEdited(short_name=_names([req.pack])[0])

    if req.title:
        result = await _client(ctx)(
            fn.RenameStickerSetRequest(stickerset=reference, title=req.title)
        )
        edited.title = _set_detail(result).title
        changed.append("title")

    if req.thumb or req.thumb_sticker or req.no_thumb:
        thumb = None
        document_id = None
        if req.thumb:
            thumb = await _sticker_document(ctx, Path(os.path.expanduser(req.thumb)), "🖼")
        elif req.thumb_sticker:
            picked = (await _media.resolve_stickers(ctx, [req.thumb_sticker]))[0]
            document_id = int(getattr(picked, "id", 0))
        await _client(ctx)(
            fn.SetStickerSetThumbRequest(
                stickerset=reference, thumb=thumb, thumb_document_id=document_id
            )
        )
        edited.thumb = MediaFile(doc_id=document_id or 0) if not req.no_thumb else None
        changed.append("thumb")

    if req.sticker:
        picked = (
            await _media.resolve_stickers(
                ctx, [f"{req.pack}/{req.sticker}" if "/" not in req.sticker else req.sticker]
            )
        )[0]
        document = _media.input_document(picked)
        edited.sticker = int(getattr(picked, "id", 0))
        if req.emoji or req.keywords or req.mask:
            await _client(ctx)(
                fn.ChangeStickerRequest(
                    sticker=document,
                    emoji=req.emoji,
                    keywords=",".join(req.keywords) or None,
                    mask_coords=_mask_coords(req.mask),
                )
            )
            changed.append("sticker")
        if req.position is not None:
            await _client(ctx)(
                fn.ChangeStickerPositionRequest(sticker=document, position=req.position)
            )
            changed.append("position")

    if not changed:
        raise UsageError("nothing to change; give --title, --thumb or --sticker", field="title")
    edited.changed = changed
    return edited


SPEC_PACK_EDIT = OperationSpec(
    id="sticker.pack.edit",
    request=PackEditReq,
    response=PackEdited,
    impl=pack_edit,
    summary="Change a pack's title or thumbnail, or one sticker's emoji, keywords or position",
    mutating=True,
    columns=("short_name", "title", "changed"),
    headers=("Pack", "Title", "Changed"),
    example={"short_name": "my_cats_by_tlgr", "title": "My cats", "changed": ["title"]},
    example_args="sticker pack edit my_cats_by_tlgr --title 'My cats'",
    covers_partial=("sticker.set-edit-meta", "sticker.set-edit-stickers"),
    coverage_note="Listing the packs you own is `sticker pack list`; deleting one is `sticker pack delete`.",
)


class PackDeleteReq(Request):
    pack: Annotated[str, arg(0, metavar="PACK", help="A pack you created.")]


async def pack_delete(ctx: OpContext, req: PackDeleteReq) -> PackDeleted:
    """Delete a pack you created.

    Irreversible and global: everyone who installed it loses it, and stickers
    already sent stop resolving to a set.
    """
    from telethon.tl.functions import stickers as fn

    ok = await _client(ctx)(
        fn.DeleteStickerSetRequest(stickerset=_media.sticker_set_ref(req.pack, field="pack"))
    )
    return PackDeleted(short_name=_names([req.pack])[0], deleted=bool(ok))


SPEC_PACK_DELETE = OperationSpec(
    id="sticker.pack.delete",
    request=PackDeleteReq,
    response=PackDeleted,
    impl=pack_delete,
    summary="Delete a pack you created",
    mutating=True,
    destructive=True,
    columns=("short_name", "deleted"),
    headers=("Pack", "Deleted"),
    example={"short_name": "my_cats_by_tlgr", "deleted": True},
    example_args="sticker pack delete my_cats_by_tlgr",
    covers_partial=("sticker.set-edit-meta",),
    coverage_note="Renaming and re-thumbnailing are `sticker pack edit`.",
)


class PackListReq(Request):
    pass


async def pack_list(ctx: OpContext, req: PackListReq) -> Page[StickerSet]:
    """The sets you created.

    Genuinely offset-paginated, unlike the installed lists: only sets with
    the creator flag appear here, and only those accept the `stickers.*`
    editing calls.
    """
    from telethon.tl.functions import messages as fn

    limit, state = _media.window(ctx, "sticker.pack.list", PageKind.LOCAL)
    result = await _client(ctx)(
        fn.GetMyStickersRequest(offset_id=int(state.get("offset_id", 0)), limit=limit)
    )
    items = [_media.covered_set(covered) for covered in getattr(result, "sets", None) or []]
    for item in items:
        item.creator = True
    next_state = {"offset_id": items[-1].id} if items else {}
    return build_page(
        items,
        op="sticker.pack.list",
        kind=PageKind.LOCAL,
        state=next_state,
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_PACK_LIST = OperationSpec(
    id="sticker.pack.list",
    request=PackListReq,
    response=Page[StickerSet],
    impl=pack_list,
    summary="Sticker sets you created",
    paginated=PageKind.LOCAL,
    columns=("short_name", "title", "count", "link"),
    headers=("Short name", "Title", "Count", "Link"),
    example={"items": [{**_EXAMPLE_SET, "creator": True}], "has_more": False},
    example_args="sticker pack list",
    covers=("sticker.set-edit-meta",),
)
