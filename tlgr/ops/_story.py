"""The story plumbing: items, audiences, overlays and feed state.

Four things in this group are genuinely fiddly, and every one of them is here
rather than in `ops/story.py` so that `post`, `edit` and `get` cannot disagree
about them.

* **Privacy rules are an ordered vector**, not a value. Telegram applies
  `[base, allow…, disallow…]` in order, so building it in one place is what
  makes "contacts, except Bob" mean the same thing on `story post` and
  `story edit`.
* **Media areas round-trip.** `story get --areas-out` writes exactly the JSON
  `--areas` reads back, which means the model → TL direction has to rebuild
  every variant the TL → model direction can produce.
* **Story items come in three shapes** for one id (`storyItem`,
  `storyItemSkipped`, `storyItemDeleted`), and a caller must be able to tell
  a placeholder from a story with no caption.
* **The feed is not offset-paginated.** `stories.getAllStories` takes an
  opaque `state` plus a `next` flag, so the cursor carries both and a naive
  offset cursor would silently restart the walk.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tlgr.core.errors import UsageError
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.peer import PeerRef, parse_peer_ref
from tlgr.models.story import (
    MediaArea,
    StealthMode,
    Story,
    StoryAlbum,
    StoryFwdHeader,
    StoryPrivacy,
    StoryViews,
)
from tlgr.ops._common import client
from tlgr.ops._serialize import entity_to_peer, media_summary, message_entities, peer_id_of

__all__ = [
    "album_model",
    "areas_from_json",
    "build_areas",
    "media_area_model",
    "privacy_model",
    "privacy_rules",
    "stealth_model",
    "story_ids",
    "story_model",
    "views_model",
]

#: `--period` → the `period` seconds `stories.sendStory` wants.
PERIODS: dict[str, int] = {"6h": 6 * 3600, "12h": 12 * 3600, "24h": 86400, "48h": 48 * 3600}


def story_ids(values: Any) -> list[int]:
    """Story ids from the CLI, accepting `10-14` ranges like message ids do."""
    from tlgr.ops._common import ids

    return ids(tuple(str(v) for v in (values or ())))


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

_BASE_RULES = ("everyone", "contacts", "close-friends", "selected")


def _split_target(text: str) -> tuple[str, str]:
    """`chat:@team` → `("chat", "@team")`; anything else is a user."""
    if text.startswith("chat:"):
        return "chat", text[len("chat:") :]
    return "user", text


async def privacy_rules(
    ctx: Any,
    *,
    base: str,
    allow: list[str] | tuple[str, ...] = (),
    exclude: list[str] | tuple[str, ...] = (),
    preset: str | None = None,
) -> list[Any]:
    """The `InputPrivacyRule` vector for an audience, in server order.

    Order is load-bearing: Telegram evaluates the vector front to back, so the
    base rule has to come first and the disallow entries last. Emitting them
    in the order the flags happened to be typed is how "contacts, except Bob"
    becomes "everyone".
    """
    from telethon.tl import types

    from tlgr.ops import _send

    if preset:
        base, allow, exclude = _preset(ctx, preset, base, list(allow), list(exclude))

    if base not in _BASE_RULES:
        raise UsageError(
            f"--privacy: expected one of {', '.join(_BASE_RULES)}, not {base!r}", field="privacy"
        )

    rules: list[Any] = []
    if base == "everyone":
        rules.append(types.InputPrivacyValueAllowAll())
    elif base == "contacts":
        rules.append(types.InputPrivacyValueAllowContacts())
    elif base == "close-friends":
        rules.append(types.InputPrivacyValueAllowCloseFriends())

    async def targets(values: Any) -> tuple[list[Any], list[int]]:
        users: list[Any] = []
        chats: list[int] = []
        for raw in values or ():
            kind, reference = _split_target(str(raw))
            peer = await _send.resolve(ctx, parse_peer_ref(reference))
            if kind == "chat":
                chats.append(abs(_send.peer_id_of(peer)) % 1_000_000_000_000)
            else:
                from telethon import utils

                try:
                    users.append(utils.get_input_user(peer))
                except (TypeError, ValueError) as exc:
                    raise UsageError(
                        f"{reference!r} is not a user; use chat:{reference} for a group",
                        field="allow",
                    ) from exc
        return users, chats

    allow_users, allow_chats = await targets(allow)
    if allow_users:
        rules.append(types.InputPrivacyValueAllowUsers(users=allow_users))
    if allow_chats:
        rules.append(types.InputPrivacyValueAllowChatParticipants(chats=allow_chats))

    deny_users, deny_chats = await targets(exclude)
    if deny_users:
        rules.append(types.InputPrivacyValueDisallowUsers(users=deny_users))
    if deny_chats:
        rules.append(types.InputPrivacyValueDisallowChatParticipants(chats=deny_chats))

    if base == "selected" and not allow_users and not allow_chats:
        raise UsageError(
            "--privacy selected needs at least one --allow, or the story is visible to nobody",
            field="allow",
        )
    return rules


def _preset(
    ctx: Any, name: str, base: str, allow: list[str], exclude: list[str]
) -> tuple[str, list[str], list[str]]:
    """A named audience from `[story.privacy_presets]` in the config.

    Presets exist because the same eight-person allow list is retyped on every
    post otherwise, and a mistyped one is a privacy incident rather than a
    typo.
    """
    config = getattr(ctx, "config", None)
    table: dict[str, Any] = {}
    if config is not None:
        section = getattr(config, "story", None) or {}
        if isinstance(section, dict):
            table = section.get("privacy_presets") or {}
        else:
            table = getattr(section, "privacy_presets", None) or {}
    preset = table.get(name) if isinstance(table, dict) else None
    if preset is None:
        raise UsageError(
            f"--privacy-preset: no preset named {name!r} in [story.privacy_presets]",
            field="privacy_preset",
        )
    if isinstance(preset, str):
        return preset, allow, exclude
    return (
        str(preset.get("base") or base),
        [*preset.get("allow", []), *allow],
        [*preset.get("exclude", []), *exclude],
    )


def privacy_model(rules: Any) -> StoryPrivacy | None:
    """A `privacyValue*` vector as the model. Only your own stories carry one."""
    if not rules:
        return None
    out = StoryPrivacy()
    for rule in rules:
        name = type(rule).__name__
        if name == "PrivacyValueAllowAll":
            out.base = "everyone"
        elif name == "PrivacyValueAllowContacts":
            out.base = "contacts"
        elif name == "PrivacyValueAllowCloseFriends":
            out.base = "close-friends"
        elif name == "PrivacyValueAllowUsers":
            out.allow_users.extend(int(u) for u in (getattr(rule, "users", None) or []))
        elif name == "PrivacyValueAllowChatParticipants":
            out.allow_chats.extend(int(c) for c in (getattr(rule, "chats", None) or []))
        elif name == "PrivacyValueDisallowUsers":
            out.disallow_users.extend(int(u) for u in (getattr(rule, "users", None) or []))
        elif name == "PrivacyValueDisallowChatParticipants":
            out.disallow_chats.extend(int(c) for c in (getattr(rule, "chats", None) or []))
    # No base rule came back, so the audience *is* the allow list.
    has_base = any(
        type(rule).__name__
        in ("PrivacyValueAllowAll", "PrivacyValueAllowContacts", "PrivacyValueAllowCloseFriends")
        for rule in rules
    )
    if not has_base and (out.allow_users or out.allow_chats):
        out.base = "selected"
    return out


# ---------------------------------------------------------------------------
# Media areas
# ---------------------------------------------------------------------------


def media_area_model(raw: Any) -> MediaArea:
    """One TL media area as the flat, round-trippable model."""
    coordinates = getattr(raw, "coordinates", None)
    area = MediaArea(
        x=float(getattr(coordinates, "x", 0.0) or 0.0),
        y=float(getattr(coordinates, "y", 0.0) or 0.0),
        w=float(getattr(coordinates, "w", 0.0) or 0.0),
        h=float(getattr(coordinates, "h", 0.0) or 0.0),
        rotation=float(getattr(coordinates, "rotation", 0.0) or 0.0),
        radius=getattr(coordinates, "radius", None),
    )
    name = type(raw).__name__
    geo = getattr(raw, "geo", None)
    if geo is not None:
        area.latitude = getattr(geo, "lat", None)
        area.longitude = getattr(geo, "long", None)
    if name == "MediaAreaGeoPoint":
        area.type = "geo"
        address = getattr(raw, "address", None)
        if address is not None:
            area.address = {
                key: str(value)
                for key, value in (
                    ("country_iso2", getattr(address, "country_iso2", None)),
                    ("state", getattr(address, "state", None)),
                    ("city", getattr(address, "city", None)),
                    ("street", getattr(address, "street", None)),
                )
                if value
            }
    elif name == "MediaAreaVenue":
        area.type = "venue"
        area.title = getattr(raw, "title", None)
        area.address_text = getattr(raw, "address", None)
        area.provider = getattr(raw, "provider", None)
        area.venue_id = getattr(raw, "venue_id", None)
        area.venue_type = getattr(raw, "venue_type", None)
    elif name == "MediaAreaSuggestedReaction":
        from tlgr.ops.reaction import name_of

        area.type = "reaction"
        area.reaction = name_of(getattr(raw, "reaction", None))
        area.dark = bool(getattr(raw, "dark", False))
        area.flipped = bool(getattr(raw, "flipped", False))
    elif name == "MediaAreaChannelPost":
        from tlgr.ops._serialize import marked_id

        area.type = "channel_post"
        area.chat_id = marked_id(int(getattr(raw, "channel_id", 0) or 0), "channel")
        area.msg_id = getattr(raw, "msg_id", None)
    elif name == "MediaAreaUrl":
        area.type = "url"
        area.url = getattr(raw, "url", None)
    elif name == "MediaAreaWeather":
        area.type = "weather"
        area.emoji = getattr(raw, "emoji", None)
        area.temperature_c = getattr(raw, "temperature_c", None)
        area.color = getattr(raw, "color", None)
    elif name == "MediaAreaStarGift":
        area.type = "star_gift"
        area.slug = getattr(raw, "slug", None)
    return area


def _coordinates(area: MediaArea) -> Any:
    from telethon.tl import types

    return types.MediaAreaCoordinates(
        x=area.x, y=area.y, w=area.w, h=area.h, rotation=area.rotation, radius=area.radius
    )


async def _area_to_tl(ctx: Any, area: MediaArea) -> Any:
    """The model back into a TL media area, resolving what has to be resolved."""
    from telethon.tl import types

    from tlgr.ops import _send

    coordinates = _coordinates(area)
    if area.type == "geo":
        address = None
        if area.address:
            address = types.GeoPointAddress(
                country_iso2=str(area.address.get("country_iso2", "")),
                state=area.address.get("state"),
                city=area.address.get("city"),
                street=area.address.get("street"),
            )
        return types.MediaAreaGeoPoint(
            coordinates=coordinates,
            geo=types.GeoPoint(
                long=float(area.longitude or 0.0), lat=float(area.latitude or 0.0), access_hash=0
            ),
            address=address,
        )
    if area.type == "venue":
        return types.MediaAreaVenue(
            coordinates=coordinates,
            geo=types.GeoPoint(
                long=float(area.longitude or 0.0), lat=float(area.latitude or 0.0), access_hash=0
            ),
            title=str(area.title or ""),
            address=str(area.address_text or ""),
            provider=str(area.provider or ""),
            venue_id=str(area.venue_id or ""),
            venue_type=str(area.venue_type or ""),
        )
    if area.type == "reaction":
        from tlgr.ops.reaction import to_tl

        return types.MediaAreaSuggestedReaction(
            coordinates=coordinates,
            reaction=to_tl(str(area.reaction or "")),
            dark=area.dark or None,
            flipped=area.flipped or None,
        )
    if area.type == "channel_post":
        from tlgr.ops._common import input_channel

        peer = await _send.resolve(ctx, parse_peer_ref(str(area.chat_id)))
        return types.InputMediaAreaChannelPost(
            coordinates=coordinates, channel=input_channel(peer), msg_id=int(area.msg_id or 0)
        )
    if area.type == "url":
        return types.MediaAreaUrl(coordinates=coordinates, url=str(area.url or ""))
    if area.type == "weather":
        return types.MediaAreaWeather(
            coordinates=coordinates,
            emoji=str(area.emoji or ""),
            temperature_c=float(area.temperature_c or 0.0),
            color=int(area.color or 0),
        )
    if area.type == "star_gift":
        return types.MediaAreaStarGift(coordinates=coordinates, slug=str(area.slug or ""))
    raise UsageError(f"unknown media area type {area.type!r}", field="areas")


def _rect(text: str, flag: str) -> tuple[float, float, float, float, float, float | None]:
    """`X,Y,W,H[,ROT[,RADIUS]]` as floats. Percentages of the media, not pixels."""
    parts = [p.strip() for p in text.split(",") if p.strip() != ""]
    if len(parts) < 4:
        raise UsageError(f"{flag}: the position must be X,Y,W,H", field=flag.lstrip("-"))
    try:
        numbers = [float(p) for p in parts]
    except ValueError as exc:
        raise UsageError(
            f"{flag}: {text!r} is not a X,Y,W,H rectangle", field=flag.lstrip("-")
        ) from exc
    x, y, w, h = numbers[:4]
    rotation = numbers[4] if len(numbers) > 4 else 0.0
    radius = numbers[5] if len(numbers) > 5 else None
    return x, y, w, h, rotation, radius


def _split_spec(value: str, flag: str) -> tuple[str, str]:
    """`payload@X,Y,W,H` → `(payload, rect)`; the last `@` wins."""
    payload, sep, rect = value.rpartition("@")
    if not sep:
        raise UsageError(
            f"{flag}: expected PAYLOAD@X,Y,W,H — got {value!r}", field=flag.lstrip("-")
        )
    return payload, rect


def _area(flag: str, value: str, **fields: Any) -> tuple[MediaArea, str]:
    payload, rect = _split_spec(value, flag)
    x, y, w, h, rotation, radius = _rect(rect, flag)
    return MediaArea(x=x, y=y, w=w, h=h, rotation=rotation, radius=radius, **fields), payload


async def build_areas(
    ctx: Any,
    *,
    areas_file: str | None = None,
    geo: tuple[str, ...] = (),
    venue: tuple[str, ...] = (),
    venue_near: str | None = None,
    venue_pick: int = 0,
    url: tuple[str, ...] = (),
    reaction: tuple[str, ...] = (),
    post: tuple[str, ...] = (),
    weather: tuple[str, ...] = (),
    gift: tuple[str, ...] = (),
) -> tuple[list[Any], list[MediaArea]]:
    """`(TL areas, the models that describe them)`.

    `--areas` is the authoritative form because it is what `--areas-out`
    writes; the `--area-*` flags are sugar that produces the same models, so a
    story can be repositioned by editing the JSON rather than by retyping
    every pill.
    """
    models: list[MediaArea] = []

    if areas_file:
        models.extend(areas_from_json(areas_file))

    for value in geo:
        area, payload = _area("--area-geo", value, type="geo")
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) < 2:
            raise UsageError("--area-geo: expected LAT,LON[,ADDR]@X,Y,W,H", field="area_geo")
        area.latitude, area.longitude = float(parts[0]), float(parts[1])
        if len(parts) > 2:
            keys = ("country_iso2", "state", "city", "street")
            area.address = {k: v for k, v in zip(keys, parts[2:], strict=False) if v}
        models.append(area)

    for value in url:
        area, payload = _area("--area-url", value, type="url")
        area.url = payload
        models.append(area)

    for value in reaction:
        area, payload = _area("--area-reaction", value, type="reaction")
        emoji, *modifiers = payload.split(":")
        area.reaction = emoji
        area.dark = "dark" in modifiers
        area.flipped = "flipped" in modifiers
        models.append(area)

    for value in post:
        area, payload = _area("--area-post", value, type="channel_post")
        chat, _, msg_id = payload.rpartition(":")
        if not chat or not msg_id.isdigit():
            raise UsageError("--area-post: expected CHAT:MSG_ID@X,Y,W,H", field="area_post")
        from tlgr.ops import _send

        peer = await _send.resolve(ctx, parse_peer_ref(chat))
        area.chat_id = _send.peer_id_of(peer)
        area.msg_id = int(msg_id)
        models.append(area)

    for value in gift:
        area, payload = _area("--area-gift", value, type="star_gift")
        area.slug = payload
        models.append(area)

    for value in weather:
        area, payload = _area("--area-weather", value, type="weather")
        if payload.strip().lower() == "auto":
            models.append(await _resolve_weather(ctx, area, venue_near))
            continue
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) < 3:
            raise UsageError(
                "--area-weather: expected EMOJI,TEMP_C,#AARRGGBB@X,Y,W,H", field="area_weather"
            )
        area.emoji = parts[0]
        area.temperature_c = float(parts[1])
        area.color = int(parts[2].lstrip("#"), 16)
        models.append(area)

    for value in venue:
        area, payload = _area("--area-venue", value, type="venue")
        models.append(await _resolve_venue(ctx, area, payload, venue_near, venue_pick))

    return [await _area_to_tl(ctx, area) for area in models], models


def areas_from_json(path_or_text: str) -> list[MediaArea]:
    """Media areas from the JSON `story get --areas-out` writes.

    A path is read; anything that starts with `[` is taken as inline JSON, so
    a script can pipe the array in without a temp file.
    """
    import msgspec

    text = path_or_text.strip()
    if not text.startswith("["):
        try:
            text = Path(text).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"--areas: {exc.strerror or exc}", field="areas") from exc
    try:
        return msgspec.json.decode(text.encode(), type=list[MediaArea])
    except (msgspec.DecodeError, msgspec.ValidationError, json.JSONDecodeError) as exc:
        raise UsageError(f"--areas: {exc}", field="areas") from exc


async def _inline_query(ctx: Any, username: str, query: str, near: str | None) -> Any:
    """One inline-bot query, which is how venue and weather areas are built.

    Neither is an API method: the official clients run an inline query against
    a bot named in the server config and use the result's `query_id`, so tlgr
    does the same rather than inventing a venue id the server will reject.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    point = None
    if near:
        try:
            lat, _, lon = near.partition(",")
            point = types.InputGeoPoint(lat=float(lat), long=float(lon))
        except ValueError as exc:
            raise UsageError(
                "--area-venue-near: expected LAT,LON", field="area_venue_near"
            ) from exc
    bot = await client(ctx).get_input_entity(username)
    return await client(ctx)(
        fn.GetInlineBotResultsRequest(
            bot=bot, peer=types.InputPeerSelf(), query=query, offset="", geo_point=point
        )
    )


