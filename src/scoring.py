"""
Relevance scoring engine for job listings.

Scoring Dimensions (weights defined in config/profile.yaml):
  workload_match   — Pensum-Passung (60-80% ideal)
  location_match   — Zürich > Barcelona/Remote > Schweiz > Andere
  company_tier     — Tier 1/2/3 aus dem Zielfirmen-System
  domain_match     — PropTech, FinTech, ClimaTech, AI/ML
  profile_match    — PM-Rolle (★★★) > AI Solutions (★★☆) > Innovation (★☆☆)
  remote_option    — Remote/Hybrid erwähnt
  mentoring        — Career Development erwähnt
  thesis_opportunity — Masterarbeit-Option
  skill_match      — Overlap mit Tech Skills aus dem CV

Plus Sprachboosts (je Profil konfiguriert) und Keyword-Penalties
(Senior, 10+ years, Director etc.)
"""

import logging
import re
from typing import Optional

from src.experience_extractor import unescape_markdown
from src.models import Job

logger = logging.getLogger(__name__)


class JobScorer:
    """Scores jobs 0.0–1.0 based on profile and preferences from config."""

    def __init__(self, config: dict):
        self.weights = config["preferences"]["weights"]
        self.profile = config["profile"]
        self.prefs = config["preferences"]
        self.search = config["search"]

        # --- Company tiers ---
        tiers = self.prefs.get("company_tiers", {})
        self._tier_1 = [c.lower() for c in tiers.get("tier_1", [])]
        self._tier_2 = [c.lower() for c in tiers.get("tier_2", [])]
        self._tier_3 = [c.lower() for c in tiers.get("tier_3", [])]

        # --- Rollenfamilien (2026-09-14): geordnete Liste mit eigenem Score ---
        # Neues Format, weil die alten vier festen Kategorien (pm_primary …)
        # eine geordnete Prioritätenliste nicht abbilden. Erster
        # Treffer in Listenreihenfolge gewinnt — "AI Solutions Engineer" landet
        # damit bei Familie 1, nicht bei "Solutions Engineer" (Familie 2).
        self._role_families = [
            (
                str(fam.get("name", f"family_{i}")),
                float(fam.get("score", 0.5)),
                _compile_patterns(fam.get("titles") or []),
            )
            for i, fam in enumerate(self.prefs.get("role_families") or [])
        ]

        # --- Profile priority keywords (legacy: four fixed categories) ---
        pk = self.prefs.get("profile_keywords", {})
        self._pm_keywords     = [k.lower() for k in pk.get("pm_primary", [])]
        self._ve_keywords     = [k.lower() for k in pk.get("value_engineer", [])]
        self._ai_keywords     = [k.lower() for k in pk.get("ai_solutions", [])]
        self._innovation_keywords = [k.lower() for k in pk.get("innovation_venture", [])]

        # --- Title-Penalty-Konfiguration (für reine Engineering-Rollen) ---
        tp = self.prefs.get("title_penalties", {}).get("engineering_only", {})
        self._eng_only_patterns = [p.lower() for p in tp.get("patterns", [])]
        self._eng_rescue_kw     = [k.lower() for k in tp.get("rescue_keywords", [])]
        self._eng_penalty       = float(tp.get("penalty", 0.0))

        # --- Domain config ---
        self._domains = self.prefs.get("domains", {})
        # Vorkompiliert, weil _score_domain über alle Domains × alle Keywords
        # läuft — bei ~13 Domains und ~300 Keywords pro Job sonst spürbar.
        self._domain_patterns = {
            name: _compile_patterns(cfg.get("keywords", []))
            for name, cfg in self._domains.items()
        }
        # Domain-Treffer, die nur in der Description stehen, zählen abgeschwächt:
        # "wir achten auf sustainability" im ESG-Absatz eines Konzern-Inserats
        # ist kein Branchen-Signal. Gleiche Logik wie _score_profile_match.
        self._domain_desc_factor = float(
            self.prefs.get("domain_description_factor", 0.75)
        )
        # Score für "keine Zieldomäne erkannt". Default 0.3 wie bisher; ein
        # Profil mit wenigen, engen Domänen setzt 0.5 — sonst bestraft es
        # alles, was nicht zufällig ein Domänen-Keyword nennt.
        self._domain_neutral = float(self.prefs.get("domain_neutral_score", 0.3))

        # --- Skill-Match: ab wie vielen Treffern volle Punktzahl? ---
        self._skill_full_marks_at = max(
            1, int((self.prefs.get("skill_match") or {}).get("full_marks_at", 3))
        )

        # --- Language boost config ---
        self._lang_boosts = self.prefs.get("language_boost", {})

        # --- General boost / avoid patterns ---
        self._boost_patterns = _compile_patterns(self.prefs.get("boost_keywords", []))
        avoid_kws = self.prefs.get("avoid_keywords", [])
        self._avoid_patterns = _compile_patterns(avoid_kws)
        # "Soft" Seniority-Wörter — werden bei senior-exempt-Rollen ignoriert
        SOFT_SENIORITY = {"senior", "lead"}
        self._avoid_patterns_relaxed = _compile_patterns(
            [k for k in avoid_kws if k.lower() not in SOFT_SENIORITY]
        )

        # Title-Patterns wo "senior"/"lead" KEINE Penalty geben sollen
        self._senior_exempt_patterns = [
            p.lower() for p in self.prefs.get("senior_exempt_patterns", [])
        ]

        # --- Experience-Penalty (strukturiert, aus jobs.min_years_experience) ---
        # Ergänzt die Keyword-Penalties: greift auch wenn "Senior" nirgends
        # steht, die Stelle aber explizit N+ Jahre verlangt.
        ep = self.prefs.get("experience_penalty", {})
        self._exp_target_max = int(ep.get("target_max_years", 3))
        self._exp_per_year   = float(ep.get("per_year", 0.06))
        self._exp_cap        = float(ep.get("cap", 0.30))

        # --- Skill patterns (handle both flat list and proficient/intermediate/basic dict) ---
        skills_raw = self.profile.get("skills", {}).get("technical", [])
        all_skills: list[str] = []
        if isinstance(skills_raw, dict):
            for level_skills in skills_raw.values():
                all_skills.extend(level_skills)
        elif isinstance(skills_raw, list):
            all_skills = skills_raw
        # `profile.skills.technical` ist die CV-Liste und geht so auch in die
        # LLM-Prompts — die bleibt unangetastet. Fürs Scoring kommen Methoden-
        # und Tool-Begriffe dazu, die Inserate tatsächlich nennen: die CV-Liste
        # allein matchte auf 65 % der Stellen kein einziges Mal, weil PM- und
        # Consulting-Inserate kein Canva oder NX CAD erwähnen.
        all_skills += list((self.prefs.get("skill_match") or {}).get("extra_keywords") or [])
        self._skill_patterns = _compile_patterns(all_skills)

        # --- Location ---
        self._target_location = self.search.get("location", "").lower()
        self._secondary_location = self.search.get("location_secondary", "").lower()
        self._target_cantons = {c.upper() for c in self.search.get("cantons", [])}

        # --- Workload ---
        # preferred_percent ist die Präferenz fürs Scoring; max_percent geht
        # als Suchfilter an jobs.ch. Früher war beides dasselbe Feld — mit
        # max_percent: 80 kamen 100-%-Stellen von jobs.ch gar nicht erst rein.
        workload_cfg = self.search.get("workload", {})
        self._ideal_workload = workload_cfg.get("preferred_percent", workload_cfg.get("max_percent", 80))

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def score(self, job: Job) -> float:
        """Return a 0.0–1.0 relevance score for a job."""
        raw = self._raw_scores(job)

        weighted = sum(
            raw.get(k, 0.0) * self.weights.get(k, 0.0)
            for k in self.weights
        )

        boost = self._calc_boost(job)
        lang_boost = self._calc_language_boost(job)
        penalty = self._calc_penalty(job)
        exp_penalty = self._calc_experience_penalty(job)

        return round(max(0.0, min(1.0, weighted + boost + lang_boost - penalty - exp_penalty)), 3)

    def score_breakdown(self, job: Job) -> dict:
        """
        Return a detailed score breakdown — useful for debugging and the
        learning notebook.

        Returns dict with keys:
          raw_scores, weighted_scores, boost, language_boost, penalty, final_score
        """
        raw = self._raw_scores(job)
        weighted = {k: raw[k] * self.weights.get(k, 0.0) for k in raw}

        boost = self._calc_boost(job)
        lang_boost = self._calc_language_boost(job)
        penalty = self._calc_penalty(job)
        exp_penalty = self._calc_experience_penalty(job)
        final = round(max(0.0, min(1.0, sum(weighted.values()) + boost + lang_boost - penalty - exp_penalty)), 3)

        return {
            "raw_scores": raw,
            "weighted_scores": weighted,
            "boost": round(boost, 3),
            "language_boost": round(lang_boost, 3),
            "penalty": round(penalty, 3),
            "experience_penalty": round(exp_penalty, 3),
            "final_score": final,
        }

    # ------------------------------------------------------------------ #
    #  Orchestration                                                       #
    # ------------------------------------------------------------------ #

    def _raw_scores(self, job: Job) -> dict:
        return {
            "workload_match": self._score_workload(job),
            "location_match": self._score_location(job),
            "company_tier": self._score_company_tier(job),
            "domain_match": self._score_domain(job),
            "profile_match": self._score_profile_match(job),
            "remote_option": self._score_remote(job),
            "mentoring": self._score_keywords(job, [
                "mentoring", "career development", "coaching",
                "entwicklung", "förderung", "weiterbildung",
                "learning & development", "l&d",
            ]),
            "thesis_opportunity": self._score_keywords(job, [
                "thesis", "masterarbeit", "master thesis",
                "abschlussarbeit", "bachelor thesis",
            ]),
            "skill_match": self._score_skills(job),
        }

    # ------------------------------------------------------------------ #
    #  Individual scoring functions                                        #
    # ------------------------------------------------------------------ #

    def _score_workload(self, job: Job) -> float:
        """
        Score how close the job's Pensum is to the ideal (80%).
        Swiss-specific: Pensum is often listed as 60%, 80%, 100% etc.
        """
        if job.workload_percent is None:
            return 0.5  # Unknown → neutral
        diff = job.workload_percent - self._ideal_workload
        if diff == 0:
            return 1.0
        if abs(diff) <= 10:
            return 0.8
        if diff > 0:
            # Mehr Pensum als gewünscht (z.B. 100 % bei Präferenz 80 %) ist
            # nie schlechter als "unbekannt" — 80 % ist Bonus, 100 % kein Malus.
            return 0.5
        if diff >= -20:
            return 0.5
        return 0.2

    def _score_location(self, job: Job) -> float:
        """
        Zürich area = 1.0 | Barcelona/Remote = 0.7 | Rest Schweiz = 0.4 | Andere = 0.2
        """
        loc = (job.location or "").lower()
        # Primary: Zürich
        if self._target_location and self._target_location in loc:
            return 1.0
        if job.canton and job.canton.upper() in self._target_cantons:
            return 0.85
        zurich_area = {"winterthur", "baden", "zug", "uster", "dietikon",
                       "dübendorf", "kloten", "schlieren", "regensdorf"}
        if any(city in loc for city in zurich_area):
            return 0.75
        # Secondary: Barcelona or Remote
        if self._secondary_location and self._secondary_location in loc:
            return 0.7
        text = self._get_searchable_text(job)
        if "remote" in text or "hybrid" in text:
            return 0.65
        # Elsewhere in Switzerland
        if loc:
            return 0.3
        return 0.4  # Location unknown

    def _score_remote(self, job: Job) -> float:
        if job.is_remote is True:
            return 1.0
        text = self._get_searchable_text(job)
        if any(t in text for t in ["remote", "home office", "homeoffice",
                                    "hybrid", "flexible arbeitsort"]):
            return 0.8
        return 0.3

    def _score_company_tier(self, job: Job) -> float:
        """
        Score based on the 29 target companies from the Job Search Matrix.
        Tier 1 = strong fit (8 companies), Tier 2 = good fit (10), Tier 3 = wildcard (7).
        Unknown companies score 0.3 — not zero, since great unknowns exist.
        """
        company = (job.company or "").lower().strip()
        if any(t in company for t in self._tier_1):
            return 1.0
        if any(t in company for t in self._tier_2):
            return 0.7
        if any(t in company for t in self._tier_3):
            return 0.45
        return 0.3

    def _score_domain(self, job: Job) -> float:
        """
        Score based on industry domain match.
        Ranking (from profile): PropTech/ArchTech > FinTech > ClimaTech > AI > Consulting

        Zwei Korrekturen gegenüber der ersten Fassung (2026-08-28):

        * **Wortgrenzen statt Substring.** Vorher matchte "dac" (Direct Air
          Capture) auf "DACH-Region" — 149 von 277 Treffern im Korpus —, "pv"
          auf "NPV"/"TPV", "grid" auf "sendgrid"/"integridad". Ergebnis: 38 %
          aller Jobs bekamen den Höchstwert 1.0.
        * **Titel schlägt Description.** Ein Branchen-Keyword im Titel oder
          Firmennamen ist ein Signal; dasselbe Wort irgendwo in einer 4000
          Zeichen langen Description ist oft Boilerplate.
        """
        title_text = " ".join([job.title or "", job.company or ""]).lower()
        full_text = self._get_searchable_text(job)

        best = 0.0
        for name, domain_cfg in self._domains.items():
            domain_score = domain_cfg.get("score", 0.5)
            if domain_score * self._domain_desc_factor <= best:
                continue  # kann den bisherigen Bestwert ohnehin nicht schlagen
            patterns = self._domain_patterns.get(name, [])
            if any(p.search(title_text) for p in patterns):
                best = max(best, domain_score)
            elif any(p.search(full_text) for p in patterns):
                best = max(best, domain_score * self._domain_desc_factor)
        return round(best, 3) if best > 0 else self._domain_neutral

    def _score_profile_match(self, job: Job) -> float:
        """
        How well does the job title match the four search profiles?

        Wichtig: Wir matchen primär gegen den TITEL, nicht die Description.
        Sonst kriegt jeder Backend-Job mit "we use Product Management tools"
        einen falschen 1.0-Score.
        """
        title = (job.title or "").lower()
        text  = self._get_searchable_text(job)

        if self._role_families:
            return self._score_role_families(title, text)

        # Title-Match wiegt mehr als Description-Match
        if any(kw in title for kw in self._pm_keywords):
            return 1.0    # ★★★ Primary: Product Manager / Owner
        if any(kw in title for kw in self._ve_keywords):
            return 0.95   # ★★★ Primary: Value Engineer / Solutions Eng
        if any(kw in title for kw in self._ai_keywords):
            return 0.7    # ★★☆ Secondary: AI Solutions / Consultant
        if any(kw in title for kw in self._innovation_keywords):
            return 0.5    # ★☆☆ Opportunistic: Innovation / Venture

        # Wenn nicht im Titel, aber stark in der Description → halbes Gewicht
        if any(kw in text for kw in self._pm_keywords + self._ve_keywords):
            return 0.4
        if any(kw in text for kw in self._ai_keywords):
            return 0.25

        return 0.0  # Komplett kein Profil-Match (vorher: 0.15 — zu generös)

    def role_family(self, job: Job) -> Optional[str]:
        """Name der Rollenfamilie, deren Titel-Muster trifft — None ohne Titeltreffer."""
        title = (job.title or "").lower()
        for name, _score, patterns in self._role_families:
            if any(p.search(title) for p in patterns):
                return name
        return None

    def _score_role_families(self, title: str, text: str) -> float:
        """Titel gegen die geordneten Rollenfamilien; Description nur als Rest.

        Gleiche Abstufung wie die Legacy-Logik: ein Treffer nur in der
        Description zählt 0.4 für die beiden obersten Familien, 0.25 für den
        Rest — ein Backend-Inserat, das "our data engineers" erwähnt, ist
        noch keine Data-Engineer-Stelle.
        """
        for _name, score, patterns in self._role_families:
            if any(p.search(title) for p in patterns):
                return score
        for rank, (_name, _score, patterns) in enumerate(self._role_families):
            if any(p.search(text) for p in patterns):
                return 0.4 if rank < 2 else 0.25
        return 0.0

    def _score_keywords(self, job: Job, keywords: list[str]) -> float:
        """Generic keyword presence scorer (used for mentoring, thesis etc.)."""
        text = self._get_searchable_text(job)
        matches = sum(1 for kw in keywords if kw.lower() in text)
        if matches >= 2:
            return 1.0
        if matches == 1:
            return 0.7
        return 0.0

    def _score_skills(self, job: Job) -> float:
        """Match job description against technical skills from the CV."""
        # Ohne Description lässt sich nichts messen → neutral, nicht 0. Sonst
        # stünde eine Stelle ohne Text besser da als eine mit Text, die zufällig
        # keinen Begriff nennt.
        if not (job.description or "").strip() or not self._skill_patterns:
            return 0.5
        text = self._get_searchable_text(job)
        matches = sum(1 for p in self._skill_patterns if p.search(text))
        # Absolutes Ziel statt Anteil an der Skill-Liste. Der alte Divisor war
        # len(self._skill_patterns): bei einer CV-Liste von knapp zwanzig Skills
        # brauchte es sechs Treffer für 1.0 — unerreichbar, weil Inserate die
        # Randbereiche eines CVs (Büro- und Designwerkzeuge, CAD) gar nicht
        # nennen. Gemessen matchte nur ein Drittel der Liste überhaupt je, der
        # Median lag bei 0.00. Drei Treffer ("Python, SQL, Data Science") sind
        # für ein Junior-Profil volle Punktzahl.
        return round(min(1.0, matches / self._skill_full_marks_at), 3)

    # ------------------------------------------------------------------ #
    #  Boost & Penalty                                                     #
    # ------------------------------------------------------------------ #

    def _calc_boost(self, job: Job) -> float:
        """Small additive boost for positive signal keywords (junior, startup, etc.)."""
        text = self._get_searchable_text(job)
        matches = sum(1 for p in self._boost_patterns if p.search(text))
        return min(0.12, matches * 0.02)

    def _calc_language_boost(self, job: Job) -> float:
        """
        Boost when a job explicitly requires a language from the profile.

        Configured per profile under ``preferences.language_boost``: each entry
        names the keywords that signal the requirement and how much it is worth.
        Size the boost by actual proficiency — a language listed at A1 should be
        worth a fraction of one you work in, or the score rewards a word in a CV
        rather than an ability.
        """
        text = self._get_searchable_text(job)
        total = 0.0
        for lang_cfg in self._lang_boosts.values():
            keywords = lang_cfg.get("keywords", [])
            boost = lang_cfg.get("boost", 0.0)
            if any(kw.lower() in text for kw in keywords):
                total += boost
        return min(0.12, total)

    def _calc_penalty(self, job: Job) -> float:
        """
        Penalty für drei Signale:
          1. Seniority im Titel ("Senior", "Director" etc.) — am stärksten
          2. Seniority in der Description — schwächer
          3. Reine Engineering-Titel ("Backend Engineer" etc.) ohne PM/Value-Bezug

        Senior-Exemption:
          Bei Value-Engineer/Solutions-Consultant-Titeln werden "senior"/"lead"
          NICHT penalisiert (Generalist-Profil passt zu Senior-Roles dieser Art).
          "Hard"-Seniority (Director/Head/VP/Principal) bleibt aber penalisiert.

        Title-Level wiegt mehr als Description-Level.
        """
        title = (job.title or "").lower()
        text = self._get_searchable_text(job)

        is_senior_exempt = any(p in title for p in self._senior_exempt_patterns)
        avoid_for_title = self._avoid_patterns_relaxed if is_senior_exempt else self._avoid_patterns

        title_hits = sum(1 for p in avoid_for_title if p.search(title))
        desc_hits  = sum(1 for p in self._avoid_patterns if p.search(text))
        # Multipliers leicht erhöht 2026-05-27 — Senior/Lead-Stellen sollen
        # härter rausfallen für Junior-Profile (war 0.15/0.04).
        seniority_penalty = title_hits * 0.22 + desc_hits * 0.05

        # Engineering-Only-Penalty: subtrahiert vollen Wert wenn Titel rein
        # technisch ist UND keine "Rettungs-Wörter" (product/value/innovation/ai) hat.
        eng_penalty = 0.0
        if self._eng_only_patterns and self._eng_penalty > 0:
            is_eng_only = any(p in title for p in self._eng_only_patterns)
            has_rescue  = any(kw in title for kw in self._eng_rescue_kw)
            if is_eng_only and not has_rescue:
                eng_penalty = self._eng_penalty

        return min(0.45, seniority_penalty + eng_penalty)

    def _calc_experience_penalty(self, job: Job) -> float:
        """Linear penalty when the posting explicitly demands more years of
        experience than the profile targets.

        Uses the structured ``min_years_experience`` field (regex/Haiku
        extraction at scrape time). Unknown (None) stays penalty-free — the
        keyword penalties and the Haiku triager remain the safety net there.
        """
        years = job.min_years_experience
        if years is None or years <= self._exp_target_max:
            return 0.0
        return min(self._exp_cap, (years - self._exp_target_max) * self._exp_per_year)

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_searchable_text(job: Job) -> str:
        # Markdown-Escapes entfernen, sonst trifft die Penalty "5+ years" nie
        # auf LinkedIn/Indeed-Text ("5\+ years").
        return unescape_markdown(
            " ".join([job.title or "", job.description or "", job.company or ""])
        ).lower()


