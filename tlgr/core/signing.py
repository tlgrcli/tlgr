"""The webhook signature, in one place.

Part of the wire contract rather than of the pusher: `daemon/webhook.py` signs
deliveries with it and `ops/webhook.py` shows a receiver what a signature will
look like, and `ops/` may not import `daemon/` (§2.2).

SEC-08 is what it fixes. v1 sent events with no signature at all, so any
process that learned the URL could forge them; a bearer token would only have
proved the sender knew a string, not that the body was unmodified.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["sign_body", "verify_body"]


def sign_body(secret: str, body: bytes) -> str:
    """`sha256=<hex>` over the exact bytes that go on the wire.

    Over the *bytes*, not over a re-encoded dict: a receiver verifies what it
    received, and any re-encoding — key order, whitespace, escaping — makes an
    honest signature fail.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_body(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time check, for a receiver written in Python.

    Exported because "verify the signature" is advice everybody follows
    slightly differently, and `==` on two hex strings is the difference
    between a check and a timing oracle.
    """
    return hmac.compare_digest(sign_body(secret, body), signature.strip())
