"""Regression cases from Santiago's 2026-09-17 report and the live HTTP 404 document loop.

D9 bounded shutdown, D12 mode-aware start hint, D16/D17 quota holds and the
daily-limit dialog, D22/D25 portal history and the failure dialog, D23 pacing,
D24 recovery bursts, D26 reconciliation without the browser owner. No portal
traffic: real Chrome only renders local HTML.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from cbrs import api, jobs, runtime_logic
from cbrs.account_pool import DEFAULT_INTERVAL_MINUTES, AccountPoolStore, PoolAccount, PoolConfig
from cbrs.api import Client, _LocalBackend, service_start_hint
from cbrs.config import load_settings
from cbrs.form_search import (
    SEARCH_FAILED_DIALOG,
    UNKNOWN_DIALOG,
    portal_dialog_evidence,
    portal_history_lists,
    portal_recent_searches,
    quota_hold,
    record_quota_hold,
    search_fna_form,
)
from cbrs.jobs import DocumentUnavailable, JobStore, WORKER_LEASE_NAME, download_job_item
from cbrs.safety import SafetyStopException, StopReason
from cbrs.worker_lock import chrome_pids_using_profiles, terminate_profile_chrome


@pytest.fixture(scope="module")
def chrome():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


def modal(message, *, heading="Atención", close="Cerrar"):
    return (
        '<div id="headlessui-dialog-panel-7" data-headlessui-state="open">'
        f"<h3>{heading}</h3><p>{message}</p>"
        f"<button onclick=\"this.closest('[id^=headlessui]').remove()\">{close}</button></div>"
    )


# Mirrors the portal markup observed on 2026-09-20: header with "Borrar historial",
# chips with a label plus a screen-reader "Quitar ..." duplicate, and the footer note.
RECIENTES = (
    '<div id="recientes"><div><span>Recientes</span><button>Borrar historial</button></div>'
    '<div><div class="m3-chip-reciente"><button>Foja 30282 · N° 12784 · 2024</button>'
    '<button><span class="sr-only">Quitar Foja 30282 · N° 12784 · 2024 de las búsquedas recientes</span>x</button></div>'
    '<div class="m3-chip-reciente"><button>Foja 2297 · N° 1224 · 1988</button></div></div>'
    "<p>Se conservan las últimas 10 búsquedas.</p></div>"
)
RECIENTES_FIRMA = RECIENTES.replace('class="m3-chip-reciente"><button>Foja 30282', 'class="m3-chip-reciente" data-firma="fna|30282|12784|2024|"><button>Foja 30282').replace(
    'class="m3-chip-reciente"><button>Foja 2297', 'class="m3-chip-reciente" data-firma="fna|2297|1224|1988|"><button>Foja 2297')
FAIL_MESSAGE = "No se pudo realizar búsqueda, intente nuevamente por favor"


def form(*, on_failure="", extra=""):
    return f"""<section aria-label="Búsqueda por foja, número y año">
