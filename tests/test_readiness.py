from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from cbrs.readiness import (
    REQUIRED_SOURCE_FILES,
    _decode_wsl_output,
    _windows_task_statuses,
    build_readiness_report,
    write_readiness_report,
)


def _write_source_assets(root: Path) -> None:
    for name in REQUIRED_SOURCE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test\n", encoding="utf-8")


def test_prerequisites_document_is_a_required_deployment_asset() -> None:
    assert "PREREQUISITES.txt" in REQUIRED_SOURCE_FILES


def _write_pool(path: Path, *, include_references: bool = True) -> None:
    account = {
        "id": "account_1",
        "profile_dir": "accounts/account_1/chrome-profile",
        "daily_quota": 20,
    }
    if include_references:
        account.update(
            {
                "username_env": "TEST_ACCOUNT_USERNAME",
                "password_env": "TEST_ACCOUNT_PASSWORD",
                "proxy_url_env": "TEST_ACCOUNT_PROXY_URL",
            }
        )
    path.write_text(
        json.dumps(
            {
                "daily_quota_per_account": 20,
                "accounts": [account],
            }
        ),
        encoding="utf-8",
    )


def test_readiness_report_is_offline_and_does_not_contain_secret_values(
    tmp_path: Path,
) -> None:
    _write_source_assets(tmp_path)
    pool_path = tmp_path / "account-pool.json"
    env_path = tmp_path / "cbrs.env"
    _write_pool(pool_path)
    env_path.write_text(
        "\n".join(
            (
                "CBRS_BROWSER_BACKEND=chrome",
                "CBRS_HEADLESS=0",
                "CBRS_EGRESS_MODE=dedicated_static_isp",
                "CBRS_EXPECTED_EGRESS_COUNTRY=CL",
                "TEST_ACCOUNT_USERNAME=private-user-value",
                "TEST_ACCOUNT_PASSWORD=private-password-value",
                "TEST_ACCOUNT_PROXY_URL=http://private-proxy-value:1234",
            )
        ),
        encoding="utf-8",
    )

    report = build_readiness_report(
        repo_root=tmp_path,
        env_file=env_path,
        pool_config_path=pool_path,
        target="current",
        distro="Ubuntu-24.04",
        system_name="Windows",
        minimum_free_gib=0,
    )

    serialized = json.dumps(report)
    assert report["safety"] == {
        "network_requests_made": False,
        "browser_started": False,
        "setup_changes_made": False,
        "secret_values_in_report": False,
    }
    assert "private-user-value" not in serialized
    assert "private-password-value" not in serialized
    assert "private-proxy-value" not in serialized
    secret_check = next(
        check for check in report["checks"] if check["check_id"] == "account_secret_values"
    )
    assert secret_check["status"] == "pass"


def test_readiness_marks_missing_account_references_as_failure(tmp_path: Path) -> None:
    _write_source_assets(tmp_path)
    pool_path = tmp_path / "account-pool.json"
    _write_pool(pool_path, include_references=False)

    report = build_readiness_report(
        repo_root=tmp_path,
        env_file=None,
        pool_config_path=pool_path,
        target="current",
        distro="Ubuntu-24.04",
        system_name="Windows",
        minimum_free_gib=0,
    )

    account_check = next(
        check
        for check in report["checks"]
        if check["check_id"] == "account_pool_configuration"
    )
    assert account_check["status"] == "fail"
    assert report["summary"]["live_test_ready"] is False


def test_readiness_reports_explicit_shared_egress_route(tmp_path: Path) -> None:
    _write_source_assets(tmp_path)
    pool_path = tmp_path / "account-pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "daily_quota_per_account": 20,
                "accounts": [
                    {
                        "id": f"account_{index}",
                        "profile_dir": f"accounts/account_{index}/chrome-profile",
                        "username_env": f"ACCOUNT_{index}_USERNAME",
                        "password_env": f"ACCOUNT_{index}_PASSWORD",
                        "proxy_url_env": f"ACCOUNT_{index}_PROXY",
                        "egress_group": "shared_chile",
                    }
                    for index in range(1, 4)
                ],
            }
        ),
        encoding="utf-8",
    )
    env_path = tmp_path / "cbrs.env"
    values = [
        "CBRS_BROWSER_BACKEND=chrome",
        "CBRS_HEADLESS=0",
        "CBRS_EGRESS_MODE=dedicated_static_isp",
        "CBRS_EXPECTED_EGRESS_COUNTRY=CL",
    ]
    for index in range(1, 4):
        values.extend(
            (
                f"ACCOUNT_{index}_USERNAME=user-{index}",
                f"ACCOUNT_{index}_PASSWORD=password-{index}",
                f"ACCOUNT_{index}_PROXY=http://shared.example.test:8080",
            )
        )
    env_path.write_text("\n".join(values), encoding="utf-8")

    report = build_readiness_report(
        repo_root=tmp_path,
        env_file=env_path,
        pool_config_path=pool_path,
        target="current",
        distro="Ubuntu-24.04",
        system_name="Windows",
        minimum_free_gib=0,
    )

    account_check = next(
        check
        for check in report["checks"]
        if check["check_id"] == "account_pool_configuration"
    )
    assert account_check["status"] == "pass"
    assert "capacity 60/day" in account_check["detail"]
    assert "1 explicit egress route" in account_check["detail"]


def test_readiness_marks_missing_wsl_distribution_as_deferred(tmp_path: Path) -> None:
    _write_source_assets(tmp_path)
    pool_path = tmp_path / "account-pool.json"
    _write_pool(pool_path)

    report = build_readiness_report(
        repo_root=tmp_path,
        env_file=None,
        pool_config_path=pool_path,
        target="wsl",
        distro="Ubuntu-24.04",
        system_name="Windows",
        wsl_distros=(),
        minimum_free_gib=0,
    )

    distro_check = next(
        check
        for check in report["checks"]
        if check["check_id"] == "wsl_ubuntu_distribution"
    )
    assert distro_check["status"] == "deferred"
    assert "Install-CbrsWsl.ps1" in str(distro_check["next_action"])


def test_wsl_utf16_output_is_decoded() -> None:
    value = "Ubuntu-24.04\r\n".encode("utf-16-le")

    assert _decode_wsl_output(value).strip() == "Ubuntu-24.04"


def test_readiness_report_write_is_sanitized_and_atomic(tmp_path: Path) -> None:
    output = tmp_path / "reports" / "readiness.json"
    report = {"schema": "test", "summary": {"live_test_ready": False}}

    result = write_readiness_report(report, output)

    assert result == output.resolve()
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert not output.with_suffix(".json.tmp").exists()


def test_windows_task_statuses_are_sanitized_and_machine_readable() -> None:
    rows = [
        {"name": "CBRS Worker", "state": "Running", "enabled": True, "last_result": 0},
        {"name": "CBRS Dashboard", "state": "Running", "enabled": True, "last_result": 0},
        {"name": "CBRS Daily Backup", "state": "Ready", "enabled": True, "last_result": 0},
    ]

    def runner(command, **kwargs):
        assert command[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        assert kwargs["timeout"] == 15
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    statuses = _windows_task_statuses(tuple(row["name"] for row in rows), command_runner=runner)

    assert statuses["CBRS Worker"] == {
        "state": "Running",
        "enabled": True,
        "last_result": 0,
    }
    assert "user" not in json.dumps(statuses).lower()
