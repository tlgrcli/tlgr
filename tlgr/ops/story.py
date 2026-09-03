"""The `story` group: post, read, react to and manage stories.

Stories are the one surface where Telegram's API and its GUI disagree the
most, and the shape of this module follows the API rather than the screen.

* **Four RPCs are one list.** The GUI shows a peer's stories as one grid with
  tabs; the API has `getPeerStories`, `getPinnedStories`, `getStoriesArchive`
  and `getAlbumStories`. `story list` is one command with four flags, so a
  caller never has to know which tab maps to which method.
* **Reading and being seen are different acts.** `stories.readStories` clears
  *your* unread ring; `stories.incrementStoryViews` is what puts you in the
  poster's viewer list. v1's story-less world never had to make the
  distinction; here `story read` does the first and `--register-view` opts
  into the second, because an agent that silently appears in somebody's
  viewer list is a privacy bug.
* **The audience is a vector, not a value.** `--privacy` sets the base rule
  and `--allow`/`--exclude` layer exceptions on top, in that order, which is
  the only way "contacts, except Bob" is expressible.
* **The feed has no offsets.** `stories.getAllStories` pages with an opaque
  `state` plus a `next` flag, so `story feed list`'s cursor carries both.

`user hide-stories` is v1's spelling of `story hide` and keeps working: the
op declares it as a legacy path, so the old invocation resolves to the new
operation rather than to a module that no longer exists.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import (
    NotFoundError,
    NotSupportedError,
    PermissionError_,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.message import Message
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef, UserRef
from tlgr.models.story import (
    AlbumDeleted,
    AlbumOrder,
    BlockedStoryUser,
    BlocklistChange,
    LiveStory,
    MediaArea,
    StealthMode,
    StoriesDeleted,
    Story,
    StoryAlbum,
    StoryEvent,
    StoryExport,
    StoryFeedPeer,
    StoryHidden,
    StoryHiddenPeer,
    StoryLimits,
    StoryPinned,
    StoryPostCheck,
    StoryReactionResult,
    StoryRead,
    StoryReply,
    StoryReport,
    StoryShared,
    StoryStats,
    StoryViewer,
)
from tlgr.ops import _send, _story
from tlgr.ops._common import already, client, random_id, window
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer, peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_STORY: dict[str, Any] = {
    "id": 42,
    "peer_id": 4242,
    "date": "2026-09-03T09:14:07Z",
    "date_unix": 1788426847,
    "expire_date": "2026-09-04T09:14:07Z",
    "caption": "morning",
    "public": True,
    "views": {"views_count": 128, "reactions_count": 9, "has_viewers": True},
}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _entities(result: Any) -> dict[int, Any]:
    """`raw id → entity` for the users and chats a story reply carries."""
    table: dict[int, Any] = {}
    for entity in (
        *(getattr(result, "users", None) or []),
        *(getattr(result, "chats", None) or []),
    ):
        table[int(getattr(entity, "id", 0) or 0)] = entity
    return table


def _peer_entity(peer: Any, table: dict[int, Any]) -> Any:
    for attribute in ("user_id", "chat_id", "channel_id"):
        value = getattr(peer, attribute, None)
        if value is not None:
            return table.get(int(value))
    return None


def _link_story_id(ref: PeerRef | None) -> int | None:
    """The story id inside `t.me/<user>/s/<id>` or `tg://…&story=<id>`.

    The peer parser already reduced the link to its peer half and kept the
    original text in `raw`, so the id is read back from there rather than by
    parsing the link a second time somewhere else.
    """
    if ref is None:
        return None
    raw = str(getattr(ref, "raw", "") or "")
    if "story=" in raw:
        tail = raw.split("story=", 1)[1].split("&", 1)[0]
        return int(tail) if tail.isdigit() else None
    parts = [p for p in raw.replace("?", "/").split("/") if p]
    for index, part in enumerate(parts[:-1]):
        if part == "s" and parts[index + 1].isdigit():
            return int(parts[index + 1])
    return None


async def _stories_of(ctx: OpContext, peer: Any, ids: list[int]) -> list[Any]:
    """`stories.getStoriesByID`, which is also how a skipped item is hydrated."""
    from telethon.tl.functions import stories as fn

    if not ids:
        return []
    result = await client(ctx)(fn.GetStoriesByIDRequest(peer=peer, id=ids))
    return list(getattr(result, "stories", None) or [])


async def _require_own_story(ctx: OpContext, peer: Any, story_id: int) -> Any:
    """Fetch one story, or say which of the two reasons it is unavailable."""
    found = await _stories_of(ctx, peer, [story_id])
    if not found or type(found[0]).__name__ == "StoryItemDeleted":
        raise NotFoundError(f"story {story_id} is not available")
    return found[0]


def _cover_attributes(attributes: list[Any], cover_ts: float | None) -> list[Any]:
    """Put `--cover-ts` on the video attribute, where the server reads it."""
    if cover_ts is None:
        return attributes
    for attribute in attributes:
        if type(attribute).__name__ == "DocumentAttributeVideo":
            attribute.video_start_ts = float(cover_ts)
    return attributes


def _sticker_documents(ids: tuple[int, ...]) -> list[Any] | None:
    """`--sticker-doc` as `InputDocument`s.

    Declarative only: the chip says "this media contains stickers" and the
    server does not fetch them, which is why an id without an access hash is
    enough here and nowhere else.
    """
    if not ids:
        return None
    from telethon.tl import types

    return [types.InputDocument(id=int(i), access_hash=0, file_reference=b"") for i in ids]


# ---------------------------------------------------------------------------
# story post
# ---------------------------------------------------------------------------


class PrivacyOptions(Request, kw_only=True):
    """The audience flags `story post`, `story edit` and `story live start` share.

    A base class rather than a duplicated block: an audience that can be set
    on a story must be editable afterwards, and two copies of four flags is
    how those two lists drift apart.
    """

    privacy: Annotated[
        str | None,
        choice("everyone", "contacts", "close-friends", "selected", help="Audience base rule."),
    ] = None
    allow: Annotated[
        list[str],
        opt("--allow", metavar="USER|chat:CHAT", help="Add to the allow list. Repeatable."),
    ] = []
    exclude: Annotated[
        list[str],
        opt("--exclude", metavar="USER|chat:CHAT", help="Add to the deny list. Repeatable."),
    ] = []
    privacy_preset: Annotated[
        str | None,
        opt("--privacy-preset", metavar="NAME", help="Reuse [story.privacy_presets].<name>."),
    ] = None


class AreaOptions(PrivacyOptions):
    """The media-area flags `story post` and `story edit` share.

    Chained onto `PrivacyOptions` rather than mixed in beside it: a msgspec
    Struct has one instance layout, so two Struct bases is a TypeError. Every
    command that takes areas also takes an audience, so the chain costs
    nothing.
    """

    areas: Annotated[
        str | None,
        opt("--areas", metavar="PATH", help="Media areas as the JSON `--areas-out` writes."),
    ] = None
    area_geo: Annotated[
        list[str],
        opt("--area-geo", metavar="LAT,LON[,ADDR]@X,Y,W,H", help="Location pill. Repeatable."),
    ] = []
    area_venue: Annotated[
        list[str],
        opt("--area-venue", metavar="QUERY@X,Y,W,H", help="Venue pill (inline query)."),
    ] = []
    area_venue_near: Annotated[
        str | None,
        opt("--area-venue-near", metavar="LAT,LON", help="Anchor point for the venue query."),
    ] = None
    area_venue_pick: Annotated[
        int, opt("--area-venue-pick", metavar="N", help="Which venue result to use.", ge=0)
    ] = 0
    area_url: Annotated[
        list[str], opt("--area-url", metavar="URL@X,Y,W,H", help="Link sticker (Premium).")
    ] = []
    area_reaction: Annotated[
        list[str],
        opt("--area-reaction", metavar="EMOJI@X,Y,W,H", help="Suggested-reaction bubble."),
    ] = []
    area_post: Annotated[
        list[str],
        opt("--area-post", metavar="CHAT:MSG_ID@X,Y,W,H", help="Channel-post card."),
    ] = []
    area_weather: Annotated[
        list[str],
        opt("--area-weather", metavar="SPEC@X,Y,W,H", help="Weather widget; `auto` resolves it."),
    ] = []
    area_gift: Annotated[
        list[str], opt("--area-gift", metavar="SLUG@X,Y,W,H", help="Collectible star-gift area.")
    ] = []


class PostReq(AreaOptions, kw_only=True):
    file: Annotated[
        list[str],
        arg(0, metavar="FILE", variadic=True, kind="path", help="Media to post, one per story."),
    ] = []
    caption: Annotated[str | None, opt("--caption", help="Story caption.")] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Caption formatting.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="CHAT", kind="peer", help="Post as this channel."),
    ] = None
    period: Annotated[
        str | None, choice("6h", "12h", "24h", "48h", help="How long it stays active.")
    ] = None
    pin: Annotated[bool, opt("--pin", help="Keep on my page when it expires.")] = False
    protect: Annotated[bool, opt("--protect", help="noforwards: block saving/forwarding.")] = False
    album: Annotated[list[int], opt("--album", metavar="ID", help="Add to this album id.")] = []
    music: Annotated[
        str | None, opt("--music", metavar="PATH", kind="path", help="Attach a soundtrack.")
    ] = None
    cover_ts: Annotated[
        float | None, opt("--cover-ts", metavar="SECONDS", help="Video cover frame.")
    ] = None
    sticker_doc: Annotated[
        list[int],
        opt("--sticker-doc", metavar="ID", help="Declare a sticker baked into the media."),
    ] = []
    repost: Annotated[
        str | None, opt("--repost", metavar="PEER:ID", help="Repost somebody else's story.")
    ] = None
    modified: Annotated[bool, opt("--modified", help="Mark the repost as edited.")] = False
    repost_message: Annotated[
        str | None,
        opt("--repost-message", metavar="CHAT:MSG_ID", help="'Repost to story' from a message."),
    ] = None
    as_message: Annotated[
        bool, opt("--as-message", help="Send the media to chats as ordinary messages instead.")
    ] = False
    until: Annotated[
        list[PeerRef],
        opt("--until", metavar="CHAT", kind="peer", help="Destinations for --as-message."),
    ] = []
    no_check: Annotated[bool, opt("--no-check", help="Skip the canSendStory pre-flight.")] = False


async def _post_media(ctx: OpContext, req: PostReq, source: str) -> Any:
    """One `--file` as the `InputMedia` a story wants."""
    media = await _send.input_media(ctx, source)
    if type(media).__name__ == "InputMediaUploadedDocument":
        media.attributes = _cover_attributes(list(media.attributes or []), req.cover_ts)
    stickers = _sticker_documents(tuple(req.sticker_doc))
    if stickers is not None and hasattr(media, "stickers"):
        media.stickers = stickers
    return media


async def _music_document(ctx: OpContext, source: str) -> Any:
    """`--music` as an `InputDocument`, by uploading and realising the file."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if source.isdigit():
        raise UsageError(
            "--music takes a path: a bare document id carries no access hash, "
            "so the server cannot look the soundtrack up",
            field="music",
        )
    media = await _send.input_media(ctx, source)
    result = await client(ctx)(fn.UploadMediaRequest(peer=types.InputPeerSelf(), media=media))
    document = getattr(result, "document", None)
    if document is None:
        raise UsageError(f"{source} is not an audio file Telegram accepted", field="music")
    return types.InputDocument(
        id=document.id, access_hash=document.access_hash, file_reference=document.file_reference
    )


async def post(ctx: OpContext, req: PostReq) -> Page[Story]:
    """Post one story per `--file`, sharing one audience and one period.

    The pre-flight runs *between* items, not only once: the weekly and monthly
    story quotas are consumed as the loop runs, and a batch that ignored that
    would fail its fourth upload after paying for three.
    """
    from telethon.tl.functions import stories as fn

    if not req.file:
        raise UsageError("give at least one FILE to post", field="file")

    peer = await _story.resolve_or_self(ctx, req.send_as)
    peer_id = _send.peer_id_of(peer)
    text, entities = _send.body(req.caption, parse=req.parse, entities=req.entities)
    rules = await _story.privacy_rules(
        ctx,
        base=req.privacy or "everyone",
        allow=tuple(req.allow),
        exclude=tuple(req.exclude),
        preset=req.privacy_preset,
    )
    areas, area_models = await _story.build_areas(
        ctx,
        areas_file=req.areas,
        geo=tuple(req.area_geo),
        venue=tuple(req.area_venue),
        venue_near=req.area_venue_near,
        venue_pick=req.area_venue_pick,
        url=tuple(req.area_url),
        reaction=tuple(req.area_reaction),
        post=tuple(req.area_post),
        weather=tuple(req.area_weather),
        gift=tuple(req.area_gift),
    )
    if req.repost_message:
        areas.append(await _repost_message_area(ctx, req.repost_message))

    _warn_excluded_mentions(ctx, entities, req.exclude)

    fwd_peer, fwd_story = (None, None)
    if req.repost:
        reference, _, story_id = str(req.repost).rpartition(":")
        if not reference or not story_id.isdigit():
            raise UsageError("--repost takes PEER:ID", field="repost")
        from tlgr.models.peer import parse_peer_ref

        fwd_peer = await _send.resolve(ctx, parse_peer_ref(reference))
        fwd_story = int(story_id)

    music = await _music_document(ctx, req.music) if req.music else None
    period = _story.PERIODS.get(req.period or "24h")

    if req.as_message:
        return await _send_as_messages(ctx, req, text, entities)

    items: list[Story] = []
    for index, source in enumerate(req.file):
        if not req.no_check:
            await _preflight(ctx, peer)
        media = await _post_media(ctx, req, source)
        updates = await client(ctx)(
            fn.SendStoryRequest(
                peer=peer,
                media=media,
                privacy_rules=rules,
                pinned=req.pin or None,
                noforwards=req.protect or None,
                fwd_modified=req.modified or None,
                media_areas=areas or None,
                caption=text or None,
                entities=_send.tl_entities(entities),
                random_id=random_id(),
                period=period,
                fwd_from_id=fwd_peer,
                fwd_from_story=fwd_story,
                albums=list(req.album) or None,
                music=music,
            )
        )
        story = _story_from_updates(updates, peer_id=peer_id)
        story.media_areas = story.media_areas or area_models
        items.append(story)
        ctx.emit("story_new", {"peer": peer_id, "story_id": story.id, "index": index})
    return Page(items=items, has_more=False, total=len(items))


