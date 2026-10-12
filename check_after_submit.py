import asyncio
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def check_after_submit():
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
        
        # Check response
        content = await page.content()
        print(f'Content length: {len(content)}')
        
        # Check for messages
        if 'exito' in content.lower():
            print('Success message found')
        elif 'error' in content.lower():
            print('Error message found')
        else:
            print('No obvious message')
        
        # Check for email verification
        if 'email' in content.lower() or 'correo' in content.lower():
            print('Email mentioned')
        
        # Check for account creation success
        if 'cuenta' in content.lower() and 'creado' in content.lower():
            print('Account created!')
        
        await browser.close()

asyncio.run(check_after_submit())
