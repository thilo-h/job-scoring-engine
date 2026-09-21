"""Stats tab — charts via chart.js (CDN)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.deps import db, templates

router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return templates.TemplateResponse(
        request,
        "stats.html",
        {"active_tab": "stats", "stats": db.stats_for_dashboard()},
    )


@router.get("/api/stats")
def stats_json():
    return JSONResponse(db.stats_for_dashboard())