def _warn_excluded_mentions(ctx: OpContext, entities: Any, exclude: list[str]) -> None:
    """Warn when the caption @-mentions somebody the audience shuts out.

    The GUI shows the same warning, and it is the difference between a story
    that reads as a shout-out and one the person named never sees.
    """
    if not exclude:
        return
    excluded = {str(e).lstrip("@").lower() for e in exclude}
    for entity in entities or []:
        if getattr(entity, "type", "") == "mention":
            ctx.warn(
                "a mentioned user may be excluded by the privacy rules "
                f"({', '.join(sorted(excluded))}); they will not see the story"
            )
            return


async def _repost_message_area(ctx: OpContext, spec: str) -> Any:
    """`--repost-message CHAT:MSG_ID[@X,Y,W,H]` as a channel-post area."""
    from telethon.tl import types

    from tlgr.models.peer import parse_peer_ref
    from tlgr.ops._common import input_channel

    payload, _, rect = spec.partition("@")
    chat, _, msg_id = payload.rpartition(":")
    if not chat or not msg_id.isdigit():
        raise UsageError("--repost-message takes CHAT:MSG_ID", field="repost_message")
    peer = await _send.resolve(ctx, parse_peer_ref(chat))
    coordinates = types.MediaAreaCoordinates(x=50.0, y=50.0, w=80.0, h=30.0, rotation=0.0)
    if rect:
        numbers = [float(p) for p in rect.split(",")]
        coordinates = types.MediaAreaCoordinates(
            x=numbers[0],
            y=numbers[1],
            w=numbers[2],
            h=numbers[3],
            rotation=numbers[4] if len(numbers) > 4 else 0.0,
        )
    return types.InputMediaAreaChannelPost(
        coordinates=coordinates, channel=input_channel(peer), msg_id=int(msg_id)
    )


async def _send_as_messages(ctx: OpContext, req: PostReq, text: str, entities: Any) -> Page[Story]:
    """`--as-message`: the prepared media goes to chats instead of the profile."""
    from telethon.tl.functions import messages as fn

    if not req.until:
        raise UsageError("--as-message needs at least one --until CHAT", field="until")
    items: list[Story] = []
    for destination in req.until:
        peer = await _send.resolve(ctx, destination)
        for source in req.file:
            media = await _post_media(ctx, req, source)
            await client(ctx)(
                fn.SendMediaRequest(
                    peer=peer,
                    media=media,
                    message=text,
                    entities=_send.tl_entities(entities),
                    random_id=random_id(),
                    noforwards=req.protect or None,
                )
            )
    ctx.warn("--as-message sent the media as ordinary messages; no story was posted")
    return Page(items=items, has_more=False, total=0)


def _story_from_updates(updates: Any, *, peer_id: int) -> Story:
    """The story an `Updates` reply carries, or a stub with the assigned id."""
    for update in getattr(updates, "updates", None) or []:
        name = type(update).__name__
        if name == "UpdateStory":
            return _story.story_model(getattr(update, "story", None), peer_id=peer_id)
    for update in getattr(updates, "updates", None) or []:
        if type(update).__name__ == "UpdateStoryID":
            return Story(id=int(getattr(update, "id", 0) or 0), peer_id=peer_id)
    return Story(id=0, peer_id=peer_id)


SPEC_POST = OperationSpec(
    id="story.post",
    request=PostReq,
    response=Page[Story],
    impl=post,
    summary="Post one or more stories, with audience, media areas and duration",
    description=(
        "Several FILEs post several stories in one run, sharing the audience, "
        "period and pin settings. Vertical media only; overlays other than "
        "media areas must already be rendered into the file."
    ),
    mutating=True,
    rate_class="send",
    timeout_s=600,
    tags=frozenset({"visible-to-others"}),
    columns=("id", "peer_id", "expire_date"),
    headers=("ID", "Peer", "Expires"),
    example={"items": [_EXAMPLE_STORY], "has_more": False},
    example_args="story post morning.jpg --caption 'morning' --privacy contacts",
    covers=(
        "bots.webapp-share-to-story",
        "groups-channels-admin.stories-as-channel",
        "stories.area-channel-post",
        "stories.area-location",
        "stories.area-star-gift",
        "stories.area-suggested-reaction",
        "stories.area-url",
        "stories.area-venue",
        "stories.area-weather",
        "stories.attached-stickers",
        "stories.mention-users",
        "stories.post-as-channel",
        "stories.post-batch",
        "stories.post-keep-on-page",
        "stories.post-music",
        "stories.post-period",
        "stories.post-photo",
        "stories.post-protect",
        "stories.post-stickers-drawing",
        "stories.post-to-album",
        "stories.post-video",
        "stories.privacy-auto-exceptions",
        "stories.privacy-close-friends",
        "stories.privacy-contacts",
        "stories.privacy-everyone",
        "stories.privacy-selected",
        "stories.repost",
        "stories.repost-message-to-story",
        "stories.send-as-message-instead",
    ),
)


# ---------------------------------------------------------------------------
# story can-post
# ---------------------------------------------------------------------------

#: app-config key → the `StoryLimits` field it fills. Nothing is hardcoded:
#: Telegram moves these numbers without moving the layer.
_LIMIT_KEYS: dict[str, tuple[str, str]] = {
    "story_expiring_limit_default": ("expiring_limit", "story_expiring_limit_premium"),
    "stories_sent_weekly_limit_default": (
        "sent_weekly_limit",
        "stories_sent_weekly_limit_premium",
    ),
    "stories_sent_monthly_limit_default": (
        "sent_monthly_limit",
        "stories_sent_monthly_limit_premium",
    ),
    "story_caption_length_limit_default": (
        "caption_length_limit",
        "story_caption_length_limit_premium",
    ),
    "stories_suggested_reactions_limit_default": (
        "suggested_reactions_limit",
        "stories_suggested_reactions_limit_premium",
    ),
}

#: app-config key → the `StoryLimits` field, for the ones with no Premium twin.
_FLAT_LIMIT_KEYS: dict[str, str] = {
    "stories_area_url_max": "area_url_max",
    "stories_albums_limit": "albums_limit",
    "stories_album_stories_limit": "album_stories_limit",
    "stories_pinned_to_top_count_max": "pinned_to_top_max",
    "story_viewers_expire_period": "viewers_expire_period",
    "stories_stealth_past_period": "stealth_past_period",
    "stories_stealth_future_period": "stealth_future_period",
    "stories_stealth_cooldown_period": "stealth_cooldown_period",
}


def _limits(config: dict[str, Any], *, premium: bool) -> StoryLimits:
    limits = StoryLimits()
    for key, (field, premium_key) in _LIMIT_KEYS.items():
        source = premium_key if premium and premium_key in config else key
        value = config.get(source)
        if isinstance(value, (int, float)):
            setattr(limits, field, int(value))
        if premium_key in config and config.get(premium_key) != config.get(key):
            limits.premium_unlocks.append(field)
    for key, field in _FLAT_LIMIT_KEYS.items():
        value = config.get(key)
        if isinstance(value, (int, float)):
            setattr(limits, field, int(value))
    limits.premium_unlocks.sort()
    return limits


#: `canSendStoryResult*` → the reason string tlgr reports.
_CANNOT: dict[str, str] = {
    "CanSendStoryResultPremiumNeeded": "PREMIUM_ACCOUNT_REQUIRED",
    "CanSendStoryResultBoostNeeded": "BOOSTS_REQUIRED",
    "CanSendStoryResultActiveStoryLimitExceeded": "STORIES_TOO_MUCH",
    "CanSendStoryResultWeeklyLimit": "STORY_SEND_FLOOD_WEEKLY",
    "CanSendStoryResultMonthlyLimit": "STORY_SEND_FLOOD_MONTHLY",
    "CanSendStoryResultLiveStoryIsActive": "STORY_LIVE_ALREADY",
}


async def _preflight(ctx: OpContext, peer: Any) -> None:
    """`stories.canSendStory`, translated into a refusal a human can act on."""
    from telethon.tl.functions import stories as fn

    result = await client(ctx)(fn.CanSendStoryRequest(peer=peer))
    name = type(result).__name__
    reason = _CANNOT.get(name)
    if reason is None:
        return
    retry = getattr(result, "retry_after", None) or getattr(result, "period", None)
    detail = f" (retry after {retry}s)" if retry else ""
    if reason == "BOOSTS_REQUIRED":
        raise PermissionError_(f"posting a story here needs more boosts{detail}")
    if reason == "PREMIUM_ACCOUNT_REQUIRED":
        raise PermissionError_("posting this story needs Telegram Premium")
    raise PermissionError_(f"cannot post a story right now: {reason}{detail}")


class CanPostReq(Request):
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="CHAT", kind="peer", help="Check this channel instead of you."),
    ] = None
    chats: Annotated[
        bool, opt("--chats", help="Also list every chat where you hold post_stories.")
    ] = False
    limits: Annotated[bool, opt("--limits/--no-limits", help="Include the limit block.")] = True


async def can_post(ctx: OpContext, req: CanPostReq) -> StoryPostCheck:
    """The pre-flight the GUI runs before it opens the camera.

    Re-run it immediately before posting: the answer is a snapshot of quotas
    that other sessions are spending at the same time.
    """
    from telethon.tl.functions import stories as fn

    from tlgr.ops import _media

    peer = await _story.resolve_or_self(ctx, req.send_as)
    result = await client(ctx)(fn.CanSendStoryRequest(peer=peer))
    name = type(result).__name__
    check = StoryPostCheck(
        can_post=name == "CanSendStoryCount",
        reason=_CANNOT.get(name, ""),
        count_remains=getattr(result, "count_remains", None),
        free_slots=getattr(result, "count_remains", None),
        retry_after=getattr(result, "retry_after", None) or getattr(result, "period", None),
        boosts_required=getattr(result, "boosts_required", None) or getattr(result, "boosts", None),
    )

    me = await client(ctx).get_me()
    check.premium = bool(getattr(me, "premium", False))
    if req.limits:
        check.limits = _limits(await _media.app_config(ctx), premium=check.premium)
    if req.chats:
        chats = await client(ctx)(fn.GetChatsToSendRequest())
        check.chats = [entity_to_peer(chat) for chat in (getattr(chats, "chats", None) or [])]
    return check


SPEC_CAN_POST = OperationSpec(
    id="story.can-post",
    request=CanPostReq,
    response=StoryPostCheck,
    impl=can_post,
    summary="Free story slots, limits, Premium gates and the chats you may post to",
    description="Re-run it immediately before posting; the quotas move under you.",
    columns=("can_post", "count_remains", "reason"),
    headers=("Can post", "Remaining", "Reason"),
    example={"can_post": True, "count_remains": 2, "free_slots": 2, "premium": False},
    example_args="story can-post --chats",
    covers=(
        "stories.can-post",
        "stories.chats-to-post",
        "stories.limits-config",
        "stories.premium-gates",
    ),
)


# ---------------------------------------------------------------------------
# story edit
# ---------------------------------------------------------------------------


