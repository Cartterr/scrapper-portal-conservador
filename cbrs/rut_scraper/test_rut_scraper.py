"""
Tests for the RUT Scraper Module

This file contains unit tests for the RUT scraper components.
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone


class TestRUTEntry:
    """Tests for the RUTEntry dataclass."""
    
    def test_rut_entry_creation(self):
        """Test creating a RUTEntry."""
        from cbrs.rut_scraper.domain import RUTEntry
        
        entry = RUTEntry(
            rut="11111111-1",
            name="Test User",
            company="Test Company",
            registration_date="2020-01-01"
        )
        
        assert entry.rut == "11111111-1"
        assert entry.name == "Test User"
        assert entry.company == "Test Company"
        assert entry.registration_date == "2020-01-01"
    
    def test_rut_entry_id_generation(self):
        """Test that RUTEntry generates a unique ID."""
        from cbrs.rut_scraper.domain import RUTEntry
        
        entry1 = RUTEntry(rut="11111111-1", name="Test")
        entry2 = RUTEntry(rut="22222222-2", name="Test")
        
        assert entry1.id != entry2.id
        assert len(entry1.id) == 12  # MD5 hex digest prefix
    
    def test_rut_entry_created_at(self):
        """Test that RUTEntry has a created_at timestamp."""
        from cbrs.rut_scraper.domain import RUTEntry
        
        entry = RUTEntry(rut="11111111-1", name="Test")
        
        assert entry.created_at is not None
        assert isinstance(entry.created_at, datetime)
        assert entry.created_at.tzinfo is not None


class TestRUTScraper:
    """Tests for the RUTScraper class."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        from cbrs.rut_scraper.ports import RUTScraperConfig
        return RUTScraperConfig()
    
    @pytest.fixture
    def mock_repository(self):
        """Create a mock repository."""
        mock = AsyncMock()
        mock.exists = AsyncMock(return_value=False)
        mock.save_rut = AsyncMock(return_value=True)
        return mock
    
    @pytest.fixture
    def mock_fetcher(self):
        """Create a mock fetcher."""
        mock = AsyncMock()
        mock.fetch_ruts = AsyncMock(return_value=[
            {
                'rut': '11111111-1',
                'name': 'Test User',
                'company': 'Test Company',
                'registration_date': '2020-01-01'
            }
        ])
        return mock
    
    async def test_fetch_oldest_ruts(self, mock_repository, mock_fetcher):
        """Test fetching oldest RUTs."""
        from cbrs.rut_scraper.domain import RUTScraper
        
        scraper = RUTScraper()
        result = await scraper.fetch_oldest_ruts(
            fetcher=mock_fetcher,
            repository=mock_repository,
            limit=10,
            skip_existing=True
        )
        
        assert len(result) == 1
        assert result[0].rut == "11111111-1"
    
    async def test_fetch_oldest_ruts_sorting(self, mock_repository, mock_fetcher):
        """Test that RUTs are sorted by registration date."""
        from cbrs.rut_scraper.domain import RUTScraper
        
        # Create mock fetcher that returns RUTs in reverse chronological order
        mock_fetcher.fetch_ruts = AsyncMock(return_value=[
            {
                'rut': '99999999-9',
                'name': 'New User',
                'registration_date': '2023-01-01'
            },
            {
                'rut': '88888888-8',
                'name': 'Old User',
                'registration_date': '2020-01-01'
            }
        ])
        
        scraper = RUTScraper()
        result = await scraper.fetch_oldest_ruts(
            fetcher=mock_fetcher,
            repository=mock_repository,
            limit=10,
            skip_existing=True
        )
        
        # Oldest should come first
        assert result[0].rut == "88888888-8"
        assert result[1].rut == "99999999-9"
    
    def test_validate_rut(self):
        """Test RUT validation."""
        from cbrs.rut_scraper.domain import RUTScraper
        
        scraper = RUTScraper()
        
        # Valid RUTs
        assert scraper.validate_rut("11111111-1") is True
        assert scraper.validate_rut("22222222-2") is True
        assert scraper.validate_rut("111111111-K") is True
        
        # Invalid RUTs
        assert scraper.validate_rut("invalid") is False
        assert scraper.validate_rut("123") is False
    
    def test_parse_rut_from_html(self):
        """Test parsing RUT from HTML (placeholder)."""
        from cbrs.rut_scraper.domain import RUTScraper
        
        scraper = RUTScraper()
        
        # Placeholder implementation returns None
        result = scraper.parse_rut_from_html("<html><body></body></html>")
        assert result is None


class TestRegistrationOrchestrator:
    """Tests for the RegistrationOrchestrator class."""
    
    @pytest.fixture
    def mock_rut_scraper(self):
        """Create a mock RUT scraper."""
        mock = AsyncMock()
        mock.fetch_oldest_ruts = AsyncMock(return_value=[])
        return mock
    
    @pytest.fixture
    def mock_repository(self):
        """Create a mock repository."""
        mock = AsyncMock()
        mock.save_rut = AsyncMock(return_value=True)
        return mock
    
    @pytest.fixture
    def mock_email_provider(self):
        """Create a mock email provider."""
        mock = AsyncMock()
        mock.create_account = AsyncMock(return_value={
            'email': 'test@gmail.com',
            'password': 'password123',
            'created': True
        })
        return mock
    
    @pytest.fixture
    def orchestrator(self, mock_rut_scraper, mock_repository, mock_email_provider):
        """Create an orchestrator instance."""
        from cbrs.rut_scraper.domain import RegistrationOrchestrator
        
        return RegistrationOrchestrator(
            rut_scraper=mock_rut_scraper,
            email_provider=mock_email_provider,
            repository=mock_repository
        )
    
    async def test_register_full_process(self, orchestrator):
        """Test the full registration process."""
        result = await orchestrator.register_full_process(
            rut="11111111-1",
            fetcher=AsyncMock(),
            email_service=orchestrator.email_provider
        )
        
        assert 'rut' in result
        assert 'status' in result
        assert 'steps' in result
    
    async def test_batch_register(self, orchestrator):
        """Test batch registration."""
        results = await orchestrator.batch_register(
            ruts=["11111111-1", "22222222-2"],
            fetcher=AsyncMock(),
            email_service=orchestrator.email_provider,
            concurrency=2
        )
        
        assert len(results) == 2


