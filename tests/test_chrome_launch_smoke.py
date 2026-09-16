"""Opt-in native Chrome smoke test using only a disposable local page/profile."""
import os

import pytest

from cbrs.browser_session import BrowserSession
from cbrs.config import load_settings


@pytest.mark.skipif(os.environ.get("CBRS_RUN_CHROME_SMOKE") != "1", reason="opt-in native Chrome test")
def test_native_chrome_launches_and_operates_with_normal_background_services(tmp_path):
    settings = load_settings({}, root=tmp_path)
    with BrowserSession(settings, headless=True) as browser:
        browser.page.set_content('<title>CBRS local smoke</title><input id="probe"><button>Save</button>')
        browser.page.locator("#probe").fill("local-only")
        assert browser.page.title() == "CBRS local smoke"
        assert browser.page.locator("#probe").input_value() == "local-only"
        assert browser.page.evaluate("navigator.webdriver") is True
