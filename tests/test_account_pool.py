import json
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from cbrs.config import load_settings
from cbrs.validation import ValidationRunResult


def test_pool_config_defaults_to_three_nominal_accounts(tmp_path: Path) -> None:
    from cbrs.account_pool import account_settings, load_account_pool_config

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )

    config = load_account_pool_config(settings)

    assert [account.account_id for account in config.accounts] == [
        "ejecutivo_1",
        "ejecutivo_2",
        "ejecutivo_3",
    ]
    assert [account.label for account in config.accounts] == [
        "Ejecutivo 1",
        "Ejecutivo 2",
        "Ejecutivo 3",
    ]
    assert config.daily_quota_per_account == 20
    assert config.pool_daily_quota == 60
    assert config.interval_minutes == 5
    assert (
        account_settings(settings, config.accounts[0]).profile_dir
        == tmp_path / ".cbrs" / "accounts" / "ejecutivo_1" / "chrome-profile"
    )


def test_pool_config_rejects_credentials_and_emails(tmp_path: Path) -> None:
    from cbrs.account_pool import load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / ".cbrs" / "account-pool.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "ejecutivo_1",
                        "label": "Ejecutivo 1",
                        "email": "person@example.test",
                        "password": "secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credentials"):
        load_account_pool_config(settings, path=config_path)


def test_pool_dry_run_distributes_cycles_and_tracks_daily_capacity(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        PoolConfig,
        PoolTarget,
        dashboard_status,
        load_account_pool_config,
        run_account_pool,
    )

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    base_config = load_account_pool_config(settings)
    config = PoolConfig(
        accounts=base_config.accounts,
        daily_quota_per_account=20,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(PoolTarget(label="safe_text", kind="text", query="BANCO DE CHILE"),),
    )
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")

    result = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=True,
        max_cycles=6,
    )

    status = dashboard_status(store, config=config)
    usage = {account["account_id"]: account["used_today"] for account in status["accounts"]}
    assert result.status == "completed"
    assert status["pool"]["used_today"] == 6
    assert status["pool"]["remaining_today"] == 54
    assert status["pool"]["daily_quota"] == 60
    assert usage == {"ejecutivo_1": 2, "ejecutivo_2": 2, "ejecutivo_3": 2}
    assert status["stats"]["downloads"] == 6
    assert len(status["artifacts"]) == 6


def test_pool_safety_stop_pauses_only_affected_account(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        PoolConfig,
        PoolTarget,
        dashboard_status,
        load_account_pool_config,
        run_account_pool,
    )

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    base_config = load_account_pool_config(settings)
    config = PoolConfig(
        accounts=base_config.accounts,
        daily_quota_per_account=20,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(PoolTarget(label="safe_text", kind="text", query="BANCO DE CHILE"),),
    )
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    calls: list[str] = []

    def fake_runner(**kwargs: object) -> ValidationRunResult:
        account_id = Path(kwargs["output_dir"]).parent.name
        calls.append(account_id)
        report_path = tmp_path / ".cbrs" / "logs" / f"validation-{len(calls)}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        if account_id == "ejecutivo_1":
            return ValidationRunResult(
                exit_code=2,
                status="safety_stop",
                report={},
                report_path=report_path,
                preflight_report_path=None,
                safety_stop="captcha_rejected",
                error="captcha challenge",
            )
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / "fake.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return ValidationRunResult(
            exit_code=0,
            status="passed",
            report={},
            report_path=report_path,
            preflight_report_path=None,
            result_count=1,
            pdf_path=pdf_path,
            pdf_size_bytes=pdf_path.stat().st_size,
        )

    result = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=False,
        max_cycles=4,
        validation_runner=fake_runner,
    )

    status = dashboard_status(store, config=config)
    accounts = {account["account_id"]: account for account in status["accounts"]}
    assert result.status == "completed"
    assert calls[0] == "ejecutivo_1"
    assert "ejecutivo_1" not in calls[1:]
    assert accounts["ejecutivo_1"]["status"] == "paused"
    assert accounts["ejecutivo_1"]["paused_reason"] == "captcha_rejected"
    assert accounts["ejecutivo_2"]["status"] == "available"
    assert accounts["ejecutivo_3"]["status"] == "available"
    assert status["stats"]["downloads"] == 3
    assert status["pool"]["paused_accounts"] == 1


