import sqlite3
from datetime import datetime, timezone, timedelta
from cbrs.form_search import account_window


def test_fixed_windows_survive_midnight_restart_and_expire_exactly(tmp_path):
    path=tmp_path/'q.db'
    first=datetime(2026,9,6,23,12,25,tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE jobs(job_id TEXT,source TEXT); CREATE TABLE job_attempts(job_id TEXT,account_id TEXT,quota_consumed INTEGER,status TEXT,finished_at TEXT,started_at TEXT,safety_stop TEXT);')
        for i,(account,offset,status) in enumerate([('a',0,'search_completed'),('a',4,'search_completed'),('a',5,'failed'),('b',6,'search_completed')]):
            stamp=(first+timedelta(hours=offset)).isoformat()
            db.execute('INSERT INTO jobs VALUES(?,?)',(str(i),'endurance'))
            db.execute('INSERT INTO job_attempts VALUES(?,?,?,?,?,?,NULL)',(str(i),account,1,status,stamp,stamp))
    assert account_window(path,'a',now=first+timedelta(hours=10))['used']==2
    assert account_window(path,'a',now=first+timedelta(hours=23,minutes=59))['used']==2
    assert account_window(path,'a',now=first+timedelta(hours=24))['used']==0
    assert account_window(path,'b',now=first+timedelta(hours=24))['used']==1
    assert account_window(path,'a',now=first+timedelta(hours=10))['resets_at']==(first+timedelta(hours=24)).isoformat()