class EditReq(AreaOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[int, arg(1, metavar="ID", help="Story id.")]
    caption: Annotated[str | None, opt("--caption", help="New caption.")] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Caption formatting.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    file: Annotated[
        str | None, opt("--file", metavar="PATH", kind="path", help="Replace the media.")
    ] = None
    cover_ts: Annotated[
        float | None,
        opt("--cover-ts", metavar="SECONDS", help="New cover frame, without re-uploading."),
    ] = None
    music: Annotated[
        str | None, opt("--music", metavar="PATH", kind="path", help="Replace the soundtrack.")
    ] = None


async def edit(ctx: OpContext, req: EditReq) -> Story:
    """Change a posted story: only the flags you pass are sent.

    `--cover-ts` alone takes the no-reupload path — the current document is
    wrapped in `inputFileStoryDocument` and resent with the new
    `video_start_ts`, which is how the GUI moves a cover frame without
    spending the upload again.
    """
    from telethon.tl import types
    from telethon.tl.functions import stories as fn

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)

    caption: str | None = None
    entities: Any = None
    if req.caption is not None:
        text, parsed = _send.body(req.caption, parse=req.parse, entities=req.entities)
        caption, entities = text, _send.tl_entities(parsed)

    media: Any = None
    if req.file:
        media = await _send.input_media(ctx, req.file)
        if type(media).__name__ == "InputMediaUploadedDocument":
            media.attributes = _cover_attributes(list(media.attributes or []), req.cover_ts)
    elif req.cover_ts is not None:
        current = await _require_own_story(ctx, peer, req.id)
        document = getattr(getattr(current, "media", None), "document", None)
        if document is None:
            raise UsageError("--cover-ts only applies to a video story", field="cover_ts")
        media = types.InputMediaUploadedDocument(
            file=types.InputFileStoryDocument(
                id=types.InputDocument(
                    id=document.id,
                    access_hash=document.access_hash,
                    file_reference=document.file_reference,
                )
            ),
            mime_type=str(getattr(document, "mime_type", "video/mp4")),
            attributes=_cover_attributes(
                list(getattr(document, "attributes", None) or []), req.cover_ts
            ),
        )

    areas, _models = await _story.build_areas(
        ctx,
        areas_file=req.areas,
        geo=tuple(req.area_geo),
        venue=tuple(req.area_venue),
        venue_near=req.area_venue_near,
        venue_pick=req.area_venue_pick,
        url=tuple(req.area_url),
        reaction=tuple(req.area_reaction),
        post=tuple(req.area_post),
        weather=tuple(req.area_weather),
        gift=tuple(req.area_gift),
    )

    rules = None
    if req.privacy or req.allow or req.exclude or req.privacy_preset:
        rules = await _story.privacy_rules(
            ctx,
            base=req.privacy or "everyone",
            allow=tuple(req.allow),
            exclude=tuple(req.exclude),
            preset=req.privacy_preset,
        )

    music = await _music_document(ctx, req.music) if req.music else None
    if not any((caption is not None, media is not None, areas, rules, music)):
        raise UsageError("nothing to edit; pass a caption, media, areas or an audience", field="id")

    await client(ctx)(
        fn.EditStoryRequest(
            peer=peer,
            id=req.id,
            media=media,
            media_areas=areas or None,
            caption=caption,
            entities=entities,
            privacy_rules=rules,
            music=music,
        )
    )
    ctx.emit("story_edited", {"peer": peer_id, "story_id": req.id})
    fresh = await _stories_of(ctx, peer, [req.id])
    story = (
        _story.story_model(fresh[0], peer_id=peer_id)
        if fresh
        else Story(id=req.id, peer_id=peer_id)
    )
    story.edited = True
    return story


SPEC_EDIT = OperationSpec(
    id="story.edit",
    request=EditReq,
    response=Story,
    impl=edit,
    summary="Edit a posted story: caption, audience, media, cover frame or areas",
    description=(
        "Privacy edits are user stories only — a channel story has no rule "
        "vector. `--cover-ts` without `--file` re-sends the current document "
        "rather than uploading it again."
    ),
    mutating=True,
    rate_class="send",
    timeout_s=300,
    tags=frozenset({"visible-to-others"}),
    columns=("id", "peer_id", "edited", "caption"),
    example={**_EXAMPLE_STORY, "edited": True},
    example_args="story edit me 42 --caption 'still morning'",
    covers=(
        "stories.edit-areas",
        "stories.edit-caption",
        "stories.edit-cover",
        "stories.edit-media",
        "stories.edit-privacy",
    ),
)


# ---------------------------------------------------------------------------
# story delete
# ---------------------------------------------------------------------------


class DeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose stories.")]
    id: Annotated[
        list[str], arg(1, metavar="ID", variadic=True, help="Story ids, or `10-14` ranges.")
    ] = []


async def delete(ctx: OpContext, req: DeleteReq) -> StoriesDeleted:
    """Delete stories permanently — active, profile-pinned or archived alike."""
    from telethon.tl.functions import stories as fn

    ids = _story.story_ids(req.id)
    if not ids:
        raise UsageError("give at least one story id", field="id")
    peer = await _send.resolve(ctx, req.chat)
    deleted = await client(ctx)(fn.DeleteStoriesRequest(peer=peer, id=ids))
    peer_id = _send.peer_id_of(peer)
    ctx.emit("story_deleted", {"peer": peer_id, "ids": list(deleted or ids)})
    return StoriesDeleted(peer=peer_id, deleted_ids=[int(i) for i in (deleted or [])])


SPEC_DELETE = OperationSpec(
    id="story.delete",
    request=DeleteReq,
    response=StoriesDeleted,
    impl=delete,
    summary="Delete stories permanently",
    description="Channel stories need the `delete_stories` admin right.",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("peer", "deleted_ids"),
    example={"peer": 4242, "deleted_ids": [42]},
    example_args="story delete me 42",
    covers=("stories.delete",),
)


# ---------------------------------------------------------------------------
# story list
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose stories.")]
    profile: Annotated[bool, opt("--profile", help="The stories kept on the profile page.")] = False
    archive: Annotated[bool, opt("--archive", help="The private archive, expired included.")] = (
        False
    )
    album: Annotated[int | None, opt("--album", metavar="ID", help="Only this album.")] = None
    offset_id: Annotated[
        int | None, opt("--offset-id", metavar="ID", help="Page from this story id downwards.")
    ] = None
    hydrate: Annotated[
        bool, opt("--hydrate/--no-hydrate", help="Resolve skipped placeholders.")
    ] = True
    translate: Annotated[
        str | None, opt("--translate", metavar="LANG", help="Also translate the captions.")
    ] = None


async def list_stories(ctx: OpContext, req: ListReq) -> Page[Story]:
    """A peer's stories: active by default, or the profile page, archive or an album.

    Listing never registers a view — that is `story read --register-view`.
    """
    from telethon.tl.functions import stories as fn

    limit, state = window(ctx, "story.list", PageKind.HISTORY, 30)
    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    offset = int(state.get("offset") or req.offset_id or 0)

    if req.album is not None:
        result = await client(ctx)(
            fn.GetAlbumStoriesRequest(peer=peer, album_id=req.album, offset=offset, limit=limit)
        )
    elif req.archive:
        result = await client(ctx)(
            fn.GetStoriesArchiveRequest(peer=peer, offset_id=offset, limit=limit)
        )
    elif req.profile:
        result = await client(ctx)(
            fn.GetPinnedStoriesRequest(peer=peer, offset_id=offset, limit=limit)
        )
    else:
        result = await client(ctx)(fn.GetPeerStoriesRequest(peer=peer))
        result = getattr(result, "stories", result)

    raw = list(getattr(result, "stories", None) or [])
    if req.hydrate:
        raw = await _hydrate(ctx, peer, raw)
    pinned_top = set(getattr(result, "pinned_to_top", None) or [])
    items = [_story.story_model(item, peer_id=peer_id) for item in raw]
    for item in items:
        if item.id in pinned_top:
            item.pinned = True
    if req.translate:
        await _translate_captions(ctx, items, req.translate)

    if req.album is not None:
        next_state = {"offset": offset + len(items)}
    else:
        next_state = {"offset": items[-1].id if items else offset}
    return build_page(
        items,
        op="story.list",
        kind=PageKind.HISTORY,
        state=next_state,
        account=ctx.account,
        limit=limit if not (req.album is None and not req.archive and not req.profile) else None,
        has_more=None if (req.album is not None or req.archive or req.profile) else False,
        total=getattr(result, "count", None),
    )


async def _hydrate(ctx: OpContext, peer: Any, raw: list[Any]) -> list[Any]:
    """Replace `storyItemSkipped` placeholders with the real items.

    A feed hands back placeholders for everything the client is assumed to
    have cached; tlgr has no cache, so without this a listing is a list of
    ids with no captions and no media.
    """
    skipped = [
        int(getattr(item, "id", 0) or 0)
        for item in raw
        if type(item).__name__ == "StoryItemSkipped"
    ]
    if not skipped:
        return raw
    resolved = {
        int(getattr(item, "id", 0) or 0): item for item in await _stories_of(ctx, peer, skipped)
    }
    return [resolved.get(int(getattr(item, "id", 0) or 0), item) for item in raw]


