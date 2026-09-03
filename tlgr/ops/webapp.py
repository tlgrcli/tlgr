"""The `webapp` group: mini apps, from a CLI that has no browser.

The return contract is the whole design, and it is deliberately narrow.

`webapp open` prints the **signed URL** and stops. It never launches a
browser, and it never hosts the `window.Telegram.WebApp` bridge — a terminal
cannot run a mini app, and pretending otherwise would mean shipping a headless
browser inside a CLI. What tlgr *can* do is everything on the Telegram side of
the boundary: mint the session, keep it alive, answer the app's peer request,
carry its data back to the bot, and check a download it proposes.

That URL is a credential, not a link: it carries the user's signed init data,
and whoever holds it can act as that user inside the app until it expires. It
is printed once, with that warning in human output, and plainly under `--json`
where the caller asked for machine-readable output on purpose.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, PermissionError_, UsageError
from tlgr.core.timefmt import fmt_dt
from tlgr.models.base import Request
from tlgr.models.bot import BotApiResult
from tlgr.models.peer import PeerRef
from tlgr.models.webapp import (
    WebAppDownload,
    WebAppInfo,
    WebAppProlong,
    WebAppSent,
    WebAppSession,
)
from tlgr.ops import _bots, _send
from tlgr.ops._common import client
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: The platform reported to Telegram. It picks the app's own layout; there is
#: no value meaning "a terminal", and lying about it is what every other
#: third-party client does too.
PLATFORM = "web"

#: How often a session that returned a `query_id` has to be prolonged.
PROLONG_EVERY = 60


def _theme(path: str | None) -> Any:
    """`--theme` as the `DataJSON` Telegram passes into the app.

    The default is a minimal object rather than nothing: an app handed no
    theme at all renders with browser defaults, which looks broken.
    """
    from telethon.tl import types

    if path:
        return _bots.data_json(path, field="theme")
    return types.DataJSON(data='{"bg_color":"#ffffff","text_color":"#000000"}')


def _session(result: Any, *, bot: str | None, kind: str, write_allowed: bool) -> WebAppSession:
    query_id = getattr(result, "query_id", None)
    return WebAppSession(
        bot=bot,
        kind=kind,
        url=str(getattr(result, "url", "") or ""),
        query_id=str(query_id) if query_id else None,
        fullsize=bool(getattr(result, "fullsize", False)),
        fullscreen=bool(getattr(result, "fullscreen", False)),
        same_origin=bool(getattr(result, "same_origin", False)),
        needs_prolong=bool(query_id),
        prolong_every=PROLONG_EVERY if query_id else None,
        write_allowed=write_allowed,
    )


async def _app(ctx: OpContext, bot: PeerRef, short_name: str) -> Any:
    """`InputBotAppShortName` for a direct-link app."""
    from telethon.tl import types

    return types.InputBotAppShortName(
        bot_id=await _bots.input_user(ctx, bot), short_name=short_name
    )


# ---------------------------------------------------------------------------
# webapp get
# ---------------------------------------------------------------------------


class GetReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The bot owning the app.")]
    short_name: Annotated[
        str, arg(1, metavar="SHORT_NAME", required=False, help="Direct-link app short name.")
    ] = ""
    button_request: Annotated[
        str | None,
        opt("--button-request", metavar="ID", help="Show the peer request behind this id."),
    ] = None


async def get(ctx: OpContext, req: GetReq) -> WebAppInfo:
    """A mini app's manifest.

    This covers every entry on the app's panel menu except "Reload page",
    which is a webview concern with no API behind it. The placeholder is an
    SVG-like path blob: its *length* is reported, because nothing that reads
    this output can render it and printing the bytes would bury the rest.
    """
    from telethon.tl.functions import bots as bots_fn
    from telethon.tl.functions import messages as fn

    handle = client(ctx)
    info = WebAppInfo(bot=str(req.bot.raw), short_name=req.short_name or None)

    if req.short_name:
        result = await handle(
            fn.GetBotAppRequest(app=await _app(ctx, req.bot, req.short_name), hash=0)
        )
        app = getattr(result, "app", None)
        if app is None or type(app).__name__ == "BotAppNotModified":
            raise NotFoundError(f"@{req.bot.raw} has no app called {req.short_name!r}")
        info.title = getattr(app, "title", None)
        info.description = getattr(app, "description", None)
        info.photo = int(getattr(getattr(app, "photo", None), "id", 0) or 0) or None
        info.document = int(getattr(getattr(app, "document", None), "id", 0) or 0) or None
        info.inactive = bool(getattr(result, "inactive", False))
        info.request_write_access = bool(getattr(result, "request_write_access", False))
        info.has_settings = bool(getattr(result, "has_settings", False))
        info.link = f"https://t.me/{str(req.bot.value or req.bot.raw).lstrip('@')}/{req.short_name}"

    peer = await _send.resolve(ctx, req.bot)
    from tlgr.ops.bot import _app_settings, _full

    full, _user = await _full(ctx, peer)
    bot_info = getattr(full, "bot_info", None)
    settings = _app_settings(getattr(bot_info, "app_settings", None)) or {}
    info.privacy_policy_url = getattr(bot_info, "privacy_policy_url", None)
    info.placeholder_path = settings.get("placeholder_path")
    info.bg_color = settings.get("bg_color")
    info.bg_dark_color = settings.get("bg_dark_color")
    info.header_color = settings.get("header_color")
    info.header_dark_color = settings.get("header_dark_color")

    attach = await handle(fn.GetAttachMenuBotRequest(bot=await _bots.input_user(ctx, req.bot)))
    entry = getattr(attach, "bot", None)
    info.installed_in_attach_menu = bool(getattr(entry, "show_in_attach_menu", False))
    info.installed_in_side_menu = bool(getattr(entry, "show_in_side_menu", False))

    if req.button_request:
        button = await handle(
            bots_fn.GetRequestedWebViewButtonRequest(
                bot=await _bots.input_user(ctx, req.bot), webapp_req_id=req.button_request
            )
        )
        info.button_request = {
            "text": getattr(button, "text", None),
            "button_id": getattr(button, "button_id", None),
            "peer_type": type(getattr(button, "peer_type", None)).__name__,
        }
    return info


SPEC_GET = OperationSpec(
    id="webapp.get",
    request=GetReq,
    response=WebAppInfo,
    impl=get,
    summary="Show a mini app's manifest",
    aliases=("app.info", "app.get"),
    columns=("short_name", "title", "installed_in_attach_menu"),
    headers=("App", "Title", "Installed"),
    example={"bot": "@my_helper_bot", "short_name": "shop", "title": "Shop"},
    example_args="webapp get @my_helper_bot shop",
    covers=(
        "bots.button-request-peer-from-miniapp",
        "bots.direct-link-app-open",
        "bots.webapp-placeholder-and-close",
    ),
    covers_partial=("bots.miniapp-panel-menu",),
    coverage_note=(
        "Installing and removing the app is `bot attach toggle`; reporting it "
        "is `bot report --app`."
    ),
)


# ---------------------------------------------------------------------------
# webapp open
# ---------------------------------------------------------------------------


class OpenReq(Request):
    bot: Annotated[
        PeerRef | None, arg(0, metavar="BOT", required=False, kind="user", help="The bot.")
    ] = None
    app: Annotated[str | None, opt("--app", metavar="NAME", help="Direct-link app short name.")] = (
        None
    )
    main: Annotated[bool, opt("--main", help="The bot's Main Mini App.")] = False
    attach: Annotated[bool, opt("--attach", help="Attachment-menu app in --chat.")] = False
    menu: Annotated[bool, opt("--menu", help="The bot's menu-button app.")] = False
    simple: Annotated[bool, opt("--simple", help="Simple web view.")] = False
    side_menu: Annotated[bool, opt("--side-menu", help="Side-menu app (implies --simple).")] = False
    from_switch_webview: Annotated[
        bool, opt("--from-switch-webview", help="Inline-mode app behind a switch_webview button.")
    ] = False
    join_query_id: Annotated[
        str | None, opt("--join-query-id", metavar="ID", help="Guard-bot chat-join app.")
    ] = None
    url: Annotated[str | None, opt("--url", metavar="URL", help="Button URL for the app.")] = None
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Chat the app is opened from."),
    ] = None
    start_param: Annotated[
        str | None, opt("--start-param", metavar="TEXT", help="startapp payload.")
    ] = None
    mode: Annotated[
        str | None, choice("compact", "fullscreen", help="Requested presentation mode.")
    ] = None
    allow_write: Annotated[bool, opt("--allow-write", help="CONSENT: let the bot message me.")] = (
        False
    )
    theme: Annotated[
        str | None, opt("--theme", metavar="PATH", kind="path", help="JSON theme params.")
    ] = None
    open_inactive: Annotated[
        bool, opt("--open-inactive", help="Open an app Telegram has marked inactive.")
    ] = False


async def open_app(ctx: OpContext, req: OpenReq) -> WebAppSession:
    """Open a mini app and print its signed URL.

    Seven entry points reach one answer, and they are genuinely different
    requests — a Main Mini App, a direct link, an attachment-menu entry, a
    menu button, a simple view, a side-menu view, an inline switch. What comes
    back is the same shape, plus one fact that matters operationally: whether
    the session has a `query_id` and therefore dies in a minute unless
    `webapp watch` keeps it alive.

    tlgr never opens a browser. `--allow-write` is never implied: opening an
    app and letting its bot message you afterwards are two decisions.
    """
    from telethon.tl.functions import messages as fn

    if req.join_query_id:
        _bots.unsupported(
            "--join-query-id",
            "messages.requestChatJoinWebView is absent from Telethon 1.44 and "
            "hand-rolling it would mean guessing at an unpublished constructor id",
        )
    if req.bot is None:
        raise UsageError("name the bot that owns the app", field="bot")

    handle = client(ctx)
    bot = await _bots.input_user(ctx, req.bot)
    peer = (
        await _send.resolve(ctx, req.chat)
        if req.chat is not None
        else await _send.resolve(ctx, req.bot)
    )
    compact = req.mode == "compact" or None
    fullscreen = req.mode == "fullscreen" or None
    theme = _theme(req.theme)

    if req.app:
        app = await _app(ctx, req.bot, req.app)
        listing = await handle(fn.GetBotAppRequest(app=app, hash=0))
        if bool(getattr(listing, "inactive", False)) and not req.open_inactive:
            raise PermissionError_(
                "Telegram marks this app inactive; pass --open-inactive to open it anyway"
            )
        result = await handle(
            fn.RequestAppWebViewRequest(
                peer=peer,
                app=app,
                platform=PLATFORM,
                write_allowed=req.allow_write or None,
                compact=compact,
                fullscreen=fullscreen,
                start_param=req.start_param,
                theme_params=theme,
            )
        )
        kind = "direct-link"
    elif req.main:
        result = await handle(
            fn.RequestMainWebViewRequest(
                peer=peer,
                bot=bot,
                platform=PLATFORM,
                compact=compact,
                fullscreen=fullscreen,
                start_param=req.start_param,
                theme_params=theme,
            )
        )
        kind = "main"
    elif req.simple or req.side_menu or req.from_switch_webview:
        result = await handle(
            fn.RequestSimpleWebViewRequest(
                bot=bot,
                platform=PLATFORM,
                from_switch_webview=req.from_switch_webview or None,
                from_side_menu=req.side_menu or None,
                compact=compact,
                fullscreen=fullscreen,
                url=req.url,
                start_param=req.start_param,
                theme_params=theme,
            )
        )
        kind = "side-menu" if req.side_menu else "simple"
    else:
        url = req.url
        if req.menu and not url:
            from tlgr.ops.bot import _full

            full, _user = await _full(ctx, await _send.resolve(ctx, req.bot))
            button = getattr(getattr(full, "bot_info", None), "menu_button", None)
            url = getattr(button, "url", None)
            if not url:
                raise NotFoundError("that bot has no menu-button app")
        result = await handle(
            fn.RequestWebViewRequest(
                peer=peer,
                bot=bot,
                platform=PLATFORM,
                from_bot_menu=req.menu or None,
                compact=compact,
                fullscreen=fullscreen,
                url=url,
                start_param=req.start_param,
                theme_params=theme,
            )
        )
        kind = "menu" if req.menu else "attach" if req.attach else "button"

    if req.allow_write and kind not in ("direct-link",):
        # Only requestAppWebView carries write_allowed; everywhere else the
        # grant is its own call, and doing it silently would be the implicit
        # consent this command refuses to give.
        from telethon.tl.functions import bots as bots_fn

        await handle(bots_fn.AllowSendMessageRequest(bot=bot))

    session = _session(result, bot=str(req.bot.raw), kind=kind, write_allowed=req.allow_write)
    if session.query_id:
        ctx.warn(
            "this URL carries your signed init data — treat it as a credential, "
            f"and keep the session alive with `tlgr webapp watch {req.bot.raw} "
            f"--query-id {session.query_id}`"
        )
    else:
        ctx.warn("this URL carries your signed init data — treat it as a credential")
    return session


SPEC_OPEN = OperationSpec(
    id="webapp.open",
    request=OpenReq,
    response=WebAppSession,
    impl=open_app,
    summary="Open a mini app and print its signed URL",
    description=(
        "Printing the URL is the only behaviour: there is no --open, because "
        "a CLI cannot host the mini-app JS bridge and a browser launched from "
        "here would carry a credential into a process tlgr does not control."
    ),
    aliases=("app.open",),
    mutating=True,
    columns=("kind", "url", "needs_prolong"),
    headers=("Kind", "URL", "Prolong"),
    example={
        "bot": "@my_helper_bot",
        "kind": "main",
        "url": "https://example.org/app#tgWebAppData=…",
    },
    example_args="webapp open @my_helper_bot --main",
    covers=(
        "attach.open-mini-app",
        "bots.attach-menu-deeplinks",
        "bots.attach-webapp-open",
        "bots.main-webapp-open",
        "bots.simple-webapp-open",
        "bots.webapp-modes",
    ),
    covers_partial=(
        "bots.direct-link-app-open",
        "bots.inline-switch-webview",
        "bots.webapp-write-access",
    ),
    coverage_note=(
        "The app's manifest is `webapp get`; the attachment-menu install is "
        "`bot attach toggle`. The guard-bot chat-join view needs layer 229 "
        "and exits 13."
    ),
)


# ---------------------------------------------------------------------------
# webapp watch
# ---------------------------------------------------------------------------


class WatchReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The mini app's bot.")]
    query_id: Annotated[
        str, opt("--query-id", metavar="ID", help="query_id from `webapp open`.")
    ] = ""
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Chat the app was opened from."),
    ] = None
    interval: Annotated[
        str, opt("--interval", metavar="DURATION", help="Prolong interval, e.g. 55s.")
    ] = "55s"
    until: Annotated[
        str | None, opt("--until", metavar="DURATION", help="Stop after this long.")
    ] = None


async def watch(ctx: OpContext, req: WatchReq) -> Any:
    """Keep an open mini-app session alive.

    Only a session that came back with a `query_id` needs this, and
    `QUERY_ID_INVALID` is how it ends normally — the session died, which is
    information, not a failure. The stream therefore closes with
    `alive: false` and exit 0 rather than raising.
    """
    import asyncio
    import time

    from telethon.tl.functions import messages as fn

    from tlgr.core.timefmt import parse_duration

    if not req.query_id:
        raise UsageError("--query-id is required", field="query_id")
    try:
        query_id = int(req.query_id)
    except ValueError as exc:
        raise UsageError("--query-id must be numeric", field="query_id") from exc

    interval = float(parse_duration(req.interval) or 55)
    deadline = time.monotonic() + float(parse_duration(req.until) or 0) if req.until else None
    handle = client(ctx)
    bot = await _bots.input_user(ctx, req.bot)
    peer = (
        await _send.resolve(ctx, req.chat)
        if req.chat is not None
        else await _send.resolve(ctx, req.bot)
    )

    while True:
        try:
            await handle(fn.ProlongWebViewRequest(peer=peer, bot=bot, query_id=query_id))
        except Exception as exc:
            reason = f"{type(exc).__name__} {exc}".upper().replace("_", "")
            if "QUERYIDINVALID" not in reason:
                raise
            yield WebAppProlong(
                query_id=req.query_id, alive=False, reason="the session has expired"
            )
            return
        yield WebAppProlong(query_id=req.query_id, prolonged_at=fmt_dt(_now()), alive=True)
        if deadline is not None and time.monotonic() >= deadline:
            return
        await asyncio.sleep(interval)


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


SPEC_WATCH = OperationSpec(
    id="webapp.watch",
    request=WatchReq,
    response=WebAppProlong,
    impl=watch,
    summary="Keep an open mini-app session alive",
    aliases=("app.session.prolong",),
    mutating=True,
    stream=True,
    timeout_s=900,
    columns=("query_id", "prolonged_at", "alive"),
    headers=("Query", "At", "Alive"),
    example={"query_id": "987654321", "alive": True},
    example_args="webapp watch @my_helper_bot --query-id 987654321",
    covers=("bots.prolong-webview",),
)


# ---------------------------------------------------------------------------
# webapp send
# ---------------------------------------------------------------------------


class SendReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The mini app's bot.")]
    button_text: Annotated[
        str, opt("--button-text", metavar="TEXT", help="Text of the button that opened the app.")
    ] = ""
    data: Annotated[str, opt("--data", metavar="PAYLOAD", help="Payload, max 4096 bytes.")] = ""


async def send(ctx: OpContext, req: SendReq) -> WebAppSent:
    """Send data from a keyboard-button mini app back to its bot.

    Valid exactly once per web-app session: a second `web_app_data_send` from
    the same session is ignored by the server, so a caller that retries is
    not doing anything.
    """
    from telethon.tl.functions import messages as fn

    if not req.button_text or not req.data:
        raise UsageError("--button-text and --data are both required", field="data")
    if len(req.data.encode()) > 4096:
        raise UsageError("--data is capped at 4096 bytes", field="data")

    await client(ctx)(
        fn.SendWebViewDataRequest(
            bot=await _bots.input_user(ctx, req.bot),
            button_text=req.button_text,
            data=req.data,
            random_id=_random_id(),
        )
    )
    peer = await _send.resolve(ctx, req.bot)
    return WebAppSent(bot_id=_send.peer_id_of(peer), sent=True)


def _random_id() -> int:
    from tlgr.ops._common import random_id

    return random_id()


SPEC_SEND = OperationSpec(
    id="webapp.send",
    request=SendReq,
    response=WebAppSent,
    impl=send,
    summary="Send data from a keyboard-button mini app back to its bot",
    aliases=("app.send-data", "webapp.send-data"),
    mutating=True,
    rate_class="send",
    columns=("bot_id", "sent"),
    headers=("Bot", "Sent"),
    example={"bot_id": 5000001, "sent": True},
    example_args='webapp send @my_helper_bot --button-text Order --data "{}"',
    covers=("bots.send-webview-data",),
)


# ---------------------------------------------------------------------------
# webapp invoke
# ---------------------------------------------------------------------------


class InvokeReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The mini app's bot.")]
    method: Annotated[str, arg(1, metavar="METHOD", help="Custom method name.")]
    params: Annotated[
        str, opt("--params", metavar="JSON", kind="json", help="JSON parameters.")
    ] = "{}"


async def invoke(ctx: OpContext, req: InvokeReq) -> BotApiResult:
    """Call a mini app's custom method. The result is opaque and passed through."""
    from telethon.tl.functions import bots as fn

    result = await client(ctx)(
        fn.InvokeWebViewCustomMethodRequest(
            bot=await _bots.input_user(ctx, req.bot),
            custom_method=req.method,
            params=_bots.data_json(req.params, field="params"),
        )
    )
    from tlgr.ops.bot import _data_json

    return BotApiResult(method=req.method, result=_data_json(result))


