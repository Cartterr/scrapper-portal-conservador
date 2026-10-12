import asyncio
from playwright.async_api import async_playwright
import re

async def scrape_ruts():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Test with nombrerutyfirma.com
        url = 'https://nombrerutyfirma.com/'
        await page.goto(url)
        
        # Wait for page to load
        await page.wait_for_timeout(2000)
        
        # Try searching for '1'
        await page.fill('input[name="term"]', '1')
        await page.press('input[name="term"]', 'Enter')
        
        # Wait for results
        await page.wait_for_timeout(3000)
        
        # Extract RUTs from results table
        content = await page.content()
        ruts = re.findall(r'(\d{1,7}[K0-9])', content)
        unique_ruts = sorted(set(ruts))
        valid_ruts = [rut for rut in unique_ruts if 6 <= len(rut) <= 8]
        
        print(f'Found {len(valid_ruts)} unique RUTs from nombrerutyfirma.com')
        for rut in valid_ruts[:50]:
            print(f'  {rut}')
        
        await browser.close()

asyncio.run(scrape_ruts())
