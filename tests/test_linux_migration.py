import importlib.util
import json
import sqlite3
from pathlib import Path


def test_offline_relocation_preserves_receipts_and_quotas(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('relocate', root / 'deploy/migrate_runtime_paths.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old = r'V:\repo\.cbrs\runtime'
    dbpath = tmp_path / 'pool.sqlite3'
    with sqlite3.connect(dbpath) as db:
        db.execute('create table receipts(id text, path text, payload text, quota integer)')
        db.execute('insert into receipts values(?,?,?,?)', ('accepted', old + r'\outputs\one.pdf', json.dumps({'profile': old + r'\accounts\one'}), 7))
    assert module.migrate(tmp_path, old) == 1
    with sqlite3.connect(dbpath) as db:
        row = db.execute('select * from receipts').fetchone()
    assert row[0] == 'accepted' and row[3] == 7
    assert row[1] == (tmp_path / 'outputs/one.pdf').as_posix()
    assert json.loads(row[2])['profile'] == (tmp_path / 'accounts/one').as_posix()


def test_worker_does_not_own_chrome_lifecycle():
    deploy = Path(__file__).resolve().parents[1] / 'deploy'
    owner = (deploy / 'cbrs-browser-owner.service').read_text()
    worker = (deploy / 'cbrs-worker.service').read_text()
    assert 'Restart=no' in owner
    assert 'PartOf=cbrs-worker' not in owner
    assert 'linux_control.py wait-owner' in worker
    assert 'linux_control.py drain-worker' in worker
    assert 'KillMode=process' in worker
