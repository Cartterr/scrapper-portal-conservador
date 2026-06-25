import sys
from pathlib import Path
from types import SimpleNamespace

from cbrs.browser_session import BrowserSession
from cbrs.config import load_settings


def test_browser_session_launches_chrome_persistent_context(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings({}, root=tmp_path)
    browser = tmp_path / "chrome.exe"
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            captured["closed"] = True

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["user_data_dir"] = user_data_dir
            captured["kwargs"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            captured["stopped"] = True

    class FakeSyncPlaywright:
        def start(self):
            captured["started"] = True
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(path=browser, family="chrome", source="auto"),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    session = BrowserSession(settings, headless=False)
    session.open()
    session.close()

    assert captured["user_data_dir"] == str(settings.profile_dir)
    assert captured["kwargs"]["executable_path"] == str(browser)
    assert captured["kwargs"]["headless"] is False
    assert captured["kwargs"]["accept_downloads"] is True
    assert captured["closed"] is True
    assert captured["stopped"] is True


def test_browser_session_launches_chrome_offscreen_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = load_settings({"CBRS_WINDOW_MODE": "offscreen"}, root=tmp_path)
    browser = tmp_path / "chrome.exe"
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["kwargs"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            pass

    class FakeSyncPlaywright:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(path=browser, family="chrome", source="auto"),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    with BrowserSession(settings, headless=False):
        pass

    assert captured["kwargs"]["headless"] is False
    assert "--window-size=1366,900" in captured["kwargs"]["args"]
    assert "--window-position=-32000,-32000" in captured["kwargs"]["args"]


def test_browser_session_launches_chrome_with_proxy(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(
        {
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
            "CBRS_PROXY_URL": "http://proxy-user:proxy-pass@example.test:33335",
        },
        root=tmp_path,
    )
    browser = tmp_path / "chrome.exe"
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["kwargs"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            pass

    class FakeSyncPlaywright:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(path=browser, family="chrome", source="auto"),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    with BrowserSession(settings, headless=False):
        pass

    assert captured["kwargs"]["proxy"] == {
        "server": "http://example.test:33335",
        "username": "proxy-user",
        "password": "proxy-pass",
    }


def test_browser_session_rejects_proxy_in_chrome_backend(tmp_path: Path) -> None:
    settings = load_settings(
        {"CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234"},
        root=tmp_path,
    )
    session = BrowserSession(settings)

    try:
        session.open()
    except RuntimeError as exc:
        assert "CBRS_CLOAK_PROXY_URL" in str(exc)
    else:
        raise AssertionError("Expected chrome backend to reject proxy config")


def test_browser_session_accepts_actual_portal_login_cookie(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakeContext:
        def cookies(self, urls):
            return [{"name": "auth_cbrs_token", "value": "[REDACTED]"}]

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is True


def test_browser_session_accepts_refresh_cookie(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakeContext:
        def cookies(self, urls):
            return [{"name": "cbrs_refresh_token", "value": "[REDACTED]"}]

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is True


def test_browser_session_checks_auth_refresh_cookie_path(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)
    captured = {}

    class FakeContext:
        def cookies(self, urls):
            captured["urls"] = urls
            if any(url.endswith("/api/v1/auth/refresh") for url in urls):
                return [{"name": "cbrs_refresh_token", "value": "[REDACTED]"}]
            return []

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is True
    assert f"{settings.base_url}/api/v1/auth/refresh" in captured["urls"]


def test_browser_session_rejects_stay_signed_in_cookie_without_tokens(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakeContext:
        def cookies(self, urls):
            return [{"name": "auth_cbrs_stay_signed_in", "value": "[REDACTED]"}]

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is False
