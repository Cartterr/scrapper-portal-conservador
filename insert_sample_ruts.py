import sqlite3
import os

db_path = os.path.join(os.getcwd(), 'rut_data.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Insert sample RUTs that are commonly used in Chile
# These are placeholder/sample RUTs for CBRS testing
sample_data = [
    ('11100000-5', 'Juan Perez', 'Perez SA'),
    ('22200000-6', 'Maria Garcia', 'Garcia LTDA'),
    ('33300000-7', 'Carlos Soto', 'Soto SPRL'),
    ('44400000-8', 'Ana Vargas', 'Vargas SRL'),
    ('55500000-9', 'Luis Torres', 'Torres SPA'),
    ('66600000-0', 'Sofia Munoz', 'Munoz S.A.'),
    ('76118158-5', 'Empresa Ejemplo', 'Empresa Ejemplo SPA'),
    ('90001890-8', 'Compania S.A.', 'Compania S.A.'),
]

for rut, name, company in sample_data:
    cursor.execute('''
        INSERT OR IGNORE INTO ruts (rut, name, company)
        VALUES (?, ?, ?)
    ''', (rut, name, company))

conn.commit()
count = cursor.execute('SELECT COUNT(*) FROM ruts').fetchone()[0]
print(f'Inserted/already exists: {count} RUTs')

# Show what we have
cursor.execute('SELECT rut, name, company FROM ruts')
rows = cursor.fetchall()
print('\nCurrent RUTs:')
for row in rows:
    print(f'  {row}')

conn.close()
