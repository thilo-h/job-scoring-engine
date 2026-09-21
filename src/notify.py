"""Benachrichtigungen an sich selbst — Watchlist-Alerts und Wochendigest.

Der einzige Pfad im Projekt, der überhaupt Mail verschickt, und er geht
ausschliesslich an die eigene Adresse. Standardmässig verschickt er nichts,
sondern legt eine ``.eml``-Datei ab:

    NOTIFY_DRY_RUN=false     # Default true → .eml in data/outbox/notifications/
    NOTIFY_EMAIL_TO=...      # optional, sonst profile.sender.email

Die SMTP-Mechanik liegt in :mod:`src.mailer` und kennt keine Anhänge.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.mailer import build_message, smtp_send

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
NOTIFY_OUTBOX = ROOT / "data" / "outbox" / "notifications"


@dataclass
class NotifyResult:
    mode: str                     # "smtp" | "dry_run"
    recipient: str
    path: Optional[Path] = None   # nur im Dry-Run


def is_dry_run() -> bool:
    load_dotenv(ROOT / ".env", override=True)
    return os.getenv("NOTIFY_DRY_RUN", "true").strip().lower() in ("1", "true", "yes")


def _recipient(config: dict) -> str:
    load_dotenv(ROOT / ".env", override=True)
    explicit = os.getenv("NOTIFY_EMAIL_TO", "").strip()
    if explicit:
        return explicit
    sender = (config.get("profile") or {}).get("sender") or {}
    address = (sender.get("email") or os.getenv("SMTP_USER") or "").strip()
    if not address:
        raise RuntimeError(
            "Keine Empfängeradresse: NOTIFY_EMAIL_TO in .env oder "
            "profile.sender.email im Profil setzen"
        )
    return address


def send_to_self(config: dict, *, subject: str, body: str, slug: str) -> NotifyResult:
    """Schickt eine Klartext-Mail an die eigene Adresse (oder schreibt .eml)."""
    recipient = _recipient(config)
    profile = config.get("profile") or {}
    sender_name = f"Job Finder · {profile.get('name', '')}".strip(" ·")
    sender_email = os.getenv("SMTP_USER", "").strip() or recipient

    msg = build_message(
        sender_name=sender_name,
        sender_email=sender_email,
        recipient_email=recipient,
        subject=subject,
        body_text=body,
    )

    if is_dry_run():
        NOTIFY_OUTBOX.mkdir(parents=True, exist_ok=True)
        path = NOTIFY_OUTBOX / f"{datetime.now():%Y-%m-%d_%H%M%S}_{slug}.eml"
        path.write_bytes(bytes(msg))
        logger.info("NOTIFY_DRY_RUN: %s → %s", subject, path)
        return NotifyResult(mode="dry_run", recipient=recipient, path=path)

    smtp_send(msg)
    return NotifyResult(mode="smtp", recipient=recipient)
