"""Render a letter to .docx in the configured style, then convert to PDF.

Layout (§1–§2 des Stil-Leitfadens, assets/example/letter_style_guide.md):

  - A4, Ränder 2 cm, Calibri
  - Fliesstext 10.5 pt, Zeilenabstand 1.15, Blocksatz
  - Absenderblock 9.5 pt — nie grösser als der Fliesstext
  - Kopf: Absender → optional Firmenblock → Datum rechtsbündig →
    fette Betreffzeile → Leerzeile → Anrede
  - Eine Seite ist das Ziel, keine Bedingung: passt der Brief nicht, wird nur
    der Absenderblock einzeilig gesetzt. Schriftgrösse, Zeilenabstand und
    Ränder werden nie verkleinert — ein knapp zweiseitiger Brief schlägt einen
    gequetschten.

Vorher: Letter-Masse in Zoll, 10 pt, oben 0.6 cm und unten 0.4 cm Rand.

Conversion to PDF uses LibreOffice headless. Override the binary location via
``LIBREOFFICE_PATH`` env var if it's not at the macOS default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Mm, Pt

from src.agent.letter_quality import REPO_LABEL, normalize_subject, normalize_text
from src.agent.letter_review import parse_markdown_to_structure

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
# Nur noch das Archiv der tatsächlich versendeten Fassungen liegt im Repo.
# Downloads, Mail-Versand und Selbsttest rendern in ein Temp-Verzeichnis
# (siehe temp_render_dir) — vorher entstand bei jedem Klick auf "Word" ein
# neues Word+PDF-Paar in data/cover_letters/, datiert und nie aufgeräumt.
ARCHIVE_DIR = ROOT / "data" / "cover_letters" / "sent"


def temp_render_dir(purpose: str) -> Path:
    """Frisches Verzeichnis im System-Temp ($TMPDIR, auf macOS /var/folders/…/T/).

    Der Aufrufer räumt es nach Gebrauch weg (shutil.rmtree).
    """
    import tempfile
    return Path(tempfile.mkdtemp(prefix=f"jobfinder_{purpose}_"))

# Default sender block — overridden by `profile.sender` in the profile YAML.
DEFAULT_NAME = "Alex Muster"
DEFAULT_ADDRESS_LINES = ["Beispielstrasse 1", "8000 Musterstadt, Switzerland"]
DEFAULT_PHONE = "+41 00 000 00 00"
DEFAULT_EMAIL = "alex.muster@example.com"

# Style constants — Untergrenzen aus §2, nie unterschreiten.
FONT_NAME = "Calibri"
HEADER_PT = 9.5          # Absenderblock: 1 pt kleiner als der Fliesstext
BODY_PT = 10.5
LINE_SPACING = 1.15
MARGIN_CM = 2.0
BLANK_LINE_PT = 12       # eine Leerzeile bei 10.5 pt × 1.15

DEFAULT_LIBREOFFICE_BIN = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def _safe_filename(text: str, max_len: int = 60) -> str:
    keep = "-_.() "
    cleaned = "".join(c if c.isalnum() or c in keep else "_" for c in text)
    cleaned = "_".join(cleaned.split())
    return cleaned[:max_len].strip("._-")


def _add_para(
    doc: Document,
    text: str = "",
    *,
    size: float = BODY_PT,
    justify: bool = False,
    align_right: bool = False,
    bold: bool = False,
    space_after: Optional[float] = None,
    line_spacing: float = LINE_SPACING,
):
    """Add a paragraph with explicit Calibri font, size and line spacing.

    ``space_after`` is in points; ``None`` keeps the Normal-style default.
    Use explicit values instead of empty spacer paragraphs — empty paras
    push content onto page 2 once the body has 5+ paragraphs.
    """
    p = doc.add_paragraph()
    if justify:
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if align_right:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.line_spacing = line_spacing
    if space_after is not None:
        p.paragraph_format.space_after = Pt(space_after)
    if text:
        run = p.add_run(text)
        run.font.name = FONT_NAME
        run.font.size = Pt(size)
        if bold:
            run.bold = True
    return p


def build_docx(
    *,
    structure: dict,
    output_path: Path,
    name: str = DEFAULT_NAME,
    address_lines: Optional[list[str]] = None,
    phone: str = DEFAULT_PHONE,
    email: str = DEFAULT_EMAIL,
    language: str = "en",
    compact_header: bool = False,
) -> Path:
    """Render the parsed structure into a .docx and return its path.

    ``compact_header`` setzt nur die Absenderzeilen einzeilig und rückt den
    Kopf enger — Schritt 3 von §2, wenn der Brief sonst überläuft.
    """
    if address_lines is None:
        address_lines = DEFAULT_ADDRESS_LINES
    header_spacing = 1.0 if compact_header else LINE_SPACING
    header_gap = 4 if compact_header else 8

    doc = Document()

    # Page geometry — A4, 2 cm rundum
    section = doc.sections[0]
    section.page_height = Mm(297)
    section.page_width = Mm(210)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Cm(MARGIN_CM))

    # Normal style — Calibri 10.5 pt, 1.15, keine automatischen Abstände.
    normal = doc.styles["Normal"]
    normal.font.name = FONT_NAME
    normal.font.size = Pt(BODY_PT)
    normal.paragraph_format.line_spacing = LINE_SPACING
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)

    # --- Absenderblock (9.5 pt) ---
    sender_lines = [name, *address_lines, phone, email]
    for i, line in enumerate(sender_lines):
        last = i == len(sender_lines) - 1
        _add_para(doc, line, size=HEADER_PT, line_spacing=header_spacing,
                  space_after=header_gap if last else 0)

    # --- Firmenblock: nur was aus der Anzeige bekannt ist ---
    company_lines = [line for line in (structure.get("company_block") or []) if line.strip()]
    for i, line in enumerate(company_lines):
        last = i == len(company_lines) - 1
        _add_para(doc, line, size=BODY_PT, line_spacing=header_spacing,
                  space_after=header_gap if last else 0)

    # --- Datum rechtsbündig ---
    _add_para(doc, structure["date_line"], size=BODY_PT, align_right=True, space_after=header_gap)

    # --- Betreffzeile fett, danach eine Leerzeile ---
    if structure.get("subject"):
        _add_para(doc, structure["subject"], size=BODY_PT, bold=True, space_after=BLANK_LINE_PT)

    # --- Anrede ---
    _add_para(doc, structure["salutation"], size=BODY_PT, space_after=6)

    # --- Intro ---
    _add_para(doc, structure["intro_paragraph"], size=BODY_PT, justify=True, space_after=6)

    # --- Middle paragraphs (flowing) OR sections (structured) ---
    for para in structure.get("middle_paragraphs") or []:
        _add_para(doc, para, size=BODY_PT, justify=True, space_after=6)

    for sec in structure.get("sections") or []:
        _add_para(doc, f"{sec['heading']}:", size=BODY_PT, space_after=3)
        for bullet in sec["bullets"]:
            p = doc.add_paragraph(style="List Bullet")
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.line_spacing = LINE_SPACING
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(bullet)
            run.font.name = FONT_NAME
            run.font.size = Pt(BODY_PT)
        if sec["bullets"]:
            doc.paragraphs[-1].paragraph_format.space_after = Pt(6)

    # --- Closing ---
    if structure.get("closing_paragraph"):
        _add_para(doc, structure["closing_paragraph"], size=BODY_PT, justify=True, space_after=10)

    # --- Signoff ---
    _add_para(doc, structure.get("signoff", "Sincerely,"), size=BODY_PT)
    _add_para(doc, structure.get("name_line", DEFAULT_NAME), size=BODY_PT)

    # Schluss-Block zusammenhalten: Wenn die Seite bricht, sollen Closing +
    # Gruß + Name gemeinsam auf Seite 2 rutschen — nie der Gruß allein.
    for para in doc.paragraphs[-3:]:
        para.paragraph_format.keep_with_next = True
        para.paragraph_format.keep_together = True
    doc.paragraphs[-1].paragraph_format.keep_with_next = False

    # --- §11 optionaler Repo-Link, klein unter dem Namen ---
    if structure.get("repo_url"):
        p = _add_para(doc, f"{REPO_LABEL.get(language, REPO_LABEL['en'])} {structure['repo_url']}", size=HEADER_PT)
        p.paragraph_format.space_before = Pt(8)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    logger.info("Wrote DOCX → %s", output_path)
    return output_path


def docx_to_pdf(docx_path: Path) -> Path:
    """Convert a .docx to .pdf via LibreOffice headless. Returns PDF path."""
    bin_path = os.getenv("LIBREOFFICE_PATH", DEFAULT_LIBREOFFICE_BIN)
    if not Path(bin_path).exists():
        for candidate in ("soffice", "libreoffice"):
            found = shutil.which(candidate)
            if found:
                bin_path = found
                break
        else:
            raise RuntimeError(
                f"LibreOffice not found at {bin_path}. Install with "
                f"`brew install --cask libreoffice` or set LIBREOFFICE_PATH."
            )

    out_dir = docx_path.parent
    cmd = [
        bin_path, "--headless",
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(docx_path),
    ]
    logger.info("Running LibreOffice: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice conversion failed (exit {result.returncode}):\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
    pdf_path = docx_path.with_suffix(".pdf")
    if not pdf_path.exists():
        raise RuntimeError(
            f"LibreOffice ran but PDF not found at {pdf_path}.\nSTDOUT: {result.stdout}"
        )
    logger.info("Wrote PDF → %s", pdf_path)
    return pdf_path


def pdf_page_count(pdf_path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf_path)).pages)


def pdf_overflow_lines(pdf_path: Path) -> int:
    """Gesetzte Textzeilen jenseits von Seite 1 (0 = passt auf eine Seite)."""
    from pypdf import PdfReader
    pages = PdfReader(str(pdf_path)).pages[1:]
    return sum(sum(1 for line in (pg.extract_text() or "").splitlines() if line.strip()) for pg in pages)


@dataclass
class PageFit:
    pages: int
    overflow_lines: int
    compact_header: bool


_FUNCTION_WORDS = {
    "de": {"und", "ich", "die", "der", "das", "nicht", "mit", "für", "bei", "ist", "habe", "meine", "ihre", "sie"},
    "en": {"and", "the", "i", "with", "for", "is", "have", "my", "your", "to", "of", "at"},
    "es": {"y", "el", "la", "los", "las", "con", "para", "mi", "su", "que", "del", "por"},
}


def detect_language(structure: dict, default: Optional[str] = None) -> str:
    """Sprache aus dem Brief selbst: Fliesstext, sonst Betreff/Anrede.

    Der Text geht vor ``default`` (gespeicherte Sprache): wer einen deutschen
    Brief über einen englischen speichert, soll kein "Application:" und kein
    "80%" bekommen. Der Fliesstext geht vor der Anrede, weil das Modell
    deutschen Briefen schon "Dear Hiring Team," vorangestellt hat.
    ``default`` greift nur, wenn der Text kein Signal gibt.
    """
    text = " ".join([structure.get("intro_paragraph") or "", structure.get("closing_paragraph") or "",
                     *(structure.get("middle_paragraphs") or []),
                     *(b for sec in structure.get("sections") or [] for b in sec.get("bullets") or [])]).lower()
    tokens = re.findall(r"[a-zäöüßáéíóúñ]+", text)
    votes = {lang: sum(t in words for t in tokens) for lang, words in _FUNCTION_WORDS.items()}
    best = max(votes, key=votes.get)
    if votes[best] >= 8 and votes[best] >= 2 * max(v for lang, v in votes.items() if lang != best):
        return best
    head = f"{structure.get('subject', '')} {structure.get('salutation', '')}".lower()
    if re.search(r"bewerbung|sehr geehrte|liebe|guten tag|hallo|grüezi|geschätzte", head):
        return "de"
    if re.search(r"candidatura|estimad|hola", head):
        return "es"
    if re.search(r"\bapplication\b|\bdear\b|\bhello\b", head):
        return "en"
    return default if default in ("de", "en", "es") else "en"


# Ob der Brief den kompakten Kopf braucht, hängt nur vom Inhalt ab. Für den
# Word-Download ohne PDF misst ein Probelauf einmal pro Fassung; danach gilt
# das Ergebnis aus diesem Cache (lebt so lange wie der Dashboard-Prozess).
_LAYOUT_CACHE: dict[str, bool] = {}


def prepare_structure(markdown: str, *, sender_name: str, language: Optional[str],
                      job_title: str) -> tuple[dict, str]:
    """Markdown → Struktur mit Pflicht-Betreff und bereinigter Typografie."""
    structure = parse_markdown_to_structure(markdown, sender_name=sender_name)
    lang = detect_language(structure, default=language)
    structure["subject"] = normalize_subject(structure.get("subject") or "", lang, job_title)
    for key in ("subject", "salutation", "intro_paragraph", "closing_paragraph"):
        structure[key] = normalize_text(structure.get(key) or "", lang)
    structure["middle_paragraphs"] = [normalize_text(p, lang) for p in structure.get("middle_paragraphs") or []]
    structure["sections"] = [
        {"heading": normalize_text(sec["heading"], lang), "bullets": [normalize_text(b, lang) for b in sec["bullets"]]}
        for sec in structure.get("sections") or []
    ]
    return structure, lang


def _render_inputs(markdown: str, *, config: Optional[dict], language: Optional[str],
                   job_title: str) -> tuple[dict, str, dict, str]:
    """Struktur, Sprache, Absenderblock und Cache-Schlüssel — gemeinsam für Rendern und Messen."""
    sender = ((config or {}).get("profile") or {}).get("sender") or {}
    sender_name = sender.get("name", DEFAULT_NAME)
    sender_kwargs = dict(
        name=sender_name,
        address_lines=sender.get("address_lines") or DEFAULT_ADDRESS_LINES,
        phone=sender.get("phone", DEFAULT_PHONE),
        email=sender.get("email", DEFAULT_EMAIL),
    )
    structure, lang = prepare_structure(markdown, sender_name=sender_name, language=language,
                                        job_title=job_title)
    cache_key = hashlib.sha1(json.dumps([structure, sender_kwargs], sort_keys=True).encode()).hexdigest()
    return structure, lang, sender_kwargs, cache_key


def measure_page_fit(*, markdown: str, job_title: str, config: Optional[dict] = None,
                     language: Optional[str] = None) -> Optional[PageFit]:
    """Probelauf mit derselben Seitensteuerung wie render_cover_letter (§2).

    Für den Generator: Läuft der Brief auch mit kompaktem Kopf über, bekommt
    das Modell einen Kürzungsauftrag. Das Ergebnis füllt nebenbei den
    Layout-Cache, der spätere Word-Download misst dann nicht noch einmal.
    ``None``, wenn LibreOffice fehlt oder die Konvertierung scheitert.
    """
    structure, lang, sender_kwargs, cache_key = _render_inputs(
        markdown, config=config, language=language, job_title=job_title)
    probe_dir = temp_render_dir("fit")

    def probe(compact: bool) -> Path:
        docx = build_docx(structure=structure, output_path=probe_dir / "probe.docx", language=lang,
                          compact_header=compact, **sender_kwargs)
        return docx_to_pdf(docx)

    try:
        pdf = probe(False)
        compact = pdf_page_count(pdf) > 1
        if compact:
            pdf = probe(True)
        fit = PageFit(pdf_page_count(pdf), pdf_overflow_lines(pdf), compact)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("Seitenmessung übersprungen: %s", exc)
        return None
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    _LAYOUT_CACHE[cache_key] = compact
    return fit


def render_cover_letter(
    *,
    markdown: str,
    company: str,
    job_title: str,
    job_id: int,
    today_iso: str,
    output_dir: Path,
    with_pdf: bool = True,
    config: Optional[dict] = None,
    language: Optional[str] = None,
) -> tuple[Path, Optional[Path]]:
    """End-to-end: parse markdown → DOCX (→ PDF). Returns (docx_path, pdf_path).

    ``output_dir`` ist Pflicht: jeder Aufrufer entscheidet bewusst, ob die
    Dateien flüchtig sind (temp_render_dir) oder ins Archiv gehören
    (ARCHIVE_DIR). ``with_pdf=False`` liefert nur Word; pdf_path ist dann None.

    Seitensteuerung (§2): erst mit normalem Kopf rendern und die Seiten
    zählen; bei Überlauf einmal mit kompaktem Kopf. Läuft er dann immer noch
    über, bleibt es dabei — Belege und Lesbarkeit gehen vor.

    If ``config`` is given, the sender block (name, address, phone, email) is
    pulled from ``config['profile']['sender']`` so the rendered letter matches
    the active profile.
    """
    structure, lang, sender_kwargs, cache_key = _render_inputs(
        markdown, config=config, language=language, job_title=job_title)
    slug = _safe_filename(f"{company}_{job_title}")
    output_dir.mkdir(parents=True, exist_ok=True)
    docx_target = (output_dir / f"{today_iso}_job{job_id}_{slug}").with_suffix(".docx")

    def build(compact: bool) -> Path:
        return build_docx(structure=structure, output_path=docx_target, language=lang,
                          compact_header=compact, **sender_kwargs)

    if cache_key in _LAYOUT_CACHE:
        docx_path = build(_LAYOUT_CACHE[cache_key])
        return docx_path, (docx_to_pdf(docx_path) if with_pdf else None)

    if with_pdf:
        docx_path = build(False)
        pdf_path = docx_to_pdf(docx_path)
        compact = pdf_page_count(pdf_path) > 1
        if compact:
            docx_path = build(True)
            pdf_path = docx_to_pdf(docx_path)
            logger.info("Brief läuft über — kompakter Kopf, jetzt %d Seite(n)", pdf_page_count(pdf_path))
        _LAYOUT_CACHE[cache_key] = compact
        return docx_path, pdf_path

    # Nur Word: Seitenzahl in einem Probelauf im Temp-Verzeichnis messen.
    probe_dir = temp_render_dir("layout")
    try:
        probe = build_docx(structure=structure, output_path=probe_dir / "probe.docx", language=lang, **sender_kwargs)
        compact = pdf_page_count(docx_to_pdf(probe)) > 1
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    _LAYOUT_CACHE[cache_key] = compact
    return build(compact), None
