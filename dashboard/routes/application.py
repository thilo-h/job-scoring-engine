"""Per-job assistance routes — briefing, triage, apply-method, letter review.

Everything here supports a decision the applicant makes; nothing here writes
a letter or sends anything.

Endpoints:
    POST /job/{id}/cover-letter/save     → save the applicant's edited markdown
                                            and return the deterministic findings
    GET  /job/{id}/cover-letter/download → render DOCX or PDF for offline editing
    POST /job/{id}/briefing/generate     → company / role / fit summaries
    POST /job/{id}/triage                → holistic fit score with reasons
    POST /job/{id}/detect-apply-method   → how to apply, which documents
    POST /job/{id}/contact-email         → save a contact address for tracking
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from dashboard.deps import (
    current_profile,
    db,
    get_config,
    templates,
)
from src.agent.apply_method import ApplyMethodDetector
from src.agent.briefing import BriefingGenerator
from src.agent.docx_generator import render_cover_letter, temp_render_dir
from src.agent.triage import JobTriager

logger = logging.getLogger(__name__)
router = APIRouter()

# Lazy singletons — keyed by profile name so a switch in the dropdown
# loads the right CV/sender/background without re-reading on every request.
_triagers: dict[str, JobTriager] = {}
_briefers: dict[str, BriefingGenerator] = {}


def _briefer() -> BriefingGenerator:
    profile = current_profile.get()
    if profile not in _briefers:
        _briefers[profile] = BriefingGenerator(config=get_config())
    return _briefers[profile]
_apply_detectors: dict[str, ApplyMethodDetector] = {}


def _trg() -> JobTriager:
    profile = current_profile.get()
    if profile not in _triagers:
        _triagers[profile] = JobTriager(config=get_config())
    return _triagers[profile]


def _apm() -> ApplyMethodDetector:
    profile = current_profile.get()
    if profile not in _apply_detectors:
        _apply_detectors[profile] = ApplyMethodDetector()
    return _apply_detectors[profile]


def _finish(request: Request, job_id: int, step: str, persist, **ctx) -> Response:
    """Persist an agent result and re-render the application section.

    Each agent route guards its own LLM call, but everything after it — the
    DB write, the refresh read, the template render — used to run unguarded.
    Any failure there escaped as a bare "Internal Server Error" even though
    the (already billed) completion had succeeded, which makes a local
    storage problem look like an LLM outage. Guard that half too and say
    which one actually broke.
    """
    try:
        persist()
        job = db.get_job(job_id)
        return templates.TemplateResponse(
            request, "partials/application_section.html",
            {"job": job, "result": None, **ctx},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s: persisting the result failed", step)
        return HTMLResponse(
            f'<div class="error">{step} succeeded, but saving it failed: {exc}</div>',
            status_code=500,
        )


@router.post("/job/{job_id}/cover-letter/save", response_class=HTMLResponse)
def save_cover_letter(
    job_id: int,
    cover_letter: str = Form(""),
):
    """Persist the applicant's edited markdown and return the fresh findings.

    The deterministic review runs on every save, so a finding cannot be made to
    disappear by editing around it. A failure in the review never blocks the
    save — losing the text would be far worse than losing the findings.
    """
    import json as _json

    from src.agent.docx_generator import detect_language
    from src.agent.letter_review import parse_markdown_to_structure, review_markdown

    job = db.get_job(job_id) or {}
    # Sprache aus dem gespeicherten Text selbst, nicht aus einem früheren
    # Stand — sonst prüft und rendert ein umgeschriebener Brief in der
    # falschen Sprache.
    language = detect_language(parse_markdown_to_structure(cover_letter), default=job.get("cover_letter_lang"))
    job = {**job, "cover_letter_lang": language}
    try:
        findings = review_markdown(cover_letter, job=job, config=get_config(),
                                   channel=(db.application_for_job(job_id) or {}).get("channel"))
    except Exception:  # Prüfung darf das Speichern nie blockieren
        logger.exception("Prüfung beim Speichern fehlgeschlagen")
        findings = []
    db.save_cover_letter(
        job_id,
        markdown=cover_letter,
        language=language,
        checks=_json.dumps(findings, ensure_ascii=False),
    )
    checks_html = templates.get_template("partials/letter_checks.html").render(
        {"job": {"id": job_id}, "checks": findings, "oob": True})
    return HTMLResponse(
        f'<span class="notes-saved">Saved {datetime.now().strftime("%H:%M:%S")}</span>' + checks_html
    )


@router.post("/job/{job_id}/detect-apply-method", response_class=HTMLResponse)
def detect_apply_method(request: Request, job_id: int):
    """Run apply-method detection (Haiku + web_search) and refresh the section."""
    import json as _json

    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        result = _apm().detect(
            title=job["title"],
            company=job["company"],
            location=job.get("location") or "",
            description=job.get("description") or "",
            url=job.get("url") or "",
        )
    except Exception as exc:
        logger.exception("Apply-method detection failed")
        return HTMLResponse(
            f'<div class="error">Detection failed: {exc}</div>',
            status_code=502,
        )

    def _save():
        db.save_apply_method(
            job_id, apply_method_json=_json.dumps(result.to_dict(), ensure_ascii=False)
        )
        # If a recipient email was found and the job doesn't have one yet, save it
        if result.email and not job.get("contact_email"):
            db.update_email_fields(job_id, contact_email=result.email)

    return _finish(request, job_id, "Detection", _save)


@router.post("/job/{job_id}/briefing/generate", response_class=HTMLResponse)
def generate_briefing(request: Request, job_id: int):
    """Generate JUST the Company / Role / Profile-fit briefing — without
    the full cover letter or the fit-check triage. Lightweight Haiku call,
    typically ~$0.001-0.003 + ~3-5s. Refreshes the application section."""
    import json as _json

    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        result = _briefer().generate(
            job_title=job["title"],
            company=job["company"],
            location=job.get("location") or "",
            description=job.get("description") or "",
            url=job.get("url") or "",
        )
    except Exception as exc:
        logger.exception("Briefing generation failed")
        return HTMLResponse(
            f'<div class="error">Briefing failed: {exc}</div>',
            status_code=502,
        )

    def _save():
        db.save_briefing(
            job_id,
            briefing_json=_json.dumps(result.to_dict(), ensure_ascii=False),
        )

    return _finish(request, job_id, "Briefing", _save)


@router.post("/job/{job_id}/triage", response_class=HTMLResponse)
def triage_job(request: Request, job_id: int):
    """Run job-quality triage (Haiku) and refresh the application section."""
    import json as _json

    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        result = _trg().triage(
            title=job["title"],
            company=job["company"],
            location=job.get("location") or "",
            description=job.get("description") or "",
        )
    except Exception as exc:
        logger.exception("Triage failed")
        return HTMLResponse(
            f'<div class="error">Triage failed: {exc}</div>',
            status_code=502,
        )

    def _save():
        details_json = _json.dumps(
            {"strengths": result.strengths, "gaps": result.gaps},
            ensure_ascii=False,
        )
        db.save_triage(
            job_id, score=result.score, reason=result.reason, details=details_json
        )

    return _finish(request, job_id, "Triage", _save)


@router.post("/job/{job_id}/contact-email", response_class=HTMLResponse)
def save_contact_email(job_id: int, contact_email: str = Form("")):
    db.update_email_fields(job_id, contact_email=contact_email.strip() or None)
    return HTMLResponse(
        f'<span class="notes-saved">Saved {datetime.now().strftime("%H:%M:%S")}</span>'
    )


@router.get("/job/{job_id}/cover-letter/download")
def download_cover_letter(job_id: int, format: str = "docx"):
    """Download the cover letter as DOCX or PDF for further editing offline.

    Re-renders from the markdown stored in ``jobs.cover_letter`` each call,
    so the latest saved version always wins. Filename: CoverLetter_<Company>_<Title>.<ext>

    Seit 2026-09-15 ohne Spuren: nur das angeklickte Format (Word startet kein
    LibreOffice mehr), gerendert in ein Temp-Verzeichnis, das nach dem
    Ausliefern gelöscht wird. Vorher blieb pro Klick ein Word+PDF-Paar in
    data/cover_letters/ liegen.
    """
    if format not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail="format must be 'docx' or 'pdf'")

    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("cover_letter"):
        raise HTTPException(status_code=404, detail="No cover letter generated yet")

    today_iso = date.today().isoformat()
    render_dir = temp_render_dir("download")
    try:
        docx_path, pdf_path = render_cover_letter(
            markdown=job["cover_letter"],
            company=job["company"],
            job_title=job["title"],
            job_id=job_id,
            today_iso=today_iso,
            output_dir=render_dir,
            with_pdf=(format == "pdf"),
            config=get_config(),
            language=job.get("cover_letter_lang"),
        )
    except Exception as exc:
        shutil.rmtree(render_dir, ignore_errors=True)
        logger.exception("Cover-letter render failed for download")
        raise HTTPException(status_code=500, detail=f"Render failed: {exc}") from exc

    path = pdf_path if format == "pdf" else docx_path
    media = (
        "application/pdf" if format == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # Pretty filename the user will see in their Downloads folder.
    # Short on purpose: "<Name>_CoverLetter_<Company>" — the role title is
    # redundant for the recruiter and blows up the filename.
    profile_name = ((get_config().get("profile") or {}).get("name", "") or "").replace(" ", "")
    safe = "".join(
        c if c.isalnum() or c in "-_." else "_"
        for c in f"{profile_name}_CoverLetter_{job['company']}"
    )[:80].rstrip("_")
    filename = f"{safe}.{format}"
    return FileResponse(
        path, media_type=media, filename=filename,
        background=BackgroundTask(shutil.rmtree, render_dir, ignore_errors=True),
    )
