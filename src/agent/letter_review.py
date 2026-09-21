"""Deterministic review of a letter the applicant wrote or edited.

This module is the assistance half of the letter workflow: it never produces a
letter, it reads one. Two directions:

* :func:`parse_markdown_to_structure` turns the markdown the applicant edited
  in the browser back into the structured form the DOCX renderer needs, so
  what they see saved is what gets rendered.
* :func:`review_markdown` runs the deterministic rules in
  :mod:`src.agent.letter_quality` over that structure and returns findings —
  no model call, same findings whether the text was typed by hand or pasted
  in. The rules themselves are documented in the letter style guide
  (``assets/example/letter_style_guide.md``), whose section numbers the
  findings refer to.

The findings are advisory. Nothing here rewrites the applicant's text; the
review UI shows each finding next to the letter and the applicant decides.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
# Fallback paths used when no config is passed. The multi-profile loader
# normally injects per-profile paths via `cfg["assets"]`.
DEFAULT_CV_PATH = ROOT / "assets" / "example" / "cv_example.md"
DEFAULT_GUIDE_PATH = ROOT / "assets" / "example" / "letter_style_guide.md"


def review_markdown(markdown: str, *, job: dict, config: dict, channel: Optional[str] = None,
                    measure_page: bool = True) -> list[dict]:
    """Deterministische Prüfung eines gespeicherten Briefs — ohne Modellaufruf.

    Läuft bei jedem Speichern über den aktuellen Text, damit ein Prüfhinweis
    nicht verschwindet, nur weil jemand auf "Save draft" geklickt hat, und
    damit ein von Hand umgeschriebener Absatz dieselben Regeln durchläuft wie
    der erste Entwurf.

    ``measure_page=False`` überspringt das Rendern — die Seitenmessung ruft
    LibreOffice auf und dauert ein paar Sekunden.
    """
    from datetime import date as _date

    from src.agent import letter_quality as lq
    from src.agent.docx_generator import detect_language
    from src.workload_extractor import extract_workload_phrase

    assets = (config or {}).get("assets") or {}
    sender_name = ((config or {}).get("profile") or {}).get("name", "Alex Muster")
    by_channel = assets.get("cv_md_by_channel") or {}
    cv_rel = by_channel.get(channel or "") or by_channel.get("default") or assets.get("cv_md")
    cv_path = ROOT / cv_rel if cv_rel and (ROOT / cv_rel).exists() else ROOT / assets.get("cv_md", DEFAULT_CV_PATH.relative_to(ROOT))
    structure = parse_markdown_to_structure(markdown, sender_name=sender_name)
    language = detect_language(structure, default=job.get("cover_letter_lang"))
    findings = lq.check_letter(
        structure, language=language, stations=lq.parse_cv_stations(cv_path.read_text(encoding="utf-8")),
        today=_date.today(), ad_text=job.get("description") or "",
        ad_pensum=extract_workload_phrase(job.get("title"), job.get("description")),
    )
    if measure_page:
        from src.agent.docx_generator import measure_page_fit

        fit = measure_page_fit(markdown=markdown, job_title=job.get("title") or "", config=config,
                               language=language)
        overflow = fit and lq.page_fit_finding(fit.pages, fit.overflow_lines)
        if overflow:
            findings.append(overflow)
    return [f.to_dict() for f in findings]


SIGNOFF_PHRASES = {
    "sincerely", "mit freundlichen grüssen", "mit freundlichen grüßen",
    "atentamente", "un cordial saludo", "kind regards", "best regards",
    "beste grüsse", "best", "regards", "herzliche grüsse",
}


def parse_markdown_to_structure(
    markdown: str, *, sender_name: str = "Alex Muster"
) -> dict:
    """Reverse direction: parse user-edited markdown back into structure form
    so the DOCX builder can produce identical layout.

    Heuristic blocks (separated by blank lines):
      - Leading blocks of "> " lines → company_block (optional)
      - First remaining block (single line) → date_line
      - A single line wrapped in ** ** → subject (optional; older letters have none)
      - Next block (single line) → salutation
      - Trailing "Project repository: <url>" line → repo_url (optional)
      - Last block before signoff → closing_paragraph
      - Last block (signoff + name) → signoff + name_line
      - Heading block: single line ending with ":" — opens a section
      - Bullet block: lines starting with "- " — closes the most recent heading
      - Plain paragraph block: text lines, joined with spaces

    ``sender_name`` is used both as fallback for the name line and as a
    last-name token to detect the trailing name block.
    """
    lines = markdown.replace("\r\n", "\n").split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    if not blocks:
        raise ValueError("Cover letter markdown is empty")

    last_name_token = (sender_name.split()[-1] if sender_name else "").lower()

    company_block: list[str] = []
    while blocks and all(line.lstrip().startswith(">") for line in blocks[0]):
        company_block.extend(line.lstrip()[1:].strip() for line in blocks.pop(0))

    repo_url: Optional[str] = None
    if blocks and len(blocks[-1]) == 1:
        m = re.match(r"^\s*(?:Project repository|Projekt-Repository|Repositorio del proyecto|Repo)\s*:\s*(https?://\S+)\s*$",
                     blocks[-1][0], re.IGNORECASE)
        if m:
            repo_url = m.group(1)
            blocks.pop()

    date_line = blocks[0][0].strip() if blocks else ""
    rest = blocks[1:]
    subject = ""
    if rest and len(rest[0]) == 1 and re.fullmatch(r"\*\*.+\*\*", rest[0][0].strip()):
        subject = rest.pop(0)[0].strip()[2:-2].strip()
    salutation = rest[0][0].strip() if rest else ""

    # Strip the trailing signoff + name block(s)
    signoff = "Sincerely,"
    name_line = sender_name
    body_blocks = rest[1:]
    if body_blocks:
        last = body_blocks[-1]
        if len(last) >= 2 and last_name_token and last_name_token in last[-1].lower():
            signoff = last[0].strip().rstrip(",") + ","
            name_line = last[-1].strip()
            body_blocks = body_blocks[:-1]
        elif len(last) == 1 and last_name_token and last_name_token in last[0].lower():
            name_line = last[0].strip()
            body_blocks = body_blocks[:-1]
            # signoff might be the previous block
            if body_blocks and len(body_blocks[-1]) == 1:
                prev = body_blocks[-1][0].strip()
                if prev.lower().rstrip(",") in SIGNOFF_PHRASES:
                    signoff = prev.rstrip(",") + ","
                    body_blocks = body_blocks[:-1]

    intro_paragraph = ""
    middle_paragraphs: list[str] = []
    closing_paragraph = ""
    sections: list[dict] = []
    pending_heading: Optional[str] = None
    paragraph_buffer: list[str] = []

    def flush_paragraph_buffer():
        nonlocal intro_paragraph
        if not paragraph_buffer:
            return
        text = " ".join(s.strip() for s in paragraph_buffer).strip()
        paragraph_buffer.clear()
        if not intro_paragraph:
            intro_paragraph = text
        else:
            middle_paragraphs.append(text)

    for blk in body_blocks:
        # Bullet block?
        if all(line.lstrip().startswith(("- ", "* ", "• ")) for line in blk):
            bullets = [line.lstrip()[2:].strip() for line in blk]
            heading = pending_heading or "Highlights"
            sections.append({"heading": heading, "bullets": bullets})
            pending_heading = None
            flush_paragraph_buffer()
            continue
        # Heading block (single line ending with ":")
        if len(blk) == 1 and blk[0].rstrip().endswith(":"):
            flush_paragraph_buffer()
            pending_heading = blk[0].rstrip().rstrip(":").strip()
            continue
        # Plain paragraph — flush any preceding paragraph FIRST so each
        # blank-line-separated block becomes its own paragraph. Without this
        # flush, consecutive plain blocks collapse into one wall of text.
        flush_paragraph_buffer()
        paragraph_buffer.extend(blk)
        # If a paragraph follows a heading without bullets, the heading is dropped
        # (treat as decorative). User can fix in the markdown if needed.
        pending_heading = None
    flush_paragraph_buffer()

    # The last "middle paragraph" is actually the closing paragraph.
    if middle_paragraphs:
        closing_paragraph = middle_paragraphs.pop()
    elif intro_paragraph and not sections:
        # only intro and no closing — promote nothing, user can fix manually
        pass

    return {
        "company_block": company_block,
        "subject": subject,
        "repo_url": repo_url,
        "date_line": date_line,
        "salutation": salutation,
        "intro_paragraph": intro_paragraph,
        "middle_paragraphs": middle_paragraphs,
        "sections": sections,
        "closing_paragraph": closing_paragraph,
        "signoff": signoff,
        "name_line": name_line,
    }
