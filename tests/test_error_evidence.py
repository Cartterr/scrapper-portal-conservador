from types import SimpleNamespace
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from cbrs.jobs import JobStore
from cbrs.error_evidence import capture_error, evidence_path
from cbrs.account_pool import AccountPoolStore, PoolConfig, PoolAccount
from cbrs.account_pool_dashboard import start_pool_dashboard
from cbrs.config import load_settings


@pytest.fixture
def runtime(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    job, _ = store.create_job(kind="text", input_data={"text": "test"})
    store.claim_next("test-owner")
    with store.connect() as db:
        db.execute("INSERT INTO job_attempts(attempt_id,job_id,account_id,quota_date,"
                   "quota_consumed,status,started_at) VALUES('attempt-test',?,'a1','2026-09-06',1,'running','now')",
                   (job["job_id"],))
    return store, job["job_id"]


class Page:
    calls = 0

    def locator(self, selector):
        assert "input" in selector
        return "mask-locator"

    def screenshot(self, **kwargs):
        assert kwargs["timeout"] == 3000
        assert kwargs["mask"] == ["mask-locator"]
        self.calls += 1
        return b"\xff\xd8test-frame"


def test_capture_is_immutable_deduplicated_and_linked_to_attempt(runtime):
    store, job = runtime
    page = Page()
    error = RuntimeError("SECRET must not be recorded")
    capture_error(store, "a1", SimpleNamespace(page=page), error)
    capture_error(store, "a1", SimpleNamespace(page=page), error)
    evidence = store.get_job(job)["attempts"][0]["error_evidence"]
    assert len(evidence) == 1 and page.calls == 1
    assert "SECRET" not in str(evidence)
    assert evidence_path(store.path, evidence[0]["evidence_id"]).read_bytes() == b"\xff\xd8test-frame"
    assert store.get_job(job)["status"] == "running"
    assert JobStore(store.path).get_job(job)["attempts"][0]["error_evidence"] == evidence


def test_missing_browser_records_unavailable_without_throwing(runtime):
    store, job = runtime
    capture_error(store, "a1", object(), RuntimeError("test"))
    assert store.get_job(job)["attempts"][0]["error_evidence"][0]["capture_status"] == "unavailable"


def test_operation_diagnostics_allowlist_excludes_credentials(runtime):
    from cbrs.safety import SafetyStopException, StopReason
    store, job = runtime
    for context in ('auth refresh', 'SECRET-token'):
        capture_error(store, 'a1', SimpleNamespace(page=Page()),
            SafetyStopException(StopReason.AUTH_REQUIRED, 'SECRET-body', status=401, context=context))
    events = store.recent_events(job_id=job)
    assert 'SECRET' not in str(events)
    diagnostic = [e for e in events if e['event'] == 'portal_operation_error']
    assert len(diagnostic) == 1


def test_capture_is_bounded_and_does_not_attach_other_accounts(runtime):
    store, job = runtime
    page = Page()
    capture_error(store, "a2", SimpleNamespace(page=page), RuntimeError())
    assert page.calls == 0
    for _ in range(10):
        capture_error(store, "a1", SimpleNamespace(page=page), RuntimeError())
    assert page.calls == 8
    assert len(store.get_job(job)["attempts"][0]["error_evidence"]) == 8


def test_error_image_route_and_ui(runtime, tmp_path):
    store, job = runtime
    capture_error(store, "a1", SimpleNamespace(page=Page()), RuntimeError())
    eid = store.get_job(job)["attempts"][0]["error_evidence"][0]["evidence_id"]
    pool = AccountPoolStore(store.path)
    config = PoolConfig(accounts=(PoolAccount('a1', 'Test'),), daily_quota_per_account=20,
                        interval_minutes=0, dashboard_host='127.0.0.1', dashboard_port=0,
                        targets=())
    server = start_pool_dashboard(pool, config=config, job_store=store, port=0,
                                  settings=load_settings({}, root=tmp_path))
    try:
        with urlopen(server.url + '/api/error-screenshot/' + eid) as response:
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.read() == b"\xff\xd8test-frame"
        with pytest.raises(HTTPError) as exc:
            urlopen(server.url + '/api/error-screenshot/not-an-id')
        assert exc.value.code == 404
        with urlopen(server.url) as response:
            html = response.read().decode()
        assert 'attempt.error_evidence' in html
        assert 'Captura no disponible' in html
        assert 'La respuesta API puede no mostrarse' in html
    finally:
        server.stop()
