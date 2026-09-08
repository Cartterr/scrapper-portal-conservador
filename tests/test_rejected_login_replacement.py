from types import SimpleNamespace
from cbrs import jobs
from cbrs.browser_session import BrowserSession, CommerceAuthState
from cbrs.config import load_settings
import pytest
import json


def test_known_login_gate_still_publishes_visible_rejection(tmp_path):
    from cbrs.runtime_observation import sample_auth
    settings = load_settings({}, root=tmp_path)
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    browser = SimpleNamespace(page=None,
        detect_commerce_auth_state=lambda: CommerceAuthState.LOGIN_GATE,
        has_visible_rejected_login=lambda: True)
    pool = jobs._PersistentAccountBrowsers(scraper_factory=None, headless=False, store=store, worker_id='test')
    old = jobs._ManagedAccountScraper(manager=None, scraper=browser, settings=settings, authenticated_once=True)
    pool._entries['a2'] = old
    sample_auth(pool)
    check = store.account_check('a2')
    assert check['browser_status'] == 'login_rejected_visible'
    assert check['browser_auth_state'] == 'login_gate'
    assert not check['browser_authenticated']
    assert old.authenticated_once and old.reauth_required


def test_legacy_or_stale_owner_cannot_receive_candidate_recovery(tmp_path):
    from cbrs.owner_protocol import owner_preserves_recovery_contexts, command_path
    settings = load_settings({}, root=tmp_path)
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    store.acquire_lease('browser_owner', 'live-owner')
    marker = command_path(settings).parent / 'capabilities.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    assert not owner_preserves_recovery_contexts(settings, store)
    marker.write_text(json.dumps({'owner': 'old-owner', 'retain_production_on_recovery': True}))
    assert not owner_preserves_recovery_contexts(settings, store)
    marker.write_text(json.dumps({'owner': 'live-owner', 'retain_production_on_recovery': True}))
    assert owner_preserves_recovery_contexts(settings, store)


def test_old_remote_owner_does_not_suppress_same_browser_login(tmp_path, monkeypatch):
    settings = load_settings({}, root=tmp_path)
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    pool = jobs._PersistentAccountBrowsers(scraper_factory=None, headless=False, store=store, worker_id='test')
    pool._entries['a2'] = jobs._ManagedAccountScraper(manager=None,
        scraper=SimpleNamespace(is_remote=True, has_visible_rejected_login=lambda: True),
        settings=settings, authenticated_once=True)
    monkeypatch.setenv('CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS', 'a2')
    assert not pool.can_replace_rejected_login('a2')


@pytest.mark.parametrize('previous_authenticated', [False, True])
def test_replacement_requires_scope_but_never_closes_production(tmp_path, monkeypatch, previous_authenticated):
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    settings = load_settings({}, root=tmp_path)
    pool = jobs._PersistentAccountBrowsers(scraper_factory=None, headless=True, store=store, worker_id='test')
    closed = []
    state = [True]
    browser = SimpleNamespace(has_visible_rejected_login=lambda: state[0],
        shutdown_service_context=lambda: closed.append('a2'))
    old = jobs._ManagedAccountScraper(manager=SimpleNamespace(__exit__=lambda *args: closed.append('manager')),
        scraper=browser, settings=settings, authenticated_once=previous_authenticated)
    pool._entries['a2'] = old
    pool._entries['a1'] = jobs._ManagedAccountScraper(manager=None, scraper=SimpleNamespace(), settings=settings, authenticated_once=True)
    assert not pool.can_replace_rejected_login('a2')
    monkeypatch.setenv('CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS', 'a2')
    assert pool.can_replace_rejected_login('a2')
    assert not pool.can_replace_rejected_login('a1')
    state[0] = False
    assert not pool.can_replace_rejected_login('a2')
    state[0] = True
    new = jobs._ManagedAccountScraper(manager=None, scraper=SimpleNamespace(
        detect_commerce_auth_state=lambda: CommerceAuthState.AUTHENTICATED_FORM), settings=settings, authenticated_once=True)
    pool.adopt_authenticated_candidate('a2', new)
    assert closed == []
    assert ('a2', old) in pool._retained_entries
    assert pool._entries['a2'] is new
    assert pool._entries['a1'].authenticated_once
    pool.discard('a2', status='later_login_rejection')
    pool.close_all()
    assert closed == []
    assert pool._entries['a2'] is new


def test_random_selection_excludes_active_pending_and_rejected_ports(tmp_path, monkeypatch):
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    store.ensure_dataimpulse_route('a', 10002)
    store.ensure_dataimpulse_route('b', 10003)
    with store.connect() as db:
        db.execute("UPDATE account_proxy_routes SET pending_port=10004 WHERE account_id='b'")
        db.execute("UPDATE account_proxy_routes SET rejected_ports_json='[10007]' WHERE account_id='a'")
    sampled = []
    monkeypatch.setattr(jobs.secrets, 'choice', lambda candidates: sampled.extend(candidates) or candidates[-1])
    candidate = store.begin_dataimpulse_rotation('a', initial_port=10002, reason='test', port_min=10000,
        port_max=10010, cooldown_seconds=300, max_rotations_per_hour=3, randomize=True)
    assert candidate['pending_port'] == 10010
    assert not {10002, 10003, 10004, 10007}.intersection(sampled)
    assert 10000 in sampled and 10010 in sampled


@pytest.mark.parametrize('index,expected', [(0, 10000), (-1, 20000)])
def test_random_selection_covers_full_provider_range(tmp_path, monkeypatch, index, expected):
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    sampled = []
    monkeypatch.setattr(jobs.secrets, 'choice', lambda candidates: sampled.extend(candidates) or candidates[index])
    result = store.begin_dataimpulse_rotation('a', initial_port=15000, reason='test',
        port_min=10000, port_max=20000, cooldown_seconds=0,
        max_rotations_per_hour=30, randomize=True)
    assert result['pending_port'] == expected
    assert len(sampled) == 10000  # Every provider port except the active route.
    assert 15000 not in sampled


def test_real_dom_rejected_login_requires_complete_visible_signature(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        chrome = p.chromium.launch(channel='chrome', headless=True)
        try:
            page = chrome.new_page()
            page.route('**/*', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
            page.goto('http://127.0.0.1:19997/login')
            session = BrowserSession(load_settings({}, root=tmp_path))
            session._context = page.context
            session.detect_commerce_auth_state = lambda: CommerceAuthState.UNKNOWN
            form = '<input type="email"><input type="password"><button>Iniciar sesión</button>'
            alert = '<div role="alert">Se ha detectado un problema, refresque la página e intente nuevamente.</div>'
            page.set_content(form + alert)
            assert session.has_visible_rejected_login()
            page.set_content(form + alert.replace('role="alert"', 'role="alert" hidden'))
            assert not session.has_visible_rejected_login()
            page.set_content(alert)
            assert not session.has_visible_rejected_login()
            page.set_content(form + alert)
            session.detect_commerce_auth_state = lambda: CommerceAuthState.AUTHENTICATED_FORM
            assert not session.has_visible_rejected_login()
            session.detect_commerce_auth_state = lambda: CommerceAuthState.UNKNOWN
            page.goto('http://127.0.0.1:19997/consultas-en-linea')
            page.set_content(form + alert)
            assert not session.has_visible_rejected_login()
        finally:
            chrome.close()  # Isolated test only.
