from playwright.sync_api import sync_playwright
import time
import re

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    
    # Navigate to search
    page.goto('https://www.rutificador.co/buscar/')
    
    # Get initial page
    initial = page.content()
    print('Initial content length:', len(initial))
    
    # Fill and search
    page.fill('input[placeholder*="nombre"]', 'Perez')
    
    # Get after fill
    after_fill = page.content()
    print('After fill length:', len(after_fill))
    
    # Click search
    page.click('button')
    
    # Get after click
    after_click = page.content()
    print('After click length:', len(after_click))
    
    # Wait and get again
    page.wait_for_timeout(10000)
    after_wait = page.content()
    print('After wait length:', len(after_wait))
    
    # Check for differences
    if len(after_wait) > len(initial):
        print('Content changed after search!')
        # Look for new RUTs
        new_ruts = re.findall(r'[0-9]{1,7}[K0-9]', after_wait)
        initial_ruts = re.findall(r'[0-9]{1,7}[K0-9]', initial)
        print('New RUTs:', set(new_ruts) - set(initial_ruts))
    
    browser.close()
