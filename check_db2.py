import sqlite3
import os

db_path = 'cbrs/rut_scraper/rut_data.db'

# Check if file exists and size
if os.path.exists(db_path):
    size = os.path.getsize(db_path)
    print(f'Database exists, size: {size} bytes')
    
    # Try to read the header
    with open(db_path, 'rb') as f:
        header = f.read(16)
        print(f'Header bytes: {header}')
        print(f'Is SQLite: {header[:16] == b"SQLite format 3"}')
else:
    print('Database does not exist')