<input id="input-fojas"><input id="input-numero"><input id="input-ano">
<button onclick="submitSearch()">Buscar</button></section>
<div id="results"></div>{extra}<script>
async function submitSearch(){{
 const body={{foja:document.querySelector('#input-fojas').value,
 numero:document.querySelector('#input-numero').value,ano:document.querySelector('#input-ano').value}};
 try {{
   const res=await fetch('/api/v1/comercio/indice/texto',{{method:'POST',body:JSON.stringify(body)}});
   document.querySelector('#results').innerHTML=(await res.json()).modal||'';
 }} catch(e) {{ document.querySelector('#results').innerHTML={json.dumps(on_failure)}; }}
}}
</script>"""


def _browser(page, tmp_path=None, **settings):
    browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url, **settings))
    if tmp_path is not None:
        browser.settings.account_id = "a1"
        browser.quota_store_path = str(tmp_path / "pool.sqlite3")
        JobStore(tmp_path / "pool.sqlite3")
    return browser


def _serve(page, body, *, on_search):
    def serve(route):
        if route.request.url.endswith("/protected"):
            route.fulfill(content_type="text/html; charset=utf-8", body=body)
        elif route.request.url.endswith("/texto"):
            on_search(route)
        else:
            route.fulfill(content_type="application/json", body="{}")
    page.route("**/*", serve)
    page.goto("http://127.0.0.1:19996/protected")


# ---------------------------------------------------------------- D17 / D25 dialogs

@pytest.mark.parametrize("html,expected", [
    (modal("Se han agotado las consultas disponibles por hoy"), "daily_limit"),
    (modal("Ha alcanzado el límite diario de consultas.", heading="Aviso", close="Aceptar"), "daily_limit"),
    (modal(FAIL_MESSAGE), SEARCH_FAILED_DIALOG),
    (modal("Mantenimiento programado hasta las 06:00."), UNKNOWN_DIALOG),
    (modal("Se ha detectado un problema, refresque la página e intente nuevamente."), "temporary_unavailable"),
    (modal("Se ha detectado un problema, refresque la página e intente nuevamente.", heading="Aviso"), UNKNOWN_DIALOG),
])
def test_dialog_matcher_tolerates_variants_and_reports_unknown_modals(chrome, html, expected):
    page = chrome.new_page()
    try:
        page.set_content(form() + html)
        evidence = portal_dialog_evidence(page)
        assert evidence["reason"] == expected
        assert evidence["heading"] and evidence["close_button"] and evidence["message"]
    finally:
        page.close()


def test_daily_limit_dialog_blocking_the_form_holds_the_account_without_a_search(chrome, tmp_path):
    """D17: the modal on the 11th attempt used to become 'form could not submit'."""
    page = chrome.new_page()
    submissions = []
    try:
        _serve(page, form() + modal("Se han agotado las consultas disponibles por hoy", heading="Aviso"),
               on_search=lambda route: submissions.append(1) or route.fulfill(body="{}"))
        browser = _browser(page, tmp_path)
        with pytest.raises(SafetyStopException) as caught:
            search_fna_form(browser, 1, 2, 2000, client=None, pace=lambda _: None)
        assert caught.value.reason is StopReason.DAILY_LIMIT
        assert quota_hold(browser.quota_store_path, "a1")["blocked"] is True
        assert submissions == []
    finally:
        page.close()


def test_failed_search_dialog_after_click_is_retry_safe_and_dismissed(chrome, tmp_path):
    """D25: the portal's own failure modal plus an absent history entry means no quota was spent."""
    page = chrome.new_page()
    try:
        _serve(page, form(on_failure=modal(FAIL_MESSAGE), extra=RECIENTES), on_search=lambda route: route.abort())
        browser = _browser(page, tmp_path)
        with pytest.raises(SafetyStopException) as caught:
            search_fna_form(browser, 9441, 4580, 1980, client=None, pace=lambda _: None)
        assert caught.value.reason is StopReason.SEARCH_NOT_SUBMITTED
        assert caught.value.portal_history is False
        assert caught.value.portal_dialog["reason"] == SEARCH_FAILED_DIALOG
        assert caught.value.request_dispatched is True
        assert portal_dialog_evidence(page) is None  # dismissed, route usable again
        events = [e for e in JobStore(tmp_path / "pool.sqlite3").recent_events() if e["event"] == "search_not_registered"]
        assert events and json.loads(events[0]["data_json"])["portal_history_listed"] is False
    finally:
        page.close()


