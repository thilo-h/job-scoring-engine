"""DB tab — read-only schema explorer + SQL playground + audit log."""

from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from dashboard.deps import db, templates

router = APIRouter()

# Whitelist of tables we allow describing (extra safety, matches our schema)
_ALLOWED_TABLES = {"jobs", "scrape_runs", "search_queries", "audit_log"}


@router.get("/db", response_class=HTMLResponse)
def db_index(request: Request, table: Optional[str] = None):
    tables = db.list_tables()
    selected = table if table in _ALLOWED_TABLES else (tables[0]["name"] if tables else None)
    schema = db.describe_table(selected) if selected else []
    sample_sql = (
        f"SELECT * FROM {selected} LIMIT 10" if selected else "SELECT 1"
    )
    return templates.TemplateResponse(
        request,
        "db.html",
        {
            "active_tab": "db",
            "tables": tables,
            "selected": selected,
            "schema": schema,
            "audit": db.recent_audit(limit=30),
            "sample_sql": sample_sql,
            "result": None,
            "error": None,
        },
    )


@router.post("/db/query", response_class=HTMLResponse)
def db_query(request: Request, sql: str = Form(...)):
    try:
        result = db.execute_readonly_sql(sql, max_rows=200)
        error = None
    except Exception as exc:  # noqa: BLE001 — surface to user
        result = None
        error = str(exc)
    # Always re-render the same page with results in place
    return templates.TemplateResponse(
        request,
        "partials/db_result.html",
        {"result": result, "error": error, "submitted_sql": sql},
    )
