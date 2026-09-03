"""The `location` group: points, venues, live shares, nearby peers, map images.

Three things a headless client has to face that a GUI does not.

* **Nobody updates a live location for you.** The API stores one position and
  a period; the phone re-sends it every few seconds. `location live start`
  therefore reports `expires_at` rather than a duration, and `--follow`
  refreshes from a source the caller names instead of pretending the share
  moves on its own.
* **There is no "list my live shares" method.** `messages.getRecentLocations`
  answers per chat, so `location live list` walks the chats it is given and
  says so, rather than inventing a global answer.
* **A map thumbnail does not live on the home DC.** `upload.getWebFile` has
  to be issued against `config.webfile_dc_id` through a borrowed sender, and
  it needs the `geoPoint.access_hash` off the received message.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, NotSupportedError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, fmt_unix, parse_duration, to_unix
from tlgr.models.base import Request
from tlgr.models.location import (
    GeoPoint,
    LiveLocation,
    LiveStopped,
    MapPreview,
    Nearby,
    NearbyPeer,
    SentLocation,
    Venue,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._common import already, client, only, random_id, window
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The periods the official clients offer. Any value is accepted; these are
#: only what `--period` defaults and snaps against in the help text.
GUI_PERIODS = (900, 3600, 28800)

_EXAMPLE_LIVE: dict[str, Any] = {
    "id": 12345,
    "chat_id": 777123,
    "geo": {"lat": 52.52, "lon": 13.405},
    "period": 3600,
    "expires_at": "2026-09-03T10:14:07Z",
    "mine": True,
}


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def _point(lat: float, lon: float, accuracy: int | None = None) -> Any:
    from telethon.tl import types

    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise UsageError(f"{lat},{lon} is not a coordinate on Earth", field="lat")
    return types.InputGeoPoint(lat=float(lat), long=float(lon), accuracy_radius=accuracy)


def geo_model(geo: Any) -> GeoPoint | None:
    """A TL `GeoPoint` → the model, access hash included for the map endpoint."""
    if geo is None or type(geo).__name__ == "GeoPointEmpty":
        return None
    return GeoPoint(
        lat=float(getattr(geo, "lat", 0.0) or 0.0),
        lon=float(getattr(geo, "long", 0.0) or 0.0),
        accuracy=getattr(geo, "accuracy_radius", None),
        access_hash=getattr(geo, "access_hash", None),
    )


def _live_of(message: Any, *, chat_id: int, me: int | None = None) -> LiveLocation | None:
    """A live-location message → the model, or None if it is not one."""
    media = getattr(message, "media", None)
    if type(media).__name__ != "MessageMediaGeoLive":
        return None
    date = getattr(message, "date", None)
    period = int(getattr(media, "period", 0) or 0)
    start = to_unix(date) or 0
    expires = start + period if period else None
    return LiveLocation(
        chat_id=chat_id,
        msg_id=int(getattr(message, "id", 0) or 0),
        id=int(getattr(message, "id", 0) or 0),
        peer_id=peer_id_of(getattr(message, "from_id", None)),
        geo=geo_model(getattr(media, "geo", None)),
        heading=getattr(media, "heading", None),
        proximity=getattr(media, "proximity_notification_radius", None),
        period=period or None,
        expires_at=fmt_unix(expires) if expires else None,
        expires_at_unix=expires,
        stopped=bool(getattr(media, "stopped", False)) or period == 0,
        mine=bool(getattr(message, "out", False)) or (me is not None and me == 0),
        date=fmt_dt(date) or "",
        date_unix=start,
    )


async def _send_media(ctx: OpContext, peer: Any, media: Any, req: Any) -> Any:
    """One `messages.sendMedia` for every "send a place" command."""
    from telethon.tl.functions import messages as fn

    values = {
        "peer": peer,
        "media": media,
        "message": "",
        "random_id": random_id(),
        "silent": getattr(req, "silent", False) or None,
        "noforwards": getattr(req, "protect", False) or None,
        "reply_to": await _send.reply_target(
            ctx, reply_to=getattr(req, "reply_to", None), topic=getattr(req, "topic", None)
        ),
        "schedule_date": _send.schedule_at(getattr(req, "schedule", None)),
        "send_as": (
            await _send.resolve(ctx, req.send_as) if getattr(req, "send_as", None) else None
        ),
        "effect": _send.effect_id(getattr(req, "effect", None)),
    }
    return await client(ctx)(fn.SendMediaRequest(**only(values, fn.SendMediaRequest)))


# ---------------------------------------------------------------------------
# location send / venue send
# ---------------------------------------------------------------------------


class SendReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to send it.")]
    lat: Annotated[float, arg(1, metavar="LAT", help="Latitude.")]
    lon: Annotated[float, arg(2, metavar="LON", help="Longitude.")]
    accuracy: Annotated[
        int | None, opt("--accuracy", metavar="METRES", help="accuracy_radius.")
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None


async def send(ctx: OpContext, req: SendReq) -> SentLocation:
    """Send a static location."""
    from telethon.tl import types

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    media = types.InputMediaGeoPoint(geo_point=_point(req.lat, req.lon, req.accuracy))
    result = await _send_media(ctx, peer, media, req)
    sent = _send.message_from_updates(result, chat_id=chat_id)
    ctx.emit("location_sent", {"chat_id": chat_id, "id": sent.id})
    return SentLocation(
        id=sent.id,
        chat_id=chat_id,
        date=sent.date,
        date_unix=sent.date_unix,
        geo=GeoPoint(lat=req.lat, lon=req.lon, accuracy=req.accuracy),
    )


SPEC_SEND = OperationSpec(
    id="location.send",
    request=SendReq,
    response=SentLocation,
    impl=send,
    summary="Send a static location",
    description="Shares the send-option set with `message send`.",
    mutating=True,
    rate_class="send",
    columns=("id", "chat_id", "geo.lat", "geo.lon"),
    example={
        "id": 12345,
        "chat_id": 777123,
        "date": "2026-09-03T09:14:07Z",
        "date_unix": 1788340447,
        "geo": {"lat": 52.52, "lon": 13.405},
    },
    example_args="location send @alice 52.5200 13.4050",
    tags=frozenset({"visible-to-others"}),
    covers=("location.send-static",),
)


class VenueSendReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to send it.")]
    lat: Annotated[float, arg(1, metavar="LAT", help="Latitude.")]
    lon: Annotated[float, arg(2, metavar="LON", help="Longitude.")]
    title: Annotated[str, opt("--title", metavar="TEXT", help="Venue name.")] = ""
    address: Annotated[str, opt("--address", metavar="TEXT", help="Street address.")] = ""
    provider: Annotated[str, opt("--provider", metavar="NAME", help="Venue provider.")] = ""
    venue_id: Annotated[str, opt("--venue-id", metavar="ID", help="Provider venue id.")] = ""
    venue_type: Annotated[
        str, opt("--venue-type", metavar="TYPE", help="Provider venue category.")
    ] = ""
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None


async def venue_send(ctx: OpContext, req: VenueSendReq) -> SentLocation:
    """Send a venue / place.

    `provider`, `venue_id` and `venue_type` may all be empty: a hand-made
    venue is a title and an address on a point, which is what a CLI user
    usually has. `location search` is where the provider ids come from.
    """
    from telethon.tl import types

    if not req.title:
        raise UsageError("a venue needs --title", field="title")
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    media = types.InputMediaVenue(
        geo_point=_point(req.lat, req.lon),
        title=req.title,
        address=req.address,
        provider=req.provider,
        venue_id=req.venue_id,
        venue_type=req.venue_type,
    )
    result = await _send_media(ctx, peer, media, req)
    sent = _send.message_from_updates(result, chat_id=chat_id)
    return SentLocation(
        id=sent.id,
        chat_id=chat_id,
        date=sent.date,
        date_unix=sent.date_unix,
        venue=Venue(
            title=req.title,
            address=req.address,
            provider=req.provider,
            venue_id=req.venue_id,
            venue_type=req.venue_type,
            geo=GeoPoint(lat=req.lat, lon=req.lon),
        ),
    )


SPEC_VENUE_SEND = OperationSpec(
    id="location.venue.send",
    request=VenueSendReq,
    response=SentLocation,
    impl=venue_send,
    summary="Send a venue / place",
    description="Use `location search` to obtain provider/venue-id pairs.",
    mutating=True,
    rate_class="send",
    columns=("id", "chat_id", "venue.title"),
    example={
        "id": 12345,
        "chat_id": 777123,
        "date": "2026-09-03T09:14:07Z",
        "date_unix": 1788340447,
        "venue": {"title": "Brandenburg Gate", "address": "Pariser Platz"},
    },
    example_args="location venue send @alice 52.5163 13.3777 --title 'Brandenburg Gate'",
    tags=frozenset({"visible-to-others"}),
    covers=("location.send-venue",),
)


# ---------------------------------------------------------------------------
# location live start / edit / stop / list
# ---------------------------------------------------------------------------


class LiveStartReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to share it.")]
    lat: Annotated[float, arg(1, metavar="LAT", help="Latitude.")]
    lon: Annotated[float, arg(2, metavar="LON", help="Longitude.")]
    period: Annotated[
        str, opt("--period", metavar="DURATION", help="How long to share (GUI: 15m/1h/8h).")
    ] = "1h"
    heading: Annotated[
        int | None, opt("--heading", metavar="DEGREES", help="Movement direction, 1-360.")
    ] = None
    proximity: Annotated[
        int | None, opt("--proximity", metavar="METRES", help="Proximity-alert radius.")
    ] = None
    accuracy: Annotated[
        int | None, opt("--accuracy", metavar="METRES", help="accuracy_radius.")
    ] = None
    follow: Annotated[
        str | None,
        opt("--follow", metavar="PATH", help="Keep updating from this source (a daemon job)."),
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None


def _heading(value: int | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= 360:
        raise UsageError("--heading is a bearing in degrees, 1 to 360", field="heading")
    return value


async def live_start(ctx: OpContext, req: LiveStartReq) -> LiveLocation:
    """Start sharing a live location.

    The server stores one position and a period; nothing moves it afterwards.
    `--follow` is refused rather than silently doing nothing, because a share
    that never updates is worse than one the caller knows they must drive.
    """
    from telethon.tl import types

    if req.follow:
        raise NotSupportedError(
            "--follow needs the daemon job scheduler, which lands with the jobs group; "
            "until then, drive the share with repeated `tlgr location live edit` calls"
        )
    period = int(parse_duration(req.period) or 3600)
    if period < 60:
        raise UsageError("--period must be at least a minute", field="period")

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    media = types.InputMediaGeoLive(
        geo_point=_point(req.lat, req.lon, req.accuracy),
        period=period,
        heading=_heading(req.heading),
        proximity_notification_radius=req.proximity,
    )
    result = await _send_media(ctx, peer, media, req)
    sent = _send.message_from_updates(result, chat_id=chat_id)
    live = LiveLocation(
        chat_id=chat_id,
        msg_id=sent.id,
        id=sent.id,
        geo=GeoPoint(lat=req.lat, lon=req.lon, accuracy=req.accuracy),
        heading=req.heading,
        proximity=req.proximity,
        period=period,
        expires_at=fmt_unix(sent.date_unix + period),
        expires_at_unix=sent.date_unix + period,
        mine=True,
        date=sent.date,
        date_unix=sent.date_unix,
    )
    ctx.emit("location_live_started", {"chat_id": chat_id, "id": sent.id, "period": period})
    return live


SPEC_LIVE_START = OperationSpec(
    id="location.live.start",
    request=LiveStartReq,
    response=LiveLocation,
    impl=live_start,
    summary="Start sharing a live location",
    description=(
        "A headless client has to run its own updater: nothing moves the "
        "share by itself, so `expires_at` is reported rather than a duration."
    ),
    mutating=True,
    rate_class="send",
    columns=("id", "chat_id", "period", "expires_at"),
    example=_EXAMPLE_LIVE,
    example_args="location live start @alice 52.5200 13.4050 --period 1h",
    tags=frozenset({"visible-to-others"}),
    covers=("location.live-send",),
)


class LiveEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="The live message.")]
    lat: Annotated[float | None, arg(2, metavar="LAT", required=False, help="Latitude.")] = None
    lon: Annotated[float | None, arg(3, metavar="LON", required=False, help="Longitude.")] = None
    heading: Annotated[int | None, opt("--heading", metavar="DEGREES", help="Direction arrow.")] = (
        None
    )
    proximity: Annotated[
        int | None, opt("--proximity", metavar="METRES", help="Proximity-alert radius.")
    ] = None
    accuracy: Annotated[
        int | None, opt("--accuracy", metavar="METRES", help="accuracy_radius.")
    ] = None


async def live_edit(ctx: OpContext, req: LiveEditReq) -> LiveLocation:
    """Move a live location, or change its heading and proximity radius.

    Position, heading and proximity are three fields of one edit, so a caller
    that only wants the arrow still resends the position — read back from the
    message rather than guessed.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    message = await client(ctx).get_messages(peer, ids=req.msg_id)
    current = _live_of(message, chat_id=chat_id) if message is not None else None
    if current is None:
        raise NotFoundError(f"message {req.msg_id} in {chat_id} is not a live location")

    lat = req.lat if req.lat is not None else (current.geo.lat if current.geo else 0.0)
    lon = req.lon if req.lon is not None else (current.geo.lon if current.geo else 0.0)
    media = types.InputMediaGeoLive(
        geo_point=_point(lat, lon, req.accuracy),
        heading=_heading(req.heading) if req.heading is not None else current.heading,
        proximity_notification_radius=(
            req.proximity if req.proximity is not None else current.proximity
        ),
        period=current.period,
    )
    result = await client(ctx)(fn.EditMessageRequest(peer=peer, id=req.msg_id, media=media))
    for update in getattr(result, "updates", None) or []:
        edited = getattr(update, "message", None)
        live = _live_of(edited, chat_id=chat_id) if edited is not None else None
        if live is not None:
            return live
    current.geo = GeoPoint(lat=lat, lon=lon, accuracy=req.accuracy)
    return current


