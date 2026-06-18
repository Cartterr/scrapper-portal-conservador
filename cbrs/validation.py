from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser_runtime import browser_runtime_metadata
from .config import SETTINGS, Settings
from .safety import redact


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
    path = log_dir / f"validation-{timestamp}.json"
    path.write_text(
        json.dumps(redact(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
