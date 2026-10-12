"""
RUT Scraper Module - Hexagonal Architecture

This module scrapes Chilean Tax RUTs from nombrerutyfirma.com and
automates full registration processes.

Architecture:
- Ports: Interfaces and contracts (rut_scraper/ports/)
- Adapters: Concrete implementations (rut_scraper/adapters/)
- Domain: Pure business logic (rut_scraper/domain/)
"""

from .ports import (
    RUTRepository,
    RUTFetcher,
    EmailProvider,
    EmailFetcher,
    RUTScraperConfig,
)
from .domain import (
    RUTScraper,
    RegistrationOrchestrator,
)
from .settings import RUTScraperSettings
from .system import RUTScraperSystem

__all__ = [
    'RUTRepository',
    'RUTFetcher',
    'EmailProvider',
    'EmailFetcher',
    'RUTScraperConfig',
    'RUTScraper',
    'RegistrationOrchestrator',
    'RUTScraperSettings',
    'RUTScraperSystem',
]
