"""A systemd **user** unit for the daemon (§6.12).

User, not system: the daemon holds session files under `$HOME` and must run as
the person who owns them. A system unit would need `User=`, a home directory
override and a login session for the keyring, and would still be the wrong
security boundary.

`Type=simple` with `--foreground`, not `Type=notify`: notify needs
`sd_notify`, which means either a C extension or hand-rolling the socket
protocol, and buys nothing here — systemd's own restart logic is driven by the
process exiting, which is what `Restart=on-failure` already watches.

`idle_timeout` is forced to 0 under a supervisor (see `idle.py`): an idle exit
is either a respawn loop or, with the wrong `Restart=` policy, a daemon that
never comes back. That is COR-39, and the unit and the config agree about it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "tlgr.service"

__all__ = [
    "SERVICE_NAME",
    "install",
    "is_installed",
    "restart",
    "uninstall",
    "unit_path",
    "unit_text",
]


def unit_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user" / SERVICE_NAME


def unit_text(base: Path, *, python: str | None = None) -> str:
    executable = python or sys.executable
    return f"""[Unit]
Description=tlgr Telegram daemon
Documentation=https://github.com/tlgrcli/tlgr
After=network-online.target

[Service]
Type=simple
ExecStart={executable} -m tlgr.daemon.main --base {base} --foreground
Restart=on-failure
RestartSec=5
# The daemon sets its own umask before creating anything; this is the
# belt-and-braces copy for the files systemd itself creates.
UMask=0077
KillSignal=SIGTERM
TimeoutStopSec=45

[Install]
WantedBy=default.target
"""


def is_installed() -> bool:
    return unit_path().exists()


def install(base: Path, *, python: str | None = None, enable: bool = True) -> Path:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(base, python=python))
    path.chmod(0o644)
    _systemctl("daemon-reload")
    if enable:
        _systemctl("enable", "--now", SERVICE_NAME)
    return path


def uninstall() -> bool:
    path = unit_path()
    if not path.exists():
        return False
    _systemctl("disable", "--now", SERVICE_NAME)
    path.unlink()
    _systemctl("daemon-reload")
    return True


def restart() -> None:
    _systemctl("restart", SERVICE_NAME)


def _systemctl(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, check=False)
