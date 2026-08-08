from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from model import Account, utc_now
from store_defs import PASSWORD_HASH_ITERATIONS, StoreError


class AuthMixin:
    def _build_account(
        self,
        username: str,
        password: str,
        *,
        roles: list[str],
        created_at: str,
    ) -> Account:
        salt = secrets.token_hex(16)
        return Account(
            username=username,
            password_hash=self._hash_password(password, salt),
            salt=salt,
            roles=roles,
            created_at=created_at,
        )

    def _hash_password(self, password: str, salt: str) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            PASSWORD_HASH_ITERATIONS,
        )
        return digest.hex()

    def _public_account(self, account: dict[str, Any]) -> dict[str, Any]:
        return {
            "username": account["username"],
            "roles": list(account.get("roles", [])),
            "created_at": account["created_at"],
            "status": account.get("status", "active"),
            "home_unit_id": account.get("home_unit_id"),
            "home_session_id": account.get("home_session_id"),
        }

    def create_account(self, username: str, *, password: str, roles: list[str] | None = None) -> dict[str, Any]:
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise StoreError("username is required")
        if not password:
            raise StoreError("password is required")
        state = self.load()
        accounts = state["accounts"]
        if normalized_username in accounts:
            raise StoreError(f"account already exists: {normalized_username}")
        now = utc_now()
        account = self._build_account(
            normalized_username,
            password,
            roles=roles or ["user"],
            created_at=now,
        )
        accounts[normalized_username] = account.to_dict()
        self._ensure_account_home_sessions(state, now=now)
        self.save(state)
        return self._public_account(accounts[normalized_username])

    def authenticate(self, username: str, *, password: str) -> dict[str, Any]:
        state = self.load()
        account = state["accounts"].get(username)
        if not account:
            raise StoreError("authentication failed")
        if account.get("status") != "active":
            raise StoreError("authentication failed")
        expected = str(account.get("password_hash") or "")
        actual = self._hash_password(password, str(account.get("salt") or ""))
        if not hmac.compare_digest(expected, actual):
            raise StoreError("authentication failed")
        return self._public_account(account)

    def account_home_session(self, username: str) -> str:
        state = self.load()
        account = state["accounts"].get(username)
        if not account:
            raise StoreError(f"unknown account: {username}")
        home_session_id = str(account.get("home_session_id") or "").strip()
        if not home_session_id:
            raise StoreError(f"account has no home session: {username}")
        if home_session_id not in state["sessions"]:
            raise StoreError(f"unknown account home session: {home_session_id}")
        return home_session_id

    def accounts(self) -> list[dict[str, Any]]:
        state = self.load()
        return sorted(
            [self._public_account(account) for account in state["accounts"].values()],
            key=lambda item: item["username"],
        )
