"""The strings tlgr sends in `initConnection`, and why they are honest.

Telegram shows `device_model` / `system_version` / `app_version` in
Settings → Devices. Two rules govern what goes there:

* **Never an official app's identity.** Borrowing an official `api_id` or
  spoofing `device_model` to obtain official-app behaviour is a ToS violation
  that gets accounts banned (§1.2). tlgr says what it is.
* **Stable across restarts.** Telethon's defaults are derived from
  `platform.uname()` at each start; anything that varies (a container
  hostname, a kernel patch level) makes the Devices entry churn, and a user
  who checks their sessions sees a new "device" every reboot. The resolved
  strings are therefore written once to `identity.json` and reused.
"""

from __future__ import annotations

import contextlib
import json
import locale
import platform
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from tlgr.core.paths import TlgrPaths, write_private

__all__ = ["Identity", "load_identity"]


@dataclass(frozen=True)
class Identity:
    device_model: str
    system_version: str
    app_version: str
    lang_code: str
    system_lang_code: str

    def params(self) -> dict[str, int]:
        """`initConnection.params` — the tz offset official clients send."""
        return {"tz_offset": -time.timezone if not time.daylight else -time.altzone}


def _derive_device_model() -> str:
    node = platform.node().split(".")[0] or "unknown-host"
    machine = platform.machine() or "unknown"
    return f"{node} ({machine})"[:64]


def _derive_system_version() -> str:
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0] or platform.release()
        return f"macOS {release}"[:64]
    if system == "Linux":
        # `platform.freedesktop_os_release()` needs 3.10+ and is absent in a
        # bare container; the kernel release is always there.
        try:
            info = platform.freedesktop_os_release()
            name = info.get("PRETTY_NAME") or info.get("NAME") or "Linux"
        except (OSError, AttributeError):
            name = "Linux"
        return f"{name} ({platform.release()})"[:64]
    return f"{system} {platform.release()}"[:64]


def _derive_lang() -> tuple[str, str]:
    try:
        code = (locale.getlocale()[0] or "").split("_")[0].lower()
    except (ValueError, TypeError):  # pragma: no cover - locale is odd on some CI images
        code = ""
    lang = code if len(code) == 2 else "en"
    return lang, lang


def load_identity(
    base: Path | None = None,
    *,
    device_model: str = "",
    system_version: str = "",
    lang_code: str = "",
    system_lang_code: str = "",
    app_version: str = "",
) -> Identity:
    """Resolve the identity, preferring config, then the cache, then the OS.

    The cache is written the first time so that the value survives an OS
    upgrade: what matters to the Devices list is that the string does not
    change, not that it stays accurate to the patch level.
    """
    from tlgr import __version__

    paths = TlgrPaths(base)
    cached: dict[str, str] = {}
    if paths.identity.exists():
        try:
            loaded = json.loads(paths.identity.read_text())
            if isinstance(loaded, dict):
                cached = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError):
            cached = {}

    derived_lang, derived_system_lang = _derive_lang()
    if not app_version:
        try:
            import telethon

            app_version = f"tlgr {__version__} (Telethon {telethon.__version__})"
        except Exception:  # pragma: no cover - telethon is a hard dependency
            app_version = f"tlgr {__version__}"

    identity = Identity(
        device_model=device_model or cached.get("device_model") or _derive_device_model(),
        system_version=system_version or cached.get("system_version") or _derive_system_version(),
        app_version=app_version,
        lang_code=lang_code or cached.get("lang_code") or derived_lang,
        system_lang_code=(
            system_lang_code or cached.get("system_lang_code") or derived_system_lang
        ),
    )

    if cached != asdict(identity):
        # A read-only home must not stop a login; the identity is then
        # re-derived next time, which is stable anyway on a stable machine.
        with contextlib.suppress(OSError):
            write_private(paths.identity, json.dumps(asdict(identity), indent=2))
    return identity