@pytest.mark.parametrize("panel,listed", [(RECIENTES, False), (RECIENTES.replace("2297 · N° 1224 · 1988", "9441 · N° 4580 · 1980"), True), ("", None)])
def test_unobserved_outcome_is_decided_by_the_portal_history(chrome, tmp_path, panel, listed):
    """D22: absent from Recientes -> retry-safe; listed or unreadable -> explicit reconciliation."""
    page = chrome.new_page()
    try:
        _serve(page, form(extra=panel), on_search=lambda route: None)  # the request hangs
        browser = _browser(page, tmp_path, search_submission_timeout_seconds=1.5,
                           search_response_timeout_seconds=1.5)
        with pytest.raises(Exception) as caught:
            search_fna_form(browser, 9441, 4580, 1980, client=None, pace=lambda _: None)
        if listed is False:
            assert isinstance(caught.value, SafetyStopException)
            assert caught.value.reason is StopReason.SEARCH_NOT_SUBMITTED
            assert caught.value.portal_history is False
        else:
            assert not isinstance(caught.value, SafetyStopException)
            assert caught.value.portal_history is listed
            names = [e["event"] for e in JobStore(tmp_path / "pool.sqlite3").recent_events()]
            assert "search_outcome_unknown_history" in names
    finally:
        page.close()


def test_recientes_panel_parsing(chrome):
    page = chrome.new_page()
    try:
        page.set_content(form(extra=RECIENTES))
        assert portal_recent_searches(page) == [(30282, 12784, 2024), (2297, 1224, 1988)]  # deduplicated
        page.set_content(form(extra=RECIENTES_FIRMA))
        assert portal_recent_searches(page) == [(30282, 12784, 2024), (2297, 1224, 1988)]  # data-firma path
        assert portal_history_lists(page, "2297", "1224", "1988") is True
        assert portal_history_lists(page, 1, 2, 2000) is False
        page.set_content(form())
        assert portal_recent_searches(page) is None
        assert portal_history_lists(page, 1, 2, 2000) is None
    finally:
        page.close()


# ------------------------------------------------------------ D16 / D17 scheduling

def runtime(tmp_path, accounts=("a1", "a2")):
    settings = load_settings({}, root=tmp_path)
    config = PoolConfig(accounts=tuple(PoolAccount(a, a.upper()) for a in accounts),
                        daily_quota_per_account=20, interval_minutes=0,
                        dashboard_host="127.0.0.1", dashboard_port=0, targets=())
    store = JobStore(tmp_path / "pool.sqlite3")
    pool = AccountPoolStore(store.path)
    pool.create_run(run_id="jobs-test", dry_run=False, config=config, dashboard_url=None)
    return settings, config, store, pool


def test_set_waiting_cadence_by_reason(tmp_path):
    _, _, store, _ = runtime(tmp_path)
    job, _ = store.create_job(kind="fna", input_data={"foja": 1, "numero": 2, "year": 2000})
    store.set_waiting(job["job_id"], "waiting_capacity", reason="search_reconciliation_required")
    delay = _seconds_until(store.get_job(job["job_id"])["next_run_at"])
    assert 590 <= delay <= 600
    store.set_waiting(job["job_id"], "waiting_capacity", reason="waiting_authenticated_alternate")
    assert 50 <= _seconds_until(store.get_job(job["job_id"])["next_run_at"]) <= 60
    store.set_waiting(job["job_id"], "waiting_capacity", reason="portal_quota_exhausted", next_run_at="2999-01-01T00:00:00+00:00")
    assert store.get_job(job["job_id"])["next_run_at"] == "2999-01-01T00:00:00+00:00"
    assert store.claim_next("w") is None


def _seconds_until(stamp):
    from datetime import datetime, timezone
    return (datetime.fromisoformat(stamp) - datetime.now(timezone.utc)).total_seconds()


