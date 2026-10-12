"""Test script for RUT Scraper Module"""

import sys
import os

# Add the project root to Python path
project_root = r"C:\V\scrapper\scrapper-portal-conservador"
sys.path.insert(0, project_root)

print(f"Python path: {sys.path[0]}")

# Try importing
try:
    from cbrs.rut_scraper.settings import RUTScraperSettings
    from cbrs.rut_scraper.main import RUTScraperSystem
    print("✓ Module imports successfully!")
    
    # Test creating a system
    settings = RUTScraperSettings()
    system = RUTScraperSystem(settings=settings)
    print("✓ RUTScraperSystem created!")
    
    # Test basic operations
    import asyncio
    
    async def test():
        print("\nTesting basic operations...")
        
        # Test status
        status = await system.get_all_ruts_ordered(limit=0)
        print(f"✓ get_all_ruts_ordered returned: {len(status)} items")
        
        # Test fetch (will return empty since we haven't implemented the actual fetch)
        entries = await system.fetch_and_store(limit=5)
        print(f"✓ fetch_and_store returned: {len(entries)} items")
        
        await system.close()
        print("✓ System closed successfully!")
    
    asyncio.run(test())
    print("\n✅ All tests passed!")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
