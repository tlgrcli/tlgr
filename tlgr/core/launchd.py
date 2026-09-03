"""macOS LaunchAgent management for the tlgr daemon (§6.12).

`KeepAlive.SuccessfulExit = false` means "restart it unless it exited 0",
which is only safe if the daemon never exits 0 on its own. It did: the idle
monitor stopped it after thirty minutes, launchd saw a clean exit, and the
daemon never came back — jobs silently stopped and the webhook silently
unsubscribed until someone noticed. That is COR-39.

Two changes close it. The plist passes `--base`, and the daemon forces
`idle_timeout` to 0 whenever a supervisor owns it, so the clean exit that
launchd will not restart cannot happen. And a manually started second daemon
exits 0 with "already running" rather than 1, so `KeepAlive` does not turn a
duplicate start into a respawn loop.

The `ExecStart` also runs `tlgr.daemon.main`, not `tlgr.daemon.server`; the
latter still works as an alias, and an already-installed v1 plist keeps
running until it is reinstalled.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

SERVICE_LABEL = "dev.tlgr.daemon"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _python_executable() -> str:
    """Return the absolute path to the current Python interpreter."""
    return sys.executable


def _build_plist(base: Path, log_dir: Path) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            _python_executable(),
            "-m",
            "tlgr.daemon.main",
            "--base",
            str(base),
            "--foreground",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        # Tells the daemon a supervisor owns it, which forces idle_timeout to
        # 0 — the other half of the COR-39 fix.
        "EnvironmentVariables": {"XPC_SERVICE_NAME": SERVICE_LABEL},
        "StandardOutPath": str(log_dir / "daemon.log"),
        "StandardErrorPath": str(log_dir / "daemon.log"),
    }


def is_installed() -> bool:
    return PLIST_PATH.exists()


def is_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
        capture_output=True,
    )
    return result.returncode == 0


def install(base: Path, log_dir: Path) -> Path:
    """Write the plist and load it into launchd. Returns the plist path."""
    if is_loaded():
        unload()

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist_data = _build_plist(base, log_dir)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist_data, f)

    _load()
    return PLIST_PATH


def uninstall() -> bool:
    """Unload and remove the plist. Returns True if anything was removed."""
    removed = False
    if is_loaded():
        unload()
        removed = True
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        removed = True
    return removed


def _load() -> None:
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)],
        check=True,
    )


def unload() -> None:
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
        capture_output=True,
    )


def kickstart() -> None:
    """Force-(re)start the service via launchctl."""
    subprocess.run(
        [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/{SERVICE_LABEL}",
        ],
        check=True,
    )