def test_all_usable_accounts_held_returns_earliest_release(tmp_path):
    from cbrs import form_search
    _, config, store, pool = runtime(tmp_path)
    assert runtime_logic._all_accounts_quota_held(store, pool, "jobs-test", config, form_search) is None
    record_quota_hold(store.path, "a1")
    assert runtime_logic._all_accounts_quota_held(store, pool, "jobs-test", config, form_search) is None
    pool.pause_account("jobs-test", "a2", reason="credentials_invalid", cooldown_seconds=None)
    release = runtime_logic._all_accounts_quota_held(store, pool, "jobs-test", config, form_search)
    assert release == quota_hold(store.path, "a1")["next_check_at"]
    pool.mark_account_available("jobs-test", "a2")
    record_quota_hold(store.path, "a2")
    both = runtime_logic._all_accounts_quota_held(store, pool, "jobs-test", config, form_search)
    assert both == min(quota_hold(store.path, a)["next_check_at"] for a in ("a1", "a2"))


def _client(tmp_path, config, store, monkeypatch, settings):
    import cbrs.account_pool
    monkeypatch.setattr(cbrs.account_pool, "load_account_pool_config", lambda settings: config)
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings, backend.store = settings, store
    backend.export_dir = tmp_path / "export"
    store.acquire_lease(WORKER_LEASE_NAME, "offline-test")
    client = Client(timeout=0)
    client._backend = backend
    return client


def test_report_shows_pending_quota_when_the_only_usable_accounts_are_held(tmp_path, monkeypatch):
    """D16: a disabled account must not turn an all-held pool into a generic pending row."""
    settings, config, store, pool = runtime(tmp_path)
    client = _client(tmp_path, config, store, monkeypatch, settings)
    result = client.get(fojas=1, numero=2, ano=2000)
    assert result.status == "pending"
    record_quota_hold(store.path, "a1")
    pool.pause_account("jobs-test", "a2", reason="credentials_invalid", cooldown_seconds=None)
    result = client.job(result.job_id)
    assert result.status == "pending_quota"
    assert result.resume_at == quota_hold(store.path, "a1")["next_check_at"]
    with pytest.raises(api.QuotaExhausted):
        result.raise_for_status()
    # The worker's own verdict is honoured even before the status view catches up.
    pool.mark_account_available("jobs-test", "a2")
    store.set_waiting(result.job_id, "waiting_capacity", reason="portal_quota_exhausted", next_run_at="2999-01-01T00:00:00+00:00")
    assert client.job(result.job_id).status == "pending_quota"


def test_reconciliation_has_its_own_status_and_exact_command(tmp_path, monkeypatch):
    """D22/D26: the report names the state, the command works without the browser owner."""
    settings, config, store, pool = runtime(tmp_path)
    client = _client(tmp_path, config, store, monkeypatch, settings)
    job_id = client.get(fojas=29929, numero=13757, ano=2022).job_id
    store.set_waiting(job_id, "waiting_capacity", reason="search_reconciliation_required")
    result = client.job(job_id)
    assert result.status == "pending_reconciliation"
    assert f"cbrs jobs reconcile {job_id} --apply" in result.error
    assert "Recientes" in result.error
    preview = store.reconcile_unconfirmed(job_id, apply=False, owner_commands_path=tmp_path / "no-owner" / "commands.sqlite3")
    assert preview == {"job_id": job_id, "eligible": True, "prior_status": "waiting_capacity",
                       "prior_reason": "search_reconciliation_required", "applied": False}
    applied = store.reconcile_unconfirmed(job_id, apply=True, owner_commands_path=tmp_path / "no-owner" / "commands.sqlite3")
    assert applied["applied"] is True
    assert store.get_job(job_id)["status"] == "queued"
    assert client.job(job_id).status == "pending"
    with pytest.raises(ValueError):
        store.reconcile_unconfirmed(job_id, apply=True)


# ------------------------------------------------------------- live 404 document loop

class _GoneScraper:
    def __init__(self, status=404):
        self.refs_calls = 0
        self.status = status

    def get_image_refs(self, ticket):
        self.refs_calls += 1
        return {"ticket": ticket}, [{"pageNumber": 1, "dataRef": "ref-1"}]

    def download_image(self, ref, path):
        raise SafetyStopException(StopReason.UNEXPECTED_STATUS, "image download stopped",
                                  status=self.status, context="image download")


