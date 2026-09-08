"""Explicit document-only recovery; never repeat an accepted commerce search."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job_ids', nargs='+')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from dotenv import dotenv_values
    from cbrs.paths import prepare_environment
    os.environ.update(prepare_environment({**os.environ, **dotenv_values(ROOT / '.env')}, ROOT))
    from cbrs.config import SETTINGS
    from cbrs.jobs import default_job_store, utc_now
    from cbrs.owner_protocol import command_path
    store = default_job_store()
    for job_id in args.job_ids:
        job = store.get_job(job_id)
        if (not job or job['status'] != 'failed' or job['error_code'] not in
                {'document_retrieval_deferred', 'document_recovery_exhausted'}
                or job['cancel_requested'] or not store.search_checkpoint(job_id)['saved']):
            raise ValueError('Only named deferred document jobs with an accepted receipt can resume')
        with sqlite3.connect(f'{command_path(SETTINGS).as_uri()}?mode=ro', uri=True) as db:
            if db.execute("SELECT 1 FROM owner_commands WHERE job_id=? AND state IN ('queued','running')", (job_id,)).fetchone():
                raise RuntimeError('Existing owner command must finish first')
        if args.apply:
            with store.connect() as db:
                changed = db.execute("""UPDATE jobs SET status='queued',finished_at=NULL,
                    next_run_at=?,updated_at=?,worker_owner=NULL,lease_expires_at=NULL
                    WHERE job_id=? AND status='failed' AND cancel_requested=0""",
                    (utc_now(), utc_now(), job_id)).rowcount
                if changed:
                    store._add_event_db(db, job_id, 'document_resume_requested',
                        {'accepted_search_preserved': True})
        print(json.dumps({'job_id': job_id, 'applied': args.apply}))


if __name__ == '__main__':
    main()
