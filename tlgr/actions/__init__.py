"""Registry-based actions for the Gateway pipeline.

A built-in action is an `Action` subclass registered with
``@register_action``: it plans payloads when the event arrives and executes
them through the op layer when the scheduler says they are due (see
`actions/base.py` and `gateway/scheduler.py`). Every built-in action is
therefore paced, persisted across restarts, and subject to the same policy,
rate limits and flood budgets as the CLI.

A plain async function registered the same way is still accepted, for
actions written against the old interface: it receives the
:class:`~tlgr.gateway.event.Event`, the action's config, the job client and
the processor chain, and runs at once, outside the scheduler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tlgr.actions.base import Action
from tlgr.gateway.event import Event
from tlgr.jobs.client import JobClient
from tlgr.processors import ProcessorChain

ActionFunc = Callable[
    [Event, Any, JobClient, ProcessorChain | None],
    Awaitable[None],
]

_REGISTRY: dict[str, Action | ActionFunc] = {}


def register_action(name: str) -> Callable[[Any], Any]:
    """Register an `Action` subclass (instantiated once) or a legacy function."""

    def decorator(obj: Any) -> Any:
        if isinstance(obj, type) and issubclass(obj, Action):
            instance = obj()
            instance.name = instance.name or name
            _REGISTRY[name] = instance
        else:
            _REGISTRY[name] = obj
        return obj

    return decorator


def get_action(name: str) -> Action | ActionFunc | None:
    return _REGISTRY.get(name)


def get_builtin(name: str) -> Action | None:
    """The scheduled `Action` registered under *name*, or None."""
    found = _REGISTRY.get(name)
    return found if isinstance(found, Action) else None


def list_actions() -> list[str]:
    return list(_REGISTRY.keys())


# Import built-in action modules so they self-register.
from tlgr.actions import forward, react, read, reply, view  # noqa: E402, F401
