"""Abstract base class for all scrapers."""

from abc import ABC, abstractmethod
from typing import Generator

from src.models import Job, SourcePortal


class BaseScraper(ABC):
    """Interface that every scraper must implement."""

    @property
    @abstractmethod
    def source(self) -> SourcePortal:
        """Which portal this scraper targets."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging."""
        ...

    @abstractmethod
    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        """
        Yield Job objects matching the search configuration.

        Args:
            search_config: Dict from the 'search' section of profile.yaml.
                Keys: keywords, location, radius_km, workload, job_types, etc.

        Yields:
            Job instances (one per listing found).
        """
        ...

    def is_available(self) -> bool:
        """Check if this scraper can run (dependencies, network, etc.)."""
        return True

    @property
    def query_log(self) -> list[dict]:
        """Per-search results, filled by scrapers that loop over keywords.

        Entries are ``{"keyword", "location", "results_count"}``. The
        orchestrator drains this after each scraper and writes it to the
        search_queries table, which is what makes "which keyword actually
        earns its runtime?" answerable later — scrapers themselves have no
        DB access.

        Built lazily so subclasses need not call ``super().__init__()``.
        """
        if not hasattr(self, "_query_log"):
            self._query_log: list[dict] = []
        return self._query_log

    def log_query(self, keyword: str, location: str, results_count: int) -> None:
        """Record one keyword/location search and how many jobs it returned."""
        self.query_log.append(
            {
                "keyword": keyword,
                "location": location,
                "results_count": results_count,
            }
        )
