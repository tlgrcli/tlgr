"""The `proxy` group: saved proxies, and which one the daemon connects through.

A Telethon client takes its proxy at construction time and cannot change it,
so "switch proxy" is really "rebuild the client, reconnect, catch up". That is
why `proxy set` is a daemon operation with a reconnect in it rather than a
config write, and why `proxy test` uses a throwaway session: a probe that
became the account's update-receiving connection would divert its events.

Credentials live in `~/.tlgr/proxies.json` at mode 0600 and never appear in
argv, in a list, or in a log. `proxy link` is the single command that prints
them, and it says so.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode, urlparse

from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.net import Proxy, ProxyLink, ProxyProbe, ProxySelection
from tlgr.models.page import Page
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_TYPES = ("socks5", "http", "mtproxy")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def _store_path() -> Any:
    from tlgr.core.paths import TlgrPaths

    return TlgrPaths().proxies


def _load() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"active": None, "proxies": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise UsageError(f"{path} must be a JSON object")
    loaded.setdefault("proxies", [])
    loaded.setdefault("active", None)
    return loaded


def _save(document: dict[str, Any]) -> None:
    from tlgr.core.paths import write_private

    write_private(_store_path(), json.dumps(document, indent=2))


def _entry(document: dict[str, Any], identifier: str) -> dict[str, Any]:
    for entry in document["proxies"]:
        if isinstance(entry, dict) and (
            entry.get("id") == identifier or entry.get("name") == identifier
        ):
            return entry
    raise NotFoundError(f"no saved proxy {identifier!r}. Run: tlgr proxy list")


def _model(entry: dict[str, Any], active: str | None) -> Proxy:
    return Proxy(
        id=str(entry.get("id", "")),
        name=str(entry.get("name", "") or ""),
        type=str(entry.get("type", "socks5")),
        host=str(entry.get("host", "")),
        port=int(entry.get("port", 0) or 0),
        user=entry.get("user") or None,
        rdns=bool(entry.get("rdns", True)),
        active=entry.get("id") == active,
        order=int(entry.get("order", 0) or 0),
        last_ping_ms=entry.get("last_ping_ms"),
        last_ok_at=entry.get("last_ok_at"),
        failures=int(entry.get("failures", 0) or 0),
        has_password=bool(entry.get("password")),
        has_secret=bool(entry.get("secret")),
    )


def _next_id(document: dict[str, Any]) -> str:
    used = {str(entry.get("id", "")) for entry in document["proxies"]}
    index = 1
    while f"p{index}" in used:
        index += 1
    return f"p{index}"


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def parse_proxy_link(link: str) -> dict[str, Any]:
    """`tg://proxy?…` or `https://t.me/proxy?…` → the fields of one proxy.

    Both spellings and both secret encodings (hex and base64url, with the
    `dd`/`ee` prefixes) are accepted, because a user pastes whatever their
    channel gave them and a parser that took only one form would be a puzzle
    rather than a feature.
    """
    parsed = urlparse(link.strip())
    kind = (parsed.netloc or parsed.path.strip("/")).lower()
    if parsed.scheme not in ("tg", "http", "https") or kind not in ("proxy", "socks"):
        raise UsageError(f"{link!r} is not a tg://proxy or t.me/proxy link", field="link")
    query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
    server = query.get("server")
    port = query.get("port")
    if not server or not port:
        raise UsageError("a proxy link needs both `server` and `port`", field="link")
    entry: dict[str, Any] = {"host": server, "port": int(port)}
    if query.get("secret"):
        entry["type"] = "mtproxy"
        entry["secret"] = query["secret"]
    else:
        entry["type"] = "socks5"
        if query.get("user"):
            entry["user"] = query["user"]
        if query.get("pass"):
            entry["password"] = query["pass"]
    return entry


def _to_link(entry: dict[str, Any], form: str) -> str:
    query: dict[str, str] = {
        "server": str(entry.get("host", "")),
        "port": str(entry.get("port", 0)),
    }
    if entry.get("type") == "mtproxy":
        query["secret"] = str(entry.get("secret", ""))
    else:
        if entry.get("user"):
            query["user"] = str(entry["user"])
        if entry.get("password"):
            query["pass"] = str(entry["password"])
    base = "https://t.me/proxy" if form == "t.me" else "tg://proxy"
    return f"{base}?{urlencode(query)}"


def _telethon_proxy(entry: dict[str, Any]) -> tuple[Any, Any]:
    """`(proxy, connection class)` for a Telethon client.

    MTProxy is a different *connection*, not a different proxy tuple, which is
    the part that catches people out: passing an MTProxy secret as a SOCKS
    password produces a connection that fails with nothing that names the
    cause.
    """
    kind = str(entry.get("type", "socks5"))
    if kind == "mtproxy":
        from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

        return (
            (str(entry.get("host", "")), int(entry.get("port", 0)), str(entry.get("secret", ""))),
            ConnectionTcpMTProxyRandomizedIntermediate,
        )
    proxy: dict[str, Any] = {
        "proxy_type": kind,
        "addr": str(entry.get("host", "")),
        "port": int(entry.get("port", 0)),
        "rdns": bool(entry.get("rdns", True)),
    }
    if entry.get("user"):
        proxy["username"] = str(entry["user"])
    if entry.get("password"):
        proxy["password"] = str(entry["password"])
    return proxy, None


def _proxy_url(entry: dict[str, Any]) -> str:
    """The `[network] proxy` spelling `SessionManager._proxy_tuple` parses."""
    kind = str(entry.get("type", "socks5"))
    host = str(entry.get("host", ""))
    port = int(entry.get("port", 0))
    if kind == "mtproxy":
        return f"mtproxy://{host}:{port}#{entry.get('secret', '')}"
    credentials = ""
    if entry.get("user"):
        credentials = str(entry["user"])
        if entry.get("password"):
            credentials += f":{entry['password']}"
        credentials += "@"
    return f"{kind}://{credentials}{host}:{port}"


# ---------------------------------------------------------------------------
# proxy add
# ---------------------------------------------------------------------------


class ProxyAddReq(Request):
    link: Annotated[
        str | None,
        arg(0, metavar="LINK", required=False, help="tg://proxy?… or https://t.me/proxy?…"),
    ] = None
    type: Annotated[str | None, choice(*_TYPES, help="Proxy kind.")] = None
    host: Annotated[str | None, opt("--host", metavar="HOST")] = None
    port: Annotated[int | None, opt("--port", metavar="PORT")] = None
    user: Annotated[str | None, opt("--user", metavar="NAME", help="SOCKS5/HTTP username.")] = None
    password: Annotated[
        str | None,
        opt(
            "--password",
            secret=True,
            envvar="TLGR_PROXY_PASSWORD",
            help="SOCKS5/HTTP password.",
        ),
    ] = None
    secret: Annotated[
        str | None,
        opt(
            "--secret",
            secret=True,
            envvar="TLGR_PROXY_SECRET",
            help="MTProxy secret (hex or base64url).",
        ),
    ] = None
    name: Annotated[str | None, opt("--name", metavar="LABEL")] = None
    rdns: Annotated[bool, opt("--rdns/--no-rdns", help="Resolve hostnames through the proxy.")] = (
        True
    )
    activate: Annotated[
        bool, opt("--set", help="Make it the active proxy immediately (reconnects).")
    ] = False


async def proxy_add(ctx: OpContext, req: ProxyAddReq) -> Proxy:
    """Save a proxy, from a link or from flags.

    Secrets never arrive as argv — `ps` is world-readable and shell history is
    forever — so `--password` and `--secret` are the `-env`/`-stdin`/`-file`
    triples every secret field generates (STYLE §3).
    """
    entry: dict[str, Any] = {}
    if req.link:
        entry.update(parse_proxy_link(req.link))
    if req.type:
        entry["type"] = req.type
    if req.host:
        entry["host"] = req.host
    if req.port:
        entry["port"] = req.port
    if req.user:
        entry["user"] = req.user
    if req.password:
        entry["password"] = req.password
    if req.secret:
        entry["secret"] = req.secret
        entry.setdefault("type", "mtproxy")
    entry.setdefault("type", "socks5")
    entry["rdns"] = req.rdns
    if req.name:
        entry["name"] = req.name

    if not entry.get("host") or not entry.get("port"):
        raise UsageError(
            "a proxy needs a host and a port: pass a tg://proxy link, or --host and --port",
            field="host",
        )
    if entry["type"] == "mtproxy" and not entry.get("secret"):
        raise UsageError(
            "an MTProxy needs its secret: --secret-env, --secret-stdin or --secret-file",
            field="secret",
        )

    document = _load()
    entry["id"] = _next_id(document)
    entry["order"] = len(document["proxies"])
    document["proxies"].append(entry)
    if req.activate:
        document["active"] = entry["id"]
    _save(document)

    model = _model(entry, document["active"])
    if req.activate:
        _write_network_proxy(_proxy_url(entry))
        ctx.warn("the daemon adopts the new proxy on its next reconnect: tlgr daemon reconnect")
    if entry["type"] == "mtproxy":
        ctx.warn(
            "Telethon's MTProxy support is experimental and cannot use proxies that require SSL"
        )
    return model


def _write_network_proxy(url: str) -> None:
    from tlgr.core.config import _load_toml, _save_toml
    from tlgr.core.paths import TlgrPaths

    path = TlgrPaths().config
    document = _load_toml(path)
    document.setdefault("network", {})["proxy"] = url
    _save_toml(path, document)


SPEC_PROXY_ADD = OperationSpec(
    id="proxy.add",
    request=ProxyAddReq,
    response=Proxy,
    impl=proxy_add,
    summary="Save a proxy",
    description=(
        "Accepts both `tg://proxy?…` and `https://t.me/proxy?…`, and both "
        "secret encodings. Secrets are read from an environment variable, a "
        "file or stdin — never argv."
    ),
    aliases=("net.proxy.add",),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=30,
    columns=("id", "name", "type", "host", "port", "active"),
    example={"id": "p1", "type": "socks5", "host": "10.0.0.5", "port": 1080, "active": False},
    example_args="proxy add 'tg://proxy?server=1.2.3.4&port=443&secret=dd00' --set",
    covers=("updates.net-proxy-mtproxy", "updates.net-proxy-socks5"),
    covers_partial=(
        "updates.net-proxy-http",
        "updates.net-proxy-list",
        "updates.net-proxy-share-link",
    ),
    coverage_note=(
        "saves one; choosing it is `proxy set`, listing is `proxy list`, and "
        "printing the shareable link is `proxy link`."
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# proxy list / remove / link
# ---------------------------------------------------------------------------


class ProxyListReq(Request):
    type: Annotated[str | None, choice(*_TYPES, help="Filter by kind.")] = None
    active_only: Annotated[bool, opt("--active-only", help="Only the proxy currently in use.")] = (
        False
    )


async def proxy_list(ctx: OpContext, req: ProxyListReq) -> Page[Proxy]:
    """Saved proxies. Credentials are reported as present, never printed."""
    document = _load()
    rows = [_model(entry, document["active"]) for entry in document["proxies"]]
    if req.type:
        rows = [row for row in rows if row.type == req.type]
    if req.active_only:
        rows = [row for row in rows if row.active]
    rows.sort(key=lambda row: row.order)
    return build_page(rows, op="proxy.list", kind=PageKind.LOCAL, has_more=False, total=len(rows))


SPEC_PROXY_LIST = OperationSpec(
    id="proxy.list",
    request=ProxyListReq,
    response=Page[Proxy],
    impl=proxy_list,
    summary="List saved proxies",
    description=(
        "`order` is the failover order. `has_password`/`has_secret` say a "
        "credential exists without printing it; only `proxy link` does that."
    ),
    aliases=("net.proxy.list",),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("id", "name", "type", "host", "port", "active", "last_ping_ms"),
    example={
        "items": [{"id": "p1", "type": "socks5", "host": "10.0.0.5", "port": 1080}],
        "has_more": False,
    },
    example_args="proxy list",
    covers_partial=("updates.net-proxy-autoswitch", "updates.net-proxy-list"),
    coverage_note="lists them; failover is `proxy test --reorder` plus `proxy set`.",
    tags=frozenset({"agent-safe"}),
)


class ProxyIdReq(Request):
    id: Annotated[str, arg(0, metavar="ID")]


async def proxy_remove(ctx: OpContext, req: ProxyIdReq) -> ProxySelection:
    """Delete a saved proxy, and say whether it was the active one."""
    document = _load()
    entry = _entry(document, req.id)
    was_active = document["active"] == entry.get("id")
    document["proxies"] = [row for row in document["proxies"] if row is not entry]
    if was_active:
        document["active"] = None
        _write_network_proxy("")
        ctx.warn(
            "the active proxy was removed; the daemon falls back to a direct "
            "connection on its next reconnect"
        )
    _save(document)
    return ProxySelection(removed=True, was_active=was_active, active=document["active"])


SPEC_PROXY_REMOVE = OperationSpec(
    id="proxy.remove",
    request=ProxyIdReq,
    response=ProxySelection,
    impl=proxy_remove,
    summary="Delete a saved proxy",
    aliases=("net.proxy.remove",),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=15,
    example={"removed": True, "was_active": False},
    example_args="proxy remove p1",
    covers=("updates.net-proxy-list",),
    tags=frozenset({"agent-safe"}),
)


class ProxyLinkReq(Request):
    id: Annotated[str, arg(0, metavar="ID")]
    form: Annotated[str, choice("tg", "t.me", help="Link flavour.")] = "tg"


async def proxy_link(ctx: OpContext, req: ProxyLinkReq) -> ProxyLink:
    """Print a saved proxy as a shareable link.

    The only command that prints proxy credentials, and it is destructive in
    the sense that matters: the link *is* the password. It is marked so that
    it asks off a TTY.
    """
    document = _load()
    entry = _entry(document, req.id)
    ctx.warn("this link contains the proxy's credentials; treat it as a secret")
    return ProxyLink(
        id=str(entry.get("id", "")),
        link=_to_link(entry, req.form),
        type=str(entry.get("type", "")),
        host=str(entry.get("host", "")),
        port=int(entry.get("port", 0) or 0),
    )


SPEC_PROXY_LINK = OperationSpec(
    id="proxy.link",
    request=ProxyLinkReq,
    response=ProxyLink,
    impl=proxy_link,
    summary="Print a saved proxy as a shareable tg:// link",
    description="The link embeds the password or MTProxy secret. Confirm off a TTY.",
    aliases=("net.proxy.link", "proxy.export"),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=15,
    columns=("id", "link"),
    example={"id": "p1", "link": "tg://proxy?server=1.2.3.4&port=443&secret=dd00"},
    example_args="proxy link p1 --yes",
    covers=("updates.net-proxy-share-link",),
    tags=frozenset({"agent-safe", "mutating-checked"}),
)


# ---------------------------------------------------------------------------
# proxy set
# ---------------------------------------------------------------------------


class ProxySetReq(Request):
    id: Annotated[
        str,
        arg(0, metavar="ID", help="A saved id, `none` for a direct connection, or `system`."),
    ]
    reconnect: Annotated[
        bool, opt("--reconnect/--no-reconnect", help="Reconnect now rather than on the next start.")
    ] = True


async def proxy_set(ctx: OpContext, req: ProxySetReq) -> ProxySelection:
    """Choose the proxy the daemon connects through, and reconnect.

    A Telethon client takes its proxy at construction, so switching means
    rebuilding the client and reconnecting — and then catching up, because the
    account was deaf for the moment it took.

    `system` reads `ALL_PROXY`/`HTTPS_PROXY`. `NO_PROXY` is meaningless here —
    there is exactly one destination — and is documented as ignored rather
    than silently honoured.
    """
    document = _load()
    if req.id == "none":
        document["active"] = None
        _save(document)
        _write_network_proxy("")
        selection = ProxySelection(active=None)
    elif req.id == "system":
        url = _system_proxy()
        if not url:
            raise NotFoundError(
                "no ALL_PROXY or HTTPS_PROXY is set in the environment; "
                "NO_PROXY is ignored (there is one destination)"
            )
        document["active"] = None
        _save(document)
        _write_network_proxy(url)
        parsed = urlparse(url)
        selection = ProxySelection(
            active="system",
            type=parsed.scheme,
            host=parsed.hostname or "",
            port=int(parsed.port or 0),
        )
    else:
        entry = _entry(document, req.id)
        document["active"] = entry.get("id")
        _save(document)
        _write_network_proxy(_proxy_url(entry))
        selection = ProxySelection(
            active=str(entry.get("id", "")),
            type=str(entry.get("type", "")),
            host=str(entry.get("host", "")),
            port=int(entry.get("port", 0) or 0),
        )

    if req.reconnect:
        selection.accounts = await _reconnect_all(ctx)
        selection.reconnected = bool(selection.accounts)
    if not selection.reconnected:
        ctx.warn("the proxy is saved; the daemon adopts it on its next reconnect")
    return selection


def _system_proxy() -> str:
    import os

    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


async def _reconnect_all(ctx: OpContext) -> list[str]:
    """Rebuild every session so the new proxy takes effect, then catch up."""
    daemon = getattr(ctx, "daemon", None)
    sessions = getattr(daemon, "sessions", None)
    if sessions is None:
        return []
    reconnected: list[str] = []
    for alias in list(getattr(sessions, "aliases", []) or []):
        with contextlib.suppress(Exception):
            await sessions.release(alias)
            session = await sessions.ensure(alias)
            await session.catch_up()
            reconnected.append(alias)
    return reconnected


SPEC_PROXY_SET = OperationSpec(
    id="proxy.set",
    request=ProxySetReq,
    response=ProxySelection,
    impl=proxy_set,
    summary="Choose the proxy the daemon connects through",
    description=(
        "`none` is a direct connection; `system` reads ALL_PROXY/HTTPS_PROXY. "
        "A Telethon client cannot change proxy in place, so this rebuilds the "
        "client, reconnects and catches up."
    ),
    aliases=("net.proxy.set", "proxy.enable", "proxy.off"),
    mutating=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=180,
    columns=("active", "type", "host", "port", "reconnected"),
    example={"active": "p1", "type": "socks5", "host": "10.0.0.5", "reconnected": True},
    example_args="proxy set p1",
    covers=("updates.net-proxy-http", "updates.net-proxy-system"),
    covers_partial=(
        "updates.net-proxy-list",
        "updates.net-proxy-mtproxy",
        "updates.net-proxy-socks5",
    ),
    coverage_note="selects one; saving and describing them is `proxy add`/`proxy list`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# proxy test
# ---------------------------------------------------------------------------


class ProxyTestReq(Request):
    id: Annotated[str | None, arg(0, metavar="ID", required=False)] = None
    every: Annotated[bool, opt("--every", help="Test every saved proxy.")] = False
    probe_timeout: Annotated[int, opt("--probe-timeout", metavar="SECONDS", ge=1, le=120)] = 10
    reorder: Annotated[
        bool, opt("--reorder", help="Rewrite the failover order by measured latency.")
    ] = False


async def proxy_test(ctx: OpContext, req: ProxyTestReq) -> Page[ProxyProbe]:
    """Probe a proxy and measure its latency.

    Through a *scratch* session, deliberately. Updates go to the last active
    connection, so a probe built on the account's real session could quietly
    divert its events to a client that is about to be thrown away.
    """
    document = _load()
    if req.every:
        entries = [entry for entry in document["proxies"] if isinstance(entry, dict)]
    elif req.id:
        entries = [_entry(document, req.id)]
    elif document["active"]:
        entries = [_entry(document, str(document["active"]))]
    else:
        raise UsageError("name a proxy id, or pass --every", field="id")

    rows: list[ProxyProbe] = []
    for entry in entries:
        rows.append(await _probe(ctx, entry, req.probe_timeout))

    for row, entry in zip(rows, entries, strict=False):
        if row.ok:
            entry["last_ping_ms"] = row.ping_ms
            entry["last_ok_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["failures"] = 0
        else:
            entry["failures"] = int(entry.get("failures", 0) or 0) + 1

    if req.reorder:
        ranked = sorted(
            document["proxies"],
            key=lambda entry: (entry.get("last_ping_ms") is None, entry.get("last_ping_ms") or 0),
        )
        for order, entry in enumerate(ranked):
            entry["order"] = order
        document["proxies"] = ranked
    _save(document)

    if not any(row.ok for row in rows):
        ctx.warn("no proxy answered; the network may be blocking them all")
    return build_page(rows, op="proxy.test", kind=PageKind.LOCAL, has_more=False, total=len(rows))


async def _probe(ctx: OpContext, entry: dict[str, Any], timeout: int) -> ProxyProbe:
    import asyncio

    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.tl import functions

    row = ProxyProbe(id=str(entry.get("id", "")), name=str(entry.get("name", "") or ""))
    api_id, api_hash = _credentials(ctx)
    if not api_id or not api_hash:
        row.error = "no API credentials are registered; run `tlgr account add` first"
        return row

    proxy, connection = _telethon_proxy(entry)
    kwargs: dict[str, Any] = {"proxy": proxy, "timeout": timeout, "connection_retries": 0}
    if connection is not None:
        kwargs["connection"] = connection
    # A memory session, never the account's: a probe must not be able to
    # become the connection Telegram delivers this account's updates to.
    client = TelegramClient(MemorySession(), api_id, api_hash, **kwargs)
    started = time.monotonic()
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        result = await asyncio.wait_for(
            client(functions.help.GetNearestDcRequest()), timeout=timeout
        )
        row.ok = True
        row.ping_ms = round((time.monotonic() - started) * 1000, 2)
        row.dc_id = int(getattr(result, "nearest_dc", 0) or 0) or None
    except Exception as exc:
        row.error = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return row


def _credentials(ctx: OpContext) -> tuple[int | None, str | None]:
    from tlgr.core.accounts import AccountManager
    from tlgr.core.paths import default_base

    manager = AccountManager(default_base())
    alias = ctx.account or manager.get_active() or ""
    if not alias:
        return None, None
    with contextlib.suppress(Exception):
        return manager.load_credentials(alias)
    return None, None


SPEC_PROXY_TEST = OperationSpec(
    id="proxy.test",
    request=ProxyTestReq,
    response=Page[ProxyProbe],
    impl=proxy_test,
    summary="Probe a proxy and measure its latency",
    description=(
        "Uses a throwaway in-memory session: updates go to the last active "
        "connection, so a probe on the real session could divert the "
        "account's events to a client that is about to be discarded."
    ),
    aliases=("net.proxy.test", "proxy.ping"),
    paginated=PageKind.LOCAL,
    mutating=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=180,
    columns=("id", "name", "ok", "ping_ms", "dc_id", "error"),
    empty_exit=EXIT_EMPTY,
    example={"items": [{"id": "p1", "ok": True, "ping_ms": 82.4, "dc_id": 4}], "has_more": False},
    example_args="proxy test --every --reorder",
    covers=("updates.net-proxy-autoswitch", "updates.net-proxy-ping"),
    tags=frozenset({"agent-safe", "mutating-checked"}),
)
