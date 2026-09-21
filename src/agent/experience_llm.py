"""LLM-Fallback für min_years_experience-Extraktion.

Der Regex-Extractor (:mod:`src.experience_extractor`) findet ~7-8% der Jobs.
Der Rest braucht Sprach-Verständnis — implizite Aussagen ("Senior PM mit
Track-Record im SaaS-Bereich" → vermutlich 5+), Umschreibungen ("solide
Erfahrung"), mehrere Anforderungs-Blöcke.

Dieser Agent benutzt Haiku 4.5 (~$0.0005/check) für genau diese Fälle.
Returns:
  - Integer >= 0 wenn klares Signal gefunden
  - None wenn auch der LLM kein verlässliches Signal sieht (keine Halluzinationen)

System-Prompt ist gecached, also linearer Cost-Verlauf über N Jobs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.llm import get_client

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 200

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_CACHE_WRITE_PER_MTOK = 1.25


SYSTEM_PROMPT = """Du extrahierst aus Job-Inseraten die geforderte Mindest-Berufserfahrung in Jahren.

Aufgabe: Lies das Inserat und gib die **Mindest-Jahre Berufserfahrung** an, die
der Arbeitgeber explizit oder implizit verlangt.

Konventionen:
- Bei Range (z.B. "3-5 years") → niedrigerer Wert (= Mindesteinstieg).
- Bei "5+ years" → 5.
- Bei Entry-Level / Praktikum / Werkstudent / Berufseinsteiger / Graduate → 0.
- Bei klar Senior-Sprache ("seasoned", "extensive experience", "Senior", "Lead",
  "proven track record") ohne explizite Zahl → 5 (typische Senior-Schwelle).
- Bei klar Mid-Level Sprache ("solide Erfahrung", "established") ohne Zahl → 3.
- Bei Junior ohne Zahl → 1.

Wenn das Inserat KEIN Signal über Berufserfahrung enthält (z.B. nur Description
von Aufgaben, kein Wort über Anforderungs-Profil) → gib `null` zurück. Nicht raten.

Sei konservativ: lieber null zurück geben als eine Zahl zu erfinden. Du
hilfst beim Filtern — falsche Zahlen sind schlechter als "unbekannt"."""


@dataclass
class ExperienceLLMResult:
    min_years: Optional[int]
    confidence: str  # "explicit" | "implicit" | "unclear"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


class ExperienceLLMExtractor:
    def __init__(self, api_key: Optional[str] = None):
        self.client = get_client(api_key, purpose="experience extraction")

    def extract(
        self,
        *,
        title: str,
        company: str = "",
        description: str = "",
    ) -> ExperienceLLMResult:
        system_blocks = [
            {"type": "text", "text": SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}},
        ]

        # Cap description to keep token usage tight — 2000 chars covers
        # almost all "requirements"/"profile" sections of postings.
        desc_clipped = (description or "")[:2000]
        user_prompt = (
            f"Titel: {title}\n"
            f"Firma: {company}\n\n"
            f"Inserat-Text:\n{desc_clipped or '(keine Description verfügbar)'}"
        )

        tool = {
            "name": "submit_experience",
            "description": "Reportet die geforderte Mindest-Berufserfahrung.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "min_years": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 30,
                        "description": (
                            "Mindest-Jahre Berufserfahrung. null wenn das "
                            "Inserat kein klares Signal enthält."
                        ),
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["explicit", "implicit", "unclear"],
                        "description": (
                            "'explicit' = Zahl explizit genannt; "
                            "'implicit' = aus Seniority-Level abgeleitet; "
                            "'unclear' = kein Signal."
                        ),
                    },
                },
                "required": ["min_years", "confidence"],
            },
        }

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_experience"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        tool_block = next(
            (b for b in response.content
             if b.type == "tool_use" and getattr(b, "name", "") == "submit_experience"),
            None,
        )
        if tool_block is None:
            raise ValueError("No submit_experience tool_use in response")
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

        # Haiku ignoriert manchmal "type: integer" und liefert str/float.
        # Coercion hier hält den Rest der Pipeline (Counter-Sort, DB-Filter)
        # type-clean. None bleibt None.
        raw = data.get("min_years")
        if raw is None:
            min_years: Optional[int] = None
        else:
            try:
                min_years = int(float(raw))
            except (TypeError, ValueError):
                min_years = None

        return ExperienceLLMResult(
            min_years=min_years,
            confidence=data.get("confidence", "unclear"),
            input_tokens=input_tok,
            output_tokens=output_tok,
            cache_read_tokens=cache_read_tok,
            cache_write_tokens=cache_write_tok,
            cost_usd=cost,
        )
