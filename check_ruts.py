import sqlite3
db_path = 'rut_data.db'
try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    print(f'Tables in database: {tables}')
    
    if tables:
        cursor.execute('PRAGMA table_info(rut_entries)')
        columns = cursor.fetchall()
        print(f'Columns: {columns}')
        cursor.execute('SELECT COUNT(*) FROM rut_entries')
        count = cursor.fetchone()[0]
        print(f'RUTs in database: {count}')
    else:
        print('No tables found')
    
    conn.close()
except Exception as e:
    print(f'Error: {e}')
