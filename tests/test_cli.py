import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbrs_portal.cli import main
from cbrs_portal.jobs import JobStore


class CliSafetyTests(unittest.TestCase):
    def test_safety_status_and_unlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            store = JobStore(Path(tmp, "cbrs.sqlite3"))
            store.set_safety_state(
                state="manual_required",
                signal="rate_limited",
                endpoint="/api",
                status=429,
                reason="limit",
                profile_path=Path(tmp, "profile"),
                operator_action="manual check",
            )
            with patch.dict(os.environ, env, clear=True):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["safety", "status"]), 0)
                self.assertIn("manual_required", output.getvalue())

                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        main(["safety", "unlock", "--reason", "checked browser"]),
                        0,
                    )
                self.assertIn('"unlocked": true', output.getvalue())

    def test_safety_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            store = JobStore(Path(tmp, "cbrs.sqlite3"))
            store.record_safety_event(
                event="wait",
                endpoint="/api",
                status=None,
                classified_code=None,
                message="waiting",
            )
            with patch.dict(os.environ, env, clear=True):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["safety", "events", "--limit", "1"]), 0)
                self.assertIn('"event": "wait"', output.getvalue())


def _env(tmp: str) -> dict[str, str]:
    return {
        "CBRS_DATA_DIR": tmp,
        "CBRS_DATABASE_URL": f"sqlite:///{Path(tmp, 'cbrs.sqlite3').as_posix()}",
        "CBRS_BROWSER_PROFILE_DIR": str(Path(tmp, "profile")),
        "CBRS_HEADLESS": "true",
        "CBRS_MIN_REQUEST_DELAY_MS": "0",
    }


if __name__ == "__main__":
    unittest.main()
