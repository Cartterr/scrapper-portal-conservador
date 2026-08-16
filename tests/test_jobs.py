from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig, PoolTarget
from cbrs.account_pool_dashboard import start_pool_dashboard
from cbrs.backup import backup_health, run_backup
from cbrs.config import load_settings
from cbrs.jobs import (
    IdempotencyConflictError,
    JobStore,
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
    AccountPoolStore(path)
    store = JobStore(path)
    job, _ = store.create_job(kind="text", input_data={"text": "A"})
    claimed = store.claim_next("worker")
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
    assert sum(store.usage_by_account(_today()).values()) == 2


def test_worker_restarts_one_failed_browser_context_with_the_same_profile(
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
    assert accounts == ["a1", "a1"]


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


def test_global_rate_limit_stops_worker(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
    store.create_job(kind="text", input_data={"text": "Authorized"})
    FakeScraper.behavior = {
        "a1": SafetyStopException(StopReason.RATE_LIMIT, "rate limited")
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

    assert result.exit_code == 2
    assert result.status == "safety_stop"
    assert store.get_control("global_safety_stop")["value"] == "rate_limit"


def test_jobs_api_is_loopback_idempotent_and_cancellable(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    config = _config()
    _credentials(monkeypatch, config)
    path = tmp_path / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    store = JobStore(path)
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
        assert "a1@example.test" not in json.dumps(dashboard_status)

        cancel = Request(
            f"{dashboard.url}/api/jobs/{created['job_id']}/cancel",
            data=b"",
            method="POST",
        )
        with urlopen(cancel) as response:
            assert json.load(response)["status"] == "cancelled"
    finally:
        dashboard.stop()


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


def _today() -> str:
    from cbrs.account_pool import local_today

    return local_today()
