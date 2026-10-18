from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


from goal.agents import AgentResult
from goal.defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE
from goal.store import Store
from kernel.defs import ROOT_SESSION_ID, StoreError


class RecordingProviderRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, provider: str, **kwargs: Any) -> AgentResult:
        turn_index = len(self.calls)
        self.calls.append({"provider": provider, **kwargs})
        return AgentResult(
            provider=provider,
            output=f"provider output {turn_index}",
            resume_token=kwargs.get("resume_token"),
        )


def _new_store() -> Store:
    state_root = Path(tempfile.mkdtemp()) / "state"
    store = Store(state_root)
    store.init()
    return store


def _session_message(store: Store, session_id: str) -> None:
    store.create_session(session_id, parent_session_ids=[ROOT_SESSION_ID])
    store.append_user_input(session_id, sender="root", body="work", reply_to="console")
    store.update_goal(session_id, body="session goal", created_by="root")


class ServiceIdentityRegistryTests(unittest.TestCase):
    def test_init_auto_registers_provider_service_accounts(self) -> None:
        store = _new_store()
        users = {user["username"]: user for user in store.users()}
        self.assertEqual(users["svc-codex"]["status"], "service")
        self.assertEqual(users["svc-codex"]["provider"], "codex")
        self.assertEqual(users["svc-codex"]["groups"], ["manage", "work"])
        self.assertEqual(users["svc-claude"]["groups"], ["manage", "work"])
        self.assertEqual(users["svc-remote-aize"]["groups"], ["work"])
        self.assertEqual(users["svc-remote-aize"]["provider"], "remote-aize")
        with self.assertRaises(StoreError):
            store.authenticate("svc-codex", password="anything")

    def test_service_accounts_cannot_authenticate(self) -> None:
        store = _new_store()
        with self.assertRaises(StoreError):
            store.authenticate("svc-codex", password="")
        with self.assertRaises(StoreError):
            store.authenticate("svc-codex", password="anything")
        authenticated = store.authenticate("root", password="root")
        self.assertEqual(authenticated["username"], "root")

    def test_service_account_namespaces_and_default_assignment(self) -> None:
        store = _new_store()
        with self.assertRaises(StoreError):
            store.create_user("svc-impersonator", password="password")
        with self.assertRaises(StoreError):
            store.create_service_user("plainname")
        created = store.create_service_user("svc-worker-extra", groups=["work"], provider="")
        self.assertEqual(created["status"], "service")
        self.assertEqual(created["groups"], ["work"])
        self.assertEqual(created["provider"], "")
        with self.assertRaises(StoreError):
            store.create_service_user("svc-worker-extra")
        state = store.load()
        self.assertNotIn("home_session_id", state["users"]["svc-codex"])
        self.assertNotIn("home_unit_id", state["users"]["svc-codex"])
        with self.assertRaises(StoreError):
            store.user_home_session("svc-codex")

    def test_group_members_listing_includes_slot_groups(self) -> None:
        store = _new_store()
        memberships = {entry["group"]: entry["members"] for entry in store.group_members()}
        self.assertEqual(sorted(memberships["manage"]), ["svc-claude", "svc-codex"])
        self.assertEqual(
            sorted(memberships["work"]),
            ["svc-claude", "svc-codex", "svc-remote-aize"],
        )


class AgentProfilePriorityTests(unittest.TestCase):
    def test_default_profiles_appoint_the_default_provider_account(self) -> None:
        store = _new_store()
        profiles = {profile["role"]: profile for profile in store.agent_profiles()}
        self.assertEqual(profiles[GOAL_MANAGER_ROLE]["accounts"], ["svc-codex"])
        self.assertEqual(profiles[WORKER_AGENT_ROLE]["accounts"], ["svc-codex"])

    def test_set_agent_provider_maps_to_service_accounts(self) -> None:
        store = _new_store()
        profiles = store.set_agent_provider(WORKER_AGENT_ROLE, provider="claude")
        self.assertEqual(profiles["accounts"], ["svc-claude"])
        workers = store.set_agent_accounts(WORKER_AGENT_ROLE, accounts=["svc-claude", "svc-codex"])
        self.assertEqual(workers["accounts"], ["svc-claude", "svc-codex"])
        cleared = store.set_agent_accounts(WORKER_AGENT_ROLE, accounts=[])
        self.assertEqual(cleared["accounts"], [])

    def test_set_agent_rejects_ineligible_providers(self) -> None:
        store = _new_store()
        with self.assertRaises(StoreError) as raised:
            store.set_agent_provider(GOAL_MANAGER_ROLE, provider="remote-aize")
        self.assertIn("not manage-capable", str(raised.exception))
        accepted = store.set_agent_provider(WORKER_AGENT_ROLE, provider="remote-aize")
        self.assertEqual(accepted["accounts"], ["svc-remote-aize"])

    def test_set_agent_accounts_validates_membership_and_service_status(self) -> None:
        store = _new_store()
        store.create_user("human", password="password")
        with self.assertRaises(StoreError):
            store.set_agent_accounts(GOAL_MANAGER_ROLE, accounts=["unknown-account"])
        with self.assertRaises(StoreError):
            store.set_agent_accounts(GOAL_MANAGER_ROLE, accounts=["human"])
        store.create_service_user("svc-work-only", groups=["work"])
        with self.assertRaises(StoreError) as raised:
            store.set_agent_accounts(GOAL_MANAGER_ROLE, accounts=["svc-work-only"])
        self.assertIn("not eligible", str(raised.exception))


