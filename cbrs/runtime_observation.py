"""Reloadable passive DOM evidence policy; no recovery or navigation."""
from .browser_session import CommerceAuthState
from cbrs.jobs import _browser_engine

# Change this in a published release to tune passive freshness without replacing
# browser settings. Bounds protect the owner from an accidental tight loop.
PREVIEW_INTERVAL_SECONDS = 2.0


def visible_login_gate(page):
    # Navigation can destroy the current JS execution context between samples.
    # That is unknown DOM, not a failed search or evidence of authentication.
    try:
        return _visible_login_gate(page)
    except Exception:
        return False


def _visible_login_gate(page):
    """Recognize both current query-string and historical path login links."""
    if page is None:
        return False
    return page.evaluate("""() => {
        const visible=e=>e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
        const text=e=>(e?.textContent||'').replace(/\\s+/g,' ').trim();
        const gate=[...document.querySelectorAll('div.m3-card-outlined')].some(card=> {
            const h=card.querySelector('h2.m3-title-large');
            const a=[...card.querySelectorAll('a[href]')].find(a=> {
                const u=new URL(a.getAttribute('href'),location.href);
                return u.origin===location.origin && (u.pathname==='/login'||u.pathname.startsWith('/login/'));
            });
            return visible(card)&&visible(h)&&visible(a)&&text(h)==='Para acceder debe iniciar sesión'
                &&text(a)==='Iniciar sesión'&&!!card.querySelector('a[href="/crear-cuenta"]');
        });
        const form=[...document.querySelectorAll('section[aria-label="Búsqueda por foja, número y año"]')]
            .some(s=>visible(s)&&['#input-fojas','#input-numero','#input-ano'].every(q=>visible(s.querySelector(q))));
        const loginRoute=location.pathname==='/login'||location.pathname.startsWith('/login/');
        const loginForm=loginRoute && [...document.querySelectorAll('form')].some(f=>
            visible(f) && visible(f.querySelector('input[type="email"]')) &&
            visible(f.querySelector('input[type="password"]')) &&
            [...f.querySelectorAll('button')].some(b=>visible(b)&&text(b)==='Iniciar sesión'));
        return (gate || loginForm) && !form;
    }""") is True


def preview_interval(configured):
    return max(1.0, min(float(configured), float(PREVIEW_INTERVAL_SECONDS), 30.0))

def sample_auth(self, *, account_ids=None):
    """Publish fail-closed DOM evidence without performing authentication."""
    for account_id, entry in tuple(self._entries.items()):
        if account_ids is not None and account_id not in account_ids:
            continue
        browser = getattr(entry.scraper, "browser", entry.scraper)
        from .runtime_updates import runtime_module
        quota_policy = runtime_module('form_search')
        try:
            quota_exhausted = quota_policy.portal_dialog_reason(browser.page) == 'daily_limit'
        except Exception:
            quota_exhausted = False  # Disconnected pages cannot supply evidence.
        if quota_exhausted:
            quota_policy.record_quota_hold(self.store.path, account_id)
        detector = getattr(browser, "detect_commerce_auth_state", None)
        if not callable(detector):
            legacy_detector = getattr(browser, "page_requires_login", None)
            if not callable(legacy_detector):
                continue
            detector = lambda: (
                CommerceAuthState.LOGIN_GATE
                if legacy_detector()
                else CommerceAuthState.UNKNOWN
            )
        try:
            raw_state = detector()
            state = (
                raw_state
                if isinstance(raw_state, CommerceAuthState)
                else CommerceAuthState(str(raw_state))
            )
        except Exception:
            state = CommerceAuthState.UNKNOWN
        if state is CommerceAuthState.UNKNOWN:
            try:
                if visible_login_gate(browser.page):
                    state = CommerceAuthState.LOGIN_GATE
            except Exception:
                pass
        rejected_login = False
        rejection_detector = getattr(browser, "has_visible_rejected_login", None)
        if state in {CommerceAuthState.UNKNOWN, CommerceAuthState.LOGIN_GATE} and callable(rejection_detector):
            try:
                rejected_login = bool(rejection_detector())
            except Exception:
                pass
        if state is CommerceAuthState.AUTHENTICATED_FORM:
            entry.authenticated_once = True
            entry.reauth_required = False
        elif state is CommerceAuthState.LOGIN_GATE or rejected_login:
            entry.reauth_required = True
        entry.unknown_checks = (
            entry.unknown_checks + 1
            if state is CommerceAuthState.UNKNOWN
            else 0
        )
        self.store.set_account_browser_state(
            account_id,
            live=True,
            authenticated=state is CommerceAuthState.AUTHENTICATED_FORM,
            headless=self.headless,
            owner=self.worker_id,
            engine=_browser_engine(entry.settings),
            status="login_rejected_visible" if rejected_login else {
                CommerceAuthState.AUTHENTICATED_FORM: "authenticated_form_visible",
                CommerceAuthState.LOGIN_GATE: "login_gate_visible",
                CommerceAuthState.CONFLICT: "authentication_dom_conflict",
                CommerceAuthState.UNKNOWN: "authentication_unknown",
            }[state],
            auth_state=state.value,
            auth_error="temporary_unavailable" if rejected_login else None,
        )
