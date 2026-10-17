from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    salt: str
    groups: list[str]
    created_at: str
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "salt": self.salt,
            "groups": list(self.groups),
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class Session:
    session_id: str
    unit_id: str | None
    created_at: str
    updated_at: str
    title: str = ""
    active: bool = True
    singleton: bool = False
    scheduling_policy: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "unit_id": self.unit_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": self.active,
            "singleton": self.singleton,
            "scheduling_policy": dict(self.scheduling_policy or {"class": "normal", "base_priority": 0}),
        }


@dataclass(frozen=True)
class Message:
    message_id: str
    from_endpoint: str
    to_endpoint: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "from": self.from_endpoint,
            "to": self.to_endpoint,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }
