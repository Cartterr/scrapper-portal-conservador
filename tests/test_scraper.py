from pathlib import Path

from cbrs.config import load_settings


def test_scraper_can_leave_browser_open_between_contexts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cbrs.scraper import CBRSScraper

    settings = load_settings({}, root=tmp_path)
    captured = {"browser_closed": 0, "browser_opened": 0, "client_closed": 0}

    class FakeBrowser:
        def __init__(self, loaded_settings, *, headless):
            self.settings = loaded_settings
            self.headless = headless

        def open(self):
            captured["browser_opened"] += 1

        def close(self):
            captured["browser_closed"] += 1

    class FakeClient:
        def __init__(self, browser, loaded_settings):
            self.browser = browser
            self.settings = loaded_settings

        def close(self):
            captured["client_closed"] += 1

    monkeypatch.setattr("cbrs.scraper.BrowserSession", FakeBrowser)
    monkeypatch.setattr("cbrs.scraper.BrowserOriginClient", FakeClient)

    scraper = CBRSScraper(
        headless=False,
        settings=settings,
        close_browser_on_exit=False,
    )
    with scraper:
        pass

    assert captured["browser_opened"] == 1
    assert captured["client_closed"] == 1
    assert captured["browser_closed"] == 0

    scraper.close_browser()

    assert captured["browser_closed"] == 1
