from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from cbrs.account_pool import (
    AccountPoolStore,
    PoolAccount,
    PoolConfig,
    PoolTarget,
    utc_now,
)
from cbrs.account_pool_dashboard import start_pool_dashboard
from cbrs.backup import backup_health, run_backup, verify_backup_restore
from cbrs.captcha_budget import CaptchaBudgetStore
from cbrs.config import load_settings
from cbrs.jobs import (
    IdempotencyConflictError,
    JobStore,
    WORKER_LEASE_NAME,
    _ManagedAccountScraper,
    _PersistentAccountBrowsers,
    _ensure_account_gate,
    _expected_artifact_path,
    run_job_worker,
    validate_pdf,
)
from cbrs.pdf import create_pdf
from cbrs.safety import SafetyStopException, StopReason


def _settings(tmp_path: Path):
    return load_settings(
        {
            "CBRS_PROFILE_DIR": str(tmp_path / "state" / "chrome-profile"),
            "CBRS_OUTPUT_DIR": str(tmp_path / "outputs"),
            "CBRS_EGRESS_MODE": "client_office",
            "CBRS_REQUEST_DELAY_SECONDS": "3.5",
        },
        root=tmp_path,
    )


def _config(*, quota: int = 20, accounts: int = 3) -> PoolConfig:
    values = tuple(
        PoolAccount(
            f"a{index}",
            f"Account {index}",
            username_env=f"CBRS_TEST_USER_{index}",
            password_env=f"CBRS_TEST_PASSWORD_{index}",
            daily_quota=quota,
        )
        for index in range(1, accounts + 1)
    )
    return PoolConfig(
        accounts=values,
        daily_quota_per_account=quota,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=0,
        targets=(PoolTarget("recovery", "text", query="authorized recovery"),),
    )


def _credentials(monkeypatch: pytest.MonkeyPatch, config: PoolConfig) -> None:
    for account in config.accounts:
        monkeypatch.setenv(str(account.username_env), f"{account.account_id}@example.test")
        monkeypatch.setenv(str(account.password_env), "secret-value")


class FakeScraper:
    behavior: dict[str, object] = {}
    results: list[dict] = []

    def __init__(self, *, settings, headless=False):
        self.settings = settings
        self.headless = headless
        self.account_id = next(
            (part for part in settings.profile_dir.parts if re.fullmatch(r"a\d+", part)), "unknown"
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def ensure_authenticated(self, username, password, *, force=False):
        assert username and password
        return "refreshed"

    def search_by_text(self, query):
        action = self.behavior.get(self.account_id)
        if isinstance(action, Exception):
            raise action
        return [dict(value) for value in self.results]

    def search_by_fna(self, foja, numero, ano):
        return self.search_by_text(f"{foja}-{numero}-{ano}")

    def get_image_refs(self, ticket):
        return (
            {"foja": 1, "numero": 2, "ano": 2026},
            [
                {"pageNumber": 1, "dataRef": f"{ticket}-1"},
                {"pageNumber": 2, "dataRef": f"{ticket}-2"},
            ],
        )

    def download_image(self, uuid, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), "white").save(output_path, "JPEG")
        return output_path


def test_persistent_browser_pool_demotes_visible_login_gate(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    pool = _PersistentAccountBrowsers(
        scraper_factory=FakeScraper,
        headless=False,
        store=store,
        worker_id="worker-test",
    )
    scraper = SimpleNamespace(page_requires_login=lambda: True)
    pool._entries["a1"] = _ManagedAccountScraper(manager=scraper, scraper=scraper)
    store.set_account_browser_state(
        "a1",
        live=True,
        authenticated=True,
        headless=False,
        owner="worker-test",
        status="authenticated_refresh",
    )

    pool.refresh_page_auth_states()

    check = store.account_check("a1")
    assert check["browser_live"] == 1
    assert check["browser_authenticated"] == 0
    assert check["browser_status"] == "login_gate_visible"


def test_persistent_browser_pool_promotes_only_protected_form_evidence(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    pool = _PersistentAccountBrowsers(
        scraper_factory=FakeScraper,
        headless=False,
        store=store,
        worker_id="worker-test",
    )
    scraper = SimpleNamespace(
        detect_commerce_auth_state=lambda: "authenticated_form"
    )
    pool._entries["a1"] = _ManagedAccountScraper(manager=scraper, scraper=scraper)

    pool.refresh_page_auth_states()

    check = store.account_check("a1")
    assert check["browser_live"] == 1
    assert check["browser_authenticated"] == 1
    assert check["browser_status"] == "authenticated_form_visible"
    assert check["browser_auth_state"] == "authenticated_form"


def test_persistent_browser_pool_uses_nested_production_browser_surface(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    pool = _PersistentAccountBrowsers(
        scraper_factory=FakeScraper,
        headless=False,
        store=store,
        worker_id="worker-nested-browser",
    )
    browser = SimpleNamespace(
        detect_commerce_auth_state=lambda: "authenticated_form",
        page=SimpleNamespace(is_closed=lambda: False),
    )
    scraper = SimpleNamespace(browser=browser)
    pool._entries["a1"] = _ManagedAccountScraper(manager=scraper, scraper=scraper)

    pool.refresh_page_auth_states()

    check = store.account_check("a1")
    assert check["browser_authenticated"] == 1
    assert check["browser_auth_state"] == "authenticated_form"


def _gate(*args, **kwargs):
    return SimpleNamespace(ok=True, report_path=None, report={})


def test_job_store_idempotency_and_input_privacy(tmp_path):
    path = tmp_path / "pool.sqlite3"
    AccountPoolStore(path)
    store = JobStore(path)

    first, created = store.create_job(
        kind="text", input_data={"text": "Sensitive Company"}, idempotency_key="request-1"
    )
    repeated, repeated_created = store.create_job(
        kind="text", input_data={"text": "Sensitive Company"}, idempotency_key="request-1"
    )

    assert created is True
    assert repeated_created is False
    assert repeated["job_id"] == first["job_id"]
    assert "Sensitive Company" not in json.dumps(store.get_job(first["job_id"]))
    assert store.get_job(first["job_id"], include_input=True)["input"]["text"] == "Sensitive Company"
    with pytest.raises(IdempotencyConflictError):
        store.create_job(
            kind="text", input_data={"text": "Different"}, idempotency_key="request-1"
        )

    with store.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_failed_attempt_releases_quota_but_success_keeps_it(tmp_path):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    config = _config(accounts=1)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})

    failed = store.begin_attempt(
        job_id=job["job_id"],
        account_id="a1",
        quota_date=_today(),
        quota=1,
        run_id="run",
        consume_quota=True,
    )
    assert failed
    assert store.usage_by_account(_today()).get("a1", 0) == 0
    assert (
        store.begin_attempt(
            job_id=job["job_id"],
            account_id="a1",
            quota_date=_today(),
            quota=1,
            run_id="run",
            consume_quota=True,
        )
        is None
    )
    store.finish_attempt(
        failed,
        status="safety_stop",
        safety_stop=StopReason.TEMPORARY_UNAVAILABLE.value,
    )
    assert store.usage_by_account(_today())["a1"] == 0

    succeeded = store.begin_attempt(
        job_id=job["job_id"],
        account_id="a1",
        quota_date=_today(),
        quota=1,
        run_id="run",
        consume_quota=True,
    )
    assert succeeded
    store.finish_attempt(succeeded, status="search_completed")
    assert store.usage_by_account(_today())["a1"] == 1
    with store.connect() as db:
        rows = db.execute(
            "SELECT status, quota_consumed FROM job_attempts ORDER BY started_at, rowid"
        ).fetchall()
    assert [(row["status"], row["quota_consumed"]) for row in rows] == [
        ("safety_stop", 0),
        ("search_completed", 1),
    ]


def test_reconcile_quota_usage_repairs_legacy_failed_attempts(tmp_path):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    config = _config(accounts=1)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})

    succeeded = store.begin_attempt(
        job_id=job["job_id"],
        account_id="a1",
        quota_date=_today(),
        quota=20,
        run_id="run",
        consume_quota=True,
    )
    store.finish_attempt(succeeded, status="search_completed")
    legacy_failure = store.begin_attempt(
        job_id=job["job_id"],
        account_id="a1",
        quota_date=_today(),
        quota=20,
        run_id="run",
        consume_quota=True,
    )
    with store.connect() as db:
        db.execute(
            "UPDATE job_attempts SET status = 'safety_stop', safety_stop = ? WHERE attempt_id = ?",
            (StopReason.TEMPORARY_UNAVAILABLE.value, legacy_failure),
        )
        db.execute(
            "UPDATE account_daily_usage SET used = 2 WHERE account_id = 'a1' AND quota_date = ?",
            (_today(),),
        )
    assert store.usage_by_account(_today())["a1"] == 2

    result = store.reconcile_quota_usage(quota_date=_today())

    assert result == {"released_attempts": 1, "accounts_updated": 1}
    assert store.usage_by_account(_today())["a1"] == 1


