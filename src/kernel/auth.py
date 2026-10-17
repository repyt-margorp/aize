from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from kernel.model import User, utc_now
from kernel.lock import state_transaction
from kernel.defs import StoreError


PASSWORD_HASH_ITERATIONS = 210_000


class AuthMixin:
    def _build_user(
        self,
        username: str,
        password: str,
        *,
        groups: list[str],
        created_at: str,
    ) -> User:
        salt = secrets.token_hex(16)
        return User(
            username=username,
            password_hash=self._hash_password(password, salt),
            salt=salt,
            groups=groups,
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

    def _public_user(self, user: dict[str, Any]) -> dict[str, Any]:
        return {
            "username": user["username"],
            "groups": list(user.get("groups", [])),
            "created_at": user["created_at"],
            "status": user.get("status", "active"),
            "home_unit_id": user.get("home_unit_id"),
            "home_session_id": user.get("home_session_id"),
        }

    @state_transaction
    def create_user(self, username: str, *, password: str, groups: list[str] | None = None) -> dict[str, Any]:
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise StoreError("username is required")
        if not password:
            raise StoreError("password is required")
        state = self.load()
        users = state["users"]
        if normalized_username in users:
            raise StoreError(f"user already exists: {normalized_username}")
        now = utc_now()
        user = self._build_user(
            normalized_username,
            password,
            groups=groups or ["user"],
            created_at=now,
        )
        users[normalized_username] = user.to_dict()
        self._ensure_user_home_sessions(state, now=now)
        self.save(state)
        return self._public_user(users[normalized_username])

    def authenticate(self, username: str, *, password: str) -> dict[str, Any]:
        state = self.load()
        user = state["users"].get(username)
        if not user:
            raise StoreError("authentication failed")
        if user.get("status") != "active":
            raise StoreError("authentication failed")
        expected = str(user.get("password_hash") or "")
        actual = self._hash_password(password, str(user.get("salt") or ""))
        if not hmac.compare_digest(expected, actual):
            raise StoreError("authentication failed")
        return self._public_user(user)

    def user_home_session(self, username: str) -> str:
        state = self.load()
        user = state["users"].get(username)
        if not user:
            raise StoreError(f"unknown user: {username}")
        home_session_id = str(user.get("home_session_id") or "").strip()
        if not home_session_id:
            raise StoreError(f"user has no home session: {username}")
        if home_session_id not in state["sessions"]:
            raise StoreError(f"unknown user home session: {home_session_id}")
        return home_session_id

    def users(self) -> list[dict[str, Any]]:
        state = self.load()
        return sorted(
            [self._public_user(user) for user in state["users"].values()],
            key=lambda item: item["username"],
        )
