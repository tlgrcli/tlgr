"""Every operation module is discovered here, and the registry is linted.

Importing this package is what populates `tlgr.registry.REGISTRY`. The modules
are found by walking the package directory rather than named in a list: the
list was a second place to forget a group, and every group PR that added one
collided with every other PR on the same three lines. Discovery is
deterministic — the names are sorted, so the registry is built in the same
order in every checkout — and `_`-prefixed modules are skipped because they
hold the plumbing (`_spec`, `_send`, …), not specs.

The lint runs at the end, so a malformed spec fails the import rather than
surfacing later as a missing command or a broken doc.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from tlgr.ops._spec import OpContext, OperationSpec, PageKind, Surface
from tlgr.registry import REGISTRY, canonical, get, lint_or_raise, register

__all__ = [
    "REGISTRY",
    "OpContext",
    "OperationSpec",
    "PageKind",
    "Surface",
    "canonical",
    "get",
    "op_module_names",
    "register",
]


def op_module_names() -> tuple[str, ...]:
    """The operation modules in this package, in registration order.

    Sorted, so two checkouts register the same specs in the same order and the
    generated docs do not depend on a directory's own ordering.
    """
    return tuple(
        sorted(
            info.name
            for info in pkgutil.iter_modules(__path__)
            if not info.name.startswith("_") and not info.ispkg
        )
    )


def _op_modules() -> tuple[ModuleType, ...]:
    return tuple(importlib.import_module(f"{__name__}.{name}") for name in op_module_names())


for _module in _op_modules():
    for _name in dir(_module):
        if _name.startswith("SPEC_"):
            _spec = getattr(_module, _name)
            if isinstance(_spec, OperationSpec) and _spec.id not in REGISTRY:
                register(_spec)

lint_or_raise()
