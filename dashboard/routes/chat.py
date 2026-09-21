"""Chat-Assistant routes — per-job multi-mode dialogue with Claude.

Endpoints:
    GET  /job/{id}/chat?mode=...            → render the chat panel partial
    POST /job/{id}/chat?mode=...            → append user message, get reply,
                                              return the new messages partial
    POST /job/{id}/chat/clear?mode=...      → wipe history (per mode)
    POST /job/{id}/chat/apply-edit/{msg_id} → adopt a proposed_edit as the
                                              new cover letter
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.deps import (
    attachment_catalog_with_existence,
    current_profile,
    db,
    get_config,
    templates,
)
from src.agent.chat import CHAT_MODES, JobChatAgent

logger = logging.getLogger(__name__)
router = APIRouter()

# Lazy per-profile chat agents — same pattern as application.py.
_agents: dict[str, JobChatAgent] = {}


def _agent() -> JobChatAgent:
    profile = current_profile.get()
    if profile not in _agents:
        _agents[profile] = JobChatAgent(config=get_config())
    return _agents[profile]


def _validate_mode(mode: Optional[str]) -> str:
    if mode not in CHAT_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode!r}")
    return mode


@router.get("/job/{job_id}/chat", response_class=HTMLResponse)
def chat_panel(request: Request, job_id: int, mode: str = Query("letter_review")):
    """Render the chat panel for a specific job + mode."""
    _validate_mode(mode)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    history = db.chat_messages(job_id, mode)
    return templates.TemplateResponse(
        request, "partials/chat_panel.html",
        {"job": job, "mode": mode, "messages": history},
    )


@router.post("/job/{job_id}/chat", response_class=HTMLResponse)
def chat_send(
    request: Request,
    job_id: int,
    mode: str = Query("letter_review"),
    message: str = Form(...),
):
    """Append a user message, call Claude, persist both turns, return the
    updated message list partial."""
    _validate_mode(mode)
    user_message = (message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Empty message")

    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Persist user message first (so if Claude blows up, we don't lose it).
    db.add_chat_message(job_id, mode, "user", user_message)

    history = db.chat_messages(job_id, mode)[:-1]  # exclude the message we just added

    apply_method = None
    am_raw = job.get("apply_method")
    if am_raw:
        try:
            apply_method = json.loads(am_raw)
        except (TypeError, ValueError):
            apply_method = None

    try:
        turn = _agent().reply(
            mode=mode,
            job=job,
            history=history,
            user_message=user_message,
            cover_letter=job.get("cover_letter") if mode == "letter_review" else None,
            apply_method=apply_method if mode == "application_advisor" else None,
            attachment_catalog=(
                attachment_catalog_with_existence() if mode == "application_advisor" else []
            ),
        )
    except Exception as exc:
        logger.exception("Chat reply failed")
        # Record a system-side error message so the UI shows it.
        db.add_chat_message(
            job_id, mode, "assistant",
            f"⚠ Error: {exc}", cost_usd=0.0,
        )
        history = db.chat_messages(job_id, mode)
        return templates.TemplateResponse(
            request, "partials/chat_messages.html",
            {"job": job, "mode": mode, "messages": history},
        )

    db.add_chat_message(
        job_id, mode, "assistant",
        turn.reply_text,
        proposed_edit=turn.proposed_edit,
        cost_usd=turn.cost_usd,
    )

    history = db.chat_messages(job_id, mode)
    return templates.TemplateResponse(
        request, "partials/chat_messages.html",
        {"job": job, "mode": mode, "messages": history},
    )


@router.post("/job/{job_id}/chat/clear", response_class=HTMLResponse)
def chat_clear(request: Request, job_id: int, mode: str = Query("letter_review")):
    """Delete all messages for (job, mode) and return an empty list partial."""
    _validate_mode(mode)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    db.clear_chat(job_id, mode)
    return templates.TemplateResponse(
        request, "partials/chat_messages.html",
        {"job": job, "mode": mode, "messages": []},
    )


@router.post("/job/{job_id}/chat/apply-edit/{msg_id}", response_class=HTMLResponse)
def chat_apply_edit(request: Request, job_id: int, msg_id: int):
    """Adopt the revision proposed in the given message as the current letter."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    msg = db.get_chat_message(msg_id)
    if not msg or msg["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="Chat message not found")
    edit = msg.get("proposed_edit")
    if not edit:
        raise HTTPException(status_code=400, detail="No proposed edit on this message")

    db.save_cover_letter(job_id, markdown=edit)
    job = db.get_job(job_id)
    history = db.chat_messages(job_id, "letter_review")
    # Mark the applied flag client-side by re-rendering the message list and
    # also returning a header that the JS uses to refresh the application
    # section.
    resp = templates.TemplateResponse(
        request, "partials/chat_messages.html",
        {"job": job, "mode": "letter_review", "messages": history, "just_applied_msg_id": msg_id},
    )
    resp.headers["HX-Trigger"] = "cover-letter-updated"
    return resp
