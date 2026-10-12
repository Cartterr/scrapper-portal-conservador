import asyncio
from playwright.async_api import async_playwright
import re

async def scrape_ruts():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Test with rutynombre.com
        url = 'https://rutynombre.com/'
        await page.goto(url)
        await page.wait_for_timeout(2000)
        
        # Fill search box with '1'
        search_input = page.locator('input[name="term"]')
        await search_input.fill('1')
        await search_input.press('Enter')
        
        # Wait for results
        await page.wait_for_timeout(3000)
        
        # Extract RUTs from results table
        content = await page.content()
        ruts = re.findall(r'(\d{1,7}[K0-9])', content)
        unique_ruts = sorted(set(ruts))
        valid_ruts = [rut for rut in unique_ruts if 6 <= len(rut) <= 8]
        
        print(f'Found {len(valid_ruts)} unique RUTs from rutynombre.com')
        for rut in valid_ruts[:50]:
            print(f'  {rut}')
        
        await browser.close()

asyncio.run(scrape_ruts())