async def _translate_captions(ctx: OpContext, items: list[Story], language: str) -> None:
    """Translate captions with the `text=` form — a story has no message id."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    for item in items:
        if not item.caption:
            continue
        result = await client(ctx)(
            fn.TranslateTextRequest(
                to_lang=language, text=[types.TextWithEntities(text=item.caption, entities=[])]
            )
        )
        blocks = getattr(result, "result", None) or []
        if blocks:
            item.translation = str(getattr(blocks[0], "text", "") or "")


SPEC_LIST = OperationSpec(
    id="story.list",
    request=ListReq,
    response=Page[Story],
    impl=list_stories,
    summary="List a peer's stories: active, profile page, archive or one album",
    description=(
        "Four RPCs behind one list, because the GUI shows one grid with tabs. "
        "`--archive` on a channel needs the `edit_stories` admin right."
    ),
    paginated=PageKind.HISTORY,
    columns=("id", "date", "expire_date", "caption"),
    headers=("ID", "Posted", "Expires", "Caption"),
    example={"items": [_EXAMPLE_STORY], "has_more": False},
    example_args="story list @alice",
    covers=(
        "contacts-users.user-stories",
        "stories.album-stories",
        "stories.channel-archive",
        "stories.own-archive",
        "stories.peer-active",
        "stories.profile-stories",
    ),
)


# ---------------------------------------------------------------------------
# story get
# ---------------------------------------------------------------------------


class GetReq(Request):
    chat: Annotated[
        PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story; a story link works too.")
    ]
    id: Annotated[
        list[str], arg(1, metavar="ID", required=False, variadic=True, help="Story ids.")
    ] = []
    link: Annotated[bool, opt("--link", help="Only export the t.me story link.")] = False
    album_link: Annotated[
        int | None, opt("--album-link", metavar="ID", help="Build the album deep link instead.")
    ] = None
    views: Annotated[bool, opt("--views", help="Also fetch fresh view counters.")] = False
    translate: Annotated[
        str | None, opt("--translate", metavar="LANG", help="Translate the caption.")
    ] = None
    areas_out: Annotated[
        str | None,
        opt("--areas-out", metavar="PATH", kind="path", help="Write media_areas as JSON."),
    ] = None


async def get(ctx: OpContext, req: GetReq) -> Page[Story]:
    """Fetch stories in full, or just their links.

    A `t.me/<user>/s/<id>` link may replace the CHAT+ID pair; the peer parser
    keeps the original text, so the id is read straight back off it.
    """
    import msgspec
    from telethon.tl.functions import stories as fn

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    ids = _story.story_ids(req.id)
    linked = _link_story_id(req.chat)
    if not ids and linked is not None:
        ids = [linked]

    if req.album_link is not None:
        username = await _username_of(ctx, peer)
        album = req.album_link
        return Page(
            items=[Story(id=album, peer_id=peer_id, link=f"https://t.me/{username}/a/{album}")],
            has_more=False,
            total=1,
        )

    if not ids:
        raise UsageError("give at least one story id, or a story link", field="id")

    if req.link:
        items = []
        for story_id in ids:
            exported = await client(ctx)(fn.ExportStoryLinkRequest(peer=peer, id=story_id))
            items.append(
                Story(id=story_id, peer_id=peer_id, link=str(getattr(exported, "link", "") or ""))
            )
        return Page(items=items, has_more=False, total=len(items))

    raw = await _stories_of(ctx, peer, ids)
    if not raw:
        raise NotFoundError(f"no story {ids[0]} on that peer")
    items = [_story.story_model(item, peer_id=peer_id) for item in raw]

    if req.views:
        fresh = await client(ctx)(fn.GetStoriesViewsRequest(peer=peer, id=ids))
        for item, views in zip(items, getattr(fresh, "views", None) or [], strict=False):
            item.views = _story.views_model(views)
    if req.translate:
        await _translate_captions(ctx, items, req.translate)
    if req.areas_out:
        areas: list[MediaArea] = [area for item in items for area in item.media_areas]
        target = Path(os.path.expanduser(req.areas_out))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(msgspec.json.format(msgspec.json.encode(areas)))
    return Page(items=items, has_more=False, total=len(items))


async def _username_of(ctx: OpContext, peer: Any) -> str:
    entity = await client(ctx).get_entity(peer)
    username = getattr(entity, "username", None)
    if not username:
        usernames = getattr(entity, "usernames", None) or []
        username = getattr(usernames[0], "username", None) if usernames else None
    if not username:
        raise NotSupportedError(
            "USER_PUBLIC_MISSING: a story link only exists for a peer with a username"
        )
    return str(username)


SPEC_GET = OperationSpec(
    id="story.get",
    request=GetReq,
    response=Page[Story],
    impl=get,
    summary="Fetch stories in full (media, caption, areas, privacy, link)",
    description=(
        "`privacy` is only populated on your own stories. A gone story comes "
        "back with `deleted: true` rather than as an error."
    ),
    columns=("id", "date", "caption", "link"),
    example={"items": [_EXAMPLE_STORY], "has_more": False},
    example_args="story get @alice 42 --views",
    covers=(
        "stories.album-link",
        "stories.caption-entities",
        "stories.get-by-id",
        "stories.link-export",
        "stories.link-resolve",
        "stories.media-areas-inspect",
        "stories.privacy-inspect",
        "stories.repost-origin",
        "stories.skipped-hydrate",
        "stories.translate-caption",
        "stories.viewers-counters",
    ),
)


# ---------------------------------------------------------------------------
# story feed list
# ---------------------------------------------------------------------------


class FeedListReq(Request):
    hidden: Annotated[bool, opt("--hidden", help="The archived stories bar instead.")] = False
    refresh: Annotated[bool, opt("--refresh", help="Re-send the stored state with no `next`.")] = (
        False
    )
    state_file: Annotated[
        str | None,
        opt("--state-file", metavar="PATH", kind="path", help="Where the feed state is kept."),
    ] = None
    peers: Annotated[
        list[PeerRef],
        opt("--peers", metavar="PEER", kind="peer", help="Only the compact max-id summary."),
    ] = []
    read_state: Annotated[
        bool, opt("--read-state", help="Emit the login-time read-state bootstrap instead.")
    ] = False
    unread_only: Annotated[bool, opt("--unread-only", help="Keep only unread peers.")] = False


async def feed_list(ctx: OpContext, req: FeedListReq) -> Page[StoryFeedPeer]:
    """The stories bar.

    Pagination is not offset-based: the first call sends no state, the reply
    carries one, and the walk continues with `state` plus `next`. The cursor
    therefore carries both — an integer offset here would silently restart the
    walk at the top every time.

    The reply also carries the account's stealth mode; `story stealth --status`
    reads it from the same call, because `Page[T]` has no room for a sidecar
    field and inventing one on every row would be worse.
    """
    from telethon.tl.functions import stories as fn

    _limit, state = window(ctx, "story.feed.list", PageKind.DIALOGS, 30)

    if req.peers:
        peers = [await _send.resolve(ctx, ref) for ref in req.peers]
        recent = await client(ctx)(fn.GetPeerMaxIDsRequest(id=peers))
        items = [
            StoryFeedPeer(
                peer_id=_send.peer_id_of(peer),
                max_id=int(getattr(row, "max_id", 0) or 0),
                live=bool(getattr(row, "live", False)),
            )
            for peer, row in zip(peers, recent or [], strict=False)
        ]
        return Page(items=items, has_more=False, total=len(items))

    if req.read_state:
        result = await client(ctx)(fn.GetAllReadPeerStoriesRequest())
        table = _entities(result)
        items = []
        for update in getattr(result, "updates", None) or []:
            if type(update).__name__ != "UpdateReadStories":
                continue
            peer = getattr(update, "peer", None)
            items.append(
                StoryFeedPeer(
                    peer_id=peer_id_of(peer) or 0,
                    peer=_peer_model(peer, table),
                    max_read_id=int(getattr(update, "max_id", 0) or 0),
                )
            )
        return Page(items=items, has_more=False, total=len(items))

    stored = _feed_state(ctx, req)
    token = state.get("state") or (stored if req.refresh else None)
    result = await client(ctx)(
        fn.GetAllStoriesRequest(
            next=bool(state.get("next")) or None,
            hidden=req.hidden or None,
            state=token,
        )
    )
    if type(result).__name__ == "AllStoriesNotModified":
        already(ctx)
        _save_feed_state(ctx, req, getattr(result, "state", "") or "")
        return Page(items=[], has_more=False, total=0)

    table = _entities(result)
    items = []
    for row in getattr(result, "peer_stories", None) or []:
        peer = getattr(row, "peer", None)
        stories = [
            _story.story_model(item, peer_id=peer_id_of(peer) or 0)
            for item in (getattr(row, "stories", None) or [])
        ]
        max_read = int(getattr(row, "max_read_id", 0) or 0)
        unread = [s for s in stories if s.id > max_read]
        item = StoryFeedPeer(
            peer_id=peer_id_of(peer) or 0,
            peer=_peer_model(peer, table),
            max_read_id=max_read,
            stories=stories,
            unread_count=len(unread),
            has_unread=bool(unread),
            live=any(s.live for s in stories),
            hidden=req.hidden,
        )
        if req.unread_only and not item.has_unread:
            continue
        items.append(item)

    feed_state = str(getattr(result, "state", "") or "")
    _save_feed_state(ctx, req, feed_state)
    return build_page(
        items,
        op="story.feed.list",
        kind=PageKind.DIALOGS,
        state={"state": feed_state, "next": True},
        account=ctx.account,
        has_more=bool(getattr(result, "has_more", False)),
        total=getattr(result, "count", None),
    )


def _peer_model(peer: Any, table: dict[int, Any]) -> Any:
    entity = _peer_entity(peer, table)
    return entity_to_peer(entity) if entity is not None else None


def _feed_path(ctx: OpContext, req: FeedListReq) -> Path | None:
    if req.state_file:
        return Path(os.path.expanduser(req.state_file))
    paths = getattr(ctx, "paths", None)
    root = getattr(paths, "cache", None) or getattr(paths, "home", None)
    if root is None:
        return None
    name = "story-feed-hidden.state" if req.hidden else "story-feed.state"
    return Path(root) / f"{ctx.account or 'default'}-{name}"


def _feed_state(ctx: OpContext, req: FeedListReq) -> str | None:
    path = _feed_path(ctx, req)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _save_feed_state(ctx: OpContext, req: FeedListReq, value: str) -> None:
    """Persist the opaque feed state so `--refresh` means something next run."""
    path = _feed_path(ctx, req)
    if path is None or not value:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError as exc:  # a read-only cache must not fail the listing
        ctx.warn(f"could not store the story feed state: {exc}")


SPEC_FEED_LIST = OperationSpec(
    id="story.feed.list",
    request=FeedListReq,
    response=Page[StoryFeedPeer],
    impl=feed_list,
    summary="List peers that have active stories (the stories bar)",
    description=(
        "Main and hidden feeds keep independent states. `--refresh` re-sends "
        "the stored state and reports `already: true` when nothing changed."
    ),
    paginated=PageKind.DIALOGS,
    columns=("peer_id", "unread_count", "max_read_id"),
    headers=("Peer", "Unread", "Read to"),
    example={
        "items": [{"peer_id": 4242, "max_read_id": 41, "unread_count": 1, "has_unread": True}],
        "has_more": False,
    },
    example_args="story feed list --unread-only",
    covers=(
        "stories.changelog-stories",
        "stories.feed-all",
        "stories.feed-hidden",
        "stories.feed-refresh-state",
        "stories.peer-max-ids",
        "stories.read-state-bootstrap",
    ),
)


# ---------------------------------------------------------------------------
# story read
# ---------------------------------------------------------------------------


class ReadReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose stories.")]
    id: Annotated[
        list[str], arg(1, metavar="ID", required=False, variadic=True, help="Story ids.")
    ] = []
    max_id: Annotated[
        int | None, opt("--max-id", metavar="ID", help="Mark everything up to this id.")
    ] = None
    register_view: Annotated[
        bool,
        opt("--register-view", help="Also appear in the poster's viewer list."),
    ] = False


async def read(ctx: OpContext, req: ReadReq) -> StoryRead:
    """Clear the unread ring, and only optionally appear as a viewer.

    `stories.readStories` is private bookkeeping; `incrementStoryViews` is
    what the poster sees. Folding them into one command without a flag would
    make every `story read` a disclosure.
    """
    from telethon.tl.functions import stories as fn

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    ids = _story.story_ids(req.id)
    max_id = req.max_id or (max(ids) if ids else 0)
    if not max_id:
        peer_stories = await client(ctx)(fn.GetPeerStoriesRequest(peer=peer))
        stories = getattr(getattr(peer_stories, "stories", None), "stories", None) or []
        max_id = max((int(getattr(s, "id", 0) or 0) for s in stories), default=0)
    if not max_id:
        raise NotFoundError("that peer has no active stories to mark as read")

    marked = await client(ctx)(fn.ReadStoriesRequest(peer=peer, max_id=max_id))
    read_ids = [int(i) for i in (marked or [])]
    if not read_ids:
        already(ctx)

    viewed: list[int] = []
    if req.register_view and ids:
        await client(ctx)(fn.IncrementStoryViewsRequest(peer=peer, id=ids))
        viewed = ids
    ctx.emit("story_read", {"peer": peer_id, "max_id": max_id})
    return StoryRead(
        peer=peer_id,
        max_id=max_id,
        ids=read_ids,
        already=not read_ids,
        viewed_ids=viewed,
    )


SPEC_READ = OperationSpec(
    id="story.read",
    request=ReadReq,
    response=StoryRead,
    impl=read,
    summary="Mark a peer's stories as seen (clears the unread ring)",
    description=(
        "This does NOT make you appear in the poster's viewer list; "
        "`--register-view` does, and only for the ids you name."
    ),
    aliases=("story.view",),
    mutating=True,
    idempotent=True,
    columns=("peer", "max_id", "ids"),
    example={"peer": 4242, "max_id": 42, "ids": [42], "ok": True},
    example_args="story read @alice",
    covers=("stories.increment-views", "stories.mark-read"),
)


# ---------------------------------------------------------------------------
# story react
# ---------------------------------------------------------------------------


class ReactReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[int, arg(1, metavar="ID", help="Story id.")]
    emoji: Annotated[
        str | None, arg(2, metavar="EMOJI", required=False, help="The reaction to send.")
    ] = None
    remove: Annotated[bool, opt("--remove", help="Clear the reaction.")] = False
    custom_emoji: Annotated[
        int | None, opt("--custom-emoji", metavar="ID", help="Custom-emoji document id.")
    ] = None
    recent: Annotated[
        bool, opt("--recent/--no-recent", help="Add it to the recent-reactions list.")
    ] = True
    as_message: Annotated[
        bool, opt("--as-message", help="Send the emoji as an ordinary story reply instead.")
    ] = False


async def react(ctx: OpContext, req: ReactReq) -> StoryReactionResult:
    """React to a story, or clear the reaction.

    A story carries at most one reaction per viewer — a single `Reaction`,
    not the vector a message has — so this replaces rather than appends.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as msg_fn
    from telethon.tl.functions import stories as fn

    from tlgr.ops.reaction import CUSTOM, to_tl

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    name = ""
    if req.custom_emoji is not None:
        name = f"{CUSTOM}{req.custom_emoji}"
    elif req.emoji:
        name = req.emoji
    if not req.remove and not name:
        raise UsageError("give an emoji, or --remove to clear the reaction", field="emoji")

    if req.as_message:
        if req.remove:
            raise UsageError("--as-message cannot remove a reaction", field="remove")
        sent = await client(ctx)(
            msg_fn.SendMessageRequest(
                peer=peer,
                message=name,
                random_id=random_id(),
                reply_to=types.InputReplyToStory(peer=peer, story_id=req.id),
            )
        )
        message = _send.message_from_updates(sent, chat_id=peer_id, sent_text=name)
        return StoryReactionResult(peer=peer_id, story_id=req.id, reaction=name, msg_id=message.id)

    reaction = types.ReactionEmpty() if req.remove else to_tl(name)
    await client(ctx)(
        fn.SendReactionRequest(
            peer=peer,
            story_id=req.id,
            reaction=reaction,
            add_to_recent=(req.recent and not req.remove) or None,
        )
    )
    ctx.emit("story_reaction", {"peer": peer_id, "story_id": req.id, "reaction": name})
    return StoryReactionResult(
        peer=peer_id, story_id=req.id, reaction="" if req.remove else name, removed=req.remove
    )


SPEC_REACT = OperationSpec(
    id="story.react",
    request=ReactReq,
    response=StoryReactionResult,
    impl=react,
    summary="React to a story, or remove your reaction",
    description="Paid (Star) reactions do not exist on stories.",
    mutating=True,
    rate_class="send",
    idempotent=True,
    tags=frozenset({"visible-to-others"}),
    columns=("peer", "story_id", "reaction"),
    example={"peer": 4242, "story_id": 42, "reaction": "🔥"},
    example_args="story react @alice 42 🔥",
    covers=("stories.react", "stories.reaction-as-message", "stories.unreact"),
)


# ---------------------------------------------------------------------------
# story reply
# ---------------------------------------------------------------------------


class ReplyReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[int, arg(1, metavar="ID", help="Story id.")]
    text: Annotated[
        str | None, arg(2, metavar="TEXT", required=False, help="Reply body; '-' reads stdin.")
    ] = None
    file: Annotated[
        list[str], opt("--file", metavar="PATH", kind="path", help="Attach a file. Repeatable.")
    ] = []
    voice: Annotated[bool, opt("--voice", help="Send the file as a voice note.")] = False
    sticker: Annotated[
        str | None, opt("--sticker", metavar="ID", help="Send a sticker document id.")
    ] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Text formatting.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    schedule: Annotated[
        str | None, opt("--schedule", metavar="TS|online", help="Schedule the reply.")
    ] = None
    paid_stars: Annotated[
        int | None, opt("--paid-stars", metavar="N", help="Agree to the peer's message price.")
    ] = None


