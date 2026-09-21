"""
Scraper für barcelonajobs.com — Tech/Creative/Multilingual Jobs in Barcelona.

Die Seite hostet auf Jobboardly (`barcelonajobs.jobboardly.com`) und stellt
einen sauberen RSS-Feed mit ALLEN aktuellen Stellen bereit. Kein HTML-Parsing
nötig — ein einziger Request liefert alle Jobs strukturiert.

Title-Pattern (>99% Hit-Rate, von 694 Einträgen geprüft):
    "{job_title} - {company} - {location}"

Edge-Cases die der Parser handhabt:
  * Mehrere Bindestriche im Titel (z.B. "Beca - Business Dev - Avnet - BCN, ES")
    → Splitte von hinten: letztes Segment = Location, vorletztes = Company.
  * "Trabajos en {company}" oder "Who are we? {company}" Prefixe in Company-Feld
    → Beide gestrippt für saubere Firmen-Namen.
  * Location oft "Barcelona, Spain" oder "Barcelona, Barcelona, Spain" — durchreichen
    wie es ist, das Scoring kümmert sich um Normalisierung.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Generator

from bs4 import BeautifulSoup

from src.models import Job, SourcePortal
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import PoliteSession, RateLimiter

logger = logging.getLogger(__name__)

RSS_URL = "https://www.barcelonajobs.com/jobs.rss"
ACCEPT = "application/rss+xml, application/xml, text/xml, */*"

# Prefixe die Companies im RSS-Title manchmal voranstellen (Jobboardly-Quirks)
COMPANY_PREFIX_RE = re.compile(
    r"^(Trabajos en|Who are we\??|Join|Work at|About)\s+",
    re.IGNORECASE,
)


class BarcelonaJobsScraper(BaseScraper):
    """Scrapt barcelonajobs.com via deren öffentlichen RSS-Feed."""

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self._rate_limiter = rate_limiter or RateLimiter()
        # PoliteSession enforces robots.txt, sends the honest UA and carries
        # certifi's CA bundle (macOS Python ships without the system roots).
        self._session = PoliteSession(self._rate_limiter, ACCEPT)

    @property
    def source(self) -> SourcePortal:
        return SourcePortal.BARCELONAJOBS

    @property
    def name(self) -> str:
        return "barcelonajobs.com"

    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        logger.info("[barcelonajobs] RSS-Feed wird gefetcht ...")

        try:
            resp = self._session.get(RSS_URL, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[barcelonajobs] RSS-Fetch fehlgeschlagen: {e}")
            return

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            logger.error(f"[barcelonajobs] RSS-Parse fehlgeschlagen: {e}")
            return

        items = root.findall(".//item")
        logger.info(f"[barcelonajobs] {len(items)} Items im Feed")

        count = 0
        for item in items:
            job = self._parse_item(item)
            if job:
                yield job
                count += 1

        logger.info(f"[barcelonajobs] {count} Jobs geyieldet")

    def _parse_item(self, item: ET.Element) -> Job | None:
        raw_title = self._text(item, "title")
        link = self._text(item, "link")
        category = self._text(item, "category")
        description_html = self._text(item, "description")

        if not raw_title or not link:
            return None

        title, company, location = self._split_title(raw_title)
        if not title:
            return None

        description = None
        if description_html:
            soup = BeautifulSoup(description_html, "lxml")
            description = soup.get_text(separator=" ", strip=True)[:3000]

        return Job(
            title=title,
            company=company,
            url=link,
            source=self.source,
            location=location,
            description=description,
            industry=category or None,
            is_startup=None,  # Board ist mixed (Startups + Mid-Market + Enterprise)
        )

    @staticmethod
    def _text(item: ET.Element, tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    @staticmethod
    def _split_title(raw: str) -> tuple[str, str, str]:
        """Zerlegt "{title} - {company} - {location}" robust.

        Splitten von hinten ist sicherer, weil viele Job-Titel Bindestriche
        enthalten (z.B. "Beca - Business Development").
        """
        parts = [p.strip() for p in raw.split(" - ") if p.strip()]
        if len(parts) < 3:
            # Fallback: nur 2 Teile = "{title} - {location}" ohne explizite Firma
            if len(parts) == 2:
                return parts[0], "", parts[1]
            return raw.strip(), "", ""

        location = parts[-1]
        company = COMPANY_PREFIX_RE.sub("", parts[-2]).strip()
        title = " - ".join(parts[:-2]).strip()
        return title, company, location
