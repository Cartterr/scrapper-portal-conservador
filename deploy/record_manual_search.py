"""Record an operator-confirmed search without submitting it or inventing a PDF."""
import argparse
import json
from cbrs.jobs import default_job_store, JobStore
from cbrs.account_pool import local_today, utc_now


def record(store, *, account, foja, numero, ano, receipt, quota_date=None):
    now = utc_now()
    date = quota_date or local_today()
    job_id = 'manual-' + receipt
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM jobs WHERE job_id=?', (job_id,)).fetchone():
            return False
        db.execute('''INSERT INTO jobs(job_id,kind,input_json,status,source,error_code,
                      created_at,updated_at,finished_at) VALUES(?,?,?,'completed','production',?,?,?,?)''',
                   (job_id,'fna',json.dumps({'foja':foja,'numero':numero,'ano':ano}),
                    'operator_reported_search_no_artifact',now,now,now))
        db.execute('''INSERT INTO job_attempts(attempt_id,job_id,account_id,quota_date,
                      quota_consumed,status,started_at,finished_at)
                      VALUES(?,?,?,?,1,'search_completed',?,?)''',
                   (job_id+'-receipt',job_id,account,date,now,now))
        JobStore._sync_account_daily_usage_db(db,account,date)
        store._add_event_db(db,job_id,'manual_search_reported',
                           {'evidence':'operator_report','pdf_created':False},account_id=account)
    return True


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('account','foja','numero','ano','receipt'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--quota-date')
    args=p.parse_args()
    print({'recorded':record(default_job_store(),**vars(args))})
