from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cbrs.owner_protocol import OwnerCommands, RemoteScraper, OWNER_LEASE, command_path, binding_fingerprint
from cbrs.browser_owner import BrowserOwner
from cbrs.browser_session import CommerceAuthState
from cbrs.config import load_settings
from cbrs.jobs import JobStore, WORKER_LEASE_NAME
from cbrs.account_pool import AccountPoolStore, PoolConfig, PoolAccount, local_today


def runtime(tmp_path, monkeypatch):
    settings = load_settings({}, root=tmp_path)
    account = PoolAccount("a", "Test", username_env="TEST_OWNER_USER", password_env="TEST_OWNER_PASS")
    config = PoolConfig(accounts=(account,), daily_quota_per_account=20, interval_minutes=0,
                        dashboard_host="127.0.0.1", dashboard_port=0, targets=())
    store = JobStore(tmp_path / "pool.sqlite3")
    pool_store = AccountPoolStore(store.path)
    pool_store.create_run(run_id="run", dry_run=False, config=config, dashboard_url=None)
    store.acquire_lease(WORKER_LEASE_NAME, "worker-one")
    monkeypatch.setenv("TEST_OWNER_USER", "test")
    monkeypatch.setenv("TEST_OWNER_PASS", "test-only")
    monkeypatch.setattr("cbrs.jobs._runtime_account_settings", lambda *args: settings)
    return settings, config, store, pool_store


def test_durable_commands_deduplicate_and_reject_changed_payload(tmp_path):
    q = OwnerCommands(tmp_path / "commands.sqlite3")
    command = q.submit("w", "a", "ensure", {"force": False}, command_id="same")
    assert q.submit("new-worker", "a", "ensure", {"force": False}, command_id="same") == command
    with pytest.raises(ValueError):
        q.submit("w", "a", "ensure", {"force": True}, command_id="same")
    assert q.claim("w")["id"] == command
    assert q.claim("w") is None
    q.finish(command, result="accepted")
    assert q.read(command)["state"] == "succeeded"
    q.recover_owner_crash()
    assert q.read(command)["state"] == "succeeded"


def test_owner_crash_never_replays_running_commands(tmp_path):
    q = OwnerCommands(tmp_path / "commands.sqlite3")
    command = q.submit("w", "a", "search_fna", {})
    q.claim("w")
    q.recover_owner_crash()
    assert q.read(command)["state"] == "uncertain"
    assert q.claim("w") is None
    stale = q.submit("dead-worker", "a", "ensure", {})
    assert q.claim("replacement") is None
    assert q.read(stale)["state"] == "cancelled"
    with pytest.raises(ValueError):
        q.submit("w", "a", "close_browser", {})


def test_worker_cannot_demote_or_close_owner_session(tmp_path, monkeypatch):
    settings, config, store, pool_store = runtime(tmp_path, monkeypatch)
    store.acquire_lease(OWNER_LEASE, "owner")
    store.set_account_browser_state("a", live=True, authenticated=True, headless=False,
        owner="owner", status="ready", auth_state="authenticated_form")
    store.set_account_browser_state("a", live=False, authenticated=False, headless=False,
        owner="worker-one", status="stopped")
    assert store.account_check("a")["browser_authenticated"]
    scraper = RemoteScraper(settings=replace(settings, account_id="a"), worker_id="worker-one",
                            store_path=store.path, commands_path=command_path(settings))
    scraper.close()
    scraper.browser.shutdown_service_context()
    store.release_lease(WORKER_LEASE_NAME, "worker-one")
    assert store.account_check("a")["browser_live"]
    assert scraper.browser.detect_commerce_auth_state() is CommerceAuthState.AUTHENTICATED_FORM


def test_live_owner_operation_prevents_abandoned_search_replay(tmp_path, monkeypatch):
    settings, config, store, pool_store = runtime(tmp_path, monkeypatch)
    job = store.create_job(kind="fna", input_data={"foja": 9441, "numero": 4580, "year": 1980})
    claimed = store.claim_next("worker-one")
    store.begin_attempt(job_id=claimed.job_id, account_id="a", quota_date=local_today(),
                        quota=20, run_id="run", consume_quota=True)
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_expires_at='2000-01-01' WHERE job_id=?", (claimed.job_id,))
    store.acquire_lease("browser_operation:" + claimed.job_id, "owner")
    assert store.recover_abandoned_jobs() == 0
    store.release_lease("browser_operation:" + claimed.job_id, "owner")
    assert store.recover_abandoned_jobs() == 1
    assert store.search_checkpoint(claimed.job_id)["uncertain"]


