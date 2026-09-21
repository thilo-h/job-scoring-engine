"""Company-type classifier — powers the startup/scale-up browse filter.

Haiku classifies each distinct company name into one of:

    startup     — early-stage, roughly < 50 people, pre-/seed-/A-funded
    scaleup     — growth-stage, roughly 50-500 people, later VC rounds
    sme         — classic small/mid businesses without the VC growth model
    enterprise  — large corporates, > ~500 people or listed
    unknown     — genuinely can't tell from the name + job context

Results are cached in the ``company_profiles`` table (one row per company),
so each company costs one classification ever. Companies are sent in batches
of ``BATCH_SIZE`` per API call to keep cost low.

Trigger from CLI:

    python -m src.main --profile example classify-companies [--limit 200] [--all-ages]
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.llm import get_client

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-haiku-4-5"
BATCH_SIZE = 20

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00

CATEGORIES = ["startup", "scaleup", "sme", "enterprise", "unknown"]

INDUSTRIES = [
    "saas_software", "ai_data", "fintech", "climate_energy",
    "health_biotech", "mobility_logistics", "ecommerce_marketplace",
    "beauty_fmcg", "proptech_construction", "industrial_hardware",
    "consulting_services", "finance_vc", "media_creative",
    "public_education", "other",
]

_SYSTEM = (
    "You classify companies by size/stage AND industry for a job-search tool. "
    "You get a list of company names with sample job titles and posting "
    "locations from the user's scraped job database. For each company return "
    "a category:\n"
    "- startup: early-stage venture, roughly under 50 employees\n"
    "- scaleup: venture-backed growth company, roughly 50-500 employees "
    "(e.g. later-stage unicorns that still operate like growth companies)\n"
    "- sme: established small/mid-sized business without the VC model "
    "(agencies, consultancies, local firms, hospitals, public sector)\n"
    "- enterprise: large corporation, over ~500 employees or publicly listed\n"
    "- unknown: you genuinely don't recognize it and the context doesn't help\n"
    "And an industry vertical (pick the dominant one):\n"
    "- saas_software: B2B/B2C software products, dev tools, platforms\n"
    "- ai_data: AI/ML products, foundation models, data infrastructure\n"
    "- fintech: banking, payments, insurance, wealth, crypto\n"
    "- climate_energy: climate tech, carbon, renewables, utilities, energy\n"
    "- health_biotech: healthcare, medtech, pharma, biotech, wellness\n"
    "- mobility_logistics: transport, automotive, delivery, freight, travel\n"
    "- ecommerce_marketplace: online retail, marketplaces, consumer platforms\n"
    "- beauty_fmcg: beauty, cosmetics, fragrance, food, consumer goods\n"
    "- proptech_construction: real estate, construction, building tech\n"
    "- industrial_hardware: manufacturing, robotics, hardware, engineering\n"
    "- consulting_services: consultancies, agencies, IT services, staffing\n"
    "- finance_vc: VC/PE funds, asset management, classic banking-adjacent\n"
    "- media_creative: media, entertainment, marketing, design\n"
    "- public_education: government, NGOs, universities, schools, hospitals (public)\n"
    "- other: none of the above fits\n"
    "Use your world knowledge for companies you recognize. For unknown names, "
    "infer carefully from job titles and locations — or say 'unknown'/'other'. "
    "Be honest with confidence: 0.9+ only for companies you clearly know."
)

_TOOL = {
    "name": "classify_companies",
    "description": "Return one classification per company, same order as given.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "industry": {"type": "string", "enum": INDUSTRIES},
                        "confidence": {
                            "type": "number", "minimum": 0, "maximum": 1,
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Very short justification (a few words).",
                        },
                    },
                    "required": ["company", "category", "industry", "confidence"],
                },
            },
        },
        "required": ["classifications"],
    },
}


@dataclass
class ClassifySummary:
    classified: int = 0
    by_category: dict = None
    api_calls: int = 0
    cost_usd: float = 0.0

    def __post_init__(self):
        if self.by_category is None:
            self.by_category = {}


def _client():
    return get_client(purpose="company classification")


def classify_companies(
    db, *, limit: int = 200, fresh_days: Optional[int] = 30,
    anthropic_client=None,
) -> ClassifySummary:
    """Classify up to ``limit`` unclassified companies and persist results."""
    candidates = db.companies_needing_classification(limit=limit, fresh_days=fresh_days)
    summary = ClassifySummary()
    if not candidates:
        logger.info("classify-companies: nothing to classify")
        return summary

    client = anthropic_client or _client()

    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        lines = []
        for c in batch:
            titles = "; ".join(t.strip()[:80] for t in c["sample_titles"] if t.strip())
            lines.append(
                f"- {c['company']} | jobs in DB: {c['n_jobs']} | "
                f"sample titles: {titles or '(none)'} | locations: {c['locations'] or '(none)'}"
            )
        user = "Classify these companies:\n" + "\n".join(lines)

        # Rate-limit friendly: retry with backoff instead of dropping the
        # batch — sequential Haiku calls can still trip ITPM limits when
        # several classify runs happen close together.
        response = None
        for attempt, delay in enumerate((0, 10, 30, 60)):
            if delay:
                time.sleep(delay)
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=2000,
                    system=_SYSTEM,
                    tools=[_TOOL],
                    tool_choice={"type": "tool", "name": "classify_companies"},
                    messages=[{"role": "user", "content": user}],
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "classify batch failed (attempt %d/4): %s", attempt + 1, exc
                )
        if response is None:
            continue

        summary.api_calls += 1
        summary.cost_usd += (
            response.usage.input_tokens * PRICE_INPUT_PER_MTOK / 1_000_000
            + response.usage.output_tokens * PRICE_OUTPUT_PER_MTOK / 1_000_000
        )

        results = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                results = block.input.get("classifications", [])
                break

        # Match on company name; Haiku echoes names back, but guard against
        # mangled echoes by falling back to batch order when lengths align.
        wanted = {c["company"] for c in batch}
        by_name = {r.get("company", ""): r for r in results}
        for idx, c in enumerate(batch):
            r = by_name.get(c["company"])
            if r is None and len(results) == len(batch):
                r = results[idx]
            if r is None:
                continue
            category = r.get("category", "unknown")
            if category not in CATEGORIES:
                category = "unknown"
            industry = r.get("industry", "other")
            if industry not in INDUSTRIES:
                industry = "other"
            db.save_company_profile(
                c["company"],
                category=category,
                industry=industry,
                confidence=float(r.get("confidence") or 0),
                reasoning=(r.get("reasoning") or "")[:300],
            )
            summary.classified += 1
            summary.by_category[category] = summary.by_category.get(category, 0) + 1
        # Drop names Haiku invented that weren't asked about.
        _ = wanted

    logger.info(
        "classify-companies: %d classified in %d call(s), $%.4f — %s",
        summary.classified, summary.api_calls, summary.cost_usd, summary.by_category,
    )
    return summary
