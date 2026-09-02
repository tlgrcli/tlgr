"""`python -m tlgr.daemon.server`, kept working as an alias for `main`.

The daemon moved to `daemon/main.py` (entry) and `daemon/app.py` (the object),
but this module path is baked into v1's launchd plist and into anyone's shell
history, so it stays as a two-line forwarder. `DaemonServer` is the new
`Daemon` under its old name, which keeps the v1 status contract — and the
regression test that pins it — pointing at the same code.

Deleted at PR-12 with the rest of the v1 surface.
"""

from __future__ import annotations

from tlgr.daemon.app import Daemon
from tlgr.daemon.main import main

#: v1's name for the daemon object.
DaemonServer = Daemon

__all__ = ["Daemon", "DaemonServer", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