class TestHttpRUTFetcher:
    """Tests for the HttpRUTFetcher adapter."""
    
    @pytest.fixture
    def fetcher(self):
        """Create a fetcher instance."""
        from cbrs.rut_scraper.adapters import HttpRUTFetcher
        return HttpRUTFetcher()
    
    async def test_fetch_ruts_empty_response(self, fetcher):
        """Test fetching with empty response."""
        with patch('aiohttp.ClientSession.post') as mock_post:
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value="")
            mock_post.return_value = mock_response
            
            result = await fetcher.fetch_ruts()
            
            assert result == []
    
    async def test_fetch_ruts_error_response(self, fetcher):
        """Test fetching with error response."""
        with patch('aiohttp.ClientSession.post') as mock_post:
            mock_response = AsyncMock()
            mock_response.status = 500
            mock_response.text = AsyncMock(return_value="Internal Server Error")
            mock_post.return_value = mock_response
            
            result = await fetcher.fetch_ruts()
            
            assert result == []
    
    def test_close(self, fetcher):
        """Test closing the fetcher."""
        fetcher.close()
        # Should not raise any exceptions


class TestSqliteRUTRepository:
    """Tests for the SqliteRUTRepository adapter."""
    
    @pytest.fixture
    def temp_db_path(self, tmp_path):
        """Create a temporary database path."""
        return str(tmp_path / "test_ruts.db")
    
    @pytest.fixture
    def repository(self, temp_db_path):
        """Create a repository instance."""
        from cbrs.rut_scraper.adapters import SqliteRUTRepository
        return SqliteRUTRepository(db_path=temp_db_path)
    
    @pytest.fixture
    def sample_entry(self):
        """Create a sample RUTEntry."""
        from cbrs.rut_scraper.domain import RUTEntry
        return RUTEntry(
            rut="11111111-1",
            name="Test User",
            company="Test Company",
            registration_date="2020-01-01"
        )
    
    async def test_save_rut(self, repository, sample_entry):
        """Test saving a single RUT."""
        result = await repository.save_rut(sample_entry)
        
        assert result is True
    
    async def test_save_batch(self, repository, sample_entry):
        """Test saving multiple RUTs."""
        entries = [
            sample_entry,
            RUTEntry(rut="22222222-2", name="User 2", registration_date="2019-01-01")
        ]
        
        count = await repository.save_batch(entries)
        
        assert count == 2
    
    async def test_get_oldest_ruts(self, repository, sample_entry):
        """Test getting oldest RUTs."""
        # First save some entries
        await repository.save_rut(sample_entry)
        
        oldest = await repository.get_oldest_ruts(limit=10)
        
        assert isinstance(oldest, list)
    
    async def test_exists(self, repository, sample_entry):
        """Test checking if a RUT exists."""
        await repository.save_rut(sample_entry)
        
        exists = await repository.exists("11111111-1")
        
        assert exists is True
    
    async def test_get_all_ruts(self, repository, sample_entry):
        """Test getting all RUTs."""
        await repository.save_rut(sample_entry)
        
        all_ruts = await repository.get_all_ruts()
        
        assert isinstance(all_ruts, list)


class TestRUTScraperSystem:
    """Tests for the RUTScraperSystem class."""
    
    @pytest.fixture
    def system(self, tmp_path):
        """Create a system instance."""
        from cbrs.rut_scraper import RUTScraperSystem
        
        settings = RUTScraperSettings(
            database_path=str(tmp_path / "test.db")
        )
        
        return RUTScraperSystem(settings=settings)
    
    async def test_fetch_and_store(self, system):
        """Test fetching and storing RUTs."""
        # This will return empty list since we haven't implemented the actual fetcher
        result = await system.fetch_and_store(limit=10)
        
        assert isinstance(result, list)
    
    async def test_get_all_ruts_ordered(self, system):
        """Test getting all RUTs ordered by registration date."""
        result = await system.get_all_ruts_ordered(order_by='registration_date')
        
        assert isinstance(result, list)
    
    async def test_close(self, system):
        """Test closing the system."""
        await system.close()


class TestRUTValidation:
    """Tests for RUT validation logic."""
    
    def test_valid_rut_formats(self):
        """Test various valid RUT formats."""
        from cbrs.rut_scraper.domain import RUTScraper
        
        scraper = RUTScraper()
        
        valid_ruts = [
            "11111111-1",
            "22222222-2",
            "111111111-K",
            "99999999-9",
            "00000000-0"
        ]
        
        for rut in valid_ruts:
            assert scraper.validate_rut(rut) is True
    
    def test_invalid_rut_formats(self):
        """Test various invalid RUT formats."""
        from cbrs.rut_scraper.domain import RUTScraper
        
        scraper = RUTScraper()
        
        invalid_ruts = [
            "invalid",
            "123",
            "111111111",  # Missing dash
            "11111111-",  # Missing verifier
            "11111111-@",  # Invalid verifier
        ]
        
        for rut in invalid_ruts:
            assert scraper.validate_rut(rut) is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
