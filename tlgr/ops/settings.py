"""The `settings` group: the cloud-synced switches, languages and themes.

`settings get` and `settings set` are one generic pair over a dozen unrelated
RPCs. That is a decision, not a shortcut: a dozen thin toggle commands would
be a dozen names to learn, a dozen response shapes and a dozen places for
the same "read-modify-write" mistake. Here every key prints the exact token
vocabulary its setter accepts, so `settings get X` and `settings set X <value>`
are a genuine round trip.

Where the group that owns a setting already implements it — sensitive media,
auto-download presets, the quick reaction, paid-reaction privacy, saved tags,
top peers, folder tags — this dispatches to that operation instead of issuing
the RPC a second time. One server call, one implementation, two entry points.

`settings theme *` is metadata only. tlgr has no theming engine and renders
nothing; what it can do is publish a theme file, install one for the account
and list what is installed, which is the server-side half the GUI shares.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind
from tlgr.core.timefmt import parse_duration
from tlgr.models.base import Request
from tlgr.models.media import AutoSaveSaved
from tlgr.models.page import Page
from tlgr.models.settings import (
    CloudTheme,
    Language,
    SettingChange,
    SettingUnset,
    SettingValue,
    ThemeInstalled,
)
from tlgr.ops import _settings
from tlgr.ops._common import client
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: key → the token vocabulary its setter accepts. Printed with every read so
#: the output of `settings get` can be piped back into `settings set`.
ACCEPTS: dict[str, str] = {
    "sensitive-content": "on|off",
    "auto-delete": "1d|1w|1m|<duration>|off",
    "top-peers": "on|off",
    "quick-reaction": "<emoji>|custom:<doc-id>",
    "folder-tags": "on|off",
    "paid-reaction-privacy": "default|anonymous|peer:<channel>",
    "sponsored-ads": "on|off",
    "browser": "external|in-app",
    "browser-close-button": "on|off",
    "browser-exception": "<url> external|in-app",
    "no-forwards": "<peer> on|off (use --peer)",
    "saved-tag": "<emoji> <title>",
    "language": "<lang-code>",
    "auto-download": "auto-download.<low|medium|high>.<field> <value>",
    "age-verification": "(read-only)",
}

#: The `auto-download.<preset>.<field>` names, in three spellings: the tlgr
#: key, the field on `AutoDownloadPreset` a read comes back on, and the flag
#: `media auto-download set` takes. Keeping the three in one table is what
#: stops the dotted key and the delegate drifting apart.
PRESETS = ("low", "medium", "high")
DOWNLOAD_FIELDS = {
    "photo-max": "photo_size_max",
    "video-max": "video_size_max",
    "file-size-max": "file_size_max",
    "video-preload-large": "video_preload_large",
    "audio-preload-next": "audio_preload_next",
    "stories-preload": "stories_preload",
    "disabled": "disabled",
}
DOWNLOAD_FLAGS = {
    "photo-max": "photo_max",
    "video-max": "video_max",
    "file-size-max": "file_max",
    "video-preload-large": "preload_large_video",
    "audio-preload-next": "preload_next_audio",
    "stories-preload": "preload_stories",
    "disabled": "disabled",
}


def _key_of(raw: str) -> tuple[str, str]:
    """`auto-download.low.photo-max` → `("auto-download", "low.photo-max")`."""
    head, _, tail = raw.strip().lower().partition(".")
    if head not in ACCEPTS:
        raise UsageError(
            f"unknown setting {raw!r}; one of: {' '.join(sorted(ACCEPTS))}", field="key"
        )
    return head, tail


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


async def _read(ctx: OpContext, key: str, tail: str, peer: str | None) -> SettingValue:
    """One key's current value, with where it came from and whether it may be set."""
    from telethon.tl import types
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import messages as mfn
    from telethon.tl.functions import users as ufn

    handle = client(ctx)
    value: Any = None
    source = "server"
    changeable = True
    reason: str | None = None

    if key == "sensitive-content":
        from tlgr.ops.media import SensitiveGetReq, sensitive_get

        content = await sensitive_get(ctx, SensitiveGetReq())
        value = "on" if content.sensitive_enabled else "off"
        changeable = content.sensitive_can_change
        reason = content.reason
    elif key == "auto-delete":
        period = int(getattr(await handle(mfn.GetDefaultHistoryTTLRequest()), "period", 0) or 0)
        value = f"{period}s" if period else "off"
    elif key == "top-peers":
        # There is no getter for the switch itself: `contacts.getTopPeers`
        # answers `topPeersDisabled` when collection is off, which is the
        # only signal the server gives.
        answer = await handle(_top_peers_request())
        value = "off" if type(answer).__name__ == "ContactsTopPeersDisabled" else "on"
    elif key == "quick-reaction":
        from tlgr.ops.reaction import DefaultGetReq, default_get

        quick = await default_get(ctx, DefaultGetReq())
        value = quick.reaction or None
    elif key == "folder-tags":
        from tlgr.ops.folder import raw_filters

        _, enabled = await raw_filters(ctx)
        value = "on" if enabled else "off"
    elif key == "paid-reaction-privacy":
        raw = await handle(mfn.GetPaidReactionPrivacyRequest())
        value = _paid_privacy_word(raw)
    elif key == "sponsored-ads":
        answer = await handle(ufn.GetFullUserRequest(id=types.InputUserSelf()))
        value = (
            "on"
            if getattr(getattr(answer, "full_user", None), "sponsored_enabled", False)
            else "off"
        )
    elif key in ("browser", "browser-close-button", "browser-exception"):
        settings = await handle(afn.GetWebBrowserSettingsRequest(hash=0))
        if key == "browser":
            value = "external" if getattr(settings, "open_external_browser", False) else "in-app"
        elif key == "browser-close-button":
            value = "on" if getattr(settings, "display_close_button", False) else "off"
        else:
            # The server keeps two vectors, not one list with a flag on each
            # row, so the mode is the vector a domain is *in*.
            value = [
                {
                    "domain": getattr(entry, "domain", ""),
                    "url": getattr(entry, "url", ""),
                    "title": getattr(entry, "title", ""),
                    "mode": mode,
                }
                for mode, vector in (
                    ("external", getattr(settings, "external_exceptions", None) or []),
                    ("in-app", getattr(settings, "inapp_exceptions", None) or []),
                )
                for entry in vector
            ]
    elif key == "no-forwards":
        answer = await handle(ufn.GetFullUserRequest(id=types.InputUserSelf()))
        full = getattr(answer, "full_user", None)
        field = "noforwards_peer_enabled" if peer else "noforwards_my_enabled"
        value = "on" if getattr(full, field, False) else "off"
    elif key == "saved-tag":
        from tlgr.ops.reaction import TagListReq, tag_list

        tags = await tag_list(ctx, TagListReq())
        value = [
            {"reaction": tag.reaction, "title": tag.title, "count": tag.count} for tag in tags.items
        ]
    elif key == "language":
        from tlgr.ops.config import ConfigGetReq, config_get

        stored = await config_get(ctx, ConfigGetReq(key="identity.lang_code"))
        value = stored.value or None
        source = "local"
    elif key == "auto-download":
        from tlgr.ops.media import AutoDownloadGetReq, auto_download_get

        presets = await auto_download_get(ctx, AutoDownloadGetReq())
        value = [
            {"preset": preset.preset, **{k: getattr(preset, v) for k, v in DOWNLOAD_FIELDS.items()}}
            for preset in presets.presets
            if not tail or preset.preset == tail.partition(".")[0]
        ]
    elif key == "age-verification":
        config = await _settings.app_config(ctx)
        value = {
            "need_age_video_verification": bool(config.get("need_age_video_verification")),
            "verify_age_min": config.get("verify_age_min"),
            "verify_age_bot_username": config.get("verify_age_bot_username"),
        }
        changeable = False
        reason = (
            "age verification runs in a Telegram-designated bot's Main Mini App "
            "with a camera; a terminal cannot complete it"
        )
        source = "app-config"

    return SettingValue(
        key=f"{key}.{tail}" if tail else key,
        value=value,
        changeable=changeable,
        source=source,
        accepts=ACCEPTS[key],
        reason=reason,
    )


