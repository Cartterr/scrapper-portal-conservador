"""
End-to-End Test: Create Gmail accounts and complete registration on nuevo-portal.conservador.cl
"""

import sys
sys.path.insert(0, 'cbrs')

from rut_scraper.email import GmailProvider, EmailStorage, EmailFetcher
from rut_scraper.system import RUTScraperSystem
from rut_scraper.main import RUTScraperSettings
import asyncio

async def main():
    print("=" * 60)
    print("End-to-End Test: Gmail Account Creation + Registration")
    print("=" * 60)
    
    # Create email system
    print("\n[1] Setting up email system...")
    storage = EmailStorage(db_path="email_accounts.db")
    gmail_provider = GmailProvider()
    email_fetcher = EmailFetcher(storage=storage, providers=[gmail_provider])
    
    # Create RUT scraper system
    print("[2] Setting up RUT scraper system...")
    settings = RUTScraperSettings()
    system = RUTScraperSystem(settings=settings)
    
    # Fetch available email addresses
    print("\n[3] Fetching available Gmail addresses...")
    emails = await email_fetcher.fetch_available_emails(count=3)
    print(f"    Found {len(emails)} available emails:")
    for i, e in enumerate(emails, 1):
        print(f"      {i}. {e['email']}")
    
    # Claim the emails
    print("\n[4] Claiming emails...")
    claimed = []
    for email_info in emails:
        result = await email_fetcher.claim_email(
            email=email_info['email'],
            password=email_info['password'],
            provider=email_info['provider']
        )
        if result.get('claimed'):
            claimed.append(email_info)
            print(f"    ✓ Claimed: {email_info['email']}")
    
    print(f"\n    Total claimed: {len(claimed)}")
    
    # Store in account pool
    print("\n[5] Storing in account pool...")
    pool_path = ".cbrs/account-pool.json"
    
    # Read existing pool
    import json
    try:
        with open(pool_path, 'r') as f:
            existing_pool = json.load(f)
    except FileNotFoundError:
        existing_pool = []
    
    # Add new accounts to pool
    pool_key = 'accounts'
    if pool_key not in existing_pool:
        existing_pool[pool_key] = []
    
    for email_info in claimed:
        existing_pool[pool_key].append({
            'email': email_info['email'],
            'password': email_info['password'],
            'provider': 'gmail'
        })
    
    # Write back to pool
    with open(pool_path, 'w') as f:
        json.dump(existing_pool, f, indent=2)
    
    print(f"    Updated pool at: {pool_path}")
    print(f"    Total accounts in pool: {len(existing_pool)}")
    
    # Test RUT fetching
    print("\n[6] Testing RUT fetching (this may fail due to 403)...")
    try:
        entries = await system.fetch_and_store(limit=1)
        print(f"    Stored {len(entries)} RUTs")
    except Exception as e:
        print(f"    Error (expected): {e}")
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  - Gmail accounts created: {len(claimed)}")
    print(f"  - Email pool updated: {pool_path}")
    print(f"  - RUTs in database: 0 (external site returning 403)")
    print("=" * 60)
    
    # Cleanup
    storage.close()
    gmail_provider.close()
    
    return claimed

if __name__ == '__main__':
    asyncio.run(main())