class DispatchAppointmentTests(unittest.TestCase):
    def _dispatch_once(self, store: Store) -> dict[str, Any]:
        runner = RecordingProviderRunner()
        with patch("goal.dispatch.AgentRunner", return_value=runner):
            result = store.dispatch_once()
        return {"result": result, "runner": runner}

    def test_dispatch_appoints_default_service_account(self) -> None:
        store = _new_store()
        _session_message(store, "svc-session")
        payload = self._dispatch_once(store)
        self.assertIsNotNone(payload["result"])
        run = payload["result"]["run"]
        self.assertEqual(run["agent_user"], "svc-codex")
        self.assertEqual(run["agent_group"], "manage")
        self.assertEqual(run["agent_provider"], "codex")
        env = payload["runner"].calls[0]["runtime_env"]
        self.assertEqual(env["AIZE_AGENT_USER"], "svc-codex")
        self.assertEqual(env["AIZE_AGENT_GROUP"], "manage")
        self.assertEqual(env["AIZE_AGENT_ROLE"], "GoalManager")
        state = store.load()
        thread = state["agent_threads"]["svc-session:GoalManager:svc-codex"]
        self.assertEqual(thread["account"], "svc-codex")
        self.assertEqual(thread["group"], "manage")

    def test_priority_order_is_honored(self) -> None:
        store = _new_store()
        store.set_agent_accounts(GOAL_MANAGER_ROLE, accounts=["svc-claude", "svc-codex"])
        _session_message(store, "priority-session")
        payload = self._dispatch_once(store)
        run = payload["result"]["run"]
        self.assertEqual(run["agent_user"], "svc-claude")
        self.assertEqual(run["agent_provider"], "claude")

    def test_unavailable_provider_falls_through(self) -> None:
        store = _new_store()
        _session_message(store, "fallthrough-session")
        fake_bin = Path(tempfile.mkdtemp()) / "bin"
        fake_bin.mkdir()
        claude = fake_bin / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n")
        claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
        previous_path = os.environ.get("PATH", "")
        previous_flag = os.environ.pop("AIZE_ENABLE_EXTERNAL_AGENTS", None)
        try:
            os.environ["PATH"] = str(fake_bin)
            os.environ["AIZE_ENABLE_EXTERNAL_AGENTS"] = "true"
            state = store.load()
            profile = {"provider": "codex", "accounts": ["svc-codex", "svc-claude"]}
            selected = store._select_dispatch_account(state, role=GOAL_MANAGER_ROLE, profile=profile)
            self.assertEqual(selected["username"], "svc-claude")
            self.assertEqual(selected["provider"], "claude")
        finally:
            os.environ["PATH"] = previous_path
            if previous_flag is None:
                os.environ.pop("AIZE_ENABLE_EXTERNAL_AGENTS", None)
            else:
                os.environ["AIZE_ENABLE_EXTERNAL_AGENTS"] = previous_flag

    def test_empty_priority_list_keeps_protocol_identity(self) -> None:
        store = _new_store()
        store.set_agent_provider(GOAL_MANAGER_ROLE, provider="local")
        _session_message(store, "protocol-session")
        payload = self._dispatch_once(store)
        run = payload["result"]["run"]
        self.assertEqual(run["agent_user"], "")
        self.assertEqual(run["agent_provider"], "local")
        state = store.load()
        self.assertIn("protocol-session:GoalManager", state["agent_threads"])
        store.append_runtime_message(
            "protocol-session",
            sender="GoalManager",
            recipient="Session",
            body="protocol sender",
            run_id=payload["result"]["run"]["run_id"],
        )
        messages = store.messages("protocol-session")
        self.assertEqual(messages[-1]["from"], "agent:GoalManager")


