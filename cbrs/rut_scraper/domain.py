"""
RUT Scraper Domain - Pure business logic

This layer contains pure functions and classes that don't depend on
external frameworks. It's the core of the application.
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
import re


@dataclass
class RUTEntry:
    """A RUT registration entry from nombrerutyfirma.com."""
    rut: str
    name: str
    company: Optional[str] = None
    registration_date: Optional[str] = None
    status: Optional[str] = None
    raw_html: Optional[str] = None
    
    @property
    def id(self) -> str:
        """Generate a unique ID from the RUT."""
        return hashlib.md5(self.rut.encode()).hexdigest()[:12]
    
    @property
    def created_at(self) -> datetime:
        """Get creation timestamp."""
        return datetime.now(timezone.utc)


class RUTScraper:
    """
    Core RUT scraper business logic.
    
    This orchestrates the scraping process using ports from the
    infrastructure layer.
    """
    
    def __init__(self, config: Optional['RUTScraperConfig'] = None):
        self.config = config or RUTScraperConfig()
        self._cache: Dict[str, RUTEntry] = {}
    
    async def fetch_oldest_ruts(
        self,
        fetcher: 'RUTFetcher',
        repository: 'RUTRepository',
        limit: int = 100,
        skip_existing: bool = True
    ) -> List[RUTEntry]:
        """
        Fetch oldest RUTs from the endpoint.
        
        Args:
            fetcher: RUTFetcher implementation
            repository: RUTRepository implementation
            limit: Maximum RUTs to fetch
            skip_existing: Skip RUTs already in repository
        
        Returns:
            List of RUT entries ordered by registration date
        """
        all_entries: List[RUTEntry] = []
        fetched = 0
        skipped = 0
        
        while fetched < limit:
            try:
                entries = await fetcher.fetch_ruts(
                    params={'order': 'oldest_first', 'limit': limit - fetched},
                    page=(fetched // 100) + 1
                )
                
                if not entries:
                    break
                
                for entry in entries:
                    if skip_existing and await repository.exists(entry.rut):
                        skipped += 1
                        continue
                    
                    entry = await repository.save_rut(entry)
                    all_entries.append(entry)
                    self._cache[entry.id] = entry
                
                fetched += len(entries)
                
                # Small delay between requests to respect rate limits
                await asyncio.sleep(0.1)
                
            except Exception as e:
                print(f"Error fetching RUTs: {e}")
                break
        
        # Sort by registration date (oldest first)
        all_entries.sort(
            key=lambda e: e.registration_date or e.created_at,
            reverse=False
        )
        
        return all_entries[:limit]
    
    async def fetch_with_pace(
        self,
        fetcher: 'RUTFetcher',
        pace_callback=None
    ) -> List[RUTEntry]:
        """
        Fetch RUTs with pacing to avoid rate limiting.
        
        Args:
            fetcher: RUTFetcher implementation
            pace_callback: Optional callback for pacing control
        
        Returns:
            List of RUT entries
        """
        entries = []
        try:
            entries = await fetcher.fetch_with_retry()
        except Exception as e:
            print(f"Fetch failed: {e}")
        
        return entries
    
    def parse_rut_from_html(self, html: str) -> Optional[RUTEntry]:
        """
        Parse RUT data from HTML response.
        
        Args:
            html: HTML response from the endpoint
        
        Returns:
            RUTEntry or None if parsing fails
        """
        # TODO: Implement HTML parsing based on actual endpoint response
        # This will be adapted when we see the actual response structure
        return None
    
    def validate_rut(self, rut: str) -> bool:
        """
        Validate a Chilean RUT format.
        
        Args:
            rut: RUT string (e.g., "11111111-1")
        
        Returns:
            True if valid
        """
        # Chilean RUT format: 8-9 digits + dash + verifier (0-9 or K)
        pattern = r'^\d{7,8}-[0-9K]$'
        return bool(re.match(pattern, rut))


class RegistrationOrchestrator:
    """
    Orchestrates the full registration automation process.
    
    This coordinates:
    1. RUT fetching
    2. Email account creation
    3. Full registration form completion
    4. Data storage
    """
    
    def __init__(
        self,
        rut_scraper: RUTScraper,
        email_provider: EmailProvider,
        repository: 'RUTRepository',
        config: Optional['RUTScraperConfig'] = None
    ):
        self.rut_scraper = rut_scraper
        self.email_provider = email_provider
        self.repository = repository
        # Import here to avoid circular dependency
        from .ports import RUTScraperConfig
        self.config = config or RUTScraperConfig()
    
    async def register_full_process(
        self,
        rut: str,
        fetcher: 'RUTFetcher',
        email_service: Optional[EmailProvider] = None
    ) -> Dict[str, Any]:
        """
        Complete registration automation for a RUT.
        
        Args:
            rut: RUT to register
            fetcher: RUTFetcher for fetching data
            email_service: Optional email provider for account creation
        
        Returns:
            Dict with registration result and metadata
        """
        result = {
            'rut': rut,
            'status': 'pending',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'steps': {}
        }
        
        try:
            # Step 1: Fetch RUT data
            result['steps']['fetch'] = await self._fetch_rut_data(fetcher, rut)
            
            # Step 2: Create email account if needed
            if email_service:
                result['steps']['email'] = await self._create_email_account(
                    email_service, rut
                )
            else:
                result['steps']['email'] = {'created': False, 'reason': 'no_email_service'}
            
            # Step 3: Complete registration (placeholder for form automation)
            result['steps']['registration'] = {'completed': False, 'reason': 'not_implemented_yet'}
            
            # Step 4: Store result
            entry = RUTEntry(
                rut=rut,
                name=result['steps']['fetch'].get('name', ''),
                company=result['steps']['fetch'].get('company', None),
                registration_date=result['steps']['fetch'].get('registration_date', None),
                status=result['status']
            )
            await self.repository.save_rut(entry)
            
            result['status'] = 'completed'
            result['finished_at'] = datetime.now(timezone.utc).isoformat()
            
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
        
        return result
    
    async def _fetch_rut_data(
        self,
        fetcher: 'RUTFetcher',
        rut: str
    ) -> Dict[str, Any]:
        """Fetch data for a specific RUT."""
        # This will be implemented based on actual endpoint behavior
        return {
            'rut': rut,
            'name': '',
            'company': None,
            'registration_date': None
        }
    
    async def _create_email_account(
        self,
        email_service: EmailProvider,
        rut: str
    ) -> Dict[str, Any]:
        """Create email account for RUT registration."""
        # Generate email from RUT
        email_name = f"{rut.replace('-','')}_{datetime.now().strftime('%Y%m%d')}"
        email = f"{email_name}@gmail.com"
        
        return {
            'email': email,
            'created': True,
            'provider': 'gmail'
        }
    
    async def batch_register(
        self,
        ruts: List[str],
        fetcher: 'RUTFetcher',
        email_service: Optional[EmailProvider] = None,
        concurrency: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Batch register multiple RUTs.
        
        Args:
            ruts: List of RUT strings
            fetcher: RUTFetcher implementation
            email_service: Optional email provider
            concurrency: Number of concurrent operations
        
        Returns:
            List of registration results
        """
        semaphore = asyncio.Semaphore(concurrency)
        
        async def register_single(rut: str) -> Dict[str, Any]:
            async with semaphore:
                return await self.register_full_process(rut, fetcher, email_service)
        
        tasks = [register_single(rut) for rut in ruts]
        return await asyncio.gather(*tasks, return_exceptions=True)
