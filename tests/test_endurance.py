from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig, PoolTarget
from cbrs.captcha_budget import CaptchaBudgetError, CaptchaBudgetStore
from cbrs.endurance import EnduranceController, EnduranceFixture, EndurancePlan
from cbrs.jobs import JobStore


def _config() -> PoolConfig:
    return PoolConfig(
        accounts=tuple(PoolAccount(f"a{i}", f"Account {i}") for i in range(1, 4)),
        daily_quota_per_account=20,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=0,
        targets=(PoolTarget("fixture", "fna", foja=9441, numero=4580, ano=1980),),
    )


def _plan(*, enabled: bool = True, cooldown: float = 600) -> EndurancePlan:
    return EndurancePlan(
        enabled=enabled,
        fixtures=(
            EnduranceFixture(
                "fna", {"foja": 9441, "numero": 4580, "year": 1980}, "proven"
            ),
        ),
        cooldown_seconds=cooldown,
    )


def test_round_robin_cursor_persists_and_skips_paused_account(tmp_path):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    config = _config()
    pool_store.create_run(run_id="r1", dry_run=False, config=config, dashboard_url=None)

    first = store.select_account(run_id="r1", config=config, quota_date="2026-08-23")
    pool_store.pause_account("r1", "a2", reason="proxy_drift")
    second = store.select_account(run_id="r1", config=config, quota_date="2026-08-23")
    third = store.select_account(run_id="r1", config=config, quota_date="2026-08-23")

    assert [first.account_id, second.account_id, third.account_id] == ["a1", "a3", "a1"]
    reloaded = JobStore(path)
    fourth = reloaded.select_account(run_id="r1", config=config, quota_date="2026-08-23")
    assert fourth.account_id == "a3"


def test_endurance_creates_only_one_low_priority_job_and_production_wins(tmp_path):
    path = tmp_path / "pool.sqlite3"
    AccountPoolStore(path)
    store = JobStore(path)
    controller = EnduranceController(store, _plan(), _config())

    endurance = controller.maybe_enqueue()
    assert endurance and endurance["source"] == "endurance" and endurance["priority"] == -10
    assert controller.maybe_enqueue() is None
    production, _ = store.create_job(kind="text", input_data={"text": "Production"})

    claimed = store.claim_next("worker")
    assert claimed.job_id == production["job_id"]
    assert claimed.source == "production"


def test_endurance_cooldown_does_not_catch_up(tmp_path):
    path = tmp_path / "pool.sqlite3"
    AccountPoolStore(path)
    store = JobStore(path)
    controller = EnduranceController(store, _plan(cooldown=600), _config())
    job = controller.maybe_enqueue()
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET status='completed', finished_at=? WHERE job_id=?",
            (datetime.now(timezone.utc).isoformat(), job["job_id"]),
        )
    assert controller.maybe_enqueue() is None
    assert controller.maybe_enqueue(force=True) is not None
    assert len([job for job in store.list_jobs() if job["source"] == "endurance"]) == 2


