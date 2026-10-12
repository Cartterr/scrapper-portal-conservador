import json

# Read current pool
with open('.cbrs/account-pool.json') as f:
    pool = json.load(f)

# Separate executive accounts from generated emails
executive = [a for a in pool['accounts'] if a.get('id', '').startswith('ejecutivo')]
generated = [a for a in pool['accounts'] if 'email' in a and 'password' in a]

# Deduplicate by email address only (keep first occurrence)
seen_emails = set()
unique_generated = []
for acc in generated:
    email = acc['email']
    if email not in seen_emails:
        seen_emails.add(email)
        unique_generated.append(acc)
        print(f'Kept: {email}')
    else:
        print(f'Skipped duplicate: {email} (password: {acc["password"]})')

# Rebuild pool with only unique accounts
pool['accounts'] = executive + unique_generated
pool['generated_email_count'] = len(unique_generated)

# Write back
with open('.cbrs/account-pool.json', 'w') as f:
    json.dump(pool, f, indent=2)

print()
print('Pool cleaned:')
print(f'  Executive: {len(executive)}')
print(f'  Unique generated: {len(unique_generated)}')
print(f'  Total: {len(pool["accounts"])}')
