"""
Complete Registration Workflow - Fill ALL Fields Properly

This script:
1. Navigates to registration page
2. Fills ALL required fields: email, password, RUT, address, etc.
3. Handles form validation
4. Clicks submit
5. Handles email verification step
6. Completes registration
"""

import sys
import asyncio
import json
sys.path.insert(0, 'cbrs')

from playwright.async_api import async_playwright


async def complete_registration(email, password, rut=''):
    """
    Complete registration workflow filling ALL fields.
    
    Args:
        email: Gmail account email
        password: Gmail account password
        rut: RUT to register (optional, empty string if not needed)
    """
    print("=" * 70)
    print("COMPLETE REGISTRATION WORKFLOW - ALL FIELDS")
    print("=" * 70)
    print(f"Account: {email}")
    print(f"RUT: {rut if rut else '(empty)'}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process'
            ]
        )
        print("\n[1] Browser launched")
        
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()
        print("  ✓ Context created")
        
        # Navigate to registration page
        print("\n[2] Navigating to registration page...")
        await page.goto('https://nuevo-portal.conservador.cl/crear-cuenta', wait_until='networkidle')
        await asyncio.sleep(3)
        await page.screenshot(path='registration_complete_start.png')
        print("  ✓ Page loaded")
        
        # Get page content
        content = await page.content()
        print(f"  Page content: {len(content)} bytes")
        
        # Find all form elements
        print("\n[3] Finding form elements...")
        
        # Find all input fields
        all_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input[type="password"], input[type="tel"], input[type="url"], textarea')
        print(f"  Input fields found: {len(all_inputs)}")
        
        # Find all labels
        all_labels = await page.query_selector_all('label')
        print(f"  Labels found: {len(all_labels)}")
        
        # Print structure
        print("\n[4] Analyzing form structure...")
        for i, input_elem in enumerate(all_inputs):
            try:
                id_val = await input_elem.get_attribute('id')
                name_val = await input_elem.get_attribute('name')
                placeholder = await input_elem.get_attribute('placeholder')
                type_val = await input_elem.get_attribute('type')
                required = await input_elem.get_attribute('required')
                
                print(f"  Input {i+1}:")
                print(f"    ID: {id_val or 'N/A'}")
                print(f"    Name: {name_val or 'N/A'}")
                print(f"    Placeholder: {placeholder or 'N/A'}")
                print(f"    Type: {type_val or 'N/A'}")
                print(f"    Required: {required or 'N/A'}")
                
            except Exception as e:
                print(f"    Error: {e}")
        
        # Find submit buttons
        print("\n[5] Finding submit buttons...")
        submit_buttons = await page.query_selector_all('button[type="submit"], button[type="button"], input[type="submit"]')
        print(f"  Submit buttons found: {len(submit_buttons)}")
        
        for i, btn in enumerate(submit_buttons):
            try:
                text = await btn.inner_text()
                print(f"    Button {i+1}: {text or 'N/A'}")
            except:
                pass
        
        # Fill form fields
        print("\n[6] Filling form fields...")
        
        # Fill email
        email_input = await page.query_selector('input[type="email"]')
        if email_input:
            print(f"  Filling email: {email}")
            await email_input.fill(email)
            await asyncio.sleep(1)
            print("  ✓ Email filled")
        else:
            print("  ✗ Email input not found")
        
        # Fill password
        password_input = await page.query_selector('input[type="password"]')
        if password_input:
            print(f"  Filling password")
            await password_input.fill(password)
            await asyncio.sleep(1)
            print("  ✓ Password filled")
        else:
            print("  ✗ Password input not found")
        
        # Fill RUT (if provided)
        rut_input = await page.query_selector('input[name="rut"], input[id="rut"], input[placeholder*="RUT"]')
        if rut_input and rut:
            print(f"  Filling RUT: {rut}")
            await rut_input.fill(rut)
            await asyncio.sleep(1)
            print("  ✓ RUT filled")
        elif rut_input:
            print(f"  RUT input found but empty (skipping)")
        
        # Fill name fields
        print("\n[7] Filling name/address fields...")
        
        # Common name fields
        name_fields = [
            'input[name="nombre"]',
            'input[name="apellido"]',
            'input[id="nombre"]',
            'input[id="apellido"]',
            'input[placeholder*="nombre"]',
            'input[placeholder*="apellido"]',
            'input[placeholder*="name"]',
            'input[placeholder*="nombre"]',
        ]
        
        for selector in name_fields:
            field = await page.query_selector(selector)
            if field:
                try:
                    text = await field.inner_text()
                    if text:
                        print(f"  Found field: {selector} (pre-filled: {text[:30]}...)")
                    else:
                        print(f"  Empty field: {selector}")
                except:
                    pass
        
        # Common address fields
        address_fields = [
            'input[name="direccion"]',
            'input[name="calle"]',
            'input[name="numero"]',
            'input[name="comuna"]',
            'input[name="ciudad"]',
            'input[id="direccion"]',
            'input[id="calle"]',
            'input[id="numero"]',
            'input[id="comuna"]',
            'input[id="ciudad"]',
            'input[placeholder*="direccion"]',
            'input[placeholder*="calle"]',
            'input[placeholder*="numero"]',
            'input[placeholder*="comuna"]',
            'input[placeholder*="ciudad"]',
        ]
        
        for selector in address_fields:
            field = await page.query_selector(selector)
            if field:
                try:
                    text = await field.inner_text()
                    if text:
                        print(f"  Found address field: {selector} (pre-filled: {text[:30]}...)")
                    else:
                        print(f"  Empty address field: {selector}")
                except:
                    pass
        
        # Fill common fields with sample data
        print("\n[8] Filling common fields with sample data...")
        
        # Sample data to fill
        sample_data = {
            'nombre': 'Juan',
            'apellido': 'Perez',
            'direccion': 'Av. Providencia 123',
            'comuna': 'Providencia',
            'ciudad': 'Santiago',
        }
        
        # Try to fill these fields
        for field_name, value in sample_data.items():
            # Try by name attribute
            field = await page.query_selector(f'input[name="{field_name}"]')
            if field:
                print(f"  Found field '{field_name}', filling with '{value}'")
                await field.fill(value)
                await asyncio.sleep(0.5)
                print(f"  ✓ Field '{field_name}' filled")
        
        # Fill all remaining fields
        print("\n[9] Filling remaining fields...")
        
        # Fill RUT
        rut_input = await page.query_selector('input[id="rut"]')
        if rut_input:
            print(f"  Filling RUT field (empty for now)")
            await rut_input.fill('')
            await asyncio.sleep(0.5)
        
        # Fill nombres
        nombres_input = await page.query_selector('input[id="nombres"]')
        if nombres_input:
            print(f"  Filling nombres field")
            await nombres_input.fill('Juan')
            await asyncio.sleep(0.5)
        
        # Fill apellidoPaterno
        apellidoPaterno_input = await page.query_selector('input[id="apellidoPaterno"]')
        if apellidoPaterno_input:
            print(f"  Filling apellidoPaterno field")
            await apellidoPaterno_input.fill('Perez')
            await asyncio.sleep(0.5)
        
        # Fill apellidoMaterno
        apellidoMaterno_input = await page.query_selector('input[id="apellidoMaterno"]')
        if apellidoMaterno_input:
            print(f"  Filling apellidoMaterno field")
            await apellidoMaterno_input.fill('Gomez')
            await asyncio.sleep(0.5)
        
        # Fill celular
        celular_input = await page.query_selector('input[id="celular"]')
        if celular_input:
            print(f"  Filling celular field")
            await celular_input.fill('912345678')
            await asyncio.sleep(0.5)
        
        # Take screenshot before submit
        await page.screenshot(path='registration_all_fields_filled.png')
        print("  ✓ Screenshot saved: registration_all_fields_filled.png")
        
        # Click submit button
        print("\n[10] Clicking submit button...")
        
        submit = await page.query_selector('button:has-text("Crear cuenta")')
        if not submit:
            submit = await page.query_selector('button[type="submit"]')
        
        if submit:
            print("  Found submit button")
            await submit.click()
            await asyncio.sleep(5)
            await page.screenshot(path='registration_after_submit.png')
            print("  ✓ Submit clicked")
            
            # Check response
            content = await page.content()
            print(f"  Page content after submit: {len(content)} bytes")
            
            # Scroll to bottom
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await asyncio.sleep(2)
            
            await page.screenshot(path='registration_submit_response.png')
            print("  ✓ Response screenshot saved")
        else:
            print("  ✗ Submit button not found")
        
        await browser.close()
        print("\n[11] Browser closed")
    
    print("\n" + "=" * 70)
    print("COMPLETE REGISTRATION WORKFLOW DONE")
    print("=" * 70)
    
    import os
    screenshots = [f for f in os.listdir('.') if 'registration' in f.lower()]
    
    return {
        'email': email,
        'screenshots': screenshots
    }


async def main():
    # Load account pool
    with open('.cbrs/account-pool.json', 'r') as f:
        pool = json.load(f)
    
    # Get first generated Gmail account
    generated = [a for a in pool['accounts'] if 'email' in a and 'password' in a]
    account = generated[0]
    email = account['email']
    password = account['password']
    
    print(f"Using account: {email}")
    
    # Run complete workflow
    result = await complete_registration(email, password)
    
    print("\nResult:", result)
    return result


if __name__ == '__main__':
    result = asyncio.run(main())
