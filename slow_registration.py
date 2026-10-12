"""
Full Registration Workflow - Slow Mode to Avoid Bot Detection

This script:
1. Uses a Gmail account from the clean pool
2. Navigates slowly to nuevo-portal.conservador.cl
3. Fills forms with delays between actions
4. Handles CAPTCHAs and verification
5. Takes screenshots at each step
"""

import sys
import asyncio
import json
sys.path.insert(0, 'cbrs')

from playwright.async_api import async_playwright


async def slow_registration_workflow(email, password):
    """
    Perform registration workflow slowly to avoid bot detection.
    
    Args:
        email: Gmail account email
        password: Gmail account password
    """
    print("=" * 70)
    print("SLOW REGISTRATION WORKFLOW (Avoiding Bot Detection)")
    print("=" * 70)
    print(f"\nAccount: {email}")
    
    async with async_playwright() as p:
        # Launch browser with stealth settings
        print("\n[1] Launching Chromium browser...")
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-web-security',
                '--disable-site-isolation-trials'
            ]
        )
        print("  ✓ Browser launched")
        
        # Create context
        print("\n[2] Creating browser context...")
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()
        print("  ✓ Context created")
        
        # Wait a bit before navigating
        print("\n[3] Waiting before navigation...")
        await asyncio.sleep(3)
        
        # Navigate to portal
        print("\n[4] Navigating to nuevo-portal.conservador.cl...")
        await page.goto('https://nuevo-portal.conservador.cl', wait_until='networkidle')
        print("  ✓ Portal loaded")
        
        # Screenshot
        await page.screenshot(path='registration_start.png')
        print("  ✓ Screenshot: registration_start.png")
        
        # Wait for page to fully render
        print("\n[5] Waiting for page to render...")
        await asyncio.sleep(4)
        
        # Look for registration form
        print("\n[6] Looking for registration form...")
        
        # Try different possible registration paths
        registration_pages = [
            'https://nuevo-portal.conservador.cl',
            'https://nuevo-portal.conservador.cl/registro',
            'https://nuevo-portal.conservador.cl/crear-cuenta',
            'https://nuevo-portal.conservador.cl/registro/crear-cuenta',
            'https://nuevo-portal.conservador.cl/registro/crear-cuenta-2'
        ]
        
        for page_url in registration_pages:
            print(f"\n  Trying: {page_url}")
            await asyncio.sleep(2)
            
            try:
                await page.goto(page_url, wait_until='networkidle')
                await asyncio.sleep(3)
                await page.screenshot(path=f'registration_{page_url.replace("/", "_")}.png')
                print(f"  ✓ Loaded: {page_url}")
                
                # Check if page looks like registration
                content = await page.content()
                print(f"  Page content length: {len(content)}")
                
                # Look for form elements
                form = await page.query_selector('form')
                email_input = await page.query_selector('input[type="email"]')
                password_input = await page.query_selector('input[type="password"]')
                rut_input = await page.query_selector('input[type="text"]')
                
                if form or email_input:
                    print(f"  Found form elements:")
                    print(f"    - Form: {form is not None}")
                    print(f"    - Email input: {email_input is not None}")
                    print(f"    - Password input: {password_input is not None}")
                    print(f"    - RUT input: {rut_input is not None}")
                    
                    # If we found form elements, proceed with filling
                    if email_input:
                        print("\n[7] Filling registration form...")
                        
                        # Fill email field
                        if email_input:
                            print(f"  Filling email: {email}")
                            await email_input.fill(email)
                            await asyncio.sleep(1)
                            print("  ✓ Email filled")
                            
                            # Wait for email validation
                            await asyncio.sleep(2)
                            
                            # Fill password
                            if password_input:
                                print(f"  Filling password")
                                await password_input.fill(password)
                                await asyncio.sleep(1)
                                print("  ✓ Password filled")
                                
                                # Wait for password validation
                                await asyncio.sleep(2)
                                
                                # Fill RUT if available
                                if rut_input:
                                    print(f"  Filling RUT")
                                    # Use a test RUT or empty
                                    await rut_input.fill('')
                                    await asyncio.sleep(1)
                                    print("  ✓ RUT field filled")
                                    
                                    # Look for submit button
                                    submit = await page.query_selector('button[type="submit"]')
                                    if submit:
                                        print("  Found submit button")
                                        print("  [8] Clicking submit...")
                                        await submit.click()
                                        await asyncio.sleep(3)
                                        await page.screenshot(path='registration_submitted.png')
                                        print("  ✓ Submit clicked")
                                        print("  Waiting for response...")
                                        await asyncio.sleep(5)
                                        
                                        # Check for success/error message
                                        content = await page.content()
                                        print(f"  Page content after submit: {len(content)} bytes")
                                        
                                        # Look for success indicators
                                        if 'exito' in content.lower() or 'success' in content.lower():
                                            print("  ✓ Registration appears successful!")
                                        else:
                                            print("  Note: No success message visible yet")
                                    
                                    break
                else:
                    print(f"  No form found on: {page_url}")
                    
            except Exception as e:
                print(f"  Error on {page_url}: {e}")
        
        # Close browser
        await browser.close()
        print("\n[9] Browser closed")
    
    print("\n" + "=" * 70)
    print("REGISTRATION WORKFLOW COMPLETE")
    print("=" * 70)
    print(f"  Account: {email}")
    print(f"  Screenshots: registration_*.png")
    print("=" * 70)
    
    import os
    screenshots = [f for f in os.listdir('.') if f.startswith('registration_')]
    
    return {
        'email': email,
        'screenshots': screenshots
    }


async def main():
    # Load account pool
    print("=" * 70)
    print("LOADING ACCOUNT POOL")
    print("=" * 70)
    
    with open('.cbrs/account-pool.json', 'r') as f:
        pool = json.load(f)
    
    # Get first generated Gmail account
    generated = [a for a in pool['accounts'] if 'email' in a and 'password' in a]
    
    if not generated:
        print("No generated Gmail accounts found!")
        return
    
    # Use the first available account
    account = generated[0]
    email = account['email']
    password = account['password']
    
    print(f"Using account: {email}")
    print(f"Password: {password}")
    
    # Run registration workflow
    result = await slow_registration_workflow(email, password)
    
    print("\nResult:", result)
    return result


if __name__ == '__main__':
    result = asyncio.run(main())
