from playwright.sync_api import sync_playwright
import time
import re

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    
    # Navigate to rutificador.co search
    page.goto('https://www.rutificador.co/buscar/')
    
    # Wait for page to load
    page.wait_for_load_state('networkidle')
    
    # Fill search
    page.fill('input[placeholder*="nombre"]', 'Garcia')
    time.sleep(1)
    
    # Click search
    page.click('button')
    
    # Wait for results
    page.wait_for_timeout(15000)
    
    # Get page content
    content = page.content()
    print('Content length:', len(content))
    
    # Look for result data
    if 'Buscando' in content:
        print('Still searching...')
    elif 'Resultados' in content:
        print('Results found!')
        # Extract RUTs
        ruts = re.findall(r'[0-9]{1,7}[K0-9]', content)
        print('RUTs found:', ruts[:20])
    else:
        print('No results found')
    
    browser.close()
