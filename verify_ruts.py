import sqlite3

conn = sqlite3.connect('rut_data.db')
cursor = conn.cursor()

# Verify data
cursor.execute('SELECT COUNT(*) FROM ruts')
total = cursor.fetchone()[0]
print(f'Total RUTs in database: {total:,}')

cursor.execute('SELECT DISTINCT status FROM ruts')
statuses = cursor.fetchall()
print(f'Statuses: {statuses}')

cursor.execute('SELECT COUNT(*) FROM ruts WHERE status = "pending"')
pending = cursor.fetchone()[0]
print(f'Pending RUTs: {pending:,}')

cursor.execute('SELECT MIN(rut), MAX(rut) FROM ruts')
min_rut, max_rut = cursor.fetchone()
print(f'RUT range: {min_rut} to {max_rut}')

conn.close()
