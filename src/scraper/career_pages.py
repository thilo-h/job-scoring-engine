"""
Scraper für kuratierte Firmen-Karriere-Seiten.

Statt einen generischen Job-Aggregator zu scrapen, geht dieser Scraper direkt
auf die `/careers`-Seiten ausgewählter Firmen aus `profile.yaml::search.career_pages`.

Vorteile gegenüber generischen Boards:
  * Garantierter Tier-Match (die Firmen-Liste = bewusst kuratiert)
  * Funktioniert wo `jobs.ch` / `LinkedIn` Listings noch nicht sieht
  * Wiederverwendbar — andere Profile (z.B. Beauty/Pharma) füllen nur die
    Liste mit ihren eigenen Firmen, gleicher Code

Heuristik pro Firma:
  1. GET der Karriere-URL
  2. Suche nach <a>-Tags deren href Job-Detail-Pattern enthält
     (/jobs/, /career/, /position/, ?gh_jid=, lever.co/, personio, …)
  3. Link-Text als Titel, dedupliziert nach URL
  4. Filterung gegen offensichtliche Nicht-Job-Links ("Apply", "View all" …)
  5. Yield ein Job-Objekt pro Treffer

Robust: Pro Firma in try/except, einzelne Failures unterbrechen den Run nicht.
Wenn eine Firma 0 Jobs liefert (z.B. SPA die alles client-side rendert), wird
das geloggt aber kein Crash.
"""

import logging
import re
from typing import Generator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.models import Job, SourcePortal
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import PoliteSession, RateLimiter

logger = logging.getLogger(__name__)

ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# href-Pattern die auf eine konkrete Job-Detailseite zeigen
JOB_HREF_PATTERNS = [
    r"/jobs?/",
    r"/career/",
    r"/careers/",
    r"/position",
    r"/opening",
    r"/role/",
    r"/p/",
    r"/posting",
    r"/apply",
    r"/vacanc",
    r"/stelle",
    r"/empleo",
    r"/oferta",
    r"gh_jid=",                       # Greenhouse query param
    r"boards\.greenhouse\.io/",       # Greenhouse embed
    r"jobs\.lever\.co/",              # Lever embed
    r"\.personio\.(de|com)/jobs/",    # Personio
    r"smartrecruiters\.com/jobs/",    # SmartRecruiters
    r"workday(jobs)?\.com",           # Workday
    r"recruitee\.com/o/",             # Recruitee
    r"join\.com/companies/",          # Join.com
]
JOB_HREF_RE = re.compile("|".join(JOB_HREF_PATTERNS), re.IGNORECASE)

# Link-Texte die offensichtlich KEIN Job-Titel sind
TEXT_BLACKLIST = {
    "apply", "apply now", "view all", "view all jobs", "see all",
    "see all openings", "see all jobs", "see open positions",
    "open positions", "all jobs", "all positions", "careers", "jobs",
    "join us", "join our team",
    "back", "next", "previous", "read more", "learn more", "more",
    "view job", "details", "share", "save", "follow",
    "alle stellen", "alle jobs", "mehr erfahren", "weiterlesen",
    "ver todas", "ver más", "más información", "todas las ofertas",
}

# Generische Department/Category-Texte — keine echten Job-Titel
GENERIC_DEPT_TEXT = {
    "engineering", "marketing", "sales", "finance", "finance & legal",
    "data & analytics", "data analytics", "operations", "people",
    "human resources", "hr", "legal", "design", "product",
    "general & administration", "general and administration", "g&a",
    "no department", "executive leadership", "leadership",
    "customer success", "support", "business development",
    "research & development", "r&d", "manufacturing", "quality",
    "regulatory", "supply chain", "it",
}

# Erkennung von Locale-/Sprach-Switcher-Links (z.B. "English (CA)", "Español")
LOCALE_TEXT_RE = re.compile(
    r"^(english|deutsch|français|francais|espanol|español|italiano|portugu[eê]s|"
    r"日本語|中文|한국어|русский|polski|nederlands|svenska|dansk|norsk|suomi)"
    r"(\s*\([a-z]{2,4}\))?$",
    re.IGNORECASE,
)

