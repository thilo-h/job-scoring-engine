"""Extract a job's required minimum years of experience from title + description.

Most scrapers don't fill a structured experience field — the info sits in the
posting text as "5+ years experience", "mindestens 3 Jahre Berufserfahrung",
"mínimo 2 años de experiencia", and similar variants.

This module turns that free text into an integer = the **minimum** years
required. The convention mirrors :mod:`workload_extractor`: a range like
"3-5 years" stores 3, because the user's question is typically "is the floor
low enough for my background?". Returns ``None`` when no signal is found —
the dashboard treats NULL as "unknown" and lets the user opt into showing
unknown rows.

Anchor strategy: numbers are only accepted when they appear together with an
**experience keyword** (``experience``, ``Erfahrung``, ``experiencia``, etc.).
This avoids capturing irrelevant numbers like "founded 5 years ago" or
"100-year-old company".
"""

from __future__ import annotations

import re
from typing import Optional

# Word that signals "this number describes required experience".
# IT (anni di esperienza) included but lower-priority for our markets.
_EXP_KW = (
    r"(?:experience|exp\.|Erfahrung|Berufserfahrung|Berufspraxis|"
    r"experiencia\s+(?:laboral|profesional|previa)?|experiencia|"
    r"esperienza|anni\s+di\s+lavoro)"
)

# The tail that follows the number in all three year patterns. Two shapes are
# accepted:
#
#   "5 years experience"                     — keyword directly after the unit
#   "5 years of professional experience"     — connector, then up to two words
#
# The adjective slot is allowed *only* after an explicit of/de/di. Without that
# guard, "we opened 5 years ago, and experience shows …" would parse as a
# five-year requirement. Requiring the connector costs nothing real: every
# posting that puts an adjective there also writes "of".
_EXP_TAIL = (
    rf"\s*(?:(?:of|de|di)\s+(?:[A-Za-zÄÖÜäöüß][\w-]*\s+){{0,2}})?{_EXP_KW}"
)

# "5+ years experience" / "5+ Jahre Erfahrung" / "5+ años experiencia"
PLUS_YEARS_RE = re.compile(
    rf"(?:^|\W)(\d{{1,2}})\s*\+\s*(?:years?|yrs?|Jahre|años|anni)"
    rf"{_EXP_TAIL}",
    re.IGNORECASE,
)

# "3-5 years experience" / "3 to 5 years experience" / "3-5 Jahre Erfahrung"
RANGE_YEARS_RE = re.compile(
    rf"(?:^|\W)(\d{{1,2}})\s*(?:[-–—]|to|bis|a)\s*(\d{{1,2}})\s*"
    rf"(?:years?|yrs?|Jahre|años|anni)"
    rf"{_EXP_TAIL}",
    re.IGNORECASE,
)

# "minimum 2 years" / "at least 3 years" / "mindestens 3 Jahre" / "mínimo 2 años"
MIN_YEARS_RE = re.compile(
    r"(?:minimum|at\s+least|min\.?|mindestens|mind\.|m[íi]nimo|al\s+menos|almeno)"
    r"\s+(?:of\s+|de\s+)?(\d{1,2})\s*(?:years?|yrs?|Jahre|años|anni)",
    re.IGNORECASE,
)

# "5 years of experience" / "5 years experience" — without "+"
# Stricter than PLUS_YEARS_RE since no "+" signal means "exactly" rather than "at least".
# Still anchored to experience keyword to avoid false positives.
YEARS_OF_EXP_RE = re.compile(
    rf"(?:^|\W)(\d{{1,2}})\s*(?:years?|yrs?|Jahre|años|anni)"
    rf"{_EXP_TAIL}",
    re.IGNORECASE,
)

# Entry-level signals → min = 0. Note "junior" alone is too noisy
# (some firms use "Junior PM" for 2-3 yrs roles) so it's not in here.
ENTRY_LEVEL_RE = re.compile(
    r"\b("
    r"entry[\s-]?level|no\s+experience\s+required|"
    r"new\s+grad(?:uate)?|fresh\s+graduate|recent\s+graduate|"
    r"Berufseinsteiger|ohne\s+Berufserfahrung|Absolvent(?:en)?|Hochschulabsolvent|"
    r"sin\s+experiencia|reci[eé]n\s+(?:graduado|titulado)|"
    r"working\s+student|Werkstudent|"
    r"internship|Praktikum|practicas?"
    r")\b",
    re.IGNORECASE,
)


# LinkedIn- und Indeed-Descriptions kommen als Markdown mit escapten Zeichen:
# "5\+ years", "full\-time", "R\&D". Das "\" vor dem "+" bricht PLUS_YEARS_RE —
# Ohne das Entschärfen bleibt min_years bei einem grossen Teil der Inserate
# aus solchen Quellen leer, obwohl die Zahl im Text steht.
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!&|~<>])")


# Mehr als 15 Jahre ist in diesen Inseraten keine Anforderung, sondern das
# Firmenalter: "consultoría con más de 30 años de experiencia", "our division
# relies on over 30 years of experience". Ohne Deckel landet eine
# Junior-Stelle auf 30 Jahren und damit auf Score 0.00. Echte 15+-Stellen
# tragen Director/Head/VP im Titel und fallen dort über die Titel-Penalty.
MAX_PLAUSIBLE_YEARS = 15


def unescape_markdown(text: Optional[str]) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\1", text or "")


def extract_min_years(text: Optional[str]) -> Optional[int]:
    """Return the minimum required years of experience hinted at, or None."""
    if not text:
        return None
    text = unescape_markdown(text)

    candidates: list[int] = []

    # 1. "5+ years experience" — strong signal, value = floor
    for m in PLUS_YEARS_RE.finditer(text):
        v = int(m.group(1))
        if 0 <= v <= MAX_PLAUSIBLE_YEARS:
            candidates.append(v)

    # 2. "3-5 years experience" — take min of range
    for m in RANGE_YEARS_RE.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo <= MAX_PLAUSIBLE_YEARS and lo <= hi <= 30:
            candidates.append(lo)

    # 3. "minimum X years" / "at least X years"
    for m in MIN_YEARS_RE.finditer(text):
        v = int(m.group(1))
        if 0 <= v <= MAX_PLAUSIBLE_YEARS:
            candidates.append(v)

    # 4. "5 years of experience" (no +)
    for m in YEARS_OF_EXP_RE.finditer(text):
        v = int(m.group(1))
        if 0 <= v <= MAX_PLAUSIBLE_YEARS:
            candidates.append(v)

    if candidates:
        # Lower bound wins — if "3+ years required, 5+ preferred" appears,
        # the requirement is 3.
        return min(candidates)

    # 5. Entry-level keyword fallback
    if ENTRY_LEVEL_RE.search(text):
        return 0

    return None


def extract_for_job(
    *, title: Optional[str], description: Optional[str]
) -> Optional[int]:
    """Try title first (high signal/noise), fall back to the description.

    Das Fenster war 2000 Zeichen. Anforderungen stehen aber meist nach Intro
    und Aufgaben — bei rund einem Drittel der Inserate mit "N+ years" erst
    dahinter, gelegentlich erst nach Zeichen 2700. Die Regexe verlangen ein
    Erfahrungs-Keyword neben der Zahl, ein längeres Fenster holt also
    Anforderungszeilen, keine "seit 10 Jahren am Markt"-Floskeln.
    """
    return (
        extract_min_years(title)
        or extract_min_years((description or "")[:8000])
    )
