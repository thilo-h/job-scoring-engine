"""POST endpoints: status updates, notes, bulk ignore, scrape trigger."""

import asyncio
import logging
import re
import sys
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.deps import PROJECT_ROOT, current_profile, db, templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _cli_python() -> str:
    """Interpreter for CLI subprocesses (`python -m src.main ...`).

    The server itself only needs fastapi/jinja, so it may well be started
    with a system Python that lacks the CLI-only deps (rich, …). Prefer
    the project venv — that's the one `pyproject.toml` is installed into and
    the one `scripts/nightly_maintenance.sh` uses.
    """
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


# Single in-process flag tracking the current scrape run.
# `per_source` is filled as the scraper outputs "<source>: N found, M new" lines.
_scrape_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "log_tail": "",
    "profile": None,
    "current_scraper": None,   # whichever scraper is mid-loop right now
    "per_source": {},           # {"linkedin": {"found": N, "new": M}, ...}
    "total_found": 0,
    "total_new": 0,
}

@router.post("/job/{job_id}/status", response_class=HTMLResponse)
def set_status(request: Request, job_id: int, status: str = Form(...)):
    db.update_status(job_id, status)
    job = db.get_job(job_id)
    if not job:
        return HTMLResponse("<p>Job not found</p>", status_code=404)
    # If the request came from the kanban, return a card; otherwise a row.
    template = (
        "partials/kanban_card.html"
        if request.headers.get("HX-Target", "").startswith("col-")
        else "partials/job_row.html"
    )
    row_html = templates.get_template(template).render({"job": job, "request": request})

    # OOB-swap snippets so the Browse sidebar's status-count badges
    # refresh themselves whenever a status changes. The browse template
    # gives each count <span> an id of "status-count-<name>".
    counts = db.status_counts()
    oob_html = "".join(
        f'<span id="status-count-{name}" class="muted small" hx-swap-oob="true">({counts.get(name, 0)})</span>'
        for name in counts
    )
    return HTMLResponse(row_html + oob_html)


@router.post("/job/{job_id}/notes", response_class=HTMLResponse)
def save_notes(request: Request, job_id: int, notes: str = Form("")):
    db.update_notes(job_id, notes)
    return HTMLResponse(
        f'<span class="notes-saved">Saved {datetime.now().strftime("%H:%M:%S")}</span>'
    )


@router.post("/jobs/bulk-status")
def bulk_status(ids: str = Form(...), status: str = Form(...)):
    job_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    n = db.update_status_bulk(job_ids, status)
    return JSONResponse({"updated": n, "status": status})


@router.post("/scrape/trigger")
async def trigger_scrape():
    """Kick off `python -m src.main scrape` as a background subprocess.

    The subprocess inherits the active profile (resolved per-request from
    the cookie via ContextVar) so jobs land in the right DB. Without this,
    a scrape triggered while viewing one profile's dashboard would silently
    write to another profile's DB, because the subprocess can't see the cookie.
    """
    if _scrape_state["running"]:
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)
    profile = current_profile.get()
    _scrape_state["running"] = True
    _scrape_state["profile"] = profile
    _scrape_state["started_at"] = datetime.now().isoformat(timespec="seconds")
    _scrape_state["finished_at"] = None
    _scrape_state["log_tail"] = ""
    _scrape_state["current_scraper"] = None
    _scrape_state["per_source"] = {}
    _scrape_state["total_found"] = 0
    _scrape_state["total_new"] = 0
    asyncio.create_task(_run_scrape(profile))
    return JSONResponse({"ok": True, "started_at": _scrape_state["started_at"], "profile": profile})


@router.get("/scrape/status")
def scrape_status():
    return JSONResponse(_scrape_state)


@router.get("/scrape/widget", response_class=HTMLResponse)
def scrape_widget(request: Request):
    """HTML partial of scrape state — polled every 5s by the pipeline banner.

    Doubles as a keep-alive heartbeat: while a scrape is running and the
    pipeline tab is open, the poll keeps the Fly machine awake so the
    subprocess survives. Solves the auto-stop-kills-scrape issue.
    """
    return templates.TemplateResponse(
        request, "partials/scrape_widget.html",
        {**_scrape_state, "now": datetime.now().isoformat(timespec="seconds")},
    )


# Matches "Running jobs.ch..." / "Running career_pages (12 companies)...".
# Scraper names contain dots and spaces, so capture up to the trailing "..."
# rather than a single \w+ token.
_RE_RUNNING = re.compile(r"^\s*Running\s+(.+?)\.\.\.", re.IGNORECASE)
# Matches "jobs.ch: 47 found, 31 new" and the multi-word
# "career_pages (12 companies): 243 found, 18 new".
# The summary line ("Total: 6975 jobs found, 2786 new") does not match because
# the count is followed by "jobs found" rather than "found," directly.
_RE_COMPLETED = re.compile(r"^\s*(.+?):\s+(\d+)\s+found,\s+(\d+)\s+new")


async def _run_scrape(profile: str) -> None:
    """Run the CLI scraper, capture last few log lines + per-source counts.

    Live-parses the scraper output for "<source>: X found, Y new" lines so
    the dashboard widget can show per-source progress.

    ``start_new_session=True`` puts the subprocess in its own process group,
    so it survives uvicorn ``--reload`` killing the FastAPI worker.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _cli_python(),
            "-m",
            "src.main",
            "--profile", profile,
            "scrape",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        tail: list[str] = []
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            tail.append(line)
            if len(tail) > 25:
                tail.pop(0)
            _scrape_state["log_tail"] = "\n".join(tail)

            # Try to parse scraper-start markers
            m = _RE_RUNNING.search(line)
            if m:
                _scrape_state["current_scraper"] = m.group(1)
                continue

            # Try to parse per-source completion lines
            m = _RE_COMPLETED.match(line)
            if m:
                source, found, new = m.group(1), int(m.group(2)), int(m.group(3))
                _scrape_state["per_source"][source] = {"found": found, "new": new}
                _scrape_state["total_found"] = sum(
                    v["found"] for v in _scrape_state["per_source"].values()
                )
                _scrape_state["total_new"] = sum(
                    v["new"] for v in _scrape_state["per_source"].values()
                )

        await proc.wait()
        logger.info("Scrape finished with code %s", proc.returncode)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Scrape subprocess crashed")
        _scrape_state["log_tail"] += f"\nERROR: {exc}"
    finally:
        _scrape_state["running"] = False
        _scrape_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _scrape_state["current_scraper"] = None
