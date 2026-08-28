import json
import threading
from dataclasses import replace
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
    assert config.allow_live_repetition is False
    assert (
        account_settings(settings, config.accounts[0]).profile_dir
        == tmp_path / ".cbrs" / "accounts" / "ejecutivo_1" / "chrome-profile"
    )


def test_pool_config_loads_and_validates_production_controls(tmp_path: Path) -> None:
    from cbrs.account_pool import load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    path = tmp_path / "account-pool.json"
    payload = {
        "human_like_behavior_enabled": False,
        "worker_poll_seconds": 2.5,
        "max_queued_production_jobs": 35,
        "instant_jobs_enabled": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_account_pool_config(settings, path=path)

    assert config.human_like_behavior_enabled is False
    assert config.worker_poll_seconds == 2.5
    assert config.max_queued_production_jobs == 35
    assert config.instant_jobs_enabled is False

    path.write_text(json.dumps({**payload, "worker_poll_seconds": 0}), encoding="utf-8")
    with pytest.raises(ValueError, match="worker_poll_seconds"):
        load_account_pool_config(settings, path=path)

    path.write_text(
        json.dumps({**payload, "max_queued_production_jobs": 10_001}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_queued_production_jobs"):
        load_account_pool_config(settings, path=path)


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


def test_pool_account_supports_only_secret_references_and_per_account_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cbrs.account_pool import account_credentials, account_settings, load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / "pool.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "executive",
                        "username_env": "CBRS_EXECUTIVE_USERNAME",
                        "password_env": "CBRS_EXECUTIVE_PASSWORD",
                        "proxy_url_env": "CBRS_EXECUTIVE_PROXY_URL",
                        "profile_dir": "executive-profile",
                        "daily_quota": 7,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CBRS_EXECUTIVE_USERNAME", "private-user")
    monkeypatch.setenv("CBRS_EXECUTIVE_PASSWORD", "private-password")
    monkeypatch.setenv("CBRS_EXECUTIVE_PROXY_URL", "http://user:pass@example.test:8080")

    loaded = load_account_pool_config(settings, path=config_path)
    account = loaded.accounts[0]

    assert loaded.pool_daily_quota == 7
    assert account_credentials(account) == ("private-user", "private-password")
    assert account_settings(settings, account).profile_dir == (
        tmp_path / ".cbrs" / "accounts" / "executive-profile"
    )


def test_pool_config_rejects_shared_proxy_reference_between_enabled_accounts(
    tmp_path: Path,
) -> None:
    from cbrs.account_pool import load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / "pool.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {"id": "one", "proxy_url_env": "CBRS_SHARED_PROXY"},
                    {"id": "two", "proxy_url_env": "CBRS_SHARED_PROXY"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="distinct proxy_url_env"):
        load_account_pool_config(settings, path=config_path)


def test_pool_config_requires_explicit_group_for_shared_proxy_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cbrs.account_pool import load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / "pool.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {"id": "one", "proxy_url_env": "CBRS_PROXY_ONE"},
                    {"id": "two", "proxy_url_env": "CBRS_PROXY_TWO"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CBRS_PROXY_ONE", "http://user:pass@example.test:8080")
    monkeypatch.setenv("CBRS_PROXY_TWO", "http://user:pass@example.test:8080")

    with pytest.raises(ValueError, match="same egress_group"):
        load_account_pool_config(settings, path=config_path)


def test_pool_config_allows_explicit_shared_egress_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cbrs.account_pool import AccountPoolStore, dashboard_status, load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / "pool.json"
    config_path.write_text(
        json.dumps(
            {
                "daily_quota_per_account": 20,
                "accounts": [
                    {
                        "id": "one",
                        "proxy_url_env": "CBRS_PROXY_ONE",
                        "egress_group": "chile_shared_1",
                    },
                    {
                        "id": "two",
                        "proxy_url_env": "CBRS_PROXY_TWO",
                        "egress_group": "chile_shared_1",
                    },
                    {
                        "id": "three",
                        "proxy_url_env": "CBRS_PROXY_THREE",
                        "egress_group": "chile_shared_1",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    shared_proxy = "http://user:pass@example.test:8080"
    for name in ("CBRS_PROXY_ONE", "CBRS_PROXY_TWO", "CBRS_PROXY_THREE"):
        monkeypatch.setenv(name, shared_proxy)

    config = load_account_pool_config(settings, path=config_path)
    status = dashboard_status(AccountPoolStore(tmp_path / "pool.sqlite3"), config=config)

    assert config.pool_daily_quota == 60
    assert status["pool"]["egress_routes"] == 1
    assert status["pool"]["shared_egress"] is True
    assert all(account["egress_shared"] for account in status["accounts"])
    assert {account["egress_group"] for account in status["accounts"]} == {
        "chile_shared_1"
    }


def test_live_pool_repetition_requires_explicit_opt_in(tmp_path: Path) -> None:
    from cbrs.account_pool import run_account_pool

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="not a production job queue"):
        run_account_pool(settings=settings, dry_run=False, max_cycles=1)


def test_pool_config_requires_boolean_live_repetition_opt_in(tmp_path: Path) -> None:
    from cbrs.account_pool import load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / ".cbrs" / "account-pool.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"allow_live_repetition": "true"}', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON boolean"):
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


def test_captcha_recovery_ignores_newer_dry_run(tmp_path: Path) -> None:
    from cbrs.account_pool import (
        AccountPoolStore,
        load_account_pool_config,
        resolve_account_captcha,
    )

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    config = load_account_pool_config(settings)
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    store.mark_account_captcha_pending("live", "ejecutivo_1")
    store.update_run("live", status="completed", finished=True)
    store.create_run(run_id="dry", dry_run=True, config=config, dashboard_url=None)

    def fake_runner(**_: object) -> ValidationRunResult:
        report_path = tmp_path / "validation.json"
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
        account_id="ejecutivo_1",
        validation_runner=fake_runner,
    )

    accounts = {row["account_id"]: row for row in store.accounts("live")}
    assert result["status"] == "resolved"
    assert accounts["ejecutivo_1"]["status"] == "available"


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

    def fake_runner(**kwargs: object) -> ValidationRunResult:
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / "fake.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        report_path = output_dir / "validation.json"
        report_path.write_text("{}", encoding="utf-8")
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

    first = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=False,
        max_cycles=1,
        validation_runner=fake_runner,
    )
    second = run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=False,
        max_cycles=1,
        validation_runner=fake_runner,
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


def test_pool_dry_run_does_not_consume_live_quota(tmp_path: Path) -> None:
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
    run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=True,
        max_cycles=3,
    )

    def fake_runner(**kwargs: object) -> ValidationRunResult:
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / "fake.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        report_path = output_dir / "validation.json"
        report_path.write_text("{}", encoding="utf-8")
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

    run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=False,
        max_cycles=1,
        validation_runner=fake_runner,
    )

    status = dashboard_status(store, config=config)
    usage = {account["account_id"]: account["used_today"] for account in status["accounts"]}
    assert status["pool"]["used_today"] == 1
    assert usage == {"ejecutivo_1": 1, "ejecutivo_2": 0, "ejecutivo_3": 0}


def test_pool_carries_captcha_pause_across_live_restart(tmp_path: Path) -> None:
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config = load_account_pool_config(settings)
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    store.create_run(run_id="first", dry_run=False, config=config, dashboard_url=None)
    store.mark_account_captcha_pending("first", "ejecutivo_1")
    store.update_run("first", status="completed", finished=True)

    store.create_run(run_id="second", dry_run=False, config=config, dashboard_url=None)

    accounts = {row["account_id"]: row for row in store.accounts("second")}
    assert accounts["ejecutivo_1"]["status"] == "captcha_pending"
    assert accounts["ejecutivo_1"]["paused_reason"] == "captcha_rejected"


def test_pool_rejects_second_live_runner_with_fresh_heartbeat(tmp_path: Path) -> None:
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config

    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config = load_account_pool_config(settings)
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    store.create_run(run_id="active", dry_run=False, config=config, dashboard_url=None)

    with pytest.raises(RuntimeError, match="already active"):
        store.create_run(run_id="duplicate", dry_run=False, config=config, dashboard_url=None)


def test_pool_reserves_failed_attempt_and_pauses_account(tmp_path: Path) -> None:
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

    def failing_runner(**kwargs: object) -> ValidationRunResult:
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "validation.json"
        report_path.write_text("{}", encoding="utf-8")
        return ValidationRunResult(
            exit_code=1,
            status="failed",
            report={},
            report_path=report_path,
            preflight_report_path=None,
            error="browser process exited unexpectedly",
        )

    run_account_pool(
        settings=settings,
        config=config,
        store=store,
        dry_run=False,
        max_cycles=1,
        validation_runner=failing_runner,
    )

    status = dashboard_status(store, config=config)
    accounts = {account["account_id"]: account for account in status["accounts"]}
    assert status["pool"]["used_today"] == 1
    assert accounts["ejecutivo_1"]["status"] == "paused"
    assert accounts["ejecutivo_1"]["used_today"] == 1


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


def test_pool_dashboard_api_and_html_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config, run_account_pool
    from cbrs.account_pool_dashboard import start_pool_dashboard

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    config = load_account_pool_config(settings)
    config = replace(
        config,
        accounts=(
            replace(config.accounts[0], username_env="CBRS_DASHBOARD_TEST_USERNAME"),
            *config.accounts[1:],
        ),
    )
    monkeypatch.setenv("CBRS_DASHBOARD_TEST_USERNAME", "operator.name@example.test")
    monkeypatch.setenv(
        "CBRS_NOVNC_URL",
        "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale",
    )
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
        resume_request = Request(f"{dashboard.url}/api/resume", method="POST")
        with urlopen(resume_request, timeout=5) as response:
            resume_payload = json.loads(response.read().decode("utf-8"))
        account_request = Request(
            f"{dashboard.url}/api/onboarding/accounts",
            data=json.dumps(
                {
                    "accounts": [
                        {
                            "id": "ejecutivo_1",
                            "username": "operator.name@example.test",
                            "password": "test-password-not-returned",
                            "proxy_url": "http://proxy-user:proxy-password@proxy.example.test:8080",
                            "daily_quota": 20,
                        }
                    ]
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(account_request, timeout=5) as response:
            account_payload = json.loads(response.read().decode("utf-8"))
    finally:
        dashboard.stop()

    serialized = json.dumps(payload)
    assert "Pool de Consultas CBRS" in html
    assert "Consultas disponibles hoy" in html
    assert "account.username_prefix || account.label" in html
    assert payload["accounts"][0]["label"] == "operator.name"
    assert "job-account-icon" in html
    assert "const initial" in html
    assert "<th>Motivo</th>" in html
    assert "límite diario informado por CBRS" in html
    assert "<th>N.º diario</th>" in html
    assert "documentOrdinals" in html
    assert "attempt-details" in html
    assert "attempt-account-groups" in html
    assert "attempt-account-group" in html
    assert "attempt-history" in html
    assert "attempt-disclosure-input" in html
    assert "renderedJobsSignature" in html
    assert "Último ${escapeHtml(latest.number)}" in html
    assert "No intentado" in html
    assert "shortJobId" in html
    assert "table-scroll" in html
    assert "captchaPhaseLabels" in html
    assert "captchaSolver.daily_limit" in html
    assert 'id="captchaAttempts"' in html
    assert "renderCaptchaAttempts" in html
    assert "captcha_attempts" in html
    assert 'class="jobs-table captcha-attempts-table"' in html
    assert ".jobs-table thead th { position: static;" in html
    assert "restore probado" in html
    assert "recovery-spinner" in html
    assert "renderStopButton" in html
    assert "Deteniendo…" in html
    assert "Reanudar worker" in html
    assert 'class="runtime-status-card"' in html
    assert "Sin worker activo · listo para iniciar." in html
    assert "Configurar cuentas" in html
    assert "Agregar a cola" in html
    assert "Buscar y descargar ahora" in html
    assert "Por empresa" in html
    assert "Por documento" in html
    assert 'id="openExamples"' in html
    assert 'id="productionSettingsModal"' in html
    assert 'id="configureProduction"' in html
    assert 'id="themeToggle"' in html
    assert 'cbrs-dashboard-theme' in html
    assert ':root[data-theme="dark"]' in html
    assert 'aria-label="Activar modo oscuro"' in html
    assert "grid-template-rows: auto minmax(0, 1fr) auto" in html
    assert "scrollbar-gutter: stable" in html
    assert "lucide@1.33.0" in html
    assert "Comportamiento humano" in html
    assert "Protecciones activas" in html
    assert "/api/examples" in html
    assert "data-preview-job" in html
    assert "pdfPreviewModal" in html
    assert "/artifacts" in html
    assert "/api/onboarding/accounts" in html
    assert "Las contraseñas existentes nunca se cargan aquí" in html
    assert "email" not in serialized.lower()
    assert "password" not in serialized.lower()
    assert payload["runtime"]["visual_url"].startswith("http://localhost:6080/")
    assert payload["runtime"]["visual_recovery_mode"] == "noVNC"
    assert "[REDACTED_IP]" not in payload["runtime"]["visual_url"]
    assert payload["pool"]["daily_quota"] == 60
    assert content.startswith(b"%PDF")
    assert stop_payload == {"ok": True, "status": "stop_requested"}
    assert resume_payload == {"ok": True, "status": "resume_requested"}
    assert account_payload == {"ok": True, "status": "account_configuration_requested"}
    assert (settings.profile_dir.parent / "control" / "resume.request").read_text() == "resume\n"
    request_payload = json.loads(
        (settings.profile_dir.parent / "control" / "account-configuration.json").read_text()
    )
    assert request_payload["accounts"][0]["id"] == "ejecutivo_1"
    assert "test-password-not-returned" not in json.dumps(account_payload)


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
    assert "captchaBreath" in html
    assert "--wave-index" in html
    assert ".jobs-table thead th" in html
    assert ".jobs-table thead th { position: static;" in html
    assert payload == {"ok": True, "status": "started", "account_id": "ejecutivo_1"}
    assert calls == ["ejecutivo_1"]


def test_pool_dashboard_visual_captcha_requires_operator_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config
    from cbrs.account_pool_dashboard import start_pool_dashboard

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    config = load_account_pool_config(settings)
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    store.create_run(run_id="pool-test", dry_run=False, config=config, dashboard_url=None)
    store.mark_account_captcha_pending("pool-test", "ejecutivo_1")
    validated = threading.Event()

    def fake_hold(
        _store, _settings, _config, *, account_id, confirmation, phase_changed=None
    ):
        assert account_id == "ejecutivo_1"
        if phase_changed:
            phase_changed("waiting_operator")
        return confirmation.wait(timeout=2)

    def fake_resolver(**kwargs):
        validated.set()
        return {"ok": True, "status": "resolved"}

    monkeypatch.setattr("cbrs.account_pool_dashboard._hold_visual_captcha_session", fake_hold)
    monkeypatch.setattr("cbrs.account_pool_dashboard.resolve_account_captcha", fake_resolver)
    dashboard = start_pool_dashboard(store, settings=settings, config=config, port=0)
    try:
        trigger = Request(
            f"{dashboard.url}/api/captcha/ejecutivo_1/trigger", data=b"", method="POST"
        )
        with urlopen(trigger, timeout=5) as response:
            trigger_payload = json.loads(response.read().decode())
        complete = Request(
            f"{dashboard.url}/api/captcha/ejecutivo_1/complete", data=b"", method="POST"
        )
        with urlopen(complete, timeout=5) as response:
            complete_payload = json.loads(response.read().decode())
        assert validated.wait(timeout=2)
    finally:
        dashboard.stop()

    assert trigger_payload["visual_confirmation_required"] is True
    assert complete_payload["status"] == "validation_requested"


def test_visual_captcha_refreshes_expired_session_before_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config
    from cbrs.account_pool_dashboard import _hold_visual_captcha_session

    settings = load_settings(
        {"CBRS_PROFILE_DIR": ".cbrs/chrome-profile", "CBRS_OUTPUT_DIR": "outputs"},
        root=tmp_path,
    )
    original = load_account_pool_config(settings)
    account = replace(
        original.accounts[0],
        username_env="CBRS_TEST_USERNAME",
        password_env="CBRS_TEST_PASSWORD",
    )
    config = replace(original, accounts=(account,))
    monkeypatch.setenv("CBRS_TEST_USERNAME", "operator@example.test")
    monkeypatch.setenv("CBRS_TEST_PASSWORD", "private")
    store = AccountPoolStore(tmp_path / ".cbrs" / "pool" / "pool.sqlite3")
    store.create_run(run_id="pool-test", dry_run=False, config=config, dashboard_url=None)
    store.mark_account_captcha_pending("pool-test", account.account_id)
    calls: list[tuple[str, str]] = []
    reloaded = threading.Event()

    class FakeBrowserSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def login_with_visible_form(self, username: str, password: str) -> str:
            calls.append((username, password))
            return "browser_form"

        def reload_current_page(self) -> None:
            reloaded.set()

    monkeypatch.setattr("cbrs.browser_session.BrowserSession", FakeBrowserSession)
    confirmation = threading.Event()
    confirmation.set()

    assert _hold_visual_captcha_session(
        store,
        settings,
        config,
        account_id=account.account_id,
        confirmation=confirmation,
    )
    assert calls == [("operator@example.test", "private")]
    assert reloaded.is_set()
