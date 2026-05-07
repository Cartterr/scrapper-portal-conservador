import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbrs_portal.jobs import JobStore


class JobStoreTests(unittest.TestCase):
    def test_dedupe_and_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            first = store.create_job("commerce.search_text", {"query": "MBX Global"})
            second = store.create_job("commerce.search_text", {"query": "MBX Global"})
            self.assertEqual(first, second)

            job = store.claim_next()
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "running")
            self.assertEqual(job.attempts, 1)
            store.complete_job(job.id)
            self.assertEqual(store.list_jobs()[0].status, "succeeded")

    def test_retry_then_failed_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            store.create_job("commerce.search_text", {"query": "A"}, max_attempts=1)
            job = store.claim_next()
            status = store.fail_job(job.id, code="captcha", message="retry", retryable=True)
            self.assertEqual(status, "failed")
            self.assertEqual(store.list_jobs()[0].last_error_code, "captcha")

    def test_account_budget_zero_means_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            store.upsert_account(
                label="account-1",
                email_hash="abc",
                display_label="account-1:ab***@example.com",
                daily_budget=0,
            )
            self.assertEqual(store.available_account_labels(), ["account-1"])
            store.record_account_query("account-1")
            self.assertEqual(store.available_account_labels(), ["account-1"])

    def test_positive_account_budget_can_still_exhaust_for_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            store.upsert_account(
                label="account-1",
                email_hash="abc",
                display_label="account-1:ab***@example.com",
                daily_budget=1,
            )
            store.record_account_query("account-1")
            self.assertEqual(store.available_account_labels(), [])

    def test_safety_state_persists_and_unlocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp, "jobs.sqlite3")
            store = JobStore(db)
            profile = Path(tmp, "profile")
            store.set_safety_state(
                state="manual_required",
                signal="rate_limited",
                endpoint="/api",
                status=429,
                reason="slow down",
                profile_path=profile,
                operator_action="manual check",
            )
            reopened = JobStore(db)
            self.assertEqual(reopened.safety_status(profile_path=profile)["state"], "manual_required")
            reopened.unlock_safety(reason="checked browser")
            self.assertEqual(reopened.safety_status(profile_path=profile)["state"], "ok")

    def test_live_lock_rejects_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp, "jobs.sqlite3"))
            profile = Path(tmp, "profile")
            first = store.acquire_live_lock(
                owner="first",
                profile_path=profile,
                stale_after_seconds=3600,
            )
            second = store.acquire_live_lock(
                owner="second",
                profile_path=profile,
                stale_after_seconds=3600,
            )
            self.assertTrue(first["acquired"])
            self.assertFalse(second["acquired"])
            store.release_live_lock(owner="first")
            third = store.acquire_live_lock(
                owner="third",
                profile_path=profile,
                stale_after_seconds=3600,
            )
            self.assertTrue(third["acquired"])


if __name__ == "__main__":
    unittest.main()
