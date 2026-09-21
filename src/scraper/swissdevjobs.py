"""
Scraper für swissdevjobs.ch — über deren öffentliche JSON-API.

Die Webseite ist eine React-SPA, aber sie liefert ihre Daten über eine
preload-API: /api/jobsLight (return: alle ~225 Jobs in einem Schwung).

Das ist viel sauberer als HTML-Scraping:
- Strukturierte Felder (company, salary, exp_level, technologies, …)
- Eine einzige Anfrage statt N Detail-Pages
- Robust gegen UI-Redesigns

Detail-URL: swissdevjobs.ch/jobs/<jobUrl-Slug>
Apply-URL:  redirectJobUrl (führt direkt zum Bewerbungsformular)
"""

import logging
from datetime import datetime
from typing import Generator

from src.models import Job, JobType, SourcePortal
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import PoliteSession, RateLimiter

logger = logging.getLogger(__name__)

API_URL = "https://swissdevjobs.ch/api/jobsLight"
DETAIL_URL_TEMPLATE = "https://swissdevjobs.ch/jobs/{slug}"
ACCEPT = "application/json"

# Mapping swissdevjobs jobType → unsere JobType Enum
JOB_TYPE_MAP: dict[str, JobType] = {
    "fulltime":        JobType.FULL_TIME,
    "parttime":        JobType.PART_TIME,
    "internship":      JobType.INTERNSHIP,
    "contract":        JobType.CONTRACT,
    "workingstudent":  JobType.WORKING_STUDENT,
}


class SwissDevJobsScraper(BaseScraper):
    """
    Scraped swissdevjobs.ch via deren JSON-API (alle Jobs auf einmal).
    Filtert dann clientseitig nach den konfigurierten Locations.
    """

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self._rate_limiter = rate_limiter or RateLimiter()
        # PoliteSession enforces robots.txt, sends the honest UA and carries
        # certifi's CA bundle (macOS Python ships without the system roots).
        self._session = PoliteSession(self._rate_limiter, ACCEPT)

    @property
    def source(self) -> SourcePortal:
        return SourcePortal.SWISSDEVJOBS

    @property
    def name(self) -> str:
        return "swissdevjobs.ch"

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def scrape(self, search_config: dict) -> Generator[Job, None, None]:
        """Holt alle Jobs einmalig und filtert nach Location."""
        target_cities = self._collect_swiss_cities(search_config)

        logger.info(f"[swissdevjobs] Fetching JSON API… (Filter: {target_cities or 'alle'})")
        try:
            resp = self._session.get(API_URL, timeout=20)
            resp.raise_for_status()
            jobs_data = resp.json()
        except Exception as e:
            logger.error(f"[swissdevjobs] API-Fehler: {e}")
            return

        if not isinstance(jobs_data, list):
            logger.error(f"[swissdevjobs] Unerwartetes API-Format: {type(jobs_data).__name__}")
            return

        logger.info(f"[swissdevjobs] {len(jobs_data)} Jobs in API-Response")

        count = 0
        for raw in jobs_data:
            if not isinstance(raw, dict):
                continue

            # Filter: nur Jobs in target_cities (oder alle wenn leer)
            city = (raw.get("cityCategory") or raw.get("actualCity") or "").lower()
            if target_cities and not any(tc in city for tc in target_cities):
                continue

            job = self._raw_to_job(raw)
            if job:
                yield job
                count += 1

        logger.info(f"[swissdevjobs] {count} Jobs nach Location-Filter geyielded")

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _collect_swiss_cities(search_config: dict) -> list[str]:
        """Sammelt alle Swiss cities aus location + locations_secondary."""
        swiss_cities = {
            "zurich", "zürich", "bern", "basel", "geneva", "genf",
            "lausanne", "luzern", "lucerne", "st. gallen", "st-gallen",
            "zug", "chur", "winterthur", "lugano", "neuchatel",
        }
        cities: list[str] = []
        primary = (search_config.get("location") or "").lower()
        if primary in swiss_cities:
            cities.append(primary.replace("ü", "u"))  # API nutzt "Zurich"

        secondary_list = search_config.get("locations_secondary") or []
        if isinstance(secondary_list, str):
            secondary_list = [secondary_list]
        legacy = search_config.get("location_secondary")
        if legacy:
            secondary_list.append(legacy)

        for sec in secondary_list:
            sl = sec.lower()
            if sl in swiss_cities:
                norm = sl.replace("ü", "u")
                if norm not in cities:
                    cities.append(norm)

        return cities

    def _raw_to_job(self, raw: dict) -> Job | None:
        """Mapped einen API-Job auf unser Job-Modell."""
        try:
            slug    = raw.get("jobUrl") or ""
            title   = (raw.get("name") or "").strip()
            company = (raw.get("company") or "").strip()

            if not title or not slug:
                return None

            url = DETAIL_URL_TEMPLATE.format(slug=slug)

            # Ort: cityCategory ist sauberer ("Zurich") als actualCity ("Fahrweid (Zürich)")
            location = raw.get("cityCategory") or raw.get("actualCity") or "Switzerland"

            # Job-Typ
            jt_raw = (raw.get("jobType") or "").lower().replace("-", "").replace(" ", "")
            job_type = JOB_TYPE_MAP.get(jt_raw, JobType.UNKNOWN)

            # Workplace: "remote" / "hybrid" / "office"
            workplace = (raw.get("workplace") or "").lower()
            is_remote = True if workplace == "remote" else (False if workplace == "office" else None)

            # Description als concat aus relevanten Feldern, da die API keine
            # Volltext-Beschreibung mitliefert (die ist nur auf der Detail-Seite)
            tech_list = raw.get("technologies") or []
            tags_list = raw.get("filterTags") or []
            perks     = raw.get("perkKeys") or []
            description_parts = [
                f"Tech: {', '.join(tech_list)}" if tech_list else "",
                f"Tags: {', '.join(tags_list)}" if tags_list else "",
                f"Perks: {', '.join(perks)}" if perks else "",
                f"Workplace: {workplace}" if workplace else "",
                f"Experience: {raw.get('expLevel') or 'n/a'}",
                f"Company size: {raw.get('companySize') or 'n/a'}",
            ]
            description = " | ".join(p for p in description_parts if p)

            # Salary
            salary_min = _safe_int(raw.get("annualSalaryFrom"))
            salary_max = _safe_int(raw.get("annualSalaryTo"))

            # Datum
            date_posted = None
            af = raw.get("activeFrom")
            if af:
                try:
                    # ISO-Format mit Timezone: 2026-04-28T00:00:00.000+02:00
                    date_posted = datetime.fromisoformat(af.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            # Sprachen
            lang_raw = raw.get("language") or []
            languages = [lang.upper() for lang in lang_raw if isinstance(lang, str)]

            return Job(
                title=title,
                company=company,
                url=url,
                source=self.source,
                location=location,
                is_remote=is_remote,
                description=description,
                job_type=job_type,
                salary_min=salary_min,
                salary_max=salary_max,
                salary_currency="CHF",
                company_size=raw.get("companySize"),
                date_posted=date_posted,
                external_id=raw.get("_id"),
                languages=languages,
            )
        except Exception as e:
            logger.debug(f"[swissdevjobs] Mapping fehlgeschlagen: {e}")
            return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
