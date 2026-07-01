import json
import threading
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
                        "proxy_url": "http://user:pass@example.test:33335",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credentials"):
        load_account_pool_config(settings, path=config_path)


def test_pool_account_proxy_url_env_resolves_to_account_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cbrs.account_pool import account_settings, load_account_pool_config

    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
        },
        root=tmp_path,
    )
    config_path = tmp_path / ".cbrs" / "account-pool.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "ejecutivo_1",
                        "label": "Ejecutivo 1",
                        "proxy_url_env": "CBRS_EJECUTIVO_1_PROXY_URL",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "CBRS_EJECUTIVO_1_PROXY_URL",
        "http://user:pass@example.test:33335",
    )

    config = load_account_pool_config(settings, path=config_path)
    runtime_settings = account_settings(settings, config.accounts[0])

    assert config.accounts[0].proxy_url_env == "CBRS_EJECUTIVO_1_PROXY_URL"
    assert runtime_settings.proxy_url == "http://user:pass@example.test:33335"


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


def test_pool_captcha_rejected_marks_only_affected_account_pending(tmp_path: Path) -> None:
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
    assert accounts["ejecutivo_1"]["status"] == "captcha_pending"
    assert accounts["ejecutivo_1"]["paused_reason"] == "captcha_rejected"
    assert accounts["ejecutivo_2"]["status"] == "available"
    assert accounts["ejecutivo_3"]["status"] == "available"
    assert status["stats"]["downloads"] == 3
    assert status["pool"]["captcha_pending_accounts"] == 1
    assert status["alert"]["title"] == "Captcha pendiente"
    assert status["alert"]["reason"] == "captcha_rejected"


def test_manual_captcha_recovery_reenables_account_after_success(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        PoolConfig,
        PoolTarget,
        dashboard_status,
        load_account_pool_config,
        mark_account_captcha_pending,
        resolve_account_captcha,
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
    run_id = "pool-test"
    store.create_run(run_id=run_id, dry_run=False, config=config, dashboard_url=None)
    mark_account_captcha_pending(
        store,
        run_id,
        "ejecutivo_1",
        reason="captcha_rejected",
    )
    calls: list[bool | None] = []

    def fake_runner(**kwargs: object) -> ValidationRunResult:
        calls.append(kwargs.get("headless"))
        report_path = tmp_path / ".cbrs" / "logs" / "validation-recovery.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return ValidationRunResult(
            exit_code=0,
            status="passed",
            report={},
            report_path=report_path,
            preflight_report_path=None,
            result_count=1,
        )

    result = resolve_account_captcha(
        settings=settings,
        config=config,
        store=store,
        run_id=run_id,
        account_id="ejecutivo_1",
        validation_runner=fake_runner,
    )

    status = dashboard_status(store, config=config)
    accounts = {account["account_id"]: account for account in status["accounts"]}
    assert result["status"] == "resolved"
    assert calls == [False]
    assert accounts["ejecutivo_1"]["status"] == "available"
    assert accounts["ejecutivo_1"]["paused_reason"] is None
    assert status["pool"]["captcha_pending_accounts"] == 0


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


def test_pool_dashboard_can_trigger_manual_captcha_recovery(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        load_account_pool_config,
        mark_account_captcha_pending,
    )
    from cbrs.account_pool_dashboard import start_pool_dashboard

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    config = load_account_pool_config(settings)
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    run_id = "pool-test"
    store.create_run(run_id=run_id, dry_run=False, config=config, dashboard_url=None)
    mark_account_captcha_pending(
        store,
        run_id,
        "ejecutivo_1",
        reason="captcha_rejected",
    )
    called = threading.Event()
    calls: list[str] = []

    def fake_resolver(**kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["account_id"]))
        called.set()
        return {"ok": True, "status": "resolved"}

    dashboard = start_pool_dashboard(
        store,
        settings=settings,
        config=config,
        port=0,
        captcha_resolver=fake_resolver,
    )
    try:
        with urlopen(f"{dashboard.url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        request = Request(
            f"{dashboard.url}/api/captcha/ejecutivo_1/trigger",
            data=b"",
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert called.wait(timeout=2)
    finally:
        dashboard.stop()

    assert "Resolver captcha" in html
    assert payload == {"ok": True, "status": "started", "account_id": "ejecutivo_1"}
    assert calls == ["ejecutivo_1"]
