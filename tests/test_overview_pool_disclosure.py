from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright


@pytest.mark.parametrize('width', [380, 960, 1440])
def test_pool_details_default_collapsed_and_do_not_stretch_headline(width):
    html = (Path(__file__).parents[1] / 'cbrs/web/overview.html').read_text(encoding='utf-8')
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': width, 'height': 1000}, java_script_enabled=False)
            page = context.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content(html, wait_until='domcontentloaded')
            details = page.locator('#poolStatus')
            headline = page.locator('.headline')
            assert details.get_attribute('open') is None
            assert not page.locator('#poolFacts').is_visible()
            before = headline.bounding_box()
            assert before['height'] < 320
            assert details.bounding_box()['height'] < 100
            assert before['x'] + before['width'] <= width
            summary = details.locator('summary')
            summary.focus()
            page.keyboard.press('Enter')
            assert details.get_attribute('open') is not None
            assert headline.bounding_box()['height'] == before['height']
            page.keyboard.press('Enter')
            assert details.get_attribute('open') is None
        finally:
            browser.close()  # Disposable UI test only, never portal instances.