class RuntimeSenderEndpointTests(unittest.TestCase):
    def _run_with_profile_session(self) -> Store:
        store = _new_store()
        _session_message(store, "sender-session")
        return store

    def test_appointed_account_sends_as_user_endpoint(self) -> None:
        store = self._run_with_profile_session()
        store.append_runtime_message(
            "sender-session",
            sender="svc-codex",
            recipient="Session",
            body="agent says hi",
            group="manage",
        )
        store.append_runtime_message(
            "sender-session",
            sender="svc-codex",
            recipient="Session",
            body="worker ",
            group="work",
        )
        messages = store.messages("sender-session")
        self.assertEqual(messages[-2]["from"], "user:svc-codex")
        self.assertEqual(messages[-1]["from"], "user:svc-codex")

    def test_sender_group_must_be_claimed_when_ambiguous(self) -> None:
        store = self._run_with_profile_session()
        with self.assertRaises(StoreError):
            store.append_runtime_message(
                "sender-session",
                sender="svc-codex",
                recipient="Session",
                body="no group claimed",
            )

    def test_worker_requests_require_manager_slot(self) -> None:
        store = self._run_with_profile_session()
        store.append_runtime_message(
            "sender-session",
            sender="svc-codex",
            recipient="Session",
            body="request",
            group="manage",
            worker_request=True,
        )
        with self.assertRaises(StoreError):
            store.append_runtime_message(
                "sender-session",
                sender="svc-codex",
                recipient="Session",
                body="worker request",
                group="work",
                worker_request=True,
            )

    def test_run_binding_binds_messages_to_the_leased_identity(self) -> None:
        store = self._run_with_profile_session()
        payload_result = None
        runner = RecordingProviderRunner()
        with patch("goal.dispatch.AgentRunner", return_value=runner):
            payload_result = store.dispatch_once()
        run = payload_result["run"]
        store.append_runtime_message(
            "sender-session",
            sender=run["agent_user"],
            recipient="Session",
            body="bound message",
            group=run["agent_group"],
            run_id=run["run_id"],
        )
        with self.assertRaises(StoreError):
            store.append_runtime_message(
                "sender-session",
                sender="svc-claude",
                recipient="Session",
                body="impostor",
                group="manage",
                run_id=run["run_id"],
            )

    def test_runtime_actor_provenance_binds_to_lease(self) -> None:
        store = self._run_with_profile_session()
        runner = RecordingProviderRunner()
        with patch("goal.dispatch.AgentRunner", return_value=runner):
            payload_result = store.dispatch_once()
        run = payload_result["run"]
        state = store.load()
        self.assertTrue(
            store._runtime_actor_allowed(
                state, actor=run["agent_user"], run_id=run["run_id"], role=GOAL_MANAGER_ROLE
            )
        )
        self.assertTrue(
            store._runtime_actor_allowed(
                state, actor=GOAL_MANAGER_ROLE, run_id=run["run_id"], role=GOAL_MANAGER_ROLE
            )
        )
        self.assertTrue(
            store._runtime_actor_allowed(
                state, actor="manage", run_id=run["run_id"], role=GOAL_MANAGER_ROLE
            )
        )
        self.assertFalse(
            store._runtime_actor_allowed(
                state, actor="svc-claude", run_id=run["run_id"], role=GOAL_MANAGER_ROLE
            )
        )


class EndpointFamilyTests(unittest.TestCase):
    def test_user_messages_use_the_user_endpoint_family(self) -> None:
        store = _new_store()
        store.create_session("endpoint-session", parent_session_ids=[ROOT_SESSION_ID])
        store.append_message("endpoint-session", sender="root", recipient="root", body="self echo")
        message = store.messages("endpoint-session")[-1]
        self.assertEqual(message["from"], "user:root")
        self.assertEqual(message["to"], "user:root")

    def test_account_endpoints_are_dual_read(self) -> None:
        store = _new_store()
        store.create_session("dual-session", parent_session_ids=[ROOT_SESSION_ID])
        first = store.append_message("dual-session", sender="root", recipient="root", body="first")
        state = store.load()
        state["endpoint_cursors"]["account:root"] = first["message_id"]
        store.save(state)
        second = store.append_message("dual-session", sender="root", recipient="root", body="second")
        received = store.receive_message("root")
        self.assertIsNotNone(received)
        self.assertEqual(received["message_id"], second["message_id"])


if __name__ == "__main__":
    unittest.main()
