import asyncio
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def check_form():
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
        
        await asyncio.sleep(1)
        
        # Check button state
        submit_btn = await page.query_selector('button:has-text("Crear cuenta")')
        if submit_btn:
            visible = await submit_btn.is_visible()
            enabled = await submit_btn.is_enabled()
            disabled = await submit_btn.get_attribute('disabled')
            
            print(f'Button visible: {visible}')
            print(f'Button enabled: {enabled}')
            print(f'Button disabled attribute: {disabled}')
            
            # Scroll and wait
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await asyncio.sleep(2)
            
            # Check again
            visible2 = await submit_btn.is_visible()
            enabled2 = await submit_btn.is_enabled()
            print(f'After scroll - visible: {visible2}, enabled: {enabled2}')
        
        await browser.close()

asyncio.run(check_form())
