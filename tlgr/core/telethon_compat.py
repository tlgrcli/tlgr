"""Every private Telethon API tlgr touches, behind a feature probe.

tlgr needs four things Telethon 1.44 does not expose publicly: persisting
update state without disconnecting, learning that a reconnect happened,
learning that the server said "your gap is too long", and reading a request's
flood-wait memory. Reaching into `client._save_states_and_entities` from five
call sites would mean five tracebacks the day Telethon renames it.

The rule here: **probe once, warn once, degrade**. Each adapter checks for the
attribute it needs, logs a single warning naming the Telethon version if it is
gone, and returns a value that lets the caller carry on. Losing the periodic
state save costs at most one `catch_up()`; losing the `*TooLong` hook costs a
resync the daemon would have done anyway on its 15-minute backstop. Neither is
worth a crash, and both are worth a log line that names the cause.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger("tlgr.compat")

__all__ = [
    "TOO_LONG_CHANNEL",
    "TOO_LONG_GLOBAL",
    "entity_count",
    "install_reconnect_hook",
    "install_too_long_hook",
    "probe",
    "save_state",
    "session_state",
    "set_session_state",
    "telethon_version",
]

TOO_LONG_GLOBAL = "global"
TOO_LONG_CHANNEL = "channel"

_warned: set[str] = set()


def telethon_version() -> str:
    try:
        import telethon

        return str(telethon.__version__)
    except Exception:  # pragma: no cover - telethon is a hard dependency
        return "unknown"


def _warn_once(feature: str, detail: str = "") -> None:
    if feature in _warned:
        return
    _warned.add(feature)
    log.warning(
        "telethon %s does not expose %s%s; tlgr degrades that behaviour",
        telethon_version(),
        feature,
        f" ({detail})" if detail else "",
    )


def probe(client: Any, attribute: str) -> bool:
    """Is *attribute* present on this client? Warns once when it is not."""
    if hasattr(client, attribute):
        return True
    _warn_once(attribute)
    return False


async def save_state(client: Any) -> bool:
    """Persist update state and the entity cache without disconnecting.

    Telethon writes `pts`/`qts`/`date` only on `disconnect()`. A daemon that is
    SIGKILLed, or a laptop that loses power, therefore replays from whatever
    the session file last held — in v1, from the last clean shutdown, which
    could be days. Called every `[daemon] state_save_interval` seconds and on
    every shutdown path.
    """
    ok = True
    saver = getattr(client, "_save_states_and_entities", None)
    if callable(saver):
        try:
            result = saver()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # pragma: no cover - depends on session backend
            log.debug("state save failed: %s", exc)
            ok = False
    else:
        _warn_once("_save_states_and_entities")
        ok = False

    session = getattr(client, "session", None)
    save = getattr(session, "save", None)
    if callable(save):
        try:
            save()
        except Exception as exc:  # pragma: no cover
            log.debug("session save failed: %s", exc)
            ok = False
    return ok


def install_reconnect_hook(client: Any, callback: Callable[[], Any]) -> bool:
    """Call *callback* after Telethon finishes an automatic reconnect.

    Telethon's `_handle_auto_reconnect` re-runs `get_me()` and nothing else, so
    an account that dropped for ten minutes comes back with a stale `pts` and
    silently misses everything that happened (checklist 1/2). Wrapping it is
    the only hook there is; when it is gone the supervisor's own reconnect path
    still calls `catch_up()`, so this is an optimisation, not a requirement.
    """
    original = getattr(client, "_handle_auto_reconnect", None)
    if not callable(original):
        _warn_once("_handle_auto_reconnect")
        return False

    async def wrapper() -> Any:
        result = original()
        if hasattr(result, "__await__"):
            result = await result
        try:
            outcome = callback()
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception as exc:  # pragma: no cover - the callback logs its own
            log.debug("reconnect hook failed: %s", exc)
        return result

    client._handle_auto_reconnect = wrapper
    return True


def install_too_long_hook(client: Any, callback: Callable[[str, int | None], Any]) -> bool:
    """Report `differenceTooLong` / `channelDifferenceTooLong` to *callback*.

    Telethon consumes both inside `MessageBox` and delivers nothing to
    handlers, so a client that was offline long enough to blow the server's
    difference window silently skips history (checklist 9). The hook wraps
    `MessageBox.apply_difference` / `apply_channel_difference` **on this
    client's own box instance**, never on the class, so one account's hook
    cannot fire for another's.

    `callback(scope, channel_id)` is called with `TOO_LONG_GLOBAL`/`None` or
    `TOO_LONG_CHANNEL`/`<id>`.
    """
    box = getattr(client, "_message_box", None)
    if box is None:
        _warn_once("_message_box")
        return False

    installed = False

    original_global = getattr(box, "apply_difference", None)
    if callable(original_global):

        def apply_difference(diff: Any, chat_hashes: Any, _orig: Any = original_global) -> Any:
            if type(diff).__name__ == "DifferenceTooLong":
                _safe(callback, TOO_LONG_GLOBAL, None)
            return _orig(diff, chat_hashes)

        box.apply_difference = apply_difference
        installed = True
    else:  # pragma: no cover - present in 1.44
        _warn_once("MessageBox.apply_difference")

    original_channel = getattr(box, "apply_channel_difference", None)
    if callable(original_channel):

        def apply_channel_difference(
            request: Any, diff: Any, chat_hashes: Any, _orig: Any = original_channel
        ) -> Any:
            if type(diff).__name__ == "ChannelDifferenceTooLong":
                channel = getattr(request, "channel", None)
                _safe(callback, TOO_LONG_CHANNEL, getattr(channel, "channel_id", None))
            return _orig(request, diff, chat_hashes)

        box.apply_channel_difference = apply_channel_difference
        installed = True
    else:  # pragma: no cover - present in 1.44
        _warn_once("MessageBox.apply_channel_difference")

    return installed


def _safe(callback: Callable[..., Any], *args: Any) -> None:
    try:
        callback(*args)
    except Exception as exc:  # pragma: no cover
        log.debug("too-long hook failed: %s", exc)


def clear_config_cache(client: Any) -> bool:
    """Drop Telethon's cached `help.getConfig` so the next call refetches.

    `UpdateConfig`/`UpdateDcOptions` mean the DC list or the limits changed
    (checklist 12). Telethon caches the config for an hour and does not listen
    for the update, so a client that saw a DC migration keeps using the old
    address list until the cache ages out.
    """
    if hasattr(client, "_config"):
        client._config = None
        return True
    _warn_once("_config")
    return False


def session_state(client: Any) -> tuple[dict[str, Any], dict[int, int]]:
    """`({pts, qts, seq, date}, {channel_id: pts})` from the session.

    Telethon 1.44 has no public accessor for its update state: the common box
    lives in the session's `update_state` table under entity id 0 and the
    per-channel boxes under their channel ids. Reading it here — once, behind
    a name — is what lets `sync status` answer "how far behind is this
    account" without every caller reaching into a private table.
    """
    common: dict[str, Any] = {}
    channels: dict[int, int] = {}
    session = getattr(client, "session", None)
    getter = getattr(session, "get_update_states", None)
    if not callable(getter):
        _warn_once("session.get_update_states")
        return common, channels
    try:
        rows = list(getter())
    except Exception as exc:  # pragma: no cover - depends on session backend
        log.debug("update state read failed: %s", exc)
        return common, channels
    for entity_id, state in rows:
        if int(entity_id) == 0:
            date = getattr(state, "date", None)
            common = {
                "pts": getattr(state, "pts", None),
                "qts": getattr(state, "qts", None),
                "seq": getattr(state, "seq", None),
                "date": date.strftime("%Y-%m-%dT%H:%M:%SZ") if date is not None else None,
                "date_unix": int(date.timestamp()) if date is not None else None,
                "unread_count": getattr(state, "unread_count", None),
            }
        else:
            channels[int(entity_id)] = int(getattr(state, "pts", 0) or 0)
    return common, channels


def set_session_state(client: Any, state: Any, entity_id: int = 0) -> bool:
    """Write one update-state row. The `sync reset` half of the pair above."""
    session = getattr(client, "session", None)
    setter = getattr(session, "set_update_state", None)
    if not callable(setter):
        _warn_once("session.set_update_state")
        return False
    try:
        setter(entity_id, state)
    except Exception as exc:  # pragma: no cover - depends on session backend
        log.debug("update state write failed: %s", exc)
        return False
    return True


def entity_count(client: Any) -> int:
    """How many peers the session has an access hash for.

    Not a statistic: an entity missing from here is a channel `catch_up()`
    will silently skip, because `getChannelDifference` needs the access hash
    and Telethon will not ask for one it does not have.
    """
    session = getattr(client, "session", None)
    cursor = getattr(session, "_cursor", None)
    if callable(cursor):
        try:
            row = cursor().execute("select count(*) from entities").fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:  # pragma: no cover - depends on session backend
            log.debug("entity count failed: %s", exc)
    cache = getattr(client, "_entity_cache", None)
    with contextlib.suppress(TypeError):
        return len(cache) if cache is not None else 0
    return 0


def flood_waited_requests(client: Any) -> dict[int, float]:
    """Telethon's in-process flood memory, `{constructor_id: until_unix}`.

    Read-only, and lost on restart — which is why tlgr keeps its own persisted
    copy (§6.4). Exposed here so the daemon can report both in `/v1/status`.
    """
    waited = getattr(client, "_flood_waited_requests", None)
    if isinstance(waited, dict):
        return dict(waited)
    _warn_once("_flood_waited_requests")
    return {}
