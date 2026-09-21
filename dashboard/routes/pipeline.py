"""Pipeline tab — Kanban over bookmarked / applied / interview / offer / rejected."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard.deps import PIPELINE_STATUSES, db, templates

router = APIRouter()


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
    columns = db.jobs_by_status(PIPELINE_STATUSES)
    template = (
        "partials/kanban_columns.html"
        if request.headers.get("HX-Request")
        else "pipeline.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_tab": "pipeline",
            "columns": columns,
            "statuses": PIPELINE_STATUSES,
        },
    )
