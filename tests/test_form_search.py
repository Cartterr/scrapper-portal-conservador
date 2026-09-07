import json
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from cbrs.form_search import search_fna_form
from cbrs.form_search import portal_dialog_reason
from cbrs.safety import SafetyStopException, StopReason


def modal(message, hidden=False):
    return f'<div id="headlessui-dialog-panel-987" data-headlessui-state="open" style="display:{"none" if hidden else "block"}"><h3>Atención</h3><p>{message}</p><button>Cerrar</button></div>'


@pytest.mark.parametrize('message,hidden,expected', [
    ('Se han agotado las consultas disponibles por hoy.', False, 'daily_limit'),
    ('Se ha detectado un problema, refresque la página e intente nuevamente.', False, 'temporary_unavailable'),
    ('Se han agotado las consultas disponibles por hoy.', True, None),
    ('Un mensaje diferente', False, None),
])
def test_visible_modal_signature(test_chrome, message, hidden, expected):
    page = test_chrome.new_page()
    try:
        page.set_content(modal(message, hidden))
        assert portal_dialog_reason(page) == expected
    finally:
        page.close()


def test_confirmed_generic_modal_one_reload_then_quota_stop(test_chrome):
    page = test_chrome.new_page()
    submits, loads = [], []
    html = HTML.replace("document.querySelector('#results').textContent=JSON.stringify(await res.json());",
        "const data=await res.json(); document.querySelector('#results').innerHTML=data.modal;")
    def serve(route):
        if route.request.url.endswith('/protected'):
            loads.append(1)
            route.fulfill(content_type='text/html; charset=utf-8', body=html)
        elif route.request.url.endswith('/texto'):
            submits.append(1)
            message = ('Se ha detectado un problema, refresque la página e intente nuevamente.'
                       if len(submits)==1 else 'Se han agotado las consultas disponibles por hoy.')
            route.fulfill(status=400, content_type='application/json',
                          body=json.dumps({'code':'intente-mas-tarde', 'modal':modal(message)}))
        else:
            route.fulfill(content_type='application/json', body='{}')
    try:
        page.route('**/*', serve)
        page.goto('http://127.0.0.1:19999/protected')
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
        with pytest.raises(SafetyStopException) as exc:
            search_fna_form(browser,9441,4580,1980,client=None,pace=lambda _:None)
        assert exc.value.reason == StopReason.DAILY_LIMIT
        assert len(submits)==2 and len(loads)==2
        assert not page.is_closed()
    finally:
        page.close()


HTML = '''<section aria-label="Búsqueda por foja, número y año">
<input id="input-fojas"><input id="input-numero"><input id="input-ano">
<button onclick="submitSearch()">Buscar</button><button>Limpiar</button></section>
<div id="results"></div><script>
window.clicks=0;
async function submitSearch(){
 window.clicks++;
 const body={foja:document.querySelector('#input-fojas').value,
 numero:document.querySelector('#input-numero').value,ano:document.querySelector('#input-ano').value};
 await fetch('/api/v1/user/recientes',{method:'POST',body:JSON.stringify(body)});
 const res=await fetch('/api/v1/comercio/indice/texto',{method:'POST',body:JSON.stringify(body)});
 document.querySelector('#results').textContent=JSON.stringify(await res.json());
}
</script>'''


@pytest.fixture(scope='module')
def test_chrome():
    # Separate ephemeral browser; no production profile, port or context attachment.
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize('status,body,expected', [
    (200, [{'ticket': 'test-ticket', 'foja': 9441}], 'success'),
    (200, [], 'success'),
    (400, {'code': 'intente-mas-tarde'}, 'temporary'),
    (200, {'unrelated': True}, 'invalid'),
])
def test_real_form_single_click_and_response_capture(test_chrome, status, body, expected):
    context = test_chrome.new_context()
    requests = []
    try:
        page = context.new_page()
        def serve(route):
            path = route.request.url
            if path.endswith('/protected'):
                route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
            elif path.endswith('/texto'):
                requests.append(route.request.post_data_json)
                route.fulfill(status=status, content_type='application/json', body=json.dumps(body))
            else:
                route.fulfill(content_type='application/json', body='{"history_saved":true}')
        page.route('**/*', serve)
        page.goto('http://127.0.0.1:19999/protected')
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
        calls=[]
        client=SimpleNamespace(post_json=lambda *a, **kw: calls.append(kw))
        operation=lambda: search_fna_form(browser,9441,4580,1980,client=client,pace=lambda _:None)
        if expected == 'temporary':
            with pytest.raises(SafetyStopException) as exc:
                operation()
            assert exc.value.reason == StopReason.TEMPORARY_UNAVAILABLE
        elif expected == 'invalid':
            with pytest.raises(RuntimeError):
                operation()
        else:
            assert operation() == body
        assert requests == [{'foja':'9441','numero':'4580','ano':'1980'}]
        assert page.evaluate('window.clicks') == 1
        assert calls == []
        assert not page.is_closed()
    finally:
        context.close()
