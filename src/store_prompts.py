from __future__ import annotations

import json
import re
from typing import Any

from envelope import render_message_bundle, xml_text


class PromptMixin:
    def _extract_goal_manager_status(self, output: str) -> str:
        marker = "AIZE_GOAL_STATUS:"
        if marker in output:
            after_marker = output.split(marker, 1)[1]
            status = after_marker.splitlines()[0].strip().lower()
            status = status.split("<", 1)[0].strip()
            if status in {"completed", "incomplete", "ready"}:
                return status
        xml_match = re.search(
            r"<AIZE_GOAL_STATUS>\s*([^<\s]+)\s*</AIZE_GOAL_STATUS>",
            output,
            flags=re.IGNORECASE,
        )
        if xml_match:
            status = xml_match.group(1).strip().lower()
            if status in {"completed", "incomplete", "ready"}:
                return status
        return "incomplete"

    def _extract_goal_manager_reason(self, output: str) -> str:
        marker = "AIZE_GOAL_REASON:"
        if marker in output:
            after_marker = output.split(marker, 1)[1]
            reason = after_marker.splitlines()[0].strip()
            reason = reason.split("</", 1)[0].strip()
            if reason:
                return reason
        xml_match = re.search(
            r"<AIZE_GOAL_REASON>\s*(.*?)\s*</AIZE_GOAL_REASON>",
            output,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if xml_match:
            reason = " ".join(xml_match.group(1).split())
            if reason:
                return reason[:240]
        for line in output.splitlines():
            normalized = line.strip()
            if not normalized or normalized.startswith("<"):
                continue
            if normalized.startswith("AIZE_GOAL_STATUS:"):
                continue
            return normalized[:240]
        return "GoalManager did not provide a reason."

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
        worker_output: str = "",
        recovery_context: str = "",
    ) -> str:
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
            "  <session-messages>",
            render_message_bundle(session_messages or []),
            "  </session-messages>",
            "  <dispatch-feed>",
            render_message_bundle(dispatch_messages or []),
            "  </dispatch-feed>",
            "  <system-signals>",
            xml_text(json.dumps(dispatch_signals or [], ensure_ascii=False, sort_keys=True)),
            "  </system-signals>",
            "  <python-message-api>",
            "    Use Python functions from agent_api for AIze message passing.",
            "    Available functions: send_user_console_message(body), send_session_message(body), send_worker_request(body), set_next_unit_run_at(next_run_at, note='').",
            "    send_worker_request(body) records a Worker request as a Session Message; Session dispatch connects it to WorkerAgent.",
            "    For a scheduled Unit, call set_next_unit_run_at(...) with a future UTC timestamp before completing the SessionGoal when the Unit has no future next_run_at.",
            "    GoalManager is the only role that may send user-facing console replies and request WorkerAgent work.",
            "    Do not use stdout as a user-facing response channel; stdout is execution log only.",
            "  </python-message-api>",
            "  <role-policy>",
            "    GoalManager verifies the SessionGoal state, checks whether work can proceed, and decides completion.",
            "    GoalManager should delegate implementation and concrete task work by writing Worker requests to Session.",
            "    If the goal is incomplete, write AIZE_GOAL_REASON as an actionable WorkerAgent instruction.",
            "    Runtime treats an incomplete GoalManager result without an explicit Worker request as an implicit WorkerAgent request.",
            "    GoalManager should not perform implementation work itself unless it is only a minimal verification needed to make the completion decision.",
            "  </role-policy>",
        ]
        if run_messages is not None:
            prompt.extend(
                [
                    "  <dispatch-messages-this-run>",
                    render_message_bundle(run_messages or []),
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
                "  <output-format>Return an aize-output XML block and include AIZE_GOAL_STATUS: completed or incomplete plus AIZE_GOAL_REASON: one concise actionable reason/instruction.</output-format>",
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
        recovery_context: str = "",
    ) -> str:
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
            "  <session-messages>",
            render_message_bundle(session_messages or []),
            "  </session-messages>",
            "  <dispatch-feed>",
            render_message_bundle(dispatch_messages or []),
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
