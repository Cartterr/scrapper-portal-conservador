import asyncio
import json
import sys
sys.path.insert(0, 'cbrs')
from playwright.async_api import async_playwright

async def fill_all_fields():
    print("=" * 70)
    print("FILL ALL FIELDS - SIMPLE VERSION")
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
        
        print("\n[1] Navigating to registration...")
        await page.goto('https://nuevo-portal.conservador.cl/crear-cuenta', wait_until='networkidle')
        await asyncio.sleep(3)
        await page.screenshot(path='registration_simple.png')
        
        print("\n[2] Finding all input fields...")
        all_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input[type="password"], textarea')
        print(f"  Found {len(all_inputs)} input fields")
        
        for i, inp in enumerate(all_inputs):
            try:
                id_val = await inp.get_attribute('id')
                name_val = await inp.get_attribute('name')
                placeholder = await inp.get_attribute('placeholder')
                value = await inp.input_value()
                
                print(f"\n  Input {i+1}:")
                print(f"    ID: {id_val or 'N/A'}")
                print(f"    Name: {name_val or 'N/A'}")
                print(f"    Placeholder: {placeholder or 'N/A'}")
                print(f"    Current value: {value or '(empty)'}")
                
                # Fill with sample data
                if id_val:
                    fill_value = ''
                    if 'rut' in id_val.lower():
                        fill_value = '12345678-9'
                    elif 'nombres' in id_val.lower():
                        fill_value = 'Juan'
                    elif 'apellido' in id_val.lower():
                        fill_value = 'Perez'
                    elif 'direccion' in id_val.lower():
                        fill_value = 'Av. Providencia 123'
                    elif 'celular' in id_val.lower():
                        fill_value = '912345678'
                    
                    if fill_value:
                        print(f"    Filling with: {fill_value}")
                        await inp.fill(fill_value)
                        await asyncio.sleep(0.5)
                        print(f"    ✓ Filled")
                    
            except Exception as e:
                print(f"    Error: {e}")
        
        print("\n[3] Finding submit button...")
        submit_btns = await page.query_selector_all('button')
        for btn in submit_btns:
            try:
                text = await btn.inner_text()
                if 'crear' in text.lower() or 'submit' in text.lower():
                    print(f"  Found submit button: {text}")
                    await btn.click()
                    await asyncio.sleep(3)
                    await page.screenshot(path='registration_after_submit_simple.png')
                    break
            except:
                pass
        
        await browser.close()
        print("\n[4] Done")

asyncio.run(fill_all_fields())