async def reply(ctx: OpContext, req: ReplyReq) -> StoryReply:
    """Reply privately to a story.

    The composition is the message group's: this builds `InputReplyToStory`
    and hands it to the same send path, so every send-time flag behaves the
    way it does on `message send` rather than almost the same way.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    text, entities = _send.body(req.text, parse=req.parse, entities=req.entities)
    reply_to = types.InputReplyToStory(peer=peer, story_id=req.id)
    schedule = _send.schedule_at(req.schedule)

    media: Any = None
    if req.sticker:
        if not req.sticker.isdigit():
            raise UsageError("--sticker takes a document id", field="sticker")
        media = types.InputMediaDocument(
            id=types.InputDocument(id=int(req.sticker), access_hash=0, file_reference=b"")
        )
    elif req.file:
        media = await _send.input_media(ctx, req.file[0], voice=req.voice)

    if media is not None:
        updates = await client(ctx)(
            fn.SendMediaRequest(
                peer=peer,
                media=media,
                message=text,
                entities=_send.tl_entities(entities),
                random_id=random_id(),
                reply_to=reply_to,
                silent=req.silent or None,
                schedule_date=schedule,
                allow_paid_stars=req.paid_stars,
            )
        )
    else:
        if not text:
            raise UsageError("give some text, --file or --sticker", field="text")
        updates = await client(ctx)(
            fn.SendMessageRequest(
                peer=peer,
                message=text,
                entities=_send.tl_entities(entities),
                random_id=random_id(),
                reply_to=reply_to,
                silent=req.silent or None,
                schedule_date=schedule,
                allow_paid_stars=req.paid_stars,
            )
        )
    message = _send.message_from_updates(updates, chat_id=peer_id, sent_text=text)
    ctx.emit("story_reply", {"peer": peer_id, "story_id": req.id, "msg_id": message.id})
    return StoryReply(
        chat_id=peer_id,
        msg_id=message.id,
        reply_to_story=req.id,
        text=text,
        message=message,
    )


SPEC_REPLY = OperationSpec(
    id="story.reply",
    request=ReplyReq,
    response=StoryReply,
    impl=reply,
    summary="Reply privately to a story (text, media, voice or sticker)",
    description=(
        "A reply is an ordinary private message carrying `InputReplyToStory`, "
        "so the peer's message restrictions — Premium-only, paid messages, "
        "channel story replies locked — apply exactly as they do to a DM."
    ),
    mutating=True,
    rate_class="send",
    tags=frozenset({"visible-to-others"}),
    columns=("chat_id", "msg_id", "reply_to_story"),
    example={"chat_id": 4242, "msg_id": 12345, "reply_to_story": 42, "text": "nice one"},
    example_args="story reply @alice 42 'nice one'",
    covers=("stories.reply", "stories.reply-media", "stories.reply-restrictions"),
)


# ---------------------------------------------------------------------------
# story share
# ---------------------------------------------------------------------------


class ShareReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[int, arg(1, metavar="ID", help="Story id.")]
    until: Annotated[
        list[PeerRef],
        opt("--until", "--to", metavar="CHAT", kind="peer", help="Destination chat. Repeatable."),
    ] = []
    text: Annotated[str | None, opt("--text", help="Caption to send with the card.")] = None
    silent: Annotated[bool, opt("--silent", help="Send without a notification.")] = False
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Forum topic id.")
    ] = None


async def share(ctx: OpContext, req: ShareReq) -> StoryShared:
    """Share a story into chats as a story card.

    Not `forwardMessages`: the receiving message carries `messageMediaStory`,
    which is what makes it render as a story rather than as a copy of its
    media. Refused when the story is `noforwards`.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    if not req.until:
        raise UsageError("give at least one --until CHAT", field="until")

    source = await _require_own_story(ctx, peer, req.id)
    if getattr(source, "noforwards", False):
        raise PermissionError_(
            f"story {req.id} is protected against forwarding; "
            f"`tlgr story get {req.chat.raw} {req.id} --link` shares a link instead"
        )

    sent: list[Message] = []
    for destination in req.until:
        target = await _send.resolve(ctx, destination)
        updates = await client(ctx)(
            fn.SendMediaRequest(
                peer=target,
                media=types.InputMediaStory(peer=peer, id=req.id),
                message=req.text or "",
                random_id=random_id(),
                silent=req.silent or None,
                reply_to=types.InputReplyToMessage(reply_to_msg_id=req.topic)
                if req.topic
                else None,
            )
        )
        sent.append(_send.message_from_updates(updates, chat_id=_send.peer_id_of(target)))
    ctx.emit("story_shared", {"peer": peer_id, "story_id": req.id, "count": len(sent)})
    return StoryShared(sent=sent, story_id=req.id, peer=peer_id)


SPEC_SHARE = OperationSpec(
    id="story.share",
    request=ShareReq,
    response=StoryShared,
    impl=share,
    summary="Share a story into chats as a story card",
    aliases=("story.forward",),
    mutating=True,
    rate_class="send",
    tags=frozenset({"visible-to-others"}),
    columns=("story_id", "peer"),
    example={
        "story_id": 42,
        "peer": 4242,
        "sent": [
            {
                "id": 12345,
                "chat_id": 777123,
                "date": "2026-09-03T09:20:00Z",
                "date_unix": 1788427200,
            }
        ],
    },
    example_args="story share @alice 42 --until @bobby",
    covers=("stories.share-to-chat",),
)


# ---------------------------------------------------------------------------
# story pin / unpin
# ---------------------------------------------------------------------------


class PinReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose stories.")]
    id: Annotated[
        list[str], arg(1, metavar="ID", required=False, variadic=True, help="Story ids.")
    ] = []
    top: Annotated[bool, opt("--top", help="Pin to the top of the profile grid instead.")] = False


async def _toggle_pinned(ctx: OpContext, req: PinReq, *, pinned: bool) -> StoryPinned:
    from telethon.tl.functions import stories as fn

    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    ids = _story.story_ids(req.id)

    if req.top:
        # `togglePinnedToTop` replaces the whole set, so an empty vector is
        # how the pinned-to-top row is cleared.
        if pinned and not ids:
            raise UsageError("--top needs the ids to pin to the top", field="id")
        order = ids if pinned else []
        await client(ctx)(fn.TogglePinnedToTopRequest(peer=peer, id=order))
        return StoryPinned(peer=peer_id, ids=ids, pinned=pinned, pinned_to_top=order)

    if not ids:
        raise UsageError("give at least one story id", field="id")
    changed = await client(ctx)(fn.TogglePinnedRequest(peer=peer, id=ids, pinned=pinned))
    if not changed:
        already(ctx)
    return StoryPinned(peer=peer_id, ids=[int(i) for i in (changed or [])], pinned=pinned)


async def pin(ctx: OpContext, req: PinReq) -> StoryPinned:
    """Keep stories on the profile page, or pin them to the top of the grid.

    "Pinned" here means "shown on the profile page", not "first in the grid" —
    that is `--top`, whose RPC replaces the whole pinned-to-top set.
    """
    return await _toggle_pinned(ctx, req, pinned=True)


async def unpin(ctx: OpContext, req: PinReq) -> StoryPinned:
    """Move stories off the profile page, or clear the pinned-to-top set."""
    return await _toggle_pinned(ctx, req, pinned=False)


SPEC_PIN = OperationSpec(
    id="story.pin",
    request=PinReq,
    response=StoryPinned,
    impl=pin,
    summary="Keep stories on the profile page, or pin them to the top",
    mutating=True,
    idempotent=True,
    columns=("peer", "ids", "pinned"),
    example={"peer": 4242, "ids": [42], "pinned": True},
    example_args="story pin me 42",
    covers=("stories.pin-to-top",),
    covers_partial=("stories.pin-to-profile",),
    coverage_note="`story unpin` owns the other half of the profile-page toggle.",
)

SPEC_UNPIN = OperationSpec(
    id="story.unpin",
    request=PinReq,
    response=StoryPinned,
    impl=unpin,
    summary="Move stories off the profile page, or clear the pinned-to-top set",
    mutating=True,
    idempotent=True,
    columns=("peer", "ids", "pinned"),
    example={"peer": 4242, "ids": [42], "pinned": False},
    example_args="story unpin me 42",
    covers=("stories.pin-to-profile",),
    covers_partial=("stories.pin-to-top",),
    coverage_note="`story pin` owns the other half of the pinned-to-top set.",
)


# ---------------------------------------------------------------------------
# story hide / unhide
# ---------------------------------------------------------------------------


class HideReq(Request):
    chat: Annotated[
        list[PeerRef],
        arg(0, metavar="CHAT", variadic=True, kind="peer", help="Whose stories to hide."),
    ] = []
    every: Annotated[bool, opt("--all", help="Collapse the whole stories bar.")] = False
    unhide: Annotated[
        bool, opt("--unhide", help="Put them back instead (v1's `user hide-stories --unhide`).")
    ] = False


async def _toggle_one(ctx: OpContext, ref: PeerRef, *, hidden: bool) -> StoryHiddenPeer:
    from telethon.tl.functions import stories as fn

    peer = await _send.resolve(ctx, ref)
    peer_id = _send.peer_id_of(peer)
    entity = await client(ctx).get_entity(peer)
    was = bool(getattr(entity, "stories_hidden", False))
    row = StoryHiddenPeer(
        user_id=peer_id if peer_id > 0 else 0,
        username=getattr(entity, "username", None),
        peer_id=peer_id,
        hidden=hidden,
        # v1 detected this and sent nothing, so a bulk pass is cheap to repeat.
        already=was == hidden,
    )
    if not row.already:
        await client(ctx)(fn.TogglePeerStoriesHiddenRequest(peer=peer, hidden=hidden))
        ctx.emit("story_peer_hidden", {"peer": peer_id, "hidden": hidden})
    return row


async def _toggle_hidden(ctx: OpContext, req: HideReq, *, hidden: bool) -> StoryHidden:
    from telethon.tl.functions import stories as fn

    result = StoryHidden(hidden=hidden)
    if req.every:
        await client(ctx)(fn.ToggleAllStoriesHiddenRequest(hidden=hidden))
        result.all = True
        if not req.chat:
            return result
    if not req.chat:
        raise UsageError("give a peer, or --all for the whole stories bar", field="chat")

    rows = [await _toggle_one(ctx, ref, hidden=hidden) for ref in req.chat]
    first = rows[0]
    result.user_id = first.user_id
    result.username = first.username
    result.peer_id = first.peer_id
    result.already = first.already
    if len(rows) > 1:
        result.peers = rows
    if all(row.already for row in rows):
        already(ctx)
    return result


async def hide(ctx: OpContext, req: HideReq) -> StoryHidden:
    """Move a peer's stories to the archive bar, or collapse the whole bar.

    Per-account and purely local: the other side is never told, and nothing
    about the chat, the contact or their access changes. Idempotent —
    `already: true` means the flag was already set and no RPC was sent.
    """
    return await _toggle_hidden(ctx, req, hidden=not req.unhide)


async def unhide(ctx: OpContext, req: HideReq) -> StoryHidden:
    """Put a peer's stories back in the main bar. The inverse of `story hide`."""
    return await _toggle_hidden(ctx, req, hidden=req.unhide)


SPEC_HIDE = OperationSpec(
    id="story.hide",
    request=HideReq,
    response=StoryHidden,
    impl=hide,
    summary="Hide a peer's stories, or hide the whole stories bar",
    description=(
        "v1 spelled this `tlgr user hide-stories`, and that path still works "
        "— including its `--unhide` flag, which is `story unhide` said the "
        "other way round."
    ),
    legacy_paths=("user hide-stories",),
    mutating=True,
    idempotent=True,
    columns=("user_id", "username", "hidden", "already"),
    example={"user_id": 4242, "username": "alice", "hidden": True, "already": False},
    example_args="story hide @alice",
    covers=(
        "contacts-users.user-hide-stories",
        "dialogs.hide-stories-peer",
        "groups-channels-admin.hide-peer-stories",
    ),
    covers_partial=("stories.hide-all", "stories.hide-peer"),
    coverage_note="`story unhide` owns the other half of both toggles.",
)

SPEC_UNHIDE = OperationSpec(
    id="story.unhide",
    request=HideReq,
    response=StoryHidden,
    impl=unhide,
    summary="Put a peer's stories back in the main bar",
    mutating=True,
    idempotent=True,
    columns=("user_id", "username", "hidden", "already"),
    example={"user_id": 4242, "username": "alice", "hidden": False, "already": False},
    example_args="story unhide @alice",
    covers=("stories.hide-all", "stories.hide-peer"),
)


# ---------------------------------------------------------------------------
# story album
# ---------------------------------------------------------------------------


class AlbumCreateReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose profile.")]
    title: Annotated[str, arg(1, metavar="TITLE", help="Album title (1-12 characters).")]
    story: Annotated[
        list[int], opt("--story", metavar="ID", help="Story to put in it. Repeatable.")
    ] = []


async def album_create(ctx: OpContext, req: AlbumCreateReq) -> StoryAlbum:
    """Create a profile album. Channel albums need the `edit_stories` right."""
    from telethon.tl.functions import stories as fn

    if not 1 <= len(req.title) <= 12:
        raise UsageError("an album title is 1 to 12 characters", field="title")
    if not req.story:
        raise UsageError("an album needs at least one --story", field="story")
    peer = await _send.resolve(ctx, req.chat)
    album = await client(ctx)(
        fn.CreateAlbumRequest(peer=peer, title=req.title, stories=list(req.story))
    )
    return _story.album_model(album, stories=list(req.story))


SPEC_ALBUM_CREATE = OperationSpec(
    id="story.album.create",
    request=AlbumCreateReq,
    response=StoryAlbum,
    impl=album_create,
    summary="Create a story album",
    mutating=True,
    columns=("id", "title", "stories"),
    example={"id": 7, "title": "Trips", "stories": [42]},
    example_args="story album create me Trips --story 42",
    covers=("stories.album-create",),
)


class AlbumDeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose profile.")]
    album_id: Annotated[int, arg(1, metavar="ALBUM_ID", help="Album id.")]


async def album_delete(ctx: OpContext, req: AlbumDeleteReq) -> AlbumDeleted:
    """Delete an album. The stories inside it stay."""
    from telethon.tl.functions import stories as fn

    peer = await _send.resolve(ctx, req.chat)
    await client(ctx)(fn.DeleteAlbumRequest(peer=peer, album_id=req.album_id))
    return AlbumDeleted(peer=_send.peer_id_of(peer), album_id=req.album_id)


