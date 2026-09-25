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
    reserved = db.execute("""SELECT COUNT(*) FROM job_attempts WHERE account_id=? AND quota_consumed=1
        AND (status='running' OR (safety_stop='search_outcome_unknown'
        AND julianday(COALESCE(finished_at,started_at))>julianday(?)))""",
        (account_id,(now-timedelta(hours=24)).isoformat())).fetchone()[0]
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


# Stops that end a due probe before the portal said anything about quota: a
# rejected CAPTCHA, an unsent search, a lost login or a compromised route (D28).
PROBE_UNANSWERED = frozenset({StopReason.AUTH_REQUIRED, StopReason.SEARCH_NOT_SUBMITTED,
    StopReason.CAPTCHA_REJECTED, StopReason.CAPTCHA_SOLVER, StopReason.TEMPORARY_UNAVAILABLE})


def restore_quota_check(path, account_id, previous_hold):
    """Give back a due probe's admission without overwriting a newer check."""
    if not previous_hold or previous_hold['blocked']:
        return
    with quota_db(path) as db:
        db.execute('UPDATE portal_quota_holds SET next_check_at=?,probe_count=? '
            'WHERE account_id=? AND probe_count=?',
            (previous_hold['next_check_at'], previous_hold['probe_count'],
             account_id, previous_hold['probe_count'] + 1))


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


# Visible portal modals. Matching is by phrase, never by an exact sentence, so
# a trailing period, a heading variant or a second paragraph cannot hide a known
# dialog behind the generic "form could not submit" classification (D17).
CLOSE_BUTTON_TEXTS = ('cerrar', 'aceptar', 'entendido', 'ok', 'continuar')
SEARCH_FAILED_DIALOG = 'search_failed_retry'
UNKNOWN_DIALOG = 'unknown_dialog'


def portal_dialog_evidence(page):
    """Return only recognized modal structure; never capture unrelated page text.

    Every visible modal with a heading and a close button is reported. Known
    messages get their reason; anything else is ``unknown_dialog`` with its
    heading and first line, so an unrecognized portal message is diagnosable
    from the event log instead of surfacing as a mysterious blocked form.
    """
    close_texts = ', '.join(repr(text) for text in CLOSE_BUTTON_TEXTS)
    return page.evaluate('''() => {
        const closeTexts=[''' + close_texts + '''];
        const visible=e=>e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
        const normalize=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/\\s+/g,' ').trim().toLowerCase();
        const clean=s=>(s||'').replace(/\\s+/g,' ').trim();
        const reasons=[];
        const panels=[...document.querySelectorAll('[id^="headlessui-dialog-panel-"][data-headlessui-state~="open"]'), ...document.querySelectorAll('[role="dialog"]')];
        for(const panel of panels){
            if(!visible(panel)) continue;
            const headingEl=[...panel.querySelectorAll('h1,h2,h3,h4')].find(visible);
            const heading=clean(headingEl && headingEl.textContent);
            const closeEl=[...panel.querySelectorAll('button')].find(e=>visible(e)&&closeTexts.includes(normalize(e.textContent)));
            if(!heading || !closeEl) continue;
            const texts=[...panel.querySelectorAll('p')].filter(visible).map(e=>normalize(e.textContent));
            const all=texts.join(' ');
            const nh=normalize(heading);
            let reason='unknown_dialog';
            if(nh==='no se encontraron resultados' && texts.some(t=>/^no se encontraron resultados para la busqueda de texto "[^"]*"[.]?$/.test(t))) reason='empty_results';
            else if(all.includes('se han agotado las consultas') || (all.includes('consultas') && /limite (diario|de consultas)/.test(all))) reason='daily_limit';
            else if(nh==='atencion' && all.includes('se ha detectado un problema') && all.includes('refresque')) reason='temporary_unavailable';
            else if(/no se pudo realizar (la )?busqueda/.test(all)) reason='search_failed_retry';
            reasons.push({reason, panel_selector: panel.id.startsWith('headlessui-dialog-panel-')
                ? '[id^="headlessui-dialog-panel-"][data-headlessui-state~="open"]' : '[role="dialog"]',
                panel_id: panel.id || null, heading, close_button: clean(closeEl.textContent),
                message_element: 'p', message: (texts[0]||'').slice(0,160)});
        }
        return reasons.find(e=>e.reason==='daily_limit') || reasons.find(e=>e.reason!=='unknown_dialog') || reasons[0] || null;
    }''')


