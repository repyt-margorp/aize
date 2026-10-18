from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


from goal.agents import AgentResult
from goal.defs import GOAL_MANAGER_ROLE
from goal.store import Store
from kernel.defs import ROOT_SESSION_ID


class RecordingProviderRunner:
    def __init__(self, thread_ids: dict[int, str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.thread_ids = thread_ids

    def run(self, provider: str, **kwargs: Any) -> AgentResult:
        turn_index = len(self.calls)
        self.calls.append({"provider": provider, **kwargs})
        return AgentResult(
            provider=provider,
            output=f"provider output {turn_index}",
            resume_token=kwargs.get("resume_token"),
            provider_thread_id=self.thread_ids.get(turn_index),
        )


class ProviderThreadContractTests(unittest.TestCase):
    def _session(self) -> Store:
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()
        store.create_session("thread-session", parent_session_ids=[ROOT_SESSION_ID])
        store.append_user_input("thread-session", sender="root", body="work", reply_to="console")
        store.update_goal("thread-session", body="threaded goal", created_by="root")
        return store

    def _dispatch_once_with_runner(self, store: Store, runner: RecordingProviderRunner):
        with patch("goal.dispatch.AgentRunner", return_value=runner):
            result = store.dispatch_once(session_id="thread-session")
        return result

    def test_provider_thread_persists_on_agent_thread(self) -> None:
        store = self._session()
        runner = RecordingProviderRunner({0: "provider-thread-77"})
        result = self._dispatch_once_with_runner(store, runner)
        self.assertIsNotNone(result)
        self.assertIn("provider_thread", runner.calls[0])

        provider = runner.calls[0]["provider"]
        state = store.load()
        thread = next(
            entry
            for entry in store.storage.hydrate_agent_threads(list(state["agent_threads"].values()))
            if entry.get("thread_id") == "thread-session:GoalManager:svc-codex"
        )
        self.assertEqual(thread["provider_threads"].get(provider), "provider-thread-77")
        self.assertEqual(thread["turns"][-1]["result"], "provider output 0")

    def test_provider_thread_resumes_after_restart(self) -> None:
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()
        store.create_session("thread-session", parent_session_ids=[ROOT_SESSION_ID])
        store.append_user_input("thread-session", sender="root", body="work", reply_to="console")
        store.update_goal("thread-session", body="threaded goal", created_by="root")

        first = RecordingProviderRunner({0: "provider-thread-777"})
        self._dispatch_once_with_runner(store, first)

        reopened = Store(state_root)
        reopened.init()
        reopened.append_user_input("thread-session", sender="root", body="resume work", reply_to="console")
        reopened.update_goal("thread-session", body="threaded goal", created_by="root")
        second = RecordingProviderRunner({})
        with patch("goal.dispatch.AgentRunner", return_value=second):
            reopened.dispatch_once(session_id="thread-session")
        provider = second.calls[0]["provider"]
        self.assertEqual(
            second.calls[0]["provider_thread"].get(provider),
            "provider-thread-777",
        )

    def test_local_provider_contract_without_thread_hint_returns_none(self) -> None:
        store = self._session()
        from goal.agents import AgentRunner

        runner = AgentRunner()
        result = runner.run(
            "local",
            role=GOAL_MANAGER_ROLE,
            prompt="local prompt",
            resume_token="thread-agent-x",
        )
        self.assertEqual(result.provider, "local")
        self.assertIsNone(result.provider_thread_id)
        self.assertEqual(result.resume_token, "thread-agent-x")


if __name__ == "__main__":
    unittest.main()
