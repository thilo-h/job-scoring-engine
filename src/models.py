"""Core data models for the Job Finder."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional


class JobType(Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    INTERNSHIP = "internship"
    WORKING_STUDENT = "working_student"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


class ApplicationStatus(Enum):
    NEW = "new"
    BOOKMARKED = "bookmarked"
    APPLIED = "applied"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    OFFER = "offer"
    IGNORED = "ignored"
    ARCHIVED = "archived"   # Retention: Score < 0.5, 60 Tage unangetastet


class SourcePortal(Enum):
    # Switzerland
    JOBS_CH = "jobs.ch"
    STARTUPTICKER = "startupticker.ch"
    SWISSDEVJOBS = "swissdevjobs.ch"
    # Spain
    TECNOEMPLEO = "tecnoempleo.com"
    BARCELONAJOBS = "barcelonajobs.com"
    # Curated company career pages (cross-country)
    CAREER_PAGE = "career_page"
    # Added by hand through the dashboard's manual-add modal
    MANUAL = "manual"


@dataclass
class Job:
    """Unified job representation across all portals."""

    # Identity
    title: str
    company: str
    url: str
    source: SourcePortal

    # Location
    location: str = ""
    canton: Optional[str] = None
    is_remote: Optional[bool] = None

    # Details
    description: Optional[str] = None
    job_type: JobType = JobType.UNKNOWN
    workload_percent: Optional[int] = None  # Swiss Pensum: 80, 100, etc.
    min_years_experience: Optional[int] = None  # Extracted from posting text
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: str = "CHF"

    # Company info
    company_size: Optional[str] = None
    industry: Optional[str] = None
    is_startup: Optional[bool] = None

    # Metadata
    date_posted: Optional[datetime] = None
    date_scraped: datetime = field(default_factory=datetime.now)
    external_id: Optional[str] = None

    # Scoring (filled by scoring engine)
    relevance_score: Optional[float] = None

    # Application tracking (Phase 2)
    application_status: ApplicationStatus = ApplicationStatus.NEW

    # Languages mentioned in posting
    languages: list[str] = field(default_factory=list)

    def dedup_key(self) -> str:
        """Generate a key for cross-portal deduplication."""
        normalized_title = self.title.lower().strip()
        normalized_company = self.company.lower().strip()
        return f"{normalized_title}|{normalized_company}|{self.location.lower().strip()}"


def job_from_row(row: Mapping[str, Any]) -> Job:
    """Job aus einer DB-Zeile — mit allen Feldern, die der Scorer liest.

    Vorher gab es drei Nachbauten mit drei Ergebnissen: der Rescore liess
    workload_percent und is_remote weg (die 80-%-Präferenz wirkte nach jedem
    nächtlichen Rescore nicht mehr), die Score-Aufschlüsselung im Drawer liess
    min_years_experience weg (die Erfahrungs-Penalty fehlte in der Anzeige).
    Wer einen gespeicherten Score erklären oder neu rechnen will, nimmt diesen.
    """
    keys = row.keys() if hasattr(row, "keys") else ()

    def get(name: str) -> Any:
        return row[name] if name in keys else None

    is_remote = get("is_remote")
    return Job(
        title=get("title") or "",
        company=get("company") or "",
        url=get("url") or "",
        source=SourcePortal.MANUAL,          # der Scorer liest die Quelle nicht
        location=get("location") or "",
        description=get("description") or "",
        workload_percent=get("workload_percent"),
        is_remote=bool(is_remote) if is_remote is not None else None,
        min_years_experience=get("min_years_experience"),
    )
