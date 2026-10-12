"""
RUT Scraper Main Module - Entry point

This module provides the main scraper class that uses hexagonal
architecture principles to compose ports and adapters.
"""

import asyncio
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from .ports import (
    RUTRepository,
    RUTFetcher,
    EmailProvider,
    EmailFetcher,
    RUTScraperConfig,
)
from .domain import RUTScraper, RegistrationOrchestrator
from .adapters import (
    HttpRUTFetcher,
    SqliteRUTRepository,
)


@dataclass
class RUTScraperSettings:
    """
    Settings for the RUT scraper.
    
    This configuration file allows easy adjustment of scraper behavior
    without changing the core logic.
    """
    
    # Target service configuration
    base_url: str = "https://nombrerutyfirma.com"
    rut_endpoint: str = "/rut"
    
    # Fetching configuration
    max_concurrent_fetches: int = 5
    retry_attempts: int = 3
    retry_delay: float = 2.0
    timeout: int = 30
    
    # Storage configuration
    database_path: str = "rut_data.db"
    
    # Email configuration
    email_provider: str = "gmail"  # gmail, outlook, protonmail, etc.
    email_domain: Optional[str] = None
    
    # Rate limiting
    request_delay: float = 0.1
    max_requests_per_minute: int = 60
    
    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = None


class RUTScraperSystem:
    """
    Complete RUT scraper system using hexagonal architecture.
    
    This system:
    1. Composes ports and adapters
    2. Orchestrates the scraping workflow
    3. Handles errors and retries
    4. Persists results
    
    The system is modular and can be extended with different
    implementations of each port.
    """
    
    def __init__(self, settings: Optional[RUTScraperSettings] = None):
        self.settings = settings or RUTScraperSettings()
        
        # Create adapters (concrete implementations)
        self.fetcher = HttpRUTFetcher(
            base_url=self.settings.base_url,
            rut_endpoint=self.settings.rut_endpoint,
            max_retries=self.settings.retry_attempts,
            retry_delay=self.settings.retry_delay,
            timeout=self.settings.timeout
        )
        
        self.repository = SqliteRUTRepository(db_path=self.settings.database_path)
        
        # Create domain objects
        self.rut_scraper = RUTScraper(config=RUTScraperConfig(
            base_url=self.settings.base_url,
            rut_endpoint=self.settings.rut_endpoint,
            max_concurrent_fetches=self.settings.max_concurrent_fetches,
            retry_attempts=self.settings.retry_attempts,
            retry_delay=self.settings.retry_delay,
            batch_size=100
        ))
        
        self.orchestrator = RegistrationOrchestrator(
            rut_scraper=self.rut_scraper,
            email_provider=None,  # TODO: Implement email provider
            repository=self.repository
        )
        
        # Rate limiting
        self._rate_limiter = asyncio.Semaphore(self.settings.max_concurrent_fetches)
    
    async def fetch_oldest_ruts(
        self,
        limit: int = 100,
        skip_existing: bool = True
    ) -> List[Any]:
        """
        Fetch oldest RUTs from the endpoint.
        
        Args:
            limit: Maximum RUTs to fetch
            skip_existing: Skip RUTs already in repository
        
        Returns:
            List of RUT entries ordered by registration date
        """
        return await self.rut_scraper.fetch_oldest_ruts(
            fetcher=self.fetcher,
            repository=self.repository,
            limit=limit,
            skip_existing=skip_existing
        )
    
    async def fetch_ruts_batch(
        self,
        limit: int = 100,
        params: Optional[Dict[str, Any]] = None
    ) -> List[Any]:
        """
        Fetch a batch of RUTs with rate limiting.
        
        Args:
            limit: Maximum RUTs to fetch
            params: Additional query parameters
        
        Returns:
            List of RUT entries
        """
        async def fetch_with_rate_limit():
            async with self._rate_limiter:
                await asyncio.sleep(self.settings.request_delay)
                return await self.fetcher.fetch_ruts(params=params)
        
        all_entries = []
        while len(all_entries) < limit:
            try:
                entries = await fetch_with_rate_limit()
                all_entries.extend(entries)
                
                if not entries:
                    break
                
                # Delay between batches
                await asyncio.sleep(self.settings.request_delay)
                
            except Exception as e:
                print(f"Batch fetch failed: {e}")
                break
        
        return all_entries[:limit]
    
    async def fetch_and_store(self, limit: int = 100) -> List[Any]:
        """
        Fetch RUTs and store them in the repository.
        
        Args:
            limit: Maximum RUTs to fetch and store
        
        Returns:
            List of stored RUT entries
        """
        entries = await self.fetch_ruts_batch(limit=limit)
        
        # Store in repository
        stored = await self.repository.save_batch(entries)
        
        return entries[:stored]
    
    async def get_all_ruts_ordered(
        self,
        order_by: str = 'registration_date',
        ascending: bool = True,
        limit: Optional[int] = None
    ) -> List[Any]:
        """
        Get all RUTs from storage, ordered by registration date.
        
        Args:
            order_by: Field to order by ('registration_date', 'created_at', 'id')
            ascending: Sort order (True=oldest first, False=newest first)
            limit: Optional limit on results
        
        Returns:
            List of RUT entries
        """
        all_ruts = await self.repository.get_all_ruts(
            order_by=order_by,
            ascending=ascending
        )
        
        if limit:
            all_ruts = all_ruts[:limit]
        
        return all_ruts
    
    async def get_oldest_ruts_from_db(
        self,
        limit: int = 100,
        min_date: Optional[str] = None
    ) -> List[Any]:
        """
        Get oldest RUTs from the database (for resuming interrupted jobs).
        
        Args:
            limit: Maximum RUTs to fetch
            min_date: Minimum registration date (optional)
        
        Returns:
            List of RUT entries ordered by registration date
        """
        return await self.repository.get_oldest_ruts(
            limit=limit,
            min_date=min_date
        )
    
    async def close(self) -> None:
        """Clean up resources."""
        await self.fetcher.close()
    
    def __del__(self):
        """Ensure cleanup on deletion."""
        try:
            asyncio.run(self.close())
        except Exception:
            pass


