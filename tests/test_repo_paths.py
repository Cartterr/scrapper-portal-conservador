import os
from pathlib import Path

import pytest

from cbrs.config import load_settings
from cbrs.paths import (
    CHROME_SOCKET_SUFFIX,
    PATH_KEYS,
    UNIX_SOCKET_PATH_MAX,
    prepare_environment,
    runtime_environment,
)


def test_locations_are_repo_owned(tmp_path):
    values = prepare_environment({'TEMP': '/elsewhere', 'CBRS_OUTPUT_DIR': '/elsewhere'}, tmp_path)
    for key in ('CBRS_PROFILE_DIR', 'CBRS_OUTPUT_DIR', 'CBRS_LOG_DIR',
                'CBRS_CAPTCHA_STATE_PATH', 'RESTIC_REPOSITORY', 'RESTIC_PASSWORD_FILE',
                'RESTIC_CACHE_DIR', 'XDG_CACHE_HOME'):
        assert Path(values[key]).is_relative_to(tmp_path)
    for key in ('TEMP', 'TMP', 'TMPDIR'):
        assert values[key] == runtime_environment(tmp_path)['TMPDIR']
    temp = Path(values['TMPDIR'])
    assert temp.is_dir()
    if not temp.is_relative_to(tmp_path):
        temp.rmdir()


def test_short_checkout_keeps_repo_local_temp():
    root = Path('/srv/cbrs')
    assert runtime_environment(root)['TMPDIR'] == str(root.resolve() / '.cbrs/runtime/tmp')


@pytest.mark.skipif(os.name == 'nt', reason='Unix socket path limit')
def test_deep_checkout_uses_short_private_temp(tmp_path):
    temp = Path(prepare_environment({}, tmp_path / ('d' * 80))['TMPDIR'])
    try:
        assert len(str(temp)) + len(CHROME_SOCKET_SUFFIX) <= UNIX_SOCKET_PATH_MAX
        assert temp.stat().st_mode & 0o077 == 0
    finally:
        temp.rmdir()


def test_production_ignores_stale_path_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('CBRS_OUTPUT_DIR', '/legacy/outputs')
    monkeypatch.chdir(tmp_path.parent)
    settings = load_settings(root=tmp_path)
    assert settings.output_dir == tmp_path / '.cbrs/runtime/outputs'
    assert settings.profile_dir == tmp_path / '.cbrs/runtime/chrome-profile'


def test_examples_have_no_directory_configuration():
    root = Path(__file__).resolve().parents[1]
    for relative in ('.env.example', 'deploy/cbrs.env.example', 'deploy/cbrs-native.env.example'):
        keys = {line.partition('=')[0] for line in (root / relative).read_text().splitlines()}
        assert not keys.intersection(PATH_KEYS)


def test_launcher_uses_repo_from_another_directory(tmp_path):
    import json
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / '.env'
    env_file.write_text('CBRS_OUTPUT_DIR=/old/runtime\n')
    code = "import os,json; print(json.dumps([os.getcwd(),os.environ['CBRS_OUTPUT_DIR'],os.environ['TEMP']]))"
    result = subprocess.run([sys.executable, str(root / 'deploy/run_with_env.py'),
                             str(env_file), '--', sys.executable, '-c', code],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    cwd, output, temp = map(Path, json.loads(result.stdout))
    assert cwd == root
    assert output == root / '.cbrs/runtime/outputs'
    assert temp == Path(runtime_environment(root)['TEMP'])