SPEC_INVOKE = OperationSpec(
    id="webapp.invoke",
    request=InvokeReq,
    response=BotApiResult,
    impl=invoke,
    summary="Call a mini app's custom method",
    aliases=("app.invoke",),
    mutating=True,
    columns=("method",),
    headers=("Method",),
    example={"method": "getOrders", "result": {"orders": []}},
    example_args='webapp invoke @my_helper_bot getOrders --params "{}"',
    covers=("bots.webapp-custom-method",),
)


# ---------------------------------------------------------------------------
# webapp download
# ---------------------------------------------------------------------------


class DownloadReq(Request):
    bot: Annotated[PeerRef, arg(0, metavar="BOT", kind="user", help="The mini app's bot.")]
    file_name: Annotated[
        str, opt("--file-name", metavar="NAME", help="File name the app proposed.")
    ] = ""
    url: Annotated[str, opt("--url", metavar="URL", help="URL the app proposed.")] = ""
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Where to write it.")
    ] = None
    fetch: Annotated[
        bool, opt("--fetch", help="Actually download it; checking alone never does.")
    ] = False


async def download(ctx: OpContext, req: DownloadReq) -> WebAppDownload:
    """Check — and only on request, perform — a download a mini app asked for.

    Check-only by default. `bots.checkDownloadFileParams` is Telegram saying
    whether the app is allowed to offer this file at all, and a client that
    fetched first and asked afterwards would have already run the risk. The
    fetch itself is plain HTTPS, not MTProto, which is the other reason it is
    opt-in: nothing about it goes through Telegram.
    """
    from telethon.tl.functions import bots as fn

    if not req.file_name or not req.url:
        raise UsageError("--file-name and --url are both required", field="url")

    allowed = bool(
        await client(ctx)(
            fn.CheckDownloadFileParamsRequest(
                bot=await _bots.input_user(ctx, req.bot),
                file_name=req.file_name,
                url=req.url,
            )
        )
    )
    result = WebAppDownload(allowed=allowed, file_name=req.file_name, url=req.url)
    if not req.fetch:
        return result
    if not allowed:
        raise PermissionError_(
            "Telegram does not allow this mini app to offer that file; nothing was downloaded"
        )
    if not req.url.startswith("https://"):
        raise PermissionError_("only https:// downloads are performed")
    target = Path(os.path.expanduser(req.out or req.file_name))
    ctx.warn(f"fetching {req.file_name} from {req.url} over plain HTTPS, outside Telegram")
    result.path = str(target)
    result.downloaded = await _fetch(req.url, target)
    return result


