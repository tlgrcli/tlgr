"""Registry-based message and event filtering.

Every filter is a plain function registered via ``@register_filter``.
Filters receive an :class:`~tlgr.gateway.event.Event` and a config value
(whatever was in the YAML) and return ``(matched: bool, reason: str)``.

Modules in this package auto-register their filters on import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tlgr.gateway.event import Event

FilterFunc = Callable[[Event, Any], tuple[bool, str]]

_REGISTRY: dict[str, FilterFunc] = {}


def register_filter(name: str):
    """Decorator that registers a filter function under *name*."""

    def decorator(func: FilterFunc) -> FilterFunc:
        _REGISTRY[name] = func
        return func

    return decorator


def get_filter(name: str) -> FilterFunc | None:
    return _REGISTRY.get(name)


def list_filters() -> list[str]:
    return list(_REGISTRY.keys())


# Import built-in filter modules so they self-register.
from tlgr.filters import content, context, message, temporal, user  # noqa: E402, F401
from tlgr.filters.compose import evaluate, parse_filter_config  # noqa: E402, F401