def portal_dialog_reason(page):
    """Match visible content without depending on generated numeric IDs."""
    evidence = portal_dialog_evidence(page)
    return evidence['reason'] if evidence else None


PORTAL_ERROR_DIALOG = 'temporary_unavailable'


def portal_error_dialog_stop(evidence, *, message, status=None, response_code=None, after_submission):
    """Build the compromised-route stop carrying the recognized dialog signature.

    The operator confirmed that the same account and search work from a clean
    network, so this exact visible dialog is treated as a compromised proxy
    exit: the caller must stop using that browser/route, not refresh and retry.
    """
    stop = SafetyStopException(StopReason.TEMPORARY_UNAVAILABLE, message, status=status,
                               context='commerce form search', response_code=response_code)
    stop.portal_dialog = {**evidence, 'after_submission': bool(after_submission),
                          'verdict': 'proxy_compromised'}
    return stop


def dismiss_dialog(page, evidence, *, timeout_ms=5000):
    """Close only the exact recognized panel; refuse ambiguous matches."""
    selector = evidence['panel_selector']
    panel_id = evidence.get('panel_id')
    if panel_id and panel_id.startswith('headlessui-dialog-panel-') and all(c.isalnum() or c in '-_:' for c in panel_id):
        selector = '[id="' + panel_id + '"]'
    panel = page.locator(selector).filter(
        has=page.get_by_role('heading', name=evidence['heading'], exact=True)
    ).filter(visible=True)
    if panel.count() != 1:
        raise RuntimeError('Ambiguous portal dialog; nothing dismissed, no search submitted')
    panel.get_by_role('button', name=evidence['close_button'], exact=True).click(timeout=timeout_ms)
    panel.wait_for(state='hidden', timeout=timeout_ms)
    return True


def dismiss_previous_empty_dialog(page):
    """Dismiss only the recognized old empty modal; never infer this job's result."""
    evidence = portal_dialog_evidence(page)
    if not evidence or evidence['reason'] != 'empty_results':
        return False
    return dismiss_dialog(page, evidence)


def portal_recent_searches(page):
    """Parse the portal's own "Recientes" panel (its last 10 searches) into tuples.

    Returns ``None`` when the panel is not on the page, so callers can tell
    "not listed" from "could not check". Only foja/numero/ano triples are read.
    """
    try:
        found = page.evaluate('''() => {
            const visible=e=>e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
            const normalize=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/\\s+/g,' ').trim().toLowerCase();
            // Preferred: the portal stamps every chip with a structured signature
            // (data-firma="fna|foja|numero|ano|"); the text below is the fallback.
            const firmas=[...document.querySelectorAll('[data-firma^="fna|"]')]
                .map(e=>(e.getAttribute('data-firma')||'').split('|'))
                .filter(p=>p.length>=4 && /^\\d+$/.test(p[1]) && /^\\d+$/.test(p[2]) && /^\\d{4}$/.test(p[3]))
                .map(p=>[Number(p[1]),Number(p[2]),Number(p[3])]);
            if(firmas.length) return {found:true, entries:firmas, source:'data-firma'};
            const titles=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,span,p,div,button,legend')]
                .filter(e=>visible(e) && e.children.length<=1 && normalize(e.textContent)==='recientes');
            for(const title of titles){
                let node=title;
                for(let depth=0; depth<8 && node; depth++, node=node.parentElement){
                    const text=normalize(node.innerText||node.textContent);
                    // The panel header alone also says "Borrar historial": keep
                    // climbing until the chips or the panel footer are included,
                    // so an empty parse never passes for an empty history.
                    if(!text.includes('borrar historial')) continue;
                    const entries=[...text.matchAll(/foja\\s*(\\d+)\\s*[^\\da-z]{1,3}\\s*n[°ºo.]*\\s*(\\d+)\\s*[^\\da-z]{1,3}\\s*(\\d{4})(?!\\d)/g)]
                        .map(m=>[Number(m[1]),Number(m[2]),Number(m[3])]);
                    if(entries.length || text.includes('se conservan las ultimas')){
                        return {found:true, entries};
                    }
                }
            }
            return {found:false, entries:[]};
        }''')
    except Exception:
        return None
    if not found or not found.get('found'):
        return None
    # Each chip also carries a screen-reader "Quitar ..." label: keep first occurrences only.
    return list(dict.fromkeys(tuple(int(value) for value in entry) for entry in found.get('entries', [])))


