"""Smart-Filter Agent — natural language → browse query parameters.

User types something like ``"Junior PM in Barcelona, kein Französisch,
vorzugsweise Startup"`` into the sidebar; Claude returns a structured filter
that drives the existing browse table. Mirrors :func:`query_jobs` so the
mapping to the table refresh is trivial.

Uses Sonnet 4.6 with tool-use. The whole point of structured output here is
to never hallucinate filter keys — the tool schema enumerates exactly what's
valid (sources, statuses, exp_band, sort columns).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.llm import get_client

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-sonnet-5"
# Adaptive thinking is on by default on this model, and max_tokens caps thinking
# plus response together. The old 600 was sized for a thinking-off model and
# would truncate the tool call; this leaves room for both.
MAX_TOKENS = 2500
# The task is a schema-constrained extraction, not a reasoning problem: the tool
# schema already enumerates every legal value. `low` keeps the button responsive
# and the cost down. Raise it if the parsed filters start missing the intent.
EFFORT = "low"

PRICE_INPUT_PER_MTOK = 2.00
PRICE_OUTPUT_PER_MTOK = 10.00
PRICE_CACHE_READ_PER_MTOK = 0.20   # 10% of input
PRICE_CACHE_WRITE_PER_MTOK = 2.50  # 1.25x input


# These match the browse.py / query_jobs allowed values. If you add a new
# filter field there, also add it here.
VALID_SORTS = ["score", "date", "company", "title", "location", "workload", "source", "last_seen", "status"]
VALID_DIRECTIONS = ["desc", "asc"]
VALID_EXP_BANDS = ["", "0-1", "1-3", "3-5", "5plus", "unknown"]
VALID_REMOTES = ["", "yes", "no"]
VALID_STATUSES = ["new", "bookmarked", "applied", "interview", "offer", "rejected", "ignored"]
VALID_COMPANY_TYPES = ["", "startup", "scaleup", "startup_scaleup"]
VALID_INDUSTRIES = [
    "", "saas_software", "ai_data", "fintech", "climate_energy",
    "health_biotech", "mobility_logistics", "ecommerce_marketplace",
    "beauty_fmcg", "proptech_construction", "industrial_hardware",
    "consulting_services", "finance_vc", "media_creative",
    "public_education", "other",
]


@dataclass
class SmartFilterResult:
    # Mirrors the browse form. Empty / None means "no filter for this field".
    q: str = ""
    sources: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    location: str = ""
    min_score: Optional[float] = None
    workload_min: Optional[int] = None
    workload_max: Optional[int] = None
    exp_band: str = ""
    remote: str = ""
    presence_ch_es: bool = False
    company_type: str = ""
    industry: str = ""
    sort: str = "score"
    direction: str = "desc"
    reasoning: str = ""
    # Telemetry
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "q": self.q,
            "sources": self.sources,
            "statuses": self.statuses,
            "location": self.location,
            "min_score": self.min_score,
            "workload_min": self.workload_min,
            "workload_max": self.workload_max,
            "exp_band": self.exp_band,
            "remote": self.remote,
            "presence_ch_es": self.presence_ch_es,
            "company_type": self.company_type,
            "industry": self.industry,
            "sort": self.sort,
            "direction": self.direction,
            "reasoning": self.reasoning,
        }


def _build_system_prompt(profile_name: str, background: str, available_sources: list[str]) -> str:
    bg = background.strip()
    src_list = ", ".join(available_sources) if available_sources else "(none)"
    return f"""Du bist ein Filter-Assistent für eine Job-Suche-Datenbank.
Der User filtert auf dem "Browse"-Tab ihre eigene Job-DB ({profile_name}).
Konvertiere die natürlichsprachliche Anfrage in strukturierte Query-Parameter
und gib sie über das `apply_filter`-Tool zurück.

Hintergrund zum User:
{bg or "(no background summary set)"}

Verfügbare `sources` in dieser DB: {src_list}.

Regeln:
- `q` ist eine LIKE-Suche über title/company/description. Nutze sie für
  ein einziges spezifisches Keyword (z.B. "Process Mining", "Climate").
  Nicht für Standorte oder Sprachen — die haben eigene Felder.
- `location` ist eine LIKE-Suche über location. "Barcelona", "BCN",
  "Zürich" sind ok. Mehrere Städte: komma-getrennt angeben — sie werden
  ODER-verknüpft (z.B. "Zürich, Barcelona").
