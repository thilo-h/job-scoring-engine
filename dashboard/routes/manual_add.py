"""Manual job entry — add a job by URL with optional Haiku auto-extract.

Two endpoints:
    POST /jobs/extract-url  → fetch URL, Haiku extracts, return JSON to fill form
    POST /jobs/manual-add   → persist a Job row to the active profile's DB
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from dashboard.deps import db, get_config
from src.agent.url_extractor import extract_from_url
from src.models import ApplicationStatus, Job, JobType, SourcePortal
from src.scoring import JobScorer

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/jobs/extract-url")
def extract_url_endpoint(url: str = Form(...)):
    """Fetch URL + run Haiku extraction. Returns JSON the client fills the form with."""
    url_clean = (url or "").strip()
    if not url_clean.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "URL must start with http(s)://"}, status_code=400)
    result = extract_from_url(url_clean)
    return JSONResponse(result)


@router.post("/jobs/manual-add")
def manual_add(
    url: str = Form(...),
    title: str = Form(...),
    company: str = Form(...),
    location: str = Form(""),
    description: str = Form(""),
    is_remote: str = Form(""),                # "yes" / "no" / ""
    workload_percent: str = Form(""),
    job_type: str = Form(""),                  # full_time / part_time / ...
    languages: str = Form(""),                 # comma-separated
    application_status: str = Form("bookmarked"),  # default: skip "new" so it shows up
):
    """Persist a manually-added Job row using the same upsert path as scrapers."""
    url_clean = url.strip()
    title_clean = title.strip()
    company_clean = company.strip()
    if not url_clean or not title_clean or not company_clean:
        raise HTTPException(status_code=400, detail="url, title, company are required")

    # Coerce optional fields
    is_remote_b: Optional[bool] = None
    if is_remote == "yes":
        is_remote_b = True
    elif is_remote == "no":
        is_remote_b = False

    workload_i: Optional[int] = None
    if workload_percent.strip():
        try:
            workload_i = int(workload_percent)
        except ValueError:
            workload_i = None

    try:
        job_type_e = JobType(job_type) if job_type else JobType.UNKNOWN
    except ValueError:
        job_type_e = JobType.UNKNOWN

    try:
        status_e = ApplicationStatus(application_status)
    except ValueError:
        status_e = ApplicationStatus.BOOKMARKED

    languages_list = [s.strip() for s in languages.split(",") if s.strip()] if languages else []

    job = Job(
        title=title_clean,
        company=company_clean,
        url=url_clean,
        source=SourcePortal.MANUAL,
        location=location.strip(),
        description=description.strip() or None,
        is_remote=is_remote_b,
        workload_percent=workload_i,
        job_type=job_type_e,
        languages=languages_list,
        application_status=status_e,
        date_posted=None,
        date_scraped=datetime.now(),
        external_id=url_clean,
    )

    # Score with the active profile's config so it shows up correctly in Browse.
    cfg = get_config()
    try:
        job.relevance_score = JobScorer(cfg).score(job)
    except Exception as exc:
        logger.warning("Scoring manually-added job failed: %s", exc)
        job.relevance_score = None

    is_new = db.upsert_job(job)
    # The status passed via form is meant to override the default 'new' that
    # upsert_job assigns on insert. Force it via a follow-up update.
    if status_e != ApplicationStatus.NEW:
        db.conn.execute(
            "UPDATE jobs SET application_status = ? WHERE url = ?",
            (status_e.value, url_clean),
        )
        db.conn.commit()

    # Look up the inserted/updated row to return its id (for "open in drawer")
    row = db.conn.execute("SELECT id FROM jobs WHERE url = ?", (url_clean,)).fetchone()
    job_id = row["id"] if row else None

    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "is_new": is_new,
        "score": job.relevance_score,
        "status": status_e.value,
    })