async def _config_username(ctx: Any, field: str) -> str:
    from telethon.tl.functions import help as help_fn

    config = await client(ctx)(help_fn.GetConfigRequest())
    username = getattr(config, field, None)
    if not username:
        from tlgr.core.errors import NotSupportedError

        raise NotSupportedError(
            f"this account's server config names no {field}, so there is nowhere to ask"
        )
    return str(username)


async def _resolve_venue(
    ctx: Any, area: MediaArea, query: str, near: str | None, pick: int
) -> MediaArea:
    username = await _config_username(ctx, "venue_search_username")
    result = await _inline_query(ctx, username, query, near)
    rows = [
        row
        for row in (getattr(result, "results", None) or [])
        if type(getattr(row, "send_message", None)).__name__ == "BotInlineMessageMediaVenue"
    ]
    if not rows:
        raise UsageError(f"--area-venue: no venue matched {query!r}", field="area_venue")
    if pick >= len(rows):
        raise UsageError(
            f"--area-venue-pick {pick}: only {len(rows)} venues matched", field="area_venue_pick"
        )
    message = rows[pick].send_message
    geo = getattr(message, "geo", None)
    area.title = getattr(message, "title", None)
    area.address_text = getattr(message, "address", None)
    area.provider = getattr(message, "provider", None)
    area.venue_id = getattr(message, "venue_id", None)
    area.venue_type = getattr(message, "venue_type", None)
    area.latitude = getattr(geo, "lat", None)
    area.longitude = getattr(geo, "long", None)
    return area


