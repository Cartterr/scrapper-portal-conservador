from types import SimpleNamespace
from cbrs import jobs
from cbrs.browser_session import BrowserSession, CommerceAuthState
from cbrs.config import load_settings
import pytest


def test_replacement_requires_exact_scope_and_visible_rejection(tmp_path, monkeypatch):
    store = jobs.JobStore(tmp_path / 'pool.sqlite3')
    settings = load_settings({}, root=tmp_path)
    pool = jobs._PersistentAccountBrowsers(scraper_factory=None, headless=True, store=store, worker_id='test')
    closed = []
    state = [True]
    browser = SimpleNamespace(has_visible_rejected_login=lambda: state[0],
        shutdown_service_context=lambda: closed.append('a2'))
    old = jobs._ManagedAccountScraper(manager=None, scraper=browser, settings=settings, authenticated_once=True)
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
    assert closed == ['a2']
    assert pool._entries['a2'] is new
    assert pool._entries['a1'].authenticated_once


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
