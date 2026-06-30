import json
from pathlib import Path

from cbrs.config import load_settings
from cbrs.proxy_health import run_proxy_health


def test_proxy_health_fails_when_recaptcha_script_cannot_tunnel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = load_settings(
        {
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
            "CBRS_PROXY_URL": "http://user:pass@example.test:33335",
        },
        root=tmp_path,
    )
    monkeypatch.setattr(
        "cbrs.proxy_health.fetch_public_egress",
        lambda loaded_settings: {"ip": "1.2.3.4", "country": "CL"},
    )

    def fake_request_status(loaded_settings, url, **kwargs):
        if "recaptcha/enterprise.js" in url:
            return None, "net::ERR_TUNNEL_CONNECTION_FAILED"
        return 200, "status=200"

    monkeypatch.setattr("cbrs.proxy_health._request_status", fake_request_status)

    result = run_proxy_health(settings, write_report=False)

    assert result.ok is False
    assert "google recaptcha script: net::ERR_TUNNEL_CONNECTION_FAILED" in result.report["errors"]


def test_proxy_health_report_redacts_proxy_credentials(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(
        {
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
            "CBRS_PROXY_URL": "http://user:pass@example.test:33335",
        },
        root=tmp_path,
    )
    monkeypatch.setattr(
        "cbrs.proxy_health.fetch_public_egress",
        lambda loaded_settings: {"ip": "1.2.3.4", "country": "CL"},
    )
    monkeypatch.setattr(
        "cbrs.proxy_health._request_status",
        lambda loaded_settings, url, **kwargs: (200, "status=200"),
    )

    result = run_proxy_health(settings, write_report=True)
    assert result.report_path is not None
    data = json.loads(result.report_path.read_text(encoding="utf-8"))
    text = result.report_path.read_text(encoding="utf-8")

    assert result.ok is True
    assert data["proxy_configured"] is True
    assert data["proxy_scheme"] == "http"
    assert data["proxy_port"] == 33335
    assert data["proxy_host_hash"]
    assert "user:pass" not in text
    assert "example.test" not in text


def test_proxy_health_posts_json_to_cbrs_home_start(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(
        {
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
            "CBRS_PROXY_URL": "http://user:pass@example.test:33335",
        },
        root=tmp_path,
    )
    monkeypatch.setattr(
        "cbrs.proxy_health.fetch_public_egress",
        lambda loaded_settings: {"ip": "1.2.3.4", "country": "CL"},
    )
    calls = []

    def fake_request_status(loaded_settings, url, **kwargs):
        calls.append((url, kwargs))
        return 200, "status=200"

    monkeypatch.setattr("cbrs.proxy_health._request_status", fake_request_status)

    result = run_proxy_health(settings, write_report=False)
    home_start_call = next(call for call in calls if call[0].endswith("/api/v1/home/start"))

    assert result.ok is True
    assert home_start_call[1]["data"] == b"{}"
    assert home_start_call[1]["headers"]["content-type"] == "application/json"