class RUTScraperCLI:
    """
    Command-line interface for the RUT scraper.
    
    Usage:
        python -m cbrs.rut_scraper.cli fetch --limit 100
        python -m cbrs.rut_scraper.cli fetch-oldest --limit 50
        python -m cbrs.rut_scraper.cli status
    """
    
    def __init__(self):
        self.system = RUTScraperSystem()
    
    async def fetch(self, limit: int = 100) -> int:
        """Fetch and store RUTs."""
        entries = await self.system.fetch_and_store(limit=limit)
        print(f"Stored {len(entries)} RUTs")
        return len(entries)
    
    async def fetch_oldest(self, limit: int = 100) -> List[Any]:
        """Fetch oldest RUTs from storage."""
        entries = await self.system.get_oldest_ruts_from_db(limit=limit)
        print(f"Found {len(entries)} RUTs in storage")
        for entry in entries[:10]:  # Show first 10
            print(f"  - {entry.rut} ({entry.name})")
        return entries
    
    async def status(self) -> Dict[str, Any]:
        """Get scraper status."""
        import sqlite3
        with sqlite3.connect(self.system.settings.database_path) as conn:
            total = conn.execute('SELECT COUNT(*) FROM ruts').fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM ruts WHERE status = 'completed'"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM ruts WHERE status = 'pending'"
            ).fetchone()[0]
        
        return {
            'total_ruts': total,
            'completed': completed,
            'pending': pending,
            'database': self.system.settings.database_path
        }
    
    async def run(self, command: str = 'status', **kwargs):
        """Run a CLI command."""
        if command == 'fetch':
            await self.fetch(**kwargs)
        elif command == 'fetch-oldest':
            await self.fetch_oldest(**kwargs)
        elif command == 'status':
            status = await self.status()
            print(f"Status: {status}")
            return status
        else:
            print(f"Unknown command: {command}")
            return None


# Main entry point
if __name__ == '__main__':
    import sys
    
    cli = RUTScraperCLI()
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        args = {'limit': int(sys.argv[2]) if len(sys.argv) > 2 else 100}
        
        if command == 'fetch':
            asyncio.run(cli.run('fetch', **args))
        elif command == 'fetch-oldest':
            asyncio.run(cli.run('fetch-oldest', **args))
        elif command == 'status':
            asyncio.run(cli.run('status'))
        else:
            print(f"Unknown command: {command}")
            print("Usage: python -m cbrs.rut_scraper <fetch|fetch-oldest|status> [limit]")
    else:
        print("RUT Scraper System")
        print("Usage: python -m cbrs.rut_scraper <fetch|fetch-oldest|status> [limit]")
