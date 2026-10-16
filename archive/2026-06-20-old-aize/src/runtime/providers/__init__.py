from .claude import run_claude, run_claude_compaction, run_claude_context_check
from .codex import run_codex, run_codex_compaction, run_codex_context_check
from .gemini import run_gemini, run_gemini_compaction, run_gemini_context_check

CONTEXT_COMPACTION_SUPPORTED_PROVIDERS = {"codex", "claude", "gemini"}


def provider_supports_context_compaction(provider_kind: str) -> bool:
    return str(provider_kind or "").strip().lower() in CONTEXT_COMPACTION_SUPPORTED_PROVIDERS


__all__ = [
    "CONTEXT_COMPACTION_SUPPORTED_PROVIDERS",
    "provider_supports_context_compaction",
    "run_claude",
    "run_claude_compaction",
    "run_claude_context_check",
    "run_codex",
    "run_codex_compaction",
    "run_codex_context_check",
    "run_gemini",
    "run_gemini_compaction",
    "run_gemini_context_check",
]
