import json
from pathlib import Path

from cbrs.config import load_settings
from cbrs.validation import (
    finish_validation_report,
    new_validation_report,
    write_validation_report,
)


def test_validation_report_does_not_store_query_value(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
        },
        root=tmp_path,
    )
    preflight_metadata = {
        "expected_egress_country": "CL",
        "egress_mode": "client_vpn",
        "egress_country": "CL",
        "egress_hash": "abc123",
        "preflight_status": "passed",
    }

    report = new_validation_report(
        settings,
        search_kind="text",
        download_first=False,
        preflight_metadata=preflight_metadata,
    )

    assert report["query_value_saved"] is False
    assert "query" not in report
    assert report["browser_backend"] == "chrome"
    assert report["browser_headless"] is False
    assert report["browser_window_mode"] == "normal"
    assert report["profile_hash"]
    assert report["expected_egress_country"] == "CL"
    assert report["egress_mode"] == "client_vpn"
    assert report["egress_country"] == "CL"
    assert report["egress_hash"] == "abc123"
    assert report["preflight_status"] == "passed"
    assert "fingerprint_seed" not in report
    assert "cloak_proxy_url" not in report


def test_write_validation_report_redacts_sensitive_values(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    report = new_validation_report(settings, search_kind="text", download_first=True)
    report["ticket"] = "secret-ticket"
    report["Authorization"] = "Bearer secret"
    report["fingerprint_seed"] = "12345"
    report["raw_ip"] = "1.2.3.4"
    finish_validation_report(report, status="passed")

    path = write_validation_report(report, settings)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["ticket"] == "[REDACTED]"
    assert data["Authorization"] == "[REDACTED]"
    assert data["fingerprint_seed"] == "[REDACTED]"
    assert data["raw_ip"] == "[REDACTED]"
    assert "1.2.3.4" not in path.read_text(encoding="utf-8")
