import json

# Show final account pool status
with open('.cbrs/account-pool.json') as f:
    pool = json.load(f)

exec_count = len([a for a in pool['accounts'] if a.get('id', '').startswith('ejecutivo')])
gen_count = len([a for a in pool['accounts'] if 'email' in a and 'password' in a])
total = len(pool['accounts'])

print('=' * 70)
print('FINAL STATUS: Clean Account Pool')
print('=' * 70)
print(f'Executive accounts: {exec_count}')
print(f'Generated Gmail accounts: {gen_count}')
print(f'Total accounts: {total}')
print()
print('Executive accounts:')
for acc in pool['accounts']:
    if acc.get('id', '').startswith('ejecutivo'):
        print(f'  - {acc["id"]}: {acc["label"]}')

print()
print('Generated Gmail accounts:')
for acc in pool['accounts']:
    if 'email' in acc and 'password' in acc:
        print(f'  - {acc["email"]}')

print()
print('Screenshots created:')
print('  - portal_current.png')
print('  - portal_registration_test.png')
print('  - registro_page.png')
print('=' * 70)
