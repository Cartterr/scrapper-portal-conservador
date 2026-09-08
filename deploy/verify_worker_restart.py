"""Explicit operational test: restart only the worker, assert Chrome survives."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    owner = subprocess.check_output(['systemctl', 'show', 'cbrs-browser-owner', '-p', 'MainPID', '--value'], text=True).strip()
    browsers = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0')
        except (OSError, ProcessLookupError):
            continue
        if args and args[0].endswith(('chrome', 'google-chrome-stable')) and not any(a.startswith('--type=') for a in args) and any(a.startswith('--user-data-dir=') and str(ROOT / '.cbrs/runtime/accounts') in a for a in args):
            browsers.append(int(proc.name))
    return {'owner_pid': int(owner), 'chrome_pids': sorted(browsers)}


if __name__ == '__main__':
    before = snapshot()
    if not before['owner_pid'] or not before['chrome_pids']:
        raise RuntimeError('No living production owner/browser to verify')
    subprocess.run(['systemctl', 'restart', 'cbrs-worker'], check=True)
    after = snapshot()
    print(json.dumps({'before': before, 'after': after, 'preserved': before == after}))
    if before != after:
        raise RuntimeError('Owner/browser process identity changed')
