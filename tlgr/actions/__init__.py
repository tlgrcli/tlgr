"""Registry-based actions for the Gateway pipeline.

Every action is an async function registered via ``@register_action``.
Actions receive an :class:`~tlgr.gateway.event.Event`, the action's config
from YAML, a :class:`~tlgr.jobs.client.JobClient`, and an optional
:class:`~tlgr.processors.ProcessorChain`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tlgr.gateway.event import Event
from tlgr.jobs.client import JobClient
from tlgr.processors import ProcessorChain

ActionFunc = Callable[
    [Event, Any, JobClient, ProcessorChain | None],
    Awaitable[None],
]

_REGISTRY: dict[str, ActionFunc] = {}


def register_action(name: str):
    """Decorator that registers an action function under *name*."""

    def decorator(func: ActionFunc) -> ActionFunc:
        _REGISTRY[name] = func
        return func

    return decorator


def get_action(name: str) -> ActionFunc | None:
    return _REGISTRY.get(name)


def list_actions() -> list[str]:
    return list(_REGISTRY.keys())


# Import built-in action modules so they self-register.
from tlgr.actions import forward, reply  # noqa: E402, F401
