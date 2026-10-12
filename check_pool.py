import json

with open('.cbrs/account-pool.json', 'r') as f:
    pool = json.load(f)
    
print('Account pool:')
print(f'Total accounts: {len(pool["accounts"])}')

# Check first account structure
first = pool['accounts'][0]
print(f'First account keys: {list(first.keys())}')
print(f'First account: {first}')
