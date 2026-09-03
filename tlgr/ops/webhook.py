"""The `webhook` group: where events go when nothing is watching.

The delivery guarantees live in `daemon/webhook.py`; this is the surface that
configures them and proves one works. Two things it makes explicit that v1
left implicit:

* **the signature, not a bearer token.** v1 had neither, so any process that
  learned the URL could forge events (SEC-08). A delivery now carries an
  HMAC over the exact bytes on the wire, and `webhook test` prints the headers
  it sent so a receiver can be verified end to end rather than by guesswork.
* **the idempotency key.** Every delivery carries the envelope's `seq` and a
  delivery id, and a re-drive reuses them — which is what makes a catch-up
  replay safe to reprocess instead of a duplicate nobody can detect.
"""

from __future__ import annotations

import contextlib
import time
from typing import Annotated, Any

from tlgr.core import eventtypes
from tlgr.core.errors import UsageError
from tlgr.models.base import Request
from tlgr.models.daemon import WebhookProbe, WebhookSettings
from tlgr.models.event import EventEnvelope
from tlgr.models.peer import PeerRef
from tlgr.ops._params import choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_REDACTED = "<redacted>"


def _config() -> Any:
    from tlgr.core.config import load_webhook_config
    from tlgr.core.paths import default_base

    return load_webhook_config(default_base())


def _settings(config: Any, *, reveal: bool) -> WebhookSettings:
    return WebhookSettings(
        enabled=bool(config.enabled),
        url=str(config.url or ""),
        events=list(config.events or []),
        filters={"chats": list(config.filters.chats or []), **(config.filters.raw or {})},
        sign="hmac-sha256" if config.signing_key else ("bearer" if config.token else "none"),
        secret=(config.secret or None) if reveal else (_REDACTED if config.secret else None),
        token=(config.token or None) if reveal else (_REDACTED if config.token else None),
        max_attempts=int(config.retry.max_attempts),
        backoff=int(config.retry.backoff_base),
        timeout=int(config.timeout),
        queue=int(config.queue_size),
    )


# ---------------------------------------------------------------------------
# webhook get
# ---------------------------------------------------------------------------


class WebhookGetReq(Request):
    show_secret: Annotated[
        bool, opt("--show-secret", help="Reveal the HMAC secret and bearer token.")
    ] = False


async def webhook_get(ctx: OpContext, req: WebhookGetReq) -> WebhookSettings:
    """The webhook configuration and its delivery health.

    Secrets are redacted unless asked for. `webhook get` is a command people
    paste into issues, and a signing key in a bug report is a signing key on
    the internet.
    """
    settings = _settings(_config(), reveal=req.show_secret)
    if req.show_secret:
        ctx.warn("this output contains the signing secret; treat it as a credential")

    status = _probe()
    if status is None:
        return settings
    live = status.get("webhook") or {}
    settings.delivered = int(live.get("delivered", 0) or 0)
    settings.failed = int(live.get("failed", 0) or 0)
    settings.dead_letters = int(live.get("dead_letters", 0) or 0)
    settings.queue_depth = int(live.get("queued", 0) or 0)
    return settings


def _probe() -> dict[str, Any] | None:
    from tlgr.core.paths import default_base
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(default_base(), timeout=2.0, auto_start=False, no_restart=True)
    with contextlib.suppress(Exception):
        return client.probe_status()
    return None


