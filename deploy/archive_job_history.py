"""Recoverably hide named terminal jobs from recent-history lists, not audit data."""
import argparse
from pathlib import Path
from cbrs.jobs import JobStore, utc_now


def archive(store, identifiers):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for identifier in identifiers:
            row=db.execute('SELECT status FROM jobs WHERE job_id=?',(identifier,)).fetchone()
            if not row or row['status'] not in ('failed','cancelled','completed','partial'):
                raise ValueError('Only existing terminal jobs may be archived')
            db.execute('INSERT OR IGNORE INTO archived_jobs VALUES (?,?)',(identifier,utc_now()))
            store._add_event_db(db,identifier,'history_archived',{'recoverable':True})


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('database',type=Path)
    parser.add_argument('job_ids',nargs='+')
    args=parser.parse_args()
    archive(JobStore(args.database),args.job_ids)
    print('Archived history entries:',len(args.job_ids))
