import json

with open('.cbrs/account-pool.json', 'r') as f:
    pool = json.load(f)

gmail_accounts = [a for a in pool['accounts'] if 'email' in a and 'password' in a]

# Update passwords to include special characters
for acc in gmail_accounts:
    password = acc['password']
    # If password doesn't have special chars, add one
    if not any(not c.isalnum() for c in password):
        # Add special character while keeping complexity
        acc['password'] = password + '!'
        print(f'Updated {acc["email"]} password to: {acc["password"]}')

# Save back
with open('.cbrs/account-pool.json', 'w') as f:
    json.dump(pool, f, indent=2)

print('Account pool updated')
