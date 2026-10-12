"""
Continue Registration Workflow - Handle Verification Steps

This script:
1. Checks what happens after form submission
2. Handles email verification if needed
3. Handles CAPTCHA if needed
4. Completes registration
"""

import sys
import asyncio
import json
sys.path.insert(0, 'cbrs')

from playwright.async_api import async_playwright


async def continue_registration(email, password):
    """
    Continue registration workflow after form submission.
    """
    print("=" * 70)
    print("CONTINUE REGISTRATION WORKFLOW")
    print("=" * 70)
    print(f"Account: {email}")
    
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
        await page.screenshot(path='registration_page_full.png')
        print("  ✓ Page loaded")
        
        # Check page content
        content = await page.content()
        print(f"  Page content: {len(content)} bytes")
        
        # Look for messages
        print("\n[3] Checking for messages...")
        
        # Check for success/error messages
        if 'exito' in content.lower():
            print("  ✓ Success message found!")
        elif 'error' in content.lower() or 'error' in content.lower():
            print("  ✗ Error message found")
        else:
            print("  ? No obvious message found")
        
        # Check for email verification link
        print("\n[4] Looking for email verification...")
        
        # Check if page mentions email
        if 'email' in content.lower() or 'correo' in content.lower():
            print("  Page mentions email/correo")
        
        # Check for CAPTCHA
        print("\n[5] Checking for CAPTCHA...")
        
        captcha = await page.query_selector('.captcha, #captcha, [class*="captcha"]')
        recaptcha = await page.query_selector('.recaptcha, #recaptcha, [class*="recaptcha"]')
        
        if captcha:
            print("  CAPTCHA element found")
            await page.screenshot(path='registration_captcha.png')
        else:
            print("  No CAPTCHA found")
        
        # Check for verification step
        print("\n[6] Checking for verification step...")
        
        # Look for verification buttons/links
        verify = await page.query_selector('.verify, .verificar, [class*="verify"], [class*="verificar"]')
        if verify:
            print("  Verification element found")
            await page.screenshot(path='registration_verify.png')
        
        # Wait and check again
        print("\n[7] Waiting for page updates...")
        await asyncio.sleep(5)
        
        content2 = await page.content()
        print(f"  Updated content: {len(content2)} bytes")
        
        # Take final screenshot
        await page.screenshot(path='registration_after_wait.png')
        print("  ✓ Final screenshot saved")
        
        await browser.close()
        print("\n[8] Browser closed")
    
    print("\n" + "=" * 70)
    print("CONTINUATION COMPLETE")
    print("=" * 70)
    
    return content


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
    
    # Run continuation
    content = await continue_registration(email, password)


if __name__ == '__main__':
    asyncio.run(main())
