"""Replies inbox — surfaced IMAP-poller output for manual review.

Lists recent replies pulled by ``src/agent/reply_tracker.py``, lets the user
manually override the auto-classification, and exposes a "Poll now" trigger
so the inbox can be refreshed without dropping to CLI.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.deps import ALL_STATUSES, db, get_db, templates

logger = logging.getLogger(__name__)
router = APIRouter()


# Replies whose classification the user can apply with one click.
ACTIONABLE_STATUSES = {"interview", "rejected", "offer", "applied", "bookmarked"}


@router.get("/replies", response_class=HTMLResponse)
def replies_inbox(request: Request, limit: int = 100):
    """Render the replies inbox."""
    replies = db.recent_replies(limit=limit)
    # Last-poll timestamp for the header — read from imap_state directly.
    row = db.conn.execute(
        "SELECT mailbox, last_uid, last_polled_at FROM imap_state ORDER BY last_polled_at DESC LIMIT 1"
    ).fetchone()
    last_poll = dict(row) if row else None

    template = (
        "partials/replies_list.html"
        if request.headers.get("HX-Request")
        else "replies.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_tab": "replies",
            "replies": replies,
            "last_poll": last_poll,
            "actionable_statuses": list(ACTIONABLE_STATUSES),
            "all_statuses": ALL_STATUSES,
        },
    )


@router.post("/replies/poll")
def poll_now():
    """Trigger an IMAP poll on-demand. Synchronous — returns the summary."""
    from src.agent.reply_tracker import poll_replies

    try:
        summary = poll_replies(get_db())
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Reply-poll failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, "summary": summary.as_dict()})


@router.post("/replies/{reply_id}/apply-status")
def apply_status(reply_id: int, status: str = Form(...)):
    """User clicks 'Apply' on a reply → bump the linked job's status."""
    if status not in ALL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status: {status}")

    reply = db.conn.execute(
        "SELECT id, job_id FROM replies WHERE id = ?", (reply_id,)
    ).fetchone()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")
    if not reply["job_id"]:
        raise HTTPException(status_code=400, detail="Reply not linked to a job")

    db.update_status(reply["job_id"], status)
    # Mark the reply as user-applied so the UI can hide the action buttons.
    db.conn.execute(
        "UPDATE replies SET auto_bumped = 1 WHERE id = ?", (reply_id,)
    )
    db.conn.commit()
    return JSONResponse({"ok": True, "job_id": reply["job_id"], "status": status})


@router.post("/replies/{reply_id}/dismiss")
def dismiss_reply(reply_id: int):
    """Hide a reply from the inbox without changing job status.

    Implemented by setting auto_bumped=1 — same idempotency flag we use for
    'already actioned'. Reply stays in the DB for audit.
    """
    cur = db.conn.execute(
        "UPDATE replies SET auto_bumped = 1 WHERE id = ?", (reply_id,)
    )
    db.conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Reply not found")
    return JSONResponse({"ok": True})
