from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .browser_runtime import browser_runtime_metadata
from .config import SETTINGS, Settings
from .preflight import preflight_validation_metadata, run_preflight
from .safety import SafetyStopException, StopReason
from .safety import redact


@dataclass(frozen=True)
class ValidationRunResult:
    exit_code: int
    status: str
    report: dict[str, Any]
    report_path: Path
    preflight_report_path: Path | None
    result_count: int | None = None
    pdf_path: Path | None = None
    pdf_size_bytes: int | None = None
    safety_stop: str | None = None
    error: str | None = None


def new_validation_report(
    settings: Settings = SETTINGS,
    *,
    search_kind: str,
    download_first: bool,
    preflight_metadata: dict[str, Any] | None = None,
    headless: bool | None = None,
) -> dict[str, Any]:
    preflight_metadata = preflight_metadata or {
        "expected_egress_country": settings.expected_egress_country,
        "egress_mode": settings.egress_mode or None,
        "egress_country": None,
        "egress_hash": None,
        "preflight_status": None,
    }
    return {
        "schema": "cbrs-validation-v1",
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "search_kind": search_kind,
        "query_value_saved": False,
        "download_first": download_first,
        "result_count": None,
        "pdf_created": False,
        "pdf_path": None,
        "pdf_size_bytes": None,
        "request_delay_seconds": settings.request_delay_seconds,
        "browser_headless": settings.headless if headless is None else headless,
        "browser_window_mode": settings.window_mode,
        **browser_runtime_metadata(settings),
        **preflight_metadata,
        "image_transport": (
            "curl_cffi_compatibility"
            if settings.use_curl_cffi_for_images
            else "browser_origin"
        ),
        "safety_stop": None,
        "error": None,
    }


def finish_validation_report(
    report: dict[str, Any],
    *,
    status: str,
    safety_stop: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    report["finished_at"] = _now()
    report["status"] = status
    report["safety_stop"] = safety_stop
    report["error"] = error
    return report


def write_validation_report(
    report: dict[str, Any],
    settings: Settings = SETTINGS,
) -> Path:
    log_dir = settings.profile_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _unique_report_path(log_dir, "validation", timestamp)
    path.write_text(
        json.dumps(redact(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def run_controlled_validation(
    *,
    settings: Settings = SETTINGS,
    search_kind: str,
    query: str | None = None,
    foja: int | None = None,
    numero: int | None = None,
    ano: int | None = None,
    download_first: bool = False,
    output_dir: Path | None = None,
    keep_images: bool = False,
    headless: bool | None = None,
    scraper_factory: Callable[..., Any] | None = None,
    preflight_runner: Callable[..., Any] | None = None,
) -> ValidationRunResult:
    preflight_runner = preflight_runner or run_preflight
    preflight_result = preflight_runner(settings, write_report=True)
    runtime_headless = settings.headless if headless is None else headless
    report = new_validation_report(
        settings,
        search_kind=search_kind,
        download_first=download_first,
        preflight_metadata=preflight_validation_metadata(preflight_result),
        headless=runtime_headless,
    )

    if not preflight_result.ok:
        finish_validation_report(
            report,
            status="safety_stop",
            safety_stop=StopReason.EGRESS_PREFLIGHT.value,
            error="Fixed-egress preflight failed.",
        )
        report_path = write_validation_report(report, settings)
        return ValidationRunResult(
            exit_code=2,
            status="safety_stop",
            report=report,
            report_path=report_path,
            preflight_report_path=preflight_result.report_path,
            safety_stop=StopReason.EGRESS_PREFLIGHT.value,
            error="Fixed-egress preflight failed.",
        )

    try:
        if scraper_factory is None:
            from .scraper import CBRSScraper

            scraper_factory = CBRSScraper

        with scraper_factory(headless=runtime_headless, settings=settings) as scraper:
            if search_kind == "text":
                if query is None:
                    raise ValueError("query is required for text validation")
                results = scraper.search_by_text(query)
            else:
                if foja is None or numero is None or ano is None:
                    raise ValueError("foja, numero, and ano are required for fna validation")
                results = scraper.search_by_fna(foja, numero, ano)

            report["result_count"] = len(results)
            pdf_path: Path | None = None
            pdf_size: int | None = None
            if download_first:
                if not results:
                    raise RuntimeError("Cannot download: validation search returned no results.")
                ticket = results[0].get("ticket")
                if not ticket:
                    raise RuntimeError("Cannot download: first result did not include a ticket.")
                pdf_path = scraper.download_all_images(
                    ticket,
                    output_dir or settings.output_dir,
                    keep_images=keep_images,
                )
                pdf_size = pdf_path.stat().st_size
                report["pdf_created"] = True
                report["pdf_path"] = str(pdf_path)
                report["pdf_size_bytes"] = pdf_size

        finish_validation_report(report, status="passed")
        report_path = write_validation_report(report, settings)
        return ValidationRunResult(
            exit_code=0,
            status="passed",
            report=report,
            report_path=report_path,
            preflight_report_path=preflight_result.report_path,
            result_count=report.get("result_count"),
            pdf_path=pdf_path,
            pdf_size_bytes=pdf_size,
        )
    except SafetyStopException as exc:
        finish_validation_report(
            report,
            status="safety_stop",
            safety_stop=exc.reason.value,
            error=str(exc),
        )
        report_path = write_validation_report(report, settings)
        return ValidationRunResult(
            exit_code=2,
            status="safety_stop",
            report=report,
            report_path=report_path,
            preflight_report_path=preflight_result.report_path,
            result_count=report.get("result_count"),
            safety_stop=exc.reason.value,
            error=str(exc),
        )
    except Exception as exc:
        finish_validation_report(report, status="failed", error=str(exc))
        report_path = write_validation_report(report, settings)
        return ValidationRunResult(
            exit_code=1,
            status="failed",
            report=report,
            report_path=report_path,
            preflight_report_path=preflight_result.report_path,
            result_count=report.get("result_count"),
            error=str(exc),
        )


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
    raise RuntimeError("Could not allocate unique validation report path")
