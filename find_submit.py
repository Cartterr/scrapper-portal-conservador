import asyncio
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def find_submit():
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
            ('password', 'uumlQ2tUYD8UsPQN!'),
            ('passwordConfirmar', 'uumlQ2tUYD8UsPQN!'),
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
        
        # Check all buttons
        all_buttons = await page.query_selector_all('button')
        print(f'Total buttons: {len(all_buttons)}')
        
        for i, btn in enumerate(all_buttons):
            try:
                text = await btn.inner_text()
                visible = await btn.is_visible()
                enabled = await btn.is_enabled()
                class_name = await btn.get_attribute('class') or 'N/A'
                
                if 'crear' in text.lower():
                    print(f'\nButton {i+1}:')
                    print(f'  Text: {text}')
                    print(f'  Visible: {visible}')
                    print(f'  Enabled: {enabled}')
                    print(f'  Class: {class_name}')
                    
            except Exception as e:
                pass
        
        await browser.close()

asyncio.run(find_submit())
