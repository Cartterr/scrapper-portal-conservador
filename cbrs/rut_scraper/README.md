# RUT Scraper Module

A hexagonal architecture-based module for scraping Chilean Tax RUTs from `nombrerutyfirma.com` and automating full registration processes.

## Architecture

This module follows **hexagonal architecture** (also known as ports and adapters):

### Ports (Interfaces)
Located in `cbrs/rut_scraper/ports.py`:
- `RUTRepository`: Interface for persisting RUT data
- `RUTFetcher`: Interface for scraping RUT data from external sources
- `EmailProvider`: Interface for creating email accounts
- `EmailFetcher`: Interface for obtaining email addresses

### Adapters (Implementations)
Located in `cbrs/rut_scraper/adapters.py`:
- `HttpRUTFetcher`: HTTP-based RUT fetcher
- `SqliteRUTRepository`: SQLite-based RUT storage

### Domain (Business Logic)
Located in `cbrs/rut_scraper/domain.py`:
- Pure business logic that depends only on ports
- Can be tested without external dependencies

## Quick Start

### Installation

```bash
cd C:\V\scrapper\scrapper-portal-conservador
python -m venv .venv
.venv\Scripts\activate
pip install aiohttp
```

### Basic Usage

```python
from cbrs.rut_scraper import RUTScraperSystem

# Create scraper system
system = RUTScraperSystem()

# Fetch oldest RUTs
ruts = await system.fetch_oldest_ruts(limit=100)

# Get all stored RUTs ordered by registration date
all_ruts = await system.get_all_ruts_ordered(order_by='registration_date')

# Clean up
await system.close()
```

### Command Line

```bash
# Fetch and store RUTs
python -m cbrs.rut_scraper fetch --limit 100

# Get oldest RUTs from storage
python -m cbrs.rut_scraper fetch-oldest --limit 50

# Check status
python -m cbrs.rut_scraper status
```

### Configuration

Create a settings file:

```python
from cbrs.rut_scraper import RUTScraperSettings

settings = RUTScraperSettings(
    base_url="https://nombrerutyfirma.com",
    rut_endpoint="/rut",
    max_concurrent_fetches=5,
    retry_attempts=3,
    retry_delay=2.0,
    database_path="rut_data.db",
    request_delay=0.1,
)

system = RUTScraperSystem(settings=settings)
```

## Extending the Module

### Adding a New RUT Fetcher

1. Create a new adapter that implements `RUTFetcher`:

```python
from cbrs.rut_scraper.ports import RUTFetcher

class MyRUTFetcher(RUTFetcher):
    async def fetch_ruts(self, params=None, page=1):
        # Implement your fetching logic
        pass
    
    async def fetch_with_retry(self, max_retries=3, backoff_seconds=2.0):
        # Implement retry logic
        pass
    
    def close(self):
        # Clean up resources
        pass
```

2. Use it in your scraper:

```python
from cbrs.rut_scraper.domain import RUTScraper

scraper = RUTScraper()
fetcher = MyRUTFetcher()
entries = await scraper.fetch_oldest_ruts(fetcher=fetcher, repository=repository)
```

### Adding a New Email Provider

1. Create a new provider:

```python
from cbrs.rut_scraper.email import EmailProvider

class OutlookProvider(EmailProvider):
    async def create_account(self, domain='outlook.com', name_prefix=None):
        # Implement Outlook account creation
        pass
    
    async def verify_account(self, email, password, verification_code=None):
        # Implement verification
        pass
    
    async def get_account_info(self, email):
        # Get account info
        pass
    
    def close(self):
        pass
```

2. Register it in the system:

```python
from cbrs.rut_scraper.email import create_email_system

email_system = create_email_system(providers=['gmail', 'outlook'])
```

## Testing

The domain layer can be tested without external dependencies:

```python
from cbrs.rut_scraper.domain import RUTScraper
from unittest.mock import Mock, AsyncMock

# Mock the fetcher
mock_fetcher = AsyncMock()
mock_fetcher.fetch_ruts = AsyncMock(return_value=[])

# Create scraper with mocked dependencies
scraper = RUTScraper()
scraper.fetch_oldest_ruts(fetcher=mock_fetcher, repository=None)
```

## Production Considerations

1. **Rate Limiting**: Respect the target service's rate limits
2. **Error Handling**: Implement proper retry logic with exponential backoff
3. **Data Validation**: Validate RUT format before processing
4. **Security**: Store passwords securely (use environment variables or secrets manager)
5. **Monitoring**: Add logging and metrics for production monitoring

## License

This module is part of the CBRS project and follows the same licensing terms.
