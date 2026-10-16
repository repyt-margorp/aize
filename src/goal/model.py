from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    activation_triggers: dict[str, bool] | None = None
    workspace_path: str = ""
    owner_account: str = ""

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
            "activation_triggers": dict(self.activation_triggers or {}),
            "workspace_path": self.workspace_path,
            "owner_account": self.owner_account,
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
