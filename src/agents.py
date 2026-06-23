from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentResult:
    provider: str
    output: str
    resume_token: str | None = None


class AgentRunner:
    def __init__(self, *, external_enabled: bool | None = None) -> None:
        if external_enabled is None:
            external_enabled = os.environ.get("AIZE_ENABLE_EXTERNAL_AGENTS", "true").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        self.external_enabled = external_enabled

    def run(
        self,
        provider: str,
        *,
        role: str,
        prompt: str,
        resume_token: str | None = None,
        runtime_env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> AgentResult:
        normalized = provider.strip().lower()
        if normalized == "local":
            return AgentResult(
                provider=normalized,
                output=self._run_local(role=role, prompt=prompt, resume_token=resume_token),
                resume_token=resume_token,
            )
        if normalized == "remote-aize":
            if role != "WorkerAgent":
                raise AgentError("remote-aize provider is only allowed for WorkerAgent")
            return AgentResult(
                provider=normalized,
                output=(
                    '<aize-output role="WorkerAgent" provider="remote-aize">\n'
                    "WorkerAgent handoff queued for remote AIze via message passing. "
                    "No local filesystem exchange was performed directly.\n\n"
                    f"{prompt}\n"
                    "</aize-output>"
                ),
                resume_token=resume_token,
            )
        if normalized in {"codex", "claude"}:
            if not self.external_enabled:
                status_line = ""
                if role == "GoalManager" and self._prompt_has_phase(prompt, "review"):
                    status_line = "\nAIZE_GOAL_STATUS: completed\nAIZE_GOAL_REASON: local disabled-provider review finished"
                return AgentResult(
                    provider=normalized,
                    output=(
                        f'<aize-output role="{role}" provider="{normalized}">\n'
                        f"{normalized} provider is assigned but external execution is disabled. "
                        "The session agent thread was still resumed in durable state."
                        f"{status_line}\n\n{prompt}\n"
                        "</aize-output>"
                    ),
                    resume_token=resume_token,
                )
            return AgentResult(
                provider=normalized,
                output=self._run_external(
                    normalized,
                    prompt=prompt,
                    resume_token=resume_token,
                    runtime_env=runtime_env,
                    cwd=cwd,
                ),
                resume_token=resume_token,
            )
        raise AgentError(f"unsupported agent provider: {provider}")

    def _run_local(self, *, role: str, prompt: str, resume_token: str | None) -> str:
        thread = f"\nThread: {resume_token}" if resume_token else ""
        if role == "GoalManager" and self._prompt_has_phase(prompt, "review"):
            return (
                f'<aize-output role="{role}" provider="local">\n'
                f"{role} handled prompt locally.{thread}\n"
                "AIZE_GOAL_STATUS: completed\n"
                "AIZE_GOAL_REASON: local review finished\n\n"
                f"{prompt}\n"
                "</aize-output>"
            )
        return (
            f'<aize-output role="{role}" provider="local">\n'
            f"{role} handled prompt locally.{thread}\n\n{prompt}\n"
            "</aize-output>"
        )

    def _prompt_has_phase(self, prompt: str, phase: str) -> bool:
        return f"Phase: {phase}" in prompt or f'phase="{phase}"' in prompt

    def _run_external(
        self,
        provider: str,
        *,
        prompt: str,
        resume_token: str | None,
        runtime_env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> str:
        executable = shutil.which(provider)
        if not executable:
            raise AgentError(f"{provider} executable not found")
        resumed_prompt = prompt
        if resume_token:
            resumed_prompt = f"Resume durable AIze agent thread: {resume_token}\n\n{prompt}"
        if provider == "codex":
            command = [
                executable,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "danger-full-access",
                "--dangerously-bypass-approvals-and-sandbox",
                resumed_prompt,
            ]
        else:
            command = [executable, "-p", resumed_prompt]
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
            env={**os.environ, **(runtime_env or {})},
        )
        output = completed.stdout.strip()
        error = completed.stderr.strip()
        if completed.returncode != 0:
            raise AgentError(f"{provider} failed with exit code {completed.returncode}: {error}")
        if error:
            return f"{output}\n\nstderr:\n{error}".strip()
        return output
