"""Streaming event watch command."""

from __future__ import annotations

import json
import sys
import time

import click

from tlgr.cli._common import resolve_account
from tlgr.ipc_client import ipc_request


@click.command("watch")
@click.option("--chat", "chats", multiple=True, help="Chat(s) to watch (default: all).")
@click.option("--events", default="new_message", help="Comma-separated event types.")
@click.option("--account", "-a", default=None)
@click.pass_context
def watch_command(
    ctx: click.Context, chats: tuple[str, ...], events: str, account: str | None
) -> None:
    """Stream events as newline-delimited JSON. Ctrl+C to stop.

    Polls the daemon for new messages and emits one JSON object per line.
    """
    acct = resolve_account(ctx, account)
    event_types = {e.strip() for e in events.split(",") if e.strip()}
    chat_set = set(chats) if chats else None

    last_ids: dict[str, int] = {}
    poll_interval = 2.0

    try:
        while True:
            if chat_set:
                target_chats = list(chat_set)
            else:
                try:
                    result = ipc_request("GET", f"/chat/list?account={acct}&limit=50")
                    target_chats = [str(c["id"]) for c in result.get("chats", [])[:20]]
                except Exception:
                    target_chats = []

            for chat_ref in target_chats:
                if "new_message" not in event_types:
                    continue
                try:
                    offset_id = last_ids.get(chat_ref, 0)
                    params = f"chat={chat_ref}&limit=10&account={acct}"
                    if offset_id:
                        params += f"&min_id={offset_id}"
                    result = ipc_request("GET", f"/message/list?{params}")
                    msgs = result.get("messages", [])
                    for msg in reversed(msgs):
                        msg_id = msg.get("id", 0)
                        if msg_id <= last_ids.get(chat_ref, 0):
                            continue
                        event = {
                            "event_type": "new_message",
                            "chat_id": chat_ref,
                            "data": msg,
                        }
                        json.dump(event, sys.stdout, default=str, ensure_ascii=False)
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        last_ids[chat_ref] = max(last_ids.get(chat_ref, 0), msg_id)
                except Exception:
                    pass

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        pass