SPEC_ALBUM_DELETE = OperationSpec(
    id="story.album.delete",
    request=AlbumDeleteReq,
    response=AlbumDeleted,
    impl=album_delete,
    summary="Delete an album (the stories stay)",
    mutating=True,
    destructive=True,
    columns=("peer", "album_id", "ok"),
    example={"peer": 4242, "album_id": 7, "ok": True},
    example_args="story album delete me 7",
    covers=("stories.album-delete",),
)


class AlbumEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose profile.")]
    album_id: Annotated[int, arg(1, metavar="ALBUM_ID", help="Album id.")]
    title: Annotated[str | None, opt("--title", help="New album title (1-12 chars).")] = None
    add: Annotated[list[int], opt("--add", metavar="ID", help="Story to add. Repeatable.")] = []
    remove: Annotated[
        list[int], opt("--remove", metavar="ID", help="Story to remove. Repeatable.")
    ] = []
    order: Annotated[
        list[int], opt("--order", metavar="ID", help="Full story order inside the album.")
    ] = []


async def album_edit(ctx: OpContext, req: AlbumEditReq) -> StoryAlbum:
    """Rename an album, add or remove stories, or reorder the ones inside it.

    One RPC (`stories.updateAlbum`) backs all four GUI actions, which is why
    they are one command with four flags rather than four near-identical ones.
    """
    from telethon.tl.functions import stories as fn

    if req.title is not None and not 1 <= len(req.title) <= 12:
        raise UsageError("an album title is 1 to 12 characters", field="title")
    if not any((req.title, req.add, req.remove, req.order)):
        raise UsageError(
            "nothing to change; pass --title, --add, --remove or --order", field="album_id"
        )
    peer = await _send.resolve(ctx, req.chat)
    album = await client(ctx)(
        fn.UpdateAlbumRequest(
            peer=peer,
            album_id=req.album_id,
            title=req.title,
            delete_stories=list(req.remove) or None,
            add_stories=list(req.add) or None,
            order=list(req.order) or None,
        )
    )
    return _story.album_model(album, stories=list(req.order) or list(req.add))


SPEC_ALBUM_EDIT = OperationSpec(
    id="story.album.edit",
    request=AlbumEditReq,
    response=StoryAlbum,
    impl=album_edit,
    summary="Rename an album, add/remove stories, or reorder the stories inside it",
    mutating=True,
    columns=("id", "title", "stories"),
    example={"id": 7, "title": "Trips 2026", "stories": [42, 43]},
    example_args="story album edit me 7 --title 'Trips 2026'",
    covers=(
        "stories.album-add-stories",
        "stories.album-remove-stories",
        "stories.album-rename",
        "stories.album-reorder-stories",
    ),
)


class AlbumListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose profile.")]
    hash: Annotated[
        int | None, opt("--hash", metavar="N", help="Cache hash; unchanged answers `already`.")
    ] = None


async def album_list(ctx: OpContext, req: AlbumListReq) -> Page[StoryAlbum]:
    """List the story albums on a profile."""
    from telethon.tl.functions import stories as fn

    limit, _state = window(ctx, "story.album.list", PageKind.LOCAL, 30)
    peer = await _send.resolve(ctx, req.chat)
    result = await client(ctx)(fn.GetAlbumsRequest(peer=peer, hash=req.hash or 0))
    if type(result).__name__ == "AlbumsNotModified":
        already(ctx)
        return Page(items=[], has_more=False, total=0)
    albums = [_story.album_model(album) for album in (getattr(result, "albums", None) or [])]
    return Page(items=albums[:limit], has_more=len(albums) > limit, total=len(albums))


SPEC_ALBUM_LIST = OperationSpec(
    id="story.album.list",
    request=AlbumListReq,
    response=Page[StoryAlbum],
    impl=album_list,
    summary="List the story albums on a profile",
    description="Open one with `story list PEER --album ID`.",
    paginated=PageKind.LOCAL,
    columns=("id", "title", "stories_count"),
    headers=("ID", "Title", "Stories"),
    example={"items": [{"id": 7, "title": "Trips"}], "has_more": False},
    example_args="story album list me",
    covers=("stories.album-list",),
)


class AlbumReorderReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose profile.")]
    album_id: Annotated[
        list[int], arg(1, metavar="ALBUM_ID", variadic=True, help="Albums, in the new order.")
    ] = []


async def album_reorder(ctx: OpContext, req: AlbumReorderReq) -> AlbumOrder:
    """Reorder the album chips. A full-replace vector, like every Telegram order."""
    from telethon.tl.functions import stories as fn

    if not req.album_id:
        raise UsageError("give the album ids in the order you want them", field="album_id")
    peer = await _send.resolve(ctx, req.chat)
    await client(ctx)(fn.ReorderAlbumsRequest(peer=peer, order=list(req.album_id)))
    return AlbumOrder(peer=_send.peer_id_of(peer), order=list(req.album_id))


SPEC_ALBUM_REORDER = OperationSpec(
    id="story.album.reorder",
    request=AlbumReorderReq,
    response=AlbumOrder,
    impl=album_reorder,
    summary="Reorder the album chips on the profile",
    mutating=True,
    columns=("peer", "order"),
    example={"peer": 4242, "order": [8, 7]},
    example_args="story album reorder me 8 7",
    covers=("stories.album-reorder",),
)


# ---------------------------------------------------------------------------
# story blocklist
# ---------------------------------------------------------------------------


class BlocklistListReq(Request):
    pass


async def blocklist_list(ctx: OpContext, req: BlocklistListReq) -> Page[BlockedStoryUser]:
    """ "Hide my stories from" — a second blocklist, independent of `user block`."""
    from telethon.tl.functions import contacts as fn

    limit, state = window(ctx, "story.blocklist.list", PageKind.PARTICIPANTS, 30)
    offset = int(state.get("offset") or 0)
    result = await client(ctx)(
        fn.GetBlockedRequest(offset=offset, limit=limit, my_stories_from=True)
    )
    users = {int(u.id): u for u in (getattr(result, "users", None) or [])}
    items: list[BlockedStoryUser] = []
    for row in getattr(result, "blocked", None) or []:
        raw_id = peer_id_of(getattr(row, "peer_id", None)) or 0
        user = users.get(abs(raw_id))
        items.append(
            BlockedStoryUser(
                user_id=raw_id,
                username=getattr(user, "username", None),
                name=" ".join(
                    part
                    for part in (
                        getattr(user, "first_name", None),
                        getattr(user, "last_name", None),
                    )
                    if part
                ),
                date=fmt_dt(getattr(row, "date", None)),
                date_unix=to_unix(getattr(row, "date", None)),
            )
        )
    return build_page(
        items,
        op="story.blocklist.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(items)},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_BLOCKLIST_LIST = OperationSpec(
    id="story.blocklist.list",
    request=BlocklistListReq,
    response=Page[BlockedStoryUser],
    impl=blocklist_list,
    summary="List the users who never see your stories",
    description="A second, independent blocklist; `user block` stays the global one.",
    paginated=PageKind.PARTICIPANTS,
    columns=("user_id", "username", "name"),
    headers=("ID", "Username", "Name"),
    example={"items": [{"user_id": 4242, "username": "alice", "name": "Alice"}], "has_more": False},
    example_args="story blocklist list",
    covers_partial=("stories.blocklist",),
    coverage_note="`story blocklist set` owns the writing half of the list.",
)


class BlocklistSetReq(Request):
    user: Annotated[
        list[UserRef], arg(0, metavar="USER", variadic=True, kind="user", help="Users.")
    ] = []
    remove: Annotated[bool, opt("--remove", help="Remove them from the list instead.")] = False
    replace: Annotated[
        bool, opt("--replace", help="Replace the whole list with exactly these users.")
    ] = False


async def blocklist_set(ctx: OpContext, req: BlocklistSetReq) -> BlocklistChange:
    """Add to, remove from or replace the story blocklist.

    `--replace` is one RPC that overwrites the list; `--add`/`--remove` are
    per-user and idempotent, which is what makes a bulk pass safe to repeat.
    """
    from telethon.tl.functions import contacts as fn

    if not req.user:
        raise UsageError("name at least one user", field="user")
    peers = [await _send.resolve(ctx, ref) for ref in req.user]
    ids = [_send.peer_id_of(peer) for peer in peers]

    if req.replace:
        await client(ctx)(fn.SetBlockedRequest(id=peers, limit=len(peers), my_stories_from=True))
        return BlocklistChange(added=ids, total=len(ids))

    changed: list[int] = []
    for peer, peer_id in zip(peers, ids, strict=True):
        request = (
            fn.UnblockRequest(id=peer, my_stories_from=True)
            if req.remove
            else fn.BlockRequest(id=peer, my_stories_from=True)
        )
        if await client(ctx)(request):
            changed.append(peer_id)
    if not changed:
        already(ctx)
    return BlocklistChange(
        added=[] if req.remove else changed,
        removed=changed if req.remove else [],
        already=not changed,
    )


SPEC_BLOCKLIST_SET = OperationSpec(
    id="story.blocklist.set",
    request=BlocklistSetReq,
    response=BlocklistChange,
    impl=blocklist_set,
    summary="Add to, remove from or replace the story blocklist",
    mutating=True,
    idempotent=True,
    columns=("added", "removed", "total"),
    example={"added": [4242], "removed": [], "total": 1},
    example_args="story blocklist set @alice",
    covers=("dialogs.block-stories", "stories.blocklist"),
)


# ---------------------------------------------------------------------------
# story viewer list
# ---------------------------------------------------------------------------


class ViewerListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[int, arg(1, metavar="ID", help="Story id.")]
    contacts: Annotated[bool, opt("--contacts", help="Contacts only.")] = False
    reactions_first: Annotated[
        bool, opt("--reactions-first", help="Sort viewers who reacted first.")
    ] = False
    forwards_first: Annotated[
        bool, opt("--forwards-first", help="Sort reposts and forwards first.")
    ] = False
    q: Annotated[str | None, opt("--q", metavar="TEXT", help="Server-side name search.")] = None
    reaction: Annotated[
        str | None, opt("--reaction", metavar="EMOJI", help="Channel stories: only this reaction.")
    ] = None
    csv_out: Annotated[
        str | None, opt("--csv", metavar="PATH", kind="path", help="Also write the rows as CSV.")
    ] = None
    hide_from: Annotated[
        list[UserRef],
        opt("--hide-from", metavar="USER", kind="user", help="Add a viewer to the blocklist."),
    ] = []


def _viewer_row(row: Any, table: dict[int, Any]) -> StoryViewer:
    from tlgr.ops._serialize import entity_to_peer as to_peer
    from tlgr.ops.reaction import name_of

    name = type(row).__name__
    if name in ("StoryView", "StoryReaction"):
        raw_id = int(getattr(row, "user_id", 0) or 0) or (
            peer_id_of(getattr(row, "peer_id", None)) or 0
        )
        reaction = getattr(row, "reaction", None)
        return StoryViewer(
            kind="view",
            user_id=raw_id,
            date=fmt_dt(getattr(row, "date", None)),
            date_unix=to_unix(getattr(row, "date", None)),
            reaction=name_of(reaction) if reaction is not None else None,
            blocked=bool(getattr(row, "blocked", False)),
            blocked_my_stories_from=bool(getattr(row, "blocked_my_stories_from", False)),
        )
    if name in ("StoryViewPublicForward", "StoryReactionPublicForward"):
        message = getattr(row, "message", None)
        return StoryViewer(
            kind="forward",
            user_id=peer_id_of(getattr(message, "peer_id", None)) or 0,
            msg_id=int(getattr(message, "id", 0) or 0),
            blocked=bool(getattr(row, "blocked", False)),
        )
    story = getattr(row, "story", None)
    peer = getattr(row, "peer_id", None)
    entity = _peer_entity(peer, table)
    return StoryViewer(
        kind="repost",
        user_id=peer_id_of(peer) or 0,
        peer=to_peer(entity) if entity is not None else None,
        story_id=int(getattr(story, "id", 0) or 0),
        blocked=bool(getattr(row, "blocked", False)),
    )


async def viewer_list(ctx: OpContext, req: ViewerListReq) -> Page[StoryViewer]:
    """Who saw a story, with their reactions.

    Your own user stories go through `getStoryViewsList`; a channel story you
    administer only has `getStoryReactionsList`, which knows about reactions,
    forwards and reposts but not about plain views. The RPC is chosen from the
    peer type and `--reaction` forces the second one, because reporting an
    empty viewer list for a channel story would read as "nobody watched".
    """
    from telethon.tl.functions import stories as fn

    limit, state = window(ctx, "story.viewer.list", PageKind.PARTICIPANTS, 30)
    peer = await _send.resolve(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    offset = str(state.get("offset") or "")

    if req.hide_from:
        await blocklist_set(ctx, BlocklistSetReq(user=list(req.hide_from)))

    reactions_only = req.reaction is not None or peer_id < 0
    if reactions_only:
        from tlgr.ops.reaction import to_tl

        result = await client(ctx)(
            fn.GetStoryReactionsListRequest(
                peer=peer,
                id=req.id,
                limit=limit,
                forwards_first=req.forwards_first or None,
                reaction=to_tl(req.reaction) if req.reaction else None,
                offset=offset or None,
            )
        )
        rows = getattr(result, "reactions", None) or []
    else:
        result = await client(ctx)(
            fn.GetStoryViewsListRequest(
                peer=peer,
                id=req.id,
                offset=offset,
                limit=limit,
                just_contacts=req.contacts or None,
                reactions_first=req.reactions_first or None,
                forwards_first=req.forwards_first or None,
                q=req.q,
            )
        )
        rows = getattr(result, "views", None) or []

    table = _entities(result)
    items = [_viewer_row(row, table) for row in rows]
    ctx.warn(
        "source: stories.getStoryReactionsList (channel stories have no plain view rows)"
        if reactions_only
        else "source: stories.getStoryViewsList"
    )
    if req.csv_out:
        _write_csv(req.csv_out, items)

    next_offset = getattr(result, "next_offset", None)
    return build_page(
        items,
        op="story.viewer.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


def _write_csv(path: str, items: list[StoryViewer]) -> None:
    """The export the GUI has no button for."""
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "username", "name", "date", "reaction", "blocked", "kind"])
        for item in items:
            user = item.user
            writer.writerow(
                [
                    item.user_id,
                    getattr(user, "username", "") or "",
                    getattr(user, "title", "") or "",
                    item.date or "",
                    item.reaction or "",
                    int(item.blocked),
                    item.kind,
                ]
            )


