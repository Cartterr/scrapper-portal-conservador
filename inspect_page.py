import asyncio
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def inspect_page():
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
        
        # Take screenshot
        await page.screenshot(path='registration_inspect.png')
        
        # Check for modals/overlays
        modals = await page.query_selector_all('.modal, .dialog, [class*="modal"], [class*="dialog"]')
        print(f'Modals found: {len(modals)}')
        
        # Check for form validation errors
        errors = await page.query_selector_all('.error, .error-message, .invalid, .alert-danger, .alert-error')
        print(f'Error elements: {len(errors)}')
        
        # Check for CAPTCHA
        captcha = await page.query_selector('.captcha, .recaptcha, .google-recaptcha')
        print(f'CAPTCHA found: {captcha is not None}')
        
        # Check for email verification
        email_verify = await page.query_selector('.email-verification, .verification-email')
        print(f'Email verification: {email_verify is not None}')
        
        # Print page structure
        print("\nPage structure:")
        headings = await page.query_selector_all('h1, h2, h3')
        for h in headings:
            try:
                print(f"  {await h.inner_text()}")
            except:
                pass
        
        await browser.close()

asyncio.run(inspect_page())
