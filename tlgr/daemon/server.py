"""Daemon main loop — orchestrates Telethon clients, jobs, webhook, and IPC.

Can be run directly: python -m tlgr.daemon.server --base ~/.tlgr
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from tlgr.core.accounts import AccountManager
from tlgr.core.client import ClientWrapper
from tlgr.core.config import (
    CONFIG_DIR,
    get_socket_path,
    load_app_config,
    load_webhook_config,
)
from tlgr.gateway.config import load_gateway_configs
from tlgr.daemon.ipc import IPCServer
from tlgr.daemon.jobs import JobRunner
from tlgr.daemon.lifecycle import write_pid, setup_logging, daemonize, read_pid
from tlgr.daemon.webhook import WebhookPusher

log = logging.getLogger("tlgr.daemon")


class DaemonServer:
    def __init__(self, base: Path | None = None):
        self.base = base or CONFIG_DIR
        self._clients: dict[str, ClientWrapper] = {}
        self._job_runner = JobRunner()
        self._webhook: WebhookPusher | None = None
        self._ipc: IPCServer | None = None
        self._shutdown_event = asyncio.Event()
        self._start_time = time.time()
        self._last_ipc_time = time.time()
        self._idle_timeout: int = 1800  # 30 minutes default

    # -- Client management --

    def get_client(self, account: str = "") -> ClientWrapper | None:
        if not account:
            if self._clients:
                return next(iter(self._clients.values()))
            return None
        return self._clients.get(account)

    async def ensure_client(self, account: str = "") -> ClientWrapper | None:
        """Return a connected client, connecting a registered account on demand."""
        client = self.get_client(account)
        if client is not None:
            return client
        if not account:
            return None
        try:
            return await self._connect_account(account)
        except Exception:
            log.exception("On-demand connect failed for account '%s'", account)
            return None

    async def _connect_account(self, alias: str) -> ClientWrapper | None:
        acct_mgr = AccountManager(self.base)
        api_id, api_hash = acct_mgr.load_credentials(alias)
        if not api_id or not api_hash:
            log.warning("No credentials for account '%s'", alias)
            return None
        session_path = acct_mgr.get_session_path(alias)
        flood_max = getattr(self, '_flood_wait_max', 120)
        client = ClientWrapper(session_path, api_id, api_hash, flood_wait_max=flood_max)
        authorized = await client.connect()
        if not authorized:
            log.warning("Account '%s' not authorized — run 'tlgr account add' first", alias)
            await client.disconnect()
            return None
        self._clients[alias] = client
        log.info("Connected account '%s' (%s)", alias, client.me.first_name)
        return client

    # -- Webhook event handlers --

    async def _setup_event_handlers(self) -> None:
        """Register Telethon event handlers that push to webhook."""
        if not self._webhook or not self._webhook.config.enabled:
            return

        from telethon import events

        for alias, client in self._clients.items():
            tel = client.client

            @tel.on(events.NewMessage())
            async def on_new_message(event, _alias=alias):
                if not self._webhook.should_push("new_message", event.chat_id):
                    return
                data = _serialize_event(event)
                await self._webhook.push("new_message", data, account=_alias)

            @tel.on(events.MessageEdited())
            async def on_edited(event, _alias=alias):
                if not self._webhook.should_push("message_edited", event.chat_id):
                    return
                data = _serialize_event(event)
                await self._webhook.push("message_edited", data, account=_alias)

            @tel.on(events.MessageDeleted())
            async def on_deleted(event, _alias=alias):
                if not self._webhook.should_push("message_deleted"):
                    return
                data = {"deleted_ids": event.deleted_ids, "chat_id": getattr(event, "chat_id", None)}
                await self._webhook.push("message_deleted", data, account=_alias)

            @tel.on(events.ChatAction())
            async def on_chat_action(event, _alias=alias):
                if not self._webhook.should_push("chat_action", event.chat_id):
                    return
                data = {
                    "chat_id": event.chat_id,
                    "user_id": event.user_id,
                    "action": type(event.action_message.action).__name__ if event.action_message else "unknown",
                }
                await self._webhook.push("chat_action", data, account=_alias)

            @tel.on(events.UserUpdate())
            async def on_user_update(event, _alias=alias):
                if not self._webhook.should_push("user_joined"):
                    return
                data = {"user_id": event.user_id, "status": str(event.status) if hasattr(event, "status") else "unknown"}
                await self._webhook.push("user_joined", data, account=_alias)

            @tel.on(events.MessageRead())
            async def on_read(event, _alias=alias):
                if not self._webhook.should_push("message_read"):
                    return
                data = {"max_id": event.max_id, "chat_id": getattr(event, "chat_id", None)}
                await self._webhook.push("message_read", data, account=_alias)

    # -- Job management --

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._job_runner.list_jobs()

    async def remove_job(self, name: str) -> bool:
        return await self._job_runner.remove_job(name)

    async def enable_job(self, name: str) -> bool:
        return await self._job_runner.enable_job(name)

    async def disable_job(self, name: str) -> bool:
        return await self._job_runner.disable_job(name)

    async def reload_jobs(self) -> dict[str, Any]:
        """Hot-reload jobs from jobs.yaml without restarting the daemon."""
        new_configs = load_gateway_configs(self.base)
        app_config = load_app_config(self.base)
        acct_mgr = AccountManager(self.base)
        default_account = app_config.default_account or acct_mgr.get_active() or ""

        old_names = set(self._job_runner._jobs.keys())
        new_names = {jc.name for jc in new_configs}

        removed = old_names - new_names
        added = new_names - old_names
        updated = old_names & new_names

        for name in removed:
            await self._job_runner.remove_job(name)

        for jc in new_configs:
            if jc.name in added or jc.name in updated:
                if jc.name in updated:
                    await self._job_runner.remove_job(jc.name)
                if not jc.enabled:
                    continue
                acct = jc.account or default_account
                client = self._clients.get(acct)
                if not client:
                    acct_needed = acct
                    if acct_needed:
                        client = await self._connect_account(acct_needed)
                if not client:
                    log.warning("Job '%s' references unknown account '%s'", jc.name, acct)
                    continue
                try:
                    job = self._job_runner.create_job(jc, client, self._webhook)
                    if job.enabled:
                        job.start()
                except Exception:
                    log.exception("Failed to create job '%s'", jc.name)

        return {"reloaded": True, "added": list(added), "removed": list(removed), "updated": list(updated)}

    # -- Status & shutdown --

    def touch_ipc(self) -> None:
        """Update the last IPC activity timestamp."""
        self._last_ipc_time = time.time()

    def status(self) -> dict[str, Any]:
        uptime = int(time.time() - self._start_time)
        return {
            "running": True,
            "pid": os.getpid(),
            "uptime_seconds": uptime,
            "accounts": list(self._clients.keys()),
            "jobs": self._job_runner.list_jobs(),
        }

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    async def _idle_monitor(self) -> None:
        """Shut down daemon if idle (no jobs + no recent IPC) for idle_timeout."""
        if self._idle_timeout <= 0:
            return
        while not self._shutdown_event.is_set():
            await asyncio.sleep(60)
            active_jobs = [j for j in self._job_runner.list_jobs() if j.get("running")]
            idle_seconds = time.time() - self._last_ipc_time
            if not active_jobs and idle_seconds >= self._idle_timeout:
                log.info("Daemon idle for %ds with no active jobs — shutting down", int(idle_seconds))
                self.request_shutdown()
                return

    # -- Main run loop --

    async def run(self) -> None:
        log.info("Daemon starting (pid=%d)", os.getpid())
        write_pid(self.base)

        # Load configs
        app_config = load_app_config(self.base)
        self._flood_wait_max = app_config.daemon.flood_wait_max
        self._idle_timeout = app_config.daemon.idle_timeout
        job_configs = load_gateway_configs(self.base)
        webhook_config = load_webhook_config(self.base)

        # Determine which accounts to connect
        accounts_needed: set[str] = set()
        acct_mgr = AccountManager(self.base)
        default_account = app_config.default_account or acct_mgr.get_active() or ""

        for jc in job_configs:
            if jc.enabled:
                accounts_needed.add(jc.account or default_account)

        if not accounts_needed and default_account:
            accounts_needed.add(default_account)

        if not accounts_needed and acct_mgr.has_accounts():
            active = acct_mgr.get_active()
            if active:
                accounts_needed.add(active)

        # Connect accounts
        for alias in accounts_needed:
            if alias:
                await self._connect_account(alias)

        if not self._clients:
            log.warning("No accounts connected — daemon will serve IPC only")

        # Webhook
        self._webhook = WebhookPusher(webhook_config)
        await self._webhook.start()

        # Resolve webhook chat filters
        if webhook_config.filters.chats and self._clients:
            resolved: set[int] = set()
            client = next(iter(self._clients.values()))
            for chat_ref in webhook_config.filters.chats:
                try:
                    cid = await client.resolve_chat(chat_ref)
                    resolved.add(cid)
                except Exception:
                    log.warning("Could not resolve webhook chat filter: %s", chat_ref)
            self._webhook.set_resolved_chats(resolved)

        # Setup event handlers for webhook
        await self._setup_event_handlers()

        # Create and start jobs
        for jc in job_configs:
            if not jc.enabled:
                continue
            acct = jc.account or default_account
            client = self._clients.get(acct)
            if not client:
                log.warning("Job '%s' references unknown account '%s'", jc.name, acct)
                continue
            try:
                self._job_runner.create_job(jc, client, self._webhook)
            except Exception:
                log.exception("Failed to create job '%s'", jc.name)

        await self._job_runner.start_all()

        # Start IPC server
        sock_path = str(get_socket_path(self.base))
        # Remove stale socket
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        self._ipc = IPCServer(self, sock_path)
        await self._ipc.start()

        # Load idle timeout from config
        idle_raw = app_config.daemon.__dict__.get("idle_timeout")
        if idle_raw is not None:
            self._idle_timeout = int(idle_raw)

        log.info("Daemon ready — %d accounts, %d jobs", len(self._clients), len(job_configs))

        # Wait for shutdown signal
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, self.request_shutdown)
        loop.add_signal_handler(signal.SIGINT, self.request_shutdown)

        asyncio.create_task(self._idle_monitor())

        await self._shutdown_event.wait()

        log.info("Shutting down...")
        await self._job_runner.stop_all()
        await self._webhook.stop()
        if self._ipc:
            await self._ipc.stop()
        for client in self._clients.values():
            await client.disconnect()
        log.info("Daemon stopped")


def _serialize_event(event) -> dict[str, Any]:
    """Serialize a Telethon event to a JSON-friendly dict (raw format)."""
    data: dict[str, Any] = {
        "chat_id": getattr(event, "chat_id", None),
    }
    msg = getattr(event, "message", None)
    if msg:
        data["message"] = {
            "id": msg.id,
            "date": str(msg.date),
            "text": msg.text or "",
            "sender_id": msg.sender_id,
            "media_type": type(msg.media).__name__ if msg.media else None,
            "reply_to_msg_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
            "forward": msg.forward is not None,
        }
        if msg.entities:
            data["message"]["entities"] = [
                {"type": type(e).__name__, "offset": e.offset, "length": e.length}
                for e in msg.entities
            ]
        # Include raw Telethon object as string for full data
        try:
            data["raw"] = msg.to_dict()
        except Exception:
            pass
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="tlgr daemon")
    parser.add_argument("--base", type=str, default=str(CONFIG_DIR))
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()

    base = Path(args.base)
    base.mkdir(parents=True, exist_ok=True)

    app_config = load_app_config(base)
    setup_logging(base, app_config.daemon.log_level)

    if not args.foreground:
        # Check if already running
        existing_pid = read_pid(base)
        if existing_pid:
            print(f"Daemon already running (pid={existing_pid})", file=sys.stderr)
            sys.exit(1)
        daemonize(base)

    server = DaemonServer(base)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
