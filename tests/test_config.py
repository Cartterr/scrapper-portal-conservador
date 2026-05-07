import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbrs_portal.config import Settings, redact_mapping


class ConfigTests(unittest.TestCase):
    def test_settings_parse_accounts_and_sqlite_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CBRS_DATA_DIR": tmp,
                "CBRS_DATABASE_URL": f"sqlite:///{Path(tmp, 'cbrs.sqlite3').as_posix()}",
                "CBRS_BROWSER_PROFILE_DIR": str(Path(tmp, "profile")),
                "CBRS_HEADLESS": "true",
                "CBRS_USER_1": "person@example.com",
                "CBRS_PASSWORD_1": "secret",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
            self.assertTrue(settings.headless)
            self.assertEqual(settings.sqlite_path, Path(tmp, "cbrs.sqlite3"))
            self.assertEqual(len(settings.accounts), 1)
            self.assertEqual(settings.accounts[0].display_label, "account-1:pe***@example.com")

    def test_redact_mapping_hides_secret_names(self):
        redacted = redact_mapping({"CBRS_PASSWORD_1": "abcdef123456", "safe": "value"})
        self.assertEqual(redacted["safe"], "value")
        self.assertNotIn("abcdef123456", redacted["CBRS_PASSWORD_1"])


if __name__ == "__main__":
    unittest.main()