SPEC_WEBHOOK_GET = OperationSpec(
    id="webhook.get",
    request=WebhookGetReq,
    response=WebhookSettings,
    impl=webhook_get,
    summary="Show the webhook configuration and delivery health",
    legacy_paths=("config webhook",),
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    columns=("enabled", "url", "sign", "delivered", "failed", "dead_letters"),
    example={
        "enabled": True,
        "url": "https://example.invalid/hook",
        "sign": "hmac-sha256",
        "events": ["message_new"],
    },
    example_args="webhook get",
    covers_partial=("updates.stream-webhook-delivery",),
    coverage_note="reports the configuration; delivering is the pusher's job.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# webhook set
# ---------------------------------------------------------------------------


class WebhookSetReq(Request):
    url: Annotated[str | None, opt("--url", metavar="URL", help="Destination URL.")] = None
    enabled: Annotated[
        bool | None, opt("--enabled/--disabled", help="Turn delivery on or off.")
    ] = None
    events: Annotated[str | None, opt("--events", metavar="TYPES", help="Event types to push.")] = (
        None
    )
    chat: Annotated[
        list[PeerRef],
        opt("--chat", metavar="CHAT", kind="peer", help="Only push events about these chats."),
    ] = []
    secret: Annotated[
        str | None,
        opt(
            "--secret",
            secret=True,
            envvar="TLGR_WEBHOOK_SECRET",
            help="HMAC-SHA256 signing secret.",
        ),
    ] = None
    token: Annotated[
        str | None,
        opt(
            "--token",
            secret=True,
            envvar="TLGR_WEBHOOK_TOKEN",
            help="Bearer token (legacy; prefer the HMAC signature).",
        ),
    ] = None
    sign: Annotated[
        str | None, choice("hmac-sha256", "bearer", "none", help="Signature scheme.")
    ] = None
    max_attempts: Annotated[
        int | None, opt("--max-attempts", metavar="N", help="Attempts before dead-lettering.")
    ] = None
    backoff: Annotated[
        int | None, opt("--backoff", metavar="SECONDS", help="Base of the exponential backoff.")
    ] = None
    request_timeout: Annotated[
        int | None, opt("--request-timeout", metavar="SECONDS", help="Per-request timeout.")
    ] = None
    queue: Annotated[
        int | None, opt("--queue", metavar="N", help="Bounded queue depth before the lag policy.")
    ] = None


async def webhook_set(ctx: OpContext, req: WebhookSetReq) -> WebhookSettings:
    """Configure the outbound webhook.

    Every event name is validated against the taxonomy: `jobs.yaml` and
    `webhook.toml` used to drop a name they did not recognise, so a typo
    produced a webhook that delivered nothing and never said why.
    """
    from tlgr.core.config import load_webhook_config, save_webhook_config
    from tlgr.core.paths import default_base

    base = default_base()
    config = load_webhook_config(base)

    if req.url is not None:
        config.url = req.url
        if req.url.startswith("http://") and not _is_loopback(req.url):
            ctx.warn(
                "a plain http:// endpoint sends event payloads and the signature in "
                "clear text over the network"
            )
    if req.enabled is not None:
        config.enabled = req.enabled
    if req.events is not None:
        wanted = eventtypes.resolve_selectors(req.events)
        config.events = sorted(wanted)
    if req.chat:
        config.filters.chats = [ref.raw for ref in req.chat]
    if req.secret is not None:
        config.secret = req.secret
    if req.token is not None:
        config.token = req.token
    if req.max_attempts is not None:
        config.retry.max_attempts = req.max_attempts
        config.retry.enabled = req.max_attempts > 1
    if req.backoff is not None:
        config.retry.backoff_base = req.backoff
    if req.request_timeout is not None:
        config.timeout = req.request_timeout
    if req.queue is not None:
        config.queue_size = req.queue
    if req.sign == "none":
        config.secret = ""
        config.token = ""
    elif req.sign == "bearer" and not config.token:
        raise UsageError(
            "--sign bearer needs a token: --token-env, --token-stdin or --token-file",
            field="token",
        )

    if config.enabled and not config.url:
        raise UsageError("a webhook cannot be enabled without a --url", field="url")
    if config.enabled and not config.signing_key:
        ctx.warn(
            "no signing secret is set: any process that learns the URL can forge "
            "events. Set one with --secret-env."
        )

    save_webhook_config(config, base)
    _reload()
    return _settings(config, reveal=False)


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _reload() -> None:
    from tlgr.core.paths import default_base
    from tlgr.transport.client import DaemonClient

    client = DaemonClient(default_base(), timeout=10.0, auto_start=False, no_restart=True)
    with contextlib.suppress(Exception):
        client.admin("reload", {"what": ["config"]})


SPEC_WEBHOOK_SET = OperationSpec(
    id="webhook.set",
    request=WebhookSetReq,
    response=WebhookSettings,
    impl=webhook_set,
    summary="Configure the outbound webhook",
    description=(
        "Signature: `X-Tlgr-Signature: sha256=<hmac of the exact body>`, plus "
        "`X-Tlgr-Event`, `X-Tlgr-Seq`, `X-Tlgr-Account` and `X-Tlgr-Delivery`. "
        "The delivery id is what makes a catch-up replay safe to reprocess. "
        "Secrets are read from an environment variable, a file or stdin."
    ),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=30,
    columns=("enabled", "url", "sign", "events"),
    example={"enabled": True, "url": "https://example.invalid/hook", "sign": "hmac-sha256"},
    example_args="webhook set --url https://example.invalid/hook --events message_new",
    covers=("updates.sync-duplicate-suppression",),
    covers_partial=("updates.stream-event-filtering", "updates.stream-webhook-delivery"),
    coverage_note="configures delivery; the queue and retries are the pusher's.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# webhook test
# ---------------------------------------------------------------------------


class WebhookTestReq(Request):
    event: Annotated[str, opt("--event", metavar="TYPE", help="Event type to synthesise.")] = (
        "message_new"
    )
    seq: Annotated[
        int | None, opt("--seq", metavar="N", help="Replay a real buffered event instead.")
    ] = None
    url: Annotated[
        str | None, opt("--url", metavar="URL", help="Override the configured URL for this test.")
    ] = None
    retry: Annotated[
        bool, opt("--retry", help="Use the configured retry policy instead of one attempt.")
    ] = False


async def webhook_test(ctx: OpContext, req: WebhookTestReq) -> WebhookProbe:
    """Send one delivery and report exactly what was sent.

    The headers — signature included — are in the response, so a receiver can
    be verified against the real bytes rather than against somebody's reading
    of the documentation. One attempt by default: a test that silently
    retried three times would hide the failure it exists to show.
    """
    from tlgr.core.signing import sign_body

    daemon = getattr(ctx, "daemon", None)
    if daemon is None:
        raise UsageError("this operation runs inside the daemon")
    pusher = daemon.webhook
    config = pusher.config
    target = req.url or config.url
    if not target:
        raise UsageError("no webhook URL is configured; run `tlgr webhook set --url …`")

    envelope = _sample(ctx, req)
    import msgspec

    delivery_id = f"test-{int(time.time())}"
    body = msgspec.json.encode({"event": envelope, "delivery_id": delivery_id})
    headers = {
        "Content-Type": "application/json",
        "X-Tlgr-Delivery": delivery_id,
        "X-Tlgr-Seq": str(envelope.seq),
        "X-Tlgr-Event": envelope.type,
        "X-Tlgr-Account": envelope.account,
        "X-Tlgr-Test": "1",
    }
    if config.signing_key:
        headers["X-Tlgr-Signature"] = sign_body(config.signing_key, body)
    if config.token:
        headers["Authorization"] = "Bearer <redacted>"

    started = time.monotonic()
    ok, error = await pusher.deliver_once(
        {
            "body": body.decode("utf-8"),
            "delivery_id": delivery_id,
            "seq": envelope.seq,
            "event": envelope.type,
            "account": envelope.account,
        },
        url=target,
    )
    probe = WebhookProbe(
        url=target,
        latency_ms=int((time.monotonic() - started) * 1000),
        request_headers=headers,
        body=body.decode("utf-8"),
        error=None if ok else error,
    )
    if ok:
        probe.status = 200
    elif error.startswith("HTTP "):
        with contextlib.suppress(ValueError):
            probe.status = int(error.split(" ", 1)[1])
    return probe


def _sample(ctx: OpContext, req: WebhookTestReq) -> EventEnvelope:
    """A real buffered event when asked for one, else a synthetic envelope."""
    bus = getattr(ctx, "bus", None)
    if req.seq is not None and bus is not None:
        events, _gap = bus.replay(ctx.account, req.seq - 1)
        for event in events:
            if int(event.seq) == req.seq:
                found: EventEnvelope = event
                return found
        raise UsageError(
            f"seq {req.seq} is not in the buffer; `tlgr events replay` shows what is",
            field="seq",
        )
    eventtypes.resolve_selectors(req.event, allow_all=False)
    from datetime import datetime, timezone

    return EventEnvelope(
        seq=0,
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        account=ctx.account,
        type=req.event,
        payload={"test": True},
    )


SPEC_WEBHOOK_TEST = OperationSpec(
    id="webhook.test",
    request=WebhookTestReq,
    response=WebhookProbe,
    impl=webhook_test,
    summary="Send a test delivery to the configured URL",
    description=(
        "Prints the exact headers, signature included, so a receiver can be "
        "verified end to end. One attempt by default: a test that retried "
        "would hide the failure it exists to show."
    ),
    mutating=True,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=60,
    columns=("url", "status", "latency_ms", "error"),
    example={"url": "https://example.invalid/hook", "status": 200, "latency_ms": 41},
    example_args="webhook test --event message_new",
    covers=("updates.stream-webhook-delivery",),
    tags=frozenset({"agent-safe"}),
)
