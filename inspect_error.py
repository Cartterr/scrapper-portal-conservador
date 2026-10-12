import asyncio
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def inspect_error():
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
        
        # Inspect error elements
        error_elements = await page.query_selector_all('.error, .error-message, .alert-danger, .alert-error, [class*="error"]')
        print(f'Error elements: {len(error_elements)}')
        
        for i, elem in enumerate(error_elements[:5]):
            try:
                text = await elem.inner_text()
                print(f'  Error {i+1}: {text[:100]}')
            except:
                print(f'  Error {i+1}: cannot get text')
        
        # Check for validation errors on inputs
        invalid_inputs = await page.query_selector_all('input.invalid, input:invalid, .invalid-input')
        print(f'Invalid inputs: {len(invalid_inputs)}')
        
        for inp in invalid_inputs[:3]:
            try:
                id_val = await inp.get_attribute('id')
                name_val = await inp.get_attribute('name')
                error_msg = await inp.get_attribute('aria-invalid')
                print(f'  Invalid input: id={id_val}, name={name_val}, aria-invalid={error_msg}')
            except:
                pass
        
        # Check for required fields
        required = await page.query_selector_all('span.required, [class*="required"]')
        print(f'Required indicators: {len(required)}')
        
        await browser.close()

asyncio.run(inspect_error())