# Locale-Prefix im URL-Pfad (z.B. /en-ca/, /jp/, /de-de/) — Sprach-Variante derselben Seite
LOCALE_PATH_RE = re.compile(
    r"/(en|de|fr|es|it|pt|ja|jp|zh|cn|nl|sv|fi|da|no|pl|cs|hu|ru|ko|ar|tr|el)"
    r"(-[a-z]{2,3})?/(?=careers?/?$|empleos?/?$|stellen/?$)",
    re.IGNORECASE,
)

# href-Pattern die KEINE Job-Pages sind (false-positive guard)
HREF_EXCLUDE_PATTERNS = [
    r"^mailto:",
    r"^tel:",
    r"^javascript:",
    r"^#",
    r"/blog/",
    r"/news/",
    r"/press/",
    r"/podcast/",
    r"/event/",
    r"/about/?$",
    r"/contact/?$",
    r"/login",
    r"/signup",
    r"/legal/",
    r"/privacy",
    r"/terms",
    r"/cookies?$",
    r"\.pdf$",
    r"\.jpg$|\.png$|\.svg$|\.webp$",
]
HREF_EXCLUDE_RE = re.compile("|".join(HREF_EXCLUDE_PATTERNS), re.IGNORECASE)

# Limit pro Firma, um Runaway-Fälle zu kappen: ein ATS-Board eines grösseren
# Arbeitgebers kann vierstellig viele Stellen führen. Der ursprüngliche Wert von
# 50 schnitt mehrere Boards hart ab, bevor die interessanten Stellen überhaupt
# gesehen wurden. Das Scoring verwirft ohnehin die grosse Mehrheit, also ist mehr
# Rohmaterial hier günstiger als ein zu enger Deckel.
MAX_JOBS_PER_COMPANY = 200


