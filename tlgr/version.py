"""Version and protocol constants — the numbers the handshake compares.

`VERSION` is the human-facing release. `PROTOCOL` is the wire contract of
§5: it changes when a field's meaning changes, not when a version does, and
it is what the CLI and the daemon actually compare. `MIN_DAEMON_PROTOCOL` is
the oldest daemon this CLI will talk to rather than restart.
"""

from __future__ import annotations

from tlgr import __version__

VERSION = __version__
PROTOCOL = 2
MIN_DAEMON_PROTOCOL = 2

#: The header names the two ends agree on (§5.1).
HEADER_CLIENT = "X-Tlgr-Client"
HEADER_PROTOCOL = "X-Tlgr-Protocol"
HEADER_REQUEST_ID = "X-Request-Id"
HEADER_TOKEN = "X-Tlgr-Token"

__all__ = [
    "HEADER_CLIENT",
    "HEADER_PROTOCOL",
    "HEADER_REQUEST_ID",
    "HEADER_TOKEN",
    "MIN_DAEMON_PROTOCOL",
    "PROTOCOL",
    "VERSION",
]
