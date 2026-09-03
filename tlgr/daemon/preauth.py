"""Login flows, owned by the daemon (§6.8).

Login moved into the daemon for one reason: **the daemon owns every session
file**. `tlgr account add` in v1 opened the session file from the CLI process
while the daemon might already hold it, which is precisely the two-writers
situation that earns `AUTH_KEY_DUPLICATED` and gets the session revoked.

There is a second reason that only shows up in a two-step login. Telethon
keeps `_phone_code_hash` in memory on the client object. A CLI that sends the
code in one process and signs in from another has lost the hash, so v1's
`account add` had to hold one process open across a human typing a code from
their phone. Here the pending login lives in the daemon, keyed by alias, with
a ten-minute bound, and `auth send-code` / `auth verify-code` are two ordinary
requests.

`auth.signUp` is never called *from a login*: a number with no account stops
with `signup_required` rather than quietly registering one. Registering is a
separate, explicitly consented command (`tlgr auth sign-up --accept-tos`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tlgr.core.errors import (
    AuthenticationError,
    AuthPasswordRequiredError,
    UsageError,
)
from tlgr.core.paths import secure_session_files

log = logging.getLogger("tlgr.daemon.preauth")

__all__ = ["PendingLogin", "PreAuthService"]

#: A login a human abandoned must not pin a session file forever.
PENDING_TTL = 600.0


@dataclass
class PendingLogin:
    alias: str
    phone: str = ""
    phone_code_hash: str = ""
    code_type: str = ""
    next_type: str = ""
    timeout: int = 0
    created: float = field(default_factory=time.monotonic)
    client: Any = None
    #: The `AccountSession` whose session-file lock this login holds.
    session: Any = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > PENDING_TTL


class PreAuthService:
    """The server side of `auth send-code|verify-code|password|qr`.

    Holds a *pending* client per alias — connected, not yet authorised — and
    the `phone_code_hash` that goes with it.
    """

    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions
        self._pending: dict[str, PendingLogin] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    def _sweep(self) -> None:
        for alias, pending in list(self._pending.items()):
            if pending.expired:
                log.info("abandoning the expired login for %s", alias, extra={"account": alias})
                self._pending.pop(alias, None)
                _close(pending.client)

    @property
    def pending_count(self) -> int:
        self._sweep()
        return len(self._pending)

    async def _client_for(self, alias: str) -> Any:
        """A connected, unauthorised client for *alias*, created once."""
        pending = self._pending.get(alias)
        if pending is not None and not pending.expired:
            return pending.client
        session = self.sessions._make(alias)
        session.lock.acquire()
        client = session._factory(session.session_path, session.options)
        await client.connect()
        self._pending[alias] = PendingLogin(alias=alias, client=client, session=session)
        return client

    # -- the primitives the `auth` operations drive -------------------------
    #
    # PR-2 moved the *protocol* into `ops/auth.py`, where the TL requests and
    # their errors belong, and left the daemon with what only the daemon can
    # do: hand out a pre-auth client on a session file it already locks, and
    # promote that client into a supervised session once the login lands.

    async def client_for(
        self, alias: str, *, api_id: int | None = None, api_hash: str | None = None
    ) -> Any:
        """A connected, unauthorised client for *alias*, created once.

        Credentials are written first when they are supplied, because the
        client cannot be built without them and an `auth send-code` that
        carried an `--api-id` should not fail on a missing one.
        """
        if api_id and api_hash:
            self.sessions.accounts.save_credentials(int(api_id), str(api_hash), alias)
        async with self._lock:
            return await self._client_for(alias)

    def pending(self, alias: str) -> PendingLogin | None:
        """The in-progress login for *alias*, or None. Never creates one."""
        self._sweep()
        return self._pending.get(alias)

    def require(self, alias: str) -> PendingLogin:
        """The in-progress login for *alias*, or a USAGE error naming the fix."""
        return self._require(alias)

    def remember(self, alias: str, **fields: Any) -> None:
        """Update the pending login and mirror it into `login-state.json`.

        The file is what makes an *unattended* login work across a daemon
        restart: `auth send-code` in one process and `auth verify-code` in
        another, half an hour later, is the shape an agent actually runs.
        """
        pending = self._pending.get(alias)
        if pending is not None:
            for name, value in fields.items():
                if hasattr(pending, name):
                    setattr(pending, name, value)
        state = self.read_state(alias)
        state.update({k: v for k, v in fields.items() if isinstance(v, (str, int, float, bool))})
        state["updated"] = time.time()
        self.write_state(alias, state)

    def state_path(self, alias: str) -> Path:
        from tlgr.core.paths import validate_alias

        return self.sessions.paths.account_dir(validate_alias(alias)) / "login-state.json"

    def read_state(self, alias: str) -> dict[str, Any]:
        path = self.state_path(alias)
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def write_state(self, alias: str, data: dict[str, Any]) -> None:
        """0600, always: a `phone_code_hash` is a login credential in flight."""
        from tlgr.core.paths import write_private

        self.sessions.paths.ensure_account_dir(alias)
        write_private(self.state_path(alias), json.dumps(data, indent=2, sort_keys=True))

    def clear_state(self, alias: str) -> None:
        with contextlib.suppress(OSError):
            self.state_path(alias).unlink(missing_ok=True)

    async def finish(self, alias: str) -> dict[str, Any]:
        """Record who we are and hand the session to the supervisor."""
        self.clear_state(alias)
        return await self._finish(alias)

    def except_ids(self) -> list[int]:
        """User ids already logged in here, so a QR login cannot re-add one."""
        found: list[int] = []
        for info in self.sessions.accounts.list_accounts():
            if info.user_id:
                found.append(int(info.user_id))
        return found

    def _require(self, alias: str) -> PendingLogin:
        self._sweep()
        pending = self._pending.get(alias)
        if pending is None:
            raise UsageError(
                f"no login is in progress for {alias!r}; run: tlgr auth send-code first",
                field="account",
            )
        return pending

    # -- flows -------------------------------------------------------------

    async def send_code(self, alias: str, phone: str) -> dict[str, Any]:
        async with self._lock:
            client = await self._client_for(alias)
            try:
                sent = await client.send_code_request(phone)
            except Exception as exc:
                raise _auth_error(exc) from exc
            pending = self._pending[alias]
            pending.phone = phone
            pending.phone_code_hash = getattr(sent, "phone_code_hash", "") or ""
            pending.code_type = type(getattr(sent, "type", None)).__name__
            pending.next_type = type(getattr(sent, "next_type", None)).__name__
            pending.timeout = int(getattr(sent, "timeout", 0) or 0)
            return {
                "account": alias,
                "phone": _mask(phone),
                "type": pending.code_type,
                "next_type": pending.next_type or None,
                "timeout": pending.timeout,
                "expires_in": int(PENDING_TTL),
            }

    async def resend_code(self, alias: str) -> dict[str, Any]:
        pending = self._require(alias)
        try:
            sent = await pending.client.send_code_request(pending.phone, force_sms=True)
        except Exception as exc:
            raise _auth_error(exc) from exc
        pending.phone_code_hash = getattr(sent, "phone_code_hash", "") or ""
        return {
            "account": alias,
            "resent": True,
            "type": type(getattr(sent, "type", None)).__name__,
        }

    async def verify_code(self, alias: str, code: str) -> dict[str, Any]:
        pending = self._require(alias)
        try:
            await pending.client.sign_in(
                phone=pending.phone, code=code, phone_code_hash=pending.phone_code_hash
            )
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                raise AuthPasswordRequiredError(
                    "this account has a two-factor password; "
                    "supply it with tlgr auth password --password-env TLGR_2FA_PASSWORD"
                ) from exc
            if type(exc).__name__ == "PhoneNumberUnoccupiedError":
                raise UsageError(
                    "that number has no Telegram account. Registering one is a separate, "
                    "explicit step: tlgr auth sign-up --first-name … --accept-tos"
                ) from exc
            raise _auth_error(exc) from exc
        return await self._finish(alias)

    async def password(self, alias: str, password: str) -> dict[str, Any]:
        pending = self._require(alias)
        try:
            await pending.client.sign_in(password=password)
        except Exception as exc:
            raise _auth_error(exc) from exc
        return await self._finish(alias)

    async def qr(self, alias: str) -> AsyncIterator[dict[str, Any]]:
        """Stream QR login tokens until one is accepted (§6.8).

        QR is the login method that always works for a third-party `api_id`:
        the code path can answer `UPDATE_APP_TO_LOGIN`, which no amount of
        retrying fixes.
        """
        async with self._lock:
            client = await self._client_for(alias)
        deadline = time.monotonic() + PENDING_TTL
        while time.monotonic() < deadline:
            login = await client.qr_login()
            yield {
                "type": "qr",
                "url": login.url,
                "expires": getattr(login, "expires", None),
            }
            try:
                await login.wait(timeout=min(60, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                if type(exc).__name__ == "SessionPasswordNeededError":
                    yield {"type": "password_required"}
                    return
                raise _auth_error(exc) from exc
            yield {"type": "done", **await self._finish(alias)}
            return
        yield {"type": "end", "ok": False, "reason": "the QR login timed out"}

    async def _finish(self, alias: str) -> dict[str, Any]:
        """Record who we are, release the pending client, hand over to the manager."""
        pending = self._pending.pop(alias)
        me = await pending.client.get_me()
        with contextlib.suppress(Exception):
            await pending.client.disconnect()
        # The session file is a complete account credential and Telethon
        # writes it with whatever the umask allowed (SEC-07).
        with contextlib.suppress(Exception):
            secure_session_files(self.sessions.paths.session(alias))
        if pending.session is not None:
            pending.session.lock.release()
        self.sessions.accounts.update_account(
            alias,
            phone=getattr(me, "phone", None),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", None),
            user_id=getattr(me, "id", None),
        )
        self.sessions.accounts.set_health(
            alias, "stopped", reason="", user_id=getattr(me, "id", None)
        )
        # Reconnect through the normal path so the account is supervised from
        # the first second rather than living on the pre-auth client.
        await self.sessions.ensure(alias)
        return {
            "account": alias,
            "user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "authorized": True,
        }

    async def cancel(self, alias: str) -> dict[str, Any]:
        pending = self._pending.pop(alias, None)
        if pending is None:
            return {"account": alias, "cancelled": False}
        _close(pending.client)
        if pending.session is not None:
            pending.session.lock.release()
        return {"account": alias, "cancelled": True}


#: Disconnects fired from a sweep have no caller to await them; the set keeps
#: a reference so the loop cannot collect the task mid-disconnect (COR-41).
_closing: set[asyncio.Task[Any]] = set()


def _close(client: Any) -> None:
    if client is None:
        return
    with contextlib.suppress(Exception):
        task = asyncio.get_event_loop().create_task(client.disconnect())
        _closing.add(task)
        task.add_done_callback(_closing.discard)


def _auth_error(exc: BaseException) -> BaseException:
    from tlgr.core.errors import rule_for

    rule = rule_for(exc)
    if rule.code in ("USAGE", "RATE_LIMITED", "AUTH_PASSWORD_REQUIRED"):
        return exc
    return AuthenticationError(str(exc))


def _mask(phone: str) -> str:
    """Never echo a full phone number back; it is in the log and the reply."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 6:
        return "***"
    return f"{digits[:3]}…{digits[-2:]}"
