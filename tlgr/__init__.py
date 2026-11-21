"""tlgr — Full Telegram account control CLI."""

from __future__ import annotations

from typing import Any

__version__ = "2.0.1"

__all__ = ["Client", "__version__"]


def __getattr__(name: str) -> Any:
    """`from tlgr import Client`, without importing it for every `import tlgr`.

    The CLI, the daemon and `tlgr.version` all import this package; resolving
    `Client` on first use keeps the transport out of their startup.
    """
    if name == "Client":
        from tlgr.client import Client

        return Client
    raise AttributeError(f"module 'tlgr' has no attribute {name!r}")