def _top_peers_request() -> Any:
    from telethon.tl.functions import contacts as fn

    return fn.GetTopPeersRequest(correspondents=True, offset=0, limit=1, hash=0)


def _paid_privacy_word(raw: Any) -> str:
    name = type(raw).__name__
    if name == "PaidReactionPrivacyAnonymous":
        return "anonymous"
    if name == "PaidReactionPrivacyPeer":
        return "peer"
    return "default"


class GetReq(Request):
    key: Annotated[
        str | None, arg(0, metavar="KEY", required=False, help="One key; omit for every key.")
    ] = None
    peer: Annotated[
        str | None, opt("--peer", metavar="CHAT", help="Target peer for per-peer keys.")
    ] = None


async def get(ctx: OpContext, req: GetReq) -> Page[SettingValue]:
    """Read cloud-synced account settings — one key, or all of them.

    Every row carries `accepts`, the exact token vocabulary its setter takes,
    so a caller never has to guess whether a switch wants `on` or `true`.
    """
    wanted = [_key_of(req.key)] if req.key else [(name, "") for name in sorted(ACCEPTS)]
    rows: list[SettingValue] = []
    for name, tail in wanted:
        try:
            rows.append(await _read(ctx, name, tail, req.peer))
        except Exception as exc:
            if req.key:
                raise
            ctx.warn(f"could not read {name}: {exc}")
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_GET = OperationSpec(
    id="settings.get",
    request=GetReq,
    response=Page[SettingValue],
    impl=get,
    summary="Read cloud-synced account settings (one key or all of them)",
    description=(
        "One generic pair instead of a dozen thin toggles. `accepts` on each "
        "row is the vocabulary `settings set` takes for that key, so the read "
        "and the write are the same words."
    ),
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("key", "value", "changeable", "accepts"),
    headers=("Key", "Value", "Changeable", "Accepts"),
    example={
        "items": [
            {"key": "auto-delete", "value": "off", "changeable": True, "accepts": "1d|1w|1m|off"}
        ],
        "has_more": False,
    },
    example_args="settings get auto-delete",
    covers=(
        "appearance.default-reaction",
        "data.auto-download",
        "privacy.paid-reaction-anonymity",
        "privacy.pm-content-protection",
        "privacy.sensitive-content",
    ),
    covers_partial=(
        "appearance.folder-tags",
        "appearance.saved-tags",
        "business.reenable-ads",
        "data.web-browser-settings",
        "lang.set",
        "privacy.age-verification",
        "privacy.default-ttl",
        "privacy.top-peers-suggest",
    ),
    coverage_note="Writing any of these keys is `settings set`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# settings set
# ---------------------------------------------------------------------------


class SetReq(Request):
    key: Annotated[str, arg(0, metavar="KEY", help="The setting to change.")]
    value: Annotated[
        tuple[str, ...], arg(1, metavar="VALUE", variadic=True, help="Value(s) for the key.")
    ] = ()
    apply_to_existing: Annotated[
        bool, opt("--apply-to-existing", help="auto-delete: rewrite every chat's TTL too.")
    ] = False
    peer: Annotated[
        str | None, opt("--peer", metavar="CHAT", help="Target peer for per-peer keys.")
    ] = None


async def set_(ctx: OpContext, req: SetReq) -> SettingChange:
    """Change one cloud-synced account setting.

    Settings whose server call replaces a whole constructor — the
    auto-download presets, the web-browser settings — are read-modify-written
    here, and the dotted key names the one field being changed.
    """
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import messages as mfn

    handle = client(ctx)
    key, tail = _key_of(req.key)
    values = [value for value in req.value if value != ""]
    before = await _read(ctx, key, tail, req.peer)
    if not before.changeable:
        from tlgr.core.errors import PermissionError_

        raise PermissionError_(before.reason or f"{key} cannot be changed from an API client")
    if not values:
        raise UsageError(f"{key} wants a value: {ACCEPTS[key]}", field="value")
    word = values[0].strip()
    result = SettingChange(key=before.key, previous=before.value)

    if key == "sensitive-content":
        from tlgr.ops.media import SensitiveSetReq, sensitive_set

        content = await sensitive_set(ctx, SensitiveSetReq(state=word))
        result.value = "on" if content.sensitive_enabled else "off"
        result.already = content.already
    elif key == "auto-delete":
        period = 0 if word.lower() in ("off", "none", "0") else int(parse_duration(word) or 0)
        if word.lower() not in ("off", "none", "0") and not period:
            raise UsageError("auto-delete takes a duration (1d, 1w, 1m) or 'off'", field="value")
        await handle(mfn.SetDefaultHistoryTTLRequest(period=period))
        result.value = f"{period}s" if period else "off"
        if req.apply_to_existing:
            result.applied_to = await _apply_ttl_everywhere(ctx, period)
    elif key == "top-peers":
        from tlgr.ops.contact import TopSetReq, top_set

        state = _settings.on_off(word, field="value")
        await top_set(ctx, TopSetReq(state="on" if state else "off"))
        result.value = "on" if state else "off"
    elif key == "quick-reaction":
        from tlgr.ops.reaction import DefaultSetReq, default_set

        await default_set(ctx, DefaultSetReq(emoji=word))
        result.value = word
    elif key == "folder-tags":
        state = _settings.on_off(word, field="value")
        await handle(mfn.ToggleDialogFilterTagsRequest(enabled=bool(state)))
        result.value = "on" if state else "off"
    elif key == "paid-reaction-privacy":
        from tlgr.ops.reaction import PrivacySetReq, privacy_set

        await privacy_set(ctx, PrivacySetReq(mode=word))
        result.value = word
    elif key == "sponsored-ads":
        state = _settings.on_off(word, field="value")
        await handle(afn.ToggleSponsoredMessagesRequest(enabled=bool(state)))
        result.value = "on" if state else "off"
    elif key in ("browser", "browser-close-button"):
        settings = await handle(afn.GetWebBrowserSettingsRequest(hash=0))
        external = bool(getattr(settings, "open_external_browser", False))
        close = bool(getattr(settings, "display_close_button", False))
        if key == "browser":
            if word not in ("external", "in-app"):
                raise UsageError("browser takes external or in-app", field="value")
            external = word == "external"
            result.value = word
        else:
            close = bool(_settings.on_off(word, field="value"))
            result.value = "on" if close else "off"
        await handle(
            afn.UpdateWebBrowserSettingsRequest(
                open_external_browser=external or None, display_close_button=close or None
            )
        )
    elif key == "browser-exception":
        if len(values) != 2 or values[1] not in ("external", "in-app"):
            raise UsageError("browser-exception takes '<url> external|in-app'", field="value")
        await handle(
            afn.ToggleWebBrowserSettingsExceptionRequest(
                url=values[0], open_external_browser=values[1] == "external" or None
            )
        )
        result.value = {"url": values[0], "mode": values[1]}
    elif key == "no-forwards":
        if not req.peer:
            raise UsageError("no-forwards needs --peer <chat>", field="peer")
        state = _settings.on_off(word, field="value")
        await handle(
            mfn.ToggleNoForwardsRequest(
                peer=await _settings.resolve(ctx, req.peer), enabled=bool(state)
            )
        )
        result.value = "on" if state else "off"
    elif key == "saved-tag":
        from tlgr.ops.reaction import TagSetReq, tag_set

        if len(values) < 2:
            raise UsageError("saved-tag takes '<emoji> <title>'", field="value")
        await tag_set(ctx, TagSetReq(emoji=values[0], title=" ".join(values[1:])))
        result.value = {"emoji": values[0], "title": " ".join(values[1:])}
    elif key == "language":
        from tlgr.ops.config import ConfigSetReq, config_set

        stored = await config_set(ctx, ConfigSetReq(key="identity.lang_code", value=word))
        result.value = stored.value
        result.already = stored.already
    elif key == "auto-download":
        result.value = await _set_auto_download(ctx, tail, word)
    else:  # pragma: no cover - `age-verification` is read-only and refused above
        raise UsageError(f"{key} cannot be written", field="key")

    if result.value == result.previous:
        result.already = True
        ctx.mark_already()
    ctx.emit("settings_set", {"key": result.key})
    return result


async def _apply_ttl_everywhere(ctx: OpContext, period: int) -> int:
    """Push the default TTL onto every existing chat, one `setHistoryTTL` each.

    Gated behind `--apply-to-existing` and `--yes` because it rewrites a
    setting on every dialog, and the server has no bulk form.
    """
    from telethon.tl.functions import messages as fn

    from tlgr.ops.chat import ListReq, list_chats

    handle = client(ctx)
    page = await list_chats(ctx, ListReq())
    touched = 0
    for dialog in page.items:
        chat = getattr(dialog, "chat", None)
        if chat is None:
            continue
        peer = await _settings.resolve(ctx, str(chat.id))
        try:
            await handle(fn.SetHistoryTTLRequest(peer=peer, period=period))
        except Exception as exc:
            ctx.warn(f"could not set the TTL on {chat.id}: {exc}")
            continue
        touched += 1
    return touched


async def _set_auto_download(ctx: OpContext, tail: str, word: str) -> Any:
    """`auto-download.<preset>.<field> <value>`, read-modify-written."""
    from tlgr.ops.media import AutoDownloadSetReq, auto_download_set

    preset, _, field = tail.partition(".")
    if preset not in PRESETS or field not in DOWNLOAD_FLAGS:
        raise UsageError(
            f"auto-download.<{'|'.join(PRESETS)}>.<{'|'.join(sorted(DOWNLOAD_FIELDS))}>",
            field="key",
        )
    flag = DOWNLOAD_FLAGS[field]
    kwargs: dict[str, Any] = {"preset": preset}
    kwargs[flag] = (
        word
        if field in ("photo-max", "video-max", "file-size-max")
        else bool(_settings.on_off(word, field="value"))
    )
    saved = await auto_download_set(ctx, AutoDownloadSetReq(**kwargs))
    return {
        field: getattr(saved.settings, DOWNLOAD_FIELDS[field], None) if saved.settings else None
    }


SPEC_SET = OperationSpec(
    id="settings.set",
    request=SetReq,
    response=SettingChange,
    impl=set_,
    summary="Change a cloud-synced account setting",
    description=(
        "`previous` is always reported, so a script can tell 'I changed it' "
        "from 'it was already like that'. Premium-only keys pass the server's "
        "`PREMIUM_ACCOUNT_REQUIRED` through rather than pretending to succeed."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("key", "value", "previous", "already"),
    headers=("Key", "Value", "Previous", "Already"),
    example={"key": "auto-delete", "value": "604800s", "previous": "off"},
    example_args="settings set auto-delete 1w",
    covers=(
        "appearance.folder-tags",
        "appearance.saved-tags",
        "business.reenable-ads",
        "gift.button-visibility",
        "privacy.age-verification",
        "privacy.default-ttl",
    ),
    covers_partial=(
        "appearance.default-reaction",
        "data.auto-download",
        "data.web-browser-settings",
        "lang.set",
        "privacy.paid-reaction-anonymity",
        "privacy.pm-content-protection",
        "privacy.sensitive-content",
        "privacy.top-peers-suggest",
    ),
    coverage_note="Reading any of these keys back is `settings get`.",
)


# ---------------------------------------------------------------------------
# settings unset
# ---------------------------------------------------------------------------


class UnsetReq(Request):
    key: Annotated[
        str, arg(0, metavar="KEY", help="top-peers | browser-exception | autosave | saved-tag.")
    ]
    value: Annotated[
        str | None,
        arg(1, metavar="VALUE", required=False, help="Peer, URL or emoji to forget."),
    ] = None
    category: Annotated[
        str | None, opt("--category", metavar="NAME", help="top-peers: which rating to reset.")
    ] = None
    every: Annotated[bool, opt("--every", help="Clear every exception for that key.")] = False


async def unset(ctx: OpContext, req: UnsetReq) -> SettingUnset:
    """Remove a per-peer or per-URL exception, or a single suggestion.

    The opposite of `settings set` only for the keys that *have* exceptions;
    everything else has a value and is changed rather than removed, which is
    why this is a separate verb and not `settings set X none`.
    """
    from telethon.tl.functions import account as afn

    handle = client(ctx)
    key = req.key.strip().lower()

    if key == "top-peers":
        from tlgr.ops.contact import TopSetReq, top_set

        if not req.value:
            raise UsageError("top-peers wants the peer whose rating to reset", field="value")
        from tlgr.models.peer import parse_peer_ref

        await top_set(
            ctx,
            TopSetReq(reset=parse_peer_ref(req.value), category=req.category or "correspondents"),
        )
        return SettingUnset(key=key, removed=1, values=[req.value])

    if key == "browser-exception":
        if req.every:
            await handle(afn.DeleteWebBrowserSettingsExceptionsRequest())
            return SettingUnset(key=key, removed=-1)
        if not req.value:
            raise UsageError("browser-exception wants a URL, or --every", field="value")
        await handle(afn.ToggleWebBrowserSettingsExceptionRequest(url=req.value, delete=True))
        return SettingUnset(key=key, removed=1, values=[req.value])

    if key == "autosave":
        await handle(afn.DeleteAutoSaveExceptionsRequest())
        return SettingUnset(key=key, removed=-1)

    if key == "saved-tag":
        from tlgr.ops.reaction import TagSetReq, tag_set

        if not req.value:
            raise UsageError("saved-tag wants the emoji whose title to clear", field="value")
        await tag_set(ctx, TagSetReq(emoji=req.value, title=""))
        return SettingUnset(key=key, removed=1, values=[req.value])

    raise UsageError("unset takes top-peers, browser-exception, autosave or saved-tag", field="key")


SPEC_UNSET = OperationSpec(
    id="settings.unset",
    request=UnsetReq,
    response=SettingUnset,
    impl=unset,
    summary="Remove a per-peer/per-URL exception or a single suggestion",
    description="`removed: -1` means the server cleared the list without saying how many.",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("key", "removed", "values"),
    headers=("Key", "Removed", "Values"),
    example={"key": "browser-exception", "removed": 1, "values": ["https://example.org"]},
    example_args="settings unset browser-exception https://example.org",
    covers=("data.web-browser-settings", "privacy.top-peers-suggest"),
    covers_partial=("data.autosave-gallery",),
    coverage_note="Setting the autosave rules is `settings autosave set`.",
)


# ---------------------------------------------------------------------------
# settings autosave set
# ---------------------------------------------------------------------------


class AutosaveSetReq(Request):
    scope: Annotated[
        str | None, opt("--scope", metavar="SCOPE", help="users | chats | broadcasts.")
    ] = None
    peer: Annotated[
        str | None, opt("--peer", metavar="CHAT", help="Set an exception for one chat instead.")
    ] = None
    photos: Annotated[str | None, opt("--photos", metavar="ON|OFF", help="Auto-save photos.")] = (
        None
    )
    videos: Annotated[str | None, opt("--videos", metavar="ON|OFF", help="Auto-save videos.")] = (
        None
    )
    max_size: Annotated[
        str | None, opt("--max-size", metavar="SIZE", help="Largest video to auto-save (100M).")
    ] = None
    clear_exceptions: Annotated[
        bool, opt("--clear-exceptions", help="Drop every per-chat exception.")
    ] = False


async def autosave_set(ctx: OpContext, req: AutosaveSetReq) -> AutoSaveSaved:
    """Save-to-gallery rules for incoming media, per scope or per chat.

    Cloud-synced, so it is genuine parity even for a CLI that has no gallery:
    the setting an official client obeys is the one written here, and tlgr's
    own downloader can read it.
    """
    from telethon.tl.functions import account as fn

    from tlgr.ops.media import AutoSaveSetReq, auto_save_set

    if req.clear_exceptions and not (req.photos or req.videos or req.max_size):
        await client(ctx)(fn.DeleteAutoSaveExceptionsRequest())
        return AutoSaveSaved(scope=req.scope or "all", ok=True, cleared_exceptions=True)

    scope = {"users": "users", "chats": "groups", "broadcasts": "channels"}.get(
        (req.scope or "users").strip().lower()
    )
    if scope is None:
        raise UsageError("--scope is users, chats or broadcasts", field="scope")
    from tlgr.models.peer import parse_peer_ref

    return await auto_save_set(
        ctx,
        AutoSaveSetReq(
            scope=scope,
            chat=parse_peer_ref(req.peer) if req.peer else None,
            photos=_settings.on_off(req.photos, field="photos"),
            videos=_settings.on_off(req.videos, field="videos"),
            video_max=req.max_size,
        ),
    )


SPEC_AUTOSAVE_SET = OperationSpec(
    id="settings.autosave.set",
    request=AutosaveSetReq,
    response=AutoSaveSaved,
    impl=autosave_set,
    summary="Save-to-gallery rules for incoming media (per scope or per chat)",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("scope", "ok", "cleared_exceptions"),
    headers=("Scope", "OK", "Cleared"),
    example={"scope": "users", "ok": True, "settings": {"photos": True, "videos": False}},
    example_args="settings autosave set --scope users --photos on",
    covers=("data.autosave-gallery",),
)


# ---------------------------------------------------------------------------
# settings language list
# ---------------------------------------------------------------------------


class LanguageListReq(Request):
    pack: Annotated[
        str, opt("--pack", metavar="NAME", help="lang_pack id (android/tdesktop/ios; '' generic).")
    ] = ""
    code: Annotated[
        str | None, opt("--code", metavar="CODE", help="Fetch one language, custom slugs included.")
    ] = None


async def language_list(ctx: OpContext, req: LanguageListReq) -> Page[Language]:
    """Interface languages the server offers.

    tlgr has no localised UI of its own, but `lang_code` is sent in
    `initConnection` and decides the language of *server-side* strings —
    service messages, error texts, country names. `settings set language
    <code>` is what changes it.
    """
    from telethon.tl.functions import langpack as fn

    handle = client(ctx)
    if req.code:
        rows = [await handle(fn.GetLanguageRequest(lang_pack=req.pack, lang_code=req.code))]
    else:
        rows = list(await handle(fn.GetLanguagesRequest(lang_pack=req.pack)) or [])
    items = [
        Language(
            lang_code=str(getattr(row, "lang_code", "") or ""),
            name=str(getattr(row, "name", "") or ""),
            native_name=str(getattr(row, "native_name", "") or ""),
            official=bool(getattr(row, "official", False)),
            beta=bool(getattr(row, "beta", False)),
            rtl=bool(getattr(row, "rtl", False)),
            strings_count=int(getattr(row, "strings_count", 0) or 0),
            translated_count=int(getattr(row, "translated_count", 0) or 0),
            translations_url=getattr(row, "translations_url", None),
            plural_code=getattr(row, "plural_code", None),
            base_lang_code=getattr(row, "base_lang_code", None),
        )
        for row in rows
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_LANGUAGE_LIST = OperationSpec(
    id="settings.language.list",
    request=LanguageListReq,
    response=Page[Language],
    impl=language_list,
    summary="List interface languages available on the server",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("lang_code", "name", "native_name", "official", "beta"),
    headers=("Code", "Name", "Native", "Official", "Beta"),
    example={
        "items": [{"lang_code": "fa", "name": "Persian", "native_name": "فارسی", "official": True}],
        "has_more": False,
    },
    example_args="settings language list",
    covers=("lang.custom-pack", "lang.list", "lang.set"),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# settings theme list / create / install
# ---------------------------------------------------------------------------


def _theme_model(raw: Any) -> CloudTheme:
    document = getattr(raw, "document", None)
    slug = str(getattr(raw, "slug", "") or "")
    return CloudTheme(
        id=int(getattr(raw, "id", 0) or 0),
        access_hash=getattr(raw, "access_hash", None),
        slug=slug,
        title=str(getattr(raw, "title", "") or ""),
        creator=bool(getattr(raw, "creator", False)),
        default=bool(getattr(raw, "default", False)),
        for_chat=bool(getattr(raw, "for_chat", False)),
        installs_count=getattr(raw, "installs_count", None),
        document_id=getattr(document, "id", None),
        emoticon=getattr(raw, "emoticon", None),
        settings=[
            {
                "base_theme": type(getattr(entry, "base_theme", None)).__name__,
                "accent_color": _settings.color_text(getattr(entry, "accent_color", 0)),
                "message_colors": [
                    _settings.color_text(value)
                    for value in getattr(entry, "message_colors", None) or []
                ],
            }
            for entry in getattr(raw, "settings", None) or []
        ],
        link=f"https://t.me/addtheme/{slug}" if slug else None,
    )


class ThemeListReq(Request):
    slug: Annotated[
        str | None, opt("--slug", metavar="SLUG", help="One theme (a t.me/addtheme link works).")
    ] = None
    gift: Annotated[bool, opt("--gift", help="Collectible-gift chat themes instead.")] = False
    format: Annotated[str, opt("--format", metavar="NAME", help="Theming engine identifier.")] = (
        "tdesktop"
    )


async def theme_list(ctx: OpContext, req: ThemeListReq) -> Page[CloudTheme]:
    """Cloud themes: installed, one by slug, or the collectible-gift ones.

    Metadata only. A theme is a rendering instruction and a CLI has nothing
    to render it with; what is useful is knowing which one is installed and
    being able to install another for the phone that shares the account.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    if req.gift:
        result = await handle(fn.GetUniqueGiftChatThemesRequest(offset="", limit=100, hash=0))
        rows = [_theme_model(theme) for theme in getattr(result, "themes", None) or []]
        return Page(items=rows, has_more=False, total=getattr(result, "count", None))

    if req.slug:
        slug = req.slug.rsplit("/", 1)[-1]
        result = await handle(
            fn.GetThemeRequest(format=req.format, theme=types.InputThemeSlug(slug=slug))
        )
        return Page(items=[_theme_model(result)], has_more=False, total=1)

    result = await handle(fn.GetThemesRequest(format=req.format, hash=0))
    rows = [_theme_model(theme) for theme in getattr(result, "themes", None) or []]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_THEME_LIST = OperationSpec(
    id="settings.theme.list",
    request=ThemeListReq,
    response=Page[CloudTheme],
    impl=theme_list,
    summary="List cloud themes (installed, one by slug, or the collectible-gift themes)",
    description="Metadata only: tlgr has no theming engine and renders nothing.",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("id", "slug", "title", "creator", "installs_count"),
    headers=("Id", "Slug", "Title", "Mine", "Installs"),
    example={
        "items": [{"id": 991, "slug": "Nord", "title": "Nord", "installs_count": 4200}],
        "has_more": False,
    },
    example_args="settings theme list",
    covers=("theme.cloud-themes", "theme.get", "theme.gift-chat-themes", "theme.list-cloud"),
    tags=frozenset({"agent-safe"}),
)


class ThemeCreateReq(Request):
    title: Annotated[str | None, arg(0, metavar="TITLE", required=False, help="Theme title.")] = (
        None
    )
    slug: Annotated[
        str | None, opt("--slug", metavar="SLUG", help="Public slug; an existing one edits it.")
    ] = None
    file: Annotated[
        str | None, opt("--file", metavar="PATH", kind="path", help="Theme file to upload.")
    ] = None
    base: Annotated[
        str | None,
        opt("--base", metavar="NAME", help="classic | day | night | tinted | arctic."),
    ] = None
    accent: Annotated[
        str | None, opt("--accent", metavar="COLOR", help="Accent colour, #RRGGBB.")
    ] = None
    outbox_accent: Annotated[
        str | None, opt("--outbox-accent", metavar="COLOR", help="Outgoing accent colour.")
    ] = None
    message_colors: Annotated[
        str | None, opt("--message-colors", metavar="LIST", help="Message gradient colours.")
    ] = None
    wallpaper: Annotated[
        str | None, opt("--wallpaper", metavar="SLUG", help="Wallpaper for the theme settings.")
    ] = None
    dark: Annotated[bool, opt("--dark", help="Mark the settings vector as the dark variant.")] = (
        False
    )


_BASE_THEMES = {
    "classic": "BaseThemeClassic",
    "day": "BaseThemeDay",
    "night": "BaseThemeNight",
    "tinted": "BaseThemeTinted",
    "arctic": "BaseThemeArctic",
}


async def theme_create(ctx: OpContext, req: ThemeCreateReq) -> ThemeInstalled:
    """Publish or edit a cloud theme you own.

    Editing is creator-only, and a CLI cannot author or preview a theme file
    — it can upload one somebody made and give it a public slug, which is the
    part that needs an account.
    """
    import mimetypes

    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    document: Any = None
    if req.file:
        path = Path(os.path.expanduser(req.file))
        if not path.exists():
            raise UsageError(f"{req.file} does not exist", field="file")
        upload = getattr(ctx, "upload_file", None)
        if upload is None:  # pragma: no cover - the daemon always supplies one
            raise UsageError("this context cannot upload files")
        uploaded = await handle(
            fn.UploadThemeRequest(
                file=await upload(path),
                file_name=path.name,
                mime_type=mimetypes.guess_type(path.name)[0] or "application/x-tgtheme",
            )
        )
        document = types.InputDocument(
            id=getattr(uploaded, "id", 0),
            access_hash=getattr(uploaded, "access_hash", 0),
            file_reference=getattr(uploaded, "file_reference", b"") or b"",
        )

    settings = _theme_settings(req)
    if req.slug:
        existing = await theme_list(ctx, ThemeListReq(slug=req.slug))
        if not existing.items:
            raise NotFoundError(f"no theme with the slug {req.slug!r}")
        theme = existing.items[0]
        result = await handle(
            fn.UpdateThemeRequest(
                format="tdesktop",
                theme=types.InputTheme(id=theme.id, access_hash=theme.access_hash or 0),
                slug=req.slug,
                title=req.title,
                document=document,
                settings=settings,
            )
        )
    else:
        if not req.title:
            raise UsageError("give a TITLE (or --slug to edit an existing theme)", field="title")
        result = await handle(
            fn.CreateThemeRequest(slug="", title=req.title, document=document, settings=settings)
        )
    model = _theme_model(result)
    ctx.emit("theme_created", {"slug": model.slug})
    return ThemeInstalled(
        slug=model.slug, id=model.id, title=model.title, document_id=model.document_id
    )


def _theme_settings(req: ThemeCreateReq) -> list[Any] | None:
    from telethon.tl import types

    if req.base is None:
        return None
    if req.base not in _BASE_THEMES:
        raise UsageError(f"--base is one of: {' '.join(sorted(_BASE_THEMES))}", field="base")
    return [
        types.InputThemeSettings(
            base_theme=getattr(types, _BASE_THEMES[req.base])(),
            accent_color=_settings.color_int(req.accent, field="accent") or 0,
            outbox_accent_color=_settings.color_int(req.outbox_accent, field="outbox_accent"),
            message_colors=[
                value
                for part in (req.message_colors or "").split(",")
                if part.strip() and (value := _settings.color_int(part, field="message_colors"))
            ]
            or None,
            wallpaper=types.InputWallPaperSlug(slug=req.wallpaper) if req.wallpaper else None,
        )
    ]


SPEC_THEME_CREATE = OperationSpec(
    id="settings.theme.create",
    request=ThemeCreateReq,
    response=ThemeInstalled,
    impl=theme_create,
    summary="Publish or edit a cloud theme you own",
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("slug", "id", "title", "document_id"),
    headers=("Slug", "Id", "Title", "Document"),
    example={"slug": "Nord", "id": 991, "title": "Nord"},
    example_args="settings theme create Nord --file nord.tdesktop-theme",
    covers=("theme.create", "theme.update"),
)


class ThemeInstallReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="The theme to install or save.")]
    dark: Annotated[bool, opt("--dark", help="Install it as the dark theme.")] = False
    save: Annotated[bool, opt("--save/--no-save", help="Also add it to the saved list.")] = True
    remove: Annotated[bool, opt("--remove", help="Remove it from the saved list instead.")] = False
    format: Annotated[str, opt("--format", metavar="NAME", help="Theming engine identifier.")] = (
        "tdesktop"
    )


async def theme_install(ctx: OpContext, req: ThemeInstallReq) -> ThemeInstalled:
    """Install, save or remove a cloud theme for this account.

    Server-side bookkeeping shared with the GUI clients: what tlgr changes
    here is what the phone signed into the same account will draw.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    slug = req.slug.rsplit("/", 1)[-1]
    theme = types.InputThemeSlug(slug=slug)

    if req.remove:
        await handle(fn.SaveThemeRequest(theme=theme, unsave=True))
        return ThemeInstalled(slug=slug, removed=True)

    if req.save:
        await handle(fn.SaveThemeRequest(theme=theme, unsave=False))
    await handle(fn.InstallThemeRequest(dark=req.dark or None, theme=theme, format=req.format))
    ctx.emit("theme_installed", {"slug": slug, "dark": req.dark})
    return ThemeInstalled(slug=slug, installed=True, saved=req.save, dark=req.dark)


SPEC_THEME_INSTALL = OperationSpec(
    id="settings.theme.install",
    request=ThemeInstallReq,
    response=ThemeInstalled,
    impl=theme_install,
    summary="Install / save / remove a cloud theme for this account",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("slug", "installed", "saved", "removed"),
    headers=("Slug", "Installed", "Saved", "Removed"),
    example={"slug": "Nord", "installed": True, "saved": True},
    example_args="settings theme install Nord",
    covers=("theme.save-install",),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
