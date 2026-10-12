import sys
sys.path.insert(0, 'cbrs')

from rut_scraper.main import RUTScraperSystem, RUTScraperSettings
import asyncio

s = RUTScraperSystem(RUTScraperSettings())
entries = asyncio.run(s.fetch_and_store(limit=10))
print(f'Stored {len(entries)} RUTs')
for e in entries[:5]:
    print(f'  {e.rut} - {e.name or "Unknown"}')