SPEC_VIEWER_LIST = OperationSpec(
    id="story.viewer.list",
    request=ViewerListReq,
    response=Page[StoryViewer],
    impl=viewer_list,
    summary="Who saw a story, with their reactions",
    description=(
        "A non-Premium account loses the list `story_viewers_expire_period` "
        "seconds after the story expires; `views.has_viewers` on the story "
        "says whether it is still available."
    ),
    paginated=PageKind.PARTICIPANTS,
    tags=frozenset({"mutating-checked"}),
    columns=("user_id", "date", "reaction", "kind"),
    headers=("User", "Seen", "Reaction", "Kind"),
    example={
        "items": [{"user_id": 4242, "date": "2026-09-03T10:00:00Z", "reaction": "🔥"}],
        "has_more": False,
    },
    example_args="story viewer list me 42",
    covers=(
        "reaction.story-list",
        "stories.channel-story-interactions",
        "stories.viewer-block",
        "stories.viewers-export",
        "stories.viewers-filters",
        "stories.viewers-list",
        "stories.viewers-search",
    ),
)


# ---------------------------------------------------------------------------
# story stealth
# ---------------------------------------------------------------------------


class StealthSetReq(Request):
    past: Annotated[bool, opt("--past", help="Erase your views from the recent window.")] = False
    future: Annotated[bool, opt("--future", help="Hide your views for the next window.")] = False
    status: Annotated[bool, opt("--status", help="Only report the state and do nothing.")] = False


async def stealth_set(ctx: OpContext, req: StealthSetReq) -> StealthMode:
    """Stealth mode: erase recent views and/or hide the next ones.

    Premium only, and rate limited by its own cooldown. A `FLOOD_WAIT` here is
    the cooldown rather than a server complaint, so it is reported as the
    remaining cooldown instead of as a raw error.
    """
    from telethon.tl.functions import stories as fn

    if req.status or not (req.past or req.future):
        feed = await client(ctx)(fn.GetAllStoriesRequest())
        return _story.stealth_model(getattr(feed, "stealth_mode", None))

    from telethon.errors import FloodWaitError

    try:
        await client(ctx)(
            fn.ActivateStealthModeRequest(past=req.past or None, future=req.future or None)
        )
    except FloodWaitError as exc:
        raise PermissionError_(f"stealth mode is still cooling down; {exc.seconds}s left") from exc
    feed = await client(ctx)(fn.GetAllStoriesRequest())
    mode = _story.stealth_model(
        getattr(feed, "stealth_mode", None), past=req.past, future=req.future
    )
    ctx.emit("story_stealth", {"active_until": mode.active_until_unix})
    return mode


SPEC_STEALTH_SET = OperationSpec(
    id="story.stealth.set",
    request=StealthSetReq,
    response=StealthMode,
    impl=stealth_set,
    summary="Stealth mode: erase recent views and/or hide the next ones",
    description=(
        "`--status` reads the state out of the feed reply, which is also "
        "where `story feed list` gets it from."
    ),
    mutating=True,
    columns=("active_until_date", "cooldown_until_date"),
    headers=("Active until", "Cooldown until"),
    example={"active_until_date": "2026-09-03T09:39:07Z", "past": True, "future": True},
    example_args="story stealth set --past --future",
    covers=("stories.stealth-activate", "stories.stealth-status"),
)


# ---------------------------------------------------------------------------
# story search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    hashtag: Annotated[
        str | None, opt("--hashtag", metavar="TAG", help="Hashtag or cashtag, without the #.")
    ] = None
    venue: Annotated[
        str | None, opt("--venue", metavar="PROVIDER:VENUE_ID", help="Search by venue area.")
    ] = None
    geo: Annotated[
        str | None, opt("--geo", metavar="LAT,LON", help="Search by geo area (needs --address).")
    ] = None
    address: Annotated[
        str | None,
        opt("--address", metavar="CC[,state,city,street]", help="Address attached to --geo."),
    ] = None
    peer: Annotated[
        PeerRef | None,
        opt("--peer", metavar="PEER", kind="peer", help="Only this poster."),
    ] = None


async def search(ctx: OpContext, req: SearchReq) -> Page[Story]:
    """Search public stories by hashtag or location.

    Only "Everyone" stories are searchable, so an empty result means "nothing
    public matched", not "nothing exists".
    """
    from telethon.tl import types
    from telethon.tl.functions import stories as fn

    limit, state = window(ctx, "story.search", PageKind.SEARCH, 30)
    given = [
        name
        for name, value in (("hashtag", req.hashtag), ("venue", req.venue), ("geo", req.geo))
        if value
    ]
    if len(given) != 1:
        raise UsageError(
            "give exactly one of --hashtag, --venue or --geo",
            field=given[0] if given else "hashtag",
        )

    area: Any = None
    coordinates = types.MediaAreaCoordinates(x=0.0, y=0.0, w=0.0, h=0.0, rotation=0.0)
    if req.venue:
        provider, _, venue_id = req.venue.partition(":")
        if not venue_id:
            raise UsageError("--venue takes PROVIDER:VENUE_ID", field="venue")
        area = types.MediaAreaVenue(
            coordinates=coordinates,
            geo=types.GeoPoint(long=0.0, lat=0.0, access_hash=0),
            title="",
            address="",
            provider=provider,
            venue_id=venue_id,
            venue_type="",
        )
    elif req.geo:
        if not req.address:
            raise UsageError("--geo is only searchable with an --address", field="address")
        lat, _, lon = req.geo.partition(",")
        parts = [p.strip() for p in req.address.split(",")]
        area = types.MediaAreaGeoPoint(
            coordinates=coordinates,
            geo=types.GeoPoint(long=float(lon), lat=float(lat), access_hash=0),
            address=types.GeoPointAddress(
                country_iso2=parts[0],
                state=parts[1] if len(parts) > 1 else None,
                city=parts[2] if len(parts) > 2 else None,
                street=parts[3] if len(parts) > 3 else None,
            ),
        )

    peer = await _send.resolve(ctx, req.peer) if req.peer is not None else None
    result = await client(ctx)(
        fn.SearchPostsRequest(
            offset=str(state.get("offset") or ""),
            limit=limit,
            hashtag=req.hashtag,
            area=area,
            peer=peer,
        )
    )
    table = _entities(result)
    items = []
    for found in getattr(result, "stories", None) or []:
        found_peer = getattr(found, "peer", None)
        story = _story.story_model(
            getattr(found, "story", None),
            peer_id=peer_id_of(found_peer) or 0,
            peer=_peer_entity(found_peer, table),
        )
        items.append(story)
    next_offset = getattr(result, "next_offset", None)
    return build_page(
        items,
        op="story.search",
        kind=PageKind.SEARCH,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
        total=getattr(result, "count", None),
    )


SPEC_SEARCH = OperationSpec(
    id="story.search",
    request=SearchReq,
    response=Page[Story],
    impl=search,
    summary="Search public stories by hashtag or location",
    paginated=PageKind.SEARCH,
    rate_class="resolve",
    columns=("peer_id", "id", "date", "caption"),
    example={"items": [_EXAMPLE_STORY], "has_more": False},
    example_args="story search --hashtag berlin",
    covers=(
        "messages-core.search-hashtag-stories",
        "stories.search-hashtag",
        "stories.search-location",
        "stories.search-peer-scoped",
    ),
)


# ---------------------------------------------------------------------------
# story report
# ---------------------------------------------------------------------------


class ReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[list[str], arg(1, metavar="ID", variadic=True, help="Story ids.")] = []
    option: Annotated[
        str | None, opt("--option", metavar="B64", help="Opaque option bytes from the last step.")
    ] = None
    message: Annotated[str | None, opt("--message", help="Free-text comment, when asked for.")] = (
        None
    )


async def report(ctx: OpContext, req: ReportReq) -> StoryReport:
    """Report a story. Multi-step: an empty `--option` starts the flow.

    The server answers with a menu (`reportResultChooseOption`) or a request
    for a comment, and `--json` makes both scriptable — the legacy
    `inputReportReason*` constructors no longer exist.
    """
    import base64

    from telethon.tl.functions import stories as fn

    ids = _story.story_ids(req.id)
    if not ids:
        raise UsageError("give at least one story id", field="id")
    peer = await _send.resolve(ctx, req.chat)
    option = base64.b64decode(req.option) if req.option else b""
    result = await client(ctx)(
        fn.ReportRequest(peer=peer, id=ids, option=option, message=req.message or "")
    )
    name = type(result).__name__
    if name == "ReportResultChooseOption":
        return StoryReport(
            result="choose_option",
            title=str(getattr(result, "title", "") or ""),
            options=[
                {
                    "text": str(getattr(item, "text", "") or ""),
                    "option": base64.b64encode(getattr(item, "option", b"")).decode(),
                }
                for item in (getattr(result, "options", None) or [])
            ],
        )
    if name == "ReportResultAddComment":
        return StoryReport(
            result="add_comment",
            comment_required=not bool(getattr(result, "optional", False)),
            options=[{"option": base64.b64encode(getattr(result, "option", b"") or b"").decode()}],
        )
    return StoryReport(result="reported", reported=True)


SPEC_REPORT = OperationSpec(
    id="story.report",
    request=ReportReq,
    response=StoryReport,
    impl=report,
    summary="Report a story",
    mutating=True,
    columns=("result", "title", "comment_required"),
    example={"result": "choose_option", "title": "What is wrong?", "options": []},
    example_args="story report @alice 42",
    covers=("stories.report",),
)


# ---------------------------------------------------------------------------
# story stats
# ---------------------------------------------------------------------------


class StatsGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Whose story.")]
    id: Annotated[int, arg(1, metavar="ID", help="Story id.")]
    forwards: Annotated[
        bool, opt("--forwards", help="List the public reposts instead of the graphs.")
    ] = False
    dark: Annotated[bool, opt("--dark", help="Ask for the dark-theme graph variant.")] = False
    raw: Annotated[bool, opt("--raw", help="Emit the raw StatsGraph JSON.")] = False


async def _graph(ctx: OpContext, graph: Any, *, dark: bool, raw: bool) -> dict[str, Any] | None:
    """Resolve a `statsGraphAsync` token before reporting it.

    An async graph is a token, not data; handing the token to the caller would
    make `story stats` return something nobody can plot.
    """
    import json as jsonlib

    from telethon.tl.functions import stats as fn

    if graph is None:
        return None
    if type(graph).__name__ == "StatsGraphAsync":
        graph = await client(ctx)(
            fn.LoadAsyncGraphRequest(token=str(getattr(graph, "token", "")), x=1 if dark else None)
        )
    if type(graph).__name__ == "StatsGraphError":
        return {"error": str(getattr(graph, "error", ""))}
    payload = getattr(getattr(graph, "json", None), "data", None)
    if payload is None:
        return None
    if raw:
        return {"json": str(payload)}
    try:
        return dict(jsonlib.loads(payload))
    except (ValueError, TypeError):
        return {"json": str(payload)}


async def stats_get(ctx: OpContext, req: StatsGetReq) -> StoryStats:
    """Story statistics: view/reaction graphs, or the public reposts."""
    from telethon.tl.functions import stats as fn

    peer = await _send.resolve(ctx, req.chat)
    if req.forwards:
        limit, state = window(ctx, "story.stats.get", PageKind.SEARCH, 30)
        result = await client(ctx)(
            fn.GetStoryPublicForwardsRequest(
                peer=peer, id=req.id, offset=str(state.get("offset") or ""), limit=limit
            )
        )
        table = _entities(result)
        forwards: list[dict[str, Any]] = []
        for row in getattr(result, "forwards", None) or []:
            if type(row).__name__ == "PublicForwardMessage":
                message = getattr(row, "message", None)
                forwards.append(
                    {
                        "kind": "message",
                        "chat_id": peer_id_of(getattr(message, "peer_id", None)) or 0,
                        "msg_id": int(getattr(message, "id", 0) or 0),
                    }
                )
            else:
                entity = _peer_entity(getattr(row, "peer_id", None), table)
                forwards.append(
                    {
                        "kind": "story",
                        "chat_id": peer_id_of(getattr(row, "peer_id", None)) or 0,
                        "story_id": int(getattr(getattr(row, "story", None), "id", 0) or 0),
                        "title": str(getattr(entity, "title", "") or ""),
                    }
                )
        return StoryStats(forwards=forwards)

    result = await client(ctx)(fn.GetStoryStatsRequest(peer=peer, id=req.id, dark=req.dark or None))
    return StoryStats(
        views_graph=await _graph(
            ctx, getattr(result, "views_graph", None), dark=req.dark, raw=req.raw
        ),
        reactions_by_emotion_graph=await _graph(
            ctx, getattr(result, "reactions_by_emotion_graph", None), dark=req.dark, raw=req.raw
        ),
    )


