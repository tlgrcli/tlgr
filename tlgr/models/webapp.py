"""Mini apps: the manifest, the signed session URL, and the session's lifetime.

`WebAppSession.url` is a short-lived *credential*, not a link: it carries the
user's signed init data, and anyone holding it can act as the app on that
user's behalf until it expires. It is therefore printed once, with that
warning in human output, and never opened — tlgr has no browser and hosting
the `window.Telegram.WebApp` bridge is not a CLI's job.

`needs_prolong` exists because the two request families differ in a way that
is invisible from the URL: a session that came back with a `query_id` dies in
about a minute unless `webapp watch` keeps prolonging it, and one that did not
simply has no session to lose.
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "WebAppDownload",
    "WebAppInfo",
    "WebAppProlong",
    "WebAppSent",
    "WebAppSession",
]


class WebAppInfo(Model):
    """A mini app's manifest, as `messages.botApp` and `botAppSettings` hold it."""

    bot: str | None = None
    short_name: str | None = None
    title: str | None = None
    description: str | None = None
    photo: int | None = None
    document: int | None = None
    inactive: bool = False
    request_write_access: bool = False
    has_settings: bool = False
    terms_url: str | None = None
    privacy_policy_url: str | None = None
    link: str | None = None
    installed_in_attach_menu: bool = False
    installed_in_side_menu: bool = False
    #: The placeholder is an SVG-like path blob; its length is reported
    #: rather than its bytes, because nothing on a terminal can render it.
    placeholder_path: int | None = None
    bg_color: int | None = None
    bg_dark_color: int | None = None
    header_color: int | None = None
    header_dark_color: int | None = None
    button_request: dict[str, Any] | None = None


class WebAppSession(Model):
    bot: str | None = None
    kind: str = ""
    url: str = ""
    query_id: str | None = None
    expires_at: str | None = None
    fullsize: bool = False
    fullscreen: bool = False
    same_origin: bool = False
    needs_prolong: bool = False
    prolong_every: int | None = None
    write_allowed: bool = False
    inactive_confirmed: bool = False


class WebAppSent(Model):
    bot_id: int = 0
    sent: bool = False


class WebAppDownload(Model):
    allowed: bool = False
    file_name: str = ""
    url: str = ""
    downloaded: bool = False
    path: str | None = None


class WebAppProlong(Model):
    query_id: str = ""
    prolonged_at: str | None = None
    alive: bool = True
    reason: str | None = None
