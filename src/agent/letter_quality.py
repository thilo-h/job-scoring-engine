"""Deterministische Qualitätsregeln für Anschreiben.

Der Stil-Leitfaden beschreibt, *wie* ein Brief aussehen soll. Dieses Modul prüft
danach, *ob* er es tut — ohne LLM, reproduzierbar, auch für Briefe, die im
Editor von Hand geändert wurden.

Zwei Arten von Regeln:

* **Korrekturen** (``normalize_text``) sind sicher und werden direkt
  angewendet: Typografie (§7), fehlende Leerzeichen nach Satzzeichen und
  verlorene Bindestriche (§6).
* **Befunde** (``check_letter``) lassen sich nicht mechanisch beheben —
  welcher von zwei doppelten Sätzen soll bleiben? Sie gehen als Prüfhinweis in
  den Drawer, wo der Verfasser entscheidet.

Die Paragraphen-Nummern (§) verweisen auf die Abschnitte im Stil-Leitfaden
(``assets/example/letter_style_guide.md``). Der Leitfaden beschreibt, *wie* ein
Brief aussehen soll; dieses Modul prüft, *ob* er es tut. Wer eine Regel ändert,
ändert beides — die Nummern sind der Vertrag zwischen den zwei Dateien.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
from typing import Optional

WORDS_MIN, WORDS_MAX = 450, 600
# Das Modell landet bei Korrekturen eher knapp unter dem genannten Wert —
# deshalb nennt der Auftrag die Mitte des Korridors, nicht die Untergrenze.
WORDS_TARGET = 500          # §2 Zielkorridor
MAX_PARAGRAPH_LINES = 7                  # §2 "keiner über 7"
CHARS_PER_LINE = 105                     # A4, 2 cm Ränder, 10.5 pt Calibri, Blocksatz — am PDF gemessen 101–105

SUBJECT_PREFIX = {"en": "Application:", "de": "Bewerbung:", "es": "Candidatura:"}
REPO_LABEL = {"en": "Project repository:", "de": "Projekt-Repository:", "es": "Repositorio del proyecto:"}


@dataclass
class Finding:
    code: str
    message: str
    repairable: bool = True     # False = for the author to judge, not mechanically fixable

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Korrekturen (§6, §7)
# ---------------------------------------------------------------------------

_NUMBER_RANGE = re.compile(r"(?<![\d.\-–—/])(\d{1,4})\s*[-–—]\s*(\d{1,4})(?![\d\-–—/])")
_SPACED_DASH = re.compile(r"\s+(?:-{1,2}|–|—)\s+")
_TIGHT_EM_DASH = re.compile(r"(?<=\w)—(?=\w)")
_MONTH = r"(?:Jan|Feb|Mär|Mar|Apr|Mai|May|Jun|Jul|Aug|Sep|Okt|Oct|Nov|Dez|Dec|ene|abr|ago|dic)[a-zäé]*\.?"
_DATE_SPAN = re.compile(rf"\b({_MONTH}\s+\d{{4}})\s+(?:-{{1,2}}|–|—)\s+(?=({_MONTH}\s+\d{{4}}|heute|today|present|ongoing|laufend))",
                        re.IGNORECASE)


def _is_date_span(text: str, m: re.Match) -> bool:
    before = text[max(0, m.start() - 12):m.start()]
    after = text[m.end():m.end() + 12]
    return bool(re.search(rf"{_MONTH}\s+\d{{4}}$", before, re.IGNORECASE)
                and re.match(rf"({_MONTH}\s+\d{{4}}|heute|today|present|ongoing|laufend)", after, re.IGNORECASE))
_PERCENT = re.compile(r"(\d)[ \u00a0\u202f]*%")
# "summer.I built" / "tool.The" — Grossbuchstabe gefolgt von Kleinbuchstabe oder
# Leerzeichen, damit "Node.JS" oder "M.Sc." nicht auseinandergerissen werden.
_MISSING_SPACE = re.compile(r"([a-zäöüß\)\]])([.!?;:])([A-ZÄÖÜ](?=[a-zäöüß\s]|$))")
_MISSING_SPACE_COMMA = re.compile(r"([A-Za-zÄÖÜäöüß\)\]]),([A-Za-zÄÖÜäöüß])")
_HYPHENATED = re.compile(r"\b([A-Za-zÄÖÜäöüß]{2,})-([A-Za-zÄÖÜäöüß]{2,})\b")


def normalize_text(text: str, language: str, *, hyphen_sources: tuple[str, ...] = (),
                   swiss_spelling: bool = False) -> str:
    """Typografie und mechanische Textfehler bereinigen. Idempotent.

    ``swiss_spelling``: in deutschen Briefen ß → ss (Profil-Learning, Schweizer
    Schreibweise). Das Modell hält sich nicht zuverlässig daran.
    """
    if not text:
        return text
    out = text.replace("\u00ad", "").replace("\u2011", "-")     # weiche / geschützte Trennstriche
    if swiss_spelling and language == "de":
        out = out.replace("ß", "ss")
        out = re.sub(r"\bZüricher(?=\w*)", "Zürcher", out)
    # §7 Zahlenbereiche zuerst — sonst würde "40 – 60" unten zum Geviertstrich.
    out = _NUMBER_RANGE.sub(lambda m: f"{m.group(1)}–{m.group(2)}", out)
    # §7 Zeiträume mit Leerzeichen ("Nov 2024 – Jul 2026") behalten den
    # Halbgeviertstrich; alle anderen gesperrten Striche sind Einschübe → Geviertstrich.
    out = _DATE_SPAN.sub(lambda m: f"{m.group(1)} – ", out)
    out = _SPACED_DASH.sub(lambda m: m.group(0) if m.group(0) == " – " and _is_date_span(out, m) else " — ", out)
    out = _TIGHT_EM_DASH.sub(" — ", out)
    # §7 Prozent: Deutsch/Spanisch mit geschütztem Leerzeichen, Englisch ohne.
    sep = "" if language == "en" else "\u00a0"
    out = _PERCENT.sub(lambda m: f"{m.group(1)}{sep}%", out)
    # §6 fehlendes Leerzeichen nach Satzzeichen ("summer.I built"). URLs,
    # Mail-Adressen und Domains bleiben unangetastet.
    out = " ".join(
        tok if ("/" in tok or "@" in tok or "www." in tok)
        else _MISSING_SPACE_COMMA.sub(r"\1, \2", _MISSING_SPACE.sub(r"\1\2 \3", tok))
        for tok in out.split(" ")
    )
    # §6 verlorene Bindestriche ("roomprogramme" statt "room-programme").
    if hyphen_sources:
        out = restore_hyphens(out, hyphen_sources)
    return re.sub(r"[ \t]{2,}", " ", out)


def restore_hyphens(text: str, sources: tuple[str, ...]) -> str:
    """Setzt Bindestriche wieder ein, die beim Umschreiben verloren gingen.

    Kennt nur Komposita, die in einer Quelle (CV, Anzeige, Brief selbst)
    mit Bindestrich stehen — und lässt die zusammengeschriebene Form in Ruhe,
    wenn eine Quelle sie selbst so schreibt (z.B. "email" neben "e-mail").
    """
    corpus = "\n".join(sources) + "\n" + text
    joined: dict[str, str] = {}
    for m in _HYPHENATED.finditer(corpus):
        joined.setdefault((m.group(1) + m.group(2)).lower(), m.group(0))
    if not joined:
        return text
    plain_words = {w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß]+", "\n".join(sources))}

    def fix(m: re.Match) -> str:
        word = m.group(0)
        hyphenated = joined.get(word.lower())
        if not hyphenated or word.lower() in plain_words:
            return word
        return (hyphenated[0].upper() + hyphenated[1:]) if word[0].isupper() else hyphenated

    return re.sub(r"\b[A-Za-zÄÖÜäöüß]{5,}\b", fix, text)


def normalize_subject(subject: str, language: str, job_title: str) -> str:
    """§1 Betreffzeile ist Pflicht und beginnt mit dem Sprach-Präfix."""
    prefix = SUBJECT_PREFIX.get(language, SUBJECT_PREFIX["en"])
    body = (subject or "").strip().strip("*").strip()
    body = re.sub(
        r"^(application|bewerbung|candidatura|betreff|subject|re)\s*(?::|—|–|-)?\s*"
        r"(?:(?:as|als|for|für|como|para|a)\s+)?(?:the\s+)?(?:(?:position|role|stelle)\s+(?:of|als)\s+)?",
        "", body, flags=re.IGNORECASE,
    ).strip(" :—–-")
    return f"{prefix} {_strip_ad_noise(body) or _strip_ad_noise(job_title)}"


# "(m/w/d)", "(all genders)", "(80–100 %)" gehören zur Anzeige, nicht in den Betreff.
_AD_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:[mwfdxh]\s*/\s*)+[mwfdxh]\s*[\)\]]"
    r"|\s*[\(\[]\s*all genders?\s*[\)\]]"
    r"|\s*[\(\[]\s*\d{2,3}\s*(?:[-–]\s*\d{2,3}\s*)?\s*%\s*[\)\]]"
    r"|\s*[-–—]?\s*\d{2,3}\s*[-–]\s*\d{2,3}\s*%\s*$",
    re.IGNORECASE,
)


def _strip_ad_noise(text: str) -> str:
    return _AD_NOISE.sub("", (text or "").strip()).strip(" ,-–—")


# ---------------------------------------------------------------------------
# CV-Stationen (§4 Zeitformen, §10 Wiederholungen)
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_RANGE = re.compile(
    r"·\s*(?P<start>(?:[A-Za-zäöü]{3,9}\.?\s+)?\d{4})\s*[–-]\s*"
    r"(?P<end>(?:[A-Za-zäöü]{3,9}\.?\s+)?\d{4}|ongoing|present|heute|laufend|today)?",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"\b(GmbH|SE|SL|AG|KG|Co\.|&|Ltd|Inc)\b\.?,?")
# Tokens too generic to identify a station on their own: a letter mentioning
# "university" says nothing about *which* one. A station whose first word is on
# this list falls back to its full name as the only alias. Extend it when a
# CV contains an organisation whose first word is this weak — but keep the
# entries generic, never a specific institution.
_GENERIC_TOKENS = {
    "swiss", "university", "universität", "international", "hochschule",
    "school", "college", "institute", "the", "ai", "group", "and",
}


@dataclass
class Station:
    name: str
    section: str
    aliases: list[tuple[str, bool]] = field(default_factory=list)   # (alias, case_sensitive)
    end: Optional[date] = None
    ongoing: bool = False
    missing_end: bool = False

    def ended(self, today: date) -> bool:
        return bool(self.end and self.end < today)

    def mentioned_in(self, text: str) -> bool:
        for alias, case_sensitive in self.aliases:
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", text, flags):
                return True
        return False


def _month_end(token: str) -> Optional[date]:
    parts = token.replace(".", "").split()
    year = int(parts[-1])
    month = _MONTHS.get(parts[0][:3].lower(), 12) if len(parts) > 1 else 12
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(next_month.toordinal() - 1)


def parse_cv_stations(cv_markdown: str) -> list[Station]:
    """Stationen aus den ###-Überschriften des CV-Markdowns, mit End-Datum."""
    stations: list[Station] = []
    section = ""
    lines = cv_markdown.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not line.startswith("### ") or not any(s in section for s in ("education", "experience", "project")):
            continue
        header = line[4:].strip()
        name = re.sub(r"\*\(.*?\)\*", "", header.split(" — ")[0]).strip()
        span = _DATE_RANGE.search(header) or next(
            (m for m in (_DATE_RANGE.search(nxt) for nxt in lines[i + 1:i + 3]) if m), None)
        st = Station(name=re.sub(r"\s*·.*$", "", name), section=section)
        if span is None or span.group("end") is None:
            st.missing_end = True
        elif span.group("end").lower() in ("ongoing", "present", "heute", "laufend", "today"):
            st.ongoing = True
        else:
            st.end = _month_end(span.group("end"))
        acronym = re.search(r"\(([A-Z]{2,6})\)", st.name)
        clean = _LEGAL.sub("", re.sub(r"\(.*?\)", "", st.name)).strip(" ,")
        if acronym:
            st.aliases.append((acronym.group(1), True))
        if len(clean) >= 3:
            st.aliases.append((clean, False))
        tokens = clean.split()
        for tok in tokens[:2]:
            tok = tok.strip(",")
            # Versalien-Kürzel zählen auch, wenn das Wort sonst generisch ist:
            # "SWISS" meint die Airline, "Swiss" im Fliesstext nicht.
            if len(tok) < 3 or (tok.lower() in _GENERIC_TOKENS and not tok.isupper()):
                continue
            st.aliases.append((tok, tok.isupper() or any(c.isdigit() for c in tok)))
            break
        stations.append(st)
    return stations


