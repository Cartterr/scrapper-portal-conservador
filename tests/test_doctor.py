from pathlib import Path
from types import SimpleNamespace

from cbrs import cli
from cbrs.config import load_settings


def test_doctor_fails_when_browser_executable_missing(monkeypatch, capsys, tmp_path: Path) -> None:
    _write_safe_gitignore(tmp_path)
    settings = load_settings({}, root=tmp_path)
    missing_status = SimpleNamespace(
        available=False,
        family=None,
        path=None,
        source=None,
        error="No Chrome or Edge executable found.",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.config, "SETTINGS", settings)
    monkeypatch.setattr(cli, "get_browser_status", lambda loaded_settings: missing_status)

    result = cli.cmd_doctor()
    out = capsys.readouterr().out

    assert result == 1
    assert "FAIL browser executable: No Chrome or Edge executable found." in out


def test_doctor_fails_when_proxy_is_configured(monkeypatch, capsys, tmp_path: Path) -> None:
    _write_safe_gitignore(tmp_path)
    browser = tmp_path / "chrome.exe"
    browser.write_text("", encoding="utf-8")
    settings = load_settings(
        {
            "CBRS_BROWSER_EXECUTABLE_PATH": str(browser),
            "CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234",
        },
        root=tmp_path,
    )
    status = SimpleNamespace(
        available=True,
        family="chrome",
        path=browser,
        source="env",
        error=None,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.config, "SETTINGS", settings)
    monkeypatch.setattr(cli, "get_browser_status", lambda loaded_settings: status)

    result = cli.cmd_doctor()
    out = capsys.readouterr().out

    assert result == 1
    assert "FAIL production proxy disabled: CBRS_CLOAK_PROXY_URL configured" in out


def _write_safe_gitignore(path: Path) -> None:
    path.joinpath(".gitignore").write_text(
        "\n".join(
            [
                ".cbrs/",
                ".env",
                ".env.local",
                "output/",
                "*.cookie",
                "*.session.json",
                "*.storage_state.json",
            ]
        ),
        encoding="utf-8",
    )
