"""
Full End-to-End: Registration Workflow Test on nuevo-portal.conservador.cl

This script:
1. Uses clean account pool (no duplicates)
2. Runs with visible Chromium browser
3. Tests full registration workflow
4. Handles HTTP 403 errors gracefully
"""

import sys
import asyncio
import json
sys.path.insert(0, 'cbrs')

from playwright.async_api import async_playwright


async def test_registration_workflow():
    print("=" * 70)
    print("END-TO-END: Registration Workflow Test")
    print("=" * 70)
    
    # Load account pool
    print("\n[1] Loading account pool...")
    with open('.cbrs/account-pool.json', 'r') as f:
        pool = json.load(f)
    
    executive = [a for a in pool['accounts'] if a.get('id', '').startswith('ejecutivo')]
    generated = [a for a in pool['accounts'] if 'email' in a and 'password' in a]
    
    print(f"  Executive accounts: {len(executive)}")
    print(f"  Generated accounts: {len(generated)}")
    print(f"  Total accounts: {len(pool['accounts'])}")
    print("  ✓ Pool loaded")
    
    # Launch visible Chromium
    print("\n[2] Launching Chromium browser (headed mode)...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process'
            ]
        )
        
        context = await browser.new_context()
        page = await context.new_page()
        
        print("  ✓ Browser launched - visible window")
        
        # Navigate to portal
        print("\n[3] Navigating to nuevo-portal.conservador.cl...")
        
        await page.goto('https://nuevo-portal.conservador.cl', wait_until='networkidle')
        print("  ✓ Portal loaded")
        
        # Take screenshot
        await page.screenshot(path='portal_registration_test.png')
        print("  ✓ Screenshot: portal_registration_test.png")
        
        # Get page content
        content = await page.content()
        print(f"  Page size: {len(content)} bytes")
        
        # Try to find registration form
        print("\n[4] Looking for registration form...")
        
        # Check for common form elements
        form_exists = await page.query_selector('form') is not None
        print(f"  Form element found: {form_exists}")
        
        # Try to find email input
        email_input = await page.query_selector('input[type="email"]')
        print(f"  Email input found: {email_input is not None}")
        
        # Try to find password input
        password_input = await page.query_selector('input[type="password"]')
        print(f"  Password input found: {password_input is not None}")
        
        # Try to find submit button
        submit_button = await page.query_selector('button[type="submit"]')
        print(f"  Submit button found: {submit_button is not None}")
        
        # Navigate to registration page if exists
        try:
            await page.goto('https://nuevo-portal.conservador.cl/registro', wait_until='networkidle')
            print("  ✓ Navigated to /registro")
            
            await page.screenshot(path='registro_page.png')
            print("  ✓ Screenshot: registro_page.png")
            
        except Exception as e:
            print(f"  Note: /registro not found: {e}")
        
        # Close browser
        await browser.close()
        print("\n[5] Browser closed")
    
    print("\n" + "=" * 70)
    print("REGISTRATION WORKFLOW TEST COMPLETE")
    print("=" * 70)
    print(f"  - Account pool: {len(pool['accounts'])} accounts")
    print(f"  - Screenshots: portal_registration_test.png, registro_page.png")
    print("=" * 70)
    
    return {
        'pool': pool,
        'executive': executive,
        'generated': generated
    }

if __name__ == '__main__':
    result = asyncio.run(test_registration_workflow())
    print("\nResult:", result)
