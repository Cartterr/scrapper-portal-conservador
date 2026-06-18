import json
from pathlib import Path

from cbrs.config import load_settings
from cbrs.preflight import baseline_file, run_preflight


def test_preflight_requires_explicit_baseline_approval(tmp_path: Path) -> None:
    settings = _settings_with_browser(tmp_path)

    result = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        write_report=False,
    )

    assert result.ok is False
    assert result.report["checks"][-1]["detail"] == "approval_required"
    assert not baseline_file(settings).exists()


def test_preflight_requires_egress_mode_before_network_lookup(tmp_path: Path) -> None:
    browser = tmp_path / "chrome.exe"
    browser.write_text("", encoding="utf-8")
    settings = load_settings({"CBRS_BROWSER_EXECUTABLE_PATH": str(browser)}, root=tmp_path)

    result = run_preflight(
        settings,
        fetch_egress=lambda: (_raise("egress lookup should not run")),
        write_report=False,
    )

    assert result.ok is False
    assert "egress mode: not configured" in result.report["errors"]
    assert result.report["egress_hash"] is None
    assert result.report["checks"][4]["detail"] == "not_checked"


def test_preflight_rejects_personal_direct_without_ack(tmp_path: Path) -> None:
    settings = _settings_with_browser(
        tmp_path,
        {
            "CBRS_EGRESS_MODE": "personal_direct",
            "CBRS_ALLOW_PERSONAL_EGRESS": "0",
        },
    )

    result = run_preflight(
        settings,
        fetch_egress=lambda: (_raise("egress lookup should not run")),
        write_report=False,
    )

    assert result.ok is False
    assert "personal_direct requires CBRS_ALLOW_PERSONAL_EGRESS=1" in result.report["errors"][0]
    assert result.report["egress_hash"] is None


def test_preflight_allows_personal_direct_with_ack(tmp_path: Path) -> None:
    settings = _settings_with_browser(
        tmp_path,
        {
            "CBRS_EGRESS_MODE": "personal_direct",
            "CBRS_ALLOW_PERSONAL_EGRESS": "1",
        },
    )

    result = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        approve_baseline=True,
        write_report=False,
    )

    assert result.ok is True
    assert result.report["egress_mode"] == "personal_direct"


def test_preflight_creates_sanitized_baseline_and_report_after_approval(tmp_path: Path) -> None:
    settings = _settings_with_browser(tmp_path)

    result = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        approve_baseline=True,
        write_report=True,
    )

    assert result.ok is True
    assert result.report["egress_country"] == "CL"
    assert result.report["egress_hash"]
    assert "1.2.3.4" not in json.dumps(result.report)
    assert result.report_path is not None
    assert "1.2.3.4" not in result.report_path.read_text(encoding="utf-8")
    assert baseline_file(settings).exists()


def test_preflight_reuses_matching_egress_baseline(tmp_path: Path) -> None:
    settings = _settings_with_browser(tmp_path)

    first = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        approve_baseline=True,
        write_report=False,
    )
    second = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        write_report=False,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.report["checks"][-1]["detail"] == "matched"


def test_preflight_fails_when_egress_hash_changes(tmp_path: Path) -> None:
    settings = _settings_with_browser(tmp_path)
    run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        approve_baseline=True,
        write_report=False,
    )

    result = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "5.6.7.8", "country": "CL"},
        write_report=False,
    )

    assert result.ok is False
    assert "fixed egress hash differs" in " ".join(result.report["errors"])


def test_preflight_fails_when_country_is_not_expected(tmp_path: Path) -> None:
    settings = _settings_with_browser(tmp_path)

    result = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "US"},
        write_report=False,
    )

    assert result.ok is False
    assert "egress country: US" in result.report["errors"]


def test_preflight_fails_when_proxy_is_configured(tmp_path: Path) -> None:
    settings = _settings_with_browser(
        tmp_path,
        {"CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234"},
    )

    result = run_preflight(
        settings,
        fetch_egress=lambda: {"ip": "1.2.3.4", "country": "CL"},
        write_report=False,
    )

    assert result.ok is False
    assert "proxy disabled: CBRS_CLOAK_PROXY_URL configured" in result.report["errors"]


def _settings_with_browser(tmp_path: Path, env: dict[str, str] | None = None):
    browser = tmp_path / "chrome.exe"
    browser.write_text("", encoding="utf-8")
    merged = {
        "CBRS_BROWSER_EXECUTABLE_PATH": str(browser),
        "CBRS_EGRESS_MODE": "client_vpn",
        **(env or {}),
    }
    return load_settings(merged, root=tmp_path)


def _raise(message: str):
    raise AssertionError(message)
