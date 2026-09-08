"""Render Linux service paths from this checkout; no directory input needed."""
from pathlib import Path
import argparse
import shutil

ROOT = Path(__file__).resolve().parents[1]


def render(text: str, root: Path = ROOT) -> str:
    # systemd path escaping, including literal percent specifiers.
    value = root.resolve().as_posix().replace(' ', '\\x20').replace('%', '%%')
    if '[Service]' in text:
        text = text.replace('[Service]', '[Service]\n'
                            'StandardOutput=append:@REPO_ROOT@/.cbrs/runtime/logs/services.log\n'
                            'StandardError=append:@REPO_ROOT@/.cbrs/runtime/logs/services.log')
    return text.replace('@REPO_ROOT@', value).replace('ProtectHome=true', 'ProtectHome=read-only')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('/etc/systemd/system'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.output.resolve() == Path('/etc/systemd/system'):
        # Known legacy drop-ins override ExecStart even after a unit is updated.
        # Archive exact files, never recursively remove arbitrary service config.
        for relative in ('cbrs-dashboard.service.d/wsl-local.conf',
                         'cbrs-worker.service.d/wsl-dashboard-viewer.conf'):
            legacy = args.output / relative
            if legacy.is_file():
                backup = ROOT / '.cbrs/deployment-rollback' / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    raise RuntimeError('Legacy override backup already exists; inspect before replacing')
                shutil.move(str(legacy), str(backup))
    for source in (ROOT / 'deploy').iterdir():
        if source.suffix in {'.service', '.timer', '.path'}:
            (args.output / source.name).write_text(render(source.read_text()), encoding='utf-8')


if __name__ == '__main__':
    main()