SPEC_STATS_GET = OperationSpec(
    id="story.stats.get",
    request=StatsGetReq,
    response=StoryStats,
    impl=stats_get,
    summary="Story statistics: view/reaction graphs and public reposts",
    description="Needs `can_view_stats` on the channel, or your own story.",
    timeout_s=300,
    columns=("forwards",),
    example={"views_graph": {"columns": []}, "forwards": []},
    example_args="story stats get me 42",
    covers=("stories.public-forwards", "stories.stats"),
)


# ---------------------------------------------------------------------------
# story live
# ---------------------------------------------------------------------------


class LiveGetReq(Request):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="Whose live story."),
    ] = None


async def live_get(ctx: OpContext, req: LiveGetReq) -> LiveStory:
    """What is known about a peer's live story.

    Telethon 1.44 speaks layer 227, whose `storyItem` carries no group-call
    reference, so the viewer count, publisher and stream settings a live story
    keeps on its call are not reachable from here — the story id, its dates
    and the live flag are. `vc` (PR-11) owns the call surface; this reports
    what the story layer actually exposes and says so rather than returning
    zeros that look like an empty broadcast.
    """
    from telethon.tl.functions import stories as fn

    peer = await _story.resolve_or_self(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    result = await client(ctx)(fn.GetPeerStoriesRequest(peer=peer))
    stories = getattr(getattr(result, "stories", None), "stories", None) or []
    live = [item for item in stories if getattr(item, "live", False)]
    if not live:
        raise NotFoundError("that peer has no live story right now")
    item = live[0]
    ctx.warn(
        "the pinned Telethon (layer 227) attaches no group call to a story, so the "
        "viewer count, publisher and stream settings are not reported"
    )
    return LiveStory(
        story_id=int(getattr(item, "id", 0) or 0),
        peer=peer_id,
        date=fmt_dt(getattr(item, "date", None)),
        expire_date=fmt_dt(getattr(item, "expire_date", None)),
    )


SPEC_LIVE_GET = OperationSpec(
    id="story.live.get",
    request=LiveGetReq,
    response=LiveStory,
    impl=live_get,
    summary="Info about a peer's live story",
    description=(
        "Layer 227 exposes the live story itself but not its group call, so "
        "the call-side fields stay null and a warning says why."
    ),
    columns=("story_id", "peer", "live"),
    example={"story_id": 42, "peer": 4242, "live": True},
    example_args="story live get @alice",
    covers_partial=("livestory.streamer-info", "stories.live-join"),
    coverage_note=(
        "The live story is reported; its group call is not reachable from "
        "layer 227's storyItem, and joining a broadcast needs a media engine "
        "tlgr does not have."
    ),
)


class LiveStartReq(PrivacyOptions, kw_only=True):
    chat: Annotated[
        PeerRef | None,
        arg(0, metavar="CHAT", required=False, kind="peer", help="Post as this channel."),
    ] = None
    rtmp: Annotated[bool, opt("--rtmp", help="RTMP mode: an external encoder supplies video.")] = (
        False
    )
    caption: Annotated[str | None, opt("--caption", help="Live story caption.")] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Caption formatting.")] = None
    pin: Annotated[bool, opt("--pin", help="Keep the recording on the profile page.")] = False
    protect: Annotated[bool, opt("--protect", help="noforwards.")] = False
    comments: Annotated[str | None, choice("on", "off", help="In-call comment overlay.")] = None
    comment_price: Annotated[
        int | None,
        opt("--comment-price", metavar="STARS", help="Minimum Stars to comment (0 = free)."),
    ] = None


async def live_start(ctx: OpContext, req: LiveStartReq) -> LiveStory:
    """Start a live story.

    Control-only unless `--rtmp`: `stories.startLive` creates the story and
    its call, but tlgr has no media engine, so a non-RTMP live story would
    broadcast silence. With `--rtmp` the CLI is a complete answer — it prints
    the ingest URL and key, and ffmpeg or OBS supplies the video.
    """
    from telethon.tl.functions import phone as phone_fn
    from telethon.tl.functions import stories as fn

    peer = await _story.resolve_or_self(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    if not req.rtmp:
        ctx.warn(
            "without --rtmp nothing will supply the video: tlgr has no media "
            "engine, so the broadcast would be silent"
        )
    text, entities = _send.body(req.caption, parse=req.parse)
    rules = await _story.privacy_rules(
        ctx,
        base=req.privacy or "everyone",
        allow=tuple(req.allow),
        exclude=tuple(req.exclude),
        preset=req.privacy_preset,
    )
    updates = await client(ctx)(
        fn.StartLiveRequest(
            peer=peer,
            privacy_rules=rules,
            pinned=req.pin or None,
            noforwards=req.protect or None,
            rtmp_stream=req.rtmp or None,
            caption=text or None,
            entities=_send.tl_entities(entities),
            random_id=random_id(),
            messages_enabled=(req.comments != "off") or None,
            send_paid_messages_stars=req.comment_price,
        )
    )
    story = _story_from_updates(updates, peer_id=peer_id)
    live = LiveStory(
        story_id=story.id,
        peer=peer_id,
        rtmp_stream=req.rtmp,
        messages_enabled=req.comments != "off",
        send_paid_messages_stars=req.comment_price,
        pinned=req.pin,
        noforwards=req.protect,
    )
    for update in getattr(updates, "updates", None) or []:
        call = getattr(update, "call", None)
        if call is not None:
            live.call_id = getattr(call, "id", None)
            live.participants_count = getattr(call, "participants_count", None)
            live.stream_dc_id = getattr(call, "stream_dc_id", None)
    if req.rtmp:
        rtmp = await client(ctx)(
            phone_fn.GetGroupCallStreamRtmpUrlRequest(peer=peer, revoke=False, live_story=True)
        )
        live.rtmp_url = getattr(rtmp, "url", None)
        live.rtmp_key = getattr(rtmp, "key", None)
    ctx.emit("story_live_started", {"peer": peer_id, "story_id": live.story_id})
    return live


SPEC_LIVE_START = OperationSpec(
    id="story.live.start",
    request=LiveStartReq,
    response=LiveStory,
    impl=live_start,
    summary="Start a live story (optionally RTMP, so an external encoder supplies the video)",
    description=(
        "One active live story per peer. Setting a comment price spends "
        "nothing; end the stream with the call commands."
    ),
    mutating=True,
    rate_class="send",
    timeout_s=300,
    tags=frozenset({"visible-to-others"}),
    columns=("story_id", "peer", "rtmp_stream", "rtmp_url"),
    example={"story_id": 42, "peer": 4242, "rtmp_stream": True, "rtmp_url": "rtmps://…"},
    example_args="story live start --rtmp",
    covers=("livestory.start-rtmp", "stories.live-start"),
)


# ---------------------------------------------------------------------------
# story export
# ---------------------------------------------------------------------------


class ExportReq(Request):
    chat: Annotated[
        PeerRef | None, arg(0, metavar="CHAT", required=False, kind="peer", help="Whose stories.")
    ] = None
    out: Annotated[str, opt("--out", metavar="DIR", kind="path", help="Output directory.")] = "."
    with_media: Annotated[
        bool, opt("--with-media/--no-media", help="Download each story's photo or video.")
    ] = True
    jsonl: Annotated[bool, opt("--jsonl", help="Also write one JSON object per story.")] = False
    archive: Annotated[
        bool, opt("--archive/--profile", help="Walk the private archive, not the profile page.")
    ] = True
    max_stories: Annotated[
        int, opt("--max-stories", metavar="N", help="Stop after this many stories.", ge=1)
    ] = 1000


async def export(ctx: OpContext, req: ExportReq) -> StoryExport:
    """Bulk-export stories with their media to disk.

    The "Export Telegram data → Stories" equivalent, and a thing the GUI has
    no button for. File references expire, so each story's media is downloaded
    from the item that was just fetched rather than from a cached listing.
    """
    import msgspec
    from telethon.tl.functions import stories as fn

    peer = await _story.resolve_or_self(ctx, req.chat)
    peer_id = _send.peer_id_of(peer)
    directory = Path(os.path.expanduser(req.out))
    directory.mkdir(parents=True, exist_ok=True)

    collected: list[Story] = []
    files: list[str] = []
    offset = 0
    while len(collected) < req.max_stories:
        page_size = min(100, req.max_stories - len(collected))
        request = (
            fn.GetStoriesArchiveRequest(peer=peer, offset_id=offset, limit=page_size)
            if req.archive
            else fn.GetPinnedStoriesRequest(peer=peer, offset_id=offset, limit=page_size)
        )
        result = await client(ctx)(request)
        raw = list(getattr(result, "stories", None) or [])
        if not raw:
            break
        for item in raw:
            story = _story.story_model(item, peer_id=peer_id)
            collected.append(story)
            if req.with_media and not story.deleted and not story.skipped:
                path = await _export_media(ctx, item, directory, story.id)
                if path:
                    files.append(path)
        offset = int(getattr(raw[-1], "id", 0) or 0)
        limiter = getattr(ctx, "limiter", None)
        if limiter is not None:
            await limiter.acquire("bulk")

    if req.jsonl:
        target = directory / f"stories-{peer_id}.jsonl"
        target.write_bytes(b"\n".join(msgspec.json.encode(item) for item in collected) + b"\n")
        files.append(str(target))
    return StoryExport(count=len(collected), out_dir=str(directory), files=files, stories=collected)


async def _export_media(ctx: OpContext, item: Any, directory: Path, story_id: int) -> str | None:
    from tlgr.ops import _media

    media = getattr(item, "media", None)
    document = _media.document_of(media) or _media.photo_of(media)
    if document is None:
        return None
    download = getattr(ctx, "download_file", None)
    if download is None:  # pragma: no cover - the daemon always supplies one
        return None
    target = directory / f"story_{story_id}"
    path = await download(
        document,
        target,
        size=int(getattr(document, "size", 0) or 0),
        dc_id=int(getattr(document, "dc_id", 0) or 0),
    )
    return str(getattr(path, "path", path))


SPEC_EXPORT = OperationSpec(
    id="story.export",
    request=ExportReq,
    response=StoryExport,
    impl=export,
    summary="Bulk-export stories with their media to disk",
    mutating=False,
    rate_class="bulk",
    timeout_s=900,
    columns=("count", "out_dir"),
    example={"count": 12, "out_dir": "./stories", "files": ["./stories/story_42"]},
    example_args="story export me --out ./stories",
    covers=("stories.export-stories",),
)


# ---------------------------------------------------------------------------
# story watch
# ---------------------------------------------------------------------------


class WatchReq(Request):
    peer: Annotated[
        list[PeerRef],
        opt("--peer", metavar="PEER", kind="peer", help="Only events for these peers."),
    ] = []
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Replay from this point.")
    ] = None


async def watch(ctx: OpContext, req: WatchReq) -> Any:
    """Stream story events off the daemon's update bus.

    A domain-scoped view of the one bus, not a second update loop: `watch
    --events story` in the daemon group emits the same records with the same
    field names, because both read the same normalised event.
    """
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise NotSupportedError("this build has no event bus to watch")

    chats = [_send.peer_id_of(await _send.resolve(ctx, ref)) for ref in req.peer]
    subscriber = bus.subscribe(
        ctx.account,
        types=("story_new", "story_id", "story_read", "story_reaction", "story_stealth"),
        chats=chats,
    )
    try:
        while True:
            event = await subscriber.queue.get()
            frame = _story_event(event)
            if frame is None:
                continue
            yield Page(items=[frame], has_more=True)
    finally:
        bus.unsubscribe(subscriber)


def _story_event(event: Any) -> StoryEvent | None:
    payload = getattr(event, "payload", None) or {}
    kind = str(payload.get("kind") or "")
    if not kind:
        return None
    stealth = payload.get("stealth_mode")
    return StoryEvent(
        kind=kind,
        peer=int(payload.get("peer") or getattr(event, "chat_id", 0) or 0),
        story_id=payload.get("story_id"),
        ids=[int(i) for i in (payload.get("ids") or [])],
        reaction=payload.get("reaction"),
        max_read_id=payload.get("max_read_id"),
        stealth_mode=StealthMode(**stealth) if isinstance(stealth, dict) else None,
        at=str(getattr(event, "at", "") or payload.get("at") or ""),
    )


SPEC_WATCH = OperationSpec(
    id="story.watch",
    request=WatchReq,
    response=Page[StoryEvent],
    impl=watch,
    summary="Stream story events (new stories, reads, reactions, stealth changes)",
    description=(
        "Event kinds: story.new, story.id-assigned, story.read, "
        "story.reaction-received, story.reaction-sent, story.stealth."
    ),
    stream=True,
    timeout_s=900,
    columns=("kind", "peer", "story_id"),
    headers=("Kind", "Peer", "Story"),
    example={
        "items": [{"event": "story", "kind": "story.new", "peer": 4242, "story_id": 42}],
        "has_more": True,
    },
    example_args="story watch --peer @alice",
    covers=("stories.new-story-events",),
)
