"""
CBRS RUT Scraper - Using Playwright with JavaScript to load results
"""

from playwright.sync_api import sync_playwright
import sqlite3
import os
import re

# Database path
DB_PATH = os.path.join(os.path.dirname(__file__), 'rut_data.db')


def scrape_rut_by_source(source_url: str, name: str) -> list:
    """
    Scrape RUT data from a given source by searching for a name.
    Uses Playwright with JavaScript to load results.
    """
    ruts = []
    
    with sync_playwright() as p:
        # Launch browser with anti-detection flags
        browser = p.chromium.launch(
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-site-isolation-trials',
                '--disable-features=IsolateOrigins,site-per-process',
            ]
        )
        page = browser.new_page()
        
        try:
            # Navigate to the source
            print(f"  Navigating to {source_url}...")
            page.goto(source_url)
            page.wait_for_timeout(2000)
            
            # Find search input
            textbox = page.query_selector('input[placeholder*="nombre"]')
            if not textbox:
                textbox = page.query_selector('input[type="text"]')
            
            if textbox:
                print(f"  Filling search form with: {name}")
                textbox.fill(name)
                
                # Find and click search button
                search_button = page.query_selector('button')
                if search_button:
                    print(f"  Clicking search button...")
                    search_button.click()
                    page.wait_for_timeout(5000)
                    
                    # Extract results from the page
                    html_content = page.content()
                    
                    # Try to parse HTML for RUT data
                    ruts = parse_rut_results(html_content, source_url)
            
            return ruts
            
        except Exception as e:
            print(f"Error scraping {source_url}: {e}")
            return []
        finally:
            browser.close()


def parse_rut_results(html_content: str, source_url: str) -> list:
    """
    Parse HTML content to extract RUT data.
    Returns list of dicts with rut, name, company fields.
    """
    results = []
    
    # Try to find RUT entries in the HTML
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html_content, re.DOTALL)
    
    for row in rows:
        # Try to extract RUT from row
        rut_matches = re.findall(r'[0-9]{1,7}[K0-9]', row)
        
        for rut in rut_matches:
            # Try to get name from same row
            name_match = re.search(r'(?:Nombre|Nombres?)[:\s]*([A-Za-záéíóúÁÉÍÓÚñÑ]+)', row)
            name = name_match.group(1) if name_match else 'Unknown'
            
            results.append({
                'rut': rut,
                'name': name,
                'company': None,
                'source': source_url
            })
    
    return results


def save_to_database(ruts: list, source_url: str):
    """Save RUT data to SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create table if not exists
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ruts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rut TEXT UNIQUE NOT NULL,
            name TEXT,
            company TEXT,
            source TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Insert results
    for rut in ruts:
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO ruts (rut, name, company, source)
                VALUES (?, ?, ?, ?)
            ''', (
                rut['rut'],
                rut['name'],
                rut['company'],
                rut['source']
            ))
            print(f"Saved: RUT={rut['rut']}, Name={rut['name']}")
        except sqlite3.IntegrityError:
            print(f"Already exists: RUT={rut['rut']}")
    
    conn.commit()
    conn.close()


def main():
    """Main function to test scraping and save to database."""
    
    # Test with a specific source
    source_url = "https://www.rutificador.co/"
    
    print(f"\n{'='*60}")
    print("CBRS RUT Scraper - Testing with Playwright")
    print(f"{'='*60}\n")
    
    # Test search with a common name
    test_name = "Gonzalez"
    results = scrape_rut_by_source(source_url, test_name)
    
    print(f"\nFound {len(results)} results for '{test_name}'")
    
    if results:
        save_to_database(results, source_url)
    
    print("Done!")


if __name__ == "__main__":
    main()
