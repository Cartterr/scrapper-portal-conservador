"""Acceptance contracts exercised against a real, isolated SQLite queue."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from cbrs import Client, DownloadFailed, InvalidInscription, QuotaExhausted, ServiceUnavailable
from cbrs.api import _LocalBackend
from cbrs.config import load_settings
from cbrs.jobs import JobStore, WORKER_LEASE_NAME, validate_pdf


@pytest.fixture
def client(tmp_path):
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings = load_settings({}, root=tmp_path)
    backend.store = JobStore(tmp_path / "pool.sqlite3")
    backend.store.acquire_lease(WORKER_LEASE_NAME, "offline-test")
    value = Client(timeout=0)
    value._backend = backend
    return value


def complete(client, tmp_path, values=(12, 34, 2020), *, empty=False):
    job = client._db.submit(values, False)
    store = client._db.store
    store.add_results(job["job_id"], [] if empty else [{"foja": values[0], "num": values[1], "ano": values[2], "ticket": "fixture"}])
    if not empty:
        item = store.get_job(job["job_id"])["items"][0]
        path = tmp_path / ("original.pdf" if values == (12, 34, 2020) else f"F{values[0]}_N{values[1]}_A{values[2]}.pdf")
        Image.new("RGB", (20, 20), "white").save(path, "PDF")
        digest, size = validate_pdf(path, expected_pages=1)
        store.complete_item(item["item_id"], expected_pages=1, output_path=path, sha256=digest, bytes_count=size)
    store.finalize_job(job["job_id"])
    return job["job_id"]


def test_import_has_no_config_browser_network_or_filesystem_effects():
    code = """
import sys
def audit(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in ('os.mkdir', 'os.remove'):
        raise AssertionError(event)
sys.addaudithook(audit)
from cbrs import Client
Client()
assert 'cbrs.config' not in sys.modules
assert 'playwright' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


def test_cache_output_names_json_and_force(client, tmp_path):
    job_id = complete(client, tmp_path)
    first = client.get(fojas=12, numero=34, ano=2020)
    second = client.get(fojas=12, numero=34, ano=2020)
    assert first.status == second.status == "done"
    assert first.pdf_path == second.pdf_path == tmp_path / "original.pdf"
    assert first.job_id == second.job_id == job_id
    assert client._db.store.summary()["counts"] == {"completed": 1}
    output = client.get(fojas=12, numero=34, ano=2020, output=tmp_path / "export")
    assert output.pdf_path.name == "F12_N34_A2020.pdf"
    assert json.loads(json.dumps(output.to_dict()))["pdf_path"] == str(output.pdf_path)
    named = client.get(fojas=12, numero=34, ano=2020, output=tmp_path / "named.pdf")
    assert named.pdf_path.name == "named.pdf"
    forced = client.get(fojas=12, numero=34, ano=2020, force=True)
    assert forced.job_id != job_id and forced.status == "pending"


def test_not_found_is_terminal_and_timeout_retains_job(client, tmp_path):
    complete(client, tmp_path, empty=True)
    assert client.get(fojas=12, numero=34, ano=2020).status == "not_found"
    first = client.get(fojas=1, numero=2, ano=3)
    assert first.status == "pending"
    assert client.job(first.job_id).status == "pending"
    assert client.get(fojas=1, numero=2, ano=3).job_id == first.job_id


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "abc", "", None])
def test_invalid_inscription_does_not_enqueue(client, value):
    with pytest.raises(InvalidInscription):
        client.get(fojas=value, numero=1, ano=2020)
    assert client._db.store.summary()["counts"] == {}


def test_unavailable_fails_before_enqueue_even_for_cache(client, tmp_path):
    complete(client, tmp_path)
    client._db.store.release_lease(WORKER_LEASE_NAME, "offline-test")
    with pytest.raises(ServiceUnavailable, match=r"cbrs (service start|jobs) worker"):
        client.get(fojas=12, numero=34, ano=2020)
    assert client._db.store.summary()["counts"] == {"completed": 1}


def test_csv_aliases_invalid_rows_duplicates_extra_columns_and_125_rows(client, tmp_path):
    complete(client, tmp_path)
    source = tmp_path / "input.csv"
    source.write_text("foja,número,año,nota\n12,34,2020,listo\n12,34,2020,duplicado\nx,1,2020,inválido\n" +
                      "".join(f"{i},1,2020,pendiente\n" for i in range(100, 225)), encoding="utf-8")
    report = tmp_path / "report.csv"
    results = client.get_batch(source, no_wait=True, report=report)
    assert len(results) == 128
    assert [r.status for r in results[:3]] == ["done", "done", "failed"]
    assert "línea 4" in results[2].error
    assert results[0].job_id == results[1].job_id
    with report.open(encoding="utf-8", newline="") as stream:
        saved = list(csv.DictReader(stream))
    assert saved[1]["nota"] == "duplicado"
    assert saved[2]["foja"] == "x"
    before = client._db.store.summary()
    repeated = client.get_batch(source, no_wait=True, report=report)
    assert [r.job_id for r in repeated] == [r.job_id for r in results]
    assert client._db.store.summary() == before


