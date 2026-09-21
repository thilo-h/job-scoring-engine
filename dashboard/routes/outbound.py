"""Outbound-Startseite und Watchlist-Übersicht.

Die Startseite zeigt nicht mehr, was gefunden wurde, sondern was rausging:
vier Zahlen oben, darunter die Listen, aus denen sie entstehen. Versand bleibt
manuell — hier werden Bewerbungen nur erfasst und weitergeschoben.
"""

from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.deps import current_profile, db, templates
from src.database import APPLICATION_CHANNELS

router = APIRouter()


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    referer = request.headers.get("referer") or fallback
    return RedirectResponse(url=referer, status_code=303)


@router.get("/", response_class=HTMLResponse)
def outbound_home(request: Request):
    from src.watchlist import recent_hits

    return templates.TemplateResponse(
        request,
        "outbound.html",
        {
            "active_tab": "outbound",
            "overview": db.outbound_overview(),
            "watchlist_hits": recent_hits(db, since=datetime.now() - timedelta(days=7)),
        },
    )


@router.post("/applications")
def create_application(
    request: Request,
    company: str = Form(...),
    role: str = Form(...),
    channel: str = Form("ats"),
    status: str = Form("drafted"),
    notes: str = Form(""),
):
    try:
        db.create_application(company=company, role=role, channel=channel, status=status, notes=notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _back(request)


@router.post("/applications/{app_id}/advance")
def advance_application(
    request: Request, app_id: int,
    action: str = Form(...), outcome: str = Form(""),
):
    try:
        db.advance_application(app_id, action, outcome=outcome or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _back(request)


@router.post("/applications/{app_id}/pbl")
def set_pbl(request: Request, app_id: int, value: str = Form(...)):
    try:
        db.set_application_pbl(app_id, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _back(request)


def _default_channel(job: dict) -> str:
    """Vorauswahl aus der Apply-Method-Erkennung bzw. einer bekannten Kontaktadresse."""
    import json

    try:
        method = json.loads(job.get("apply_method") or "null") or {}
    except ValueError:
        method = {}
    primary = method.get("primary_channel")
    if primary == "email" or (not primary and job.get("contact_email")):
        return "email"
    return "ats"


def _badge(request: Request, job_id: int, *, error: str | None = None):
    # Fehler bewusst mit 200 und Meldung im Block: htmx 2 tauscht bei 4xx
    # nichts aus, eine 400 käme im Drawer also nie als Meldung an.
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        request,
        "partials/outbound_badge.html",
        {"job": job, "application": db.application_for_job(job_id),
         "default_channel": _default_channel(job), "error": error},
    )


def _recipient(raw: str) -> tuple[str | None, str | None]:
    """Empfänger-Eingabe → (Adresse | None, Domain).

        "a@firma.ch" → ("a@firma.ch", "firma.ch")
        "@firma.ch"  → (None, "firma.ch")
        "firma.ch"   → (None, "firma.ch")
        ""           → (None, None)        kein Empfänger angegeben

    Eine reine Domain reicht fürs Antwort-Tracking (der Reply-Tracker matcht
    per Absender-Domain), ist aber keine Kontaktadresse — deshalb getrennt.
    """
    value = (raw or "").strip()
    if not value:
        return None, None
    if any(ch.isspace() for ch in value):
        raise ValueError(f"„{value}“ enthält Leerzeichen — Adresse oder Domain, z.B. jobs@firma.ch oder @firma.ch")
    local, _at, domain = value.rpartition("@")
    domain = domain.lower()
    if "@" in local or "." not in domain or domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ValueError(f"„{value}“ ist weder Adresse noch Domain — z.B. jobs@firma.ch oder @firma.ch")
    return (value if local else None), domain


# Kanäle, bei denen eine volle Adresse eine Kontaktadresse ist. Bei Portal und
# Referral zählt nur die Domain, von der Antworten kommen (Reply-Tracking).
_MAIL_CHANNELS = ("email", "direct_contact")


def _tracking(raw: str, channel: str) -> tuple[str | None, str | None]:
    address, domain = _recipient(raw)
    return (address if channel in _MAIL_CHANNELS else None), domain


def _mark_sent(
    job_id: int, app_id: int, recipient: tuple[str | None, str | None], message_id: str | None
) -> None:
    """Versendet — mit Empfänger/Domain über mark_sent (Reply-Tracking), sonst direkt."""
    address, domain = recipient
    if domain:
        # mark_sent setzt Job-Status + Empfängerfelder; der Pipeline-Sync schiebt
        # die bereits angelegte Bewerbung dabei von drafted auf sent. Der gewählte
        # Kanal bleibt erhalten, weil der Sync ihn nur beim Neuanlegen setzt.
        if address:  # eine reine Domain ist keine Kontaktadresse für „Send…"
            db.update_email_fields(job_id, contact_email=address)
        db.mark_sent(job_id, eml_or_smtp="manual", message_id=message_id,
                     recipient_email=address, recipient_domain=domain)
    else:
        db.advance_application(app_id, "sent")


@router.get("/job/{job_id}/outbound", response_class=HTMLResponse)
def job_outbound_badge(request: Request, job_id: int):
    return _badge(request, job_id)


@router.post("/job/{job_id}/outbound", response_class=HTMLResponse)
def job_outbound_create(
    request: Request,
    job_id: int,
    channel: str = Form("ats"),
    status: str = Form("drafted"),
    recipient_email: str = Form(""),
    message_id: str = Form(""),
):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if channel not in APPLICATION_CHANNELS:
        return _badge(request, job_id, error=f"Unbekannter Kanal {channel!r}")
    try:
        recipient = _tracking(recipient_email, channel)
    except ValueError as exc:
        return _badge(request, job_id, error=str(exc))
    app_id = db.create_application(
        company=job["company"], role=job["title"], channel=channel, job_id=job_id, status="drafted"
    )
    if status == "sent":
        _mark_sent(job_id, app_id, recipient, message_id.strip() or None)
    elif recipient[1]:  # Entwurf: Adresse → Kontakt, reine Domain → nur vorgemerkt
        db.remember_draft_recipient(job_id, *recipient)
    return _badge(request, job_id)


@router.post("/job/{job_id}/outbound/advance", response_class=HTMLResponse)
def job_outbound_advance(
    request: Request,
    job_id: int,
    action: str = Form(...),
    recipient_email: str = Form(""),
    message_id: str = Form(""),
):
    app = db.application_for_job(job_id)
    if app is None:
        return _badge(request, job_id, error="Noch keine Bewerbung erfasst")
    try:
        if action == "sent":
            recipient = _tracking(recipient_email, app["channel"])
            _mark_sent(job_id, app["id"], recipient, message_id.strip() or None)
        else:
            db.advance_application(app["id"], action)
    except ValueError as exc:
        return _badge(request, job_id, error=str(exc))
    return _badge(request, job_id)


@router.post("/job/{job_id}/outbound/recipient", response_class=HTMLResponse)
def job_outbound_recipient(
    request: Request, job_id: int, recipient_email: str = Form(""), message_id: str = Form("")
):
    app = db.application_for_job(job_id) or {}
    try:
        address, domain = _tracking(recipient_email, app.get("channel") or "ats")
    except ValueError as exc:
        return _badge(request, job_id, error=str(exc))
    if not domain:
        return _badge(request, job_id, error="Bitte eine Adresse oder Domain angeben, z.B. jobs@firma.ch oder @firma.ch")
    db.set_application_recipient(job_id, address, domain, message_id.strip() or None)
    return _badge(request, job_id)


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request):
    from src.watchlist import company_states, load_watchlist, recent_hits

    watchlist = load_watchlist(current_profile.get())
    entries = {e.key: e for e in watchlist.entries} if watchlist else {}
    companies = company_states(db)
    for c in companies:
        entry = entries.get(c["company_key"])
        c["why"] = entry.why if entry else ""
        c["has_ats"] = entry.ats if entry and entry.ats_slug else None
    tiers = Counter(c["tier"] for c in companies)
    return templates.TemplateResponse(
        request,
        "watchlist.html",
        {
            "active_tab": "watchlist",
            "profile": current_profile.get(),
            "companies": companies,
            "tier_counts": sorted(tiers.items(), key=lambda kv: ["A", "A2", "B", "C", "D"].index(kv[0])),
            "last_poll": max((c["last_polled_at"] for c in companies), default=""),
            "hits": recent_hits(db, since=datetime.now() - timedelta(days=14)),
        },
    )