def portal_history_lists(page, foja, numero, ano):
    """True/False when the Recientes panel is readable, None when it is not."""
    entries = portal_recent_searches(page)
    if entries is None:
        return None
    return (int(foja), int(numero), int(ano)) in entries


def _history_verdict(page, values, *, evidence):
    """Decide whether an unobserved outcome was registered by the portal.

    Returns ``(listed, stop)``. ``stop`` is the retry-safe
    ``SEARCH_NOT_SUBMITTED`` when the portal itself proves nothing was
    registered: its "No se pudo realizar búsqueda" modal, or a readable
    "Recientes" history that does not list the tuple (D22/D25). A listed
    tuple, or an unreadable history without that modal, keeps the outcome
    unknown so reconciliation stays explicit and quota is never charged twice.
    """
    listed = portal_history_lists(page, values['foja'], values['numero'], values['ano'])
    failed_dialog = bool(evidence and evidence['reason'] == SEARCH_FAILED_DIALOG)
    if failed_dialog:
        try:
            dismiss_dialog(page, evidence)
        except Exception:
            pass
    if listed is True or (listed is None and not failed_dialog):
        return listed, None
    stop = SafetyStopException(
        StopReason.SEARCH_NOT_SUBMITTED,
        'Portal did not register the search (failure dialog and/or absent from Recientes); '
        'no quota consumed, retry allowed',
        context='commerce form search')
    stop.portal_dialog = dict(evidence) if failed_dialog else None
    stop.portal_history = listed
    return listed, stop


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
    due_probe = bool(previous_hold and not previous_hold['blocked'])
    try:
        initial = portal_dialog_evidence(browser.page)
        if initial and initial['reason'] == 'empty_results':
            # This predates our submission and belongs to a previous query. The
            # portal uses the literal text "null" even for tuple searches.
            from .jobs import JobStore
            try:
                dismiss_previous_empty_dialog(browser.page)
            except Exception as exc:
                if path:
                    JobStore(path).add_event('previous_empty_dialog_dismiss_failed', account_id=account_id,
                        data={'error_type': type(exc).__name__})
                raise
            if path:
                JobStore(path).add_event('previous_empty_dialog_dismissed', account_id=account_id,
                    data={'panel_selector': initial['panel_selector'], 'panel_id': initial.get('panel_id'),
                          'new_search_result_inferred': False})
        if initial and initial['reason'] == PORTAL_ERROR_DIALOG:
            # The portal error dialog is already open on this route. It is never
            # refreshed away: the exit is compromised and another account must take
            # this search while the route is replaced.
            stop = portal_error_dialog_stop(initial, after_submission=False,
                message='Portal error dialog visible before submission; route compromised, no search submitted')
            notify_browser_error(browser, stop)
            raise stop
        if due_probe:
            # A due probe has its own atomic hourly admission; render a fresh page
            # so yesterday's daily-limit modal cannot mask the outcome.
            browser.page.reload(wait_until='domcontentloaded', timeout=60000)
        if portal_dialog_reason(browser.page) == 'daily_limit':
            raise SafetyStopException(StopReason.DAILY_LIMIT, 'Portal daily quota exhausted', context='commerce form search')
        result = _search_fna_once(browser, foja, numero, ano, client=client, pace=pace)
        if path:
            clear_quota_hold(path, account_id)
        return result
    except SafetyStopException as exc:
        if exc.reason in PROBE_UNANSWERED and due_probe and path:
            restore_quota_check(path, account_id, previous_hold)
        if exc.reason == StopReason.DAILY_LIMIT:
            if path:
                record_quota_hold(path, account_id)
            notify_browser_error(browser, exc)
            raise
        if exc.reason == StopReason.SEARCH_NOT_SUBMITTED and path and hasattr(exc, 'portal_history'):
            from .jobs import JobStore
            JobStore(path).add_event('search_not_registered', account_id=account_id, data={
                'portal_history_listed': exc.portal_history,
                'failure_dialog': bool(getattr(exc, 'portal_dialog', None)),
                'request_dispatched': bool(getattr(exc, 'request_dispatched', False)),
                'foja': foja, 'numero': numero, 'ano': ano})
        if exc.reason == StopReason.SEARCH_NOT_SUBMITTED and path and getattr(exc, 'blocking_dialog', None):
            from .jobs import JobStore
            JobStore(path).add_event('search_form_blocked_by_dialog', account_id=account_id,
                level='warning', data=dict(exc.blocking_dialog))
        if exc.reason == StopReason.TEMPORARY_UNAVAILABLE and not getattr(exc, 'portal_dialog', None):
            # A definitive matching rejection plus its visible modal identifies
            # a compromised exit. No reload, no same-search replay: the caller
            # hands the search to another account and replaces this route.
            evidence = portal_dialog_evidence(browser.page)
            if evidence and evidence['reason'] == PORTAL_ERROR_DIALOG:
                stop = portal_error_dialog_stop(evidence, after_submission=True,
                    status=exc.status, response_code=exc.response_code,
                    message='Portal error dialog after a confirmed rejection; route compromised, no replay')
                notify_browser_error(browser, stop)
                raise stop from exc
        raise
    except Exception as exc:
        if path and hasattr(exc, 'portal_history'):
            from .jobs import JobStore
            JobStore(path).add_event('search_outcome_unknown_history', account_id=account_id,
                level='warning', data={'portal_history_listed': exc.portal_history,
                                       'foja': foja, 'numero': numero, 'ano': ano})
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
        raise SafetyStopException(StopReason.SEARCH_NOT_SUBMITTED,
            "Protected search route is not open; no request submitted", context='commerce form search')
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
    clicked = False
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
        # From this point onward a browser-side click may have reached the
        # portal even if Playwright later reports a timeout or lost response.
        # Mark it first so no exception can silently replay the query on a
        # sibling account without explicit reconciliation.
        clicked = True
        form.get_by_role('button', name='Buscar', exact=True).click(timeout=10000)
        import time
        submission_timeout = float(
            getattr(browser.settings, "search_submission_timeout_seconds", 90.0)
        )
        response_timeout = float(
            getattr(browser.settings, "search_response_timeout_seconds", 90.0)
        )
        submission_deadline = time.monotonic() + submission_timeout
        response_deadline = None
        next_dialog_check = time.monotonic() + 1.0
        while not observed:
            if not submitted and runtime_module('runtime_observation').visible_login_gate(page):
                raise SafetyStopException(StopReason.AUTH_REQUIRED,
                    'Session expired before any commerce search request', context='commerce form search')
            if not submitted:
                visible = urlsplit(page.url)
                if (visible.scheme, visible.netloc, visible.path) != (
                    origin.scheme, origin.netloc, origin.path
                ):
                    raise SafetyStopException(
                        StopReason.SEARCH_NOT_SUBMITTED,
                        'Portal left the protected search route before dispatching the commerce request',
                        context='commerce form search',
                    )
            if time.monotonic() >= next_dialog_check:
                # The portal reports a failed dispatch or an exhausted quota with
                # its own modal instead of a response we could observe.
                next_dialog_check = time.monotonic() + 1.0
                try:
                    evidence = portal_dialog_evidence(page)
                except Exception:
                    evidence = None
                if evidence and evidence['reason'] == 'daily_limit':
                    raise SafetyStopException(StopReason.DAILY_LIMIT, 'Portal daily quota exhausted',
                                              context='commerce form search')
                if evidence and evidence['reason'] == SEARCH_FAILED_DIALOG:
                    _listed, stop = _history_verdict(page, values, evidence=evidence)
                    if stop is not None:
                        stop.request_dispatched = bool(submitted)
                        raise stop
            if submitted and response_deadline is None:
                response_deadline = time.monotonic() + response_timeout
            if not submitted and time.monotonic() >= submission_deadline:
                raise TimeoutError(
                    'Portal commerce submission remained unconfirmed after the single click; '
                    'no automatic replay'
                )
            if response_deadline is not None and time.monotonic() >= response_deadline:
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
    except SafetyStopException as exc:
        notify_browser_error(browser, exc)
        raise
    except Exception as exc:
        if not submitted and runtime_module('runtime_observation').visible_login_gate(page):
            auth = SafetyStopException(StopReason.AUTH_REQUIRED,
                'Refreshed page rendered login gate before search submission', context='commerce form search')
            notify_browser_error(browser, auth)
            raise auth from exc
        notify_browser_error(browser, exc)
        try:
            evidence = portal_dialog_evidence(page)
        except Exception:
            evidence = None
        if not submitted and not clicked:
            # The dialog can open between the pre-submission check and the
            # first keystroke. It then blocks the React-controlled inputs and
            # the field read-back fails. Nothing was submitted, so classify by
            # the modal that blocked the form instead of handing the same
            # route another query later (D17).
            if evidence and evidence['reason'] == PORTAL_ERROR_DIALOG:
                stop = portal_error_dialog_stop(evidence, after_submission=False,
                    message='Portal error dialog blocked the search form; route compromised, nothing submitted')
                notify_browser_error(browser, stop)
                raise stop from exc
            if evidence and evidence['reason'] == 'daily_limit':
                raise SafetyStopException(StopReason.DAILY_LIMIT,
                    'Portal daily-limit dialog blocked the search form; nothing submitted',
                    context='commerce form search') from exc
            if evidence and evidence['close_button'].strip().lower() == 'cerrar':
                # A retryable or unrecognized portal modal: close it so the route
                # is usable on the next pass, and report what blocked the form.
                try:
                    dismiss_dialog(page, evidence)
                except Exception:
                    pass
            stop = SafetyStopException(StopReason.SEARCH_NOT_SUBMITTED,
                'Form could not submit a commerce request; alternate account required',
                context='commerce form search')
            stop.blocking_dialog = dict(evidence) if evidence else None
            raise stop from exc
        # Clicked (and maybe dispatched) with no observable outcome. The portal's
        # own history decides: absent means nothing was registered and the job
        # may continue elsewhere; listed or unreadable keeps the outcome unknown
        # for explicit reconciliation. A timeout never causes another click or
        # API replay.
        listed, stop = _history_verdict(page, values, evidence=evidence)
        if stop is not None:
            stop.request_dispatched = bool(submitted)
            raise stop from exc
        exc.portal_history = listed
        raise
    finally:
        page.remove_listener('request', request_sent)
        page.remove_listener('response', response_received)
