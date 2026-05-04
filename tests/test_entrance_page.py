from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.html_renderer import render_entrance_plugin_page
from runtime.http_handler import (
    _communication_forward_hints,
    _infer_communication_forward_target_session_id,
    _is_communication_chat_noise,
    _is_communication_session_settings,
)


class EntrancePageTests(unittest.TestCase):
    def test_entrance_page_renders_chat_polling_surface(self) -> None:
        page = render_entrance_plugin_page(display_name="AIze", username="repyt")

        self.assertIn("Entrance Chat", page)
        self.assertIn("id='chat-log'", page)
        self.assertIn("entrance-status-badges", page)
        self.assertIn("renderEntranceState", page)
        self.assertIn("double-enter-send", page)
        self.assertIn("Send with double Enter", page)
        self.assertIn("submitEntrancePrompt", page)
        self.assertIn("/overview?scope=all", page)
        self.assertIn("visibleAssistantText", page)
        self.assertIn("assistanttext", page)
        self.assertIn("mergeMessages", page)
        self.assertIn("renderChat([entry])", page)
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

    def test_communication_forward_hints_detect_development_session_request(self) -> None:
        self.assertEqual(
            _communication_forward_hints("この修正を行いたくて、AIze開発セッションの下で開発してください"),
            {"開発", "development", "dev"},
        )
        self.assertEqual(
            _communication_forward_hints("Please send this to the AIze Development session."),
            {"aize", "development", "dev", "開発"},
        )
        self.assertEqual(_communication_forward_hints("こんにちは"), set())

    def test_infer_communication_forward_target_session_id_prefers_development_session(self) -> None:
        sessions = [
            {
                "session_id": "entrance",
                "label": "Entrance Verify Clean",
                "session_ui_mode": "communication",
                "communication_agent_enabled": True,
            },
            {
                "session_id": "dev",
                "label": "AIze Development",
                "session_ui_mode": "standard",
                "communication_agent_enabled": False,
            },
        ]
        self.assertEqual(
            _infer_communication_forward_target_session_id(
                sessions,
                current_session_id="entrance",
                prompt_text="この修正を行いたくて、AIze開発セッションの下で開発してください",
            ),
            "dev",
        )

    def test_infer_communication_forward_target_session_id_returns_none_on_tie(self) -> None:
        sessions = [
            {"session_id": "entrance", "label": "Entrance", "session_ui_mode": "communication"},
            {"session_id": "dev-a", "label": "Development A"},
            {"session_id": "dev-b", "label": "Development B"},
        ]
        self.assertIsNone(
            _infer_communication_forward_target_session_id(
                sessions,
                current_session_id="entrance",
                prompt_text="development session に送ってください",
            )
        )


if __name__ == "__main__":
    unittest.main()
