"""End-to-end policy for the portal error dialog: a compromised proxy exit.

The operator proved the same account and the same search succeed from a clean
network while every pooled exit showed `Atención / Se ha detectado un problema,
refresque la página e intente nuevamente. / Cerrar`. That exact visible dialog
is therefore treated as evidence about the ROUTE, not the account: the browser
bound to it is logged out, closed and deleted, the exit is quarantined, and a
replacement exit must authenticate before the account is used again. Another
already authenticated account takes the search immediately; when every account
is quarantined the search waits for the first proven replacement instead of a
fixed multi-hour pause.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from cbrs import jobs
from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig
from cbrs.browser_session import CommerceAuthState
from cbrs.config import load_settings
from cbrs.form_search import PORTAL_ERROR_DIALOG, portal_dialog_evidence, search_fna_form
from cbrs.jobs import (
    JobStore,
    _ManagedAccountScraper,
    _PersistentAccountBrowsers,
    _handle_account_safety_stop,
    _recover_compromised_routes,
    is_portal_error_dialog,
    run_job_worker,
)
from cbrs.safety import SafetyStopException, StopReason

ERROR_MESSAGE = "Se ha detectado un problema, refresque la página e intente nuevamente."
QUOTA_MESSAGE = "Se han agotado las consultas disponibles por hoy."


def modal(message: str) -> str:
    return (
        '<div id="headlessui-dialog-panel-31" data-headlessui-state="open">'
        f"<h3>Atención</h3><p>{message}</p><button>Cerrar</button></div>"
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


@pytest.fixture(scope="module")
def chrome():
    # Separate ephemeral browser; never a production profile, port or context.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


def _dialog_stop(**evidence):
    stop = SafetyStopException(
        StopReason.TEMPORARY_UNAVAILABLE, "Portal error dialog", context="commerce form search"
    )
    stop.portal_dialog = {
        "reason": StopReason.TEMPORARY_UNAVAILABLE.value,
        "verdict": "proxy_compromised",
        **evidence,
    }
    return stop


def _settings(tmp_path, **env):
    return load_settings(
        {
            "CBRS_PROFILE_DIR": str(tmp_path / "state" / "chrome-profile"),
            "CBRS_OUTPUT_DIR": str(tmp_path / "outputs"),
            "CBRS_EGRESS_MODE": "mobile_sticky",
            "CBRS_EXPECTED_EGRESS_COUNTRY": "CL",
            "DATAIMPULSE_PROXY_LOGIN": "mobile-login",
            "DATAIMPULSE_PROXY_PASSWORD": "mobile-password",
            **env,
        },
        root=tmp_path,
    )


def _config(accounts: int = 3, *, quota: int = 20) -> PoolConfig:
    return PoolConfig(
        accounts=tuple(
            PoolAccount(
                f"a{index}",
                f"Account {index}",
                username_env=f"CBRS_TEST_USER_{index}",
                password_env=f"CBRS_TEST_PASSWORD_{index}",
                daily_quota=quota,
                proxy_provider="dataimpulse_mobile_sticky",
                dataimpulse_port=10000 + index,
            )
            for index in range(1, accounts + 1)
        ),
        daily_quota_per_account=quota,
        interval_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=0,
        targets=(),
    )


def _credentials(monkeypatch, config):
    for account in config.accounts:
        monkeypatch.setenv(str(account.username_env), f"{account.account_id}@example.test")
        monkeypatch.setenv(str(account.password_env), "secret-value")


def _gate(settings, **_kwargs):
    """Unique per-route egress so the pool's uniqueness gate stays meaningful."""
    port = str(settings.proxy_url or "direct").rsplit(":", 1)[-1].split("@")[0]
    return SimpleNamespace(
        ok=True,
        report_path=None,
        report={"egress_hash": f"egress-{port}", "egress_country": "CL", "checks": []},
    )


