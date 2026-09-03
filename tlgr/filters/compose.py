"""Filter composition engine — AND / OR / NOT trees.

YAML filter blocks are parsed into a tree of :class:`FilterNode` objects
and evaluated recursively against an :class:`~tlgr.gateway.event.Event`.

Top-level keys are AND'd.  ``any_of`` gives OR, ``none_of`` gives NOT.
Both ``any_of`` and ``none_of`` accept a list of filter-sets that can
themselves contain ``any_of`` / ``none_of``, enabling arbitrary nesting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tlgr.gateway.event import Event


class Op(Enum):
    AND = "and"
    OR = "or"
    NOT = "not"
    LEAF = "leaf"


@dataclass
class FilterNode:
    op: Op
    children: list[FilterNode] = field(default_factory=list)
    filter_name: str = ""
    filter_value: Any = None

    def __repr__(self) -> str:
        if self.op is Op.LEAF:
            return f"Leaf({self.filter_name}={self.filter_value!r})"
        return f"{self.op.value.upper()}({self.children})"


_COMPOSITION_KEYS = {"any_of", "none_of"}


def parse_filter_config(raw: dict[str, Any] | None) -> FilterNode | None:
    """Turn a YAML filter block into a :class:`FilterNode` tree."""
    if not raw:
        return None

    and_children: list[FilterNode] = []

    for key, value in raw.items():
        if key == "any_of":
            or_children = _parse_child_list(value)
            if or_children:
                and_children.append(FilterNode(op=Op.OR, children=or_children))
        elif key == "none_of":
            or_children = _parse_child_list(value)
            if or_children:
                inner = FilterNode(op=Op.OR, children=or_children)
                and_children.append(FilterNode(op=Op.NOT, children=[inner]))
        else:
            and_children.append(FilterNode(op=Op.LEAF, filter_name=key, filter_value=value))

    if not and_children:
        return None
    if len(and_children) == 1:
        return and_children[0]
    return FilterNode(op=Op.AND, children=and_children)


def _parse_child_list(items: list[dict[str, Any]] | Any) -> list[FilterNode]:
    """Parse a list of filter-set dicts (used inside ``any_of`` / ``none_of``)."""
    if not isinstance(items, list):
        return []
    nodes: list[FilterNode] = []
    for item in items:
        if isinstance(item, dict):
            child = parse_filter_config(item)
            if child is not None:
                nodes.append(child)
    return nodes


def evaluate(node: FilterNode | None, event: Event) -> tuple[bool, str]:
    """Recursively evaluate *node* against *event*.

    Returns ``(passed, reason)`` just like individual filters.
    """
    if node is None:
        return True, "no filters"

    if node.op is Op.LEAF:
        from tlgr.filters import get_filter

        func = get_filter(node.filter_name)
        if func is None:
            return False, f"unknown filter: {node.filter_name}"
        return func(event, node.filter_value)

    if node.op is Op.AND:
        for child in node.children:
            ok, reason = evaluate(child, event)
            if not ok:
                return False, reason
        return True, "all passed"

    if node.op is Op.OR:
        reasons: list[str] = []
        for child in node.children:
            ok, reason = evaluate(child, event)
            if ok:
                return True, reason
            reasons.append(reason)
        return False, f"none matched: {'; '.join(reasons)}"

    if node.op is Op.NOT:
        ok, reason = evaluate(node.children[0], event)
        if ok:
            return False, f"excluded: {reason}"
        return True, "not-match passed"

    return False, "invalid node"