def test_csv_structure_and_source_preservation(client, tmp_path):
    source = tmp_path / "input.csv"
    source.write_text("foja,fojas,num,year\n1,1,2,2020\n", encoding="utf-8")
    with pytest.raises(InvalidInscription, match="línea 1"):
        client.get_batch(source)
    with pytest.raises(InvalidInscription, match="reporte"):
        client.get_batch(source, report=source)
    assert client._db.store.summary()["counts"] == {}


def test_output_never_overwrites_different_file(client, tmp_path):
    complete(client, tmp_path)
    target = tmp_path / "occupied.pdf"
    target.write_bytes(b"user document")
    with pytest.raises(DownloadFailed):
        client.get(fojas=12, numero=34, ano=2020, output=target)
    assert target.read_bytes() == b"user document"


def test_quota_requires_all_accounts_and_never_confuses_login_failure(client, monkeypatch):
    monkeypatch.setattr(client._db, "status", lambda: {"accounts": [
        {"portal_quota": True, "resume_at": "2030-01-01"},
        {"portal_quota": False, "status": "login_pending"},
    ]})
    assert client.get(fojas=1, numero=2, ano=2020).status == "pending"
    monkeypatch.setattr(client._db, "status", lambda: {"accounts": [
        {"portal_quota": True, "resume_at": "2030-01-01"},
    ]})
    result = client.get(fojas=1, numero=2, ano=2020)
    assert result.status == "pending_quota" and result.resume_at == "2030-01-01"
    with pytest.raises(QuotaExhausted):
        result.raise_for_status()


def test_missing_pdf_is_not_reported_done(client, tmp_path):
    original = complete(client, tmp_path)
    (tmp_path / "original.pdf").unlink()
    result = client.get(fojas=12, numero=34, ano=2020)
    assert result.status == "pending" and result.job_id == original
    assert client._db.store.search_checkpoint(original)["saved"]
    assert client._db.store.get_job(original)["items"][0]["status"] == "pending"


@pytest.mark.parametrize("count", [1, 2, 3, 4, 12])
def test_account_discovery_from_credentials(count, tmp_path):
    from cbrs.account_pool import load_account_pool_config
    env = {}
    for i in range(1, count + 1):
        env[f"CBRS_EJECUTIVO_{i}_USERNAME"] = "fixture"
        env[f"CBRS_EJECUTIVO_{i}_PASSWORD"] = "fixture"
    settings = load_settings(env, root=tmp_path)
    accounts = load_account_pool_config(settings).accounts
    assert len(accounts) == count
    assert len({account.dataimpulse_port for account in accounts}) == count
    assert all(account.proxy_provider == "dataimpulse_mobile_sticky" for account in accounts)


def test_account_discovery_reports_missing_partner(tmp_path):
    from cbrs.account_pool import load_account_pool_config
    settings = load_settings({"CBRS_EJECUTIVO_4_USERNAME": "fixture"}, root=tmp_path)
    with pytest.raises(ValueError, match="CBRS_EJECUTIVO_4_PASSWORD"):
        load_account_pool_config(settings)


