"""What this build genuinely cannot do, said once and said the same way.

Telethon 1.44 speaks MTProto layer 227. Two server features tlgr's command
surface names — Communities and the `ephemeral.*` welcome messages — are
layer 229 and have no TL classes to call. The commands are registered anyway
and refuse with `NOT_SUPPORTED` (exit 13 — "tlgr cannot do this", not "the
operation failed", §7.3), because a command that is absent teaches an agent
nothing while a command that refuses with a reason teaches it exactly what is
missing and when it will arrive.

`ARCHITECTURE §6.14` describes the escape hatch (`core/custom_tl.py`) that
would close these: hand-written `TLRequest` subclasses invoked with an
explicit layer. Nothing on the P0/P1 path needs it, so the gap is declared
rather than hand-rolled.
"""

from __future__ import annotations

from tlgr.core.errors import NotSupportedError

__all__ = ["LAYER", "NEEDED_LAYER", "community_gap", "welcome_gap"]

#: The layer the installed Telethon speaks.
LAYER = 227

#: The layer these features arrived in.
NEEDED_LAYER = 229


def _refuse(command: str, methods: str, feature: str) -> None:
    raise NotSupportedError(
        f"{command} needs {methods}, which arrived in MTProto layer {NEEDED_LAYER}; "
        f"Telethon 1.44 speaks layer {LAYER}, so tlgr has no request class to send. "
        f"The {feature} surface is reserved: the command shape is settled and will "
        "start working with the layer uplift, without a flag or a path changing"
    )


def community_gap(command: str, methods: str) -> None:
    """Refuse a `communities.*` command (layer 229)."""
    _refuse(command, methods, "Community")


def welcome_gap(command: str, methods: str) -> None:
    """Refuse an `ephemeral.*` welcome-message command (layer 229)."""
    _refuse(command, methods, "welcome-message")