async def _resolve_weather(ctx: Any, area: MediaArea, near: str | None) -> MediaArea:
    username = await _config_username(ctx, "weather_search_username")
    result = await _inline_query(ctx, username, "", near)
    rows = list(getattr(result, "results", None) or [])
    if not rows:
        raise UsageError(
            "--area-weather auto: the weather bot returned nothing for that point",
            field="area_weather",
        )
    message = getattr(rows[0], "send_message", None)
    area.emoji = getattr(rows[0], "title", None) or "🌡"
    text = str(getattr(message, "message", "") or getattr(rows[0], "description", "") or "")
    digits = "".join(c for c in text if c.isdigit() or c in "-.")
    area.temperature_c = float(digits) if digits else 0.0
    area.color = 0xFF000000
    return area


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def views_model(raw: Any) -> StoryViews | None:
    if raw is None:
        return None
    from tlgr.ops.reaction import name_of

    return StoryViews(
        views_count=int(getattr(raw, "views_count", 0) or 0),
        forwards_count=getattr(raw, "forwards_count", None),
        reactions_count=getattr(raw, "reactions_count", None),
        has_viewers=bool(getattr(raw, "has_viewers", False)),
        recent_viewers=[int(v) for v in (getattr(raw, "recent_viewers", None) or [])],
        reactions={
            name_of(getattr(count, "reaction", None)): int(getattr(count, "count", 0) or 0)
            for count in (getattr(raw, "reactions", None) or [])
        },
    )