def cv_timeline_brief(stations: list[Station], today: date) -> str:
    """Kompakte Liste für den Prompt: welche Station ist abgeschlossen, welche läuft."""
    rows = []
    for st in stations:
        if st.ongoing:
            status = "LAUFEND → Präsens erlaubt"
        elif st.ended(today):
            status = f"ABGESCHLOSSEN ({st.end:%m/%Y}) → nur Präteritum/Perfekt, nie 'current/aktuell'"
        elif st.end:
            status = f"endet {st.end:%m/%Y}"
        else:
            status = "END-DATUM FEHLT IM CV → nicht als laufend darstellen"
        rows.append(f"- {st.name}: {status}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Befunde (§2, §3, §4, §5, §6, §10)
# ---------------------------------------------------------------------------

_ABBREVIATIONS = ("z. B.", "z.B.", "e.g.", "i.e.", "u. a.", "d. h.", "Dr.", "Prof.", "M.Sc.", "B.Sc.",
                  "bzw.", "etc.", "ca.", "Nr.", "vs.", "Sr.", "Jr.", "St.", "approx.")
_CONJUNCTION_START = {
    "en": r"(?:And|But|Or|Nor)\b",
    "de": r"(?:Und|Aber|Oder|Sowie)\b",
    "es": r"(?:Y|Pero|O|Ni)\b",
}
_SUMMARY_SENTENCE = re.compile(
    r"\b(both|these|all (?:of )?these|each of these) (experiences|roles|positions|stations)\b"
    r"|\breflects? (?:the|exactly the) (responsibilities|requirements)\b"
    r"|\b(beide[nr]?|diese[nr]?|all diese[nr]?) (Erfahrungen|Stationen|Tätigkeiten|Rollen|Positionen)\b"
    r"|\bentspr\w+ (genau )?(den|dem) (Anforderungen|Profil)\b"
    r"|\b(ambas|estas) experiencias\b",
    re.IGNORECASE,
)
_PRESENT_MARKERS = re.compile(
    r"\b(currently|current (?:role|position|job)|at present|I am (?:working|employed)|I work at"
    r"|aktuell|derzeit|momentan|zurzeit|gegenwärtig|jetzigen|aktuellen (?:Rolle|Position|Stelle)"
    r"|actualmente|puesto actual|rol actual)\b",
    re.IGNORECASE,
)
# Words that confirm ongoing enrolment in a closing paragraph. Institution
# names deliberately absent — that belongs in the profile, not in a rule.
_ENROLLMENT_CONFIRMED = re.compile(
    r"thesis|masterarbeit|abschlussarbeit|tesis|immatrikul|enrolled|enrolment|"
    r"enrollment|matricul|eingeschrieben|studiere|studying|master|bachelor|"
    r"msc|bsc|m\.sc|b\.sc",
    re.IGNORECASE,
)

_ENROLLMENT_REQUIRED = re.compile(
    r"immatrikul|eingeschrieben|Werkstudent|studentische|working student|student assistant"
    r"|currently enrolled|enrolled (?:in|at)|current(?:ly)? (?:a )?student|matricul|estudiante",
    re.IGNORECASE,
)


def split_sentences(paragraph: str) -> list[str]:
    masked = paragraph
    for abbr in _ABBREVIATIONS:
        masked = masked.replace(abbr, abbr.replace(".", "\u2024"))
    parts = re.split(r"(?<=[.!?])\s+(?=[\"„«(]?[A-ZÄÖÜ])", masked.strip())
    return [p.replace("\u2024", ".").strip() for p in parts if p.strip()]


def labelled_paragraphs(structure: dict) -> list[tuple[str, str]]:
    """(Bezeichnung, Text) — Bezeichnungen, die das Modell eindeutig zuordnen kann."""
    paras = [("Intro", structure.get("intro_paragraph") or "")]
    paras += [(f"Mittelabsatz {i}", p) for i, p in enumerate(structure.get("middle_paragraphs") or [], 1)]
    for sec in structure.get("sections") or []:
        paras.append((f"Sektion „{sec.get('heading', '')}“", " ".join([sec.get("heading", "")] + list(sec.get("bullets") or []))))
    paras.append(("Schlussabsatz", structure.get("closing_paragraph") or ""))
    return [(label, text) for label, text in paras if text.strip()]


def body_paragraphs(structure: dict) -> list[str]:
    """Intro, Mittelteil (Absätze oder je Sektion ein Block) und Schluss."""
    return [text for _label, text in labelled_paragraphs(structure)]


def word_count(structure: dict) -> int:
    return sum(len(re.findall(r"\b[\w’'-]+\b", p)) for p in body_paragraphs(structure))


# §10 Sprachen nur nennen, wenn die Anzeige sie verlangt. Deutsch und Englisch
# sind in der Schweiz Arbeitssprachen und bleiben aussen vor.
_LANGUAGE_NAMES = {
    "de": "deutsch|german|alemán|allemand|tedesco",
    "en": "englisch|english|inglés|anglais|inglese",
    "fr": "französisch|french|francés|français|francese",
    "it": "italienisch|italian|italiano|italien",
    "es": "spanisch|spanish|español|castellano|espagnol|spagnolo",
    "ca": "katalanisch|catalan|catalán|català",
    "pt": "portugiesisch|portuguese|portugués|portugais|portoghese",
    "rm": "rätoromanisch|rumantsch|romansh|romanche|romancio",
    "ja": "japanisch|japanese|japonés|japonais|giapponese",
}
_LANGUAGE_LABEL = {"fr": "Französisch", "it": "Italienisch", "es": "Spanisch", "ca": "Katalanisch",
                   "pt": "Portugiesisch", "rm": "Rätoromanisch", "ja": "Japanisch"}
_LANGUAGE_RE = {code: re.compile(rf"\b(?:{names})\w*", re.IGNORECASE) for code, names in _LANGUAGE_NAMES.items()}

# Der Brief ist das Anschreiben — es gehört nicht in die Liste der Anlagen.
_ATTACHMENT_SELF = re.compile(
    r"(Unterlagen|Anhang|Anlagen?|attached|enclosed|attachments?|adjunt\w*)[^.]*\b(Anschreiben|Motivationsschreiben|cover letter|carta de (?:presentación|motivación))\b"
    r"|\b(Anschreiben|Motivationsschreiben|cover letter|carta de (?:presentación|motivación))\b[^.]*(im Anhang|anbei|attached|enclosed|adjunt\w*)",
    re.IGNORECASE,
)

_SALUTATION = {
    "de": re.compile(r"^(Sehr geehrte|Liebe|Lieber|Guten Tag|Hallo|Grüezi|Geschätzte)", re.IGNORECASE),
    "en": re.compile(r"^(Dear|Hello|Hi|Good (morning|afternoon)|To whom)", re.IGNORECASE),
    "es": re.compile(r"^(Estimad|Querid|Hola|Buen[oa]s)", re.IGNORECASE),
}


def languages_in(text: str) -> set[str]:
    return {code for code, rx in _LANGUAGE_RE.items() if rx.search(text or "")}


def page_fit_finding(pages: int, overflow_lines: int) -> Optional[Finding]:
    """§2 Überlauf nur melden, nie kürzen lassen.

    Einen Kürzungsauftrag gibt es bewusst nicht: im Test 2026-09-17 strich das
    Modell von 490 auf 413 Wörter, und die Seite passte trotzdem nicht. Die
    Wortzahl steuert der Korridor, die Seite ist nur ein Hinweis.
    """
    if pages <= 1:
        return None
    return Finding("page_overflow", f"Läuft {overflow_lines} Zeile(n) auf Seite 2 über — akzeptiert (§2)",
                   repairable=False)


def check_letter(
    structure: dict,
    *,
    language: str,
    stations: list[Station],
    today: date,
    ad_text: str = "",
    ad_pensum: Optional[str] = None,
    check_length: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    paras = body_paragraphs(structure)
    closing = structure.get("closing_paragraph") or ""

    # §2 Länge und Absatzlänge
    if check_length:
        words = word_count(structure)
        if words < WORDS_MIN or words > WORDS_MAX:
            if words < WORDS_MIN:
                message = (f"{words} Wörter — zu kurz (Korridor {WORDS_MIN}–{WORDS_MAX}). "
                           f"Ergänze etwa {WORDS_TARGET - words} Wörter auf rund {WORDS_TARGET} — nur mit Inhalten, "
                           f"die im CV oder in der Anzeige stehen (z.B. eine noch nicht genannte CV-Station oder "
                           f"der Bezug einer Anforderung zu einem Beleg). Keine Details erfinden: keine Teamgrössen, "
                           f"Methoden, Tools oder Zahlen, die nicht im CV stehen. Lieber einen Absatz mehr als "
                           f"längere Absätze")
            else:
                message = (f"{words} Wörter — {words - WORDS_MAX} zu viel für {WORDS_MIN}–{WORDS_MAX}. "
                           f"Erst Redundanz streichen, dann verdichten, keine Belege streichen")
            findings.append(Finding("word_count", message))
    labelled = labelled_paragraphs(structure)
    for label, p in labelled:
        if label.startswith("Sektion"):
            continue
        lines = -(-len(p) // CHARS_PER_LINE)      # aufrunden: 7.2 geschätzte Zeilen sind gesetzt 8
        if lines > MAX_PARAGRAPH_LINES:
            findings.append(Finding("paragraph_too_long", f"{label} hat etwa {lines} Zeilen (max. {MAX_PARAGRAPH_LINES}) — in zwei Absätze teilen oder verdichten"))

    # §6 Dopplungen und Satzfragmente
    conj = re.compile(rf"^[\"„«(]?{_CONJUNCTION_START.get(language, _CONJUNCTION_START['en'])}")
    for label, p in labelled:
        sentences = split_sentences(p)
        for s in sentences:
            if conj.match(s):
                findings.append(Finding("fragment", f"{label}: Satz beginnt mit Konjunktion — „{s[:60]}…“"))
        norm = [re.sub(r"[^\w\s]", "", s.lower()).split() for s in sentences]
        for a in range(len(sentences)):
            for b in range(a + 1, len(sentences)):
                same_start = len(norm[a]) >= 5 and norm[a][:4] == norm[b][:4]
                similar = SequenceMatcher(None, " ".join(norm[a]), " ".join(norm[b])).ratio() >= 0.85
                if same_start or similar:
                    findings.append(Finding("duplicate_sentence",
                                            f"{label}: zwei fast gleiche Sätze — „{sentences[b][:60]}…“"))

    # §10 Zusammenfassungssätze und wiederholte Stationen
    for label, p in labelled:
        for m in _SUMMARY_SENTENCE.finditer(p):
            findings.append(Finding("summary_sentence", f"{label}: zusammenfassender Satz („{m.group(0)}“) — streichen"))
    if ad_text:
        allowed = languages_in(ad_text) | {"de", "en"}
        for label, p in labelled:
            for s in split_sentences(p):
                named = languages_in(s)
                extra = sorted(named - allowed)
                if len(named) >= 2 and extra:
                    names = ", ".join(_LANGUAGE_LABEL.get(c, c) for c in extra)
                    findings.append(Finding(
                        "language_list",
                        f"{label}: Sprachen-Aufzählung mit {names} — die Anzeige verlangt das nicht, streichen (§10)"))
    salutation = structure.get("salutation") or ""
    if salutation and not _SALUTATION.get(language, re.compile("")).search(salutation):
        findings.append(Finding("salutation", f"Anrede „{salutation}“ passt nicht zur Briefsprache ({language})"))
    for label, p in labelled:
        if _ATTACHMENT_SELF.search(p):
            findings.append(Finding(
                "attachments", f"{label}: Anlagen-Satz zählt das Anschreiben selbst auf — nur CV, Zeugnisse usw. nennen"))
    for st in stations:
        where = [label for label, p in labelled if st.mentioned_in(p)]
        if len(where) > 1:
            findings.append(Finding("station_repeated",
                                    f"{st.name} kommt vor in: {', '.join(where)} — nur an einer Stelle nennen, an den anderen streichen"))

    # §4 Zeitformen: abgeschlossene Stationen nie im Präsens
    ongoing = [st for st in stations if st.ongoing]
    for st in stations:
        if not st.ended(today):
            continue
        for p in paras:
            for s in split_sentences(p):
                if st.mentioned_in(s) and _PRESENT_MARKERS.search(s) and not any(o.mentioned_in(s) for o in ongoing):
                    findings.append(Finding("tense", f"{st.name} ist seit {st.end:%m/%Y} beendet, steht aber im Präsens: „{s[:70]}…“"))

    # §3 Pensum aus der Anzeige, bei Spanne exakt diese
    if ad_pensum:
        lo, _, hi = ad_pensum.partition("–")
        wanted = rf"\b{lo}\s*[–-]\s*{hi}\s*%" if hi else rf"\b{lo}\s*%"
        if not re.search(wanted, closing):
            findings.append(Finding("pensum", f"Schlussabsatz nennt das Pensum der Anzeige nicht ({ad_pensum} %)"))
        others = {m.group(0).replace("\u00a0", " ") for m in re.finditer(r"\b\d{2,3}(?:\s*[–-]\s*\d{2,3})?\s*%", closing)}
        if any(not re.fullmatch(wanted, o) for o in others):
            findings.append(Finding("pensum", f"Schlussabsatz nennt ein anderes Pensum als die Anzeige ({ad_pensum} %): {', '.join(sorted(others))}"))

    # §5 Immatrikulation bestätigen, wenn die Anzeige sie verlangt
    if ad_text and _ENROLLMENT_REQUIRED.search(ad_text):
        # A confirmation needs both a word that means "still enrolled" and a
        # year, so that "I am studying" alone does not satisfy the rule — the
        # posting wants a timeline, not a claim.
        if not (_ENROLLMENT_CONFIRMED.search(closing) and re.search(r"20\d\d", closing)):
            findings.append(Finding("enrollment", "Anzeige verlangt laufende Immatrikulation — im Schlussabsatz mit Zeitplan bestätigen"))

    # Hinweise, die nur der Verfasser beheben kann
    for st in stations:
        if st.missing_end:
            findings.append(Finding("cv_end_date", f"CV: End-Datum fehlt bei „{st.name}“ — bitte im CV-Markdown nachtragen", repairable=False))
    return findings


def validate_repo_url(url: Optional[str], allowed: list[str]) -> tuple[Optional[str], Optional[Finding]]:
    """§11 nur ein konfiguriertes, stellenspezifisches Repo — nie der Account."""
    if not url:
        return None, None
    clean = url.strip().rstrip("/")
    if re.fullmatch(r"https?://(www\.)?github\.com/[^/]+", clean):
        return None, Finding("repo", f"Repo-Link entfernt: {clean} ist der GitHub-Account, kein Repo", repairable=False)
    if clean not in {a.strip().rstrip("/") for a in allowed}:
        return None, Finding("repo", f"Repo-Link entfernt: {clean} steht nicht in cover_letter.repos", repairable=False)
    return clean, None


def company_block(company: str, location: str) -> list[str]:
    """§1 Firmenblock nur mit Firmenname UND Ort aus der Anzeige — nie erfunden."""
    name = (company or "").split(" / ")[0].strip()
    loc = re.sub(r"^[A-Z]{2}\s*-\s*", "", (location or "").strip())
    if not name or not loc:
        return []
    if re.search(r"remote|hybrid|\d+\s+locations?|multiple|various|anywhere|worldwide|europe\b", loc, re.IGNORECASE):
        return []
    parts: list[str] = []
    for part in (p.strip() for p in loc.split(",")):
        if part and part.lower() not in {x.lower() for x in parts}:
            parts.append(part)
    return [name, ", ".join(parts)]