def test_document_404_revalidates_cached_refs_once_then_fails_terminally(tmp_path):
    item = {"item_id": "item-1", "sequence": 1, "ticket_ref": "t-1", "result": {"foja": 1, "numero": 2, "ano": 2000}}
    staging = tmp_path / "jobs" / "job-1" / ".staging" / "item-1"
    staging.mkdir(parents=True)
    (staging / "manifest.json").write_text(json.dumps({"ticket": "t-1", "sample_pages": None,
                                                        "refs": [{"pageNumber": 1, "dataRef": "stale"}]}))
    scraper = _GoneScraper()
    with pytest.raises(DocumentUnavailable) as caught:
        download_job_item(scraper, item, job_id="job-1", output_root=tmp_path)
    assert scraper.refs_calls == 1  # cached refs were re-validated exactly once
    assert "--force" in str(caught.value)
    fresh = _GoneScraper()
    (staging / "manifest.json").unlink()
    with pytest.raises(DocumentUnavailable):
        download_job_item(fresh, item, job_id="job-1", output_root=tmp_path)
    assert fresh.refs_calls == 1
    other = _GoneScraper(status=500)
    with pytest.raises(SafetyStopException) as unchanged:
        download_job_item(other, item, job_id="job-1", output_root=tmp_path)
    assert unchanged.value.status == 500

    class _TicketGone(_GoneScraper):
        def get_image_refs(self, ticket):
            self.refs_calls += 1
            raise SafetyStopException(StopReason.UNEXPECTED_STATUS, "ticket validation stopped",
                                      status=404, context="ticket validation")
    (staging / "manifest.json").unlink(missing_ok=True)
    with pytest.raises(DocumentUnavailable):
        download_job_item(_TicketGone(), item, job_id="job-1", output_root=tmp_path)

    class _RelayedByOwner(_GoneScraper):
        def download_image(self, ref, path):
            # The independent owner relays the stop under its own context label.
            raise SafetyStopException(StopReason.UNEXPECTED_STATUS, "Browser owner reported a portal failure",
                                      status=404, context="browser owner")
    (staging / "manifest.json").unlink(missing_ok=True)
    with pytest.raises(DocumentUnavailable):
        download_job_item(_RelayedByOwner(), item, job_id="job-1", output_root=tmp_path)


def test_document_unavailable_job_is_terminal_and_not_requeued(tmp_path, monkeypatch):
    settings, config, store, _ = runtime(tmp_path)
    client = _client(tmp_path, config, store, monkeypatch, settings)
    job_id = client.get(fojas=1, numero=2, ano=2000).job_id
    store.add_results(job_id, [{"foja": 1, "num": 2, "ano": 2000, "ticket": "t"}])
    item = store.get_job(job_id)["items"][0]
    store.fail_item(item["item_id"], code="document_unavailable", message="HTTP 404")
    assert store.finalize_job(job_id) == "failed"
    store.fail_job(job_id, code="document_unavailable", message="Repita con --force")
    result = client.get(fojas=1, numero=2, ano=2000)
    assert result.status == "failed" and result.job_id == job_id and "--force" in result.error
    assert store.get_job(job_id)["status"] == "failed"  # no automatic document requeue
    assert client.get(fojas=1, numero=2, ano=2000, force=True).job_id != job_id


# ----------------------------------------------------------------------- D24 pacing

