"""Extract a job's workload percentage from its title + description.

Most scrapers don't fill `Job.workload_percent` because the source APIs don't
expose it as a structured field. The information is, however, almost always
present in the posting text — usually as a range like ``"60–80%"`` or with a
keyword like ``"Pensum: 80%"``.

This module turns that free-text signal into an integer percentage. It's
intentionally conservative: it skips ambiguous standalone percentages
("100% remote", "+20% growth") to avoid false positives. Returns ``None`` when
no workload signal is found — the dashboard treats NULL as "unknown" and shows
the job regardless of the workload filter.

Convention: when a range is found, return the **minimum** of the range, since
the user typically asks "max 80%?" meaning "is at most 80% offered?". A job
posted as "60–100%" therefore stores 60 — passes a "<= 80" filter and is
visible to the user, who can confirm in the description.
"""

from __future__ import annotations

import re
from typing import Optional

# Range like "60-80%", "80 – 100%", "60–80 %"
RANGE_PATTERN = re.compile(r"(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*[%‰]")

# Single percentage attached to a workload keyword (high precision).
#
# The employment-type words (Teilzeit, Vollzeit, part-time, …) are in here as
# well as in the keyword fallbacks further down, and the order matters: when a
# posting writes "Teilzeit 50 %", the explicit figure has to win over the
# fallback's assumption about what Teilzeit usually means. The 8-character
# window between keyword and number is what keeps this precise — it matches
# "Arbeitszeit: 80%" and "Teilzeit, 50%" but not "Teilzeit möglich. 20 % …".
KEYWORD_PCT_PATTERN = re.compile(
    r"(?:Pensum|Workload|Beschäftigungsgrad|Stellenprozent|Arbeitspensum|"
    r"Arbeitszeit|Anstellungsgrad|Anstellung|Teilzeit|Vollzeit|"
    r"part[\s-]?time|full[\s-]?time|jornada|tiempo\s+parcial)"
    r"[^\d\n]{0,8}(\d{2,3})\s*[%‰]",
    re.IGNORECASE,
)

# Standalone percentage followed by a workload keyword.
PCT_FIRST_PATTERN = re.compile(
    r"(\d{2,3})\s*[%‰]\s*(?:Pensum|Stelle|Anstellung|Teilzeit|Vollzeit|jornada)",
    re.IGNORECASE,
)

# "ab 60%" / "from 60%" / "desde 60%" — common in postings like
# "Vollzeit oder Teilzeit ab 60%". The number after the keyword is the floor,
# so use it directly.
AB_PCT_PATTERN = re.compile(
    r"\b(?:ab|from|desde)\s+(\d{2,3})\s*[%‰]",
    re.IGNORECASE,
)


def extract_workload(text: Optional[str]) -> Optional[int]:
    """Return the workload percentage hinted at by ``text``, or None."""
    if not text:
        return None

    # 1. Range (very reliable)
    for m in RANGE_PATTERN.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 10 <= lo <= hi <= 100:
            return lo

    # 2. Keyword-anchored percentage (e.g., "Pensum: 80%")
    for m in KEYWORD_PCT_PATTERN.finditer(text):
        v = int(m.group(1))
        if 10 <= v <= 100:
            return v

    # 3. Pct-first form ("80% Pensum", "80% jornada completa")
    for m in PCT_FIRST_PATTERN.finditer(text):
        v = int(m.group(1))
        if 10 <= v <= 100:
            return v

    # 4. "ab/from/desde N%" — captures floor of "Vollzeit oder Teilzeit ab 60%"
    for m in AB_PCT_PATTERN.finditer(text):
        v = int(m.group(1))
        if 10 <= v <= 100:
            return v

    # 5. Keyword fallbacks (no number nearby)
    lower = text.lower()
    if "working student" in lower or "werkstudent" in lower:
        return 40  # typical DACH ceiling for working students during semester
    if "internship" in lower or "praktikum" in lower or "intern " in lower:
        return 100
    if "vollzeit" in lower or "full-time" in lower or "full time" in lower or "tiempo completo" in lower:
        return 100
    if "teilzeit" in lower or "part-time" in lower or "part time" in lower or "tiempo parcial" in lower:
        # Default Teilzeit ≈ 60% in DACH; safer than skipping entirely.
        return 60

    return None


def extract_for_job(*, title: Optional[str], description: Optional[str]) -> Optional[int]:
    """Try title first (less noisy), fall back to first 1500 chars of description."""
    return (
        extract_workload(title)
        or extract_workload((description or "")[:1500])
    )


def extract_workload_phrase(title: Optional[str], description: Optional[str]) -> Optional[str]:
    """Pensum so, wie es in der Anzeige steht: "80–100" bei einer Spanne, "80" sonst.

    Anders als ``extract_for_job`` (liefert das Minimum für den Filter) bleibt
    die Spanne erhalten — das Anschreiben soll exakt "40–60 %" nennen, wenn die
    Anzeige das tut. Titel zuerst, dort steht das Pensum am verlässlichsten.
    """
    from src.experience_extractor import unescape_markdown

    for raw in (title, (description or "")[:8000]):
        text = unescape_markdown(raw or "")
        if not text:
            continue
        for m in RANGE_PATTERN.finditer(text):
            lo, hi = int(m.group(1)), int(m.group(2))
            if 10 <= lo < hi <= 100:
                return f"{lo}–{hi}"
        for pattern in (KEYWORD_PCT_PATTERN, PCT_FIRST_PATTERN, AB_PCT_PATTERN):
            for m in pattern.finditer(text):
                v = int(m.group(1))
                if 10 <= v <= 100:
                    return str(v)
    return None
