"""Regression cases from Santiago's 2026-09-30 report and the 2026-10-06 call.

D32 the dialog a rejected search reCAPTCHA leaves open is not route evidence,
D33 a cancelled job never answers a new request for the same inscription,
D34 a pool held by the login cap waits with resume_at instead of re-claiming,
D35 an IP lookup service error is not a proxy failure, and the opt-in session
keep-alive. No portal traffic: real Chrome only renders local HTML.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from playwright.sync_api import sync_playwright

from cbrs import config, preflight, runtime_logic
from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig
from cbrs.api import Client, QuotaExhausted, _LocalBackend
from cbrs.browser_session import BrowserFetchResponse, BrowserSession, CommerceAuthState
from cbrs.config import load_settings
from cbrs.form_search import (
    PORTAL_ERROR_DIALOG,
    dialog_attributed,
    portal_dialog_evidence,
    quota_hold,
    record_quota_hold,
    search_fna_form,
)
from cbrs.jobs import JobStore, WORKER_LEASE_NAME, _ManagedAccountScraper, _PersistentAccountBrowsers
from cbrs.runtime_observation import sample_auth
from cbrs.safety import SafetyStopException, StopReason

ERROR_MESSAGE = "Se ha detectado un problema, refresque la página e intente nuevamente."


@pytest.fixture(scope="module")
def chrome():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


def modal(message, *, closable=True, panel="headlessui-dialog-panel-31"):
    onclick = " onclick=\"this.closest('[id^=headlessui]').remove()\"" if closable else ""
    return (
        f'<div id="{panel}" data-headlessui-state="open">'
        f"<h3>Atención</h3><p>{message}</p><button{onclick}>Cerrar</button></div>"
    )


FORM = """<section aria-label="Búsqueda por foja, número y año">
<input id="input-fojas"><input id="input-numero"><input id="input-ano">
<button onclick="submitSearch()">Buscar</button><button>Limpiar</button></section>
<div id="results"></div><script>
async function submitSearch(){
 const body={foja:document.querySelector('#input-fojas').value,
 numero:document.querySelector('#input-numero').value,ano:document.querySelector('#input-ano').value};
 const res=await fetch('/api/v1/comercio/indice/texto',{method:'POST',body:JSON.stringify(body)});
 document.querySelector('#results').innerHTML=(await res.json()).modal;
}
</script>"""


class RejectingClient:
    """Browser solver mode: the observed rejection stays a rejection."""

    def post_json(self, *args, **kwargs):
        raise SafetyStopException(StopReason.CAPTCHA_REJECTED, "captcha-rechazado",
                                  status=400, context="commerce form search", response_code="captcha-rechazado")


def _rejected_search(chrome, *, closable):
    page = chrome.new_page()
    submissions = []

    def serve(route):
        if route.request.url.endswith("/protected"):
            route.fulfill(content_type="text/html; charset=utf-8", body=FORM)
        elif route.request.url.endswith("/texto"):
            submissions.append(route.request.post_data_json)
            route.fulfill(status=400, content_type="application/json",
                          body=json.dumps({"code": "captcha-rechazado",
                                           "modal": modal(ERROR_MESSAGE, closable=closable)}))
        else:
            route.fulfill(content_type="application/json", body="{}")

    page.route("**/*", serve)
    page.goto("http://127.0.0.1:19996/protected")
    browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
    with pytest.raises(SafetyStopException) as caught:
        search_fna_form(browser, 9441, 4580, 1980, client=RejectingClient(), pace=lambda _: None)
    assert caught.value.reason is StopReason.CAPTCHA_REJECTED
    assert len(submissions) == 1
    return page, browser


def _sampler(tmp_path, browser):
    store = JobStore(tmp_path / "pool.sqlite3")
    pool = _PersistentAccountBrowsers(scraper_factory=None, headless=False, store=store, worker_id="test")
    browser.detect_commerce_auth_state = lambda: CommerceAuthState.AUTHENTICATED_FORM
    entry = _ManagedAccountScraper(manager=None, scraper=browser, settings=load_settings({}, root=tmp_path))
    pool._entries["a1"] = entry
    return store, pool, entry


# --------------------------------------------------------------------------
# D32
# --------------------------------------------------------------------------


def test_rejected_captcha_closes_its_dialog_so_the_sampler_sees_a_clean_page(chrome, tmp_path):
    page, browser = _rejected_search(chrome, closable=True)
    try:
        assert portal_dialog_evidence(page) is None
        assert browser.attributed_dialog is None
        store, pool, entry = _sampler(tmp_path, browser)
        sample_auth(pool)
        assert entry.portal_error_dialog is False
        assert store.account_check("a1")["browser_status"] == "authenticated_form_visible"
        assert not page.is_closed()
    finally:
        page.close()


def test_unclosable_rejection_dialog_is_attributed_not_passive_route_evidence(chrome, tmp_path):
    page, browser = _rejected_search(chrome, closable=False)
    try:
        evidence = portal_dialog_evidence(page)
        assert evidence["reason"] == PORTAL_ERROR_DIALOG  # the close click did not work
        assert browser.attributed_dialog["cause"] == "captcha_rejected"
        assert dialog_attributed(browser, evidence)
        store, pool, entry = _sampler(tmp_path, browser)
        sample_auth(pool)
        assert entry.portal_error_dialog is False
        assert store.account_check("a1")["browser_status"] == "authenticated_form_visible"
        # A different dialog later is still the compromised-route signature.
        page.evaluate("document.querySelector('[id^=headlessui]').id='headlessui-dialog-panel-77'")
        sample_auth(pool)
        assert entry.portal_error_dialog is True
        assert store.account_check("a1")["browser_status"] == "portal_error_dialog_visible"
    finally:
        page.close()


def test_leftover_attributed_dialog_before_submission_is_closed_not_quarantined(chrome):
    page = chrome.new_page()

    def serve(route):
        if route.request.url.endswith("/texto"):
            route.fulfill(content_type="application/json", body="[]")
        else:
            route.fulfill(content_type="text/html; charset=utf-8",
                          body=FORM + modal(ERROR_MESSAGE, panel="headlessui-dialog-panel-5"))

    try:
        page.route("**/*", serve)
        page.goto("http://127.0.0.1:19996/protected")
        evidence = portal_dialog_evidence(page)
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url),
                                  attributed_dialog={**evidence, "cause": "captcha_rejected"})
        # Not the compromised-route verdict: the dialog is closed and the search goes on.
        assert search_fna_form(browser, 1, 2, 2000, client=RejectingClient(), pace=lambda _: None) == []
        assert portal_dialog_evidence(page) is None
        assert browser.attributed_dialog is None
    finally:
        page.close()


# --------------------------------------------------------------------------
# D33
# --------------------------------------------------------------------------


def test_cancelled_job_releases_its_idempotency_key(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    request = {"foja": 1, "numero": 2, "year": 2020}
    first, created = store.create_job(kind="fna", input_data=request, idempotency_key="get:1:2:2020")
    assert created
    assert store.request_cancel(first["job_id"])["status"] == "cancelled"
    again, created = store.create_job(kind="fna", input_data=request, idempotency_key="get:1:2:2020")
    assert created and again["job_id"] != first["job_id"] and again["status"] == "queued"
    assert store.get_job(first["job_id"])["idempotency_key"] is None
    # An open job keeps answering the same key.
    same, created = store.create_job(kind="fna", input_data=request, idempotency_key="get:1:2:2020")
    assert not created and same["job_id"] == again["job_id"]


def test_get_batch_row_recovers_after_its_job_was_cancelled(tmp_path):
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings = load_settings({}, root=tmp_path)
    backend.store = JobStore(tmp_path / "pool.sqlite3")
    backend.store.acquire_lease(WORKER_LEASE_NAME, "offline-test")
    client = Client(timeout=0)
    client._backend = backend
    cancelled = backend.submit((7, 8, 2021), False)
    backend.store.request_cancel(cancelled["job_id"])
    result = client.get(fojas=7, numero=8, ano=2021)
    assert result.status == "pending" and result.job_id != cancelled["job_id"]


# --------------------------------------------------------------------------
# D34
# --------------------------------------------------------------------------


def _runtime(tmp_path, accounts=("a1", "a2")):
    settings = load_settings({}, root=tmp_path)
    pool_config = PoolConfig(accounts=tuple(PoolAccount(a, a.upper()) for a in accounts),
                             daily_quota_per_account=20, interval_minutes=0,
                             dashboard_host="127.0.0.1", dashboard_port=0, targets=())
    store = JobStore(tmp_path / "pool.sqlite3")
    pool = AccountPoolStore(store.path)
    pool.create_run(run_id="jobs-test", dry_run=False, config=pool_config, dashboard_url=None)
    return settings, pool_config, store, pool


def test_login_cap_pause_counts_as_held_with_its_resume_time(tmp_path):
    from cbrs import form_search
    _, pool_config, store, pool = _runtime(tmp_path)
    record_quota_hold(store.path, "a1")
    assert runtime_logic._accounts_release(store, pool, "jobs-test", pool_config, form_search) is None
    pool.pause_account("jobs-test", "a2", reason="retry_allowance_exhausted", resume_at="2999-01-01T00:00:00+00:00")
    release = runtime_logic._accounts_release(store, pool, "jobs-test", pool_config, form_search)
    assert release == (quota_hold(store.path, "a1")["next_check_at"], "accounts_unavailable")
    # A pause whose end has passed is not a hold: the job must keep flowing.
    pool.pause_account("jobs-test", "a2", reason="retry_allowance_exhausted", resume_at="2000-01-01T00:00:00+00:00")
    assert runtime_logic._accounts_release(store, pool, "jobs-test", pool_config, form_search) is None
    # Quota holds alone keep the historical reason.
    record_quota_hold(store.path, "a2")
    assert runtime_logic._accounts_release(store, pool, "jobs-test", pool_config, form_search)[1] == "portal_quota_exhausted"
    assert runtime_logic._all_accounts_quota_held(store, pool, "jobs-test", pool_config, form_search) == min(
        quota_hold(store.path, a)["next_check_at"] for a in ("a1", "a2"))


def test_report_shows_pending_quota_with_resume_at_when_the_login_cap_holds_the_last_account(tmp_path, monkeypatch):
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings = load_settings({}, root=tmp_path)
    backend.store = JobStore(tmp_path / "pool.sqlite3")
    backend.store.acquire_lease(WORKER_LEASE_NAME, "offline-test")
    client = Client(timeout=0)
    client._backend = backend
    monkeypatch.setattr(backend, "status", lambda: {"accounts": [
        {"portal_quota": True, "status": "held", "resume_at": "2030-01-02T00:00:00+00:00"},
        {"portal_quota": False, "status": "paused", "resume_at": "2030-01-01T00:00:00+00:00",
         "error": "retry_allowance_exhausted"},
    ]})
    result = client.get(fojas=1, numero=2, ano=2020)
    assert result.status == "pending_quota" and result.resume_at == "2030-01-01T00:00:00+00:00"
    assert "retry_allowance_exhausted" in result.error
    with pytest.raises(QuotaExhausted):
        result.raise_for_status()
    # A paused account without a resume time is not a hold.
    monkeypatch.setattr(backend, "status", lambda: {"accounts": [
        {"portal_quota": True, "status": "held", "resume_at": "2030-01-02T00:00:00+00:00"},
        {"portal_quota": False, "status": "paused", "resume_at": None, "error": "temporary_unavailable"},
    ]})
    assert client.get(fojas=1, numero=2, ano=2020).status == "pending"


# --------------------------------------------------------------------------
# D35
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def _egress(monkeypatch, tmp_path, answers):
    settings = load_settings({"CBRS_PROXY_URL": "http://proxy.test:10001"}, root=tmp_path)
    seen, naps = [], []

    def open_fn(request, timeout):
        seen.append(request.full_url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    monkeypatch.setattr(preflight, "_proxy_opener", lambda url: SimpleNamespace(open=open_fn))
    return settings, seen, naps


def _http(code):
    return HTTPError("https://ipinfo.io/json", code, "error", hdrs=None, fp=None)


def test_ip_service_errors_retry_then_use_the_second_service(monkeypatch, tmp_path):
    settings, seen, naps = _egress(monkeypatch, tmp_path, [_http(503), _http(429), {"ip": "192.0.2.9", "country": "CL"}])
    data = preflight.fetch_public_egress(settings, sleep=naps.append)
    assert data == {"ip": "192.0.2.9", "country": "CL"}
    assert seen == [preflight.EGRESS_INFO_URL, preflight.EGRESS_INFO_URL, preflight.EGRESS_INFO_FALLBACK_URL]
    assert naps == [preflight.EGRESS_SERVICE_RETRY_SECONDS] * 2


def test_every_ip_service_failing_is_named_as_a_service_outage(monkeypatch, tmp_path):
    settings, _seen, naps = _egress(monkeypatch, tmp_path, [_http(503), _http(503), _http(500)])
    with pytest.raises(preflight.EgressServiceUnavailable) as caught:
        preflight.fetch_public_egress(settings, sleep=naps.append)
    assert "ipinfo.io HTTP 503" in str(caught.value) and "ipapi.co HTTP 500" in str(caught.value)
    assert naps == [preflight.EGRESS_SERVICE_RETRY_SECONDS] * 2


def test_proxy_transport_failure_is_still_immediate(monkeypatch, tmp_path):
    settings, seen, naps = _egress(monkeypatch, tmp_path, [URLError("tunnel connection failed"), {"ip": "x"}])
    with pytest.raises(URLError):
        preflight.fetch_public_egress(settings, sleep=naps.append)
    assert len(seen) == 1 and naps == []


# --------------------------------------------------------------------------
# Session keep-alive (opt-in) and the headless message
# --------------------------------------------------------------------------


def test_keepalive_is_off_by_default_and_paced_when_enabled(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    pool = _PersistentAccountBrowsers(scraper_factory=None, headless=False, store=store, worker_id="test")
    calls = []
    browser = SimpleNamespace(refresh_session_token=lambda: calls.append(1) or {"ok": True, "http_status": 200})
    entry = _ManagedAccountScraper(manager=None, scraper=SimpleNamespace(browser=browser), authenticated_once=True)

    pool._keepalive("a1", entry, load_settings({}, root=tmp_path))
    assert calls == [] and entry.last_keepalive_at == 0.0

    on = load_settings({"CBRS_SESSION_KEEPALIVE_SECONDS": "600"}, root=tmp_path)
    pool._keepalive("a1", entry, on)
    assert calls == [] and entry.last_keepalive_at > 0  # the login just issued a token
    pool._keepalive("a1", entry, on)
    assert calls == []  # not due yet
    entry.last_keepalive_at -= 601
    pool._keepalive("a1", entry, on)
    assert calls == [1]
    events = [e for e in store.recent_events(limit=10) if e["event"] == "session_keepalive"]
    assert events and json.loads(events[0]["data_json"])["ok"] is True
    # A page needing re-login is never "kept alive": that would be a login.
    entry.reauth_required = True
    entry.last_keepalive_at -= 601
    pool._keepalive("a1", entry, on)
    assert calls == [1]


def test_keepalive_setting_rejects_tight_intervals(tmp_path):
    with pytest.raises(ValueError):
        load_settings({"CBRS_SESSION_KEEPALIVE_SECONDS": "60"}, root=tmp_path)
    assert load_settings({"CBRS_SESSION_KEEPALIVE_SECONDS": "0"}, root=tmp_path).session_keepalive_seconds == 0


def test_refresh_session_token_renews_without_navigation(tmp_path):
    session = BrowserSession(load_settings({}, root=tmp_path))
    cookies = iter(["old-refresh", "new-refresh"])
    session._context = SimpleNamespace(cookies=lambda urls: [{"name": "cbrs_refresh_token", "value": next(cookies)}])
    requests, stored = [], []
    session.fetch_json = lambda path, **kwargs: requests.append(path) or BrowserFetchResponse(
        200, {}, body_text=json.dumps({"token": "fresh"}))
    session.set_auth_cookie = stored.append
    outcome = session.refresh_session_token()
    assert requests == ["/api/v1/auth/refresh"] and stored == ["fresh"]
    assert outcome == {"ok": True, "http_status": 200, "refresh_cookie_present": True, "refresh_cookie_rotated": True}

    session._context = SimpleNamespace(cookies=lambda urls: [])
    session.fetch_json = lambda path, **kwargs: BrowserFetchResponse(401, {}, body_text="{}")
    stored.clear()
    assert session.refresh_session_token()["ok"] is False and stored == []


def test_headless_refusal_names_the_supported_xvfb_command():
    assert "xvfb-run" in config.HEADLESS_UNSUPPORTED and "cbrs jobs worker" in config.HEADLESS_UNSUPPORTED
