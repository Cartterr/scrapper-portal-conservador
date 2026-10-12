import asyncio
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def test_submit():
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
        
        # Try to find ANY button with submit-like behavior
        all_buttons = await page.query_selector_all('button')
        print(f'Total buttons: {len(all_buttons)}')
        
        for i, btn in enumerate(all_buttons):
            try:
                text = await btn.inner_text()
                class_name = await btn.get_attribute('class') or 'N/A'
                disabled = await btn.get_attribute('disabled') or 'N/A'
                
                # Check if button looks like submit
                if 'crear' in text.lower() or 'submit' in text.lower() or 'enviar' in text.lower():
                    print(f'\nButton {i+1}:')
                    print(f'  Text: {text}')
                    print(f'  Class: {class_name}')
                    print(f'  Disabled: {disabled}')
                    
                    # Try clicking with force
                    await btn.click(force=True)
                    print(f'  Clicked!')
                    await asyncio.sleep(3)
                    
                    # Check response
                    content = await page.content()
                    print(f'  Content length: {len(content)}')
                    
            except Exception as e:
                print(f'Button {i+1}: error - {e}')
        
        await browser.close()

asyncio.run(test_submit())
