"""The v1 route table, kept alive until PR-12 (§2.4, §12.4).

The handlers below are v1's, unchanged in what they return: their JSON shapes
are a documented contract and each one goes when its group migrates to the
registry. What *has* changed is everything around them. They are registered
into the v2 application (`daemon/app.py`), so they now run behind the peer-uid
check, the policy allowlist, the version handshake and idle accounting; and
`_handle_exception` funnels through `core.errors.classify`, so a flood wait is
RATE_LIMITED/exit 7 and a missing chat is NOT_FOUND/exit 5 instead of every
failure being IPC_ERROR/exit 12 (COR-06).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from tlgr.core.errors import (
    AccountNotFoundError,
    AccountRequiredError,
    classify,
    error_body_dict,
    http_status_for,
)

if TYPE_CHECKING:
    from tlgr.daemon.app import Daemon

log = logging.getLogger("tlgr.daemon.ipc")


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        body=json.dumps(data, default=str, ensure_ascii=False),
        content_type="application/json",
        status=status,
    )


def _error_response(msg: str, status: int = 400, code: str = "IPC_ERROR") -> web.Response:
    return _json_response({"error": msg, "code": code}, status=status)


def _ref(value: Any) -> Any:
    """Coerce numeric chat/user references (arriving as strings) to int."""
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            return int(s)
    return value


async def _get_body(request: web.Request) -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        return {}


def _no_client(account: str) -> web.Response:
    """Why there is no client, said precisely.

    v1 answered `404 IPC_ERROR "No client for account"` for three different
    situations — no account was given, the alias is not registered, and the
    account is registered but not usable — so the caller could not tell a typo
    from a revoked session. The empty case is ACCOUNT_REQUIRED (exit 2)
    because the daemon does not choose an account for you (COR-02).
    """
    if not account:
        return _handle_exception(
            AccountRequiredError("no account was given and the daemon does not choose one")
        )
    return _handle_exception(
        AccountNotFoundError(
            f"account {account!r} is not connected. "
            f"Check: tlgr account list, and tlgr daemon status"
        )
    )


def _handle_exception(e: Exception) -> web.Response:
    """Classify once, in the same table the v2 dispatcher uses (COR-06).

    v1 recognised three exception types here and answered 500/IPC_ERROR for
    everything else, so "this chat does not exist", "you are not an admin" and
    "the daemon is broken" were one exit code. The body keeps v1's flat shape
    — `error`, `code`, `exit_code` at the top level — because that is what its
    callers parse.
    """
    return _json_response(error_body_dict(classify(e)), status=http_status_for(e))


def register_legacy_routes(app: web.Application, daemon: Daemon) -> None:
    """Attach the v1 routes to the v2 application.

    They are no longer served by their own aiohttp app with its own
    (nonexistent) authentication; they are part of the one application whose
    middleware chain enforces §8.2 for everything.
    """
    LegacyRoutes(daemon).register(app)


class LegacyRoutes:
    def __init__(self, daemon: Daemon):
        self.daemon = daemon

    def register(self, app: web.Application) -> None:
        self._register_routes(app)

    def _register_routes(self, app: web.Application) -> None:
        # The daemon and job routes are gone: `daemon status`, `daemon stop`
        # and the whole `job` group are registry operations now, reachable at
        # `POST /v1/op` and — for a v1 caller — at the same command paths
        # through `legacy_paths` (§12.4).

        # Chats
        app.router.add_post("/chat/create", self._chat_create)
        app.router.add_get("/chat/members", self._chat_members)

        # Contacts

        # Users

        # Profile
        app.router.add_get("/profile/get", self._profile_get)
        app.router.add_post("/profile/update", self._profile_update)

        # Media

    # -- Chats --

    async def _chat_create(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.create_chat(
                body["name"],
                chat_type=body.get("type", "group"),
                members=body.get("members"),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _chat_members(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            members = await client.list_participants(
                _ref(q["chat"]),
                limit=int(q["limit"]) if q.get("limit") else None,
                admins_only=q.get("admins") == "1",
                search=q.get("search"),
            )
            return _json_response({"members": members})
        except Exception as e:
            return _handle_exception(e)

    # -- Profile --

    async def _profile_get(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            profile = await client.get_profile()
            return _json_response(profile)
        except Exception as e:
            return _handle_exception(e)

    async def _profile_update(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.update_profile(
                first_name=body.get("first_name"),
                last_name=body.get("last_name"),
                bio=body.get("bio"),
                photo=body.get("photo"),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)
