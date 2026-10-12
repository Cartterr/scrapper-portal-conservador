import sys
sys.path.insert(0, 'cbrs')

from rut_scraper.email import EmailFetcher, GmailProvider, EmailStorage
import asyncio
import json

async def create_accounts():
    storage = EmailStorage(db_path='email_accounts.db')
    gmail_provider = GmailProvider()
    fetcher = EmailFetcher(storage=storage, providers=[gmail_provider])
    
    # Create 5 fresh unique accounts
    emails = await fetcher.fetch_available_emails(count=5)
    print(f'Created {len(emails)} new accounts:')
    for e in emails:
        print(f'  - {e["email"]}')
    
    # Claim and add to pool
    claimed = []
    for e in emails:
        result = await fetcher.claim_email(
            email=e['email'],
            password=e['password'],
            provider=e['provider']
        )
        if result.get('claimed'):
            claimed.append(e)
            print(f'  Claimed: {e["email"]}')
    
    # Add to pool
    with open('.cbrs/account-pool.json', 'r') as f:
        pool = json.load(f)
    
    for e in claimed:
        pool['accounts'].append({
            'email': e['email'],
            'password': e['password'],
            'provider': 'gmail'
        })
    
    with open('.cbrs/account-pool.json', 'w') as f:
        json.dump(pool, f, indent=2)
    
    print(f'Total accounts in pool: {len(pool["accounts"])}')
    
    storage.close()
    gmail_provider.close()

asyncio.run(create_accounts())