def test_compromised_recovery_bursts_only_while_the_queue_is_idle(tmp_path, monkeypatch):
    settings, config, store, pool = runtime(tmp_path, accounts=("a1", "a2"))
    settings = load_settings({"CBRS_DATAIMPULSE_CANDIDATES_PER_RECOVERY": "10"}, root=tmp_path)
    store.mark_route_compromised("a1", initial_port=10001)
    seen = []
    monkeypatch.setattr(jobs, "_recover_compromised_route",
                        lambda *args, **kwargs: seen.append(kwargs.get("candidates_per_pass")) or False)
    call = lambda: jobs._recover_compromised_routes(settings=settings, config=config, store=store, pool_store=pool,
                                                    run_id="jobs-test", browser_pool=None,
                                                    preflight_runner=None, proxy_health_runner=None)
    call()
    assert seen == [10]  # nothing queued: whole batch back to back
    store.create_job(kind="fna", input_data={"foja": 1, "numero": 2, "year": 2000})
    call()
    assert seen[-1] == 1  # a healthy available account has work waiting
    pool.pause_account("jobs-test", "a2", reason="daily_limit", cooldown_seconds=None)
    call()
    assert seen[-1] == 10  # queued work cannot run anyway


def test_interval_default_is_zero_for_env_only_installs():
    assert DEFAULT_INTERVAL_MINUTES == 0.0


# ------------------------------------------------------------------------ D9 shutdown

def test_sigterm_takes_the_keyboard_interrupt_path():
    before = signal.getsignal(signal.SIGTERM)
    previous = jobs._install_terminate_handler()
    try:
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        jobs._restore_terminate_handler(previous)
    assert signal.getsignal(signal.SIGTERM) == before


def test_interrupt_inside_a_greenlet_is_raised_in_its_caller():
    # D31: Ctrl+C during a Playwright call runs the handler inside the sync
    # dispatcher greenlet. It must stay alive: the interrupt belongs to the
    # caller, and later calls (the shutdown close) must still reach it.
    from greenlet import greenlet
    trace = []

    def dispatcher():
        jobs._interrupt(signal.SIGINT, None)
        trace.append("dispatcher resumed")
        return "loop still usable"

    fiber = greenlet(dispatcher)
    with pytest.raises(KeyboardInterrupt):
        fiber.switch()
    assert not fiber.dead
    assert fiber.switch() == "loop still usable"
    assert trace == ["dispatcher resumed"]


def test_shutdown_watchdog_bounds_a_stalled_close_in_embedded_mode_only(tmp_path, monkeypatch):
    import cbrs.worker_lock
    calls = []
    monkeypatch.setattr(cbrs.worker_lock, "terminate_profile_chrome", lambda accounts_dir, **kw: calls.append(str(accounts_dir)) or 0)
    settings = load_settings({}, root=tmp_path)
    pool = SimpleNamespace(close_all=lambda **kwargs: time.sleep(0.6))
    jobs._shutdown_browser_pool(pool, settings, "embedded", deadline=0.1)
    assert calls == [str(settings.profile_dir.parent / "accounts")] * 2  # watchdog fired, then final sweep
    calls.clear()
    jobs._shutdown_browser_pool(pool, settings, "external", deadline=0.1)
    assert calls == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc scan is Linux-only")
def test_terminate_profile_chrome_stops_only_processes_using_our_profiles(tmp_path):
    accounts = tmp_path / "accounts"
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             f"--user-data-dir={accounts}/a1/chrome-profile"])
    theirs = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               f"--user-data-dir={tmp_path}/elsewhere/chrome-profile"])
    try:
        time.sleep(0.3)
        assert ours.pid in chrome_pids_using_profiles(accounts)
        assert theirs.pid not in chrome_pids_using_profiles(accounts)
        assert terminate_profile_chrome(accounts, grace=3) >= 1
        assert ours.wait(timeout=5) is not None
        assert theirs.poll() is None
    finally:
        for proc in (ours, theirs):
            proc.kill()
            proc.wait(timeout=5)


# ------------------------------------------------------------------------ D12 hint

def test_service_start_hint_matches_the_installation_mode(tmp_path, monkeypatch):
    unit = tmp_path / "cbrs-worker.service"
    monkeypatch.setattr(api, "SYSTEMD_UNIT", unit)
    assert service_start_hint() == "cbrs jobs worker (en otra terminal)"
    unit.write_text("[Unit]")
    assert service_start_hint() == "cbrs service start worker"