class DialogScraper:
    """Fake portal client whose configured accounts show the error dialog."""

    dialog_accounts: set[str] = set()
    results: list[dict] = []
    on_search = None

    def __init__(self, *, settings, headless=False):
        self.settings = settings
        self.account_id = str(settings.account_id or "unknown")
        self.browser = SimpleNamespace(
            settings=settings,
            page=None,
            detect_commerce_auth_state=lambda: CommerceAuthState.AUTHENTICATED_FORM,
            wait_for_commerce_auth_state=lambda: CommerceAuthState.AUTHENTICATED_FORM,
            preserve_for_service_lifetime=lambda: None,
            set_preview_callback=lambda _callback: None,
            shutdown_service_context=lambda: None,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def ensure_authenticated(self, username, password, *, force=False):
        assert username and password
        return "refreshed"

    def search_by_text(self, query):
        if self.account_id in type(self).dialog_accounts:
            raise _dialog_stop(after_submission=True)
        if type(self).on_search:
            type(self).on_search(f"search:{self.account_id}")
        return [dict(row) for row in type(self).results]

    def search_by_fna(self, foja, numero, ano):
        return self.search_by_text(f"{foja}-{numero}-{ano}")


# --------------------------------------------------------------------------
# Detection: the live DOM signature, in a real browser
# --------------------------------------------------------------------------


def test_open_error_dialog_stops_before_submission_without_reload(chrome):
    """An already visible dialog is not refreshed away and costs no search."""
    page = chrome.new_page()
    submissions, loads = [], []

    def serve(route):
        if route.request.url.endswith("/protected"):
            loads.append(1)
            route.fulfill(
                content_type="text/html; charset=utf-8",
                body=FORM + modal(ERROR_MESSAGE),
            )
        else:
            submissions.append(route.request.url)
            route.fulfill(content_type="application/json", body="{}")

    try:
        page.route("**/*", serve)
        page.goto("http://127.0.0.1:19997/protected")
        errors = []
        browser = SimpleNamespace(
            page=page,
            settings=SimpleNamespace(commerce_url=page.url),
            error_capture_callback=errors.append,
        )
        with pytest.raises(SafetyStopException) as caught:
            search_fna_form(browser, 9441, 4580, 1980, client=None, pace=lambda _: None)

        assert caught.value.reason is StopReason.TEMPORARY_UNAVAILABLE
        assert caught.value.portal_dialog["verdict"] == "proxy_compromised"
        assert caught.value.portal_dialog["after_submission"] is False
        assert caught.value.portal_dialog["heading"] == "Atención"
        assert caught.value.portal_dialog["close_button"] == "Cerrar"
        assert is_portal_error_dialog(caught.value)
        assert submissions == [] and loads == [1]
        assert errors[-1] is caught.value
        assert not page.is_closed()
    finally:
        page.close()


def test_error_dialog_after_one_rejection_is_never_replayed_or_reloaded(chrome):
    """One click, one rejection, one verdict: the exit is compromised."""
    page = chrome.new_page()
    submissions, loads = [], []

    def serve(route):
        if route.request.url.endswith("/protected"):
            loads.append(1)
            route.fulfill(content_type="text/html; charset=utf-8", body=FORM)
        elif route.request.url.endswith("/texto"):
            submissions.append(route.request.post_data_json)
            route.fulfill(
                status=400,
                content_type="application/json",
                body=json.dumps({"code": "intente-mas-tarde", "modal": modal(ERROR_MESSAGE)}),
            )
        else:
            route.fulfill(content_type="application/json", body="{}")

    try:
        page.route("**/*", serve)
        page.goto("http://127.0.0.1:19997/protected")
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
        with pytest.raises(SafetyStopException) as caught:
            search_fna_form(browser, 9441, 4580, 1980, client=None, pace=lambda _: None)

        assert caught.value.reason is StopReason.TEMPORARY_UNAVAILABLE
        assert caught.value.portal_dialog["after_submission"] is True
        assert caught.value.portal_dialog["verdict"] == "proxy_compromised"
        # Exactly one submission and no reload: no quota is spent twice and the
        # same query is never replayed on the compromised route.
        assert len(submissions) == 1 and loads == [1]
        assert portal_dialog_evidence(page)["reason"] == PORTAL_ERROR_DIALOG
        assert not page.is_closed()
    finally:
        page.close()


def test_daily_limit_dialog_is_still_a_quota_stop_not_a_compromised_route(chrome):
    """The quota message keeps its own meaning; only the error dialog quarantines."""
    page = chrome.new_page()
    try:
        page.set_content(FORM + modal(QUOTA_MESSAGE))
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
        with pytest.raises(SafetyStopException) as caught:
            search_fna_form(browser, 1, 2, 2000, client=None, pace=lambda _: None)
        assert caught.value.reason is StopReason.DAILY_LIMIT
        assert not is_portal_error_dialog(caught.value)
    finally:
        page.close()


def test_passive_sampler_flags_the_dialog_on_an_idle_page(tmp_path):
    """Detection is continuous: no search is needed to notice the dialog."""
    from cbrs.runtime_observation import sample_auth

    store = JobStore(tmp_path / "pool.sqlite3")
    settings = _settings(tmp_path)
    evaluations = []

    class Page:
        def evaluate(self, script):
            evaluations.append(script)
            return {
                "reason": PORTAL_ERROR_DIALOG,
                "panel_selector": '[role="dialog"]',
                "heading": "Atención",
                "close_button": "Cerrar",
                "message_element": "p",
            }

    browser = SimpleNamespace(
        page=Page(),
        detect_commerce_auth_state=lambda: CommerceAuthState.AUTHENTICATED_FORM,
    )
    pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=False, store=store, worker_id="test"
    )
    entry = _ManagedAccountScraper(manager=None, scraper=browser, settings=settings)
    pool._entries["a1"] = entry

    sample_auth(pool)

    assert entry.portal_error_dialog is True
    check = store.account_check("a1")
    assert check["browser_status"] == "portal_error_dialog_visible"
    # Observation only: the page was read, never reloaded, clicked or closed.
    assert all("reload" not in script and "click" not in script for script in evaluations)