#: A mini app names its own file size nowhere, so the fetch is capped here.
MAX_DOWNLOAD = 64 * 1024 * 1024


async def _fetch(url: str, target: Path) -> bool:
    """Fetch *url* into *target*, capped. Plain HTTPS: no Telegram involved."""
    import aiohttp

    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            async for chunk in response.content.iter_chunked(64 * 1024):
                written += len(chunk)
                if written > MAX_DOWNLOAD:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise PermissionError_(
                        f"the file exceeds tlgr's {MAX_DOWNLOAD // (1024 * 1024)} MB cap "
                        "for a mini-app download"
                    )
                handle.write(chunk)
    return True


SPEC_DOWNLOAD = OperationSpec(
    id="webapp.download",
    request=DownloadReq,
    response=WebAppDownload,
    impl=download,
    summary="Check a file download a mini app asked for",
    aliases=("app.check-download",),
    columns=("allowed", "file_name", "downloaded"),
    headers=("Allowed", "File", "Downloaded"),
    example={"allowed": True, "file_name": "invoice.pdf", "url": "https://example.org/i.pdf"},
    example_args="webapp download @my_helper_bot --file-name i.pdf --url https://example.org/i.pdf",
    covers=("attach.file-download-check", "bots.webapp-file-download-check"),
)
