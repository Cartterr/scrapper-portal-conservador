"""Bounded service lifecycle operations; independent owner is never a worker child."""
from pathlib import Path
import argparse
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from cbrs.paths import prepare_environment
os.environ.update(prepare_environment({**os.environ, **{k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None}}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['recover-absent', 'drain-owner', 'drain-worker', 'wait-owner', 'watchdog'])
    args = parser.parse_args()
    from cbrs.jobs import default_job_store, WORKER_LEASE_NAME
    from cbrs.owner_protocol import OwnerCommands, command_path
    from cbrs.config import SETTINGS
    store = default_job_store()
    if args.action == 'recover-absent':
        from cbrs.owner_lock import OwnerLock
        # A crashed/rebooted distro leaves short-lived database leases. Clear
        # only after process-level exclusion AND absence of account Chrome.
        with OwnerLock(command_path(SETTINGS).parent / 'owner.lock'):
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit() or int(entry.name) == os.getpid():
                    continue
                try:
                    argv = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                owner = '-m' in argv and 'cbrs.browser_owner' in argv and 'run' in argv
                worker = '-m' in argv and 'cbrs' in argv and 'jobs' in argv and 'worker' in argv
                chrome = any(arg.startswith('--user-data-dir=') and str(SETTINGS.profile_dir.parent) in arg for arg in argv)
                if owner or worker or chrome:
                    raise RuntimeError('Surviving owner/worker/browser preserved; startup requires inspection')
            for name in ('browser_owner', WORKER_LEASE_NAME):
                lease = store.lease(name)
                if lease:
                    store.release_lease(name, str(lease['owner']))
        return
    if args.action == 'watchdog':
        from cbrs.account_pool import AccountPoolStore
        if AccountPoolStore(store.path).stop_requested():
            return  # An overview/operator stop must remain stopped.
        # Restarting the worker cannot close the independent owner. Never
        # automatically restart a missing/stale owner with surviving Chrome.
        if store.active_lease('browser_owner') and not store.active_lease():
            enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', 'cbrs-worker.service']).returncode == 0
            active = subprocess.run(['systemctl', 'is-active', '--quiet', 'cbrs-worker.service']).returncode == 0
            if enabled and not active:
                subprocess.run(['systemctl', 'start', 'cbrs-worker.service'], check=True)
        return
    if args.action == 'wait-owner':
        for _ in range(60):
            if store.active_lease('browser_owner'):
                return
            time.sleep(1)
        raise RuntimeError('Independent owner unavailable; refusing coupled fallback')
    lease_name = 'browser_owner' if args.action == 'drain-owner' else 'worker'
    if args.action == 'drain-owner':
        OwnerCommands(command_path(SETTINGS)).stop()
    else:
        subprocess.run([sys.executable, '-m', 'cbrs', 'pool', 'stop'], cwd=ROOT, check=True)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        lease = store.active_lease('browser_owner') if lease_name == 'browser_owner' else store.active_lease()
        if not lease:
            return
        time.sleep(1)
    raise RuntimeError('Drain did not complete; inspect the living owner before maintenance')


if __name__ == '__main__':
    main()
