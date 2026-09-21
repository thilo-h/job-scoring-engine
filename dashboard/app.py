"""FastAPI dashboard entry point.

Run:
    uvicorn dashboard.app:app --reload --host 127.0.0.1 --port 8000
"""

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from dashboard.deps import (
    current_profile,
    get_config,
    list_profiles,
    templates,
)
from dashboard.routes import (
    actions,
    application,
    browse,
    chat,
    db_explorer,
    manual_add,
    outbound,
    pipeline,
    replies,
    stats,
)
from src.config import DEFAULT_PROFILE, resolve_profile

ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Job Finder Dashboard", docs_url=None, redoc_url=None)

# ── Optional HTTP Basic Auth ────────────────────────────────────────────
# Activated only when both DASHBOARD_USER + DASHBOARD_PASS env vars are set
# (e.g. when the dashboard is exposed beyond localhost). Local dev runs
# unauthenticated.
_BASIC_USER = os.getenv("DASHBOARD_USER", "").strip()
_BASIC_PASS = os.getenv("DASHBOARD_PASS", "").strip()


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """Single-user Basic Auth — gate everything except /static/* assets."""
    if not (_BASIC_USER and _BASIC_PASS):
        return await call_next(request)
    if request.url.path.startswith("/static/"):
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Job Finder"'},
        )
    import base64
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, _, passwd = decoded.partition(":")
    except Exception:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Job Finder"'})

    # Constant-time comparison to avoid timing-attack leak.
    user_ok = secrets.compare_digest(user, _BASIC_USER)
    pass_ok = secrets.compare_digest(passwd, _BASIC_PASS)
    if not (user_ok and pass_ok):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Job Finder"'})
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# Make profile + config available to all templates (header dropdown).
@app.middleware("http")
async def profile_middleware(request: Request, call_next):
    """Resolve the active profile per request from cookie → env var → default.

    The cookie is validated against ``list_profiles()`` — a stale cookie that
    references a renamed or deleted profile is ignored and overwritten with
    the default on the way out, so the user recovers automatically on the
    next request.
    """
    cookie_profile = request.cookies.get("jobfinder_profile")
    available = set(list_profiles())
    stale_cookie = False
    if cookie_profile and cookie_profile not in available:
        # Cookie points at a profile that no longer exists. Fall back and
        # mark for cleanup below.
        stale_cookie = True
        cookie_profile = None

    profile = resolve_profile(cookie_profile)
    # If even the resolved profile doesn't exist (config dir is empty, or the
    # default profile was renamed), pick whatever is available.
    if profile not in available and available:
        profile = sorted(available)[0]

    token = current_profile.set(profile)
    try:
        request.state.profile = profile
        response = await call_next(request)
        if stale_cookie:
            # Overwrite the bad cookie so subsequent requests don't repeat
            # the failure path.
            response.set_cookie(
                "jobfinder_profile", profile,
                max_age=60 * 60 * 24 * 365,
                samesite="lax", httponly=False,
            )
        return response
    finally:
        current_profile.reset(token)


# Make profile context globally available via Jinja (read at render time
# from the ContextVar through a function reference).
def _active_profile_ctx() -> dict:
    try:
        cfg = get_config()
        return {
            "profile": current_profile.get(),
            "profile_display": cfg.get("profile", {}).get("name") or current_profile.get(),
        }
    except Exception:
        return {"profile": current_profile.get(), "profile_display": current_profile.get()}


templates.env.globals["active_profile"] = lambda: _active_profile_ctx()
templates.env.globals["available_profiles"] = list_profiles


@app.post("/profile/switch")
def switch_profile(request: Request, profile: str = Form("")):
    """Set the profile cookie and redirect back to the page the user came from.

    The cookie is read by the middleware on every subsequent request, so the
    next page load uses the chosen profile's database + assets.
    """
    target = profile.strip() or DEFAULT_PROFILE
    available = set(list_profiles())
    if target not in available:
        target = DEFAULT_PROFILE

    referer = request.headers.get("referer", "/")
    response = RedirectResponse(url=referer, status_code=303)
    response.set_cookie(
        "jobfinder_profile",
        target,
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
        httponly=False,
    )
    return response


app.include_router(outbound.router)
app.include_router(browse.router)
app.include_router(pipeline.router)
app.include_router(stats.router)
app.include_router(db_explorer.router)
app.include_router(actions.router)
app.include_router(application.router)
app.include_router(chat.router)
app.include_router(manual_add.router)
app.include_router(replies.router)
