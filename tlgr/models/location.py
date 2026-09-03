"""Locations: points, venues, live shares and who is nearby.

`GeoPoint` carries `access_hash` because the map-thumbnail endpoint needs it,
and it is exactly the kind of value `--redact` exists for: it is a capability
token for a place somebody is standing.
"""

from __future__ import annotations

from typing import Literal

from tlgr.models.base import Model

__all__ = [
    "GeoPoint",
    "LiveLocation",
    "LiveStopped",
    "MapPreview",
    "Nearby",
    "NearbyPeer",
    "SentLocation",
    "Venue",
]


class GeoPoint(Model):
    lat: float = 0.0
    lon: float = 0.0
    accuracy: int | None = None
    access_hash: int | None = None


class Venue(Model):
    title: str = ""
    address: str = ""
    provider: str = ""
    venue_id: str = ""
    venue_type: str = ""
    geo: GeoPoint | None = None


class SentLocation(Model):
    """What a `location send` / `location venue send` produced."""

    id: int = 0
    chat_id: int = 0
    date: str = ""
    date_unix: int = 0
    geo: GeoPoint | None = None
    venue: Venue | None = None


class LiveLocation(Model):
    """One live share — mine or somebody else's.

    `expires_at` rather than `period` alone: a caller resuming after a restart
    needs to know whether the share is still running, and a duration measured
    from a start time it never saw does not answer that.
    """

    chat_id: int = 0
    msg_id: int = 0
    id: int = 0
    peer_id: int | None = None
    geo: GeoPoint | None = None
    heading: int | None = None
    proximity: int | None = None
    period: int | None = None
    expires_at: str | None = None
    expires_at_unix: int | None = None
    stopped: bool = False
    mine: bool = False
    date: str = ""
    date_unix: int = 0


class LiveStopped(Model):
    """`location live stop`, which may have stopped several shares at once."""

    stopped: bool = False
    count: int = 0
    items: list[LiveLocation] = []
    already: bool = False


class NearbyPeer(Model):
    """A `updatePeerLocated` row: a person or a group near a point."""

    peer_id: int = 0
    kind: Literal["user", "group", "channel", "self"] = "user"
    distance: int | None = None
    expires: str | None = None
    expires_unix: int | None = None


class Nearby(Model):
    """The people-nearby answer, plus what I published about myself."""

    items: list[NearbyPeer] = []
    self_expires: int | None = None
    published: bool = False


class MapPreview(Model):
    """The rendered map thumbnail for a location message."""

    chat_id: int = 0
    msg_id: int = 0
    path: str | None = None
    bytes: int = 0
    mime_type: str | None = None
    zoom: int = 15
    size: str = "512x512"
    scale: int = 2
    base64: str | None = None
