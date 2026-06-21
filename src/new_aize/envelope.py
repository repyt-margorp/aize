from __future__ import annotations

import json
from typing import Any
from xml.sax.saxutils import escape


def xml_text(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def render_message_envelope(message: dict[str, Any]) -> str:
    attrs = {
        "id": message.get("message_id", ""),
        "from": message.get("from", ""),
        "to": message.get("to", ""),
        "created_at": message.get("created_at", ""),
    }
    attr_text = " ".join(f'{key}="{xml_text(value)}"' for key, value in attrs.items() if value)
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    lines = [f"<aize-message {attr_text}>", "  <payload>"]
    for key, value in sorted(payload.items()):
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        lines.append(f"    <{xml_text(key)}>{xml_text(rendered)}</{xml_text(key)}>")
    lines.append("  </payload>")
    lines.append("</aize-message>")
    return "\n".join(lines)


def render_message_bundle(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "<aize-message-bundle />"
    rendered = "\n".join(render_message_envelope(message) for message in messages)
    return f"<aize-message-bundle>\n{rendered}\n</aize-message-bundle>"
