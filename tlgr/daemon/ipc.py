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

import asyncio
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
        # Daemon
        app.router.add_get("/daemon/status", self._daemon_status)
        app.router.add_post("/daemon/stop", self._daemon_stop)

        # Messages
        app.router.add_post("/message/send", self._message_send)
        app.router.add_get("/message/list", self._message_list)
        app.router.add_get("/message/get", self._message_get)
        app.router.add_post("/message/delete", self._message_delete)
        app.router.add_get("/message/search", self._message_search)
        app.router.add_post("/message/pin", self._message_pin)
        app.router.add_post("/message/react", self._message_react)
        app.router.add_post("/message/edit", self._message_edit)
        app.router.add_post("/message/forward", self._message_forward)

        app.router.add_post("/message/read", self._message_read)

        # Chats
        app.router.add_get("/chat/list", self._chat_list)
        app.router.add_get("/chat/catchup", self._chat_catchup)
        app.router.add_post("/chat/open", self._chat_open)
        app.router.add_post("/chat/unread", self._chat_unread)
        app.router.add_get("/chat/get", self._chat_get)
        app.router.add_post("/chat/create", self._chat_create)
        app.router.add_post("/chat/archive", self._chat_archive)
        app.router.add_post("/chat/mute", self._chat_mute)
        app.router.add_post("/chat/leave", self._chat_leave)
        app.router.add_post("/chat/typing", self._chat_typing)
        app.router.add_get("/chat/members", self._chat_members)
        app.router.add_get("/chat/posters", self._chat_posters)

        app.router.add_post("/draft/set", self._draft_set)
        app.router.add_post("/draft/clear", self._draft_clear)
        app.router.add_get("/draft/list", self._draft_list)

        # Contacts
        app.router.add_get("/contact/list", self._contact_list)
        app.router.add_post("/contact/add", self._contact_add)
        app.router.add_post("/contact/remove", self._contact_remove)
        app.router.add_get("/contact/search", self._contact_search)
        app.router.add_post("/contact/rename", self._contact_rename)

        # Users
        app.router.add_get("/user/get", self._user_get)
        app.router.add_get("/user/dialog-status", self._user_dialog_status)
        app.router.add_post("/user/stories-hidden", self._user_stories_hidden)

        # Profile
        app.router.add_get("/profile/get", self._profile_get)
        app.router.add_post("/profile/update", self._profile_update)

        # Media
        app.router.add_post("/media/download", self._media_download)
        app.router.add_post("/media/upload", self._media_upload)

        # Jobs
        app.router.add_get("/job/list", self._job_list)
        app.router.add_post("/job/remove", self._job_remove)
        app.router.add_post("/job/enable", self._job_enable)
        app.router.add_post("/job/disable", self._job_disable)
        app.router.add_post("/job/reload", self._job_reload)

    # -- Daemon --

    async def _daemon_status(self, request: web.Request) -> web.Response:
        return _json_response(self.daemon.status())

    async def _daemon_stop(self, request: web.Request) -> web.Response:
        asyncio.get_event_loop().call_soon(self.daemon.request_shutdown)
        return _json_response({"stopping": True})

    # -- Messages --

    async def _message_send(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.send_message(
                _ref(body["chat"]),
                body.get("text", ""),
                reply_to=body.get("reply_to"),
                silent=body.get("silent", False),
                file=body.get("file"),
                caption=body.get("caption"),
                typing_s=float(body.get("typing_s", 0) or 0),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _message_list(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            msgs = await client.get_messages(
                _ref(q["chat"]),
                limit=int(q.get("limit", 20)),
                offset_id=int(q.get("offset_id", 0)),
                include_sender=q.get("sender") == "1",
                include_media=q.get("media") == "1",
                include_reactions=q.get("reactions") == "1",
                include_entities=q.get("entities") == "1",
            )
            return _json_response({"messages": msgs})
        except Exception as e:
            return _handle_exception(e)

    async def _message_get(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            msg = await client.get_message(_ref(q["chat"]), int(q["msg_id"]))
            return _json_response(msg)
        except Exception as e:
            return _handle_exception(e)

    async def _message_delete(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            deleted = await client.delete_messages(_ref(body["chat"]), body["msg_ids"])
            return _json_response({"deleted": deleted})
        except Exception as e:
            return _handle_exception(e)

    async def _message_search(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            msgs = await client.search_messages(
                _ref(q["chat"]),
                q.get("query", ""),
                limit=int(q.get("limit", 20)),
                local=q.get("local") == "1",
                regex=q.get("regex"),
            )
            return _json_response({"messages": msgs})
        except Exception as e:
            return _handle_exception(e)

    async def _message_pin(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.pin_message(_ref(body["chat"]), body["msg_id"])
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _message_react(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.react_to_message(
                _ref(body["chat"]), body["msg_id"], body["emoji"]
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _message_edit(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.edit_message(
                _ref(body["chat"]),
                body["msg_id"],
                body.get("text", ""),
                typing_s=float(body.get("typing_s", 0) or 0),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _message_forward(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.forward_messages(
                _ref(body["from_chat"]),
                body["msg_ids"],
                _ref(body["to_chat"]),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    # -- Drafts --

    async def _draft_set(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.set_draft(
                _ref(body["chat"]),
                body.get("text", ""),
                reply_to=body.get("reply_to"),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _draft_clear(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.set_draft(_ref(body["chat"]), "")
            result["cleared"] = True
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _draft_list(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            drafts = await client.list_drafts()
            return _json_response({"drafts": drafts})
        except Exception as e:
            return _handle_exception(e)

    async def _message_read(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.mark_read(_ref(body["chat"]), up_to=body.get("up_to"))
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    # -- Chats --

    async def _chat_list(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            chats: list[dict[str, Any]] = []
            async for c in client.list_chats(
                limit=int(q.get("limit", 100)) if q.get("limit") else None,
                chat_type=q.get("type"),
                search=q.get("search"),
                unread_only=q.get("unread") == "1",
                offset=int(q.get("offset", 0)),
            ):
                chats.append(c)
            return _json_response({"chats": chats})
        except Exception as e:
            return _handle_exception(e)

    async def _chat_catchup(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            chats = await client.catchup(
                limit_chats=int(q.get("limit_chats", 20)),
                per_chat=int(q.get("per_chat", 10)),
                chat_type=q.get("type"),
            )
            return _json_response({"chats": chats})
        except Exception as e:
            return _handle_exception(e)

    async def _chat_open(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.open_chat(
                _ref(body["chat"]),
                limit=int(body.get("limit", 30)),
                mark_read=body.get("mark_read", True),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _chat_unread(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.mark_chat_unread(
                _ref(body["chat"]), unread=body.get("unread", True)
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _chat_get(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            info = await client.get_chat_info(_ref(q["chat"]))
            return _json_response(info)
        except Exception as e:
            return _handle_exception(e)

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

    async def _chat_archive(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.archive_chat(_ref(body["chat"]))
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _chat_mute(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.mute_chat(_ref(body["chat"]), body.get("duration"))
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _chat_leave(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.leave_chat(_ref(body["chat"]))
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _chat_typing(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.send_typing(_ref(body["chat"]), duration=body.get("duration", 5))
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

    async def _chat_posters(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.chat_posters(
                _ref(q["chat"]),
                limit=int(q["limit"]) if q.get("limit") else None,
                max_messages=int(q.get("max_messages", 2000)),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    # -- Contacts --

    async def _contact_list(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            contacts = await client.list_contacts()
            return _json_response({"contacts": contacts})
        except Exception as e:
            return _handle_exception(e)

    async def _contact_add(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.add_contact(body["phone"], body.get("name", ""))
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _contact_remove(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.remove_contact(_ref(body["user"]))
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _contact_rename(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.rename_contact(
                _ref(body["user"]),
                first_name=body.get("first_name"),
                last_name=body.get("last_name"),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _contact_search(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            contacts = await client.search_contacts(q.get("query", ""))
            return _json_response({"contacts": contacts})
        except Exception as e:
            return _handle_exception(e)

    # -- Users --

    async def _user_get(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            info = await client.get_user_info(_ref(q["user"]))
            return _json_response(info)
        except Exception as e:
            return _handle_exception(e)

    async def _user_dialog_status(self, request: web.Request) -> web.Response:
        q = request.query
        account = q.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            status = await client.dialog_status(
                _ref(q["user"]),
                max_dialogs=int(q.get("max_dialogs", 5000)),
            )
            return _json_response(status)
        except Exception as e:
            return _handle_exception(e)

    async def _user_stories_hidden(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.set_stories_hidden(
                _ref(body["user"]), hidden=bool(body.get("hidden", True))
            )
            return _json_response(result)
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

    # -- Media --

    async def _media_download(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.download_media(
                _ref(body["chat"]),
                body["msg_id"],
                out_dir=body.get("out_dir"),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    async def _media_upload(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        account = body.get("account", "")
        client = await self.daemon.ensure_client(account)
        if not client:
            return _no_client(account)
        try:
            result = await client.upload_file(
                _ref(body["chat"]),
                body["path"],
                caption=body.get("caption", ""),
            )
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)

    # -- Jobs --

    async def _job_list(self, request: web.Request) -> web.Response:
        return _json_response({"jobs": self.daemon.list_jobs()})

    async def _job_remove(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        ok = await self.daemon.remove_job(body["name"])
        return _json_response({"removed": ok})

    async def _job_enable(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        ok = await self.daemon.enable_job(body["name"])
        return _json_response({"enabled": ok})

    async def _job_disable(self, request: web.Request) -> web.Response:
        body = await _get_body(request)
        ok = await self.daemon.disable_job(body["name"])
        return _json_response({"disabled": ok})

    async def _job_reload(self, request: web.Request) -> web.Response:
        try:
            result = await self.daemon.reload_jobs()
            return _json_response(result)
        except Exception as e:
            return _handle_exception(e)
