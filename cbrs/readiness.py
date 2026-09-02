from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values

from .account_pool import load_account_pool_config
from .config import MIN_SAFE_DELAY_SECONDS, Settings, load_settings
from .safety import redact_text

READINESS_SCHEMA = "cbrs-indefinite-readiness-v1"
REQUIRED_PACKAGES = {
    "playwright": "1.60.0",
    "Pillow": "12.1.1",
    "pytest": "9.0.3",
    "python-dotenv": "1.0.1",
}
REQUIRED_SOURCE_FILES = (
    "PREREQUISITES.txt",
    "INSTALL-CBRS.bat",
    "requirements.txt",
    "deploy/configure_runtime.py",
    "deploy/run_with_env.py",
    "deploy/windows/Install-CbrsE2E.ps1",
    "deploy/install-ubuntu.sh",
    "deploy/install-wsl.sh",
    "deploy/cbrs.env.example",
    "deploy/account-pool.json.example",
    "deploy/cbrs-worker.service",
    "deploy/cbrs-dashboard.service",
    "deploy/cbrs-configuration-apply.path",
    "deploy/cbrs-configuration-apply.service",
    "deploy/cbrs-backup.timer",
)


@dataclass(frozen=True)
class ReadinessCheck:
    check_id: str
    stage: str
    status: str
    blocking: bool
    detail: str
    next_action: str | None = None


def load_readiness_environment(path: Path | None) -> dict[str, str]:
    """Load an env file for validation without exporting or displaying values."""
    values = dict(os.environ)
    if path and path.exists():
        values.update(
            {
                key: str(value)
                for key, value in dotenv_values(path).items()
                if value is not None
            }
        )
    return values


def settings_for_readiness(*, env_file: Path | None, root: Path) -> Settings:
    return load_settings(load_readiness_environment(env_file), root=root)


