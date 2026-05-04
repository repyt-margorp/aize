from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.html_renderer import render_entrance_plugin_page
from runtime.http_handler import _is_communication_chat_noise, _is_communication_session_settings


class EntrancePageTests(unittest.TestCase):
    def test_entrance_page_renders_chat_polling_surface(self) -> None:
        page = render_entrance_plugin_page(display_name="AIze", username="repyt")

        self.assertIn("Entrance Chat", page)
        self.assertIn("id='chat-log'", page)
        self.assertIn("entrance-status-badges", page)
        self.assertIn("renderEntranceState", page)
        self.assertIn("/messages?session_id=", page)
        self.assertIn("InteractiveAgent", page)
        self.assertIn("agent_message.delta", page)
        self.assertIn("user", page)
        self.assertIn("normalizedText==='response started'", page)
        self.assertIn("eventType==='agent.turn_started'", page)

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


if __name__ == "__main__":
    unittest.main()
