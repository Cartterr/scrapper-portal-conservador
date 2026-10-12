"""
CBRS RUT Scraper - Direct approach with Playwright
"""
from playwright.sync_api import sync_playwright
import time
import re

def scrape_all_sources():
    """Try all available sources to get RUT data"""
    
    sources = {
        'rutificador.co': 'https://www.rutificador.co/buscar/',
        'rutyfirma.com': 'https://rutyfirma.com/buscar/',
    }
    
    # Common Chilean names to search
    names = ['Garcia', 'Perez', 'Soto', 'Vargas', 'Torres', 'Munoz']
    
    all_ruts = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
            ]
        )
        
        for source_name, url in sources.items():
            print(f"\n{'='*60}")
            print(f"Source: {source_name}")
            print(f"{'='*60}")
            
            page = browser.new_page()
            page.goto(url)
            page.wait_for_load_state('networkidle')
            
            # Try to find and fill search form
            try:
                # Look for text input
                textbox = page.query_selector('input[type="text"]')
                if textbox:
                    print(f"Found text input")
                    textbox.fill('Garcia')
                    
                    # Click search button
                    button = page.query_selector('button[type="submit"]')
                    if not button:
                        button = page.query_selector('button')
                    
                    if button:
                        print("Clicking search button")
                        button.click()
                        
                        # Wait for results
                        print("Waiting for results...")
                        page.wait_for_timeout(20000)
                        
                        # Get page content
                        content = page.content()
                        
                        # Check status
                        if 'Buscando' in content:
                            print("Still searching...")
                        elif 'Resultados' in content and 'Buscando' not in content:
                            print("Results loaded!")
                            # Extract data
                            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', content, re.DOTALL)
                            for row in rows:
                                ruts = re.findall(r'[0-9]{1,7}[K0-9]', row)
                                names_found = re.findall(r'(?:Nombre|Nombres?)[:\s]*([A-Za-záéíóúÁÉÍÓÚñÑ]+)', row)
                                for rut in ruts:
                                    name = names_found[0] if names_found else 'Unknown'
                                    all_ruts.append({
                                        'rut': rut,
                                        'name': name,
                                        'source': source_name
                                    })
                                    print(f"Found: RUT={rut}, Name={name}")
                        
                        else:
                            print("No results found or unknown status")
                            
            except Exception as e:
                print(f"Error: {e}")
            
            page.close()
        
        browser.close()
    
    return all_ruts

if __name__ == "__main__":
    ruts = scrape_all_sources()
    print(f"\nTotal RUTs found: {len(ruts)}")
    for rut in ruts:
        print(f"  {rut}")