def build_readiness_report(
    *,
    repo_root: Path,
    env_file: Path | None,
    pool_config_path: Path,
    target: str,
    distro: str,
    probe_wsl_runtime: bool = False,
    minimum_free_gib: float = 20.0,
    require_active_runtime: bool = False,
    system_name: str | None = None,
    wsl_distros: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Inspect readiness without browser startup, network traffic, or setup changes."""
    repo_root = repo_root.resolve()
    env_file = env_file.resolve() if env_file else None
    pool_config_path = pool_config_path.resolve()
    system_name = system_name or platform.system()
    if target == "windows":
        return _build_windows_readiness_report(
            repo_root=repo_root,
            env_file=env_file,
            pool_config_path=pool_config_path,
            system_name=system_name,
            require_active_runtime=require_active_runtime,
        )
    checks: list[ReadinessCheck] = []

    def add(
        check_id: str,
        stage: str,
        status: str,
        detail: str,
        *,
        blocking: bool = True,
        next_action: str | None = None,
    ) -> None:
        checks.append(
            ReadinessCheck(
                check_id=check_id,
                stage=stage,
                status=status,
                blocking=blocking,
                detail=detail,
                next_action=next_action,
            )
        )

    missing_source = [name for name in REQUIRED_SOURCE_FILES if not (repo_root / name).is_file()]
    add(
        "source_bundle",
        "offline",
        "pass" if not missing_source else "fail",
        "all deployment assets are present"
        if not missing_source
        else f"missing deployment assets: {', '.join(missing_source)}",
        next_action="Restore the missing tracked deployment files." if missing_source else None,
    )

    python_ok = sys.version_info >= (3, 14)
    add(
        "python_runtime",
        "offline",
        "pass" if python_ok else "fail",
        f"Python {platform.python_version()}",
        next_action="Install Python 3.14 in the Ubuntu runtime." if not python_ok else None,
    )
    package_failures: list[str] = []
    package_versions: list[str] = []
    for package, expected in REQUIRED_PACKAGES.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_failures.append(f"{package} missing")
            continue
        package_versions.append(f"{package}={actual}")
        if actual != expected:
            package_failures.append(f"{package} expected {expected}, found {actual}")
    add(
        "python_dependencies",
        "offline",
        "pass" if not package_failures else "fail",
        ", ".join(package_versions) if not package_failures else "; ".join(package_failures),
        next_action="Install the pinned requirements in the selected virtualenv."
        if package_failures
        else None,
    )

    try:
        settings = settings_for_readiness(env_file=env_file, root=repo_root)
    except Exception as exc:
        add(
            "runtime_configuration",
            "configuration",
            "fail",
            "configuration could not be parsed: " + redact_text(str(exc))[:500],
            next_action="Correct the non-secret runtime settings before setup.",
        )
        settings = load_settings({}, root=repo_root)
    else:
        setting_problems = []
        if settings.browser_backend != "chrome":
            setting_problems.append("browser backend must be chrome")
        if settings.headless:
            setting_problems.append("headed mode is required for the live soak")
        if settings.expected_egress_country != "CL":
            setting_problems.append("expected egress country must be CL")
        if settings.egress_mode not in {"dedicated_static_isp", "residential_sticky"}:
            setting_problems.append(
                "egress mode must be dedicated_static_isp or residential_sticky"
            )
        if settings.request_delay_seconds < MIN_SAFE_DELAY_SECONDS:
            setting_problems.append("request delay is below the safety minimum")
        add(
            "runtime_configuration",
            "configuration",
            "pass" if not setting_problems else "deferred",
            "safe headed Chrome settings are selected"
            if not setting_problems
            else "; ".join(setting_problems),
            next_action="Complete the Ubuntu environment file from deploy/cbrs.env.example."
            if setting_problems
            else None,
        )

    environment = load_readiness_environment(env_file)
    if not pool_config_path.exists():
        add(
            "account_pool_configuration",
            "configuration",
            "deferred",
            "account pool configuration is not present",
            next_action="Create it from deploy/account-pool.json.example without embedding secrets.",
        )
    else:
        try:
            pool = load_account_pool_config(settings, path=pool_config_path)
        except Exception as exc:
            add(
                "account_pool_configuration",
                "configuration",
                "fail",
                "account pool configuration is invalid: "
                + redact_text(str(exc))[:500],
                next_action="Correct the account-pool JSON before setup.",
            )
        else:
            enabled = [account for account in pool.accounts if account.enabled]
            missing_references: list[str] = []
            missing_values: list[str] = []
            proxy_references: list[str] = []
            profile_paths: list[str] = []
            accounts_by_proxy_value: dict[str, list[Any]] = {}
            for account in enabled:
                references = (
                    ("username_env", account.username_env),
                    ("password_env", account.password_env),
                    ("proxy_url_env", account.proxy_url_env),
                )
                for label, reference in references:
                    if not reference:
                        missing_references.append(f"{account.account_id}.{label}")
                    elif not environment.get(reference):
                        missing_values.append(reference)
                if account.proxy_url_env:
                    proxy_references.append(account.proxy_url_env)
                    proxy_value = environment.get(account.proxy_url_env)
                    if proxy_value:
                        accounts_by_proxy_value.setdefault(proxy_value, []).append(account)
                if account.profile_dir:
                    profile_paths.append(str(account.profile_dir))
            structural_problems = []
            if missing_references:
                structural_problems.append(
                    f"missing env references: {', '.join(sorted(missing_references))}"
                )
            if len(proxy_references) != len(set(proxy_references)):
                structural_problems.append("proxy env references are not unique")
            if len(profile_paths) != len(set(profile_paths)):
                structural_problems.append("profile paths are not unique")
            for shared_accounts in accounts_by_proxy_value.values():
                if len(shared_accounts) < 2:
                    continue
                groups = {account.egress_group for account in shared_accounts}
                if None in groups or len(groups) != 1:
                    structural_problems.append(
                        "shared proxy values require one explicit egress_group"
                    )
            resolved_routes = len(accounts_by_proxy_value)
            route_detail = (
                f", {resolved_routes} explicit egress route(s)"
                if resolved_routes
                else ""
            )
            add(
                "account_pool_configuration",
                "configuration",
                "pass" if not structural_problems else "fail",
                f"{len(enabled)} enabled account(s), capacity {pool.pool_daily_quota}/day"
                + route_detail
                if not structural_problems
                else "; ".join(structural_problems),
                next_action="Use one profile and one secret/proxy env reference per account."
                if structural_problems
                else None,
            )
            unique_missing_values = sorted(set(missing_values))
            add(
                "account_secret_values",
                "secrets",
                "pass" if not unique_missing_values else "deferred",
                "all referenced values are present"
                if not unique_missing_values
                else "missing values for: " + ", ".join(unique_missing_values),
                next_action="Populate the protected service environment file once credentials and proxies are approved."
                if unique_missing_values
                else None,
            )
            missing_baselines = []
            invalid_baselines = []
            for account in enabled:
                profile_dir = account.profile_dir or (
                    settings.profile_dir.parent
                    / "accounts"
                    / account.account_id
                    / "chrome-profile"
                )
                baseline = profile_dir.parent / "fixed-egress-baseline.json"
                if not baseline.is_file():
                    missing_baselines.append(account.account_id)
                    continue
                try:
                    payload = json.loads(baseline.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    invalid_baselines.append(account.account_id)
                    continue
                if (
                    payload.get("schema") != "cbrs-fixed-egress-baseline-v1"
                    or not payload.get("egress_hash")
                ):
                    invalid_baselines.append(account.account_id)
            baseline_problems = []
            if missing_baselines:
                baseline_problems.append(
                    "approval missing for " + ", ".join(sorted(missing_baselines))
                )
            if invalid_baselines:
                baseline_problems.append(
                    "invalid baseline for " + ", ".join(sorted(invalid_baselines))
                )
            add(
                "account_egress_baselines",
                "live_gate",
                "pass" if not baseline_problems else "deferred",
                "a sanitized egress baseline exists for every enabled account"
                if not baseline_problems
                else "; ".join(baseline_problems),
                next_action="During the authorized setup stage, run pool proxy-health with explicit baseline approval."
                if baseline_problems
                else None,
            )

    backup_missing = [
        name
        for name in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE")
        if not environment.get(name)
    ]
    add(
        "backup_configuration",
        "configuration",
        "pass" if not backup_missing else "deferred",
        "restic repository and password-file references are configured"
        if not backup_missing
        else "missing references for: " + ", ".join(backup_missing),
        next_action="Configure the encrypted restic repository before the indefinite test."
        if backup_missing
        else None,
    )

    if target == "ubuntu" and system_name.lower() == "linux" and not backup_missing:
        password_file = Path(environment["RESTIC_PASSWORD_FILE"]).expanduser()
        try:
            password_stat = password_file.stat()
            password_ok = (
                password_file.is_file()
                and password_stat.st_size > 0
                and password_stat.st_mode & 0o007 == 0
            )
        except OSError:
            password_ok = False
        add(
            "backup_password_file",
            "setup",
            "pass" if password_ok else "fail",
            "restic password file exists, is non-empty, and is not world-accessible"
            if password_ok
            else "restic password file is missing, empty, or world-accessible",
            next_action="Create /etc/cbrs/restic-password with mode 0640 root:cbrs."
            if not password_ok
            else None,
        )

        repository = environment["RESTIC_REPOSITORY"]
        if repository.startswith("/"):
            repository_path = Path(repository)
            repository_initialized = (repository_path / "config").is_file()
            add(
                "backup_repository_initialized",
                "setup",
                "pass" if repository_initialized else "deferred",
                "local restic repository is initialized"
                if repository_initialized
                else "local restic repository is not initialized",
                next_action="Mount the secondary volume and run restic init."
                if not repository_initialized
                else None,
            )
            if repository_initialized and settings.output_dir.exists():
                separate_device = (
                    repository_path.stat().st_dev != settings.output_dir.stat().st_dev
                )
                add(
                    "backup_secondary_storage",
                    "setup",
                    "pass" if separate_device else "fail",
                    "restic repository and primary outputs are on different devices"
                    if separate_device
                    else "restic repository is on the same device as primary outputs",
                    next_action="Mount the approved second volume or use a remote restic backend."
                    if not separate_device
                    else None,
                )
        else:
            add(
                "backup_repository_initialized",
                "setup",
                "pass",
                "a remote restic repository reference is configured",
            )

        backup_status_path = settings.profile_dir.parent / "backup" / "status.json"
        backup_ok = False
        if backup_status_path.is_file():
            try:
                backup_payload = json.loads(backup_status_path.read_text(encoding="utf-8"))
                finished_at = datetime.fromisoformat(str(backup_payload["finished_at"]))
                age_hours = (
                    datetime.now(timezone.utc) - finished_at
                ).total_seconds() / 3600
                backup_ok = bool(backup_payload.get("ok")) and 0 <= age_hours <= 36
            except (KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
                backup_ok = False
        add(
            "backup_smoke",
            "live_gate",
            "pass" if backup_ok else "deferred",
            "a successful backup completed within the last 36 hours"
            if backup_ok
            else "no successful backup from the last 36 hours was found",
            next_action="Run python -m cbrs jobs backup and verify its sanitized status."
            if not backup_ok
            else None,
        )

    if target == "ubuntu":
        expected_paths = {
            "profile parent": Path("/var/lib/cbrs"),
            "output": Path("/var/lib/cbrs/outputs"),
            "log": Path("/var/log/cbrs"),
        }
        actual_paths = {
            "profile parent": settings.profile_dir.parent,
            "output": settings.output_dir,
            "log": settings.log_dir,
        }
        mismatches = [
            f"{name}={actual_paths[name]}"
            for name, expected in expected_paths.items()
            if actual_paths[name] != expected
        ]
        add(
            "production_paths",
            "configuration",
            "pass" if not mismatches else "fail",
            "production state, outputs, and logs use the fixed Ubuntu paths"
            if not mismatches
            else "unexpected paths: " + ", ".join(mismatches),
            next_action="Use deploy/cbrs.env.example for the Ubuntu service environment."
            if mismatches
            else None,
        )

    _add_host_checks(
        add=add,
        target=target,
        system_name=system_name,
        distro=distro,
        probe_wsl_runtime=probe_wsl_runtime,
        wsl_distros=wsl_distros,
    )

    try:
        free_gib = shutil.disk_usage(repo_root).free / (1024**3)
    except OSError:
        add(
            "workspace_storage",
            "host",
            "warning",
            "workspace free space could not be measured",
            blocking=False,
        )
    else:
        enough = free_gib >= minimum_free_gib
        add(
            "workspace_storage",
            "host",
            "pass" if enough else "warning",
            f"{free_gib:.1f} GiB free on the workspace volume",
            blocking=False,
            next_action=f"Keep at least {minimum_free_gib:.0f} GiB free; permanent PDFs are never pruned automatically."
            if not enough
            else None,
        )

    blocking = [check for check in checks if check.blocking and check.status != "pass"]
    failures = [check for check in checks if check.status == "fail"]
    deferred = [check for check in checks if check.status == "deferred"]
    return {
        "schema": READINESS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "distro": distro if target == "wsl" else None,
        "safety": {
            "network_requests_made": False,
            "browser_started": False,
            "setup_changes_made": False,
            "secret_values_in_report": False,
        },
        "summary": {
            "offline_assets_ready": not any(
                check.status == "fail" and check.stage == "offline" for check in checks
            ),
            "live_test_ready": not blocking,
            "blocking_checks": len(blocking),
            "failures": len(failures),
            "deferred": len(deferred),
            "warnings": sum(check.status == "warning" for check in checks),
        },
        "checks": [asdict(check) for check in checks],
    }


def _add_host_checks(
    *,
    add: Any,
    target: str,
    system_name: str,
    distro: str,
    probe_wsl_runtime: bool,
    wsl_distros: Sequence[str] | None,
) -> None:
    if target == "current":
        add("target_environment", "host", "pass", f"current host is {system_name}")
        return

    if target == "ubuntu":
        is_linux = system_name.lower() == "linux"
        ubuntu = is_linux and _is_ubuntu()
        add(
            "ubuntu_host",
            "host",
            "pass" if ubuntu else "fail",
            "Ubuntu runtime detected" if ubuntu else f"current host is {system_name}, not Ubuntu",
            next_action="Run this gate inside the target Ubuntu runtime." if not ubuntu else None,
        )
        if ubuntu:
            _add_linux_command_checks(add)
            required_units = (
                "cbrs-display.service",
                "cbrs-x11vnc.service",
                "cbrs-novnc.service",
                "cbrs-worker.service",
                "cbrs-dashboard.service",
                "cbrs-backup.timer",
            )
            missing_units = [
                unit
                for unit in required_units
                if not (Path("/etc/systemd/system") / unit).is_file()
            ]
            add(
                "systemd_units",
                "setup",
                "pass" if not missing_units else "deferred",
                "all worker, dashboard, display, recovery, and backup units are installed"
                if not missing_units
                else "missing units: " + ", ".join(missing_units),
                next_action="Run deploy/install-ubuntu.sh before the live gate."
                if missing_units
                else None,
            )
        return

    if target != "wsl":
        add("target_environment", "host", "fail", f"unsupported target: {target}")
        return

    if system_name.lower() != "windows":
        add(
            "wsl2_host",
            "host",
            "fail",
            f"WSL2 target requires Windows; current host is {system_name}",
        )
        return
    wsl_executable = shutil.which("wsl.exe") or shutil.which("wsl")
    add(
        "wsl2_feature",
        "host",
        "pass" if wsl_executable else "fail",
        "WSL command is available" if wsl_executable else "WSL command is missing",
        next_action="Enable WSL2 from an elevated PowerShell session."
        if not wsl_executable
        else None,
    )
    if not wsl_executable:
        return
    detected = list(wsl_distros) if wsl_distros is not None else list_wsl_distros()
    matching = next((name for name in detected if name.casefold() == distro.casefold()), None)
    if matching is None:
        ubuntu_like = next((name for name in detected if "ubuntu" in name.casefold()), None)
        detail = (
            f"requested distro {distro} is absent; Ubuntu distro found: {ubuntu_like}"
            if ubuntu_like
            else f"no {distro} distribution is installed"
        )
        add(
            "wsl_ubuntu_distribution",
            "setup",
            "deferred",
            detail,
            next_action=f"Later, run deploy/windows/Install-CbrsWsl.ps1 -Apply -DistroName {distro}.",
        )
        return
    add(
        "wsl_ubuntu_distribution",
        "setup",
        "pass",
        f"{matching} is installed",
    )
    if not probe_wsl_runtime:
        add(
            "wsl_linux_runtime",
            "setup",
            "deferred",
            "Linux package probe was not requested",
            next_action="Re-run readiness with --probe-wsl-runtime after the deferred setup stage.",
        )
        return
    probe = probe_wsl_commands(matching)
    add(
        "wsl_linux_runtime",
        "setup",
        "pass" if probe["ok"] else "deferred",
        probe["detail"],
        next_action="Run deploy/install-wsl.sh inside Ubuntu, then repeat the probe."
        if not probe["ok"]
        else None,
    )


def list_wsl_distros() -> tuple[str, ...]:
    executable = shutil.which("wsl.exe") or shutil.which("wsl")
    if not executable:
        return ()
    completed = subprocess.run(
        [executable, "--list", "--quiet"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    text = _decode_wsl_output(completed.stdout)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def probe_wsl_commands(distro: str) -> dict[str, Any]:
    executable = shutil.which("wsl.exe") or shutil.which("wsl")
    if not executable:
        return {"ok": False, "detail": "WSL command is missing"}
    commands = "python3.14 google-chrome-stable Xvfb restic"
    script = (
        "set -eu; "
        ". /etc/os-release; printf 'os=%s\\n' \"$ID\"; "
        f"for name in {commands}; do command -v \"$name\" >/dev/null || printf 'missing=%s\\n' \"$name\"; done"
    )
    completed = subprocess.run(
        [executable, "--distribution", distro, "--exec", "bash", "-lc", script],
        capture_output=True,
        check=False,
        timeout=20,
    )
    output = _decode_wsl_output(completed.stdout + completed.stderr)
    missing = [line.removeprefix("missing=") for line in output.splitlines() if line.startswith("missing=")]
    ubuntu = "os=ubuntu" in output.lower()
    ok = completed.returncode == 0 and ubuntu and not missing
    if ok:
        detail = "Ubuntu, Python 3.14, Linux Chrome, Xvfb, and restic are present"
    elif missing:
        detail = "missing Linux runtime commands: " + ", ".join(missing)
    elif not ubuntu:
        detail = "selected WSL distribution is not Ubuntu"
    else:
        detail = "WSL runtime probe failed"
    return {"ok": ok, "detail": detail}


def _add_linux_command_checks(add: Any) -> None:
    missing = [
        name
        for name in ("python3.14", "google-chrome-stable", "Xvfb", "restic", "systemctl")
        if shutil.which(name) is None
    ]
    add(
        "linux_runtime_commands",
        "setup",
        "pass" if not missing else "deferred",
        "all Ubuntu runtime commands are present"
        if not missing
        else "missing commands: " + ", ".join(missing),
        next_action="Run deploy/install-ubuntu.sh, then repeat readiness."
        if missing
        else None,
    )


def _is_ubuntu() -> bool:
    path = Path("/etc/os-release")
    if not path.exists():
        return False
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values.get("ID", "").lower() == "ubuntu"


def _decode_wsl_output(value: bytes) -> str:
    if not value:
        return ""
    if b"\x00" in value:
        return value.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return value.decode(errors="replace")


def write_readiness_report(report: Mapping[str, Any], path: Path) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def format_readiness_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"CBRS indefinite-test readiness ({report['target']})",
        "No browser, paid CAPTCHA task, or setup change was started.",
        "",
    ]
    labels = {"pass": "PASS", "fail": "FAIL", "deferred": "WAIT", "warning": "WARN"}
    for check in report["checks"]:
        lines.append(
            f"{labels.get(check['status'], check['status'].upper()):4} "
            f"{check['check_id']}: {check['detail']}"
        )
        if check.get("next_action"):
            lines.append(f"     Next: {check['next_action']}")
    summary = report["summary"]
    lines.extend(
        [
            "",
            "LIVE READY" if summary["live_test_ready"] else "NOT LIVE READY",
            f"blocking={summary['blocking_checks']} failures={summary['failures']} "
            f"deferred={summary['deferred']} warnings={summary['warnings']}",
        ]
    )
    return "\n".join(lines)


def _build_windows_readiness_report(
    *,
    repo_root: Path,
    env_file: Path | None,
    pool_config_path: Path,
    system_name: str,
    require_active_runtime: bool = False,
) -> dict[str, Any]:
    """Validate the native Windows runtime without creating portal or CAPTCHA tasks."""
    from urllib.parse import urlparse

    from .browser_runtime import get_browser_status
    from .capsolver import CapSolverClient, CapSolverError
    from .captcha_solver import TwoCaptchaClient, TwoCaptchaError

    checks: list[ReadinessCheck] = []

    def add(check_id: str, status: str, detail: str, next_action: str | None = None) -> None:
        checks.append(ReadinessCheck(check_id, "windows", status, True, detail, next_action))

    environment = load_readiness_environment(env_file)
    settings_environment = dict(environment)
    configured_solver_mode = environment.get("CBRS_CAPTCHA_SOLVER_MODE", "browser")
    configured_solver_key = environment.get("CBRS_2CAPTCHA_API_KEY", "").strip()
    configured_capsolver_key = environment.get("CBRS_CAPSOLVER_API_KEY", "").strip()
    if configured_solver_mode in {
        "2captcha",
        "2captcha_manual",
        "2captcha_fallback",
    } and not configured_solver_key:
        settings_environment["CBRS_CAPTCHA_SOLVER_MODE"] = "browser"
    if configured_solver_mode in {
        "capsolver",
        "capsolver_manual",
        "capsolver_fallback",
    } and not configured_capsolver_key:
        settings_environment["CBRS_CAPTCHA_SOLVER_MODE"] = "browser"
    settings = load_settings(settings_environment, root=repo_root)
    native_assets = (
        "deploy/windows/Install-CbrsNative.ps1",
        "deploy/windows/Start-CbrsNative.ps1",
        "deploy/windows/Stop-CbrsNative.ps1",
        "deploy/windows/Get-CbrsNativeStatus.ps1",
        "deploy/windows/Invoke-CbrsNativeTask.ps1",
        "deploy/windows/Invoke-CbrsRuntimeWatchdog.ps1",
        "deploy/windows/Open-CbrsNativeRecovery.ps1",
        "deploy/cbrs-native.env.example",
        "deploy/account-pool.native.json.example",
        "deploy/endurance-plan.json.example",
    )
    assets_ok = all((repo_root / name).is_file() for name in native_assets)
    add(
        "native_assets",
        "pass" if assets_ok else "fail",
        "all native Windows deployment assets are present"
        if assets_ok
        else "one or more native Windows deployment assets are missing",
    )
    add(
        "native_windows",
        "pass" if system_name == "Windows" else "fail",
        "native Windows runtime selected" if system_name == "Windows" else "runtime is not Windows",
    )
    browser = get_browser_status(settings)
    add(
        "chrome",
        "pass" if browser.available and browser.family == "chrome" else "fail",
        "installed Chrome browser is available"
        if browser.available and browser.family == "chrome"
        else "Google Chrome is unavailable",
        "Install Google Chrome natively."
        if not browser.available or browser.family != "chrome"
        else None,
    )
    add(
        "browser_transport",
        "pass" if not settings.use_curl_cffi_for_images else "fail",
        "all browser and PDF traffic uses the account browser transport"
        if not settings.use_curl_cffi_for_images
        else "curl_cffi image transport would bypass account proxies",
    )
    add(
        "captcha_manual_only",
        "pass"
        if configured_solver_mode in {"2captcha_manual", "capsolver_manual"}
        else "fail",
        "the external solver requires a manual one-shot authorization"
        if configured_solver_mode in {"2captcha_manual", "capsolver_manual"}
        else "set CBRS_CAPTCHA_SOLVER_MODE to a supported manual mode",
    )
    state_root = settings.profile_dir.parent
    output_drive = settings.output_dir.drive.upper()
    repository = Path(environment.get("RESTIC_REPOSITORY", ""))
    storage_ok = (
        state_root.drive.upper() == "G:"
        and output_drive == "G:"
        and repository.drive.upper() == "E:"
    )
    add(
        "storage_layout",
        "pass" if storage_ok else "fail",
        "primary state is on G: and encrypted backup repository is on E:"
        if storage_ok
        else "expected primary storage on G: and restic repository on E:",
    )
    configured_restic = environment.get("CBRS_RESTIC_EXECUTABLE_PATH", "").strip()
    restic_executable = configured_restic or shutil.which("restic")
    restic_ok = bool(restic_executable and Path(restic_executable).is_file())
    add(
        "restic",
        "pass" if restic_ok else "fail",
        "native restic is available" if restic_ok else "native restic is unavailable",
    )
    base_task_name_sets = (
        ("CBRS Worker", "CBRS Dashboard", "CBRS Daily Backup"),
        ("CBRS User Worker", "CBRS User Dashboard", "CBRS User Daily Backup"),
    )
    task_name_sets = (
        ("CBRS Worker", "CBRS Dashboard", "CBRS Daily Backup", "CBRS Runtime Watchdog"),
        (
            "CBRS User Worker",
            "CBRS User Dashboard",
            "CBRS User Daily Backup",
            "CBRS User Runtime Watchdog",
        ),
    )
    task_results_by_set = {
        names: [
            subprocess.run(
                ["schtasks.exe", "/Query", "/TN", name],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            for name in names
        ]
        for names in task_name_sets
    } if system_name == "Windows" else {names: [1, 1, 1, 1] for names in task_name_sets}
    base_task_results_by_set = {
        names: [
            subprocess.run(
                ["schtasks.exe", "/Query", "/TN", name],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            for name in names
        ]
        for names in base_task_name_sets
    } if system_name == "Windows" else {names: [1, 1, 1] for names in base_task_name_sets}
    registered_task_set = next(
        (
            names
            for names, results in base_task_results_by_set.items()
            if all(code == 0 for code in results)
        ),
        None,
    )
    add(
        "scheduled_tasks",
        "pass" if registered_task_set else "fail",
        "worker, dashboard, daily backup, and recovery watchdog tasks are registered"
        if registered_task_set
        else "one or more CBRS scheduled tasks are missing",
    )
    task_statuses: dict[str, dict[str, Any]] = {}
    if require_active_runtime:
        all_task_names = tuple(name for names in task_name_sets for name in names)
        task_statuses = _windows_task_statuses(all_task_names) if system_name == "Windows" else {}

        def task_set_active(names: tuple[str, str, str, str]) -> bool:
            enabled = all(bool(task_statuses.get(name, {}).get("enabled")) for name in names)
            persistent_registered = all(
                str(task_statuses.get(name, {}).get("state") or "").lower()
                in {"ready", "running"}
                for name in names[:2]
            )
            backup_state = str(task_statuses.get(names[2], {}).get("state") or "").lower()
            watchdog_state = str(task_statuses.get(names[3], {}).get("state") or "").lower()
            return (
                enabled
                and persistent_registered
                and backup_state in {"ready", "running"}
                and watchdog_state in {"ready", "running"}
            )

        tasks_active = any(task_set_active(names) for names in task_name_sets)
        add(
            "scheduled_tasks_active",
            "pass" if tasks_active else "fail",
            "runtime tasks are enabled; service liveness is checked by lease and health gates"
            if tasks_active
            else "one or more registered runtime tasks are disabled or unavailable",
            "Start the native runtime through Start-CbrsNative.ps1 and rerun operational readiness."
            if not tasks_active
            else None,
        )
    env_ok = bool(env_file and env_file.is_file())
    restic_password_file = Path(environment.get("RESTIC_PASSWORD_FILE", ""))
    secret_files = [path for path in (env_file, restic_password_file) if path]
    acl_ok = False
    if env_ok and restic_password_file.is_file() and system_name == "Windows":
        acl_ok = True
        for secret_file in secret_files:
            acl = subprocess.run(
                ["icacls.exe", str(secret_file)], capture_output=True, text=True, check=False
            )
            acl_text = (acl.stdout or "").lower()
            broad_acl = any(
                marker in acl_text
                for marker in ("everyone:", "todos:", "builtin\\users:", "builtin\\usuarios:")
            )
            acl_ok = acl_ok and acl.returncode == 0 and not broad_acl
    add(
        "secret_acl",
        "pass" if env_ok and acl_ok else "fail",
        "environment and restic password files exist with restricted ACLs"
        if env_ok and acl_ok
        else "secret files are missing or readable by broad groups",
    )
    raw_pool: dict[str, Any] = {}
    proxy_refs: list[str] = []
    proxy_providers: list[str] = []
    proxy_providers_ok = False
    pool_ok = False
    credentials_ok = False
    try:
        from .proxy_provider import normalize_proxy_provider

        raw_pool = json.loads(pool_config_path.read_text(encoding="utf-8"))
        enabled_accounts = [
            account
            for account in raw_pool.get("accounts", [])
            if account.get("enabled", True)
        ]
        proxy_providers = [
            normalize_proxy_provider(account.get("proxy_provider"))
            for account in enabled_accounts
        ]
        proxy_providers_ok = len(proxy_providers) == 3
        proxy_refs = [
            str(account.get("proxy_url_env") or "")
            for account in enabled_accounts
            if normalize_proxy_provider(account.get("proxy_provider"))
            != "dataimpulse_residential_sticky"
        ]
        proxy_values = [environment.get(name, "") for name in proxy_refs]
        dataimpulse_accounts = [
            account
            for account in enabled_accounts
            if normalize_proxy_provider(account.get("proxy_provider"))
            == "dataimpulse_residential_sticky"
        ]
        dataimpulse_ports = [
            int(account.get("dataimpulse_port"))
            for account in dataimpulse_accounts
        ]
        dataimpulse_credentials_ok = all(
            environment.get(name, "").strip()
            and not environment.get(name, "").strip().upper().startswith("REPLACE_")
            for name in ("DATAIMPULSE_PROXY_LOGIN", "DATAIMPULSE_PROXY_PASSWORD")
        ) if dataimpulse_accounts else True
        credential_refs = [
            (
                str(account.get("username_env") or ""),
                str(account.get("password_env") or ""),
            )
            for account in enabled_accounts
        ]
        credential_values = [
            environment.get(reference, "").strip()
            for pair in credential_refs
            for reference in pair
        ]
        credentials_ok = (
            len(credential_refs) == 3
            and all(all(pair) for pair in credential_refs)
            and all(
                value and not value.upper().startswith("REPLACE_")
                for value in credential_values
            )
        )
        pool_ok = (
            raw_pool.get("selection_policy") == "round_robin"
            and len(proxy_values) + len(dataimpulse_ports) == 3
            and all(urlparse(value).scheme in {"http", "https"} for value in proxy_values)
            and len(set(proxy_values)) == len(proxy_values)
            and len(set(dataimpulse_ports)) == len(dataimpulse_ports)
            and all(10000 <= port <= 20000 for port in dataimpulse_ports)
            and dataimpulse_credentials_ok
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    add(
        "account_credentials",
        "pass" if credentials_ok else "fail",
        "three account credential references resolve to configured secrets"
        if credentials_ok
        else "three non-placeholder account usernames and passwords are required",
    )
    add(
        "proxy_configuration",
        "pass" if pool_ok else "fail",
        "three distinct account proxy endpoints are configured"
        if pool_ok else "three distinct account proxy endpoints are required",
    )
    two_captcha_providers = {
        "2captcha_dedicated_isp",
        "2captcha_residential_sticky",
    }
    two_captcha_provider_count = sum(
        provider in two_captcha_providers for provider in proxy_providers
    )
    dataimpulse_provider_count = sum(
        provider == "dataimpulse_residential_sticky"
        for provider in proxy_providers
    )
    add(
        "proxy_provider_configuration",
        "pass" if proxy_providers_ok else "fail",
        (
            f"provider metadata is valid ({dataimpulse_provider_count} DataImpulse, "
            f"{two_captcha_provider_count} 2Captcha, "
            f"{len(proxy_providers) - two_captcha_provider_count - dataimpulse_provider_count} generic static)"
        )
        if proxy_providers_ok
        else "three valid account proxy providers are required",
    )
    provider_health: dict[str, Any] = {
        "status": "not_applicable",
        "ok": True,
    }
    if dataimpulse_provider_count:
        from .proxy_provider import dataimpulse_configuration_health

        provider_health = dataimpulse_configuration_health(
            environment.get("DATAIMPULSE_PROXY_LOGIN"),
            environment.get("DATAIMPULSE_PROXY_PASSWORD"),
        )
    elif two_captcha_provider_count:
        from .proxy_provider import two_captcha_proxy_health

        provider_name = next(
            provider for provider in proxy_providers if provider in two_captcha_providers
        )
        provider_health = two_captcha_proxy_health(
            configured_solver_key,
            provider=provider_name,
            force=True,
        )
    add(
        "proxy_provider_health",
        "pass" if provider_health.get("ok") else "fail",
        (
            "DataImpulse proxy credentials are configured; live health is verified per route"
            if dataimpulse_provider_count and provider_health.get("ok")
            else "DataImpulse proxy credentials are missing"
            if dataimpulse_provider_count
            else "2Captcha proxy account is active with traffic remaining"
            if two_captcha_provider_count and provider_health.get("ok")
            else "2Captcha proxy account is not active with usable traffic"
            if two_captcha_provider_count
            else "generic static proxies do not use a provider account check"
        ),
    )
    baseline_ok = False
    db_path = Path(environment.get("CBRS_CAPTCHA_STATE_PATH", state_root / "pool" / "pool.sqlite3"))
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path) as db:
                rows = db.execute(
                    """
                    SELECT account_id, egress_hash FROM account_checks
                    WHERE proxy_status = 'passed'
                    """
                ).fetchall()
            checks_by_account = {str(row[0]): str(row[1]) for row in rows if row[1]}
            approved_hashes: list[str] = []
            for account in raw_pool.get("accounts", []):
                if not account.get("enabled", True):
                    continue
                account_id = str(account.get("id") or "")
                profile_dir = Path(account.get("profile_dir")) if account.get("profile_dir") else (
                    state_root / "accounts" / account_id / "chrome-profile"
                )
                baseline_path = profile_dir.parent / "fixed-egress-baseline.json"
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                approved_hash = str(baseline.get("egress_hash") or "")
                if not approved_hash or checks_by_account.get(account_id) != approved_hash:
                    approved_hashes = []
                    break
                approved_hashes.append(approved_hash)
            baseline_ok = len(approved_hashes) == 3 and len(set(approved_hashes)) == 3
        except (sqlite3.Error, OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    add(
        "proxy_baselines",
        "pass" if baseline_ok and settings.expected_egress_country == "CL" else "fail",
        "three unique approved Chilean egress baselines are present"
        if baseline_ok and settings.expected_egress_country == "CL"
        else "prove and approve three unique stable Chilean egress baselines",
    )
    captcha_ok = False
    using_capsolver = configured_solver_mode.startswith("capsolver")
    captcha_provider = "CapSolver" if using_capsolver else "2Captcha"
    selected_solver_key = configured_capsolver_key if using_capsolver else configured_solver_key
    captcha_detail = f"{captcha_provider} API key is missing"
    usable_solver_key = selected_solver_key and not selected_solver_key.startswith("REPLACE_")
    if usable_solver_key:
        try:
            client = (
                CapSolverClient(selected_solver_key)
                if using_capsolver
                else TwoCaptchaClient(selected_solver_key)
            )
            balance = client.get_balance()
            captcha_ok = balance > 0
            captcha_detail = (
                f"{captcha_provider} authentication succeeded and balance is positive"
                if captcha_ok
                else f"{captcha_provider} balance is zero"
            )
        except (CapSolverError, TwoCaptchaError) as exc:
            captcha_detail = f"{captcha_provider} balance check failed ({exc.code})"
    add("captcha_balance", "pass" if captcha_ok else "fail", captcha_detail)
    stale_lease = False
    active_lease = False
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path) as db:
                lease = db.execute(
                    "SELECT expires_at FROM leases WHERE lease_name = 'portal_worker'"
                ).fetchone()
            lease_expiry = (
                datetime.fromisoformat(str(lease[0]))
                if lease and lease[0]
                else None
            )
            stale_lease = bool(
                lease_expiry and lease_expiry < datetime.now(timezone.utc)
            )
            active_lease = bool(
                lease_expiry and lease_expiry >= datetime.now(timezone.utc)
            )
        except sqlite3.Error:
            pass
    add(
        "worker_lease",
        "fail" if stale_lease else "pass",
        "no stale worker lease detected" if not stale_lease else "stale worker lease must be recovered",
    )
    if require_active_runtime:
        add(
            "worker_active_lease",
            "pass" if active_lease else "fail",
            "worker heartbeat lease is active"
            if active_lease
            else "worker task has no active heartbeat lease",
        )
        dashboard_port = int(raw_pool.get("dashboard_port") or 8765)
        dashboard_healthy = _loopback_dashboard_healthy(dashboard_port)
        add(
            "dashboard_health",
            "pass" if dashboard_healthy else "fail",
            "loopback dashboard health endpoint returned successfully"
            if dashboard_healthy
            else "loopback dashboard health endpoint is unavailable",
        )
    backup_status = state_root / "backup" / "status.json"
    backup_ok = False
    if backup_status.is_file():
        try:
            stored_backup = json.loads(backup_status.read_text(encoding="utf-8"))
            finished_at = datetime.fromisoformat(str(stored_backup.get("finished_at")))
            backup_ok = bool(stored_backup.get("ok")) and (
                datetime.now(timezone.utc) - finished_at
            ) <= timedelta(hours=36)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    add(
        "daily_backup",
        "pass" if backup_ok else "fail",
        "a successful backup is recorded" if backup_ok else "run and verify the first native backup",
    )
    blocking = [check for check in checks if check.status != "pass"]
    return {
        "schema": READINESS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "windows",
        "distro": None,
        "safety": {
            "network_requests_made": bool(usable_solver_key),
            "browser_started": False,
            "setup_changes_made": False,
            "secret_values_in_report": False,
            "captcha_tasks_created": False,
        },
        "summary": {
            "offline_assets_ready": True,
            "live_test_ready": not blocking,
            "blocking_checks": len(blocking),
            "failures": len(blocking),
            "deferred": 0,
            "warnings": 0,
        },
        "checks": [asdict(check) for check in checks],
    }


def _windows_task_statuses(
    task_names: Sequence[str],
    *,
    command_runner: Any = subprocess.run,
) -> dict[str, dict[str, Any]]:
    quoted_names = ",".join(
        "'" + str(name).replace("'", "''") + "'" for name in task_names
    )
    script = (
        f"$names=@({quoted_names});"
        "$rows=foreach($name in $names){"
        "$task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;"
        "if($task){$info=Get-ScheduledTaskInfo -TaskName $name;"
        "[pscustomobject]@{name=$name;state=[string]$task.State;"
        "enabled=[bool]$task.Settings.Enabled;last_result=[int64]$info.LastTaskResult}}};"
        "$rows|ConvertTo-Json -Compress"
    )
    result = command_runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if int(result.returncode) != 0 or not (result.stdout or "").strip():
        return {}
    try:
        raw = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    rows = raw if isinstance(raw, list) else [raw]
    statuses: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        statuses[str(row["name"])] = {
            "state": str(row.get("state") or ""),
            "enabled": bool(row.get("enabled")),
            "last_result": int(row.get("last_result") or 0),
        }
    return statuses


def _loopback_dashboard_healthy(port: int) -> bool:
    from urllib.request import urlopen

    try:
        with urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=3) as response:
            if int(response.status) != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
        return bool(isinstance(payload, dict) and payload.get("ok") is True)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
