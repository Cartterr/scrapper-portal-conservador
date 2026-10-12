import json
import sqlite3

# Check RUTs in database
print('Checking for stored RUTs...')

conn = sqlite3.connect('rut_data.db')
cursor = conn.cursor()

# Check table structure
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print(f'Tables: {tables}')

# Check ruts table
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ruts'")
ruts_table = cursor.fetchall()
print(f'RUTs table exists: {len(ruts_table) > 0}')

if ruts_table:
    cursor.execute('SELECT COUNT(*) FROM ruts')
    count = cursor.fetchone()[0]
    print(f'RUTs in database: {count}')
    
    if count > 0:
        cursor.execute('SELECT rut, name, company FROM ruts LIMIT 5')
        rows = cursor.fetchall()
        print('Sample RUTs:')
        for row in rows:
            print(f'  {row[0]} - {row[1]} / {row[2]}')
    else:
        print('No RUTs stored yet')

conn.close()

print('\nAccount pool check:')
with open('.cbrs/account-pool.json', 'r') as f:
    pool = json.load(f)
    
print(f'Total accounts: {len(pool["accounts"])}')
print(f'Executive accounts: {len([a for a in pool["accounts"] if "ejecutivo" in a["email"].lower()])}')
print(f'Generated Gmail accounts: {len([a for a in pool["accounts"] if "email" in a and "password" in a])}')
