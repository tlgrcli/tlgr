"""Webhook delivery, against a real local HTTP server (§6.5, §12.3 item 13).

The payload under test is a real one: a `message_new` carrying a `Message`
model built from a Telethon object with a `datetime` and media on it. That is
exactly what v1 could not serialise — `json.dumps(..., default=str)` over a
`to_dict()` either mangled the datetime or raised on the bytes, and the raise
was counted as a *delivery* failure, retried three times, and dead-lettered.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from aiohttp import web
from fake_telethon import make_message

from tlgr.core.config import WebhookConfig, WebhookRetryConfig
from tlgr.daemon.webhook import WebhookPusher, sign_body
from tlgr.models.event import EventEnvelope

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def receiver():
    """A local aiohttp server that records what it was sent."""
    received: list[dict] = []
    status = {"code": 200}

    async def handle(request: web.Request) -> web.Response:
        body = await request.read()
        received.append({"body": body, "headers": dict(request.headers)})
        return web.Response(status=status["code"])

    app = web.Application()
    app.router.add_post("/hook", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield {
            "url": f"http://127.0.0.1:{port}/hook",
            "received": received,
            "status": status,
        }
    finally:
        await runner.cleanup()


def _message_event() -> EventEnvelope:
    from tlgr.models.base import to_builtins
    from tlgr.ops._serialize import message_to_model

    message = make_message(42, chat_id=-1000000000123, text="سلام #1", sender_id=5)
    payload = to_builtins(message_to_model(message, chat_id=-1000000000123))
    return EventEnvelope(
        seq=91824,
        ts="2026-09-02T09:14:07Z",
        account="work",
        type="message_new",
        payload=payload,
        chat_id=-1000000000123,
        sender_id=5,
    )


async def _drain(pusher: WebhookPusher, received: list, expected: int = 1) -> None:
    for _ in range(200):
        if len(received) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"only {len(received)} of {expected} deliveries arrived")


class TestDelivery:
    async def test_a_real_payload_is_delivered_and_signed(self, receiver, tlgr_home: Path):
        config = WebhookConfig(
            enabled=True, url=receiver["url"], secret="topsecret", events=["message_new"]
        )
        pusher = WebhookPusher(config, tlgr_home)
        await pusher.start()
        try:
            await pusher.on_event(_message_event())
            await _drain(pusher, receiver["received"])
        finally:
            await pusher.stop(drain=0.1)

        delivery = receiver["received"][0]
        body = delivery["body"]
        headers = delivery["headers"]

        expected = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
        assert headers["X-Tlgr-Signature"] == expected
        assert headers["X-Tlgr-Seq"] == "91824"
        assert headers["X-Tlgr-Event"] == "message_new"
        assert headers["X-Tlgr-Delivery"]

        decoded = json.loads(body)
        assert decoded["event"]["payload"]["text"] == "سلام #1"
        assert decoded["event"]["payload"]["date"].endswith("Z")
        assert decoded["delivery_id"] == headers["X-Tlgr-Delivery"]

    async def test_the_signature_is_over_the_exact_bytes(self):
        """Re-encoding the dict to verify would make an honest signature fail."""
        body = b'{"a": 1}'
        assert sign_body("k", body) != sign_body("k", b'{"a":1}')

    async def test_an_event_type_that_was_not_subscribed_is_not_sent(
        self, receiver, tlgr_home: Path
    ):
        config = WebhookConfig(enabled=True, url=receiver["url"], events=["message_edited"])
        pusher = WebhookPusher(config, tlgr_home)
        await pusher.start()
        try:
            await pusher.on_event(_message_event())
            await asyncio.sleep(0.05)
        finally:
            await pusher.stop(drain=0.1)
        assert receiver["received"] == []


class TestFailure:
    async def test_an_exhausted_delivery_is_dead_lettered_at_0600(self, receiver, tlgr_home: Path):
        receiver["status"]["code"] = 500
        config = WebhookConfig(
            enabled=True,
            url=receiver["url"],
            events=["message_new"],
            retry=WebhookRetryConfig(enabled=True, max_attempts=2, backoff_base=1),
        )
        pusher = WebhookPusher(config, tlgr_home)
        await pusher.start()
        try:
            await pusher.on_event(_message_event())
            for _ in range(300):
                if pusher.dead_letters:
                    break
                await asyncio.sleep(0.01)
        finally:
            await pusher.stop(drain=0.1)

        path = tlgr_home / "dead_letter.jsonl"
        assert path.exists(), "a failed delivery vanished"
        assert path.stat().st_mode & 0o777 == 0o600
        entries = pusher.read_dead_letters()
        assert entries[0]["event"] == "message_new"
        assert entries[0]["seq"] == "91824"
        assert "HTTP 500" in entries[0]["reason"]

    async def test_an_unencodable_payload_is_a_logged_bug_not_a_retry(
        self, tlgr_home: Path, caplog
    ):
        """COR-07: a serialisation defect must not look like a flaky endpoint."""
        import logging

        config = WebhookConfig(enabled=True, url="http://127.0.0.1:1/hook", events=["x"])
        pusher = WebhookPusher(config, tlgr_home)
        event = EventEnvelope(seq=1, ts="", account="work", type="x", payload={"blob": object()})
        with caplog.at_level(logging.ERROR):
            pusher.enqueue(event)
        assert any("BUG" in record.message for record in caplog.records)
        assert pusher.dead_letters == 0, "a bug was recorded as a delivery failure"

    async def test_a_full_queue_dead_letters_rather_than_blocking(self, tlgr_home: Path):
        """ROB-02: the bus must never wait for the webhook."""
        config = WebhookConfig(
            enabled=True, url="http://127.0.0.1:1/hook", events=["message_new"], queue_size=16
        )
        pusher = WebhookPusher(config, tlgr_home)
        pusher._queue = asyncio.Queue(maxsize=1)
        pusher._queue.put_nowait((b"{}", {}))
        pusher.enqueue(_message_event())
        assert pusher.dropped == 1
        assert pusher.dead_letters == 1

    async def test_dead_letters_rotate_and_are_capped(self, tlgr_home: Path):
        from tlgr.daemon import webhook as webhook_module

        config = WebhookConfig(enabled=True, url="http://127.0.0.1:1/x")
        pusher = WebhookPusher(config, tlgr_home)
        original = webhook_module._DEAD_LETTER_MAX_BYTES
        webhook_module._DEAD_LETTER_MAX_BYTES = 512
        try:
            for index in range(40):
                pusher._dead_letter(b'{"padding": "' + b"x" * 100 + b'"}', {}, f"n{index}")
        finally:
            webhook_module._DEAD_LETTER_MAX_BYTES = original
        assert (tlgr_home / "dead_letter.jsonl.1").exists()
        assert (tlgr_home / "dead_letter.jsonl").stat().st_size < 4096

    async def test_purging_empties_the_file(self, tlgr_home: Path):
        config = WebhookConfig(enabled=True, url="http://127.0.0.1:1/x")
        pusher = WebhookPusher(config, tlgr_home)
        pusher._dead_letter(b"{}", {}, "because")
        assert pusher.purge_dead_letters() == 1
        assert pusher.read_dead_letters() == []


class TestWiring:
    async def test_the_daemon_subscribes_the_webhook_to_the_bus(
        self, receiver, tlgr_home: Path, stub_account, world
    ):
        """The pusher is a bus handler, not a Telethon handler (ROB-02)."""
        from fake_telethon import fake_client_factory

        from tlgr.core.config import save_webhook_config
        from tlgr.daemon.app import Daemon

        save_webhook_config(
            WebhookConfig(enabled=True, url=receiver["url"], secret="k", events=["message_new"]),
            tlgr_home,
        )
        daemon = Daemon(tlgr_home, client_factory=fake_client_factory(world))
        await daemon.start_services()
        try:
            daemon.bus.emit("work", "message_new", {"id": 1}, chat_id=-100)
            await _drain(daemon.webhook, receiver["received"])
        finally:
            await daemon.shutdown(drain=0.1)
        assert json.loads(receiver["received"][0]["body"])["event"]["payload"]["id"] == 1

    async def test_an_enabled_webhook_disables_idle_stop(self, tlgr_home: Path, stub_account):
        """COR-08: a daemon that exits has silently unsubscribed."""
        from tlgr.core.config import save_webhook_config
        from tlgr.daemon.app import Daemon

        save_webhook_config(WebhookConfig(enabled=True, url="http://127.0.0.1:1/x"), tlgr_home)
        daemon = Daemon(tlgr_home)
        assert daemon.idle_timeout == 0
        assert daemon.activity.busy is True

    async def test_shutdown_dead_letters_what_it_cannot_flush(self, tlgr_home: Path):
        config = WebhookConfig(enabled=True, url="http://127.0.0.1:1/x", events=["x"])
        pusher = WebhookPusher(config, tlgr_home)
        pusher._queue = asyncio.Queue(maxsize=4)
        pusher._queue.put_nowait((b"{}", {"X-Tlgr-Event": "x"}))
        await pusher.stop(drain=0.05)
        assert pusher.dead_letters == 1
        assert "shutting down" in pusher.read_dead_letters()[0]["reason"]
