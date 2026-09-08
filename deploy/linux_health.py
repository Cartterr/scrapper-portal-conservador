"""Read-only operational checks for the Linux service; prints no credentials."""
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
UNITS = ('cbrs-browser-owner', 'cbrs-worker', 'cbrs-dashboard', 'cbrs-display',
         'cbrs-novnc', 'cbrs-backup.timer', 'cbrs-watchdog.timer')


def main():
    units = {unit: subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0 for unit in UNITS}
    with urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=15) as response:
        status = json.load(response)
    accounts = [{'id': a['account_id'], 'authenticated': bool(a.get('browser_authenticated')),
                 'preview_fresh': a.get('browser_preview_age_seconds') is not None and a['browser_preview_age_seconds'] < 30,
                 'proxy_health': a.get('proxy_health_status'), 'baseline': a.get('egress_baseline_status')}
                for a in status.get('accounts', [])]
    result = {'root': str(ROOT), 'units': units, 'accounts': accounts,
              'independent_owner': status.get('runtime', {}).get('owner_mode') == 'external'}
    result['ok'] = all(units.values()) and result['independent_owner'] and bool(accounts) and all(a['authenticated'] and a['preview_fresh'] for a in accounts)
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
