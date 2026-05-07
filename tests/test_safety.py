import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbrs_portal.errors import ClassifiedResponse, ErrorCode
from cbrs_portal.jobs import JobStore
from cbrs_portal.safety import LiveLockError, LiveSafetyGovernor, SafetyPolicy, SafetyStop


class SafetyGovernorTests(unittest.TestCase):
    def test_successful_requests_do_not_hit_a_count_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            governor = LiveSafetyGovernor(
                store,
                Path(tmp, "profile"),
                SafetyPolicy(min_request_delay_ms=0),
                owner="test",
            )
            for _ in range(25):
                governor.before_request("/api")
                governor.after_response(
                    "/api",
                    status=200,
                    classified=ClassifiedResponse(ErrorCode.OK, False, "ok"),
                )
            self.assertEqual(
                store.safety_status(profile_path=Path(tmp, "profile"))["successful_requests"],
                25,
            )

    def test_waf_sets_manual_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            governor = LiveSafetyGovernor(
                store,
                Path(tmp, "profile"),
                SafetyPolicy(min_request_delay_ms=0),
                owner="test",
            )
            with self.assertRaises(SafetyStop):
                governor.after_response(
                    "/api",
                    status=403,
                    classified=ClassifiedResponse(ErrorCode.WAF, True, "waf"),
                )
            status = store.safety_status(profile_path=Path(tmp, "profile"))
            self.assertEqual(status["state"], "manual_required")
            self.assertEqual(status["last_signal"], str(ErrorCode.WAF))

    def test_auth_refresh_failure_does_not_stop_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            governor = LiveSafetyGovernor(
                store,
                Path(tmp, "profile"),
                SafetyPolicy(min_request_delay_ms=0),
                owner="test",
            )
            governor.after_response(
                "/api/v1/auth/refresh",
                status=401,
                classified=ClassifiedResponse(ErrorCode.AUTH, False, "auth"),
            )
            self.assertEqual(store.safety_status(profile_path=Path(tmp, "profile"))["state"], "ok")

    def test_lock_blocks_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            profile = Path(tmp, "profile")
            first = LiveSafetyGovernor(
                store,
                profile,
                SafetyPolicy(min_request_delay_ms=0),
                owner="first",
            )
            second = LiveSafetyGovernor(
                store,
                profile,
                SafetyPolicy(min_request_delay_ms=0),
                owner="second",
            )
            first.acquire()
            try:
                with self.assertRaises(LiveLockError):
                    second.acquire()
            finally:
                first.release()

    def test_transient_backoff_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            governor = LiveSafetyGovernor(
                store,
                Path(tmp, "profile"),
                SafetyPolicy(min_request_delay_ms=0, transient_backoff_ms=(1,)),
                owner="test",
            )
            with patch("time.sleep") as sleep:
                governor.after_response(
                    "/api",
                    status=500,
                    classified=ClassifiedResponse(ErrorCode.TRANSIENT, True, "temporary"),
                )
            sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
