"""IMAP reply-tracker — closes the application loop.

Polls an IMAP inbox for new mails, correlates them with previously-sent
applications via two strategies (Message-ID threading, sender-domain fallback),
classifies the reply with Haiku, and auto-bumps the job's application_status
when the classification is confident.

Setup — see ``.env.example`` for the full list:

    IMAP_HOST=imap.example.com
    IMAP_PORT=993
    IMAP_USER=you@example.com
    IMAP_PASS=<app-password>
    IMAP_MAILBOX=INBOX
    REPLY_AUTO_BUMP=true   # set to false to only log replies without status change

Trigger from CLI:

    python -m src.main --profile example poll-replies

Mid-confidence replies are recorded but not auto-bumped — they show up in
``recent_replies()`` for manual review in the dashboard.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

HAIKU_MODEL = "claude-haiku-4-5"

# Body excerpt cap — Haiku doesn't need the full thread to classify a reply,
# and trimming keeps the classification cost predictable.
BODY_EXCERPT_CHARS = 2000

# Confidence floor for auto-bumping. Below this we record but defer to the
# user for manual override via the dashboard.
AUTO_BUMP_CONFIDENCE = 0.75

# On the very first run there is no last UID, so a bare `UID 1:*` search would
# pull the whole mailbox. Limit the initial sweep to this window instead.
FIRST_RUN_WINDOW_DAYS = 60

# Map classification → ApplicationStatus value (DB string).
CLASSIFICATION_TO_STATUS = {
    "interview": "interview",
    "rejected": "rejected",
    "offer": "offer",
    # 'acknowledgement' means "ATS auto-reply, thanks for applying" — keep
    # status at applied, but record the reply for traceability.
    "acknowledgement": None,
    "needs_review": None,
    "unrelated": None,
}


@dataclass
class PollSummary:
    fetched: int = 0
    matched: int = 0
    classified: int = 0
    bumped: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _imap_settings() -> dict:
    load_dotenv(ROOT / ".env", override=True)
    host = os.getenv("IMAP_HOST")
    user = os.getenv("IMAP_USER")
    password = os.getenv("IMAP_PASS")
    if not all([host, user, password]):
        raise RuntimeError(
            "IMAP_HOST / IMAP_USER / IMAP_PASS must all be set in .env"
        )
    return {
        "host": host,
        "port": int(os.getenv("IMAP_PORT", "993")),
        "user": user,
        "password": password,
        "mailbox": os.getenv("IMAP_MAILBOX", "INBOX"),
        "auto_bump": os.getenv("REPLY_AUTO_BUMP", "true").strip().lower()
        in ("1", "true", "yes"),
    }


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _first_address(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    addrs = getaddresses([value])
    for _, addr in addrs:
        if addr and "@" in addr:
            return addr.lower().strip()
    return None


def _extract_body(msg: email.message.Message) -> str:
    """Return the best-effort plain-text body (excerpt-sized)."""
    text_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    text_parts.append(payload.decode(charset, errors="replace"))
                except LookupError:
                    text_parts.append(payload.decode("utf-8", errors="replace"))
        if not text_parts:
            # Fallback: strip HTML crudely
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html = payload.decode(charset, errors="replace")
                    except LookupError:
                        html = payload.decode("utf-8", errors="replace")
                    text_parts.append(re.sub(r"<[^>]+>", " ", html))
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text_parts.append(payload.decode(charset, errors="replace"))
        except LookupError:
            text_parts.append(payload.decode("utf-8", errors="replace"))
    body = "\n".join(text_parts).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body[:BODY_EXCERPT_CHARS]


def _parse_references(header: Optional[str]) -> list[str]:
    if not header:
        return []
    return [m for m in re.findall(r"<[^>]+>", header)]


def _normalize_msgid(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    m = re.search(r"<[^>]+>", value)
    return m.group(0) if m else value.strip()


def _classify(client, subject: str, body_excerpt: str) -> Optional[dict]:
    """Call Haiku to classify the reply. Returns dict or None on failure."""
    tool = {
        "name": "classify_reply",
        "description": (
            "Classify an inbound email reply to a job application. "
            "Decide what stage of the funnel this represents and how confident you are."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": [
                        "interview",
                        "rejected",
                        "offer",
                        "acknowledgement",
                        "needs_review",
                        "unrelated",
                    ],
                    "description": (
                        "interview = invited to call/interview/next round. "
                        "rejected = explicit no/regret/we moved on. "
                        "offer = formal offer extended. "
                        "acknowledgement = auto-ack 'we received your application'. "
                        "needs_review = recruiter wrote something nuanced, ambiguous, or asks a question. "
                        "unrelated = newsletter/marketing/wrong-thread."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "0-1. Use ≥0.85 only when wording is unambiguous.",
                },
                "summary": {
                    "type": "string",
                    "description": "One short sentence summarizing what the sender said.",
                },
            },
            "required": ["classification", "confidence", "summary"],
        },
    }

    system = (
        "You triage inbound replies to job applications. "
        "Be conservative: when in doubt prefer 'needs_review' over guessing. "
        "Treat ATS auto-acks ('Vielen Dank für Ihre Bewerbung', "
        "'We have received your application') as 'acknowledgement', "
        "not 'interview'."
    )
    user = (
        f"Subject: {subject}\n\n"
        f"Body:\n{body_excerpt or '(empty body)'}"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "classify_reply"},
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Haiku classification failed: %s", exc)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            data = dict(block.input)
            data["_usage"] = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            return data
    return None


def _match_reply(db, headers: dict) -> tuple[Optional[dict], Optional[str]]:
    """Find the application this reply belongs to.

    Returns (job_dict, match_method) — match_method ∈ {message_id, domain, None}.
    """
    # Strategy 1: Message-ID threading
    candidates: list[str] = []
    if headers.get("in_reply_to"):
        candidates.append(_normalize_msgid(headers["in_reply_to"]))
    candidates.extend(_parse_references(headers.get("references")))
    for cand in candidates:
        if not cand:
            continue
        job = db.find_job_by_message_id(cand)
        if job:
            return job, "message_id"

    # Strategy 2: domain fallback
    from_addr = headers.get("from_email")
    if from_addr and "@" in from_addr:
        domain = from_addr.split("@", 1)[1].lower().strip()
        # Ignore generic free-mail and aggregator domains as fallback signal.
        if domain not in {
            "gmail.com", "googlemail.com", "yahoo.com", "outlook.com",
            "hotmail.com", "icloud.com", "me.com", "bluewin.ch", "gmx.ch",
            "gmx.net", "gmx.de", "web.de", "linkedin.com", "indeed.com",
        }:
            jobs = db.find_jobs_by_recipient_domain(domain)
            if len(jobs) == 1:
                return jobs[0], "domain"
            if jobs:
                # Multiple candidates → ambiguous. Record against the most recent
                # but mark as low-confidence by flagging the match_method.
                return jobs[0], "domain_ambiguous"
    return None, None


def _bump_status(db, job_id: int, new_status: str) -> None:
    """Update the job's application_status without touching the audit fields."""
    now = datetime.now().isoformat()
    db.conn.execute(
        "UPDATE jobs SET application_status = ?, date_updated = ? WHERE id = ?",
        (new_status, now, job_id),
    )
    # Umgeht update_status (kein Audit-Eintrag), deshalb den Outbound-Sync
    # hier explizit — sonst stünde ein Interview nur im Kanban.
    db.sync_application_from_job(job_id, new_status)
    db.conn.commit()