def test_cli_result_contracts(client, tmp_path, monkeypatch, capsys):
    from cbrs import download_cli
    from cbrs.cli import main
    monkeypatch.setattr(download_cli, "Client", lambda **kwargs: client)
    complete(client, tmp_path)
    assert main(["get", "--fojas", "12", "--numero", "34", "--ano", "2020", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"
    assert main(["get", "--fojas", "12", "--numero", "34", "--ano", "2020"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "original.pdf")
    client._db.store.release_lease(WORKER_LEASE_NAME, "offline-test")
    assert main(["get", "--fojas", "12", "--numero", "34", "--ano", "2020"]) == 2
    from cbrs.api import service_start_hint
    assert service_start_hint() in capsys.readouterr().err


def test_batch_cli_mixed_outcomes_and_repeat(client, tmp_path, monkeypatch, capsys):
    from cbrs import download_cli
    from cbrs.cli import main
    monkeypatch.setattr(download_cli, "Client", lambda **kwargs: client)
    for number in range(1, 11):
        complete(client, tmp_path, (number, 1, 2020))
    complete(client, tmp_path, (11, 1, 2020), empty=True)
    source = tmp_path / "batch.csv"
    source.write_text("fojas,numero,ano\n" + "".join(f"{i},1,2020\n" for i in range(1, 12)) + "bad,1,2020\n")
    report = tmp_path / "report.csv"
    args = ["get-batch", str(source), "--report", str(report), "--json"]
    for _ in range(2):
        assert main(args) == 1
        summary = json.loads(capsys.readouterr().out)
        assert summary["done"] == 10 and summary["not_found"] == 1 and summary["failed"] == 1
    assert client._db.store.summary()["counts"] == {"completed": 11}


def test_batch_timeout_enqueues_all_before_waiting(client):
    results = client.get_batch([(1, 2, 2020), (2, 3, 2020)], timeout=0)
    assert len(results) == 2 and all(result.status == "pending" and result.job_id for result in results)


def test_production_document_retries_have_no_exhaustion_cap(client, tmp_path):
    from cbrs.runtime_logic import finish_for_review
    job_id = complete(client, tmp_path)
    store = client._db.store
    for _ in range(12):
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE job_id=?", (job_id,))
        assert finish_for_review(store, job_id, "document_retrieval_deferred")
        saved = store.get_job(job_id)
        assert saved["status"] == "waiting_capacity" and saved["next_run_at"]
        assert store.search_checkpoint(job_id)["saved"]


def test_document_retry_does_not_release_a_live_browser_operation(client):
    from cbrs.runtime_logic import finish_for_review
    job = client._db.submit((1, 2, 2020), False)
    store = client._db.store
    store.acquire_lease("browser_operation:" + job["job_id"], "protected-owner")
    assert finish_for_review(store, job["job_id"], "document_retrieval_deferred") is False
    assert store.get_job(job["job_id"])["status"] == "queued"
    assert store.active_lease("browser_operation:" + job["job_id"])["owner"] == "protected-owner"


def test_minimal_credentials_select_mobile_and_optional_solver(tmp_path):
    settings = load_settings({"DATAIMPULSE_PROXY_LOGIN": "fixture"}, root=tmp_path)
    assert settings.egress_mode == "mobile_sticky"
    assert settings.captcha_solver_mode == "browser"
    settings = load_settings({"CBRS_2CAPTCHA_API_KEY": "fixture"}, root=tmp_path)
    assert settings.captcha_solver_mode == "2captcha_fallback"


def test_discovered_accounts_continue_beyond_estimate_but_legacy_limits_remain(tmp_path):
    from dataclasses import replace
    from cbrs.account_pool import AccountPoolStore, load_account_pool_config, local_today
    settings = load_settings({"CBRS_EJECUTIVO_1_USERNAME": "fixture", "CBRS_EJECUTIVO_1_PASSWORD": "fixture"}, root=tmp_path)
    config = load_account_pool_config(settings)
    assert config.enforce_estimated_quota is False
    store = JobStore(tmp_path / "budget.sqlite3")
    pool = AccountPoolStore(store.path)
    pool.create_run(run_id="r", dry_run=False, config=config, dashboard_url=None)
    for i in range(10):
        account = store.select_account(run_id="r", config=config, quota_date=local_today())
        assert account is not None
        job, _ = store.create_job(kind="fna", input_data={"foja": i + 1, "numero": 1, "year": 2020})
        attempt = store.begin_attempt(job_id=job["job_id"], account_id=account.account_id,
                                     quota_date=local_today(), quota=config.search_limit_for(account), run_id="r", consume_quota=True)
        assert attempt
        store.add_results(job["job_id"], [], attempt_id=attempt)
        store.finalize_job(job["job_id"])
    legacy = replace(config, enforce_estimated_quota=True)
    assert store.select_account(run_id="r", config=legacy, quota_date=local_today()) is None
    from cbrs.form_search import record_quota_hold
    record_quota_hold(store.path, account.account_id)
    # The public state is driven by the portal hold, independently of estimates.
    backend = _LocalBackend.__new__(_LocalBackend)
    backend.settings, backend.store = settings, store
    assert backend.status()["accounts"][0]["portal_quota"] is True


def test_config_validate_checks_missing_fields(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from cbrs import operator_cli
    settings = load_settings({
        "CBRS_EJECUTIVO_1_USERNAME": "fixture", "CBRS_EJECUTIVO_1_PASSWORD": "fixture",
    }, root=tmp_path)
    monkeypatch.setattr(operator_cli.config, "load_settings", lambda **kwargs: settings)
    assert operator_cli.cmd_config(SimpleNamespace(config_action="validate")) == 1
    error = capsys.readouterr().err
    assert "DATAIMPULSE_PROXY_LOGIN" in error and "DATAIMPULSE_PROXY_PASSWORD" in error
    assert "fixture" not in error