# --------------------------------------------------------------------------
# Durable quarantine
# --------------------------------------------------------------------------


def test_quarantine_is_durable_and_only_a_promoted_exit_clears_it(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    store.ensure_dataimpulse_route("a1", 10001)

    assert store.compromised_routes() == []
    assert store.mark_route_compromised("a1", initial_port=10001) is True
    assert store.mark_route_compromised("a1", initial_port=10001) is False
    assert store.route_compromised("a1") and store.compromised_routes() == ["a1"]
    first_seen = store.dataimpulse_route("a1")["compromised_since"]

    # A failed candidate must never look like a recovered route.
    store.begin_dataimpulse_rotation(
        "a1", initial_port=10001, reason="portal_error_dialog_recovery",
        port_min=10000, port_max=20000, cooldown_seconds=0, max_rotations_per_hour=5,
    )
    store.finish_dataimpulse_rotation("a1", promoted=False, error_code="candidate_login_rejected")
    assert store.route_compromised("a1")
    assert store.dataimpulse_route("a1")["compromised_since"] == first_seen

    # Only adopting a proven replacement exit lifts the quarantine.
    store.begin_dataimpulse_rotation(
        "a1", initial_port=10001, reason="portal_error_dialog_recovery",
        port_min=10000, port_max=20000, cooldown_seconds=0, max_rotations_per_hour=5,
    )
    route = store.finish_dataimpulse_rotation("a1", promoted=True)
    assert route["status"] == "active" and not store.route_compromised("a1")
    assert store.compromised_routes() == []


def test_compromised_route_refuses_new_sessions_and_background_login(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    settings = _settings(tmp_path)
    logins = []
    scraper = SimpleNamespace(
        browser=SimpleNamespace(
            page=None,
            detect_commerce_auth_state=lambda: CommerceAuthState.LOGIN_GATE,
            preserve_for_service_lifetime=lambda: None,
            set_preview_callback=lambda _callback: None,
        ),
        ensure_authenticated=lambda *args, **kwargs: logins.append(args) or "refreshed",
    )
    pool = _PersistentAccountBrowsers(
        scraper_factory=lambda **_kwargs: pytest.fail("A compromised exit must never be launched"),
        headless=True, store=store, worker_id="test",
    )
    pool._entries["a1"] = _ManagedAccountScraper(
        manager=None, scraper=scraper, settings=settings, authenticated_once=True
    )
    pool._known_accounts["a1"] = (settings, "user", "secret")
    store.mark_route_compromised("a1", initial_port=10001)

    with pytest.raises(SafetyStopException) as caught:
        with pool.session("a1", settings, "user", "secret"):
            pytest.fail("A compromised exit must never be used")
    assert is_portal_error_dialog(caught.value)

    pool.reconcile()
    assert logins == []  # no re-login through the compromised exit


def test_ditching_logs_out_closes_every_context_and_deletes_profiles(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    events = []

    def build(name):
        directory = tmp_path / "profiles" / name
        (directory / "Default").mkdir(parents=True)
        (directory / "Default" / "Cookies").write_text("session-token", encoding="utf-8")
        page = SimpleNamespace(
            evaluate=lambda script: events.append(("evaluate", name, script)) or True,
            wait_for_timeout=lambda _ms: None,
        )
        browser = SimpleNamespace(
            _context=SimpleNamespace(clear_cookies=lambda: events.append(("cookies", name))),
            page=page,
            set_preview_callback=lambda _callback: None,
            shutdown_service_context=lambda: events.append(("closed", name)),
        )
        settings = replace(_settings(tmp_path), profile_dir=directory)
        return _ManagedAccountScraper(
            manager=SimpleNamespace(__exit__=lambda *args: events.append(("manager", name))),
            scraper=SimpleNamespace(browser=browser, close=lambda: None),
            settings=settings, authenticated_once=True,
        )

    pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    pool._entries["a1"] = build("current")
    pool._retained_entries.append(("a1", build("retained")))
    keep = build("other-account")
    pool._entries["a2"] = keep

    removed = pool.ditch_compromised("a1")

    assert sorted(Path(path).name for path in removed) == ["current", "retained"]
    assert not (tmp_path / "profiles" / "current").exists()
    assert not (tmp_path / "profiles" / "retained").exists()
    # Logout, cookie clear and close ran for both contexts of the bad exit.
    for name in ("current", "retained"):
        assert ("closed", name) in events and ("cookies", name) in events
        assert any(kind == "evaluate" and owner == name and "logout" in script
                   for kind, owner, script in (e for e in events if len(e) == 3))
    assert "a1" not in pool._entries and pool._retained_entries == []
    # A healthy sibling is untouched.
    assert pool._entries["a2"] is keep
    assert (tmp_path / "profiles" / "other-account").exists()
    assert store.account_check("a1")["browser_status"] == "proxy_compromised_discarded"
    assert any(event["event"] == "proxy_compromised_browser_discarded"
               for event in store.recent_events(limit=10))


def test_repeated_ditch_after_recovery_failure_is_quiet(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    assert pool.ditch_compromised("a1") == []
    assert store.recent_events(limit=10) == []


# --------------------------------------------------------------------------
# Recovery orchestration
# --------------------------------------------------------------------------


def _pool_run(tmp_path, config, *, started=True):
    """The worker creates its own run; direct policy tests need one up front."""
    store = JobStore(tmp_path / "pool.sqlite3")
    pool_store = AccountPoolStore(store.path)
    if started:
        pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    return store, pool_store


def _promoting_rotation(store, calls):
    def rotate(account, *args, **kwargs):
        calls.append(kwargs.get("reason") or "rotate")
        store.begin_dataimpulse_rotation(
            account.account_id, initial_port=account.dataimpulse_port, reason="test",
            port_min=10000, port_max=20000, cooldown_seconds=0, max_rotations_per_hour=5,
        )
        store.finish_dataimpulse_rotation(account.account_id, promoted=True)
        return True
    return rotate


def test_dialog_stop_quarantines_at_once_and_frees_the_job(tmp_path, monkeypatch):
    """Detection must not block the queue behind a candidate sweep."""
    settings, config = _settings(tmp_path), _config(accounts=1)
    store, pool_store = _pool_run(tmp_path, config)
    account = config.accounts[0]
    store.ensure_dataimpulse_route(account.account_id, account.dataimpulse_port)
    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    rotations = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", _promoting_rotation(store, rotations))
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})

    outcome = _handle_account_safety_stop(
        _dialog_stop(after_submission=True), job_id=job["job_id"], account=account, store=store,
        pool_store=pool_store, run_id="run", config=config, settings=settings,
        browser_pool=browser_pool, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    # The job is handed straight back to the pool for another account.
    assert outcome == "retry_account"
    assert rotations == []  # no Chrome work inline
    assert store.route_compromised(account.account_id)
    state = {row["account_id"]: row for row in pool_store.accounts("run")}[account.account_id]
    assert state["status"] == "paused" and state["paused_reason"] == jobs.PROXY_COMPROMISED_REASON
    assert state["resume_at"] is None  # released by recovery, never by a clock
    detected = [event for event in store.recent_events(limit=20)
                if event["event"] == "portal_error_dialog_detected"]
    assert detected and json.loads(detected[0]["data_json"])["verdict"] == "proxy_compromised"
    # A route problem never opens the shared portal-outage circuit.
    assert store.global_cooldown() is None

    # The periodic driver then ditches and replaces the exit.
    assert _recover_compromised_routes(
        settings=settings, config=config, store=store, pool_store=pool_store, run_id="run",
        browser_pool=browser_pool, preflight_runner=_gate, proxy_health_runner=_gate,
    ) == 1
    assert rotations == [jobs.PROXY_COMPROMISED_RECOVERY_REASON]
    assert not store.route_compromised(account.account_id)
    state = {row["account_id"]: row for row in pool_store.accounts("run")}[account.account_id]
    assert state["status"] == "available"
    assert any(event["event"] == "proxy_compromised_route_replaced"
               for event in store.recent_events(limit=20))


def test_failed_recovery_keeps_the_account_held_and_retries_without_a_long_wait(
    tmp_path, monkeypatch
):
    settings, config = _settings(tmp_path), _config(accounts=1)
    store, pool_store = _pool_run(tmp_path, config)
    account = config.accounts[0]
    store.ensure_dataimpulse_route(account.account_id, account.dataimpulse_port)
    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    attempts = []
    monkeypatch.setattr(
        jobs, "_rotate_dataimpulse_route",
        lambda *args, **kwargs: attempts.append(kwargs.get("reason")) or False,
    )
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})

    _handle_account_safety_stop(
        _dialog_stop(after_submission=True), job_id=job["job_id"], account=account, store=store,
        pool_store=pool_store, run_id="run", config=config, settings=settings,
        browser_pool=browser_pool, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    # Every later worker pass retries immediately instead of waiting hours.
    for _ in range(3):
        _recover_compromised_routes(
            settings=settings, config=config, store=store, pool_store=pool_store,
            run_id="run", browser_pool=browser_pool,
            preflight_runner=_gate, proxy_health_runner=_gate,
        )
    assert len(attempts) == 3
    assert store.route_compromised(account.account_id)
    state = {row["account_id"]: row for row in pool_store.accounts("run")}[account.account_id]
    assert state["status"] == "paused" and state["resume_at"] is None
    assert pool_store.reactivate_expired_cooldowns("run") == 0  # never released by a clock


def test_real_rotation_replaces_a_compromised_protected_session(tmp_path, monkeypatch):
    """The preservation rule yields only to proven-compromised exits."""
    settings, config = _settings(tmp_path), _config(accounts=1)
    store, pool_store = _pool_run(tmp_path, config)
    account = config.accounts[0]
    store.ensure_dataimpulse_route(account.account_id, account.dataimpulse_port)
    store.set_account_check(account.account_id, egress_hash="egress-old")
    baseline = tmp_path / "state" / "accounts" / "a1" / "fixed-egress-baseline.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        json.dumps({"schema": "cbrs-fixed-egress-baseline-v1", "egress_hash": "egress-old",
                    "egress_country": "CL"}),
        encoding="utf-8",
    )
    _credentials(monkeypatch, config)

    launched = []

    def factory(*, settings, headless=False):
        launched.append(settings.profile_dir)
        return DialogScraper(settings=settings, headless=headless)

    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=factory, headless=True, store=store, worker_id="test"
    )
    old_profile = tmp_path / "state" / "accounts" / "a1" / "chrome-profile"
    old_profile.mkdir(parents=True, exist_ok=True)
    closed = []
    browser_pool._entries["a1"] = _ManagedAccountScraper(
        manager=SimpleNamespace(__exit__=lambda *args: closed.append("manager")),
        scraper=SimpleNamespace(
            browser=SimpleNamespace(
                _context=SimpleNamespace(clear_cookies=lambda: None),
                page=SimpleNamespace(evaluate=lambda _s: True, wait_for_timeout=lambda _ms: None),
                set_preview_callback=lambda _callback: None,
                shutdown_service_context=lambda: closed.append("context"),
            ),
            close=lambda: None,
        ),
        settings=replace(settings, profile_dir=old_profile, account_id="a1"),
        username="a1@example.test", password="secret-value", authenticated_once=True,
    )
    store.mark_route_compromised("a1", initial_port=account.dataimpulse_port)

    recovered = jobs._recover_compromised_route(
        account, settings, store, pool_store, "run", browser_pool, _gate, _gate,
    )

    assert recovered is True
    assert closed == ["context", "manager"] and not old_profile.exists()
    assert not store.route_compromised("a1")
    route = store.dataimpulse_route("a1")
    assert route["status"] == "active" and int(route["active_port"]) != account.dataimpulse_port
    # The adopted browser is the exact candidate proven on the new exit.
    assert launched and str(int(route["active_port"])) in launched[-1].name
    assert browser_pool._entries["a1"].settings.profile_dir == launched[-1]
    assert store.account_check("a1")["egress_hash"] != "egress-old"


