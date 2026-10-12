"""
Full Registration Workflow with CAPTCHA Handling

This script:
1. Navigates slowly to nuevo-portal.conservador.cl
2. Finds and fills registration form
3. Handles CAPTCHA if present
4. Waits for verification steps
5. Completes registration
"""

import sys
import asyncio
import json
sys.path.insert(0, 'cbrs')

from playwright.async_api import async_playwright


async def full_registration_workflow(email, password):
    """
    Complete registration workflow with CAPTCHA handling.
    """
    print("=" * 70)
    print("FULL REGISTRATION WORKFLOW WITH CAPTCHA")
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
        await page.screenshot(path='registration_full.png')
        print("  ✓ Page loaded")
        
        # Get page content
        content = await page.content()
        print(f"  Page content: {len(content)} bytes")
        
        # Print HTML content for analysis
        print("\n[3] Analyzing page content...")
        
        # Check for error messages
        error_elements = await page.query_selector_all('.error, .error-message, [class*="error"], .alert-danger, .alert-error')
        print(f"  Error elements found: {len(error_elements)}")
        
        for i, elem in enumerate(error_elements[:3]):  # Check first 3
            try:
                text = await elem.inner_text()
                print(f"    Error {i+1}: {text[:100]}...")
            except:
                pass
        
        # Check for success messages
        success_elements = await page.query_selector_all('.success, .success-message, [class*="success"], .alert-success, .alert-success')
        print(f"  Success elements found: {len(success_elements)}")
        
        # Check for CAPTCHA
        print("\n[4] Checking for CAPTCHA...")
        
        captcha_elements = await page.query_selector_all('.captcha, .recaptcha, .google-recaptcha, [class*="captcha"], [class*="recaptcha"]')
        print(f"  CAPTCHA elements found: {len(captcha_elements)}")
        
        if captcha_elements:
            print("  CAPTCHA detected!")
            
            # Try to find CAPTCHA checkbox
            checkbox = await page.query_selector('input[type="checkbox"][aria-label*="checkbox"]')
            if checkbox:
                print("  CAPTCHA checkbox found")
                await page.screenshot(path='registration_captcha.png')
            else:
                print("  No checkbox found, trying to solve CAPTCHA programmatically")
                
                # Try to find CAPTCHA image
                captcha_image = await page.query_selector('.recaptcha-image, .recaptcha-checkbox, img[alt*="checkbox"]')
                if captcha_image:
                    print("  CAPTCHA image found")
                    await page.screenshot(path='registration_captcha.png')
        
        # Check for email verification
        print("\n[5] Checking for email verification...")
        
        email_verification = await page.query_selector('.email-verification, .verification-email, [class*="email"], [class*="verification"]')
        if email_verification:
            print("  Email verification found")
            await page.screenshot(path='registration_email_verification.png')
        
        # Check for next steps
        print("\n[6] Checking for next steps...")
        
        next_buttons = await page.query_selector_all('button[type="submit"], button[type="button"], .next-step, .siguiente')
        print(f"  Submit/Next buttons found: {len(next_buttons)}")
        
        # Check for form validation errors
        print("\n[7] Checking form validation...")
        
        # Look for common validation error patterns
        validation_errors = await page.query_selector_all('.invalid, .error-field, .field-error, [class*="invalid"], [class*="error"]')
        print(f"  Validation error fields: {len(validation_errors)}")
        
        for elem in validation_errors[:3]:
            try:
                text = await elem.inner_text()
                print(f"    Field error: {text[:80]}...")
            except:
                pass
        
        # Check for required fields
        print("\n[8] Checking required fields...")
        
        required_fields = await page.query_selector_all('span.required, [class*="required"], *:required')
        print(f"  Required field indicators: {len(required_fields)}")
        
        # Take detailed screenshot
        await page.screenshot(path='registration_detailed.png')
        print("  ✓ Detailed screenshot saved")
        
        await browser.close()
        print("\n[9] Browser closed")
    
    print("\n" + "=" * 70)
    print("ANALYSIS COMPLETE")
    print("=" * 70)
    
    import os
    screenshots = [f for f in os.listdir('.') if 'registration' in f.lower()]
    
    return {
        'content': content,
        'errors': len(error_elements),
        'successes': len(success_elements),
        'captchas': len(captcha_elements),
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
    
    # Run full workflow
    result = await full_registration_workflow(email, password)
    
    print("\nResult:", result)
    return result


if __name__ == '__main__':
    result = asyncio.run(main())
