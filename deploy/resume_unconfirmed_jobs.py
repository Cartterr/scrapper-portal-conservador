"""Explicitly resume named unconfirmed jobs after portal-history review."""
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
    file_env = {
        key: value
        for key, value in dotenv_values(ROOT / '.env').items()
        if value is not None
    }
    os.environ.update(prepare_environment({**file_env, **os.environ}, ROOT))
    from cbrs.config import SETTINGS
    from cbrs.jobs import default_job_store, utc_now
    from cbrs.owner_protocol import command_path
    store = default_job_store()
    for job_id in args.job_ids:
        job = store.get_job(job_id)
        historical = bool(
            job
            and job['status'] == 'failed'
            and job['error_code'] == 'search_outcome_unknown'
        )
        awaiting_reconciliation = bool(
            job
            and job['status'] == 'waiting_capacity'
            and job['error_code'] == 'search_reconciliation_required'
        )
        if not historical and not awaiting_reconciliation:
            raise ValueError(
                'Only named unconfirmed jobs awaiting explicit reconciliation can be resumed'
            )
        with sqlite3.connect(f'{command_path(SETTINGS).as_uri()}?mode=ro', uri=True) as db:
            if db.execute("SELECT 1 FROM owner_commands WHERE job_id=? AND state IN ('queued','running')", (job_id,)).fetchone():
                raise RuntimeError('Existing owner command must finish first')
        if args.apply:
            if not store.authorize_alternate_search(job_id):
                raise RuntimeError('Receipt, cancellation or live operation prevents retry')
            with store.connect() as db:
                changed = db.execute("""UPDATE jobs SET status='queued', finished_at=NULL,
                    error_code=NULL,error_message=NULL,next_run_at=?,updated_at=?,
                    worker_owner=NULL,lease_expires_at=NULL,current_account_id=NULL
                    WHERE job_id=? AND result_count IS NULL AND cancel_requested=0
                      AND ((status='failed' AND error_code='search_outcome_unknown')
                        OR (status='waiting_capacity'
                          AND error_code='search_reconciliation_required'))""",
                    (utc_now(),utc_now(),job_id)).rowcount
                if changed:
                    store._add_event_db(db,job_id,'uncertain_job_reconciled',
                        {'policy':'alternate_account_only','attempt_history_preserved':True})
        print(json.dumps({
            'job_id': job_id,
            'eligible': True,
            'prior_status': job['status'],
            'prior_reason': job['error_code'],
            'applied': args.apply,
        }))


if __name__ == '__main__':
    main()
