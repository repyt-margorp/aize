from __future__ import annotations

import json
import re
from typing import Any


def build_schema_instructions(schema_id: str) -> str:
    if schema_id != "service_control_v1":
        raise RuntimeError(f"unsupported response schema: {schema_id}")
    return "\n".join(
        [
            "Return only valid JSON matching response schema service_control_v1.",
            'Use "assistant_text" only when you have user-visible output worth showing now: concrete progress made, a result, a blocker with evidence, or a direct answer.',
            'Do not use "assistant_text" for plan-only, intent-only, acknowledgment-only, or "I will continue" status replies. In those cases, return an empty string.',
            'Use "spawn_requests" for new service creation requests.',
            'Each spawn_requests item must be exactly {"service": {...}, "allowed_peers": [...], "initial_prompt": "..."}; do not invent keys like "service_type", "goal", or "context".',
            'The "service" object must include exactly: "service_id", "kind", "display_name", "persona", "response_schema_id", and "max_turns".',
            'Example: {"assistant_text":"","spawn_requests":[{"service":{"service_id":"service-codex-helper","kind":"codex","display_name":"Helper","persona":"You are a focused helper.","response_schema_id":"service_control_v1","max_turns":4},"allowed_peers":["service-codex-001"],"initial_prompt":"Do the delegated task and report back."}]}',
            'If no services should be created, return "spawn_requests": [].',
            "Do not wrap the JSON in markdown fences.",
        ]
    )


def validate_service_control_v1(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError("schema output must be a JSON object")
    if set(data.keys()) != {"assistant_text", "spawn_requests"}:
        raise RuntimeError("schema output must contain only assistant_text and spawn_requests")

    assistant_text = data.get("assistant_text")
    spawn_requests = data.get("spawn_requests")
    if not isinstance(assistant_text, str):
        raise RuntimeError("assistant_text must be a string")
    if not isinstance(spawn_requests, list):
        raise RuntimeError("spawn_requests must be an array")

    validated_requests: list[dict[str, Any]] = []
    for item in spawn_requests:
        if not isinstance(item, dict):
            raise RuntimeError("spawn_requests items must be objects")
        if set(item.keys()) != {"service", "allowed_peers", "initial_prompt"}:
            raise RuntimeError("spawn_requests items must contain only service, allowed_peers, initial_prompt")

        service = item.get("service")
        allowed_peers = item.get("allowed_peers")
        initial_prompt = item.get("initial_prompt")
        if not isinstance(service, dict):
            raise RuntimeError("spawn request service must be an object")

        required_service_keys = {
            "service_id",
            "kind",
            "display_name",
            "persona",
            "max_turns",
            "response_schema_id",
        }
        if not required_service_keys.issubset(service.keys()):
            raise RuntimeError("spawn request service is missing required fields")
        if not set(service.keys()).issubset(required_service_keys):
            raise RuntimeError("spawn request service has unsupported fields")
        if not isinstance(service["service_id"], str):
            raise RuntimeError("service_id must be a string")
        if not isinstance(service["kind"], str):
            raise RuntimeError("kind must be a string")
        if not isinstance(service["display_name"], str):
            raise RuntimeError("display_name must be a string")
        if not isinstance(service["persona"], str):
            raise RuntimeError("persona must be a string")
        if not isinstance(service["max_turns"], int):
            raise RuntimeError("max_turns must be an integer")
        if not isinstance(service["response_schema_id"], str):
            raise RuntimeError("response_schema_id must be a string")
        if not isinstance(allowed_peers, list) or not all(isinstance(peer, str) for peer in allowed_peers):
            raise RuntimeError("allowed_peers must be an array of strings")
        if not isinstance(initial_prompt, str):
            raise RuntimeError("initial_prompt must be a string")
        validated_requests.append(
            {
                "service": service,
                "allowed_peers": allowed_peers,
                "initial_prompt": initial_prompt,
            }
        )

    return {
        "assistant_text": assistant_text.strip(),
        "spawn_requests": validated_requests,
    }


def parse_service_response(text: str, schema_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    if schema_id is None:
        return text.strip(), []
    if schema_id != "service_control_v1":
        raise RuntimeError(f"unsupported response schema: {schema_id}")

    stripped = text.strip()
    if not stripped:
        raise RuntimeError(f"invalid JSON output for {schema_id}: empty response")

    candidates: list[str] = []
    seen: set[str] = set()

    def _add_candidate(candidate: str) -> None:
        normalized = candidate.strip()
        if normalized and normalized not in seen:
            candidates.append(normalized)
            seen.add(normalized)

    _add_candidate(stripped)
    for match in re.finditer(r"```(?:json|service_control_v1)?\s*([\s\S]*?)```", stripped, flags=re.DOTALL | re.IGNORECASE):
        _add_candidate(match.group(1))

    decoder = json.JSONDecoder()
    for idx, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            _, end = decoder.raw_decode(stripped, idx)
        except json.JSONDecodeError:
            continue
        _add_candidate(stripped[idx:end])

    last_decode_error: json.JSONDecodeError | None = None
    last_schema_error: RuntimeError | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            if last_decode_error is None:
                last_decode_error = exc
            continue
        try:
            validated = validate_service_control_v1(parsed)
            return validated["assistant_text"], validated["spawn_requests"]
        except RuntimeError as exc:
            last_schema_error = exc
            continue

    if last_decode_error is not None:
        raise RuntimeError(f"invalid JSON output for {schema_id}: {last_decode_error}") from last_decode_error
    if last_schema_error is not None:
        raise last_schema_error
    raise RuntimeError(f"invalid JSON output for {schema_id}: no valid JSON object found")



def parse_service_response_with_fallback(
    text: str,
    schema_id: str | None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    try:
        visible_text, spawn_requests = parse_service_response(text, schema_id)
        return visible_text, spawn_requests, None
    except RuntimeError as exc:
        if schema_id is None:
            raise
        return text.strip(), [], str(exc)


def extract_agent_message_visible_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    visible_text, _spawn_requests, error = parse_service_response_with_fallback(
        stripped,
        "service_control_v1",
    )
    if error is None and visible_text:
        return visible_text
    return stripped


def build_prompt(service: dict[str, Any], peer_service: dict[str, Any], text: str, reply_index: int) -> str:
    max_turns = int(service.get("max_turns", 100) or 0)
    turn_limit_text = (
        f"This is your reply number {reply_index}. There is no max-turn limit for this service."
        if max_turns < 0
        else f"This is your reply number {reply_index} of {max_turns}."
    )
    parts = [
        str(service["persona"]),
        "Reply normally and directly for the task at hand. Never mention being an AI.",
        "Do not burn a turn on a plan-only or acknowledgment-only reply. If you have not advanced the work yet, keep assistant_text empty and continue executing.",
        "If you need information from the user before a goal can continue, keep the user-visible question in assistant_text and append a trailing <aize_user_response_wait><timeout_seconds>300</timeout_seconds></aize_user_response_wait> control block inside the same assistant_text. Values above 300 seconds are capped by the runtime; the XML block is hidden from the user-facing text and tells GoalManager to wait briefly, then resume autonomously after the timeout.",
        f"You are talking to {peer_service['display_name']}.",
        turn_limit_text,
    ]
    if text.lstrip().startswith("<aize_"):
        parts.extend(
            [
                "The incoming input envelope is below.",
                text,
            ]
        )
    else:
        parts.append(f"{peer_service['display_name']} said: {text}")
    if service.get("response_schema_id"):
        parts.append(build_schema_instructions(str(service["response_schema_id"])))
    return "\n".join(parts)
