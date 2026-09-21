"""
Scraper für tecnoempleo.com — Spanisches Tech-Jobboard.

Server-rendered, requests + BeautifulSoup reichen.

URL-Schema:
  Suche:  /ofertas-trabajo/?te=<keyword>&pr=<province>&pagina=<n>
  Detail: /<title-slug>-<company-slug>/<id>/

Strategie:
- Keywords aus der Config nutzen
- Nur Barcelona als Provinz (passt zu unserem location_secondary)
- Pagination via &pagina=
- Detail-Pages für volle Description holen (rate-limited)
"""

import logging
from typing import Generator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.models import Job, SourcePortal
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import PoliteSession, RateLimiter

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.tecnoempleo.com"
SEARCH_URL = "https://www.tecnoempleo.com/ofertas-trabajo/"
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

MAX_PAGES_PER_KEYWORD = 3  # 30 Treffer/Seite × 3 = max 90 Jobs/Keyword

# Mapping unserer Stadt-Schreibweisen → tecnoempleo Provinz-Slugs
SPANISH_CITY_TO_PROVINCE: dict[str, str] = {
    "barcelona": "barcelona",
    "madrid":    "madrid",
    "valencia":  "valencia",
    "sevilla":   "sevilla",
    "bilbao":    "vizcaya",      # Bilbao gehört zu Provinz Vizcaya
    "malaga":    "malaga",
    "zaragoza":  "zaragoza",
}


class TecnoempleoScraper(BaseScraper):
    """tecnoempleo.com — IT/Tech Jobs Spanien."""

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self._rate_limiter = rate_limiter or RateLimiter()
        # PoliteSession enforces robots.txt, sends the honest UA and carries
        # certifi's CA bundle (macOS Python ships without the system roots).
        self._session = PoliteSession(self._rate_limiter, ACCEPT)

    @property
    def source(self) -> SourcePortal:
        return SourcePortal.TECNOEMPLEO

    @property
    def name(self) -> str:
        return "tecnoempleo.com"

    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        # Alle konfigurierten Sekundär-Locations sammeln
        locations_secondary = search_config.get("locations_secondary") or []
        if isinstance(locations_secondary, str):
            locations_secondary = [locations_secondary]
        legacy = search_config.get("location_secondary")
        if legacy and legacy not in locations_secondary:
            locations_secondary.append(legacy)

        # Nur spanische Städte mit Provinz-Mapping behalten
        spanish_provinces = []
        for loc in locations_secondary:
            slug = SPANISH_CITY_TO_PROVINCE.get(loc.lower())
            if slug and slug not in spanish_provinces:
                spanish_provinces.append(slug)

        if not spanish_provinces:
            logger.info("[tecnoempleo] Übersprungen — keine spanische Stadt in Config")
            return

        keywords = search_config.get("keywords", [])
        seen_urls: set[str] = set()

        for province in spanish_provinces:
            for keyword in keywords:
                yielded = 0
                for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
                    logger.info(f"[tecnoempleo] '{keyword}' @ {province} (Seite {page})")
                    try:
                        cards = self._fetch_page(keyword, province, page)
                    except Exception as e:
                        logger.warning(f"[tecnoempleo] Fehler bei '{keyword}' Seite {page}: {e}")
                        break

                    if not cards:
                        break  # Keine weiteren Seiten

                    new_in_page = 0
                    for card in cards:
                        if card["url"] in seen_urls:
                            continue
                        seen_urls.add(card["url"])

                        job = self._card_to_job(card, province)
                        if job:
                            yield job
                            yielded += 1
                            new_in_page += 1

                    if new_in_page == 0:
                        break
                    self._rate_limiter.wait()

                logger.info(f"[tecnoempleo] '{keyword}' @ {province}: {yielded} Jobs")

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _fetch_page(self, keyword: str, province: str, page: int) -> list[dict]:
        """
        Liest die Listings-Seite und extrahiert Title/Company/Location/URL
        direkt aus den Job-Karten — KEIN Detail-Fetch (URLs verfallen mit 410).

        DOM-Struktur (verifiziert):
          div.p-3.border.rounded.mb-3.bg-white  ← Card
            ├── a.font-weight-bold              ← Titel + URL
            ├── a.text-primary.link-muted       ← Firma
            └── span.d-block.d-lg-none          ← "Ort - Datum"
        """
        params = {"te": keyword, "pr": province, "pagina": page}
        resp = self._session.get(SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        cards: list[dict] = []

        for card_div in soup.select("div.p-3.border.rounded.mb-3.bg-white"):
            title_a = card_div.select_one("a.font-weight-bold")
            if not title_a:
                continue
            href = title_a.get("href", "")
            full_url = href if href.startswith("http") else urljoin(BASE_URL, href)
            if "/rf-" not in full_url:
                continue
            title = title_a.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            company_a = card_div.select_one("a.text-primary.link-muted")
            company = company_a.get_text(strip=True) if company_a else ""

            loc_span = card_div.select_one("span.d-block.d-lg-none")
            loc_text = loc_span.get_text(" ", strip=True) if loc_span else ""
            # Format: "Zaragoza - 23/04/2026" oder "Madrid (Híbrido) - 23/04/2026"
            location = loc_text.split("-", 1)[0].strip() if loc_text else ""

            cards.append({
                "url": full_url,
                "title": title,
                "company": company,
                "location": location,
            })

        return cards

    def _card_to_job(self, card: dict, fallback_province: str) -> Job | None:
        """
        Erstellt einen Job-Eintrag direkt aus der Listing-Card.
        Kein Detail-Fetch — tecnoempleo's Detail-URLs verfallen schnell (410).
        Die Description bleibt leer; Scoring greift dann v.a. auf Titel zurück.
        """
        location = card.get("location") or fallback_province.title()
        return Job(
            title=card["title"],
            company=card.get("company") or "Unknown",
            url=card["url"],
            source=self.source,
            location=location,
            description="",  # Detail-Pages liefern 410 — verzichten wir drauf
        )
