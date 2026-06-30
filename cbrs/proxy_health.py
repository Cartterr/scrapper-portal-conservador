from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import SETTINGS, Settings, proxy_metadata
from .preflight import fetch_public_egress
from .proxy import build_authenticated_proxy_opener
from .safety import redact

RECAPTCHA_SCRIPT_URL = "https://www.google.com/recaptcha/enterprise.js"
REQUEST_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class ProxyHealthResult:
    ok: bool
    report: dict[str, Any]
    report_path: Path | None = None


def run_proxy_health(
    settings: Settings = SETTINGS,
    *,
    write_report: bool = True,
) -> ProxyHealthResult:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    started_at = _now()

    proxy_meta = proxy_metadata(settings)
    if not settings.proxy_url:
        _add_check(checks, errors, "proxy configured", True, "not_required")
    else:
        _add_check(checks, errors, "proxy configured", True, "configured")
        _check_egress(settings, checks, errors)
        _check_recaptcha(settings, checks, errors)
        _check_cbrs_home_start(settings, checks, errors)

    report = {
        "schema": "cbrs-proxy-health-v1",
        "started_at": started_at,
        "finished_at": _now(),
        "status": "passed" if not errors else "failed",
        "expected_egress_country": settings.expected_egress_country,
        "checks": checks,
        "errors": errors,
        **proxy_meta,
    }
    report_path = write_proxy_health_report(report, settings) if write_report else None
    return ProxyHealthResult(ok=not errors, report=report, report_path=report_path)


def proxy_health_validation_metadata(result: ProxyHealthResult | None) -> dict[str, Any]:
    if result is None:
        return {
            "proxy_health_status": None,
            "proxy_health_report_path": None,
        }
    return {
        "proxy_health_status": result.report.get("status"),
        "proxy_health_report_path": str(result.report_path) if result.report_path else None,
    }


def write_proxy_health_report(report: dict[str, Any], settings: Settings = SETTINGS) -> Path:
    log_dir = settings.profile_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _unique_report_path(log_dir, "proxy-health", timestamp)
    path.write_text(json.dumps(redact(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _check_egress(settings: Settings, checks: list[dict[str, Any]], errors: list[str]) -> None:
    try:
        egress = fetch_public_egress(settings)
        country = str(egress.get("country") or "").upper()
        _add_check(
            checks,
            errors,
            "egress country",
            country == settings.expected_egress_country,
            country or "unknown",
        )
    except Exception as exc:
        _add_check(checks, errors, "egress country", False, _safe_error(exc))


def _check_recaptcha(settings: Settings, checks: list[dict[str, Any]], errors: list[str]) -> None:
    url = f"{RECAPTCHA_SCRIPT_URL}?render={settings.recaptcha_sitekey}"
    status, detail = _request_status(settings, url, method="GET")
    _add_check(
        checks,
        errors,
        "google recaptcha script",
        status == 200,
        detail,
    )


def _check_cbrs_home_start(settings: Settings, checks: list[dict[str, Any]], errors: list[str]) -> None:
    status, detail = _request_status(
        settings,
        f"{settings.base_url}/api/v1/home/start",
        method="POST",
        data=b"{}",
        headers={
            "content-type": "application/json",
            "origin": settings.base_url,
            "referer": settings.commerce_url,
            "accept": "application/json, text/plain, */*",
        },
    )
    _add_check(checks, errors, "cbrs home start", status == 200, detail)


def _request_status(
    settings: Settings,
    url: str,
    *,
    method: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, str]:
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "user-agent": "Mozilla/5.0 cbrs-proxy-health/1.0",
            **(headers or {}),
        },
    )
    opener = _proxy_opener(settings.proxy_url)
    open_fn = opener.open if opener else urlopen
    try:
        with open_fn(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return int(response.status), f"status={int(response.status)}"
    except HTTPError as exc:
        return exc.code, f"status={exc.code}"
    except URLError as exc:
        return None, _safe_error(exc)
    except Exception as exc:
        return None, _safe_error(exc)


def _proxy_opener(proxy_url: str | None):
    return build_authenticated_proxy_opener(
        proxy_url,
        supported_schemes={"http", "https"},
        error_prefix="proxy health",
    )


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


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:240] or type(exc).__name__


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
    raise RuntimeError("Could not allocate unique proxy health report path")