SPEC_LIVE_EDIT = OperationSpec(
    id="location.live.edit",
    request=LiveEditReq,
    response=LiveLocation,
    impl=live_edit,
    summary="Update a live location (position, heading, proximity radius)",
    description=(
        "One edit carries all three; the fields not named are read off the "
        "message and resent unchanged."
    ),
    aliases=("location.live.update",),
    mutating=True,
    rate_class="send",
    columns=("id", "chat_id", "geo.lat", "geo.lon", "heading"),
    example=_EXAMPLE_LIVE,
    example_args="location live edit @alice 12345 52.5210 13.4100",
    covers=("location.live-heading", "location.live-proximity", "location.live-update"),
)


class LiveStopReq(Request):
    chat: Annotated[
        PeerRef | None, arg(0, metavar="CHAT", required=False, kind="peer", help="Chat.")
    ] = None
    msg_id: Annotated[
        int | None, arg(1, metavar="MSG_ID", required=False, kind="msg_id", help="Which share.")
    ] = None
    every: Annotated[bool, opt("--every", help="Stop every live share in that chat.")] = False


async def live_stop(ctx: OpContext, req: LiveStopReq) -> LiveStopped:
    """Stop sharing a live location.

    Stopping is an edit to `inputMediaGeoLive(stopped=True)` with an empty
    point — the position is deliberately not resent, because the last thing a
    stopped share should do is publish where you were when you stopped.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.chat is None:
        raise UsageError("name the chat whose share should stop", field="chat")
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    targets: list[int] = []
    if req.msg_id is not None:
        targets = [req.msg_id]
    elif req.every:
        targets = [
            live.msg_id for live in await _recent(ctx, peer, chat_id, limit=100) if live.mine
        ]
    else:
        raise UsageError("name a message id, or pass --every", field="msg_id")

    stopped: list[LiveLocation] = []
    for msg_id in targets:
        result = await client(ctx)(
            fn.EditMessageRequest(
                peer=peer,
                id=msg_id,
                media=types.InputMediaGeoLive(geo_point=types.InputGeoPointEmpty(), stopped=True),
            )
        )
        for update in getattr(result, "updates", None) or []:
            edited = getattr(update, "message", None)
            live = _live_of(edited, chat_id=chat_id) if edited is not None else None
            if live is not None:
                stopped.append(live)
    if not targets:
        already(ctx)
        return LiveStopped(stopped=False, count=0, already=True)
    ctx.emit("location_live_stopped", {"chat_id": chat_id, "ids": targets})
    return LiveStopped(stopped=True, count=len(targets), items=stopped)


SPEC_LIVE_STOP = OperationSpec(
    id="location.live.stop",
    request=LiveStopReq,
    response=LiveStopped,
    impl=live_stop,
    summary="Stop sharing a live location",
    description=(
        "The stop edit carries an empty point on purpose: a stopped share "
        "should not publish where you were when you stopped it."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("stopped", "count"),
    example={"stopped": True, "count": 1},
    example_args="location live stop @alice 12345",
    covers=("location.live-stop",),
)


async def _recent(ctx: OpContext, peer: Any, chat_id: int, *, limit: int) -> list[LiveLocation]:
    from telethon.tl.functions import messages as fn

    result = await client(ctx)(fn.GetRecentLocationsRequest(peer=peer, limit=limit, hash=0))
    out: list[LiveLocation] = []
    for message in getattr(result, "messages", None) or []:
        live = _live_of(message, chat_id=chat_id)
        if live is not None:
            out.append(live)
    return out


class LiveListReq(Request):
    chat: Annotated[
        PeerRef | None, arg(0, metavar="CHAT", required=False, kind="peer", help="Chat.")
    ] = None
    mine: Annotated[bool, opt("--mine", help="Only my own active shares.")] = False


async def live_list(ctx: OpContext, req: LiveListReq) -> Page[LiveLocation]:
    """Live locations shared in a chat — everyone's, or only mine.

    There is no server method that lists my own shares across chats, so this
    asks per chat and says so rather than pretending to a global answer.
    """
    limit, state = window(ctx, "location.live.list", PageKind.LOCAL)
    if req.chat is None:
        raise UsageError(
            "name a chat: Telegram has no method that lists live shares across chats",
            field="chat",
        )
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    rows = await _recent(ctx, peer, chat_id, limit=100)
    if req.mine:
        rows = [row for row in rows if row.mine]

    offset = int(state.get("offset") or 0)
    page = rows[offset : offset + limit]
    return build_page(
        page,
        op="location.live.list",
        kind=PageKind.LOCAL,
        state={"offset": offset + len(page)},
        account=ctx.account,
        has_more=offset + len(page) < len(rows),
        total=len(rows),
    )


SPEC_LIVE_LIST = OperationSpec(
    id="location.live.list",
    request=LiveListReq,
    response=Page[LiveLocation],
    impl=live_list,
    summary="Live locations shared in a chat",
    description=(
        "`messages.getRecentLocations` answers per chat and there is no "
        "cross-chat method, so a chat is required rather than optional."
    ),
    paginated=PageKind.LOCAL,
    columns=("chat_id", "msg_id", "geo.lat", "geo.lon", "expires_at"),
    example={"items": [_EXAMPLE_LIVE], "has_more": False},
    example_args="location live list @alice",
    covers=("location.live-list-mine", "location.recent-in-chat"),
    coverage_note=(
        "`location.live-list-mine` is answered per chat with `--mine`: there "
        "is no MTProto method that enumerates a user's own live shares."
    ),
)


# ---------------------------------------------------------------------------
# location nearby list
# ---------------------------------------------------------------------------


class NearbyListReq(Request):
    lat: Annotated[float, arg(0, metavar="LAT", help="Latitude.")]
    lon: Annotated[float, arg(1, metavar="LON", help="Longitude.")]
    accuracy: Annotated[
        int | None, opt("--accuracy", metavar="METRES", help="accuracy_radius.")
    ] = None
    publish: Annotated[
        str | None,
        opt("--publish", metavar="DURATION", help="Make myself visible for this long."),
    ] = None
    background: Annotated[
        bool, opt("--background", help="Background refresh, without a visible listing.")
    ] = False
    unpublish: Annotated[bool, opt("--unpublish", help="Stop sharing (self_expires=0).")] = False


async def nearby_list(ctx: OpContext, req: NearbyListReq) -> Nearby:
    """People and groups near a point, and optionally publish my own location.

    Publishing is a privacy-relevant write, which is why it is opt-in
    (`--publish`) rather than a side effect of looking: `contacts.getLocated`
    with `self_expires` set is what puts you on other people's lists.
    """
    from telethon.tl.functions import contacts as fn

    self_expires: int | None = None
    if req.unpublish:
        self_expires = 0
    elif req.publish:
        self_expires = int(parse_duration(req.publish) or 3600)

    result = await client(ctx)(
        fn.GetLocatedRequest(
            geo_point=_point(req.lat, req.lon, req.accuracy),
            background=req.background or None,
            self_expires=self_expires,
        )
    )
    items: list[NearbyPeer] = []
    for update in getattr(result, "updates", None) or []:
        for located in getattr(update, "peers", None) or []:
            if type(located).__name__ == "PeerSelfLocated":
                continue
            peer_id = peer_id_of(getattr(located, "peer", None)) or 0
            expires = getattr(located, "expires", None)
            items.append(
                NearbyPeer(
                    peer_id=peer_id,
                    kind="user" if peer_id > 0 else "group",
                    distance=getattr(located, "distance", None),
                    expires=fmt_dt(expires),
                    expires_unix=to_unix(expires),
                )
            )
    if self_expires:
        ctx.warn("your location is now visible to people nearby until it expires")
    return Nearby(items=items, self_expires=self_expires, published=bool(self_expires))


SPEC_NEARBY_LIST = OperationSpec(
    id="location.nearby.list",
    request=NearbyListReq,
    response=Nearby,
    impl=nearby_list,
    summary="People and groups near a point",
    description=(
        "`--publish` puts your own position on other people's lists for the "
        "duration given, and `--unpublish` takes it off again. Looking never "
        "publishes."
    ),
    mutating=True,
    destructive=True,
    rate_class="resolve",
    columns=("published", "self_expires"),
    example={
        "items": [{"peer_id": 4242, "kind": "user", "distance": 120}],
        "published": False,
    },
    example_args="location nearby list 52.5200 13.4050",
    covers=(
        "contacts-users.nearby-publish",
        "contacts-users.nearby-stop",
        "location.people-nearby",
    ),
)


# ---------------------------------------------------------------------------
# location search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    lat: Annotated[float, arg(0, metavar="LAT", help="Latitude.")]
    lon: Annotated[float, arg(1, metavar="LON", help="Longitude.")]
    query: Annotated[str, arg(2, metavar="QUERY", required=False, help="What to look for.")] = ""
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Peer context for the inline query."),
    ] = None
    provider: Annotated[
        str | None,
        opt("--provider", metavar="USERNAME", help="Override config.venue_search_username."),
    ] = None


async def search(ctx: OpContext, req: SearchReq) -> Page[Venue]:
    """Search nearby places.

    Venue search is not an API method: it is an inline query against the bot
    `help.getConfig().venue_search_username` (Foursquare by default) carrying
    a geo point. That is why it needs a peer for context and why the results
    come back as `botInlineMessageMediaVenue`.
    """
    from telethon.tl.functions import help as help_fn
    from telethon.tl.functions import messages as fn

    limit, state = window(ctx, "location.search", PageKind.PARTICIPANTS)
    provider = req.provider
    if not provider:
        config = await client(ctx)(help_fn.GetConfigRequest())
        provider = getattr(config, "venue_search_username", None)
    if not provider:
        raise NotSupportedError(
            "this account's server config names no venue search bot "
            "(help.config.venue_search_username), so there is nowhere to ask"
        )

    context = await _send.resolve(ctx, req.chat) if req.chat is not None else None
    if context is None:
        from telethon.tl import types

        context = types.InputPeerSelf()
    bot = await client(ctx).get_input_entity(provider)
    result = await client(ctx)(
        fn.GetInlineBotResultsRequest(
            bot=bot,
            peer=context,
            query=req.query,
            offset=state.get("offset") or "",
            geo_point=_point(req.lat, req.lon),
        )
    )
    items: list[Venue] = []
    for row in getattr(result, "results", None) or []:
        message = getattr(row, "send_message", None)
        if type(message).__name__ not in ("BotInlineMessageMediaVenue",):
            continue
        items.append(
            Venue(
                title=str(getattr(message, "title", "") or ""),
                address=str(getattr(message, "address", "") or ""),
                provider=str(getattr(message, "provider", "") or ""),
                venue_id=str(getattr(message, "venue_id", "") or ""),
                venue_type=str(getattr(message, "venue_type", "") or ""),
                geo=geo_model(getattr(message, "geo", None)),
            )
        )
    next_offset = getattr(result, "next_offset", None)
    return build_page(
        items[:limit],
        op="location.search",
        kind=PageKind.PARTICIPANTS,
        state={"offset": next_offset},
        account=ctx.account,
        has_more=bool(next_offset),
    )


SPEC_SEARCH = OperationSpec(
    id="location.search",
    request=SearchReq,
    response=Page[Venue],
    impl=search,
    summary="Search nearby places (venue provider inline bot)",
    description=(
        "There is no venue-search method: this is an inline query against "
        "`help.config.venue_search_username` with a geo point attached."
    ),
    paginated=PageKind.PARTICIPANTS,
    rate_class="resolve",
    columns=("title", "address", "venue_id"),
    example={
        "items": [
            {
                "title": "Brandenburg Gate",
                "address": "Pariser Platz",
                "provider": "foursquare",
                "venue_id": "4ac518",
            }
        ],
        "has_more": False,
    },
    example_args="location search 52.5200 13.4050 museum",
    covers=("location.venue-search",),
)


# ---------------------------------------------------------------------------
# location preview
# ---------------------------------------------------------------------------


class PreviewReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Location message id.")]
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Write the PNG here.")
    ] = None
    stdout: Annotated[bool, opt("--stdout", help="Return the image base64 instead.")] = False
    zoom: Annotated[int, opt("--zoom", metavar="N", help="13-20.", ge=13, le=20)] = 15
    size: Annotated[str, opt("--size", metavar="WxH", help="16-1024 per side.")] = "512x512"
    scale: Annotated[int, opt("--scale", metavar="N", help="Pixel density 1-3.", ge=1, le=3)] = 2


def _size(value: str) -> tuple[int, int]:
    width, sep, height = value.lower().partition("x")
    if not sep:
        raise UsageError("--size wants WxH", field="size")
    try:
        w, h = int(width), int(height)
    except ValueError as exc:
        raise UsageError(f"--size {value!r} is not WxH", field="size") from exc
    if not (16 <= w <= 1024 and 16 <= h <= 1024):
        raise UsageError("--size is 16..1024 on each side", field="size")
    return w, h


async def preview(ctx: OpContext, req: PreviewReq) -> MapPreview:
    """Render the map thumbnail for a location message.

    Two things make this different from every other download: it needs the
    `geoPoint.access_hash` off the *received* message (a point built from
    coordinates will not do), and it is served by the webfile data centre
    named in `config.webfile_dc_id`, not the home DC.
    """
    import base64 as b64
    from pathlib import Path

    from telethon.tl import types
    from telethon.tl.functions import help as help_fn
    from telethon.tl.functions import upload as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    message = await client(ctx).get_messages(peer, ids=req.msg_id)
    media = getattr(message, "media", None) if message is not None else None
    geo = getattr(media, "geo", None)
    point = geo_model(geo)
    if point is None:
        raise NotFoundError(f"message {req.msg_id} in {chat_id} carries no location")
    if point.access_hash is None:
        raise NotSupportedError(
            "this location has no access hash, which the map endpoint requires; "
            "it can only be rendered for a location tlgr actually received"
        )

    width, height = _size(req.size)
    location = types.InputWebFileGeoPointLocation(
        geo_point=types.InputGeoPoint(lat=point.lat, long=point.lon),
        access_hash=point.access_hash,
        w=width,
        h=height,
        zoom=req.zoom,
        scale=req.scale,
    )
    config = await client(ctx)(help_fn.GetConfigRequest())
    dc_id = int(getattr(config, "webfile_dc_id", 0) or 0)
    request = fn.GetWebFileRequest(location=location, offset=0, limit=1024 * 1024)

    handle = client(ctx)
    sender = await handle._borrow_exported_sender(dc_id)
    try:
        result = await sender.send(request)
    finally:
        await handle._return_exported_sender(sender)

    payload = bytes(getattr(result, "bytes", b"") or b"")
    preview = MapPreview(
        chat_id=chat_id,
        msg_id=req.msg_id,
        bytes=len(payload),
        mime_type=getattr(result, "mime_type", None),
        zoom=req.zoom,
        size=req.size,
        scale=req.scale,
    )
    if req.stdout or not req.out:
        preview.base64 = b64.b64encode(payload).decode()
    else:
        path = Path(req.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        preview.path = str(path)
    return preview


SPEC_PREVIEW = OperationSpec(
    id="location.preview",
    request=PreviewReq,
    response=MapPreview,
    impl=preview,
    summary="Render the map thumbnail for a location message",
    description=(
        "Served by the webfile data centre, not the home DC, and it needs the "
        "`geoPoint.access_hash` carried by the received message."
    ),
    rate_class="file",
    columns=("chat_id", "msg_id", "bytes", "path"),
    example={"chat_id": 777123, "msg_id": 12345, "bytes": 20480, "path": "/tmp/map.png"},
    example_args="location preview @alice 12345 --out /tmp/map.png",
    covers=("location.map-preview",),
    timeout_s=180,
)
