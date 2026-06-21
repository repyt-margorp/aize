"""Gemini LLM service handler."""
from runtime.agent_service import run_agent_service


def run_service(**kwargs) -> int:
    return run_agent_service(**kwargs)
