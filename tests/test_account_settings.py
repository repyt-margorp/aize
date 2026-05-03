from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kernel.auth import create_user, update_user_password, verify_user_password


class AccountSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self._tmp.name) / ".aize-runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_update_user_password_replaces_login_secret(self) -> None:
        ok, username = create_user(self.runtime_root, username="repyt", password="old-pass")
        self.assertTrue(ok)
        self.assertEqual(username, "repyt")

        changed, result = update_user_password(
            self.runtime_root,
            username="repyt",
            current_password="old-pass",
            new_password="new-pass",
        )

        self.assertTrue(changed)
        self.assertEqual(result, "repyt")
        self.assertFalse(verify_user_password(self.runtime_root, username="repyt", password="old-pass"))
        self.assertTrue(verify_user_password(self.runtime_root, username="repyt", password="new-pass"))

    def test_update_user_password_rejects_wrong_current_password(self) -> None:
        create_user(self.runtime_root, username="repyt", password="old-pass")

        changed, result = update_user_password(
            self.runtime_root,
            username="repyt",
            current_password="wrong-pass",
            new_password="new-pass",
        )

        self.assertFalse(changed)
        self.assertEqual(result, "current_password_incorrect")
        self.assertTrue(verify_user_password(self.runtime_root, username="repyt", password="old-pass"))

if __name__ == "__main__":
    unittest.main()
