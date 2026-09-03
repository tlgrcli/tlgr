"""Network, data-centre, proxy and server-configuration shapes.

The distinction these encode is the one v1 never made: **the server's
configuration** (`help.getConfig`, `help.getAppConfig`) is not the same thing
as **the connection** (which DC, which transport, how far the clock is off),
and neither is **the proxy** (a client-side list with credentials in a 0600
file). Three nouns, three shapes.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "AppConfigDoc",
    "Country",
    "CountryCode",
    "DcOption",
    "InfoTopic",
    "NearestDc",
    "NetStatus",
    "NetUsage",
    "PingResult",
    "PromoData",
    "Proxy",
    "ProxyLink",
    "ProxyProbe",
    "ProxySelection",
    "ServerConfig",
    "SyncCursors",
]


class DcOption(Model):
    id: int
    ip_address: str = ""
    port: int = 0
    ipv6: bool = False
    media_only: bool = False
    tcpo_only: bool = False
    cdn: bool = False
    static: bool = False
    this_port_only: bool = False
    secret: str | None = None
    current: bool = False


class NearestDc(Model):
    country: str = ""
    this_dc: int = 0
    nearest_dc: int = 0
    current_dc: int | None = None


class PingResult(Model):
    account: str = ""
    dc_id: int | None = None
    proxy: str | None = None
    probes: int = 0
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None
    loss: float = 0.0


class SyncCursors(Model):
    """pts/qts/seq/date, the four numbers the update transport turns on."""

    pts: int | None = None
    qts: int | None = None
    seq: int | None = None
    date: str | None = None
    date_unix: int | None = None


class NetStatus(Model):
    account: str = ""
    authorized: bool = False
    connected: bool = False
    phase: str = "disconnected"
    dc_id: int | None = None
    dc_address: str | None = None
    ipv6: bool = False
    transport: str = ""
    proxy: str | None = None
    ping_ms: float | None = None
    layer: int = 0
    #: |offset| > 30 s is reported as a warning: it pushes msg_ids outside the
    #: server's window, and requests are then dropped with no error at all.
    time_offset_seconds: int = 0
    exported_senders: int = 0
    reconnects: int = 0
    last_error: str | None = None
    state: SyncCursors | None = None
    behind_seconds: int | None = None
    frozen: bool = False


class NetUsage(Model):
    account: str = ""
    since: str = ""
    rpc_bytes_sent: int = 0
    rpc_bytes_received: int = 0
    download_bytes: int = 0
    upload_bytes: int = 0
    requests: int = 0
    updates_received: int = 0
    reconnects: int = 0


class Proxy(Model):
    """A saved proxy. Credentials live in a 0600 file and are never printed."""

    id: str
    name: str = ""
    type: str = "socks5"
    host: str = ""
    port: int = 0
    user: str | None = None
    rdns: bool = True
    active: bool = False
    order: int = 0
    last_ping_ms: float | None = None
    last_ok_at: str | None = None
    failures: int = 0
    has_password: bool = False
    has_secret: bool = False


class ProxyProbe(Model):
    id: str = ""
    name: str = ""
    ok: bool = False
    ping_ms: float | None = None
    dc_id: int | None = None
    error: str | None = None


class ProxyLink(Model):
    id: str
    link: str
    type: str = ""
    host: str = ""
    port: int = 0
    qr: str | None = None


class ProxySelection(Model):
    active: str | None = None
    type: str = ""
    host: str = ""
    port: int = 0
    reconnected: bool = False
    accounts: list[str] = []
    removed: bool = False
    was_active: bool = False


class ServerConfig(Model):
    """`help.getConfig`, the fields a client actually acts on."""

    expires: str | None = None
    test_mode: bool = False
    this_dc: int = 0
    date: str | None = None
    date_unix: int | None = None
    chat_size_max: int = 0
    megagroup_size_max: int = 0
    message_length_max: int = 0
    caption_length_max: int = 0
    online_update_period_ms: int = 0
    offline_blur_timeout_ms: int = 0
    offline_idle_timeout_ms: int = 0
    edit_time_limit: int = 0
    revoke_time_limit: int = 0
    rating_e_decay: int = 0
    forwarded_count_max: int = 0
    push_chat_period_ms: int = 0
    dc_options: list[DcOption] = []
    values: dict[str, Any] = {}


class AppConfigDoc(Model):
    """`help.getAppConfig`, hash-cached, with the freeze fields lifted out."""

    hash: int = 0
    not_modified: bool = False
    freeze_since_date: str | None = None
    freeze_until_date: str | None = None
    freeze_appeal_url: str | None = None
    values: dict[str, Any] = {}
    config: ServerConfig | None = None


class CountryCode(Model):
    country_code: str = ""
    prefixes: list[str] = []
    patterns: list[str] = []


class Country(Model):
    iso2: str
    name: str = ""
    default_name: str = ""
    hidden: bool = False
    flag_emoji: str = ""
    preferred_language: str = ""
    codes: list[CountryCode] = []
    #: Filled only by `--phone`: what the number was classified as.
    matched_prefix: str | None = None
    valid: bool | None = None


class InfoTopic(Model):
    """One of the flat `help.*` / `langpack.*` read-only endpoints."""

    topic: str
    items: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}


class PromoData(Model):
    kind: str = "none"
    chat_id: int | None = None
    psa_type: str | None = None
    psa_message: str | None = None
    proxy: bool = False
    expires: str | None = None
    hidden: bool = False
    pending_suggestions: list[str] = []
