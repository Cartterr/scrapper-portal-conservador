"""
Final Registration Test - Complete All Fields and Submit

This script:
1. Navigates to registration page
2. Fills ALL fields with test data
3. Submits form
4. Captures response
5. Checks for email verification step
"""

import asyncio
import json
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def final_registration_test():
    print("=" * 70)
    print("FINAL REGISTRATION TEST - COMPLETE")
    print("=" * 70)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process'
            ]
        )
        
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()
        
        # Test account
        test_email = '20261011_acnl@gmail.com'
        test_password = 'uumlQ2tUYD8UsPQN'
        
        print(f"\nTest account: {test_email}")
        
        # Navigate
        print("\n[1] Navigating...")
        await page.goto('https://nuevo-portal.conservador.cl/crear-cuenta', wait_until='networkidle')
        await asyncio.sleep(3)
        
        # Fill all fields
        print("\n[2] Filling all fields...")
        
        fields_data = {
            'email': test_email,
            'emailConfirmar': test_email,
            'password': test_password,
            'passwordConfirmar': test_password,
            'rut': '12345678-9',
            'nombres': 'Juan',
            'apellidoPaterno': 'Perez',
            'apellidoMaterno': 'Gomez',
            'direccion': 'Av. Providencia 123',
            'celular': '912345678'
        }
        
        for field_name, value in fields_data.items():
            field = await page.query_selector(f'input[id="{field_name}"]')
            if field:
                await field.fill(value)
                print(f"  ✓ {field_name}: {value[:20]}...")
        
        # Click submit
        print("\n[3] Submitting form...")
        submit = await page.query_selector('button:has-text("Crear cuenta")')
        if submit:
            await submit.click()
            await asyncio.sleep(5)
            
            # Scroll to bottom
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await asyncio.sleep(2)
            
            # Take screenshot
            await page.screenshot(path='registration_final_test.png')
            print("  ✓ Screenshot saved: registration_final_test.png")
            
            # Check page content
            content = await page.content()
            print(f"\n[4] Page content: {len(content)} bytes")
            
            # Check for messages
            if 'exito' in content.lower():
                print("  ✓ Success message found")
            elif 'error' in content.lower():
                print("  ✗ Error message found")
            else:
                print("  ? No obvious message")
            
            # Check for email verification
            if 'email' in content.lower() or 'correo' in content.lower() or 'verificar' in content.lower():
                print("  Email verification mentioned")
            
            # Check for CAPTCHA
            if 'captcha' in content.lower() or 'recaptcha' in content.lower():
                print("  CAPTCHA detected")
        
        await browser.close()
        print("\n[5] Done")
        
        return content

asyncio.run(final_registration_test())