def test_owner_commits_search_before_reply_and_never_repeats_it(tmp_path, monkeypatch):
    settings, config, store, pool_store = runtime(tmp_path, monkeypatch)
    calls = []
    browser = SimpleNamespace(page=None)
    scraper = SimpleNamespace(browser=browser, search_by_fna=lambda *args: calls.append(args) or [{"ticket": "dummy-test-ticket", "foja": 9441}])
    owner = BrowserOwner(settings, config, store)
    owner.pool._entries["a"] = SimpleNamespace(scraper=scraper)
    job = store.create_job(kind="fna", input_data={"foja": 9441, "numero": 4580, "year": 1980})
    claimed = store.claim_next("worker-one")
    store.begin_attempt(job_id=claimed.job_id, account_id="a", quota_date=local_today(),
                        quota=20, run_id="run", consume_quota=True)
    identity = owner.commands.submit("worker-one", "a", "search_fna", {"foja": 9441, "numero": 4580, "ano": 1980}, job_id=claimed.job_id)
    command = owner.commands.claim("worker-one")
    result = owner.execute(command)
    # Simulate worker disappearing before a reply is observed.
    store.release_lease(WORKER_LEASE_NAME, "worker-one")
    owner.commands.finish(identity, result=result)
    assert store.search_checkpoint(claimed.job_id)["saved"]
    assert store.usage_by_account(local_today())["a"] == 1
    store.acquire_lease(WORKER_LEASE_NAME, "worker-two")
    command["worker"] = "worker-two"
    with pytest.raises(ValueError, match="already accepted"):
        owner.execute(command)
    assert len(calls) == 1


def test_owner_rejects_unapproved_binding_and_paths(tmp_path, monkeypatch):
    settings, config, store, pool_store = runtime(tmp_path, monkeypatch)
    owner = BrowserOwner(settings, config, store)
    command = {"version": 1, "worker": "worker-one", "account": "a", "operation": "ensure",
               "payload": json.dumps({"binding": "wrong"})}
    with pytest.raises(ValueError, match="binding"):
        owner.execute(command)
    owner.pool._entries["a"] = SimpleNamespace(scraper=SimpleNamespace())
    command.update(operation="download_image", payload=json.dumps({"output_path": str(tmp_path / "escape.jpg")}))
    with pytest.raises(ValueError, match="permitted"):
        owner.execute(command)


def test_worker_timeout_keeps_running_owner_reservation(tmp_path, monkeypatch):
    settings, config, store, pool_store = runtime(tmp_path, monkeypatch)
    store.create_job(kind="fna", input_data={"foja": 9441, "numero": 4580, "year": 1980})
    job = store.claim_next("worker-one")
    attempt = store.begin_attempt(job_id=job.job_id, account_id="a", quota_date=local_today(),
                                  quota=20, run_id="run", consume_quota=True)
    store.acquire_lease("browser_operation:" + job.job_id, "owner")
    store.finish_attempt(attempt, status="failed", error="caller timeout")
    with store.connect() as db:
        row = db.execute("SELECT status,quota_consumed FROM job_attempts WHERE attempt_id=?", (attempt,)).fetchone()
        assert tuple(row) == ("running", 1)
    store.add_results(job.job_id, [{"ticket": "test-ticket"}], attempt_id=attempt)
    assert store.search_checkpoint(job.job_id)["saved"]


def test_owner_lock_survives_expired_lease_and_releases_cleanly(tmp_path):
    from cbrs.owner_lock import OwnerLock
    path = tmp_path / "owner.lock"
    with OwnerLock(path):
        with pytest.raises(RuntimeError, match="still holds"):
            with OwnerLock(path):
                pytest.fail("duplicate owner")
    with OwnerLock(path):
        pass


def test_owner_recovery_rechecks_scoped_live_dom_and_updates_binding(tmp_path, monkeypatch):
    settings, config, store, pool_store = runtime(tmp_path, monkeypatch)
    owner = BrowserOwner(settings, config, store)
    visible = [False]
    owner.pool._entries['a'] = SimpleNamespace(scraper=SimpleNamespace(
        browser=SimpleNamespace(has_visible_rejected_login=lambda: visible[0])))
    command = {'version': 1, 'worker': 'worker-one', 'account': 'a',
               'operation': 'recover_route', 'payload': '{}'}
    calls = []
    monkeypatch.setattr('cbrs.jobs._rotate_dataimpulse_route', lambda *args, **kwargs: calls.append(kwargs) or True)
    monkeypatch.setenv('CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS', 'a')
    assert owner.execute(command) is False
    assert calls == []
    visible[0] = True
    assert owner.execute(command) is True
    assert calls[0]['_owner_execution'] is True
    assert owner.bindings['a'] == binding_fingerprint(settings, settings.headless)
    monkeypatch.setenv('CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS', '')
    assert owner.execute(command) is False