def test_priority_job_is_claimed_before_older_queued_work(tmp_path):
    path = tmp_path / "pool.sqlite3"
    AccountPoolStore(path)
    store = JobStore(path)
    ordinary, _ = store.create_job(kind="text", input_data={"text": "Ordinary"})
    priority, _ = store.create_job(
        kind="fna",
        input_data={"foja": 1, "numero": 2, "year": 2020},
        priority=1,
    )

    claimed = store.claim_next("worker")

    assert claimed is not None
    assert claimed.job_id == priority["job_id"]
    assert ordinary["job_id"] != priority["job_id"]


def test_successful_fna_examples_are_deduplicated(tmp_path):
    path = tmp_path / "pool.sqlite3"
    AccountPoolStore(path)
    store = JobStore(path)
    first, _ = store.create_job(
        kind="fna", input_data={"foja": 10, "numero": 20, "year": 2020}
    )
    duplicate, _ = store.create_job(
        kind="fna", input_data={"foja": 10, "numero": 20, "year": 2020}
    )
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET status = 'completed', completed_items = 1, finished_at = ? WHERE job_id IN (?, ?)",
            ("2026-01-01T00:00:00+00:00", first["job_id"], duplicate["job_id"]),
        )

    assert store.successful_fna_examples() == [
        {"foja": 10, "numero": 20, "year": 2020, "success_count": 2}
    ]


