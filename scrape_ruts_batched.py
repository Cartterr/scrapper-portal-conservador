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

def scrape_rut_batch(start_num, end_num):
    """Scrape RUTs from a batch of numbers."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # Headed for debugging
        page = browser.new_page()
        
        all_results = []
        
        for num in range(start_num, end_num + 1):
            rut_input = str(num)
            
            try:
                # Navigate to search page
                page.goto('https://www.nombrerutyfirma.com/buscarRut')
                time.sleep(1.5)
                
                # Fill and submit
                page.fill('input[name="rut"]', rut_input)
                time.sleep(0.5)
                
                # Click search button
                page.click('button[aria-label="Buscar"]')
                time.sleep(2)
                
                # Get results
                page.wait_for_load_state('networkidle')
                
                # Extract RUTs from table
                table = page.query_selector('table')
                if not table:
                    print(f"  No table found for {rut_input}")
                    continue
                
                rows = table.query_selector_all('tr')
                
                for row in rows[1:]:  # Skip header
                    cells = row.query_selector_all('td')
                    if len(cells) >= 2:
                        name = cells[0].text_content().strip()
                        rut = cells[1].text_content().strip()
                        
                        if name and rut:
                            all_results.append({
                                'rut': rut,
                                'name': name
                            })
                
                # Small delay between requests
                time.sleep(0.8)
                
            except Exception as e:
                print(f"  Error for {rut_input}: {str(e)[:100]}")
                continue
        
        browser.close()
        return all_results

def insert_to_database(ruts_list):
    """Insert scraped RUTs into database."""
    inserted_count = 0
    
    for item in ruts_list:
        cursor.execute('''
            INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (str(uuid.uuid4()), item['rut'], item['name'], 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
        conn.commit()
        inserted_count += 1
    
    return inserted_count

# Batch 1: Numbers 1-50
print("\nBatch 1: Scraping numbers 1-50...")
results1 = scrape_rut_batch(1, 50)
inserted1 = insert_to_database(results1)
print(f"  Found {len(results1)} RUTs, inserted {inserted1}")

# Batch 2: Numbers 51-100
print("\nBatch 2: Scraping numbers 51-100...")
results2 = scrape_rut_batch(51, 100)
inserted2 = insert_to_database(results2)
print(f"  Found {len(results2)} RUTs, inserted {inserted2}")

# Batch 3: Numbers 101-150
print("\nBatch 3: Scraping numbers 101-150...")
results3 = scrape_rut_batch(101, 150)
inserted3 = insert_to_database(results3)
print(f"  Found {len(results3)} RUTs, inserted {inserted3}")

# Batch 4: Numbers 151-200
print("\nBatch 4: Scraping numbers 151-200...")
results4 = scrape_rut_batch(151, 200)
inserted4 = insert_to_database(results4)
print(f"  Found {len(results4)} RUTs, inserted {inserted4}")

# Batch 5: Numbers 201-250
print("\nBatch 5: Scraping numbers 201-250...")
results5 = scrape_rut_batch(201, 250)
inserted5 = insert_to_database(results5)
print(f"  Found {len(results5)} RUTs, inserted {inserted5}")

# Final count
cursor.execute("SELECT COUNT(*) FROM ruts")
total_count = cursor.fetchone()[0]

print("\n" + "=" * 60)
print(f"TOTAL RUTs in database: {total_count}")
print("=" * 60)

conn.close()
print("Done!")