def _isolated_native_owner(root, ready):
    """Actual separate owner process, actual Chrome; no target-portal traffic."""
    from playwright.sync_api import sync_playwright
    import time
    root = Path(root)
    settings = replace(load_settings({}, root=root), account_id="a")
    config = PoolConfig(accounts=(PoolAccount("a", "Test"),), daily_quota_per_account=20,
                        interval_minutes=0, dashboard_host="127.0.0.1", dashboard_port=0, targets=())
    store = JobStore(root / "test.sqlite3")
    owner = BrowserOwner(settings, config, store)
    import cbrs.jobs
    cbrs.jobs._runtime_account_settings = lambda *args: settings
    with sync_playwright() as p:
        chrome = p.chromium.launch(channel="chrome", headless=True)
        context = chrome.new_context()
        page = context.new_page()
        page.route("**/*", lambda r: r.fulfill(body="<h1>Isolated owner test</h1>", content_type="text/html"))
        page.goto("http://127.0.0.1:19998/test")
        page.evaluate("localStorage.setItem('proof','retained')")
        context.add_cookies([{"name": "test-session", "value": "retained", "url": page.url}])
        cdp = chrome.new_browser_cdp_session()
        browser = SimpleNamespace(detect_commerce_auth_state=lambda: CommerceAuthState.AUTHENTICATED_FORM)
        owner.pool._entries["a"] = SimpleNamespace(scraper=SimpleNamespace(browser=browser))
        def observe(**kwargs):
            proof = {"pid": next(v["id"] for v in cdp.send("SystemInfo.getProcessInfo")["processInfo"] if v["type"] == "browser"),
                     "storage": page.evaluate("localStorage.getItem('proof')"), "cookie": context.cookies()[0]["value"],
                     "observed": time.time()}
            path = root / "proof.json"
            staged = root / "proof.new"
            staged.write_text(json.dumps(proof))
            staged.replace(path)
            store.set_account_browser_state("a", live=True, authenticated=True, headless=True,
                owner=owner.identity, status="ready", auth_state="authenticated_form")
            ready.set()
        owner.pool.capture_previews = observe
        owner.pool.close_all = lambda **kwargs: chrome.close()
        owner.run()


def _isolated_worker(root, identity, ready):
    import time
    root = Path(root)
    settings = replace(load_settings({}, root=root), account_id="a")
    store = JobStore(root / "test.sqlite3")
    assert store.acquire_lease(WORKER_LEASE_NAME, identity)
    with RemoteScraper(settings=settings, worker_id=identity, store_path=store.path,
                       commands_path=command_path(settings)) as scraper:
        assert scraper.ensure_authenticated() == "browser_form"
        ready.set()
        time.sleep(60)


def test_real_chrome_survives_worker_process_death_and_replacement(tmp_path):
    import multiprocessing
    import time
    ctx = multiprocessing.get_context("spawn")
    owner_ready = ctx.Event()
    owner = ctx.Process(target=_isolated_native_owner, args=(str(tmp_path), owner_ready))
    workers = []
    owner.start()
    try:
        assert owner_ready.wait(30), "isolated owner failed to start"
        original = json.loads((tmp_path / "proof.json").read_text())
        for identity in ("worker-one", "worker-two"):
            ready = ctx.Event()
            worker = ctx.Process(target=_isolated_worker, args=(str(tmp_path), identity, ready))
            workers.append(worker)
            worker.start()
            assert ready.wait(20), "worker did not attach"
            worker.terminate()  # Only this disposable test worker, never production.
            worker.join(10)
            store = JobStore(tmp_path / "test.sqlite3")
            store.release_lease(WORKER_LEASE_NAME, identity)
            before = json.loads((tmp_path / "proof.json").read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                after = json.loads((tmp_path / "proof.json").read_text())
                if after["observed"] > before["observed"]:
                    break
                time.sleep(.1)
            assert after["observed"] > before["observed"], "owner observations stopped with worker"
            assert after["pid"] == original["pid"]
            assert after["cookie"] == after["storage"] == "retained"
            assert store.account_check("a")["browser_authenticated"]
        assert owner.is_alive()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(10)
        settings = load_settings({}, root=tmp_path)
        OwnerCommands(command_path(settings)).stop()
        owner.join(20)
        if owner.is_alive():
            owner.terminate()
            owner.join(10)
    assert owner.exitcode == 0