- `presence_ch_es`: true wenn der User nur Firmen sehen will die sowohl in
  der Schweiz als auch in Spanien Stellen ausschreiben ("Firmen mit Büro in
  beiden Ländern", "operiert in CH und ES"). Sonst false/weglassen.
- `company_type`: "startup" / "scaleup" / "startup_scaleup" wenn der User
  explizit nach Startups bzw. Scale-ups fragt ("vorzugsweise Startup" →
  "startup_scaleup" wenn unklar welche Größe gemeint ist). Sonst leer.
- `industry`: Branche/Vertical der FIRMA (nicht der Rolle!). "ClimateTech-
  Firmen" → "climate_energy". "Jobs bei Banken/Fintechs" → "fintech".
  Aber: "PM-Stellen" ist eine Rolle, keine Branche → industry leer lassen
  und stattdessen `q` nutzen. Sonst leer.
- `min_score` ist 0–1 (relevance_score). User-Begriff "gute Treffer" ≈ 0.4.
- `exp_band`: nur exakt einer der Werte ["0-1", "1-3", "3-5", "5plus",
  "unknown"]. "Junior" → "0-1" oder "1-3". "Mid-Level" → "3-5".
- `sources`: nur die oben gelisteten Werte. Leere Liste = alle.
- `statuses`: ["new", "bookmarked", "applied", "interview", "offer",
  "rejected", "ignored"]. Leere Liste = Default (alles außer ignored).
- `remote`: "yes" / "no" / "". User sagt "remote" → "yes". "vor Ort" → "no".
- `workload_min`/`max`: in Prozent (0-100). "Teilzeit" → max=80. "80%" → min=80.
- `sort` und `direction`: Default "score"/"desc". User sagt "neueste" → "date"/"desc".

Wenn die Anfrage mehrdeutig oder unsinnig ist: setze nur die Felder die
eindeutig sind und lass den Rest weg (oder leer/null). Im `reasoning`
kurz (1 Satz) erklären was du gemacht hast.

WICHTIG: Wende NUR Filter an die der User explizit oder implizit gefordert hat.
Füge keine "hilfreichen" Default-Filter hinzu die der User nicht wollte.
"""


class SmartFilter:
    def __init__(self, api_key: Optional[str] = None, *, config: Optional[dict] = None):
        self.client = get_client(api_key, purpose="the smart filter")

        profile = (config or {}).get("profile") or {}
        self._profile_name = profile.get("name", "User")
        self._background = profile.get("background_summary", "")

    def interpret(
        self, prompt: str, *, available_sources: list[str]
    ) -> SmartFilterResult:
        """Convert a natural-language query into a SmartFilterResult."""
        system_prompt = _build_system_prompt(
            self._profile_name, self._background, available_sources
        )

        tool = {
            "name": "apply_filter",
            "description": "Apply the filter parameters derived from the user's prompt.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "Free-text search over title/company/description. Leave empty unless the user asked for a specific keyword.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Single city or region (LIKE match). Empty if no location given.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": available_sources or [""]},
                        "description": "Which scrapers to include. Empty = all.",
                    },
                    "statuses": {
                        "type": "array",
                        "items": {"type": "string", "enum": VALID_STATUSES},
                        "description": "Application statuses to include. Empty = all except ignored.",
                    },
                    "min_score": {
                        "type": ["number", "null"],
                        "minimum": 0, "maximum": 1,
                        "description": "Minimum relevance score 0-1. null if not constrained.",
                    },
                    "workload_min": {
                        "type": ["integer", "null"],
                        "minimum": 0, "maximum": 100,
                        "description": "Minimum workload percentage. null = no constraint.",
                    },
                    "workload_max": {
                        "type": ["integer", "null"],
                        "minimum": 0, "maximum": 100,
                        "description": "Maximum workload percentage. null = no constraint.",
                    },
                    "exp_band": {
                        "type": "string", "enum": VALID_EXP_BANDS,
                        "description": "Required experience band. Empty = any.",
                    },
                    "remote": {
                        "type": "string", "enum": VALID_REMOTES,
                        "description": "Remote / on-site constraint. Empty = any.",
                    },
                    "presence_ch_es": {
                        "type": "boolean",
                        "description": "true = only companies posting jobs in BOTH Switzerland and Spain. Default false.",
                    },
                    "company_type": {
                        "type": "string", "enum": VALID_COMPANY_TYPES,
                        "description": "Restrict to startups and/or scale-ups. Empty = any company type.",
                    },
                    "industry": {
                        "type": "string", "enum": VALID_INDUSTRIES,
                        "description": "Industry vertical of the company (not the role!). Empty = any.",
                    },
                    "sort": {
                        "type": "string", "enum": VALID_SORTS,
                        "description": "Sort column. Default 'score'.",
                    },
                    "direction": {
                        "type": "string", "enum": VALID_DIRECTIONS,
                        "description": "Sort direction. Default 'desc'.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "One short sentence explaining what you set and why.",
                    },
                },
                "required": ["reasoning"],
            },
        }

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "apply_filter"},
            messages=[{"role": "user", "content": prompt}],
        )

        tool_block = next(
            (b for b in response.content
             if b.type == "tool_use" and getattr(b, "name", "") == "apply_filter"),
            None,
        )
        if tool_block is None:
            raise ValueError("Claude did not return apply_filter tool_use")
        data = tool_block.input

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
            "smart_filter '%s' → %s (cost $%.4f)",
            prompt[:60], data.get("reasoning", "")[:80], cost,
        )

        return SmartFilterResult(
            q=data.get("q") or "",
            sources=data.get("sources") or [],
            statuses=data.get("statuses") or [],
            location=data.get("location") or "",
            min_score=data.get("min_score"),
            workload_min=data.get("workload_min"),
            workload_max=data.get("workload_max"),
            exp_band=data.get("exp_band") or "",
            remote=data.get("remote") or "",
            presence_ch_es=bool(data.get("presence_ch_es")),
            company_type=(
                data.get("company_type")
                if data.get("company_type") in VALID_COMPANY_TYPES else ""
            ) or "",
            industry=(
                data.get("industry")
                if data.get("industry") in VALID_INDUSTRIES else ""
            ) or "",
            sort=data.get("sort") or "score",
            direction=data.get("direction") or "desc",
            reasoning=data.get("reasoning", ""),
            input_tokens=input_tok,
            output_tokens=output_tok,
            cache_read_tokens=cache_read_tok,
            cache_write_tokens=cache_write_tok,
            cost_usd=cost,
        )
