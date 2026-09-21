"""Job-quality triager — a holistic model judgement on whether a posting fits.

Distinct from ``relevance_score`` (deterministic, keyword/tier-based, 0–1).
This one is holistic and judgmental, scored 0–10, with a short reason plus
strengths and gaps. It exists so the applicant's attention goes to the few
postings that genuinely fit, rather than to the top of an unfiltered list.

Uses Haiku 4.5 for cost/speed (~$0.001 per check).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.llm import get_client

# Regex für XML-style "<item>foo</item><item>bar</item>" Quirk —
# manche Haiku-Outputs ignorieren das Array-Schema und packen alles in
# einen einzigen String mit Tags. Wir parsen das defensiv zurück.
_XML_ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)


def _normalize_to_list(value, max_items: int = 5) -> list[str]:
    """Normalize strengths/gaps to a clean list[str], egal was das Modell gibt.

    Handles:
      - Echtes Array: ["foo", "bar"] → ["foo", "bar"]
      - XML-Wrapped String: "<item>foo</item><item>bar</item>" → ["foo", "bar"]
      - Bullet-Liste als String: "- foo\\n- bar" → ["foo", "bar"]
      - Single String: "foo" → ["foo"]
      - None / leer → []
    """
    if value is None:
        return []
    # Schon Liste: säubern und trimmen
    if isinstance(value, list):
        cleaned = [str(x).strip() for x in value if str(x).strip()]
        return cleaned[:max_items]
    # String: drei Parse-Strategien probieren
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        # Strategie 1: XML-style <item>...</item>
        items = _XML_ITEM_RE.findall(s)
        if items:
            return [i.strip() for i in items if i.strip()][:max_items]
        # Strategie 2: Bullet-Liste oder zeilenweise
        lines = [
            re.sub(r"^[-•*\d+\.\s]+", "", line).strip()
            for line in s.split("\n") if line.strip()
        ]
        lines = [line for line in lines if line]
        if len(lines) > 1:
            return lines[:max_items]
        # Strategie 3: kommagetrennt
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) > 1:
            return parts[:max_items]
        # Single Item — als 1-Element-Liste zurück
        return [s]
    # Anderes (z.B. dict) — leere Liste, sicherer Fallback
    return []

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CV_PATH = ROOT / "assets" / "example" / "cv_example.md"

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 600

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_CACHE_WRITE_PER_MTOK = 1.25


def _build_system_prompt(name: str, background: str) -> str:
    bg = background.strip() or (
        f"{name} is Junior/Associate-Level. See CV below for details."
    )
    return f"""You are a critical recruiter helping {name} with their job
search. You assess whether a posting genuinely fits their profile — be
honest, not friendly.

**OUTPUT LANGUAGE: ALWAYS respond in ENGLISH**, regardless of the language
of the job posting. The user reads English fluently and prefers a consistent
output language for filtering.

Score scale (0–10):
- 10 = perfect match (skills, seniority level, location, language, domain)
- 8–9 = very good fit, small bridgeable gaps
- 6–7 = solid fit with clear weaknesses (e.g. one tier-class too high/low)
- 4–5 = possible but with significant gaps (wrong domain, multiple required skills missing)
- 0–3 = clear mismatch (Senior role, wrong industry, wrong language)

Profile background:
{bg}

Evaluation factors:
- Seniority level: postings with "5+ years experience" or "Senior" → max 5
  (unless the background summary says otherwise).
- Domain: must match the background.
- Location, language, tech stack: match against the CV below.

Be critical rather than encouraging. A generous score costs the applicant a
wasted application, so when the fit is weak, prefer the low score and say why.
If the profile simply doesn't fit, say so clearly.
"""


@dataclass
class TriageResult:
    score: float
    reason: str
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    # Telemetry
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


class JobTriager:
    def __init__(self, api_key: Optional[str] = None, *, config: Optional[dict] = None):
        self.client = get_client(api_key, purpose="job triage")

        assets = (config or {}).get("assets") or {}
        cv_path = ROOT / assets.get("cv_md", DEFAULT_CV_PATH.relative_to(ROOT))

        profile = (config or {}).get("profile") or {}
        self._profile_name = profile.get("name", "Alex Muster")
        self._background = profile.get("background_summary", "") or ""
        self._system_prompt = _build_system_prompt(self._profile_name, self._background)

        self._cv_text = cv_path.read_text(encoding="utf-8")

    def triage(
        self, *, title: str, company: str, location: str, description: str
    ) -> TriageResult:
        system_blocks = [
            {"type": "text", "text": self._system_prompt,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text",
             "text": f"## Lebenslauf von {self._profile_name}:\n\n" + self._cv_text,
             "cache_control": {"type": "ephemeral"}},
        ]

        user_prompt = (
            f"Assess the following posting for {self._profile_name}.\n"
            f"Respond in ENGLISH (all fields: reason, strengths, gaps).\n\n"
            f"Title: {title}\n"
            f"Company: {company}\n"
            f"Location: {location}\n\n"
            "Description:\n"
            f"{description or '(no description scraped — work from title/company/location only)'}"
        )

        tool = {
            "name": "submit_match_score",
            "description": "Submit the match-quality assessment for this job. ALL TEXT FIELDS MUST BE IN ENGLISH.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "score": {
                        "type": "number", "minimum": 0, "maximum": 10,
                        "description": "Match score 0-10, half-points OK (e.g. 6.5).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "2-3 sentences explaining the score. **Write in English.**",
                    },
                    "strengths": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "1-3 concrete match points. **Each item MUST be a separate string in the array** — "
                            "do NOT concatenate into one string, do NOT use XML/HTML tags like <item>. "
                            "Write each in English, one sentence."
                        ),
                    },
                    "gaps": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "1-3 concrete gaps/risks. **Each item MUST be a separate string in the array** — "
                            "do NOT concatenate into one string, do NOT use XML/HTML tags like <item>. "
                            "Write each in English, one sentence."
                        ),
                    },
                },
                "required": ["score", "reason", "strengths", "gaps"],
            },
        }

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_match_score"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        tool_block = next(
            (b for b in response.content
             if b.type == "tool_use" and getattr(b, "name", "") == "submit_match_score"),
            None,
        )
        if tool_block is None:
            raise ValueError("No submit_match_score tool_use in response")
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
            "Triage %s @ %s → score=%.1f, $%.4f",
            title[:50], company, float(data["score"]), cost,
        )

        return TriageResult(
            score=float(data["score"]),
            reason=data["reason"],
            strengths=_normalize_to_list(data.get("strengths")),
            gaps=_normalize_to_list(data.get("gaps")),
            input_tokens=input_tok,
            output_tokens=output_tok,
            cache_read_tokens=cache_read_tok,
            cache_write_tokens=cache_write_tok,
            cost_usd=cost,
        )
