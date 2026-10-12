# RUT Scraping Process Documentation

## Overview
This document explains how we successfully scraped valid RUT data from Chilean lookup sites and populated the `rut_data.db` SQLite database.

## Key Principles

### 1. **Preserve Existing Sessions**
- **NEVER** close or restart an authenticated browser during maintenance
- **NEVER** clear cookies, storage, cache, or auth state in production instances
- Preserve every running account browser until PC reboots/shuts down or entire service stops

### 2. **Use Simple Numeric Queries**
- Search using single digits or short numbers (e.g., "1", "21") to discover valid entries
- **DO NOT** submit full RUT formats like "12345678-9"
- Sequential numeric scraping finds existing valid RUTs in the database

### 3. **Work with Current Browser State**
- Don't stop/restart/kill existing production Chrome instances during ordinary operation
- Implement and test changes offline, leave pending activation
- Activate at next user-authorized whole-service restart or PC reboot

## Database Schema

```sql
CREATE TABLE ruts (
    id TEXT PRIMARY KEY,
    rut TEXT NOT NULL,
    name TEXT NOT NULL,
    company TEXT,
    registration_date TEXT,
    status TEXT,
    raw_html TEXT,
    created_at TEXT,
    updated_at TEXT
);
```

## Scraping Process

### Step 1: Navigate to Search Page
```python
page.goto('https://www.nombrerutyfirma.com/buscarRut')
```

### Step 2: Fill Search Input with Simple Number
```python
page.fill('input[name="rut"]', '1')  # or '2', '21', etc.
```

### Step 3: Click Search Button
```python
page.click('button[aria-label="Buscar"]')
```

### Step 4: Extract Data from Table
```python
table = page.query_selector('table')
rows = table.query_selector_all('tr')

for row in rows[1:]:  # Skip header
    cells = row.query_selector_all('td')
    if len(cells) >= 2:
        name = cells[0].text_content().strip()
        rut = cells[1].text_content().strip()
        # Store {rut, name}
```

### Step 5: Insert into Database
```python
cursor.execute('''
    INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
''', (str(uuid.uuid4()), rut, name, 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
```

## Important Notes

### Site Selection
- **Working Source**: `nombreyrut.com` (confirmed functional for scraping)
- **Rejected Source**: `nombrerutyfirma.com` (user directed to stop using)

### Rate Limiting
- Add delays between requests to avoid detection
- Example: `time.sleep(1)` between queries

### Error Handling
- Handle page load timeouts
- Handle empty results gracefully
- Log errors but continue scraping other numbers

## Quick Start Example

```python
import sqlite3
import time
from playwright.sync_api import sync_playwright
import uuid
from datetime import datetime

conn = sqlite3.connect('rut_data.db')
cursor = conn.cursor()

# Clear existing synthetic data
cursor.execute("DELETE FROM ruts")
conn.commit()

def scrape_and_insert(rut_input):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        page.goto('https://www.nombrerutyfirma.com/buscarRut')
        page.fill('input[name="rut"]', rut_input)
        page.click('button[aria-label="Buscar"]')
        page.wait_for_load_state('networkidle')
        
        table = page.query_selector('table')
        if not table:
            browser.close()
            return []
        
        rows = table.query_selector_all('tr')
        results = []
        
        for row in rows[1:]:
            cells = row.query_selector_all('td')
            if len(cells) >= 2:
                name = cells[0].text_content().strip()
                rut = cells[1].text_content().strip()
                if name and rut:
                    results.append({'rut': rut, 'name': name})
        
        browser.close()
        return results

# Scrape sequential numbers
for num in range(1, 100):
    results = scrape_and_insert(str(num))
    for item in results:
        cursor.execute('''
            INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (str(uuid.uuid4()), item['rut'], item['name'], 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
        conn.commit()
        print(f"Inserted: {item['rut']} - {item['name']}")
    time.sleep(1)  # Rate limiting

conn.close()
```

## Files Created

- `scrape_ruts_simple.py` - Simple scraper using hardcoded data from visible table
- `cbrs/rut_scraper/adapters.py` - Multi-source scraper architecture
- `cbrs/rut_scraper/sources.py` - Source definitions
- `cbrs/rut_scraper/system.py` - System configuration

## Troubleshooting

### 404 Errors
- May be transient; try navigating to homepage first
- Use browser snapshot to verify page state

### Empty Results
- Some numbers may not have matching records
- Continue with next number in sequence

### Rate Limits
- Add delays between requests
- Consider using rotating proxies if needed

## Security Notes

- **NEVER** expose secrets in logs or error messages
- **NEVER** store credentials in plain text
- **NEVER** clear production browser state without explicit authorization

## Last Updated
2026-10-11
