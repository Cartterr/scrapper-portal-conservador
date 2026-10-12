import sqlite3
import os

db_path = os.path.join(os.getcwd(), 'rut_data.db')

if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if table exists and count records
    cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
    tables = cursor.fetchall()
    print('Tables:', tables)
    
    if tables:
        cursor.execute('SELECT COUNT(*) FROM ruts')
        count = cursor.fetchone()[0]
        print(f'RUT records: {count}')
        
        cursor.execute('SELECT rut, name, company FROM ruts LIMIT 10')
        rows = cursor.fetchall()
        print('\nSample records:')
        for row in rows:
            print(f'  {row}')
    
    conn.close()
else:
    print('Database does not exist yet')
