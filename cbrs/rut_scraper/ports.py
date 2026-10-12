"""
RUT Scraper Ports - Interface definitions

Hexagonal architecture: Ports define contracts that adapters implement.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Iterator
from dataclasses import dataclass


@dataclass
class RUTEntry:
    """A RUT registration entry from nombrerutyfirma.com."""
    rut: str
    name: str
    company: Optional[str] = None
    registration_date: Optional[str] = None
    status: Optional[str] = None
    raw_html: Optional[str] = None


class RUTRepository(ABC):
    """
    Repository port for persisting RUT data.
    
    Implementations can use SQLite, PostgreSQL, or any storage.
    """
    
    @abstractmethod
    async def save_rut(self, rut: RUTEntry) -> bool:
        """Save a single RUT entry."""
        pass
    
    @abstractmethod
    async def save_batch(self, entries: List[RUTEntry]) -> int:
        """Save multiple RUT entries. Returns count of saved entries."""
        pass
    
    @abstractmethod
    async def get_oldest_ruts(
        self, 
        limit: int = 100,
        min_date: Optional[str] = None
    ) -> List[RUTEntry]:
        """Get RUTs ordered by registration date (oldest first)."""
        pass
    
    @abstractmethod
    async def get_all_ruts(
        self,
        order_by: str = 'registration_date',
        ascending: bool = True
    ) -> List[RUTEntry]:
        """Get all RUTs, optionally ordered by registration date."""
        pass
    
    @abstractmethod
    async def exists(self, rut: str) -> bool:
        """Check if a RUT already exists in storage."""
        pass


class RUTFetcher(ABC):
    """
    Fetcher port for scraping RUT data from external sources.
    
    This handles HTTP requests, session management, and response parsing.
    """
    
    @abstractmethod
    async def fetch_ruts(
        self,
        params: Optional[Dict[str, Any]] = None,
        page: int = 1
    ) -> List[RUTEntry]:
        """
        Fetch RUTs from the target endpoint.
        
        Args:
            params: Query parameters (e.g., date ranges, filters)
            page: Page number for pagination
        
        Returns:
            List of RUTEntry objects
        """
        pass
    
    @abstractmethod
    async def fetch_with_retry(
        self,
        max_retries: int = 3,
        backoff_seconds: float = 2.0
    ) -> List[RUTEntry]:
        """Fetch with exponential backoff retry logic."""
        pass
    
    @abstractmethod
    def close(self) -> None:
        """Clean up resources (sessions, connections)."""
        pass


class EmailProvider(ABC):
    """
    Email provider port for obtaining permanent email addresses.
    
    Supports various services: Gmail, Outlook, ProtonMail, etc.
    """
    
    @abstractmethod
    async def create_account(
        self,
        domain: str = 'gmail.com',
        name_prefix: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new email account.
        
        Returns:
            Dict with 'email', 'password', 'created_at', and any metadata
        """
        pass
    
    @abstractmethod
    async def verify_account(
        self,
        email: str,
        password: str,
        verification_code: Optional[str] = None
    ) -> bool:
        """Verify account creation (CAPTCHA, 2FA, etc.)."""
        pass
    
    @abstractmethod
    async def get_account_info(self, email: str) -> Optional[Dict[str, Any]]:
        """Get account information."""
        pass
    
    @abstractmethod
    def close(self) -> None:
        """Clean up resources."""
        pass


class EmailFetcher(ABC):
    """
    Fetcher port for obtaining email addresses from various sources.
    
    Can use:
    - Email provider APIs
    - Scraping email directories
    - Purchased email lists
    - Custom email services
    """
    
    @abstractmethod
    async def fetch_available_emails(
        self,
        count: int = 10,
        domain: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch available email addresses.
        
        Returns:
            List of dicts with 'email', 'password', 'provider', 'status'
        """
        pass
    
    @abstractmethod
    async def claim_email(
        self,
        email: str,
        password: str,
        provider: str
    ) -> Dict[str, Any]:
        """Claim an email address from a provider."""
        pass
    
    @abstractmethod
    def close(self) -> None:
        """Clean up resources."""
        pass


class RUTScraperConfig:
    """Configuration for the RUT scraper."""
    
    def __init__(
        self,
        base_url: str = "https://nombrerutyfirma.com",
        rut_endpoint: str = "/rut",
        max_concurrent_fetches: int = 5,
        retry_attempts: int = 3,
        retry_delay: float = 2.0,
        batch_size: int = 100,
    ):
        self.base_url = base_url
        self.rut_endpoint = rut_endpoint
        self.max_concurrent_fetches = max_concurrent_fetches
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self.batch_size = batch_size
