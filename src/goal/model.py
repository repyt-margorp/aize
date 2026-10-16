from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
