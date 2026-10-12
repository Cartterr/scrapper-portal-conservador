import sqlite3
import time
from playwright.sync_api import sync_playwright
import uuid
from datetime import datetime

conn = sqlite3.connect('rut_data.db')
cursor = conn.cursor()

def scrape_rut(rut):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        try:
            page.goto('https://nombreyrut.com/buscarRut')
            time.sleep(1)
            
            page.fill('input[placeholder="Ej: 12.345.678-9"]', rut)
            time.sleep(0.5)
            
            page.click('button:has-text("Buscar RUT")')
            time.sleep(2)
            
            page.wait_for_load_state('networkidle')
            
            if 'Sin resultados' in page.content():
                return None
            
            table = page.query_selector('table')
            if not table:
                return None
            
            rows = table.query_selector_all('tr')
            for row in rows[1:]:
                cells = row.query_selector_all('td')
                if len(cells) >= 4:
                    data = {
                        'rut': cells[0].text_content().strip(),
                        'nombre': cells[1].text_content().strip(),
                        'sexo': cells[2].text_content().strip(),
                        'direccion': cells[3].text_content().strip(),
                        'ciudad': cells[4].text_content().strip() if len(cells) > 4 else ''
                    }
                    if data['rut'] and data['nombre']:
                        return data
            
            return None
        except Exception as e:
            print(f'Error: {e}')
            return None
        finally:
            browser.close()

# Test a few RUTs
test_ruts = ['11.111.111-1', '22.222.222-2', '33.333.333-3']
for rut in test_ruts:
    result = scrape_rut(rut)
    if result:
        cursor.execute('''
            INSERT OR REPLACE INTO ruts (id, rut, name, registration_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (str(uuid.uuid4()), result['rut'], result['nombre'], 'unknown', 'scraped', datetime.now().isoformat(), datetime.now().isoformat()))
        conn.commit()
        print(f'Inserted: {rut} - {result["nombre"]}')
    else:
        print(f'No data for: {rut}')

conn.close()
