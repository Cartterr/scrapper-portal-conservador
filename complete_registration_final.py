"""
Complete Registration Test - Fill ALL fields with proper data and submit

This script:
1. Loads a Gmail account from pool
2. Navigates to registration
3. Fills ALL required fields
4. Submits form
5. Captures response and any errors
"""

import asyncio
import json
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def complete_registration_test():
    print("=" * 70)
    print("COMPLETE REGISTRATION TEST - ALL FIELDS")
    print("=" * 70)
    
    # Load account pool
    with open('.cbrs/account-pool.json', 'r') as f:
        pool = json.load(f)
    
    # Get first Gmail account
    gmail_accounts = [a for a in pool['accounts'] if 'email' in a and 'password' in a]
    account = gmail_accounts[0]
    
    test_email = account['email']
    test_password = account['password']
    
    print(f"\nTest account: {test_email}")
    print(f"Password: {test_password}")
    
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
        
        print("\n[1] Navigating to registration...")
        await page.goto('https://nuevo-portal.conservador.cl/crear-cuenta', wait_until='networkidle')
        await asyncio.sleep(3)
        await page.screenshot(path='registration_complete_test.png')
        print("  ✓ Page loaded")
        
        # Fill all fields
        print("\n[2] Filling all required fields...")
        
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
        
        print("\n[3] Checking for CAPTCHA...")
        captcha = await page.query_selector('.captcha, .recaptcha, .google-recaptcha')
        if captcha:
            print("  CAPTCHA detected!")
            await page.screenshot(path='registration_captcha_detected.png')
        else:
            print("  No CAPTCHA found")
        
        print("\n[4] Checking for validation errors...")
        error_elements = await page.query_selector_all('.error, .error-message, .invalid, .alert-danger, .alert-error')
        print(f"  Error elements: {len(error_elements)}")
        
        for elem in error_elements[:5]:
            try:
                text = await elem.inner_text()
                print(f"    Error: {text[:100]}")
            except:
                pass
        
        print("\n[5] Checking required fields...")
        required = await page.query_selector_all('span.required, [class*="required"]')
        print(f"  Required indicators: {len(required)}")
        
        print("\n[6] Clicking submit button...")
        submit_btn = await page.query_selector('button:has-text("Crear cuenta")')
        if submit_btn:
            await submit_btn.click(force=True)
            await asyncio.sleep(5)
            
            # Scroll to bottom
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await asyncio.sleep(2)
            
            # Take screenshot
            await page.screenshot(path='registration_after_submit_complete.png')
            print("  ✓ Submit clicked")
            
            # Check response
            content = await page.content()
            print(f"\n[7] Response: {len(content)} bytes")
            
            # Check for messages
            if 'exito' in content.lower() or 'success' in content.lower():
                print("  ✓ Success message found!")
            elif 'error' in content.lower():
                print("  ✗ Error message found")
            
            # Check for email verification
            if 'email' in content.lower() or 'correo' in content.lower() or 'verificar' in content.lower():
                print("  Email verification mentioned")
            
            # Check for account creation
            if 'cuenta' in content.lower() and 'creado' in content.lower():
                print("  Account created!")
            
            # Check for password requirements error
            if 'contraseña' in content.lower() and 'requisito' in content.lower():
                print("  Password requirements not met")
            
            # Check for RUT validation
            if 'RUT' in content.lower() and 'error' in content.lower():
                print("  RUT validation error")
        
        await browser.close()
        print("\n[8] Browser closed")
        
        import os
        screenshots = [f for f in os.listdir('.') if 'registration' in f.lower()]
        
        return {
            'email': test_email,
            'screenshots': screenshots
        }

if __name__ == '__main__':
    result = asyncio.run(complete_registration_test())
    print("\nResult:", result)
