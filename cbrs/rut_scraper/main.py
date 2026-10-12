"""
Main entry point for the RUT Scraper System.

Usage:
    python -m cbrs.rut_scraper.main fetch --limit 100
    python -m cbrs.rut_scraper.main status
    python -m cbrs.rut_scraper.main fetch-oldest --limit 50
"""

import asyncio
import argparse
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import from submodules to avoid circular imports
from .settings import RUTScraperSettings
from .system import RUTScraperSystem
from .domain import RUTScraper, RegistrationOrchestrator
from .ports import RUTScraperConfig
from .adapters import HttpRUTFetcher, SqliteRUTRepository


def create_system_from_args(args) -> RUTScraperSystem:
    """Create a RUTScraperSystem from command-line arguments."""
    settings = RUTScraperSettings(
        base_url=args.base_url,
        rut_endpoint=args.rut_endpoint,
        max_concurrent_fetches=args.concurrency,
        retry_attempts=args.retries,
        retry_delay=args.retry_delay,
        database_path=args.database,
        request_delay=args.request_delay,
    )
    
    return RUTScraperSystem(settings=settings)


async def command_fetch(system, limit, skip_existing=True):
    """Fetch and store RUTs."""
    print(f"Fetching up to {limit} RUTs...")
    print(f"Skipping existing: {skip_existing}")
    
    entries = await system.fetch_and_store(limit=limit)
    
    print(f"Stored {len(entries)} RUTs")
    
    if entries:
        print("\nFirst few RUTs:")
        for i, entry in enumerate(entries[:5], 1):
            print(f"  {i}. {entry.rut} - {entry.name or 'Unknown'}")
    
    return entries


async def command_fetch_oldest(system, limit):
    """Fetch oldest RUTs from storage."""
    print(f"Fetching oldest {limit} RUTs from storage...")
    
    entries = await system.get_oldest_ruts_from_db(limit=limit)
    
    print(f"Found {len(entries)} RUTs in storage")
    
    if entries:
        print("\nOldest RUTs:")
        for i, entry in enumerate(entries[:10], 1):
            print(f"  {i}. {entry.rut} - {entry.name or 'Unknown'} "
                  f"(registered: {entry.registration_date or 'N/A'})")
    
    return entries


async def command_status(system):
    """Show system status."""
    import sqlite3
    
    db_path = system.settings.database_path
    
    with sqlite3.connect(db_path) as conn:
        total = conn.execute('SELECT COUNT(*) FROM ruts').fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM ruts WHERE status = 'completed'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM ruts WHERE status = 'pending'"
        ).fetchone()[0]
    
    print("RUT Scraper Status")
    print("=" * 40)
    print(f"  Database: {db_path}")
    print(f"  Total RUTs stored: {total}")
    print(f"  Completed: {completed}")
    print(f"  Pending: {pending}")
    
    return {
        'total': total,
        'completed': completed,
        'pending': pending,
        'database': db_path
    }


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='RUT Scraper System - Fetch and store Chilean Tax RUTs'
    )
    
    parser.add_argument(
        'command',
        choices=['fetch', 'fetch-oldest', 'status'],
        help='Command to execute'
    )
    
    parser.add_argument(
        '--limit', '-l',
        type=int,
        default=100,
        help='Maximum number of RUTs to fetch (default: 100)'
    )
    
    parser.add_argument(
        '--base-url',
        type=str,
        default="https://nombrerutyfirma.com",
        help='Base URL of the RUT service'
    )
    
    parser.add_argument(
        '--rut-endpoint',
        type=str,
        default="/rut",
        help='RUT endpoint path'
    )
    
    parser.add_argument(
        '--concurrency',
        type=int,
        default=5,
        help='Maximum concurrent fetches (default: 5)'
    )
    
    parser.add_argument(
        '--retries',
        type=int,
        default=3,
        help='Number of retry attempts (default: 3)'
    )
    
    parser.add_argument(
        '--retry-delay',
        type=float,
        default=2.0,
        help='Retry delay in seconds (default: 2.0)'
    )
    
    parser.add_argument(
        '--database',
        type=str,
        default="rut_data.db",
        help='Database file path (default: rut_data.db)'
    )
    
    parser.add_argument(
        '--request-delay',
        type=float,
        default=0.1,
        help='Request delay in seconds (default: 0.1)'
    )
    
    parser.add_argument(
        '--skip-existing',
        action='store_true',
        default=True,
        help='Skip RUTs already in storage (default: True)'
    )
    
    args = parser.parse_args()
    
    # Create system
    system = create_system_from_args(args)
    
    try:
        if args.command == 'fetch':
            await command_fetch(system, args.limit, skip_existing=args.skip_existing)
        elif args.command == 'fetch-oldest':
            await command_fetch_oldest(system, args.limit)
        elif args.command == 'status':
            await command_status(system)
    finally:
        await system.close()


# Export RUTScraperSystem for importing
__all__ = ['RUTScraperSystem']

if __name__ == '__main__':
    asyncio.run(main())
