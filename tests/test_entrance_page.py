from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.html_renderer import render_entrance_plugin_page


class EntrancePageTests(unittest.TestCase):
    def test_entrance_page_renders_chat_polling_surface(self) -> None:
        page = render_entrance_plugin_page(display_name="AIze", username="repyt")

        self.assertIn("Entrance Chat", page)
        self.assertIn("id='chat-log'", page)
        self.assertIn("/messages?session_id=", page)
        self.assertIn("Entrance Agent", page)
        self.assertIn("agent_message.delta", page)
        self.assertIn("user", page)


if __name__ == "__main__":
    unittest.main()
