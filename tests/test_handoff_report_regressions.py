"""Regression cases from Santiago's 2026-09-16 report, with no portal traffic."""
import json
import logging
import socket
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cbrs import jobs
from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig
from cbrs.api import _LocalBackend
from cbrs.browser_session import BrowserFetchResponse, BrowserSession, CredentialsRejectedError
from cbrs.config import load_settings
from cbrs.preflight import baseline_file, run_preflight
from cbrs.safety import SafetyStopException, StopReason
from cbrs.worker_lock import exclusive_worker
from cbrs.worker_lock import local_owner_is_dead


@pytest.mark.parametrize("status", [400, 401, 422])
def test_explicit_captcha_code_never_becomes_invalid_credentials(status):
    with pytest.raises(SafetyStopException) as exc:
        BrowserSession._check_login_response(BrowserFetchResponse(
            status, {}, body_text=json.dumps({"code": "captcha-rechazado"})))
    assert exc.value.reason == StopReason.CAPTCHA_REJECTED
    assert exc.value.response_code == "captcha-rechazado"


def test_auth_exception_is_terminal_and_not_captcha():
    with pytest.raises(CredentialsRejectedError) as exc:
        BrowserSession._check_login_response(BrowserFetchResponse(
            401, {}, body_text='{"code":"auth-exception"}'))
    assert exc.value.response_code == "auth-exception"


@pytest.mark.parametrize("mode,expected", [("browser", False), ("2captcha", True),
                                           ("2captcha_fallback", True), ("capsolver", True)])
def test_form_rejection_uses_configured_solver_once(tmp_path, mode, expected):
    settings = load_settings({"CBRS_CAPTCHA_SOLVER_MODE": mode,
                              "CBRS_2CAPTCHA_API_KEY": "test-only",
                              "CBRS_CAPSOLVER_API_KEY": "test-only"}, root=tmp_path)
    session = BrowserSession(settings)
    session.open = lambda: session
    checks = iter([False, True])
    session.has_active_login = lambda: next(checks)
    def form(*args):
        raise SafetyStopException(StopReason.CAPTCHA_REJECTED, "rejected", context="auth login")
    session._login_with_form = form
    calls = []
    session._login_with_fetch = lambda *args, **kwargs: calls.append(kwargs)
    if expected:
        assert session.ensure_authenticated("test", "test") == "browser_fetch"
        assert calls == [{"external_only": True}]
    else:
        with pytest.raises(SafetyStopException):
            session.ensure_authenticated("test", "test")
        assert not calls


def test_mobile_baseline_initializes_only_after_country_validation(tmp_path):
    browser = tmp_path / "chrome.exe"
    browser.touch()
    settings = load_settings({"CBRS_BROWSER_EXECUTABLE_PATH": str(browser),
                              "CBRS_EGRESS_MODE": "mobile_sticky",
                              "CBRS_PROXY_URL": "http://proxy.test:10001"}, root=tmp_path)
    def check(ip, country):
        return run_preflight(settings, fetch_egress=lambda: {"ip": ip, "country": country}, write_report=False)
    assert not check("192.0.2.1", "US").ok
    assert not baseline_file(settings).exists()
    assert check("192.0.2.1", "CL").ok
    assert baseline_file(settings).exists()
    assert not check("192.0.2.2", "CL").ok
    assert "192.0.2.1" not in baseline_file(settings).read_text()


def runtime(tmp_path):
    settings = load_settings({}, root=tmp_path)
    config = PoolConfig(accounts=(PoolAccount("a1", "Account 1"),),
                        daily_quota_per_account=20, interval_minutes=0,
                        dashboard_host="127.0.0.1", dashboard_port=0, targets=())
    store = jobs.JobStore(tmp_path / "pool.sqlite3")
    pool = AccountPoolStore(store.path)
    pool.create_run(run_id="jobs-test", dry_run=False, config=config, dashboard_url=None)
    return settings, config, store, pool


def test_idle_retries_failed_gate_only_after_cooldown(tmp_path, monkeypatch):
    settings, config, store, pool = runtime(tmp_path)
    store.set_account_check("a1", proxy_status="failed")
    pool.pause_account("jobs-test", "a1", reason="egress_preflight_failed")
    browsers = SimpleNamespace(_entries={}, has_protected_session=lambda account: False)
    calls = []
    monkeypatch.setattr(jobs, "_runtime_account_settings", lambda *args: settings)
    monkeypatch.setattr(jobs, "_ensure_account_gate", lambda *args, **kwargs: calls.append(args[0].account_id) or False)
    run = lambda: jobs._run_startup_gates(settings, config, store, pool, "jobs-test",
                                        None, None, browsers, retry_only=True)
    run()
    assert not calls
    pool.mark_account_available("jobs-test", "a1")
    run()
    assert calls == ["a1"]
    browsers._entries["a1"] = object()
    run()
    assert calls == ["a1"]  # no reopening an existing Chrome


@pytest.mark.parametrize("reason,expected", [("captcha_rejected", "captcha_pending"),
                                             ("credentials_invalid", "disabled"),
                                             ("egress_preflight_failed", "paused")])
