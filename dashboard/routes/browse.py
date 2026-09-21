"""Browse tab — dense filter sidebar + table + status buttons."""

import logging
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.deps import current_profile, db, get_config, templates
from src.agent.smart_filter import SmartFilter

logger = logging.getLogger(__name__)
router = APIRouter()

# Lazy per-profile smart-filter agents. Mirror of the application.py pattern.
_smart_filters: dict[str, SmartFilter] = {}


def _smart_filter_agent() -> SmartFilter:
    profile = current_profile.get()
    if profile not in _smart_filters:
        _smart_filters[profile] = SmartFilter(config=get_config())
    return _smart_filters[profile]

PAGE_SIZE = 50

VALID_SORTS = {
    "score", "date", "company", "title",
    "location", "workload", "source", "last_seen", "status",
}


def _opt_float(value: Optional[str]) -> Optional[float]:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _opt_int(value: Optional[str]) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _opt_str(value: Optional[str]) -> Optional[str]:
    if value is None or value.strip() == "":
        return None
    return value


@router.get("/browse", response_class=HTMLResponse)
def browse(
    request: Request,
    q: Optional[str] = None,
    sources: list[str] = Query(default=[]),
    statuses: list[str] = Query(default=[]),
    location: Optional[str] = None,
    # Off by default; the checkbox only sends "1" when checked.
    presence_ch_es: Optional[str] = None,
    # "", "startup", "scaleup", "startup_scaleup" — maps to company_profiles categories.
    company_type: Optional[str] = None,
    # One of db.COMPANY_INDUSTRIES or "" = any.
    industry: Optional[str] = None,
    # Numeric filters arrive as strings — empty inputs send "" which would
    # fail Pydantic's float/int coercion (422). Parse manually below.
    min_score: Optional[str] = None,
    workload_min: Optional[str] = None,
    workload_max: Optional[str] = None,
    exp_band: Optional[str] = None,
    remote: Optional[str] = None,
    # Hidden field always sends "0"; checkbox sends "1" when checked. Last
    # value wins, so we get "1" iff checkbox is checked.
    hide_ignored: list[str] = Query(default=["1"]),
    hide_stale: list[str] = Query(default=["1"]),
    sort: str = "score",
    direction: str = "desc",
    page: int = 1,
):
    """Render the browse table. With HX-Request header, returns only the table partial."""
    is_remote = {"yes": True, "no": False}.get(remote or "")
    src_list = sources or None
    status_list = statuses or None
    hide_ign = (hide_ignored[-1] if hide_ignored else "0") == "1"
    hide_stl = (hide_stale[-1] if hide_stale else "0") == "1"
    exp_band_clean = exp_band if exp_band in {"0-1", "1-3", "3-5", "5plus", "unknown"} else None
    if sort not in VALID_SORTS:
        sort = "score"
    if direction not in {"asc", "desc"}:
        direction = "desc"

    min_score_f = _opt_float(min_score)
    workload_min_i = _opt_int(workload_min)
    workload_max_i = _opt_int(workload_max)
    location_clean = _opt_str(location)
    q_clean = _opt_str(q)
    presence_ch_es_b = presence_ch_es == "1"
    company_type_map = {
        "startup": ["startup"],
        "scaleup": ["scaleup"],
        "startup_scaleup": ["startup", "scaleup"],
    }
    company_types = company_type_map.get(company_type or "")
    company_type_clean = company_type if company_types else ""
    industry_clean = industry if industry in db.COMPANY_INDUSTRIES else ""
    industries = [industry_clean] if industry_clean else None

    filters = dict(
        search=q_clean,
        sources=src_list,
        statuses=status_list,
        location=location_clean,
        presence_ch_es=presence_ch_es_b,
        company_types=company_types,
        industries=industries,
        min_score=min_score_f,
        workload_min=workload_min_i,
        workload_max=workload_max_i,
        experience_band=exp_band_clean,
        is_remote=is_remote,
        hide_ignored=hide_ign and not status_list,
        hide_stale=hide_stl and not status_list,
    )

    jobs = db.query_jobs(
        **filters,
        sort=sort,
        direction=direction,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    total = db.count_jobs(
        search=q_clean,
        sources=src_list,
        statuses=status_list,
        location=location_clean,
        presence_ch_es=presence_ch_es_b,
        company_types=company_types,
        industries=industries,
        min_score=min_score_f,
        workload_min=workload_min_i,
        workload_max=workload_max_i,
        experience_band=exp_band_clean,
        is_remote=is_remote,
        hide_ignored=hide_ign and not status_list,
        hide_stale=hide_stl and not status_list,
    )

    ctx = {
        "request": request,
        "active_tab": "browse",
        "jobs": jobs,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "page_count": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        "all_sources": db.distinct_sources(),
        "selected_sources": src_list or [],
        "selected_statuses": status_list or [],
        "q": q_clean or "",
        "location": location_clean or "",
        "presence_ch_es": presence_ch_es_b,
        "company_type": company_type_clean,
        "industry": industry_clean,
        "all_industries": db.COMPANY_INDUSTRIES,
        "min_score": min_score_f,
        "workload_min": workload_min_i,
        "workload_max": workload_max_i,
        "exp_band": exp_band_clean or "",
        "remote": remote or "",
        "hide_ignored": hide_ign,
        "hide_stale": hide_stl,
        "stale_days": 30,
        "sort": sort,
        "direction": direction,
        "status_counts": db.status_counts(),
    }

    template = "partials/browse_table.html" if request.headers.get("HX-Request") else "browse.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    job = db.get_job(job_id)
    if not job:
        return HTMLResponse("<p>Job not found</p>", status_code=404)
    return templates.TemplateResponse(
        request, "partials/job_detail.html", {"job": job}
    )


@router.get("/job/{job_id}/score-breakdown", response_class=HTMLResponse)
def score_breakdown(request: Request, job_id: int):
    """Recompute and render the deterministic relevance_score component-by-component.

    Useful when the score disagrees with the LLM match_score — shows exactly
    which factors contributed how much, so the user can spot misweighting
    (e.g. company_tier 1.0 dragging up an otherwise weak match).
    """
    from src.models import job_from_row
    from src.scoring import JobScorer

    job_dict = db.get_job(job_id)
    if not job_dict:
        return HTMLResponse("<p>Job not found</p>", status_code=404)
    cfg = get_config()
    # Gleicher Nachbau wie der Rescore — sonst erklärt die Tabelle eine andere
    # Zahl als die, die in der Liste steht (vorher fehlte min_years_experience).
    job_obj = job_from_row(job_dict)
    breakdown = JobScorer(cfg).score_breakdown(job_obj)

    # Sort contributions by absolute impact for the UI table.
    contribs = sorted(
        breakdown["weighted_scores"].items(),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )
    weights = cfg["preferences"].get("weights", {})

    return templates.TemplateResponse(
        request, "partials/score_breakdown.html",
        {
            "job": job_dict,
            "bd": breakdown,
            "contribs": contribs,
            "weights": weights,
        },
    )


@router.get("/job/{job_id}/radar", response_class=HTMLResponse)
def job_radar(request: Request, job_id: int):
    """FIFA-Karte mit Nonagon im Drawer — dieselbe Grafik wie scripts/job_radar.py."""
    from src.models import job_from_row
    from src.radar import TEAM_NOTE, build_radar
    from src.scoring import JobScorer

    job = db.get_job(job_id)
    if not job:
        return HTMLResponse("<p>Job not found</p>", status_code=404)
    radar = build_radar(JobScorer(get_config()), job_from_row(job))
    return templates.TemplateResponse(
        request, "partials/job_radar.html",
        {"job": job, "radar": radar, "team_note": TEAM_NOTE},
    )


@router.post("/smart-filter")
def smart_filter(prompt: str = Form(...)):
    """Translate a natural-language query into structured filter params.

    Returns JSON the client applies directly to the filter form. The
    /browse re-render then happens via the existing HTMX flow.
    """
    prompt_clean = (prompt or "").strip()
    if not prompt_clean:
        return JSONResponse({"ok": False, "error": "Empty prompt"}, status_code=400)
    try:
        result = _smart_filter_agent().interpret(
            prompt_clean,
            available_sources=db.distinct_sources(),
        )
    except Exception as exc:
        logger.exception("Smart filter failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, **result.to_dict(), "cost_usd": result.cost_usd})