def test_endurance_and_production_share_the_full_daily_account_quota(tmp_path):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    config = _config()
    pool_store.create_run(run_id="r1", dry_run=False, config=config, dashboard_url=None)
    quota_date = "2026-08-23"
    for index in range(15):
        job, _ = store.create_job(
            kind="fna",
            input_data={"foja": 9441, "numero": 4580, "year": 1980},
            idempotency_key=f"quota-{index}",
            source="endurance",
        )
        attempt = store.begin_attempt(
            job_id=job["job_id"],
            account_id="a1",
            quota_date=quota_date,
            quota=20,
            run_id="r1",
            consume_quota=True,
        )
        store.finish_attempt(attempt, status="completed")
        with store.connect() as db:
            db.execute(
                "UPDATE jobs SET status='completed', finished_at=? WHERE job_id=?",
                (datetime.now(timezone.utc).isoformat(), job["job_id"]),
            )

    with store.connect() as db:
        db.execute(
            """
            INSERT INTO account_rotation(name, next_index, updated_at) VALUES ('jobs', 0, ?)
            ON CONFLICT(name) DO UPDATE SET next_index=0, updated_at=excluded.updated_at
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
    endurance_account = store.select_account(
        run_id="r1",
        config=config,
        quota_date=quota_date,
        source="endurance",
        source_quota_by_account=_plan().source_quota(config),
    )
    with store.connect() as db:
        db.execute("UPDATE account_rotation SET next_index=0 WHERE name='jobs'")
    production_account = store.select_account(
        run_id="r1", config=config, quota_date=quota_date, source="production"
    )

    assert endurance_account.account_id == "a1"
    assert production_account.account_id == "a1"


def test_captcha_budget_enforces_daily_cap_and_circuit_without_sensitive_fields(tmp_path):
    store = CaptchaBudgetStore(tmp_path / "state.sqlite3", daily_limit=2, circuit_seconds=900)
    first = store.reserve(account_id="a1", action="login")
    store.finish(first, status="failed", error_code="NETWORK_ERROR", open_circuit=True)
    with pytest.raises(CaptchaBudgetError, match="CIRCUIT_OPEN"):
        store.reserve(account_id="a2", action="login")

    with store._connect() as db:
        db.execute("DELETE FROM captcha_solver_state")
    second = store.reserve(account_id="a2", action="search")
    store.finish(second, status="succeeded", cost_usd=0.002, latency_seconds=1.5)
    with pytest.raises(CaptchaBudgetError, match="DAILY_LIMIT"):
        store.reserve(account_id="a3", action="search")
    columns = {
        row[1] for row in store._connect().execute("PRAGMA table_info(captcha_attempts)")
    }
    assert not columns.intersection({"api_key", "proxy_url", "token", "worker_ip", "raw_ip"})


def test_captcha_auth_failure_disables_fallback_until_health_clears_it(tmp_path):
    store = CaptchaBudgetStore(tmp_path / "state.sqlite3", daily_limit=10, circuit_seconds=900)
    reservation = store.reserve(account_id="a1", action="login")
    store.finish(
        reservation,
        status="failed",
        error_code="ERROR_WRONG_USER_KEY",
        open_circuit=True,
        disable_external=True,
    )
    with pytest.raises(CaptchaBudgetError, match="EXTERNAL_FALLBACK_DISABLED"):
        store.reserve(account_id="a2", action="login")
    assert store.status()["external_fallback_disabled"] is True
    store.clear_solver_disable()
    assert store.status()["external_fallback_disabled"] is False


def test_portal_rejection_is_traced_and_blocks_repeat_paid_solves(tmp_path):
    store = CaptchaBudgetStore(
        tmp_path / "state.sqlite3",
        daily_limit=10,
        circuit_seconds=900,
        rejection_cooldown_seconds=21_600,
    )
    store.arm_manual(account_id="a1")
    reservation = store.reserve(
        account_id="a1",
        action="indice_com_texto",
        require_manual_authorization=True,
    )
    store.finish(
        reservation,
        status="succeeded",
        cost_usd=0.00299,
        latency_seconds=18.8,
    )
    store.record_portal_outcome(
        reservation.attempt_id,
        status="rejected",
        error_code="captcha_rejected",
    )

    attempt = store.recent_attempts(limit=1)[0]
    assert attempt["status"] == "succeeded"
    assert attempt["portal_status"] == "rejected"
    assert attempt["portal_error_code"] == "captcha_rejected"
    assert attempt["paid_retry_blocked_until"]
    assert store.status()["rejected"] == 1
    with pytest.raises(CaptchaBudgetError, match="RECENT_PORTAL_REJECTION"):
        store.arm_manual(account_id="a1")


def test_manual_captcha_authorization_is_one_shot_and_cannot_accumulate(tmp_path):
    store = CaptchaBudgetStore(tmp_path / "state.sqlite3", daily_limit=10, circuit_seconds=900)
    with pytest.raises(CaptchaBudgetError, match="MANUAL_AUTH_REQUIRED"):
        store.reserve(
            account_id="a1", action="search", require_manual_authorization=True
        )
    store.arm_manual(account_id="a1")
    store.arm_manual(account_id="a1")
    assert store.status()["manual_authorizations_armed"] == 1
    reservation = store.reserve(
        account_id="a1", action="search", require_manual_authorization=True
    )
    store.finish(reservation, status="succeeded")
    assert store.manual_armed(account_id="a1") is False
    with pytest.raises(CaptchaBudgetError, match="MANUAL_AUTH_REQUIRED"):
        store.reserve(
            account_id="a1", action="search", require_manual_authorization=True
        )


def test_manual_captcha_authorization_expires(tmp_path):
    store = CaptchaBudgetStore(
        tmp_path / "captcha.sqlite3", daily_limit=10, circuit_seconds=900
    )
    store.arm_manual(account_id="a1")
    with store._connect() as db:
        db.execute(
            "UPDATE captcha_manual_authorizations SET expires_at = ? WHERE account_id = ?",
            ("2000-01-01T00:00:00+00:00", "a1"),
        )

    assert store.manual_armed(account_id="a1") is False
    assert store.status()["manual_authorizations_armed"] == 0
    with pytest.raises(CaptchaBudgetError, match="MANUAL_AUTH_REQUIRED"):
        store.reserve(
            account_id="a1", action="search", require_manual_authorization=True
        )


def test_manual_authorization_activity_records_not_required_without_paid_attempt(tmp_path):
    store = CaptchaBudgetStore(
        tmp_path / "captcha.sqlite3", daily_limit=10, circuit_seconds=900
    )
    store.arm_manual(account_id="a1")
    store.finish_manual_authorization(
        account_id="a1",
        status="not_required",
        reason="no_waiting_captcha_job",
    )

    activity = store.recent_activity(limit=5)
    assert activity[0]["kind"] == "authorization"
    assert activity[0]["status"] == "not_required"
    assert activity[0]["portal_status"] == "not_required"
    assert activity[0]["error_code"] == "no_waiting_captcha_job"
    assert store.recent_attempts() == []
    assert store.status()["attempts"] == 0
    assert store.manual_armed(account_id="a1") is False


def test_manual_authorization_activity_is_consumed_when_paid_task_is_reserved(tmp_path):
    store = CaptchaBudgetStore(
        tmp_path / "captcha.sqlite3", daily_limit=10, circuit_seconds=900
    )
    store.arm_manual(account_id="a1")
    store.reserve(
        account_id="a1",
        action="indice_com_texto",
        require_manual_authorization=True,
    )

    authorization = next(
        item for item in store.recent_activity() if item["kind"] == "authorization"
    )
    assert authorization["status"] == "consumed"
    assert authorization["error_code"] == "paid_task_reserved"


def test_automatic_captcha_preference_is_persistent_and_sanitized(tmp_path):
    path = tmp_path / "captcha.sqlite3"
    store = CaptchaBudgetStore(path, daily_limit=10, circuit_seconds=900)
    assert store.automatic_enabled() is False

    assert store.set_automatic_enabled(True) is True
    reloaded = CaptchaBudgetStore(path, daily_limit=10, circuit_seconds=900)
    assert reloaded.automatic_enabled() is True
    assert reloaded.status()["automatic_enabled"] is True
    assert reloaded.set_automatic_enabled(False) is False
    assert reloaded.automatic_enabled() is False