def test_status_exposes_worker_reason_and_deadline(tmp_path, monkeypatch, reason, expected):
    settings, config, store, pool = runtime(tmp_path)
    import cbrs.account_pool
    monkeypatch.setattr(cbrs.account_pool, "load_account_pool_config", lambda settings: config)
    if reason == "captcha_rejected":
        pool.mark_account_captcha_pending("jobs-test", "a1", reason=reason, cooldown_seconds=120)
    else:
        pool.pause_account("jobs-test", "a1", reason=reason,
                           cooldown_seconds=None if reason == "credentials_invalid" else 120)
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings, backend.store = settings, store
    account = backend.status()["accounts"][0]
    assert account["status"] == expected
    assert account["error"] == reason
    assert bool(account["resume_at"]) == (reason != "credentials_invalid")


def test_event_log_omits_payloads_and_freeform_reason(tmp_path, caplog):
    _, _, store, _ = runtime(tmp_path)
    with caplog.at_level(logging.INFO, logger="cbrs.jobs"):
        store.add_event("account_gate_failed", account_id="a1", data={
            "reason": "private user info", "password": "private-secret", "body": "private-body"})
    assert "account_gate_failed" in caplog.text
    assert "private" not in caplog.text


def test_worker_process_lock_excludes_second_entry_and_releases(tmp_path):
    settings, _, store, _ = runtime(tmp_path)
    from cbrs.owner_lock import OwnerLock
    calls = []
    @exclusive_worker
    def worker(**kwargs):
        calls.append(True)
    with OwnerLock(store.path.parent / "worker.lock"):
        with pytest.raises(RuntimeError, match="job worker"):
            worker(settings=settings, store=store)
    worker(settings=settings, store=store)
    assert calls == [True]


@pytest.mark.parametrize("protected,scoped,visible,expected", [
    (False, True, True, True), (True, True, True, False),
    (False, False, True, False), (False, True, False, False),
])
def test_captcha_candidate_recovery_requires_all_preservation_gates(
    tmp_path, monkeypatch, protected, scoped, visible, expected,
):
    settings, config, store, pool = runtime(tmp_path)
    account = replace(config.accounts[0], proxy_provider="dataimpulse_mobile_sticky", dataimpulse_port=10001)
    monkeypatch.setenv("CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS", "a1" if scoped else "")
    browsers = SimpleNamespace(can_replace_rejected_login=lambda _: visible,
                               has_protected_session=lambda _: protected)
    calls = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", lambda *a, **k: calls.append(k) or True)
    jobs._handle_account_safety_stop(
        SafetyStopException(StopReason.CAPTCHA_REJECTED, "rejected", status=400, context="auth login"),
        job_id=None, account=account, store=store, pool_store=pool, run_id="jobs-test",
        config=config, settings=settings, browser_pool=browsers, preflight_runner=None, proxy_health_runner=None,
    )
    assert bool(calls) == expected


def test_explicit_portal_captcha_code_rotates_without_visible_form(tmp_path, monkeypatch):
    settings, config, store, pool = runtime(tmp_path)
    account = replace(config.accounts[0], proxy_provider="dataimpulse_mobile_sticky", dataimpulse_port=10001)
    monkeypatch.setenv("CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS", "a1")
    browsers = SimpleNamespace(can_replace_rejected_login=lambda _: False,
                               has_protected_session=lambda _: False)
    calls = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", lambda *a, **k: calls.append(k) or True)
    outcome = jobs._handle_account_safety_stop(
        SafetyStopException(StopReason.CAPTCHA_REJECTED, "rejected", status=400,
                            context="auth login", response_code="captcha-rechazado"),
        job_id=None, account=account, store=store, pool_store=pool, run_id="jobs-test",
        config=config, settings=settings, browser_pool=browsers, preflight_runner=None, proxy_health_runner=None,
    )
    assert outcome == "retry_account"
    assert [call["reason"] for call in calls] == ["login_captcha_rejected"]


def test_explicit_portal_captcha_code_still_requires_replacement_scope(tmp_path, monkeypatch):
    settings, config, store, pool = runtime(tmp_path)
    account = replace(config.accounts[0], proxy_provider="dataimpulse_mobile_sticky", dataimpulse_port=10001)
    monkeypatch.setenv("CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS", "")
    browsers = SimpleNamespace(can_replace_rejected_login=lambda _: False,
                               has_protected_session=lambda _: False)
    calls = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", lambda *a, **k: calls.append(k) or True)
    jobs._handle_account_safety_stop(
        SafetyStopException(StopReason.CAPTCHA_REJECTED, "rejected", status=400,
                            context="auth login", response_code="captcha-rechazado"),
        job_id=None, account=account, store=store, pool_store=pool, run_id="jobs-test",
        config=config, settings=settings, browser_pool=browsers, preflight_runner=None, proxy_health_runner=None,
    )
    assert not calls


