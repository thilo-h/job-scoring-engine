"""Apply-Method-Detector — classify how to apply for a given job.

For each job, decide:
  - primary channel (email / portal / linkedin / job_board / phone / unclear)
  - recipient email (if any)
  - portal/apply URL (if any)
  - required documents (cv, cover_letter, transcripts, references, portfolio, …)
  - free-form notes (constraints the user should know)

Output is a JSON blob persisted in ``jobs.apply_method``. UI uses it to
pre-fill the Send modal and warn when a job needs a non-email channel.

Uses Haiku 4.5 (cheap, fast) + web_search (find the careers page when the
job posting doesn't mention an email).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.llm import get_client

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 800

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_CACHE_WRITE_PER_MTOK = 1.25


CHANNELS = ["email", "portal", "linkedin", "job_board", "phone", "unclear"]
DOC_TYPES = ["cv", "cover_letter", "transcripts", "references", "portfolio", "certificates", "other"]


SYSTEM_PROMPT = f"""Du bist ein Bewerbungsassistent. Bestimme für einen
Jobpost den optimalen Bewerbungsweg: per Email, über ein Bewerbungsportal
(Workday/Greenhouse/Lever/Eigenes), via LinkedIn, über die Job-Board-eigene
Apply-Funktion (z.B. jobs.ch Schnellbewerbung) oder telefonisch.

Bewertungs-Reihenfolge (was die Anzeige sagt zählt):
1. Wenn ein konkreter Apply-Link (Workday, Greenhouse, Lever, Personio,
   eigene Karriere-Seite, etc.) in der Description ist → "portal"
2. Wenn eine Email-Adresse für die Bewerbung explizit drin steht → "email"
3. Wenn nur "Bewerbung über LinkedIn" oder LinkedIn-Easy-Apply → "linkedin"
4. Wenn nur ein "Jetzt bewerben"-Button auf dem Job-Board (ohne weiteres) → "job_board"
5. Wenn nur eine Telefonnummer → "phone"
6. Sonst: "unclear" — und nutze `web_search` um die Firmen-Karriere-Seite
   zu finden ("<company> jobs apply" oder "<company> Karriere") und checke
   ob dort Email oder Portal genannt sind.

Required documents: was die Anzeige verlangt. Wenn nicht explizit, default
Set für Schweizer/deutsche Bewerbungen: ["cv", "cover_letter"]. Wenn Zeugnisse
erwähnt werden → +"transcripts". Wenn Referenzen → +"references". Portfolio
nur wenn explizit verlangt (Design/UX-Stellen).

Mögliche Dokument-Typen: {", ".join(DOC_TYPES)}
Mögliche Kanäle: {", ".join(CHANNELS)}

Wenn du dir nicht sicher bist, lieber `unclear` zurückgeben statt zu raten.
"""


@dataclass
class ApplyMethodResult:
    primary_channel: str
    email: Optional[str]
    portal_url: Optional[str]
    apply_url: Optional[str]
    required_documents: list[str] = field(default_factory=list)
    notes: str = ""
    web_searches: int = 0
    # Telemetry
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "primary_channel": self.primary_channel,
            "email": self.email,
            "portal_url": self.portal_url,
            "apply_url": self.apply_url,
            "required_documents": self.required_documents,
            "notes": self.notes,
        }


class ApplyMethodDetector:
    def __init__(self, api_key: Optional[str] = None):
        self.client = get_client(api_key, purpose="apply-method detection")

    def detect(
        self, *, title: str, company: str, location: str, description: str, url: str
    ) -> ApplyMethodResult:
        system_blocks = [
            {"type": "text", "text": SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}},
        ]

        user_prompt = (
            "Analysiere diesen Job und gib das Bewerbungs-Profil zurück.\n\n"
            f"Title: {title}\n"
            f"Company: {company}\n"
            f"Location: {location}\n"
            f"URL: {url}\n\n"
            "Description:\n"
            f"{description or '(keine Beschreibung gescraped)'}\n\n"
            "Wenn weder Email noch Portal in der Description stehen, nutze "
            "web_search (1–2 Anfragen reichen) um die Firmen-Karriere-Seite "
            "zu finden und dort nach Bewerbungs-Email oder Portal zu schauen."
        )

        tools: list[dict] = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2,
            },
            {
                "name": "submit_apply_method",
                "description": "Submit the apply-method profile for this job.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "primary_channel": {
                            "type": "string", "enum": CHANNELS,
                            "description": "Wie soll man sich bewerben.",
                        },
                        "email": {
                            "type": ["string", "null"],
                            "description": "Bewerbungs-Email falls bekannt, sonst null.",
                        },
                        "portal_url": {
                            "type": ["string", "null"],
                            "description": "URL zum Portal (Workday/Greenhouse/...) falls vorhanden.",
                        },
                        "apply_url": {
                            "type": ["string", "null"],
                            "description": "Allgemeiner Apply-CTA-Link (kann == portal_url sein).",
                        },
                        "required_documents": {
                            "type": "array",
                            "items": {"type": "string", "enum": DOC_TYPES},
                            "description": "Welche Dokumente die Anzeige verlangt.",
                        },
                        "notes": {
                            "type": "string",
                            "description": "1-2 Sätze Constraints/Hinweise für den User.",
                        },
                    },
                    "required": [
                        "primary_channel", "email", "portal_url", "apply_url",
                        "required_documents", "notes",
                    ],
                },
            },
        ]

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=tools,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        tool_block = next(
            (b for b in response.content
             if b.type == "tool_use" and getattr(b, "name", "") == "submit_apply_method"),
            None,
        )
        if tool_block is None:
            types = [(b.type, getattr(b, "name", "")) for b in response.content]
            raise ValueError(f"No submit_apply_method tool_use; got: {types}")
        data = tool_block.input

        n_searches = sum(
            1 for b in response.content if b.type == "server_tool_use"
            and getattr(b, "name", "") == "web_search"
        )

        usage = response.usage
        input_tok = getattr(usage, "input_tokens", 0)
        output_tok = getattr(usage, "output_tokens", 0)
        cache_read_tok = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write_tok = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (
            input_tok * PRICE_INPUT_PER_MTOK / 1_000_000
            + cache_read_tok * PRICE_CACHE_READ_PER_MTOK / 1_000_000
            + cache_write_tok * PRICE_CACHE_WRITE_PER_MTOK / 1_000_000
            + output_tok * PRICE_OUTPUT_PER_MTOK / 1_000_000
        )
        logger.info(
            "Apply-method %s @ %s → %s (search %d, $%.4f)",
            title[:40], company, data["primary_channel"], n_searches, cost,
        )

        return ApplyMethodResult(
            primary_channel=data["primary_channel"],
            email=data.get("email"),
            portal_url=data.get("portal_url"),
            apply_url=data.get("apply_url"),
            required_documents=data.get("required_documents") or [],
            notes=data.get("notes", ""),
            web_searches=n_searches,
            input_tokens=input_tok,
            output_tokens=output_tok,
            cache_read_tokens=cache_read_tok,
            cache_write_tokens=cache_write_tok,
            cost_usd=cost,
        )
