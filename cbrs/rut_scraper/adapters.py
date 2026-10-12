"""
RUT Scraper Infrastructure - Concrete implementations with multi-source support

This layer adapts external services (HTTP, database, email providers)
to work with the domain layer. Supports rotation across multiple RUT sources.
"""

import asyncio
import aiohttp
import re
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
import sqlite3
from datetime import datetime, timezone

# Import multi-source configuration
from .sources import WORKING_SOURCES, BLOCKED_SOURCES


@dataclass
class RUTEntry:
    """A RUT registration entry."""
    rut: str
    name: str
    company: Optional[str] = None
    registration_date: Optional[str] = None
    status: Optional[str] = None
    raw_html: Optional[str] = None
    
    @property
    def id(self) -> str:
        """Generate a unique ID from the RUT."""
        import hashlib
        return hashlib.md5(self.rut.encode()).hexdigest()[:12]
    
    @property
    def created_at(self) -> datetime:
        """Get creation timestamp."""
        return datetime.now(timezone.utc)


class MultiSourceRUTFetcher:
    """
    HTTP-based RUT fetcher with multi-source rotation.
    
    This adapts the HTTP client to the RUTFetcher port,
    trying multiple sources when the primary fails.
    """
    
    def __init__(
        self,
        working_sources=None,
        blocked_sources=None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        timeout: int = 30
    ):
        self.working_sources = working_sources or WORKING_SOURCES
        self.blocked_sources = blocked_sources or BLOCKED_SOURCES
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=5,
                ttl_dns_cache=300,
                ssl=False
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36'
                    ),
                    'Accept': 'application/json, text/html, */*',
                    'Accept-Language': 'es-CL,es;q=0.9,en;q=0.8',
                }
            )
        return self._session
    
    async def _try_source(
        self, 
        source: Dict,
        search_term: Optional[str] = None
    ) -> Optional[RUTEntry]:
        """Try to fetch from a single source."""
        session = await self._get_session()
        url = source['url']
        
        try:
            async with session.get(url, timeout=self.timeout) as response:
                response_text = await response.text()
                
                if response.status != 200:
                    print(f"Source {source['name']} HTTP {response.status}")
                    return None
                
                # Parse response and extract RUT data
                # Look for RUT patterns (12345678-9 format)
                rut_pattern = r'(\d{8}-[\dkK])'
                found_ruts = re.findall(rut_pattern, response_text)
                
                if found_ruts:
                    for rut in found_ruts:
                        entry = RUTEntry(
                            rut=rut,
                            name="",  # TODO: Extract name from source
                            company="",  # TODO: Extract company
                            registration_date=None,
                            status='pending',
                            raw_html=response_text[:10000]
                        )
                        return entry
                
                return None
                
        except asyncio.TimeoutError:
            print(f"Timeout fetching from {source['name']}")
            return None
        except Exception as e:
            print(f"Error from {source['name']}: {e}")
            return None
    
    async def fetch_ruts(
        self,
        search_term: Optional[str] = None,
        page: int = 1
    ) -> List[RUTEntry]:
        """
        Fetch RUTs from multiple sources with rotation.
        
        Args:
            search_term: Optional search term to filter results
            page: Page number for pagination
        
        Returns:
            List of RUTEntry objects
        """
        all_entries = []
        
        # Try each working source
        for source in self.working_sources:
            source_name = source['name']
            source_url = source['url']
            
            print(f"Trying source: {source_name} ({source_url})")
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(source_url, timeout=self.timeout) as response:
                        if response.status == 200:
                            response_text = await response.text()
                            
                            # Parse response and extract RUT data
                            rut_pattern = r'(\d{8}-[\dkK])'
                            found_ruts = re.findall(rut_pattern, response_text)
                            
                            if found_ruts:
                                for rut in found_ruts:
                                    entry = RUTEntry(
                                        rut=rut,
                                        name="",
                                        company="",
                                        registration_date=None,
                                        status='pending',
                                        raw_html=response_text[:10000]
                                    )
                                    all_entries.append(entry)
                                    print(f"  Found RUT: {rut}")
                        else:
                            print(f"  Source {source_name} returned HTTP {response.status}")
                            
            except Exception as e:
                print(f"  Error from {source_name}: {e}")
                continue
            
            # Rate limiting between sources
            await asyncio.sleep(self.retry_delay)
        
        return all_entries
    
    async def fetch_with_retry(
        self,
        max_retries: Optional[int] = None,
        backoff_seconds: float = 2.0
    ) -> List[RUTEntry]:
        """Fetch with exponential backoff retry logic."""
        retries = max_retries or self.max_retries
        
        for attempt in range(retries):
            try:
                return await self.fetch_ruts()
            except Exception as e:
                if attempt < retries - 1:
                    delay = backoff_seconds * (2 ** attempt)
                    print(f"Retry {attempt + 1}/{retries} in {delay:.1f}s: {e}")
                    await asyncio.sleep(delay)
                else:
                    print(f"Failed after {retries} attempts")
        
        return []
    
    async def close(self) -> None:
        """Clean up resources."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


class SqliteRUTRepository:
    """
    SQLite-based RUT repository implementation.
    
    This adapts SQLite to the RUTRepository port.
    """
    
    def __init__(self, db_path: str = "rut_data.db"):
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS ruts (
                    id TEXT PRIMARY KEY,
                    rut TEXT UNIQUE NOT NULL,
                    name TEXT,
                    company TEXT,
                    registration_date TEXT,
                    status TEXT DEFAULT 'pending',
                    raw_html TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_rut ON ruts(rut)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_registration_date ON ruts(registration_date)')
            conn.commit()
    
    async def save_rut(self, rut_entry: RUTEntry) -> bool:
        """Save a single RUT entry."""
        loop = asyncio.get_event_loop()
        
        def _save():
            with sqlite3.connect(self.db_path) as conn:
                try:
                    conn.execute('''
                        INSERT OR REPLACE INTO ruts 
                        (id, rut, name, company, registration_date, status, raw_html, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        rut_entry.id,
                        rut_entry.rut,
                        rut_entry.name,
                        rut_entry.company,
                        rut_entry.registration_date,
                        rut_entry.status or 'pending',
                        rut_entry.raw_html,
                        rut_entry.created_at.isoformat(),
                        datetime.now(timezone.utc).isoformat()
                    ))
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False
        
        return await loop.run_in_executor(None, _save)
    
    async def save_batch(self, entries: List[RUTEntry]) -> int:
        """Save multiple RUT entries. Returns count of saved entries."""
        loop = asyncio.get_event_loop()
        
        def _save():
            saved = 0
            with sqlite3.connect(self.db_path) as conn:
                for entry in entries:
                    try:
                        conn.execute('''
                            INSERT OR REPLACE INTO ruts 
                            (id, rut, name, company, registration_date, status, raw_html, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            entry.id,
                            entry.rut,
                            entry.name,
                            entry.company,
                            entry.registration_date,
                            entry.status or 'pending',
                            entry.raw_html,
                            entry.created_at.isoformat(),
                            datetime.now(timezone.utc).isoformat()
                        ))
                        saved += 1
                    except sqlite3.IntegrityError:
                        pass
                conn.commit()
            return saved
        
        count = await loop.run_in_executor(None, _save)
        return count
    
    async def get_oldest_ruts(
        self,
        limit: int = 100,
        min_date: Optional[str] = None
    ) -> List[RUTEntry]:
        """Get RUTs ordered by registration date (oldest first)."""
        loop = asyncio.get_event_loop()
        
        query = '''
            SELECT r.*, strftime('%Y-%m-%d', r.registration_date) as reg_date
            FROM ruts r
            WHERE r.status = 'completed'
            '''
        
        if min_date:
            query += ' AND r.registration_date >= ?'
            params = [min_date]
        else:
            params = []
        
        query += ' ORDER BY r.registration_date ASC, r.id ASC'
        query += ' LIMIT ' + str(limit)
        
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(query, params).fetchall()
                return [
                    RUTEntry(
                        rut=row[1],
                        name=row[2],
                        company=row[3],
                        registration_date=row[4],
                        status=row[5],
                        raw_html=row[6]
                    )
                    for row in rows
                ]
        
        return await loop.run_in_executor(None, _fetch)
    
    async def get_all_ruts(
        self,
        order_by: str = 'registration_date',
        ascending: bool = True
    ) -> List[RUTEntry]:
        """Get all RUTs, optionally ordered by registration date."""
        loop = asyncio.get_event_loop()
        
        if order_by == 'registration_date':
            query = '''
                SELECT 
                    r.id,
                    r.rut,
                    r.name,
                    r.company,
                    r.registration_date,
                    r.status,
                    r.raw_html,
                    strftime('%Y-%m-%d', r.registration_date) as reg_date
                FROM ruts r
                ORDER BY r.registration_date ASC, r.id ASC
            '''
        elif order_by == 'created_at':
            query = '''
                SELECT 
                    r.id,
                    r.rut,
                    r.name,
                    r.company,
                    r.registration_date,
                    r.status,
                    r.raw_html,
                    strftime('%Y-%m-%d', r.created_at) as reg_date
                FROM ruts r
                ORDER BY r.created_at ASC, r.id ASC
            '''
        else:
            query = '''
                SELECT 
                    r.id,
                    r.rut,
                    r.name,
                    r.company,
                    r.registration_date,
                    r.status,
                    r.raw_html,
                    NULL as reg_date
                FROM ruts r
                ORDER BY r.id ASC
            '''
        
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(query).fetchall()
                return [
                    RUTEntry(
                        rut=row[1],
                        name=row[2],
                        company=row[3],
                        registration_date=row[4],
                        status=row[5],
                        raw_html=row[6]
                    )
                    for row in rows
                ]
        
        return await loop.run_in_executor(None, _fetch)
    
    async def exists(self, rut: str) -> bool:
        """Check if a RUT already exists in storage."""
        loop = asyncio.get_event_loop()
        
        def _check():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('SELECT 1 FROM ruts WHERE rut = ?', (rut,))
                return cursor.fetchone() is not None
        
        return await loop.run_in_executor(None, _check)
    
    def close(self) -> None:
        """Close database connection (SQLite handles this automatically)."""
        pass