@pytest.mark.parametrize("failures,reason,expected_reason", [
    (2, StopReason.CAPTCHA_REJECTED, None),
    (3, StopReason.CAPTCHA_REJECTED, "login_captcha_rejected"),
    (3, StopReason.CAPTCHA_SOLVER, "login_captcha_solver_failed"),
])
def test_repeated_login_captcha_failures_on_one_route_rotate(
    tmp_path, monkeypatch, failures, reason, expected_reason,
):
    settings, config, store, pool = runtime(tmp_path)
    account = replace(config.accounts[0], proxy_provider="dataimpulse_mobile_sticky", dataimpulse_port=10001)
    monkeypatch.setenv("CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS", "a1")
    for _ in range(failures):
        store.add_event("background_auth_failed", account_id="a1", level="warning",
                        data={"reason": "captcha_rejected"})
    browsers = SimpleNamespace(can_replace_rejected_login=lambda _: False,
                               has_protected_session=lambda _: False)
    calls = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", lambda *a, **k: calls.append(k) or True)
    context = "auth login" if reason is StopReason.CAPTCHA_REJECTED else "recaptcha solver"
    jobs._handle_account_safety_stop(
        SafetyStopException(reason, "failed", context=context),
        job_id=None, account=account, store=store, pool_store=pool, run_id="jobs-test",
        config=config, settings=settings, browser_pool=browsers, preflight_runner=None, proxy_health_runner=None,
    )
    assert [call["reason"] for call in calls] == ([expected_reason] if expected_reason else [])


def test_login_captcha_failure_count_resets_at_last_rotation(tmp_path):
    settings, config, store, pool = runtime(tmp_path)
    for _ in range(3):
        store.add_event("background_auth_failed", account_id="a1", level="warning",
                        data={"reason": "captcha_rejected"})
    assert jobs._login_captcha_failures_on_route(store, "a1") == 3
    with store.connect() as db:
        db.execute(
            "INSERT INTO account_proxy_routes(account_id, active_port, updated_at) VALUES ('a1', 10002, ?)",
            (jobs.utc_now(),),
        )
        db.execute(
            "UPDATE account_proxy_routes SET last_rotated_at = ? WHERE account_id = 'a1'",
            ("2999-01-01T00:00:00+00:00",),
        )
    assert jobs._login_captcha_failures_on_route(store, "a1") == 0


@pytest.mark.parametrize("browsers_live,external,orphan_chrome,allowed", [
    (False, False, False, True), (True, False, True, False), (True, False, False, True),
    (True, True, True, True)])
def test_proven_dead_worker_recovery_preserves_browser_ownership(
    tmp_path, monkeypatch, browsers_live, external, orphan_chrome, allowed,
):
    settings, _, store, pool = runtime(tmp_path)
    monkeypatch.setenv("CBRS_BROWSER_OWNER_MODE", "external" if external else "embedded")
    monkeypatch.setattr("cbrs.worker_lock.embedded_chrome_survives", lambda _settings: orphan_chrome)
    store.acquire_lease(jobs.WORKER_LEASE_NAME, "dead-fixture")
    if external:
        store.acquire_lease("browser_owner", "living-owner")
    if browsers_live:
        store.set_account_browser_state("a1", live=True, authenticated=False, headless=True,
                                        owner="living-owner", status="unknown")
    monkeypatch.setattr("cbrs.worker_lock.local_owner_is_dead", lambda _: True)
    calls = []
    @exclusive_worker
    def worker(**kwargs):
        calls.append(True)
    if allowed:
        worker(settings=settings, store=store)
        assert calls == [True]
        assert store.lease(jobs.WORKER_LEASE_NAME) is None
        assert pool.latest_run()["status"] == "stale"
        if browsers_live and not external:
            check = store.account_check("a1") or {}
            assert not check.get("browser_live")
            assert check.get("browser_status") == "browser_closed"
    else:
        with pytest.raises(RuntimeError, match="ownership survives"):
            worker(settings=settings, store=store)
        assert not calls
        assert store.lease(jobs.WORKER_LEASE_NAME)
    if external:
        assert store.active_lease("browser_owner")["owner"] == "living-owner"


def test_real_process_lock_releases_after_test_process_termination(tmp_path):
    from cbrs.owner_lock import OwnerLock
    path = tmp_path / "isolated-test-worker.lock"
    script = ("import sys,time; from cbrs.owner_lock import OwnerLock; "
              "lock=OwnerLock(sys.argv[1]); lock.__enter__(); "
              "print('ready',flush=True); time.sleep(30)")
    # Windows venv python.exe can be a redirector with a separate child PID;
    # launch the actual interpreter so terminate/wait refer to the lock holder.
    interpreter = getattr(sys, "_base_executable", sys.executable)
    child = subprocess.Popen([interpreter, "-c", script, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        owner = f"{socket.gethostname()}-{child.pid}-abcdef"
        assert not local_owner_is_dead(owner)
        with pytest.raises(RuntimeError, match="holds its lock"):
            with OwnerLock(path):
                pytest.fail("two owners")
    finally:
        child.terminate()  # only our isolated test child, never a service/browser
        child.wait(timeout=10)
        child.stdout.close()
    assert local_owner_is_dead(owner)
    with OwnerLock(path):
        pass
