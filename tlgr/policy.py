"""The operation allowlist matcher, shared by the CLI and the daemon (SEC-04).

Four properties:

* **matched by canonical id.** `--enable-commands message.send` also permits
  the `send` alias and the legacy `message send` path, because every name is
  canonicalised through the registry before it is compared. v1 matched the
  literal command path, so an allowlist written against one spelling let the
  other through, or, more often, blocked a command the operator thought they
  had allowed.
* **one matcher.** The CLI's `--enable-commands` check (`registry.policy_allows`)
  and the daemon's `[policy]` enforcement both call `Policy.check`, so a class
  entry such as `*:read` means the same thing on both sides of the socket.
* **`deny` beats `allow`,** and behaviour classes (`*:mutating`,
  `*:destructive`, `*:read`, `*:credential`) can be named wholesale, so "this
  agent may read anything and change nothing" is one entry instead of five
  hundred.
* **a dead entry is an error.** `Policy.problems()` checks every entry against
  the registry, and `Policy.from_config` refuses a policy with one: a
  misspelled `deny = ["mesage.delete"]` used to deny nothing while the
  operator believed deletion was off.

Credentials are their own class. An operation tagged `bearer-credential`
prints or mints something that grants access (a session string, a login code,
an autologin token, a contact token, a webhook secret), so it is never in
`*:read` even when it changes nothing. It has to be allowed by name, by its
group, by `*:credential` or by `*`.

Only the daemon's copy, read from `config.toml`, has teeth: `--enable-commands`
is a flag the caller controls. This is a usability guard, not a sandbox:
anything that can open the socket can also read the session file. §8.3 says
so, and `SECURITY.md` repeats it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tlgr.core.errors import ConfigurationError, PermissionError_

if TYPE_CHECKING:  # pragma: no cover
    from tlgr.ops._spec import OperationSpec

__all__ = ["CLASSES", "CREDENTIAL_TAG", "Policy", "PolicyDecision", "classes_of"]

#: The tag that moves an operation out of `*:read` and into `*:credential`.
CREDENTIAL_TAG = "bearer-credential"

#: Behaviour classes usable in place of an id, as `*:<class>`.
CLASSES = frozenset({"read", "mutating", "destructive", "stream", "local", "credential"})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


def classes_of(spec: OperationSpec | None) -> set[str]:
    """The behaviour classes *spec* belongs to.

    `read` means "changes nothing and reveals no credential". An op that
    prints or mints a bearer credential is `credential` instead, so
    `allow = ["*:read"]` cannot export the session.
    """
    if spec is None:
        return set()
    classes = {"stream"} if spec.stream else set()
    credential = CREDENTIAL_TAG in spec.tags
    if credential:
        classes.add("credential")
    if spec.destructive:
        classes |= {"mutating", "destructive"}
    elif spec.mutating:
        classes.add("mutating")
    elif not credential:
        classes.add("read")
    if spec.surface.value == "local":
        classes.add("local")
    return classes


def _canonical(name: str) -> str:
    from tlgr.registry import ALIASES

    key = name.strip().lower().replace(" ", ".")
    return ALIASES.get(key, key)


def _matches(entry: str, op_id: str, classes: set[str]) -> bool:
    entry = entry.strip().lower()
    if not entry:
        return False
    if entry in ("*", "all"):
        return True
    if entry.startswith("*:"):
        return entry[2:] in classes and entry[2:] in CLASSES
    if entry.endswith(".*"):
        return op_id.startswith(entry[:-1])
    resolved = _canonical(entry)
    return resolved == op_id or op_id.startswith(f"{resolved}.")


@dataclass
class Policy:
    """An allow/deny pair over canonical operation ids."""

    allow: list[str] = field(default_factory=lambda: ["*"])
    deny: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, allow: list[str] | None, deny: list[str] | None) -> Policy:
        """The `[policy]` table, validated against the registry.

        Raises CONFIG_ERROR naming every entry that matches no operation,
        which is what keeps the daemon from starting, and a reload from
        swapping, on a typo.
        """
        policy = cls(allow=list(allow or ["*"]), deny=list(deny or []))
        problems = policy.problems()
        if problems:
            raise ConfigurationError(
                "[policy] has entries that match no operation:\n  " + "\n  ".join(problems)
            )
        return policy

    @classmethod
    def parse(cls, spec: str) -> Policy:
        """Parse a `--enable-commands`-style string into an allowlist."""
        entries = [e.strip() for e in spec.replace(" ", ",").split(",") if e.strip()]
        return cls(allow=entries or ["*"])

    # -- validation --------------------------------------------------------

    def problems(self) -> list[str]:
        """One message per entry that can never match, empty when all are live."""
        import tlgr.ops  # noqa: F401  (populates the registry)
        from tlgr.registry import ALIASES, REGISTRY

        specs = [(op_id, classes_of(spec)) for op_id, spec in REGISTRY.items()]
        out: list[str] = []
        for list_name, entries in (("allow", self.allow), ("deny", self.deny)):
            for raw in entries:
                entry = str(raw).strip().lower()
                if entry in ("*", "all"):
                    continue
                if not entry:
                    out.append(f"{list_name}: an empty entry matches nothing")
                    continue
                if entry.startswith("*:"):
                    if entry[2:] not in CLASSES:
                        known = ", ".join(f"*:{c}" for c in sorted(CLASSES))
                        out.append(
                            f"{list_name}: {raw!r} is not a behaviour class (known: {known})"
                        )
                    continue
                if any(_matches(entry, op_id, classes) for op_id, classes in specs):
                    continue
                names = sorted({*ALIASES, *(op_id.split(".")[0] for op_id in REGISTRY)})
                close = difflib.get_close_matches(entry.removesuffix(".*"), names, n=1)
                hint = f"; did you mean {close[0]!r}?" if close else ""
                out.append(f"{list_name}: {raw!r} matches no operation{hint}")
        return out

    # -- matching ----------------------------------------------------------

    def check(self, op_id: str, spec: OperationSpec | None = None) -> PolicyDecision:
        """Is *op_id* permitted? `deny` wins over `allow`, always."""
        canonical = _canonical(op_id)
        classes = classes_of(spec)
        for entry in self.deny:
            if _matches(entry, canonical, classes):
                return PolicyDecision(False, f"denied by policy entry {entry!r}")
        for entry in self.allow:
            if _matches(entry, canonical, classes):
                return PolicyDecision(True)
        return PolicyDecision(False, "not in the policy allowlist")

    def enforce(self, op_id: str, spec: OperationSpec | None = None) -> None:
        decision = self.check(op_id, spec)
        if not decision.allowed:
            raise PermissionError_(
                f"operation {op_id!r} is not enabled: {decision.reason}",
            )
