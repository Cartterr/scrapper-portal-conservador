"""
RUT Scraper Settings - Configuration module

This module provides a settings class for configuring the RUT scraper.
"""

from dataclasses import dataclass
from typing import Optional


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
