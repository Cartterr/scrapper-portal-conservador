import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cbrs import request_log
from cbrs.config import load_settings


def _request(url, method="GET", resource_type="xhr"):
    return SimpleNamespace(url=url, method=method, resource_type=resource_type)


def test_records_portal_and_recaptcha_calls_without_query_or_tokens(tmp_path):
    directory = tmp_path / "requests" / "a1"
    at = datetime(2026, 9, 24, 14, 7, tzinfo=timezone.utc)
    ticket = "eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop"
    session = {"client": "chrome", "profile": "chrome-profile-route-3-port-10491", "port": 10491, "headless": True}
    request_log.record(directory, _request(f"https://nuevo-portal.conservador.cl/api/v1/doc/{ticket}/img?page=2&t=x",
                                           method="POST"), 200, now=at, session=session)
    request_log.record(directory, _request("https://www.google.com/recaptcha/enterprise/reload?k=x", method="POST"),
                       200, now=at, session=session)
    request_log.record(directory, _request("https://nuevo-portal.conservador.cl/app.js", resource_type="script"), 200, now=at)
    request_log.record(directory, _request("https://fonts.gstatic.com/s/roboto.woff2", resource_type="font"), 200, now=at)
    request_log.record(directory, _request("https://ipinfo.io/json"), 200, now=at)
    lines = (directory / "2026-09-24.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"at": "2026-09-24T14:07:00.000+00:00", "method": "POST", "host": "nuevo-portal.conservador.cl",
         "path": "/api/v1/doc/:id/img", "type": "xhr", "status": 200, **session},
        {"at": "2026-09-24T14:07:00.000+00:00", "method": "POST", "host": "www.google.com",
         "path": "/recaptcha/enterprise/reload", "type": "xhr", "status": 200, **session},
    ]
    assert ticket not in lines[0] and "page=" not in lines[0]


def test_session_names_profile_port_and_mode():
    settings = SimpleNamespace(profile_dir="/r/accounts/a1/chrome-profile-route-4-port-14068",
                               proxy_url="http://user:secret@gw.dataimpulse.com:14068")
    assert request_log.session_of(settings, headless=False) == {
        "client": "chrome", "profile": "chrome-profile-route-4-port-14068", "port": 14068, "headless": False}


def test_proxy_health_portal_call_is_recorded(tmp_path, monkeypatch):
    # It reaches the portal through the account's exit without Chrome.
    from cbrs import proxy_health
    settings = SimpleNamespace(log_dir=tmp_path, account_id="a1", profile_dir=tmp_path / "p",
                               proxy_url=None, base_url="https://nuevo-portal.conservador.cl",
                               commerce_url="https://nuevo-portal.conservador.cl/x")
    monkeypatch.setattr(proxy_health, "_send", lambda settings, request: (403, "status=403"))
    proxy_health._check_cbrs_home_start(settings, [], [])
    [entry] = request_log.read(settings, "a1")
    assert (entry["method"], entry["path"], entry["status"], entry["type"], entry["client"]) == (
        "POST", "/api/v1/home/start", 403, "health", "proxy_health")


def test_window_read_and_per_endpoint_summary(tmp_path):
    settings = SimpleNamespace(log_dir=tmp_path, account_id="a1")
    directory = request_log.request_log_dir(settings)  # window test needs no session
    for hour, path, status in [(13, "/login", 200), (15, "/api/v1/auth/login", 400),
                               (16, "/api/v1/auth/login", 400), (23, "/api/v1/auth/login", 401)]:
        request_log.record(directory, _request("https://nuevo-portal.conservador.cl" + path, method="POST"),
                           status, now=datetime(2026, 9, 24, hour, tzinfo=timezone.utc))
    entries = list(request_log.read(settings, "a1", since="2026-09-24T14:07", until="2026-09-24T22:37"))
    assert request_log.summary(entries) == [("POST", "/api/v1/auth/login", "400", 2)]


def test_cli_prints_counts_per_endpoint(tmp_path, monkeypatch, capsys):
    from cbrs import cli
    settings = load_settings({}, root=tmp_path)
    monkeypatch.setattr(cli.config, "SETTINGS", settings)
    directory = settings.log_dir / "requests" / "a1"
    request_log.record(directory, _request("https://nuevo-portal.conservador.cl/api/v1/buscar", method="POST"), 200)
    args = cli.build_parser().parse_args(["requests", "a1"])
    assert cli.cmd_requests(args) == 0
    out = capsys.readouterr().out
    assert "1 portal request(s) for a1" in out and "POST" in out and "/api/v1/buscar" in out


def test_real_chrome_context_records_responses_and_failures(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    settings = SimpleNamespace(log_dir=tmp_path, account_id="a1", profile_dir=tmp_path / "prof", proxy_url=None)
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - machine without Chrome
            pytest.skip(f"Chrome unavailable: {exc}")
        context = browser.new_context()
        request_log.attach(context, settings, headless=True)

        def portal(route):
            if route.request.url.endswith("/fail"):
                route.abort()
            elif "/api/" in route.request.url:
                route.fulfill(status=400, body="{}", content_type="application/json")
            else:
                route.fulfill(status=200, body="<p>index</p>", content_type="text/html")

        context.route("https://nuevo-portal.conservador.cl/**", portal)
        page = context.new_page()
        page.goto("https://nuevo-portal.conservador.cl/consultas-en-linea?x=1")
        page.evaluate("""async () => {
            await fetch('/api/v1/auth/login', {method: 'POST'});
            try { await fetch('/api/v1/fail'); } catch (e) {}
        }""")
        page.wait_for_timeout(200)
        browser.close()
    entries = list(request_log.read(settings, "a1"))
    assert all(e["headless"] is True and e["profile"] == "prof" for e in entries)
    assert [(e["method"], e["path"], e["status"], e["type"]) for e in entries] == [
        ("GET", "/consultas-en-linea", 200, "document"),
        ("POST", "/api/v1/auth/login", 400, "fetch"),
        ("GET", "/api/v1/fail", "failed", "fetch"),
    ]
