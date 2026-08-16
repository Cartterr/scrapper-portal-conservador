from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from cbrs.readiness import REQUIRED_SOURCE_FILES


ROOT = Path(__file__).resolve().parents[1]


def _load_configure_module():
    path = ROOT / "deploy" / "configure_runtime.py"
    spec = importlib.util.spec_from_file_location("cbrs_configure_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(*, proxy_url: str = "http://user:password@proxy.example.test:8080"):
    return {
        "accounts": [
            {
                "id": "ejecutivo_1",
                "label": "Ejecutivo 1",
                "username": "operator@example.test",
                "password": "pa'ss$word`tick\\slash\"quote",
                "proxy_url": proxy_url,
                "daily_quota": 20,
            }
        ],
        "backup": {
            "repository": "/srv/secondary/restic",
            "password": "backup$secret`value",
            "password_file": "/etc/cbrs/restic-password",
        },
    }


def test_installer_assets_are_required_by_readiness() -> None:
    expected = {
        "INSTALL-CBRS.bat",
        "PREREQUISITES.txt",
        "deploy/configure_runtime.py",
        "deploy/run_with_env.py",
        "deploy/cbrs-configuration-apply.path",
        "deploy/cbrs-configuration-apply.service",
        "deploy/windows/Install-CbrsE2E.ps1",
    }
    assert expected.issubset(REQUIRED_SOURCE_FILES)
    assert all((ROOT / path).is_file() for path in expected)


def test_protected_configuration_preserves_special_characters(tmp_path: Path) -> None:
    module = _load_configure_module()
    validated = module.validate_payload(_payload())
    env_path = tmp_path / "cbrs.env"
    environment = module.build_environment(
        template_path=ROOT / "deploy" / "cbrs.env.example",
        existing_path=env_path,
        validated=validated,
    )
    env_path.write_text(module._dotenv_text(environment), encoding="utf-8")
    parsed = dotenv_values(env_path)

    assert parsed["CBRS_ACCOUNT_1_PASSWORD"] == _payload()["accounts"][0]["password"]
    assert parsed["RESTIC_REPOSITORY"] == "/srv/secondary/restic"
    assert parsed["CBRS_HEADLESS"] == "0"


def test_protected_configuration_rejects_unsupported_proxy() -> None:
    module = _load_configure_module()

    with pytest.raises(ValueError, match="http:// or https://"):
        module.validate_payload(_payload(proxy_url="socks5://proxy.example.test:1080"))


def test_protected_configuration_rejects_control_characters() -> None:
    module = _load_configure_module()
    payload = _payload()
    payload["accounts"][0]["password"] = "line1\nline2"

    with pytest.raises(ValueError, match="printable line"):
        module.validate_payload(payload)


def test_account_update_preserves_blank_existing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_configure_module()
    env_path = tmp_path / "cbrs.env"
    env_path.write_text(
        "CBRS_ACCOUNT_1_USERNAME=operator@example.test\n"
        "CBRS_ACCOUNT_1_PASSWORD=existing-password\n"
        "CBRS_ACCOUNT_1_PROXY_URL=http://user:pass@proxy.example.test:8080\n"
        "RESTIC_REPOSITORY=/srv/restic\n"
        "RESTIC_PASSWORD_FILE=/etc/cbrs/restic-password\n",
        encoding="utf-8",
    )
    pool_path = tmp_path / "account-pool.json"
    pool_path.write_text(
        '{"accounts":[{"id":"ejecutivo_1","username_env":"CBRS_ACCOUNT_1_USERNAME","password_env":"CBRS_ACCOUNT_1_PASSWORD","proxy_url_env":"CBRS_ACCOUNT_1_PROXY_URL","daily_quota":20}]}',
        encoding="utf-8",
    )
    password_path = Path("/etc/cbrs/restic-password")
    # Exercise only the account merge, avoiding a dependency on host root paths.
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs):
        if path == password_path:
            return "backup-password\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    merged = module.build_account_update_payload(
        {"accounts": [{"id": "ejecutivo_1", "username": "", "password": "", "proxy_url": "", "daily_quota": 25}]},
        existing_env_path=env_path,
        state_dir=tmp_path,
    )

    account = merged["accounts"][0]
    assert account["username"] == "operator@example.test"
    assert account["password"] == "existing-password"
    assert account["proxy_url"] == "http://user:pass@proxy.example.test:8080"
    assert account["daily_quota"] == 25


def test_run_with_env_does_not_evaluate_shell_syntax(tmp_path: Path) -> None:
    env_path = tmp_path / "runtime.env"
    expected = "literal$HOME`command\\tail"
    env_path.write_text(f"CBRS_TEST_VALUE={expected!r}\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy" / "run_with_env.py"),
            str(env_path),
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['CBRS_TEST_VALUE'])",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": "must-not-expand"},
    )

    assert result.stdout.strip() == expected


def test_windows_installer_has_required_safety_gates() -> None:
    batch = (ROOT / "INSTALL-CBRS.bat").read_text(encoding="utf-8")
    powershell = (ROOT / "deploy" / "windows" / "Install-CbrsE2E.ps1").read_text(
        encoding="utf-8"
    )

    assert "fltmc.exe" in batch
    assert "-Verb RunAs" in batch
    assert "-ExecutionPolicy Bypass" in batch
    assert "--plan" in batch
    assert "Read-Host $Prompt -AsSecureString" in powershell
    assert "configure_runtime.py" in powershell
    assert "run_with_env.py" in powershell
    assert "--approve-egress-baseline" in powershell
    assert "¿Autoriza habilitar e iniciar ahora el worker CBRS?" in powershell
    assert "Start-Transcript" not in powershell
    assert "source /etc/cbrs/cbrs.env" not in powershell


def test_windows_wsl_helpers_strip_cr_before_bash() -> None:
    for relative_path in (
        "deploy/windows/Get-CbrsIndefiniteStatus.ps1",
        "deploy/windows/Start-CbrsIndefiniteTest.ps1",
        "deploy/windows/Stop-CbrsIndefiniteTest.ps1",
        "deploy/windows/Install-CbrsE2E.ps1",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert '-replace "`r", \'\'' in source