def test_recover_abandoned_job_and_item(tmp_path):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    config = _config(accounts=1)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    job, _ = store.create_job(kind="text", input_data={"text": "A"})
    claimed = store.claim_next("worker")
    attempt_id = store.begin_attempt(
        job_id=job["job_id"],
        account_id="a1",
        quota_date=_today(),
        quota=20,
        run_id="run",
        consume_quota=True,
    )
    assert attempt_id
    store.add_results(claimed.job_id, [{"ticket": "private", "foja": 1}])
    item = store.items(claimed.job_id, public=False)[0]
    store.mark_item_downloading(item["item_id"], tmp_path / "out.pdf")
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job["job_id"],),
        )

    assert store.recover_abandoned_jobs() == 1
    assert store.get_job(job["job_id"])["status"] == "queued"
    assert store.items(job["job_id"], public=False)[0]["status"] == "pending"
    assert store.usage_by_account(_today())["a1"] == 0
    with store.connect() as db:
        attempt = db.execute(
            "SELECT status, quota_consumed FROM job_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    assert attempt["status"] == "worker_recovered"
    assert attempt["quota_consumed"] == 0


def test_worker_lease_rejects_a_second_owner_until_stale(tmp_path):
    path = tmp_path / "pool.sqlite3"
    AccountPoolStore(path)
    store = JobStore(path)

    assert store.acquire_lease("portal_worker", "first", stale_seconds=60) is True
    assert store.acquire_lease("portal_worker", "second", stale_seconds=60) is False
    with store.connect() as db:
        db.execute(
            "UPDATE leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE lease_name = 'portal_worker'"
        )
    assert store.acquire_lease("portal_worker", "second", stale_seconds=60) is True


def test_dataimpulse_route_rotation_is_durable_unique_and_two_phase(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    store.ensure_dataimpulse_route("a1", 10000)
    store.ensure_dataimpulse_route("a2", 10001)
    store.ensure_dataimpulse_route("a3", 10002)

    candidate = store.begin_dataimpulse_rotation(
        "a1",
        initial_port=10000,
        reason="confirmed_connection_failure",
        port_min=10000,
        port_max=20000,
        cooldown_seconds=300,
        max_rotations_per_hour=3,
    )

    assert candidate["ok"] is True
    assert candidate["pending_port"] == 10003
    assert store.dataimpulse_route("a1")["active_port"] == 10000
    promoted = store.finish_dataimpulse_rotation("a1", promoted=True)
    assert promoted["active_port"] == 10003
    assert promoted["pending_port"] is None
    assert promoted["generation"] == 1
    assert {route["active_port"] for route in store.dataimpulse_routes()} == {
        10001,
        10002,
        10003,
    }


def test_dataimpulse_rotation_rate_limit_fails_closed(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    store.ensure_dataimpulse_route("a1", 10000)
    with store.connect() as db:
        db.execute(
            """
            UPDATE account_proxy_routes
            SET rotation_count = 3,
                rotation_window_started_at = ?, cooldown_until = NULL
            WHERE account_id = 'a1'
            """,
            (utc_now(),),
        )

    blocked = store.begin_dataimpulse_rotation(
        "a1",
        initial_port=10000,
        reason="confirmed_connection_failure",
        port_min=10000,
        port_max=20000,
        cooldown_seconds=300,
        max_rotations_per_hour=3,
    )

    assert blocked["ok"] is False
    assert blocked["reason"] == "proxy_recovery_exhausted"
    assert store.dataimpulse_route("a1")["status"] == "proxy_recovery_exhausted"


def test_quota_day_releases_daily_limit_but_preserves_captcha(tmp_path):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    config = _config(accounts=2)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    pool_store.pause_account("run", "a1", reason="daily_limit")
    pool_store.mark_account_captcha_pending("run", "a2")
    with pool_store._connect() as db:
        db.execute("UPDATE accounts SET quota_date = '2000-01-01' WHERE run_id = 'run'")

    pool_store.reset_quota_day("run", _today())

    states = {row["account_id"]: row for row in pool_store.accounts("run")}
    assert states["a1"]["status"] == "available"
    assert states["a1"]["paused_reason"] is None
    assert states["a2"]["status"] == "captcha_pending"


def test_sticky_residential_gate_rotates_baseline_after_every_safety_gate(
    tmp_path, monkeypatch
):
    settings = replace(
        _settings(tmp_path),
        proxy_url="http://user:password@proxy.test:8000",
        two_captcha_api_key="provider-test-key",
    )
    config = _config(accounts=1)
    account = replace(
        config.accounts[0], proxy_provider="2captcha_residential_sticky"
    )
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    store = JobStore(path)
    calls: list[bool] = []

    def preflight(_settings, *, allow_baseline_replacement=False, **_kwargs):
        calls.append(allow_baseline_replacement)
        if not allow_baseline_replacement:
            return SimpleNamespace(ok=False, report={})
        return SimpleNamespace(
            ok=True,
            report={
                "egress_hash": "new-sanitized-hash",
                "egress_country": "CL",
                "checks": [
                    {
                        "name": "egress baseline",
                        "ok": True,
                        "detail": "replacement_pending",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "cbrs.proxy_provider.two_captcha_proxy_health",
        lambda *_args, **_kwargs: {"ok": True},
    )
    replacements = []
    monkeypatch.setattr(
        "cbrs.preflight.replace_egress_baseline",
        lambda *_args, **kwargs: replacements.append(kwargs),
    )

    assert _ensure_account_gate(
        account,
        settings,
        store,
        pool_store,
        "run",
        preflight,
        lambda *_args, **_kwargs: SimpleNamespace(ok=True),
        force=True,
    )
    assert calls == [False, True]
    assert replacements == [
        {"egress_hash": "new-sanitized-hash", "egress_country": "CL"}
    ]
    event = next(
        row
        for row in store.recent_events()
        if row["event"] == "residential_egress_rotated"
    )
    assert "new-sanitized-hash" not in event["data_json"]
    assert store.account_check(account.account_id)["proxy_status"] == "passed"


def test_generic_proxy_gate_never_auto_replaces_a_mismatched_baseline(tmp_path):
    settings = replace(
        _settings(tmp_path), proxy_url="http://user:password@proxy.test:8000"
    )
    config = _config(accounts=1)
    account = config.accounts[0]
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    store = JobStore(path)
    calls = []

    def preflight(_settings, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=False, report={})

    assert not _ensure_account_gate(
        account,
        settings,
        store,
        pool_store,
        "run",
        preflight,
        lambda *_args, **_kwargs: SimpleNamespace(ok=True),
        force=True,
    )
    assert len(calls) == 1
    state = pool_store.accounts("run")[0]
    assert state["status"] == "paused"
    assert state["resume_at"]


def test_worker_downloads_every_search_result_and_validates_artifacts(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {}
    FakeScraper.results = [
        {"ticket": "ticket-1", "foja": 10, "numero": 20, "ano": 2020},
        {"ticket": "ticket-2", "foja": 11, "numero": 21, "ano": 2021},
    ]

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.exit_code == 0
    saved = store.get_job(job["job_id"])
    assert saved["status"] == "completed"
    assert saved["result_count"] == 2
    assert saved["completed_items"] == 2
    artifacts = store.artifacts(job_id=job["job_id"])
    assert len(artifacts) == 2
    assert all(artifact["page_count"] == 2 for artifact in artifacts)
    for record in artifacts:
        raw = store.artifact_record(record["artifact_id"])
        digest, size = validate_pdf(Path(raw["path"]), expected_pages=2)
        assert digest == record["sha256"]
        assert size == record["bytes"]
    database_bytes = path.read_bytes()
    wal_path = Path(f"{path}-wal")
    if wal_path.exists():
        database_bytes += wal_path.read_bytes()
    assert b"secret-value" not in database_bytes
    assert b"example.test" not in database_bytes


def test_worker_keeps_one_headless_browser_per_account_until_worker_exit(
    tmp_path, monkeypatch
):
    class TrackingScraper(FakeScraper):
        created: dict[str, list[int]] = {}
        entered = 0
        exited = 0
        searched: list[tuple[str, int]] = []

        def __init__(self, *, settings, headless=False):
            super().__init__(settings=settings, headless=headless)
            self.created.setdefault(self.account_id, []).append(id(self))

        def __enter__(self):
            type(self).entered += 1
            return self

        def __exit__(self, *args):
            type(self).exited += 1
            return None

        def search_by_text(self, query):
            type(self).searched.append((self.account_id, id(self)))
            return []

    settings = _settings(tmp_path)
    config = _config(accounts=3)
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    for number in range(4):
        store.create_job(kind="text", input_data={"text": f"Authorized {number}"})
    TrackingScraper.created = {}
    TrackingScraper.entered = 0
    TrackingScraper.exited = 0
    TrackingScraper.searched = []

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        headless=True,
        max_jobs=4,
        poll_seconds=0,
        scraper_factory=TrackingScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.processed_jobs == 4
    assert set(TrackingScraper.created) == {"a1", "a2", "a3"}
    assert all(len(instances) == 1 for instances in TrackingScraper.created.values())
    assert TrackingScraper.entered == 3
    assert TrackingScraper.exited == 3
    for account_id, instance_id in TrackingScraper.searched:
        assert instance_id == TrackingScraper.created[account_id][0]
    assert len(TrackingScraper.searched) == 4
    assert len({instance_id for _, instance_id in TrackingScraper.searched}) == 3
    for account in config.accounts:
        check = store.account_check(account.account_id)
        assert check["browser_live"] == 0
        assert check["browser_authenticated"] == 0
        assert check["browser_headless"] == 1
        assert check["browser_status"] == "worker_stopped"


def test_endurance_fixture_publishes_only_a_clearly_named_bounded_sample(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(
        kind="fna",
        input_data={
            "foja": 9441,
            "numero": 4580,
            "year": 1980,
            "sample_pages": 1,
        },
        source="endurance",
    )
    FakeScraper.behavior = {}
    FakeScraper.results = [
        {"ticket": "ticket-1", "foja": 9441, "numero": 4580, "ano": 1980}
    ]

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.exit_code == 0
    artifact = store.artifacts(job_id=job["job_id"])[0]
    raw = store.artifact_record(artifact["artifact_id"])
    assert artifact["page_count"] == 1
    assert "_test-sample-max1p.pdf" in str(raw["path"])
    validate_pdf(Path(raw["path"]), expected_pages=1)


def test_endurance_sample_page_limit_is_bounded() -> None:
    from cbrs.jobs import normalize_job_input

    assert normalize_job_input(
        "fna",
        {"foja": 1, "numero": 2, "year": 2026, "sample_pages": 3},
    )["sample_pages"] == 3
    with pytest.raises(ValueError, match="sample_pages"):
        normalize_job_input(
            "fna",
            {"foja": 1, "numero": 2, "year": 2026, "sample_pages": 44},
        )


def test_targeted_captcha_validation_uses_only_requested_account_and_no_download(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config(accounts=2)
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    budget = CaptchaBudgetStore(
        settings.captcha_state_path,
        daily_limit=settings.two_captcha_daily_limit,
        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
    )
    budget.arm_manual(account_id="a2")
    job, _ = store.create_job(
        kind="fna",
        input_data={
            "foja": 9441,
            "numero": 4580,
            "year": 1980,
            "validation_only": True,
            "target_account_id": "a2",
        },
        source="captcha_validation",
    )
    FakeScraper.behavior = {}
    FakeScraper.results = [
        {"ticket": "ticket-1", "foja": 9441, "numero": 4580, "ano": 1980}
    ]

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=False,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    saved = store.get_job(job["job_id"])
    assert result.status == "completed"
    assert result.processed_jobs == 1
    assert saved["status"] == "completed"
    assert saved["result_count"] is None
    assert saved["completed_items"] == 0
    assert store.artifacts(job_id=job["job_id"]) == []
    assert [attempt["account_id"] for attempt in saved["attempts"]] == ["a2"]
    latest = budget.recent_activity(limit=1)[0]
    assert latest["kind"] == "authorization"
    assert latest["status"] == "not_required"
    assert latest["error_code"] == "browser_token_accepted"


def test_targeted_captcha_validation_never_fails_over_to_another_account(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config(accounts=2)
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(
        kind="fna",
        input_data={
            "foja": 9441,
            "numero": 4580,
            "year": 1980,
            "validation_only": True,
            "target_account_id": "a1",
        },
        source="captcha_validation",
    )
    FakeScraper.behavior = {
        "a1": SafetyStopException(StopReason.CAPTCHA_REJECTED, "captcha")
    }
    FakeScraper.results = []

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=False,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    saved = store.get_job(job["job_id"])
    assert result.status == "waiting_captcha"
    assert saved["status"] == "waiting_captcha"
    assert [attempt["account_id"] for attempt in saved["attempts"]] == ["a1"]
    run = pool_store.latest_run()
    states = {
        row["account_id"]: row["status"]
        for row in pool_store.accounts(run["run_id"])
    }
    assert states["a1"] == "captcha_pending"
    assert states["a2"] == "available"


def test_worker_restart_registers_an_atomically_published_pdf_without_redownload(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    claimed = store.claim_next("dead-worker")
    items = store.add_results(
        claimed.job_id,
        [{"ticket": "ticket-1", "foja": 10, "numero": 20, "ano": 2020}],
    )
    final_path = _expected_artifact_path(settings.output_dir, claimed.job_id, items[0])
    final_path.parent.mkdir(parents=True)
    recovery_image = tmp_path / "recovery_page1.jpg"
    Image.new("RGB", (16, 16), "white").save(recovery_image, "JPEG")
    create_pdf([recovery_image], final_path)
    original = final_path.read_bytes()
    store.mark_item_downloading(items[0]["item_id"], final_path)
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job["job_id"],),
        )
    FakeScraper.behavior = {}
    FakeScraper.results = []

    run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    completed = store.get_job(job["job_id"])
    assert completed["status"] == "completed"
    assert completed["account_id"] == "a1"
    assert len(completed["attempts"]) == 1
    assert completed["attempts"][0]["account_id"] == "a1"
    assert completed["attempts"][0]["status"] == "completed"
    assert completed["attempts"][0]["reason"] == "completed"
    assert final_path.read_bytes() == original
    assert len(store.artifacts(job_id=job["job_id"])) == 1


def test_worker_preserves_successful_pdfs_and_finishes_partial(tmp_path, monkeypatch):
    class PartialScraper(FakeScraper):
        def download_image(self, uuid, output_path):
            if str(uuid).startswith("ticket-2"):
                raise RuntimeError("local image decode failure")
            return super().download_image(uuid, output_path)

    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    PartialScraper.behavior = {}
    PartialScraper.results = [
        {"ticket": "ticket-1", "foja": 10, "numero": 20, "ano": 2020},
        {"ticket": "ticket-2", "foja": 11, "numero": 21, "ano": 2021},
    ]

    run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=PartialScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    saved = store.get_job(job["job_id"])
    assert saved["status"] == "partial"
    assert saved["completed_items"] == 1
    assert saved["failed_items"] == 1
    assert len(store.artifacts(job_id=job["job_id"])) == 1


def test_worker_fails_over_after_account_captcha(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {
        "a1": SafetyStopException(StopReason.CAPTCHA_REJECTED, "captcha")
    }
    FakeScraper.results = []

    run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert store.get_job(job["job_id"])["status"] == "completed"
    run = pool_store.latest_run()
    states = {row["account_id"]: row["status"] for row in pool_store.accounts(run["run_id"])}
    assert states["a1"] == "captcha_pending"
    assert sum(store.usage_by_account(_today()).values()) == 1


def test_worker_fails_over_to_next_account_after_browser_context_failure(
    tmp_path,
    monkeypatch,
):
    class FlakyBrowserScraper(FakeScraper):
        search_calls = 0

        def search_by_text(self, query):
            type(self).search_calls += 1
            if type(self).search_calls == 1:
                raise RuntimeError("Target page, context or browser has been closed")
            return []

    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})

    run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FlakyBrowserScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert store.get_job(job["job_id"])["status"] == "completed"
    with store.connect() as db:
        accounts = [
            row["account_id"]
            for row in db.execute(
                "SELECT account_id FROM job_attempts WHERE job_id = ? ORDER BY started_at, rowid",
                (job["job_id"],),
            ).fetchall()
        ]
    assert accounts == ["a1", "a2"]


def test_worker_waits_when_all_accounts_require_captcha(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {
        account.account_id: SafetyStopException(StopReason.CAPTCHA_REJECTED, "captcha")
        for account in config.accounts
    }
    FakeScraper.results = []

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.status == "waiting_captcha"
    assert store.get_job(job["job_id"])["status"] == "waiting_captcha"


def test_daily_capacity_places_next_job_in_waiting_capacity(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = _config(quota=1)
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    for value in range(4):
        store.create_job(kind="text", input_data={"text": f"Authorized {value}"})
    FakeScraper.behavior = {}
    FakeScraper.results = []

    run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        max_jobs=4,
        poll_seconds=0,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    statuses = [job["status"] for job in store.list_jobs()]
    assert statuses.count("completed") == 3
    assert statuses.count("waiting_capacity") == 1
    assert sum(store.usage_by_account(_today()).values()) == 3


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (StopReason.RATE_LIMIT, "rate_limit"),
    ],
)
def test_global_infrastructure_signal_uses_a_timed_cooldown(
    tmp_path, monkeypatch, reason, expected
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {"a1": SafetyStopException(reason, "global stop")}
    FakeScraper.results = []

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.exit_code == 0
    assert result.status == "cooldown"
    cooldown = store.global_cooldown()
    assert cooldown["reason"] == expected
    assert cooldown["resume_at"]
    assert store.get_control("global_safety_stop") is None
    saved = store.list_jobs()[0]
    assert saved["status"] == "queued"


def test_worker_restart_does_not_run_startup_gates_during_global_cooldown(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    store.set_global_cooldown("temporary_unavailable_all_accounts", 300)
    gate_calls: list[str] = []

    def forbidden_gate(*_args, **_kwargs):
        gate_calls.append("called")
        raise AssertionError("startup gate touched the portal during cooldown")

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=forbidden_gate,
        proxy_health_runner=forbidden_gate,
    )

    assert result.status == "cooldown"
    assert gate_calls == []


def test_all_accounts_temporary_unavailable_opens_external_outage_circuit(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {
        account.account_id: SafetyStopException(
            StopReason.TEMPORARY_UNAVAILABLE, "portal unavailable"
        )
        for account in config.accounts
    }
    FakeScraper.results = []

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.status == "cooldown"
    assert sum(store.usage_by_account(_today()).values()) == 0
    assert store.list_jobs()[0]["status"] == "queued"
    assert store.global_cooldown()["reason"] == "temporary_unavailable_all_accounts"
    backoff = json.loads(store.get_control("external_outage_backoff")["value"])
    assert backoff["streak"] == 1


def test_external_outage_backoff_progresses_and_resets(tmp_path) -> None:
    store = JobStore(tmp_path / "pool.sqlite3")

    assert [store.advance_external_outage_backoff()["seconds"] for _ in range(5)] == [
        300.0,
        900.0,
        3600.0,
        3600.0,
        3600.0,
    ]

    store.clear_external_outage_backoff()
    assert store.advance_external_outage_backoff()["seconds"] == 300.0


def test_expired_account_cooldown_reactivates_but_daily_limit_does_not(
    tmp_path,
):
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    config = _config(accounts=2)
    pool_store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    pool_store.pause_account("live", "a1", reason="proxy_health_failed", cooldown_seconds=1)
    pool_store.pause_account("live", "a2", reason="daily_limit")
    with pool_store._connect() as db:
        db.execute(
            "UPDATE accounts SET resume_at = '2000-01-01T00:00:00+00:00' WHERE account_id = 'a1'"
        )

    assert pool_store.reactivate_expired_cooldowns("live") == 1
    states = {
        row["account_id"]: row for row in pool_store.accounts("live")
    }
    assert states["a1"]["status"] == "available"
    assert states["a1"]["resume_at"] is None
    assert states["a2"]["status"] == "paused"
    assert states["a2"]["paused_reason"] == "daily_limit"


def test_expired_worker_lease_recovery_never_clears_an_active_lease(tmp_path) -> None:
    store = JobStore(tmp_path / "pool.sqlite3")
    assert store.acquire_lease("portal_worker", "worker-a") is True
    assert store.clear_expired_lease() is False

    with store.connect() as db:
        db.execute(
            "UPDATE leases SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE lease_name = 'portal_worker'"
        )

    assert store.clear_expired_lease() is True
    assert store.lease() is None


def test_solver_failure_is_account_scoped_and_next_account_continues(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {
        "a1": SafetyStopException(StopReason.CAPTCHA_SOLVER, "solver unavailable")
    }
    FakeScraper.results = []

    result = run_job_worker(
        settings=settings,
        config=config,
        store=store,
        pool_store=pool_store,
        once=True,
        scraper_factory=FakeScraper,
        preflight_runner=_gate,
        proxy_health_runner=_gate,
    )

    assert result.exit_code == 0
    assert store.get_control("global_safety_stop") is None
    attempts = store.get_job(job["job_id"])["attempts"]
    assert [attempt["account_id"] for attempt in attempts] == ["a1", "a2"]


def test_jobs_api_is_loopback_idempotent_and_cancellable(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings = replace(
        settings,
        proxy_url="http://private-proxy-user:private-proxy-password@198.51.100.10:8080",
    )
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    assert store.acquire_lease(WORKER_LEASE_NAME, "worker-test")
    for account in config.accounts:
        baseline_path = (
            settings.profile_dir.parent
            / "accounts"
            / account.account_id
            / "fixed-egress-baseline.json"
        )
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "schema": "cbrs-fixed-egress-baseline-v1",
                    "egress_hash": f"private-hash-{account.account_id}",
                    "egress_country": "CL",
                }
            ),
            encoding="utf-8",
        )
        store.set_account_check(
            account.account_id,
            proxy_status="passed",
            egress_hash=f"private-hash-{account.account_id}",
        )
        store.set_account_browser_state(
            account.account_id,
            live=True,
            authenticated=True,
            headless=True,
            owner="worker-test",
            status="authenticated",
        )
    with pytest.raises(ValueError, match="loopback"):
        start_pool_dashboard(
            pool_store,
            settings=settings,
            config=config,
            host="0.0.0.0",
            port=0,
            job_store=store,
        )

    private_dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="0.0.0.0",
        port=0,
        job_store=store,
        allow_private_bind=True,
    )
    private_dashboard.stop()

    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        body = json.dumps(
            {"kind": "text", "text": "Private query", "idempotency_key": "api-1"}
        ).encode()
        request = Request(
            f"{dashboard.url}/api/jobs",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            assert response.status == 202
            created = json.load(response)
        with urlopen(request) as response:
            repeated = json.load(response)
        assert repeated["job_id"] == created["job_id"]

        instant = Request(
            f"{dashboard.url}/api/jobs/instant",
            data=json.dumps(
                {"kind": "fna", "foja": 10, "numero": 20, "year": 2020}
            ).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(instant) as response:
            instant_created = json.load(response)
        assert instant_created["priority"] == 1
        assert instant_created["worker_requested"] is True
        assert (settings.profile_dir.parent / "control" / "resume.request").read_text() == "resume\n"

        instant_text = Request(
            f"{dashboard.url}/api/jobs/instant",
            data=json.dumps({"kind": "text", "text": "Immediate company"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(instant_text) as response:
            instant_text_created = json.load(response)
        assert instant_text_created["priority"] == 1

        conflict_body = json.dumps(
            {"kind": "text", "text": "Different query", "idempotency_key": "api-1"}
        ).encode()
        conflict = Request(
            f"{dashboard.url}/api/jobs",
            data=conflict_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(conflict)
        assert error.value.code == 409

        with urlopen(f"{dashboard.url}/api/jobs/{created['job_id']}") as response:
            status = json.load(response)
        assert status["request"] == {"text_saved": True}
        assert "Private query" not in json.dumps(status)

        with urlopen(f"{dashboard.url}/api/status") as response:
            dashboard_status = json.load(response)
        accounts = {row["account_id"]: row for row in dashboard_status["accounts"]}
        assert accounts["a1"]["username_prefix"] == "a1"
        assert accounts["a1"]["proxy_provider"] == "generic_static"
        assert accounts["a1"]["proxy_brand"] is None
        assert accounts["a1"]["proxy_health_status"] == "passed"
        assert accounts["a1"]["egress_baseline_status"] == "matched"
        assert accounts["a1"]["egress_country"] == "CL"
        assert accounts["a1"]["proxy_endpoint"] == "198.51.100.10:8080"
        assert accounts["a1"]["egress_route_id"].startswith("ip-")
        assert len(accounts["a1"]["egress_route_id"]) == 13
        assert accounts["a1"]["proxy_checked_at"]
        assert accounts["a1"]["browser_live"] is True
        assert accounts["a1"]["browser_authenticated"] is True
        assert accounts["a1"]["worker_active"] is True
        assert accounts["a1"]["browser_mode"] == "headless"
        assert accounts["a1"]["browser_status"] == "authenticated"
        assert accounts["a1"]["browser_started_at"]
        assert accounts["a1"]["browser_checked_at"]
        assert len({account["egress_route_id"] for account in accounts.values()}) == 3
        assert dashboard_status["proxy_provider"] == {
            "provider": "generic_static",
            "status": "not_applicable",
            "ok": True,
            "configured_accounts": 0,
        }
        assert "a1@example.test" not in json.dumps(dashboard_status)
        assert "private-hash" not in json.dumps(dashboard_status)
        assert "private-proxy-user" not in json.dumps(dashboard_status)
        assert "private-proxy-password" not in json.dumps(dashboard_status)

        cancel = Request(
            f"{dashboard.url}/api/jobs/{created['job_id']}/cancel",
            data=b"",
            method="POST",
        )
        with urlopen(cancel) as response:
            assert json.load(response)["status"] == "cancelled"
    finally:
        dashboard.stop()


def test_dashboard_exposes_only_redacted_2captcha_provider_health(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    base_config = _config()
    config = replace(
        base_config,
        accounts=tuple(
            replace(account, proxy_provider="2captcha_residential_sticky")
            for account in base_config.accounts
        ),
    )
    _credentials(monkeypatch, config)
    monkeypatch.setattr(
        "cbrs.proxy_provider.two_captcha_proxy_health",
        lambda api_key, **_kwargs: {
            "provider": "2captcha_residential_sticky",
            "status": "healthy",
            "ok": True,
            "account_active": True,
            "traffic_remaining": True,
            "remaining_ratio": 0.75,
            "checked_at": "2026-08-29T00:00:00+00:00",
            "error_code": None,
        },
    )
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    for account in config.accounts:
        baseline_path = (
            settings.profile_dir.parent
            / "accounts"
            / account.account_id
            / "fixed-egress-baseline.json"
        )
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "schema": "cbrs-fixed-egress-baseline-v1",
                    "egress_hash": f"private-hash-{account.account_id}",
                    "egress_country": "CL",
                }
            ),
            encoding="utf-8",
        )
        store.set_account_check(
            account.account_id,
            proxy_status="passed",
            egress_hash=f"private-hash-{account.account_id}",
        )
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        with urlopen(f"{dashboard.url}/api/status") as response:
            payload = json.load(response)
    finally:
        dashboard.stop()

    assert payload["proxy_provider"]["status"] == "healthy"
    assert payload["proxy_provider"]["configured_accounts"] == 3
    assert all(
        account["proxy_provider"] == "2captcha_residential_sticky"
        for account in payload["accounts"]
    )
    serialized = json.dumps(payload)
    for forbidden in (
        "api_key",
        "proxy_url",
        "egress_hash",
        "secret-value",
        "a1@example.test",
    ):
        assert forbidden not in serialized


def test_dashboard_production_settings_are_safe_validated_and_persisted(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    state_root = settings.profile_dir.parent
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "account-pool.json").write_text(
        json.dumps(
            {
                "selection_policy": "round_robin",
                "daily_quota_per_account": 20,
                "accounts": [
                    {
                        "id": account.account_id,
                        "label": account.label,
                        "username_env": account.username_env,
                        "password_env": account.password_env,
                        "daily_quota": account.daily_quota,
                    }
                    for account in config.accounts
                ],
            }
        ),
        encoding="utf-8",
    )
    (state_root / "endurance-plan.json").write_text(
        json.dumps(
            {
                "enabled": False,
                "cooldown_seconds": 300,
                "jobs_per_account_per_day": 15,
                "production_reserve_per_account": 5,
                "max_outstanding_jobs": 1,
                "no_catch_up": True,
                "fixtures": [],
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    payload = {
        "pool": {
            "daily_quota_per_account": 20,
            "human_like_behavior_enabled": False,
            "job_interval_min_seconds": 4,
            "job_interval_max_seconds": 12,
            "worker_poll_seconds": 2.5,
            "max_queued_production_jobs": 40,
            "instant_jobs_enabled": False,
        },
        "endurance": {
            "enabled": False,
            "cooldown_seconds": 300,
            "jobs_per_account_per_day": 14,
            "production_reserve_per_account": 6,
        },
    }
    try:
        with urlopen(f"{dashboard.url}/api/settings") as response:
            before = json.load(response)
        assert before["locked"]["selection_policy"] == "round_robin"
        assert before["locked"]["dashboard_bind"] == "loopback_only"
        assert "username" not in json.dumps(before).lower()
        assert "password" not in json.dumps(before).lower()

        request = Request(
            f"{dashboard.url}/api/settings",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            saved = json.load(response)
        assert saved["status"] == "settings_saved"
        assert saved["settings"]["pool"]["human_like_behavior_enabled"] is False
        assert saved["settings"]["pool"]["max_queued_production_jobs"] == 40
        assert saved["settings"]["endurance"]["cooldown_seconds"] == 300

        pool_file = json.loads((state_root / "account-pool.json").read_text())
        endurance_file = json.loads((state_root / "endurance-plan.json").read_text())
        assert pool_file["worker_poll_seconds"] == 2.5
        assert pool_file["selection_policy"] == "round_robin"
        assert endurance_file["max_outstanding_jobs"] == 1
        assert endurance_file["no_catch_up"] is True

        pool_store.create_run(
            run_id="active",
            dry_run=False,
            config=config,
            dashboard_url=None,
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 409
    finally:
        dashboard.stop()


def test_dashboard_enforces_queue_limit_and_instant_job_switch(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = replace(
        _config(),
        max_queued_production_jobs=1,
        instant_jobs_enabled=False,
    )
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        body = json.dumps({"kind": "text", "text": "First"}).encode()
        with urlopen(
            Request(
                f"{dashboard.url}/api/jobs",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
        ) as response:
            assert response.status == 202
        with pytest.raises(HTTPError) as queue_error:
            urlopen(
                Request(
                    f"{dashboard.url}/api/jobs",
                    data=json.dumps({"kind": "text", "text": "Second"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
            )
        assert queue_error.value.code == 409

        with pytest.raises(HTTPError) as instant_error:
            urlopen(
                Request(
                    f"{dashboard.url}/api/jobs/instant",
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
            )
        assert instant_error.value.code == 409
    finally:
        dashboard.stop()


def test_dashboard_manual_captcha_button_arms_exactly_one_solve(tmp_path, monkeypatch):
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": str(tmp_path / "state" / "chrome-profile"),
            "CBRS_OUTPUT_DIR": str(tmp_path / "outputs"),
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    pool_store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    pool_store.mark_account_captcha_pending("live", "a1")
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    store.set_waiting(job["job_id"], "waiting_captcha", reason="captcha_rejected")
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        request = Request(
            f"{dashboard.url}/api/captcha/a1/solve-external", data=b"", method="POST"
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 202
            payload = json.load(response)
    finally:
        dashboard.stop()

    budget = CaptchaBudgetStore(
        settings.captcha_state_path,
        daily_limit=settings.two_captcha_daily_limit,
        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
    )
    assert payload["status"] == "one_solve_armed"
    assert payload["released_jobs"] == 1
    assert budget.manual_armed(account_id="a1") is True
    assert store.get_job(job["job_id"])["status"] == "queued"
    account = next(row for row in pool_store.accounts("live") if row["account_id"] == "a1")
    assert account["status"] == "available"


def test_dashboard_automatic_captcha_toggle_releases_waiting_accounts(
    tmp_path, monkeypatch
):
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": str(tmp_path / "state" / "chrome-profile"),
            "CBRS_OUTPUT_DIR": str(tmp_path / "outputs"),
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    pool_store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    pool_store.mark_account_captcha_pending("live", "a1")
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    store.set_waiting(job["job_id"], "waiting_captcha", reason="captcha_rejected")
    worker_requests: list[bool] = []
    monkeypatch.setattr(
        "cbrs.account_pool_dashboard._request_worker_resume",
        lambda _settings: worker_requests.append(True),
    )
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        request = Request(
            f"{dashboard.url}/api/captcha/automatic",
            data=json.dumps({"enabled": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
        with urlopen(f"{dashboard.url}/api/status", timeout=5) as response:
            status = json.load(response)
    finally:
        dashboard.stop()

    assert payload == {
        "ok": True,
        "automatic_enabled": True,
        "released_jobs": 1,
        "worker_requested": True,
    }
    assert worker_requests == [True]
    assert store.get_job(job["job_id"])["status"] == "queued"
    account = next(
        row for row in pool_store.accounts("live") if row["account_id"] == "a1"
    )
    assert account["status"] == "available"
    assert status["captcha_solver"]["automatic_enabled"] is True


def test_dashboard_automatic_captcha_toggle_supports_capsolver(
    tmp_path, monkeypatch
):
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": str(tmp_path / "state" / "chrome-profile"),
            "CBRS_OUTPUT_DIR": str(tmp_path / "outputs"),
            "CBRS_CAPTCHA_SOLVER_MODE": "capsolver_manual",
            "CBRS_CAPSOLVER_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    pool_store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    worker_requests: list[bool] = []
    monkeypatch.setattr(
        "cbrs.account_pool_dashboard._request_worker_resume",
        lambda _settings: worker_requests.append(True),
    )
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        request = Request(
            f"{dashboard.url}/api/captcha/automatic",
            data=json.dumps({"enabled": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
        with urlopen(f"{dashboard.url}/api/status", timeout=5) as response:
            status = json.load(response)
    finally:
        dashboard.stop()

    assert payload["automatic_enabled"] is True
    assert payload["worker_requested"] is True
    assert worker_requests == [True]
    assert store.get_job(job["job_id"])["status"] == "queued"
    assert status["captcha_solver"]["automatic_enabled"] is True


def test_dashboard_queues_targeted_validation_when_no_blocked_job_remains(
    tmp_path, monkeypatch
):
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": str(tmp_path / "state" / "chrome-profile"),
            "CBRS_OUTPUT_DIR": str(tmp_path / "outputs"),
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    pool_store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    pool_store.mark_account_captcha_pending("live", "a1")
    worker_requests: list[bool] = []
    monkeypatch.setattr(
        "cbrs.account_pool_dashboard._request_worker_resume",
        lambda _settings: worker_requests.append(True),
    )
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        request = Request(
            f"{dashboard.url}/api/captcha/a1/solve-external", data=b"", method="POST"
        )
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
        with urlopen(f"{dashboard.url}/api/status", timeout=5) as response:
            status = json.load(response)
    finally:
        dashboard.stop()

    assert payload["ok"] is True
    assert payload["status"] == "captcha_validation_queued"
    assert payload["account_id"] == "a1"
    assert payload["released_jobs"] == 0
    assert payload["worker_requested"] is True
    validation_job = store.get_job(payload["validation_job_id"], include_input=True)
    assert validation_job["source"] == "captcha_validation"
    assert validation_job["input"]["validation_only"] is True
    assert validation_job["input"]["target_account_id"] == "a1"
    assert worker_requests == [True]
    latest = status["captcha_attempts"][0]
    assert latest["kind"] == "authorization"
    assert latest["status"] == "armed"
    assert latest["portal_status"] is None
    assert status["captcha_solver"]["attempts"] == 0


def test_jobs_api_rejects_artifact_outside_job_output_root(tmp_path):
    settings = _settings(tmp_path)
    config = _config()
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "A"})
    store.add_results(job["job_id"], [{"ticket": "t", "foja": 1}])
    item = store.items(job["job_id"], public=False)[0]
    outside = tmp_path / "outside.pdf"
    outside_image = tmp_path / "outside_page1.jpg"
    Image.new("RGB", (16, 16), "white").save(outside_image, "JPEG")
    create_pdf([outside_image], outside)
    digest, size = validate_pdf(outside, expected_pages=1)
    artifact = store.complete_item(
        item["item_id"],
        expected_pages=1,
        output_path=outside,
        sha256=digest,
        bytes_count=size,
    )
    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        host="127.0.0.1",
        port=0,
        job_store=store,
    )
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(f"{dashboard.url}/api/artifacts/{artifact['artifact_id']}")
        assert error.value.code == 404
    finally:
        dashboard.stop()


def test_backup_uses_online_sqlite_snapshot_and_reports_health(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    database = tmp_path / "pool.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE evidence(value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('kept')")
    (settings.output_dir / "jobs").mkdir(parents=True)
    (settings.output_dir / "jobs" / "artifact.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("cbrs.backup.shutil.which", lambda name: "/usr/bin/restic")
    monkeypatch.setattr(
        "cbrs.backup.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100, used=50, free=50),
    )
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = run_backup(
        settings=settings,
        database_path=database,
        env={"RESTIC_REPOSITORY": "/backup", "RESTIC_PASSWORD": "do-not-log"},
        command_runner=runner,
    )

    assert result["ok"] is True
    assert calls[0][0][1:3] == ["backup", "--tag"]
    assert "do-not-log" not in json.dumps(result)
    assert backup_health(settings)["status"] == "healthy"


def test_backup_restore_verification_is_temporary_and_checks_sqlite_and_pdf(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"test")

    def runner(command, **kwargs):
        target = Path(command[command.index("--target") + 1])
        restored = target / "G" / "CBRS" / "backup" / "snapshot"
        restored.mkdir(parents=True)
        db = sqlite3.connect(restored / "cbrs.sqlite3")
        try:
            db.execute("CREATE TABLE evidence(value TEXT)")
            db.execute("INSERT INTO evidence VALUES ('restored')")
            db.commit()
        finally:
            db.close()
        output = target / "G" / "CBRS" / "outputs" / "jobs" / "job-1"
        output.mkdir(parents=True)
        (output / "artifact.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        return SimpleNamespace(returncode=0, stdout="restored", stderr="")

    result = verify_backup_restore(
        settings=settings,
        require_pdf=True,
        env={
            "RESTIC_REPOSITORY": str(tmp_path / "repository"),
            "CBRS_RESTIC_EXECUTABLE_PATH": str(restic),
        },
        command_runner=runner,
    )

    assert result["ok"] is True
    assert result["database_quick_check"] is True
    assert result["pdf_count"] == 1
    assert not list((settings.profile_dir.parent / "backup").glob("restore-verify-*"))
    health = backup_health(settings)
    assert health["restore_status"] == "verified"
    assert health["restored_pdf_count"] == 1


def _today() -> str:
    from cbrs.account_pool import local_today

    return local_today()