# --------------------------------------------------------------------------
# Whole-worker behavior
# --------------------------------------------------------------------------


def test_worker_serves_the_search_first_then_replaces_the_route(tmp_path, monkeypatch):
    """The healthy sibling runs the query before any candidate sweep starts."""
    settings, config = _settings(tmp_path), _config(accounts=3)
    _credentials(monkeypatch, config)
    store, pool_store = _pool_run(tmp_path, config, started=False)
    first, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    second, _ = store.create_job(kind="text", input_data={"text": "Authorized too"})
    order = []
    DialogScraper.dialog_accounts = {"a1"}
    DialogScraper.results = []
    DialogScraper.on_search = order.append
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", _promoting_rotation(store, order))

    run_job_worker(
        settings=settings, config=config, store=store, pool_store=pool_store, max_jobs=2,
        scraper_factory=DialogScraper, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    assert store.get_job(first["job_id"])["status"] == "completed"
    assert store.get_job(second["job_id"])["status"] == "completed"
    # The accepted search happened before the replacement exit was proven.
    assert order.index("search:a2") < order.index(jobs.PROXY_COMPROMISED_RECOVERY_REASON)
    with store.connect() as db:
        attempts = [
            (row["account_id"], row["status"], row["quota_consumed"])
            for row in db.execute(
                "SELECT account_id,status,quota_consumed FROM job_attempts "
                "WHERE job_id=? ORDER BY started_at, rowid", (first["job_id"],)
            ).fetchall()
        ]
    # The compromised account paid no quota; the healthy sibling ran the search.
    assert attempts[0][0] == "a1" and attempts[0][2] == 0
    assert attempts[-1][0] == "a2" and attempts[-1][1] == "search_completed"
    assert not store.route_compromised("a1")
    assert store.global_cooldown() is None
    assert any(event["event"] == "proxy_compromised_route_replaced"
               for event in store.recent_events(limit=40))


def test_every_account_compromised_pauses_the_search_and_keeps_recovering(
    tmp_path, monkeypatch
):
    settings, config = _settings(tmp_path), _config(accounts=3)
    _credentials(monkeypatch, config)
    store, pool_store = _pool_run(tmp_path, config, started=False)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    DialogScraper.dialog_accounts = {"a1", "a2", "a3"}
    DialogScraper.results = []
    DialogScraper.on_search = None
    attempts = []
    monkeypatch.setattr(
        jobs, "_rotate_dataimpulse_route",
        lambda account, *args, **kwargs: attempts.append(account.account_id) or False,
    )

    result = run_job_worker(
        settings=settings, config=config, store=store, pool_store=pool_store, once=True,
        scraper_factory=DialogScraper, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    assert result.status == "waiting_capacity"
    saved = store.get_job(job["job_id"])
    assert saved["status"] == "waiting_capacity" and saved["finished_at"] is None
    assert sorted(store.compromised_routes()) == ["a1", "a2", "a3"]
    states = {row["account_id"]: row for row in pool_store.accounts("run")}
    assert all(state["status"] == "paused" for state in states.values())
    assert all(state["paused_reason"] == jobs.PROXY_COMPROMISED_REASON for state in states.values())
    # No search was accepted and no quota was spent while every exit was bad.
    assert sum(store.usage_by_account(jobs.local_today()).values()) == 0
    # The shared "portal outage" backoff must not hide a proxy problem.
    assert store.global_cooldown() is None
    assert store.get_control("external_outage_backoff") is None

    # The paused pool keeps trying every quarantined exit, with no 24-hour wait.
    # One account per pass, taking turns, so a recovering pool never blocks the
    # shared browser owner for minutes at a time.
    run = pool_store.latest_run(dry_run=False)["run_id"]
    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    for _ in range(3):
        _recover_compromised_routes(
            settings=settings, config=config, store=store, pool_store=pool_store, run_id=run,
            browser_pool=browser_pool, preflight_runner=_gate, proxy_health_runner=_gate,
        )
    assert len(attempts) == 3
    assert sorted(attempts) == ["a1", "a2", "a3"]


def test_recovered_account_becomes_selectable_again(tmp_path, monkeypatch):
    settings, config = _settings(tmp_path), _config(accounts=1)
    _credentials(monkeypatch, config)
    store, pool_store = _pool_run(tmp_path, config)
    account = config.accounts[0]
    store.ensure_dataimpulse_route(account.account_id, account.dataimpulse_port)
    store.mark_route_compromised(account.account_id, initial_port=account.dataimpulse_port)
    pool_store.pause_account(
        "run", account.account_id, reason=jobs.PROXY_COMPROMISED_REASON, cooldown_seconds=None
    )
    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    monkeypatch.setattr(
        jobs, "_rotate_dataimpulse_route",
        lambda account, *args, **kwargs: bool(
            store.begin_dataimpulse_rotation(
                account.account_id, initial_port=account.dataimpulse_port, reason="test",
                port_min=10000, port_max=20000, cooldown_seconds=0, max_rotations_per_hour=5,
            ) and store.finish_dataimpulse_rotation(account.account_id, promoted=True)
        ),
    )

    assert store.select_account(
        run_id="run", config=config, quota_date=jobs.local_today()
    ) is None

    assert _recover_compromised_routes(
        settings=settings, config=config, store=store, pool_store=pool_store, run_id="run",
        browser_pool=browser_pool, preflight_runner=_gate, proxy_health_runner=_gate,
    ) == 1

    selected = store.select_account(
        run_id="run", config=config, quota_date=jobs.local_today()
    )
    assert selected is not None and selected.account_id == account.account_id


# --------------------------------------------------------------------------
# Independent browser owner
# --------------------------------------------------------------------------


def _owner(tmp_path, monkeypatch):
    from cbrs.browser_owner import BrowserOwner
    from cbrs.jobs import WORKER_LEASE_NAME

    settings = _settings(tmp_path)
    config = _config(accounts=1)
    store, pool_store = _pool_run(tmp_path, config)
    store.acquire_lease(WORKER_LEASE_NAME, "worker-one")
    _credentials(monkeypatch, config)
    monkeypatch.setattr("cbrs.jobs._runtime_account_settings", lambda *args, **kwargs: settings)
    return BrowserOwner(settings, config, store), store, pool_store


def test_owner_ditches_and_rotates_a_compromised_route_without_a_login_gate(
    tmp_path, monkeypatch
):
    owner, store, _pool_store = _owner(tmp_path, monkeypatch)
    ditched, rotations = [], []
    owner.pool.ditch_compromised = lambda account_id, **kwargs: ditched.append(account_id)
    monkeypatch.setattr(
        jobs, "_rotate_dataimpulse_route",
        lambda *args, **kwargs: rotations.append(kwargs.get("reason")) or True,
    )
    command = {
        "version": 1, "worker": "worker-one", "account": "a1",
        "operation": "recover_route",
        "payload": json.dumps({"reason": jobs.PROXY_COMPROMISED_RECOVERY_REASON}),
    }

    # Without the quarantine the owner still requires a visible rejected login.
    owner.pool._entries["a1"] = _ManagedAccountScraper(
        manager=None,
        scraper=SimpleNamespace(browser=SimpleNamespace(has_visible_rejected_login=lambda: False)),
        settings=owner.settings,
    )
    assert owner.execute(command) is False
    assert ditched == [] and rotations == []

    store.mark_route_compromised("a1", initial_port=10001)
    assert owner.execute(command) is True
    assert ditched == ["a1"]
    assert rotations == [jobs.PROXY_COMPROMISED_RECOVERY_REASON]


def test_owner_refuses_to_authenticate_through_a_compromised_route(tmp_path, monkeypatch):
    owner, store, _pool_store = _owner(tmp_path, monkeypatch)
    store.mark_route_compromised("a1", initial_port=10001)
    owner.pool.session = lambda *args, **kwargs: pytest.fail("Compromised exit must not be used")
    from cbrs.owner_protocol import binding_fingerprint

    command = {
        "version": 1, "worker": "worker-one", "account": "a1", "operation": "ensure",
        "payload": json.dumps(
            {"force": False, "binding": binding_fingerprint(owner.settings, owner.settings.headless)}
        ),
    }
    with pytest.raises(SafetyStopException) as caught:
        owner.execute(command)
    assert is_portal_error_dialog(caught.value)


def test_owner_quarantines_on_detection_and_tells_the_worker(tmp_path, monkeypatch):
    from cbrs.owner_protocol import OwnerCommands, RemoteScraper, OWNER_LEASE, command_path

    owner, store, _pool_store = _owner(tmp_path, monkeypatch)
    store.acquire_lease(OWNER_LEASE, owner.identity)
    owner.execute = lambda command: (_ for _ in ()).throw(_dialog_stop(after_submission=True))
    commands = OwnerCommands(command_path(owner.settings))
    identity = commands.submit("worker-one", "a1", "ensure", {"force": False})

    owner.tick()

    row = commands.read(identity)
    assert row["state"] == "failed"
    assert json.loads(row["error"])["route_compromised"] is True
    assert store.route_compromised("a1")
    assert any(event["event"] == "portal_error_dialog_detected"
               for event in store.recent_events(limit=10))

    # The worker rebuilds the same verdict from the durable reply.
    scraper = RemoteScraper(
        settings=replace(owner.settings, account_id="a1"), worker_id="worker-one",
        store_path=store.path, commands_path=command_path(owner.settings),
    )
    scraper.commands.submit = lambda *args, **kwargs: identity
    with pytest.raises(SafetyStopException) as caught:
        scraper.ensure_authenticated("user", "secret")
    assert is_portal_error_dialog(caught.value)


def test_recovery_failure_never_escapes_into_the_scheduler(tmp_path, monkeypatch):
    """A pending owner reply or provider error must not stall the worker."""
    settings, config = _settings(tmp_path), _config(accounts=2)
    _credentials(monkeypatch, config)
    store, pool_store = _pool_run(tmp_path, config, started=False)
    job, _ = store.create_job(kind="text", input_data={"text": "Authorized"})
    DialogScraper.dialog_accounts = {"a1"}
    DialogScraper.results = []

    def explode(*_args, **_kwargs):
        raise RuntimeError("Browser operation outcome pending; do not replay")

    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", explode)

    result = run_job_worker(
        settings=settings, config=config, store=store, pool_store=pool_store, once=True,
        scraper_factory=DialogScraper, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    # The search still succeeded on the healthy account, and the bad exit stays
    # quarantined for the next recovery pass.
    assert result.status == "completed"
    assert store.get_job(job["job_id"])["status"] == "completed"
    assert store.compromised_routes() == ["a1"]
    _recover_compromised_routes(
        settings=settings, config=config, store=store, pool_store=pool_store,
        run_id=pool_store.latest_run(dry_run=False)["run_id"],
        browser_pool=_PersistentAccountBrowsers(
            scraper_factory=None, headless=True, store=store, worker_id="test"),
        preflight_runner=_gate, proxy_health_runner=_gate,
    )
    assert store.compromised_routes() == ["a1"]
    pending = [
        event for event in store.recent_events(limit=20)
        if event["event"] == "proxy_compromised_recovery_pending"
    ]
    assert pending and "do not replay" in json.loads(pending[0]["data_json"])["error"]


def test_dialog_opening_mid_fill_is_attributed_to_the_route(chrome):
    """A blocked form plus the visible dialog is a compromised exit, not a miss."""
    page = chrome.new_page()
    submissions = []
    # Nothing is visible when the search starts; the dialog opens on the first
    # interaction and the React-controlled inputs then refuse the typed value,
    # exactly as the live portal behaves once its exit is rejected.
    blocked = FORM + """<script>
    document.querySelector('#input-fojas').addEventListener('focus', () => {
      if (document.querySelector('[role="dialog"]')) return;
      document.body.insertAdjacentHTML('beforeend', %s);
    }, {once: true});
    for (const id of ['#input-fojas', '#input-numero', '#input-ano']) {
      const field = document.querySelector(id);
      // A controlled React input re-renders with the old state while the
      // dialog owns the page, so the typed value never sticks.
      field.addEventListener('input', () => {
        if (document.querySelector('[role="dialog"], [id^=headlessui-dialog-panel-]')) field.value = '';
      });
    }
    </script>""" % json.dumps(modal(ERROR_MESSAGE))

    def serve(route):
        if route.request.url.endswith("/protected"):
            route.fulfill(content_type="text/html; charset=utf-8", body=blocked)
        else:
            submissions.append(route.request.url)
            route.fulfill(content_type="application/json", body="{}")

    try:
        page.route("**/*", serve)
        page.goto("http://127.0.0.1:19997/protected")
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
        # The search starts with a clean page: the pre-submission check passes.
        assert portal_dialog_evidence(page) is None
        with pytest.raises(SafetyStopException) as caught:
            search_fna_form(browser, 9441, 4580, 1980, client=None, pace=lambda _: None)

        assert caught.value.reason is StopReason.TEMPORARY_UNAVAILABLE
        assert is_portal_error_dialog(caught.value)
        assert caught.value.portal_dialog["after_submission"] is False
        assert submissions == []
    finally:
        page.close()


def test_quarantined_pool_does_not_advertise_a_daily_reset_countdown(tmp_path, monkeypatch):
    """Waiting on recovery must not look like waiting for tomorrow's quota."""
    settings, config = _settings(tmp_path), _config(accounts=2)
    _credentials(monkeypatch, config)
    store, pool_store = _pool_run(tmp_path, config, started=False)
    store.create_job(kind="text", input_data={"text": "Authorized"})
    DialogScraper.dialog_accounts = {"a1", "a2"}
    DialogScraper.results = []
    DialogScraper.on_search = None
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", lambda *args, **kwargs: False)

    published = []
    original = pool_store.update_run

    def record(run_id, **kwargs):
        published.append(kwargs)
        return original(run_id, **kwargs)

    pool_store.update_run = record

    run_job_worker(
        settings=settings, config=config, store=store, pool_store=pool_store, once=True,
        scraper_factory=DialogScraper, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    # Only the scheduler's own pause publishes a countdown; the shutdown
    # update at the end of the run carries no cycle time.
    waiting = [call for call in published
               if call.get("status") == "waiting_capacity" and "next_cycle_at" in call]
    assert waiting, published
    assert all(call["blocked_reason"] == jobs.PROXY_COMPROMISED_REASON for call in waiting)
    # No quota-reset timestamp: recovery is retried every minute, not tomorrow.
    assert all(not call["next_cycle_at"] for call in waiting)
    assert not pool_store.latest_run(dry_run=False)["next_cycle_at"]


def test_a_cooling_down_route_gives_its_turn_to_another_quarantined_account(
    tmp_path, monkeypatch
):
    """One pass must not be spent on an account that is still waiting anyway."""
    settings, config = _settings(tmp_path), _config(accounts=2)
    store, pool_store = _pool_run(tmp_path, config)
    for account in config.accounts:
        store.ensure_dataimpulse_route(account.account_id, account.dataimpulse_port)
        store.mark_route_compromised(account.account_id, initial_port=account.dataimpulse_port)
    # a1 is first in line but still serving its candidate retry delay.
    store.finish_dataimpulse_rotation(
        "a1", promoted=False, error_code="candidate_login_rejected", retry_seconds=600
    )
    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=True, store=store, worker_id="test"
    )
    attempts = []
    monkeypatch.setattr(
        jobs, "_rotate_dataimpulse_route",
        lambda account, *args, **kwargs: attempts.append(account.account_id) or False,
    )

    _recover_compromised_routes(
        settings=settings, config=config, store=store, pool_store=pool_store, run_id="run",
        browser_pool=browser_pool, preflight_runner=_gate, proxy_health_runner=_gate,
    )

    assert attempts == ["a2"]
    assert sorted(store.compromised_routes()) == ["a1", "a2"]
