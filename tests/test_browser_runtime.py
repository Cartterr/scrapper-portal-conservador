from pathlib import Path

import pytest

from cbrs.browser_runtime import detect_browser, validate_service_browser
from cbrs.config import load_settings


@pytest.mark.parametrize("backend", ["gologin", "cloak", "dolphin"])
def test_service_rejects_alternate_backend_before_worker_side_effects(tmp_path, backend):
    from cbrs.jobs import run_job_worker

    settings = load_settings({"CBRS_BROWSER_BACKEND": backend}, root=tmp_path)
    with pytest.raises(ValueError, match="requires CBRS_BROWSER_BACKEND=chrome"):
        run_job_worker(settings=settings, once=True)
    assert not settings.captcha_state_path.exists()


@pytest.mark.parametrize("executable", [
    "GoLogin/Orbita/chrome.exe", "Dolphin/chrome.exe", "cloakbrowser/chrome.exe",
    "orbita.exe", "msedge.exe", "custom-browser.exe",
])
def test_service_rejects_disguised_or_alternate_executable(tmp_path, executable):
    settings = load_settings({"CBRS_BROWSER_EXECUTABLE_PATH": str(tmp_path / executable)}, root=tmp_path)
    with pytest.raises(ValueError, match="requires regular Google Chrome"):
        validate_service_browser(settings)


@pytest.mark.parametrize("headless", ["0", "1"])
def test_service_accepts_regular_chrome_in_both_modes(tmp_path, headless):
    settings = load_settings({"CBRS_HEADLESS": headless,
                              "CBRS_BROWSER_EXECUTABLE_PATH": str(tmp_path / "chrome.exe")}, root=tmp_path)
    validate_service_browser(settings)


def test_regular_backend_will_not_launch_an_edge_fallback(tmp_path, monkeypatch):
    from cbrs.browser_runtime import BrowserExecutable
    from cbrs.browser_session import BrowserSession

    settings = load_settings({}, root=tmp_path)
    monkeypatch.setattr("cbrs.browser_session.detect_browser", lambda _: BrowserExecutable(
        family="edge", path=tmp_path / "msedge.exe", source="auto"))
    with pytest.raises(ValueError, match="requires regular Google Chrome"):
        BrowserSession(settings).open()


def test_recovery_rejects_gologin_before_route_or_browser_mutation(tmp_path):
    from cbrs.jobs import _rotate_dataimpulse_route

    settings = load_settings({"CBRS_BROWSER_BACKEND": "gologin"}, root=tmp_path)
    with pytest.raises(ValueError, match="requires CBRS_BROWSER_BACKEND=chrome"):
        _rotate_dataimpulse_route(None, settings, None, None, "test", None, None, None, reason="test")


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


def test_detect_browser_finds_linux_chrome_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings({}, root=tmp_path)
    chrome = tmp_path / "google-chrome"
    chrome.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "cbrs.browser_runtime.shutil.which",
        lambda command: str(chrome) if command == "google-chrome-stable" else None,
    )
    monkeypatch.setattr("cbrs.browser_runtime.WINDOWS_BROWSER_PATHS", ())
    monkeypatch.setattr("cbrs.browser_runtime.LINUX_BROWSER_PATHS", ())

    executable = detect_browser(settings)

    assert executable.family == "chrome"
    assert executable.path == chrome
    assert executable.source == "path"
