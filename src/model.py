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
class Account:
    username: str
    password_hash: str
    salt: str
    roles: list[str]
    created_at: str
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "salt": self.salt,
            "roles": list(self.roles),
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class Unit:
    unit_id: str
    created_at: str
    status: str = "active"
    instance_policy: str = "multi"
    singleton_session_id: str | None = None
    display_name: str = ""
    description: str = ""
    goal_text: str = ""
    initial_prompt: str = ""
    schedule: dict[str, Any] | None = None
    automation: dict[str, Any] | None = None
    workspace_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "created_at": self.created_at,
            "status": self.status,
            "instance_policy": self.instance_policy,
            "singleton_session_id": self.singleton_session_id,
            "display_name": self.display_name,
            "description": self.description,
            "goal_text": self.goal_text,
            "initial_prompt": self.initial_prompt,
            "schedule": dict(self.schedule or {}),
            "automation": dict(self.automation or {}),
            "workspace_path": self.workspace_path,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "unit_id": self.unit_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": self.active,
            "singleton": self.singleton,
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


@dataclass(frozen=True)
class Goal:
    goal_id: str
    session_id: str
    body: str
    created_by: str
    created_at: str
    completion_state: str = "incomplete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "body": self.body,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "completion_state": self.completion_state,
        }
