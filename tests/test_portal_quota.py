from datetime import datetime, timedelta, timezone
import sqlite3

from cbrs.form_search import quota_hold, record_quota_hold, admit_quota_check, clear_quota_hold


def test_due_probe_refreshes_stale_modal_and_clears_only_on_acceptance(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cbrs import form_search as policy
    path = tmp_path / 'quota.sqlite3'
    past = datetime.now(timezone.utc)-timedelta(hours=1)
    with policy.quota_db(path) as db:
        db.execute('INSERT INTO portal_quota_holds VALUES(?,?,?,?,?,0)',
                   ('a3',past.isoformat(),None,past.isoformat(),'test'))
    class Page:
        reloaded = 0
        def evaluate(self, script):
            return 'daily_limit' if not self.reloaded else None
        def reload(self, **kwargs):
            self.reloaded += 1
    page = Page()
    browser = SimpleNamespace(settings=SimpleNamespace(account_id='a3'), quota_store_path=path, page=page)
    monkeypatch.setattr(policy, '_search_fna_once', lambda *a, **kw: [])
    monkeypatch.setattr(policy, 'notify_browser_error', lambda *a: None)
    monkeypatch.setattr(policy, 'claim_dialog_reload', lambda *a: False)
    assert policy.search_fna_form(browser,1,2,2000,client=None,pace=None) == []
    assert page.reloaded == 1
    assert quota_hold(path, 'a3') is None


def test_hold_survives_midnight_restart_and_only_one_due_probe(tmp_path):
    path = tmp_path / 'quota.sqlite3'
    first = datetime(2026, 9, 6, 23, 12, 25, tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE jobs(job_id TEXT,finished_at TEXT);'
                         'CREATE TABLE job_attempts(job_id TEXT,account_id TEXT,status TEXT,finished_at TEXT,started_at TEXT);')
        db.execute('INSERT INTO jobs VALUES(?,?)', ('j', first.isoformat()))
        db.execute('INSERT INTO job_attempts VALUES(?,?,?,?,?)', ('j','a3','search_completed',first.isoformat(),first.isoformat()))
    detected = first + timedelta(hours=3)
    hold = record_quota_hold(path, 'a3', now=detected)
    deadline = first + timedelta(hours=24)
    assert hold['next_check_at'] == deadline.isoformat()
    assert not hold['reset_confirmed']
    assert quota_hold(path, 'a2', now=detected) is None
    assert not admit_quota_check(path, 'a3', now=detected)
    assert record_quota_hold(path, 'a3', now=detected+timedelta(hours=2))['next_check_at'] == deadline.isoformat()
    assert quota_hold(path, 'a3', now=deadline-timedelta(seconds=1))['blocked']
    assert admit_quota_check(path, 'a3', now=deadline)
    assert not admit_quota_check(path, 'a3', now=deadline)
    assert quota_hold(path, 'a3', now=deadline)['probe_count'] == 1
    clear_quota_hold(path, 'a3')
    assert quota_hold(path, 'a3') is None
