"""The daemon's view of the operation allowlist (SEC-04).

The matcher lives in `tlgr.policy` so the CLI's `--enable-commands` check and
the daemon's `[policy]` enforcement reach the same verdict from the same code;
the CLI may not import `tlgr.daemon` (§2.2). This module keeps the import path
the daemon and its tests already use.

The daemon's copy is the one with teeth: it comes from `config.toml`, which
the caller cannot change per request, while `--enable-commands` is a flag the
caller controls and is checked only in the CLI.
"""

from __future__ import annotations

from tlgr.policy import CLASSES, CREDENTIAL_TAG, Policy, PolicyDecision, classes_of

__all__ = ["CLASSES", "CREDENTIAL_TAG", "Policy", "PolicyDecision", "classes_of"]
