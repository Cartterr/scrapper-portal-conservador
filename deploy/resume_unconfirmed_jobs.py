"""Explicitly resume named unconfirmed jobs after portal-history review.

Equivalent to ``cbrs jobs reconcile JOB_ID [--apply]``. Works with or without
the independent browser owner (D26): the owner command ledger is consulted
only when it exists.
"""
import argparse
import json
import os
from pathlib import Path
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
    from cbrs.jobs import default_job_store
    from cbrs.owner_protocol import command_path
    store = default_job_store()
    for job_id in args.job_ids:
        print(json.dumps(store.reconcile_unconfirmed(
            job_id, apply=args.apply, owner_commands_path=command_path(SETTINGS))))


if __name__ == '__main__':
    main()
