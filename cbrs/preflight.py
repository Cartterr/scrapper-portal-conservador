from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from .browser_runtime import get_browser_status, profile_hash
from .config import ALLOWED_EGRESS_MODES, PERSONAL_DIRECT_EGRESS_MODE, SETTINGS, Settings
from .safety import redact

EGRESS_INFO_URL = "https://ipinfo.io/json"


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    report: dict[str, Any]
    report_path: Path | None = None


class PreflightError(RuntimeError):
    pass


def run_preflight(
    settings: Settings = SETTINGS,
    *,
    fetch_egress: Callable[[], dict[str, Any]] | None = None,
    write_report: bool = True,
    approve_baseline: bool = False,
) -> PreflightResult:
    fetch_egress = fetch_egress or fetch_public_egress
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    browser_status = get_browser_status(settings)

    _add_check(
        checks,
        errors,
        "browser executable",
        browser_status.available,
        browser_status.family or browser_status.error or "missing",
    )
    _add_check(
        checks,
        errors,
        "browser backend",
        settings.browser_backend == "chrome",
        settings.browser_backend,
    )
    _add_check(
        checks,
        errors,
        "proxy disabled",
        settings.cloak_proxy_url is None,
        "not configured" if settings.cloak_proxy_url is None else "CBRS_CLOAK_PROXY_URL configured",
    )
    _add_check(
        checks,
        errors,
        "egress mode",
        _egress_mode_allowed(settings),
        _egress_mode_detail(settings),
    )

    egress_info: dict[str, Any] = {}
    egress_hash: str | None = None
    egress_country: str | None = None
    if not errors:
        try:
            egress_info = fetch_egress()
            raw_ip = str(egress_info.get("ip") or "").strip()
            egress_hash = _hash_text(raw_ip) if raw_ip else None
            egress_country = str(egress_info.get("country") or "").strip().upper() or None
        except Exception as exc:
            errors.append(f"egress lookup failed: {exc}")

        _add_check(
            checks,
            errors,
            "egress country",
            egress_country == settings.expected_egress_country,
            egress_country or "unknown",
        )
    else:
        checks.append(
            {
                "name": "egress country",
                "ok": False,
                "detail": "not_checked",
            }
        )

    baseline_status = "not_checked"
    if not errors and egress_hash:
        baseline_status = _check_or_create_baseline(
            settings,
            egress_hash=egress_hash,
            egress_country=egress_country,
            approve=approve_baseline,
        )
        if baseline_status == "mismatch":
            errors.append("fixed egress hash differs from saved baseline")
        elif baseline_status == "approval_required":
            errors.append(
                "fixed egress baseline approval required; rerun preflight with "
                "--approve-egress-baseline only from the intended client-owned Chile egress"
            )

    checks.append(
        {
            "name": "egress baseline",
            "ok": baseline_status in {"created", "matched"},
            "detail": baseline_status,
        }
    )

    report = {
        "schema": "cbrs-preflight-v1",
        "started_at": _now(),
        "finished_at": _now(),
        "status": "passed" if not errors else "failed",
        "browser_backend": settings.browser_backend,
        "browser_family": browser_status.family,
        "browser_executable_source": browser_status.source,
        "browser_executable_hash": _hash_text(str(browser_status.path)) if browser_status.path else None,
        "profile_hash": profile_hash(settings),
        "egress_mode": settings.egress_mode or None,
        "expected_egress_country": settings.expected_egress_country,
        "egress_country": egress_country,
        "egress_hash": egress_hash,
        "checks": checks,
        "errors": errors,
    }
    report_path = write_preflight_report(report, settings) if write_report else None
    return PreflightResult(ok=not errors, report=report, report_path=report_path)


def ensure_preflight_ok(settings: Settings = SETTINGS) -> PreflightResult:
    result = run_preflight(settings, write_report=True)
    if not result.ok:
        raise PreflightError("Fixed-egress preflight failed.")
    return result


def fetch_public_egress() -> dict[str, Any]:
    request = Request(EGRESS_INFO_URL, headers={"user-agent": "cbrs-preflight/1.0"})
    with urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8", errors="replace")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise RuntimeError("egress service returned unexpected payload")
    return data


def write_preflight_report(
    report: dict[str, Any],
    settings: Settings = SETTINGS,
) -> Path:
    log_dir = settings.profile_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _unique_report_path(log_dir, "preflight", timestamp)
    path.write_text(
        json.dumps(redact(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def preflight_validation_metadata(result: PreflightResult) -> dict[str, Any]:
    report = result.report
    return {
        "expected_egress_country": report.get("expected_egress_country"),
        "egress_mode": report.get("egress_mode"),
        "egress_country": report.get("egress_country"),
        "egress_hash": report.get("egress_hash"),
        "profile_hash": report.get("profile_hash"),
        "preflight_status": report.get("status"),
    }


def baseline_file(settings: Settings = SETTINGS) -> Path:
    return settings.profile_dir.parent / "fixed-egress-baseline.json"


def _check_or_create_baseline(
    settings: Settings,
    *,
    egress_hash: str,
    egress_country: str | None,
    approve: bool,
) -> str:
    path = baseline_file(settings)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return "matched" if data.get("egress_hash") == egress_hash else "mismatch"

    if not approve:
        return "approval_required"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "cbrs-fixed-egress-baseline-v1",
                "created_at": _now(),
                "egress_hash": egress_hash,
                "egress_country": egress_country,
                "profile_hash": profile_hash(settings),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return "created"


def _add_check(
    checks: list[dict[str, Any]],
    errors: list[str],
    name: str,
    ok: bool,
    detail: str,
) -> None:
    checks.append({"name": name, "ok": ok, "detail": detail})
    if not ok:
        errors.append(f"{name}: {detail}")


def _egress_mode_allowed(settings: Settings) -> bool:
    if settings.egress_mode in ALLOWED_EGRESS_MODES:
        return True
    return (
        settings.egress_mode == PERSONAL_DIRECT_EGRESS_MODE
        and settings.allow_personal_egress
    )


def _egress_mode_detail(settings: Settings) -> str:
    if not settings.egress_mode:
        return "not configured"
    if settings.egress_mode == PERSONAL_DIRECT_EGRESS_MODE:
        return (
            "personal_direct acknowledged"
            if settings.allow_personal_egress
            else "personal_direct requires CBRS_ALLOW_PERSONAL_EGRESS=1"
        )
    return settings.egress_mode


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _unique_report_path(log_dir: Path, prefix: str, timestamp: str) -> Path:
    path = log_dir / f"{prefix}-{timestamp}.json"
    if not path.exists():
        return path
    for counter in range(2, 1000):
        candidate = log_dir / f"{prefix}-{timestamp}-{counter}.json"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate unique preflight report path")
