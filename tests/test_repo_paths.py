from pathlib import Path

from cbrs.config import load_settings
from cbrs.paths import PATH_KEYS, prepare_environment, runtime_environment


def test_locations_are_repo_owned(tmp_path):
    values = prepare_environment({'TEMP': '/elsewhere', 'CBRS_OUTPUT_DIR': '/elsewhere'}, tmp_path)
    for key in ('CBRS_PROFILE_DIR', 'CBRS_OUTPUT_DIR', 'CBRS_LOG_DIR',
                'CBRS_CAPTCHA_STATE_PATH', 'RESTIC_REPOSITORY', 'RESTIC_PASSWORD_FILE',
                'RESTIC_CACHE_DIR', 'TEMP', 'TMP', 'TMPDIR', 'XDG_CACHE_HOME'):
        assert Path(values[key]).is_relative_to(tmp_path)
    assert Path(values['TMPDIR']).is_dir()


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
    assert temp == root / '.cbrs/runtime/tmp'
