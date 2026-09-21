"""Scraper for jobs.ch using their public search API."""

import logging
import random
import time
from datetime import datetime
from typing import Generator

from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from src.models import Job, JobType, SourcePortal
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import PoliteSession, RateLimiter

logger = logging.getLogger(__name__)

# jobs.ch public search API (proxied through www, no auth needed)
SEARCH_URL = "https://www.jobs.ch/api/v1/public/search"
# Detail endpoint — liefert template_text (volle Beschreibung als HTML).
# Die Suche selbst gibt nur "preview" (~100 Zeichen Skill-Tags) zurück.
DETAIL_URL = "https://www.jobs.ch/api/v1/public/search/job/{job_id}"

# Detail-Fetches sind ein leichter API-Call pro Job; der volle RateLimiter
# (2–5s) würde einen Scrape-Run um >30min verlängern. Kurzes Delay reicht.
DETAIL_DELAY_RANGE = (0.4, 1.0)

# Max 20 results per page, max 2000 total per query
MAX_ROWS_PER_PAGE = 20
MAX_TOTAL_RESULTS = 2000

# Employment type IDs on jobs.ch
EMPLOYMENT_TYPE_MAP = {
    "permanent": "5",
    "temporary": "2",
    "freelance": "3",
}


class JobsChScraper(BaseScraper):
    """Scraper for jobs.ch — the largest Swiss job portal."""

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self._rate_limiter = rate_limiter or RateLimiter()
        # PoliteSession enforces robots.txt, sends the honest UA and carries
        # certifi's CA bundle (macOS Python ships without the system roots).
        self._session = PoliteSession(self._rate_limiter, "application/json")

    @property
    def source(self) -> SourcePortal:
        return SourcePortal.JOBS_CH

    @property
    def name(self) -> str:
        return "jobs.ch"

    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        keywords = search_config.get("keywords", [])
        location = search_config.get("location", "Zürich")
        results_per_keyword = min(
            search_config.get("results_per_keyword", 30),
            MAX_TOTAL_RESULTS,
        )

        workload = search_config.get("workload", {})
        workload_min = workload.get("min_percent")
        workload_max = workload.get("max_percent")

        seen_ids: set[str] = set()

        for keyword in keywords:
            logger.info(f"[jobs.ch] Searching: '{keyword}' in {location}")
            yielded = 0

            page = 1
            while yielded < results_per_keyword:
                try:
                    data = self._fetch_page(
                        query=keyword,
                        location=location,
                        page=page,
                        workload_min=workload_min,
                        workload_max=workload_max,
                    )
                except Exception as e:
                    logger.error(f"[jobs.ch] Failed to fetch page {page} for '{keyword}': {e}")
                    break

                documents = data.get("documents", [])
                if not documents:
                    break

                for doc in documents:
                    job_id = doc.get("job_id", "")
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    yield self._parse_job(doc)
                    yielded += 1

                    if yielded >= results_per_keyword:
                        break

                total_hits = data.get("total_hits", 0)
                if page * MAX_ROWS_PER_PAGE >= min(total_hits, MAX_TOTAL_RESULTS):
                    break

                page += 1
                self._rate_limiter.wait()

            logger.info(f"[jobs.ch] '{keyword}': {yielded} jobs found")
            self.log_query(keyword, location, yielded)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_page(
        self,
        query: str,
        location: str,
        page: int = 1,
        workload_min: int | None = None,
        workload_max: int | None = None,
    ) -> dict:
        """Fetch a single page of search results from jobs.ch API."""
        params: dict = {
            "query": query,
            "location": location,
            "rows": MAX_ROWS_PER_PAGE,
            "page": page,
        }

        if workload_min is not None:
            params["employment-grade-from"] = workload_min
        if workload_max is not None:
            params["employment-grade-to"] = workload_max

        response = self._session.get(SEARCH_URL, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def _parse_job(self, doc: dict) -> Job:
        """Convert a jobs.ch API document into a Job model."""
        job_id = doc.get("job_id", "")

        # Build the English detail URL
        links = doc.get("_links", {})
        url = links.get("detail_en", {}).get("href", "")
        if not url:
            url = f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"

        # Parse workload from tags
        workload_min, workload_max = self._parse_workload(doc)

        # Parse language skills
        lang_skills = doc.get("language_skills", [])
        languages = [ls.get("language", "").upper() for ls in lang_skills]

        # Determine job type from employment_type_ids
        job_type = self._parse_job_type(doc.get("employment_type_ids", []))

        # Volle Beschreibung vom Detail-Endpoint; preview nur als Fallback.
        description = self._fetch_description(job_id) or doc.get("preview", "")

        return Job(
            title=doc.get("title", "").strip(),
            company=doc.get("company_name", "").strip(),
            url=url,
            source=self.source,
            location=doc.get("place", ""),
            is_remote=None,  # jobs.ch doesn't expose this directly in search
            description=description,
            job_type=job_type,
            workload_percent=workload_max,  # Use max as the primary value
            external_id=job_id,
            date_posted=self._parse_date(doc.get("publication_date")),
            languages=languages,
            company_size=self._parse_company_segment(doc.get("company_segmentation")),
        )

    def _fetch_description(self, job_id: str) -> str | None:
        """Fetch the full job description from the detail endpoint.

        Returns plain text (HTML stripped) or None on any failure — the
        caller falls back to the search preview snippet.
        """
        if not job_id:
            return None
        try:
            response = self._session.get(
                DETAIL_URL.format(job_id=job_id), timeout=15
            )
            response.raise_for_status()
            detail = response.json()
        except Exception as e:
            logger.debug(f"[jobs.ch] Detail fetch failed for {job_id}: {e}")
            return None
        finally:
            time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

        parts = []
        lead = self._html_to_text(detail.get("template_lead_text") or "")
        body = self._html_to_text(detail.get("template_text") or "")
        if not body:
            # Manche Inserate haben nur das volle HTML-Template
            body = self._html_to_text(detail.get("template") or "")
        if lead:
            parts.append(lead)
        if body:
            parts.append(body)

        text = "\n\n".join(parts).strip()
        return text or None

    @staticmethod
    def _html_to_text(html: str) -> str:
        if not html or not html.strip():
            return ""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)

    @staticmethod
    def _parse_workload(doc: dict) -> tuple[int | None, int | None]:
        """Extract min/max workload percentage from tags."""
        for tag in doc.get("tags", []):
            if tag.get("type") == "employment_grade":
                return tag.get("value_min"), tag.get("value_max")
        # Fallback to employment_grades list
        grades = doc.get("employment_grades", [])
        if grades:
            return min(grades), max(grades)
        return None, None

    @staticmethod
    def _parse_job_type(type_ids: list) -> JobType:
        if "5" in type_ids:
            return JobType.FULL_TIME
        if "2" in type_ids:
            return JobType.CONTRACT
        if "3" in type_ids:
            return JobType.CONTRACT
        return JobType.UNKNOWN

    @staticmethod
    def _parse_date(raw: str | None) -> datetime | None:
        """Parse publication_date from jobs.ch API (ISO 8601 string) to datetime.

        Fixes the "'str' object has no attribute 'isoformat'" bug: the Job model
        expects datetime, but the API returns strings like "2024-01-15T00:00:00Z"
        or "2024-01-15". Returns None on any parsing failure (safe fallback).
        """
        if not raw or not isinstance(raw, str):
            return None
        try:
            # Handle the trailing "Z" (UTC) by replacing it with +00:00
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            logger.debug(f"[jobs.ch] Could not parse date: {raw!r}")
            return None

    @staticmethod
    def _parse_company_segment(segment: str | None) -> str | None:
        mapping = {
            "kmu": "SME",
            "gu": "Large Enterprise",
            "pdl": "Staffing Agency",
        }
        return mapping.get(segment) if segment else None
