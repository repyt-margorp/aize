from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.html_renderer import render_entrance_unit_page, render_main_page
from runtime.agent_service import (
    _dispatch_target_agent_id,
    _interactive_resume_xml,
    _interactive_worker_result_fallback_text,
    _interactive_worker_resume_target,
    _post_turn_followup_pending_state,
    _should_complete_communication_goal_after_reply,
    _should_preserve_prompt_cycle_progress_during_goal_review,
)
from runtime.http_handler import (
    _collapse_communication_duplicate_outputs,
    _communication_dispatch_plan,
    _matching_communication_skill_routes,
    _forwarded_session_pending_input,
    _infer_communication_forward_target_session_id,
    _materialize_communication_routed_child_session,
    _is_communication_chat_noise,
    _is_communication_session_settings,
    _select_communication_worker_service_id,
)
from runtime.persistent_state_pkg import (
    append_history,
    append_service_pending_input,
    create_child_conversation_session,
    create_conversation_session,
    ensure_state,
    get_session_settings,
    join_session_agent,
    list_session_children,
    list_sessions,
    load_session_skills,
    update_session_goal,
    update_session_goal_flags,
)
from runtime.ui_history import build_session_ui_history
from session_template import get_launchable_session_template, launch_session_template


class EntrancePageTests(unittest.TestCase):
    def test_entrance_page_renders_chat_polling_surface(self) -> None:
        page = render_entrance_unit_page(display_name="AIze", username="repyt")

        self.assertIn("Entrance Chat", page)
        self.assertIn("id='chat-log'", page)
        self.assertIn("entrance-status-badges", page)
        self.assertIn("renderEntranceState", page)
        self.assertIn("enter-send", page)
        self.assertIn("Enter sends message", page)
        self.assertIn("submitEntrancePrompt", page)
        self.assertIn("/overview?scope=all", page)
        self.assertIn("visibleAssistantText", page)
        self.assertIn("assistanttext", page)
        self.assertIn("mergeMessages", page)
        self.assertIn("kind==='agent'?[role,kind,value,turnBucket].join('|')", page)
        self.assertIn("renderChat([entry])", page)
        self.assertIn("/messages?session_id=", page)
        self.assertIn("pollTimer=setInterval(()=>{refreshChat();},2500)", page)
        self.assertIn("let statePollTimer=0;", page)
        self.assertIn("let entranceStateRefreshInFlight=false;", page)
        self.assertIn("statePollTimer=setInterval(()=>{refreshEntranceState();},10000)", page)
        self.assertIn("refreshChat();refreshEntranceState();startRealtime();", page)
        self.assertNotIn("if(!realtimeConnected)refreshChat()", page)
        self.assertIn("InteractiveAgent", page)
        self.assertIn("agent_message.delta", page)
        self.assertIn("user", page)
        self.assertIn("normalizedText==='response started'", page)
        self.assertIn("eventType==='agent.turn_started'", page)
        self.assertIn("entranceClipboardImageFiles", page)
        self.assertIn("entranceClipboardHasText", page)
        self.assertIn("entranceUploadFileName", page)
        self.assertIn("promptText.addEventListener('paste'", page)
        self.assertIn("queueEntranceFiles(imageFiles)", page)
        self.assertIn("formData.append('file',file,entranceUploadFileName(file,index))", page)
        self.assertIn("pasted-image-${index+1}.${ext}", page)
        self.assertIn("if(!entranceClipboardHasText(clipboardData))ev.preventDefault();", page)
        self.assertIn(".form{position:fixed", page)
        self.assertIn("left:0;right:0;bottom:0", page)
        self.assertIn("background:#fff", page)
        self.assertIn("border-radius:24px 24px 0 0", page)
        self.assertIn("calc(250px + env(safe-area-inset-bottom))", page)
        self.assertLess(page.index("</div></section><form id='entrance-form'"), page.index("</form></section>"))

    def test_entrance_status_events_refresh_immediately_and_state_poll_converges(self) -> None:
        page = render_entrance_unit_page(display_name="AIze", username="repyt")

        self.assertIn("if(entranceNeedsStateRefresh(eventType)){renderEntranceEventState(entry);renderChat([entry]);refreshEntranceState();", page)
        self.assertIn("eventType==='goal.status_changed'", page)
        self.assertIn("for(const key of ['goal_active','goal_completed','goal_progress_state','goal_completion_policy'])", page)
        self.assertIn("eventType==='runtime.status_changed'", page)
        self.assertIn("for(const key of ['runtime_execution_state','runtime_in_progress','agent_running','goal_manager_state','worker','goal_manager_worker'])", page)
        self.assertIn("const goalManagerState=String(s.goal_manager_state||'').trim().toLowerCase();", page)
        self.assertIn("goalManagerState==='running'?'running':'idle'", page)
        self.assertIn("statePollTimer=setInterval(()=>{refreshEntranceState();},10000)", page)
        self.assertIn("jsonFetch(`/sessions?_=${Date.now()}`,{cache:'no-store'})", page)
        self.assertIn("/overview?scope=all", page)
        self.assertLess(page.index("renderEntranceEventState(entry);renderChat([entry]);refreshEntranceState();"), page.index("statePollTimer=setInterval(()=>{refreshEntranceState();},10000)"))

    def test_entrance_ui_history_collapses_replayed_interactive_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir) / "runtime"
            ensure_state(runtime_root)
            (runtime_root / "logs").mkdir(parents=True, exist_ok=True)
            session = create_conversation_session(runtime_root, username="repyt", label="Entrance")
            session_id = str(session["session_id"])
            service_id = "service-codex-001"
            join_session_agent(
                runtime_root,
                username="repyt",
                session_id=session_id,
                service_id=service_id,
                provider="codex",
                role="interactive_agent",
                transport="http_user_dialogue",
            )
            append_history(
                runtime_root,
                username="repyt",
                session_id=session_id,
                entry={
                    "direction": "out",
                    "ts": "2026-05-18T16:14:00Z",
                    "to": service_id,
                    "session_id": session_id,
                    "text": "Please check status",
                },
                limit=500,
            )
            append_history(
                runtime_root,
                username="repyt",
                session_id=session_id,
                entry={
                    "direction": "in",
                    "ts": "2026-05-18T16:14:11Z",
                    "from": service_id,
                    "session_id": session_id,
                    "text": "The status is ready.",
                },
                limit=500,
            )
            (runtime_root / "logs" / "service-http-001.jsonl").write_text(
                json.dumps(
                    {
                        "type": "message.in",
                        "ts": "2026-05-18T16:14:33Z",
                        "service_id": "service-http-001",
                        "message": {
                            "from": service_id,
                            "to": "service-http-001",
                            "type": "prompt",
                            "meta": {"conversation": {"username": "repyt", "session_id": session_id}},
                            "payload": {"text": "The status is ready."},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            history = build_session_ui_history(runtime_root, username="repyt", session_id=session_id, limit=20)

        replies = [
            entry
            for entry in history
            if entry.get("direction") == "in" and entry.get("text") == "The status is ready."
        ]
        self.assertEqual(len(replies), 1)

    def test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir) / "runtime"
            ensure_state(runtime_root)
            (runtime_root / "logs").mkdir(parents=True, exist_ok=True)
            session = create_conversation_session(runtime_root, username="repyt", label="Entrance")
            session_id = str(session["session_id"])
            service_id = "service-codex-001"
            append_history(
                runtime_root,
                username="repyt",
                session_id=session_id,
                entry={
                    "direction": "out",
                    "ts": "2026-05-18T16:15:00Z",
                    "to": service_id,
                    "session_id": session_id,
                    "text": "Please confirm the fix",
                },
                limit=500,
            )
            (runtime_root / "logs" / "service-http-001.jsonl").write_text(
                json.dumps(
                    {
                        "type": "message.in",
                        "ts": "2026-05-18T16:15:11Z",
                        "service_id": "service-http-001",
                        "message": {
                            "from": service_id,
                            "to": "service-http-001",
                            "type": "prompt",
                            "meta": {"conversation": {"username": "repyt", "session_id": session_id}},
                            "payload": {"text": '{"assistant_text":"The duplicate fix is confirmed."}'},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (runtime_root / "logs" / f"{service_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "service.event",
                        "ts": "2026-05-18T16:15:12Z",
                        "service_id": service_id,
                        "scope": {"username": "repyt", "session_id": session_id},
                        "event": {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "The duplicate fix is confirmed."},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            history = build_session_ui_history(runtime_root, username="repyt", session_id=session_id, limit=20)

        replies: list[dict[str, object]] = []
        for entry in history:
            if entry.get("direction") == "in":
                visible_text = str(entry.get("text") or "").strip()
            else:
                event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                visible_text = str(item.get("text") or "").strip()
            if "duplicate fix is confirmed" in visible_text.lower():
                replies.append(entry)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].get("direction"), "in")

    def test_entrance_plugin_route_aliases_include_singular_unit(self) -> None:
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "src/runtime/http_handler.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"/unit/entrance"', source)
        self.assertIn('"/units/entrance"', source)
        self.assertIn('"/plugins/entrance"', source)

    def test_communication_prompt_dispatch_no_longer_uses_worker_text_heuristic(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src/runtime/http_handler.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("_interactive_prompt_needs_worker(prompt_text)", source)
        self.assertIn("_forwarded_session_pending_input(", source)
        self.assertIn("_communication_dispatch_plan(", source)
        self.assertIn("append_goal_manager_pending_input(", source)
        self.assertIn('"reason": "goal_manager_review"', source)
        self.assertNotIn("and not forwarded_session_id", source)

    def test_main_page_renders_latest_first_workspace_header_and_fixed_composer(self) -> None:
        page = render_main_page(
            username="repyt",
            session_id="session-123",
            role_name="superuser",
            is_superuser=True,
            initial_session_scope="owned",
            display_name="AIze",
            default_target="service-codex-001",
            default_provider="codex",
            initial_session_map_open=False,
            entries_json="[]",
            initial_runtime_journal_summary_json="{}",
            context_status_json="{}",
            initial_auto_compact_threshold=20,
            initial_session_label="WorkspaceView",
            initial_goal_text="Ship the WorkspaceView refresh",
            initial_active_goal_id="goal-1",
            initial_goal_history_json="[]",
            initial_goal_active=True,
            initial_goal_completed=False,
            initial_goal_progress_state="in_progress",
            initial_goal_audit_state="all_clear",
            initial_bound_service_id="service-codex-001",
            default_httpbridge_recent_messages_limit=40,
            initial_goal_reset_completed_on_prompt=True,
            initial_goal_auto_compact_enabled=True,
            initial_auto_resume_enabled=False,
            initial_auto_resume_interval_seconds=0,
            initial_auto_resume_next_at="",
            initial_auto_resume_reason="",
            initial_user_response_wait_status="idle",
            initial_user_response_wait_active=False,
            initial_user_response_wait_timeout_seconds=0,
            initial_user_response_wait_effective_timeout_seconds=0,
            initial_user_response_wait_started_at="",
            initial_user_response_wait_until_at="",
            initial_user_response_wait_request_id="",
            initial_user_response_wait_prompt_text="",
            initial_user_response_wait_reason="",
            initial_user_response_wait_last_cleared_at="",
            initial_user_response_wait_last_timeout_at="",
            initial_session_group="",
            initial_session_ui_mode="standard",
            initial_session_permissions_json='{"send_prompt": true}',
            initial_child_session_sharing_json='{"mode":"private","allowed_source_session_ids":[],"allowed_source_template_ids":[]}',
            initial_preferred_provider="codex",
            initial_agent_priority=[],
            initial_goal_manager_priority=[],
            initial_session_priority=0,
            initial_goal_manager_state="running",
            initial_agent_welcome_enabled=False,
            initial_welcomed_agents=[],
            initial_selected_agents=[],
            initial_session_window_seconds=86400,
            recent_messages_limit_max=200,
            initial_session_summaries_json="[]",
            initial_worker_counts_json="{}",
            initial_latest_user_prompt="",
            initial_latest_agent_reply="",
            session_nav_items="",
            goal_board_items="",
            sidebar_system_html="",
            codex_service_pool=["service-codex-001"],
            claude_service_pool=[],
            gemini_service_pool=[],
            items="",
        )

        self.assertIn("renderSessionStatusStrip", page)
        self.assertIn("composer-dock", page)
        self.assertIn("composer-dock-form", page)
        self.assertIn("list.replaceChildren(...timeline.map", page)
        self.assertIn("Send to the active ${providerLabel()} service", page)
        self.assertIn("unit-launcher-open-interface", page)
        self.assertIn("unitLauncherOpenInterface", page)
        self.assertIn("Latest Display", page)
        self.assertIn("goal-board-time-window", page)
        self.assertIn("session_window_seconds", page)
        self.assertIn("refreshSessionIndex(); refreshWorkspaceBoard();", page)
        self.assertNotIn("window.setInterval(() => { if (!document.hidden) refreshSessionIndex(); }, 7000);", page)
        self.assertIn(".goal-board-shell.unit-launcher-shell{overflow-y:auto;overflow-x:hidden;scroll-padding-bottom:24px}", page)
        self.assertIn(".unit-launcher-panel{display:flex;flex-direction:column;gap:12px;padding:14px;border:1px solid rgba(31,26,20,.09);border-radius:18px;background:rgba(255,253,248,.7);box-shadow:0 6px 18px rgba(31,26,20,.05);flex:0 0 auto;min-height:auto;overflow:visible}", page)
        self.assertIn(".unit-launcher-body{display:flex;flex-direction:column;gap:12px;min-height:0}", page)
        self.assertIn(".unit-launcher-head{display:flex;flex-direction:column;gap:4px;flex:0 0 auto}", page)
        self.assertIn(".unit-launcher-actions{position:sticky;bottom:0;display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:auto;padding-top:10px;padding-bottom:2px;background:linear-gradient(180deg,rgba(255,253,248,0) 0%,rgba(255,253,248,.94) 24%,rgba(255,253,248,.98) 100%)}", page)
        self.assertIn("<div id='apps-pane' class='pane-view pane-view-map is-hidden'><div class='goal-board-shell unit-launcher-shell'>", page)
        self.assertIn("<div class='unit-launcher-body'><div id='unit-launcher-list' class='unit-launcher-list'></div>", page)
        self.assertIn("id='unit-launcher-label' class='full' placeholder='Session label' aria-label='Session label'", page)
        self.assertIn("id='unit-launcher-goal' class='full' placeholder='Goal text' aria-label='Goal text'", page)
        self.assertIn("id='unit-launcher-prompt' class='full' placeholder='Initial prompt' aria-label='Initial prompt'", page)
        self.assertIn("id='unit-launcher-provider' aria-label='Preferred provider'", page)
        self.assertIn("id='goal-board-refresh-button' class='toolbar-button ghost goal-board-view-btn' type='button'>Latest Display</button>", page)
        self.assertIn("id='goal-board-time-window' class='provider-select' aria-label='Session time window'", page)
        self.assertIn("sessionMapWindowSeconds", page)
        self.assertIn("const patchWorkspaceSummaryFromEvent = (entry) => {", page)
        self.assertIn("patchWorkspaceSummaryFromEvent(entry);", page)
        self.assertIn("sessionWithinMapTimeWindow", page)
        self.assertNotIn("setInterval(() => { if (!document.hidden && sessionMapOpen) refreshWorkspaceBoard(); }, 5000)", page)
        self.assertIn("sessionRuntimeLabel", page)
        self.assertIn("sessionRuntimeBadgeClass", page)
        self.assertIn("Goal Active", page)
        self.assertIn("Goal In Progress", page)
        self.assertIn("Executing", page)
        self.assertIn("Runtime Idle", page)
        self.assertIn("All Clear", page)
        self.assertIn("runtime-journal-panel", page)
        self.assertIn("Runtime Event Log", page)
        self.assertIn("runtimeJournalSummaryText", page)
        self.assertIn("/session/runtime-log?session_id=", page)
        self.assertIn("renderRuntimeJournalMeta();", page)
        self.assertIn("eventsTitle.textContent = 'Event Log';", page)
        self.assertIn("rawHead.textContent = 'Raw JSON';", page)
        self.assertIn("raw.textContent = JSON.stringify(eventEntry.event, null, 2);", page)
        self.assertIn("raw.textContent = JSON.stringify(item, null, 2);", page)
        self.assertIn(".workspace-goal-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex:1 1 auto;min-width:0}", page)
        self.assertIn(".workspace-goal-heading textarea.goal-state-text", page)
        self.assertIn("<textarea id='workspace-goal-state-text' class='goal-state-text' readonly aria-label='Current goal text'>Ship the WorkspaceView refresh</textarea>", page)
        self.assertIn("workspaceGoalStateText instanceof HTMLTextAreaElement", page)
        self.assertNotIn("<button id='workspace-goal-toggle' class='workspace-goal-toggle' type='button'><div id='workspace-goal-state-text'", page)
        self.assertIn("workspaceGoalToggle.setAttribute('aria-expanded', workspaceGoalCollapsed ? 'false' : 'true');", page)
        self.assertIn("workspaceGoalToggle.setAttribute('aria-expanded', nextCollapsed ? 'false' : 'true');", page)
        self.assertIn("Goal Active' : 'Goal Inactive", page)
        self.assertIn("Goal Active' : 'Goal Inactive'} | ${goalProgressState === 'complete' ? 'Goal Completed' : 'Goal In Progress'", page)
        self.assertIn("allowed_source_unit_ids", page)
        self.assertIn("Only the listed sessions or units may create child sessions here.", page)
        self.assertIn("Allowed unit IDs", page)
        self.assertNotIn("Allowed template IDs", page)
        self.assertNotIn("UnitFiles", page)
        self.assertNotIn("UnitFile", page)
        self.assertNotIn("unit-launcher-open-plugin-ui", page)
        self.assertNotIn("unitLauncherOpenPluginUi", page)
        self.assertIn("id='session-status-strip' class='session-status-strip'", page)
        self.assertIn("<span class='session-status-chip is-active'>Goal Active</span>", page)
        self.assertIn("<span class='session-status-chip'>Goal In Progress</span>", page)
        self.assertIn("<span class='session-status-chip is-running'>Executing</span>", page)
        self.assertIn("<span class='session-status-chip is-audit-ok'>All Clear</span>", page)
        self.assertIn("id='composer-dock-meta' class='composer-dock-meta'", page)
        self.assertIn("<span class='composer-dock-chip is-active'>Goal Active</span>", page)
        self.assertNotIn("Workspace Header", page)

    def test_main_page_keeps_workspace_views_and_recent_messages_on_one_desktop_row(self) -> None:
        page = render_main_page(
            username="repyt",
            session_id="session-123",
            role_name="superuser",
            is_superuser=True,
            initial_session_scope="owned",
            display_name="AIze",
            default_target="service-codex-001",
            default_provider="codex",
            initial_session_map_open=False,
            entries_json="[]",
            initial_runtime_journal_summary_json="{}",
            context_status_json="{}",
            initial_auto_compact_threshold=20,
            initial_session_label="WorkspaceView",
            initial_goal_text="Ship the WorkspaceView refresh",
            initial_active_goal_id="goal-1",
            initial_goal_history_json="[]",
            initial_goal_active=True,
            initial_goal_completed=False,
            initial_goal_progress_state="in_progress",
            initial_goal_audit_state="all_clear",
            initial_bound_service_id="service-codex-001",
            default_httpbridge_recent_messages_limit=40,
            initial_goal_reset_completed_on_prompt=True,
            initial_goal_auto_compact_enabled=True,
            initial_auto_resume_enabled=False,
            initial_auto_resume_interval_seconds=0,
            initial_auto_resume_next_at="",
            initial_auto_resume_reason="",
            initial_user_response_wait_status="idle",
            initial_user_response_wait_active=False,
            initial_user_response_wait_timeout_seconds=0,
            initial_user_response_wait_effective_timeout_seconds=0,
            initial_user_response_wait_started_at="",
            initial_user_response_wait_until_at="",
            initial_user_response_wait_request_id="",
            initial_user_response_wait_prompt_text="",
            initial_user_response_wait_reason="",
            initial_user_response_wait_last_cleared_at="",
            initial_user_response_wait_last_timeout_at="",
            initial_session_group="",
            initial_session_ui_mode="standard",
            initial_session_permissions_json='{"send_prompt": true}',
            initial_child_session_sharing_json='{"mode":"private","allowed_source_session_ids":[],"allowed_source_template_ids":[]}',
            initial_preferred_provider="codex",
            initial_agent_priority=[],
            initial_goal_manager_priority=[],
            initial_session_priority=0,
            initial_goal_manager_state="running",
            initial_agent_welcome_enabled=False,
            initial_welcomed_agents=[],
            initial_selected_agents=[],
            initial_session_window_seconds=86400,
            recent_messages_limit_max=200,
            initial_session_summaries_json="[]",
            initial_worker_counts_json="{}",
            initial_latest_user_prompt="",
            initial_latest_agent_reply="",
            session_nav_items="",
            goal_board_items="",
            sidebar_system_html="",
            codex_service_pool=["service-codex-001"],
            claude_service_pool=[],
            gemini_service_pool=[],
            items="",
        )

        self.assertIn(".topbar-tools{position:relative;z-index:2;display:flex;flex-direction:row;", page)
        self.assertIn("justify-content:flex-end;gap:12px;flex-wrap:nowrap;", page)
        self.assertIn(".view-toolbar-group{flex:1 1 auto;min-width:0}", page)
        self.assertIn(".topbar-panel-group{flex-direction:row;align-items:center;gap:10px;padding:10px 12px}", page)
        self.assertIn(".topbar-panel-group .session-toolbar-actions{flex:1 1 auto;min-width:0;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin}", page)
        self.assertIn(".toolbar-button{white-space:nowrap}", page)
        self.assertIn(".recent-messages-control{display:flex;flex:0 0 auto;", page)
        self.assertIn(".recent-messages-form{display:flex;gap:8px;align-items:center;min-width:0;flex-wrap:nowrap;white-space:nowrap}", page)
        self.assertIn(".recent-messages-input{width:68px;min-width:68px;", page)
        self.assertIn(
            "<div class='view-toolbar-group topbar-panel-group'>",
            page,
        )
        self.assertIn(
            "<section class='recent-messages-control'>",
            page,
        )
        self.assertIn(
            "<form id='recent-messages-limit-form' class='recent-messages-form'><label class='recent-messages-inline-label' for='recent-messages-limit-input'><span class='thread-toolbar-label'>RecentMessages</span><span class='recent-messages-copy'>up to</span></label>",
            page,
        )
        self.assertNotIn("recent-messages-head", page)
        self.assertLess(
            page.index("<div class='view-toolbar-group topbar-panel-group'>"),
            page.index("<section class='recent-messages-control'>"),
        )
        self.assertIn(".topbar-tools{flex-direction:column;gap:8px;flex:none;width:100%;max-width:100%}", page)
        self.assertIn(".recent-messages-control{flex:none;min-width:0;max-width:100%;width:100%;align-self:stretch}", page)

    def test_communication_session_settings_detect_interactive_sessions(self) -> None:
        self.assertTrue(_is_communication_session_settings({"session_interactive": True}))
        self.assertTrue(_is_communication_session_settings({"communication_agent_enabled": True}))
        self.assertTrue(_is_communication_session_settings({"session_ui_mode": "communication"}))
        self.assertFalse(_is_communication_session_settings({"session_ui_mode": "standard"}))
        self.assertFalse(_is_communication_session_settings(None))

    def test_communication_chat_noise_filters_protocol_chatter(self) -> None:
        self.assertTrue(_is_communication_chat_noise({"event_type": "agent.turn_started"}))
        self.assertTrue(_is_communication_chat_noise({"event_type": "thread.started"}))
        self.assertTrue(_is_communication_chat_noise({"event_type": "turn.started"}))
        self.assertTrue(_is_communication_chat_noise({"text": "response started"}))
        self.assertTrue(_is_communication_chat_noise({"text": " Response Started "}))
        self.assertFalse(_is_communication_chat_noise({"direction": "in", "text": "actual reply"}))
        self.assertFalse(_is_communication_chat_noise({"direction": "out", "text": "user prompt"}))

    def test_matching_communication_skill_routes_prefers_explicit_default_route(self) -> None:
        current_session = {
            "session_skills": [
                {
                    "skill_id": "canonical-development-routing",
                    "routing_mode": "direct_session_template",
                    "route_when_unhandled": True,
                    "canonical_session_key": "aize.development",
                }
            ]
        }
        self.assertEqual(
            len(
                _matching_communication_skill_routes(
                    current_session,
                    prompt_text="この修正を行いたくて、AIze開発セッションの下で開発してください",
                )
            ),
            1,
        )
        self.assertEqual(
            len(_matching_communication_skill_routes(current_session, prompt_text="こんにちは")),
            1,
        )

    def test_matching_communication_skill_routes_launcher_template_does_not_auto_route_by_default(self) -> None:
        current_session = {
            "launcher_template_id": "entrance.service",
            "session_skills": [],
        }

        self.assertEqual(
            _matching_communication_skill_routes(
                current_session,
                prompt_text="Please fix the implementation routing.",
            ),
            [],
        )

    def test_matching_communication_skill_routes_ignores_tags_without_opt_in(self) -> None:
        current_session = {
            "session_skills": [
                {
                    "skill_id": "canonical-development-routing",
                    "routing_mode": "direct_session_template",
                    "routing_tags": ["development"],
                    "canonical_session_key": "aize.development",
                }
            ]
        }

        self.assertEqual(
            _matching_communication_skill_routes(current_session, prompt_text="development work"),
            [],
        )

    def test_infer_communication_forward_target_session_id_skips_direct_development_route(self) -> None:
        sessions = [
            {
                "session_id": "entrance",
                "label": "Entrance Verify Clean",
                "session_ui_mode": "communication",
                "communication_agent_enabled": True,
                "session_skills": [
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "direct_session_template",
                        "route_when_unhandled": True,
                        "routing_tags": ["development", "dev", "開発"],
                        "canonical_session_key": "aize.development",
                    }
                ],
            },
            {
                "session_id": "dev",
                "label": "AIze Development",
                "session_ui_mode": "standard",
                "communication_agent_enabled": False,
                "session_skills": [
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            },
        ]
        self.assertIsNone(
            _infer_communication_forward_target_session_id(
                sessions,
                current_session_id="entrance",
                prompt_text="この修正を行いたくて、AIze開発セッションの下で開発してください",
                current_session=sessions[0],
            )
        )

    def test_infer_communication_forward_target_session_id_returns_none_on_tie(self) -> None:
        sessions = [
            {
                "session_id": "entrance",
                "label": "Entrance",
                "session_ui_mode": "communication",
                "session_skills": [
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "direct_session_template",
                        "route_when_unhandled": True,
                        "routing_tags": ["development"],
                        "canonical_session_key": "aize.development",
                    }
                ],
            },
            {
                "session_id": "dev-a",
                "label": "Development A",
                "session_skills": [{"skill_id": "dev-a", "canonical_session_key": "aize.development"}],
            },
            {
                "session_id": "dev-b",
                "label": "Development B",
                "session_skills": [{"skill_id": "dev-b", "canonical_session_key": "aize.development"}],
            },
        ]
        self.assertIsNone(
            _infer_communication_forward_target_session_id(
                sessions,
                current_session_id="entrance",
                prompt_text="development session に送ってください",
                current_session=sessions[0],
            )
        )

    def test_materialize_communication_routed_child_session_creates_canonical_development_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            current_session = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["development", "dev", "開発"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_goal_text": "Implement the requested changes here.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                        "spawn_session_skills": [
                            {
                                "skill_id": "aize-development-session",
                                "canonical_session_key": "aize.development",
                            }
                        ],
                    }
                ],
            )

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=current_session,
                prompt_text="Please send this to the development session.",
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertEqual(str(routed.get("label") or ""), "Development Task")
            parent = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(routed.get("parent_session_id") or ""),
            )
            self.assertIsNotNone(parent)
            assert parent is not None
            self.assertEqual(str(parent.get("label") or ""), "AIze Development")
            self.assertEqual(routed["goal_text"], "Please send this to the development session.")
            self.assertEqual(
                load_session_skills(
                    runtime_root,
                    username="repyt",
                    session_id=str(routed["session_id"]),
                )[0]["canonical_session_key"],
                "aize.development",
            )

    def test_materialize_direct_development_route_launches_canonical_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "direct_session_template",
                        "route_when_unhandled": True,
                        "routing_tags": ["implementation", "fix"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_child_label": "AIze Development Task",
                        "target_goal_text": "Coordinate routed development work.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                    }
                ],
            )

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please implement the remaining routing fix.",
                sessions=list_sessions(runtime_root, username="repyt"),
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertEqual(str(routed.get("label") or ""), "AIze Development")
            self.assertEqual(routed["goal_text"], "Please implement the remaining routing fix.")
            stored = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(routed["session_id"]),
            )
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["launcher_template_id"], "aize-development.bug-hunting")
            self.assertEqual(stored["label"], "AIze Development")
            self.assertEqual(stored["parent_session_id"], "default")
            self.assertEqual(stored["session_group"], "root")
            self.assertEqual(list_session_children(runtime_root, username="repyt", session_id=str(stored["session_id"])), [])

    def test_materialize_communication_routed_child_session_ignores_noncanonical_development_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["development", "dev", "開発"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_goal_text": "Implement the requested changes here.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                        "spawn_session_skills": [
                            {
                                "skill_id": "aize-development-session",
                                "canonical_session_key": "aize.development",
                            }
                        ],
                    }
                ],
            )
            noncanonical = create_conversation_session(
                runtime_root,
                username="repyt",
                label="AIze Development",
                session_skills=[
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )
            noncanonical = update_session_goal(
                runtime_root,
                username="repyt",
                session_id=str(noncanonical["session_id"]),
                goal_text="No backward compatibility; implement directly in AIze Development.",
            )
            assert noncanonical is not None

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="開発してください。legacy compatibility は切ってください。",
                sessions=[entrance, noncanonical],
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertNotEqual(routed["parent_session_id"], noncanonical["session_id"])
            self.assertEqual(str(routed.get("label") or ""), "Development Task")
            self.assertEqual(routed["goal_text"], "開発してください。legacy compatibility は切ってください。")
            parent = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(routed["parent_session_id"]),
            )
            self.assertIsNotNone(parent)
            assert parent is not None
            self.assertEqual(parent["launcher_template_id"], "aize-development.bug-hunting")
            self.assertEqual(parent["parent_session_id"], "default")
            self.assertEqual(parent["session_group"], "root")
            self.assertIn("child of the Root session", str(parent.get("goal_text") or ""))

    def test_materialize_communication_routed_child_session_prefers_top_level_canonical_parent_when_multiple_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["development", "dev", "開発"],
                        "canonical_session_key": "aize.development",
                        "target_label": "AIze Development",
                        "target_goal_text": "Implement the requested changes here.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                    }
                ],
            )
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template(
                    "aize-development.bug-hunting",
                    default_provider="codex",
                )
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=unit,
                    label="AIze Development",
                )
            development_root = launched["session"]
            older_child = create_child_conversation_session(
                runtime_root,
                username="repyt",
                parent_session_id=str(development_root["session_id"]),
                label="Older routed task",
                goal_text="An earlier routed task.",
                created_by_username="repyt",
                created_by_type="user",
                session_skills=[
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )
            assert older_child is not None

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please route this into the development session.",
                sessions=[entrance, development_root, older_child],
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertEqual(routed["parent_session_id"], development_root["session_id"])
            self.assertEqual(routed["origin_session_id"], entrance["session_id"])

    def test_materialize_communication_routed_child_session_prefers_target_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            current_session = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["development", "dev", "開発"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_goal_text": "Implement the requested changes here.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                    }
                ],
            )

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=current_session,
                prompt_text="Please send this to the development session.",
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertEqual(str(routed.get("label") or ""), "Development Task")
            stored = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(routed["session_id"]),
            )
            self.assertIsNotNone(stored)
            assert stored is not None
            parent = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(stored["parent_session_id"]),
            )
            self.assertIsNotNone(parent)
            assert parent is not None
            self.assertEqual(parent["launcher_template_id"], "aize-development.bug-hunting")
            self.assertIn("child of the Root session", str(parent.get("goal_text") or ""))
            self.assertEqual(parent["parent_session_id"], "default")
            self.assertEqual(parent["session_group"], "root")
            self.assertEqual(parent["session_ui_mode"], "standard")
            self.assertFalse(bool(parent.get("communication_agent_enabled", False)))
            self.assertEqual(stored["goal_text"], "Please send this to the development session.")
            self.assertEqual(stored["selected_agents"], ["codex_pool"])
            skills = load_session_skills(
                runtime_root,
                username="repyt",
                session_id=str(routed["session_id"]),
            )
            self.assertEqual(skills[0]["canonical_session_key"], "aize.development")

    def test_materialize_launcher_route_does_not_auto_delegate_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[],
            )
            entrance["launcher_template_id"] = "entrance.service"

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please implement this development routing fix.",
                sessions=list_sessions(runtime_root, username="repyt"),
            )

            self.assertIsNone(routed)

    def test_materialize_communication_routed_child_session_reuses_registered_parent_for_parallel_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["implementation", "fix"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_child_label": "AIze Development Task",
                        "target_goal_text": "Coordinate routed development work.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                    }
                ],
            )

            first = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please implement the routing fix.",
            )
            sessions_after_first = list_sessions(runtime_root, username="repyt")
            second = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please fix the child materialization test.",
                sessions=sessions_after_first,
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertEqual(first["parent_session_id"], second["parent_session_id"])
            self.assertNotEqual(first["session_id"], second["session_id"])
            parent = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(first["parent_session_id"]),
            )
            self.assertIsNotNone(parent)
            assert parent is not None
            self.assertEqual(parent["launcher_template_id"], "aize-development.bug-hunting")
            self.assertEqual(parent["session_ui_mode"], "standard")
            self.assertFalse(bool(parent.get("communication_agent_enabled", False)))

    def test_materialize_communication_routed_child_session_prefers_registered_bug_hunting_parent_over_existing_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["implementation", "fix"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_child_label": "AIze Development Task",
                        "target_goal_text": "Coordinate routed development work.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                    }
                ],
            )
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template(
                    "aize-development.bug-hunting",
                    default_provider="codex",
                )
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=unit,
                    label="AIze Bug Hunting",
                )
            development_parent = launched["session"]
            existing_child = create_child_conversation_session(
                runtime_root,
                username="repyt",
                parent_session_id=str(development_parent["session_id"]),
                label="Existing Task",
                goal_text="Fix the earlier bug.",
                created_by_username="repyt",
                created_by_type="user",
                session_skills=[
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )
            assert existing_child is not None

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please fix the next bug under AIze Development.",
                sessions=list_sessions(runtime_root, username="repyt"),
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertEqual(routed["parent_session_id"], development_parent["session_id"])
            self.assertNotEqual(routed["parent_session_id"], existing_child["session_id"])
            parent = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=str(routed["parent_session_id"]),
            )
            self.assertIsNotNone(parent)
            assert parent is not None
            self.assertEqual(parent["launcher_template_id"], "aize-development.bug-hunting")

    def test_materialize_communication_routed_child_session_prefers_registered_parent_even_when_parent_goal_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                        "routing_tags": ["implementation", "fix"],
                        "canonical_session_key": "aize.development",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_child_label": "AIze Development Task",
                        "target_goal_text": "Coordinate routed development work.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                    }
                ],
            )
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template(
                    "aize-development.bug-hunting",
                    default_provider="codex",
                )
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=unit,
                    label="AIze Bug Hunting",
                )
            development_parent = launched["session"]
            updated_parent = update_session_goal_flags(
                runtime_root,
                username="repyt",
                session_id=str(development_parent["session_id"]),
                goal_active=False,
                goal_completed=True,
                goal_progress_state="complete",
            )
            self.assertIsNotNone(updated_parent)

            active_child = create_child_conversation_session(
                runtime_root,
                username="repyt",
                parent_session_id=str(development_parent["session_id"]),
                label="Existing Task",
                goal_text="Fix the earlier bug.",
                created_by_username="repyt",
                created_by_type="user",
                session_skills=[
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )
            assert active_child is not None

            routed = _materialize_communication_routed_child_session(
                runtime_root,
                username="repyt",
                current_session=entrance,
                prompt_text="Please fix the next bug under AIze Development.",
                sessions=list_sessions(runtime_root, username="repyt"),
            )

            self.assertIsNotNone(routed)
            assert routed is not None
            self.assertEqual(routed["parent_session_id"], development_parent["session_id"])
            self.assertNotEqual(routed["parent_session_id"], active_child["session_id"])

    def test_forwarded_session_pending_input_uses_goal_feedback_for_goal_session(self) -> None:
        pending_input, dispatch_reason = _forwarded_session_pending_input(
            {
                "session_id": "dev",
                "goal_text": "Fix the routing bug",
                "goal_active": True,
            },
            prompt_text="HTTPBridge routing is still wrong. Keep fixing it.",
            submitted_by_username="repyt",
        )

        self.assertEqual(dispatch_reason, "goal_feedback")
        self.assertEqual(pending_input["kind"], "goal_feedback")
        self.assertEqual(pending_input["role"], "system")
        self.assertIn("<aize_goal_feedback>", pending_input["text"])
        self.assertIn("HTTPBridge routing is still wrong", pending_input["text"])

    def test_forwarded_session_pending_input_uses_user_message_without_goal(self) -> None:
        pending_input, dispatch_reason = _forwarded_session_pending_input(
            {
                "session_id": "dev",
                "goal_text": "",
                "goal_active": False,
            },
            prompt_text="Please inspect this session.",
            submitted_by_username="repyt",
            user_response_request_ids=["req-1"],
        )

        self.assertEqual(dispatch_reason, "http_prompt")
        self.assertEqual(pending_input["kind"], "user_message")
        self.assertEqual(pending_input["role"], "user")
        self.assertEqual(pending_input["text"], "Please inspect this session.")
        self.assertEqual(pending_input["user_response_request_ids"], ["req-1"])

    def test_select_communication_worker_service_id_prefers_other_pool_member(self) -> None:
        self.assertEqual(
            _select_communication_worker_service_id("service-codex-001", ["service-codex-001", "service-codex-002"]),
            "service-codex-002",
        )
        self.assertEqual(
            _select_communication_worker_service_id("service-codex-001", ["service-codex-001"]),
            "service-codex-001",
        )

    def test_communication_dispatch_plan_keeps_worker_when_prompt_is_forwarded(self) -> None:
        plan = _communication_dispatch_plan(
            session_id="entrance",
            interactive_service_id="service-codex-001",
            worker_service_id="service-codex-002",
            goal_manager_service_id="service-codex-003",
            forwarded_session_id="dev-session",
            forwarded_service_id="service-codex-004",
            forwarded_dispatch_reason="goal_feedback",
        )

        self.assertEqual(
            plan,
            [
                {
                    "channel": "interactive",
                    "service_id": "service-codex-001",
                    "session_id": "entrance",
                    "reason": "http_user_dialogue",
                },
                {
                    "channel": "worker",
                    "service_id": "service-codex-002",
                    "session_id": "entrance",
                    "reason": "interactive_worker_request",
                },
                {
                    "channel": "goal_manager",
                    "service_id": "service-codex-003",
                    "session_id": "entrance",
                    "reason": "goal_manager_review",
                },
                {
                    "channel": "forwarded",
                    "service_id": "service-codex-004",
                    "session_id": "dev-session",
                    "reason": "goal_feedback",
                },
            ],
        )

    def test_communication_dispatch_plan_keeps_worker_without_forwarded_route(self) -> None:
        plan = _communication_dispatch_plan(
            session_id="entrance",
            interactive_service_id="service-codex-001",
            worker_service_id="service-codex-002",
            goal_manager_service_id="service-codex-003",
            forwarded_session_id="",
            forwarded_service_id="",
            forwarded_dispatch_reason="",
        )

        self.assertEqual(
            [step["channel"] for step in plan],
            ["interactive", "worker", "goal_manager"],
        )

    def test_interactive_worker_result_resumes_original_interactive_service(self) -> None:
        target_service_id, target_agent_id = _interactive_worker_resume_target(
            {
                "interactive_service_id": "service-codex-001",
                "interactive_agent_id": "service-codex-001@@entrance@@interactive_agent",
            },
            fallback_service_id="service-codex-002",
            session_id="entrance",
        )

        self.assertEqual(target_service_id, "service-codex-001")
        self.assertEqual(target_agent_id, "service-codex-001@@entrance@@interactive_agent")

    def test_interactive_worker_result_falls_back_to_worker_for_old_pending_items(self) -> None:
        target_service_id, target_agent_id = _interactive_worker_resume_target(
            {},
            fallback_service_id="service-codex-002",
            session_id="entrance",
        )

        self.assertEqual(target_service_id, "service-codex-002")
        self.assertEqual(target_agent_id, "service-codex-002@@entrance@@interactive_agent")

    def test_interactive_dispatch_without_explicit_agent_id_uses_interactive_slot_queue(self) -> None:
        self.assertEqual(
            _dispatch_target_agent_id(
                None,
                runtime_root=ROOT,
                username="repyt",
                session_id="entrance",
                service_id="service-codex-001",
                provider_session_slot="interactive_agent",
            ),
            "service-codex-001@@entrance@@interactive_agent",
        )

    def test_post_turn_followup_pending_state_includes_service_slot_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            agent_id = "service-codex-001@@entrance@@interactive_agent"
            append_service_pending_input(
                runtime_root,
                service_id="service-codex-001",
                agent_id=agent_id,
                username="repyt",
                session_id="entrance",
                entry={"kind": "interactive_worker_result", "text": "worker replied"},
            )

            (
                followup_agent_id,
                session_pending_inputs,
                service_pending_inputs,
                has_actionable_pending,
            ) = _post_turn_followup_pending_state(
                runtime_root,
                username="repyt",
                session_id="entrance",
                service_id="service-codex-001",
                provider_session_slot="interactive_agent",
            )

            self.assertEqual(followup_agent_id, agent_id)
            self.assertEqual(session_pending_inputs, [])
            self.assertEqual(len(service_pending_inputs), 1)
            self.assertEqual(service_pending_inputs[0]["kind"], "interactive_worker_result")
            self.assertTrue(has_actionable_pending)

    def test_interactive_worker_result_fallback_text_extracts_worker_result(self) -> None:
        resume_xml = _interactive_resume_xml(
            request_id="req-1",
            source_user_text="おはよ",
            worker_text="Worker says <done> & ready.",
        )

        self.assertEqual(
            _interactive_worker_result_fallback_text(resume_xml),
            "Worker says <done> & ready.",
        )

    def test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event(self) -> None:
        history = [
            {
                "direction": "out",
                "ts": "2026-05-14T13:48:45Z",
                "text": "Please fix the Entrance duplication.",
            },
            {
                "direction": "in",
                "ts": "2026-05-14T13:48:50Z",
                "service_id": "service-codex-001",
                "from": "service-codex-001",
                "text": '{"assistant_text":"I fixed the duplicate output."}',
            },
            {
                "direction": "agent",
                "ts": "2026-05-14T13:48:51Z",
                "service_id": "service-codex-001",
                "from": "service-codex-001",
                "event_type": "item.completed",
                "text": "I fixed the duplicate output.",
                "event": {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I fixed the duplicate output."},
                },
            },
        ]

        collapsed = _collapse_communication_duplicate_outputs(history)

        self.assertEqual(len(collapsed), 2)
        self.assertEqual([entry.get("direction") for entry in collapsed], ["out", "in"])
        self.assertEqual(
            collapsed[-1]["text"],
            '{"assistant_text":"I fixed the duplicate output."}',
        )

    def test_communication_goal_cycle_completes_only_on_visible_reply(self) -> None:
        session_settings = {
            "communication_agent_enabled": True,
            "goal_completion_policy": "per_prompt",
            "goal_text": "Act as Entrance",
            "goal_active": True,
        }

        self.assertTrue(
            _should_complete_communication_goal_after_reply(
                session_settings,
                visible_text="Forwarded to AIze Development and queued a review.",
            )
        )
        self.assertFalse(
            _should_complete_communication_goal_after_reply(
                session_settings,
                visible_text="",
            )
        )
        self.assertFalse(
            _should_complete_communication_goal_after_reply(
                {
                    **session_settings,
                    "goal_completion_policy": "continuous",
                },
                visible_text="Entrance is still watching for user work.",
            )
        )

    def test_goal_manager_review_preserves_prompt_cycle_completion(self) -> None:
        session_settings = {
            "communication_agent_enabled": True,
            "goal_completion_policy": "per_prompt",
        }

        self.assertTrue(
            _should_preserve_prompt_cycle_progress_during_goal_review(
                session_settings,
                audit_progress_state="in_progress",
                resolved_audit_state="all_clear",
            )
        )
        self.assertFalse(
            _should_preserve_prompt_cycle_progress_during_goal_review(
                session_settings,
                audit_progress_state="complete",
                resolved_audit_state="all_clear",
            )
        )


if __name__ == "__main__":
    unittest.main()
