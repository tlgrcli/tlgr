"""The alias registry: which accounts exist, which is active, and how each is.

Three things changed from v1 and each of them was a bug:

* **an alias is validated before it becomes a path** (SEC-02). v1 accepted
  anything `str.isalnum()` liked after stripping `_` and `-`, which passes for
  the empty string and for a 4 KB name, and rejected nothing that mattered.
* **a read never creates.** `get_account_dir()` called `mkdir(parents=True)`,
  so `tlgr account list` materialised a directory for an alias that did not
  exist, and a typo left litter behind.
* **health is persisted here.** The daemon writes each account's state
  (`online`/`degraded`/`needs_login`/`frozen`) into this file, so
  `tlgr account list` tells the truth about an account even when the daemon is
  not running — which is exactly when you ask.

Order is preserved everywhere: `list_accounts()` and `connect_order()` return
lists, never a `set`. v1 connected "whichever alias came first out of a set",
which is COR-02 — a two-account user could send from the wrong identity.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tlgr.core.errors import AccountNotFoundError, TlgrError
from tlgr.core.paths import TlgrPaths, validate_alias, write_private

ACCOUNTS_FILE = "accounts.json"

#: The states an `AccountSession` can persist (§6.2). `unknown` is what an
#: account reads as before a daemon has ever held it.
ACCOUNT_STATES = (
    "unknown",
    "starting",
    "online",
    "degraded",
    "needs_login",
    "frozen",
    "stopped",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class AccountHealth:
    """What the daemon last knew about an account."""

    state: str = "unknown"
    reason: str = ""
    since: str = ""
    user_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccountInfo:
    alias: str
    phone: str | None = None
    username: str | None = None
    first_name: str | None = None
    user_id: int | None = None
    created_at: str | None = None
    health: AccountHealth = field(default_factory=AccountHealth)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountInfo:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        raw_health = known.pop("health", None)
        health = AccountHealth()
        if isinstance(raw_health, dict):
            health = AccountHealth(
                **{k: v for k, v in raw_health.items() if k in AccountHealth.__dataclass_fields__}
            )
        return cls(**known, health=health)

    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        if self.phone:
            return f"{self.phone[:4]}...{self.phone[-2:]}"
        return self.alias


class AccountManager:
    def __init__(self, base_dir: Path | None = None):
        self.paths = TlgrPaths(base_dir)
        self.base_dir = self.paths.base
        self.accounts_file = self.paths.accounts_file
        self._data: dict[str, Any] | None = None

    # -- persistence -------------------------------------------------------

    @property
    def accounts_dir(self) -> Path:
        """The accounts directory. Reading this never creates it."""
        return self.paths.accounts

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self.accounts_file.exists():
            self._data = {"active": None, "accounts": {}}
            return self._data
        try:
            with open(self.accounts_file) as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("accounts.json is not an object")
            loaded.setdefault("accounts", {})
            loaded.setdefault("active", None)
            self._data = loaded
        except (OSError, json.JSONDecodeError, ValueError):
            self._data = {"active": None, "accounts": {}}
        return self._data

    def reload(self) -> None:
        """Drop the cached view so the next read sees another process's write."""
        self._data = None

    def _save(self) -> None:
        if self._data is None:
            return
        write_private(self.accounts_file, json.dumps(self._data, indent=2, ensure_ascii=False))

    # -- active alias ------------------------------------------------------

    def get_active(self) -> str | None:
        data = self._load()
        active = data.get("active")
        if active and active in data.get("accounts", {}):
            return str(active)
        accounts = data.get("accounts", {})
        if accounts:
            first = next(iter(accounts))
            self.set_active(first)
            return str(first)
        return None

    def set_active(self, alias: str) -> bool:
        data = self._load()
        if alias not in data.get("accounts", {}):
            return False
        data["active"] = alias
        self._save()
        return True

    # -- queries -----------------------------------------------------------

    def list_accounts(self) -> list[AccountInfo]:
        data = self._load()
        return [AccountInfo.from_dict(v) for v in data.get("accounts", {}).values()]

    def aliases(self) -> list[str]:
        """Registered aliases, in insertion order."""
        return list(self._load().get("accounts", {}))

    def get_account(self, alias: str) -> AccountInfo | None:
        data = self._load()
        info = data.get("accounts", {}).get(alias)
        return AccountInfo.from_dict(info) if info else None

    def require_account(self, alias: str) -> AccountInfo:
        validate_alias(alias)
        info = self.get_account(alias)
        if info is None:
            raise AccountNotFoundError(f"no account named {alias!r}")
        return info

    def has_accounts(self) -> bool:
        return bool(self._load().get("accounts"))

    def connect_order(self, config_default: str = "", extra: list[str] | None = None) -> list[str]:
        """The ordered list of aliases the daemon connects at start (§6.1).

        A *list*: the union used in v1 was a `set`, so the daemon's connect
        order — and therefore which account answered an under-specified
        request — depended on hash randomisation.
        """
        ordered: list[str] = []
        known = set(self.aliases())

        def push(alias: str | None) -> None:
            if alias and alias in known and alias not in ordered:
                ordered.append(alias)

        for alias in extra or []:
            push(alias)
        push(config_default)
        push(self._load().get("active"))
        return ordered

    # -- mutation ----------------------------------------------------------

    def add_account(self, alias: str) -> AccountInfo:
        validate_alias(alias)
        data = self._load()
        if alias in data.get("accounts", {}):
            raise TlgrError(f"Account '{alias}' already exists")
        self.paths.ensure_account_dir(alias)
        account = AccountInfo(alias=alias, created_at=datetime.now().isoformat())
        data.setdefault("accounts", {})[alias] = account.to_dict()
        if not data.get("active"):
            data["active"] = alias
        self._save()
        return account

    def update_account(
        self,
        alias: str,
        *,
        phone: str | None = None,
        username: str | None = None,
        first_name: str | None = None,
        user_id: int | None = None,
    ) -> AccountInfo | None:
        data = self._load()
        if alias not in data.get("accounts", {}):
            return None
        stored = data["accounts"][alias]
        if phone is not None:
            stored["phone"] = phone
        if username is not None:
            stored["username"] = username
        if first_name is not None:
            stored["first_name"] = first_name
        if user_id is not None:
            stored["user_id"] = user_id
        self._save()
        return AccountInfo.from_dict(stored)

    def set_health(
        self,
        alias: str,
        state: str,
        *,
        reason: str = "",
        user_id: int | None = None,
    ) -> None:
        """Record what the daemon knows about *alias* so the CLI can read it.

        Written even when the alias is unknown to the registry (an imported
        session that was never `account add`-ed still has a health story), so
        this never raises on the daemon's hot path.
        """
        validate_alias(alias)
        self.reload()
        data = self._load()
        accounts = data.setdefault("accounts", {})
        stored = accounts.setdefault(alias, AccountInfo(alias=alias).to_dict())
        previous = stored.get("health") or {}
        if previous.get("state") == state and previous.get("reason", "") == reason:
            since = previous.get("since") or _now()
        else:
            since = _now()
        stored["health"] = {
            "state": state,
            "reason": reason,
            "since": since,
            "user_id": user_id if user_id is not None else previous.get("user_id"),
        }
        if user_id is not None:
            stored["user_id"] = user_id
        self._save()

    def get_health(self, alias: str) -> AccountHealth:
        info = self.get_account(alias)
        return info.health if info else AccountHealth()

    def remove_account(self, alias: str, delete_data: bool = True) -> bool:
        validate_alias(alias)
        data = self._load()
        if alias not in data.get("accounts", {}):
            return False
        del data["accounts"][alias]
        if data.get("active") == alias:
            remaining = list(data["accounts"])
            data["active"] = remaining[0] if remaining else None
        self._save()
        if delete_data:
            account_dir = self.paths.account_dir(alias)
            if account_dir.exists():
                shutil.rmtree(account_dir)
        return True

    def rename_account(self, old_alias: str, new_alias: str) -> bool:
        validate_alias(old_alias)
        validate_alias(new_alias)
        data = self._load()
        if old_alias not in data.get("accounts", {}):
            return False
        if new_alias in data.get("accounts", {}):
            raise TlgrError(f"Account '{new_alias}' already exists")
        stored = data["accounts"].pop(old_alias)
        stored["alias"] = new_alias
        data["accounts"][new_alias] = stored
        if data.get("active") == old_alias:
            data["active"] = new_alias
        self._save()
        old_dir = self.paths.account_dir(old_alias)
        new_dir = self.paths.account_dir(new_alias)
        if old_dir.exists():
            old_dir.rename(new_dir)
        return True

    # -- paths -------------------------------------------------------------

    def _resolve_alias(self, alias: str | None) -> str:
        if alias is None:
            alias = self.get_active()
        if not alias:
            raise TlgrError("No account specified and no active account")
        return validate_alias(alias)

    def get_account_dir(self, alias: str | None = None, *, create: bool = False) -> Path:
        """The account's directory. Does **not** create it unless asked."""
        resolved = self._resolve_alias(alias)
        if create:
            return self.paths.ensure_account_dir(resolved)
        return self.paths.account_dir(resolved)

    def ensure_account_dir(self, alias: str | None = None) -> Path:
        return self.get_account_dir(alias, create=True)

    def get_session_path(self, alias: str | None = None) -> Path:
        return self.paths.session(self._resolve_alias(alias))

    def get_session_lock_path(self, alias: str | None = None) -> Path:
        return self.paths.session_lock(self._resolve_alias(alias))

    def get_credentials_path(self, alias: str | None = None) -> Path:
        return self.paths.credentials(self._resolve_alias(alias))

    # -- credentials -------------------------------------------------------

    def load_credentials(self, alias: str | None = None) -> tuple[int | None, str | None]:
        cred_path = self.get_credentials_path(alias)
        api_id: int | None = None
        api_hash: str | None = None

        if cred_path.exists():
            try:
                with open(cred_path) as handle:
                    data = json.load(handle)
                api_id = data.get("api_id")
                api_hash = data.get("api_hash")
            except (OSError, json.JSONDecodeError):
                pass

        env_id = os.environ.get("TELEGRAM_API_ID")
        env_hash = os.environ.get("TELEGRAM_API_HASH")
        if env_id:
            try:
                api_id = int(env_id)
            except ValueError:
                pass
        if env_hash:
            api_hash = env_hash

        return api_id, api_hash

    def save_credentials(self, api_id: int, api_hash: str, alias: str | None = None) -> None:
        resolved = self._resolve_alias(alias)
        self.paths.ensure_account_dir(resolved)
        write_private(
            self.paths.credentials(resolved),
            json.dumps({"api_id": api_id, "api_hash": api_hash}, indent=2),
        )