def test_pool_daily_usage_survives_runner_restart(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        PoolConfig,
        PoolTarget,
        dashboard_status,
        load_account_pool_config,
        run_account_pool,
    )

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    base_config = load_account_pool_config(settings)
    config = PoolConfig(
        accounts=base_config.accounts,
        daily_quota_per_account=20,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(PoolTarget(label="safe_text", kind="text", query="BANCO DE CHILE"),),
    )
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")

    first = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=True,
        max_cycles=1,
    )
    second = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=True,
        max_cycles=1,
    )

    status = dashboard_status(store, config=config)
    usage = {account["account_id"]: account["used_today"] for account in status["accounts"]}
    assert first.run_id != second.run_id
    assert status["run"]["run_id"] == second.run_id
    assert status["stats"]["total_cycles"] == 1
    assert status["pool"]["used_today"] == 2
    assert status["pool"]["remaining_today"] == 58
    assert usage["ejecutivo_1"] == 1
    assert usage["ejecutivo_2"] == 1


def test_live_scraper_cache_reuses_account_browser(tmp_path: Path) -> None:
    from cbrs.account_pool import _LiveScraperCache

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    created: list[object] = []

    class FakeReusableScraper:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.close_count = 0
            created.append(self)

        def close_browser(self) -> None:
            self.close_count += 1

    cache = _LiveScraperCache(scraper_cls=FakeReusableScraper)
    factory = cache.factory_for("ejecutivo_1")

    first = factory(headless=False, settings=settings)
    second = factory(headless=True, settings=settings)

    assert first is second
    assert len(created) == 1
    assert first.kwargs["close_browser_on_exit"] is False

    cache.close_all()

    assert first.close_count == 1


def test_pool_waits_when_daily_capacity_is_exhausted(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        PoolConfig,
        PoolTarget,
        dashboard_status,
        load_account_pool_config,
        run_account_pool,
    )

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    base_config = load_account_pool_config(settings)
    config = PoolConfig(
        accounts=base_config.accounts,
        daily_quota_per_account=1,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(PoolTarget(label="safe_text", kind="text", query="BANCO DE CHILE"),),
    )
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")

    result = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=True,
        max_cycles=4,
    )

    status = dashboard_status(store, config=config)
    assert result.status == "waiting_capacity"
    assert status["status"] == "waiting_capacity"
    assert status["pool"]["used_today"] == 3
    assert status["pool"]["remaining_today"] == 0
    assert status["stats"]["total_cycles"] == 3
    assert status["alert"]["title"] == "Pool diario agotado"


def test_pool_dashboard_api_and_html_are_sanitized(tmp_path: Path) -> None:
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config, run_account_pool
    from cbrs.account_pool_dashboard import start_pool_dashboard

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    config = load_account_pool_config(settings)
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    run_account_pool(settings=settings, config=config, store=store, dry_run=True, max_cycles=1)

    dashboard = start_pool_dashboard(store, settings=settings, config=config, port=0)
    try:
        with urlopen(f"{dashboard.url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        with urlopen(f"{dashboard.url}/api/status", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        artifact = payload["artifacts"][0]
        with urlopen(f"{dashboard.url}/artifact/{artifact['cycle_id']}", timeout=5) as response:
            content = response.read()
        request = Request(f"{dashboard.url}/api/stop", method="POST")
        with urlopen(request, timeout=5) as response:
            stop_payload = json.loads(response.read().decode("utf-8"))
    finally:
        dashboard.stop()

    serialized = json.dumps(payload)
    assert "Pool de Consultas CBRS" in html
    assert "Consultas disponibles hoy" in html
    assert "Ejecutivo 1" in html
    assert "email" not in serialized.lower()
    assert "password" not in serialized.lower()
    assert payload["pool"]["daily_quota"] == 60
    assert content.startswith(b"%PDF")
    assert stop_payload == {"ok": True, "status": "stop_requested"}
