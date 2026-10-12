from playwright.sync_api import sync_playwright
import re

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    
    # Navigate to search
    page.goto('https://www.rutificador.co/buscar/')
    
    # Get page after navigation
    content1 = page.content()
    
    # Fill search field
    page.fill('input[placeholder*="nombre"]', 'Perez')
    
    # Get page after fill
    content2 = page.content()
    
    # Click search button
    page.click('button')
    
    # Get page immediately after click
    content3 = page.content()
    
    # Wait longer
    page.wait_for_timeout(30000)
    
    # Get page after wait
    content4 = page.content()
    
    # Check content changes
    print('Content changes:')
    print(f'  After fill: {len(content2) - len(content1)} chars changed')
    print(f'  After click: {len(content3) - len(content2)} chars changed')
    print(f'  After wait: {len(content4) - len(content3)} chars changed')
    
    # Look for result indicators
    print('\nResult indicators:')
    print('  Buscando in content4:', 'Buscando' in content4)
    print('  Resultados in content4:', 'Resultados' in content4)
    
    # Look for any table rows with data
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', content4, re.DOTALL)
    print(f'  Table rows: {len(rows)}')
    
    browser.close()
