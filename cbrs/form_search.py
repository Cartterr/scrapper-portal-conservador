"""Use the portal's own FNA form handler, without replaying its browser request."""
from urllib.parse import urlsplit
from types import SimpleNamespace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

from .error_evidence import notify_browser_error
from .safety import SafetyStopException, StopReason, ensure_safe_response


def account_window_db(db, account_id, *, now=None):
    """Fixed 24h windows anchored by accepted searches, reconstructed from receipts.

    Calendar dates remain historical reporting fields, never reset instructions.
    UTC arithmetic makes windows exactly 24 hours across DST and restarts.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.execute('''SELECT COALESCE(a.finished_at,a.started_at),j.source
        FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id
        WHERE a.account_id=? AND a.quota_consumed=1
          AND a.status IN ('search_completed','completed')
        ORDER BY julianday(COALESCE(a.finished_at,a.started_at)),a.rowid''', (account_id,)).fetchall()
    start = end = None
    used = 0
    sources = {}
    for stamp, source in rows:
        accepted = datetime.fromisoformat(stamp)
        if accepted.tzinfo is None:
            accepted = accepted.replace(tzinfo=timezone.utc)
        if accepted > now:
            continue
        if end is None or accepted >= end:
            start, end = accepted, accepted + timedelta(hours=24)
            used, sources = 0, {}
        used += 1
        sources[source] = sources.get(source, 0) + 1
    if end is not None and now >= end:
        start = end = None
        used, sources = 0, {}
    reserved = db.execute("SELECT COUNT(*) FROM job_attempts WHERE account_id=? AND quota_consumed=1 AND status='running'", (account_id,)).fetchone()[0]
    return {'used':used,'reserved':reserved,'source_used':sources,
            'started_at':start.isoformat() if start else None,
            'resets_at':end.isoformat() if end else None,'policy':'first_success_24h'}


def account_window(path, account_id, *, now=None):
    with sqlite3.connect(path, timeout=30) as db:
        return account_window_db(db, account_id, now=now)


@contextmanager
def quota_db(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS portal_quota_holds(
            account_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL,
            first_success_at TEXT, next_check_at TEXT NOT NULL,
            evidence TEXT NOT NULL, probe_count INTEGER NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TABLE IF NOT EXISTS portal_search_reloads(
            job_id TEXT PRIMARY KEY, used_at TEXT NOT NULL)''')
        db.commit()
        yield db
        db.commit()
    finally:
        db.close()


def quota_hold(path, account_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    with quota_db(path) as db:
        row = db.execute('SELECT * FROM portal_quota_holds WHERE account_id=?', (account_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['blocked'] = datetime.fromisoformat(result['next_check_at']) > now
    result['reset_confirmed'] = False
    return result


def record_quota_hold(path, account_id, *, evidence='visible_portal_daily_limit', now=None):
    now = now or datetime.now(timezone.utc)
    with quota_db(path) as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM portal_quota_holds WHERE account_id=?', (account_id,)).fetchone()
        # Repeated passive observations must not push the deadline forever.
        if old:
            return dict(old)
        row = db.execute('''SELECT MIN(COALESCE(j.finished_at,a.finished_at,a.started_at))
            FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id
            WHERE a.account_id=? AND a.status IN ('search_completed','completed')
            AND julianday(COALESCE(j.finished_at,a.finished_at,a.started_at))>=julianday(?)''',
            (account_id, (now-timedelta(hours=24)).isoformat())).fetchone()
        first = row[0] if row and row[0] else None
        estimate = datetime.fromisoformat(first) + timedelta(hours=24) if first else now + timedelta(hours=24)
        estimate = max(estimate, now + timedelta(minutes=1))
        db.execute('INSERT INTO portal_quota_holds VALUES(?,?,?,?,?,0)',
            (account_id, now.isoformat(), first, estimate.isoformat(), evidence))
    return quota_hold(path, account_id, now=now)


def admit_quota_check(path, account_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    with quota_db(path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT next_check_at FROM portal_quota_holds WHERE account_id=?', (account_id,)).fetchone()
        if not row:
            return True
        if datetime.fromisoformat(row[0]) > now:
            return False
        # One cautious probe when due, not an assumed restored balance.
        db.execute('UPDATE portal_quota_holds SET next_check_at=?,probe_count=probe_count+1 WHERE account_id=?',
                   ((now+timedelta(hours=1)).isoformat(), account_id))
        return True


def clear_quota_hold(path, account_id):
    with quota_db(path) as db:
        db.execute('DELETE FROM portal_quota_holds WHERE account_id=?', (account_id,))


def browser_quota_path(browser):
    if not getattr(browser.settings, 'account_id', None):
        return None
    if getattr(browser, 'quota_store_path', None):
        return browser.quota_store_path
    from .config import SETTINGS
    return SETTINGS.profile_dir.parent / 'pool' / 'pool.sqlite3'


def portal_dialog_reason(page):
    """Match open, visible content; generated HeadlessUI numeric IDs can vary."""
    return page.evaluate('''() => {
        const visible=e=>e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
        const normalize=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/\\s+/g,' ').trim().toLowerCase();
        const reasons=[];
        for(const panel of document.querySelectorAll('[id^="headlessui-dialog-panel-"][data-headlessui-state~="open"], [role="dialog"]')){
            if(!visible(panel)) continue;
            const heading=[...panel.querySelectorAll('h2,h3')].some(e=>visible(e)&&normalize(e.textContent)==='atencion');
            const close=[...panel.querySelectorAll('button')].some(e=>visible(e)&&normalize(e.textContent)==='cerrar');
            if(!heading || !close) continue;
            const texts=[...panel.querySelectorAll('p')].filter(visible).map(e=>normalize(e.textContent));
            if(texts.includes('se han agotado las consultas disponibles por hoy.')) reasons.push('daily_limit');
            if(texts.includes('se ha detectado un problema, refresque la pagina e intente nuevamente.')) reasons.push('temporary_unavailable');
        }
        return reasons.includes('daily_limit') ? 'daily_limit' : reasons[0] || null;
    }''')


def claim_dialog_reload(browser):
    path = browser_quota_path(browser)
    if path is None:
        return True  # isolated test browser; no durable production job
    with quota_db(path) as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT job_id FROM job_attempts WHERE account_id=? AND status='running' AND quota_consumed=1",
                          (browser.settings.account_id,)).fetchall()
        if len(rows) != 1:
            return False
        return db.execute('INSERT OR IGNORE INTO portal_search_reloads VALUES(?,?)',
            (rows[0][0], datetime.now(timezone.utc).isoformat())).rowcount == 1


def search_fna_form(browser, foja, numero, ano, *, client, pace):
    from .runtime_updates import runtime_module
    if runtime_module('runtime_observation').visible_login_gate(browser.page):
        raise SafetyStopException(StopReason.AUTH_REQUIRED,
            'Visible login gate before quota admission', context='commerce form search')
    path = browser_quota_path(browser)
    account_id = getattr(browser.settings, 'account_id', None)
    previous_hold = quota_hold(path, account_id) if path else None
    if path and not admit_quota_check(path, account_id):
        raise SafetyStopException(StopReason.DAILY_LIMIT, 'Portal quota hold remains active', context='commerce form search')
    reloaded = False
    initial_dialog = portal_dialog_reason(browser.page)
    due_probe = bool(previous_hold and not previous_hold['blocked'])
    if due_probe or initial_dialog == 'temporary_unavailable':
        # A due probe has its own atomic hourly admission. A historical job's
        # already-used generic retry must not trap it behind yesterday's modal.
        if due_probe or claim_dialog_reload(browser):
            notify_browser_error(browser, SafetyStopException(StopReason.TEMPORARY_UNAVAILABLE,
                                 'Visible portal dialog before bounded refresh', context='commerce form search'))
            browser.page.reload(wait_until='domcontentloaded', timeout=60000)
            reloaded = True
    for attempt in range(2):
        try:
            if portal_dialog_reason(browser.page) == 'daily_limit':
                raise SafetyStopException(StopReason.DAILY_LIMIT, 'Portal daily quota exhausted', context='commerce form search')
            result = _search_fna_once(browser, foja, numero, ano, client=client, pace=pace)
            if path:
                clear_quota_hold(path, account_id)
            return result
        except SafetyStopException as exc:
            if exc.reason == StopReason.AUTH_REQUIRED and due_probe and path:
                # Admission reserved a probe, but the refreshed page never
                # submitted it. Preserve the historical hold and restore its
                # eligibility, without overwriting a newer concurrent check.
                with quota_db(path) as db:
                    db.execute('UPDATE portal_quota_holds SET next_check_at=?,probe_count=? '
                        'WHERE account_id=? AND probe_count=?',
                        (previous_hold['next_check_at'], previous_hold['probe_count'],
                         account_id, previous_hold['probe_count'] + 1))
            if exc.reason == StopReason.DAILY_LIMIT:
                if path:
                    record_quota_hold(path, account_id)
                notify_browser_error(browser, exc)
                raise
            # Only a definitive matching rejection plus its visible modal can
            # trigger one reload. Timeouts/unknown outcomes never replay search.
            if (attempt == 0 and not reloaded and exc.reason == StopReason.TEMPORARY_UNAVAILABLE
                    and portal_dialog_reason(browser.page) == 'temporary_unavailable'
                    and claim_dialog_reload(browser)):
                notify_browser_error(browser, exc)
                browser.page.reload(wait_until='domcontentloaded', timeout=60000)
                reloaded = True
                continue
            raise


def _search_fna_once(browser, foja, numero, ano, *, client, pace):
    page = browser.page
    from .runtime_updates import runtime_module
    if runtime_module('runtime_observation').visible_login_gate(page):
        raise SafetyStopException(StopReason.AUTH_REQUIRED,
            'Visible login gate before search submission', context='commerce form search')
    origin = urlsplit(browser.settings.commerce_url)
    current = urlsplit(page.url)
    if (current.scheme, current.netloc, current.path) != (origin.scheme, origin.netloc, origin.path):
        raise RuntimeError("Protected search route is not open; no automatic navigation performed")
    values = {"foja": str(foja), "numero": str(numero), "ano": str(ano)}

    def matches(response):
        url = urlsplit(response.url)
        if (url.scheme, url.netloc, url.path) != (origin.scheme, origin.netloc, '/api/v1/comercio/indice/texto'):
            return False
        if response.request.method != 'POST':
            return False
        try:
            body = response.request.post_data_json
            return all(str(body.get(key)) == value for key, value in values.items())
        except Exception:
            return False

    submitted = False
    observed = []
    def request_sent(request):
        nonlocal submitted
        url = urlsplit(request.url)
        if (url.scheme, url.netloc, url.path) == (origin.scheme, origin.netloc, '/api/v1/comercio/indice/texto') and request.method == 'POST':
            submitted = True
    def response_received(response):
        if matches(response):
            observed.append(response)
    page.on('request', request_sent)
    page.on('response', response_received)
    try:
        form = page.locator('section[aria-label="Búsqueda por foja, número y año"]')
        form.wait_for(state='visible', timeout=10000)
        for selector, value in (("#input-fojas", foja), ("#input-numero", numero), ("#input-ano", ano)):
            field = form.locator(selector)
            field.fill(str(value), timeout=10000)
            if field.input_value() != str(value):
                raise RuntimeError("Search field read-back mismatch")
        pace("commerce form search")
        # Listeners are armed before the single click. A click is not evidence
        # that a search request left the browser: expired sessions redirect
        # during the application's auth check, before its commerce POST.
        form.get_by_role('button', name='Buscar', exact=True).click(timeout=10000)
        import time
        deadline = time.monotonic() + 90
        while not observed:
            if not submitted and runtime_module('runtime_observation').visible_login_gate(page):
                raise SafetyStopException(StopReason.AUTH_REQUIRED,
                    'Session expired before any commerce search request', context='commerce form search')
            if time.monotonic() >= deadline:
                raise TimeoutError('Commerce search response was not observed; no automatic replay')
            page.wait_for_timeout(100)
        response = observed[0]
        captured = SimpleNamespace(status=response.status, headers=response.all_headers(), body_text=response.text())
        try:
            ensure_safe_response(captured.status, captured.headers, captured.body_text, context="commerce form search")
        except SafetyStopException as exc:
            notify_browser_error(browser, exc)
            # Give the framework one bounded render window after the response.
            if exc.reason in {StopReason.TEMPORARY_UNAVAILABLE, StopReason.DAILY_LIMIT}:
                page.wait_for_timeout(300)
                if portal_dialog_reason(page) == 'daily_limit':
                    raise SafetyStopException(StopReason.DAILY_LIMIT, 'Portal daily quota exhausted',
                                              status=captured.status, context='commerce form search') from exc
            if exc.reason != StopReason.CAPTCHA_REJECTED:
                raise
            # Reuse the observed rejection with the existing bounded solver chain.
            # Do NOT submit a second browser-token request first.
            result = client.post_json(
                '/api/v1/comercio/indice/texto', response.request.post_data_json,
                captcha_action='indice_com_texto', include_recaptcha_in_body=True,
                context='commerce form search', _initial_response=captured,
            )
        else:
            result = response.json()
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise RuntimeError("Search response did not contain a result list")
        return result
    except Exception as exc:
        if not submitted and runtime_module('runtime_observation').visible_login_gate(page):
            auth = SafetyStopException(StopReason.AUTH_REQUIRED,
                'Refreshed page rendered login gate before search submission', context='commerce form search')
            notify_browser_error(browser, auth)
            raise auth from exc
        notify_browser_error(browser, exc)
        # In particular, a timeout never causes another click or API replay.
        raise
    finally:
        page.remove_listener('request', request_sent)
        page.remove_listener('response', response_received)
