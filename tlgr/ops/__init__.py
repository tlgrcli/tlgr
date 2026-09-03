"""Every operation module is imported here, and the registry is linted.

Importing this package is what populates `tlgr.registry.REGISTRY`. The lint
runs at the end, so a malformed spec fails the import rather than surfacing
later as a missing command or a broken doc.
"""

from __future__ import annotations

from tlgr.ops import agent, chat, draft, folder, message
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
    "register",
]

_MODULES = (agent, chat, draft, folder, message)

for _module in _MODULES:
    for _name in dir(_module):
        if _name.startswith("SPEC_"):
            _spec = getattr(_module, _name)
            if isinstance(_spec, OperationSpec) and _spec.id not in REGISTRY:
                register(_spec)

lint_or_raise()
