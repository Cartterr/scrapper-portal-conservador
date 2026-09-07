from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import pytest

from cbrs import jobs
from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig
from cbrs.browser_session import CommerceAuthState
from cbrs.config import load_settings
from cbrs.safety import SafetyStopException, StopReason


def setup_runtime(tmp_path, monkeypatch):
    settings = replace(load_settings({}, root=tmp_path),
                       browser_healthcheck_seconds=0, browser_reauth_backoff_seconds=0)
    config = PoolConfig(accounts=tuple(
        PoolAccount(f"a{i}", f"Account {i}", username_env=f"TEST_USER_{i}",
                    password_env=f"TEST_PASSWORD_{i}",
                    proxy_provider="dataimpulse_mobile_sticky", dataimpulse_port=10000+i)
        for i in range(1, 4)), daily_quota_per_account=20, interval_minutes=0,
        dashboard_host="127.0.0.1", dashboard_port=0, targets=())
    for i in range(1, 4):
        monkeypatch.setenv(f"TEST_USER_{i}", f"a{i}")
        monkeypatch.setenv(f"TEST_PASSWORD_{i}", "test-only")
    store = jobs.JobStore(tmp_path / "pool.sqlite3")
    pool_store = AccountPoolStore(store.path)
    pool_store.create_run(run_id="test-run", dry_run=False, config=config, dashboard_url=None)
    store.acquire_lease(jobs.WORKER_LEASE_NAME, "test-worker")

    class Scraper:
        def __init__(self, **kwargs):
            self.state = CommerceAuthState.UNKNOWN
            self.calls = 0
            self.browser = self
            self.page = SimpleNamespace(is_closed=lambda: False)

        def ensure_authenticated(self, username, password, **kwargs):
            self.calls += 1
            if username != "a1":
                raise SafetyStopException(StopReason.TEMPORARY_UNAVAILABLE,
                                          "rejected", status=400, context="auth login")
            self.state = CommerceAuthState.AUTHENTICATED_FORM
            return "browser_form"

        def detect_commerce_auth_state(self):
            return self.state

        def reload_current_page(self):
            pass

        def wait_for_commerce_auth_state(self):
            return self.state

    pool = jobs._PersistentAccountBrowsers(scraper_factory=Scraper, headless=True,
                                          store=store, worker_id="test-worker")
    jobs._wire_browser_auth_recovery(settings, config, store, pool_store, "test-run",
                                    pool, lambda *a, **k: None, lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_runtime_account_settings", lambda *a, **k: settings)
    monkeypatch.setattr(jobs, "_ensure_account_gate", lambda *a, **k: True)
    return settings, config, store, pool_store, pool


def test_startup_and_idle_rejections_share_recovery_without_touching_healthy_context(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    rotated = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route",
                        lambda account, *a, **k: rotated.append(account.account_id) or False)
    jobs._run_startup_gates(settings, config, store, pool_store, "test-run",
                            lambda: None, lambda: None, pool)
    healthy = pool._entries["a1"]
    assert [store.dataimpulse_route(a)["temporary_failure_count"] for a in ("a2", "a3")] == [1, 1]
    assert rotated == []
    pool.reconcile()
    assert pool._entries["a2"].scraper.calls == 1  # account cooldown blocks retries
    with store.connect() as db:
        db.execute("UPDATE accounts SET resume_at = '2000-01-01T00:00:00+00:00' WHERE status = 'paused'")
    pool_store.reactivate_expired_cooldowns("test-run")
    for entry in pool._entries.values():
        entry.last_reauth_at = 0
    pool.reconcile()
    assert rotated == ["a2", "a3"]
    assert pool._entries["a1"] is healthy
    assert healthy.scraper.calls == 1
    assert store.account_check("a3")["browser_last_auth_error"] == "temporary_unavailable"
    assert store.account_check("a3")["browser_last_auth_http_status"] == 400
    assert store.account_check("a3")["browser_authenticated_at"] is None
    # Passive DOM publication must retain the precise failure.
    pool.refresh_page_auth_states()
    assert store.account_check("a3")["browser_last_auth_error"] == "temporary_unavailable"


def test_form_evidence_requires_current_owner_and_is_only_used_for_login(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    store.set_account_browser_state("a1", live=True, authenticated=True, headless=True,
                                    owner="test-worker", status="authenticated",
                                    auth_state="authenticated_form")
    assert store.another_account_succeeded_recently("a2", allow_authenticated_form=True)
    assert not store.another_account_succeeded_recently("a2")
    with store.connect() as db:
        db.execute("UPDATE account_checks SET browser_checked_at = '2000-01-01T00:00:00+00:00'")
    assert not store.another_account_succeeded_recently("a2", allow_authenticated_form=True)
    store.set_account_browser_state("a1", live=True, authenticated=True, headless=True,
                                    owner="old-worker", status="authenticated",
                                    auth_state="authenticated_form")
    assert not store.another_account_succeeded_recently("a2", allow_authenticated_form=True)


def test_global_and_rotation_cooldowns_suppress_background_login(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    assert pool.can_reauthenticate("a2")
    store.set_global_cooldown("temporary_unavailable_all_accounts", 300)
    assert not pool.can_reauthenticate("a2")
    store.clear_control("global_safety_cooldown")
    assert pool.can_reauthenticate("a2")
    # The route cooldown is durable even if an account cooldown has elapsed.
    store.ensure_dataimpulse_route("a2", 10002)
    with store.connect() as db:
        db.execute("UPDATE account_proxy_routes SET cooldown_until = '2999-01-01T00:00:00+00:00'")
    assert not pool.can_reauthenticate("a2")


def test_all_startup_logins_rejected_open_outage_circuit_without_rotation(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)

    def rejected(self, *args, **kwargs):
        self.calls += 1
        raise SafetyStopException(StopReason.TEMPORARY_UNAVAILABLE, "rejected", status=400)

    monkeypatch.setattr(pool.scraper_factory, "ensure_authenticated", rejected)
    rotations = []
    monkeypatch.setattr(jobs, "_rotate_dataimpulse_route", lambda *a, **k: rotations.append(a))
    jobs._run_startup_gates(settings, config, store, pool_store, "test-run",
                            lambda: None, lambda: None, pool)
    assert store.global_cooldown()["reason"] == "temporary_unavailable_all_accounts"
    pool.reconcile()
    assert all(entry.scraper.calls == 1 for entry in pool._entries.values())
    assert rotations == []


def test_confirmed_form_clears_error_but_unknown_never_advances_success_time(tmp_path):
    store = jobs.JobStore(tmp_path / "pool.sqlite3")
    args = dict(live=True, headless=True, owner="test-worker", status="checked")
    store.set_account_browser_state("a1", authenticated=False, auth_state="unknown",
                                    auth_error="temporary_unavailable", auth_http_status=400, **args)
    assert store.account_check("a1")["browser_authenticated_at"] is None
    store.set_account_browser_state("a1", authenticated=True, auth_state="authenticated_form", **args)
    confirmed = store.account_check("a1")["browser_authenticated_at"]
    store.set_account_browser_state("a1", authenticated=False, auth_state="unknown", **args)
    assert store.account_check("a1")["browser_authenticated_at"] == confirmed
    assert store.account_check("a1")["browser_last_auth_error"] is None


def test_existing_context_cannot_be_closed_or_rotated_until_service_shutdown(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    closed = []
    scraper = pool.scraper_factory()
    scraper.close = lambda: closed.append(True)
    entry = jobs._ManagedAccountScraper(manager=scraper, scraper=scraper,
                                        authenticated_once=True)
    pool._entries["a1"] = entry
    for reason in ("credentials_invalid", "browser_context_failed", "proxy_route_rotated"):
        pool.discard("a1", status=reason)
        assert pool._entries["a1"] is entry
    pool.close_all()
    assert closed == []
    assert not jobs._rotate_dataimpulse_route(
        config.accounts[0], settings, store, pool_store, "test-run", pool,
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Must not probe")),
        lambda *a, **k: None, reason="temporary_unavailable")
    assert store.dataimpulse_route("a1")["pending_port"] is None
    pool.close_all(service_shutdown=True)
    assert closed == [True]
    assert not pool._entries


def test_previously_authenticated_unknown_context_is_not_reloaded_or_relogged(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    with pool.session("a1", settings, "a1", "test-only"):
        pass
    entry = pool._entries["a1"]
    entry.scraper.state = CommerceAuthState.UNKNOWN
    entry.unknown_checks = 3
    entry.reauth_required = True
    entry.scraper.reload_current_page = lambda: (_ for _ in ()).throw(AssertionError("Must not reload"))
    pool.reconcile()
    assert pool._entries["a1"] is entry
    assert entry.scraper.calls == 1
    assert entry.authenticated_once


def test_continuous_worker_retains_contexts_after_scheduler_error_until_stop(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    closed = []
    retained = []

    class OwnedScraper(pool.scraper_factory):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

    def scheduler_error(*args, **kwargs):
        raise RuntimeError("scheduler failed")

    def wait_for_operator(_seconds):
        assert len(closed) == 0
        retained.append(True)
        pool_store.request_stop()

    monkeypatch.setattr(store, "claim_next", scheduler_error)
    store.release_lease(jobs.WORKER_LEASE_NAME, "test-worker")
    pool_store.update_run("test-run", status="stopped", finished=True)
    result = jobs.run_job_worker(
        settings=settings, config=config, store=store, pool_store=pool_store,
        scraper_factory=OwnedScraper,
        preflight_runner=lambda *a, **k: None, proxy_health_runner=lambda *a, **k: None,
        sleep_fn=wait_for_operator,
    )
    assert retained
    assert result.status == "stopped"
    assert len(closed) == 3, store.recent_events(limit=10)


def candidate_runtime(tmp_path, monkeypatch, *, accepted=True, persistence_error=False, same_exit=False):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    old = pool.scraper_factory()
    pool._entries["a2"] = jobs._ManagedAccountScraper(manager=old, scraper=old, settings=settings)
    healthy = pool.scraper_factory()
    healthy.state = CommerceAuthState.AUTHENTICATED_FORM
    pool._entries["a1"] = jobs._ManagedAccountScraper(manager=healthy, scraper=healthy,
                                                     settings=settings, authenticated_once=True)
    closed, probes, baselines = [], [], []

    class Probe(pool.scraper_factory):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            probes.append(self)

        def ensure_authenticated(self, *args, **kwargs):
            self.calls += 1
            if not accepted:
                raise SafetyStopException(StopReason.TEMPORARY_UNAVAILABLE, "rejected", status=400)
            self.state = CommerceAuthState.AUTHENTICATED_FORM

        def close(self):
            closed.append(self)

    pool.scraper_factory = Probe
    store.set_account_check("a2", proxy_status="passed", egress_hash="old-exit-hash")

    def replace_baseline(*args, **kwargs):
        if persistence_error:
            raise OSError("storage unavailable")
        baselines.append(kwargs["egress_hash"])

    monkeypatch.setattr("cbrs.preflight.replace_egress_baseline", replace_baseline)
    gate = lambda *a, **k: SimpleNamespace(ok=True, report={
        "egress_hash": "old-exit-hash" if same_exit else "new-exit-hash", "egress_country": "CL"})
    result = jobs._rotate_dataimpulse_route(config.accounts[1], settings, store, pool_store,
                                          "test-run", pool, gate, gate, reason="mobile_login_canary")
    return result, pool, store, old, healthy, probes, closed, baselines


def test_successful_candidate_is_adopted_alive_without_relogin_or_closing_old_context(tmp_path, monkeypatch):
    ok, pool, store, old, healthy, probes, closed, baselines = candidate_runtime(tmp_path, monkeypatch)
    assert ok
    assert len(probes) == 1
    assert probes[0].calls == 1
    assert closed == []
    assert pool._entries["a2"].scraper is probes[0]
    assert pool._entries["a1"].scraper is healthy
    assert any(key == "a2" and entry.scraper is old for key, entry in pool._retained_entries)
    assert store.account_check("a2")["browser_auth_state"] == "authenticated_form"
    assert baselines == ["new-exit-hash"]
    assert store.dataimpulse_route("a2")["active_port"] != 10002


def test_rejected_candidate_closes_only_disposable_probe_and_preserves_routes(tmp_path, monkeypatch):
    ok, pool, store, old, healthy, probes, closed, baselines = candidate_runtime(tmp_path, monkeypatch, accepted=False)
    assert not ok
    assert closed == probes
    assert pool._entries["a2"].scraper is old
    assert pool._entries["a1"].scraper is healthy
    assert store.dataimpulse_route("a2")["active_port"] == 10002
    assert json.loads(store.dataimpulse_route("a2")["rejected_ports_json"])
    assert baselines == []
    assert store.candidate_exit_rejected("a2", "new-exit-hash")


def test_new_port_with_same_exit_does_not_attempt_login(tmp_path, monkeypatch):
    ok, pool, store, old, healthy, probes, closed, baselines = candidate_runtime(tmp_path, monkeypatch, same_exit=True)
    assert not ok
    assert probes == []
    assert closed == []
    assert baselines == []


def test_proven_candidate_survives_persistence_error_and_blocks_further_rotation(tmp_path, monkeypatch):
    ok, pool, store, old, healthy, probes, closed, baselines = candidate_runtime(tmp_path, monkeypatch, persistence_error=True)
    assert not ok
    assert closed == []
    assert pool._entries["a2"].scraper is old
    assert any(entry.scraper is probes[0] for _, entry in pool._retained_entries)
    assert pool.has_protected_session("a2")
    assert store.dataimpulse_route("a2")["active_port"] == 10002


def test_mobile_canary_budget_is_pool_wide_durable_and_bounded(tmp_path):
    store = jobs.JobStore(tmp_path / "pool.sqlite3")
    for _ in range(3):
        assert store.reserve_mobile_login_canary()
        assert not jobs.JobStore(store.path).reserve_mobile_login_canary()
        state = json.loads(store.get_control("mobile_login_canary")["value"])
        state["last_attempt"] = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        store.set_control("mobile_login_canary", json.dumps(state))
    assert not store.reserve_mobile_login_canary()


def test_failed_exit_history_is_durable_and_expires(tmp_path):
    store = jobs.JobStore(tmp_path / "pool.sqlite3")
    store.record_failed_candidate_exit("a2", "sanitized-hash")
    assert jobs.JobStore(store.path).candidate_exit_rejected("a2", "sanitized-hash")
    with store.connect() as db:
        db.execute("UPDATE proxy_candidate_exits SET failed_at='2000-01-01T00:00:00+00:00'")
    assert not store.candidate_exit_rejected("a2", "sanitized-hash")


def pending_fast_retry(tmp_path, monkeypatch):
    settings, config, store, pool_store, pool = setup_runtime(tmp_path, monkeypatch)
    settings = replace(settings, browser_healthcheck_seconds=30,
                       browser_reauth_backoff_seconds=60)
    now = [100.0]
    monkeypatch.setattr(jobs.time, "monotonic", lambda: now[0])
    pool._last_reconcile_at = 99.0  # normal health scan is NOT due
    for account_id in ("a1", "a2"):
        scraper = pool.scraper_factory()
        protected = account_id == "a1"
        scraper.state = (CommerceAuthState.AUTHENTICATED_FORM if protected
                         else CommerceAuthState.UNKNOWN)
        entry = jobs._ManagedAccountScraper(
            manager=scraper, scraper=scraper, settings=settings,
            username=account_id, password="test-only", authenticated_once=protected,
            reauth_required=not protected, unknown_checks=4)
        pool._entries[account_id] = entry
        pool._known_accounts[account_id] = (settings, account_id, "test-only")
    healthy = pool._entries["a1"]
    healthy.scraper.observations = 0

    def observe_healthy():
        healthy.scraper.observations += 1
        return CommerceAuthState.AUTHENTICATED_FORM

    healthy.scraper.detect_commerce_auth_state = observe_healthy
    pending = pool._entries["a2"]
    pending.scraper.reload_current_page = lambda: (_ for _ in ()).throw(
        AssertionError("known failed login must not do a redundant reload"))

    def recovered(*args, **kwargs):
        pending.scraper.calls += 1
        pending.scraper.state = CommerceAuthState.AUTHENTICATED_FORM
        return "browser_form"

    pending.scraper.ensure_authenticated = recovered
    return settings, store, pool_store, pool, pending, healthy, now


def test_due_retry_does_not_wait_for_health_scan_or_reload_again(tmp_path, monkeypatch):
    _, store, _, pool, pending, healthy, _ = pending_fast_retry(tmp_path, monkeypatch)
    pool.reconcile()
    assert pending.scraper.calls == 1
    assert pending.authenticated_once
    assert not pending.reauth_required
    assert pool._last_reconcile_at == 99.0
    assert pool._entries["a1"] is healthy
    assert healthy.scraper.observations == 0
    assert store.account_check("a2")["browser_auth_state"] == "authenticated_form"
    pool.reconcile()
    assert pending.scraper.calls == 1  # no duplicate immediate login


@pytest.mark.parametrize("gate", ["account", "global", "route", "captcha"])
def test_fast_retry_keeps_every_safety_gate(tmp_path, monkeypatch, gate):
    _, store, pool_store, pool, pending, _, _ = pending_fast_retry(tmp_path, monkeypatch)
    if gate == "global":
        store.set_global_cooldown("temporary_unavailable_all_accounts", 300)
    elif gate == "route":
        with store.connect() as db:
            db.execute("UPDATE account_proxy_routes SET cooldown_until = '2999-01-01T00:00:00+00:00'")
    elif gate == "captcha":
        pool_store.mark_account_captcha_pending("test-run", "a2", reason="captcha_rejected")
    else:
        pool_store.pause_account("test-run", "a2", reason="temporary_unavailable", cooldown_seconds=120)
    pool.reconcile()
    assert pending.scraper.calls == 0
    assert pending.reauth_required


def test_fast_retry_waits_until_exact_retry_floor(tmp_path, monkeypatch):
    _, _, _, pool, pending, _, now = pending_fast_retry(tmp_path, monkeypatch)
    pending.last_reauth_at = 50.0
    now[0] = 109.9
    pool.reconcile()
    assert pending.scraper.calls == 0
    now[0] = 110.0
    pool.reconcile()
    assert pending.scraper.calls == 1


def test_fast_retry_refreshes_clock_after_another_accounts_slow_login(tmp_path, monkeypatch):
    settings, _, _, pool, pending, _, now = pending_fast_retry(tmp_path, monkeypatch)
    next_scraper = pool.scraper_factory()
    next_scraper.state = CommerceAuthState.LOGIN_GATE
    next_entry = jobs._ManagedAccountScraper(
        manager=next_scraper, scraper=next_scraper, settings=settings,
        username="a3", password="test-only", reauth_required=True,
        last_reauth_at=50.0)
    pool._entries["a3"] = next_entry
    pool._known_accounts["a3"] = (settings, "a3", "test-only")
    original_recover = pending.scraper.ensure_authenticated

    def slow_recover(*args, **kwargs):
        now[0] = 115.0
        return original_recover(*args, **kwargs)

    def next_recover(*args, **kwargs):
        next_scraper.calls += 1
        next_scraper.state = CommerceAuthState.AUTHENTICATED_FORM
        return "browser_form"

    pending.scraper.ensure_authenticated = slow_recover
    next_scraper.ensure_authenticated = next_recover
    pool.reconcile()
    assert pending.scraper.calls == 1
    assert next_scraper.calls == 1
    assert next_entry.last_reauth_at == 115.0


def test_fast_retry_promotes_manual_success_without_submitting(tmp_path, monkeypatch):
    _, store, _, pool, pending, _, _ = pending_fast_retry(tmp_path, monkeypatch)
    pending.scraper.state = CommerceAuthState.AUTHENTICATED_FORM
    pool.reconcile()
    assert pending.scraper.calls == 0
    assert pending.authenticated_once
    assert not pending.reauth_required
    assert store.account_check("a2")["browser_authenticated"] == 1


def test_fast_retry_never_reloads_a_previously_authenticated_unknown_context(tmp_path, monkeypatch):
    _, _, _, pool, pending, _, _ = pending_fast_retry(tmp_path, monkeypatch)
    pending.authenticated_once = True
    pool.reconcile()
    assert pending.scraper.calls == 0
    assert pool._entries["a2"] is pending
