"""Cloud-synced account settings, languages and cloud themes.

`settings get`/`settings set` are one generic pair over a dozen unrelated
RPCs, so the model is a *key and a value* rather than a struct per toggle.
That is not laziness: every key prints the exact token vocabulary its setter
accepts, which makes `tlgr settings get X | tlgr settings set X -` a real
round trip and keeps twelve near-identical commands out of the surface.

`previous` is always reported on a write. A setting that was already in the
wanted state answers `already: true` with `previous == value`, which is the
only way a script can tell "I changed it" from "it was like that".
"""

from __future__ import annotations

from typing import Any

from tlgr.models.base import Model

__all__ = [
    "CloudTheme",
    "Language",
    "SettingChange",
    "SettingUnset",
    "SettingValue",
    "ThemeInstalled",
]


class SettingValue(Model):
    """One cloud setting, read.

    `changeable` is false when the server will refuse the write for a reason
    that is not an error — Premium-only keys, and the sensitive-content
    toggle in regions that require an age check first.
    """

    key: str
    value: Any = None
    changeable: bool = True
    #: server | app-config | derived — where the value came from.
    source: str = "server"
    accepts: str = ""
    reason: str | None = None


class SettingChange(Model):
    key: str
    value: Any = None
    previous: Any = None
    already: bool = False
    #: `auto-delete --apply-to-existing`: how many chats were rewritten.
    applied_to: int | None = None


class SettingUnset(Model):
    key: str
    removed: int
    values: list[str] = []
    already: bool = False


class Language(Model):
    lang_code: str
    name: str = ""
    native_name: str = ""
    official: bool = False
    beta: bool = False
    rtl: bool = False
    strings_count: int = 0
    translated_count: int = 0
    translations_url: str | None = None
    plural_code: str | None = None
    base_lang_code: str | None = None


class CloudTheme(Model):
    """A cloud theme's metadata. tlgr has no theming engine and renders none."""

    id: int = 0
    access_hash: int | None = None
    slug: str = ""
    title: str = ""
    creator: bool = False
    default: bool = False
    for_chat: bool = False
    installs_count: int | None = None
    document_id: int | None = None
    emoticon: str | None = None
    settings: list[dict[str, Any]] = []
    link: str | None = None


class ThemeInstalled(Model):
    slug: str | None = None
    id: int | None = None
    title: str | None = None
    installed: bool = False
    saved: bool = False
    removed: bool = False
    dark: bool = False
    document_id: int | None = None
    already: bool = False
