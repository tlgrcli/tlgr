"""Account management for multi-account support."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tlgr.core.config import CONFIG_DIR, get_accounts_dir
from tlgr.core.errors import TlgrError

ACCOUNTS_FILE = "accounts.json"


@dataclass
class AccountInfo:
    alias: str
    phone: str | None = None
    username: str | None = None
    first_name: str | None = None
    user_id: int | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountInfo:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

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
        self.base_dir = base_dir or CONFIG_DIR
        self.accounts_file = self.base_dir / ACCOUNTS_FILE
        self.accounts_dir = get_accounts_dir(self.base_dir)
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self.accounts_file.exists():
            self._data = {"active": None, "accounts": {}}
            return self._data
        try:
            with open(self.accounts_file) as f:
                self._data = json.load(f)
                return self._data
        except (OSError, json.JSONDecodeError):
            self._data = {"active": None, "accounts": {}}
            return self._data

    def _save(self) -> None:
        if self._data is None:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        with open(self.accounts_file, "w") as f:
            json.dump(self._data, f, indent=2)
        self.accounts_file.chmod(0o600)

    def get_active(self) -> str | None:
        data = self._load()
        active = data.get("active")
        if active and active in data.get("accounts", {}):
            return active
        accounts = data.get("accounts", {})
        if accounts:
            first = next(iter(accounts))
            self.set_active(first)
            return first
        return None

    def set_active(self, alias: str) -> bool:
        data = self._load()
        if alias not in data.get("accounts", {}):
            return False
        data["active"] = alias
        self._save()
        return True

    def list_accounts(self) -> list[AccountInfo]:
        data = self._load()
        return [AccountInfo.from_dict(v) for v in data.get("accounts", {}).values()]

    def get_account(self, alias: str) -> AccountInfo | None:
        data = self._load()
        info = data.get("accounts", {}).get(alias)
        return AccountInfo.from_dict(info) if info else None

    def add_account(self, alias: str) -> AccountInfo:
        if not alias or not alias.replace("_", "").replace("-", "").isalnum():
            raise TlgrError(f"Invalid alias '{alias}'. Use letters, numbers, dashes, underscores.")
        data = self._load()
        if alias in data.get("accounts", {}):
            raise TlgrError(f"Account '{alias}' already exists")
        account_dir = self.accounts_dir / alias
        account_dir.mkdir(parents=True, exist_ok=True)
        account = AccountInfo(alias=alias, created_at=datetime.now().isoformat())
        if "accounts" not in data:
            data["accounts"] = {}
        data["accounts"][alias] = account.to_dict()
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
        ad = data["accounts"][alias]
        if phone is not None:
            ad["phone"] = phone
        if username is not None:
            ad["username"] = username
        if first_name is not None:
            ad["first_name"] = first_name
        if user_id is not None:
            ad["user_id"] = user_id
        self._save()
        return AccountInfo.from_dict(ad)

    def remove_account(self, alias: str, delete_data: bool = True) -> bool:
        data = self._load()
        if alias not in data.get("accounts", {}):
            return False
        del data["accounts"][alias]
        if data.get("active") == alias:
            remaining = list(data["accounts"])
            data["active"] = remaining[0] if remaining else None
        self._save()
        if delete_data:
            account_dir = self.accounts_dir / alias
            if account_dir.exists():
                shutil.rmtree(account_dir)
        return True

    def rename_account(self, old_alias: str, new_alias: str) -> bool:
        if not new_alias or not new_alias.replace("_", "").replace("-", "").isalnum():
            raise TlgrError(f"Invalid alias '{new_alias}'.")
        data = self._load()
        if old_alias not in data.get("accounts", {}):
            return False
        if new_alias in data.get("accounts", {}):
            raise TlgrError(f"Account '{new_alias}' already exists")
        ad = data["accounts"].pop(old_alias)
        ad["alias"] = new_alias
        data["accounts"][new_alias] = ad
        if data.get("active") == old_alias:
            data["active"] = new_alias
        self._save()
        old_dir = self.accounts_dir / old_alias
        new_dir = self.accounts_dir / new_alias
        if old_dir.exists():
            old_dir.rename(new_dir)
        return True

    def get_account_dir(self, alias: str | None = None) -> Path:
        if alias is None:
            alias = self.get_active()
        if alias is None:
            raise TlgrError("No account specified and no active account")
        d = self.accounts_dir / alias
        d.mkdir(parents=True, exist_ok=True)
        return d

    def has_accounts(self) -> bool:
        data = self._load()
        return bool(data.get("accounts"))

    def get_session_path(self, alias: str | None = None) -> Path:
        return self.get_account_dir(alias) / "session"

    def get_credentials_path(self, alias: str | None = None) -> Path:
        return self.get_account_dir(alias) / "config.json"

    def load_credentials(self, alias: str | None = None) -> tuple[int | None, str | None]:
        cred_path = self.get_credentials_path(alias)
        api_id: int | None = None
        api_hash: str | None = None

        if cred_path.exists():
            try:
                with open(cred_path) as f:
                    data = json.load(f)
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
        cred_path = self.get_credentials_path(alias)
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cred_path, "w") as f:
            json.dump({"api_id": api_id, "api_hash": api_hash}, f, indent=2)
        cred_path.chmod(0o600)
