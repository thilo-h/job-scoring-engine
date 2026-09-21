"""
Scraper für startupticker.ch/en/jobs — Schweizer Startup-Ökosystem.

startupticker.ch listet Jobs aus dem Schweizer Startup-Umfeld.
Die Seite wird server-seitig gerendert, requests + BeautifulSoup reichen.

Falls die Seitenstruktur sich ändert und keine Jobs gefunden werden,
gibt der Scraper einfach 0 Resultate zurück (kein Crash).
"""

import logging
import re
from typing import Generator

from bs4 import BeautifulSoup

from src.models import Job, SourcePortal
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import PoliteSession, RateLimiter

logger = logging.getLogger(__name__)

BASE_URL  = "https://www.startupticker.ch"
JOBS_URL  = "https://www.startupticker.ch/en/jobs"
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# Die Detailseite verschachtelt Label + Wert im selben Element, get_text()
# liefert daher "Company name<NAME>Company location<CITY>".
_COMPANY_BLOCK_RE = re.compile(r"^Company name(.*?)(?:Company location(.*))?$", re.S)


class StartuptickerScraper(BaseScraper):
    """
    Scraped alle aktuellen Jobs von startupticker.ch.

    Kein Keyword-Filter (die Seite hat keine Suchfunktion) — alle Jobs
    werden geholt und das Scoring filtert relevante heraus.
    """

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self._rate_limiter = rate_limiter or RateLimiter()
        # PoliteSession enforces robots.txt, sends the honest UA and carries
        # certifi's CA bundle (macOS Python ships without the system roots).
        self._session = PoliteSession(self._rate_limiter, ACCEPT)

    @property
    def source(self) -> SourcePortal:
        return SourcePortal.STARTUPTICKER

    @property
    def name(self) -> str:
        return "startupticker.ch"

    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        """Fetcht alle Jobs von startupticker.ch (keine Keyword-Filterung)."""
        logger.info("[startupticker] Fetching jobs listing...")

        try:
            resp = self._session.get(JOBS_URL, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[startupticker] Fetch fehlgeschlagen: {e}")
            return

        soup  = BeautifulSoup(resp.text, "lxml")
        count = 0

        # Strategie 1: Suche nach Link-Elementen die auf Job-Detail-Seiten zeigen
        # startupticker.ch listet Jobs als Karten mit Titel, Firma und Link
        job_links = self._find_job_links(soup)

        if not job_links:
            logger.warning("[startupticker] Keine Job-Links gefunden — Seitenstruktur evtl. geändert")
            return

        for link_data in job_links:
            job = self._fetch_job_detail(link_data)
            if job:
                yield job
                count += 1
                self._rate_limiter.wait()

        logger.info(f"[startupticker] {count} Jobs gefunden")

    def _find_job_links(self, soup: BeautifulSoup) -> list[dict]:
        """
        Extrahiert Job-Links aus der Listings-Seite.
        Versucht mehrere Selektoren da sich die Seitenstruktur ändern kann.
        """
        links = []

        # Variante A: <a> Tags mit /jobs/ oder /en/jobs/ im href
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/jobs/" in href and href != "/en/jobs" and href != "/jobs":
                full_url = href if href.startswith("http") else BASE_URL + href
                title = a.get_text(strip=True)
                if title and len(title) > 5:  # Mindestlänge für sinnvolle Titel
                    links.append({"url": full_url, "title": title, "company": ""})

        # Variante B: Suche nach job-spezifischen CSS-Klassen
        if not links:
            for item in soup.select(".job-item, .job-card, .vacancy, [class*='job']"):
                a = item.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                full_url = href if href.startswith("http") else BASE_URL + href
                title = item.get_text(strip=True)[:100]
                links.append({"url": full_url, "title": title, "company": ""})

        # Deduplizieren nach URL
        seen = set()
        unique = []
        for link in links:
            if link["url"] not in seen:
                seen.add(link["url"])
                unique.append(link)

        return unique

    def _fetch_job_detail(self, link_data: dict) -> Job | None:
        """Fetcht eine Job-Detailseite und extrahiert die Informationen."""
        url   = link_data["url"]
        title = link_data.get("title", "")

        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            logger.debug(f"[startupticker] Detail-Fetch fehlgeschlagen ({url}): {e}")
            # Fallback: nur mit den Listing-Daten arbeiten
            if title:
                return Job(
                    title=title,
                    company=link_data.get("company", ""),
                    url=url,
                    source=self.source,
                    location="Switzerland",
                )
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # Titel aus der Detailseite (präziser als Listing-Text)
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

        # Firma suchen
        company = ""
        company_city = ""
        for selector in [".company-name", ".employer", "[class*='company']", "h2"]:
            el = soup.select_one(selector)
            if el:
                company = el.get_text(strip=True)
                m = _COMPANY_BLOCK_RE.match(company)
                if m:
                    company = m.group(1).strip()
                    company_city = (m.group(2) or "").strip()
                break

        # Beschreibung: grösster Textblock auf der Seite
        description = ""
        main = soup.find("main") or soup.find("article") or soup.find("body")
        if main:
            description = main.get_text(separator=" ", strip=True)[:3000]

        # Standort — die Stadt steckt im Company-Block ("Company location<CITY>")
        location = company_city or "Switzerland"
        for selector in [".location", "[class*='location']", "[class*='place']"]:
            el = soup.select_one(selector)
            if el:
                text = re.sub(r"^Company location", "", el.get_text(strip=True)).strip()
                if text:
                    location = text
                break

        if not title:
            return None

        return Job(
            title=title,
            company=company,
            url=url,
            source=self.source,
            location=location,
            description=description,
            is_startup=True,  # startupticker.ch ist per Definition Startup-fokussiert
        )
