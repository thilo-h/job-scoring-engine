"""Plain-text mail plumbing for self-notifications.

Deliberately minimal and deliberately one-directional: this module builds and
sends plain-text mail **to the operator's own address** — watchlist alerts and
the weekly digest. It has no attachment support and no notion of a recipient
other than the configured one, so it cannot be used to send an application.

Credentials come from the environment (see ``.env.example``). Whether anything
is actually sent is decided by the caller — :mod:`src.notify` defaults to
writing an ``.eml`` file instead.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def build_message(
    *,
    sender_name: str,
    sender_email: str,
    recipient_email: str,
    subject: str,
    body_text: str,
) -> EmailMessage:
    """Build a plain-text MIME message. Nothing is sent here."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    msg.set_content(body_text)
    return msg


def smtp_send(msg: EmailMessage) -> None:
    """Send via SMTP using credentials from the environment. Raises on failure."""
    load_dotenv(ROOT / ".env", override=True)
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")

    if not all([host, user, password]):
        raise RuntimeError("SMTP_HOST / SMTP_USER / SMTP_PASS not all set")

    logger.info("Connecting to %s:%s as %s …", host, port, user)
    # macOS Python ships without trusting the system root CAs. Use certifi's
    # bundle to avoid `[SSL: CERTIFICATE_VERIFY_FAILED]`.
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        # 587 → STARTTLS
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)
    logger.info("SMTP send OK to %s", msg["To"])