def poll_replies(db, *, anthropic_client=None, dry_run: bool = False) -> PollSummary:
    """Connect to IMAP, process new messages, return a summary.

    The poller is idempotent on Message-ID via ``replies.raw_message_id``
    UNIQUE index — re-running won't double-record.
    """
    settings = _imap_settings()
    summary = PollSummary()

    last_uid = db.get_imap_last_uid(settings["mailbox"])
    logger.info(
        "IMAP poll start mailbox=%s last_uid=%s host=%s",
        settings["mailbox"], last_uid, settings["host"],
    )

    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()

    if anthropic_client is None and not dry_run:
        try:
            from src.llm import get_client, mock_enabled
            if mock_enabled() or os.getenv("ANTHROPIC_API_KEY"):
                anthropic_client = get_client(purpose="reply classification")
        except Exception as exc:  # noqa: BLE001
            logger.warning("No Anthropic client available — replies will be unclassified: %s", exc)

    with imaplib.IMAP4_SSL(settings["host"], settings["port"], ssl_context=ctx) as m:
        m.login(settings["user"], settings["password"])
        m.select(settings["mailbox"])

        # UID search > last_uid. If last_uid==0, we'd pull *everything* — guard
        # by only taking mail from the last 60 days on first run.
        if last_uid == 0:
            since = (datetime.now() - timedelta(days=FIRST_RUN_WINDOW_DAYS)).strftime("%d-%b-%Y")
            status, data = m.uid("search", None, f"(SINCE {since})")
        else:
            status, data = m.uid("search", None, f"(UID {last_uid + 1}:*)")
        if status != "OK":
            logger.error("IMAP search failed: %s", data)
            return summary

        uids = [int(u) for u in (data[0] or b"").split()] if data else []
        # IMAP `UID N:*` quirk: when no new mail exists, servers often still
        # return the most recent UID. Filter strictly greater than last_uid.
        uids = [u for u in uids if u > last_uid]
        logger.info("IMAP: %d new message(s) to process", len(uids))

        max_uid_seen = last_uid
        for uid in uids:
            summary.fetched += 1
            try:
                status, fetched = m.uid("fetch", str(uid).encode(), "(RFC822)")
                if status != "OK" or not fetched or not fetched[0]:
                    summary.errors += 1
                    continue
                raw = fetched[0][1]
                msg = email.message_from_bytes(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fetch UID %s failed: %s", uid, exc)
                summary.errors += 1
                continue

            max_uid_seen = max(max_uid_seen, uid)

            raw_msgid = _normalize_msgid(msg.get("Message-ID"))
            if raw_msgid and db.reply_exists(raw_msgid):
                summary.skipped += 1
                continue

            headers = {
                "from_email": _first_address(_decode(msg.get("From"))),
                "subject": _decode(msg.get("Subject")),
                "in_reply_to": _normalize_msgid(msg.get("In-Reply-To")),
                "references": msg.get("References") or "",
            }

            received_at = None
            date_hdr = msg.get("Date")
            if date_hdr:
                try:
                    received_at = parsedate_to_datetime(date_hdr).isoformat()
                except (TypeError, ValueError):
                    received_at = None

            job, match_method = _match_reply(db, headers)
            if not job:
                summary.skipped += 1
                # Still advance last_uid so we don't re-scan endlessly. Don't
                # record unmatched mail — that's just inbox-spam from our POV.
                continue

            summary.matched += 1
            body_excerpt = _extract_body(msg)

            classification = None
            confidence = None
            classified_summary = None
            if anthropic_client and not dry_run:
                result = _classify(
                    anthropic_client,
                    subject=headers["subject"],
                    body_excerpt=body_excerpt,
                )
                if result:
                    summary.classified += 1
                    classification = result.get("classification")
                    confidence = float(result.get("confidence", 0))
                    classified_summary = result.get("summary")

            new_status = CLASSIFICATION_TO_STATUS.get(classification)
            should_bump = (
                settings["auto_bump"]
                and not dry_run
                and new_status is not None
                and confidence is not None
                and confidence >= AUTO_BUMP_CONFIDENCE
            )

            if should_bump:
                _bump_status(db, job["id"], new_status)
                summary.bumped += 1
                logger.info(
                    "Bumped job %s (%s @ %s) → %s (conf=%.2f)",
                    job["id"], job.get("title"), job.get("company"),
                    new_status, confidence,
                )

            db.record_reply(
                job_id=job["id"],
                imap_uid=uid,
                raw_message_id=raw_msgid,
                in_reply_to=headers["in_reply_to"],
                references_header=headers["references"] or None,
                from_email=headers["from_email"],
                subject=headers["subject"],
                body_excerpt=(
                    f"{classified_summary}\n\n---\n{body_excerpt}"
                    if classified_summary else body_excerpt
                ),
                received_at=received_at,
                classification=classification,
                classification_confidence=confidence,
                match_method=match_method,
                auto_bumped=should_bump,
            )

        if not dry_run and max_uid_seen > last_uid:
            db.set_imap_last_uid(settings["mailbox"], max_uid_seen)

        m.close()

    logger.info("IMAP poll done: %s", json.dumps(summary.as_dict()))
    return summary
