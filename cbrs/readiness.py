from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
    system_name: str | None = None,
    wsl_distros: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Inspect readiness without browser startup, network traffic, or setup changes."""
    repo_root = repo_root.resolve()
    env_file = env_file.resolve() if env_file else None
    pool_config_path = pool_config_path.resolve()
    system_name = system_name or platform.system()
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
        if settings.egress_mode != "dedicated_static_isp":
            setting_problems.append("egress mode must be dedicated_static_isp")
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
        "Offline only: no browser, setup, or network traffic was started.",
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