def _compile_patterns(keywords: list[str]) -> list[re.Pattern]:
    r"""Compile keyword strings into anchored regex patterns.

    Two things the naive ``\b`` + keyword + ``\b`` version got wrong:

    1. ``\b`` only asserts a boundary *next to a word character*. Appending it
       to a keyword that ends in punctuation makes the pattern unmatchable:
       ``\bc\+\+\b`` never fires, because after "++" the regex demands a word
       char where real text has a space. Same for "sr.". We therefore anchor
       each side only when the adjacent keyword character is alphanumeric.
    2. German compounds, which grow in both directions — "Gebäudetechnik" puts
       the stem first, "Unternehmensberatung" puts it last. Measured on the
       corpus, boundaries drop `gebäude` from 24 hits to 5. A ``*`` in the
       config drops the anchor on that side, so the YAML can say which it means:

           "gebäude*"    → Gebäude, Gebäudetechnik      (offen nach rechts)
           "*beratung"   → Beratung, Unternehmensberatung (offen nach links)
           "*berater*"   → Berater, Beraterin, Unternehmensberater

    3. Gender-Formen. Ein ``*`` *innerhalb* eines Wortes ist der Genderstern,
       kein Platzhalter: ``"wissenschaftliche*r mitarbeiter*in"`` matcht
       Wissenschaftliche Mitarbeiterin, Wissenschaftlicher Mitarbeiter,
       wissenschaftliche:r Mitarbeiter:in, Mitarbeiter/in, Mitarbeiterinnen.
       Leerzeichen im Keyword matchen beliebigen Whitespace (Zeilenumbrüche
       in gescrapten Descriptions).
    """
    patterns = []
    for raw in keywords:
        kw = (raw or "").lower().strip()
        if not kw:
            continue
        open_left = kw.startswith("*")
        open_right = kw.endswith("*")
        kw = kw.strip("*")
        if not kw:
            continue
        left = "" if open_left else (r"\b" if kw[0].isalnum() else "")
        right = "" if open_right else (r"\b" if kw[-1].isalnum() else "")
        try:
            patterns.append(re.compile(left + _keyword_body(kw) + right))
        except re.error:
            patterns.append(re.compile(re.escape(kw)))
    return patterns


# Genderstern plus die gängigen Alternativen, die in Inseraten tatsächlich stehen.
_GENDER_MARK = r"[*:/_]?"


def _keyword_body(kw: str) -> str:
    """Regex-Körper eines Keywords: escaped, Genderstern und Whitespace flexibel.

    Jedes innere ``*`` macht das direkt folgende Buchstaben-Suffix optional,
    samt beliebigem Gender-Zeichen davor. Beim Suffix "in" ist auch die
    Pluralform "innen" erlaubt.
    """
    head, *tails = kw.split("*")
    body = re.escape(head)
    for tail in tails:
        m = re.match(r"[^\W\d_]+", tail)
        suffix = m.group(0) if m else ""
        rest = tail[len(suffix):]
        suffix_re = r"in(?:nen)?" if suffix == "in" else re.escape(suffix)
        body += f"(?:{_GENDER_MARK}{suffix_re})?" + re.escape(rest)
    # re.escape(" ") ergibt "\ " — durch flexiblen Whitespace ersetzen.
    return body.replace("\\ ", r"\s+")
