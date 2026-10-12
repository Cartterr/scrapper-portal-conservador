"""
Save sample RUTs to database
"""
import asyncio
from cbrs.rut_scraper.adapters import RUTEntry, SqliteRUTRepository
from datetime import datetime, timezone

async def main():
    # Create sample RUT entries
    sample_entries = [
        RUTEntry(
            rut='11100000-5',
            name='Juan Perez',
            company='Perez SA',
            registration_date=datetime.now(timezone.utc).isoformat(),
            status='pending',
            raw_html=None
        ),
        RUTEntry(
            rut='22200000-6',
            name='Maria Garcia',
            company='Garcia LTDA',
            registration_date=datetime.now(timezone.utc).isoformat(),
            status='pending',
            raw_html=None
        ),
        RUTEntry(
            rut='33300000-7',
            name='Carlos Soto',
            company='Soto SPRL',
            registration_date=datetime.now(timezone.utc).isoformat(),
            status='pending',
            raw_html=None
        ),
        RUTEntry(
            rut='44400000-8',
            name='Ana Vargas',
            company='Vargas SRL',
            registration_date=datetime.now(timezone.utc).isoformat(),
            status='pending',
            raw_html=None
        ),
        RUTEntry(
            rut='55500000-9',
            name='Luis Torres',
            company='Torres SPA',
            registration_date=datetime.now(timezone.utc).isoformat(),
            status='pending',
            raw_html=None
        ),
        RUTEntry(
            rut='66600000-0',
            name='Sofia Munoz',
            company='Munoz S.A.',
            registration_date=datetime.now(timezone.utc).isoformat(),
            status='pending',
            raw_html=None
        ),
    ]

    # Save to database
    repo = SqliteRUTRepository('rut_data.db')
    
    saved_count = 0
    for entry in sample_entries:
        result = await repo.save_rut(entry)
        if result:
            saved_count += 1
            print(f'Saved: {entry.rut}')
        else:
            print(f'Skipped (exists): {entry.rut}')
    
    print(f'\nTotal saved: {saved_count}')
    
    # Verify by querying
    all_ruts = await repo.get_all_ruts()
    print(f'\nTotal RUTs in database: {len(all_ruts)}')
    for rut in all_ruts:
        print(f'  {rut.rut}: {rut.name} - {rut.company}')

if __name__ == "__main__":
    asyncio.run(main())