class CareerPagesScraper(BaseScraper):
    """Scrapt eine Liste kuratierter Firmen-Karriereseiten."""

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self._rate_limiter = rate_limiter or RateLimiter()
        # PoliteSession enforces robots.txt, sends the honest UA and carries
        # certifi's CA bundle (macOS Python ships without the system roots).
        self._session = PoliteSession(self._rate_limiter, ACCEPT)

    @property
    def source(self) -> SourcePortal:
        return SourcePortal.CAREER_PAGE

    @property
    def name(self) -> str:
        return "career_pages"

    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        companies = search_config.get("career_pages") or []
        if not companies:
            logger.warning(
                "[career_pages] Keine Einträge unter search.career_pages in profile.yaml"
            )
            return

        logger.info(f"[career_pages] {len(companies)} Firmen werden geprüft")

        total = 0
        for entry in companies:
            try:
                count = 0
                for job in self._scrape_company(entry):
                    yield job
                    count += 1
                    total += 1
                logger.info(
                    f"[career_pages] {entry.get('name', '?')}: {count} Jobs"
                )
            except Exception as e:
                logger.warning(
                    f"[career_pages] {entry.get('name', '?')} fehlgeschlagen: {e}"
                )
            self._rate_limiter.wait()

        logger.info(f"[career_pages] Total: {total} Jobs aus {len(companies)} Firmen")

    def scrape_company(self, entry: dict) -> Generator[Job, None, None]:
        """Öffentlicher Einstieg für eine einzelne Firma (Watchlist-Poll).

        ``entry`` hat dieselbe Form wie ein Eintrag unter
        ``search.career_pages``: mindestens ``name`` plus ``url`` oder
        ``ats``/``ats_slug``.
        """
        yield from self._scrape_company(entry)

    def _scrape_company(self, entry: dict) -> Generator[Job, None, None]:
        name = entry.get("name", "")
        if not name:
            return

        # ATS-direkter Pfad (preferred) — viel zuverlässiger als HTML-Parsing,
        # weil viele moderne Career-Pages SPAs sind und Jobs erst nach JS-Load
        # via ATS-API fetchen.
        ats = (entry.get("ats") or "").lower()
        if ats == "greenhouse":
            yield from self._scrape_greenhouse(entry)
            return
        if ats == "lever":
            yield from self._scrape_lever(entry)
            return
        if ats == "workday":
            yield from self._scrape_workday(entry)
            return
        if ats == "teamtailor":
            yield from self._scrape_teamtailor(entry)
            return
        if ats == "ashby":
            yield from self._scrape_ashby(entry)
            return

        url = entry.get("url")
        if not url:
            return

        try:
            resp = self._session.get(url, timeout=15)
        except Exception as e:
            logger.debug(f"[career_pages] GET fehlgeschlagen ({url}): {e}")
            return

        if resp.status_code != 200:
            logger.debug(
                f"[career_pages] {name}: HTTP {resp.status_code} ({url})"
            )
            return

        soup = BeautifulSoup(resp.text, "lxml")
        seen_urls: set[str] = set()
        count = 0

        for a in soup.find_all("a", href=True):
            if count >= MAX_JOBS_PER_COMPANY:
                break

            href = a["href"].strip()
            if not href or HREF_EXCLUDE_RE.search(href):
                continue
            if not JOB_HREF_RE.search(href):
                continue
            # Locale-Switcher: gleicher Pfad in anderer Sprache
            if LOCALE_PATH_RE.search(href):
                continue

            text = a.get_text(strip=True)
            if not text or len(text) < 5 or len(text) > 150:
                continue
            text_lower = text.lower()
            if text_lower in TEXT_BLACKLIST:
                continue
            if text_lower in GENERIC_DEPT_TEXT:
                continue
            if LOCALE_TEXT_RE.match(text):
                continue

            full_url = urljoin(url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            # Filtere out wenn URL exakt der Karriere-Übersichtsseite entspricht
            if full_url.rstrip("/") == url.rstrip("/"):
                continue

            # Department-Übersichtsseite erkennen: URL endet nach /careers/
            # mit nur einem Segment OHNE Bindestriche/IDs (= category page,
            # nicht echter Job). Z.B. firma.example/careers/marketing
            path = urlparse(full_url).path.rstrip("/")
            last_seg = path.rsplit("/", 1)[-1] if "/" in path else ""
            if last_seg and "-" not in last_seg and not any(c.isdigit() for c in last_seg):
                # nur 1 Wort, keine Slug-Struktur, keine Job-ID → vermutlich Category
                # Aber: kurze Job-Titel wie "ceo" lassen wir durch via Whitelist-Check
                if last_seg.lower() in GENERIC_DEPT_TEXT:
                    continue

            yield Job(
                title=text,
                company=name,
                url=full_url,
                source=self.source,
                location=entry.get("location", ""),
                industry=entry.get("industry"),
                is_startup=entry.get("is_startup"),
                description=None,
            )
            count += 1

    # ---- ATS-spezifische Adapter ----

    def _scrape_greenhouse(self, entry: dict) -> Generator[Job, None, None]:
        """Greenhouse Job-Board (z.B. boards.greenhouse.io/<slug>).

        Public JSON-API: viel zuverlässiger als HTML-Scraping. Liefert
        title, location, departments, absolute_url, updated_at pro Job.
        """
        slug = entry.get("ats_slug") or entry.get("name", "").lower().replace(" ", "")
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            resp = self._session.get(api_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.debug(f"[career_pages] Greenhouse-API fail ({slug}): {e}")
            return

        for job in data.get("jobs") or []:
            title = (job.get("title") or "").strip()
            url = job.get("absolute_url") or ""
            if not title or not url:
                continue
            location = (job.get("location") or {}).get("name", "")
            yield Job(
                title=title,
                company=entry.get("name", ""),
                url=url,
                source=self.source,
                location=location or entry.get("location", ""),
                industry=entry.get("industry"),
                is_startup=entry.get("is_startup"),
                external_id=f"gh:{slug}:{job.get('id')}",
            )

    def _scrape_workday(self, entry: dict) -> Generator[Job, None, None]:
        """Workday Job-Board (z.B. galderma.wd3.myworkdayjobs.com/external).

        Public JSON-API at /wday/cxs/<tenant>/<site>/jobs accepts a POST
        with pagination + filters. Tenant + site are usually the same as
        the URL path segments. ``ats_slug`` here is `<tenant>/<site>` with
        an optional region prefix `wdN.` (e.g. ``wd3.galderma/external``).
        Default region is ``wd3`` if not given.

        Discovery (manual): open the company's careers page, DevTools →
        Network → filter ``myworkdayjobs.com`` → first request shows
        ``https://<tenant>.wd<N>.myworkdayjobs.com/<site>``. Set
        ``ats_slug: "<tenant>/<site>"`` (and optionally prefix ``wd<N>.``).
        """
        slug_raw = (entry.get("ats_slug") or "").strip()
        if "/" not in slug_raw:
            logger.debug(
                f"[career_pages] Workday slug must be '<tenant>/<site>' "
                f"(optionally prefixed with 'wdN.'), got {slug_raw!r}"
            )
            return

        # Optional region prefix
        region = "wd3"
        if slug_raw.split(".")[0].startswith("wd") and slug_raw.split(".")[0][2:].isdigit():
            region, slug_raw = slug_raw.split(".", 1)
        tenant, site = slug_raw.split("/", 1)
        api_url = (
            f"https://{tenant}.{region}.myworkdayjobs.com/wday/cxs/"
            f"{tenant}/{site}/jobs"
        )

        # Workday paginates in offsets of 20. Cap to MAX_JOBS_PER_COMPANY.
        offset = 0
        per_page = 20
        try:
            while offset < MAX_JOBS_PER_COMPANY:
                payload = {
                    "appliedFacets": {},
                    "limit": per_page,
                    "offset": offset,
                    "searchText": "",
                }
                resp = self._session.post(
                    api_url, json=payload, timeout=15,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    logger.debug(
                        f"[career_pages] Workday {tenant}/{site}: HTTP {resp.status_code}"
                    )
                    return
                data = resp.json()
                postings = data.get("jobPostings") or []
                if not postings:
                    return

                for p in postings:
                    title = (p.get("title") or "").strip()
                    external_path = p.get("externalPath") or ""
                    if not title or not external_path:
                        continue
                    full_url = (
                        f"https://{tenant}.{region}.myworkdayjobs.com/"
                        f"{site}{external_path}"
                    )
                    location = p.get("locationsText") or ""
                    yield Job(
                        title=title,
                        company=entry.get("name", ""),
                        url=full_url,
                        source=self.source,
                        location=location or entry.get("location", ""),
                        industry=entry.get("industry"),
                        is_startup=entry.get("is_startup"),
                        external_id=f"workday:{tenant}:{site}:{p.get('bulletFields', [None])[0] or external_path}",
                    )

                if len(postings) < per_page:
                    return
                offset += per_page
        except Exception as e:
            logger.debug(f"[career_pages] Workday fail ({tenant}/{site}): {e}")
            return

    def _scrape_lever(self, entry: dict) -> Generator[Job, None, None]:
        """Lever Job-Board (z.B. jobs.lever.co/<slug>).

        Public JSON-API gibt eine flache Liste von Postings zurück.
        """
        slug = entry.get("ats_slug") or entry.get("name", "").lower().replace(" ", "")
        api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            resp = self._session.get(api_url, timeout=15)
            resp.raise_for_status()
            postings = resp.json()
        except Exception as e:
            logger.debug(f"[career_pages] Lever-API fail ({slug}): {e}")
            return

        if not isinstance(postings, list):
            return

        for p in postings:
            title = (p.get("text") or "").strip()
            url = p.get("hostedUrl") or ""
            if not title or not url:
                continue
            categories = p.get("categories") or {}
            location = categories.get("location", "")
            yield Job(
                title=title,
                company=entry.get("name", ""),
                url=url,
                source=self.source,
                location=location or entry.get("location", ""),
                industry=entry.get("industry"),
                is_startup=entry.get("is_startup"),
                external_id=f"lever:{slug}:{p.get('id')}",
            )

    def _scrape_teamtailor(self, entry: dict) -> Generator[Job, None, None]:
        """Teamtailor Job-Board (z.B. careers.eurofragance.com).

        Teamtailor stellt für jedes Customer-Career-Site einen sauberen
        öffentlichen RSS-Feed unter ``/jobs.rss`` bereit. Kein Auth, kein
        Bot-Schutz — viel zuverlässiger als die HTML-Seite zu scrapen.

        ``ats_slug`` Format:
          - Vollständiger Career-Subdomain (z.B. "careers.eurofragance.com")
          - ODER: nur Firmenname für Standard-Subdomain
            (wird als "{slug}.teamtailor.com" probiert)

        RSS-Item-Struktur:
            <item>
              <title>Sales Manager, Eastern China</title>
              <link>https://careers.eurofragance.com/jobs/12345</link>
              <pubDate>...</pubDate>
              <description>HTML-Block mit Job-Beschreibung</description>
            </item>

        Discovery: Career-Page öffnen → View Source (Cmd+U) →
        ``Cmd+F`` "teamtailor". Wenn Treffer → die Career-Subdomain
        ist der Slug.
        """
        import xml.etree.ElementTree as ET
        from html import unescape

        slug = (entry.get("ats_slug") or entry.get("name", "")).strip()
        if not slug:
            return

        # Slug zur Feed-URL machen — Subdomain oder kurzer Name
        if "." in slug:
            feed_url = f"https://{slug}/jobs.rss"
        else:
            feed_url = f"https://{slug}.teamtailor.com/jobs.rss"

        try:
            resp = self._session.get(feed_url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            logger.debug(f"[career_pages] Teamtailor-Feed fail ({slug}): {e}")
            return

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            logger.debug(f"[career_pages] Teamtailor-RSS-Parse fail ({slug}): {e}")
            return

        company_name = entry.get("name", "")
        # RSS-Items: channel/item
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            if not title or not url:
                continue

            # Description ist HTML — kürze auf 2000 chars, strippe Tags grob
            raw_desc = item.findtext("description") or ""
            desc = unescape(raw_desc)
            # Sehr grober HTML-Strip — für ML-Scoring reicht's
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()[:2000]

            # Teamtailor RSS-Items haben oft kein direktes Location-Feld;
            # nutze die location aus der YAML als Default.
            location = entry.get("location", "")

            # external_id aus der URL ziehen (numerische ID am Ende)
            id_match = re.search(r"/jobs?/(\d+)", url)
            ext_id = f"tt:{slug}:{id_match.group(1)}" if id_match else f"tt:{slug}:{hash(url) & 0xffff}"

            yield Job(
                title=title,
                company=company_name,
                url=url,
                source=self.source,
                location=location,
                description=desc or None,
                industry=entry.get("industry"),
                is_startup=entry.get("is_startup"),
                external_id=ext_id,
            )

    def _scrape_ashby(self, entry: dict) -> Generator[Job, None, None]:
        """Ashby Job-Board (z.B. jobs.ashbyhq.com/<slug>).

        Ashby ist ein neueres ATS (ab ~2020), verbreitet bei Tech-Startups.
        Bietet einen sauberen dokumentierten Public-API-Endpoint, der die
        Job-Liste direkt als JSON zurückgibt.

        Endpoint:  https://api.ashbyhq.com/posting-api/job-board/{slug}
        Slug:      identisch mit dem URL-Path-Segment auf jobs.ashbyhq.com

        Response-Felder pro Job:
          id, title, location, employmentType, jobUrl, department,
          publishedAt, descriptionHtml, isRemote, address.{city,region,country}
        """
        slug = (entry.get("ats_slug") or entry.get("name", "").lower().replace(" ", "")).strip()
        if not slug:
            return
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            resp = self._session.get(api_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.debug(f"[career_pages] Ashby-API fail ({slug}): {e}")
            return

        company_name = entry.get("name", "")
        for job in data.get("jobs") or []:
            title = (job.get("title") or "").strip()
            url = job.get("jobUrl") or ""
            if not title or not url:
                continue

            # Location: Ashby gibt entweder einen flachen String oder ein
            # address-Objekt — wir nehmen was da ist.
            location = job.get("location") or ""
            if not location and (addr := job.get("address")):
                parts = [addr.get("city"), addr.get("region"), addr.get("country")]
                location = ", ".join(p for p in parts if p)

            yield Job(
                title=title,
                company=company_name,
                url=url,
                source=self.source,
                location=location or entry.get("location", ""),
                is_remote=job.get("isRemote"),
                industry=entry.get("industry"),
                is_startup=entry.get("is_startup"),
                external_id=f"ashby:{slug}:{job.get('id')}",
            )
