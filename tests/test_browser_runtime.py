from pathlib import Path

import pytest

from cbrs.browser_runtime import detect_browser
from cbrs.config import load_settings


def test_detect_browser_prefers_chrome_then_edge(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    edge = tmp_path / "msedge.exe"
    chrome.write_text("", encoding="utf-8")
    edge.write_text("", encoding="utf-8")
    settings = load_settings({}, root=tmp_path)

    executable = detect_browser(
        settings,
        candidates=(("chrome", str(chrome)), ("edge", str(edge))),
    )

    assert executable.family == "chrome"
    assert executable.path == chrome
    assert executable.source == "auto"


def test_detect_browser_falls_back_to_edge(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    edge = tmp_path / "msedge.exe"
    edge.write_text("", encoding="utf-8")
    settings = load_settings({}, root=tmp_path)

    executable = detect_browser(
        settings,
        candidates=(("chrome", str(chrome)), ("edge", str(edge))),
    )

    assert executable.family == "edge"
    assert executable.path == edge


def test_detect_browser_uses_configured_executable(tmp_path: Path) -> None:
    browser = tmp_path / "custom-msedge.exe"
    browser.write_text("", encoding="utf-8")
    settings = load_settings({"CBRS_BROWSER_EXECUTABLE_PATH": str(browser)}, root=tmp_path)

    executable = detect_browser(settings, candidates=())

    assert executable.family == "edge"
    assert executable.path == browser
    assert executable.source == "env"


def test_detect_browser_fails_clearly_when_missing(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    with pytest.raises(RuntimeError, match="No Chrome or Edge executable"):
        detect_browser(settings, candidates=())