def _fwd(raw: Any) -> StoryFwdHeader | None:
    if raw is None:
        return None
    return StoryFwdHeader(
        from_id=peer_id_of(getattr(raw, "from_", None)),
        from_name=getattr(raw, "from_name", None),
        story_id=getattr(raw, "story_id", None),
        modified=bool(getattr(raw, "modified", False)),
    )


def story_model(raw: Any, *, peer_id: int = 0, peer: Any = None, link: str | None = None) -> Story:
    """A `storyItem` / `storyItemSkipped` / `storyItemDeleted` as one model."""
    from tlgr.ops.reaction import name_of

    name = type(raw).__name__
    date = getattr(raw, "date", None)
    expire = getattr(raw, "expire_date", None)
    story = Story(
        id=int(getattr(raw, "id", 0) or 0),
        peer_id=peer_id,
        peer=entity_to_peer(peer) if peer is not None else None,
        date=fmt_dt(date),
        date_unix=to_unix(date),
        expire_date=fmt_dt(expire),
        expire_date_unix=to_unix(expire),
        link=link,
        deleted=name == "StoryItemDeleted",
        skipped=name == "StoryItemSkipped",
        live=bool(getattr(raw, "live", False)),
        close_friends=bool(getattr(raw, "close_friends", False)),
    )
    if story.deleted or story.skipped:
        return story

    story.caption = str(getattr(raw, "caption", "") or "")
    story.entities = message_entities(raw)
    story.media = media_summary(getattr(raw, "media", None))
    story.media_areas = [media_area_model(a) for a in (getattr(raw, "media_areas", None) or [])]
    story.privacy = privacy_model(getattr(raw, "privacy", None))
    story.public = bool(getattr(raw, "public", False))
    story.contacts = bool(getattr(raw, "contacts", False))
    story.selected_contacts = bool(getattr(raw, "selected_contacts", False))
    story.pinned = bool(getattr(raw, "pinned", False))
    story.noforwards = bool(getattr(raw, "noforwards", False))
    story.edited = bool(getattr(raw, "edited", False))
    story.out = bool(getattr(raw, "out", False))
    story.min = bool(getattr(raw, "min", False))
    story.fwd_from = _fwd(getattr(raw, "fwd_from", None))
    reaction = getattr(raw, "sent_reaction", None)
    story.sent_reaction = name_of(reaction) if reaction is not None else None
    story.albums = [int(a) for a in (getattr(raw, "albums", None) or [])]
    music = getattr(raw, "music", None)
    if music is not None:
        from telethon.tl import types as tl

        story.music = media_summary(tl.MessageMediaDocument(document=music))
    story.views = views_model(getattr(raw, "views", None))
    return story


def album_model(raw: Any, *, stories: list[int] | None = None) -> StoryAlbum:
    from tlgr.ops._serialize import photo_summary

    return StoryAlbum(
        id=int(getattr(raw, "album_id", 0) or 0),
        title=str(getattr(raw, "title", "") or ""),
        icon=photo_summary(getattr(raw, "icon_photo", None)),
        stories=list(stories or []),
    )


def stealth_model(raw: Any, *, past: bool = False, future: bool = False) -> StealthMode:
    import time

    active = getattr(raw, "active_until_date", None)
    cooldown = getattr(raw, "cooldown_until_date", None)
    active_unix = to_unix(active)
    return StealthMode(
        active_until_date=fmt_dt(active),
        active_until_unix=active_unix,
        cooldown_until_date=fmt_dt(cooldown),
        cooldown_until_unix=to_unix(cooldown),
        past=past,
        future=future,
        active=bool(active_unix and active_unix > int(time.time())),
    )


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------


async def resolve_or_self(ctx: Any, ref: PeerRef | None) -> Any:
    """The peer, or this account. Story commands default to your own stories."""
    from telethon.tl import types

    from tlgr.ops import _send

    if ref is None:
        return types.InputPeerSelf()
    return await _send.resolve(ctx, ref)
