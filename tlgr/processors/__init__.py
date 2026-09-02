"""Registry-based text processors (formerly "transforms").

Every processor is a plain function registered via ``@register_processor``.
Processors take ``(text, config)`` and return the modified text.

Use :class:`ProcessorChain` to run multiple processors in sequence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

ProcessorFunc = Callable[[str, dict[str, Any]], str]

_REGISTRY: dict[str, ProcessorFunc] = {}


def register_processor(name: str):
    """Decorator that registers a processor function under *name*."""

    def decorator(func: ProcessorFunc) -> ProcessorFunc:
        _REGISTRY[name] = func
        return func

    return decorator


def get_processor(name: str) -> ProcessorFunc | None:
    return _REGISTRY.get(name)


def list_processors() -> list[str]:
    return list(_REGISTRY.keys())


class ProcessorChain:
    """Ordered pipeline of processors applied to text."""

    def __init__(self) -> None:
        self.processors: list[tuple[ProcessorFunc, dict[str, Any]]] = []

    def add(self, name: str, config: dict[str, Any] | None = None) -> ProcessorChain:
        func = get_processor(name)
        if func is None:
            raise ValueError(f"Unknown processor: {name}")
        self.processors.append((func, config or {}))
        return self

    def add_inline(self, pattern: str, replacement: str = "", flags: str = "") -> ProcessorChain:
        func = get_processor("regex_replace")
        assert func is not None
        self.processors.append(
            (func, {"pattern": pattern, "replacement": replacement, "flags": flags})
        )
        return self

    def apply(self, text: str) -> str:
        result = text
        for func, config in self.processors:
            result = func(result, config)
        return result

    def __len__(self) -> int:
        return len(self.processors)


def create_chain_from_spec(spec: str) -> ProcessorChain:
    """Create from spec string like ``'replace_mentions,strip_formatting'``."""
    chain = ProcessorChain()
    if not spec:
        return chain
    parts = re.split(r"(?<!\\),", spec)
    for part in parts:
        part = part.strip().replace("\\,", ",")
        if not part:
            continue
        if ":" in part:
            segments = part.split(":")
            name = segments[0]
            config: dict[str, Any] = {}
            for segment in segments[1:]:
                if "=" in segment:
                    key, value = segment.split("=", 1)
                    config[key] = value
            chain.add(name, config)
        else:
            chain.add(part)
    return chain


def create_chain_from_list(items: list[str | dict]) -> ProcessorChain:
    """Build a chain from a YAML config list.

    Items can be:
    - ``"name"`` — plain processor name
    - ``"name:key=val"`` — name with inline config
    - ``{"type": "regex", "pattern": ..., "replacement": ...}`` — inline regex
    """
    chain = ProcessorChain()
    for item in items:
        if isinstance(item, str):
            if ":" in item:
                segments = item.split(":")
                name = segments[0]
                config: dict[str, Any] = {}
                for segment in segments[1:]:
                    if "=" in segment:
                        k, v = segment.split("=", 1)
                        config[k] = v
                chain.add(name, config)
            else:
                chain.add(item)
        elif isinstance(item, dict):
            chain.add_inline(
                item.get("pattern", ""),
                item.get("replacement", ""),
                item.get("flags", ""),
            )
    return chain


# Import built-in processor modules so they self-register.
from tlgr.processors import regex, text  # noqa: E402, F401
