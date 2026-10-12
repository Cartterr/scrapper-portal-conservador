import sqlite3
import time
import re
import uuid
from datetime import datetime
from playwright.sync_api import sync_playwright

conn = sqlite3.connect('rut_data.db')
cursor = conn.cursor()

# Clear any existing data first
cursor.execute("DELETE FROM ruts")
conn.commit()
print("Database cleared. Starting fresh scrape...")
print("=" * 60)

def scrape_current_page():
    """Extract data from the current page."""
    page_data = []
    
    # Find all rows in the table
    rows = page.query_selector_all('table tr')
    
    for row in rows[1:]:  # Skip header
        cells = row.query_selector_all('td')
        if len(cells) >= 2:
            name = cells[0].text_content().strip()
            rut = cells[1].text_content().strip()
            
            if name and rut:
                page_data.append({'rut': rut, 'name': name})
    
    return page_data

def scrape_next_page():
    """Scrape next page by clicking 'Buscar otro'."""
    page.click('a:has-text("Buscar otro")')
    time.sleep(2)
    
    results = scrape_current_page()
    time.sleep(0.5)
    return results

# Clear browser navigation history to start fresh
browser = playwright_browser_page.browser
page.goto('https://www.nombrerutyfirma.com/buscarRut')
time.sleep(1.5)

# Fill with "1" and search
page.fill('input[name="rut"]', '1')
time.sleep(0.5)
page.click('button[aria-label="Buscar"]')
time.sleep(2)

print("Scraping page 1 (RUTs starting with 1)...")
results1 = scrape_current_page()
print(f"  Found {len(results1)} RUTs")

# Insert these
for item in results1:
    cursor.execute('''
        INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (str(uuid.uuid4()), item['rut'], item['name'], 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
    conn.commit()

# Click to next page
page.click('a:has-text("Buscar otro")')
time.sleep(2)

print("\nScraping page 2...")
results2 = scrape_current_page()
print(f"  Found {len(results2)} RUTs")

for item in results2:
    cursor.execute('''
        INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (str(uuid.uuid4()), item['rut'], item['name'], 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
    conn.commit()

# Continue scraping more pages
for page_num in range(2, 11):
    try:
        page.click('a:has-text("Buscar otro")')
        time.sleep(2)
        
        results = scrape_current_page()
        if results:
            print(f"\nScraping page {page_num + 1}...")
            print(f"  Found {len(results)} RUTs")
            
            for item in results:
                cursor.execute('''
                    INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (str(uuid.uuid4()), item['rut'], item['name'], 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
                conn.commit()
        else:
            print(f"\nPage {page_num + 1} has no results. Moving to next number.")
            break
        
        time.sleep(0.5)
        
    except Exception as e:
        print(f"\nError on page {page_num + 1}: {str(e)[:80]}")
        break

browser.close()

# Final count
cursor.execute("SELECT COUNT(*) FROM ruts")
total_count = cursor.fetchone()[0]

print("\n" + "=" * 60)
print(f"TOTAL RUTs in database: {total_count}")
print("=" * 60)

conn.close()
print("Done!")
