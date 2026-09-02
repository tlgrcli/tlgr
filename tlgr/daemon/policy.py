"""The operation allowlist, enforced where it cannot be bypassed (SEC-04).

Three properties, each of which v1 lacked:

* **matched by canonical id.** `--enable-commands message.send` also permits
  the `send` alias and the legacy `message send` path, because every name is
  canonicalised through the registry before it is compared. v1 matched the
  literal command path, so an allowlist written against one spelling let the
  other through — or, more often, blocked a command the operator thought they
  had allowed.
* **enforced in the daemon.** The CLI checks too, for a fast, local error, but
  the CLI flag is under the agent's control. The daemon's copy is the one with
  teeth, and it comes from `config.toml` or a token-bound policy file.
* **`deny` beats `allow`,** and behaviour classes (`*:mutating`,
  `*:destructive`, `*:read`) can be denied wholesale, so "this agent may read
  anything and change nothing" is one entry instead of five hundred.

This is a usability guard, not a sandbox: anything that can open the socket can
also read the session file. §8.3 says so, and `SECURITY.md` repeats it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tlgr.core.errors import PermissionError_

if TYPE_CHECKING:  # pragma: no cover
    from tlgr.ops._spec import OperationSpec

__all__ = ["Policy", "PolicyDecision"]

#: Behaviour classes usable in place of an id.
_CLASSES = {"read", "mutating", "destructive", "stream", "local"}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


@dataclass
class Policy:
    """An allow/deny pair over canonical operation ids."""

    allow: list[str] = field(default_factory=lambda: ["*"])
    deny: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, allow: list[str] | None, deny: list[str] | None) -> Policy:
        return cls(allow=list(allow or ["*"]), deny=list(deny or []))

    @classmethod
    def parse(cls, spec: str) -> Policy:
        """Parse a `--enable-commands`-style string into an allowlist."""
        entries = [e.strip() for e in spec.replace(" ", ",").split(",") if e.strip()]
        return cls(allow=entries or ["*"])

    # -- matching ----------------------------------------------------------

    def _canonical(self, name: str) -> str:
        from tlgr.registry import ALIASES

        key = name.strip().lower().replace(" ", ".")
        return ALIASES.get(key, key)

    def _classes_of(self, spec: OperationSpec | None) -> set[str]:
        if spec is None:
            return set()
        classes = {"stream"} if spec.stream else set()
        if spec.destructive:
            classes |= {"mutating", "destructive"}
        elif spec.mutating:
            classes.add("mutating")
        else:
            classes.add("read")
        if spec.surface.value == "local":
            classes.add("local")
        return classes

    def _matches(self, entry: str, op_id: str, classes: set[str]) -> bool:
        entry = entry.strip().lower()
        if not entry:
            return False
        if entry in ("*", "all"):
            return True
        if entry.startswith("*:"):
            return entry[2:] in classes and entry[2:] in _CLASSES
        if entry.endswith(".*"):
            return op_id.startswith(entry[:-1])
        resolved = self._canonical(entry)
        return resolved == op_id or op_id.startswith(f"{resolved}.")

    def check(self, op_id: str, spec: OperationSpec | None = None) -> PolicyDecision:
        """Is *op_id* permitted? `deny` wins over `allow`, always."""
        canonical = self._canonical(op_id)
        classes = self._classes_of(spec)
        for entry in self.deny:
            if self._matches(entry, canonical, classes):
                return PolicyDecision(False, f"denied by policy entry {entry!r}")
        for entry in self.allow:
            if self._matches(entry, canonical, classes):
                return PolicyDecision(True)
        return PolicyDecision(False, "not in the policy allowlist")

    def enforce(self, op_id: str, spec: OperationSpec | None = None) -> None:
        decision = self.check(op_id, spec)
        if not decision.allowed:
            raise PermissionError_(
                f"operation {op_id!r} is not enabled: {decision.reason}",
            )
