import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cbrs.rut_scraper.main import RUTScraperSystem, RUTScraperSettings
import asyncio

s = RUTScraperSystem(RUTScraperSettings())
result = asyncio.run(s.get_oldest_ruts_from_db(limit=10))
print(f'Found {len(result)} RUTs')
for r in result:
    print(f'  {r.rut} - {r.name or "Unknown"}')
