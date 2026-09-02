"""Who is on the other end of the socket (SEC-01).

The socket is 0600, which is the real control. This is defence in depth over
it, for two reasons: a umask can be changed by anything in the process tree
that started the daemon, and a refusal here produces an auditable log line
naming the peer's pid, which "connection refused by file permissions" does
not.

The two platforms disagree about how to ask:

* **Linux** — `getsockopt(SOL_SOCKET, SO_PEERCRED)` returns `struct ucred`,
  three native ints: pid, uid, gid.
* **macOS/BSD** — `getsockopt(SOL_LOCAL, LOCAL_PEERCRED)` returns
  `struct xucred`: version (u32), uid (u32), ngroups (short), groups[16].
  There is no pid in it. `SOL_LOCAL` is 0 and `LOCAL_PEERCRED` is 1; the
  layout was verified on Darwin 25.6.

Where neither works, `peer_of()` returns `None` and the caller falls back to
the shared token (§8.2) — which is why `require_token` exists as a config key
rather than a hard-coded platform test.
"""

from __future__ import annotations

import hmac
import os
import socket
import struct
import sys
from dataclasses import dataclass

__all__ = ["Peer", "peer_of", "token_matches"]

_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1


@dataclass(frozen=True)
class Peer:
    uid: int
    pid: int | None = None
    gid: int | None = None


def peer_of(sock: socket.socket | None) -> Peer | None:
    """The credentials of the process on the other end, or None if unknown."""
    if sock is None:
        return None
    try:
        if sys.platform.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", raw)
            return Peer(uid=uid, pid=pid, gid=gid)
        if sys.platform == "darwin" or "bsd" in sys.platform:
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 4 + 4 + 2 + 4 * 16)
            # cr_version, cr_uid — the rest of xucred is groups we do not need.
            _version, uid = struct.unpack_from("=II", raw, 0)
            return Peer(uid=uid)
    except (OSError, struct.error):
        return None
    return None


def token_matches(supplied: str | None, expected: str | None) -> bool:
    """Constant-time comparison; both sides absent is *not* a match."""
    if not expected:
        return False
    if not supplied:
        return False
    return hmac.compare_digest(supplied.strip(), expected.strip())


def current_uid() -> int:
    return os.getuid()
