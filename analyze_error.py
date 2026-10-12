import asyncio
import sys
import re
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def analyze_error():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto('https://nuevo-portal.conservador.cl/crear-cuenta', wait_until='networkidle')
        await asyncio.sleep(3)
        
        # Fill all fields
        fields = [
            ('email', '20261011_acnl@gmail.com'),
            ('emailConfirmar', '20261011_acnl@gmail.com'),
            ('password', 'test123'),
            ('passwordConfirmar', 'test123'),
            ('rut', '12345678-9'),
            ('nombres', 'Juan'),
            ('apellidoPaterno', 'Perez'),
            ('apellidoMaterno', 'Gomez'),
            ('direccion', 'Av. Providencia 123'),
            ('celular', '912345678')
        ]
        
        for field_name, value in fields:
            field = await page.query_selector(f'input[id="{field_name}"]')
            if field:
                await field.fill(value)
        
        # Click submit
        all_buttons = await page.query_selector_all('button')
        for btn in all_buttons:
            try:
                text = await btn.inner_text()
                if 'crear' in text.lower():
                    await btn.click(force=True)
                    await asyncio.sleep(5)
                    break
            except:
                pass
        
        # Get full content
        content = await page.content()
        
        # Extract error messages
        # Look for error patterns
        error_patterns = [
            r'error.*?[:\s](.*?)',
            r'class.*?error.*?>(.*?)',
            r'alert.*?>(.*?)',
        ]
        
        for pattern in error_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches[:3]:
                print(f'Error pattern match: {match[:200]}')
        
        # Look for email verification links
        verify_links = await page.query_selector_all('a[href*="verify"], a[href*="email"], a[href*="correo"]')
        print(f'Verification links: {len(verify_links)}')
        
        await browser.close()

asyncio.run(analyze_error())
