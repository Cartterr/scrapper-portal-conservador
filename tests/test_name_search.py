"""Search by company or partner name, end to end, with no portal traffic.

The portal's commerce index shows one of two forms chosen by the "Tipo de
búsqueda" toggle. The name form posts to the same endpoint as FNA with
``origen: texto`` and returns rows that carry a ticket each. A name job keeps
every row, downloads at most the one the selection policy picks, and otherwise
hands the matches back (``needs_selection``) for the caller to choose from and
request with ``get``. Real Chrome renders local HTML only.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from cbrs import Client, SelectionRequired
from cbrs.api import InvalidInscription, _LocalBackend
from cbrs.browser_session import BrowserSession, CommerceAuthState
from cbrs.config import load_settings
from cbrs.form_search import (
    normalize_name,
    portal_history_lists_text,
    search_fna_form,
    search_text_form,
    select_search_mode,
)
from cbrs.jobs import JobStore, WORKER_LEASE_NAME, normalize_job_input, select_matches, validate_pdf
from cbrs.runtime_observation import visible_login_gate
from cbrs.safety import SafetyStopException, StopReason


@pytest.fixture(scope="module")
def chrome():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


# Mirrors the portal markup: a toggle group, two sections of which one is
# rendered, and the SPA posting the typed values with their origin.
PAGE = """<div role="group" aria-label="Tipo de búsqueda">
<button type="button" aria-pressed="true" onclick="mode('fna')">Foja, número y año</button>
<button type="button" aria-pressed="false" onclick="mode('texto')">Razón social o nombre de socio</button>
</div>
<section class="m3-card-outlined" id="sec-fna" aria-label="Búsqueda por foja, número y año">
<input id="input-fojas"><input id="input-numero"><input id="input-ano">
<button onclick="submitFna()">Buscar</button><button>Limpiar</button></section>
<section class="m3-card-outlined" id="sec-texto" aria-label="Búsqueda por razón social o nombre de socio" style="display:none">
<input id="indice-texto" maxlength="50">
<button onclick="submitTexto()">Buscar</button><button>Limpiar</button></section>
<div id="recientes"></div><div id="results"></div>
<script>
function mode(kind){
  document.getElementById('sec-fna').style.display = kind==='fna' ? '' : 'none';
  document.getElementById('sec-texto').style.display = kind==='texto' ? '' : 'none';
  for (const b of document.querySelectorAll('[role=group] button')) b.setAttribute('aria-pressed', String(b.textContent.startsWith(kind==='fna'?'Foja':'Raz')));
}
async function post(body){
  const res=await fetch('/api/v1/comercio/indice/texto',{method:'POST',body:JSON.stringify(body)});
  const data=await res.json();
  document.getElementById('results').textContent = JSON.stringify(data);
  if (body.origen==='texto') {
    const chip=document.createElement('div'); chip.className='m3-chip-reciente';
    chip.setAttribute('data-firma', ['texto','','','',body.texto].join('|')); chip.textContent=body.texto;
    document.getElementById('recientes').appendChild(chip);
  }
}
function submitFna(){ post({foja:q('#input-fojas'),numero:q('#input-numero'),ano:q('#input-ano'),texto:null,origen:'fna'}); }
function submitTexto(){ post({foja:null,numero:null,ano:null,texto:q('#indice-texto'),origen:'texto'}); }
function q(s){ return document.querySelector(s).value; }
</script>"""

ROWS = [
    {"id": "1", "nombreSociedad": "ACME LIMITADA", "foja": 100, "num": 50, "ano": 2019, "acto": "CONSTITUCION", "ticket": "t-1"},
    {"id": "2", "nombreSociedad": "ACME LIMITADA", "foja": 200, "num": 60, "ano": 2021, "acto": "MODIFICACION", "ticket": "t-2"},
    {"id": "3", "nombreSociedad": "ACME HOLDING SPA", "foja": 300, "num": 70, "ano": 2021, "acto": "CONSTITUCION", "ticket": "t-3"},
]


def _open(chrome, rows=ROWS, posts=None):
    page = chrome.new_page()
    posts = posts if posts is not None else []

    def serve(route):
        if route.request.url.endswith("/texto"):
            posts.append(route.request.post_data_json)
            route.fulfill(content_type="application/json", body=json.dumps(rows))
        else:
            route.fulfill(content_type="text/html; charset=utf-8", body=PAGE)

    page.route("**/*", serve)
    page.goto("http://127.0.0.1:19995/protected")
    browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
    return page, browser, posts


# --------------------------------------------------------------------------
# The form
# --------------------------------------------------------------------------


def test_name_search_uses_the_toggle_posts_the_text_and_restores_the_fna_form(chrome):
    page, browser, posts = _open(chrome)
    try:
        assert page.locator("#sec-texto").is_hidden()
        result = search_text_form(browser, "  Acme   Limitada ", client=None, pace=lambda _: None)
        assert result == ROWS
        assert posts == [{"foja": None, "numero": None, "ano": None, "texto": "Acme Limitada", "origen": "texto"}]
        # The page is left on the FNA form: the health sampler's evidence.
        assert page.locator("#sec-fna").is_visible() and page.locator("#sec-texto").is_hidden()
        # And an FNA search right after works without any manual toggle.
        assert search_fna_form(browser, 1, 2, 2000, client=None, pace=lambda _: None) == ROWS
        assert posts[1]["origen"] == "fna" and len(posts) == 2
    finally:
        page.close()


def test_fna_search_switches_back_when_the_page_was_left_in_name_mode(chrome):
    page, browser, posts = _open(chrome)
    try:
        page.evaluate("mode('texto')")
        assert search_fna_form(browser, 9, 8, 1999, client=None, pace=lambda _: None) == ROWS
        assert posts[0]["foja"] == "9" and page.locator("#sec-fna").is_visible()
    finally:
        page.close()


def test_name_history_chip_is_read_from_its_signature(chrome):
    page, browser, _posts = _open(chrome)
    try:
        assert portal_history_lists_text(page, "Acme Limitada") is None  # no chips yet: unknown
        search_text_form(browser, "Acme Limitada", client=None, pace=lambda _: None)
        assert portal_history_lists_text(page, "acme  LIMITADA") is True
        assert portal_history_lists_text(page, "Otra Sociedad") is False
    finally:
        page.close()


def test_name_form_counts_as_the_authenticated_page(chrome, tmp_path):
    page, _browser, _posts = _open(chrome)
    try:
        session = BrowserSession(load_settings({}, root=tmp_path))
        session._context = SimpleNamespace(pages=[page])
        assert session.detect_commerce_auth_state() is CommerceAuthState.AUTHENTICATED_FORM
        select_search_mode(page, "texto")
        assert session.detect_commerce_auth_state() is CommerceAuthState.AUTHENTICATED_FORM
        assert visible_login_gate(page) is False
    finally:
        page.close()


def test_toggle_missing_is_a_safe_not_submitted_stop(chrome):
    page = chrome.new_page()
    try:
        page.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body=PAGE.replace('role="group"', 'role="list"')))
        page.goto("http://127.0.0.1:19995/protected")
        browser = SimpleNamespace(page=page, settings=SimpleNamespace(commerce_url=page.url))
        with pytest.raises(SafetyStopException) as caught:
            search_text_form(browser, "Acme", client=None, pace=lambda _: None)
        assert caught.value.reason is StopReason.SEARCH_NOT_SUBMITTED
    finally:
        page.close()


# --------------------------------------------------------------------------
# Selection policy
# --------------------------------------------------------------------------


def test_normalize_name_matches_the_portal_rules():
    assert normalize_name("  Áçme   LIMITADA ") == "acme limitada"
    assert normalize_name(None) == ""


def test_select_policy_normalization_and_matching():
    assert "select" not in normalize_job_input("text", {"text": "acme"})
    job_input = normalize_job_input("text", {"text": "acme", "select": {"download": True, "ano": "2021", "nombre": " acme  limitada "}})
    assert job_input["select"] == {"download": True, "ano": 2021, "nombre": "acme limitada"}
    with pytest.raises(ValueError):
        normalize_job_input("text", {"text": "acme", "select": {"ano": "x"}})
    with pytest.raises(ValueError):
        normalize_job_input("text", {"text": "acme", "select": "all"})

    assert select_matches({"text": "acme"}, ROWS) is None  # legacy: every row
    assert select_matches({"text": "acme", "select": {"download": False, "ano": None, "nombre": None}}, ROWS) == set()
    assert select_matches({"text": "acme", "select": {"download": True, "ano": None, "nombre": None}}, ROWS) == set()  # 3 matches
    assert select_matches({"text": "acme", "select": {"download": True, "ano": 2019, "nombre": None}}, ROWS) == {1}
    assert select_matches({"text": "acme", "select": {"download": True, "ano": 2021, "nombre": "ACME Limitada"}}, ROWS) == {2}
    assert select_matches({"text": "acme", "select": {"download": True, "ano": 2021, "nombre": None}}, ROWS) == set()  # 2 remain
    assert select_matches({"text": "acme", "select": {"download": True, "ano": 1900, "nombre": None}}, ROWS) == set()


def test_skipped_rows_are_kept_but_never_downloaded_or_counted_as_failures(tmp_path):
    store = JobStore(tmp_path / "pool.sqlite3")
    job, _ = store.create_job(kind="text", input_data={"text": "acme", "select": {"download": True, "ano": 2019}})
    items = store.add_results(job["job_id"], ROWS, download_sequences={1})
    assert [item["status"] for item in items] == ["pending", "skipped", "skipped"]
    assert store.finalize_job(job["job_id"]) == "running"  # the chosen row still downloads
    pdf = tmp_path / "doc.pdf"
    Image.new("RGB", (20, 20), "white").save(pdf, "PDF")
    digest, size = validate_pdf(pdf, expected_pages=1)
    store.complete_item(items[0]["item_id"], expected_pages=1, output_path=pdf, sha256=digest, bytes_count=size)
    assert store.finalize_job(job["job_id"]) == "completed"
    payload = store.get_job(job["job_id"])
    assert payload["document_status"] == "completed" and payload["completed_items"] == 1

    listing, _ = store.create_job(kind="text", input_data={"text": "acme", "select": {"download": False}})
    store.add_results(listing["job_id"], ROWS, download_sequences=set())
    assert store.finalize_job(listing["job_id"]) == "completed"
    assert store.get_job(listing["job_id"])["document_status"] == "selection_required"


# --------------------------------------------------------------------------
# Public API and CLI
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings = load_settings({}, root=tmp_path)
    backend.store = JobStore(tmp_path / "pool.sqlite3")
    backend.store.acquire_lease(WORKER_LEASE_NAME, "offline-test")
    value = Client(timeout=0)
    value._backend = backend
    return value


def _worker_answers(client, job_id, rows, download_sequences, tmp_path=None):
    """Stand in for the worker: save the receipt and, when chosen, the PDF."""
    store = client._db.store
    items = store.add_results(job_id, rows, download_sequences=download_sequences)
    for item in items:
        if item["status"] != "pending":
            continue
        pdf = tmp_path / f"{item['item_id']}.pdf"
        Image.new("RGB", (20, 20), "white").save(pdf, "PDF")
        digest, size = validate_pdf(pdf, expected_pages=1)
        store.complete_item(item["item_id"], expected_pages=1, output_path=pdf, sha256=digest, bytes_count=size)
    store.finalize_job(job_id)


def test_find_lists_matches_and_the_caller_chooses_with_get(client, tmp_path):
    pending = client.find("Acme Limitada")
    assert pending.status == "pending" and pending.query == "Acme Limitada"
    assert client.find("Acme   Limitada").job_id == pending.job_id  # same search, same job
    assert client.find("Acme Limitada", force=True).job_id != pending.job_id

    _worker_answers(client, pending.job_id, ROWS, set(), tmp_path)
    result = client.job(pending.job_id)
    assert result.status == "needs_selection" and result.fojas is None and result.pdf_path is None
    assert [m["numero"] for m in result.matches] == [50, 60, 70]
    assert result.matches[0] == {"foja": 100, "numero": 50, "ano": 2019, "nombre": "ACME LIMITADA", "acto": "CONSTITUCION",
                                 "tipo": None, "personas": None, "folio": None, "id": "1", "selected": False}
    assert "3 coincidencias" in result.error
    with pytest.raises(SelectionRequired) as caught:
        result.raise_for_status()
    assert len(caught.value.matches) == 3
    assert "matches" in result.to_dict() and result.to_dict()["query"] is None  # the job never stores the text publicly


def test_find_with_download_fetches_the_single_filtered_match(client, tmp_path):
    pending = client.find("Acme Limitada", ano=2021, exact=True, download=True)
    _worker_answers(client, pending.job_id, ROWS, select_matches(
        {"select": {"download": True, "ano": 2021, "nombre": "Acme Limitada"}}, ROWS), tmp_path)
    result = client.job(pending.job_id)
    assert result.status == "done" and (result.fojas, result.numero, result.ano) == (200, 60, 2021)
    assert result.pdf_path and result.pdf_path.exists()
    assert [m["selected"] for m in result.matches] == [False, True, False]


def test_find_without_matches_is_not_found_and_bad_queries_never_enqueue(client, tmp_path):
    pending = client.find("Nadie")
    _worker_answers(client, pending.job_id, [], set(), tmp_path)
    assert client.job(pending.job_id).status == "not_found"
    for bad in ("", "   ", "x" * 51):
        with pytest.raises(InvalidInscription):
            client.find(bad)
    with pytest.raises(InvalidInscription):
        client.find("Acme", ano=0)


def test_find_cli_prints_matches_then_the_hint(client, tmp_path, monkeypatch, capsys):
    from cbrs import download_cli
    from cbrs.cli import main
    monkeypatch.setattr(download_cli, "Client", lambda **kwargs: client)
    pending = client.find("Acme")
    _worker_answers(client, pending.job_id, ROWS, set(), tmp_path)
    assert main(["find", "Acme"]) == 0
    out = capsys.readouterr().out
    assert "ACME HOLDING SPA | fojas 300 | numero 70 | ano 2021" in out
    assert "status=needs_selection" in out and "cbrs get --fojas F --numero N --ano A" in out
    assert main(["find", "Acme", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "needs_selection" and len(data["matches"]) == 3 and data["query"] == "Acme"
