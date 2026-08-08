from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from envelope import render_message_bundle, xml_text


class PromptMixin:
    def _agent_cli_prefix(self) -> str:
        src_root = Path(__file__).resolve().parent
        return f"PYTHONPATH={src_root} python3 -m cli --root {self.root}"

    def _summarize_messages_for_prompt(
        self,
        messages: list[dict[str, Any]] | None,
        *,
        limit: int = 8,
        body_limit: int = 700,
    ) -> list[dict[str, Any]]:
        summarized: list[dict[str, Any]] = []
        for message in list(messages or [])[-limit:]:
            payload = message.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            summary_payload: dict[str, Any] = {}
            for key in (
                "body",
                "text",
                "user_input",
                "worker_request",
                "worker_role",
                "worker_followup",
                "implicit_worker_request",
                "schedule_update",
                "next_run_at",
                "run_id",
                "reply_to",
            ):
                if key not in payload:
                    continue
                value = payload[key]
                if isinstance(value, str) and len(value) > body_limit:
                    value = f"{value[:body_limit]}...[truncated; use CLI history access for full payload]"
                summary_payload[key] = value
            summarized.append(
                {
                    "message_id": message.get("message_id", ""),
                    "from": message.get("from", ""),
                    "to": message.get("to", ""),
                    "created_at": message.get("created_at", ""),
                    "payload": summary_payload,
                }
            )
        return summarized

    def _render_history_access_block(
        self,
        session: dict[str, Any],
        *,
        role: str,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> list[str]:
        session_id = str(session["session_id"])
        cli = self._agent_cli_prefix()
        log_range = ""
        if from_seq is not None:
            log_range += f" --from {int(from_seq)}"
        if to_seq is not None:
            log_range += f" --to {int(to_seq)}"
        return [
            "  <history-access>",
            "    Full Session history is intentionally not embedded in this prompt.",
            "    Pull only the needed history with these CLI commands when more context is required.",
            f"    <session-log-window>{xml_text(cli)} session-log {xml_text(session_id)}{xml_text(log_range)} --limit 0</session-log-window>",
            f"    <session-log-after-cursor>{xml_text(cli)} session-log {xml_text(session_id)} --role {xml_text(role)} --after-cursor --limit 0</session-log-after-cursor>",
            f"    <messages-tail>{xml_text(cli)} messages {xml_text(session_id)} --limit 20</messages-tail>",
            f"    <messages-all>{xml_text(cli)} messages {xml_text(session_id)} --limit 0</messages-all>",
            f"    <dispatch-runs>{xml_text(cli)} dispatch-runs {xml_text(session_id)}</dispatch-runs>",
            "  </history-access>",
        ]

    def _render_goal_manager_prompt(
        self,
        goal: dict[str, Any],
        session: dict[str, Any],
        unit: dict[str, Any] | None,
        *,
        phase: str,
        session_messages: list[dict[str, Any]] | None = None,
        dispatch_messages: list[dict[str, Any]] | None = None,
        dispatch_signals: list[dict[str, Any]] | None = None,
        run_messages: list[dict[str, Any]] | None = None,
        log_window: dict[str, Any] | None = None,
        worker_output: str = "",
        recovery_context: str = "",
    ) -> str:
        from_seq = (log_window or {}).get("from_log_seq")
        to_seq = (log_window or {}).get("to_log_seq")
        prompt = [
            f'<aize-agent-input role="GoalManager" phase="{xml_text(phase)}">',
            "  <session>",
            f"    <id>{xml_text(session['session_id'])}</id>",
            f"    <title>{xml_text(session.get('title') or '')}</title>",
            f"    <unit>{xml_text((unit or {}).get('unit_id') or '')}</unit>",
            "  </session>",
            "  <unit-schedule>",
            xml_text(json.dumps((unit or {}).get("schedule") or {}, ensure_ascii=False, sort_keys=True)),
            "  </unit-schedule>",
            "  <session-capabilities>",
            xml_text(json.dumps(session.get("capabilities") or {}, ensure_ascii=False, sort_keys=True)),
            "  </session-capabilities>",
            "  <session-goal>",
            f"    <body>{xml_text(goal.get('body') or '')}</body>",
            "  </session-goal>",
            *self._render_history_access_block(session, role="GoalManager", from_seq=from_seq, to_seq=to_seq),
            "  <session-messages>",
            render_message_bundle(self._summarize_messages_for_prompt(session_messages, limit=5)),
            "  </session-messages>",
            "  <dispatch-feed>",
            render_message_bundle(self._summarize_messages_for_prompt(dispatch_messages, limit=10)),
            "  </dispatch-feed>",
            "  <system-signals>",
            xml_text(json.dumps(dispatch_signals or [], ensure_ascii=False, sort_keys=True)),
            "  </system-signals>",
            "  <python-message-api>",
            "    Use Python functions from agent_api for AIze message passing.",
            "    Available functions: send_user_console_message(body), send_session_message(body), send_worker_request(body), set_goal_completion_state(completion_state, reason, schedule_parameters=None), schedule_next_unit_run(parameters=None).",
            "    send_worker_request(body) records a Worker request as a Session Message; Session dispatch connects it to WorkerAgent.",
            "    For a scheduled Unit, pass call-time schedule_parameters when declaring completion. AIze combines them with the Unit's fixed schedule parameters and persisted Session timing context.",
            "    Resolvers that use completed_at must run through set_goal_completion_state('complete', ..., schedule_parameters=...).",
            "    GoalManager is the only role that may send user-facing console replies and request WorkerAgent work.",
            "    Do not use stdout as a user-facing response channel; stdout is execution log only.",
            "  </python-message-api>",
            "  <role-policy>",
            "    GoalManager verifies the SessionGoal state, checks whether work can proceed, and decides completion.",
            "    GoalManager should delegate implementation and concrete task work by writing Worker requests to Session.",
            "    Always call set_goal_completion_state('complete' or 'incomplete', reason) before exiting.",
            "    If incomplete, make its reason an actionable WorkerAgent instruction.",
            "    Runtime treats an incomplete decision without an explicit Worker request as an implicit WorkerAgent request.",
            "    GoalManager should not perform implementation work itself unless it is only a minimal verification needed to make the completion decision.",
            "  </role-policy>",
        ]
        if run_messages is not None:
            prompt.extend(
                [
                    "  <dispatch-messages-this-run>",
                    render_message_bundle(self._summarize_messages_for_prompt(run_messages, limit=5)),
                    "  </dispatch-messages-this-run>",
                ]
            )
        if recovery_context:
            prompt.extend(["  <recovery-context>", xml_text(recovery_context), "  </recovery-context>"])
        if worker_output:
            prompt.extend(["  <worker-output>", xml_text(worker_output), "  </worker-output>"])
        prompt.extend(
            [
                "  <instruction>Decide whether the SessionGoal can proceed or is completed.</instruction>",
                "  <output-format>Use the Python API for all state changes. stdout is only a concise diagnostic summary.</output-format>",
                "</aize-agent-input>",
            ]
        )
        return "\n".join(prompt)

    def _render_worker_prompt(
        self,
        goal: dict[str, Any],
        session: dict[str, Any],
        unit: dict[str, Any] | None,
        *,
        session_messages: list[dict[str, Any]] | None = None,
        dispatch_messages: list[dict[str, Any]] | None = None,
        dispatch_signals: list[dict[str, Any]] | None = None,
        log_window: dict[str, Any] | None = None,
        recovery_context: str = "",
    ) -> str:
        from_seq = (log_window or {}).get("from_log_seq")
        to_seq = (log_window or {}).get("to_log_seq")
        prompt = [
            '<aize-agent-input role="WorkerAgent" phase="work">',
            "  <session>",
            f"    <id>{xml_text(session['session_id'])}</id>",
            f"    <title>{xml_text(session.get('title') or '')}</title>",
            f"    <unit>{xml_text((unit or {}).get('unit_id') or '')}</unit>",
            "  </session>",
            "  <session-capabilities>",
            xml_text(json.dumps(session.get("capabilities") or {}, ensure_ascii=False, sort_keys=True)),
            "  </session-capabilities>",
            "  <session-goal>",
            f"    <body>{xml_text(goal.get('body') or '')}</body>",
            "  </session-goal>",
            *self._render_history_access_block(session, role="WorkerAgent", from_seq=from_seq, to_seq=to_seq),
            "  <session-messages>",
            render_message_bundle(self._summarize_messages_for_prompt(session_messages, limit=5)),
            "  </session-messages>",
            "  <dispatch-feed>",
            render_message_bundle(self._summarize_messages_for_prompt(dispatch_messages, limit=10)),
            "  </dispatch-feed>",
            "  <system-signals>",
            xml_text(json.dumps(dispatch_signals or [], ensure_ascii=False, sort_keys=True)),
            "  </system-signals>",
            "  <python-message-api>",
            "    Use Python functions from agent_api for AIze message passing.",
            "    Available functions: send_session_message(body).",
            "    WorkerAgent must report execution progress and results to Session only.",
            "    WorkerAgent may not message GoalManager directly, decide SessionGoal completion, or send user-facing console replies.",
            "    Do not use stdout as a user-facing response channel; stdout is execution log only.",
            "  </python-message-api>",
        ]
        if recovery_context:
            prompt.extend(["  <recovery-context>", xml_text(recovery_context), "  </recovery-context>"])
        prompt.extend(
            [
                "  <instruction>Work toward the SessionGoal by using the Python AIze message API and report the result.</instruction>",
                "  <output-format>Return only a concise execution summary on stdout after sending AIze messages through the Python API.</output-format>",
                "</aize-agent-input>",
            ]
        )
        return "\n".join(prompt)
